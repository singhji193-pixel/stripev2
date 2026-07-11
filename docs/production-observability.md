# Production Observability

ERPNext runtime errors are monitored through Frappe's native Sentry integration. This is deployment-level configuration: the Stripe app does not initialize a second Sentry client or contain a DSN.

## Runtime Configuration

The production image pins `sentry-sdk==1.45.1`. Runtime secrets and sampling are supplied by the deployment environment:

```yaml
environment:
  FRAPPE_SENTRY_DSN: "${FRAPPE_SENTRY_DSN:?Set FRAPPE_SENTRY_DSN in the deployment .env}"
  SENTRY_ENVIRONMENT: "${SENTRY_ENVIRONMENT:-production}"
  SENTRY_TRACING_SAMPLE_RATE: "${SENTRY_TRACING_SAMPLE_RATE:-0.05}"
```

Set **Enable Telemetry** in Frappe System Settings. Keep `FRAPPE_SENTRY_DSN` only in the protected deployment `.env`; `.env` files are ignored by Git.

The production profile uses:

- environment `production`
- 5% transaction tracing
- database query monitoring disabled
- profiling disabled
- Python local-variable capture disabled to reduce sensitive-data exposure and remain compatible with Python 3.14 frame locals

## Coverage

The DSN is provided to the Frappe web backend and both RQ queue workers. This covers:

- unhandled Desk and HTTP request failures
- backend exceptions enriched with Frappe site and user tags
- scheduled jobs after they are enqueued into RQ
- short- and long-queue job failures

The scheduler loop and Node websocket process are not Python application workers. Monitor their container health and logs separately.

## RQ Compatibility

The production Frappe 16 runtime carries narrow compatibility overlays for its native Sentry path. They:

1. handle timezone-aware RQ enqueue timestamps,
2. capture exceptions from Frappe's `execute_job` failure paths,
3. flush Sentry inside forked RQ work horses before they exit, and
4. disable local-variable serialization under Python 3.14.

These files belong in the deployment image, not in this app. A future Frappe upgrade must compare the upstream implementations before retaining, changing, or removing the overlays.

## Static Asset Integrity

Frappe's `assets.json` is a bench-wide manifest. Do not run `bench build --app stripe_integration` in the production image: this app has no compiled asset bundle, and a single-app build can rewrite the shared manifest without refreshing every app's public files.

The production deployment keeps the captured asset archive and app snapshot paired. It mounts the versioned asset tree read-only at `sites/assets`, validates every manifest path before container recreation, and runs the same verifier as the frontend health check. This avoids stale Docker asset volumes serving hashes from a different image.

An approved image update must prepare and validate the runtime asset tree before switching the web, queue, scheduler, and websocket services. A missing manifest key or referenced file is a deployment failure, even if the HTML endpoint still returns HTTP 200.

## Verification Checklist

After an approved deployment change:

1. Confirm the exact image is running on backend, queues, scheduler, frontend, and websocket services.
2. Confirm the runtime asset verifier passes and every manifest URL returns HTTP 200 with the expected content type.
3. Confirm the public ERPNext endpoint returns HTTP 200 and a browser renders the styled login or Desk page.
4. Confirm both RQ workers are online and scheduler jobs complete.
5. Trigger one controlled backend or queued exception with no customer data only when telemetry itself changed.
6. Confirm the Sentry event has the production environment, Frappe release, transaction, site, and user tags.
7. Resolve the verification issue and remove its ERPNext Error Log and queued-job artifacts.
8. Confirm no new failed or queued Stripe test events remain.

Never use a real customer invoice, payment method, webhook, or accounting document for telemetry verification.

## Change Control

Do not rebuild the image, run `bench migrate`, recreate containers, or modify accounting records as an informal troubleshooting step. Obtain operator approval, preserve a deployment backup, apply the smallest change, and repeat the checklist above.

Never commit a Sentry DSN, authentication token, Stripe key, webhook secret, customer identifier, or production `.env` file.
