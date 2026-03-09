import frappe


def execute():
    if not frappe.db.exists('Custom Field', {'dt': 'Subscription', 'fieldname': 'stripe_sync_action'}):
        cf = frappe.new_doc('Custom Field')
        cf.dt = 'Subscription'
        cf.fieldname = 'stripe_sync_action'
        cf.label = 'Stripe Sync Action'
        cf.fieldtype = 'Select'
        cf.options = '\npause\nresume\nplan_change'
        cf.insert_after = 'stripe_subscription_id'
        cf.insert(ignore_permissions=True)
    frappe.db.commit()
