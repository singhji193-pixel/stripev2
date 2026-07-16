# Production deployment

`coengine-stripe-deploy.sh` is the source-controlled deployment tool for the
Stripe integration on `next.coengine.ai`. It packages the same guarded rollout
used in production without storing a Stripe key, Sentry DSN, database password,
or Compose environment value.

## Storage locations

Keep the tool in all of these places:

1. This GitHub repository is the source of truth and review history.
2. Install the script at `/usr/local/sbin/coengine-stripe-deploy` on the VPS.
3. Install `runtime-overlays.sha256` at
   `/etc/coengine/runtime-overlays.sha256` on the VPS.
4. Keep a root-owned versioned copy under
   `/opt/coengine/deployment-tools/<script-commit>/` for disaster recovery.
5. Each execution copies the exact script and rollback material into
   `/opt/coengine/releases/<release-id>/`.
6. Copy completed site backups to an encrypted off-server object store. The VPS
   release directory is rollback convenience, not the only backup location.

The script and checksum manifest are root-owned. Recommended modes are `0750`
for the executable, `0644` for the checksum manifest, and `0700` for release
directories. Do not put production secrets in GitHub or beside the script.

## One-time installation

From a reviewed checkout on the VPS:

```bash
install -o root -g root -m 0750 \
  ops/production/coengine-stripe-deploy.sh \
  /usr/local/sbin/coengine-stripe-deploy
install -d -o root -g root -m 0755 /etc/coengine
install -o root -g root -m 0644 \
  ops/production/runtime-overlays.sha256 \
  /etc/coengine/runtime-overlays.sha256
test "$(sha256sum ops/production/coengine-stripe-deploy.sh | awk '{print $1}')" = \
  "$(sha256sum /usr/local/sbin/coengine-stripe-deploy | awk '{print $1}')"
test "$(sha256sum ops/production/runtime-overlays.sha256 | awk '{print $1}')" = \
  "$(sha256sum /etc/coengine/runtime-overlays.sha256 | awk '{print $1}')"
```

Validate the installed copy without changing production:

```bash
coengine-stripe-deploy self-test
coengine-stripe-deploy status
```

## Create a release archive

Create the archive from a clean local checkout with `git archive`. A full
40-character commit, the archive's embedded Git commit metadata, and the
archive checksum must all agree, so the VPS never deploys a moving branch or a
tarball mislabeled as another commit.

```bash
commit="$(git rev-parse HEAD)"
git archive --format=tar.gz --prefix=stripe_integration/ \
  -o "stripev2-${commit}.tar.gz" "${commit}"
sha256sum "stripev2-${commit}.tar.gz"
scp "stripev2-${commit}.tar.gz" root@82.180.137.121:/opt/coengine/incoming/
```

Never include `.env`, API keys, customer data, or a working-tree snapshot.

## Deploy

Run the online preparation first. It creates an uncompressed apps-volume
snapshot, builds an immutable candidate, runs the unit suite, and verifies the
paired assets and Sentry overlays while the public site stays online.

```bash
coengine-stripe-deploy prepare \
  --commit FULL_COMMIT_SHA \
  --archive /opt/coengine/incoming/stripev2-FULL_COMMIT_SHA.tar.gz \
  --sha256 ARCHIVE_SHA256
```

The command prints a release ID. Activate it only after preparation succeeds:

```bash
coengine-stripe-deploy activate \
  --release RELEASE_ID \
  --confirm FULL_COMMIT_SHA
```

For an attended one-command rollout, `deploy` accepts the preparation options
plus `--confirm FULL_COMMIT_SHA`.

The cutover enables maintenance mode, stops the scheduler, lets both RQ workers
finish any active job with an indefinite warm shutdown, and then stops the
remaining application services. It creates a compressed
database/public/private-file backup, switches only the Stripe app directory,
migrates, and recreates services in waves with `--no-deps`. It never stops or
recreates MariaDB, Redis, `configurator`, or `create-site`, and it never runs a
single-app asset build. Never interrupt or send a second signal to a worker
that is warm-stopping; RQ treats a second signal as a forced shutdown.

## Failure and rollback

Before migration starts, a cutover failure automatically restores the previous
app and image. Once migration starts, the tool fails closed with maintenance
mode enabled and application services quarantined because restoring only code
is not a complete database rollback. Quarantine suppresses container restart
policies, stops the public app services, and warm-stops RQ workers; a future
Compose recreation restores the declared restart policies.

For a backward-compatible migration, an operator can restore the previous code,
image, and durable build definition explicitly:

```bash
coengine-stripe-deploy rollback \
  --release RELEASE_ID \
  --confirm RELEASE_ID \
  --acknowledge-db-not-restored
```

That command deliberately does **not** restore database or site files. For an
incompatible or partially applied migration, inspect the release's verified
backup under `backups/site/` and follow the ERPNext database/file restoration
runbook while maintenance remains enabled.

Do not delete old releases automatically. After an agreed retention period,
copy their backups off-server before removing them manually.
