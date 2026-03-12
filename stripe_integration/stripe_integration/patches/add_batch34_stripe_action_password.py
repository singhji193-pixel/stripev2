import frappe


def execute():
    if not frappe.db.exists("Custom Field", {"dt": "Stripe Settings", "fieldname": "stripe_action_password"}):
        cf = frappe.new_doc("Custom Field")
        cf.dt = "Stripe Settings"
        cf.fieldname = "stripe_action_password"
        cf.label = "Stripe Action Password"
        cf.fieldtype = "Password"
        cf.insert_after = "enabled"
        cf.description = "Optional gate for Stripe payment link/refund/void actions"
        cf.insert(ignore_permissions=True)

    frappe.db.commit()
