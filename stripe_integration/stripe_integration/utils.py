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


def resolve_customer_email(customer_name: str):
    """Resolve the best available email for a Customer.

    ERPNext stores customer emails in several places depending on how the
    record was created. Check them in order of preference:

    1. Customer.email_id (legacy, often blank in modern installs).
    2. Customer.customer_primary_contact -> Contact.email_id.
    3. Any Contact linked to the Customer via Dynamic Link, picking the
       primary Contact Email row (then primary contact, then oldest).

    Returns the email string, or None if no email is configured anywhere.
    Used by both the Sales Invoice payment-request flow and the
    Subscription lifecycle / payment-method flows, so that fixing one
    customer's email setup is enough for both.
    """
    if not customer_name:
        return None

    email = frappe.db.get_value("Customer", customer_name, "email_id")
    if email:
        return email

    primary_contact = frappe.db.get_value("Customer", customer_name, "customer_primary_contact")
    if primary_contact:
        email = frappe.db.get_value("Contact", primary_contact, "email_id")
        if email:
            return email

    rows = frappe.db.sql(
        """
        SELECT ce.email_id
        FROM `tabContact Email` ce
        JOIN `tabContact` c ON c.name = ce.parent
        JOIN `tabDynamic Link` dl ON dl.parent = c.name
        WHERE dl.link_doctype = 'Customer'
          AND dl.link_name = %s
          AND ce.email_id IS NOT NULL
          AND ce.email_id != ''
        ORDER BY ce.is_primary DESC, c.is_primary_contact DESC, c.creation ASC
        LIMIT 1
        """,
        (customer_name,),
    )
    if rows:
        return rows[0][0]
    return None


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
