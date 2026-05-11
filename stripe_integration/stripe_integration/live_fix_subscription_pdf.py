import frappe
from stripe_integration.stripe_integration.subscription_sync import request_subscription_payment_method

@frappe.whitelist()
def run():
    subname = 'ACC-SUB-2026-00014'
    sub = frappe.get_doc('Subscription', subname)

    sis = frappe.get_all(
        'Sales Invoice',
        filters={'subscription': subname, 'docstatus': ['!=', 2]},
        fields=['name', 'docstatus', 'status', 'outstanding_amount'],
        order_by='creation desc',
        limit=1,
    )

    created = None
    if not sis:
        inv = sub.generate_invoice()
        if inv and inv.docstatus == 0:
            inv.submit()
        created = inv.name if inv else None

    out = request_subscription_payment_method(subname, send_email=1)
    frappe.db.commit()
    return {'created_invoice': created, 'result': out}