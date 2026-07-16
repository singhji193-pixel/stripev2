import frappe


def execute():
	if not frappe.db.exists(
		"Custom Field",
		{"dt": "Subscription", "fieldname": "custom_do_not_generate_invoices"},
	):
		field = frappe.new_doc("Custom Field")
		field.dt = "Subscription"
		field.fieldname = "custom_do_not_generate_invoices"
		field.label = "Do Not Generate Invoices"
		field.fieldtype = "Check"
		field.default = "0"
		field.insert_after = "generate_new_invoices_past_due_date"
		field.description = (
			"Prevents automatic and manual invoice generation. Use only when the service "
			"is fully included in another invoice."
		)
		field.read_only = 1
		field.no_copy = 1
		field.allow_on_submit = 1
		field.in_list_view = 1
		field.print_hide = 1
		field.insert(ignore_permissions=True)
	frappe.db.commit()
