import frappe


def _add_cf(dt, fieldname, label, insert_after):
    if frappe.db.exists("Custom Field", {"dt": dt, "fieldname": fieldname}):
        return
    cf = frappe.new_doc("Custom Field")
    cf.dt = dt
    cf.fieldname = fieldname
    cf.label = label
    cf.fieldtype = "Data"
    cf.insert_after = insert_after
    cf.insert(ignore_permissions=True)


def execute():
    _add_cf("Sales Invoice", "stripe_invoice_id", "Stripe Invoice ID", "customer")
    _add_cf("Sales Invoice", "stripe_subscription_id", "Stripe Subscription ID", "stripe_invoice_id")
    _add_cf("Sales Invoice", "stripe_payment_intent_id", "Stripe Payment Intent ID", "stripe_subscription_id")
    _add_cf("Payment Entry", "stripe_payment_intent_id", "Stripe Payment Intent ID", "reference_no")
    _add_cf("Subscription", "stripe_subscription_id", "Stripe Subscription ID", "party")
    frappe.db.commit()
