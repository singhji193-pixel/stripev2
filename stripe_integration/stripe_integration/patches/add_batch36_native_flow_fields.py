import frappe


def _add_custom_field(dt: str, fieldname: str, label: str, fieldtype: str, insert_after: str, **kwargs):
    if frappe.db.exists("Custom Field", {"dt": dt, "fieldname": fieldname}):
        return

    field = frappe.new_doc("Custom Field")
    field.dt = dt
    field.fieldname = fieldname
    field.label = label
    field.fieldtype = fieldtype
    field.insert_after = insert_after
    for key, value in kwargs.items():
        setattr(field, key, value)
    field.insert(ignore_permissions=True)


def execute():
    _add_custom_field(
        "Stripe Settings",
        "enable_payout_sync",
        "Enable Payout Sync",
        "Check",
        "enable_subscription_state_sync",
        default="0",
    )
    _add_custom_field(
        "Payment Entry",
        "stripe_refund_id",
        "Stripe Refund ID",
        "Data",
        "stripe_payment_intent_id",
        unique=1,
        read_only=1,
    )
    _add_custom_field(
        "Payment Entry",
        "stripe_balance_transaction_id",
        "Stripe Balance Transaction ID",
        "Data",
        "stripe_refund_id",
        read_only=1,
    )
    _add_custom_field(
        "Subscription",
        "stripe_customer_id",
        "Stripe Customer ID",
        "Data",
        "stripe_subscription_id",
        read_only=1,
    )
    _add_custom_field(
        "Subscription",
        "stripe_setup_token_nonce",
        "Stripe Setup Token Nonce",
        "Data",
        "stripe_customer_id",
        hidden=1,
        no_copy=1,
        read_only=1,
    )
    frappe.db.commit()
