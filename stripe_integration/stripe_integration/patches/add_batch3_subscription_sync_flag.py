import frappe


def execute():
    if not frappe.db.exists("Custom Field", {"dt": "Stripe Settings", "fieldname": "enable_subscription_state_sync"}):
        cf = frappe.new_doc("Custom Field")
        cf.dt = "Stripe Settings"
        cf.fieldname = "enable_subscription_state_sync"
        cf.label = "Enable Subscription State Sync"
        cf.fieldtype = "Check"
        cf.insert_after = "enabled"
        cf.default = "0"
        cf.insert(ignore_permissions=True)
    frappe.db.commit()
