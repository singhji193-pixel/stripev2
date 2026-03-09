import json, time, hmac, hashlib
import requests
import frappe
from stripe_integration.stripe_integration.utils import get_webhook_secret


def _sign(secret: str, payload: str) -> str:
    ts = int(time.time())
    sig = hmac.new(secret.encode('utf-8'), f"{ts}.{payload}".encode('utf-8'), hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def run():
    invoice_name = 'ACC-SINV-2026-00142'
    session_id = 'cs_test_a1eByB7AphlWvWccIItoPWpNdhLkPWRK9WxfLm9cIzVGybg7UlRr45UE99'
    amount_total = 2000
    pi = f'pi_mock_remainder_{invoice_name}'

    secret = get_webhook_secret('COSL')
    if not secret:
        raise Exception('Missing webhook secret for COSL')

    event = {
        'id': f'evt_mock_remainder_{invoice_name}',
        'type': 'checkout.session.completed',
        'data': {
            'object': {
                'id': session_id,
                'object': 'checkout.session',
                'payment_status': 'paid',
                'status': 'complete',
                'amount_total': amount_total,
                'currency': 'cad',
                'payment_intent': pi,
                'metadata': {
                    'doctype': 'Sales Invoice',
                    'docname': invoice_name,
                    'company_abbr': 'COSL',
                    'request_kind': 'remainder',
                },
            }
        },
    }

    payload = json.dumps(event)
    sig = _sign(secret, payload)

    res = requests.post(
        'https://next.coengine.ai/api/method/stripe_integration.stripe_integration.webhook.handle_webhook',
        data=payload,
        headers={'Stripe-Signature': sig, 'Content-Type': 'application/json'},
        timeout=30,
    )

    status, out = frappe.db.get_value('Sales Invoice', invoice_name, ['status', 'outstanding_amount'])
    pe_count = frappe.db.count('Payment Entry', {'reference_no': pi, 'docstatus': ['!=', 2]})

    return {
        'http': {'code': res.status_code, 'body': res.text},
        'invoice': {'name': invoice_name, 'status': status, 'outstanding': float(out or 0)},
        'payment_entry_count': pe_count,
    }
