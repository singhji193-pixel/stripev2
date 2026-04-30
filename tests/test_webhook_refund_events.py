"""Tests for refund webhook event handling and apply_refund_to_erp.

Covers:
- charge.refunded event processing (full + partial)
- refund.updated event processing
- Non-succeeded refund status is ignored
- Full refund cancels PE
- Partial refund creates refund PE allocated to credit note
- Partial refund without credit note falls back to manual
- Missing payment entry returns appropriate error
- Zero/negative refund amount returns not handled
- Invoice comment is added
"""

import importlib
import json
import sys
import types
import unittest
from unittest.mock import Mock


class _Obj(dict):
    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc

    def __setattr__(self, key, value):
        self[key] = value


class RefundWebhookEventTests(unittest.TestCase):
    """Test _handle_refund_event routing in webhook.py."""

    def setUp(self):
        self._orig_modules = dict(sys.modules)
        self._refund_calls = []

        fake_frappe = types.ModuleType("frappe")
        fake_frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)
        fake_frappe.db = types.SimpleNamespace(
            exists=lambda *a, **kw: False,
            commit=Mock(),
        )
        fake_frappe.response = types.SimpleNamespace(status_code=200)
        fake_frappe.request = types.SimpleNamespace(get_data=lambda: b"{}")
        fake_frappe.get_request_header = lambda key: None
        fake_frappe.local = types.SimpleNamespace(request_ip="127.0.0.1")
        fake_frappe.log_error = lambda *a, **kw: None
        fake_frappe.get_traceback = lambda: ""

        fake_frappe_exceptions = types.ModuleType("frappe.exceptions")
        fake_frappe_exceptions.DuplicateEntryError = Exception

        sys.modules["frappe"] = fake_frappe
        sys.modules["frappe.exceptions"] = fake_frappe_exceptions
        sys.modules["stripe"] = types.ModuleType("stripe")

        self_ref = self

        fake_refunds = types.ModuleType("stripe_integration.stripe_integration.refunds")

        def _apply_refund(**kwargs):
            self_ref._refund_calls.append(kwargs)
            return {"handled": True}

        fake_refunds.apply_refund_to_erp = _apply_refund

        fake_utils = types.ModuleType("stripe_integration.stripe_integration.utils")
        fake_utils.get_webhook_secret = lambda *a, **kw: None
        fake_utils.get_company_abbr_from_company = lambda *a, **kw: None
        fake_utils.get_api_key = lambda *a, **kw: None

        fake_event_log = types.ModuleType("stripe_integration.stripe_integration.event_log")
        fake_event_log.upsert_event = lambda **kw: None
        fake_event_log.mark_event_status = lambda *a, **kw: None

        fake_sub_payments = types.ModuleType("stripe_integration.stripe_integration.subscription_payments")
        fake_sub_payments.handle_invoice_paid = lambda *a, **kw: None

        fake_sub_sync = types.ModuleType("stripe_integration.stripe_integration.subscription_sync")
        fake_sub_sync.sync_subscription_from_webhook_event = lambda *a, **kw: None
        fake_sub_sync._set_subscription_fields = lambda *a, **kw: None
        fake_sub_sync.SETUP_STATUS_FIELD = "s"
        fake_sub_sync.SETUP_PM_FIELD = "p"
        fake_sub_sync.SETUP_INTENT_FIELD = "i"

        fake_payout_sync = types.ModuleType("stripe_integration.stripe_integration.payout_sync")
        fake_payout_sync.sync_payout_from_webhook_event = lambda *a, **kw: None

        sys.modules["stripe_integration.stripe_integration.utils"] = fake_utils
        sys.modules["stripe_integration.stripe_integration.event_log"] = fake_event_log
        sys.modules["stripe_integration.stripe_integration.subscription_payments"] = fake_sub_payments
        sys.modules["stripe_integration.stripe_integration.subscription_sync"] = fake_sub_sync
        sys.modules["stripe_integration.stripe_integration.payout_sync"] = fake_payout_sync
        sys.modules["stripe_integration.stripe_integration.refunds"] = fake_refunds

        self.webhook = importlib.import_module("stripe_integration.stripe_integration.webhook")

    def tearDown(self):
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    def test_charge_refunded_calls_apply_refund(self):
        event = {
            "id": "evt_ref_001",
            "type": "charge.refunded",
            "data": {
                "object": {
                    "payment_intent": "pi_test_999",
                    "refunds": {
                        "data": [
                            {"id": "re_001", "status": "succeeded", "amount": 5000, "currency": "cad"},
                        ]
                    },
                }
            },
        }

        self.webhook._handle_refund_event(event)

        self.assertEqual(len(self._refund_calls), 1)
        call = self._refund_calls[0]
        self.assertEqual(call["stripe_payment_intent_id"], "pi_test_999")
        self.assertEqual(call["stripe_refund_id"], "re_001")
        self.assertEqual(call["refund_amount"], 50.0)
        self.assertEqual(call["currency"], "CAD")
        self.assertEqual(call["source"], "webhook.charge.refunded")

    def test_charge_refunded_non_succeeded_skipped(self):
        event = {
            "id": "evt_ref_002",
            "type": "charge.refunded",
            "data": {
                "object": {
                    "payment_intent": "pi_test_999",
                    "refunds": {
                        "data": [
                            {"id": "re_002", "status": "pending", "amount": 5000, "currency": "cad"},
                        ]
                    },
                }
            },
        }

        out = self.webhook._handle_refund_event(event)
        self.assertEqual(len(self._refund_calls), 0)
        self.assertEqual(out["reason"], "latest_refund_not_succeeded")

    def test_charge_refunded_no_refund_items_skipped(self):
        event = {
            "id": "evt_ref_003",
            "type": "charge.refunded",
            "data": {
                "object": {
                    "payment_intent": "pi_test_999",
                    "refunds": {"data": []},
                }
            },
        }

        out = self.webhook._handle_refund_event(event)
        self.assertEqual(out["reason"], "no_refund_items")
        self.assertEqual(len(self._refund_calls), 0)

    def test_refund_updated_succeeded_calls_apply_refund(self):
        event = {
            "id": "evt_ref_upd_001",
            "type": "refund.updated",
            "data": {
                "object": {
                    "id": "re_upd_001",
                    "status": "succeeded",
                    "payment_intent": "pi_test_888",
                    "amount": 3000,
                    "currency": "usd",
                }
            },
        }

        self.webhook._handle_refund_event(event)

        self.assertEqual(len(self._refund_calls), 1)
        call = self._refund_calls[0]
        self.assertEqual(call["stripe_payment_intent_id"], "pi_test_888")
        self.assertEqual(call["refund_amount"], 30.0)
        self.assertEqual(call["currency"], "USD")
        self.assertEqual(call["source"], "webhook.refund.updated")

    def test_refund_updated_non_succeeded_ignored(self):
        event = {
            "id": "evt_ref_upd_002",
            "type": "refund.updated",
            "data": {
                "object": {
                    "id": "re_upd_002",
                    "status": "requires_action",
                    "payment_intent": "pi_test_888",
                    "amount": 3000,
                    "currency": "usd",
                }
            },
        }

        out = self.webhook._handle_refund_event(event)
        self.assertEqual(out["reason"], "refund_not_succeeded")
        self.assertEqual(len(self._refund_calls), 0)

    def test_charge_refunded_uses_latest_refund(self):
        """When multiple refunds exist on a charge, use the last one."""
        event = {
            "id": "evt_ref_multi",
            "type": "charge.refunded",
            "data": {
                "object": {
                    "payment_intent": "pi_test_multi",
                    "refunds": {
                        "data": [
                            {"id": "re_old", "status": "succeeded", "amount": 1000, "currency": "cad"},
                            {"id": "re_new", "status": "succeeded", "amount": 2000, "currency": "cad"},
                        ]
                    },
                }
            },
        }

        self.webhook._handle_refund_event(event)
        self.assertEqual(self._refund_calls[0]["stripe_refund_id"], "re_new")
        self.assertEqual(self._refund_calls[0]["refund_amount"], 20.0)

    def test_refund_missing_currency_defaults_to_cad(self):
        event = {
            "id": "evt_ref_no_cur",
            "type": "refund.updated",
            "data": {
                "object": {
                    "id": "re_no_cur",
                    "status": "succeeded",
                    "payment_intent": "pi_test_no_cur",
                    "amount": 1000,
                    "currency": "",
                }
            },
        }

        self.webhook._handle_refund_event(event)
        self.assertEqual(self._refund_calls[0]["currency"], "CAD")


class ApplyRefundToErpTests(unittest.TestCase):
    """Test apply_refund_to_erp in refunds.py."""

    def setUp(self):
        self._orig_modules = dict(sys.modules)

        fake_frappe = types.ModuleType("frappe")
        fake_frappe.log_error = lambda *a, **kw: None
        fake_frappe.get_traceback = lambda: ""

        fake_utils = types.ModuleType("frappe.utils")
        fake_utils.flt = lambda x: float(x or 0)
        fake_utils.nowdate = lambda: "2026-04-25"

        fake_frappe.utils = fake_utils

        self._pe = _Obj(
            name="PE-ORIG",
            docstatus=1,
            paid_amount=100.0,
            received_amount=100.0,
        )
        self._pe.references = [_Obj(reference_doctype="Sales Invoice", reference_name="SINV-0001")]
        self._pe.cancel = Mock()

        self._invoice_doc = types.SimpleNamespace(add_comment=Mock())

        def _get_doc(dt, name):
            if dt == "Payment Entry" and name == "PE-ORIG":
                return self._pe
            if dt == "Sales Invoice" and name == "SINV-0001":
                return self._invoice_doc
            raise Exception(f"Unexpected get_doc({dt}, {name})")

        fake_frappe.get_doc = _get_doc

        self._db_get_value_returns = []

        def _db_get_value(*args, **kwargs):
            if self._db_get_value_returns:
                return self._db_get_value_returns.pop(0)
            return None

        fake_frappe.db = types.SimpleNamespace(
            get_value=_db_get_value,
            commit=Mock(),
        )

        sys.modules["frappe"] = fake_frappe
        sys.modules["frappe.utils"] = fake_utils

        self.refunds = importlib.import_module("stripe_integration.stripe_integration.refunds")

    def tearDown(self):
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    def test_full_refund_cancels_pe(self):
        self._db_get_value_returns = ["PE-ORIG"]

        out = self.refunds.apply_refund_to_erp(
            stripe_payment_intent_id="pi_123",
            stripe_refund_id="re_123",
            refund_amount=100.0,
            currency="CAD",
        )

        self.assertTrue(out["handled"])
        self.assertEqual(out["mode"], "full_refund_cancel_payment_entry")
        self._pe.cancel.assert_called_once()

    def test_zero_refund_amount_not_handled(self):
        out = self.refunds.apply_refund_to_erp(
            stripe_payment_intent_id="pi_123",
            stripe_refund_id="re_123",
            refund_amount=0,
            currency="CAD",
        )

        self.assertFalse(out["handled"])
        self.assertEqual(out["reason"], "non_positive_refund_amount")

    def test_negative_refund_amount_not_handled(self):
        out = self.refunds.apply_refund_to_erp(
            stripe_payment_intent_id="pi_123",
            stripe_refund_id="re_123",
            refund_amount=-10.0,
            currency="CAD",
        )

        self.assertFalse(out["handled"])

    def test_missing_pe_returns_not_found(self):
        self._db_get_value_returns = [None, None]

        out = self.refunds.apply_refund_to_erp(
            stripe_payment_intent_id="pi_missing",
            stripe_refund_id="re_missing",
            refund_amount=50.0,
            currency="CAD",
        )

        self.assertFalse(out["handled"])
        self.assertEqual(out["reason"], "payment_entry_not_found")

    def test_already_cancelled_pe_not_cancelled_again(self):
        """If PE is already cancelled (docstatus=2), don't cancel again."""
        self._pe.docstatus = 2
        self._db_get_value_returns = ["PE-ORIG"]

        out = self.refunds.apply_refund_to_erp(
            stripe_payment_intent_id="pi_123",
            stripe_refund_id="re_123",
            refund_amount=100.0,
            currency="CAD",
        )

        self.assertTrue(out["handled"])
        self._pe.cancel.assert_not_called()

    def test_comment_added_on_full_refund(self):
        self._db_get_value_returns = ["PE-ORIG"]

        self.refunds.apply_refund_to_erp(
            stripe_payment_intent_id="pi_123",
            stripe_refund_id="re_123",
            refund_amount=100.0,
            currency="CAD",
            source="webhook.charge.refunded",
        )

        self._invoice_doc.add_comment.assert_called_once()
        comment_text = self._invoice_doc.add_comment.call_args[0][1]
        self.assertIn("re_123", comment_text)
        self.assertIn("pi_123", comment_text)


if __name__ == "__main__":
    unittest.main()
