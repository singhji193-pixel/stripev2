import importlib
import sys
import types
import unittest
from datetime import date, timedelta

from module_isolation import restore_modules


class _BaseSubscription(dict):
	def __getattr__(self, key):
		try:
			return self[key]
		except KeyError as exc:
			raise AttributeError(key) from exc

	def process(self, posting_date=None):
		self["base_process_calls"] = self.get("base_process_calls", 0) + 1
		return "processed"

	def create_invoice(self, *args, **kwargs):
		self["base_create_invoice_calls"] = self.get("base_create_invoice_calls", 0) + 1
		return "invoice"

	def can_generate_new_invoice(self, posting_date=None):
		self["base_can_generate_calls"] = self.get("base_can_generate_calls", 0) + 1
		return True

	def validate_end_date(self):
		self["base_validate_end_date_calls"] = self.get("base_validate_end_date_calls", 0) + 1

	def get_billing_cycle_data(self):
		return {"years": 1, "days": -1}

	def set_subscription_status(self, posting_date=None):
		self["status_posting_date"] = posting_date

	def save(self):
		self["save_calls"] = self.get("save_calls", 0) + 1


class NonBillingSubscriptionTests(unittest.TestCase):
	def setUp(self):
		self._orig_modules = dict(sys.modules)

		fake_frappe = types.ModuleType("frappe")
		fake_frappe._ = lambda message: message
		fake_frappe.whitelist = lambda *args, **kwargs: lambda fn: fn
		fake_frappe.throw = lambda message, exc=None: (_ for _ in ()).throw(RuntimeError(message))

		fake_frappe_utils = types.ModuleType("frappe.utils")
		fake_frappe_utils.getdate = lambda value=None: (
			value if isinstance(value, date) else date.fromisoformat(value)
		)

		def add_to_date(value, years=0, months=0, weeks=0, days=0):
			current = fake_frappe_utils.getdate(value)
			if years:
				current = current.replace(year=current.year + years)
			return current + timedelta(weeks=weeks, days=days)

		fake_frappe_utils.add_to_date = add_to_date

		fake_subscription_module = types.ModuleType("erpnext.accounts.doctype.subscription.subscription")
		fake_subscription_module.Subscription = _BaseSubscription

		sys.modules["frappe"] = fake_frappe
		sys.modules["frappe.utils"] = fake_frappe_utils
		sys.modules["erpnext"] = types.ModuleType("erpnext")
		sys.modules["erpnext.accounts"] = types.ModuleType("erpnext.accounts")
		sys.modules["erpnext.accounts.doctype"] = types.ModuleType("erpnext.accounts.doctype")
		sys.modules["erpnext.accounts.doctype.subscription"] = types.ModuleType(
			"erpnext.accounts.doctype.subscription"
		)
		sys.modules["erpnext.accounts.doctype.subscription.subscription"] = fake_subscription_module
		sys.modules.pop("stripe_integration.stripe_integration.subscription_override", None)

		self.module = importlib.import_module("stripe_integration.stripe_integration.subscription_override")

	def tearDown(self):
		restore_modules(self._orig_modules)

	def test_non_billing_subscription_process_never_runs_invoice_path(self):
		subscription = self.module.NonBillingSubscription(custom_do_not_generate_invoices=1)

		result = subscription.process(posting_date="2026-07-15")

		self.assertFalse(result)
		self.assertNotIn("base_process_calls", subscription)
		self.assertEqual(subscription["status_posting_date"], "2026-07-15")
		self.assertEqual(subscription["save_calls"], 1)

	def test_non_billing_subscription_rejects_manual_invoice_generation(self):
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-TEST",
			custom_do_not_generate_invoices=1,
		)

		with self.assertRaisesRegex(RuntimeError, "invoice generation is disabled"):
			subscription.create_invoice()

		self.assertNotIn("base_create_invoice_calls", subscription)

	def test_regular_subscription_keeps_standard_invoice_behavior(self):
		subscription = self.module.NonBillingSubscription(custom_do_not_generate_invoices=0)

		self.assertEqual(subscription.process(posting_date="2026-07-15"), "processed")
		self.assertEqual(subscription.create_invoice(), "invoice")
		self.assertTrue(subscription.can_generate_new_invoice("2026-07-15"))
		self.assertEqual(subscription["base_can_generate_calls"], 1)

	def test_non_billing_subscription_is_never_eligible_for_an_invoice(self):
		subscription = self.module.NonBillingSubscription(custom_do_not_generate_invoices=1)

		self.assertFalse(subscription.can_generate_new_invoice("2026-07-01"))
		self.assertNotIn("base_can_generate_calls", subscription)

	def test_non_billing_subscription_allows_one_inclusive_annual_period(self):
		subscription = self.module.NonBillingSubscription(
			custom_do_not_generate_invoices=1,
			start_date="2026-07-01",
			end_date="2027-06-30",
		)

		subscription.validate_end_date()

		self.assertNotIn("base_validate_end_date_calls", subscription)

	def test_non_billing_subscription_rejects_end_before_first_period(self):
		subscription = self.module.NonBillingSubscription(
			custom_do_not_generate_invoices=1,
			start_date="2026-07-01",
			end_date="2027-06-29",
		)

		with self.assertRaisesRegex(RuntimeError, "on or after"):
			subscription.validate_end_date()


if __name__ == "__main__":
	unittest.main()
