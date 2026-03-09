import json
import time
import hmac
import hashlib

import frappe
import requests

from frappe.utils import flt, now_datetime

from stripe_integration.stripe_integration.api import request_payment_stripe
from stripe_integration.stripe_integration.utils import (
    get_company_abbr_from_company,
    get_webhook_secret,
)


def _sign_payload(webhook_secret: str, payload: str, timestamp: int | None = None) -> str:
    ts = int(timestamp or time.time())
    signed_payload = f"{ts}.{payload}".encode("utf-8")
    sig = hmac.new(webhook_secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _post_stripe_webhook(company_abbr: str, event: dict) -> tuple[int, str]:
    secret = get_webhook_secret(company_abbr)
    if not secret:
        raise Exception(f"No webhook secret configured for {company_abbr}")

    payload = json.dumps(event)
    sig = _sign_payload(secret, payload)

    res = requests.post(
        "https://next.coengine.ai/api/method/stripe_integration.stripe_integration.webhook.handle_webhook",
        data=payload,
        headers={
            "Stripe-Signature": sig,
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    return res.status_code, res.text


def _wait_invoice(invoice_name: str, expect_outstanding: float | None = None, timeout_s: int = 90):
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        status, outstanding, processed = frappe.db.get_value(
            "Sales Invoice",
            invoice_name,
            ["status", "outstanding_amount", "custom_stripe_payment_processed"],
        )
        outstanding = float(outstanding or 0)
        cur = (status, outstanding, int(processed or 0))
        if cur != last:
            last = cur
            print("invoice_state", invoice_name, cur)
        if expect_outstanding is None:
            return cur
        if abs(outstanding - float(expect_outstanding)) < 0.0001:
            return cur
        time.sleep(2)
    return last


def run():
    # Avoid blocking on email during automated E2E.
    frappe.sendmail = lambda *args, **kwargs: None  # type: ignore

    # Create a fresh invoice so we respect Update After Submit constraints.
    customer = "Test Customer - Stripe Payment"
    company = "CoreOrbit Systems Ltd."
    item_code = "MetaAds"
    income_account = "4130 - Marketing Services Revenue - COSL"

    inv = frappe.new_doc("Sales Invoice")
    inv.customer = customer
    inv.company = company
    inv.currency = "CAD"

    inv.payment_split_type = "Split Payment"
    inv.initial_payment_percentage = 60
    inv.custom_stripe_payment_processed = 0

    inv.append(
        "items",
        {
            "item_code": item_code,
            "qty": 1,
            "rate": 50,
            "income_account": income_account,
        },
    )

    inv.insert(ignore_permissions=True)
    inv.submit()

    invoice_name = inv.name
    frappe.db.commit()
    print("created_invoice", invoice_name, "grand_total", float(inv.grand_total))

    company_abbr = get_company_abbr_from_company(company)

    # 1) First request (deposit)
    r1 = request_payment_stripe(invoice_name)
    print("request_1", r1)

    deposit_amount = flt(r1["amount"])
    if r1.get("request_kind") != "deposit":
        raise Exception(f"Expected deposit request_kind, got: {r1}")

    pi1 = f"pi_mock_deposit_{invoice_name}"
    evt1 = {
        "id": f"evt_mock_deposit_{invoice_name}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": r1["session_id"],
                "object": "checkout.session",
                "payment_status": "paid",
                "status": "complete",
                "amount_total": int(round(deposit_amount * 100)),
                "currency": "cad",
                "payment_intent": pi1,
                "metadata": {
                    "doctype": "Sales Invoice",
                    "docname": invoice_name,
                    "company": company,
                    "company_abbr": company_abbr,
                    "site": frappe.local.site,
                    "request_kind": "deposit",
                },
            }
        },
    }

    code1, body1 = _post_stripe_webhook(company_abbr, evt1)
    print("webhook_1", code1, body1)
    if code1 != 200:
        raise Exception(f"deposit webhook failed: {code1} {body1}")

    expected_out_after_deposit = flt(inv.grand_total) - deposit_amount
    _wait_invoice(invoice_name, expect_outstanding=expected_out_after_deposit, timeout_s=120)

    # 2) Second request (remainder)
    r2 = request_payment_stripe(invoice_name)
    print("request_2", r2)

    remainder_amount = flt(r2["amount"])
    if r2.get("request_kind") != "remainder":
        raise Exception(f"Expected remainder request_kind, got: {r2}")

    pi2 = f"pi_mock_remainder_{invoice_name}"
    evt2 = {
        "id": f"evt_mock_remainder_{invoice_name}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": r2["session_id"],
                "object": "checkout.session",
                "payment_status": "paid",
                "status": "complete",
                "amount_total": int(round(remainder_amount * 100)),
                "currency": "cad",
                "payment_intent": pi2,
                "metadata": {
                    "doctype": "Sales Invoice",
                    "docname": invoice_name,
                    "company": company,
                    "company_abbr": company_abbr,
                    "site": frappe.local.site,
                    "request_kind": "remainder",
                },
            }
        },
    }

    code2, body2 = _post_stripe_webhook(company_abbr, evt2)
    print("webhook_2", code2, body2)
    if code2 != 200:
        raise Exception(f"remainder webhook failed: {code2} {body2}")

    status, outstanding, processed = _wait_invoice(invoice_name, expect_outstanding=0.0, timeout_s=180)

    pe1 = frappe.db.count("Payment Entry", {"reference_no": pi1, "docstatus": ["!=", 2]})
    pe2 = frappe.db.count("Payment Entry", {"reference_no": pi2, "docstatus": ["!=", 2]})

    return {
        "ok": True,
        "invoice": invoice_name,
        "deposit": float(deposit_amount),
        "remainder": float(remainder_amount),
        "final_status": status,
        "final_outstanding": float(outstanding),
        "processed_flag": int(processed),
        "payment_entries": {pi1: pe1, pi2: pe2},
        "timestamp": str(now_datetime()),
    }
