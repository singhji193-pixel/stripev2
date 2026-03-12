import importlib
import json
import sys
import types
import unittest


class WebhookAccountBindingTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)

        self.headers = {"Stripe-Signature": "t=1,v1=test"}
        self.payload = b"{}"

        fake_frappe = types.ModuleType("frappe")
        fake_frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)
        fake_frappe.request = types.SimpleNamespace(get_data=lambda: self.payload)
        fake_frappe.response = types.SimpleNamespace(status_code=200)
        fake_frappe.get_request_header = lambda key: self.headers.get(key)
        fake_frappe.get_traceback = lambda: "traceback"
        fake_frappe.log_error = lambda *args, **kwargs: None
        fake_frappe.throw = lambda msg, exc=None: (_ for _ in ()).throw(Exception(msg))
        fake_frappe.ValidationError = Exception
        fake_frappe.db = types.SimpleNamespace(
            exists=lambda *args, **kwargs: False,
            get_value=lambda doctype, name, field: "COEngine Service Inc." if (doctype, name, field) == ("Sales Invoice", "SINV-0001", "company") else None,
            set_value=lambda *args, **kwargs: None,
            commit=lambda: None,
            sql=lambda *args, **kwargs: [[1]],
        )

        fake_frappe_exceptions = types.ModuleType("frappe.exceptions")
        fake_frappe_exceptions.DuplicateEntryError = Exception

        stripe_mod = types.ModuleType("stripe")

        self.event = {
            "id": "evt_123",
            "type": "checkout.session.completed",
            "data": {"object": {"metadata": {}}},
        }

        def construct_event(payload, sig_header, secret):
            if secret == "whsec_coe":
                return self.event
            raise Exception("bad secret")

        stripe_mod.Webhook = types.SimpleNamespace(construct_event=construct_event)

        fake_utils_mod = types.ModuleType("stripe_integration.stripe_integration.utils")
        fake_utils_mod.get_webhook_secret = lambda abbr: {"COE": "whsec_coe", "COSL": "whsec_cosl"}.get(abbr)
        fake_utils_mod.get_company_abbr_from_company = lambda company: "COE" if company == "COEngine Service Inc." else "COSL" if company == "CoreOrbit Systems Ltd." else None
        fake_utils_mod.get_api_key = lambda *_args, **_kwargs: None

        fake_event_log = types.ModuleType("stripe_integration.stripe_integration.event_log")
        fake_event_log.upsert_event = lambda **kwargs: None
        fake_event_log.mark_event_status = lambda *args, **kwargs: None

        fake_subscription_payments = types.ModuleType("stripe_integration.stripe_integration.subscription_payments")
        fake_subscription_payments.handle_invoice_paid = lambda *_args, **_kwargs: None

        fake_subscription_sync = types.ModuleType("stripe_integration.stripe_integration.subscription_sync")
        fake_subscription_sync.sync_subscription_from_webhook_event = lambda *_args, **_kwargs: None
        fake_subscription_sync._set_subscription_fields = lambda *_args, **_kwargs: None
        fake_subscription_sync.SETUP_STATUS_FIELD = "setup_status"
        fake_subscription_sync.SETUP_PM_FIELD = "setup_pm"
        fake_subscription_sync.SETUP_INTENT_FIELD = "setup_intent"

        fake_payout_sync = types.ModuleType("stripe_integration.stripe_integration.payout_sync")
        fake_payout_sync.sync_payout_from_webhook_event = lambda *_args, **_kwargs: None

        fake_refunds = types.ModuleType("stripe_integration.stripe_integration.refunds")
        fake_refunds.apply_refund_to_erp = lambda **kwargs: {"handled": True}

        sys.modules["frappe"] = fake_frappe
        sys.modules["frappe.exceptions"] = fake_frappe_exceptions
        sys.modules["stripe"] = stripe_mod
        sys.modules["stripe_integration.stripe_integration.utils"] = fake_utils_mod
        sys.modules["stripe_integration.stripe_integration.event_log"] = fake_event_log
        sys.modules["stripe_integration.stripe_integration.subscription_payments"] = fake_subscription_payments
        sys.modules["stripe_integration.stripe_integration.subscription_sync"] = fake_subscription_sync
        sys.modules["stripe_integration.stripe_integration.payout_sync"] = fake_payout_sync
        sys.modules["stripe_integration.stripe_integration.refunds"] = fake_refunds

        self.webhook = importlib.import_module("stripe_integration.stripe_integration.webhook")

    def tearDown(self):
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    def test_extract_claimed_company_abbr_from_company_name(self):
        event = {
            "data": {
                "object": {
                    "metadata": {
                        "company": "COEngine Service Inc.",
                    }
                }
            }
        }
        self.assertEqual(self.webhook._extract_claimed_company_abbr(event), "COE")

    def test_handle_webhook_rejects_metadata_company_mismatch_with_signature_account(self):
        self.event["data"]["object"]["metadata"] = {"company_abbr": "COSL"}

        out = self.webhook.handle_webhook()

        self.assertEqual(out["status"], "account_mismatch")
        self.assertEqual(out["reason"], "metadata_company_abbr_mismatch")

    def test_handle_webhook_rejects_doc_company_mismatch_with_signature_account(self):
        self.event["data"]["object"]["metadata"] = {
            "doctype": "Sales Invoice",
            "docname": "SINV-0001",
            "company_abbr": "COE",
        }

        # Force doc company resolve to COSL while signature matched COE.
        self.webhook.get_company_abbr_from_company = lambda company: "COSL"

        out = self.webhook.handle_webhook()

        self.assertEqual(out["status"], "account_mismatch")
        self.assertEqual(out["reason"], "document_company_abbr_mismatch")

    def test_safe_payload_includes_matched_company_abbr(self):
        safe = self.webhook._build_safe_payload_text(self.payload, self.event, matched_company_abbr="COE")
        out = json.loads(safe)
        self.assertEqual(out["matched_company_abbr"], "COE")


if __name__ == "__main__":
    unittest.main()
