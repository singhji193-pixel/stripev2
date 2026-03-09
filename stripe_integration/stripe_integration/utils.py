import frappe


def get_company_abbr_from_company(company: str):
    if not company:
        return None
    return frappe.db.get_value("Company", company, "abbr")


def _normalize_abbr(company_abbr: str) -> str:
    return (company_abbr or "").strip().upper()


def _get_account_doc(company_abbr: str):
    abbr = _normalize_abbr(company_abbr)
    if not abbr:
        frappe.throw("Missing company abbr")

    name = frappe.db.get_value("Stripe Account", {"company_abbr": abbr, "enabled": 1}, "name")
    if not name:
        frappe.throw(f"Stripe account not configured for company abbr: {abbr}")

    return frappe.get_doc("Stripe Account", name)


def get_api_key(company_abbr: str) -> str:
    doc = _get_account_doc(company_abbr)
    key = doc.get_password("secret_key")
    if not key:
        frappe.throw(f"Stripe secret key not configured for {company_abbr}")
    return key


def get_publishable_key(company_abbr: str) -> str:
    doc = _get_account_doc(company_abbr)
    key = (doc.publishable_key or "").strip()
    if not key:
        frappe.throw(f"Stripe publishable key not configured for {company_abbr}")
    return key


def get_webhook_secret(company_abbr: str):
    doc = _get_account_doc(company_abbr)
    return doc.get_password("webhook_secret")


def clone_cosl_stripe_email_templates():
    """Clone existing COSL Stripe email templates for deposit + remainder."""
    import frappe

    source_name = "Payment Request - Stripe"
    targets = [
        ("Stripe Deposit Requested - COSL", "Deposit Request from CoreOrbit Systems Ltd. - {{ doc.name }}"),
        ("Stripe Remaining Balance Requested - COSL", "Remaining Balance Request from CoreOrbit Systems Ltd. - {{ doc.name }}"),
    ]

    src = frappe.get_doc("Email Template", source_name)
    created = []

    for name, subject in targets:
        if frappe.db.exists("Email Template", name):
            continue

        d = frappe.new_doc("Email Template")
        for f in ["response", "body", "use_html", "enabled", "sender", "sender_email", "reply_to"]:
            if hasattr(src, f):
                setattr(d, f, getattr(src, f))

        d.name = name
        d.subject = subject
        d.insert(ignore_permissions=True)
        created.append(name)

    frappe.db.commit()
    return created
