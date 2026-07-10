from datetime import datetime, timezone

import frappe

from stripe_integration.stripe_integration.utils import get_company_abbr_from_company


class MariaDBNamedLock:
    """Serialize financial work that Stripe can deliver more than once."""

    def __init__(self, name: str, timeout: int = 30):
        safe_name = "".join(ch if ch.isalnum() or ch in "_-" else "-" for ch in str(name))
        self.name = safe_name[:64]
        self.timeout = timeout
        self.acquired = False

    def __enter__(self):
        rows = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (self.name, self.timeout))
        if not (rows and rows[0] and rows[0][0] == 1):
            frappe.throw("Could not acquire Stripe accounting lock", frappe.ValidationError)
        self.acquired = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.acquired:
            try:
                frappe.db.sql("SELECT RELEASE_LOCK(%s)", (self.name,))
            except Exception:
                pass


def get_stripe_account_mapping(
    company_abbr: str,
    *,
    require_bank: bool = False,
    require_fee: bool = False,
) -> dict:
    abbr = (company_abbr or "").strip().upper()
    if not abbr:
        frappe.throw("Missing Stripe company abbreviation")

    account_doc = frappe.get_doc("Stripe Account", abbr)
    mapping = {
        "company_abbr": abbr,
        "company": account_doc.get("company"),
        "clearing": account_doc.get("stripe_clearing_account"),
        "bank": account_doc.get("bank_account"),
        "fee": account_doc.get("stripe_fee_account"),
    }

    required = ["company", "clearing"]
    if require_bank:
        required.append("bank")
    if require_fee:
        required.append("fee")
    missing = [field for field in required if not mapping.get(field)]
    if missing:
        frappe.throw("Missing Stripe account mapping: " + ", ".join(missing))

    for key in ("clearing", "bank", "fee"):
        account = mapping.get(key)
        if not account:
            continue
        account_company = frappe.db.get_value("Account", account, "company")
        if account_company and account_company != mapping["company"]:
            frappe.throw(
                f"Stripe {key} account {account} belongs to {account_company}, not {mapping['company']}"
            )

    mapping["currency"] = (
        frappe.db.get_value("Account", mapping["clearing"], "account_currency")
        or frappe.get_cached_value("Company", mapping["company"], "default_currency")
    )
    return mapping


def route_payment_entry_to_stripe_clearing(payment_entry, company_abbr: str | None = None) -> dict:
    abbr = company_abbr or get_company_abbr_from_company(payment_entry.get("company"))
    mapping = get_stripe_account_mapping(abbr)
    payment_type = (payment_entry.get("payment_type") or "").strip()

    if payment_type == "Receive":
        payment_entry.paid_to = mapping["clearing"]
        if payment_entry.meta.get_field("paid_to_account_currency"):
            payment_entry.paid_to_account_currency = mapping["currency"]
    elif payment_type == "Pay":
        payment_entry.paid_from = mapping["clearing"]
        if payment_entry.meta.get_field("paid_from_account_currency"):
            payment_entry.paid_from_account_currency = mapping["currency"]
    else:
        frappe.throw(f"Unsupported Payment Entry type for Stripe accounting: {payment_type}")

    return mapping


def prepare_stripe_receipt_payment_entry(
    invoice,
    paid_amount: float,
    stripe_reference: str,
    company_abbr: str,
    posting_date: str | None = None,
):
    """Build a Stripe receipt, leaving any payment above the live balance unallocated."""

    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    amount = float(paid_amount or 0)
    if amount <= 0:
        frappe.throw("Stripe payment amount must be positive")

    invoice_currency = invoice.get("currency")
    mapping = get_stripe_account_mapping(company_abbr)
    validate_stripe_currency(
        mapping.get("currency"),
        invoice_currency,
        f"Stripe Clearing for Sales Invoice {invoice.name}",
    )

    outstanding = max(float(invoice.get("outstanding_amount") or 0), 0.0)
    reference_date = posting_date or frappe.utils.nowdate()

    if outstanding > 0:
        payment_entry = get_payment_entry(
            "Sales Invoice",
            invoice.name,
            reference_date=reference_date,
        )
    else:
        # ERPNext otherwise infers a Pay entry for a fully paid Sales Invoice.
        payment_entry = get_payment_entry(
            "Sales Invoice",
            invoice.name,
            party_amount=amount,
            bank_amount=amount,
            payment_type="Receive",
            reference_date=reference_date,
        )
        payment_entry.set("references", [])

    route_payment_entry_to_stripe_clearing(payment_entry, company_abbr)
    payment_entry.posting_date = reference_date
    payment_entry.reference_date = reference_date
    payment_entry.reference_no = stripe_reference
    payment_entry.paid_amount = amount
    payment_entry.received_amount = amount

    amount_to_allocate = min(amount, outstanding)
    remaining = amount_to_allocate
    for reference in payment_entry.get("references") or []:
        row_outstanding = abs(float(reference.get("outstanding_amount") or 0))
        reference.allocated_amount = min(row_outstanding, remaining)
        remaining -= float(reference.allocated_amount or 0)
        if remaining <= 0:
            remaining = 0

    allocated_amount = amount_to_allocate - remaining
    return payment_entry, allocated_amount, max(amount - allocated_amount, 0.0)


def validate_stripe_currency(currency: str | None, expected_currency: str | None, context: str):
    actual = (currency or "").strip().upper()
    expected = (expected_currency or "").strip().upper()
    if actual and expected and actual != expected:
        frappe.throw(f"Stripe currency mismatch for {context}: {actual} != {expected}")


def stripe_timestamp_date(timestamp: int | None):
    if not timestamp:
        return frappe.utils.nowdate()
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).date().isoformat()
