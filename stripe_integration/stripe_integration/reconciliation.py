from datetime import datetime, timezone

import frappe
import stripe
from frappe.utils import add_to_date, now_datetime, nowdate

from stripe_integration.stripe_integration.accounting import MariaDBNamedLock
from stripe_integration.stripe_integration.event_log import mark_event_status
from stripe_integration.stripe_integration.stripe_fees import (
    audit_unposted_fee_entries,
    ensure_fee_posted,
)
from stripe_integration.stripe_integration.subscription_pause import (
    PAUSE_ACTIVE_FIELD,
    PAUSE_LAST_RECONCILED_AT_FIELD,
    PAUSE_OPERATION_FIELD,
    PAUSE_STATE_FIELD,
    PENDING_RESUME_AT_FIELD,
    RESUME_AT_FIELD,
    STATE_CANCELLING,
    STATE_PAUSING,
    STATE_RESUMING,
)
from stripe_integration.stripe_integration.utils import get_api_key

MAX_EVENT_RETRIES = 168
MAX_DUE_PAUSE_RECONCILIATIONS = 100


def _utc_now_timestamp() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _rotate_due_pause_row(subscription_name: str) -> None:
    frappe.db.set_value(
        "Subscription",
        subscription_name,
        PAUSE_LAST_RECONCILED_AT_FIELD,
        now_datetime(),
        update_modified=False,
    )


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


def _set_event_row_status(row, event_id: str, status: str, error: str | None = None) -> None:
    if event_id:
        mark_event_status(event_id, status, error)
        return
    frappe.db.set_value(
        "Stripe Event Log",
        row.get("name"),
        {"status": status, "error": error},
        update_modified=False,
    )


def reconcile_due_subscription_pauses(limit: int = MAX_DUE_PAUSE_RECONCILIATIONS):
    """Give coordinated pauses an hourly, transaction-isolated process pass."""

    frappe.set_user("Administrator")
    page_limit = max(
        1,
        min(int(limit or MAX_DUE_PAUSE_RECONCILIATIONS), MAX_DUE_PAUSE_RECONCILIATIONS),
    )
    effective_resume_at = f"""
        CASE
            WHEN COALESCE(`{PAUSE_STATE_FIELD}`, '') = %s
                THEN NULLIF(`{PENDING_RESUME_AT_FIELD}`, '')
            ELSE NULLIF(`{RESUME_AT_FIELD}`, '')
        END
    """
    rows = frappe.db.sql(
        f"""
        SELECT `name`
        FROM `tabSubscription`
        WHERE `docstatus` < 2
          AND `{PAUSE_ACTIVE_FIELD}` = 1
          AND COALESCE(`{PAUSE_STATE_FIELD}`, '') NOT IN (%s, %s)
          AND ({effective_resume_at}) REGEXP '^[0-9]+$'
          AND CAST(({effective_resume_at}) AS UNSIGNED) <= %s
        ORDER BY `{PAUSE_LAST_RECONCILED_AT_FIELD}` ASC,
                 CAST(({effective_resume_at}) AS UNSIGNED) ASC,
                 `name` ASC
        LIMIT %s
        """,
        (
            STATE_PAUSING,
            STATE_CANCELLING,
            STATE_RESUMING,
            STATE_RESUMING,
            _utc_now_timestamp(),
            STATE_RESUMING,
            page_limit,
        ),
        as_dict=True,
    )
    posting_date = nowdate()
    results = []
    for row in rows:
        subscription_name = row.get("name")
        try:
            subscription = frappe.get_doc("Subscription", subscription_name)
            processed = subscription.process(posting_date=posting_date)
            _rotate_due_pause_row(subscription_name)
            frappe.db.commit()
            results.append(
                {
                    "subscription": subscription_name,
                    "status": "Completed",
                    "processed": bool(processed),
                }
            )
        except Exception as exc:
            frappe.db.rollback()
            _rotate_due_pause_row(subscription_name)
            frappe.db.commit()
            results.append(
                {
                    "subscription": subscription_name,
                    "status": "Failed",
                    "error": str(exc)[:300],
                }
            )

    return results


def reconcile_nonterminal_events(limit: int = 25):
    """Replay recent failed/stuck Stripe events from Stripe's canonical copy."""

    frappe.set_user("Administrator")
    cutoff = add_to_date(now_datetime(), minutes=-5)
    rows = frappe.get_all(
        "Stripe Event Log",
        filters={
            "status": ["in", ["Queued", "Processing", "Failed"]],
            "modified": ["<=", cutoff],
            "retry_count": ["<", MAX_EVENT_RETRIES],
        },
        fields=[
            "name",
            "event_id",
            "event_type",
            "company_abbr",
            "stripe_object_id",
            "retry_count",
        ],
        # NULL (never retried) rows sort first on MariaDB. Retried poison rows
        # rotate behind other eligible events instead of monopolizing this page.
        order_by="last_retry_at asc, modified asc",
        limit_page_length=max(1, min(int(limit or 25), 100)),
    )

    results = []
    for row in rows:
        event_id = row.get("event_id") or ""
        company_abbr = (row.get("company_abbr") or "").strip().upper()
        retry_count = int(row.get("retry_count") or 0)
        if event_id.startswith("local_outbound_"):
            if retry_count >= MAX_EVENT_RETRIES:
                continue
            try:
                with MariaDBNamedLock(f"stripe-event-{event_id}", timeout=30):
                    current_status = frappe.db.get_value("Stripe Event Log", row.get("name"), "status")
                    if current_status not in {"Queued", "Processing", "Failed"}:
                        continue

                    if not company_abbr:
                        reason = "missing_company_abbr"
                        mark_event_status(event_id, "Ignored", reason)
                        _set_retry_state(row.get("name"), retry_count + 1)
                        frappe.db.commit()
                        results.append(
                            {
                                "event_id": event_id,
                                "status": "Ignored",
                                "reason": reason,
                            }
                        )
                        continue

                    action = (row.get("event_type") or "").rsplit(".", 1)[-1]
                    if action not in {"pause", "resume", "cancel"}:
                        reason = "unsupported_local_action"
                        mark_event_status(event_id, "Ignored", reason)
                        _set_retry_state(row.get("name"), retry_count + 1)
                        frappe.db.commit()
                        results.append(
                            {
                                "event_id": event_id,
                                "status": "Ignored",
                                "reason": reason,
                            }
                        )
                        continue
                    subscription_name = frappe.db.get_value(
                        "Subscription",
                        {"stripe_subscription_id": row.get("stripe_object_id")},
                        "name",
                    )
                    if not subscription_name:
                        raise RuntimeError("subscription_not_found")

                    with MariaDBNamedLock(
                        f"stripe-subscription-action-{subscription_name}",
                        timeout=30,
                    ):
                        current_status = frappe.db.get_value(
                            "Stripe Event Log",
                            row.get("name"),
                            "status",
                        )
                        if current_status not in {"Queued", "Processing", "Failed"}:
                            continue

                        subscription = frappe.get_doc("Subscription", subscription_name)
                        operation_id = subscription.get(PAUSE_OPERATION_FIELD)
                        if not operation_id or not event_id.endswith(operation_id):
                            mark_event_status(event_id, "Ignored", "superseded_operation")
                            frappe.db.commit()
                            continue
                        from stripe_integration.stripe_integration.subscription_sync import (
                            _sync_subscription,
                        )

                        result = _sync_subscription(subscription, action)

                    status = "Completed" if result.get("handled") else "Ignored"
                    mark_event_status(
                        event_id,
                        status,
                        None if result.get("handled") else result.get("reason"),
                    )
                    _set_retry_state(row.get("name"), retry_count + 1)
                    frappe.db.commit()
                    results.append({"event_id": event_id, "status": status, "result": result})
            except Exception as exc:
                frappe.db.rollback()
                _set_retry_state(row.get("name"), retry_count + 1, str(exc))
                mark_event_status(event_id, "Failed", str(exc))
                frappe.db.commit()
                results.append({"event_id": event_id, "status": "Failed", "error": str(exc)[:300]})
            continue

        invalid_reason = None
        if not event_id:
            invalid_reason = "missing_event_id"
        elif not event_id.startswith("evt_"):
            invalid_reason = "unsupported_event_id"
        elif not company_abbr:
            invalid_reason = "missing_company_abbr"
        if invalid_reason:
            try:
                lock_key = event_id or row.get("name")
                with MariaDBNamedLock(f"stripe-event-{lock_key}", timeout=30):
                    current_status = frappe.db.get_value(
                        "Stripe Event Log",
                        row.get("name"),
                        "status",
                    )
                    if current_status not in {"Queued", "Processing", "Failed"}:
                        continue
                    _set_event_row_status(row, event_id, "Ignored", invalid_reason)
                    _set_retry_state(row.get("name"), retry_count + 1)
                    frappe.db.commit()
                    results.append(
                        {
                            "event_id": event_id,
                            "status": "Ignored",
                            "reason": invalid_reason,
                        }
                    )
            except Exception as exc:
                frappe.db.rollback()
                _set_retry_state(row.get("name"), retry_count + 1, str(exc))
                _set_event_row_status(row, event_id, "Failed", str(exc))
                frappe.db.commit()
                results.append(
                    {
                        "event_id": event_id,
                        "status": "Failed",
                        "error": str(exc)[:300],
                    }
                )
            continue

        if retry_count >= MAX_EVENT_RETRIES:
            continue

        try:
            with MariaDBNamedLock(f"stripe-event-{event_id}", timeout=30):
                current_status = frappe.db.get_value("Stripe Event Log", row.get("name"), "status")
                if current_status not in {"Queued", "Processing", "Failed"}:
                    continue

                from stripe_integration.stripe_integration.webhook import _dispatch_verified_event

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
        "pauses": reconcile_due_subscription_pauses(),
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
