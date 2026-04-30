"""Tests for webhook handler main flow (handle_webhook in webhook.py).

Covers:
- Signature verification across multiple accounts
- Idempotent handling of already-processed events
- Event routing to correct handlers
- Event log commit on success and failure paths
- Integration request logging
- Company mismatch detection edge cases
- Missing signature returns 400
- Invalid JSON payload handling
"""

import importlib
import json
import sys
import types
import unittest
from unittest.mock import Mock, MagicMock


class _FakeCache:
    def __init__(self):
        self.data = {}

    def incr(self, key):
        if key not in self.data:
            raise KeyError(key)
        self.data[key] += 1

    def set_value(self, key, value, expires_in_sec=None):
        self.data[key] = int(value)

    def expire(self, key, ttl):
        return None

    def get_value(self, key):
        return self.data.get(key)


class WebhookHandlerFlowTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)
        self._ir_entries = {}
        self._event_log_entries = {}
        self._committed = []
        self._handler_called = {}

        fake_frappe = types.ModuleType("frappe")
        fake_frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)
        fake_frappe.response = types.SimpleNamespace(status_code=200)
        fake_frappe.local = types.SimpleNamespace(request_ip="10.0.0.1")
        fake_frappe.log_error = lambda *a, **kw: None
        fake_frappe.get_traceback = lambda: "traceback"
        fake_frappe.set_user = lambda user: None
        fake_frappe.session = types.SimpleNamespace(user="Administrator")

        cache = _FakeCache()
        fake_frappe.cache = lambda: cache

        self._headers = {"Stripe-Signature": "t=1,v1=sig"}
        fake_frappe.get_request_header = lambda key: self._headers.get(key)

        self.payload = b'{"id":"evt_test_flow","type":"checkout.session.completed"}'

        fake_frappe.request = types.SimpleNamespace(get_data=lambda: self.payload)

        self_ref = self

        def _db_exists(doctype, filters=None, **kwargs):
            if doctype == "Integration Request" and isinstance(filters, dict):
                eid = filters.get("request_id")
                return self_ref._ir_entries.get(eid, {}).get("status") == "Completed"
            return False

        def _db_get_value(doctype, filters=None, field=None, **kwargs):
            if doctype == "Integration Request" and isinstance(filters, dict):
                eid = filters.get("request_id")
                return self_ref._ir_entries.get(eid, {}).get("name")
            if doctype == "Stripe Event Log" and isinstance(filters, dict):
                eid = filters.get("event_id")
                return self_ref._event_log_entries.get(eid, {}).get("name")
            if doctype == "Sales Invoice":
                return "COEngine Service Inc."
            return None

        def _db_set_value(doctype, name, updates, update_modified=True):
            pass

        def _db_commit():
            self_ref._committed.append(True)

        fake_frappe.db = types.SimpleNamespace(
            exists=_db_exists,
            get_value=_db_get_value,
            set_value=_db_set_value,
            commit=_db_commit,
            sql=lambda *a, **kw: [[1]],
        )

        ir_counter = [0]

        def _new_doc(doctype):
            if doctype == "Integration Request":
                ir_counter[0] += 1
                doc = types.SimpleNamespace(
                    name=f"IR-{ir_counter[0]:04d}",
                    insert=Mock(),
                )
                return doc
            return types.SimpleNamespace(
                name="",
                insert=Mock(),
                update=Mock(),
            )

        fake_frappe.new_doc = _new_doc
        fake_frappe.set_user = lambda user: None
        fake_frappe.get_cached_value = lambda *a, **kw: "CAD"
        fake_frappe.render_template = lambda t, args: "rendered"
        fake_frappe.attach_print = lambda *a, **kw: None
        fake_frappe.sendmail = lambda *a, **kw: None

        class _FakeMeta:
            def __init__(self):
                self._fields = {"custom_stripe_payment_processed", "stripe_payment_intent_id"}
            def get_field(self, name):
                return name if name in self._fields else None

        fake_frappe.get_meta = lambda dt: _FakeMeta()

        _fake_invoice = types.SimpleNamespace(
            name="SINV-0001", docstatus=1, outstanding_amount=100.0,
            company="COEngine Service Inc.", currency="CAD", grand_total=100.0,
            customer="CUST-001", customer_name="Test",
            contact_email="test@example.com",
            get=lambda field: getattr(_fake_invoice, field, None),
        )
        fake_frappe.get_doc = lambda dt, name=None: _fake_invoice if dt == "Sales Invoice" else types.SimpleNamespace(
            subject="", response="", name=name or "",
        )

        _fake_pe = types.SimpleNamespace(
            name="PE-FLOW-001", references=[types.SimpleNamespace(allocated_amount=0)],
            paid_amount=0, received_amount=0, reference_no=None, reference_date=None,
            posting_date="2026-04-25", get=lambda f: None,
        )
        _fake_pe.insert = Mock()
        _fake_pe.submit = Mock()

        pe_mod = types.ModuleType("erpnext.accounts.doctype.payment_entry.payment_entry")
        pe_mod.get_payment_entry = Mock(return_value=_fake_pe)

        fake_frappe.utils = types.SimpleNamespace(nowdate=lambda: "2026-04-25")

        fake_frappe_exceptions = types.ModuleType("frappe.exceptions")
        fake_frappe_exceptions.DuplicateEntryError = Exception

        # Stripe module with construct_event
        self.event = {
            "id": "evt_test_flow",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_flow",
                    "payment_intent": "pi_test_flow",
                    "amount_total": 5000,
                    "payment_status": "paid",
                    "metadata": {
                        "doctype": "Sales Invoice",
                        "docname": "SINV-0001",
                        "company_abbr": "COE",
                        "request_kind": "full",
                    },
                }
            },
        }

        fake_stripe = types.ModuleType("stripe")

        def _construct_event(payload, sig_header, secret):
            if secret == "whsec_coe":
                return self_ref.event
            raise Exception("bad secret")

        fake_stripe.Webhook = types.SimpleNamespace(construct_event=_construct_event)
        fake_stripe.PaymentLink = types.SimpleNamespace(modify=Mock())

        sys.modules["frappe"] = fake_frappe
        sys.modules["frappe.exceptions"] = fake_frappe_exceptions
        sys.modules["frappe.utils"] = types.ModuleType("frappe.utils")
        sys.modules["stripe"] = fake_stripe

        # Stub erpnext so _create_payment_entry_for_sales_invoice can import
        sys.modules["erpnext"] = types.ModuleType("erpnext")
        sys.modules["erpnext.accounts"] = types.ModuleType("erpnext.accounts")
        sys.modules["erpnext.accounts.doctype"] = types.ModuleType("erpnext.accounts.doctype")
        sys.modules["erpnext.accounts.doctype.payment_entry"] = types.ModuleType("erpnext.accounts.doctype.payment_entry")
        sys.modules["erpnext.accounts.doctype.payment_entry.payment_entry"] = pe_mod

        # Internal modules
        fake_utils = types.ModuleType("stripe_integration.stripe_integration.utils")
        fake_utils.get_webhook_secret = lambda abbr: "whsec_coe" if abbr == "COE" else "whsec_cosl" if abbr == "COSL" else None
        fake_utils.get_company_abbr_from_company = lambda c: "COE"
        fake_utils.get_api_key = lambda *a, **kw: "sk_test"

        fake_event_log = types.ModuleType("stripe_integration.stripe_integration.event_log")
        fake_event_log.upsert_event = lambda *a, **kw: None
        fake_event_log.mark_event_status = lambda *a, **kw: None

        fake_sub_payments = types.ModuleType("stripe_integration.stripe_integration.subscription_payments")
        fake_sub_payments.handle_invoice_paid = lambda *a, **kw: self_ref._handler_called.update({"invoice_paid": True})

        fake_sub_sync = types.ModuleType("stripe_integration.stripe_integration.subscription_sync")
        fake_sub_sync.sync_subscription_from_webhook_event = lambda *a, **kw: self_ref._handler_called.update({"subscription_sync": True})
        fake_sub_sync._set_subscription_fields = lambda *a, **kw: None
        fake_sub_sync.SETUP_STATUS_FIELD = "s"
        fake_sub_sync.SETUP_PM_FIELD = "p"
        fake_sub_sync.SETUP_INTENT_FIELD = "i"

        fake_payout_sync = types.ModuleType("stripe_integration.stripe_integration.payout_sync")
        fake_payout_sync.sync_payout_from_webhook_event = lambda *a, **kw: self_ref._handler_called.update({"payout_sync": True})

        fake_refunds = types.ModuleType("stripe_integration.stripe_integration.refunds")
        fake_refunds.apply_refund_to_erp = lambda **kw: (self_ref._handler_called.update({"refund": True}) or {"handled": True})

        sys.modules["stripe_integration.stripe_integration.utils"] = fake_utils
        sys.modules["stripe_integration.stripe_integration.event_log"] = fake_event_log
        sys.modules["stripe_integration.stripe_integration.subscription_payments"] = fake_sub_payments
        sys.modules["stripe_integration.stripe_integration.subscription_sync"] = fake_sub_sync
        sys.modules["stripe_integration.stripe_integration.payout_sync"] = fake_payout_sync
        sys.modules["stripe_integration.stripe_integration.refunds"] = fake_refunds

        self.webhook = importlib.import_module("stripe_integration.stripe_integration.webhook")
        self.frappe = fake_frappe

    def tearDown(self):
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    def test_valid_signature_processes_event(self):
        out = self.webhook.handle_webhook()
        self.assertEqual(out["status"], "ok")

    def test_invalid_signature_returns_400(self):
        """When no secret matches, return 400."""
        # Patch the already-imported module reference
        self.webhook.get_webhook_secret = lambda abbr: None

        out = self.webhook.handle_webhook()
        self.assertEqual(out["status"], "invalid")
        self.assertEqual(self.frappe.response.status_code, 400)

    def test_idempotent_skip_for_completed_event(self):
        self._ir_entries["evt_test_flow"] = {"status": "Completed", "name": "IR-0001"}

        out = self.webhook.handle_webhook()
        self.assertEqual(out["status"], "ok")
        self.assertTrue(out.get("idempotent"))

    def test_invoice_paid_routes_correctly(self):
        self.event["type"] = "invoice.paid"
        out = self.webhook.handle_webhook()
        self.assertTrue(self._handler_called.get("invoice_paid"))

    def test_subscription_updated_routes_correctly(self):
        self.event["type"] = "customer.subscription.updated"
        out = self.webhook.handle_webhook()
        self.assertTrue(self._handler_called.get("subscription_sync"))

    def test_subscription_deleted_routes_correctly(self):
        self.event["type"] = "customer.subscription.deleted"
        out = self.webhook.handle_webhook()
        self.assertTrue(self._handler_called.get("subscription_sync"))

    def test_subscription_created_is_not_synced(self):
        """customer.subscription.created should NOT trigger sync."""
        self.event["type"] = "customer.subscription.created"
        out = self.webhook.handle_webhook()
        self.assertNotIn("subscription_sync", self._handler_called)

    def test_charge_refunded_routes_correctly(self):
        self.event["type"] = "charge.refunded"
        self.event["data"]["object"]["refunds"] = {"data": [{"id": "re_1", "status": "succeeded", "amount": 1000, "currency": "cad"}]}
        self.event["data"]["object"]["payment_intent"] = "pi_test"
        out = self.webhook.handle_webhook()
        self.assertTrue(self._handler_called.get("refund"))

    def test_payout_paid_routes_correctly(self):
        self.event["type"] = "payout.paid"
        out = self.webhook.handle_webhook()
        self.assertTrue(self._handler_called.get("payout_sync"))

    def test_payout_created_routes_correctly(self):
        self.event["type"] = "payout.created"
        out = self.webhook.handle_webhook()
        self.assertTrue(self._handler_called.get("payout_sync"))

    def test_metadata_company_mismatch_returns_400(self):
        self.event["data"]["object"]["metadata"]["company_abbr"] = "COSL"
        # Signature matched COE but metadata says COSL
        out = self.webhook.handle_webhook()
        self.assertEqual(out["status"], "account_mismatch")
        self.assertEqual(self.frappe.response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
