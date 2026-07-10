import frappe
from stripe_integration.stripe_integration.api import refund_payment_stripe


def ensure_credit_note(invoice_name: str):
    existing = frappe.get_all(
        'Sales Invoice',
        filters={'is_return': 1, 'return_against': invoice_name, 'docstatus': 1},
        fields=['name'],
        limit=1,
    )
    if existing:
        return existing[0]['name']

    si = frappe.get_doc('Sales Invoice', invoice_name)
    cn = frappe.copy_doc(si)
    cn.is_return = 1
    cn.return_against = invoice_name
    cn.posting_date = frappe.utils.nowdate()
    cn.due_date = frappe.utils.nowdate()
    for it in cn.items:
        if (it.qty or 0) > 0:
            it.qty = -1 * it.qty
    cn.insert(ignore_permissions=True)
    cn.submit()
    return cn.name


def run(gate_password: str):
    if not gate_password:
        frappe.throw("gate_password is required")

    invoices = [
        'ACC-SINV-2026-00177',  # COE
        'ACC-SINV-2026-00183',  # COSL
    ]
    results = []

    for inv in invoices:
        cn = ensure_credit_note(inv)
        res = refund_payment_stripe(
            invoice_name=inv,
            gate_password=gate_password,
            reason="requested_by_customer",
        )
        results.append({'invoice': inv, 'credit_note': cn, 'refund_result': res})

    frappe.db.commit()
    return results
