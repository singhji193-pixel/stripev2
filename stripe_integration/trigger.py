import time, json, hmac, hashlib, time
import frappe, stripe, requests
from stripe_integration.stripe_integration.utils import get_company_abbr_from_company, get_api_key, get_webhook_secret

def run():
    invoice_name = 'ACC-SINV-2026-00139'
    si = frappe.get_doc('Sales Invoice', invoice_name)
    company_abbr = get_company_abbr_from_company(si.company)
    stripe.api_key = get_api_key(company_abbr)
    wh_secret = get_webhook_secret(company_abbr)
    
    sess_id = 'cs_test_a1poFiL56pF2SkA62WatSIL05W1TzeIxgsuxhUyBNTGvAQ4MfujeSCC9GY'
    sess = stripe.checkout.Session.retrieve(sess_id)
    
    # fake completed session payload
    payload = {
        'id': 'evt_test_123',
        'type': 'checkout.session.completed',
        'data': {
            'object': sess.to_dict()
        }
    }
    # set mock payment intent to test duplicate protection and payment entry creation
    payload['data']['object']['payment_intent'] = 'pi_test_mock_123'
    payload['data']['object']['payment_status'] = 'paid'
    
    payload_str = json.dumps(payload)
    timestamp = str(int(time.time()))
    signed_payload = f'{timestamp}.{payload_str}'
    signature = hmac.new(wh_secret.encode('utf-8'), signed_payload.encode('utf-8'), hashlib.sha256).hexdigest()
    
    stripe_signature = f't={timestamp},v1={signature}'
    
    headers = {
        'Stripe-Signature': stripe_signature,
        'Content-Type': 'application/json'
    }
    
    print('Firing webhook...')
    res = requests.post('https://next.coengine.ai/api/method/stripe_integration.stripe_integration.webhook.handle_webhook', data=payload_str, headers=headers)
    print('Webhook response:', res.status_code, res.text)
    
    # check ERPNext
    for _ in range(5):
        frappe.db.commit()
        status, out = frappe.db.get_value('Sales Invoice', invoice_name, ['status', 'outstanding_amount'])
        pe = frappe.db.count('Payment Entry', {'reference_no': 'pi_test_mock_123', 'docstatus': 1})
        print(f'Status: {status}, Out: {out}, PE: {pe}')
        if status == 'Paid': break
        time.sleep(2)
