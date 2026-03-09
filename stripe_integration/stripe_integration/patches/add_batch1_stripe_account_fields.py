import frappe


def execute():
    fields = [
        dict(fieldname="stripe_account_id", label="Stripe Account ID", fieldtype="Data", insert_after="company_abbr"),
        dict(fieldname="webhook_tolerance_seconds", label="Webhook Tolerance Seconds", fieldtype="Int", default="300", insert_after="webhook_secret"),
        dict(fieldname="bank_account", label="Bank Account", fieldtype="Link", options="Account", insert_after="webhook_tolerance_seconds"),
        dict(fieldname="stripe_clearing_account", label="Stripe Clearing Account", fieldtype="Link", options="Account", insert_after="bank_account"),
        dict(fieldname="stripe_fee_account", label="Stripe Fee Account", fieldtype="Link", options="Account", insert_after="stripe_clearing_account"),
    ]

    for f in fields:
        if not frappe.db.exists("Custom Field", {"dt": "Stripe Account", "fieldname": f["fieldname"]}):
            cf = frappe.new_doc("Custom Field")
            cf.dt = "Stripe Account"
            cf.fieldname = f["fieldname"]
            cf.label = f["label"]
            cf.fieldtype = f["fieldtype"]
            cf.insert_after = f["insert_after"]
            if f.get("options"):
                cf.options = f["options"]
            if f.get("default") is not None:
                cf.default = f["default"]
            cf.insert(ignore_permissions=True)

    frappe.db.commit()
