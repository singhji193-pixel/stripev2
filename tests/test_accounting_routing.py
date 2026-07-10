import importlib
import sys
import types
import unittest
from unittest.mock import Mock


class _Obj(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def __setattr__(self, key, value):
        self[key] = value

    def set(self, key, value):
        self[key] = value


class AccountingRoutingTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)

        fake_frappe = types.ModuleType("frappe")
        fake_frappe.get_doc = lambda doctype, name: _Obj(
            company="COEngine Service Inc.",
            stripe_clearing_account="1150 - Stripe Clearing Account - COE",
            bank_account="1120 - TD Business Chequing - COE",
            stripe_fee_account="5085 - Stripe Processing Fees - COE",
        )

        def get_value(doctype, name, fieldname):
            if doctype == "Account" and fieldname == "company":
                return "COEngine Service Inc."
            if doctype == "Account" and fieldname == "account_currency":
                return "CAD"
            return None

        fake_frappe.db = types.SimpleNamespace(get_value=get_value)
        fake_frappe.get_cached_value = lambda *args, **kwargs: "CAD"
        fake_frappe.throw = lambda message, exc=None: (_ for _ in ()).throw(Exception(message))
        fake_frappe.ValidationError = Exception
        fake_frappe.utils = types.SimpleNamespace(nowdate=lambda: "2026-07-10")

        fake_utils = types.ModuleType("stripe_integration.stripe_integration.utils")
        fake_utils.get_company_abbr_from_company = lambda company: "COE"

        sys.modules["frappe"] = fake_frappe
        sys.modules["stripe_integration.stripe_integration.utils"] = fake_utils
        sys.modules.pop("stripe_integration.stripe_integration.accounting", None)
        self.accounting = importlib.import_module("stripe_integration.stripe_integration.accounting")

    def tearDown(self):
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    @staticmethod
    def payment_entry(payment_type):
        return _Obj(
            company="COEngine Service Inc.",
            payment_type=payment_type,
            paid_from="Debtors - COE" if payment_type == "Receive" else "Cash - COE",
            paid_to="Cash - COE" if payment_type == "Receive" else "Debtors - COE",
            meta=types.SimpleNamespace(get_field=lambda fieldname: object()),
        )

    def test_received_stripe_payment_debits_clearing(self):
        payment_entry = self.payment_entry("Receive")

        self.accounting.route_payment_entry_to_stripe_clearing(payment_entry, "COE")

        self.assertEqual(payment_entry.paid_to, "1150 - Stripe Clearing Account - COE")
        self.assertEqual(payment_entry.paid_to_account_currency, "CAD")

    def test_refund_credits_clearing(self):
        payment_entry = self.payment_entry("Pay")

        self.accounting.route_payment_entry_to_stripe_clearing(payment_entry, "COE")

        self.assertEqual(payment_entry.paid_from, "1150 - Stripe Clearing Account - COE")
        self.assertEqual(payment_entry.paid_from_account_currency, "CAD")

    def test_successful_payment_above_live_balance_is_recorded_unallocated(self):
        payment_entry = self.payment_entry("Receive")
        payment_entry.references = [
            _Obj(outstanding_amount=40, allocated_amount=40),
            _Obj(outstanding_amount=20, allocated_amount=20),
        ]
        get_payment_entry = Mock(return_value=payment_entry)
        payment_entry_module = types.ModuleType(
            "erpnext.accounts.doctype.payment_entry.payment_entry"
        )
        payment_entry_module.get_payment_entry = get_payment_entry
        sys.modules["erpnext"] = types.ModuleType("erpnext")
        sys.modules["erpnext.accounts"] = types.ModuleType("erpnext.accounts")
        sys.modules["erpnext.accounts.doctype"] = types.ModuleType("erpnext.accounts.doctype")
        sys.modules["erpnext.accounts.doctype.payment_entry"] = types.ModuleType(
            "erpnext.accounts.doctype.payment_entry"
        )
        sys.modules[
            "erpnext.accounts.doctype.payment_entry.payment_entry"
        ] = payment_entry_module

        invoice = _Obj(
            name="SINV-0001",
            currency="CAD",
            outstanding_amount=60,
        )
        pe, allocated, unallocated = self.accounting.prepare_stripe_receipt_payment_entry(
            invoice,
            100,
            "pi_test",
            "COE",
            posting_date="2026-07-10",
        )

        self.assertEqual(pe.paid_amount, 100)
        self.assertEqual(pe.received_amount, 100)
        self.assertEqual([row.allocated_amount for row in pe.references], [40, 20])
        self.assertEqual(allocated, 60)
        self.assertEqual(unallocated, 40)


if __name__ == "__main__":
    unittest.main()
