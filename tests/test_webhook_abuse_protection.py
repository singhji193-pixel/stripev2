import importlib
import sys
import types
import unittest


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


class WebhookAbuseProtectionTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)

        fake_frappe = types.ModuleType("frappe")
        fake_frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)
        fake_frappe.db = types.SimpleNamespace(exists=lambda *args, **kwargs: False)
        fake_frappe.response = types.SimpleNamespace(status_code=200)
        fake_frappe.local = types.SimpleNamespace(request_ip="127.0.0.1")

        cache = _FakeCache()
        fake_frappe.cache = lambda: cache
        self._cache = cache

        headers = {}
        fake_frappe.get_request_header = lambda key: headers.get(key)
        self._headers = headers

        fake_frappe_exceptions = types.ModuleType("frappe.exceptions")
        fake_frappe_exceptions.DuplicateEntryError = Exception

        sys.modules["frappe"] = fake_frappe
        sys.modules["frappe.exceptions"] = fake_frappe_exceptions
        sys.modules["stripe"] = types.ModuleType("stripe")

        fake_utils_mod = types.ModuleType("stripe_integration.stripe_integration.utils")
        fake_utils_mod.get_webhook_secret = lambda *_args, **_kwargs: None
        fake_utils_mod.get_company_abbr_from_company = lambda *_args, **_kwargs: None
        fake_utils_mod.get_api_key = lambda *_args, **_kwargs: None

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
        self.frappe = fake_frappe

    def tearDown(self):
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    def test_webhook_blocks_large_payload(self):
        payload = b"x" * (self.webhook.WEBHOOK_MAX_PAYLOAD_BYTES + 1)

        out = self.webhook._enforce_webhook_rate_limits(payload)

        self.assertEqual(self.frappe.response.status_code, 413)
        self.assertEqual(out["status"], "payload_too_large")

    def test_webhook_ip_rate_limit_blocks_excess(self):
        payload = b"{}"
        for _ in range(self.webhook.WEBHOOK_RATE_LIMIT_MAX_PER_IP):
            out = self.webhook._enforce_webhook_rate_limits(payload)
            self.assertIsNone(out)

        out = self.webhook._enforce_webhook_rate_limits(payload)
        self.assertEqual(self.frappe.response.status_code, 429)
        self.assertEqual(out["status"], "rate_limited")


if __name__ == "__main__":
    unittest.main()
