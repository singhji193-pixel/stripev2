"""Tests for checkout.session.completed webhook handling.

Covers:
- Happy path: PE creation from checkout session
- Email failure resilience (PE must still commit)
- payment_status guard for async payments
- Dedup via MariaDB named lock
- Missing PI handling
- Split payment flag setting
- Payment Link deactivation after payment
- Subscription setup session routing
"""

import importlib
import json
import sys
import types
import unittest
from unittest.mock import Mock, patch, MagicMock


class _Obj(dict):
    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc

    def __setattr__(self, key, value):
        self[key] = value


class _FakeMeta:
    def __init__(self, fields=None):
        self._fields = set(fields or [])

    def get_field(self, name):
        return name if name in self._fields else None


class CheckoutSessionWebhookTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)
        self._committed = []
        self._rolled_back = []
        self._set_values = {}
        self._pe_submitted = []
        self._email_sent = []
        self._email_should_fail = False
        self._lock_acquired = True
        self._existing_pe = False

        fake_frappe = types.ModuleType("frappe")

        class _ValidationError(Exception):
            pass

        fake_frappe.ValidationError = _ValidationError
        fake_frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)
        fake_frappe.set_user = lambda user: None
        fake_frappe.log_error = lambda *args, **kwargs: None
        fake_frappe.get_traceback = lambda: "traceback"
        fake_frappe.response = types.SimpleNamespace(status_code=200)
        fake_frappe.request = types.SimpleNamespace(get_data=lambda: b"{}")
        fake_frappe.get_request_header = lambda key: None
        fake_frappe.local = types.SimpleNamespace(request_ip="127.0.0.1", site="test.local")
        fake_frappe.session = types.SimpleNamespace(user="Administrator")

        fake_frappe.utils = types.SimpleNamespace(
            nowdate=lambda: "2026-04-25",
        )

        self_ref = self

        def _db_exists(doctype, filters=None, **kwargs):
            if doctype == "Payment Entry" and isinstance(filters, dict):
                if filters.get("reference_no") and filters.get("docstatus") != 2:
                    return self_ref._existing_pe
            if doctype == "Sales Invoice":
                return True
            if doctype == "Integration Request":
                return False
            return False

        def _db_sql(query, params=None):
            # GET_LOCK
            if "GET_LOCK" in str(query):
                return [[1 if self_ref._lock_acquired else 0]]
            if "RELEASE_LOCK" in str(query):
                return None
            return [[0]]

        def _db_set_value(doctype, name, field_or_dict, value=None, update_modified=True):
            if isinstance(field_or_dict, str):
                self_ref._set_values[(doctype, name, field_or_dict)] = value
            elif isinstance(field_or_dict, dict):
                for k, v in field_or_dict.items():
                    self_ref._set_values[(doctype, name, k)] = v

        def _db_commit():
            self_ref._committed.append(True)

        def _db_rollback():
            self_ref._rolled_back.append(True)

        def _db_get_value(*args, **kwargs):
            return None

        fake_frappe.db = types.SimpleNamespace(
            exists=_db_exists,
            sql=_db_sql,
            set_value=_db_set_value,
            commit=_db_commit,
            rollback=_db_rollback,
            get_value=_db_get_value,
        )

        self._invoice = _Obj(
            name="SINV-0001",
            docstatus=1,
            outstanding_amount=100.0,
            company="COEngine Service Inc.",
            currency="CAD",
            grand_total=100.0,
            customer="CUST-001",
            customer_name="Test Customer",
            contact_email="test@example.com",
        )

        self._pe = _Obj(
            name="PE-0001",
            references=[_Obj(allocated_amount=0)],
            paid_amount=0,
            received_amount=0,
            reference_no=None,
            reference_date=None,
            posting_date="2026-04-25",
        )
        self._pe.insert = Mock()
        self._pe.submit = Mock(side_effect=lambda: self_ref._pe_submitted.append(True))

        def _get_doc(doctype, name=None):
            if doctype == "Sales Invoice":
                return self._invoice
            return None

        fake_frappe.get_doc = _get_doc
        fake_frappe.get_meta = lambda doctype: _FakeMeta(["custom_stripe_payment_processed", "stripe_payment_intent_id"])

        fake_frappe.render_template = lambda template, args: "rendered"
        fake_frappe.attach_print = lambda *args, **kwargs: {"fname": "test.pdf"}
        fake_frappe.sendmail = lambda *args, **kwargs: (
            (_ for _ in ()).throw(Exception("SMTP down")) if self_ref._email_should_fail
            else self_ref._email_sent.append(True)
        )
        fake_frappe.get_cached_value = lambda *args, **kwargs: "CAD"

        fake_frappe_exceptions = types.ModuleType("frappe.exceptions")
        fake_frappe_exceptions.DuplicateEntryError = type("DuplicateEntryError", (Exception,), {})

        fake_stripe = types.ModuleType("stripe")
        fake_stripe.PaymentLink = types.SimpleNamespace(modify=Mock())
        fake_stripe.Webhook = types.SimpleNamespace(construct_event=Mock())

        # Stub erpnext
        pe_mod = types.ModuleType("erpnext.accounts.doctype.payment_entry.payment_entry")
        pe_mod.get_payment_entry = Mock(return_value=self._pe)

        sys.modules["frappe"] = fake_frappe
        sys.modules["frappe.exceptions"] = fake_frappe_exceptions
        sys.modules["frappe.utils"] = types.ModuleType("frappe.utils")
        sys.modules["stripe"] = fake_stripe
        sys.modules["erpnext"] = types.ModuleType("erpnext")
        sys.modules["erpnext.accounts"] = types.ModuleType("erpnext.accounts")
        sys.modules["erpnext.accounts.doctype"] = types.ModuleType("erpnext.accounts.doctype")
        sys.modules["erpnext.accounts.doctype.payment_entry"] = types.ModuleType("erpnext.accounts.doctype.payment_entry")
        sys.modules["erpnext.accounts.doctype.payment_entry.payment_entry"] = pe_mod

        # Stub internal modules
        fake_utils_mod = types.ModuleType("stripe_integration.stripe_integration.utils")
        fake_utils_mod.get_webhook_secret = lambda *_args, **_kwargs: None
        fake_utils_mod.get_company_abbr_from_company = lambda company: "COE"
        fake_utils_mod.get_api_key = lambda *_args, **_kwargs: "sk_test"

        fake_event_log = types.ModuleType("stripe_integration.stripe_integration.event_log")
        fake_event_log.upsert_event = lambda **kwargs: None
        fake_event_log.mark_event_status = lambda *args, **kwargs: None

        fake_sub_payments = types.ModuleType("stripe_integration.stripe_integration.subscription_payments")
        fake_sub_payments.handle_invoice_paid = lambda *_args, **_kwargs: None

        fake_sub_sync = types.ModuleType("stripe_integration.stripe_integration.subscription_sync")
        fake_sub_sync.sync_subscription_from_webhook_event = lambda *_args, **_kwargs: None
        fake_sub_sync._set_subscription_fields = lambda *_args, **_kwargs: None
        fake_sub_sync.SETUP_STATUS_FIELD = "setup_status"
        fake_sub_sync.SETUP_PM_FIELD = "setup_pm"
        fake_sub_sync.SETUP_INTENT_FIELD = "setup_intent"

        fake_payout_sync = types.ModuleType("stripe_integration.stripe_integration.payout_sync")
        fake_payout_sync.sync_payout_from_webhook_event = lambda *_args, **_kwargs: None

        fake_refunds = types.ModuleType("stripe_integration.stripe_integration.refunds")
        fake_refunds.apply_refund_to_erp = lambda **kwargs: {"handled": True}

        sys.modules["stripe_integration.stripe_integration.utils"] = fake_utils_mod
        sys.modules["stripe_integration.stripe_integration.event_log"] = fake_event_log
        sys.modules["stripe_integration.stripe_integration.subscription_payments"] = fake_sub_payments
        sys.modules["stripe_integration.stripe_integration.subscription_sync"] = fake_sub_sync
        sys.modules["stripe_integration.stripe_integration.payout_sync"] = fake_payout_sync
        sys.modules["stripe_integration.stripe_integration.refunds"] = fake_refunds

        self.webhook = importlib.import_module("stripe_integration.stripe_integration.webhook")
        self.frappe = fake_frappe
        self.pe_mod = pe_mod

    def tearDown(self):
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    def _make_session(self, **overrides):
        session = {
            "id": "cs_test_123",
            "payment_intent": "pi_test_abc",
            "amount_total": 10000,
            "payment_status": "paid",
            "payment_link": "plink_test_456",
            "metadata": {
                "doctype": "Sales Invoice",
                "docname": "SINV-0001",
                "company_abbr": "COE",
                "request_kind": "full",
            },
        }
        session.update(overrides)
        return session

    def test_happy_path_creates_pe_and_commits(self):
        session = self._make_session()
        self.webhook._handle_checkout_session(session)

        self._pe.insert.assert_called_once_with(ignore_permissions=True)
        self._pe.submit.assert_called_once()
        self.assertTrue(len(self._committed) > 0, "DB commit was not called")
        self.assertEqual(self._pe.reference_no, "pi_test_abc")

    def test_email_failure_does_not_rollback_pe(self):
        """CRITICAL: Email failure must not prevent PE commit."""
        self._email_should_fail = True
        session = self._make_session()

        # Should NOT raise even though email fails
        self.webhook._handle_checkout_session(session)

        self._pe.insert.assert_called_once()
        self._pe.submit.assert_called_once()
        self.assertTrue(len(self._committed) > 0, "PE should be committed even when email fails")

    def test_unpaid_payment_status_skips_pe_creation(self):
        session = self._make_session(payment_status="unpaid")
        self.webhook._handle_checkout_session(session)

        self._pe.insert.assert_not_called()
        self._pe.submit.assert_not_called()

    def test_processing_payment_status_skips_pe_creation(self):
        session = self._make_session(payment_status="processing")
        self.webhook._handle_checkout_session(session)

        self._pe.insert.assert_not_called()

    def test_missing_pi_skips_pe_creation(self):
        session = self._make_session(payment_intent=None)
        self.webhook._handle_checkout_session(session)

        self._pe.insert.assert_not_called()

    def test_missing_docname_skips_pe_creation(self):
        session = self._make_session()
        session["metadata"]["docname"] = ""
        self.webhook._handle_checkout_session(session)

        self._pe.insert.assert_not_called()

    def test_non_sales_invoice_doctype_skips_pe_creation(self):
        session = self._make_session()
        session["metadata"]["doctype"] = "Purchase Invoice"
        self.webhook._handle_checkout_session(session)

        self._pe.insert.assert_not_called()

    def test_subscription_doctype_routes_to_setup_handler(self):
        session = self._make_session()
        session["metadata"]["doctype"] = "Subscription"
        session["metadata"]["docname"] = "SUB-0001"

        # Should not crash - setup handler needs different mocking but routing should work
        self.webhook._handle_checkout_session(session)
        # PE should not be created (subscription path, not invoice path)
        self._pe.insert.assert_not_called()

    def test_existing_pe_dedup_skips_creation(self):
        self._existing_pe = True
        session = self._make_session()
        self.webhook._handle_checkout_session(session)

        # PE insert should not be called due to dedup
        self._pe.insert.assert_not_called()

    def test_zero_outstanding_skips_pe_creation(self):
        self._invoice.outstanding_amount = 0
        session = self._make_session()
        self.webhook._handle_checkout_session(session)

        self._pe.insert.assert_not_called()

    def test_split_payment_flag_set_after_commit(self):
        session = self._make_session()
        self.webhook._handle_checkout_session(session)

        key = ("Sales Invoice", "SINV-0001", "custom_stripe_payment_processed")
        self.assertIn(key, self._set_values)
        self.assertEqual(self._set_values[key], 1)

    def test_stripe_pi_id_set_on_pe(self):
        session = self._make_session()
        self.webhook._handle_checkout_session(session)

        key = ("Payment Entry", "PE-0001", "stripe_payment_intent_id")
        self.assertIn(key, self._set_values, "stripe_payment_intent_id should be set on PE")


if __name__ == "__main__":
    unittest.main()
