import frappe
import stripe
from datetime import datetime

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

    stripe_sub_id = obj.get("subscription")
    if stripe_sub_id and frappe.get_meta("Subscription").get_field("stripe_subscription_id"):
        return frappe.db.get_value("Subscription", {"stripe_subscription_id": stripe_sub_id}, "name")

    return None


def _period_dates_from_invoice_object(obj: dict):
    lines = ((obj.get("lines") or {}).get("data") or [])
    if not lines:
        return None, None
    period = (lines[0] or {}).get("period") or {}
    start = period.get("start")
    end = period.get("end")
    from_date = datetime.utcfromtimestamp(start).date().isoformat() if start else None
    # Stripe line period end is exclusive-ish for recurring periods; ERPNext invoice windows are date-based.
    to_date = datetime.utcfromtimestamp(end).date().isoformat() if end else None
    return from_date, to_date


def _ensure_sales_invoice_for_subscription_payment(subscription_name: str, obj: dict, stripe_invoice_id: str | None, stripe_pi_id: str | None):
    sub = frappe.get_doc("Subscription", subscription_name)
    existing = frappe.get_all("Sales Invoice", filters={"subscription": subscription_name}, fields=["name","posting_date"], order_by="posting_date desc, modified desc", limit_page_length=1)
    if existing and stripe_invoice_id and frappe.get_meta("Sales Invoice").get_field("stripe_invoice_id"):
        # Reuse latest only if already linked to this Stripe invoice
        matched = frappe.db.get_value("Sales Invoice", {"subscription": subscription_name, "stripe_invoice_id": stripe_invoice_id}, "name")
        if matched:
            return matched

    from_date, to_date = _period_dates_from_invoice_object(obj)
    posting_date = datetime.utcfromtimestamp(obj.get("created") or 0).date().isoformat() if obj.get("created") else nowdate()
    si = sub.generate_invoice(from_date=from_date or sub.get("current_invoice_start"), to_date=to_date or sub.get("current_invoice_end"), posting_date=posting_date)
    if frappe.get_meta("Sales Invoice").get_field("stripe_invoice_id") and stripe_invoice_id:
        frappe.db.set_value("Sales Invoice", si.name, "stripe_invoice_id", stripe_invoice_id, update_modified=False)
    if frappe.get_meta("Sales Invoice").get_field("stripe_payment_intent_id") and stripe_pi_id:
        frappe.db.set_value("Sales Invoice", si.name, "stripe_payment_intent_id", stripe_pi_id, update_modified=False)
    frappe.db.commit()
    return si.name

def handle_invoice_paid(event: dict):
    obj = (event or {}).get("data", {}).get("object", {})
    stripe_invoice_id = obj.get("id")
    stripe_pi_id = obj.get("payment_intent")
    amount_paid = flt((obj.get("amount_paid") or 0) / 100.0)

    md = (obj.get("metadata") or {})
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

    alloc = min(float(amount_paid or 0), float(inv.outstanding_amount or 0))
    if alloc <= 0:
        return {"handled": True, "reason": "no_outstanding", "sales_invoice": si_name}

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

    # Send branded payment receipt email + PDF for subscription invoice.paid path as well.
    # Company-wise format selection is handled inside webhook._send_payment_receipt_email.
    try:
        from stripe_integration.stripe_integration.webhook import _send_payment_receipt_email
        _send_payment_receipt_email(inv, pe, request_kind="subscription")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Subscription invoice.paid: receipt email send failed")

    # Store both IDs on Sales Invoice for comprehensive tracking
    if frappe.get_meta("Sales Invoice").get_field("stripe_invoice_id") and stripe_invoice_id:
        frappe.db.set_value("Sales Invoice", inv.name, "stripe_invoice_id", stripe_invoice_id, update_modified=False)
    if frappe.get_meta("Sales Invoice").get_field("stripe_payment_intent_id") and stripe_pi_id:
        frappe.db.set_value("Sales Invoice", inv.name, "stripe_payment_intent_id", stripe_pi_id, update_modified=False)
    
    # Store PI on Payment Entry for refund lookup
    if frappe.get_meta("Payment Entry").get_field("stripe_payment_intent_id") and stripe_pi_id:
        frappe.db.set_value("Payment Entry", pe.name, "stripe_payment_intent_id", stripe_pi_id, update_modified=False)

    frappe.db.commit()
    return {"handled": True, "sales_invoice": inv.name, "payment_entry": pe.name, "payment_intent": stripe_pi_id}
