import frappe
from frappe.utils import flt

from stripe_integration.stripe_integration.accounting import (
    MariaDBNamedLock,
    route_payment_entry_to_stripe_clearing,
    validate_stripe_currency,
)
from stripe_integration.stripe_integration.utils import get_company_abbr_from_company


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


def _find_existing_refund_payment_entry(stripe_refund_id: str):
    if not stripe_refund_id:
        return None

    if frappe.get_meta("Payment Entry").get_field("stripe_refund_id"):
        name = frappe.db.get_value(
            "Payment Entry",
            {"stripe_refund_id": stripe_refund_id, "docstatus": ["!=", 2]},
            "name",
        )
        if name:
            return name
    return frappe.db.get_value(
        "Payment Entry",
        {"reference_no": stripe_refund_id, "docstatus": ["!=", 2]},
        "name",
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
        company_abbr = get_company_abbr_from_company(pe.get("company"))
        mapping = route_payment_entry_to_stripe_clearing(pe, company_abbr)
        credit_note_currency = frappe.db.get_value("Sales Invoice", credit_note_name, "currency")
        validate_stripe_currency(
            mapping.get("currency"),
            credit_note_currency,
            f"Stripe Clearing for Credit Note {credit_note_name}",
        )

        reference_no = stripe_refund_id or stripe_payment_intent_id
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

        if pe.meta.get_field("stripe_payment_intent_id") and stripe_payment_intent_id:
            pe.stripe_payment_intent_id = stripe_payment_intent_id
        if pe.meta.get_field("stripe_refund_id") and stripe_refund_id:
            pe.stripe_refund_id = stripe_refund_id

        pe.insert(ignore_permissions=True)
        pe.submit()
        return pe.name
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Stripe refund: credit note allocation failed")
        return None


def apply_refund_to_erp(
    stripe_payment_intent_id: str,
    stripe_refund_id: str,
    refund_amount: float,
    currency: str,
    source: str = "webhook.refund",
    invoice_name: str | None = None,
):
    """Link Stripe refund into ERP records.

    Every successful Stripe refund requires a submitted Credit Note and creates
    an outgoing Payment Entry against that Credit Note. The original receipt is
    retained as the historical record of the money received.

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

    with MariaDBNamedLock(f"stripe-refund-{stripe_refund_id}", timeout=30):
        existing_refund_pe = _find_existing_refund_payment_entry(stripe_refund_id)
        if existing_refund_pe:
            return {
                "handled": True,
                "dedup": True,
                "refund_payment_entry": existing_refund_pe,
                "stripe_refund_id": stripe_refund_id,
            }

        pe = _find_matching_payment_entry(stripe_payment_intent_id)
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

        invoice_currency = frappe.db.get_value("Sales Invoice", resolved_invoice_name, "currency")
        validate_stripe_currency(currency, invoice_currency, f"refund {stripe_refund_id}")

        credit_note_name = _find_submitted_credit_note(resolved_invoice_name)
        if not credit_note_name:
            _comment_on_invoice(
                resolved_invoice_name,
                (
                    f"Stripe refund {stripe_refund_id} for {currency} {refund_amount:.2f} requires "
                    "a submitted Credit Note before ERP accounting can be completed."
                ),
            )
            return {
                "handled": False,
                "reason": "credit_note_required",
                "payment_entry": pe.name,
                "invoice": resolved_invoice_name,
                "refund_amount": refund_amount,
            }

        credit_outstanding = abs(
            flt(frappe.db.get_value("Sales Invoice", credit_note_name, "outstanding_amount") or 0)
        )
        if refund_amount > credit_outstanding + 0.01:
            return {
                "handled": False,
                "reason": "refund_exceeds_credit_note_outstanding",
                "credit_note": credit_note_name,
                "refund_amount": refund_amount,
                "credit_note_outstanding": credit_outstanding,
            }

        refund_pe_name = _create_refund_payment_entry(
            credit_note_name=credit_note_name,
            stripe_payment_intent_id=stripe_payment_intent_id,
            stripe_refund_id=stripe_refund_id,
            refund_amount=refund_amount,
        )
        if not refund_pe_name:
            return {
                "handled": False,
                "reason": "refund_payment_entry_failed",
                "credit_note": credit_note_name,
            }

        _comment_on_invoice(
            resolved_invoice_name,
            (
                f"Stripe refund linked ({currency} {refund_amount:.2f}, refund {stripe_refund_id}, "
                f"source {source}) for PI {stripe_payment_intent_id}. Created refund Payment Entry "
                f"{refund_pe_name} against Credit Note {credit_note_name}."
            ),
        )
        frappe.db.commit()

        return {
            "handled": True,
            "mode": "refund_credit_note_allocated",
            "payment_entry": pe.name,
            "refund_payment_entry": refund_pe_name,
            "invoice": resolved_invoice_name,
            "credit_note": credit_note_name,
            "refund_amount": refund_amount,
            "paid_amount": paid_amount,
        }
