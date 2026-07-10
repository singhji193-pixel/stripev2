import importlib
import sys
import types
import unittest

from module_isolation import restore_modules


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


class RefundRateLimitTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)

        fake_frappe = types.ModuleType("frappe")

        class _PermissionError(Exception):
            pass

        fake_frappe.PermissionError = _PermissionError
        fake_frappe.AuthenticationError = Exception
        fake_frappe.session = types.SimpleNamespace(user="ratelimit@example.com")
        fake_frappe.throw = lambda msg, exc: (_ for _ in ()).throw(exc(msg))
        fake_frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)

        cache = _FakeCache()
        fake_frappe.cache = lambda: cache
        self._cache = cache

        fake_utils = types.ModuleType("frappe.utils")
        fake_utils.flt = lambda x: float(x or 0)
        fake_utils.get_url = lambda: "http://localhost"
        fake_utils.now = lambda: "2026-03-12"
        fake_utils.fmt_money = lambda v: f"{float(v):.2f}"

        fake_password = types.ModuleType("frappe.utils.password")
        fake_password.check_password = lambda *args, **kwargs: True

        fake_frappe.db = types.SimpleNamespace(get_single_value=lambda *args, **kwargs: 0)
        fake_frappe.get_roles = lambda *_args, **_kwargs: ["System Manager"]

        sys.modules["frappe"] = fake_frappe
        sys.modules["frappe.utils"] = fake_utils
        sys.modules["frappe.utils.password"] = fake_password
        sys.modules["stripe"] = types.ModuleType("stripe")

        fake_utils_mod = types.ModuleType("stripe_integration.stripe_integration.utils")
        fake_utils_mod.get_api_key = lambda *_args, **_kwargs: "sk_test"
        fake_utils_mod.get_company_abbr_from_company = lambda *_args, **_kwargs: "COE"

        fake_refunds_mod = types.ModuleType("stripe_integration.stripe_integration.refunds")
        fake_refunds_mod.apply_refund_to_erp = lambda **kwargs: {"handled": True}

        sys.modules["stripe_integration.stripe_integration.utils"] = fake_utils_mod
        sys.modules["stripe_integration.stripe_integration.refunds"] = fake_refunds_mod

        self.api = importlib.import_module("stripe_integration.stripe_integration.api")

    def tearDown(self):
        restore_modules(self._orig_modules)

    def test_refund_rate_limit_allows_first_attempts(self):
        for _ in range(self.api.REFUND_RATE_LIMIT_MAX_ATTEMPTS):
            self.api._enforce_refund_rate_limit("SINV-0001")

    def test_refund_rate_limit_blocks_excess_attempts(self):
        for _ in range(self.api.REFUND_RATE_LIMIT_MAX_ATTEMPTS):
            self.api._enforce_refund_rate_limit("SINV-0001")

        with self.assertRaises(Exception) as err:
            self.api._enforce_refund_rate_limit("SINV-0001")

        self.assertIn("Too many refund attempts", str(err.exception))


if __name__ == "__main__":
    unittest.main()
