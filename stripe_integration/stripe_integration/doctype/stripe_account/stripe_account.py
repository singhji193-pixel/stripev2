import frappe
from frappe.model.document import Document


class StripeAccount(Document):
    def validate(self):
        self.company_abbr = (self.company_abbr or "").strip().upper()
        company_abbr = frappe.db.get_value("Company", self.company, "abbr")
        if company_abbr and company_abbr != self.company_abbr:
            frappe.throw(
                f"Stripe Account abbreviation {self.company_abbr} does not match "
                f"Company {self.company} ({company_abbr})"
            )

        publishable_key = (self.publishable_key or "").strip()
        secret_key = self.get_password("secret_key", raise_exception=False) or ""
        webhook_secret = self.get_password("webhook_secret", raise_exception=False) or ""
        expected_prefix = "test" if self.test_mode else "live"
        if publishable_key and not publishable_key.startswith(f"pk_{expected_prefix}_"):
            frappe.throw(f"Publishable Key does not match {'Test' if self.test_mode else 'Live'} mode")
        if secret_key and not secret_key.startswith(f"sk_{expected_prefix}_"):
            frappe.throw(f"Secret Key does not match {'Test' if self.test_mode else 'Live'} mode")
        if webhook_secret and not webhook_secret.startswith("whsec_"):
            frappe.throw("Webhook Secret must start with whsec_")

        stripe_account_id = (self.get("stripe_account_id") or "").strip()
        if stripe_account_id and not stripe_account_id.startswith("acct_"):
            frappe.throw("Stripe Account ID must start with acct_")

        tolerance = int(self.get("webhook_tolerance_seconds") or 300)
        if tolerance < 60 or tolerance > 900:
            frappe.throw("Webhook tolerance must be between 60 and 900 seconds")

        if self.enabled:
            required = {
                "webhook_secret": webhook_secret,
                "stripe_clearing_account": self.get("stripe_clearing_account"),
                "stripe_fee_account": self.get("stripe_fee_account"),
            }
            if self.get("payout_sync_enabled"):
                required["bank_account"] = self.get("bank_account")
            missing = [fieldname for fieldname, value in required.items() if not value]
            if missing:
                frappe.throw("Enabled Stripe Account is missing: " + ", ".join(missing))

        for fieldname in ("bank_account", "stripe_clearing_account", "stripe_fee_account"):
            account = self.get(fieldname)
            if not account:
                continue
            account_company = frappe.db.get_value("Account", account, "company")
            if account_company and account_company != self.company:
                frappe.throw(f"{account} belongs to {account_company}, not {self.company}")
