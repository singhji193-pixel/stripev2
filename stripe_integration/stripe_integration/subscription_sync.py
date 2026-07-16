import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone
from datetime import time as dtime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import frappe
import stripe
from frappe.utils import get_system_timezone, get_url, getdate, nowdate

from stripe_integration.stripe_integration.accounting import MariaDBNamedLock
from stripe_integration.stripe_integration.event_log import mark_event_status, upsert_event
from stripe_integration.stripe_integration.subscription_pause import (
    CADENCE_SNAPSHOT_FIELD,
    COORDINATED_PAUSE_FIELDS,
    OPERATION_ATTEMPT_FIELD,
    PAUSE_ACTIVE_FIELD,
    PAUSE_CYCLES_FIELD,
    PAUSE_LAST_RECONCILED_AT_FIELD,
    PAUSE_OPERATION_FIELD,
    PAUSE_START_AT_FIELD,
    PAUSE_START_FIELD,
    PAUSE_STATE_FIELD,
    PENDING_RESUME_AT_FIELD,
    PENDING_RESUME_FIELD,
    RESUME_AT_FIELD,
    RESUME_CANCEL_BEFORE_START_FIELD,
    RESUME_ON_FIELD,
    STATE_CANCELLING,
    STATE_PAUSED,
    STATE_PAUSING,
    STATE_RESUMING,
    advance_billing_timestamp,
    build_pause_window,
    build_resume_target,
    build_stripe_cadence_snapshot,
    load_cadence_snapshot,
)
from stripe_integration.stripe_integration.utils import get_api_key, get_company_abbr_from_company

LIFECYCLE_TEMPLATE_MAP = {
    "COE": {
        "add_payment_method": "Stripe COEngine Add Payment Method",
        "started": "Stripe COEngine Subscription Started",
        "resumed": "Stripe COEngine Subscription Resumed",
        "paused": "Stripe COEngine Subscription Paused",
        "cancelled": "Stripe COEngine Subscription Cancelled",
    },
    "COSL": {
        "add_payment_method": "Stripe CoreOrbit Add Payment Method",
        "started": "Stripe CoreOrbit Subscription Started",
        "resumed": "Stripe CoreOrbit Subscription Started",
        "paused": "Stripe CoreOrbit Subscription Paused",
        "cancelled": "Stripe CoreOrbit Subscription Cancelled",
    },
}

ALLOWED_COMPANY_ABBR = {"COE", "COSL"}
VALID_ACTIONS = {"pause", "resume", "cancel", "plan_change"}
TERMINAL_STRIPE_STATUSES = {"canceled", "cancelled", "incomplete_expired"}

SETUP_URL_FIELD = "stripe_setup_checkout_url"
SETUP_SESSION_FIELD = "stripe_setup_session_id"
SETUP_CREATED_AT_FIELD = "stripe_setup_link_created_at"
SETUP_EXPIRES_AT_FIELD = "stripe_setup_link_expires_at"
SETUP_STATUS_FIELD = "stripe_setup_link_status"
SETUP_PM_FIELD = "stripe_default_payment_method_id"
SETUP_INTENT_FIELD = "stripe_last_setup_intent_id"
SETUP_TOKEN_NONCE_FIELD = "stripe_setup_token_nonce"

STABLE_SETUP_ROUTE = "/api/method/stripe_integration.stripe_integration.subscription_sync.open_subscription_setup_link"
NO_INVOICE_FIELD = "custom_do_not_generate_invoices"


def _is_non_billing_subscription(subscription_doc) -> bool:
    return bool(int(subscription_doc.get(NO_INVOICE_FIELD) or 0))

def _stripe_get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        value = obj[key]
        return default if value is None else value
    except Exception:
        pass
    try:
        if hasattr(obj, key):
            value = getattr(obj, key)
            if not callable(value):
                return default if value is None else value
    except Exception:
        pass
    try:
        as_dict = obj.to_dict_recursive() if hasattr(obj, "to_dict_recursive") else obj.to_dict()
        if isinstance(as_dict, dict):
            value = as_dict.get(key, default)
            return default if value is None else value
    except Exception:
        pass
    return default


def _stable_setup_secret():
    return (getattr(frappe.local.conf, "encryption_key", None) or frappe.local.site or "stripe-subscription-setup").encode()


def _make_subscription_setup_token(subscription_name: str, nonce: str | None = None) -> str:
    nonce = nonce or frappe.db.get_value("Subscription", subscription_name, SETUP_TOKEN_NONCE_FIELD)
    if not nonce:
        return ""
    payload = f"{subscription_name}:{nonce}".encode()
    sig = hmac.new(_stable_setup_secret(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def _subscription_setup_token_valid(subscription_name: str, token: str | None) -> bool:
    if not subscription_name or not token:
        return False
    setup = frappe.db.get_value(
        "Subscription",
        subscription_name,
        [SETUP_TOKEN_NONCE_FIELD, SETUP_STATUS_FIELD, "status"],
        as_dict=True,
    ) or {}
    if setup.get(SETUP_STATUS_FIELD) != "pending":
        return False
    if (setup.get("status") or "").strip().lower() in {"cancelled", "canceled"}:
        return False
    expected = _make_subscription_setup_token(
        subscription_name,
        setup.get(SETUP_TOKEN_NONCE_FIELD),
    )
    if not expected:
        return False
    return hmac.compare_digest(expected, str(token).strip())


def _build_stable_subscription_setup_url(sub_doc) -> str:
    token = _make_subscription_setup_token(
        sub_doc.name,
        sub_doc.get(SETUP_TOKEN_NONCE_FIELD),
    )
    query = urlencode({"subscription_name": sub_doc.name, "token": token})
    return f"{get_url()}{STABLE_SETUP_ROUTE}?{query}"


def _rotate_subscription_setup_token(subscription_name: str):
    nonce = secrets.token_urlsafe(24)
    _set_subscription_fields(subscription_name, {SETUP_TOKEN_NONCE_FIELD: nonce})
    return nonce


def _get_company_letterhead(company: str | None) -> str | None:
    if not company:
        return None
    return frappe.db.get_value("Company", company, "default_letter_head")


def _is_enabled() -> bool:
    try:
        return int(frappe.db.get_single_value("Stripe Settings", "enable_subscription_state_sync") or 0) == 1
    except Exception:
        return False


def _normalize_action(action: str | None) -> str | None:
    if not action:
        return None
    action = str(action).strip().lower()
    return action if action in VALID_ACTIONS else None


def _validate_company_for_stripe(company: str):
    company_abbr = get_company_abbr_from_company(company)
    if company_abbr not in ALLOWED_COMPANY_ABBR:
        frappe.throw(f"Company {company_abbr} not allowed for Stripe sync")
    get_api_key(company_abbr)
    return company_abbr


def _require_subscription_permission(subscription_name: str, permission_type: str = "read"):
    subscription = frappe.get_doc("Subscription", subscription_name)
    subscription.check_permission(permission_type)
    return subscription


def _require_subscription_action_role():
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection({"System Manager", "Accounts Manager"}):
        frappe.throw("Not permitted", frappe.PermissionError)


def _assert_remote_subscription_ownership(
    subscription_doc,
    remote,
    company_abbr: str,
    *,
    expected_subscription_id: str | None = None,
):
    expected_id = str(
        expected_subscription_id
        or subscription_doc.get("stripe_subscription_id")
        or ""
    ).strip()
    remote_id = str(_stripe_get(remote, "id") or "").strip()
    if not expected_id or remote_id != expected_id:
        frappe.throw(
            f"Stripe returned subscription {remote_id or '[missing]'} instead of {expected_id or '[missing]'}"
        )

    metadata = _stripe_get(remote, "metadata") or {}
    if str(_stripe_get(metadata, "doctype") or "").strip() != "Subscription":
        frappe.throw("Stripe subscription ownership metadata is missing doctype=Subscription")
    if str(_stripe_get(metadata, "docname") or "").strip() != str(subscription_doc.name):
        frappe.throw("Stripe subscription belongs to a different ERPNext Subscription")

    local_company = str(subscription_doc.get("company") or "").strip()
    metadata_company = str(_stripe_get(metadata, "company") or "").strip()
    if not metadata_company or metadata_company != local_company:
        frappe.throw("Stripe subscription belongs to a different ERPNext company")

    metadata_company_abbr = str(_stripe_get(metadata, "company_abbr") or "").strip().upper()
    if not metadata_company_abbr or metadata_company_abbr != str(company_abbr or "").strip().upper():
        frappe.throw("Stripe subscription belongs to a different Stripe company account")

    local_customer = str(subscription_doc.get("stripe_customer_id") or "").strip()
    if local_customer:
        remote_customer = _stripe_get(remote, "customer")
        remote_customer_id = str(_stripe_get(remote_customer, "id") or remote_customer or "").strip()
        if remote_customer_id != local_customer:
            frappe.throw("Stripe subscription belongs to a different Stripe customer")

    return remote


def _retrieve_owned_subscription(
    subscription_doc,
    company_abbr: str,
    api_key: str,
    *,
    expected_subscription_id: str | None = None,
):
    subscription_id = expected_subscription_id or subscription_doc.get("stripe_subscription_id")
    remote = stripe.Subscription.retrieve(subscription_id, api_key=api_key)
    return _assert_remote_subscription_ownership(
        subscription_doc,
        remote,
        company_abbr,
        expected_subscription_id=subscription_id,
    )


def _event_stub(subscription_doc, action: str, operation_id: str | None = None):
    event_key = operation_id or f"{action}_{secrets.token_hex(12)}"
    return {
        "id": f"local_outbound_{subscription_doc.name}_{event_key}",
        "type": f"subscription.{action}",
        "data": {"object": {"id": getattr(subscription_doc, "stripe_subscription_id", None)}},
    }


def _new_operation_id(action: str) -> str:
    return f"{action}_{secrets.token_hex(12)}"


def _require_coordinated_pause_fields() -> None:
    meta = frappe.get_meta("Subscription")
    missing = [fieldname for fieldname in COORDINATED_PAUSE_FIELDS if not meta.get_field(fieldname)]
    if missing:
        frappe.throw(
            "Subscription pause migration is incomplete; missing fields: " + ", ".join(missing)
        )


def _remote_pause_collection(remote):
    return _stripe_get(remote, "pause_collection") or None


def _utc_now_timestamp() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _remote_pause_is_active(remote) -> bool:
    pause_collection = _remote_pause_collection(remote)
    if not pause_collection:
        return False
    resumes_at = int(_stripe_get(pause_collection, "resumes_at") or 0)
    return not resumes_at or resumes_at > _utc_now_timestamp()


def _pause_collection_matches(pause_collection, expected_resume: int) -> bool:
    return bool(
        pause_collection
        and str(_stripe_get(pause_collection, "behavior") or "").strip().lower() == "void"
        and int(_stripe_get(pause_collection, "resumes_at") or 0) == int(expected_resume)
    )


def _remote_subscription_status(remote) -> str:
    return str(_stripe_get(remote, "status") or "").strip().lower()


def _coordinated_pause_requires_cadence(subscription_doc) -> bool:
    state = str(subscription_doc.get(PAUSE_STATE_FIELD) or "").strip()
    return bool(
        int(subscription_doc.get(PAUSE_ACTIVE_FIELD) or 0)
        and state != STATE_CANCELLING
    )


def _remote_period_end_timestamp(remote) -> int | None:
    timestamp = _stripe_get(remote, "current_period_end")
    if timestamp:
        return int(timestamp)
    items = _stripe_get(_stripe_get(remote, "items") or {}, "data") or []
    if items:
        timestamp = _stripe_get(items[0], "current_period_end")
    return int(timestamp) if timestamp else None


def _require_supported_remote_cadence(remote, subscription_doc=None) -> None:
    billing_mode = _stripe_get(remote, "billing_mode")
    billing_mode_type = (
        _stripe_get(billing_mode, "type")
        if billing_mode and not isinstance(billing_mode, str)
        else billing_mode
    )
    if str(billing_mode_type or "").strip().lower() == "flexible":
        frappe.throw("Stripe flexible billing is not supported for coordinated pauses")

    for fieldname, label in (
        ("schedule", "a subscription schedule"),
        ("pending_update", "a pending update"),
        ("billing_thresholds", "subscription billing thresholds"),
    ):
        if _stripe_get(remote, fieldname) is not None:
            frappe.throw(
                f"Stripe {label} creates unsupported dynamic cadence for coordinated pauses"
            )

    items = _stripe_get(_stripe_get(remote, "items") or {}, "data") or []
    cadences = set()
    for item in items:
        if _stripe_get(item, "billing_thresholds") is not None:
            frappe.throw(
                "Stripe item billing thresholds create unsupported dynamic cadence "
                "for coordinated pauses"
            )
        price = _stripe_get(item, "price") or {}
        recurring = _stripe_get(price, "recurring") or _stripe_get(item, "plan") or {}
        if str(_stripe_get(recurring, "usage_type") or "").strip().lower() == "metered":
            frappe.throw(
                "Stripe metered usage creates unsupported dynamic cadence for coordinated pauses"
            )
        interval = str(_stripe_get(recurring, "interval") or "").strip().lower()
        if interval:
            cadences.add((interval, int(_stripe_get(recurring, "interval_count") or 1)))

    if not items:
        plan = _stripe_get(remote, "plan") or {}
        if str(_stripe_get(plan, "usage_type") or "").strip().lower() == "metered":
            frappe.throw(
                "Stripe metered usage creates unsupported dynamic cadence for coordinated pauses"
            )
        interval = str(_stripe_get(plan, "interval") or "").strip().lower()
        if interval:
            cadences.add((interval, int(_stripe_get(plan, "interval_count") or 1)))

    item_periods = {
        (
            _stripe_get(item, "current_period_start"),
            _stripe_get(item, "current_period_end"),
        )
        for item in items
    }
    if len(item_periods) > 1:
        frappe.throw("Stripe subscription items must expose one shared billing period")

    cadence_snapshot = load_cadence_snapshot(subscription_doc) if subscription_doc else None
    if cadence_snapshot:
        remote_anchor = int(_stripe_get(remote, "billing_cycle_anchor") or 0)
        expected_cadence = {
            (
                cadence_snapshot["interval"],
                int(cadence_snapshot["interval_count"]),
            )
        }
        if (
            remote_anchor != int(cadence_snapshot["billing_cycle_anchor"])
            or cadences != expected_cadence
        ):
            frappe.throw(
                "Stripe billing cadence changed after the coordinated pause was admitted"
            )


def _validate_cadence_for_status_sync(subscription_doc, remote) -> None:
    if (
        _remote_subscription_status(remote) not in TERMINAL_STRIPE_STATUSES
        and _coordinated_pause_requires_cadence(subscription_doc)
    ):
        _require_supported_remote_cadence(remote, subscription_doc)


def _utc_date_from_timestamp(timestamp: int):
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).date()


def _require_remote_pause_boundary(remote, pause_start, expected_timestamp: int | None = None) -> int:
    timestamp = _remote_period_end_timestamp(remote)
    if not timestamp:
        frappe.throw("Stripe subscription does not expose a verifiable next billing boundary")
    if expected_timestamp and int(timestamp) != int(expected_timestamp):
        frappe.throw(
            f"Stripe next billing timestamp {timestamp} does not match the persisted anchor {expected_timestamp}"
        )
    remote_boundary = _utc_date_from_timestamp(timestamp)
    if remote_boundary != getdate(pause_start):
        frappe.throw(
            f"Stripe next billing boundary {remote_boundary} does not match ERPNext {getdate(pause_start)}"
        )
    return int(timestamp)


def retrieve_subscription_pause_state(subscription_doc) -> dict:
    company_abbr = _validate_company_for_stripe(subscription_doc.company)
    api_key = get_api_key(company_abbr)
    remote = _retrieve_owned_subscription(
        subscription_doc,
        company_abbr,
        api_key,
    )
    remote_status = _remote_subscription_status(remote)
    paused = _remote_pause_is_active(remote)
    if remote_status not in TERMINAL_STRIPE_STATUSES:
        _require_supported_remote_cadence(remote, subscription_doc)
        if not paused:
            _assert_due_resume_is_unbilled(subscription_doc, remote, api_key)
    return {
        "remote": remote,
        "paused": paused,
        "company_abbr": company_abbr,
    }


def _update_doc_values(subscription_doc, values: dict) -> None:
    if hasattr(subscription_doc, "update"):
        subscription_doc.update(values)
        return
    for fieldname, value in values.items():
        setattr(subscription_doc, fieldname, value)


def _persist_operation_intent(subscription_doc, event: dict, company_abbr: str, values: dict) -> None:
    upsert_event(
        event,
        payload=json.dumps(event).encode(),
        company_abbr=company_abbr,
        status="Processing",
    )
    _set_subscription_fields(subscription_doc.name, values, required=COORDINATED_PAUSE_FIELDS)
    _update_doc_values(subscription_doc, values)


def _next_operation_attempt(subscription_doc) -> int:
    attempt = int(subscription_doc.get(OPERATION_ATTEMPT_FIELD) or 0) + 1
    values = {OPERATION_ATTEMPT_FIELD: attempt}
    _set_subscription_fields(
        subscription_doc.name,
        values,
        required=COORDINATED_PAUSE_FIELDS,
    )
    _update_doc_values(subscription_doc, values)
    return attempt


def _operation_idempotency_key(subscription_doc, operation_id: str, attempt: int) -> str:
    return f"erpnext-{subscription_doc.name}-{operation_id}-{int(attempt)}"


def _finish_operation(event_id: str, status: str, error: str | None = None) -> None:
    mark_event_status(event_id, status, error)
    frappe.db.commit()


def _sync_subscription_plan(subscription_doc, stripe_sub_id: str, company_abbr: str):
    remote = _retrieve_owned_subscription(
        subscription_doc,
        company_abbr,
        get_api_key(company_abbr),
        expected_subscription_id=stripe_sub_id,
    )
    existing_items = _stripe_get(_stripe_get(remote, "items") or {}, "data") or []
    desired_items = _build_stripe_subscription_items(subscription_doc)
    updates = []

    for index, desired in enumerate(desired_items):
        update = dict(desired)
        if index < len(existing_items):
            update["id"] = _stripe_get(existing_items[index], "id")
        updates.append(update)

    for extra in existing_items[len(desired_items):]:
        updates.append({"id": _stripe_get(extra, "id"), "deleted": True})

    pricing = _build_subscription_pricing_params(subscription_doc, company_abbr)
    params = {
        "items": updates,
        "discounts": pricing.get("discounts", []),
        "default_tax_rates": pricing.get("default_tax_rates", []),
        "proration_behavior": "none",
        "payment_behavior": "error_if_incomplete",
    }
    signature = hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:20]
    params["idempotency_key"] = f"erpnext-plan-change-{subscription_doc.name}-{signature}"
    updated = stripe.Subscription.modify(
        stripe_sub_id,
        api_key=get_api_key(company_abbr),
        **params,
    )
    updated = _assert_remote_subscription_ownership(
        subscription_doc,
        updated,
        company_abbr,
        expected_subscription_id=stripe_sub_id,
    )

    updated_items = _stripe_get(_stripe_get(updated, "items") or {}, "data") or []
    _set_subscription_fields(
        subscription_doc.name,
        {
            "stripe_subscription_item_id": (
                _stripe_get(updated_items[0], "id") if updated_items else ""
            ),
            "stripe_status": _stripe_get(updated, "status") or "",
        },
    )
    return updated


def _base_action_result(subscription_doc, action: str, company_abbr: str) -> dict:
    return {
        "handled": True,
        "subscription": subscription_doc.name,
        "stripe_subscription_id": subscription_doc.stripe_subscription_id,
        "action": action,
        "company_abbr": company_abbr,
    }


def _clear_unestablished_pause(subscription_doc, event: dict, company_abbr: str) -> dict:
    values = {
        PAUSE_ACTIVE_FIELD: 0,
        PAUSE_STATE_FIELD: "",
        PAUSE_OPERATION_FIELD: "",
        PENDING_RESUME_FIELD: None,
        PENDING_RESUME_AT_FIELD: "",
        RESUME_CANCEL_BEFORE_START_FIELD: 0,
        OPERATION_ATTEMPT_FIELD: 0,
        PAUSE_CYCLES_FIELD: 0,
        CADENCE_SNAPSHOT_FIELD: "",
        PAUSE_LAST_RECONCILED_AT_FIELD: None,
        "stripe_paused": 0,
    }
    _set_subscription_fields(
        subscription_doc.name,
        values,
        required=COORDINATED_PAUSE_FIELDS,
    )
    _update_doc_values(subscription_doc, values)
    _finish_operation(
        event["id"],
        "Ignored",
        "stripe_pause_not_established_before_boundary",
    )
    return {
        **_base_action_result(subscription_doc, "pause", company_abbr),
        "handled": False,
        "reason": "stripe_pause_not_established_before_boundary",
    }


def _stripe_invoice_periods(invoice) -> list[tuple[int, int]]:
    periods = []
    candidates = [invoice]
    candidates.extend(
        _stripe_get(_stripe_get(invoice, "lines") or {}, "data") or []
    )
    for candidate in candidates:
        period = _stripe_get(candidate, "period") or {}
        start = _stripe_get(period, "start") or _stripe_get(candidate, "period_start")
        end = _stripe_get(period, "end") or _stripe_get(candidate, "period_end")
        if start and end:
            periods.append((int(start), int(end)))
    return periods


def _assert_pause_window_not_billed(
    subscription_doc,
    api_key: str,
    pause_start_at: int,
    resume_at: int,
) -> None:
    invoices = _stripe_list_all(
        stripe.Invoice,
        api_key,
        subscription=subscription_doc.stripe_subscription_id,
    )
    for invoice in invoices:
        periods = _stripe_invoice_periods(invoice)
        relevant = any(
            start < int(resume_at) and end > int(pause_start_at)
            for start, end in periods
        )
        if not periods:
            created = int(_stripe_get(invoice, "created") or 0)
            relevant = int(pause_start_at) <= created < int(resume_at)
        if not relevant:
            continue
        status = str(_stripe_get(invoice, "status") or "").strip().lower()
        if status not in {"void", "deleted"}:
            frappe.throw(
                "The coordinated Stripe pause period was already billed or charged; "
                "manual reconciliation is required"
            )


def _assert_due_resume_is_unbilled(subscription_doc, remote, api_key: str) -> None:
    if not _coordinated_pause_requires_cadence(subscription_doc):
        return

    resume_field = (
        PENDING_RESUME_AT_FIELD
        if subscription_doc.get(PAUSE_STATE_FIELD) == STATE_RESUMING
        else RESUME_AT_FIELD
    )
    pause_start_at = int(subscription_doc.get(PAUSE_START_AT_FIELD) or 0)
    resume_at = int(subscription_doc.get(resume_field) or 0)
    if not pause_start_at or not resume_at or resume_at <= pause_start_at:
        frappe.throw("Persisted Stripe pause window is invalid; manual reconciliation is required")
    if _utc_now_timestamp() < resume_at:
        return

    pause_collection = _remote_pause_collection(remote)
    if pause_collection:
        behavior = str(_stripe_get(pause_collection, "behavior") or "").strip().lower()
        remote_resume_at = int(_stripe_get(pause_collection, "resumes_at") or 0)
        if behavior != "void" or remote_resume_at != resume_at:
            frappe.throw(
                "Stripe does not retain the persisted void pause contract; "
                "manual reconciliation is required"
            )

    _assert_pause_window_not_billed(
        subscription_doc,
        api_key,
        pause_start_at,
        resume_at,
    )


def _sync_pause_action(subscription_doc, pause_cycles: int, company_abbr: str, api_key: str):
    _require_coordinated_pause_fields()
    local_active = bool(int(subscription_doc.get(PAUSE_ACTIVE_FIELD) or 0))
    state = subscription_doc.get(PAUSE_STATE_FIELD) or ""
    remote = None

    if local_active:
        if state != STATE_PAUSING or not subscription_doc.get(PAUSE_OPERATION_FIELD):
            return {
                "handled": False,
                "reason": "already_paused",
                "subscription": subscription_doc.name,
                "action": "pause",
            }
        operation_id = subscription_doc.get(PAUSE_OPERATION_FIELD)
        pause_window = {
            "billing_cycles": int(subscription_doc.get(PAUSE_CYCLES_FIELD) or 0),
            "pause_start": str(subscription_doc.get(PAUSE_START_FIELD)),
            "resume_on": str(subscription_doc.get(RESUME_ON_FIELD)),
        }
        anchor_timestamp = int(subscription_doc.get(PAUSE_START_AT_FIELD) or 0)
        expected_resume = int(subscription_doc.get(RESUME_AT_FIELD) or 0)
        if not anchor_timestamp or not expected_resume:
            frappe.throw("Persisted Stripe pause anchors are missing; manual reconciliation is required")
        event = _event_stub(subscription_doc, "pause", operation_id=operation_id)
        remote = _retrieve_owned_subscription(subscription_doc, company_abbr, api_key)
        upsert_event(
            event,
            payload=json.dumps(event).encode(),
            company_abbr=company_abbr,
            status="Processing",
        )
        frappe.db.commit()
        remote_status = _remote_subscription_status(remote)
        if remote_status in TERMINAL_STRIPE_STATUSES:
            canonical_remote = (
                remote.to_dict_recursive()
                if hasattr(remote, "to_dict_recursive")
                else dict(remote)
            )
            _apply_subscription_state(subscription_doc.name, canonical_remote)
            _finish_operation(event["id"], "Ignored", "stripe_subscription_terminal")
            return {
                "handled": False,
                "reason": "stripe_subscription_terminal",
                "subscription": subscription_doc.name,
                "action": "pause",
                "stripe_status": remote_status,
            }
        _require_supported_remote_cadence(remote, subscription_doc)
    else:
        # Validate the local ERPNext period before making a Stripe request. The
        # canonical Stripe cadence is then snapshotted and checked across its
        # recurrence so an incompatible short-month anchor fails closed.
        pause_window = build_pause_window(subscription_doc, pause_cycles)
        remote = _retrieve_owned_subscription(subscription_doc, company_abbr, api_key)
        remote_status = _remote_subscription_status(remote)
        if remote_status in TERMINAL_STRIPE_STATUSES:
            canonical_remote = (
                remote.to_dict_recursive()
                if hasattr(remote, "to_dict_recursive")
                else dict(remote)
            )
            _apply_subscription_state(subscription_doc.name, canonical_remote)
            return {
                "handled": False,
                "reason": "stripe_subscription_terminal",
                "subscription": subscription_doc.name,
                "action": "pause",
                "stripe_status": remote_status,
            }
        _require_supported_remote_cadence(remote)
        if _remote_pause_is_active(remote):
            frappe.throw(
                "Stripe is already paused without an ERPNext coordinated hold; "
                "manual reconciliation is required"
            )
        anchor_timestamp = _require_remote_pause_boundary(remote, pause_window["pause_start"])
        cadence_snapshot = build_stripe_cadence_snapshot(
            subscription_doc,
            remote,
            pause_start_at=anchor_timestamp,
        )
        pause_window = build_pause_window(
            subscription_doc,
            pause_cycles,
            cadence_snapshot=cadence_snapshot,
            pause_start_at=anchor_timestamp,
        )
        anchor_timestamp = pause_window["pause_start_at"]
        expected_resume = pause_window["resume_at"]
        if _utc_date_from_timestamp(expected_resume) != getdate(pause_window["resume_on"]):
            frappe.throw(
                "Stripe and ERPNext calculate different resume boundaries; billing was not paused"
            )
        operation_id = _new_operation_id("pause")
        event = _event_stub(subscription_doc, "pause", operation_id=operation_id)
        _persist_operation_intent(
            subscription_doc,
            event,
            company_abbr,
            {
                PAUSE_ACTIVE_FIELD: 1,
                PAUSE_STATE_FIELD: STATE_PAUSING,
                PAUSE_OPERATION_FIELD: operation_id,
                PAUSE_START_FIELD: pause_window["pause_start"],
                RESUME_ON_FIELD: pause_window["resume_on"],
                PENDING_RESUME_FIELD: None,
                CADENCE_SNAPSHOT_FIELD: pause_window["cadence_snapshot"],
                PAUSE_START_AT_FIELD: str(anchor_timestamp),
                RESUME_AT_FIELD: str(expected_resume),
                PENDING_RESUME_AT_FIELD: "",
                RESUME_CANCEL_BEFORE_START_FIELD: 0,
                OPERATION_ATTEMPT_FIELD: 0,
                PAUSE_CYCLES_FIELD: pause_window["billing_cycles"],
                PAUSE_LAST_RECONCILED_AT_FIELD: None,
            },
        )

    try:
        pause_collection = _remote_pause_collection(remote) or {}
        if pause_collection and not local_active and not _remote_pause_is_active(remote):
            pause_collection = {}
        if not _pause_collection_matches(pause_collection, expected_resume):
            if pause_collection:
                frappe.throw(
                    "Stripe has a different pause behavior or boundary; manual reconciliation is required"
                )
            pause_intent_active = bool(
                int(subscription_doc.get(PAUSE_ACTIVE_FIELD) or 0)
                and subscription_doc.get(PAUSE_STATE_FIELD) == STATE_PAUSING
            )
            if pause_intent_active and _utc_now_timestamp() >= anchor_timestamp:
                return _clear_unestablished_pause(
                    subscription_doc,
                    event,
                    company_abbr,
                )
            if local_active:
                _require_remote_pause_boundary(
                    remote,
                    pause_window["pause_start"],
                    expected_timestamp=anchor_timestamp,
                )
                if subscription_doc.is_current_invoice_generated(
                    pause_window["pause_start"],
                    subscription_doc.get("current_invoice_end"),
                ):
                    frappe.throw(
                        "The stored pause period is already invoiced; manual reconciliation is required"
                    )
            attempt = _next_operation_attempt(subscription_doc)
            stripe.Subscription.modify(
                subscription_doc.stripe_subscription_id,
                pause_collection={"behavior": "void", "resumes_at": expected_resume},
                api_key=api_key,
                idempotency_key=_operation_idempotency_key(
                    subscription_doc,
                    operation_id,
                    attempt,
                ),
            )
            remote = _retrieve_owned_subscription(subscription_doc, company_abbr, api_key)
            _require_supported_remote_cadence(remote, subscription_doc)
            pause_collection = _remote_pause_collection(remote) or {}
        if not _pause_collection_matches(pause_collection, expected_resume):
            if not pause_collection and _utc_now_timestamp() >= anchor_timestamp:
                return _clear_unestablished_pause(
                    subscription_doc,
                    event,
                    company_abbr,
                )
            frappe.throw("Stripe did not retain the requested void pause and resume boundary")

        _assert_pause_window_not_billed(
            subscription_doc,
            api_key,
            anchor_timestamp,
            expected_resume,
        )

        completed_values = {
            PAUSE_STATE_FIELD: STATE_PAUSED,
            OPERATION_ATTEMPT_FIELD: 0,
            "stripe_paused": 1,
        }
        _set_subscription_fields(
            subscription_doc.name,
            completed_values,
            required=COORDINATED_PAUSE_FIELDS,
            update_modified=True,
        )
        _update_doc_values(subscription_doc, completed_values)
        _finish_operation(event["id"], "Completed")
    except Exception as exc:
        frappe.db.rollback()
        _finish_operation(event["id"], "Failed", str(exc))
        raise

    result = _base_action_result(subscription_doc, "pause", company_abbr)
    result.update(pause_window)
    return result


def _complete_due_resume(subscription_doc, target):
    if not hasattr(subscription_doc, "complete_billing_pause") or not hasattr(
        subscription_doc, "_process_subscription"
    ):
        frappe.throw("Subscription override is unavailable; ERPNext billing remains paused")

    subscription_doc.complete_billing_pause()
    lock_flag_setter = getattr(subscription_doc, "_set_lock_flag", None)
    if lock_flag_setter:
        lock_flag_setter(True)
    try:
        # Use the native processing path so a generated resume-period invoice is
        # followed by the same period advance, end-date, and status handling as
        # ERPNext's scheduler.
        subscription_doc._process_subscription(target)
    finally:
        if lock_flag_setter:
            lock_flag_setter(False)


def _sync_resume_action(subscription_doc, company_abbr: str, api_key: str):
    _require_coordinated_pause_fields()
    local_active = bool(int(subscription_doc.get(PAUSE_ACTIVE_FIELD) or 0))
    state = subscription_doc.get(PAUSE_STATE_FIELD) or ""
    if state == STATE_CANCELLING:
        return _sync_cancel_action(subscription_doc, company_abbr, api_key)
    if state == STATE_PAUSING:
        frappe.throw("Complete or retry the pending Stripe pause before resuming billing")
    remote = _retrieve_owned_subscription(subscription_doc, company_abbr, api_key)
    remote_status = _remote_subscription_status(remote)
    if remote_status in TERMINAL_STRIPE_STATUSES:
        canonical_remote = (
            remote.to_dict_recursive()
            if hasattr(remote, "to_dict_recursive")
            else dict(remote)
        )
        _apply_subscription_state(subscription_doc.name, canonical_remote)
        return {
            "handled": False,
            "reason": "stripe_subscription_terminal",
            "subscription": subscription_doc.name,
            "action": "resume",
            "stripe_status": remote_status,
        }
    _require_supported_remote_cadence(remote, subscription_doc)
    remote_paused = _remote_pause_is_active(remote)
    remote_pause_collection = _remote_pause_collection(remote)
    if local_active and remote_pause_collection and str(
        _stripe_get(remote_pause_collection, "behavior") or ""
    ).strip().lower() != "void":
        frappe.throw("Stripe pause behavior is not void; manual reconciliation is required")

    if state == STATE_RESUMING and subscription_doc.get(PAUSE_OPERATION_FIELD):
        operation_id = subscription_doc.get(PAUSE_OPERATION_FIELD)
        pending_resume = subscription_doc.get(PENDING_RESUME_FIELD)
        target = str(pending_resume) if pending_resume else None
        target_timestamp = int(subscription_doc.get(PENDING_RESUME_AT_FIELD) or 0) or None
        cycles = int(subscription_doc.get(PAUSE_CYCLES_FIELD) or 0)
        cancel_before_start = bool(
            int(subscription_doc.get(RESUME_CANCEL_BEFORE_START_FIELD) or 0)
        )
        event = _event_stub(subscription_doc, "resume", operation_id=operation_id)
        upsert_event(
            event,
            payload=json.dumps(event).encode(),
            company_abbr=company_abbr,
            status="Processing",
        )
        frappe.db.commit()
    else:
        if not local_active and not remote_paused:
            if int(subscription_doc.get("stripe_paused") or 0):
                _set_subscription_fields(subscription_doc.name, {"stripe_paused": 0})
                return {
                    **_base_action_result(subscription_doc, "resume", company_abbr),
                    "recovered_stale_state": True,
                }
            return {
                "handled": False,
                "reason": "not_paused",
                "subscription": subscription_doc.name,
                "action": "resume",
            }

        resume_target = (
            build_resume_target(
                subscription_doc,
                current_timestamp=_utc_now_timestamp(),
            )
            if local_active
            else None
        )
        target = resume_target["resume_on"] if resume_target else None
        cycles = int(resume_target["billing_cycles"] if resume_target else 0)
        cancel_before_start = bool(resume_target and resume_target["cancel_before_start"])
        target_timestamp = None
        if local_active:
            anchor_timestamp = int(subscription_doc.get(PAUSE_START_AT_FIELD) or 0)
            if not anchor_timestamp:
                frappe.throw("Persisted Stripe pause anchor is missing; manual reconciliation is required")
            target_timestamp = int(resume_target.get("resume_at") or 0) or None
            if not target_timestamp:
                target_timestamp = advance_billing_timestamp(
                    subscription_doc,
                    anchor_timestamp,
                    cycles,
                )
            if _utc_date_from_timestamp(target_timestamp) != getdate(target):
                frappe.throw(
                    "Stripe and ERPNext calculate different resume boundaries; billing remains paused"
                )
        operation_id = _new_operation_id("resume")
        event = _event_stub(subscription_doc, "resume", operation_id=operation_id)
        intent_values = {
            PAUSE_STATE_FIELD: STATE_RESUMING,
            PAUSE_OPERATION_FIELD: operation_id,
            PENDING_RESUME_FIELD: target,
            PENDING_RESUME_AT_FIELD: str(target_timestamp) if target_timestamp else "",
            RESUME_CANCEL_BEFORE_START_FIELD: 1 if cancel_before_start else 0,
            OPERATION_ATTEMPT_FIELD: 0,
        }
        if local_active:
            intent_values[PAUSE_CYCLES_FIELD] = cycles
        _persist_operation_intent(
            subscription_doc,
            event,
            company_abbr,
            intent_values,
        )

    try:
        if local_active and not target_timestamp:
            frappe.throw("Pending Stripe resume anchor is missing; billing remains paused")
        resume_boundary_passed = bool(
            local_active
            and target_timestamp
            and int(target_timestamp) <= _utc_now_timestamp()
        )
        clear_immediately = not local_active or cancel_before_start or resume_boundary_passed
        if clear_immediately:
            if resume_boundary_passed and remote_paused:
                frappe.throw(
                    "The requested resume boundary passed while Stripe remained paused; "
                    "manual reconciliation is required"
                )
            if remote_paused:
                attempt = _next_operation_attempt(subscription_doc)
                stripe.Subscription.modify(
                    subscription_doc.stripe_subscription_id,
                    pause_collection="",
                    api_key=api_key,
                    idempotency_key=_operation_idempotency_key(
                        subscription_doc,
                        operation_id,
                        attempt,
                    ),
                )
                remote = _retrieve_owned_subscription(subscription_doc, company_abbr, api_key)
                _require_supported_remote_cadence(remote, subscription_doc)
                remote_paused = _remote_pause_is_active(remote)
            if remote_paused:
                frappe.throw("Stripe subscription is still paused after the resume request")
        elif remote_paused:
            expected_resume = int(target_timestamp)
            pause_collection = _remote_pause_collection(remote) or {}
            if str(_stripe_get(pause_collection, "behavior") or "").strip().lower() != "void":
                frappe.throw("Stripe pause behavior is not void; manual reconciliation is required")
            if int(_stripe_get(pause_collection, "resumes_at") or 0) != expected_resume:
                attempt = _next_operation_attempt(subscription_doc)
                stripe.Subscription.modify(
                    subscription_doc.stripe_subscription_id,
                    pause_collection={"behavior": "void", "resumes_at": expected_resume},
                    api_key=api_key,
                    idempotency_key=_operation_idempotency_key(
                        subscription_doc,
                        operation_id,
                        attempt,
                    ),
                )
                remote = _retrieve_owned_subscription(subscription_doc, company_abbr, api_key)
                _require_supported_remote_cadence(remote, subscription_doc)
                pause_collection = _remote_pause_collection(remote) or {}
            if not _pause_collection_matches(pause_collection, expected_resume):
                frappe.throw("Stripe did not retain the requested void pause and resume boundary")

        if local_active and resume_boundary_passed:
            _assert_due_resume_is_unbilled(subscription_doc, remote, api_key)

        if not local_active or (cancel_before_start and not resume_boundary_passed):
            completed_values = {
                PAUSE_ACTIVE_FIELD: 0,
                PAUSE_STATE_FIELD: "",
                PAUSE_OPERATION_FIELD: "",
                PENDING_RESUME_FIELD: None,
                PENDING_RESUME_AT_FIELD: "",
                RESUME_CANCEL_BEFORE_START_FIELD: 0,
                OPERATION_ATTEMPT_FIELD: 0,
                PAUSE_CYCLES_FIELD: 0,
                CADENCE_SNAPSHOT_FIELD: "",
                PAUSE_LAST_RECONCILED_AT_FIELD: None,
                "stripe_paused": 0,
            }
        elif clear_immediately:
            _complete_due_resume(subscription_doc, target)
            completed_values = {
                PAUSE_ACTIVE_FIELD: 0,
                RESUME_ON_FIELD: target,
                RESUME_AT_FIELD: str(target_timestamp) if target_timestamp else "",
                PAUSE_STATE_FIELD: "",
                PAUSE_OPERATION_FIELD: "",
                PENDING_RESUME_FIELD: None,
                PENDING_RESUME_AT_FIELD: "",
                RESUME_CANCEL_BEFORE_START_FIELD: 0,
                OPERATION_ATTEMPT_FIELD: 0,
                PAUSE_CYCLES_FIELD: 0,
                CADENCE_SNAPSHOT_FIELD: "",
                PAUSE_LAST_RECONCILED_AT_FIELD: None,
                "stripe_paused": 0,
            }
        else:
            completed_values = {
                RESUME_ON_FIELD: target,
                RESUME_AT_FIELD: str(target_timestamp) if target_timestamp else "",
                PAUSE_STATE_FIELD: STATE_PAUSED,
                PAUSE_OPERATION_FIELD: "",
                PENDING_RESUME_FIELD: None,
                PENDING_RESUME_AT_FIELD: "",
                RESUME_CANCEL_BEFORE_START_FIELD: 0,
                OPERATION_ATTEMPT_FIELD: 0,
                PAUSE_CYCLES_FIELD: cycles,
                "stripe_paused": 1 if remote_paused else 0,
            }
        _set_subscription_fields(
            subscription_doc.name,
            completed_values,
            required=COORDINATED_PAUSE_FIELDS,
        )
        _update_doc_values(subscription_doc, completed_values)
        _finish_operation(event["id"], "Completed")
    except Exception as exc:
        frappe.db.rollback()
        _finish_operation(event["id"], "Failed", str(exc))
        raise

    result = _base_action_result(subscription_doc, "resume", company_abbr)
    if target:
        result["resume_on"] = target
        result["scheduled"] = bool(
            local_active
            and not cancel_before_start
            and target_timestamp
            and int(target_timestamp) > _utc_now_timestamp()
        )
    return result


def _stripe_resource_is_missing(exc: Exception) -> bool:
    return (
        str(_stripe_get(exc, "code") or "").strip().lower() == "resource_missing"
        or "no such subscription" in str(exc).lower()
    )


def _sync_cancel_action(subscription_doc, company_abbr: str, api_key: str):
    _require_coordinated_pause_fields()
    state = subscription_doc.get(PAUSE_STATE_FIELD) or ""
    operation_id = subscription_doc.get(PAUSE_OPERATION_FIELD)

    try:
        initial_remote = _retrieve_owned_subscription(
            subscription_doc,
            company_abbr,
            api_key,
        )
    except Exception as exc:
        if _stripe_resource_is_missing(exc):
            frappe.throw(
                "Stripe subscription was not found with the configured company account; "
                "cancellation was not confirmed"
            )
        raise

    if state == STATE_CANCELLING and operation_id:
        event = _event_stub(subscription_doc, "cancel", operation_id=operation_id)
        upsert_event(
            event,
            payload=json.dumps(event).encode(),
            company_abbr=company_abbr,
            status="Processing",
        )
        frappe.db.commit()
    else:
        operation_id = _new_operation_id("cancel")
        event = _event_stub(subscription_doc, "cancel", operation_id=operation_id)
        _persist_operation_intent(
            subscription_doc,
            event,
            company_abbr,
            {
                PAUSE_ACTIVE_FIELD: 1,
                PAUSE_STATE_FIELD: STATE_CANCELLING,
                PAUSE_OPERATION_FIELD: operation_id,
                OPERATION_ATTEMPT_FIELD: 0,
                PAUSE_LAST_RECONCILED_AT_FIELD: None,
            },
        )

    try:
        remote = initial_remote
        remote_missing = False
        remote_status = _remote_subscription_status(remote)
        if not remote_missing and remote_status not in TERMINAL_STRIPE_STATUSES:
            attempt = _next_operation_attempt(subscription_doc)
            deleted_remote = stripe.Subscription.delete(
                subscription_doc.stripe_subscription_id,
                api_key=api_key,
                idempotency_key=_operation_idempotency_key(
                    subscription_doc,
                    operation_id,
                    attempt,
                ),
            )
            deleted_status = _remote_subscription_status(deleted_remote)
            deleted_id = str(_stripe_get(deleted_remote, "id") or "")
            try:
                remote = _retrieve_owned_subscription(subscription_doc, company_abbr, api_key)
                remote_status = _remote_subscription_status(remote)
            except Exception as exc:
                if not _stripe_resource_is_missing(exc):
                    raise
                if (
                    deleted_id != str(subscription_doc.stripe_subscription_id)
                    or deleted_status not in TERMINAL_STRIPE_STATUSES
                ):
                    frappe.throw("Stripe did not confirm cancellation before the subscription disappeared")
                remote_missing = True
        if not remote_missing and remote_status not in TERMINAL_STRIPE_STATUSES:
            frappe.throw("Stripe did not canonically confirm subscription cancellation")

        completed_values = {
            PAUSE_ACTIVE_FIELD: 0,
            PAUSE_STATE_FIELD: "",
            PAUSE_OPERATION_FIELD: "",
            PENDING_RESUME_FIELD: None,
            PENDING_RESUME_AT_FIELD: "",
            RESUME_CANCEL_BEFORE_START_FIELD: 0,
            OPERATION_ATTEMPT_FIELD: 0,
            PAUSE_CYCLES_FIELD: 0,
            CADENCE_SNAPSHOT_FIELD: "",
            PAUSE_LAST_RECONCILED_AT_FIELD: None,
            "stripe_paused": 0,
            "status": "Cancelled",
            "cancelation_date": nowdate(),
        }
        _set_subscription_fields(
            subscription_doc.name,
            completed_values,
            required=COORDINATED_PAUSE_FIELDS,
            update_modified=True,
        )
        _update_doc_values(subscription_doc, completed_values)
        _finish_operation(event["id"], "Completed")
    except Exception as exc:
        frappe.db.rollback()
        _finish_operation(event["id"], "Failed", str(exc))
        raise

    return _base_action_result(subscription_doc, "cancel", company_abbr)


def _sync_subscription(subscription_doc, action: str, pause_cycles: int = 1):
    action = _normalize_action(action)
    if not action:
        return {"handled": False, "reason": "unsupported_action", "action": action}

    stripe_sub_id = getattr(subscription_doc, "stripe_subscription_id", None)
    if not stripe_sub_id:
        return {
            "handled": False,
            "reason": "missing_stripe_subscription_id",
            "subscription": subscription_doc.name,
        }

    company_abbr = _validate_company_for_stripe(subscription_doc.company)
    api_key = get_api_key(company_abbr)
    if action == "pause":
        return _sync_pause_action(subscription_doc, pause_cycles, company_abbr, api_key)
    if action == "resume":
        return _sync_resume_action(subscription_doc, company_abbr, api_key)
    if action == "cancel":
        return _sync_cancel_action(subscription_doc, company_abbr, api_key)

    if action == "plan_change" and int(subscription_doc.get(PAUSE_ACTIVE_FIELD) or 0):
        return {
            "handled": False,
            "reason": "pause_active",
            "subscription": subscription_doc.name,
            "action": action,
        }

    # The only action reaching this legacy branch is plan_change. Establish
    # canonical ownership before its event row is committed and before any
    # Stripe mutation can be attempted.
    _retrieve_owned_subscription(
        subscription_doc,
        company_abbr,
        api_key,
        expected_subscription_id=stripe_sub_id,
    )

    event = _event_stub(subscription_doc, action)
    upsert_event(
        event,
        payload=json.dumps(event).encode(),
        company_abbr=company_abbr,
        status="Processing",
    )
    frappe.db.commit()
    try:
        if action == "plan_change":
            _sync_subscription_plan(subscription_doc, stripe_sub_id, company_abbr)

        _finish_operation(event["id"], "Completed")
        return _base_action_result(subscription_doc, action, company_abbr)
    except Exception as exc:
        _finish_operation(event["id"], "Failed", str(exc))
        raise


def _erp_status_options():
    try:
        opts = frappe.db.get_value("DocField", {"parent": "Subscription", "fieldname": "status"}, "options") or ""
        return {x.strip() for x in opts.split("\n") if x.strip()}
    except Exception:
        return set()


def _map_stripe_to_erp_status(stripe_status: str | None, paused: bool = False):
    s = (stripe_status or "").strip().lower()
    if s in {"canceled", "incomplete_expired"}:
        return "Cancelled"
    if s in {"past_due"}:
        return "Past Due Date"
    if s in {"unpaid", "incomplete"}:
        return "Unpaid"
    if s in {"active", "trialing"}:
        return "Active"
    return None


def _resolve_subscription_email(sub_doc):
    # 1. Direct fields on the Subscription doc.
    for fn in ("contact_email", "email", "subscriber_email", "customer_email"):
        v = sub_doc.get(fn)
        if v:
            return v

    # 2-4. Walk the linked Customer via the shared resolver (Customer.email_id,
    # customer_primary_contact, then linked Contact via Dynamic Link).
    party_type = (sub_doc.get("party_type") or "").strip()
    party = sub_doc.get("party")
    if party_type != "Customer" or not party:
        return None

    from stripe_integration.stripe_integration.utils import resolve_customer_email
    return resolve_customer_email(party)


def _pick_lifecycle_kind(prev_status: str | None, prev_paused: bool, new_status: str | None, new_paused: bool):
    ps = (prev_status or "").strip().lower()
    ns = (new_status or "").strip().lower()

    if ns in {"canceled", "incomplete_expired"} and ns != ps:
        return "cancelled"
    if new_paused and not prev_paused:
        return "paused"
    if ns in {"unpaid", "incomplete", "past_due"} and ns != ps:
        return "add_payment_method"
    if ns in {"active", "trialing"} and not new_paused and prev_paused:
        return "resumed"
    if ns in {"active", "trialing"} and not new_paused and ps not in {"active", "trialing"}:
        return "started"
    return None


def _resolve_sender(company_abbr: str):
    if company_abbr == "COSL":
        return {
            "sender": "CoreOrbit Billing <billing@coreorbit.io>",
            "email_account": "CoreOrbit Billing",
        }
    return {
        "sender": "COEngine <erp@coengine.ai>",
        "email_account": "COEngine",
    }


def _set_subscription_fields(
    sub_name: str,
    values: dict,
    required=None,
    *,
    update_modified: bool = False,
):
    meta = frappe.get_meta("Subscription")
    missing = [fieldname for fieldname in (required or ()) if not meta.get_field(fieldname)]
    if missing:
        frappe.throw(
            "Subscription pause migration is incomplete; missing fields: " + ", ".join(missing)
        )
    update = {k: v for k, v in (values or {}).items() if meta.get_field(k)}
    if update:
        frappe.db.set_value(
            "Subscription",
            sub_name,
            update,
            update_modified=update_modified,
        )
        frappe.db.commit()


def _build_stripe_subscription_items(sub_doc) -> list[dict]:
    items = []
    for row in sub_doc.get("plans") or []:
        plan = row.get("plan") if hasattr(row, "get") else getattr(row, "plan", None)
        qty = row.get("qty") if hasattr(row, "get") else getattr(row, "qty", None)
        if not plan:
            continue
        price_id = frappe.db.get_value("Subscription Plan", plan, "product_price_id")
        if not price_id:
            frappe.throw(f"Subscription Plan {plan} is missing Stripe product_price_id")
        item = {"price": price_id}
        if qty:
            item["quantity"] = int(qty)
        items.append(item)

    if not items:
        frappe.throw(f"Subscription {sub_doc.name} has no billable plans configured")
    return items


def _subscription_currency(sub_doc) -> str:
    return (
        frappe.get_cached_value("Company", sub_doc.get("company"), "default_currency")
        or "CAD"
    ).lower()


def _stripe_list_all(resource, api_key: str, **params):
    params = {**params, "limit": 100}
    rows = []
    while True:
        page = resource.list(api_key=api_key, **params)
        data = _stripe_get(page, "data") or []
        rows.extend(data)
        if not _stripe_get(page, "has_more"):
            return rows
        params["starting_after"] = _stripe_get(data[-1], "id")


def _ensure_subscription_discount(sub_doc, company_abbr: str, currency: str) -> str | None:
    percentage = float(sub_doc.get("additional_discount_percentage") or 0)
    amount = float(sub_doc.get("additional_discount_amount") or 0)
    if percentage <= 0 and amount <= 0:
        return None

    if percentage > 0:
        kind = "percent"
        value = round(percentage, 6)
        create_args = {"percent_off": value}
    else:
        kind = "amount"
        value = round(amount * 100)
        if value <= 0:
            return None
        create_args = {"amount_off": value, "currency": currency}

    signature = hashlib.sha256(
        f"{company_abbr}:{currency}:{kind}:{value}".encode()
    ).hexdigest()[:24]
    api_key = get_api_key(company_abbr)
    for coupon in _stripe_list_all(stripe.Coupon, api_key):
        metadata = _stripe_get(coupon, "metadata") or {}
        if metadata.get("erpnext_signature") == signature:
            return _stripe_get(coupon, "id")

    coupon = stripe.Coupon.create(
        duration="forever",
        name=f"ERPNext {kind} discount {value}",
        metadata={
            "erpnext_signature": signature,
            "company_abbr": company_abbr,
            "source": "erpnext_subscription",
        },
        idempotency_key=f"erpnext-subscription-coupon-{signature}",
        api_key=api_key,
        **create_args,
    )
    return _stripe_get(coupon, "id")


def _ensure_subscription_tax_rates(sub_doc, company_abbr: str) -> list[str]:
    template_name = sub_doc.get("sales_tax_template")
    if not template_name:
        return []

    template = frappe.get_doc("Sales Taxes and Charges Template", template_name)
    api_key = get_api_key(company_abbr)
    existing_rates = _stripe_list_all(stripe.TaxRate, api_key, active=True)
    tax_rate_ids = []
    company_country = frappe.get_cached_value("Company", sub_doc.get("company"), "country")
    country_code = (
        frappe.db.get_value("Country", company_country, "code")
        if company_country
        else None
    )
    country_code = (country_code or "").strip().upper()

    for row in template.get("taxes") or []:
        rate = float(row.get("rate") or 0)
        if rate == 0:
            continue
        if (row.get("add_deduct_tax") or "Add") != "Add" or row.get("charge_type") != "On Net Total":
            frappe.throw(
                f"Stripe subscriptions only support additive 'On Net Total' taxes; "
                f"{template_name} row {row.get('idx')} is unsupported"
            )

        inclusive = bool(row.get("included_in_print_rate"))
        label = (row.get("description") or row.get("account_head") or template_name)[:50]
        signature = hashlib.sha256(
            f"{company_abbr}:{label}:{rate:.6f}:{int(inclusive)}".encode()
        ).hexdigest()[:24]

        matched = None
        for tax_rate in existing_rates:
            metadata = _stripe_get(tax_rate, "metadata") or {}
            if metadata.get("erpnext_signature") == signature:
                matched = _stripe_get(tax_rate, "id")
                break

        if not matched:
            create_args = {
                "display_name": label,
                "description": f"ERPNext {template_name}",
                "percentage": rate,
                "inclusive": inclusive,
                "metadata": {
                    "erpnext_signature": signature,
                    "company_abbr": company_abbr,
                    "source": "erpnext_subscription",
                },
                "idempotency_key": f"erpnext-subscription-tax-{signature}",
                "api_key": api_key,
            }
            if len(country_code) == 2:
                create_args["country"] = country_code

            tax_rate = stripe.TaxRate.create(
                **create_args,
            )
            existing_rates.append(tax_rate)
            matched = _stripe_get(tax_rate, "id")

        tax_rate_ids.append(matched)

    return tax_rate_ids


def _build_subscription_pricing_params(sub_doc, company_abbr: str) -> dict:
    currency = _subscription_currency(sub_doc)
    params = {}
    coupon_id = _ensure_subscription_discount(sub_doc, company_abbr, currency)
    if coupon_id:
        params["discounts"] = [{"coupon": coupon_id}]

    tax_rate_ids = _ensure_subscription_tax_rates(sub_doc, company_abbr)
    if tax_rate_ids:
        params["default_tax_rates"] = tax_rate_ids
    return params


def _build_stripe_subscription_create_params(sub_doc, stripe_customer_id: str, payment_method: str, company_abbr: str):
    items = _build_stripe_subscription_items(sub_doc)

    params = {
        "customer": stripe_customer_id,
        "items": items,
        "default_payment_method": payment_method,
        "collection_method": "charge_automatically",
        "metadata": {
            "doctype": "Subscription",
            "docname": sub_doc.name,
            "company": sub_doc.get("company") or "",
            "company_abbr": company_abbr,
            "site": frappe.local.site,
            "source": "subscription_setup_completion",
        },
        "payment_settings": {"save_default_payment_method": "on_subscription"},
    }
    params.update(_build_subscription_pricing_params(sub_doc, company_abbr))

    start_date = getdate(sub_doc.get("start_date")) if sub_doc.get("start_date") else None
    current_period_start = (
        getdate(sub_doc.get("current_invoice_start"))
        if sub_doc.get("current_invoice_start")
        else None
    )
    today = getdate(nowdate())
    first_stripe_billing_date = next(
        (
            candidate
            for candidate in (current_period_start, start_date)
            if candidate and candidate > today
        ),
        None,
    )
    if first_stripe_billing_date:
        local_midnight = datetime.combine(
            first_stripe_billing_date,
            dtime.min,
            tzinfo=ZoneInfo(get_system_timezone()),
        )
        params["trial_end"] = int(local_midnight.astimezone(timezone.utc).timestamp())

    return params


def ensure_stripe_subscription_for_subscription(subscription_name: str, payment_method: str | None = None, stripe_customer_id: str | None = None):
    with MariaDBNamedLock(f"stripe-subscription-create-{subscription_name}", timeout=30):
        sub_doc = frappe.get_doc("Subscription", subscription_name)
        if _is_non_billing_subscription(sub_doc):
            frappe.throw(
                f"Subscription {subscription_name} is non-billing and cannot be linked to Stripe"
            )
        if sub_doc.get("stripe_subscription_id"):
            return {
                "created": False,
                "reason": "already_linked",
                "stripe_subscription_id": sub_doc.get("stripe_subscription_id"),
            }

        company_abbr = _validate_company_for_stripe(sub_doc.company)
        api_key = get_api_key(company_abbr)
        payment_method = payment_method or sub_doc.get(SETUP_PM_FIELD)
        supplied_payment_method = bool(payment_method)
        customer_email = _resolve_subscription_email(sub_doc)
        stripe_customer_id = stripe_customer_id or sub_doc.get("stripe_customer_id") or None
        payment_method_doc = None
        attached_customer = None
        if payment_method:
            payment_method_doc = stripe.PaymentMethod.retrieve(
                payment_method,
                api_key=api_key,
            )
            attached_customer = _stripe_get(payment_method_doc, "customer")
            if stripe_customer_id and attached_customer and attached_customer != stripe_customer_id:
                frappe.throw("Stripe payment method belongs to a different customer")
            if not stripe_customer_id and attached_customer:
                stripe_customer_id = attached_customer

        if not stripe_customer_id and customer_email:
            candidates = _stripe_get(
                stripe.Customer.list(email=customer_email, limit=100, api_key=api_key),
                "data",
            ) or []
            for customer in candidates:
                metadata = _stripe_get(customer, "metadata") or {}
                if (
                    metadata.get("docname") == sub_doc.name
                    or (
                        metadata.get("company_abbr") == company_abbr
                        and metadata.get("erpnext_party_type") == (sub_doc.get("party_type") or "")
                        and metadata.get("erpnext_party") == (sub_doc.get("party") or "")
                    )
                ):
                    stripe_customer_id = _stripe_get(customer, "id")
                    break

        if stripe_customer_id and attached_customer and attached_customer != stripe_customer_id:
            frappe.throw("Stripe payment method belongs to a different customer")

        customer_doc = None
        if stripe_customer_id:
            customer_doc = stripe.Customer.retrieve(stripe_customer_id, api_key=api_key)
            customer_metadata = dict(_stripe_get(customer_doc, "metadata") or {})
            metadata_company = (customer_metadata.get("company_abbr") or "").strip().upper()
            metadata_party = customer_metadata.get("erpnext_party")
            if metadata_company and metadata_company != company_abbr:
                frappe.throw("Stripe customer belongs to a different company")
            if metadata_party and metadata_party != (sub_doc.get("party") or ""):
                frappe.throw("Stripe customer belongs to a different ERPNext party")

            if not payment_method:
                invoice_settings = _stripe_get(customer_doc, "invoice_settings") or {}
                default_payment_method = _stripe_get(
                    invoice_settings,
                    "default_payment_method",
                )
                payment_method = _stripe_get(default_payment_method, "id") or default_payment_method
                if payment_method:
                    payment_method_doc = stripe.PaymentMethod.retrieve(
                        payment_method,
                        api_key=api_key,
                    )
                    attached_customer = _stripe_get(payment_method_doc, "customer")
                    if attached_customer and attached_customer != stripe_customer_id:
                        frappe.throw("Stripe default payment method belongs to a different customer")

        if not payment_method:
            return {
                "created": False,
                "reason": "missing_payment_method",
                "stripe_customer_id": stripe_customer_id,
            }

        if not stripe_customer_id:
            party_signature = hashlib.sha256(
                f"{company_abbr}:{sub_doc.get('party_type') or ''}:{sub_doc.get('party') or sub_doc.name}".encode()
            ).hexdigest()[:24]
            customer_kwargs = {
                "name": sub_doc.get("party") or sub_doc.name,
                "metadata": {
                    "doctype": "Subscription",
                    "docname": sub_doc.name,
                    "company_abbr": company_abbr,
                    "erpnext_party_type": sub_doc.get("party_type") or "",
                    "erpnext_party": sub_doc.get("party") or "",
                },
                "idempotency_key": f"erpnext-customer-{company_abbr}-{party_signature}",
            }
            if customer_email:
                customer_kwargs["email"] = customer_email
            customer_kwargs["api_key"] = api_key
            customer_doc = stripe.Customer.create(**customer_kwargs)
            stripe_customer_id = _stripe_get(customer_doc, "id")

        if not attached_customer:
            stripe.PaymentMethod.attach(
                payment_method,
                customer=stripe_customer_id,
                api_key=api_key,
            )

        if not customer_doc:
            customer_doc = stripe.Customer.retrieve(stripe_customer_id, api_key=api_key)
        customer_metadata = dict(_stripe_get(customer_doc, "metadata") or {})
        metadata_company = (customer_metadata.get("company_abbr") or "").strip().upper()
        metadata_party = customer_metadata.get("erpnext_party")
        if metadata_company and metadata_company != company_abbr:
            frappe.throw("Stripe customer belongs to a different company")
        if metadata_party and metadata_party != (sub_doc.get("party") or ""):
            frappe.throw("Stripe customer belongs to a different ERPNext party")
        customer_metadata.update(
            {
                "company_abbr": company_abbr,
                "erpnext_party_type": sub_doc.get("party_type") or "",
                "erpnext_party": sub_doc.get("party") or "",
            }
        )
        stripe.Customer.modify(
            stripe_customer_id,
            invoice_settings={"default_payment_method": payment_method},
            metadata=customer_metadata,
            api_key=api_key,
        )

        params = _build_stripe_subscription_create_params(
            sub_doc,
            stripe_customer_id,
            payment_method,
            company_abbr,
        )
        params["idempotency_key"] = f"erpnext-subscription-{company_abbr}-{sub_doc.name}"
        params["api_key"] = api_key
        remote_sub = stripe.Subscription.create(**params)

        remote_items = _stripe_get(_stripe_get(remote_sub, "items") or {}, "data") or []
        _set_subscription_fields(
            sub_doc.name,
            {
                "stripe_customer_id": stripe_customer_id,
                "stripe_subscription_id": _stripe_get(remote_sub, "id") or "",
                "stripe_subscription_item_id": (
                    _stripe_get(remote_items[0], "id") if remote_items else ""
                ),
                "stripe_status": _stripe_get(remote_sub, "status") or "",
                "stripe_paused": 1 if bool(_stripe_get(remote_sub, "pause_collection")) else 0,
            },
        )

        return {
            "created": True,
            "subscription": sub_doc.name,
            "stripe_customer_id": stripe_customer_id,
            "stripe_subscription_id": _stripe_get(remote_sub, "id"),
            "stripe_status": _stripe_get(remote_sub, "status"),
            "trial_end": params.get("trial_end"),
            "used_saved_payment_method": not supplied_payment_method,
        }


def _generate_subscription_setup_checkout_url(sub_doc, company_abbr: str, to_email: str | None = None):
    # Create a fresh setup-mode Checkout Session so customer adds a payment method
    # without immediate charge. This avoids stale/expired one-time payment links.
    stripe_sub_id = sub_doc.get("stripe_subscription_id")
    _validate_company_for_stripe(sub_doc.get("company"))
    api_key = get_api_key(company_abbr)

    stripe_customer_id = None
    if stripe_sub_id:
        try:
            remote_sub = stripe.Subscription.retrieve(stripe_sub_id, api_key=api_key)
            stripe_customer_id = getattr(remote_sub, "customer", None)
        except Exception:
            stripe_customer_id = None

    success_url = get_url() + "/api/method/stripe_integration.stripe_integration.api.payment_success?subscription=" + sub_doc.name
    cancel_url = get_url() + "/api/method/stripe_integration.stripe_integration.api.payment_cancelled?subscription=" + sub_doc.name

    currency = (frappe.get_cached_value("Company", sub_doc.get("company"), "default_currency") or "CAD").lower()

    params = {
        "mode": "setup",
        "currency": currency,
        "payment_method_types": ["card"],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": {
            "doctype": "Subscription",
            "docname": sub_doc.name,
            "company": sub_doc.get("company") or "",
            "company_abbr": company_abbr,
            "source": "subscription_add_payment_method",
            "stripe_subscription_id": stripe_sub_id,
            "site": frappe.local.site,
        },
        "api_key": api_key,
    }

    if stripe_customer_id:
        params["customer"] = stripe_customer_id
    elif to_email:
        params["customer_email"] = to_email

    try:
        session = stripe.checkout.Session.create(**params)
    except Exception as e:
        # Retry without customer_email if Stripe rejects malformed/invalid address.
        if params.get("customer_email"):
            params.pop("customer_email", None)
            session = stripe.checkout.Session.create(**params)
        else:
            raise e

    checkout_url = session.get("url")

    if checkout_url:
        _set_subscription_fields(
            sub_doc.name,
            {
                SETUP_URL_FIELD: checkout_url,
                SETUP_SESSION_FIELD: session.get("id") or "",
                SETUP_CREATED_AT_FIELD: frappe.utils.now_datetime(),
                SETUP_EXPIRES_AT_FIELD: frappe.utils.get_datetime(session.get("expires_at")) if session.get("expires_at") else None,
                SETUP_STATUS_FIELD: "pending",
                # keep legacy field in sync for backward-compatible templates
                "stripe_checkout_url": checkout_url,
            },
        )

    return checkout_url or ""


def _build_subscription_invoice_attachment(sub_doc):
    """Build Sales Invoice PDF attachment ONLY from this subscription's invoices.

    Avoids customer-wide fallback so wrong invoice never gets attached.
    Returns: (pdf_attachment_or_none, invoice_name_or_none)
    """
    si_name = None
    try:
        # Primary: latest submitted invoice tied to this subscription
        # (attach even if paid; user expects invoice PDF on add-payment-method email)
        si_name = frappe.db.get_value(
            "Sales Invoice",
            {"subscription": sub_doc.name, "docstatus": 1},
            "name",
            order_by="posting_date desc, posting_time desc, modified desc",
        )

        # Fallback: latest invoice tied to this subscription (any docstatus)
        if not si_name:
            si_name = frappe.db.get_value(
                "Sales Invoice",
                {"subscription": sub_doc.name},
                "name",
                order_by="posting_date desc, posting_time desc, modified desc",
            )

        if not si_name:
            return None, None

        company_abbr = get_company_abbr_from_company(sub_doc.get("company"))
        pf = "CoreOrbit Beautiful Invoice" if company_abbr == "COSL" else "COEngine Beautiful Invoice"

        letterhead = _get_company_letterhead(sub_doc.get("company"))
        try:
            # Preferred branded format
            return frappe.attach_print(
                "Sales Invoice",
                si_name,
                file_name=f"{si_name}.pdf",
                print_format=pf,
                letterhead=letterhead,
            ), si_name
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"Attach print failed with forced format {pf} for {si_name}")
            try:
                # Safe fallback: Standard + company letterhead (avoids no-attachment outcome)
                return frappe.attach_print(
                    "Sales Invoice",
                    si_name,
                    file_name=f"{si_name}.pdf",
                    print_format="Standard",
                    letterhead=letterhead,
                ), si_name
            except Exception:
                frappe.log_error(frappe.get_traceback(), f"Attach print failed with Standard fallback for {si_name}")
                return None, si_name
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Build subscription invoice attachment failed for {sub_doc.name}")
        return None, None


def _send_lifecycle_email(subscription_name: str, company_abbr: str, kind: str, stripe_sub_obj: dict):
    template_name = (LIFECYCLE_TEMPLATE_MAP.get(company_abbr or "", {}) or {}).get(kind)
    if not template_name:
        return {"sent": False, "reason": "template_not_mapped"}

    if not frappe.db.exists("Email Template", template_name):
        return {"sent": False, "reason": "template_missing", "template": template_name}

    sub_doc = frappe.get_doc("Subscription", subscription_name)
    to_email = _resolve_subscription_email(sub_doc)
    if not to_email:
        return {"sent": False, "reason": "recipient_email_missing", "template": template_name}

    customer_name = sub_doc.get("party") or ""
    if (sub_doc.get("party_type") or "") == "Customer" and sub_doc.get("party"):
        customer_name = frappe.db.get_value("Customer", sub_doc.get("party"), "customer_name") or customer_name

    plan_name = ""
    try:
        plans = sub_doc.get("plans") or []
        if plans and getattr(plans[0], "plan", None):
            plan_name = plans[0].plan
    except Exception:
        plan_name = ""

    checkout_url = sub_doc.get(SETUP_URL_FIELD) or sub_doc.get("stripe_checkout_url") or ""
    if kind == "add_payment_method":
        if sub_doc.get(SETUP_STATUS_FIELD) != "pending" or not checkout_url:
            _rotate_subscription_setup_token(sub_doc.name)
            checkout_url = _generate_subscription_setup_checkout_url(
                sub_doc,
                company_abbr,
                to_email=to_email,
            )
            sub_doc = frappe.get_doc("Subscription", subscription_name)
        if not checkout_url:
            return {
                "sent": False,
                "reason": "setup_checkout_url_missing",
                "template": template_name,
            }
        checkout_url = _build_stable_subscription_setup_url(sub_doc)

    args = {
        "subscription_name": sub_doc.name,
        "party": sub_doc.get("party") or "",
        "customer_name": customer_name,
        "plan_name": plan_name,
        "company": sub_doc.get("company") or "",
        "stripe_subscription_id": sub_doc.get("stripe_subscription_id") or (stripe_sub_obj or {}).get("id") or "",
        "stripe_status": (stripe_sub_obj or {}).get("status") or "",
        "stripe_checkout_url": checkout_url,
        "paused": 1 if bool((stripe_sub_obj or {}).get("pause_collection")) else 0,
    }

    et = frappe.get_doc("Email Template", template_name)
    subject = frappe.render_template(et.subject or "Subscription Update", args)
    message = frappe.render_template(et.response or "", args)

    sender_cfg = _resolve_sender(company_abbr)
    attachments = []
    attached_invoice = None
    if kind == "add_payment_method":
        inv_pdf, attached_invoice = _build_subscription_invoice_attachment(sub_doc)
        if inv_pdf:
            attachments.append(inv_pdf)

    frappe.sendmail(
        recipients=[to_email],
        subject=subject,
        message=message,
        sender=sender_cfg["sender"],
        attachments=attachments or None,
        now=True,
        delayed=False,
        add_unsubscribe_link=0,
        reference_doctype="Subscription",
        reference_name=sub_doc.name,
    )
    return {
        "sent": True,
        "template": template_name,
        "to": to_email,
        "kind": kind,
        "has_attachment": bool(attachments),
        "attached_invoice": attached_invoice,
    }


def _apply_subscription_state(sub_name: str, stripe_sub_obj: dict, subscription_doc=None):
    stripe_status = (stripe_sub_obj or {}).get("status")
    paused = _remote_pause_is_active(stripe_sub_obj)
    cancel_at_period_end = int(bool((stripe_sub_obj or {}).get("cancel_at_period_end")))

    meta = frappe.get_meta("Subscription")
    tracked_fields = [
        "status",
        "cancelation_date",
        "stripe_status",
        "stripe_paused",
        "cancel_at_period_end",
        *COORDINATED_PAUSE_FIELDS,
    ]
    tracked_fields = list(dict.fromkeys(fieldname for fieldname in tracked_fields if meta.get_field(fieldname)))
    prev = (
        frappe.db.get_value(
            "Subscription",
            sub_name,
            tracked_fields,
            as_dict=True,
        )
        or {}
    )
    prev_status = prev.get("stripe_status")
    prev_paused = bool(prev.get("stripe_paused"))
    durable_cancelling = bool(
        prev.get(PAUSE_STATE_FIELD) == STATE_CANCELLING
        and prev.get(PAUSE_OPERATION_FIELD)
    )

    update = {}
    if meta.get_field("stripe_status"):
        update["stripe_status"] = stripe_status or ""
    if meta.get_field("stripe_paused"):
        update["stripe_paused"] = 1 if paused else 0
    if meta.get_field("cancel_at_period_end") and (
        not durable_cancelling
        or _remote_subscription_status(stripe_sub_obj) in TERMINAL_STRIPE_STATUSES
    ):
        update["cancel_at_period_end"] = cancel_at_period_end

    erp_status = _map_stripe_to_erp_status(stripe_status, paused=paused)
    allowed = _erp_status_options()
    applied_erp_status = erp_status if erp_status and (not allowed or erp_status in allowed) else None
    if durable_cancelling and erp_status != "Cancelled":
        applied_erp_status = None
    if applied_erp_status:
        update["status"] = erp_status
    if (
        subscription_doc
        and not paused
        and int(subscription_doc.get(PAUSE_ACTIVE_FIELD) or 0)
        and (subscription_doc.get(PAUSE_STATE_FIELD) or "") in {"", STATE_PAUSED}
    ):
        pause_start_at = int(subscription_doc.get(PAUSE_START_AT_FIELD) or 0)
        planned_resume_at = int(subscription_doc.get(RESUME_AT_FIELD) or 0)
        current_timestamp = _utc_now_timestamp()
        if pause_start_at and current_timestamp < pause_start_at:
            update.update(
                {
                    PAUSE_ACTIVE_FIELD: 0,
                    PAUSE_STATE_FIELD: "",
                    PAUSE_OPERATION_FIELD: "",
                    PENDING_RESUME_FIELD: None,
                    PENDING_RESUME_AT_FIELD: "",
                    RESUME_CANCEL_BEFORE_START_FIELD: 0,
                    OPERATION_ATTEMPT_FIELD: 0,
                    PAUSE_CYCLES_FIELD: 0,
                    CADENCE_SNAPSHOT_FIELD: "",
                    PAUSE_LAST_RECONCILED_AT_FIELD: None,
                }
            )
        elif pause_start_at and planned_resume_at and current_timestamp < planned_resume_at:
            resume_target = build_resume_target(
                subscription_doc,
                current_timestamp=current_timestamp,
            )
            if not resume_target["cancel_before_start"] and int(
                resume_target["billing_cycles"] or 0
            ):
                update.update(
                    {
                        PAUSE_ACTIVE_FIELD: 1,
                        PAUSE_STATE_FIELD: STATE_PAUSED,
                        PAUSE_OPERATION_FIELD: "",
                        PENDING_RESUME_FIELD: None,
                        PENDING_RESUME_AT_FIELD: "",
                        RESUME_CANCEL_BEFORE_START_FIELD: 0,
                        OPERATION_ATTEMPT_FIELD: 0,
                        RESUME_ON_FIELD: resume_target["resume_on"],
                        RESUME_AT_FIELD: str(resume_target["resume_at"]),
                        PAUSE_CYCLES_FIELD: int(resume_target["billing_cycles"]),
                    }
                )
    if erp_status == "Cancelled":
        for fieldname, value in {
            "cancelation_date": prev.get("cancelation_date") or nowdate(),
            PAUSE_ACTIVE_FIELD: 0,
            PAUSE_STATE_FIELD: "",
            PAUSE_OPERATION_FIELD: "",
            PENDING_RESUME_FIELD: None,
            PENDING_RESUME_AT_FIELD: "",
            RESUME_CANCEL_BEFORE_START_FIELD: 0,
            OPERATION_ATTEMPT_FIELD: 0,
            PAUSE_CYCLES_FIELD: 0,
            CADENCE_SNAPSHOT_FIELD: "",
            PAUSE_LAST_RECONCILED_AT_FIELD: None,
        }.items():
            if meta.get_field(fieldname):
                update[fieldname] = value

    numeric_fields = {
        "stripe_paused",
        "cancel_at_period_end",
        PAUSE_ACTIVE_FIELD,
        RESUME_CANCEL_BEFORE_START_FIELD,
        OPERATION_ATTEMPT_FIELD,
        PAUSE_CYCLES_FIELD,
    }

    def values_match(fieldname, current, desired):
        if fieldname in numeric_fields:
            return int(current or 0) == int(desired or 0)
        if current in (None, "") and desired in (None, ""):
            return True
        return current == desired or str(current) == str(desired)

    changes = {
        fieldname: value
        for fieldname, value in update.items()
        if not values_match(fieldname, prev.get(fieldname), value)
    }
    modified_fields = {
        "status",
        "cancelation_date",
        "stripe_status",
        "cancel_at_period_end",
    }
    if changes:
        frappe.db.set_value(
            "Subscription",
            sub_name,
            changes,
            update_modified=bool(modified_fields.intersection(changes)),
        )
        frappe.db.commit()

    return {
        "subscription": sub_name,
        "stripe_status": stripe_status,
        "paused": paused,
        "erp_status": applied_erp_status,
        "prev_stripe_status": prev_status,
        "prev_paused": prev_paused,
    }


def sync_subscription_from_webhook_event(event: dict):
    stripe_sub = (event or {}).get("data", {}).get("object", {}) or {}
    stripe_sub_id = stripe_sub.get("id")
    if not stripe_sub_id:
        return {"handled": False, "reason": "missing_stripe_subscription_id"}

    sub_name = frappe.db.get_value("Subscription", {"stripe_subscription_id": stripe_sub_id}, "name")
    link_from_metadata = False
    if not sub_name:
        metadata = stripe_sub.get("metadata") or {}
        candidate = metadata.get("docname") if metadata.get("doctype") == "Subscription" else None
        if candidate and frappe.db.exists("Subscription", candidate):
            sub_name = candidate
            link_from_metadata = True
    if not sub_name:
        return {"handled": False, "reason": "subscription_not_found", "stripe_subscription_id": stripe_sub_id}

    with MariaDBNamedLock(f"stripe-subscription-action-{sub_name}", timeout=30):
        subscription = frappe.get_doc("Subscription", sub_name)
        company_abbr = _validate_company_for_stripe(subscription.company)
        existing_subscription_id = str(subscription.get("stripe_subscription_id") or "").strip()
        if existing_subscription_id and existing_subscription_id != stripe_sub_id:
            frappe.throw("ERPNext Subscription is already linked to a different Stripe subscription")
        remote = _retrieve_owned_subscription(
            subscription,
            company_abbr,
            get_api_key(company_abbr),
            expected_subscription_id=stripe_sub_id,
        )
        if link_from_metadata:
            items = _stripe_get(_stripe_get(remote, "items") or {}, "data") or []
            linked_values = {
                "stripe_customer_id": _stripe_get(remote, "customer") or "",
                "stripe_subscription_id": stripe_sub_id,
                "stripe_subscription_item_id": _stripe_get(items[0], "id") if items else "",
            }
            _set_subscription_fields(sub_name, linked_values)
            _update_doc_values(subscription, linked_values)
        canonical_stripe_sub = (
            remote.to_dict_recursive()
            if hasattr(remote, "to_dict_recursive")
            else dict(remote)
        )
        _validate_cadence_for_status_sync(subscription, remote)
        out = _apply_subscription_state(
            sub_name,
            canonical_stripe_sub,
            subscription,
        )
        out["handled"] = True

    kind = _pick_lifecycle_kind(
        out.get("prev_stripe_status"),
        bool(out.get("prev_paused")),
        out.get("stripe_status"),
        bool(out.get("paused")),
    )

    if kind and company_abbr in ALLOWED_COMPANY_ABBR:
        try:
            email_out = _send_lifecycle_email(sub_name, company_abbr, kind, stripe_sub)
            out["email"] = email_out
            out["lifecycle_kind"] = kind
        except Exception as e:
            out["email"] = {"sent": False, "reason": str(e)[:300]}

    return out


@frappe.whitelist()
def reconcile_subscription_status(subscription_name: str):
    sub = _require_subscription_permission(subscription_name, "read")
    stripe_sub_id = getattr(sub, "stripe_subscription_id", None)
    if not stripe_sub_id:
        return {"handled": False, "reason": "missing_stripe_subscription_id", "subscription": subscription_name}

    with MariaDBNamedLock(f"stripe-subscription-action-{subscription_name}", timeout=30):
        sub = frappe.get_doc("Subscription", subscription_name)
        stripe_sub_id = getattr(sub, "stripe_subscription_id", None)
        if not stripe_sub_id:
            return {
                "handled": False,
                "reason": "missing_stripe_subscription_id",
                "subscription": subscription_name,
            }
        company_abbr = _validate_company_for_stripe(sub.company)
        remote = _retrieve_owned_subscription(sub, company_abbr, get_api_key(company_abbr))
        _validate_cadence_for_status_sync(sub, remote)
        return _apply_subscription_state(sub.name, dict(remote), sub)


@frappe.whitelist()
def sync_subscription_action(subscription_name: str, action: str, pause_cycles: int = 1):
    _require_subscription_action_role()
    _require_subscription_permission(subscription_name, "write")
    if not _is_enabled():
        return {"handled": False, "reason": "subscription_sync_disabled"}
    with MariaDBNamedLock(f"stripe-subscription-action-{subscription_name}", timeout=30):
        sub = frappe.get_doc("Subscription", subscription_name)
        return _sync_subscription(sub, action, pause_cycles=pause_cycles)


@frappe.whitelist()
def request_subscription_payment_method(subscription_name: str, send_email: int = 1):
    sub = _require_subscription_permission(subscription_name, "write")
    company_abbr = _validate_company_for_stripe(sub.company)

    if not sub.get("stripe_subscription_id"):
        saved_payment_result = ensure_stripe_subscription_for_subscription(
            subscription_name,
        )
        if saved_payment_result.get("created"):
            return {
                "ok": True,
                "subscription": subscription_name,
                "subscription_created": True,
                "reused_saved_payment_method": bool(
                    saved_payment_result.get("used_saved_payment_method")
                ),
                "stripe_subscription_id": saved_payment_result.get(
                    "stripe_subscription_id"
                ),
                "email_sent": False,
            }
        if saved_payment_result.get("reason") not in {
            "missing_payment_method",
            "already_linked",
        }:
            return {
                "ok": False,
                "subscription": subscription_name,
                "reason": saved_payment_result.get("reason") or "stripe_subscription_creation_failed",
            }

    to_email = _resolve_subscription_email(sub)
    _rotate_subscription_setup_token(subscription_name)
    checkout_url = _generate_subscription_setup_checkout_url(sub, company_abbr, to_email=to_email)

    if not checkout_url:
        return {
            "ok": False,
            "reason": "setup_checkout_url_missing",
            "subscription": subscription_name,
        }

    out = {
        "ok": True,
        "subscription": subscription_name,
        "checkout_url": checkout_url,
        "email_sent": False,
        "stripe_subscription_linked": bool(getattr(sub, "stripe_subscription_id", None)),
    }

    if int(send_email or 0):
        try:
            email_out = _send_lifecycle_email(subscription_name, company_abbr, "add_payment_method", {})
            out["email"] = email_out
            out["email_sent"] = bool((email_out or {}).get("sent"))
        except Exception as e:
            out["email"] = {"sent": False, "reason": str(e)[:300]}

    return out


def _sync_cancelled_subscription_after_commit(subscription_name: str):
    with MariaDBNamedLock(f"stripe-subscription-action-{subscription_name}", timeout=30):
        subscription = frappe.get_doc("Subscription", subscription_name)
        status_is_cancelled = (subscription.get("status") or "").strip().lower() in {
            "cancelled",
            "canceled",
        }
        durable_cancelling = bool(
            subscription.get(PAUSE_STATE_FIELD) == STATE_CANCELLING
            and subscription.get(PAUSE_OPERATION_FIELD)
        )
        if not status_is_cancelled and not durable_cancelling:
            return {
                "handled": False,
                "reason": "subscription_not_cancelled",
                "subscription": subscription_name,
            }
        return _sync_subscription(subscription, "cancel")


def queue_subscription_action(subscription_name: str, action: str, *, trusted_cancel: bool = False):
    action = _normalize_action(action)
    if not action:
        return None
    method = "stripe_integration.stripe_integration.subscription_sync.sync_subscription_action"
    kwargs = {"subscription_name": subscription_name, "action": action}
    if trusted_cancel and action == "cancel":
        method = (
            "stripe_integration.stripe_integration.subscription_sync."
            "_sync_cancelled_subscription_after_commit"
        )
        kwargs = {"subscription_name": subscription_name}
    return frappe.enqueue(
        method,
        queue="short",
        timeout=300,
        enqueue_after_commit=True,
        **kwargs,
    )


@frappe.whitelist()
def get_recent_subscription_sync_events(subscription_name: str, limit: int = 20):
    subscription = _require_subscription_permission(subscription_name, "read")
    stripe_sub_id = getattr(subscription, "stripe_subscription_id", None)
    if not stripe_sub_id:
        return []

    return frappe.get_all(
        "Stripe Event Log",
        filters={"stripe_object_id": stripe_sub_id},
        fields=["name", "event_id", "event_type", "status", "error", "processed_at", "modified"],
        order_by="modified desc",
        limit_page_length=max(1, min(int(limit or 20), 100)),
    )



@frappe.whitelist()
def retry_failed_subscription_events(limit: int = 20):
    frappe.only_for("System Manager")
    rows = frappe.get_all(
        "Stripe Event Log",
        filters={"status": "Failed"},
        fields=["name", "event_id", "event_type", "stripe_object_id", "error", "modified"],
        order_by="modified desc",
        limit_page_length=max(1, min(int(limit or 20), 100)),
    )

    out = []
    for r in rows:
        et = (r.get("event_type") or "").strip().lower()
        if not et.startswith("customer.subscription."):
            continue

        sub_name = frappe.db.get_value("Subscription", {"stripe_subscription_id": r.get("stripe_object_id")}, "name")
        if not sub_name:
            out.append({"event_id": r.get("event_id"), "retried": False, "reason": "subscription_not_found"})
            continue

        try:
            with MariaDBNamedLock(f"stripe-subscription-action-{sub_name}", timeout=30):
                subscription = frappe.get_doc("Subscription", sub_name)
                company_abbr = _validate_company_for_stripe(subscription.company)
                remote = _retrieve_owned_subscription(
                    subscription,
                    company_abbr,
                    get_api_key(company_abbr),
                    expected_subscription_id=r.get("stripe_object_id"),
                )
                _validate_cadence_for_status_sync(subscription, remote)
                result = _apply_subscription_state(sub_name, dict(remote), subscription)
                mark_event_status(r.get("event_id"), "Completed")
                frappe.db.commit()
            out.append({"event_id": r.get("event_id"), "subscription": sub_name, "result": result})
        except Exception as e:
            out.append({"event_id": r.get("event_id"), "subscription": sub_name, "error": str(e)[:300]})

    return out


@frappe.whitelist()
def get_subscription_sync_health(hours: int = 24):
    frappe.only_for("System Manager")
    hours = max(1, min(int(hours or 24), 168))
    failed = frappe.db.sql(
        """
        select count(*) from `tabStripe Event Log`
        where status='Failed' and modified >= (NOW() - INTERVAL %s HOUR)
        """,
        (hours,),
    )[0][0]
    completed = frappe.db.sql(
        """
        select count(*) from `tabStripe Event Log`
        where status='Completed' and modified >= (NOW() - INTERVAL %s HOUR)
        """,
        (hours,),
    )[0][0]
    ignored = frappe.db.sql(
        """
        select count(*) from `tabStripe Event Log`
        where status='Ignored' and modified >= (NOW() - INTERVAL %s HOUR)
        """,
        (hours,),
    )[0][0]

    return {
        "window_hours": hours,
        "completed": int(completed),
        "failed": int(failed),
        "ignored": int(ignored),
    }

def _enforce_subscription_billing_defaults(doc):
    if _is_non_billing_subscription(doc):
        return

    # Keep ERP subscription invoicing fully automatic.
    # We use db_set so this still works on submitted subscriptions where normal field updates are blocked.
    try:
        if int(getattr(doc, "submit_invoice", 0) or 0) != 1:
            doc.db_set("submit_invoice", 1, update_modified=False)
    except Exception:
        pass

    try:
        if (getattr(doc, "generate_invoice_at", None) or "") != "Beginning of the current subscription period":
            doc.db_set("generate_invoice_at", "Beginning of the current subscription period", update_modified=False)
    except Exception:
        pass


def _persist_native_cancellation_intent(subscription_doc) -> str:
    _require_coordinated_pause_fields()
    company_abbr = _validate_company_for_stripe(subscription_doc.company)
    operation_id = subscription_doc.get(PAUSE_OPERATION_FIELD)
    existing_intent = bool(
        subscription_doc.get(PAUSE_STATE_FIELD) == STATE_CANCELLING
        and operation_id
    )
    if not existing_intent:
        operation_id = _new_operation_id("cancel")

    event = _event_stub(subscription_doc, "cancel", operation_id=operation_id)
    upsert_event(
        event,
        payload=json.dumps(event).encode(),
        company_abbr=company_abbr,
        status="Queued",
    )
    values = {
        PAUSE_ACTIVE_FIELD: 1,
        PAUSE_STATE_FIELD: STATE_CANCELLING,
        PAUSE_OPERATION_FIELD: operation_id,
        PAUSE_LAST_RECONCILED_AT_FIELD: None,
    }
    if not existing_intent:
        values[OPERATION_ATTEMPT_FIELD] = 0
    frappe.db.set_value(
        "Subscription",
        subscription_doc.name,
        values,
        update_modified=False,
    )
    _update_doc_values(subscription_doc, values)
    return operation_id


def on_subscription_update(doc, method=None):
    _enforce_subscription_billing_defaults(doc)

    if _is_non_billing_subscription(doc):
        return

    if not _is_enabled():
        return

    if not getattr(doc, "stripe_subscription_id", None):
        return

    status = (doc.status or "").lower().strip()
    if status in ("cancelled", "canceled"):
        _persist_native_cancellation_intent(doc)
        queue_subscription_action(doc.name, "cancel", trusted_cancel=True)
        return

    legacy_action = str(getattr(doc, "stripe_sync_action", None) or "").strip()
    if legacy_action:
        _require_subscription_action_role()
        frappe.throw(
            "Legacy stripe_sync_action saves are not supported; use the Stripe action controls"
        )


@frappe.whitelist(allow_guest=True)
def open_subscription_setup_link(subscription_name: str, token: str | None = None):
    if not _subscription_setup_token_valid(subscription_name, token):
        frappe.throw("Invalid or missing subscription setup token", frappe.PermissionError)

    sub = frappe.get_doc("Subscription", subscription_name)
    company_abbr = _validate_company_for_stripe(sub.company)
    to_email = _resolve_subscription_email(sub)
    checkout_url = _generate_subscription_setup_checkout_url(sub, company_abbr, to_email=to_email)
    if not checkout_url:
        frappe.throw("Unable to generate a fresh Stripe setup link")

    frappe.local.response["type"] = "redirect"
    frappe.local.response["location"] = checkout_url
    return
