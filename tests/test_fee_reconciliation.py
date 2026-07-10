import importlib
import sys
import types
import unittest
from unittest.mock import Mock


class FeeReconciliationTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)

        fake_frappe = types.ModuleType("frappe")
        fake_frappe.get_doc = lambda *args, **kwargs: types.SimpleNamespace(
            company="COEngine Service Inc.",
            get=lambda fieldname: (
                "1150 - Stripe Clearing Account - COE"
                if fieldname == "stripe_clearing_account"
                else None
            ),
        )
        fake_frappe.get_all = lambda *args, **kwargs: [
            {
                "name": "PE-OLD-CASH",
                "reference_no": "pi_old",
                "posting_date": "2026-07-01",
                "paid_amount": 100,
                "paid_to": "Cash - COE",
            }
        ]

        fake_stripe = types.ModuleType("stripe")
        self.retrieve_payment_intent = Mock()
        fake_stripe.PaymentIntent = types.SimpleNamespace(
            retrieve=self.retrieve_payment_intent
        )

        fake_accounting = types.ModuleType(
            "stripe_integration.stripe_integration.accounting"
        )
        fake_accounting.MariaDBNamedLock = object
        fake_accounting.get_stripe_account_mapping = lambda *args, **kwargs: {}
        fake_accounting.stripe_timestamp_date = lambda value: "2026-07-01"
        fake_accounting.validate_stripe_currency = lambda *args, **kwargs: None

        fake_utils = types.ModuleType("stripe_integration.stripe_integration.utils")
        fake_utils.get_api_key = lambda company_abbr: "test-key"

        sys.modules["frappe"] = fake_frappe
        sys.modules["stripe"] = fake_stripe
        sys.modules["stripe_integration.stripe_integration.accounting"] = fake_accounting
        sys.modules["stripe_integration.stripe_integration.utils"] = fake_utils
        sys.modules.pop("stripe_integration.stripe_integration.stripe_fees", None)

        self.module = importlib.import_module(
            "stripe_integration.stripe_integration.stripe_fees"
        )

    def tearDown(self):
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    def test_historical_cash_receipt_is_not_auto_posted_to_clearing(self):
        result = self.module.audit_unposted_fee_entries("COE")

        self.assertEqual(result["checked"], 1)
        self.assertEqual(
            result["missing"][0]["reason"],
            "payment_not_routed_to_stripe_clearing",
        )
        self.retrieve_payment_intent.assert_not_called()


if __name__ == "__main__":
    unittest.main()
