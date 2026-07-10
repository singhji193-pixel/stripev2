import importlib
import sys
import types
import unittest
from datetime import date, timedelta, timezone
from unittest.mock import Mock


class _Obj(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def __setattr__(self, key, value):
        self[key] = value


class SubscriptionPricingTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)

        fake_frappe = types.ModuleType("frappe")
        fake_frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)
        fake_frappe.local = types.SimpleNamespace(site="coengine", conf=types.SimpleNamespace())
        fake_frappe.db = types.SimpleNamespace(
            get_value=lambda doctype, name, fieldname, **kwargs: (
                "price_monthly_149"
                if (doctype, name, fieldname) == ("Subscription Plan", "PLAN-149", "product_price_id")
                else None
            ),
        )
        fake_frappe.get_cached_value = lambda *args, **kwargs: "CAD"
        fake_frappe.throw = lambda message, exc=None: (_ for _ in ()).throw(Exception(message))

        self.tax_template = _Obj(
            name="Canada GST 5% - COE",
            taxes=[
                _Obj(
                    idx=1,
                    rate=5.0,
                    add_deduct_tax="Add",
                    charge_type="On Net Total",
                    included_in_print_rate=0,
                    description="GST",
                    account_head="GST - COE",
                )
            ],
        )
        fake_frappe.get_doc = lambda doctype, name: self.tax_template

        fake_frappe_utils = types.ModuleType("frappe.utils")
        fake_frappe_utils.get_url = lambda: "https://next.coengine.ai"
        fake_frappe_utils.getdate = lambda value: value if isinstance(value, date) else date.fromisoformat(value)
        fake_frappe_utils.nowdate = lambda: "2026-07-10"
        fake_frappe_utils.get_system_timezone = lambda: "America/Vancouver"
        fake_frappe.utils = fake_frappe_utils

        self.coupon_create = Mock(return_value=_Obj(id="coupon_79"))
        self.tax_rate_create = Mock(return_value=_Obj(id="txr_gst", metadata={"erpnext_signature": "new"}))
        fake_stripe = types.ModuleType("stripe")
        fake_stripe.Coupon = types.SimpleNamespace(
            list=lambda **kwargs: _Obj(data=[], has_more=False),
            create=self.coupon_create,
        )
        fake_stripe.TaxRate = types.SimpleNamespace(
            list=lambda **kwargs: _Obj(data=[], has_more=False),
            create=self.tax_rate_create,
        )

        fake_app_utils = types.ModuleType("stripe_integration.stripe_integration.utils")
        fake_app_utils.get_company_abbr_from_company = lambda company: "COE"
        fake_app_utils.get_api_key = lambda company_abbr: "test-key"

        fake_event_log = types.ModuleType("stripe_integration.stripe_integration.event_log")
        fake_event_log.upsert_event = Mock()
        fake_event_log.mark_event_status = Mock()

        sys.modules["frappe"] = fake_frappe
        sys.modules["frappe.utils"] = fake_frappe_utils
        sys.modules["stripe"] = fake_stripe
        sys.modules["stripe_integration.stripe_integration.utils"] = fake_app_utils
        sys.modules["stripe_integration.stripe_integration.event_log"] = fake_event_log
        sys.modules.pop("stripe_integration.stripe_integration.accounting", None)
        sys.modules.pop("stripe_integration.stripe_integration.subscription_sync", None)
        self.module = importlib.import_module("stripe_integration.stripe_integration.subscription_sync")
        self.module.ZoneInfo = lambda name: timezone(timedelta(hours=-7))

    def tearDown(self):
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    @staticmethod
    def subscription():
        return _Obj(
            name="ACC-SUB-0001",
            company="COEngine Service Inc.",
            start_date="2026-08-01",
            plans=[_Obj(plan="PLAN-149", qty=1)],
            additional_discount_percentage=0,
            additional_discount_amount=79,
            sales_tax_template="Canada GST 5% - COE",
        )

    def test_subscription_create_params_include_erp_discount_and_tax(self):
        params = self.module._build_stripe_subscription_create_params(
            self.subscription(),
            "cus_test",
            "pm_test",
            "COE",
        )

        self.assertEqual(params["items"], [{"price": "price_monthly_149", "quantity": 1}])
        self.assertEqual(params["discounts"], [{"coupon": "coupon_79"}])
        self.assertEqual(params["default_tax_rates"], ["txr_gst"])
        self.assertEqual(params["trial_end"], 1785567600)

        coupon_args = self.coupon_create.call_args.kwargs
        self.assertEqual(coupon_args["amount_off"], 7900)
        self.assertEqual(coupon_args["currency"], "cad")
        self.assertEqual(coupon_args["api_key"], "test-key")

        tax_args = self.tax_rate_create.call_args.kwargs
        self.assertEqual(tax_args["percentage"], 5.0)
        self.assertFalse(tax_args["inclusive"])
        self.assertEqual(tax_args["api_key"], "test-key")

    def test_existing_erp_period_defers_first_stripe_renewal(self):
        sub = self.subscription()
        sub.start_date = "2026-07-01"
        sub.current_invoice_start = "2026-08-01"

        params = self.module._build_stripe_subscription_create_params(
            sub,
            "cus_test",
            "pm_test",
            "COE",
        )

        self.assertEqual(params["trial_end"], 1785567600)

    def test_setup_token_is_only_valid_while_pending(self):
        token = self.module._make_subscription_setup_token("ACC-SUB-0001", "nonce-1")
        self.module.frappe.db.get_value = lambda *args, **kwargs: {
            self.module.SETUP_TOKEN_NONCE_FIELD: "nonce-1",
            self.module.SETUP_STATUS_FIELD: "pending",
            "status": "Active",
        }

        self.assertTrue(
            self.module._subscription_setup_token_valid("ACC-SUB-0001", token)
        )

        self.module.frappe.db.get_value = lambda *args, **kwargs: {
            self.module.SETUP_TOKEN_NONCE_FIELD: "nonce-1",
            self.module.SETUP_STATUS_FIELD: "completed",
            "status": "Active",
        }
        self.assertFalse(
            self.module._subscription_setup_token_valid("ACC-SUB-0001", token)
        )

    def test_setup_url_encodes_subscription_name(self):
        sub = _Obj(name="Subscription With Space", stripe_setup_token_nonce="nonce-1")

        url = self.module._build_stable_subscription_setup_url(sub)

        self.assertIn("subscription_name=Subscription+With+Space", url)

    def test_existing_customer_default_card_creates_subscription_without_new_setup(self):
        sub = self.subscription()
        sub.update(
            {
                "party_type": "Customer",
                "party": "Koala Tire Ltd.",
                "stripe_customer_id": "cus_existing",
                "stripe_subscription_id": "",
                "stripe_default_payment_method_id": "",
            }
        )
        original_get_doc = self.module.frappe.get_doc
        self.module.frappe.get_doc = lambda doctype, name: (
            sub if doctype == "Subscription" else original_get_doc(doctype, name)
        )

        class _NullLock:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        self.module.MariaDBNamedLock = _NullLock
        self.module._resolve_subscription_email = lambda doc: "billing@example.com"
        self.module._set_subscription_fields = Mock()

        payment_method_retrieve = Mock(
            return_value=_Obj(id="pm_saved", customer="cus_existing")
        )
        payment_method_attach = Mock()
        self.module.stripe.PaymentMethod = types.SimpleNamespace(
            retrieve=payment_method_retrieve,
            attach=payment_method_attach,
        )
        self.module.stripe.Customer = types.SimpleNamespace()
        self.module.stripe.Customer.retrieve = Mock(
            return_value=_Obj(
                id="cus_existing",
                metadata={
                    "company_abbr": "COE",
                    "erpnext_party": "Koala Tire Ltd.",
                },
                invoice_settings={"default_payment_method": "pm_saved"},
            )
        )
        self.module.stripe.Customer.modify = Mock()
        subscription_create = Mock(
            return_value=_Obj(
                id="sub_created",
                status="trialing",
                pause_collection=None,
                items=_Obj(data=[_Obj(id="si_created")]),
            )
        )
        self.module.stripe.Subscription = types.SimpleNamespace(create=subscription_create)

        result = self.module.ensure_stripe_subscription_for_subscription(sub.name)

        self.assertTrue(result["created"])
        self.assertTrue(result["used_saved_payment_method"])
        self.assertEqual(result["stripe_subscription_id"], "sub_created")
        payment_method_retrieve.assert_called_once_with(
            "pm_saved",
            api_key="test-key",
        )
        payment_method_attach.assert_not_called()
        self.assertEqual(
            subscription_create.call_args.kwargs["default_payment_method"],
            "pm_saved",
        )


if __name__ == "__main__":
    unittest.main()
