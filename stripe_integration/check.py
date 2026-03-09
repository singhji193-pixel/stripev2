import time, frappe, stripe
from stripe_integration.stripe_integration.utils import get_company_abbr_from_company, get_api_key

def run():
    invoice_name = 'ACC-SINV-2026-00139'
    si = frappe.get_doc('Sales Invoice', invoice_name)
    stripe.api_key = get_api_key(get_company_abbr_from_company(si.company))
    
    sess = stripe.checkout.Session.retrieve('cs_test_a1poFiL56pF2SkA62WatSIL05W1TzeIxgsuxhUyBNTGvAQ4MfujeSCC9GY', expand=['payment_intent'])
    pi = sess.payment_intent
    pi_id = pi.id if hasattr(pi, 'id') else pi
    print('PI:', pi_id, 'Status:', sess.status)
    
    if sess.status != 'complete':
        try:
            stripe.test_helpers.PaymentIntent.confirm(pi_id, payment_method='pm_card_visa')
            print('Confirmed PI via Test Helpers')
        except Exception as e:
            print('Failed to confirm:', e)
    
    for _ in range(15):
        frappe.db.commit()
        status, out = frappe.db.get_value('Sales Invoice', invoice_name, ['status', 'outstanding_amount'])
        pe = frappe.db.count('Payment Entry', {'reference_no': pi_id, 'docstatus': 1})
        print(f'Status: {status}, Out: {out}, PE: {pe}')
        if status == 'Paid': break
        time.sleep(2)
