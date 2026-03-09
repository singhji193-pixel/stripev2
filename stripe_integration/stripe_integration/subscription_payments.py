import frappe
from frappe.utils import flt


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
        return {"handled": False, "reason": "sales_invoice_not_found", "stripe_invoice_id": stripe_invoice_id}

    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    if stripe_pi_id and frappe.db.exists("Payment Entry", {"reference_no": stripe_pi_id, "docstatus": ["!=", 2]}):
        return {"handled": True, "dedup": True, "sales_invoice": si_name}

    inv = frappe.get_doc("Sales Invoice", si_name)
    if inv.docstatus != 1:
        return {"handled": False, "reason": "invoice_not_submitted", "sales_invoice": si_name}

    alloc = min(float(amount_paid or 0), float(inv.outstanding_amount or 0))
    if alloc <= 0:
        return {"handled": True, "reason": "no_outstanding", "sales_invoice": si_name}

    pe = get_payment_entry("Sales Invoice", inv.name)
    pe.reference_no = stripe_pi_id or stripe_invoice_id
    pe.reference_date = frappe.utils.nowdate()
    pe.paid_amount = alloc
    pe.received_amount = alloc
    if pe.references:
        pe.references[0].allocated_amount = alloc
    pe.insert(ignore_permissions=True)
    pe.submit()

    if frappe.get_meta("Sales Invoice").get_field("stripe_invoice_id") and stripe_invoice_id:
        frappe.db.set_value("Sales Invoice", inv.name, "stripe_invoice_id", stripe_invoice_id, update_modified=False)
    if frappe.get_meta("Sales Invoice").get_field("stripe_payment_intent_id") and stripe_pi_id:
        frappe.db.set_value("Sales Invoice", inv.name, "stripe_payment_intent_id", stripe_pi_id, update_modified=False)
    if frappe.get_meta("Payment Entry").get_field("stripe_payment_intent_id") and stripe_pi_id:
        frappe.db.set_value("Payment Entry", pe.name, "stripe_payment_intent_id", stripe_pi_id, update_modified=False)

    frappe.db.commit()
    return {"handled": True, "sales_invoice": inv.name, "payment_entry": pe.name}
