import frappe
from erpnext.accounts.doctype.subscription.subscription import Subscription
from frappe import _
from frappe.utils import add_to_date, getdate

NO_INVOICE_FIELD = "custom_do_not_generate_invoices"


class NonBillingSubscription(Subscription):
	def invoicing_is_disabled(self) -> bool:
		return bool(int(self.get(NO_INVOICE_FIELD) or 0))

	def validate_end_date(self) -> None:
		if not self.invoicing_is_disabled():
			return super().validate_end_date()

		if not self.end_date:
			return

		billing_cycle = self.get_billing_cycle_data()
		if not billing_cycle:
			return

		first_period_end = add_to_date(self.start_date, **billing_cycle)
		if getdate(self.end_date) < getdate(first_period_end):
			frappe.throw(
				_("Non-billing Subscription End Date must be on or after {0}").format(first_period_end)
			)

	def can_generate_new_invoice(self, posting_date=None) -> bool:
		if self.invoicing_is_disabled():
			return False
		return super().can_generate_new_invoice(posting_date)

	def create_invoice(self, *args, **kwargs):
		if self.invoicing_is_disabled():
			frappe.throw(_("Subscription {0}: invoice generation is disabled").format(self.name))
		return super().create_invoice(*args, **kwargs)

	@frappe.whitelist()
	def process(self, posting_date=None) -> bool:
		if not self.invoicing_is_disabled():
			return super().process(posting_date)

		self.set_subscription_status(posting_date=posting_date)
		self.save()
		return False
