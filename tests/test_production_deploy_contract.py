from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ops" / "production" / "coengine-stripe-deploy.sh"
RUNBOOK = ROOT / "ops" / "production" / "README.md"


def _array_values(source: str, name: str) -> list[str]:
	match = re.search(rf"readonly -a {name}=\(\n(?P<body>.*?)\n\)", source, re.DOTALL)
	if not match:
		raise AssertionError(f"missing Bash array: {name}")
	return [line.strip() for line in match.group("body").splitlines() if line.strip()]


def _between(source: str, start: str, end: str) -> str:
	start_index = source.index(start)
	end_index = source.index(end, start_index)
	return source[start_index:end_index]


class ProductionDeployContractTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		cls.source = SCRIPT.read_text(encoding="utf-8")

	def test_service_scope_is_explicit(self) -> None:
		self.assertEqual(
			_array_values(self.source, "APP_SERVICES"),
			["backend", "frontend", "websocket", "queue-short", "queue-long", "scheduler"],
		)
		self.assertEqual(
			_array_values(self.source, "PROTECTED_SERVICES"),
			["db", "redis-cache", "redis-queue", "redis-socketio"],
		)

	def test_dangerous_deployment_commands_are_absent(self) -> None:
		for forbidden in (
			"compose down",
			"--remove-orphans",
			"docker system prune",
			"docker volume rm",
			"bench build",
			"set -x",
			"eval ",
			"docker compose config",
		):
			with self.subTest(forbidden=forbidden):
				self.assertNotIn(forbidden, self.source)

	def test_cutover_requires_backup_and_scoped_recreation(self) -> None:
		activation = _between(self.source, "activate_release() {", "rollback_preflight() {")
		ordered_steps = (
			"set-maintenance-mode on",
			"drain_queue_workers",
			"backup --with-files --compress",
			"MIGRATION_STARTED=1",
			"verify_candidate_runtime",
			"set-maintenance-mode off",
		)
		positions = [activation.index(step) for step in ordered_steps]
		self.assertEqual(positions, sorted(positions))
		self.assertIn("compose up -d --no-deps --force-recreate", self.source)
		self.assertNotIn("stop_app_service queue-short", activation)
		self.assertNotIn("stop_app_service queue-long", activation)

	def test_archive_and_confirmation_are_pinned(self) -> None:
		self.assertIn("^[0-9a-f]{40}$", self.source)
		self.assertIn("release archive checksum mismatch", self.source)
		self.assertIn('bundle.pax_headers.get("comment")', self.source)
		self.assertIn("release archive commit does not match --commit", self.source)
		self.assertIn("--confirm must exactly match", self.source)
		self.assertIn("unsafe archive member", self.source)

	def test_prepare_locks_before_reading_production_state(self) -> None:
		prepare = _between(self.source, "prepare_release() {", "drain_queue_workers() {")
		self.assertLess(prepare.index("acquire_lock"), prepare.index("production_preflight"))

	def test_worker_shutdown_is_warm_and_indefinite(self) -> None:
		drain = _between(self.source, "drain_queue_workers() {", "stop_app_service() {")
		self.assertIn('docker stop --signal TERM --time -1', drain)
		self.assertIn("WORKERS_DRAINING", drain)
		self.assertIn("WORKERS_DRAINED", drain)
		self.assertNotIn("docker kill", drain)
		self.assertLess(drain.index("set_release_state WORKERS_DRAINING"), drain.index("WORKER_TERM_SENT=1"))
		quarantine = _between(
			self.source,
			"quarantine_queue_workers() {",
			"verify_service_image_contract() {",
		)
		self.assertIn("queue workers are already warm-stopping", quarantine)
		self.assertIn('"${WORKER_TERM_SENT}" -eq 1', quarantine)
		self.assertIn('docker stop --signal TERM --time -1', quarantine)
		self.assertIn("docker update --restart=no", quarantine)
		self.assertIn("running|restarting", quarantine)
		self.assertIn('"${restart_policy}" == "no"', quarantine)

	def test_automatic_recovery_verifies_before_reopening(self) -> None:
		recovery = _between(self.source, "restore_before_migration() {", "activation_error() {")
		self.assertLess(
			recovery.index("verify_application_runtime"),
			recovery.index("try_set_maintenance_mode off"),
		)
		self.assertIn("trap recovery_failed EXIT", recovery)
		failure_handlers = _between(self.source, "recovery_failed() {", "install_durable_candidate() {")
		self.assertGreaterEqual(failure_handlers.count("quarantine_application_services"), 3)

	def test_manual_rollback_is_staged_and_fail_closed(self) -> None:
		rollback = _between(self.source, "rollback_release() {", "status_command() {")
		self.assertIn('rollback_image_id}" == "${previous_image_id}', rollback)
		self.assertLess(rollback.index('cp -a "${restore_source}"'), rollback.index('mv "${live_path}"'))
		self.assertLess(
			rollback.index("verify_application_runtime"),
			rollback.index("try_set_maintenance_mode off"),
		)
		self.assertIn("trap rollback_failed EXIT ERR INT TERM HUP", rollback)

	def test_overlay_manifest_requires_exact_file_set(self) -> None:
		self.assertEqual(
			_array_values(self.source, "EXPECTED_OVERLAY_FILES"),
			[
				"frappe-app.py",
				"frappe-background_jobs.py",
				"frappe-sentry.py",
				"verify_frappe_assets.py",
			],
		)
		self.assertIn("overlay manifest does not contain the exact approved file set", self.source)

	def test_all_manifest_assets_are_checked_over_http(self) -> None:
		asset_check = _between(
			self.source,
			"verify_public_asset_manifest() {",
			"verify_workers_and_scheduler() {",
		)
		self.assertIn("for key, value in sorted(manifest.items())", asset_check)
		self.assertIn("%{content_type}", asset_check)

	def test_runbook_names_all_storage_locations(self) -> None:
		runbook = RUNBOOK.read_text(encoding="utf-8")
		self.assertIn("GitHub repository", runbook)
		self.assertIn("/usr/local/sbin/coengine-stripe-deploy", runbook)
		self.assertIn("/opt/coengine/releases/", runbook)
		self.assertIn("encrypted off-server object store", runbook)


if __name__ == "__main__":
	unittest.main()
