import importlib
import json
import sys
import types
import unittest

from module_isolation import restore_modules


class WebhookPayloadSanitizationTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)

        fake_frappe = types.ModuleType("frappe")
        fake_frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)
        fake_frappe.db = types.SimpleNamespace(exists=lambda *args, **kwargs: False)

        fake_frappe_exceptions = types.ModuleType("frappe.exceptions")
        fake_frappe_exceptions.DuplicateEntryError = Exception

        sys.modules["frappe"] = fake_frappe
        sys.modules["frappe.exceptions"] = fake_frappe_exceptions
        sys.modules["stripe"] = types.ModuleType("stripe")

        fake_utils_mod = types.ModuleType("stripe_integration.stripe_integration.utils")
        fake_utils_mod.get_webhook_secret = lambda *_args, **_kwargs: None
        fake_utils_mod.get_company_abbr_from_company = lambda *_args, **_kwargs: None
        fake_utils_mod.get_api_key = lambda *_args, **_kwargs: None
        fake_utils_mod.resolve_customer_email = lambda *_args, **_kwargs: None

        fake_event_log = types.ModuleType("stripe_integration.stripe_integration.event_log")
        fake_event_log.upsert_event = lambda **kwargs: None
        fake_event_log.mark_event_status = lambda *args, **kwargs: None

        fake_subscription_payments = types.ModuleType("stripe_integration.stripe_integration.subscription_payments")
        fake_subscription_payments.handle_invoice_paid = lambda *_args, **_kwargs: None

        fake_subscription_sync = types.ModuleType("stripe_integration.stripe_integration.subscription_sync")
        fake_subscription_sync.sync_subscription_from_webhook_event = lambda *_args, **_kwargs: None
        fake_subscription_sync.ensure_stripe_subscription_for_subscription = lambda *_args, **_kwargs: None
        fake_subscription_sync._set_subscription_fields = lambda *_args, **_kwargs: None
        fake_subscription_sync.SETUP_STATUS_FIELD = "setup_status"
        fake_subscription_sync.SETUP_PM_FIELD = "setup_pm"
        fake_subscription_sync.SETUP_INTENT_FIELD = "setup_intent"

        fake_payout_sync = types.ModuleType("stripe_integration.stripe_integration.payout_sync")
        fake_payout_sync.sync_payout_from_webhook_event = lambda *_args, **_kwargs: None

        fake_refunds = types.ModuleType("stripe_integration.stripe_integration.refunds")
        fake_refunds.apply_refund_to_erp = lambda **kwargs: {"handled": True}

        sys.modules["stripe_integration.stripe_integration.utils"] = fake_utils_mod
        sys.modules["stripe_integration.stripe_integration.event_log"] = fake_event_log
        sys.modules["stripe_integration.stripe_integration.subscription_payments"] = fake_subscription_payments
        sys.modules["stripe_integration.stripe_integration.subscription_sync"] = fake_subscription_sync
        sys.modules["stripe_integration.stripe_integration.payout_sync"] = fake_payout_sync
        sys.modules["stripe_integration.stripe_integration.refunds"] = fake_refunds

        self.webhook = importlib.import_module("stripe_integration.stripe_integration.webhook")

    def tearDown(self):
        restore_modules(self._orig_modules)

    def test_build_safe_payload_text_redacts_pii_and_masks_ids(self):
        raw_event = {
            "id": "evt_1234567890123456",
            "type": "checkout.session.completed",
            "created": 1710000000,
            "livemode": False,
            "data": {
                "object": {
                    "customer_id": "cus_1234567890123456",
                    "email": "customer@example.com",
                    "phone": "+16045551234",
                    "metadata": {
                        "company_abbr": "COE",
                        "doctype": "Sales Invoice",
                        "docname": "SINV-0001",
                        "request_kind": "full",
                        "internal_secret": "should-not-leak",
                    },
                }
            },
        }
        payload = json.dumps(raw_event).encode("utf-8")

        safe = self.webhook._build_safe_payload_text(payload, raw_event)
        out = json.loads(safe)

        obj = out["event"]["data"]["object"]
        self.assertEqual(obj["email"], "[redacted]")
        self.assertEqual(obj["phone"], "[redacted]")
        self.assertTrue(obj["customer_id"].startswith("cus_12"))
        self.assertNotIn("internal_secret", obj["metadata"])
        self.assertEqual(obj["metadata"]["company_abbr"], "COE")
        self.assertIn("payload_hash", out)
        self.assertGreater(out["payload_size"], 0)


if __name__ == "__main__":
    unittest.main()
