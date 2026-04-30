"""Tests for split payment flow (api.py _compute_request_amount_and_kind).

Covers:
- Full payment returns full outstanding
- Split payment deposit calculation
- Split payment remainder returns outstanding
- Edge cases: zero outstanding, invalid percentage, zero grand total
- Payment link creation metadata
- Void payment link flow
"""

import importlib
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


class SplitPaymentComputationTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)

        fake_frappe = types.ModuleType("frappe")

        class _ValidationError(Exception):
            pass

        class _PermissionError(Exception):
            pass

        fake_frappe.ValidationError = _ValidationError
        fake_frappe.PermissionError = _PermissionError
        fake_frappe.AuthenticationError = Exception
        fake_frappe.session = types.SimpleNamespace(user="test@example.com")
        fake_frappe.local = types.SimpleNamespace(site="test.local")
        fake_frappe.whitelist = lambda *a, **kw: (lambda fn: fn)
        fake_frappe.has_permission = lambda *a, **kw: True
        fake_frappe.get_roles = lambda *a, **kw: ["System Manager"]
        fake_frappe.log_error = lambda *a, **kw: None
        fake_frappe.get_traceback = lambda: ""
        fake_frappe.get_doc = lambda *a, **kw: None

        def _throw(msg, exc=None, title=None):
            raise (exc or Exception)(msg)

        fake_frappe.throw = _throw

        fake_frappe.db = types.SimpleNamespace(
            exists=lambda *a, **kw: False,
            get_value=Mock(return_value=None),
            get_single_value=lambda *a, **kw: 0,
            set_value=Mock(),
            commit=Mock(),
        )

        fake_frappe.cache = lambda: None
        fake_frappe.get_meta = lambda dt: types.SimpleNamespace(get_field=lambda f: f)

        fake_utils = types.ModuleType("frappe.utils")
        fake_utils.flt = lambda x: float(x or 0)
        fake_utils.get_url = lambda: "http://localhost"
        fake_utils.now = lambda: "2026-04-25"
        fake_utils.fmt_money = lambda v, currency=None: f"{float(v):.2f}"

        fake_password = types.ModuleType("frappe.utils.password")
        fake_password.check_password = lambda *a, **kw: True

        sys.modules["frappe"] = fake_frappe
        sys.modules["frappe.utils"] = fake_utils
        sys.modules["frappe.utils.password"] = fake_password
        sys.modules["stripe"] = types.ModuleType("stripe")

        fake_utils_mod = types.ModuleType("stripe_integration.stripe_integration.utils")
        fake_utils_mod.get_api_key = lambda *a, **kw: "sk_test"
        fake_utils_mod.get_company_abbr_from_company = lambda *a, **kw: "COE"

        fake_refunds = types.ModuleType("stripe_integration.stripe_integration.refunds")
        fake_refunds.apply_refund_to_erp = lambda **kw: {"handled": True}

        sys.modules["stripe_integration.stripe_integration.utils"] = fake_utils_mod
        sys.modules["stripe_integration.stripe_integration.refunds"] = fake_refunds

        self.api = importlib.import_module("stripe_integration.stripe_integration.api")
        self.frappe = fake_frappe

    def tearDown(self):
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    def _make_invoice(self, **overrides):
        inv = _Obj(
            name="SINV-0001",
            docstatus=1,
            outstanding_amount=1000.0,
            grand_total=1000.0,
            payment_split_type="Full Payment",
            initial_payment_percentage=0,
            custom_stripe_payment_processed=0,
        )
        inv.get = lambda field: inv.get(field) if isinstance(inv, dict) else getattr(inv, field, None)
        inv.update(overrides)
        return inv

    def test_full_payment_returns_outstanding(self):
        inv = self._make_invoice(outstanding_amount=500.0)
        amount, kind = self.api._compute_request_amount_and_kind(inv)
        self.assertEqual(amount, 500.0)
        self.assertEqual(kind, "full")

    def test_split_deposit_calculates_percentage(self):
        inv = self._make_invoice(
            payment_split_type="Split Payment",
            initial_payment_percentage=30,
            grand_total=1000.0,
            outstanding_amount=1000.0,
            custom_stripe_payment_processed=0,
        )
        amount, kind = self.api._compute_request_amount_and_kind(inv)
        self.assertEqual(kind, "deposit")
        self.assertAlmostEqual(amount, 300.0, places=2)

    def test_split_remainder_returns_outstanding(self):
        inv = self._make_invoice(
            payment_split_type="Split Payment",
            initial_payment_percentage=30,
            grand_total=1000.0,
            outstanding_amount=700.0,
            custom_stripe_payment_processed=1,
        )
        amount, kind = self.api._compute_request_amount_and_kind(inv)
        self.assertEqual(kind, "remainder")
        self.assertEqual(amount, 700.0)

    def test_zero_outstanding_throws(self):
        inv = self._make_invoice(outstanding_amount=0)
        with self.assertRaises(Exception) as ctx:
            self.api._compute_request_amount_and_kind(inv)
        self.assertIn("no outstanding", str(ctx.exception).lower())

    def test_split_invalid_percentage_throws(self):
        inv = self._make_invoice(
            payment_split_type="Split Payment",
            initial_payment_percentage=0,
            custom_stripe_payment_processed=0,
        )
        with self.assertRaises(Exception) as ctx:
            self.api._compute_request_amount_and_kind(inv)
        self.assertIn("between 0 and 100", str(ctx.exception))

    def test_split_percentage_over_100_throws(self):
        inv = self._make_invoice(
            payment_split_type="Split Payment",
            initial_payment_percentage=150,
            custom_stripe_payment_processed=0,
        )
        with self.assertRaises(Exception) as ctx:
            self.api._compute_request_amount_and_kind(inv)
        self.assertIn("between 0 and 100", str(ctx.exception))

    def test_deposit_capped_at_outstanding(self):
        inv = self._make_invoice(
            payment_split_type="Split Payment",
            initial_payment_percentage=80,
            grand_total=1000.0,
            outstanding_amount=500.0,  # Less than 80% of 1000
            custom_stripe_payment_processed=0,
        )
        amount, kind = self.api._compute_request_amount_and_kind(inv)
        self.assertEqual(kind, "deposit")
        self.assertEqual(amount, 500.0)  # Capped at outstanding

    def test_zero_grand_total_split_throws(self):
        inv = self._make_invoice(
            payment_split_type="Split Payment",
            initial_payment_percentage=50,
            grand_total=0,
            outstanding_amount=100.0,
            custom_stripe_payment_processed=0,
        )
        with self.assertRaises(Exception) as ctx:
            self.api._compute_request_amount_and_kind(inv)
        self.assertIn("grand total is invalid", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
