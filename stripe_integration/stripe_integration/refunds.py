import frappe
from frappe.utils import flt


def _find_matching_payment_entry(stripe_payment_intent_id: str):
    if not stripe_payment_intent_id:
        return None

    pe_name = frappe.db.get_value(
        "Payment Entry",
        {
            "docstatus": 1,
            "stripe_payment_intent_id": stripe_payment_intent_id,
        },
        "name",
        order_by="modified desc",
    )
    if pe_name:
        return frappe.get_doc("Payment Entry", pe_name)

    pe_name = frappe.db.get_value(
        "Payment Entry",
        {
            "docstatus": 1,
            "reference_no": stripe_payment_intent_id,
        },
        "name",
        order_by="modified desc",
    )
    if pe_name:
        return frappe.get_doc("Payment Entry", pe_name)

    return None


def _comment_on_invoice(invoice_name: str, text: str):
    if not invoice_name:
        return
    try:
        si = frappe.get_doc("Sales Invoice", invoice_name)
        si.add_comment("Comment", text)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Stripe refund: unable to add Sales Invoice comment")


def apply_refund_to_erp(
    stripe_payment_intent_id: str,
    stripe_refund_id: str,
    refund_amount: float,
    currency: str,
    source: str = "webhook.refund",
):
    """Link Stripe refund into ERP records.

    Current behavior (intentionally small/safe scope):
    - Full refund => cancel matching submitted Payment Entry
    - Partial refund => record comment + explicit manual action hint
    """

    refund_amount = flt(refund_amount)
    if refund_amount <= 0:
        return {"handled": False, "reason": "non_positive_refund_amount"}

    pe = _find_matching_payment_entry(stripe_payment_intent_id)
    if not pe:
        return {
            "handled": False,
            "reason": "payment_entry_not_found",
            "stripe_payment_intent_id": stripe_payment_intent_id,
        }

    paid_amount = flt(pe.paid_amount or pe.received_amount or 0)
    invoice_name = None
    if pe.references:
        for ref in pe.references:
            if ref.reference_doctype == "Sales Invoice" and ref.reference_name:
                invoice_name = ref.reference_name
                break

    epsilon = 0.01
    if refund_amount + epsilon < paid_amount:
        _comment_on_invoice(
            invoice_name,
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
            "invoice": invoice_name,
            "refund_amount": refund_amount,
            "paid_amount": paid_amount,
        }

    if pe.docstatus == 1:
        pe.cancel()

    _comment_on_invoice(
        invoice_name,
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
        "invoice": invoice_name,
        "refund_amount": refund_amount,
        "paid_amount": paid_amount,
    }
