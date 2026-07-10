from datetime import datetime, timedelta, timezone

import frappe
import stripe

from frappe.utils import flt, getdate, nowdate

from stripe_integration.stripe_integration.utils import get_api_key, get_company_abbr_from_company
from stripe_integration.stripe_integration.stripe_fees import ensure_fee_posted


def _stripe_get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        if hasattr(obj, key):
            value = getattr(obj, key)
            return default if value is None else value
    except Exception:
        pass
    try:
        value = obj[key]
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


def _invoice_subscription_details(obj: dict) -> dict:
    parent = (obj or {}).get("parent") or {}
    return parent.get("subscription_details") or {}


def _invoice_metadata(obj: dict) -> dict:
    return (obj or {}).get("metadata") or _invoice_subscription_details(obj).get("metadata") or {}


def _invoice_subscription_id(obj: dict) -> str | None:
    return (obj or {}).get("subscription") or _invoice_subscription_details(obj).get("subscription")


def _resolve_payment_intent_from_stripe_invoice(stripe_invoice_id: str, company_abbr: str | None = None) -> str | None:
    """Fetch Payment Intent ID from Stripe Invoice when not directly available.
    
    For subscription payments, the invoice.paid event may have payment_intent=None
    because the charge was created via Stripe Invoice, not direct PaymentIntent.
    This fetches the underlying PI for proper linkage.
    """
    if not stripe_invoice_id:
        return None

    try:
        if company_abbr:
            stripe.api_key = get_api_key(company_abbr)
        
        # Basil API versions moved invoice payments out of the legacy
        # invoice.payment_intent / invoice.charge fields.
        invoice_payment_api = getattr(stripe, "InvoicePayment", None)
        if invoice_payment_api:
            invoice_payments = invoice_payment_api.list(invoice=stripe_invoice_id, limit=100)
            for invoice_payment in _stripe_get(invoice_payments, "data") or []:
                if _stripe_get(invoice_payment, "status") != "paid":
                    continue
                payment = _stripe_get(invoice_payment, "payment") or {}
                pi_id = _stripe_get(payment, "payment_intent")
                if pi_id:
                    return pi_id

        inv = stripe.Invoice.retrieve(stripe_invoice_id)
        pi_id = _stripe_get(inv, "payment_intent")
        if pi_id:
            return pi_id
        
        # Fallback: get charge and resolve PI from there
        charge_id = _stripe_get(inv, "charge") or _stripe_get(inv, "latest_charge")
        if charge_id:
            charge = stripe.Charge.retrieve(charge_id)
            return _stripe_get(charge, "payment_intent")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Resolve PI from Stripe Invoice failed")
    
    return None



def _resolve_subscription_name_from_invoice_event(obj: dict, metadata: dict) -> str | None:
    sub_name = metadata.get("docname") if metadata.get("doctype") == "Subscription" else None
    if sub_name and frappe.db.exists("Subscription", sub_name):
        return sub_name

    stripe_sub_id = _invoice_subscription_id(obj)
    if stripe_sub_id and frappe.get_meta("Subscription").get_field("stripe_subscription_id"):
        return frappe.db.get_value("Subscription", {"stripe_subscription_id": stripe_sub_id}, "name")

    return None


def _period_dates_from_invoice_object(obj: dict):
    lines = ((obj.get("lines") or {}).get("data") or [])
    if not lines:
        return None, None

    recurring_line = next(
        (
            line
            for line in lines
            if (((line or {}).get("parent") or {}).get("type") == "subscription_item_details")
        ),
        lines[0],
    )
    period = (recurring_line or {}).get("period") or {}
    start = period.get("start")
    end = period.get("end")
    from_value = datetime.fromtimestamp(start, tz=timezone.utc).date() if start else None
    to_value = datetime.fromtimestamp(end, tz=timezone.utc).date() if end else None
    if from_value and to_value and to_value > from_value:
        # Stripe's recurring period end is exclusive; ERPNext's to_date is inclusive.
        to_value -= timedelta(days=1)
    from_date = from_value.isoformat() if from_value else None
    to_date = to_value.isoformat() if to_value else None
    return from_date, to_date


def _link_sales_invoice_to_stripe(si_name: str, stripe_invoice_id: str | None, stripe_pi_id: str | None):
    if frappe.get_meta("Sales Invoice").get_field("stripe_invoice_id") and stripe_invoice_id:
        frappe.db.set_value("Sales Invoice", si_name, "stripe_invoice_id", stripe_invoice_id, update_modified=False)
    if frappe.get_meta("Sales Invoice").get_field("stripe_payment_intent_id") and stripe_pi_id:
        frappe.db.set_value("Sales Invoice", si_name, "stripe_payment_intent_id", stripe_pi_id, update_modified=False)


def _ensure_sales_invoice_for_subscription_payment(subscription_name: str, obj: dict, stripe_invoice_id: str | None, stripe_pi_id: str | None):
    sub = frappe.get_doc("Subscription", subscription_name)
    if stripe_invoice_id and frappe.get_meta("Sales Invoice").get_field("stripe_invoice_id"):
        matched = frappe.db.get_value("Sales Invoice", {"subscription": subscription_name, "stripe_invoice_id": stripe_invoice_id}, "name")
        if matched:
            return matched

    from_date, to_date = _period_dates_from_invoice_object(obj)
    if from_date and to_date:
        matched = frappe.db.get_value(
            "Sales Invoice",
            {
                "subscription": subscription_name,
                "from_date": from_date,
                "to_date": to_date,
                "docstatus": ["!=", 2],
            },
            "name",
            order_by="posting_date desc, modified desc",
        )
        if matched:
            _link_sales_invoice_to_stripe(matched, stripe_invoice_id, stripe_pi_id)
            frappe.db.commit()
            return matched

    posting_date = (
        datetime.fromtimestamp(obj.get("created"), tz=timezone.utc).date().isoformat()
        if obj.get("created")
        else nowdate()
    )
    si = sub.generate_invoice(from_date=from_date or sub.get("current_invoice_start"), to_date=to_date or sub.get("current_invoice_end"), posting_date=posting_date)
    _link_sales_invoice_to_stripe(si.name, stripe_invoice_id, stripe_pi_id)
    frappe.db.commit()
    return si.name

def handle_invoice_paid(event: dict):
    obj = (event or {}).get("data", {}).get("object", {})
    stripe_invoice_id = obj.get("id")
    stripe_pi_id = obj.get("payment_intent")
    amount_paid = flt((obj.get("amount_paid") or 0) / 100.0)

    if amount_paid <= 0:
        return {"handled": True, "reason": "zero_amount_invoice", "stripe_invoice_id": stripe_invoice_id}

    md = _invoice_metadata(obj)
    si_name = md.get("docname") if md.get("doctype") == "Sales Invoice" else None

    if not si_name and stripe_invoice_id and frappe.get_meta("Sales Invoice").get_field("stripe_invoice_id"):
        si_name = frappe.db.get_value("Sales Invoice", {"stripe_invoice_id": stripe_invoice_id}, "name")

    if not si_name:
        subscription_name = _resolve_subscription_name_from_invoice_event(obj, md)
        if subscription_name:
            try:
                si_name = _ensure_sales_invoice_for_subscription_payment(subscription_name, obj, stripe_invoice_id, stripe_pi_id)
            except Exception:
                frappe.log_error(frappe.get_traceback(), f"Subscription invoice.paid: sales invoice generation failed for {subscription_name}")
                return {"handled": False, "reason": "sales_invoice_generation_failed", "subscription": subscription_name, "stripe_invoice_id": stripe_invoice_id}

    if not si_name:
        return {"handled": False, "reason": "sales_invoice_not_found", "stripe_invoice_id": stripe_invoice_id}

    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    inv = frappe.get_doc("Sales Invoice", si_name)
    
    # Resolve company_abbr for Stripe API calls
    company_abbr = get_company_abbr_from_company(inv.company) if inv.company else None

    # CRITICAL FIX: If PI is missing, fetch it from Stripe Invoice
    if not stripe_pi_id and stripe_invoice_id:
        stripe_pi_id = _resolve_payment_intent_from_stripe_invoice(stripe_invoice_id, company_abbr)

    # Dedup check: use PI if available, otherwise invoice ID
    dedup_ref = stripe_pi_id or stripe_invoice_id
    if dedup_ref and frappe.db.exists("Payment Entry", {"reference_no": dedup_ref, "docstatus": ["!=", 2]}):
        return {"handled": True, "dedup": True, "sales_invoice": si_name}

    if inv.docstatus != 1:
        return {"handled": False, "reason": "invoice_not_submitted", "sales_invoice": si_name}

    event_currency = (obj.get("currency") or "").upper()
    invoice_currency = (inv.currency or "").upper()
    if event_currency and invoice_currency and event_currency != invoice_currency:
        return {
            "handled": False,
            "reason": "currency_mismatch",
            "sales_invoice": si_name,
            "stripe_currency": event_currency,
            "invoice_currency": invoice_currency,
        }

    outstanding = float(inv.outstanding_amount or 0)
    if abs(float(amount_paid) - outstanding) > 0.01:
        return {
            "handled": False,
            "reason": "amount_mismatch",
            "sales_invoice": si_name,
            "stripe_amount_paid": float(amount_paid),
            "invoice_outstanding": outstanding,
        }

    alloc = float(amount_paid)

    pe = get_payment_entry("Sales Invoice", inv.name)
    # CRITICAL: Always prefer PI for reference_no (enables refund lookup)
    pe.reference_no = stripe_pi_id or stripe_invoice_id
    pe.reference_date = frappe.utils.nowdate()
    pe.paid_amount = alloc
    pe.received_amount = alloc
    if pe.references:
        pe.references[0].allocated_amount = alloc
    pe.insert(ignore_permissions=True)
    pe.submit()

    # Post Stripe processing fee (charge-level) to fee account (e.g. 5085) with idempotency.
    try:
        if company_abbr and stripe_pi_id:
            ensure_fee_posted(
                company_abbr=company_abbr,
                stripe_payment_intent_id=stripe_pi_id,
                remark_ctx=f"invoice.paid {inv.name}",
                enqueue_retry=True,
            )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Subscription invoice.paid: fee JE posting failed")

    # Store both IDs on Sales Invoice for comprehensive tracking
    if frappe.get_meta("Sales Invoice").get_field("stripe_invoice_id") and stripe_invoice_id:
        frappe.db.set_value("Sales Invoice", inv.name, "stripe_invoice_id", stripe_invoice_id, update_modified=False)
    if frappe.get_meta("Sales Invoice").get_field("stripe_payment_intent_id") and stripe_pi_id:
        frappe.db.set_value("Sales Invoice", inv.name, "stripe_payment_intent_id", stripe_pi_id, update_modified=False)
    
    # Store PI on Payment Entry for refund lookup
    if frappe.get_meta("Payment Entry").get_field("stripe_payment_intent_id") and stripe_pi_id:
        frappe.db.set_value("Payment Entry", pe.name, "stripe_payment_intent_id", stripe_pi_id, update_modified=False)

    frappe.db.commit()

    # Email only after the accounting entry is durable.
    try:
        from stripe_integration.stripe_integration.webhook import _send_payment_receipt_email
        _send_payment_receipt_email(inv, pe, request_kind="subscription")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Subscription invoice.paid: receipt email send failed")

    return {"handled": True, "sales_invoice": inv.name, "payment_entry": pe.name, "payment_intent": stripe_pi_id}
