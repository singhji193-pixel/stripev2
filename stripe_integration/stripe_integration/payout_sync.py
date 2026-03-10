import json
import frappe
import stripe

from stripe_integration.stripe_integration.utils import get_api_key
from stripe_integration.stripe_integration.event_log import upsert_event, mark_event_status


def _is_enabled() -> bool:
    try:
        return int(frappe.db.get_single_value("Stripe Settings", "enable_payout_sync") or 0) == 1
    except Exception:
        return False


def _get_accounts_for_company_abbr(company_abbr: str):
    acc = frappe.get_doc("Stripe Account", company_abbr)
    return {
        "company": acc.company,
        "clearing": getattr(acc, "stripe_clearing_account", None),
        "bank": getattr(acc, "bank_account", None),
        "fee": getattr(acc, "stripe_fee_account", None),
    }


def _validate_accounts(a: dict):
    missing = [k for k in ("company", "clearing", "bank", "fee") if not a.get(k)]
    if missing:
        frappe.throw("Missing payout mapping on Stripe Account: " + ", ".join(missing))


def _je_exists_for_payout(payout_id: str) -> bool:
    return bool(frappe.db.exists("Journal Entry", {"cheque_no": payout_id, "docstatus": ["!=", 2]}))


def _sum_balance_txns_for_payout(payout_id: str):
    total_gross = 0
    total_fee = 0
    total_net = 0

    params = {"limit": 100, "payout": payout_id}
    while True:
        page = stripe.BalanceTransaction.list(**params)
        for bt in (page.get("data", []) or []):
            total_gross += int(bt.get("amount") or 0)
            total_fee += int(bt.get("fee") or 0)
            total_net += int(bt.get("net") or 0)

        if not page.get("has_more"):
            break
        params["starting_after"] = page["data"][-1]["id"]

    return total_gross, total_fee, total_net


def _make_journal_entry(company: str, payout_id: str, gross: float, fee: float, net: float, accounts: dict):
    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.company = company
    je.posting_date = frappe.utils.nowdate()
    je.cheque_no = payout_id
    je.cheque_date = frappe.utils.nowdate()
    je.user_remark = f"Stripe payout {payout_id} (gross={gross}, fee={fee}, net={net})"

    je.append("accounts", {"account": accounts["bank"], "debit_in_account_currency": net})

    if fee:
        je.append("accounts", {"account": accounts["fee"], "debit_in_account_currency": fee})

    je.append("accounts", {"account": accounts["clearing"], "credit_in_account_currency": gross})

    je.insert(ignore_permissions=True)
    je.submit()
    frappe.db.commit()
    return je.name


def sync_payout_from_webhook_event(event: dict):
    if not _is_enabled():
        return {"handled": False, "reason": "payout_sync_disabled"}

    obj = (event.get("data", {}) or {}).get("object", {}) or {}
    payout_id = obj.get("id")

    metadata = obj.get("metadata") or {}
    company_abbr = (metadata.get("company_abbr") or "").strip()

    if not payout_id or not company_abbr:
        return {
            "handled": False,
            "reason": "missing_payout_id_or_company_abbr",
            "payout_id": payout_id,
            "company_abbr": company_abbr,
        }

    upsert_event(event, payload=json.dumps(event).encode(), company_abbr=company_abbr, status="Processing")

    if _je_exists_for_payout(payout_id):
        mark_event_status(event.get("id"), "Ignored", "already_posted")
        return {"handled": False, "reason": "already_posted", "payout_id": payout_id}

    stripe.api_key = get_api_key(company_abbr)

    try:
        gross_c, fee_c, net_c = _sum_balance_txns_for_payout(payout_id)
    except Exception:
        # Stripe blocks payout-filtered balance transaction listing for some manual payouts.
        # Fallback to payout object amounts to keep posting reliable.
        payout = stripe.Payout.retrieve(payout_id)
        gross_c = int(payout.get("amount") or 0)
        fee_c = int(payout.get("fee") or 0)
        net_c = int(payout.get("amount") or 0) - int(payout.get("fee") or 0)

    gross = gross_c / 100.0
    fee = abs(fee_c) / 100.0
    net = net_c / 100.0

    accounts = _get_accounts_for_company_abbr(company_abbr)
    _validate_accounts(accounts)

    je_name = _make_journal_entry(accounts["company"], payout_id, gross, fee, net, accounts)
    mark_event_status(event.get("id"), "Completed")

    return {
        "handled": True,
        "payout_id": payout_id,
        "company_abbr": company_abbr,
        "journal_entry": je_name,
        "gross": gross,
        "fee": fee,
        "net": net,
    }
