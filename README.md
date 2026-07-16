# Stripe Integration

Native Stripe integration for ERPNext and Frappe. The app connects ERPNext Sales Invoices and Subscriptions to Stripe-hosted payments, then reconciles Stripe webhooks back into ERPNext accounting documents.

## Capabilities

- Per-company Stripe accounts and webhook account binding
- Sales Invoice payment links and submitted Payment Entries
- ERPNext subscription pricing, discounts, taxes, and billing anchors in Stripe
- Saved-card reuse and secure payment-method setup links
- Refunds backed by ERPNext credit notes
- Stripe Clearing, fee, payout, and hourly reconciliation flows
- Replay-safe webhook processing with audit logs and MariaDB locks

The current `main` flow is covered by 44 mocked-Frappe tests on Python 3.11. Production verification also covers Python 3.14 imports and read-only account checks.

## Installation

Install the app with Bench, then migrate the target site:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/singhji193-pixel/stripev2.git --branch main
bench --site $SITE_NAME install-app stripe_integration
bench --site $SITE_NAME migrate
```

Configure one enabled `Stripe Account` document per ERPNext company. Keep Stripe API keys and webhook signing secrets in protected Frappe Password fields or deployment secrets; never commit them to this repository.

## Verification

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

GitHub Actions runs the same suite for pushes and pull requests targeting `main` or `develop`.

## Operations

- [Production observability](docs/production-observability.md)
- [Production deployment tool and runbook](ops/production/README.md)
- Review `Stripe Event Log` and the hourly reconciliation result before correcting accounting data.
- Manual Stripe payouts are flagged for review because Stripe does not expose their full component balance transactions. Automatic payout Journal Entries post only after every component is matched.

Production rebuilds, migrations, container recreation, and accounting corrections require an approved maintenance action and a verified backup.

## Contributing

This app uses `pre-commit` with Ruff, ESLint, Prettier, and pyupgrade:

```bash
pre-commit install
pre-commit run --all-files
```

## License

MIT
