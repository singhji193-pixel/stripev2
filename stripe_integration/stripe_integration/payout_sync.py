import json

import frappe
import stripe

from stripe_integration.stripe_integration.accounting import (
    MariaDBNamedLock,
    get_stripe_account_mapping,
    stripe_timestamp_date,
    validate_stripe_currency,
)
from stripe_integration.stripe_integration.event_log import mark_event_status, upsert_event
from stripe_integration.stripe_integration.utils import get_api_key


def _stripe_get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        value = obj[key]
        return default if value is None else value
    except Exception:
        pass
    try:
        if hasattr(obj, key):
            value = getattr(obj, key)
            if not callable(value):
                return default if value is None else value
    except Exception:
        pass
    try:
        as_dict = obj.to_dict_recursive() if hasattr(obj, "to_dict_recursive") else obj.to_dict()
        if isinstance(as_dict, dict):
            value = as_dict.get(key, default)
            return default if value is None else value
    except Exception:
        pass
    return default


def _is_enabled() -> bool:
    try:
        return int(frappe.db.get_single_value("Stripe Settings", "enable_payout_sync") or 0) == 1
    except Exception:
        return False


def _je_exists_for_payout(payout_id: str) -> bool:
    return bool(frappe.db.exists("Journal Entry", {"cheque_no": payout_id, "docstatus": ["!=", 2]}))


def _clearing_balance(account: str, posting_date: str) -> float:
    rows = frappe.db.sql(
        """
        SELECT COALESCE(SUM(debit - credit), 0)
        FROM `tabGL Entry`
        WHERE account = %s
          AND posting_date <= %s
          AND is_cancelled = 0
        """,
        (account, posting_date),
    )
    return float(rows[0][0] or 0) if rows else 0.0


def _find_payment_entry_by_pi(payment_intent_id: str):
    name = frappe.db.get_value(
        "Payment Entry",
        {"reference_no": payment_intent_id, "docstatus": 1},
        "name",
    )
    if not name and frappe.get_meta("Payment Entry").get_field("stripe_payment_intent_id"):
        name = frappe.db.get_value(
            "Payment Entry",
            {"stripe_payment_intent_id": payment_intent_id, "docstatus": 1},
            "name",
        )
    if not name:
        return None
    return frappe.db.get_value(
        "Payment Entry",
        name,
        ["name", "paid_to", "company"],
        as_dict=True,
    )


def _find_refund_payment_entry(refund_id: str):
    if frappe.get_meta("Payment Entry").get_field("stripe_refund_id"):
        name = frappe.db.get_value(
            "Payment Entry",
            {"stripe_refund_id": refund_id, "docstatus": 1},
            "name",
        )
    else:
        name = frappe.db.get_value(
            "Payment Entry",
            {"reference_no": refund_id, "docstatus": 1},
            "name",
        )
    if not name:
        return None
    return frappe.db.get_value(
        "Payment Entry",
        name,
        ["name", "paid_from", "company"],
        as_dict=True,
    )


def _fee_je_exists(balance_transaction_id: str) -> bool:
    return bool(
        frappe.db.exists(
            "Journal Entry",
            {
                "cheque_no": f"fee_{balance_transaction_id}",
                "docstatus": 1,
            },
        )
    )


def _audit_payout_transactions(
    payout_id: str,
    payout_amount_cents: int,
    currency: str,
    accounts: dict,
    api_key: str,
):
    """Require every automatic-payout transaction to exist in ERP accounting."""

    transactions = []
    params = {"limit": 100, "payout": payout_id}
    while True:
        try:
            page = stripe.BalanceTransaction.list(api_key=api_key, **params)
        except Exception as exc:
            if "only be filtered on automatic" in str(exc).lower():
                return {
                    "reconciled": False,
                    "reason": "manual_payout_requires_review",
                    "transaction_count": 0,
                    "net_cents": 0,
                    "unmatched": [],
                }
            raise
        data = _stripe_get(page, "data") or []
        transactions.extend(data)
        if not _stripe_get(page, "has_more"):
            break
        params["starting_after"] = _stripe_get(data[-1], "id")

    if not transactions:
        return {
            "reconciled": False,
            "reason": "payout_transactions_unavailable",
            "transaction_count": 0,
            "net_cents": 0,
            "unmatched": [],
        }

    unmatched = []
    net_cents = 0
    for transaction in transactions:
        transaction_id = _stripe_get(transaction, "id") or ""
        transaction_type = (_stripe_get(transaction, "type") or "").lower()
        source_id = _stripe_get(transaction, "source") or ""
        transaction_currency = (_stripe_get(transaction, "currency") or "").upper()
        validate_stripe_currency(transaction_currency, currency, f"balance transaction {transaction_id}")
        net_cents += int(_stripe_get(transaction, "net") or 0)

        if transaction_type == "charge" and str(source_id).startswith("ch_"):
            charge = stripe.Charge.retrieve(source_id, api_key=api_key)
            payment_intent_id = _stripe_get(charge, "payment_intent")
            payment_entry = _find_payment_entry_by_pi(payment_intent_id) if payment_intent_id else None
            if not payment_entry:
                unmatched.append({"balance_transaction": transaction_id, "reason": "payment_entry_missing"})
                continue
            if payment_entry.get("company") != accounts["company"] or payment_entry.get("paid_to") != accounts["clearing"]:
                unmatched.append(
                    {
                        "balance_transaction": transaction_id,
                        "payment_entry": payment_entry.get("name"),
                        "reason": "payment_not_routed_to_stripe_clearing",
                    }
                )
                continue
            if int(_stripe_get(transaction, "fee") or 0) > 0 and not _fee_je_exists(transaction_id):
                unmatched.append(
                    {
                        "balance_transaction": transaction_id,
                        "payment_entry": payment_entry.get("name"),
                        "reason": "stripe_fee_not_posted",
                    }
                )
            continue

        if transaction_type in {"refund", "payment_refund"} and str(source_id).startswith("re_"):
            refund_entry = _find_refund_payment_entry(source_id)
            if not refund_entry:
                unmatched.append({"balance_transaction": transaction_id, "reason": "refund_payment_entry_missing"})
                continue
            if refund_entry.get("company") != accounts["company"] or refund_entry.get("paid_from") != accounts["clearing"]:
                unmatched.append(
                    {
                        "balance_transaction": transaction_id,
                        "payment_entry": refund_entry.get("name"),
                        "reason": "refund_not_routed_to_stripe_clearing",
                    }
                )
                continue
            if int(_stripe_get(transaction, "fee") or 0) != 0:
                unmatched.append(
                    {
                        "balance_transaction": transaction_id,
                        "reason": "refund_fee_adjustment_requires_review",
                    }
                )
            continue

        unmatched.append(
            {
                "balance_transaction": transaction_id,
                "transaction_type": transaction_type,
                "reason": "unsupported_or_external_stripe_transaction",
            }
        )

    if net_cents != int(payout_amount_cents):
        unmatched.append(
            {
                "reason": "payout_net_does_not_match_transactions",
                "payout_amount_cents": int(payout_amount_cents),
                "transaction_net_cents": net_cents,
            }
        )

    return {
        "reconciled": not unmatched,
        "reason": None if not unmatched else "payout_contains_unreconciled_transactions",
        "transaction_count": len(transactions),
        "net_cents": net_cents,
        "unmatched": unmatched[:25],
        "unmatched_count": len(unmatched),
    }


def _make_journal_entry(
    company: str,
    payout_id: str,
    net: float,
    accounts: dict,
    posting_date: str | None = None,
    currency: str | None = None,
):
    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.company = company
    je.posting_date = posting_date or frappe.utils.nowdate()
    je.cheque_no = payout_id
    je.cheque_date = je.posting_date
    currency_label = f" {currency}" if currency else ""
    je.user_remark = f"Stripe paid payout {payout_id} ({net:.2f}{currency_label})"

    je.append("accounts", {"account": accounts["bank"], "debit_in_account_currency": net})
    je.append("accounts", {"account": accounts["clearing"], "credit_in_account_currency": net})

    je.insert(ignore_permissions=True)
    je.submit()
    frappe.db.commit()
    return je.name


def sync_payout_from_webhook_event(event: dict, company_abbr_hint: str | None = None):
    if not _is_enabled():
        return {"handled": False, "reason": "payout_sync_disabled"}

    event_type = (event or {}).get("type")
    obj = (event.get("data", {}) or {}).get("object", {}) or {}
    payout_id = obj.get("id")

    if event_type != "payout.paid" or (obj.get("status") and obj.get("status") != "paid"):
        return {
            "handled": False,
            "reason": "payout_not_paid",
            "event_type": event_type,
            "payout_id": payout_id,
            "payout_status": obj.get("status"),
        }

    metadata = obj.get("metadata") or {}
    metadata_company_abbr = (metadata.get("company_abbr") or "").strip().upper()
    company_abbr = (company_abbr_hint or metadata_company_abbr or "").strip().upper()

    if metadata_company_abbr and company_abbr_hint and metadata_company_abbr != company_abbr:
        return {
            "handled": False,
            "reason": "payout_company_mismatch",
            "payout_id": payout_id,
        }

    if not payout_id or not company_abbr:
        return {
            "handled": False,
            "reason": "missing_payout_id_or_company_abbr",
            "payout_id": payout_id,
            "company_abbr": company_abbr,
            "company_abbr_hint": company_abbr_hint,
        }

    if (
        frappe.get_meta("Stripe Account").get_field("payout_sync_enabled")
        and not int(frappe.db.get_value("Stripe Account", company_abbr, "payout_sync_enabled") or 0)
    ):
        return {
            "handled": False,
            "reason": "company_payout_sync_disabled",
            "payout_id": payout_id,
            "company_abbr": company_abbr,
        }

    upsert_event(
        event,
        payload=json.dumps(event, default=str).encode(),
        company_abbr=company_abbr,
        status="Processing",
    )

    with MariaDBNamedLock(f"stripe-payout-{payout_id}", timeout=30):
        if _je_exists_for_payout(payout_id):
            mark_event_status(event.get("id"), "Ignored", "already_posted")
            return {"handled": True, "dedup": True, "reason": "already_posted", "payout_id": payout_id}

        api_key = get_api_key(company_abbr)
        payout = stripe.Payout.retrieve(payout_id, api_key=api_key)
        if _stripe_get(payout, "status") != "paid":
            return {
                "handled": False,
                "reason": "payout_not_paid",
                "payout_id": payout_id,
                "payout_status": _stripe_get(payout, "status"),
            }

        amount_c = int(_stripe_get(payout, "amount") or 0)
        if amount_c <= 0:
            return {"handled": False, "reason": "invalid_payout_amount", "payout_id": payout_id}

        currency = (_stripe_get(payout, "currency") or "").upper()
        accounts = get_stripe_account_mapping(company_abbr, require_bank=True)
        validate_stripe_currency(currency, accounts.get("currency"), f"payout {payout_id}")

        if _stripe_get(payout, "automatic") is False:
            return {
                "handled": False,
                "reason": "manual_payout_requires_review",
                "payout_id": payout_id,
                "payout_amount": amount_c / 100.0,
                "currency": currency,
            }

        net = amount_c / 100.0
        transaction_audit = _audit_payout_transactions(
            payout_id,
            amount_c,
            currency,
            accounts,
            api_key,
        )
        if not transaction_audit.get("reconciled"):
            return {
                "handled": False,
                "reason": transaction_audit.get("reason"),
                "payout_id": payout_id,
                "transaction_audit": transaction_audit,
            }

        posting_date = stripe_timestamp_date(_stripe_get(payout, "arrival_date") or _stripe_get(payout, "created"))
        clearing_balance = _clearing_balance(accounts["clearing"], posting_date)
        if clearing_balance + 0.01 < net:
            return {
                "handled": False,
                "reason": "insufficient_stripe_clearing_balance",
                "payout_id": payout_id,
                "payout_amount": net,
                "clearing_balance": clearing_balance,
                "clearing_account": accounts["clearing"],
            }

        je_name = _make_journal_entry(
            accounts["company"],
            payout_id,
            net,
            accounts,
            posting_date=posting_date,
            currency=currency,
        )
        mark_event_status(event.get("id"), "Completed")

    return {
        "handled": True,
        "payout_id": payout_id,
        "company_abbr": company_abbr,
        "journal_entry": je_name,
        "payout_amount": net,
        "net": net,
        "currency": currency,
        "posting_date": posting_date,
        "clearing_balance_before": clearing_balance,
        "transaction_audit": transaction_audit,
    }
