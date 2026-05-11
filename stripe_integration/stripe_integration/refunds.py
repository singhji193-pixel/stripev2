import frappe
from frappe.utils import flt


def _find_matching_payment_entry(stripe_ref_id: str):
    """Find Payment Entry by PI, invoice ID, or charge ID.
    
    Supports multiple Stripe reference types for maximum linkage success:
    - Payment Intent ID (pi_xxx)
    - Stripe Invoice ID (in_xxx) 
    - Charge ID (ch_xxx)
    """
    if not stripe_ref_id:
        return None

    # Path 1: Direct field lookup (custom field stripe_payment_intent_id)
    pe_name = frappe.db.get_value(
        "Payment Entry",
        {
            "docstatus": 1,
            "stripe_payment_intent_id": stripe_ref_id,
        },
        "name",
        order_by="modified desc",
    )
    if pe_name:
        return frappe.get_doc("Payment Entry", pe_name)

    # Path 2: reference_no field (stores PI or invoice ID)
    pe_name = frappe.db.get_value(
        "Payment Entry",
        {
            "docstatus": 1,
            "reference_no": stripe_ref_id,
        },
        "name",
        order_by="modified desc",
    )
    if pe_name:
        return frappe.get_doc("Payment Entry", pe_name)

    return None


def _find_payment_entry_by_invoice(invoice_name: str):
    """Find the latest submitted Payment Entry allocated to a Sales Invoice."""
    if not invoice_name:
        return None

    refs = frappe.get_all(
        "Payment Entry Reference",
        filters={
            "reference_doctype": "Sales Invoice",
            "reference_name": invoice_name,
            "parenttype": "Payment Entry",
            "docstatus": 1,
        },
        fields=["parent"],
        order_by="modified desc",
        limit_page_length=1,
    )
    if refs:
        return frappe.get_doc("Payment Entry", refs[0].get("parent"))
    return None


def _find_submitted_credit_note(invoice_name: str):
    if not invoice_name:
        return None

    return frappe.db.get_value(
        "Sales Invoice",
        {
            "docstatus": 1,
            "is_return": 1,
            "return_against": invoice_name,
        },
        "name",
        order_by="modified desc",
    )


def _comment_on_invoice(invoice_name: str, text: str):
    if not invoice_name:
        return
    try:
        si = frappe.get_doc("Sales Invoice", invoice_name)
        si.add_comment("Comment", text)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Stripe refund: unable to add Sales Invoice comment")


def _create_refund_payment_entry(credit_note_name: str, stripe_payment_intent_id: str, stripe_refund_id: str, refund_amount: float):
    """Create a refund-side Payment Entry allocated to submitted Credit Note.

    Keeps automation ERP-safe: only create if credit note exists and amount is positive.
    """

    if not credit_note_name or flt(refund_amount) <= 0:
        return None

    try:
        from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

        pe = get_payment_entry("Sales Invoice", credit_note_name)

        reference_no = stripe_payment_intent_id or stripe_refund_id
        if reference_no:
            pe.reference_no = reference_no

        if getattr(frappe.utils, "nowdate", None):
            pe.reference_date = frappe.utils.nowdate()

        amount = flt(refund_amount)
        if hasattr(pe, "paid_amount"):
            pe.paid_amount = amount
        if hasattr(pe, "received_amount"):
            pe.received_amount = amount

        if getattr(pe, "references", None):
            allocated = False
            for ref in pe.references:
                if (
                    getattr(ref, "reference_doctype", None) == "Sales Invoice"
                    and getattr(ref, "reference_name", None) == credit_note_name
                ):
                    ref.allocated_amount = amount
                    allocated = True
                    break
            if not allocated and pe.references:
                pe.references[0].allocated_amount = amount

        pe.insert(ignore_permissions=True)
        pe.submit()
        return pe.name
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Stripe partial refund: credit note allocation failed")
        return None


def apply_refund_to_erp(
    stripe_payment_intent_id: str,
    stripe_refund_id: str,
    refund_amount: float,
    currency: str,
    source: str = "webhook.refund",
    invoice_name: str = None,
):
    """Link Stripe refund into ERP records.

    Behavior:
    - Full refund => cancel matching submitted Payment Entry
    - Partial refund => try credit-note allocation via refund Payment Entry
      and fallback to manual adjustment comment when allocation is not possible.
    
    Args:
        stripe_payment_intent_id: PI, invoice ID, or charge ID to match PE
        stripe_refund_id: Stripe refund object ID
        refund_amount: Amount refunded
        currency: Currency code
        source: Source identifier for logging
        invoice_name: Optional Sales Invoice name for fallback PE lookup
    """

    refund_amount = flt(refund_amount)
    if refund_amount <= 0:
        return {"handled": False, "reason": "non_positive_refund_amount"}

    # Try multiple paths to find the Payment Entry
    pe = _find_matching_payment_entry(stripe_payment_intent_id)
    
    # Fallback: find PE by invoice if direct lookup failed
    if not pe and invoice_name:
        pe = _find_payment_entry_by_invoice(invoice_name)
    
    if not pe:
        return {
            "handled": False,
            "reason": "payment_entry_not_found",
            "stripe_payment_intent_id": stripe_payment_intent_id,
            "invoice_name": invoice_name,
        }

    paid_amount = flt(pe.paid_amount or pe.received_amount or 0)
    resolved_invoice_name = invoice_name
    if pe.references:
        for ref in pe.references:
            if ref.reference_doctype == "Sales Invoice" and ref.reference_name:
                resolved_invoice_name = ref.reference_name
                break

    epsilon = 0.01
    if refund_amount + epsilon < paid_amount:
        credit_note_name = _find_submitted_credit_note(resolved_invoice_name)
        if credit_note_name:
            refund_pe_name = _create_refund_payment_entry(
                credit_note_name=credit_note_name,
                stripe_payment_intent_id=stripe_payment_intent_id,
                stripe_refund_id=stripe_refund_id,
                refund_amount=refund_amount,
            )
            if refund_pe_name:
                _comment_on_invoice(
                    resolved_invoice_name,
                    (
                        f"Stripe partial refund auto-linked ({currency} {refund_amount:.2f}, "
                        f"refund {stripe_refund_id}, source {source}) for PI {stripe_payment_intent_id}. "
                        f"Created refund Payment Entry {refund_pe_name} allocated to Credit Note {credit_note_name}."
                    ),
                )
                frappe.db.commit()
                return {
                    "handled": True,
                    "mode": "partial_refund_credit_note_allocated",
                    "payment_entry": pe.name,
                    "refund_payment_entry": refund_pe_name,
                    "invoice": resolved_invoice_name,
                    "credit_note": credit_note_name,
                    "refund_amount": refund_amount,
                    "paid_amount": paid_amount,
                }

        _comment_on_invoice(
            resolved_invoice_name,
            (
                f"Stripe partial refund received ({currency} {refund_amount:.2f}, "
                f"refund {stripe_refund_id}, source {source}) for PI {stripe_payment_intent_id}. "
                "Payment Entry was not auto-adjusted; create a manual adjustment for the partial refund."
            ),
        )
        return {
            "handled": True,
            "mode": "partial_refund_manual_adjustment_required",
            "payment_entry": pe.name,
            "invoice": resolved_invoice_name,
            "credit_note": credit_note_name,
            "refund_amount": refund_amount,
            "paid_amount": paid_amount,
        }

    if pe.docstatus == 1:
        pe.cancel()

    _comment_on_invoice(
        resolved_invoice_name,
        (
            f"Stripe refund applied: {currency} {refund_amount:.2f} (refund {stripe_refund_id}, "
            f"source {source}) for PI {stripe_payment_intent_id}. "
            f"Linked Payment Entry {pe.name} was cancelled."
        ),
    )

    frappe.db.commit()

    return {
        "handled": True,
        "mode": "full_refund_cancel_payment_entry",
        "payment_entry": pe.name,
        "invoice": resolved_invoice_name,
        "refund_amount": refund_amount,
        "paid_amount": paid_amount,
    }
