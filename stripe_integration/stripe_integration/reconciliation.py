import frappe
import stripe
from frappe.utils import add_to_date, now_datetime

from stripe_integration.stripe_integration.accounting import MariaDBNamedLock
from stripe_integration.stripe_integration.event_log import mark_event_status
from stripe_integration.stripe_integration.stripe_fees import (
    audit_unposted_fee_entries,
    ensure_fee_posted,
)
from stripe_integration.stripe_integration.utils import get_api_key

MAX_EVENT_RETRIES = 5


def _enabled_company_abbrs() -> list[str]:
    return [
        row.get("company_abbr")
        for row in frappe.get_all(
            "Stripe Account",
            filters={"enabled": 1},
            fields=["company_abbr"],
            order_by="company_abbr asc",
        )
        if row.get("company_abbr")
    ]


def _set_retry_state(event_log_name: str, retry_count: int, error: str | None = None):
    values = {}
    meta = frappe.get_meta("Stripe Event Log")
    if meta.get_field("retry_count"):
        values["retry_count"] = retry_count
    if meta.get_field("last_retry_at"):
        values["last_retry_at"] = now_datetime()
    if error:
        values["error"] = error[:2000]
    if values:
        frappe.db.set_value("Stripe Event Log", event_log_name, values, update_modified=False)


def reconcile_nonterminal_events(limit: int = 25):
    """Replay recent failed/stuck Stripe events from Stripe's canonical copy."""

    frappe.set_user("Administrator")
    cutoff = add_to_date(now_datetime(), minutes=-5)
    rows = frappe.get_all(
        "Stripe Event Log",
        filters={
            "status": ["in", ["Queued", "Processing", "Failed"]],
            "modified": ["<=", cutoff],
        },
        fields=[
            "name",
            "event_id",
            "event_type",
            "company_abbr",
            "retry_count",
        ],
        order_by="modified asc",
        limit_page_length=max(1, min(int(limit or 25), 100)),
    )

    results = []
    from stripe_integration.stripe_integration.webhook import _dispatch_verified_event

    for row in rows:
        event_id = row.get("event_id") or ""
        company_abbr = (row.get("company_abbr") or "").strip().upper()
        retry_count = int(row.get("retry_count") or 0)
        if not event_id.startswith("evt_") or not company_abbr or retry_count >= MAX_EVENT_RETRIES:
            continue

        try:
            with MariaDBNamedLock(f"stripe-event-{event_id}", timeout=30):
                current_status = frappe.db.get_value("Stripe Event Log", row.get("name"), "status")
                if current_status not in {"Queued", "Processing", "Failed"}:
                    continue

                event = stripe.Event.retrieve(event_id, api_key=get_api_key(company_abbr))
                result = _dispatch_verified_event(event, company_abbr)
                if result.get("retryable") and not result.get("handled"):
                    raise RuntimeError(result.get("reason") or "reconciliation_failed")

                status = "Completed" if result.get("handled") else "Ignored"
                mark_event_status(event_id, status, None if result.get("handled") else result.get("reason"))
                _set_retry_state(row.get("name"), retry_count + 1)
                frappe.db.commit()
                results.append({"event_id": event_id, "status": status, "result": result})
        except Exception as exc:
            frappe.db.rollback()
            _set_retry_state(row.get("name"), retry_count + 1, str(exc))
            mark_event_status(event_id, "Failed", str(exc))
            frappe.db.commit()
            results.append({"event_id": event_id, "status": "Failed", "error": str(exc)[:300]})

    return results


def reconcile_unposted_fees(limit_per_company: int = 100):
    frappe.set_user("Administrator")
    results = []
    for company_abbr in _enabled_company_abbrs():
        audit = audit_unposted_fee_entries(company_abbr, limit=limit_per_company)
        for row in audit.get("missing") or []:
            if row.get("reason") != "missing_fee_je":
                continue
            result = ensure_fee_posted(
                company_abbr,
                row.get("payment_intent"),
                remark_ctx=f"scheduled reconciliation {row.get('payment_entry')}",
                enqueue_retry=False,
            )
            results.append(
                {
                    "company_abbr": company_abbr,
                    "payment_entry": row.get("payment_entry"),
                    "result": result,
                }
            )
    return results


def run_hourly_reconciliation():
    if not int(frappe.db.get_single_value("Stripe Settings", "enabled") or 0):
        return {"enabled": False}
    return {
        "events": reconcile_nonterminal_events(),
        "fees": reconcile_unposted_fees(),
    }


@frappe.whitelist()
def get_native_flow_health():
    frappe.only_for("System Manager")
    event_counts = frappe.db.sql(
        """
        SELECT status, COUNT(*)
        FROM `tabStripe Event Log`
        GROUP BY status
        """
    )

    companies = []
    for company_abbr in _enabled_company_abbrs():
        account = frappe.get_doc("Stripe Account", company_abbr)
        clearing = account.get("stripe_clearing_account")
        clearing_balance = 0.0
        if clearing:
            clearing_balance = float(
                frappe.db.sql(
                    """
                    SELECT COALESCE(SUM(debit-credit), 0)
                    FROM `tabGL Entry`
                    WHERE account=%s AND is_cancelled=0
                    """,
                    (clearing,),
                )[0][0]
                or 0
            )
        companies.append(
            {
                "company_abbr": company_abbr,
                "company": account.get("company"),
                "test_mode": bool(account.get("test_mode")),
                "payout_sync_enabled": bool(account.get("payout_sync_enabled")),
                "mappings_complete": all(
                    account.get(field)
                    for field in ("stripe_clearing_account", "stripe_fee_account", "bank_account")
                ),
                "clearing_account": clearing,
                "clearing_balance": clearing_balance,
                "fee_audit": audit_unposted_fee_entries(company_abbr, limit=100),
            }
        )

    return {
        "event_counts": {status: int(count) for status, count in event_counts},
        "companies": companies,
        "scheduler_enabled": not bool(frappe.conf.get("pause_scheduler")),
    }
