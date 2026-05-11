import frappe
from frappe.utils import nowdate
from stripe_integration.stripe_integration.api import request_payment_stripe


def run_banjot_both_companies():
    tests = [
        {
            'company': 'COEngine Service Inc.',
            'taxes_and_charges': 'Canada HST 15% - COE',
            'cost_center': 'Main - COE',
            'debit_to': 'Debtors - COE',
            'income_account': 'Sales - COE',
            'label': 'COE',
        },
        {
            'company': 'CoreOrbit Systems Ltd.',
            'taxes_and_charges': 'GST 5% - COSL',
            'cost_center': 'Main - COSL',
            'debit_to': 'Debtors - COSL',
            'income_account': 'Sales - COSL',
            'label': 'COSL',
        },
    ]

    out = []
    for t in tests:
        si = frappe.get_doc({
            'doctype': 'Sales Invoice',
            'company': t['company'],
            'customer': 'Banjot Judge',
            'posting_date': nowdate(),
            'due_date': nowdate(),
            'debit_to': t['debit_to'],
            'cost_center': t['cost_center'],
            'currency': 'CAD',
            'taxes_and_charges': t['taxes_and_charges'],
            'set_posting_time': 1,
            'items': [{
                'item_code': 'ADSPEND',
                'qty': 1,
                'rate': 1.00,
                'cost_center': t['cost_center'],
                'income_account': t['income_account'],
            }],
        })
        si.insert(ignore_permissions=True)
        si.submit()

        req = request_payment_stripe(si.name)
        inv = frappe.get_doc('Sales Invoice', si.name)

        out.append({
            'company': t['label'],
            'invoice': inv.name,
            'status': inv.status,
            'grand_total': float(inv.grand_total or 0),
            'outstanding': float(inv.outstanding_amount or 0),
            'stripe_invoice_id': inv.stripe_invoice_id,
            'stripe_payment_intent_id': inv.stripe_payment_intent_id,
            'stripe_checkout_session_id': inv.stripe_checkout_session_id,
            'has_checkout_url': bool(inv.stripe_checkout_url),
            'checkout_url': inv.stripe_checkout_url,
            'request_ok': req.get('ok') if isinstance(req, dict) else None,
            'email_sent': req.get('email_sent') if isinstance(req, dict) else None,
        })

    frappe.db.commit()
    return out
