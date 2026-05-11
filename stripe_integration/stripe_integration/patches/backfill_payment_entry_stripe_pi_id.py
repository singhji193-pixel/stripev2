import frappe


def execute():
    """Copy `reference_no` into `stripe_payment_intent_id` for submitted
    Payment Entries where the field was not populated when the row was
    created. Refund lookups join on `stripe_payment_intent_id`, so legacy
    rows without it cannot be matched to incoming Stripe refund events.
    """
    meta = frappe.get_meta("Payment Entry")
    if not meta.get_field("stripe_payment_intent_id"):
        return

    rows = frappe.db.sql(
        r"""
        SELECT name, reference_no
        FROM `tabPayment Entry`
        WHERE docstatus = 1
          AND reference_no LIKE 'pi\_%' ESCAPE '\\'
          AND (stripe_payment_intent_id IS NULL OR stripe_payment_intent_id = '')
        """,
        as_dict=True,
    )

    for row in rows:
        frappe.db.set_value(
            "Payment Entry",
            row.name,
            "stripe_payment_intent_id",
            row.reference_no,
            update_modified=False,
        )

    if rows:
        frappe.db.commit()
