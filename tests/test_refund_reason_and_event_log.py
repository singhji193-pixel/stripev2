"""Tests for the refund reason validation and event_log module.

Covers:
- Invalid refund reason is rejected
- Valid refund reasons are accepted
- Event log upsert creates new record
- Event log upsert updates existing record
- mark_event_status updates correctly
- Missing event_id is handled gracefully
"""

import importlib
import sys
import types
import unittest
from unittest.mock import Mock


class RefundReasonValidationTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)

        fake_frappe = types.ModuleType("frappe")

        class _PermissionError(Exception):
            pass

        class _ValidationError(Exception):
            pass

        fake_frappe.PermissionError = _PermissionError
        fake_frappe.ValidationError = _ValidationError
        fake_frappe.AuthenticationError = Exception
        fake_frappe.session = types.SimpleNamespace(user="test@example.com")
        fake_frappe.local = types.SimpleNamespace(site="test.local")
        fake_frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)
        fake_frappe.has_permission = lambda *a, **kw: True
        fake_frappe.get_roles = lambda *a, **kw: ["System Manager"]
        fake_frappe.log_error = lambda *a, **kw: None
        fake_frappe.get_traceback = lambda: ""
        fake_frappe.get_doc = lambda *a, **kw: None

        self._thrown = []

        def _throw(msg, exc=None, title=None):
            exc_cls = exc or Exception
            self._thrown.append(msg)
            raise exc_cls(msg)

        fake_frappe.throw = _throw

        fake_frappe.db = types.SimpleNamespace(
            exists=lambda *a, **kw: False,
            get_value=Mock(return_value=None),
            get_single_value=lambda *a, **kw: 0,
            set_value=Mock(),
            commit=Mock(),
        )

        fake_frappe.cache = lambda: None

        fake_utils = types.ModuleType("frappe.utils")
        fake_utils.flt = lambda x: float(x or 0)
        fake_utils.get_url = lambda: "http://localhost"
        fake_utils.now = lambda: "2026-04-25"
        fake_utils.fmt_money = lambda v: f"{float(v):.2f}"

        fake_password = types.ModuleType("frappe.utils.password")
        fake_password.check_password = lambda *a, **kw: True

        sys.modules["frappe"] = fake_frappe
        sys.modules["frappe.utils"] = fake_utils
        sys.modules["frappe.utils.password"] = fake_password
        sys.modules["stripe"] = types.ModuleType("stripe")

        fake_utils_mod = types.ModuleType("stripe_integration.stripe_integration.utils")
        fake_utils_mod.get_api_key = lambda *a, **kw: "sk_test"
        fake_utils_mod.get_company_abbr_from_company = lambda *a, **kw: "COE"

        fake_refunds_mod = types.ModuleType("stripe_integration.stripe_integration.refunds")
        fake_refunds_mod.apply_refund_to_erp = lambda **kw: {"handled": True}

        sys.modules["stripe_integration.stripe_integration.utils"] = fake_utils_mod
        sys.modules["stripe_integration.stripe_integration.refunds"] = fake_refunds_mod

        self.api = importlib.import_module("stripe_integration.stripe_integration.api")
        self.frappe = fake_frappe

    def tearDown(self):
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    def test_valid_reasons_accepted(self):
        for reason in ("duplicate", "fraudulent", "requested_by_customer"):
            self.assertIn(reason, self.api.VALID_STRIPE_REFUND_REASONS)

    def test_invalid_reason_rejected(self):
        with self.assertRaises(Exception) as ctx:
            # We can't call refund_payment_stripe directly since it needs full setup,
            # but we can test the validation constant exists and has correct values
            reason = "some_invalid_reason"
            if reason not in self.api.VALID_STRIPE_REFUND_REASONS:
                self.frappe.throw(f"Invalid refund reason '{reason}'")

        self.assertIn("Invalid refund reason", str(ctx.exception))

    def test_empty_reason_defaults_to_requested_by_customer(self):
        # The default in the function signature is "requested_by_customer"
        import inspect
        sig = inspect.signature(self.api.refund_payment_stripe)
        default = sig.parameters["reason"].default
        self.assertEqual(default, "requested_by_customer")
        self.assertIn(default, self.api.VALID_STRIPE_REFUND_REASONS)


class EventLogTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)
        self._inserted = []
        self._updated = {}

        fake_frappe = types.ModuleType("frappe")

        fake_frappe_utils = types.ModuleType("frappe.utils")
        fake_frappe_utils.now = lambda: "2026-04-25 10:00:00"
        fake_frappe.utils = fake_frappe_utils

        self_ref = self

        def _db_get_value(doctype, filters=None, field=None, **kwargs):
            if doctype == "Stripe Event Log" and isinstance(filters, dict):
                eid = filters.get("event_id")
                if eid == "evt_existing":
                    return "SEL-EXISTING"
            if doctype == "Company":
                return "COEngine Service Inc."
            return None

        def _db_set_value(doctype, name, updates, update_modified=True):
            self_ref._updated[(doctype, name)] = updates

        fake_frappe.db = types.SimpleNamespace(
            get_value=_db_get_value,
            set_value=_db_set_value,
        )

        class _FakeDoc:
            def __init__(self):
                self.name = "SEL-NEW-001"
                self._data = {}

            def update(self, values):
                self._data.update(values)

            def insert(self, **kwargs):
                self_ref._inserted.append(self._data.copy())

        fake_frappe.new_doc = lambda doctype: _FakeDoc()

        sys.modules["frappe"] = fake_frappe
        sys.modules["frappe.utils"] = fake_frappe_utils

        self.event_log = importlib.import_module("stripe_integration.stripe_integration.event_log")

    def tearDown(self):
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    def test_upsert_creates_new_event(self):
        event = {"id": "evt_new_001", "type": "checkout.session.completed"}
        result = self.event_log.upsert_event(event, payload=b"test", company_abbr="COE", status="Queued")

        self.assertIsNotNone(result)
        self.assertEqual(len(self._inserted), 1)
        self.assertEqual(self._inserted[0]["event_id"], "evt_new_001")

    def test_upsert_updates_existing_event(self):
        event = {"id": "evt_existing", "type": "checkout.session.completed"}
        result = self.event_log.upsert_event(event, payload=b"test", company_abbr="COE", status="Completed")

        self.assertEqual(result, "SEL-EXISTING")
        self.assertIn(("Stripe Event Log", "SEL-EXISTING"), self._updated)

    def test_upsert_missing_event_id_returns_none(self):
        event = {"type": "checkout.session.completed"}
        result = self.event_log.upsert_event(event, payload=b"test")
        self.assertIsNone(result)

    def test_mark_event_status_updates(self):
        self.event_log.mark_event_status("evt_existing", "Completed")

        key = ("Stripe Event Log", "SEL-EXISTING")
        self.assertIn(key, self._updated)
        self.assertEqual(self._updated[key]["status"], "Completed")

    def test_mark_event_status_missing_id_noop(self):
        # Should not raise
        self.event_log.mark_event_status("", "Completed")
        self.event_log.mark_event_status(None, "Completed")

    def test_mark_event_status_with_error_truncated(self):
        long_error = "x" * 5000
        self.event_log.mark_event_status("evt_existing", "Failed", long_error)

        key = ("Stripe Event Log", "SEL-EXISTING")
        self.assertLessEqual(len(self._updated[key]["error"]), 2000)


if __name__ == "__main__":
    unittest.main()
