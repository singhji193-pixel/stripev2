import json
import frappe
import stripe

from frappe.exceptions import DuplicateEntryError
from stripe_integration.stripe_integration.utils import get_webhook_secret, get_company_abbr_from_company, get_api_key
from stripe_integration.stripe_integration.event_log import upsert_event, mark_event_status
from stripe_integration.stripe_integration.subscription_payments import handle_invoice_paid
from stripe_integration.stripe_integration.subscription_sync import (
    sync_subscription_from_webhook_event,
    _set_subscription_fields,
    SETUP_STATUS_FIELD,
    SETUP_PM_FIELD,
    SETUP_INTENT_FIELD,
)
from stripe_integration.stripe_integration.payout_sync import sync_payout_from_webhook_event
from stripe_integration.stripe_integration.refunds import apply_refund_to_erp


def _integration_request_is_completed(event_id: str) -> bool:
    return frappe.db.exists(
        "Integration Request",
        {
            "integration_request_service": "Stripe",
            "request_id": event_id,
            "status": "Completed",
        },
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


class _MariaDBNamedLock:
    """DB-level lock (MariaDB GET_LOCK) to prevent multi-worker races."""

    def __init__(self, name: str, timeout: int = 30):
        self.name = name
        self.timeout = timeout
        self.acquired = False

    def __enter__(self):
        # GET_LOCK returns 1 on success, 0 on timeout, NULL on error
        res = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (self.name, self.timeout))
        ok = bool(res and res[0] and res[0][0] == 1)
        if not ok:
            frappe.throw("Could not acquire Stripe webhook lock", frappe.ValidationError)
        self.acquired = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.acquired:
            try:
                frappe.db.sql("SELECT RELEASE_LOCK(%s)", (self.name,))
            except Exception:
                pass


@frappe.whitelist(allow_guest=True)
def handle_webhook():
    payload = frappe.request.get_data() or b""
    sig_header = frappe.get_request_header("Stripe-Signature")

    event = None
    for abbr in ["COE", "COSL"]:
        endpoint_secret = get_webhook_secret(abbr)
        if not endpoint_secret:
            continue
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
            break
        except Exception:
            continue

    if not event:
        frappe.log_error(frappe.get_traceback(), "Stripe Webhook Signature Verification Failed")
        frappe.response.status_code = 400
        return {"status": "invalid"}

    event_id = event.get("id")
    event_type = event.get("type")

    company_abbr = None
    try:
        metadata = (event.get("data", {}).get("object", {}) or {}).get("metadata", {}) or {}
        company_abbr = metadata.get("company_abbr")
    except Exception:
        company_abbr = None


    if event_id and _integration_request_is_completed(event_id):
        return {"status": "ok", "idempotent": True}

    req_name = None
    if event_id:
        req_name = _log_integration_request(
            event_id=event_id,
            status="Queued",
            payload_text=payload.decode("utf-8", errors="replace"),
        )
        try:
            upsert_event(event=event, payload=payload, company_abbr=company_abbr, request_id=req_name, status="Queued")
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Stripe Event Log upsert failed (Queued)")

    try:
        if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            session = event["data"]["object"]
            _handle_checkout_session(session)
        elif event_type == "invoice.paid":
            handle_invoice_paid(event)
        elif event_type.startswith("customer.subscription.") and event_type not in ("customer.subscription.created", "customer.subscription.trial_will_end"):
            sync_subscription_from_webhook_event(event)
        elif event_type in ("charge.refunded", "refund.updated"):
            _handle_refund_event(event)
        elif event_type.startswith("payout."):
            sync_payout_from_webhook_event(event)

        if event_id and not _integration_request_is_completed(event_id):
            _log_integration_request(
                event_id=event_id,
                status="Completed",
                payload_text=payload.decode("utf-8", errors="replace"),
                output_text=json.dumps({"handled": True}),
            )
        if event_id:
            try:
                mark_event_status(event_id, "Completed")
            except Exception:
                frappe.log_error(frappe.get_traceback(), "Stripe Event Log update failed (Completed)")
        return {"status": "ok"}
    except Exception as e:
        if event_id:
            try:
                mark_event_status(event_id, "Failed", str(e))
            except Exception:
                pass
        raise


def _handle_checkout_session(session: dict):
    frappe.set_user("Administrator")

    metadata = session.get("metadata") or {}
    doctype = metadata.get("doctype")
    docname = metadata.get("docname")
    request_kind = metadata.get("request_kind")

    if doctype == "Subscription" and docname:
        _handle_subscription_setup_session(session)
        return

    if doctype != "Sales Invoice" or not docname:
        return

    paid_amount = (session.get("amount_total", 0) or 0) / 100.0
    pi_id = session.get("payment_intent")

    if not pi_id:
        return

    _create_payment_entry_for_sales_invoice(docname, pi_id, paid_amount, request_kind=request_kind)


def _handle_subscription_setup_session(session: dict):
    metadata = session.get("metadata") or {}
    sub_name = metadata.get("docname")
    if not sub_name or not frappe.db.exists("Subscription", sub_name):
        return

    company_abbr = metadata.get("company_abbr") or get_company_abbr_from_company(
        frappe.db.get_value("Subscription", sub_name, "company")
    )
    if not company_abbr:
        return

    stripe.api_key = get_api_key(company_abbr)

    setup_intent_id = session.get("setup_intent")
    stripe_sub_id = metadata.get("stripe_subscription_id") or frappe.db.get_value("Subscription", sub_name, "stripe_subscription_id")

    if not setup_intent_id:
        return

    si = stripe.SetupIntent.retrieve(setup_intent_id)
    payment_method = getattr(si, "payment_method", None)
    stripe_customer_id = session.get("customer") or getattr(si, "customer", None)

    if not payment_method:
        return

    # Ensure payment method is reusable for off-session subscription charges
    if stripe_customer_id:
        try:
            stripe.PaymentMethod.attach(payment_method, customer=stripe_customer_id)
        except Exception:
            pass

        try:
            stripe.Customer.modify(stripe_customer_id, invoice_settings={"default_payment_method": payment_method})
        except Exception:
            pass

    if stripe_sub_id:
        try:
            stripe.Subscription.modify(stripe_sub_id, default_payment_method=payment_method)
        except Exception:
            pass

    _set_subscription_fields(
        sub_name,
        {
            SETUP_STATUS_FIELD: "completed",
            SETUP_PM_FIELD: payment_method,
            SETUP_INTENT_FIELD: setup_intent_id,
        },
    )


def _handle_refund_event(event: dict):
    obj = (event or {}).get("data", {}).get("object", {}) or {}
    event_type = (event or {}).get("type")

    # charge.refunded carries charge object with `refunds.data[*]` and `payment_intent`
    # refund.updated carries refund object directly.
    if event_type == "refund.updated":
        if (obj.get("status") or "") != "succeeded":
            return {"handled": True, "reason": "refund_not_succeeded"}
        stripe_pi_id = obj.get("payment_intent")
        stripe_refund_id = obj.get("id")
        refund_amount = float((obj.get("amount") or 0) / 100.0)
        currency = (obj.get("currency") or "").upper() or "CAD"
        return apply_refund_to_erp(
            stripe_payment_intent_id=stripe_pi_id,
            stripe_refund_id=stripe_refund_id,
            refund_amount=refund_amount,
            currency=currency,
            source="webhook.refund.updated",
        )

    stripe_pi_id = obj.get("payment_intent")
    refunds = ((obj.get("refunds") or {}).get("data") or [])
    if not refunds:
        return {"handled": True, "reason": "no_refund_items"}

    refund = refunds[-1]
    if (refund.get("status") or "") != "succeeded":
        return {"handled": True, "reason": "latest_refund_not_succeeded"}

    return apply_refund_to_erp(
        stripe_payment_intent_id=stripe_pi_id,
        stripe_refund_id=refund.get("id"),
        refund_amount=float((refund.get("amount") or 0) / 100.0),
        currency=(refund.get("currency") or "").upper() or "CAD",
        source="webhook.charge.refunded",
    )


def _create_payment_entry_for_sales_invoice(sales_invoice_name: str, stripe_pi_id: str, paid_amount: float, request_kind: str | None = None):
    if not stripe_pi_id:
        return

    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    lock_name = f"stripe_pi_{stripe_pi_id}"

    with _MariaDBNamedLock(lock_name, timeout=30):
        if not frappe.db.exists("Sales Invoice", sales_invoice_name):
            return

        invoice = frappe.get_doc("Sales Invoice", sales_invoice_name)
        if invoice.docstatus != 1:
            return

        if (invoice.outstanding_amount or 0) <= 0:
            return

        # Re-check inside lock
        if frappe.db.exists(
            "Payment Entry",
            {"reference_no": stripe_pi_id, "docstatus": ["!=", 2]},
        ):
            return

        alloc = min(float(paid_amount), float(invoice.outstanding_amount))
        if alloc <= 0:
            return

        pe = get_payment_entry("Sales Invoice", invoice.name)
        pe.reference_no = stripe_pi_id
        pe.reference_date = frappe.utils.nowdate()

        pe.paid_amount = alloc
        pe.received_amount = alloc
        if pe.references:
            pe.references[0].allocated_amount = alloc

        try:
            pe.insert(ignore_permissions=True)
            pe.submit()

            _send_payment_receipt_email(invoice, pe, request_kind=request_kind)

            # For Split Payment workflow: mark that at least one Stripe payment was processed.
            # This makes the next "Request Payment (Stripe)" send the remaining balance.
            if frappe.get_meta("Sales Invoice").get_field("custom_stripe_payment_processed"):
                frappe.db.set_value("Sales Invoice", invoice.name, "custom_stripe_payment_processed", 1, update_modified=False)

            frappe.db.commit()
        except DuplicateEntryError:
            frappe.db.rollback()
            return


def _get_customer_email_from_invoice(invoice):
    for fn in ("contact_email", "customer_email", "email_id"):
        v = invoice.get(fn)
        if v:
            return v
    cust = invoice.get("customer")
    if cust:
        return frappe.db.get_value("Customer", cust, "email_id")
    return None


def _send_payment_receipt_email(invoice, pe, request_kind=None):
    # Reload invoice after Payment Entry submit to avoid stale outstanding_amount
    invoice = frappe.get_doc("Sales Invoice", invoice.name)

    company_abbr = get_company_abbr_from_company(invoice.get("company")) or ""
    is_cosl = (company_abbr.upper() == "COSL")
    brand_name = "CoreOrbit" if is_cosl else "COEngine"

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
        sender=f"{brand_name} <erp@coengine.ai>",
        now=True,
        delayed=False,
        with_container=False,
        add_unsubscribe_link=0,
        reference_doctype="Payment Entry",
        reference_name=pe.name,
        attachments=[pe_pdf] if pe_pdf else None,
    )
