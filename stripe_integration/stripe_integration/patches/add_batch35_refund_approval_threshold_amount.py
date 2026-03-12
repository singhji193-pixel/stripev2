import frappe


def execute():
    if not frappe.db.exists("Custom Field", {"dt": "Stripe Settings", "fieldname": "refund_approval_threshold_amount"}):
        cf = frappe.new_doc("Custom Field")
        cf.dt = "Stripe Settings"
        cf.fieldname = "refund_approval_threshold_amount"
        cf.label = "Refund Approval Threshold Amount"
        cf.fieldtype = "Currency"
        cf.insert_after = "stripe_action_password"
        cf.description = "Optional: refunds at or above this amount require System Manager approval"
        cf.default = "0"
        cf.insert(ignore_permissions=True)

    frappe.db.commit()
