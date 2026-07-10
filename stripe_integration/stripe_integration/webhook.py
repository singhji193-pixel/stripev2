import hashlib
import json
import time

import frappe
import stripe
from frappe.exceptions import DuplicateEntryError

from stripe_integration.stripe_integration.accounting import (
    MariaDBNamedLock,
    prepare_stripe_receipt_payment_entry,
    stripe_timestamp_date,
    validate_stripe_currency,
)
from stripe_integration.stripe_integration.event_log import mark_event_status, upsert_event
from stripe_integration.stripe_integration.payout_sync import sync_payout_from_webhook_event
from stripe_integration.stripe_integration.refunds import apply_refund_to_erp
from stripe_integration.stripe_integration.stripe_fees import ensure_fee_posted
from stripe_integration.stripe_integration.subscription_payments import handle_invoice_paid
from stripe_integration.stripe_integration.subscription_sync import (
    SETUP_INTENT_FIELD,
    SETUP_PM_FIELD,
    SETUP_STATUS_FIELD,
    _set_subscription_fields,
    ensure_stripe_subscription_for_subscription,
    sync_subscription_from_webhook_event,
)
from stripe_integration.stripe_integration.utils import (
    get_api_key,
    get_company_abbr_from_company,
    get_webhook_secret,
    resolve_customer_email,
)

SENSITIVE_LOG_FIELDS = {
    "email",
    "name",
    "phone",
    "address",
    "line1",
    "line2",
    "postal_code",
    "city",
    "state",
    "country",
    "customer_details",
    "billing_details",
    "shipping",
    "description",
    "receipt_email",
    "client_secret",
    "secret",
    "api_key",
    "webhook_secret",
    "payment_method",
    "source",
}

# Webhook abuse-protection defaults.
WEBHOOK_MAX_PAYLOAD_BYTES = 256 * 1024
WEBHOOK_RATE_LIMIT_WINDOW_SECONDS = 60
WEBHOOK_RATE_LIMIT_MAX_PER_IP = 120
WEBHOOK_GLOBAL_RATE_LIMIT_WINDOW_SECONDS = 600
WEBHOOK_GLOBAL_RATE_LIMIT_MAX = 1000
WEBHOOK_ACCOUNT_ABBRS = ("COE", "COSL")


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
        value = getattr(obj, key)
        if not callable(value):
            return default if value is None else value
    except Exception:
        pass
    return default


def _normalize_company_abbr(value: str | None) -> str | None:
    v = (value or "").strip().upper()
    return v or None


def _extract_event_metadata(event: dict | None) -> dict:
    obj = (((event or {}).get("data") or {}).get("object") or {})
    metadata = obj.get("metadata") or {}
    if metadata:
        return metadata

    # Basil API versions moved subscription invoice metadata under parent.
    subscription_details = ((obj.get("parent") or {}).get("subscription_details") or {})
    metadata = subscription_details.get("metadata") or {}
    if metadata:
        return metadata

    # Retain a final fallback for invoice payloads that only include line metadata.
    for line in ((obj.get("lines") or {}).get("data") or []):
        metadata = (line or {}).get("metadata") or {}
        if metadata:
            return metadata

    return {}


def _extract_claimed_company_abbr(event: dict | None) -> str | None:
    metadata = _extract_event_metadata(event)

    company_abbr = _normalize_company_abbr(metadata.get("company_abbr"))
    if company_abbr:
        return company_abbr

    company_name = (metadata.get("company") or "").strip()
    if not company_name:
        return None

    try:
        return _normalize_company_abbr(get_company_abbr_from_company(company_name))
    except Exception:
        return None


def _extract_doc_company_abbr(event: dict | None) -> str | None:
    metadata = _extract_event_metadata(event)
    doctype = (metadata.get("doctype") or "").strip()
    docname = (metadata.get("docname") or "").strip()
    if not doctype or not docname:
        return None

    try:
        company = frappe.db.get_value(doctype, docname, "company")
        if not company:
            return None
        return _normalize_company_abbr(get_company_abbr_from_company(company))
    except Exception:
        return None


def _mask_identifier(value: str, keep_prefix: int = 6, keep_suffix: int = 4) -> str:
    if not value:
        return ""
    value = str(value)
    if len(value) <= (keep_prefix + keep_suffix):
        return "***"
    return f"{value[:keep_prefix]}...{value[-keep_suffix:]}"


def _sanitize_for_log(data, depth: int = 0):
    if depth > 4:
        return "[truncated]"

    if isinstance(data, dict):
        out = {}
        for key, value in data.items():
            lk = str(key).lower()
            if lk in SENSITIVE_LOG_FIELDS:
                out[key] = "[redacted]"
            elif lk.endswith("_id") and isinstance(value, str):
                out[key] = _mask_identifier(value)
            elif lk == "metadata" and isinstance(value, dict):
                # Keep only integration-routing metadata keys.
                out[key] = {
                    mk: value.get(mk)
                    for mk in ("company_abbr", "doctype", "docname", "request_kind")
                    if mk in value
                }
            else:
                out[key] = _sanitize_for_log(value, depth + 1)
        return out

    if isinstance(data, list):
        # Keep bounded sample only.
        return [_sanitize_for_log(v, depth + 1) for v in data[:5]]

    if isinstance(data, str):
        return data if len(data) <= 120 else f"{data[:120]}...[truncated]"

    return data


def _build_safe_payload_text(payload: bytes, event: dict | None, matched_company_abbr: str | None = None) -> str:
    payload_hash = hashlib.sha256(payload or b"").hexdigest()
    size = len(payload or b"")

    base = {
        "payload_hash": payload_hash,
        "payload_size": size,
    }
    if matched_company_abbr:
        base["matched_company_abbr"] = matched_company_abbr

    if not event:
        return json.dumps(base, default=str)

    safe = {
        "id": event.get("id"),
        "type": event.get("type"),
        "created": event.get("created"),
        "livemode": event.get("livemode"),
        "data": {
            "object": _sanitize_for_log((event.get("data") or {}).get("object") or {}),
        },
    }
    base["event"] = safe
    return json.dumps(base, default=str)


def _cache_get():
    cache_fn = getattr(frappe, "cache", None)
    if callable(cache_fn):
        return cache_fn()
    return cache_fn


def _cache_inc_with_ttl(key: str, ttl_seconds: int) -> int:
    cache = _cache_get()
    if not cache:
        return 0

    try:
        cache.incr(key)
    except Exception:
        cache.set_value(key, 1, expires_in_sec=ttl_seconds)
        return 1

    try:
        cache.expire(key, ttl_seconds)
    except Exception:
        pass

    try:
        return int(cache.get_value(key) or 0)
    except Exception:
        return 0


def _enforce_webhook_rate_limits(payload: bytes):
    if len(payload or b"") > WEBHOOK_MAX_PAYLOAD_BYTES:
        frappe.response.status_code = 413
        return {"status": "payload_too_large"}

    ip = (
        frappe.get_request_header("X-Forwarded-For")
        or frappe.get_request_header("CF-Connecting-IP")
        or getattr(getattr(frappe, "local", None), "request_ip", None)
        or "unknown"
    )
    ip = str(ip).split(",")[0].strip() or "unknown"

    ip_bucket = int(time.time() // WEBHOOK_RATE_LIMIT_WINDOW_SECONDS)
    global_bucket = int(time.time() // WEBHOOK_GLOBAL_RATE_LIMIT_WINDOW_SECONDS)

    ip_key = f"stripe:webhook:ip:{ip}:{ip_bucket}"
    global_key = f"stripe:webhook:global:{global_bucket}"

    ip_count = _cache_inc_with_ttl(ip_key, WEBHOOK_RATE_LIMIT_WINDOW_SECONDS + 5)
    global_count = _cache_inc_with_ttl(global_key, WEBHOOK_GLOBAL_RATE_LIMIT_WINDOW_SECONDS + 5)

    if ip_count > WEBHOOK_RATE_LIMIT_MAX_PER_IP or global_count > WEBHOOK_GLOBAL_RATE_LIMIT_MAX:
        frappe.response.status_code = 429
        return {"status": "rate_limited"}

    return None


def _integration_request_is_terminal(event_id: str) -> bool:
    integration_terminal = frappe.db.exists(
        "Integration Request",
        {
            "integration_request_service": "Stripe",
            "request_id": event_id,
            "status": ["in", ["Completed", "Ignored"]],
        },
    )
    if integration_terminal:
        return True
    return bool(
        frappe.db.exists(
            "Stripe Event Log",
            {"event_id": event_id, "status": ["in", ["Completed", "Ignored"]]},
        )
    )


def _log_integration_request(event_id: str, status: str, payload_text: str, output_text: str = ""):
    existing = frappe.db.get_value(
        "Integration Request",
        {"integration_request_service": "Stripe", "request_id": event_id},
        "name",
    )

    if not existing:
        doc = frappe.new_doc("Integration Request")
        doc.integration_request_service = "Stripe"
        doc.is_webhook_call = 1
        doc.request_id = event_id
        doc.status = status
        doc.data = payload_text
        doc.output = output_text
        doc.insert(ignore_permissions=True)
        name = doc.name
    else:
        name = existing
        frappe.db.set_value(
            "Integration Request",
            name,
            {"status": status, "output": output_text},
            update_modified=False,
        )

    frappe.db.commit()
    return name


@frappe.whitelist(allow_guest=True)
def handle_webhook():
    payload = frappe.request.get_data() or b""

    blocked = _enforce_webhook_rate_limits(payload)
    if blocked:
        return blocked

    sig_header = frappe.get_request_header("Stripe-Signature")

    event = None
    matched_company_abbr = None
    for abbr in WEBHOOK_ACCOUNT_ABBRS:
        endpoint_secret = get_webhook_secret(abbr)
        if not endpoint_secret:
            continue
        tolerance = int(
            frappe.db.get_value("Stripe Account", abbr, "webhook_tolerance_seconds")
            or 300
        )
        try:
            event = stripe.Webhook.construct_event(
                payload,
                sig_header,
                endpoint_secret,
                tolerance=tolerance,
            )
            matched_company_abbr = _normalize_company_abbr(abbr)
            break
        except Exception:
            continue

    if not event:
        frappe.log_error(frappe.get_traceback(), "Stripe Webhook Signature Verification Failed")
        frappe.response.status_code = 400
        return {"status": "invalid"}

    event_id = event.get("id")
    event_type = event.get("type")
    event_livemode = event.get("livemode")
    if event_livemode is not None and matched_company_abbr:
        account_test_mode = bool(
            frappe.db.get_value("Stripe Account", matched_company_abbr, "test_mode")
        )
        if bool(event_livemode) == account_test_mode:
            frappe.response.status_code = 400
            return {"status": "account_mode_mismatch"}

    claimed_company_abbr = _extract_claimed_company_abbr(event)
    doc_company_abbr = _extract_doc_company_abbr(event)

    if claimed_company_abbr and matched_company_abbr and claimed_company_abbr != matched_company_abbr:
        frappe.response.status_code = 400
        return {"status": "account_mismatch", "reason": "metadata_company_abbr_mismatch"}

    if doc_company_abbr and matched_company_abbr and doc_company_abbr != matched_company_abbr:
        frappe.response.status_code = 400
        return {"status": "account_mismatch", "reason": "document_company_abbr_mismatch"}

    if claimed_company_abbr and doc_company_abbr and claimed_company_abbr != doc_company_abbr:
        frappe.response.status_code = 400
        return {"status": "company_mismatch", "reason": "metadata_vs_document_mismatch"}

    company_abbr = claimed_company_abbr or doc_company_abbr or matched_company_abbr
    safe_payload_text = _build_safe_payload_text(payload, event, matched_company_abbr=matched_company_abbr)

    if event_id and _integration_request_is_terminal(event_id):
        return {"status": "ok", "idempotent": True}

    lock_name = f"stripe-event-{event_id or hashlib.sha256(payload).hexdigest()}"
    with MariaDBNamedLock(lock_name, timeout=30):
        if event_id and _integration_request_is_terminal(event_id):
            return {"status": "ok", "idempotent": True}

        req_name = None
        if event_id:
            req_name = _log_integration_request(
                event_id=event_id,
                status="Queued",
                payload_text=safe_payload_text,
            )
            try:
                upsert_event(
                    event=event,
                    payload=payload,
                    company_abbr=company_abbr,
                    request_id=req_name,
                    status="Queued",
                )
            except Exception:
                frappe.log_error(frappe.get_traceback(), "Stripe Event Log upsert failed (Queued)")

        try:
            handler_result = _dispatch_verified_event(event, company_abbr)
            if handler_result.get("retryable") and not handler_result.get("handled"):
                frappe.throw(
                    f"Stripe {event_type} was not reconciled: {handler_result.get('reason') or 'unknown'}",
                    frappe.ValidationError,
                )

            final_status = "Completed" if handler_result.get("handled") else "Ignored"
            if event_id:
                _log_integration_request(
                    event_id=event_id,
                    status=final_status,
                    payload_text=safe_payload_text,
                    output_text=json.dumps(handler_result, default=str),
                )
                try:
                    mark_event_status(
                        event_id,
                        final_status,
                        None if handler_result.get("handled") else handler_result.get("reason"),
                    )
                except Exception:
                    frappe.log_error(frappe.get_traceback(), f"Stripe Event Log update failed ({final_status})")
            return {"status": "ok", "result": handler_result}
        except Exception as exc:
            if event_id:
                try:
                    frappe.db.rollback()
                    _log_integration_request(
                        event_id=event_id,
                        status="Failed",
                        payload_text=safe_payload_text,
                        output_text=json.dumps({"error": str(exc)}, default=str),
                    )
                    mark_event_status(event_id, "Failed", str(exc))
                    frappe.db.commit()
                except Exception:
                    pass
            raise


def _dispatch_verified_event(event: dict, company_abbr: str | None):
    event_type = (event or {}).get("type") or ""

    if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        return _handle_checkout_session(event["data"]["object"])
    if event_type == "payment_intent.succeeded":
        return _handle_payment_intent_succeeded(event["data"]["object"])
    if event_type == "invoice.paid":
        result = handle_invoice_paid(event) or {"handled": False, "reason": "empty_handler_result"}
        if not result.get("handled"):
            result["retryable"] = True
        return result
    if event_type.startswith("customer.subscription.") and event_type != "customer.subscription.trial_will_end":
        return sync_subscription_from_webhook_event(event) or {
            "handled": False,
            "reason": "empty_subscription_handler_result",
        }
    if event_type in ("charge.refunded", "charge.refund.updated", "refund.updated"):
        return _handle_refund_event(event, company_abbr=company_abbr) or {
            "handled": False,
            "reason": "empty_refund_handler_result",
        }
    if event_type == "payout.paid":
        result = sync_payout_from_webhook_event(event, company_abbr_hint=company_abbr)
        if result and not result.get("handled") and result.get("reason") not in {
            "payout_sync_disabled",
            "company_payout_sync_disabled",
            "manual_payout_requires_review",
        }:
            result["retryable"] = True
        return result or {"handled": False, "reason": "empty_payout_handler_result", "retryable": True}
    if event_type.startswith("payout."):
        return {"handled": False, "reason": "payout_event_not_actionable", "event_type": event_type}

    return {"handled": False, "reason": "event_not_routed", "event_type": event_type}


def _handle_checkout_session(session: dict):
    frappe.set_user("Administrator")

    metadata = session.get("metadata") or {}
    doctype = metadata.get("doctype")
    docname = metadata.get("docname")
    request_kind = metadata.get("request_kind")

    if doctype == "Subscription" and docname:
        return _handle_subscription_setup_session(session)

    if doctype != "Sales Invoice" or not docname:
        return {"handled": False, "reason": "checkout_not_linked_to_sales_invoice"}

    if (session.get("status") or "complete") != "complete":
        return {"handled": False, "reason": "checkout_not_complete"}

    if session.get("payment_status") != "paid":
        return {"handled": False, "reason": "checkout_not_paid"}

    paid_amount = (session.get("amount_total", 0) or 0) / 100.0
    pi_id = session.get("payment_intent")

    if not pi_id:
        return {"handled": False, "reason": "checkout_missing_payment_intent", "retryable": True}

    result = _create_payment_entry_for_sales_invoice(
        docname,
        pi_id,
        paid_amount,
        paid_currency=session.get("currency"),
        expected_amount=metadata.get("requested_amount"),
        request_kind=request_kind,
        posting_date=stripe_timestamp_date(session.get("created")),
    )

    # Deactivate the Payment Link so it cannot be paid again
    payment_link_id = session.get("payment_link")
    if payment_link_id and result.get("handled"):
        try:
            company_abbr = metadata.get("company_abbr")
            if not company_abbr:
                frappe.throw("Missing company for Stripe Payment Link deactivation")
            stripe.PaymentLink.modify(
                payment_link_id,
                active=False,
                api_key=get_api_key(company_abbr),
            )
            for fieldname in (
                "stripe_checkout_url",
                "stripe_checkout_session_id",
                "stripe_last_payment_link_sent",
            ):
                if frappe.get_meta("Sales Invoice").get_field(fieldname):
                    frappe.db.set_value(
                        "Sales Invoice",
                        docname,
                        fieldname,
                        None,
                        update_modified=False,
                    )
            frappe.db.commit()
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Stripe: failed to deactivate Payment Link after payment")

    return result


def _handle_payment_intent_succeeded(payment_intent: dict):
    metadata = payment_intent.get("metadata") or {}
    if metadata.get("doctype") != "Sales Invoice" or not metadata.get("docname"):
        return {"handled": False, "reason": "payment_intent_not_linked_to_sales_invoice"}
    if payment_intent.get("status") != "succeeded":
        return {"handled": False, "reason": "payment_intent_not_succeeded"}

    amount_received = float(payment_intent.get("amount_received") or 0) / 100.0
    return _create_payment_entry_for_sales_invoice(
        metadata.get("docname"),
        payment_intent.get("id"),
        amount_received,
        paid_currency=payment_intent.get("currency"),
        expected_amount=metadata.get("requested_amount"),
        request_kind=metadata.get("request_kind"),
        posting_date=stripe_timestamp_date(payment_intent.get("created")),
    )


def _handle_subscription_setup_session(session: dict):
    metadata = session.get("metadata") or {}
    sub_name = metadata.get("docname")
    if not sub_name or not frappe.db.exists("Subscription", sub_name):
        return {"handled": False, "reason": "subscription_not_found", "retryable": True}

    if (session.get("mode") or "setup") != "setup" or (session.get("status") or "complete") != "complete":
        return {"handled": False, "reason": "setup_checkout_not_complete", "retryable": True}

    company_abbr = metadata.get("company_abbr") or get_company_abbr_from_company(
        frappe.db.get_value("Subscription", sub_name, "company")
    )
    if not company_abbr:
        return {"handled": False, "reason": "subscription_company_not_found", "retryable": True}

    api_key = get_api_key(company_abbr)

    setup_intent_id = session.get("setup_intent")
    stripe_sub_id = metadata.get("stripe_subscription_id") or frappe.db.get_value("Subscription", sub_name, "stripe_subscription_id")

    if not setup_intent_id:
        return {"handled": False, "reason": "setup_intent_missing", "retryable": True}

    si = stripe.SetupIntent.retrieve(setup_intent_id, api_key=api_key)
    if _stripe_get(si, "status") != "succeeded":
        return {"handled": False, "reason": "setup_intent_not_succeeded", "retryable": True}

    payment_method = _stripe_get(si, "payment_method")
    stripe_customer_id = session.get("customer") or _stripe_get(si, "customer")

    if not payment_method:
        return {"handled": False, "reason": "setup_payment_method_missing", "retryable": True}

    # Ensure payment method is reusable for off-session subscription charges
    if stripe_customer_id:
        payment_method_doc = stripe.PaymentMethod.retrieve(payment_method, api_key=api_key)
        attached_customer = _stripe_get(payment_method_doc, "customer")
        if attached_customer and attached_customer != stripe_customer_id:
            return {"handled": False, "reason": "payment_method_customer_mismatch", "retryable": True}
        if not attached_customer:
            stripe.PaymentMethod.attach(
                payment_method,
                customer=stripe_customer_id,
                api_key=api_key,
            )
        stripe.Customer.modify(
            stripe_customer_id,
            invoice_settings={"default_payment_method": payment_method},
            api_key=api_key,
        )

    if stripe_sub_id:
        stripe.Subscription.modify(
            stripe_sub_id,
            default_payment_method=payment_method,
            api_key=api_key,
        )

    update = {
        SETUP_STATUS_FIELD: "completed",
        SETUP_PM_FIELD: payment_method,
        SETUP_INTENT_FIELD: setup_intent_id,
        "stripe_customer_id": stripe_customer_id,
    }
    _set_subscription_fields(sub_name, update)

    creation_result = None
    if not stripe_sub_id:
        creation_result = ensure_stripe_subscription_for_subscription(
            sub_name,
            payment_method=payment_method,
            stripe_customer_id=stripe_customer_id,
        )
        if not creation_result.get("created") and creation_result.get("reason") != "already_linked":
            creation_result["handled"] = False
            creation_result["retryable"] = True
            return creation_result

    return {
        "handled": True,
        "subscription": sub_name,
        "payment_method_saved": True,
        "stripe_subscription_id": stripe_sub_id or (creation_result or {}).get("stripe_subscription_id"),
    }


def _resolve_refund_invoice_name(
    stripe_payment_intent_id: str | None,
    metadata: dict | None,
    company_abbr: str | None,
):
    metadata = metadata or {}
    if metadata.get("doctype") == "Sales Invoice" and metadata.get("docname"):
        return metadata.get("docname")
    if not stripe_payment_intent_id:
        return None

    if not company_abbr:
        frappe.throw("Missing company for Stripe refund reconciliation")
    payment_intent = stripe.PaymentIntent.retrieve(
        stripe_payment_intent_id,
        api_key=get_api_key(company_abbr),
    )
    payment_metadata = _stripe_get(payment_intent, "metadata") or {}
    payment_company = (payment_metadata.get("company_abbr") or "").strip().upper()
    if payment_company and company_abbr and payment_company != company_abbr:
        frappe.throw("Stripe refund PaymentIntent belongs to a different company")
    if payment_metadata.get("doctype") == "Sales Invoice":
        return payment_metadata.get("docname")
    return None


def _apply_refund_object(
    refund: dict,
    stripe_payment_intent_id: str | None,
    source: str,
    company_abbr: str | None,
    fallback_metadata: dict | None = None,
):
    refund_metadata = refund.get("metadata") or fallback_metadata or {}
    invoice_name = _resolve_refund_invoice_name(
        stripe_payment_intent_id,
        refund_metadata,
        company_abbr,
    )
    result = apply_refund_to_erp(
        stripe_payment_intent_id=stripe_payment_intent_id,
        stripe_refund_id=refund.get("id"),
        refund_amount=float((refund.get("amount") or 0) / 100.0),
        currency=(refund.get("currency") or "").upper() or "CAD",
        source=source,
        invoice_name=invoice_name,
    )
    if not result.get("handled") and (
        result.get("reason") != "payment_entry_not_found" or invoice_name
    ):
        result["retryable"] = True
    return result


def _handle_refund_event(event: dict, company_abbr: str | None = None):
    obj = (event or {}).get("data", {}).get("object", {}) or {}
    event_type = (event or {}).get("type")

    # charge.refunded carries a Charge. refund.updated and the legacy
    # charge.refund.updated event carry a Refund directly.
    if event_type in ("refund.updated", "charge.refund.updated"):
        if (obj.get("status") or "") != "succeeded":
            return {"handled": True, "reason": "refund_not_succeeded"}
        stripe_pi_id = obj.get("payment_intent")
        return _apply_refund_object(
            obj,
            stripe_pi_id,
            source=f"webhook.{event_type}",
            company_abbr=company_abbr,
        )

    stripe_pi_id = obj.get("payment_intent")
    refunds = ((obj.get("refunds") or {}).get("data") or [])
    if not refunds:
        return {"handled": True, "reason": "no_refund_items"}

    results = []
    charge_metadata = obj.get("metadata") or {}
    for refund in refunds:
        if (refund.get("status") or "") != "succeeded":
            continue
        results.append(
            _apply_refund_object(
                refund,
                stripe_pi_id,
                source="webhook.charge.refunded",
                company_abbr=company_abbr,
                fallback_metadata=charge_metadata,
            )
        )

    if not results:
        return {"handled": True, "reason": "no_succeeded_refunds"}
    retryable = next(
        (result for result in results if result.get("retryable") and not result.get("handled")),
        None,
    )
    if retryable:
        return {
            "handled": False,
            "retryable": True,
            "reason": retryable.get("reason"),
            "refund_results": results,
        }
    return {
        "handled": any(result.get("handled") for result in results),
        "reason": None if any(result.get("handled") for result in results) else "refund_not_linked_to_erp",
        "refund_results": results,
    }


def _create_payment_entry_for_sales_invoice(
    sales_invoice_name: str,
    stripe_pi_id: str,
    paid_amount: float,
    request_kind: str | None = None,
    paid_currency: str | None = None,
    expected_amount: str | float | None = None,
    posting_date: str | None = None,
):
    if not stripe_pi_id:
        return {"handled": False, "reason": "missing_payment_intent", "retryable": True}

    lock_name = f"stripe_pi_{stripe_pi_id}"

    with MariaDBNamedLock(lock_name, timeout=30):
        if not frappe.db.exists("Sales Invoice", sales_invoice_name):
            return {"handled": False, "reason": "sales_invoice_not_found", "retryable": True}

        if frappe.db.exists(
            "Payment Entry",
            {"reference_no": stripe_pi_id, "docstatus": ["!=", 2]},
        ):
            return {"handled": True, "dedup": True, "sales_invoice": sales_invoice_name}

        invoice = frappe.get_doc("Sales Invoice", sales_invoice_name)
        if invoice.docstatus != 1:
            return {"handled": False, "reason": "invoice_not_submitted", "retryable": True}

        company_abbr = get_company_abbr_from_company(invoice.company)
        validate_stripe_currency(paid_currency, invoice.currency, f"Sales Invoice {invoice.name}")

        paid_amount = float(paid_amount or 0)
        if paid_amount <= 0:
            return {"handled": False, "reason": "non_positive_payment_amount", "retryable": True}

        if expected_amount not in (None, ""):
            try:
                requested_amount = float(expected_amount)
            except (TypeError, ValueError):
                return {"handled": False, "reason": "invalid_requested_amount_metadata", "retryable": True}
            if abs(requested_amount - paid_amount) > 0.01:
                return {
                    "handled": False,
                    "reason": "checkout_amount_mismatch",
                    "paid_amount": paid_amount,
                    "requested_amount": requested_amount,
                    "retryable": True,
                }

        outstanding = float(invoice.outstanding_amount or 0)
        pe, allocated_amount, unallocated_amount = prepare_stripe_receipt_payment_entry(
            invoice,
            paid_amount,
            stripe_pi_id,
            company_abbr,
            posting_date=posting_date,
        )

        try:
            pe.insert(ignore_permissions=True)
            pe.submit()

            # For Split Payment workflow: mark that at least one Stripe payment was processed.
            # This makes the next "Request Payment (Stripe)" send the remaining balance.
            if frappe.get_meta("Sales Invoice").get_field("custom_stripe_payment_processed"):
                frappe.db.set_value("Sales Invoice", invoice.name, "custom_stripe_payment_processed", 1, update_modified=False)

            # Populate stripe_payment_intent_id on the Payment Entry so refund
            # events that look up by PI can locate the row. Without this, only
            # legacy rows fixed by backfill_payment_entry_stripe_pi_id are
            # findable; new PEs would fall through to manual matching.
            if frappe.get_meta("Payment Entry").get_field("stripe_payment_intent_id"):
                frappe.db.set_value("Payment Entry", pe.name, "stripe_payment_intent_id", stripe_pi_id, update_modified=False)
            if frappe.get_meta("Sales Invoice").get_field("stripe_payment_intent_id"):
                frappe.db.set_value("Sales Invoice", invoice.name, "stripe_payment_intent_id", stripe_pi_id, update_modified=False)

            frappe.db.commit()
        except DuplicateEntryError:
            frappe.db.rollback()
            return {"handled": True, "dedup": True, "sales_invoice": invoice.name}

        fee_result = ensure_fee_posted(
            company_abbr=company_abbr,
            stripe_payment_intent_id=stripe_pi_id,
            remark_ctx=f"checkout payment {invoice.name}",
            enqueue_retry=True,
        )

        # Receipt email runs AFTER the commit, wrapped, so an SMTP failure can
        # never roll back the Payment Entry we just submitted. Logging is best
        # effort; the customer-facing payment is what matters.
        try:
            _send_payment_receipt_email(invoice, pe, request_kind=request_kind)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Stripe: payment receipt email failed (PE already committed)")

        return {
            "handled": True,
            "sales_invoice": invoice.name,
            "payment_entry": pe.name,
            "payment_intent": stripe_pi_id,
            "invoice_outstanding_before": outstanding,
            "allocated_amount": allocated_amount,
            "unallocated_amount": unallocated_amount,
            "balance_changed_since_request": abs(paid_amount - outstanding) > 0.01,
            "fee": fee_result,
        }


def _get_customer_email_from_invoice(invoice):
    for fn in ("contact_email", "customer_email", "email_id"):
        v = invoice.get(fn)
        if v:
            return v
    cust = invoice.get("customer")
    if cust:
        return resolve_customer_email(cust)
    return None


def _send_payment_receipt_email(invoice, pe, request_kind=None):
    # Reload invoice after Payment Entry submit to avoid stale outstanding_amount
    invoice = frappe.get_doc("Sales Invoice", invoice.name)

    company_abbr = get_company_abbr_from_company(invoice.get("company")) or ""
    is_cosl = (company_abbr.upper() == "COSL")
    sender = "CoreOrbit Billing <billing@coreorbit.io>" if is_cosl else "COEngine <erp@coengine.ai>"

    to_email = _get_customer_email_from_invoice(invoice)
    if not to_email:
        return

    grand_total = float(invoice.get("grand_total") or 0)
    payment_amount = float(pe.get("paid_amount") or pe.get("received_amount") or 0)

    # Prefer ledger-derived remaining, but guard against stale invoice values at send time.
    invoice_remaining = float(invoice.get("outstanding_amount") or 0)
    computed_remaining = max(grand_total - payment_amount, 0.0)

    if payment_amount >= (grand_total - 0.01):
        remaining_balance = 0.0
    else:
        remaining_balance = max(min(invoice_remaining, computed_remaining), 0.0)

    payment_percentage = round((payment_amount / grand_total) * 100, 2) if grand_total > 0 else 100
    remaining_percentage = round((remaining_balance / grand_total) * 100, 2) if grand_total > 0 else 0

    is_fully_paid = remaining_balance <= 0.01
    is_partial_payment = not is_fully_paid

    args = {
        "payment_entry_name": pe.name,
        "invoice_name": invoice.name,
        "customer_name": invoice.get("customer_name") or invoice.get("customer") or "Customer",
        "company": invoice.get("company") or "",
        "is_cosl": 1 if is_cosl else 0,
        "is_partial_payment": 1 if is_partial_payment else 0,
        "is_fully_paid": 1 if is_fully_paid else 0,
        "has_remaining": 1 if remaining_balance > 0.01 else 0,
        "request_kind": request_kind or "full",
        "is_initial_payment": 1 if (request_kind == "deposit") else 0,
        "is_remainder_payment": 1 if (request_kind == "remainder") else 0,
        "payment_percentage": payment_percentage,
        "grand_total": f"{grand_total:.2f}",
        "payment_amount": f"{payment_amount:.2f}",
        "remaining_balance": remaining_balance,
        "remaining_percentage": remaining_percentage,
        "currency": invoice.get("currency") or "CAD",
        "posting_date": pe.get("posting_date") or frappe.utils.nowdate(),
    }

    et = frappe.get_doc("Email Template", "Stripe Payment Receipt Branded")
    subject = frappe.render_template(et.subject or "Payment Receipt", args)
    message = frappe.render_template(et.response or "", args)

    pf = "Payment Receipt - CoreOrbit" if is_cosl else "Payment Receipt - COEngine"
    pe_pdf = frappe.attach_print("Payment Entry", pe.name, file_name=f"{pe.name}.pdf", print_format=pf)

    frappe.sendmail(
        recipients=[to_email],
        subject=subject,
        message=message,
        sender=sender,
        now=True,
        delayed=False,
        with_container=False,
        add_unsubscribe_link=0,
        reference_doctype="Payment Entry",
        reference_name=pe.name,
        attachments=[pe_pdf] if pe_pdf else None,
    )
