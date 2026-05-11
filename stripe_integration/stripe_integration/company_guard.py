import frappe


def _company_of(doctype: str, name: str | None) -> str | None:
    if not name:
        return None
    return frappe.db.get_value(doctype, name, "company")


def _throw(msg: str):
    frappe.throw(msg, title="Company Scope Validation")


def validate_sales_invoice_company_scope(doc, method=None):
    company = doc.get("company")
    if not company:
        return

    # Header-level account / dimensions
    for doctype, field, label in [
        ("Account", "debit_to", "Receivable Account"),
        ("Cost Center", "cost_center", "Cost Center"),
        ("Sales Taxes and Charges Template", "taxes_and_charges", "Taxes and Charges Template"),
    ]:
        value = doc.get(field)
        if not value:
            continue
        c = _company_of(doctype, value)
        if c and c != company:
            _throw(f"{label} {value} belongs to {c}, not {company}.")

    # Item-level dimensions/accounts
    for idx, row in enumerate(doc.get("items") or [], start=1):
        for doctype, field, label in [
            ("Account", "income_account", "Income Account"),
            ("Cost Center", "cost_center", "Cost Center"),
            ("Account", "expense_account", "Expense Account"),
        ]:
            value = row.get(field)
            if not value:
                continue
            c = _company_of(doctype, value)
            if c and c != company:
                _throw(f"Row {idx}: {label} {value} belongs to {c}, not {company}.")


def validate_subscription_company_scope(doc, method=None):
    company = doc.get("company")
    if not company:
        return

    # Sales tax template on subscription (custom field in this stack)
    tax_template = doc.get("sales_tax_template")
    if tax_template:
        c = _company_of("Sales Taxes and Charges Template", tax_template)
        if c and c != company:
            _throw(f"Sales Tax Template {tax_template} belongs to {c}, not {company}.")

    # Enforce company-consistent subscription plans when plan has company field
    has_plan_company = bool(frappe.get_meta("Subscription Plan").get_field("company"))
    if not has_plan_company:
        return

    for idx, row in enumerate(doc.get("plans") or [], start=1):
        plan = row.get("plan")
        if not plan or not frappe.db.exists("Subscription Plan", plan):
            continue
        plan_company = frappe.db.get_value("Subscription Plan", plan, "company")
        if plan_company and plan_company != company:
            _throw(f"Row {idx}: Subscription Plan {plan} belongs to {plan_company}, not {company}.")
