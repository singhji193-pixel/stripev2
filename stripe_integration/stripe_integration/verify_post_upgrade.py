import frappe

REQUIRED_TEMPLATES = [
    "Stripe CoreOrbit Add Payment Method",
    "Stripe CoreOrbit Subscription Started",
    "Stripe CoreOrbit Subscription Paused",
    "Stripe CoreOrbit Subscription Cancelled",
    "Stripe CoreOrbit Payment Request",
    "Stripe COEngine Add Payment Method",
    "Stripe COEngine Subscription Started",
    "Stripe COEngine Subscription Paused",
    "Stripe COEngine Subscription Resumed",
    "Stripe COEngine Subscription Cancelled",
    "Stripe COEngine Payment Request",
    "Stripe Payment Receipt Branded",
    "Stripe CoreOrbit Refund Processed",
    "Stripe COEngine Refund Processed",
]

REQUIRED_PRINT_FORMATS = [
    "CoreOrbit Beautiful Invoice",
    "COEngine Beautiful Invoice",
    "CoreOrbit Refund Confirmation",
    "COEngine Refund Confirmation",
]

REQUIRED_SUB_FIELDS = [
    "stripe_setup_checkout_url",
    "stripe_setup_session_id",
    "stripe_setup_link_created_at",
    "stripe_setup_link_expires_at",
    "stripe_setup_link_status",
    "stripe_default_payment_method_id",
    "stripe_last_setup_intent_id",
]


def run():
    out = {
        "ok": True,
        "missing_templates": [],
        "missing_print_formats": [],
        "missing_subscription_fields": [],
        "notes": [],
    }

    for name in REQUIRED_TEMPLATES:
        if not frappe.db.exists("Email Template", name):
            out["missing_templates"].append(name)

    for name in REQUIRED_PRINT_FORMATS:
        if not frappe.db.exists("Print Format", name):
            out["missing_print_formats"].append(name)

    meta = frappe.get_meta("Subscription")
    for fieldname in REQUIRED_SUB_FIELDS:
        if not meta.get_field(fieldname):
            out["missing_subscription_fields"].append(fieldname)

    if out["missing_templates"] or out["missing_print_formats"] or out["missing_subscription_fields"]:
        out["ok"] = False

    # smoke: confirm COSL add-payment template uses setup URL first
    et = frappe.db.get_value("Email Template", "Stripe CoreOrbit Add Payment Method", "response") or ""
    if "stripe_setup_checkout_url" not in et:
        out["ok"] = False
        out["notes"].append("COSL add-payment template missing stripe_setup_checkout_url variable")

    return out
