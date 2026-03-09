import frappe


def execute():
    if not frappe.db.exists("Custom Field", {"dt": "Subscription", "fieldname": "stripe_paused"}):
        cf = frappe.new_doc("Custom Field")
        cf.dt = "Subscription"
        cf.fieldname = "stripe_paused"
        cf.label = "Stripe Paused"
        cf.fieldtype = "Check"
        cf.default = "0"
        cf.read_only = 1
        cf.no_copy = 1
        cf.insert_after = "stripe_status"
        cf.insert(ignore_permissions=True)
    frappe.db.commit()
