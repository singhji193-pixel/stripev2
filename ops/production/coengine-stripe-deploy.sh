#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'
umask 077

readonly COENGINE_DIR="/opt/coengine"
readonly COMPOSE_FILE="${COENGINE_DIR}/docker-compose.yml"
readonly SITE="coengine"
readonly PUBLIC_URL="https://next.coengine.ai"
readonly IMAGE_REPOSITORY="coengine-erpnext"
readonly RELEASES_DIR="${COENGINE_DIR}/releases"
readonly RUNTIME_DIR="${COENGINE_DIR}/runtime"
readonly RUNTIME_ASSETS_DIR="${COENGINE_DIR}/runtime-assets"
readonly APPS_VOLUME="coengine_apps"
readonly SITES_VOLUME="coengine_sites"
readonly OVERLAY_MANIFEST="${COENGINE_OVERLAY_MANIFEST:-/etc/coengine/runtime-overlays.sha256}"
readonly LOCK_FILE="/var/lock/coengine-stripe-deploy.lock"
readonly EXPECTED_APP_COUNT=12
readonly EXPECTED_WORKER_COUNT=2
readonly EXPECTED_SENTRY_VERSION="1.45.1"
readonly MIN_FREE_GB="${COENGINE_MIN_FREE_GB:-20}"
readonly STOP_TIMEOUT="${COENGINE_STOP_TIMEOUT:-60}"

readonly -a APP_SERVICES=(
	backend
	frontend
	websocket
	queue-short
	queue-long
	scheduler
)
readonly -a PROTECTED_SERVICES=(
	db
	redis-cache
	redis-queue
	redis-socketio
)
readonly -a EXPECTED_OVERLAY_FILES=(
	frappe-app.py
	frappe-background_jobs.py
	frappe-sentry.py
	verify_frappe_assets.py
)
readonly -a EXPECTED_APPS=(
	crm
	drive
	erpnext
	frappe
	helpdesk
	hrms
	insights
	payments
	print_designer
	stripe_integration
	telephony
	twilio_integration
)

COMMAND="${1:-help}"
if (( $# > 0 )); then
	shift
fi

TARGET_COMMIT=""
SOURCE_ARCHIVE=""
SOURCE_ARCHIVE_SHA256=""
RELEASE_ID=""
CONFIRM_VALUE=""
ACKNOWLEDGE_DB_NOT_RESTORED=0
DEPLOY_NOOP=0

CURRENT_IMAGE_ID=""
CURRENT_COMMIT=""
CURRENT_APPS_DIGEST=""
CURRENT_ASSETS_DIGEST=""
APPS_VOLUME_PATH=""
SITES_VOLUME_PATH=""
ACTIVE_RELEASE_DIR=""
CANDIDATE_IMAGE_TAG=""
ROLLBACK_IMAGE_TAG=""
CUTOVER_STARTED=0
LIVE_CODE_SWITCHED=0
MIGRATION_STARTED=0
LOCK_HELD=0
WORKER_TERM_SENT=0
WORKERS_DRAINED=0
declare -A PROTECTED_CONTAINER_IDS=()
SELF_TEST_TEMPORARY=""

usage() {
	cat <<'EOF'
Coengine Stripe production deployment tool

Usage:
  coengine-stripe-deploy status
  coengine-stripe-deploy preflight --commit SHA --archive FILE --sha256 SHA256
  coengine-stripe-deploy prepare --commit SHA --archive FILE --sha256 SHA256
  coengine-stripe-deploy activate --release RELEASE_ID --confirm SHA
  coengine-stripe-deploy deploy --commit SHA --archive FILE --sha256 SHA256 --confirm SHA
  coengine-stripe-deploy rollback --release RELEASE_ID --confirm RELEASE_ID \
    --acknowledge-db-not-restored
  coengine-stripe-deploy self-test

prepare builds and validates a candidate while production remains online.
activate performs the short maintenance-window cutover.
deploy runs prepare and activate together.

Rollback restores the prior image, Stripe app, and durable build definition. It
does not restore the database or site files. The release backup is retained for
an operator-led data restore when a migration is not backward compatible.
EOF
}

log() {
	printf '%s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

warn() {
	log "WARNING: $*" >&2
}

die() {
	log "ERROR: $*" >&2
	exit 1
}

parse_options() {
	while (( $# > 0 )); do
		case "$1" in
			--commit)
				(( $# >= 2 )) || die "--commit requires a value"
				TARGET_COMMIT="$2"
				shift 2
				;;
			--archive)
				(( $# >= 2 )) || die "--archive requires a value"
				SOURCE_ARCHIVE="$2"
				shift 2
				;;
			--sha256)
				(( $# >= 2 )) || die "--sha256 requires a value"
				SOURCE_ARCHIVE_SHA256="$2"
				shift 2
				;;
			--release)
				(( $# >= 2 )) || die "--release requires a value"
				RELEASE_ID="$2"
				shift 2
				;;
			--confirm)
				(( $# >= 2 )) || die "--confirm requires a value"
				CONFIRM_VALUE="$2"
				shift 2
				;;
			--acknowledge-db-not-restored)
				ACKNOWLEDGE_DB_NOT_RESTORED=1
				shift
				;;
			-h|--help)
				usage
				exit 0
				;;
			*)
				die "unknown option: $1"
				;;
		esac
	done
}

require_commands() {
	local command_name
	for command_name in "$@"; do
		command -v "$command_name" >/dev/null 2>&1 || die "required command is missing: ${command_name}"
	done
}

require_operator_runtime() {
	[[ "${EUID}" -eq 0 ]] || die "run this command as root"
	[[ "${STOP_TIMEOUT}" =~ ^[1-9][0-9]*$ ]] || die "COENGINE_STOP_TIMEOUT must be a positive integer"
	require_commands \
		awk basename chown cp curl date df diff dirname docker find flock grep gzip head install \
		ln mktemp mv python3 realpath rm sed seq sha256sum sort stat tar timeout touch wc xargs
	[[ -d "${COENGINE_DIR}" ]] || die "missing deployment directory: ${COENGINE_DIR}"
	[[ -f "${COMPOSE_FILE}" ]] || die "missing Compose file: ${COMPOSE_FILE}"
	[[ -f "${OVERLAY_MANIFEST}" ]] || die "missing overlay manifest: ${OVERLAY_MANIFEST}"
	docker compose version >/dev/null
}

acquire_lock() {
	if [[ "${LOCK_HELD}" -eq 1 ]]; then
		return 0
	fi
	exec 9>"${LOCK_FILE}"
	flock -n 9 || die "another Coengine deployment is already running"
	LOCK_HELD=1
}

compose() {
	docker compose --project-directory "${COENGINE_DIR}" -f "${COMPOSE_FILE}" "$@"
}

service_container() {
	local service="$1"
	local container_id
	container_id="$(compose ps -a -q "${service}" | head -n 1)"
	[[ -n "${container_id}" ]] || die "missing Compose container for service: ${service}"
	printf '%s\n' "${container_id}"
}

validate_commit() {
	local commit="$1"
	[[ "${commit}" =~ ^[0-9a-f]{40}$ ]] || die "commit must be a lowercase, full 40-character SHA"
}

validate_sha256() {
	local digest="$1"
	[[ "${digest}" =~ ^[0-9a-f]{64}$ ]] || die "SHA-256 must be 64 lowercase hexadecimal characters"
}

validate_release_id() {
	local value="$1"
	[[ "${value}" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$ ]] || die "invalid release ID: ${value}"
}

release_path() {
	local value="$1"
	local resolved
	validate_release_id "${value}"
	resolved="$(realpath -m "${RELEASES_DIR}/${value}")"
	[[ "$(dirname "${resolved}")" == "$(realpath -m "${RELEASES_DIR}")" ]] \
		|| die "release path escapes ${RELEASES_DIR}"
	printf '%s\n' "${resolved}"
}

assert_exact_path() {
	local actual="$1"
	local expected="$2"
	[[ "$(realpath -m "${actual}")" == "$(realpath -m "${expected}")" ]] \
		|| die "refusing unsafe path: ${actual}"
}

validate_archive() {
	local archive="$1"
	local expected_digest="$2"
	local expected_commit="$3"
	local actual_digest

	[[ -f "${archive}" ]] || die "release archive does not exist: ${archive}"
	validate_sha256 "${expected_digest}"
	validate_commit "${expected_commit}"
	actual_digest="$(sha256sum "${archive}" | awk '{print $1}')"
	[[ "${actual_digest}" == "${expected_digest}" ]] \
		|| die "release archive checksum mismatch"

	python3 - "${archive}" "${expected_commit}" <<'PY'
import posixpath
import re
import sys
import tarfile
from pathlib import PurePosixPath

archive, expected_commit = sys.argv[1:3]
required = {
    "stripe_integration/pyproject.toml",
    "stripe_integration/stripe_integration/hooks.py",
    "stripe_integration/stripe_integration/public/js/subscription_stripe.js",
    "stripe_integration/stripe_integration/stripe_integration/verify_post_upgrade.py",
}
seen = set()

with tarfile.open(archive, "r:gz") as bundle:
    archive_commit = bundle.pax_headers.get("comment")
    if archive_commit is None:
        raise SystemExit("archive is missing Git commit metadata")
    if not re.fullmatch(r"[0-9a-f]{40}", archive_commit):
        raise SystemExit("archive has malformed Git commit metadata")
    if archive_commit != expected_commit:
        raise SystemExit("release archive commit does not match --commit")
    members = bundle.getmembers()
    if not members:
        raise SystemExit("archive is empty")
    for member in members:
        name = member.name.rstrip("/")
        path = PurePosixPath(name)
        if not name or path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"unsafe archive member: {member.name!r}")
        if any(ord(character) < 32 or ord(character) == 127 for character in name):
            raise SystemExit("archive member contains a control character")
        if path.parts[0] != "stripe_integration":
            raise SystemExit(f"unexpected archive prefix: {member.name!r}")
        if member.isdev() or member.isfifo():
            raise SystemExit(f"unsupported archive member type: {member.name!r}")
        if member.issym():
            target = posixpath.normpath(posixpath.join(posixpath.dirname(name), member.linkname))
            target_path = PurePosixPath(target)
            if target_path.is_absolute() or ".." in target_path.parts or target_path.parts[0] != "stripe_integration":
                raise SystemExit(f"unsafe symlink target: {member.name!r}")
        if member.islnk():
            raise SystemExit(f"hard links are not allowed: {member.name!r}")
        seen.add(name)

missing = sorted(required - seen)
if missing:
    raise SystemExit("archive is missing required files: " + ", ".join(missing))
PY
}

manifest_add() {
	local key="$1"
	local value="$2"
	[[ "${key}" =~ ^[A-Z0-9_]+$ ]] || die "invalid manifest key: ${key}"
	[[ "${value}" != *$'\n'* && "${value}" != *$'\r'* ]] || die "manifest value contains a newline"
	printf '%s=%s\n' "${key}" "${value}" >>"${ACTIVE_RELEASE_DIR}/release.env"
}

manifest_get() {
	local manifest="$1"
	local key="$2"
	awk -F= -v key="${key}" '$1 == key {value = substr($0, index($0, "=") + 1)} END {print value}' "${manifest}"
}

service_manifest_key() {
	local service="$1"
	local key="${service^^}"
	printf '%s\n' "${key//-/_}"
}

set_release_state() {
	local state="$1"
	manifest_add STATE "${state}"
	log "release ${RELEASE_ID}: ${state}"
}

verify_free_space() {
	local available_kb
	local required_kb
	available_kb="$(df -Pk "${COENGINE_DIR}" | awk 'NR == 2 {print $4}')"
	required_kb=$((MIN_FREE_GB * 1024 * 1024))
	(( available_kb >= required_kb )) \
		|| die "less than ${MIN_FREE_GB} GB is free under ${COENGINE_DIR}"
}

verify_services_running() {
	local service
	local container_id
	local state
	local image_id
	local shared_image=""

	for service in "${APP_SERVICES[@]}"; do
		container_id="$(service_container "${service}")"
		state="$(docker inspect --format '{{.State.Status}}' "${container_id}")"
		[[ "${state}" == "running" ]] || die "application service is not running: ${service}"
		image_id="$(docker inspect --format '{{.Image}}' "${container_id}")"
		if [[ -z "${shared_image}" ]]; then
			shared_image="${image_id}"
		elif [[ "${shared_image}" != "${image_id}" ]]; then
			die "application services are not using one image"
		fi
	done

	CURRENT_IMAGE_ID="${shared_image}"
	CURRENT_COMMIT="$(docker image inspect "${CURRENT_IMAGE_ID}" --format '{{index .Config.Labels "com.coengine.stripev2.commit"}}')"
	CURRENT_APPS_DIGEST="$(docker image inspect "${CURRENT_IMAGE_ID}" --format '{{index .Config.Labels "com.coengine.runtime-apps.sha256"}}')"
	CURRENT_ASSETS_DIGEST="$(docker image inspect "${CURRENT_IMAGE_ID}" --format '{{index .Config.Labels "com.coengine.runtime-assets.sha256"}}')"
	validate_commit "${CURRENT_COMMIT}"
	validate_sha256 "${CURRENT_APPS_DIGEST}"
	validate_sha256 "${CURRENT_ASSETS_DIGEST}"
}

verify_mount_contract() {
	local service
	local container_id
	local apps_mount
	local assets_mount

	for service in "${APP_SERVICES[@]}"; do
		container_id="$(service_container "${service}")"
		apps_mount="$(docker inspect --format \
			'{{range .Mounts}}{{if eq .Destination "/home/frappe/frappe-bench/apps"}}{{.Name}}|{{.RW}}{{end}}{{end}}' \
			"${container_id}")"
		assets_mount="$(docker inspect --format \
			'{{range .Mounts}}{{if eq .Destination "/home/frappe/frappe-bench/sites/assets"}}{{.Source}}|{{.RW}}{{end}}{{end}}' \
			"${container_id}")"
		[[ "${apps_mount}" == "${APPS_VOLUME}|true" ]] \
			|| die "apps-volume mount contract drifted on ${service}"
		[[ "${assets_mount}" == "${RUNTIME_ASSETS_DIR}/current|false" ]] \
			|| die "read-only runtime-assets mount drifted on ${service}"
	done
}

capture_protected_services() {
	local service
	local container_id
	local state

	for service in "${PROTECTED_SERVICES[@]}"; do
		container_id="$(service_container "${service}")"
		state="$(docker inspect --format '{{.State.Status}}' "${container_id}")"
		[[ "${state}" == "running" ]] || die "protected service is not running: ${service}"
		PROTECTED_CONTAINER_IDS["${service}"]="$(docker inspect --format '{{.Id}}' "${container_id}")"
	done
}

verify_protected_services_unchanged() {
	local service
	local container_id
	local current_id
	local state

	for service in "${PROTECTED_SERVICES[@]}"; do
		container_id="$(service_container "${service}")"
		current_id="$(docker inspect --format '{{.Id}}' "${container_id}")"
		state="$(docker inspect --format '{{.State.Status}}' "${container_id}")"
		[[ "${current_id}" == "${PROTECTED_CONTAINER_IDS[${service}]}" ]] \
			|| die "protected container identity changed: ${service}"
		[[ "${state}" == "running" ]] || die "protected service stopped: ${service}"
	done
}

verify_app_set() {
	local backend_id
	local live_apps
	local expected_apps
	local expected_count

	backend_id="$(service_container backend)"
	live_apps="$(mktemp)"
	expected_apps="$(mktemp)"
	printf '%s\n' "${EXPECTED_APPS[@]}" | sort >"${expected_apps}"
	docker exec "${backend_id}" bash -lc \
		"cd /home/frappe/frappe-bench && bench --site '${SITE}' list-apps" \
		| awk 'NF {print $1}' | sort >"${live_apps}"

	if ! diff -u "${expected_apps}" "${live_apps}"; then
		rm -f "${expected_apps}" "${live_apps}"
		die "installed app set differs from the approved production set"
	fi
	expected_count="$(wc -l <"${live_apps}")"
	rm -f "${expected_apps}" "${live_apps}"
	[[ "${expected_count}" -eq "${EXPECTED_APP_COUNT}" ]] || die "expected ${EXPECTED_APP_COUNT} installed apps"
}

verify_overlay_manifest() {
	local backend_id
	local expected_digest
	local filename
	local extra
	local host_path
	local container_path
	local host_digest
	local container_digest
	local sentry_version
	local required_filename
	local -A seen=()

	backend_id="$(service_container backend)"
	while IFS=' ' read -r expected_digest filename extra; do
		[[ -n "${expected_digest}" && -n "${filename}" ]] || continue
		[[ -z "${extra:-}" ]] || die "invalid overlay manifest line for ${filename}"
		validate_sha256 "${expected_digest}"
		[[ -z "${seen[${filename}]:-}" ]] || die "duplicate overlay manifest entry: ${filename}"
		seen["${filename}"]=1
		case "${filename}" in
			frappe-app.py)
				host_path="${RUNTIME_DIR}/patches/frappe-app.py"
				container_path="/home/frappe/frappe-bench/apps/frappe/frappe/app.py"
				;;
			frappe-background_jobs.py)
				host_path="${RUNTIME_DIR}/patches/frappe-background_jobs.py"
				container_path="/home/frappe/frappe-bench/apps/frappe/frappe/utils/background_jobs.py"
				;;
			frappe-sentry.py)
				host_path="${RUNTIME_DIR}/patches/frappe-sentry.py"
				container_path="/home/frappe/frappe-bench/apps/frappe/frappe/utils/sentry.py"
				;;
			verify_frappe_assets.py)
				host_path="${RUNTIME_DIR}/verify_frappe_assets.py"
				container_path="/usr/local/bin/verify-frappe-assets.py"
				;;
			*)
				die "unexpected file in overlay manifest: ${filename}"
				;;
		esac
		[[ -f "${host_path}" ]] || die "missing runtime file: ${host_path}"
		host_digest="$(sha256sum "${host_path}" | awk '{print $1}')"
		container_digest="$(docker exec "${backend_id}" sha256sum "${container_path}" | awk '{print $1}')"
		[[ "${host_digest}" == "${expected_digest}" ]] || die "host runtime hash drift: ${filename}"
		[[ "${container_digest}" == "${expected_digest}" ]] || die "container runtime hash drift: ${filename}"
	done <"${OVERLAY_MANIFEST}"
	for required_filename in "${EXPECTED_OVERLAY_FILES[@]}"; do
		[[ "${seen[${required_filename}]:-}" == "1" ]] \
			|| die "overlay manifest is missing: ${required_filename}"
	done
	[[ "${#seen[@]}" -eq "${#EXPECTED_OVERLAY_FILES[@]}" ]] \
		|| die "overlay manifest does not contain the exact approved file set"

	sentry_version="$(docker exec "${backend_id}" /home/frappe/frappe-bench/env/bin/python -c \
		'import sentry_sdk; print(sentry_sdk.VERSION)')"
	[[ "${sentry_version}" == "${EXPECTED_SENTRY_VERSION}" ]] \
		|| die "unexpected Sentry SDK version: ${sentry_version}"
}

verify_sentry_environment() {
	local service
	local container_id
	for service in backend queue-short queue-long; do
		container_id="$(service_container "${service}")"
		docker exec "${container_id}" bash -lc 'test -n "${FRAPPE_SENTRY_DSN:-}"' \
			|| die "Sentry DSN is not set on ${service}"
	done
}

verify_runtime_assets() {
	local backend_id
	local resolved_assets
	local stripe_asset_target

	backend_id="$(service_container backend)"
	[[ -L "${RUNTIME_ASSETS_DIR}/current" ]] || die "runtime-assets/current is not a symlink"
	resolved_assets="$(realpath "${RUNTIME_ASSETS_DIR}/current")"
	[[ "${resolved_assets}" == "${RUNTIME_ASSETS_DIR}/"*/assets ]] \
		|| die "runtime asset symlink escapes the versioned asset root"
	docker exec "${backend_id}" /home/frappe/frappe-bench/env/bin/python \
		/usr/local/bin/verify-frappe-assets.py >/dev/null
	stripe_asset_target="$(docker exec "${backend_id}" readlink \
		/home/frappe/frappe-bench/sites/assets/stripe_integration)"
	[[ "${stripe_asset_target}" == "/home/frappe/frappe-bench/apps/stripe_integration/stripe_integration/public" ]] \
		|| die "Stripe runtime asset symlink has drifted"
}

verify_public_asset_manifest() {
	local backend_id
	local asset_list
	local asset_url
	local bare_path
	local content_type
	local checked=0

	backend_id="$(service_container backend)"
	asset_list="$(mktemp)"
	docker exec "${backend_id}" /home/frappe/frappe-bench/env/bin/python -c '
import json
from pathlib import Path

manifest = json.loads(Path("/home/frappe/frappe-bench/sites/assets/assets.json").read_text())
if not isinstance(manifest, dict) or not manifest:
    raise SystemExit("assets.json is empty or invalid")
for key, value in sorted(manifest.items()):
    if not isinstance(key, str) or not isinstance(value, str) or not value.startswith("/assets/"):
        raise SystemExit("assets.json contains an invalid URL")
    print(value)
' >"${asset_list}"

	while IFS= read -r asset_url; do
		[[ "${asset_url}" =~ ^/assets/[A-Za-z0-9._/@+~-]+$ && "${asset_url}" != *"/../"* ]] \
			|| die "asset manifest contains an unsafe URL"
		content_type="$(curl -fsSL --connect-timeout 5 --max-time 30 \
			-o /dev/null -w '%{content_type}' "${PUBLIC_URL}${asset_url}")"
		content_type="${content_type,,}"
		bare_path="${asset_url%%\?*}"
		case "${bare_path}" in
			*.js|*.mjs)
				[[ "${content_type}" == *javascript* ]] || die "wrong content type for ${asset_url}"
				;;
			*.css)
				[[ "${content_type}" == text/css* ]] || die "wrong content type for ${asset_url}"
				;;
			*.json|*.map)
				[[ "${content_type}" == application/json* ]] || die "wrong content type for ${asset_url}"
				;;
			*.svg|*.png|*.jpg|*.jpeg|*.gif|*.webp|*.ico)
				[[ "${content_type}" == image/* ]] || die "wrong content type for ${asset_url}"
				;;
			*.woff|*.woff2|*.ttf|*.otf)
				[[ "${content_type}" == font/* || "${content_type}" == application/font* \
					|| "${content_type}" == application/octet-stream* ]] \
					|| die "wrong content type for ${asset_url}"
				;;
			*)
				[[ -n "${content_type}" && "${content_type}" != text/html* ]] \
					|| die "unexpected content type for ${asset_url}"
				;;
		esac
		checked=$((checked + 1))
	done <"${asset_list}"
	rm -f "${asset_list}"
	(( checked > 0 )) || die "asset manifest did not contain any public URLs"
}

verify_workers_and_scheduler() {
	local backend_id
	local doctor_output
	local scheduler_output

	backend_id="$(service_container backend)"
	doctor_output="$(docker exec "${backend_id}" bash -lc \
		'cd /home/frappe/frappe-bench && bench doctor')"
	grep -Fq "Workers online: ${EXPECTED_WORKER_COUNT}" <<<"${doctor_output}" \
		|| die "expected ${EXPECTED_WORKER_COUNT} online workers"
	scheduler_output="$(docker exec "${backend_id}" bash -lc \
		"cd /home/frappe/frappe-bench && bench --site '${SITE}' scheduler status")"
	grep -Fq "Scheduler is enabled" <<<"${scheduler_output}" || die "scheduler is not enabled"
}

verify_public_health() {
	local root_code
	local ping_code
	root_code="$(curl -fsS -o /dev/null -w '%{http_code}' "${PUBLIC_URL}/")"
	ping_code="$(curl -fsS -o /dev/null -w '%{http_code}' "${PUBLIC_URL}/api/method/ping")"
	[[ "${root_code}" == "200" && "${ping_code}" == "200" ]] \
		|| die "public health check failed: root=${root_code} ping=${ping_code}"
}

verify_durable_runtime() {
	local apps_archive="${RUNTIME_DIR}/apps-runtime-20260710.tar.gz"
	local assets_archive="${RUNTIME_DIR}/assets-runtime-20260710.tar.gz"
	[[ -f "${apps_archive}" && -f "${assets_archive}" ]] || die "paired runtime archives are missing"
	[[ "$(sha256sum "${apps_archive}" | awk '{print $1}')" == "${CURRENT_APPS_DIGEST}" ]] \
		|| die "runtime app archive does not match the running image label"
	[[ "$(sha256sum "${assets_archive}" | awk '{print $1}')" == "${CURRENT_ASSETS_DIGEST}" ]] \
		|| die "runtime asset archive does not match the running image label"
}

production_preflight() {
	require_operator_runtime
	verify_free_space
	verify_services_running
	verify_mount_contract
	capture_protected_services
	APPS_VOLUME_PATH="$(docker volume inspect "${APPS_VOLUME}" --format '{{.Mountpoint}}')"
	SITES_VOLUME_PATH="$(docker volume inspect "${SITES_VOLUME}" --format '{{.Mountpoint}}')"
	[[ -d "${APPS_VOLUME_PATH}/stripe_integration" ]] || die "Stripe app is missing from the apps volume"
	verify_app_set
	verify_overlay_manifest
	verify_sentry_environment
	verify_runtime_assets
	verify_public_asset_manifest
	verify_workers_and_scheduler
	verify_durable_runtime
	verify_public_health
	log "production preflight passed: commit=${CURRENT_COMMIT} image=${CURRENT_IMAGE_ID}"
}

validate_target_options() {
	[[ -n "${TARGET_COMMIT}" ]] || die "--commit is required"
	[[ -n "${SOURCE_ARCHIVE}" ]] || die "--archive is required"
	[[ -n "${SOURCE_ARCHIVE_SHA256}" ]] || die "--sha256 is required"
	validate_commit "${TARGET_COMMIT}"
	validate_archive "${SOURCE_ARCHIVE}" "${SOURCE_ARCHIVE_SHA256}" "${TARGET_COMMIT}"
}

non_stripe_tree_hash() {
	local apps_root="$1"
	(
		cd "${apps_root}"
		{
			find . -path './stripe_integration' -prune -o -type f -print0 \
				| sort -z | xargs -0 sha256sum
			find . -path './stripe_integration' -prune -o -type l -printf '%p -> %l\n' | sort
		} | sha256sum | awk '{print $1}'
	)
}

tree_hash() {
	local root="$1"
	[[ -d "${root}" ]] || die "tree hash root is missing: ${root}"
	(
		cd "${root}"
		{
			find . -type f -print0 | sort -z | xargs -0 -r sha256sum
			find . -type l -printf '%p -> %l\n' | sort
			find . -type d -printf '%p|%m\n' | sort
		} | sha256sum | awk '{print $1}'
	)
}

snapshot_non_stripe_roots() {
	local output="$1"
	find "${APPS_VOLUME_PATH}" -mindepth 1 -maxdepth 1 \
		! -name 'stripe_integration' ! -name '.stripe_integration.*' \
		-printf '%f|%i|%m|%U|%G|%s|%T@|%l\n' | sort >"${output}"
}

replace_once() {
	local file="$1"
	local old_value="$2"
	local new_value="$3"
	local count
	count="$(grep -Fo "${old_value}" "${file}" | wc -l)"
	[[ "${count}" -eq 1 ]] || die "expected one occurrence of ${old_value} in ${file}"
	sed -i "s/${old_value}/${new_value}/" "${file}"
}

candidate_validate() {
	local candidate_id
	local candidate_commit
	local candidate_apps_digest
	local candidate_assets_digest
	local js_digest
	local expected_digest
	local filename
	local container_path
	local actual_digest

	candidate_id="$(docker image inspect "${CANDIDATE_IMAGE_TAG}" --format '{{.Id}}')"
	candidate_commit="$(docker image inspect "${CANDIDATE_IMAGE_TAG}" --format '{{index .Config.Labels "com.coengine.stripev2.commit"}}')"
	candidate_apps_digest="$(docker image inspect "${CANDIDATE_IMAGE_TAG}" --format '{{index .Config.Labels "com.coengine.runtime-apps.sha256"}}')"
	candidate_assets_digest="$(docker image inspect "${CANDIDATE_IMAGE_TAG}" --format '{{index .Config.Labels "com.coengine.runtime-assets.sha256"}}')"
	[[ "${candidate_commit}" == "${TARGET_COMMIT}" ]] || die "candidate commit label mismatch"
	[[ "${candidate_apps_digest}" == "$(manifest_get "${ACTIVE_RELEASE_DIR}/release.env" CANDIDATE_APPS_DIGEST)" ]] \
		|| die "candidate app digest mismatch"
	[[ "${candidate_assets_digest}" == "${CURRENT_ASSETS_DIGEST}" ]] || die "candidate asset digest mismatch"

	docker run --rm --entrypoint bash "${CANDIDATE_IMAGE_TAG}" -lc '
		set -euo pipefail
		cd /home/frappe/frappe-bench
		env/bin/python -m compileall -q apps/stripe_integration/stripe_integration
		env/bin/python -c "import sentry_sdk; import stripe_integration.stripe_integration.subscription_pause; import stripe_integration.stripe_integration.subscription_sync; import stripe_integration.stripe_integration.verify_post_upgrade; assert sentry_sdk.VERSION == \"1.45.1\""
		env/bin/python /usr/local/bin/verify-frappe-assets.py --assets-root /home/frappe/frappe-bench/assets
		env/bin/python -m unittest discover -s apps/stripe_integration/tests
		test "$(wc -l < sites/apps.txt)" -eq 12
		for app in crm drive erpnext frappe helpdesk hrms insights payments print_designer stripe_integration telephony twilio_integration; do
			test -d "apps/${app}"
		done
	'

	while IFS=' ' read -r expected_digest filename; do
		[[ -n "${expected_digest}" && -n "${filename}" ]] || continue
		case "${filename}" in
			frappe-app.py)
				container_path="/home/frappe/frappe-bench/apps/frappe/frappe/app.py"
				;;
			frappe-background_jobs.py)
				container_path="/home/frappe/frappe-bench/apps/frappe/frappe/utils/background_jobs.py"
				;;
			frappe-sentry.py)
				container_path="/home/frappe/frappe-bench/apps/frappe/frappe/utils/sentry.py"
				;;
			verify_frappe_assets.py)
				container_path="/usr/local/bin/verify-frappe-assets.py"
				;;
			*)
				die "unexpected file in overlay manifest: ${filename}"
				;;
		esac
		actual_digest="$(docker run --rm --entrypoint sha256sum "${CANDIDATE_IMAGE_TAG}" \
			"${container_path}" | awk '{print $1}')"
		[[ "${actual_digest}" == "${expected_digest}" ]] \
			|| die "candidate runtime hash drift: ${filename}"
	done <"${OVERLAY_MANIFEST}"

	js_digest="$(docker run --rm --entrypoint sha256sum "${CANDIDATE_IMAGE_TAG}" \
		/home/frappe/frappe-bench/apps/stripe_integration/stripe_integration/public/js/subscription_stripe.js \
		| awk '{print $1}')"
	[[ "${js_digest}" == "$(manifest_get "${ACTIVE_RELEASE_DIR}/release.env" STRIPE_JS_DIGEST)" ]] \
		|| die "candidate Stripe JavaScript does not match the release"
	manifest_add CANDIDATE_IMAGE_ID "${candidate_id}"
}

prepare_release() {
	local timestamp
	local build_root
	local source_copy
	local old_non_stripe_hash
	local new_non_stripe_hash
	local candidate_apps_digest
	local stripe_js_digest
	local current_short

	validate_target_options
	require_operator_runtime
	acquire_lock
	production_preflight
	if [[ "${CURRENT_COMMIT}" == "${TARGET_COMMIT}" ]]; then
		DEPLOY_NOOP=1
		log "commit ${TARGET_COMMIT} is already deployed; no changes required"
		return 0
	fi
	timestamp="$(date -u +'%Y%m%dT%H%M%SZ')"
	RELEASE_ID="${timestamp}-${TARGET_COMMIT:0:12}"
	ACTIVE_RELEASE_DIR="$(release_path "${RELEASE_ID}")"
	[[ ! -e "${ACTIVE_RELEASE_DIR}" ]] || die "release directory already exists: ${ACTIVE_RELEASE_DIR}"
	install -d -m 0700 \
		"${ACTIVE_RELEASE_DIR}/incoming" \
		"${ACTIVE_RELEASE_DIR}/context/runtime/patches" \
		"${ACTIVE_RELEASE_DIR}/staging" \
		"${ACTIVE_RELEASE_DIR}/backups/site" \
		"${ACTIVE_RELEASE_DIR}/backups/apps" \
		"${ACTIVE_RELEASE_DIR}/backups/durable"
	install -m 0600 /dev/null "${ACTIVE_RELEASE_DIR}/release.env"
	manifest_add RELEASE_ID "${RELEASE_ID}"
	manifest_add TARGET_COMMIT "${TARGET_COMMIT}"
	manifest_add SOURCE_ARCHIVE_SHA256 "${SOURCE_ARCHIVE_SHA256}"
	manifest_add PREVIOUS_IMAGE_ID "${CURRENT_IMAGE_ID}"
	manifest_add PREVIOUS_COMMIT "${CURRENT_COMMIT}"
	manifest_add PREVIOUS_APPS_DIGEST "${CURRENT_APPS_DIGEST}"
	manifest_add ASSETS_DIGEST "${CURRENT_ASSETS_DIGEST}"
	for service in "${PROTECTED_SERVICES[@]}"; do
		manifest_add "PROTECTED_$(service_manifest_key "${service}")_ID" \
			"${PROTECTED_CONTAINER_IDS[${service}]}"
	done
	set_release_state PREPARING

	source_copy="${ACTIVE_RELEASE_DIR}/incoming/stripev2-${TARGET_COMMIT}.tar.gz"
	install -m 0600 "${SOURCE_ARCHIVE}" "${source_copy}"
	validate_archive "${source_copy}" "${SOURCE_ARCHIVE_SHA256}" "${TARGET_COMMIT}"
	install -m 0750 "$0" "${ACTIVE_RELEASE_DIR}/coengine-stripe-deploy.sh"

	current_short="${CURRENT_COMMIT:0:12}"
	ROLLBACK_IMAGE_TAG="${IMAGE_REPOSITORY}:rollback-${current_short}-${timestamp}"
	CANDIDATE_IMAGE_TAG="${IMAGE_REPOSITORY}:stripev2-${TARGET_COMMIT:0:12}"
	docker image tag "${CURRENT_IMAGE_ID}" "${ROLLBACK_IMAGE_TAG}"
	manifest_add ROLLBACK_IMAGE_TAG "${ROLLBACK_IMAGE_TAG}"
	manifest_add CANDIDATE_IMAGE_TAG "${CANDIDATE_IMAGE_TAG}"

	log "copying the apps volume uncompressed while production remains online"
	cp -a "${APPS_VOLUME_PATH}" "${ACTIVE_RELEASE_DIR}/backups/apps/apps-volume"
	[[ -d "${ACTIVE_RELEASE_DIR}/backups/apps/apps-volume/stripe_integration" ]] \
		|| die "full apps-volume snapshot is incomplete"

	cp -a "${COENGINE_DIR}/Dockerfile" "${COENGINE_DIR}/.dockerignore" \
		"${ACTIVE_RELEASE_DIR}/context/"
	cp -a \
		"${RUNTIME_DIR}/apps-runtime-20260710.tar.gz" \
		"${RUNTIME_DIR}/assets-runtime-20260710.tar.gz" \
		"${RUNTIME_DIR}/apps-runtime-20260710.txt" \
		"${RUNTIME_DIR}/verify_frappe_assets.py" \
		"${RUNTIME_DIR}/SHA256SUMS" \
		"${ACTIVE_RELEASE_DIR}/context/runtime/"
	cp -a "${RUNTIME_DIR}/patches/." "${ACTIVE_RELEASE_DIR}/context/runtime/patches/"

	build_root="${ACTIVE_RELEASE_DIR}/staging/build"
	install -d -m 0700 "${build_root}"
	tar -xzf "${ACTIVE_RELEASE_DIR}/context/runtime/apps-runtime-20260710.tar.gz" -C "${build_root}"
	[[ -d "${build_root}/apps/stripe_integration" ]] || die "runtime app archive has no Stripe app"
	old_non_stripe_hash="$(non_stripe_tree_hash "${build_root}/apps")"
	assert_exact_path "${build_root}/apps/stripe_integration" \
		"${ACTIVE_RELEASE_DIR}/staging/build/apps/stripe_integration"
	rm -rf -- "${build_root}/apps/stripe_integration"
	tar -xzf "${source_copy}" -C "${build_root}/apps"
	new_non_stripe_hash="$(non_stripe_tree_hash "${build_root}/apps")"
	[[ "${old_non_stripe_hash}" == "${new_non_stripe_hash}" ]] \
		|| die "a non-Stripe app changed while preparing the candidate"

	stripe_js_digest="$(sha256sum \
		"${build_root}/apps/stripe_integration/stripe_integration/public/js/subscription_stripe.js" \
		| awk '{print $1}')"
	manifest_add STRIPE_JS_DIGEST "${stripe_js_digest}"

	tar --numeric-owner --owner=1000 --group=1000 -czf \
		"${ACTIVE_RELEASE_DIR}/context/runtime/apps-runtime-20260710.tar.gz.new" \
		-C "${build_root}" apps
	mv -f \
		"${ACTIVE_RELEASE_DIR}/context/runtime/apps-runtime-20260710.tar.gz.new" \
		"${ACTIVE_RELEASE_DIR}/context/runtime/apps-runtime-20260710.tar.gz"
	candidate_apps_digest="$(sha256sum \
		"${ACTIVE_RELEASE_DIR}/context/runtime/apps-runtime-20260710.tar.gz" | awk '{print $1}')"
	manifest_add CANDIDATE_APPS_DIGEST "${candidate_apps_digest}"
	replace_once "${ACTIVE_RELEASE_DIR}/context/Dockerfile" "${CURRENT_APPS_DIGEST}" "${candidate_apps_digest}"
	replace_once "${ACTIVE_RELEASE_DIR}/context/Dockerfile" "${CURRENT_COMMIT}" "${TARGET_COMMIT}"
	replace_once "${ACTIVE_RELEASE_DIR}/context/runtime/SHA256SUMS" \
		"${CURRENT_APPS_DIGEST}" "${candidate_apps_digest}"

	log "building ${CANDIDATE_IMAGE_TAG}; production is still online"
	docker build --pull=false --progress=plain -t "${CANDIDATE_IMAGE_TAG}" \
		"${ACTIVE_RELEASE_DIR}/context"
	candidate_validate
	set_release_state PREPARED
	log "prepared release ${RELEASE_ID}"
	printf 'RELEASE_ID=%s\n' "${RELEASE_ID}"
}

drain_queue_workers() {
	local short_id
	local long_id
	local service
	local container_id
	local expected_cmd
	local actual_cmd
	local state
	local exit_code
	local oom_killed
	local -a running_ids=()

	short_id="$(service_container queue-short)"
	long_id="$(service_container queue-long)"
	[[ "${short_id}" != "${long_id}" ]] || die "queue workers resolve to the same container"

	for service in queue-short queue-long; do
		container_id="$(service_container "${service}")"
		case "${service}" in
			queue-short)
				expected_cmd='["bench","worker","--queue","short,default"]'
				;;
			queue-long)
				expected_cmd='["bench","worker","--queue","long"]'
				;;
		esac
		actual_cmd="$(docker inspect --format '{{json .Config.Cmd}}' "${container_id}")"
		[[ "${actual_cmd}" == "${expected_cmd}" ]] \
			|| die "unexpected worker command for ${service}"
		state="$(docker inspect --format '{{.State.Status}}' "${container_id}")"
		case "${state}" in
			running)
				running_ids+=("${container_id}")
				;;
			exited)
				exit_code="$(docker inspect --format '{{.State.ExitCode}}' "${container_id}")"
				oom_killed="$(docker inspect --format '{{.State.OOMKilled}}' "${container_id}")"
				[[ "${exit_code}" == "0" && "${oom_killed}" == "false" ]] \
					|| die "${service} was not previously warm-stopped cleanly"
				;;
			*)
				die "${service} is not running or cleanly exited"
				;;
		esac
	done

	set_release_state WORKERS_DRAINING
	if (( ${#running_ids[@]} > 0 )); then
		WORKER_TERM_SENT=1
		docker stop --signal TERM --time -1 "${running_ids[@]}" >/dev/null
	fi

	for service in queue-short queue-long; do
		container_id="$(service_container "${service}")"
		state="$(docker inspect --format '{{.State.Status}}' "${container_id}")"
		exit_code="$(docker inspect --format '{{.State.ExitCode}}' "${container_id}")"
		oom_killed="$(docker inspect --format '{{.State.OOMKilled}}' "${container_id}")"
		[[ "${state}" == "exited" && "${exit_code}" == "0" && "${oom_killed}" == "false" ]] \
			|| die "${service} did not complete a clean warm shutdown"
	done
	WORKERS_DRAINED=1
	set_release_state WORKERS_DRAINED
}

stop_app_service() {
	local service="$1"
	local allowed=0
	local candidate
	local container_id

	[[ "${service}" != "queue-short" && "${service}" != "queue-long" ]] \
		|| die "queue workers must be stopped with drain_queue_workers"
	for candidate in "${APP_SERVICES[@]}"; do
		[[ "${service}" == "${candidate}" ]] && allowed=1
	done
	[[ "${allowed}" -eq 1 ]] || die "refusing to stop a protected or unknown service: ${service}"
	container_id="$(service_container "${service}")"
	if [[ "$(docker inspect --format '{{.State.Status}}' "${container_id}")" == "running" ]]; then
		if ! timeout "$((STOP_TIMEOUT + 15))" docker stop -t "${STOP_TIMEOUT}" "${container_id}" >/dev/null; then
			warn "${service} exceeded its graceful stop timeout; terminating only this app container"
			docker kill "${container_id}" >/dev/null
		fi
	fi
	[[ "$(docker inspect --format '{{.State.Status}}' "${container_id}")" == "exited" ]] \
		|| die "application service did not stop: ${service}"
}

recreate_services() {
	local service
	local allowed=0
	local candidate
	for service in "$@"; do
		allowed=0
		for candidate in "${APP_SERVICES[@]}"; do
			[[ "${service}" == "${candidate}" ]] && allowed=1
		done
		[[ "${allowed}" -eq 1 ]] || die "refusing to recreate a protected or unknown service: ${service}"
	done
	compose up -d --no-deps --force-recreate "$@"
}

try_set_maintenance_mode() {
	local mode="$1"
	local backend_id
	local state

	[[ "${mode}" == "on" || "${mode}" == "off" ]] || return 2
	backend_id="$(compose ps -a -q backend | head -n 1 || true)"
	if [[ -n "${backend_id}" ]]; then
		state="$(docker inspect --format '{{.State.Status}}' "${backend_id}" 2>/dev/null || true)"
		if [[ "${state}" == "running" ]] && docker exec "${backend_id}" bash -lc \
			"cd /home/frappe/frappe-bench && bench --site '${SITE}' set-maintenance-mode '${mode}'" \
			>/dev/null; then
			return 0
		fi
	fi
	compose run --rm --no-deps backend bench --site "${SITE}" set-maintenance-mode "${mode}" \
		>/dev/null
}

quarantine_queue_workers() {
	local service
	local container_id
	local state
	local restart_policy
	local -a container_ids=()
	local -a stop_ids=()

	if [[ "${WORKER_TERM_SENT}" -eq 1 && "${WORKERS_DRAINED}" -eq 0 ]]; then
		warn "queue workers are already warm-stopping; no second signal will be sent"
		return 0
	fi
	for service in queue-short queue-long; do
		container_id="$(compose ps -a -q "${service}" | head -n 1 || true)"
		[[ -n "${container_id}" ]] || return 1
		container_ids+=("${container_id}")
		state="$(docker inspect --format '{{.State.Status}}' "${container_id}" 2>/dev/null || true)"
		case "${state}" in
			running|restarting)
				stop_ids+=("${container_id}")
				;;
			exited)
				;;
			*)
				return 1
				;;
		esac
	done

	WORKERS_DRAINED=0
	set_release_state FAILURE_WORKERS_DRAINING || true
	docker update --restart=no "${container_ids[@]}" >/dev/null || return 1
	if (( ${#stop_ids[@]} > 0 )); then
		WORKER_TERM_SENT=1
		docker stop --signal TERM --time -1 "${stop_ids[@]}" >/dev/null || return 1
	fi
	for container_id in "${container_ids[@]}"; do
		state="$(docker inspect --format '{{.State.Status}}' "${container_id}" 2>/dev/null || true)"
		restart_policy="$(docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' \
			"${container_id}" 2>/dev/null || true)"
		[[ "${state}" == "exited" && "${restart_policy}" == "no" ]] || return 1
	done
	WORKERS_DRAINED=1
}

quarantine_application_services() {
	local service
	local container_id
	local state
	local restart_policy
	local failed=0

	for service in backend frontend websocket scheduler; do
		container_id="$(compose ps -a -q "${service}" | head -n 1 || true)"
		if [[ -z "${container_id}" ]]; then
			failed=1
			continue
		fi
		docker update --restart=no "${container_id}" >/dev/null 2>&1 || failed=1
		state="$(docker inspect --format '{{.State.Status}}' "${container_id}" 2>/dev/null || true)"
		if [[ "${state}" == "running" || "${state}" == "restarting" ]]; then
			docker stop -t "${STOP_TIMEOUT}" "${container_id}" >/dev/null 2>&1 \
				|| docker kill "${container_id}" >/dev/null 2>&1 \
				|| failed=1
		fi
		state="$(docker inspect --format '{{.State.Status}}' "${container_id}" 2>/dev/null || true)"
		restart_policy="$(docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' \
			"${container_id}" 2>/dev/null || true)"
		[[ "${state}" == "exited" && "${restart_policy}" == "no" ]] || failed=1
	done
	quarantine_queue_workers || failed=1
	(( failed == 0 ))
}

verify_service_image_contract() {
	local expected_image_id="$1"
	local expected_commit="$2"
	local service
	local container_id
	local image_id
	local commit_label

	for service in "${APP_SERVICES[@]}"; do
		container_id="$(service_container "${service}")"
		[[ "$(docker inspect --format '{{.State.Status}}' "${container_id}")" == "running" ]] \
			|| die "restored application service is not running: ${service}"
		image_id="$(docker inspect --format '{{.Image}}' "${container_id}")"
		commit_label="$(docker inspect --format '{{index .Config.Labels "com.coengine.stripev2.commit"}}' \
			"${container_id}")"
		[[ "${image_id}" == "${expected_image_id}" ]] || die "restored image mismatch on ${service}"
		[[ "${commit_label}" == "${expected_commit}" ]] || die "restored commit mismatch on ${service}"
	done
}

verify_application_runtime() {
	local expected_image_id="$1"
	local expected_commit="$2"
	verify_service_image_contract "${expected_image_id}" "${expected_commit}"
	verify_protected_services_unchanged
	verify_mount_contract
	verify_app_set
	verify_overlay_manifest
	verify_sentry_environment
	verify_runtime_assets
	verify_public_asset_manifest
	verify_workers_and_scheduler
	verify_durable_runtime
}

wait_for_backend() {
	local backend_id
	backend_id="$(service_container backend)"
	for _ in $(seq 1 60); do
		if docker exec "${backend_id}" /home/frappe/frappe-bench/env/bin/python -c \
			"import socket; connection = socket.create_connection(('127.0.0.1', 8000), 2); connection.close()" \
			>/dev/null 2>&1; then
			return 0
		fi
		sleep 1
	done
	die "backend did not become ready"
}

wait_for_frontend() {
	local frontend_id
	local health
	frontend_id="$(service_container frontend)"
	for _ in $(seq 1 90); do
		health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
			"${frontend_id}")"
		[[ "${health}" == "healthy" ]] && return 0
		[[ "${health}" == "unhealthy" ]] && die "frontend health check failed"
		sleep 1
	done
	die "frontend did not become healthy"
}

wait_for_workers() {
	local backend_id
	local output
	backend_id="$(service_container backend)"
	for _ in $(seq 1 60); do
		output="$(docker exec "${backend_id}" bash -lc \
			'cd /home/frappe/frappe-bench && bench doctor' 2>&1 || true)"
		grep -Fq "Workers online: ${EXPECTED_WORKER_COUNT}" <<<"${output}" && return 0
		sleep 2
	done
	die "workers did not become ready"
}

wait_for_public_health() {
	local root_code
	local ping_code
	for _ in $(seq 1 90); do
		root_code="$(curl -sS -o /dev/null -w '%{http_code}' "${PUBLIC_URL}/" || true)"
		ping_code="$(curl -sS -o /dev/null -w '%{http_code}' \
			"${PUBLIC_URL}/api/method/ping" || true)"
		[[ "${root_code}" == "200" && "${ping_code}" == "200" ]] && return 0
		sleep 1
	done
	die "public endpoint did not become healthy"
}

verify_candidate_runtime() {
	local candidate_id="$1"
	local service
	local container_id
	local image_id
	local commit_label
	local asset_hash
	local public_hash
	local public_asset

	for service in "${APP_SERVICES[@]}"; do
		container_id="$(service_container "${service}")"
		image_id="$(docker inspect --format '{{.Image}}' "${container_id}")"
		commit_label="$(docker inspect --format '{{index .Config.Labels "com.coengine.stripev2.commit"}}' \
			"${container_id}")"
		[[ "${image_id}" == "${candidate_id}" ]] || die "wrong image on service: ${service}"
		[[ "${commit_label}" == "${TARGET_COMMIT}" ]] || die "wrong commit label on service: ${service}"
	done
	verify_protected_services_unchanged
	verify_mount_contract
	verify_app_set
	verify_overlay_manifest
	verify_sentry_environment
	verify_runtime_assets
	verify_public_asset_manifest
	wait_for_workers

	asset_hash="$(docker exec "$(service_container frontend)" sha256sum \
		/home/frappe/frappe-bench/sites/assets/stripe_integration/js/subscription_stripe.js \
		| awk '{print $1}')"
	[[ "${asset_hash}" == "$(manifest_get "${ACTIVE_RELEASE_DIR}/release.env" STRIPE_JS_DIGEST)" ]] \
		|| die "mounted Stripe JavaScript hash mismatch"
	public_hash="$(curl -fsS "${PUBLIC_URL}/assets/stripe_integration/js/subscription_stripe.js" \
		| sha256sum | awk '{print $1}')"
	[[ "${public_hash}" == "${asset_hash}" ]] || die "public Stripe JavaScript is stale"
	public_asset="$(curl -fsS "${PUBLIC_URL}/assets/stripe_integration/js/subscription_stripe.js")"
	grep -Fq '__("Pause")' <<<"${public_asset}" || die "Pause control is missing from the public asset"
	grep -Fq '__("Stripe")' <<<"${public_asset}" || die "Stripe action group is missing from the public asset"
}

copy_new_site_backups() {
	local marker="$1"
	local source_dir="${SITES_VOLUME_PATH}/${SITE}/private/backups"
	local destination="${ACTIVE_RELEASE_DIR}/backups/site"
	local backup_count=0
	local backup_file

	while IFS= read -r -d '' backup_file; do
		[[ -s "${backup_file}" ]] || die "site backup contains an empty file"
		cp -a "${backup_file}" "${destination}/"
		sha256sum "${destination}/$(basename "${backup_file}")" \
			>>"${destination}/SHA256SUMS"
		backup_count=$((backup_count + 1))
	done < <(find "${source_dir}" -maxdepth 1 -type f -newer "${marker}" -print0)
	(( backup_count >= 4 )) || die "expected database, config, public-file, and private-file backups"
	(
		cd "${destination}"
		sha256sum -c SHA256SUMS >/dev/null
		for backup_file in *.sql.gz; do gzip -t "${backup_file}"; done
		for backup_file in *-files.tgz; do tar -tzf "${backup_file}" >/dev/null; done
	)
	manifest_add SITE_BACKUP_COUNT "${backup_count}"
}

recovery_failed() {
	local exit_code=$?
	local state="RECOVERY_FAILED_QUARANTINE_UNCONFIRMED"
	trap - EXIT ERR INT TERM HUP
	set +e
	warn "automatic recovery did not pass verification; production will remain fail-closed"
	if ! try_set_maintenance_mode on; then
		warn "maintenance mode could not be confirmed"
	fi
	if quarantine_application_services; then
		state="RECOVERY_FAILED_QUARANTINED"
	else
		warn "not every application service could be confirmed quarantined"
	fi
	set_release_state "${state}"
	(( exit_code != 0 )) || exit_code=1
	exit "${exit_code}"
}

restore_before_migration() {
	local app_root="${APPS_VOLUME_PATH}"
	local live_path="${app_root}/stripe_integration"
	local old_path="${app_root}/.stripe_integration.old-${RELEASE_ID}"
	local failed_path="${app_root}/.stripe_integration.failed-${RELEASE_ID}"
	local manifest="${ACTIVE_RELEASE_DIR}/release.env"
	local previous_image_id
	local previous_commit
	local previous_stripe_digest

	trap - ERR INT TERM HUP
	trap recovery_failed EXIT
	previous_image_id="$(manifest_get "${manifest}" PREVIOUS_IMAGE_ID)"
	previous_commit="$(manifest_get "${manifest}" PREVIOUS_COMMIT)"
	previous_stripe_digest="$(manifest_get "${manifest}" PREVIOUS_STRIPE_TREE_DIGEST)"
	warn "activation failed before migration; restoring and fully verifying the previous runtime"
	try_set_maintenance_mode on
	if [[ "${LIVE_CODE_SWITCHED}" -eq 1 ]]; then
		assert_exact_path "${live_path}" "${APPS_VOLUME_PATH}/stripe_integration"
		assert_exact_path "${old_path}" "${APPS_VOLUME_PATH}/.stripe_integration.old-${RELEASE_ID}"
		assert_exact_path "${failed_path}" "${APPS_VOLUME_PATH}/.stripe_integration.failed-${RELEASE_ID}"
		[[ -d "${old_path}" && ! -e "${failed_path}" ]] || die "automatic recovery paths are not clean"
		if [[ -d "${live_path}" ]]; then
			mv "${live_path}" "${failed_path}"
		fi
		mv "${old_path}" "${live_path}"
		[[ "$(tree_hash "${live_path}")" == "${previous_stripe_digest}" ]] \
			|| die "restored Stripe app does not match the cutover snapshot"
	fi
	[[ "$(docker image inspect "${ROLLBACK_IMAGE_TAG}" --format '{{.Id}}')" == "${previous_image_id}" ]] \
		|| die "rollback image no longer matches the recorded previous image"
	docker image tag "${ROLLBACK_IMAGE_TAG}" "${IMAGE_REPOSITORY}:latest"
	recreate_services backend websocket
	wait_for_backend
	recreate_services frontend
	wait_for_frontend
	if [[ "${WORKERS_DRAINED}" -eq 1 ]]; then
		recreate_services queue-short queue-long
	fi
	recreate_services scheduler
	wait_for_workers
	docker exec "$(service_container backend)" bash -lc \
		"cd /home/frappe/frappe-bench && bench --site '${SITE}' clear-cache" >/dev/null
	verify_application_runtime "${previous_image_id}" "${previous_commit}"
	set_release_state RESTORED_VERIFIED_MAINTENANCE_ON
	try_set_maintenance_mode off
	wait_for_public_health
	verify_workers_and_scheduler
	verify_protected_services_unchanged
	set_release_state ROLLED_BACK_BEFORE_MIGRATION
	trap - EXIT ERR INT TERM HUP
	exit 1
}

activation_error() {
	local exit_code=$?
	trap - EXIT ERR INT TERM HUP
	if [[ "${WORKER_TERM_SENT}" -eq 1 && "${WORKERS_DRAINED}" -eq 0 ]]; then
		set +e
		if ! try_set_maintenance_mode on; then
			warn "maintenance mode could not be confirmed"
		fi
		if quarantine_application_services; then
			set_release_state FAILED_WORKER_DRAIN
		else
			set_release_state FAILED_WORKER_DRAIN_QUARANTINE_UNCONFIRMED
		fi
		warn "workers are still warm-stopping; do not signal or recreate them"
		exit "${exit_code}"
	fi
	if [[ "${CUTOVER_STARTED}" -eq 1 && "${MIGRATION_STARTED}" -eq 0 ]]; then
		restore_before_migration
	fi
	if [[ "${CUTOVER_STARTED}" -eq 1 ]]; then
		set +e
		if ! try_set_maintenance_mode on; then
			warn "maintenance mode could not be confirmed"
		fi
		if quarantine_application_services; then
			set_release_state FAILED_AFTER_MIGRATION_QUARANTINED
		else
			set_release_state FAILED_AFTER_MIGRATION_QUARANTINE_UNCONFIRMED
		fi
		warn "migration began; the verified site backup must be assessed before services are restored"
	fi
	exit "${exit_code}"
}

install_durable_candidate() {
	local temporary
	local source
	local destination

	cp -a "${COENGINE_DIR}/Dockerfile" "${ACTIVE_RELEASE_DIR}/backups/durable/"
	cp -a "${RUNTIME_DIR}/apps-runtime-20260710.tar.gz" \
		"${RUNTIME_DIR}/SHA256SUMS" "${ACTIVE_RELEASE_DIR}/backups/durable/"
	for relative_path in Dockerfile runtime/apps-runtime-20260710.tar.gz runtime/SHA256SUMS; do
		source="${ACTIVE_RELEASE_DIR}/context/${relative_path}"
		destination="${COENGINE_DIR}/${relative_path}"
		temporary="${destination}.new-${RELEASE_ID}"
		cp -a "${source}" "${temporary}"
		mv -f "${temporary}" "${destination}"
	done
	(
		cd "${COENGINE_DIR}"
		sha256sum -c runtime/SHA256SUMS >/dev/null
	)
}

restore_durable_snapshot() {
	local source
	local destination
	local temporary
	local relative_path

	for relative_path in Dockerfile runtime/apps-runtime-20260710.tar.gz runtime/SHA256SUMS; do
		source="${ACTIVE_RELEASE_DIR}/backups/durable/$(basename "${relative_path}")"
		destination="${COENGINE_DIR}/${relative_path}"
		temporary="${destination}.rollback-new-${RELEASE_ID}"
		[[ -f "${source}" ]] || die "durable rollback file is missing: ${source}"
		[[ ! -e "${temporary}" ]] || die "durable rollback staging path already exists"
		cp -a "${source}" "${temporary}"
		mv -f "${temporary}" "${destination}"
	done
	(
		cd "${COENGINE_DIR}"
		sha256sum -c runtime/SHA256SUMS >/dev/null
	)
}

activate_release() {
	local manifest
	local state
	local recorded_target
	local candidate_id
	local app_root
	local live_path
	local new_path
	local old_path
	local current_owner
	local backup_marker
	local roots_before
	local roots_after
	local current_link
	local previous_stripe_digest

	validate_release_id "${RELEASE_ID}"
	ACTIVE_RELEASE_DIR="$(release_path "${RELEASE_ID}")"
	manifest="${ACTIVE_RELEASE_DIR}/release.env"
	[[ -f "${manifest}" ]] || die "release manifest is missing: ${manifest}"
	recorded_target="$(manifest_get "${manifest}" TARGET_COMMIT)"
	validate_commit "${recorded_target}"
	[[ "${CONFIRM_VALUE}" == "${recorded_target}" ]] \
		|| die "--confirm must exactly match the release's full commit SHA"
	TARGET_COMMIT="${recorded_target}"
	state="$(manifest_get "${manifest}" STATE)"
	[[ "${state}" == "PREPARED" ]] || die "release must be PREPARED, found: ${state}"

	require_operator_runtime
	acquire_lock
	production_preflight
	[[ "${CURRENT_COMMIT}" != "${TARGET_COMMIT}" ]] || die "target commit is already deployed"
	CANDIDATE_IMAGE_TAG="$(manifest_get "${manifest}" CANDIDATE_IMAGE_TAG)"
	ROLLBACK_IMAGE_TAG="$(manifest_get "${manifest}" ROLLBACK_IMAGE_TAG)"
	candidate_id="$(manifest_get "${manifest}" CANDIDATE_IMAGE_ID)"
	[[ "$(docker image inspect "${CANDIDATE_IMAGE_TAG}" --format '{{.Id}}')" == "${candidate_id}" ]] \
		|| die "candidate image has drifted since prepare"
	[[ "$(docker image inspect "${ROLLBACK_IMAGE_TAG}" --format '{{.Id}}')" == "${CURRENT_IMAGE_ID}" ]] \
		|| die "rollback image no longer matches current production"

	APPS_VOLUME_PATH="$(docker volume inspect "${APPS_VOLUME}" --format '{{.Mountpoint}}')"
	SITES_VOLUME_PATH="$(docker volume inspect "${SITES_VOLUME}" --format '{{.Mountpoint}}')"
	for service in "${PROTECTED_SERVICES[@]}"; do
		[[ "${PROTECTED_CONTAINER_IDS[${service}]}" == \
			"$(manifest_get "${manifest}" "PROTECTED_$(service_manifest_key "${service}")_ID")" ]] \
			|| die "protected service identity changed after prepare: ${service}"
	done

	CUTOVER_STARTED=1
	trap activation_error EXIT ERR INT TERM HUP
	set_release_state MAINTENANCE_ON
	docker exec "$(service_container backend)" bash -lc \
		"cd /home/frappe/frappe-bench && bench --site '${SITE}' set-maintenance-mode on"
	stop_app_service scheduler
	drain_queue_workers

	backup_marker="${ACTIVE_RELEASE_DIR}/.site-backup-marker"
	touch "${backup_marker}"
	docker exec "$(service_container backend)" bash -lc \
		"cd /home/frappe/frappe-bench && bench --site '${SITE}' backup --with-files --compress"
	copy_new_site_backups "${backup_marker}"
	set_release_state BACKUP_COMPLETE

	stop_app_service frontend
	stop_app_service websocket
	stop_app_service backend
	app_root="${APPS_VOLUME_PATH}"
	live_path="${app_root}/stripe_integration"
	new_path="${app_root}/.stripe_integration.new-${RELEASE_ID}"
	old_path="${app_root}/.stripe_integration.old-${RELEASE_ID}"
	assert_exact_path "${live_path}" "${APPS_VOLUME_PATH}/stripe_integration"
	assert_exact_path "${new_path}" "${APPS_VOLUME_PATH}/.stripe_integration.new-${RELEASE_ID}"
	assert_exact_path "${old_path}" "${APPS_VOLUME_PATH}/.stripe_integration.old-${RELEASE_ID}"
	[[ -d "${live_path}" && ! -e "${new_path}" && ! -e "${old_path}" ]] \
		|| die "Stripe app swap paths are not clean"
	previous_stripe_digest="$(tree_hash "${live_path}")"
	cp -a "${live_path}" "${ACTIVE_RELEASE_DIR}/backups/apps/stripe_integration-cutover"
	[[ "$(tree_hash "${ACTIVE_RELEASE_DIR}/backups/apps/stripe_integration-cutover")" == \
		"${previous_stripe_digest}" ]] || die "Stripe cutover snapshot is incomplete"
	manifest_add PREVIOUS_STRIPE_TREE_DIGEST "${previous_stripe_digest}"
	current_owner="$(stat -c '%u:%g' "${live_path}")"
	cp -a "${ACTIVE_RELEASE_DIR}/staging/build/apps/stripe_integration" "${new_path}"
	chown -R "${current_owner}" "${new_path}"
	[[ "$(sha256sum "${new_path}/stripe_integration/public/js/subscription_stripe.js" | awk '{print $1}')" == \
		"$(manifest_get "${manifest}" STRIPE_JS_DIGEST)" ]] || die "staged live app hash mismatch"
	roots_before="${ACTIVE_RELEASE_DIR}/backups/apps/non-stripe-roots-before.txt"
	roots_after="${ACTIVE_RELEASE_DIR}/backups/apps/non-stripe-roots-after.txt"
	snapshot_non_stripe_roots "${roots_before}"
	LIVE_CODE_SWITCHED=1
	mv "${live_path}" "${old_path}"
	mv "${new_path}" "${live_path}"
	snapshot_non_stripe_roots "${roots_after}"
	diff -u "${roots_before}" "${roots_after}" >/dev/null \
		|| die "a non-Stripe apps-volume root changed during cutover"
	docker image tag "${CANDIDATE_IMAGE_TAG}" "${IMAGE_REPOSITORY}:latest"
	set_release_state LIVE_CODE_SWITCHED

	MIGRATION_STARTED=1
	set_release_state MIGRATION_STARTED
	compose run --rm --no-deps backend bench --site "${SITE}" migrate
	set_release_state MIGRATED
	compose run --rm --no-deps backend bash -lc \
		"bench --site '${SITE}' clear-cache && bench --site '${SITE}' execute stripe_integration.stripe_integration.verify_post_upgrade.run"

	recreate_services backend websocket
	wait_for_backend
	recreate_services frontend
	wait_for_frontend
	recreate_services queue-short queue-long scheduler
	wait_for_workers
	set_release_state RECREATED
	verify_candidate_runtime "${candidate_id}"
	install_durable_candidate
	set_release_state VERIFIED

	docker exec "$(service_container backend)" bash -lc \
		"cd /home/frappe/frappe-bench && bench --site '${SITE}' set-maintenance-mode off"
	wait_for_public_health
	verify_workers_and_scheduler
	verify_protected_services_unchanged
	current_link="${RELEASES_DIR}/.current-stripev2-${RELEASE_ID}"
	ln -s "${ACTIVE_RELEASE_DIR}" "${current_link}"
	mv -Tf "${current_link}" "${RELEASES_DIR}/current-stripev2"
	set_release_state COMPLETE
	trap - EXIT ERR INT TERM HUP
	log "release ${RELEASE_ID} is live and verified"
}

rollback_preflight() {
	local manifest="$1"
	local service
	local recorded_id

	require_operator_runtime
	verify_free_space
	capture_protected_services
	for service in "${PROTECTED_SERVICES[@]}"; do
		recorded_id="$(manifest_get "${manifest}" \
			"PROTECTED_$(service_manifest_key "${service}")_ID")"
		[[ "${PROTECTED_CONTAINER_IDS[${service}]}" == "${recorded_id}" ]] \
			|| die "protected service identity changed: ${service}"
	done
	APPS_VOLUME_PATH="$(docker volume inspect "${APPS_VOLUME}" --format '{{.Mountpoint}}')"
	SITES_VOLUME_PATH="$(docker volume inspect "${SITES_VOLUME}" --format '{{.Mountpoint}}')"
	CURRENT_IMAGE_ID="$(docker image inspect "${IMAGE_REPOSITORY}:latest" --format '{{.Id}}')"
	CURRENT_COMMIT="$(docker image inspect "${IMAGE_REPOSITORY}:latest" \
		--format '{{index .Config.Labels "com.coengine.stripev2.commit"}}')"
	validate_commit "${CURRENT_COMMIT}"
}

rollback_failed() {
	local exit_code=$?
	local state="ROLLBACK_FAILED_QUARANTINE_UNCONFIRMED"
	trap - EXIT ERR INT TERM HUP
	set +e
	if [[ "${WORKER_TERM_SENT}" -eq 1 && "${WORKERS_DRAINED}" -eq 0 ]]; then
		if ! try_set_maintenance_mode on; then
			warn "maintenance mode could not be confirmed"
		fi
		if quarantine_application_services; then
			set_release_state ROLLBACK_FAILED_WORKER_DRAIN
		else
			set_release_state ROLLBACK_FAILED_WORKER_DRAIN_QUARANTINE_UNCONFIRMED
		fi
		warn "workers are still warm-stopping; do not signal or recreate them"
		(( exit_code != 0 )) || exit_code=1
		exit "${exit_code}"
	fi
	warn "rollback did not pass verification; production will remain fail-closed"
	if ! try_set_maintenance_mode on; then
		warn "maintenance mode could not be confirmed"
	fi
	if quarantine_application_services; then
		state="ROLLBACK_FAILED_QUARANTINED"
	else
		warn "not every application service could be confirmed quarantined"
	fi
	set_release_state "${state}"
	(( exit_code != 0 )) || exit_code=1
	exit "${exit_code}"
}

rollback_release() {
	local manifest
	local state
	local target_commit
	local rollback_tag
	local rollback_image_id
	local previous_image_id
	local previous_commit
	local candidate_image_id
	local previous_stripe_digest
	local app_root
	local live_path
	local restore_source
	local restore_staging
	local rollback_copy

	validate_release_id "${RELEASE_ID}"
	[[ "${CONFIRM_VALUE}" == "${RELEASE_ID}" ]] || die "--confirm must exactly match the release ID"
	[[ "${ACKNOWLEDGE_DB_NOT_RESTORED}" -eq 1 ]] \
		|| die "rollback requires --acknowledge-db-not-restored"
	ACTIVE_RELEASE_DIR="$(release_path "${RELEASE_ID}")"
	manifest="${ACTIVE_RELEASE_DIR}/release.env"
	[[ -f "${manifest}" ]] || die "release manifest is missing"
	state="$(manifest_get "${manifest}" STATE)"
	case "${state}" in
		COMPLETE|FAILED_AFTER_MIGRATION|FAILED_AFTER_MIGRATION_QUARANTINED)
			;;
		*)
			die "release is not eligible for rollback: ${state}"
			;;
	esac
	target_commit="$(manifest_get "${manifest}" TARGET_COMMIT)"
	rollback_tag="$(manifest_get "${manifest}" ROLLBACK_IMAGE_TAG)"
	previous_image_id="$(manifest_get "${manifest}" PREVIOUS_IMAGE_ID)"
	previous_commit="$(manifest_get "${manifest}" PREVIOUS_COMMIT)"
	candidate_image_id="$(manifest_get "${manifest}" CANDIDATE_IMAGE_ID)"
	previous_stripe_digest="$(manifest_get "${manifest}" PREVIOUS_STRIPE_TREE_DIGEST)"
	validate_commit "${target_commit}"
	validate_commit "${previous_commit}"
	validate_sha256 "${previous_stripe_digest}"

	require_operator_runtime
	acquire_lock
	rollback_preflight "${manifest}"
	[[ "${CURRENT_COMMIT}" == "${target_commit}" ]] \
		|| die "production is not currently running this release"
	[[ "${CURRENT_IMAGE_ID}" == "${candidate_image_id}" ]] \
		|| die "current image does not match the release candidate"
	rollback_image_id="$(docker image inspect "${rollback_tag}" --format '{{.Id}}')"
	[[ "${rollback_image_id}" == "${previous_image_id}" ]] \
		|| die "rollback image does not match the recorded previous image"
	APPS_VOLUME_PATH="$(docker volume inspect "${APPS_VOLUME}" --format '{{.Mountpoint}}')"
	restore_source="${ACTIVE_RELEASE_DIR}/backups/apps/stripe_integration-cutover"
	[[ -d "${restore_source}" ]] || die "Stripe rollback snapshot is missing"
	[[ "$(tree_hash "${restore_source}")" == "${previous_stripe_digest}" ]] \
		|| die "Stripe rollback snapshot has drifted"

	warn "this rollback does not restore the database or site files"
	app_root="${APPS_VOLUME_PATH}"
	live_path="${app_root}/stripe_integration"
	restore_staging="${app_root}/.stripe_integration.restore-${RELEASE_ID}"
	rollback_copy="${app_root}/.stripe_integration.rollback-from-${RELEASE_ID}"
	assert_exact_path "${live_path}" "${APPS_VOLUME_PATH}/stripe_integration"
	assert_exact_path "${restore_staging}" "${APPS_VOLUME_PATH}/.stripe_integration.restore-${RELEASE_ID}"
	assert_exact_path "${rollback_copy}" "${APPS_VOLUME_PATH}/.stripe_integration.rollback-from-${RELEASE_ID}"
	[[ -d "${live_path}" && ! -e "${restore_staging}" && ! -e "${rollback_copy}" ]] \
		|| die "rollback app paths are not clean"
	cp -a "${restore_source}" "${restore_staging}"
	[[ "$(tree_hash "${restore_staging}")" == "${previous_stripe_digest}" ]] \
		|| die "staged Stripe rollback copy is incomplete"

	trap rollback_failed EXIT ERR INT TERM HUP
	try_set_maintenance_mode on
	stop_app_service scheduler
	if [[ "${state}" == "COMPLETE" ]]; then
		drain_queue_workers
	else
		quarantine_queue_workers || die "failed release workers could not be confirmed quarantined"
	fi
	for service in frontend websocket backend; do
		stop_app_service "${service}"
	done
	mv "${live_path}" "${rollback_copy}"
	mv "${restore_staging}" "${live_path}"
	[[ "$(tree_hash "${live_path}")" == "${previous_stripe_digest}" ]] \
		|| die "live Stripe rollback copy is incomplete"
	docker image tag "${rollback_tag}" "${IMAGE_REPOSITORY}:latest"
	restore_durable_snapshot
	CURRENT_APPS_DIGEST="$(docker image inspect "${previous_image_id}" \
		--format '{{index .Config.Labels "com.coengine.runtime-apps.sha256"}}')"
	CURRENT_ASSETS_DIGEST="$(docker image inspect "${previous_image_id}" \
		--format '{{index .Config.Labels "com.coengine.runtime-assets.sha256"}}')"
	validate_sha256 "${CURRENT_APPS_DIGEST}"
	validate_sha256 "${CURRENT_ASSETS_DIGEST}"
	recreate_services backend websocket
	wait_for_backend
	recreate_services frontend
	wait_for_frontend
	recreate_services queue-short queue-long scheduler
	wait_for_workers
	docker exec "$(service_container backend)" bash -lc \
		"cd /home/frappe/frappe-bench && bench --site '${SITE}' clear-cache"
	verify_application_runtime "${previous_image_id}" "${previous_commit}"
	set_release_state ROLLBACK_VERIFIED_MAINTENANCE_ON
	try_set_maintenance_mode off
	wait_for_public_health
	verify_workers_and_scheduler
	verify_protected_services_unchanged
	set_release_state ROLLED_BACK_CODE_ONLY
	trap - EXIT ERR INT TERM HUP
	warn "code rollback completed; use the verified site backup if the migration also requires data restoration"
}

status_command() {
	production_preflight
	printf 'STATUS=healthy\nCOMMIT=%s\nIMAGE=%s\nAPPS=%s\nSENTRY=%s\n' \
		"${CURRENT_COMMIT}" "${CURRENT_IMAGE_ID}" "${EXPECTED_APP_COUNT}" "${EXPECTED_SENTRY_VERSION}"
}

self_test() {
	local temporary
	local valid_root
	local valid_archive
	local valid_digest
	local test_commit
	local mismatch_commit="ffffffffffffffffffffffffffffffffffffffff"
	local malicious_archive
	require_commands git python3 sha256sum tar
	temporary="$(mktemp -d)"
	SELF_TEST_TEMPORARY="${temporary}"
	trap 'rm -rf -- "${SELF_TEST_TEMPORARY}"' EXIT
	valid_root="${temporary}/valid"
	install -d \
		"${valid_root}/stripe_integration/public/js" \
		"${valid_root}/stripe_integration/stripe_integration"
	touch \
		"${valid_root}/pyproject.toml" \
		"${valid_root}/stripe_integration/hooks.py" \
		"${valid_root}/stripe_integration/public/js/subscription_stripe.js" \
		"${valid_root}/stripe_integration/stripe_integration/verify_post_upgrade.py"
	git -C "${valid_root}" init -q
	git -C "${valid_root}" config user.name "Deployment Self Test"
	git -C "${valid_root}" config user.email "deploy-self-test@example.invalid"
	git -C "${valid_root}" add .
	git -C "${valid_root}" commit -q -m "self-test fixture"
	test_commit="$(git -C "${valid_root}" rev-parse HEAD)"
	valid_archive="${temporary}/valid.tar.gz"
	git -C "${valid_root}" archive --format=tar.gz --prefix=stripe_integration/ \
		-o "${valid_archive}" "${test_commit}"
	valid_digest="$(sha256sum "${valid_archive}" | awk '{print $1}')"
	validate_archive "${valid_archive}" "${valid_digest}" "${test_commit}"
	(validate_archive "${valid_archive}" "${valid_digest}" "${mismatch_commit}") \
		>/dev/null 2>&1 && die "archive commit binding self-test failed"
	(validate_commit "not-a-commit") >/dev/null 2>&1 && die "invalid commit self-test failed"

	malicious_archive="${temporary}/malicious.tar.gz"
	python3 - "${malicious_archive}" "${test_commit}" <<'PY'
import io
import sys
import tarfile

with tarfile.open(
    sys.argv[1],
    "w:gz",
    format=tarfile.PAX_FORMAT,
    pax_headers={"comment": sys.argv[2]},
) as bundle:
    member = tarfile.TarInfo("../escape")
    payload = b"unsafe"
    member.size = len(payload)
    bundle.addfile(member, io.BytesIO(payload))
PY
	valid_digest="$(sha256sum "${malicious_archive}" | awk '{print $1}')"
	(validate_archive "${malicious_archive}" "${valid_digest}" "${test_commit}") >/dev/null 2>&1 \
		&& die "archive traversal self-test failed"
	printf 'SELF_TEST_OK\n'
}

parse_options "$@"

case "${COMMAND}" in
	help)
		usage
		;;
	self-test)
		self_test
		;;
	status)
		status_command
		;;
	preflight)
		validate_target_options
		production_preflight
		if [[ "${CURRENT_COMMIT}" == "${TARGET_COMMIT}" ]]; then
			printf 'TARGET_STATUS=already-deployed\n'
		else
			printf 'TARGET_STATUS=ready-to-prepare\n'
		fi
		;;
	prepare)
		prepare_release
		;;
	activate)
		[[ -n "${RELEASE_ID}" && -n "${CONFIRM_VALUE}" ]] || die "activate requires --release and --confirm"
		activate_release
		;;
	deploy)
		validate_target_options
		[[ "${CONFIRM_VALUE}" == "${TARGET_COMMIT}" ]] \
			|| die "deploy requires --confirm with the exact full commit SHA"
		prepare_release
		if [[ "${DEPLOY_NOOP}" -eq 0 ]]; then
			CONFIRM_VALUE="${TARGET_COMMIT}"
			activate_release
		fi
		;;
	rollback)
		[[ -n "${RELEASE_ID}" && -n "${CONFIRM_VALUE}" ]] || die "rollback requires --release and --confirm"
		rollback_release
		;;
	*)
		usage >&2
		die "unknown command: ${COMMAND}"
		;;
esac
