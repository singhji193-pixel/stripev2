import json

import frappe
from frappe.utils.password import set_encrypted_password


def _require_system_manager():
    # allow in console/patch too
    if frappe.session.user == "Administrator":
        return
    roles = set(frappe.get_roles(frappe.session.user))
    if "System Manager" not in roles:
        frappe.throw("Not permitted", frappe.PermissionError)


def _normalize_abbr(abbr: str) -> str:
    return (abbr or "").strip().upper()


def upsert_accounts(accounts: list[dict]):
    """Upsert Stripe Account docs.

    accounts: [{company_abbr,publishable_key,secret_key,webhook_secret,company?,enabled?,test_mode?}]
    """

    frappe.set_user("Administrator")

    touched = []

    for a in accounts:
        abbr = _normalize_abbr(a.get("company_abbr"))
        if not abbr:
            frappe.throw("company_abbr is required")

        pub = (a.get("publishable_key") or "").strip()
        if not pub:
            frappe.throw(f"publishable_key is required for {abbr}")

        name = frappe.db.get_value("Stripe Account", {"company_abbr": abbr}, "name")
        if name:
            doc = frappe.get_doc("Stripe Account", name)
        else:
            doc = frappe.new_doc("Stripe Account")
            doc.company_abbr = abbr

        # optional fields
        if a.get("company"):
            doc.company = a.get("company")

        doc.enabled = 1 if a.get("enabled", 1) else 0
        doc.test_mode = 1 if a.get("test_mode", 1) else 0
        doc.publishable_key = pub

        doc.save(ignore_permissions=True)

        # store secrets using encrypted password storage
        if a.get("secret_key"):
            set_encrypted_password(doc.doctype, doc.name, "secret_key", a.get("secret_key"))

        if a.get("webhook_secret"):
            set_encrypted_password(doc.doctype, doc.name, "webhook_secret", a.get("webhook_secret"))

        touched.append(abbr)

    frappe.db.commit()

    configured = [
        r.company_abbr
        for r in frappe.get_all(
            "Stripe Account",
            filters={"enabled": 1},
            fields=["company_abbr"],
            order_by="company_abbr asc",
        )
    ]

    return {"ok": True, "touched": touched, "configured": configured}


@frappe.whitelist()
def import_accounts(payload: str):
    """Importer endpoint.

    payload can be JSON string: {"accounts": [...]}

    Security: System Manager only
    """

    _require_system_manager()

    if not payload:
        frappe.throw("payload is required")

    if isinstance(payload, str):
        data = json.loads(payload)
    else:
        data = payload

    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not accounts:
        frappe.throw("accounts is required")

    return upsert_accounts(accounts)
