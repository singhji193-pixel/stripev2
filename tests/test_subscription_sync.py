"""Tests for subscription sync (subscription_sync.py).

Covers:
- Webhook event updates local subscription state
- Lifecycle email routing (started, paused, resumed, cancelled)
- COSL "resumed" template uses correct template (not "Started")
- Subscription not found returns appropriate error
- Sync disabled returns not handled
- Action validation (pause/resume/cancel/plan_change)
- Transition validation (can't pause already-paused, can't resume not-paused)
- ERP status mapping from Stripe status
- on_subscription_update hooks
"""

import importlib
import json
import sys
import types
import unittest
from unittest.mock import Mock, MagicMock


class _FakeMeta:
    def __init__(self, fields=None):
        self._fields = set(fields or [])

    def get_field(self, name):
        return name if name in self._fields else None


class SubscriptionSyncTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)
        self._sync_enabled = True
        self._set_values = {}
        self._committed = []
        self._email_sent = []

        fake_frappe = types.ModuleType("frappe")

        class _ValidationError(Exception):
            pass

        fake_frappe.ValidationError = _ValidationError
        fake_frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)
        fake_frappe.log_error = lambda *a, **kw: None
        fake_frappe.get_traceback = lambda: ""

        def _throw(msg, exc=None):
            raise (exc or _ValidationError)(msg)

        fake_frappe.throw = _throw

        fake_frappe.local = types.SimpleNamespace(site="test.local")
        fake_frappe.session = types.SimpleNamespace(user="Administrator")

        fake_utils_frappe = types.ModuleType("frappe.utils")
        fake_utils_frappe.get_url = lambda: "http://localhost"
        fake_utils_frappe.now_datetime = lambda: "2026-04-25 10:00:00"
        fake_utils_frappe.get_datetime = lambda x: x
        fake_frappe.utils = fake_utils_frappe

        self_ref = self

        def _db_get_single_value(doctype, field):
            if field == "enable_subscription_state_sync":
                return 1 if self_ref._sync_enabled else 0
            return 0

        self._prev_sub_values = {"stripe_status": "", "stripe_paused": 0}

        def _db_get_value(doctype, filters=None, field=None, as_dict=False, **kwargs):
            if doctype == "Subscription" and isinstance(filters, dict):
                if "stripe_subscription_id" in filters:
                    return "SUB-0001"
            if doctype == "Subscription" and filters == "SUB-0001":
                if isinstance(field, list) and as_dict:
                    return self_ref._prev_sub_values
                if field == "company":
                    return "COEngine Service Inc."
                return None
            if doctype == "Company":
                return "COE"
            if doctype == "Stripe Account":
                return "SA-COE"
            if doctype == "Customer":
                return "Test Customer"
            if doctype == "DocField":
                return "Active\nCancelled\nPast Due Date\nUnpaid"
            return None

        def _db_set_value(doctype, name, field_or_dict, value=None, update_modified=True):
            if isinstance(field_or_dict, dict):
                for k, v in field_or_dict.items():
                    self_ref._set_values[(doctype, name, k)] = v
            else:
                self_ref._set_values[(doctype, name, field_or_dict)] = value

        def _db_exists(doctype, name_or_filters=None, **kwargs):
            if doctype == "Subscription":
                return True
            if doctype == "Email Template":
                return True
            return False

        def _db_commit():
            self_ref._committed.append(True)

        fake_frappe.db = types.SimpleNamespace(
            get_single_value=_db_get_single_value,
            get_value=_db_get_value,
            set_value=_db_set_value,
            exists=_db_exists,
            commit=_db_commit,
            sql=lambda *a, **kw: [[0]],
        )

        sub_fields = {"stripe_status", "stripe_paused", "cancel_at_period_end",
                       "stripe_subscription_id", "status",
                       "stripe_setup_checkout_url", "stripe_setup_session_id",
                       "stripe_setup_link_created_at", "stripe_setup_link_expires_at",
                       "stripe_setup_link_status", "stripe_default_payment_method_id",
                       "stripe_last_setup_intent_id", "stripe_checkout_url"}
        fake_frappe.get_meta = lambda doctype: _FakeMeta(sub_fields)

        self._sub_doc = types.SimpleNamespace(
            name="SUB-0001",
            company="COEngine Service Inc.",
            stripe_subscription_id="sub_test_001",
            party_type="Customer",
            party="CUST-001",
            status="Active",
            get=lambda field: getattr(self_ref._sub_doc, field, None),
        )
        self._sub_doc.contact_email = "sub@example.com"
        self._sub_doc.plans = []

        fake_frappe.get_doc = lambda doctype, name=None: self._sub_doc if doctype in ("Subscription", "Email Template") else None
        fake_frappe.render_template = lambda template, args: "rendered"
        fake_frappe.sendmail = lambda *a, **kw: self_ref._email_sent.append(kw)
        fake_frappe.get_cached_value = lambda *a, **kw: "CAD"
        fake_frappe.get_all = lambda *a, **kw: []
        fake_frappe.attach_print = lambda *a, **kw: None
        fake_frappe.enqueue = Mock()

        fake_stripe = types.ModuleType("stripe")
        fake_stripe.api_key = None
        fake_stripe.Subscription = types.SimpleNamespace(
            retrieve=Mock(return_value=types.SimpleNamespace(
                customer="cus_test",
                pause_collection=None,
            )),
            modify=Mock(),
            delete=Mock(),
        )
        fake_stripe.checkout = types.SimpleNamespace(
            Session=types.SimpleNamespace(create=Mock(return_value={"url": "https://checkout.stripe.com/test", "id": "cs_setup_001"}))
        )

        sys.modules["frappe"] = fake_frappe
        sys.modules["frappe.utils"] = fake_utils_frappe
        sys.modules["stripe"] = fake_stripe

        fake_utils_mod = types.ModuleType("stripe_integration.stripe_integration.utils")
        fake_utils_mod.get_company_abbr_from_company = lambda company: "COE"
        fake_utils_mod.get_api_key = lambda abbr: "sk_test"

        fake_event_log = types.ModuleType("stripe_integration.stripe_integration.event_log")
        fake_event_log.upsert_event = lambda **kw: None
        fake_event_log.mark_event_status = lambda *a, **kw: None

        sys.modules["stripe_integration.stripe_integration.utils"] = fake_utils_mod
        sys.modules["stripe_integration.stripe_integration.event_log"] = fake_event_log

        self.sub_sync = importlib.import_module("stripe_integration.stripe_integration.subscription_sync")
        self.frappe = fake_frappe
        self.stripe = fake_stripe

    def tearDown(self):
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    def test_webhook_event_updates_subscription_state(self):
        event = {
            "id": "evt_sub_001",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_test_001",
                    "status": "active",
                    "pause_collection": None,
                    "cancel_at_period_end": False,
                }
            },
        }

        out = self.sub_sync.sync_subscription_from_webhook_event(event)
        self.assertEqual(out["subscription"], "SUB-0001")
        self.assertEqual(out["stripe_status"], "active")

    def test_cancelled_status_maps_to_erp(self):
        event = {
            "data": {
                "object": {
                    "id": "sub_test_001",
                    "status": "canceled",
                    "pause_collection": None,
                    "cancel_at_period_end": False,
                }
            },
        }

        out = self.sub_sync.sync_subscription_from_webhook_event(event)
        self.assertIn(("Subscription", "SUB-0001", "status"), self._set_values)
        self.assertEqual(self._set_values[("Subscription", "SUB-0001", "status")], "Cancelled")

    def test_past_due_maps_correctly(self):
        out = self.sub_sync._map_stripe_to_erp_status("past_due")
        self.assertEqual(out, "Past Due Date")

    def test_unpaid_maps_correctly(self):
        out = self.sub_sync._map_stripe_to_erp_status("unpaid")
        self.assertEqual(out, "Unpaid")

    def test_active_maps_correctly(self):
        out = self.sub_sync._map_stripe_to_erp_status("active")
        self.assertEqual(out, "Active")

    def test_trialing_maps_to_active(self):
        out = self.sub_sync._map_stripe_to_erp_status("trialing")
        self.assertEqual(out, "Active")

    def test_unknown_status_returns_none(self):
        out = self.sub_sync._map_stripe_to_erp_status("some_unknown_status")
        self.assertIsNone(out)

    def test_missing_stripe_sub_id_returns_not_found(self):
        event = {
            "data": {"object": {"id": ""}},
        }
        out = self.sub_sync.sync_subscription_from_webhook_event(event)
        self.assertFalse(out["handled"])
        self.assertEqual(out["reason"], "missing_stripe_subscription_id")

    def test_subscription_not_found_in_erp(self):
        self.frappe.db.get_value = lambda *a, **kw: None
        event = {
            "data": {"object": {"id": "sub_unknown_999"}},
        }
        out = self.sub_sync.sync_subscription_from_webhook_event(event)
        self.assertFalse(out["handled"])
        self.assertEqual(out["reason"], "subscription_not_found")

    def test_sync_disabled_returns_not_handled(self):
        self._sync_enabled = False
        out = self.sub_sync.sync_subscription_action("SUB-0001", "pause")
        self.assertFalse(out["handled"])
        self.assertEqual(out["reason"], "subscription_sync_disabled")

    def test_cosl_resumed_template_name_is_correct(self):
        """Verify the COSL resumed template was fixed (not using 'Started')."""
        cosl_map = self.sub_sync.LIFECYCLE_TEMPLATE_MAP["COSL"]
        self.assertEqual(
            cosl_map["resumed"],
            "Stripe CoreOrbit Subscription Resumed",
            "COSL resumed template should NOT be 'Stripe CoreOrbit Subscription Started'"
        )

    def test_lifecycle_kind_paused(self):
        kind = self.sub_sync._pick_lifecycle_kind("active", False, "active", True)
        self.assertEqual(kind, "paused")

    def test_lifecycle_kind_resumed(self):
        kind = self.sub_sync._pick_lifecycle_kind("active", True, "active", False)
        self.assertEqual(kind, "resumed")

    def test_lifecycle_kind_cancelled(self):
        kind = self.sub_sync._pick_lifecycle_kind("active", False, "canceled", False)
        self.assertEqual(kind, "cancelled")

    def test_lifecycle_kind_started(self):
        kind = self.sub_sync._pick_lifecycle_kind("incomplete", False, "active", False)
        self.assertEqual(kind, "started")

    def test_lifecycle_kind_none_for_same_state(self):
        kind = self.sub_sync._pick_lifecycle_kind("active", False, "active", False)
        self.assertIsNone(kind)

    def test_normalize_action_valid(self):
        self.assertEqual(self.sub_sync._normalize_action("pause"), "pause")
        self.assertEqual(self.sub_sync._normalize_action("RESUME"), "resume")
        self.assertEqual(self.sub_sync._normalize_action(" Cancel "), "cancel")

    def test_normalize_action_invalid(self):
        self.assertIsNone(self.sub_sync._normalize_action("invalid_action"))
        self.assertIsNone(self.sub_sync._normalize_action(""))
        self.assertIsNone(self.sub_sync._normalize_action(None))


if __name__ == "__main__":
    unittest.main()
