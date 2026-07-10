import importlib
import sys
import types
import unittest

from module_isolation import restore_modules


class RefundThresholdApprovalTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)

        fake_frappe = types.ModuleType("frappe")

        class _PermissionError(Exception):
            pass

        fake_frappe.PermissionError = _PermissionError
        fake_frappe.AuthenticationError = Exception
        fake_frappe.session = types.SimpleNamespace(user="test@example.com")
        self._roles = ["Accounts Manager"]

        def _throw(msg, exc):
            raise exc(msg)

        fake_frappe.throw = _throw
        fake_frappe.get_roles = lambda _user: list(self._roles)
        fake_frappe.db = types.SimpleNamespace(get_single_value=lambda *args, **kwargs: 0)
        fake_frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)

        fake_utils = types.ModuleType("frappe.utils")
        fake_utils.flt = lambda x: float(x or 0)
        fake_utils.get_url = lambda: "http://localhost"
        fake_utils.now = lambda: "2026-03-12"
        fake_utils.fmt_money = lambda v: f"{float(v):.2f}"

        fake_password = types.ModuleType("frappe.utils.password")
        fake_password.check_password = lambda *args, **kwargs: True

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
        self.frappe = fake_frappe

    def tearDown(self):
        restore_modules(self._orig_modules)

    def test_threshold_disabled_allows_non_system_manager(self):
        self.frappe.db.get_single_value = lambda *args, **kwargs: 0
        self._roles = ["Accounts User"]

        self.api._enforce_refund_threshold_approval(500)

    def test_below_threshold_allows_non_system_manager(self):
        self.frappe.db.get_single_value = lambda *args, **kwargs: 1000
        self._roles = ["Accounts Manager"]

        self.api._enforce_refund_threshold_approval(999.99)

    def test_at_or_above_threshold_requires_system_manager(self):
        self.frappe.db.get_single_value = lambda *args, **kwargs: 1000
        self._roles = ["Accounts Manager"]

        with self.assertRaises(self.frappe.PermissionError):
            self.api._enforce_refund_threshold_approval(1000)

    def test_at_or_above_threshold_allows_system_manager(self):
        self.frappe.db.get_single_value = lambda *args, **kwargs: 1000
        self._roles = ["System Manager"]

        self.api._enforce_refund_threshold_approval(1500)


if __name__ == "__main__":
    unittest.main()
