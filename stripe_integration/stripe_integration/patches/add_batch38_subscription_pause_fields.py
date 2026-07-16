import frappe

FIELDS = (
	{
		"fieldname": "stripe_erpnext_pause_active",
		"label": "ERPNext Billing Paused",
		"fieldtype": "Check",
		"default": "0",
		"insert_after": "stripe_paused",
		"description": "Stops ERPNext invoice generation while the coordinated Stripe billing hold is active.",
	},
	{
		"fieldname": "stripe_pause_start",
		"label": "Pause Starts On",
		"fieldtype": "Date",
		"insert_after": "stripe_erpnext_pause_active",
		"description": "First unbilled period included in the coordinated billing hold.",
	},
	{
		"fieldname": "stripe_resume_on",
		"label": "Billing Resumes On",
		"fieldtype": "Date",
		"insert_after": "stripe_pause_start",
		"description": "Aligned billing-period boundary when Stripe and ERPNext billing resume.",
	},
	{
		"fieldname": "stripe_pause_state",
		"label": "Stripe Pause State",
		"fieldtype": "Select",
		"options": "\nPausing\nPaused\nResuming\nCancelling",
		"insert_after": "stripe_resume_on",
		"description": "Durable state for coordinated Stripe and ERPNext pause operations.",
	},
	{
		"fieldname": "stripe_pause_operation_id",
		"label": "Stripe Pause Operation ID",
		"fieldtype": "Data",
		"insert_after": "stripe_pause_state",
		"description": "Stable retry identity for the current pause or resume operation.",
	},
	{
		"fieldname": "stripe_pending_resume_on",
		"label": "Pending Resume On",
		"fieldtype": "Date",
		"insert_after": "stripe_pause_operation_id",
		"description": "Resume boundary persisted before Stripe is mutated.",
	},
	{
		"fieldname": "stripe_pause_cycles",
		"label": "Paused Billing Cycles",
		"fieldtype": "Int",
		"default": "0",
		"insert_after": "stripe_pending_resume_on",
		"description": "Number of complete billing cycles excluded from the fixed service term.",
	},
	{
		"fieldname": "stripe_pause_cadence_snapshot",
		"label": "Stripe Pause Cadence Snapshot",
		"fieldtype": "Small Text",
		"insert_after": "stripe_pause_cycles",
		"description": "Immutable Stripe billing anchor and interval used by the active coordinated pause.",
	},
	{
		"fieldname": "stripe_pause_start_at",
		"label": "Stripe Pause Anchor (UTC)",
		"fieldtype": "Data",
		"insert_after": "stripe_pause_cadence_snapshot",
		"description": "Canonical Stripe UTC timestamp for the first paused billing boundary.",
	},
	{
		"fieldname": "stripe_resume_at",
		"label": "Stripe Resume Anchor (UTC)",
		"fieldtype": "Data",
		"insert_after": "stripe_pause_start_at",
		"description": "Canonical Stripe UTC timestamp for the planned resume boundary.",
	},
	{
		"fieldname": "stripe_pending_resume_at",
		"label": "Pending Stripe Resume Anchor (UTC)",
		"fieldtype": "Data",
		"insert_after": "stripe_resume_at",
		"description": "Durable UTC timestamp for a pending early-resume operation.",
	},
	{
		"fieldname": "stripe_resume_cancel_before_start",
		"label": "Resume Cancels Pending Pause",
		"fieldtype": "Check",
		"default": "0",
		"insert_after": "stripe_pending_resume_at",
		"description": "Distinguishes cancelling a future pause from a zero-cycle boundary resume.",
	},
	{
		"fieldname": "stripe_operation_attempt",
		"label": "Stripe Operation Attempt",
		"fieldtype": "Int",
		"default": "0",
		"insert_after": "stripe_resume_cancel_before_start",
		"description": "Monotonic retry generation used for safe Stripe idempotency keys.",
	},
	{
		"fieldname": "stripe_pause_last_reconciled_at",
		"label": "Stripe Pause Last Reconciled At",
		"fieldtype": "Datetime",
		"insert_after": "stripe_operation_attempt",
		"description": "Last scheduler attempt for fair coordinated-pause reconciliation.",
	},
)


def execute():
	for definition in FIELDS:
		existing = frappe.db.exists(
			"Custom Field",
			{"dt": "Subscription", "fieldname": definition["fieldname"]},
		)
		if existing:
			if definition["fieldname"] == "stripe_pause_state":
				frappe.db.set_value(
					"Custom Field",
					existing,
					"options",
					definition["options"],
					update_modified=False,
				)
			continue

		field = frappe.new_doc("Custom Field")
		field.dt = "Subscription"
		field.update(definition)
		field.read_only = 1
		field.no_copy = 1
		field.allow_on_submit = 1
		field.print_hide = 1
		field.insert(ignore_permissions=True)
	frappe.db.commit()
