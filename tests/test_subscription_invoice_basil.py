import importlib
import sys
import types
import unittest
from datetime import datetime, timezone

from module_isolation import restore_modules


class SubscriptionInvoiceBasilTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)

        fake_frappe = types.ModuleType("frappe")
        fake_frappe.get_traceback = lambda: "traceback"
        fake_frappe.log_error = lambda *args, **kwargs: None
        fake_frappe.get_meta = lambda _doctype: types.SimpleNamespace(get_field=lambda _field: object())
        fake_frappe.db = types.SimpleNamespace(
            exists=lambda doctype, name: doctype == "Subscription" and name == "ACC-SUB-0001",
            get_value=lambda doctype, filters, field, **kwargs: (
                "ACC-SUB-0001"
                if doctype == "Subscription" and filters == {"stripe_subscription_id": "sub_basil"}
                else None
            ),
        )

        fake_frappe_utils = types.ModuleType("frappe.utils")
        fake_frappe_utils.flt = float
        fake_frappe_utils.getdate = lambda value: value
        fake_frappe_utils.nowdate = lambda: "2026-07-10"
        fake_frappe.utils = fake_frappe_utils

        self.invoice_payments = []
        stripe_mod = types.ModuleType("stripe")
        stripe_mod.InvoicePayment = types.SimpleNamespace(
            list=lambda **kwargs: types.SimpleNamespace(data=self.invoice_payments)
        )
        stripe_mod.Invoice = types.SimpleNamespace(
            retrieve=lambda _invoice_id: (_ for _ in ()).throw(AssertionError("legacy lookup should not run"))
        )
        stripe_mod.Charge = types.SimpleNamespace(retrieve=lambda _charge_id: None)

        fake_utils = types.ModuleType("stripe_integration.stripe_integration.utils")
        fake_utils.get_api_key = lambda _abbr: "test-key"
        fake_utils.get_company_abbr_from_company = lambda _company: "COE"

        fake_fees = types.ModuleType("stripe_integration.stripe_integration.stripe_fees")
        fake_fees.ensure_fee_posted = lambda **kwargs: {"handled": True}

        sys.modules["frappe"] = fake_frappe
        sys.modules["frappe.utils"] = fake_frappe_utils
        sys.modules["stripe"] = stripe_mod
        sys.modules["stripe_integration.stripe_integration.utils"] = fake_utils
        sys.modules["stripe_integration.stripe_integration.stripe_fees"] = fake_fees
        sys.modules.pop("stripe_integration.stripe_integration.subscription_payments", None)

        self.module = importlib.import_module("stripe_integration.stripe_integration.subscription_payments")

    def tearDown(self):
        restore_modules(self._orig_modules)

    @staticmethod
    def _basil_invoice_object():
        return {
            "metadata": {},
            "subscription": None,
            "parent": {
                "type": "subscription_details",
                "subscription_details": {
                    "subscription": "sub_basil",
                    "metadata": {
                        "doctype": "Subscription",
                        "docname": "ACC-SUB-0001",
                        "company_abbr": "COE",
                    },
                },
            },
        }

    def test_extracts_subscription_context_from_basil_parent(self):
        obj = self._basil_invoice_object()

        self.assertEqual(self.module._invoice_subscription_id(obj), "sub_basil")
        self.assertEqual(self.module._invoice_metadata(obj)["docname"], "ACC-SUB-0001")
        self.assertEqual(self.module._resolve_subscription_name_from_invoice_event(obj, {}), "ACC-SUB-0001")

    def test_resolves_payment_intent_through_invoice_payment(self):
        self.invoice_payments.append(
            types.SimpleNamespace(
                status="paid",
                payment=types.SimpleNamespace(payment_intent="pi_basil"),
            )
        )

        result = self.module._resolve_payment_intent_from_stripe_invoice("in_basil", "COE")

        self.assertEqual(result, "pi_basil")

    def test_converts_exclusive_stripe_period_end_to_inclusive_erp_date(self):
        start = int(datetime(2026, 8, 1, 7, 0, tzinfo=timezone.utc).timestamp())
        end = int(datetime(2026, 9, 1, 7, 0, tzinfo=timezone.utc).timestamp())
        obj = {
            "lines": {
                "data": [
                    {
                        "period": {"start": start, "end": end},
                        "parent": {"type": "subscription_item_details"},
                    }
                ]
            }
        }

        self.assertEqual(self.module._period_dates_from_invoice_object(obj), ("2026-08-01", "2026-08-31"))


if __name__ == "__main__":
    unittest.main()
