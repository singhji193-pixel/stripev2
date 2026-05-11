import json

import frappe
import stripe

from stripe_integration.stripe_integration.utils import get_api_key


def _stripe_get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        if hasattr(obj, key):
            value = getattr(obj, key)
            return default if value is None else value
    except Exception:
        pass
    try:
        value = obj[key]
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


def _get_accounts_for_company_abbr(company_abbr: str):
    acc = frappe.get_doc("Stripe Account", company_abbr)
    return {
        "company": acc.company,
        "clearing": getattr(acc, "stripe_clearing_account", None),
        "fee": getattr(acc, "stripe_fee_account", None),
    }


def _validate_accounts(a: dict):
    missing = [k for k in ("company", "clearing", "fee") if not a.get(k)]
    if missing:
        frappe.throw("Missing Stripe fee mapping on Stripe Account: " + ", ".join(missing))


def _je_exists_for_balance_txn(balance_txn_id: str) -> bool:
    if not balance_txn_id:
        return False
    return bool(
        frappe.db.exists(
            "Journal Entry",
            {
                "cheque_no": f"fee_{balance_txn_id}",
                "docstatus": ["!=", 2],
            },
        )
    )


def _create_fee_journal_entry(company: str, balance_txn_id: str, fee: float, accounts: dict, remark_ctx: str | None = None):
    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.company = company
    je.posting_date = frappe.utils.nowdate()
    je.cheque_no = f"fee_{balance_txn_id}"
    je.cheque_date = frappe.utils.nowdate()
    ctx = f" ({remark_ctx})" if remark_ctx else ""
    je.user_remark = f"Stripe fee {balance_txn_id}{ctx}"

    je.append("accounts", {"account": accounts["fee"], "debit_in_account_currency": fee})
    je.append("accounts", {"account": accounts["clearing"], "credit_in_account_currency": fee})

    je.insert(ignore_permissions=True)
    je.submit()
    return je.name


def post_fee_for_payment_intent(company_abbr: str, stripe_payment_intent_id: str, remark_ctx: str | None = None):
    if not company_abbr or not stripe_payment_intent_id:
        return {"handled": False, "reason": "missing_company_or_pi"}

    stripe.api_key = get_api_key(company_abbr)

    pi = stripe.PaymentIntent.retrieve(stripe_payment_intent_id)
    charge_id = _stripe_get(pi, "latest_charge")
    if not charge_id:
        charges = _stripe_get(pi, "charges") or {}
        charges_data = _stripe_get(charges, "data") or []
        if charges_data:
            charge_id = _stripe_get(charges_data[-1], "id")

    if not charge_id:
        return {"handled": False, "reason": "no_charge_on_pi", "payment_intent": stripe_payment_intent_id}

    ch = stripe.Charge.retrieve(charge_id)
    bt_id = _stripe_get(ch, "balance_transaction")
    if not bt_id:
        return {"handled": False, "reason": "no_balance_transaction", "charge": charge_id}

    if _je_exists_for_balance_txn(bt_id):
        return {"handled": True, "dedup": True, "balance_transaction": bt_id}

    bt = stripe.BalanceTransaction.retrieve(bt_id)
    fee = abs(float((_stripe_get(bt, "fee") or 0)) / 100.0)
    if fee <= 0:
        return {"handled": True, "reason": "zero_fee", "balance_transaction": bt_id}

    accounts = _get_accounts_for_company_abbr(company_abbr)
    _validate_accounts(accounts)

    je_name = _create_fee_journal_entry(accounts["company"], bt_id, fee, accounts, remark_ctx=remark_ctx)
    frappe.db.commit()
    return {
        "handled": True,
        "balance_transaction": bt_id,
        "fee": fee,
        "journal_entry": je_name,
    }


def _enqueue_fee_retry(company_abbr: str, stripe_payment_intent_id: str, remark_ctx: str | None = None):
    safe_company = (company_abbr or 'NA').replace(':', '-').replace('/', '-')
    safe_pi = (stripe_payment_intent_id or 'NA').replace(':', '-').replace('/', '-')
    job_name = f"stripe-fee-retry-{safe_company}-{safe_pi}"
    frappe.enqueue(
        "stripe_integration.stripe_integration.stripe_fees.retry_post_fee_for_payment_intent",
        queue="short",
        timeout=300,
        enqueue_after_commit=True,
        job_name=job_name,
        company_abbr=company_abbr,
        stripe_payment_intent_id=stripe_payment_intent_id,
        remark_ctx=remark_ctx,
    )


def ensure_fee_posted(company_abbr: str, stripe_payment_intent_id: str, remark_ctx: str | None = None, enqueue_retry: bool = True):
    """Attempt fee posting immediately; on non-terminal miss, enqueue retry + durable error log."""
    try:
        result = post_fee_for_payment_intent(company_abbr, stripe_payment_intent_id, remark_ctx=remark_ctx)
    except Exception as e:
        result = {"handled": False, "reason": "exception", "error": str(e)}

    if result.get("handled"):
        return result

    reason = result.get("reason") or "unknown"

    # Durable logging for audit/debug.
    frappe.log_error(
        title="Stripe Fee Auto-Posting Miss",
        message=json.dumps(
            {
                "company_abbr": company_abbr,
                "payment_intent": stripe_payment_intent_id,
                "remark_ctx": remark_ctx,
                "result": result,
            },
            default=str,
        ),
    )

    # Transient cases should retry asynchronously.
    if enqueue_retry and reason in {"no_charge_on_pi", "no_balance_transaction", "exception"}:
        try:
            _enqueue_fee_retry(company_abbr, stripe_payment_intent_id, remark_ctx=remark_ctx)
            result["retry_enqueued"] = True
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Stripe fee retry enqueue failed")

    return result


def retry_post_fee_for_payment_intent(company_abbr: str, stripe_payment_intent_id: str, remark_ctx: str | None = None):
    """Queue-safe retry entrypoint."""
    try:
        return post_fee_for_payment_intent(company_abbr, stripe_payment_intent_id, remark_ctx=remark_ctx)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Stripe fee retry execution failed")
        return {"handled": False, "reason": "retry_exception"}


def audit_unposted_fee_entries(company_abbr: str, limit: int = 100):
    """Find posted Payment Entries with PI refs where fee JE is missing.

    Returns lightweight audit rows for manual/retry follow-up.
    """
    stripe.api_key = get_api_key(company_abbr)
    account_doc = frappe.get_doc("Stripe Account", (company_abbr or "").strip().upper())
    company_name = account_doc.company

    rows = frappe.get_all(
        "Payment Entry",
        filters={
            "docstatus": 1,
            "company": company_name,
            "reference_no": ["like", "pi_%"],
        },
        fields=["name", "reference_no", "posting_date", "paid_amount"],
        order_by="creation desc",
        limit_page_length=max(1, min(int(limit or 100), 500)),
    )

    missing = []
    for pe in rows:
        pi_id = pe.get("reference_no")
        try:
            pi = stripe.PaymentIntent.retrieve(pi_id)
            charge_id = _stripe_get(pi, "latest_charge")
            if not charge_id:
                charges = _stripe_get(pi, "charges") or {}
                charges_data = _stripe_get(charges, "data") or []
                if charges_data:
                    charge_id = _stripe_get(charges_data[-1], "id")
            if not charge_id:
                missing.append({"payment_entry": pe["name"], "payment_intent": pi_id, "reason": "no_charge"})
                continue
            ch = stripe.Charge.retrieve(charge_id)
            bt_id = _stripe_get(ch, "balance_transaction")
            if not bt_id:
                missing.append({"payment_entry": pe["name"], "payment_intent": pi_id, "reason": "no_balance_txn"})
                continue
            if not _je_exists_for_balance_txn(bt_id):
                missing.append({"payment_entry": pe["name"], "payment_intent": pi_id, "balance_transaction": bt_id, "reason": "missing_fee_je"})
        except Exception as e:
            missing.append({"payment_entry": pe["name"], "payment_intent": pi_id, "reason": f"audit_exception: {e}"})

    return {"company_abbr": company_abbr, "checked": len(rows), "missing": missing}
