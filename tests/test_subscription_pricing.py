import calendar
import importlib
import sys
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock

from module_isolation import restore_modules


class _Obj(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def __setattr__(self, key, value):
        self[key] = value


class _ResourceMissingError(Exception):
    code = "resource_missing"


class _StripeSubscriptionAPI:
    def __init__(
        self,
        subscription_id="sub_pause",
        pause_collection=None,
        current_period_end=None,
        current_period_start=1782889200,
        billing_cycle_anchor=None,
        interval="month",
        interval_count=1,
        customer="cus_test",
        metadata=None,
    ):
        if billing_cycle_anchor is None:
            billing_cycle_anchor = current_period_end
        if metadata is None:
            metadata = {
                "doctype": "Subscription",
                "docname": "ACC-SUB-0001",
                "company": "COEngine Service Inc.",
                "company_abbr": "COE",
            }
        self.remote = _Obj(
            id=subscription_id,
            customer=customer,
            metadata=metadata,
            pause_collection=pause_collection,
            current_period_end=current_period_end,
            billing_cycle_anchor=billing_cycle_anchor,
            items=_Obj(
                data=[
                    _Obj(
                        current_period_start=current_period_start,
                        current_period_end=current_period_end,
                        price=_Obj(
                            recurring=_Obj(
                                interval=interval,
                                interval_count=interval_count,
                            )
                        ),
                    )
                ]
            ),
        )
        self.retrieve = Mock(side_effect=self._retrieve)
        self.modify = Mock(side_effect=self._modify)
        self.delete = Mock(side_effect=self._delete)

    def _retrieve(self, subscription_id, **kwargs):
        return self.remote

    def _modify(self, subscription_id, **kwargs):
        pause_collection = kwargs.get("pause_collection")
        self.remote.pause_collection = None if pause_collection == "" else pause_collection
        return self.remote

    def _delete(self, subscription_id, **kwargs):
        self.remote.status = "canceled"
        return self.remote


class SubscriptionPricingTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)

        fake_frappe = types.ModuleType("frappe")
        fake_frappe._ = lambda message: message
        fake_frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)
        fake_frappe.local = types.SimpleNamespace(site="coengine", conf=types.SimpleNamespace())
        fake_frappe.db = types.SimpleNamespace(
            get_value=lambda doctype, name, fieldname, **kwargs: (
                "price_monthly_149"
                if (doctype, name, fieldname) == ("Subscription Plan", "PLAN-149", "product_price_id")
                else None
            ),
            commit=Mock(),
            rollback=Mock(),
        )
        fake_frappe.get_meta = lambda doctype: types.SimpleNamespace(
            get_field=lambda fieldname: object()
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

        def add_to_date(value, years=0, months=0, weeks=0, days=0):
            current = fake_frappe_utils.getdate(value)
            if years:
                target_year = current.year + years
                current = current.replace(
                    year=target_year,
                    day=min(current.day, calendar.monthrange(target_year, current.month)[1]),
                )
            if months:
                month_index = current.year * 12 + current.month - 1 + months
                target_year, target_month_index = divmod(month_index, 12)
                target_month = target_month_index + 1
                current = current.replace(
                    year=target_year,
                    month=target_month,
                    day=min(current.day, calendar.monthrange(target_year, target_month)[1]),
                )
            return current + timedelta(weeks=weeks, days=days)

        fake_frappe_utils.add_to_date = add_to_date
        fake_frappe_utils.add_days = lambda value, days: fake_frappe_utils.getdate(value) + timedelta(days=days)
        fake_frappe_utils.add_months = lambda value, months: add_to_date(value, months=months)
        fake_frappe_utils.get_last_day = lambda value: add_to_date(
            date(fake_frappe_utils.getdate(value).year, fake_frappe_utils.getdate(value).month, 1),
            months=1,
            days=-1,
        )
        fake_frappe.utils = fake_frappe_utils

        self.coupon_create = Mock(return_value=_Obj(id="coupon_79"))
        self.tax_rate_create = Mock(return_value=_Obj(id="txr_gst", metadata={"erpnext_signature": "new"}))
        self.invoice_list = Mock(return_value=_Obj(data=[], has_more=False))
        fake_stripe = types.ModuleType("stripe")
        fake_stripe.Coupon = types.SimpleNamespace(
            list=lambda **kwargs: _Obj(data=[], has_more=False),
            create=self.coupon_create,
        )
        fake_stripe.TaxRate = types.SimpleNamespace(
            list=lambda **kwargs: _Obj(data=[], has_more=False),
            create=self.tax_rate_create,
        )
        fake_stripe.Invoice = types.SimpleNamespace(list=self.invoice_list)

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
        sys.modules.pop("stripe_integration.stripe_integration.subscription_pause", None)
        sys.modules.pop("stripe_integration.stripe_integration.subscription_sync", None)
        self.module = importlib.import_module("stripe_integration.stripe_integration.subscription_sync")
        self.pause_module = importlib.import_module(
            "stripe_integration.stripe_integration.subscription_pause"
        )
        self.module.ZoneInfo = lambda name: timezone(timedelta(hours=-7))
        self.module._new_operation_id = lambda action: f"{action}_op_test"
        self.module._utc_now_timestamp = lambda: int(
            datetime(2026, 7, 10, 12, tzinfo=timezone.utc).timestamp()
        )

    def tearDown(self):
        restore_modules(self._orig_modules)

    @staticmethod
    def subscription():
        return _Obj(
            name="ACC-SUB-0001",
            company="COEngine Service Inc.",
            stripe_customer_id="cus_test",
            start_date="2026-08-01",
            current_invoice_start="2026-08-01",
            current_invoice_end="2026-08-31",
            generate_invoice_at="Beginning of the current subscription period",
            is_current_invoice_generated=lambda start, end: False,
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

    def test_non_billing_subscription_skips_automatic_billing_defaults(self):
        sub = _Obj(
            submit_invoice=0,
            generate_invoice_at="End of the current subscription period",
            custom_do_not_generate_invoices=1,
            db_set=Mock(),
        )

        self.module._enforce_subscription_billing_defaults(sub)

        sub.db_set.assert_not_called()

    def test_non_billing_subscription_update_skips_all_stripe_sync(self):
        sub = _Obj(custom_do_not_generate_invoices=1)
        self.module._enforce_subscription_billing_defaults = Mock()
        self.module._is_enabled = Mock(return_value=True)

        self.module.on_subscription_update(sub)

        self.module._enforce_subscription_billing_defaults.assert_called_once_with(sub)
        self.module._is_enabled.assert_not_called()

    def test_native_cancellation_persists_an_outbox_intent_before_enqueue(self):
        sub = self.subscription()
        sub.update(
            {
                "status": "Cancelled",
                "stripe_subscription_id": "sub_native_cancel",
            }
        )
        self.module._enforce_subscription_billing_defaults = Mock()
        self.module._is_enabled = Mock(return_value=True)
        order = []
        self.module.upsert_event = Mock(side_effect=lambda *args, **kwargs: order.append("event"))
        self.module.queue_subscription_action = Mock(
            side_effect=lambda *args, **kwargs: order.append("enqueue")
        )
        self.module.frappe.db.set_value = Mock(
            side_effect=lambda *args, **kwargs: order.append("state")
        )
        self.module.frappe.db.commit.reset_mock()

        self.module.on_subscription_update(sub)

        event = self.module.upsert_event.call_args.args[0]
        self.assertEqual(event["id"], "local_outbound_ACC-SUB-0001_cancel_op_test")
        self.assertEqual(self.module.upsert_event.call_args.kwargs["status"], "Queued")
        values = self.module.frappe.db.set_value.call_args.args[2]
        self.assertEqual(values["stripe_pause_state"], "Cancelling")
        self.assertEqual(values["stripe_pause_operation_id"], "cancel_op_test")
        self.assertEqual(values["stripe_erpnext_pause_active"], 1)
        self.assertIsNone(values["stripe_pause_last_reconciled_at"])
        self.module.queue_subscription_action.assert_called_once_with(
            sub.name,
            "cancel",
            trusted_cancel=True,
        )
        self.assertEqual(order, ["event", "state", "enqueue"])
        self.module.frappe.db.commit.assert_not_called()

    def test_cancel_worker_uses_durable_intent_even_if_status_was_reverted(self):
        sub = self.subscription()
        sub.update(
            {
                "status": "Active",
                "stripe_pause_state": "Cancelling",
                "stripe_pause_operation_id": "cancel_pending",
            }
        )

        class _NullLock:
            def __init__(self, name, timeout=30):
                self.name = name

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        self.module.MariaDBNamedLock = _NullLock
        self.module.frappe.get_doc = Mock(return_value=sub)
        self.module._sync_subscription = Mock(return_value={"handled": True})

        result = self.module._sync_cancelled_subscription_after_commit(sub.name)

        self.assertTrue(result["handled"])
        self.module._sync_subscription.assert_called_once_with(sub, "cancel")

    def test_legacy_subscription_action_is_role_gated_and_rejected_synchronously(self):
        sub = self.subscription()
        sub.update(
            {
                "status": "Active",
                "stripe_subscription_id": "sub_legacy_action",
                "stripe_sync_action": "pause",
            }
        )
        self.module._enforce_subscription_billing_defaults = Mock()
        self.module._is_enabled = Mock(return_value=True)
        self.module._require_subscription_action_role = Mock()
        self.module.queue_subscription_action = Mock()
        self.module.frappe.db.set_value = Mock()

        with self.assertRaisesRegex(Exception, "Legacy stripe_sync_action"):
            self.module.on_subscription_update(sub)

        self.module._require_subscription_action_role.assert_called_once_with()
        self.module.queue_subscription_action.assert_not_called()
        self.module.frappe.db.set_value.assert_not_called()

    def test_non_billing_subscription_cannot_create_stripe_subscription(self):
        sub = self.subscription()
        sub.custom_do_not_generate_invoices = 1
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

        with self.assertRaisesRegex(Exception, "non-billing"):
            self.module.ensure_stripe_subscription_for_subscription(sub.name)

    def test_pause_schedules_one_cycle_on_the_same_stripe_and_erp_boundary(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_pause",
                "current_invoice_start": "2026-08-01",
                "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
            }
        )
        stripe_api = _StripeSubscriptionAPI(
            pause_collection=None,
            current_period_end=1785567600,
        )
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        result = self.module._sync_subscription(sub, "pause", pause_cycles=1)

        pause_call = stripe_api.modify.call_args
        self.assertEqual(pause_call.args, ("sub_pause",))
        self.assertEqual(
            pause_call.kwargs["pause_collection"],
            {"behavior": "void", "resumes_at": 1788246000},
        )
        self.assertEqual(
            pause_call.kwargs["idempotency_key"],
            "erpnext-ACC-SUB-0001-pause_op_test-1",
        )
        self.module._set_subscription_fields.assert_any_call(
            sub.name,
            {
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Pausing",
                "stripe_pause_operation_id": "pause_op_test",
                "stripe_pause_start": "2026-08-01",
                "stripe_resume_on": "2026-09-01",
                "stripe_pending_resume_on": None,
                "stripe_pause_cadence_snapshot": (
                    '{"billing_cycle_anchor":1785567600,'
                    '"follow_calendar_months":0,"interval":"month",'
                    '"interval_count":1,"version":1}'
                ),
                "stripe_pause_start_at": "1785567600",
                "stripe_resume_at": "1788246000",
                "stripe_pending_resume_at": "",
                "stripe_resume_cancel_before_start": 0,
                "stripe_operation_attempt": 0,
                "stripe_pause_cycles": 1,
                "stripe_pause_last_reconciled_at": None,
            },
            required=self.module.COORDINATED_PAUSE_FIELDS,
        )
        self.module._set_subscription_fields.assert_any_call(
            sub.name,
            {"stripe_pause_state": "Paused", "stripe_operation_attempt": 0, "stripe_paused": 1},
            required=self.module.COORDINATED_PAUSE_FIELDS,
            update_modified=True,
        )
        self.assertTrue(result["handled"])
        self.assertEqual(result["pause_start"], "2026-08-01")
        self.assertEqual(result["resume_on"], "2026-09-01")

    def test_pause_uses_stripe_utc_anchor_for_a_dst_crossing_resume(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_dst_pause",
                "current_invoice_start": "2026-08-01",
                "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
            }
        )
        stripe_api = _StripeSubscriptionAPI(
            subscription_id="sub_dst_pause",
            current_period_end=1785567600,
        )
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        result = self.module._sync_subscription(sub, "pause", pause_cycles=4)

        self.assertEqual(result["resume_on"], "2026-12-01")
        self.assertEqual(
            stripe_api.modify.call_args.kwargs["pause_collection"]["resumes_at"],
            1796108400,
        )

    def test_pause_rejects_month_end_cadence_that_would_drift_after_a_short_month(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_month_end_pause",
                "current_invoice_start": "2027-01-31",
                "current_invoice_end": "2027-02-27",
                "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
            }
        )
        stripe_api = _StripeSubscriptionAPI(
            subscription_id="sub_month_end_pause",
            current_period_end=1801382400,
            billing_cycle_anchor=1801382400,
        )
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        with self.assertRaisesRegex(Exception, "cannot stay aligned"):
            self.module._sync_subscription(sub, "pause", pause_cycles=2)

        stripe_api.modify.assert_not_called()
        self.module._set_subscription_fields.assert_not_called()

    def test_pause_rejects_flexible_stripe_billing_before_persisting_a_hold(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_flexible",
                "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
            }
        )
        stripe_api = _StripeSubscriptionAPI(
            subscription_id="sub_flexible",
            current_period_end=1785567600,
        )
        stripe_api.remote.billing_mode = _Obj(type="flexible")
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        with self.assertRaisesRegex(Exception, "flexible billing"):
            self.module._sync_subscription(sub, "pause", pause_cycles=1)

        stripe_api.modify.assert_not_called()
        self.module._set_subscription_fields.assert_not_called()

    def test_pause_rejects_items_with_different_stripe_billing_periods(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_split_periods",
                "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
            }
        )
        stripe_api = _StripeSubscriptionAPI(
            subscription_id="sub_split_periods",
            current_period_end=1785567600,
        )
        stripe_api.remote["items"]["data"].append(
            _Obj(
                current_period_start=1782975600,
                current_period_end=1785654000,
                price=_Obj(recurring=_Obj(interval="month", interval_count=1)),
            )
        )
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        with self.assertRaisesRegex(Exception, "shared billing period"):
            self.module._sync_subscription(sub, "pause", pause_cycles=1)

        stripe_api.modify.assert_not_called()
        self.module._set_subscription_fields.assert_not_called()

    def test_pause_rejects_dynamic_stripe_cadence_features(self):
        cases = {
            "subscription schedule": lambda remote: remote.update(schedule="sub_sched"),
            "pending update": lambda remote: remote.update(pending_update=_Obj()),
            "subscription billing thresholds": lambda remote: remote.update(
                billing_thresholds=_Obj(amount_gte=1000)
            ),
            "item billing thresholds": lambda remote: remote["items"]["data"][0].update(
                billing_thresholds=_Obj(usage_gte=10)
            ),
            "metered usage": lambda remote: remote["items"]["data"][0]["price"][
                "recurring"
            ].update(usage_type="metered"),
        }

        for label, configure in cases.items():
            with self.subTest(label=label):
                sub = self.subscription()
                sub.update(
                    {
                        "stripe_subscription_id": "sub_dynamic",
                        "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
                    }
                )
                stripe_api = _StripeSubscriptionAPI(
                    subscription_id="sub_dynamic",
                    current_period_end=1785567600,
                )
                configure(stripe_api.remote)
                self.module.stripe.Subscription = stripe_api
                self.module._set_subscription_fields = Mock()

                with self.assertRaisesRegex(Exception, "dynamic cadence"):
                    self.module._sync_subscription(sub, "pause", pause_cycles=1)

                stripe_api.modify.assert_not_called()
                self.module._set_subscription_fields.assert_not_called()

    def test_pause_revalidates_dynamic_cadence_after_stripe_accepts_the_hold(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_dynamic_after_pause",
                "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
            }
        )
        stripe_api = _StripeSubscriptionAPI(
            subscription_id="sub_dynamic_after_pause",
            current_period_end=1785567600,
        )

        def add_dynamic_schedule(subscription_id, **kwargs):
            stripe_api.remote.pause_collection = kwargs["pause_collection"]
            stripe_api.remote.schedule = "sub_sched"
            return stripe_api.remote

        stripe_api.modify = Mock(side_effect=add_dynamic_schedule)
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        with self.assertRaisesRegex(Exception, "dynamic cadence"):
            self.module._sync_subscription(sub, "pause", pause_cycles=1)

        completed_writes = [
            call
            for call in self.module._set_subscription_fields.call_args_list
            if call.args[1].get("stripe_pause_state") == "Paused"
        ]
        self.assertEqual(completed_writes, [])

    def test_winter_stripe_anchor_is_compared_as_utc_not_previous_local_date(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_winter_anchor",
                "current_invoice_start": "2026-12-01",
                "current_invoice_end": "2026-12-31",
                "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
            }
        )
        stripe_api = _StripeSubscriptionAPI(
            subscription_id="sub_winter_anchor",
            current_period_end=1796108400,
        )
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()
        self.module.ZoneInfo = lambda name: timezone(timedelta(hours=-8))

        result = self.module._sync_subscription(sub, "pause", pause_cycles=1)

        self.assertEqual(result["pause_start"], "2026-12-01")
        self.assertEqual(result["resume_on"], "2027-01-01")
        self.assertEqual(
            stripe_api.modify.call_args.kwargs["pause_collection"]["resumes_at"],
            1798786800,
        )

    def test_pause_fails_closed_when_migration_metadata_is_missing(self):
        sub = self.subscription()
        sub["stripe_subscription_id"] = "sub_pause"
        self.module.frappe.get_meta = lambda doctype: types.SimpleNamespace(
            get_field=lambda fieldname: None if fieldname == "stripe_pause_state" else object()
        )
        stripe_api = _StripeSubscriptionAPI(current_period_end=1785567600)
        self.module.stripe.Subscription = stripe_api

        with self.assertRaisesRegex(Exception, "migration is incomplete"):
            self.module._sync_subscription(sub, "pause", pause_cycles=1)

        stripe_api.retrieve.assert_not_called()
        stripe_api.modify.assert_not_called()

    def test_pause_rejects_end_of_period_billing_instead_of_skipping_partial_cycle(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_pause",
                "generate_invoice_at": "End of the current subscription period",
                "current_invoice_start": "2026-07-01",
                "current_invoice_end": "2026-07-31",
            }
        )
        stripe_api = _StripeSubscriptionAPI(current_period_end=1785567600)
        self.module.stripe.Subscription = stripe_api

        with self.assertRaisesRegex(Exception, "beginning-of-period billing"):
            self.module._sync_subscription(sub, "pause", pause_cycles=1)

        stripe_api.retrieve.assert_not_called()

    def test_pause_rejects_same_day_boundary_to_avoid_already_billed_race(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_pause",
                "current_invoice_start": "2026-07-10",
                "current_invoice_end": "2026-07-31",
            }
        )
        stripe_api = _StripeSubscriptionAPI(current_period_end=1785567600)
        self.module.stripe.Subscription = stripe_api

        with self.assertRaisesRegex(Exception, "must be after today"):
            self.module._sync_subscription(sub, "pause", pause_cycles=1)

        stripe_api.retrieve.assert_not_called()

    def test_pause_rejects_a_future_period_already_invoiced_in_erpnext(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_pause",
                "is_current_invoice_generated": lambda start, end: True,
            }
        )
        stripe_api = _StripeSubscriptionAPI(current_period_end=1785567600)
        self.module.stripe.Subscription = stripe_api

        with self.assertRaisesRegex(Exception, "already invoiced"):
            self.module._sync_subscription(sub, "pause", pause_cycles=1)

        stripe_api.retrieve.assert_not_called()

    def test_pause_rejects_stripe_and_erpnext_boundary_drift(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_pause",
                "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
            }
        )
        stripe_api = _StripeSubscriptionAPI(current_period_end=1785654000)
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        with self.assertRaisesRegex(Exception, "does not match ERPNext"):
            self.module._sync_subscription(sub, "pause", pause_cycles=1)

        stripe_api.modify.assert_not_called()
        self.module._set_subscription_fields.assert_not_called()

    def test_pause_rejects_an_uncoordinated_existing_stripe_pause(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_external_pause",
                "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
            }
        )
        stripe_api = _StripeSubscriptionAPI(
            subscription_id="sub_external_pause",
            current_period_end=1785567600,
            pause_collection={"behavior": "void", "resumes_at": 1788246000},
        )
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        with self.assertRaisesRegex(Exception, "already paused without an ERPNext"):
            self.module._sync_subscription(sub, "pause", pause_cycles=1)

        stripe_api.modify.assert_not_called()
        self.module._set_subscription_fields.assert_not_called()

    def test_pause_rejects_remote_owned_by_another_erp_subscription(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_wrong_owner",
                "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
            }
        )
        stripe_api = _StripeSubscriptionAPI(
            subscription_id="sub_wrong_owner",
            current_period_end=1785567600,
            metadata={
                "doctype": "Subscription",
                "docname": "ACC-SUB-OTHER",
                "company": "COEngine Service Inc.",
                "company_abbr": "COE",
            },
        )
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        with self.assertRaisesRegex(Exception, "different ERPNext Subscription"):
            self.module._sync_subscription(sub, "pause", pause_cycles=1)

        stripe_api.modify.assert_not_called()
        self.module._set_subscription_fields.assert_not_called()

    def test_pause_canonicalizes_a_terminal_stripe_subscription(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_terminal_pause",
                "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
            }
        )
        stripe_api = _StripeSubscriptionAPI(
            subscription_id="sub_terminal_pause",
            current_period_end=1785567600,
        )
        stripe_api.remote.status = "incomplete_expired"
        self.module.stripe.Subscription = stripe_api
        self.module._apply_subscription_state = Mock(return_value={})

        result = self.module._sync_subscription(sub, "pause", pause_cycles=1)

        self.assertFalse(result["handled"])
        self.assertEqual(result["reason"], "stripe_subscription_terminal")
        self.module._apply_subscription_state.assert_called_once()
        stripe_api.modify.assert_not_called()

    def test_two_cycle_pause_resumes_on_the_second_aligned_boundary(self):
        sub = self.subscription()
        sub.update(
            {
                "current_invoice_start": "2026-08-01",
                "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
            }
        )

        window = self.module.build_pause_window(sub, billing_cycles=2)

        self.assertEqual(
            window,
            {
                "billing_cycles": 2,
                "pause_start": "2026-08-01",
                "resume_on": "2026-10-01",
            },
        )

    def test_calendar_month_pause_resumes_on_first_day_of_next_month(self):
        sub = self.subscription()
        sub.update(
            {
                "current_invoice_start": "2026-08-15",
                "follow_calendar_months": 1,
                "get_billing_cycle_and_interval": lambda: [
                    {"billing_interval": "Month", "billing_interval_count": 1}
                ],
            }
        )

        window = self.module.build_pause_window(sub, billing_cycles=1)

        self.assertEqual(window["pause_start"], "2026-08-15")
        self.assertEqual(window["resume_on"], "2026-09-01")

    def test_calendar_month_extension_preserves_partial_fixed_end_day(self):
        sub = self.subscription()
        sub.update(
            {
                "follow_calendar_months": 1,
                "get_billing_cycle_and_interval": lambda: [
                    {"billing_interval": "Month", "billing_interval_count": 1}
                ],
            }
        )
        pause_module = importlib.import_module(
            "stripe_integration.stripe_integration.subscription_pause"
        )

        extended = pause_module.extend_end_date(sub, "2026-12-15", 1)

        self.assertEqual(extended, date(2027, 1, 15))

    def test_multi_cycle_extension_preserves_month_end_anchor(self):
        regular = self.subscription()
        regular["get_billing_cycle_data"] = lambda: {"months": 1, "days": -1}
        calendar_sub = self.subscription()
        calendar_sub.update(
            {
                "follow_calendar_months": 1,
                "get_billing_cycle_and_interval": lambda: [
                    {"billing_interval": "Month", "billing_interval_count": 1}
                ],
            }
        )

        self.assertEqual(
            self.pause_module.extend_end_date(regular, "2027-01-31", 2),
            date(2027, 3, 31),
        )
        self.assertEqual(
            self.pause_module.extend_end_date(calendar_sub, "2027-01-31", 2),
            date(2027, 3, 31),
        )

    def test_pause_resume_timestamp_preserves_stripe_utc_anchor_across_dst(self):
        sub = self.subscription()
        sub["get_billing_cycle_data"] = lambda: {"months": 1, "days": -1}

        resume_timestamp = self.module.advance_billing_timestamp(sub, 1785567600, 4)

        self.assertEqual(resume_timestamp, 1796108400)
        self.assertEqual(
            datetime.fromtimestamp(resume_timestamp, tz=timezone.utc).isoformat(),
            "2026-12-01T07:00:00+00:00",
        )

    def test_annual_pause_preserves_the_annual_billing_boundary(self):
        sub = self.subscription()
        sub.update(
            {
                "current_invoice_start": "2026-08-01",
                "get_billing_cycle_data": lambda: {"years": 1, "days": -1},
            }
        )

        window = self.module.build_pause_window(sub, billing_cycles=1)

        self.assertEqual(window["pause_start"], "2026-08-01")
        self.assertEqual(window["resume_on"], "2027-08-01")

    def test_resume_schedules_the_next_boundary_without_clearing_local_pause(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_pause",
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Paused",
                "stripe_pause_start": "2026-07-01",
                "stripe_resume_on": "2026-10-01",
                "stripe_pause_start_at": "1782889200",
                "stripe_resume_at": "1790838000",
                "stripe_pause_cycles": 3,
                "current_invoice_start": "2026-07-01",
                "current_invoice_end": "2026-07-31",
                "end_date": "2027-01-31",
                "follow_calendar_months": 0,
                "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
            }
        )
        stripe_api = _StripeSubscriptionAPI(
            pause_collection={"behavior": "void", "resumes_at": 1790838000},
        )
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        result = self.module._sync_subscription(sub, "resume")

        stripe_api.modify.assert_called_once_with(
            "sub_pause",
            pause_collection={"behavior": "void", "resumes_at": 1785567600},
            api_key="test-key",
            idempotency_key="erpnext-ACC-SUB-0001-resume_op_test-1",
        )
        self.module._set_subscription_fields.assert_any_call(
            sub.name,
            {
                "stripe_pause_state": "Resuming",
                "stripe_pause_operation_id": "resume_op_test",
                "stripe_pending_resume_on": "2026-08-01",
                "stripe_pending_resume_at": "1785567600",
                "stripe_resume_cancel_before_start": 0,
                "stripe_operation_attempt": 0,
                "stripe_pause_cycles": 1,
            },
            required=self.module.COORDINATED_PAUSE_FIELDS,
        )
        self.module._set_subscription_fields.assert_any_call(
            sub.name,
            {
                "stripe_resume_on": "2026-08-01",
                "stripe_resume_at": "1785567600",
                "stripe_pause_state": "Paused",
                "stripe_pause_operation_id": "",
                "stripe_pending_resume_on": None,
                "stripe_pending_resume_at": "",
                "stripe_resume_cancel_before_start": 0,
                "stripe_operation_attempt": 0,
                "stripe_pause_cycles": 1,
                "stripe_paused": 1,
            },
            required=self.module.COORDINATED_PAUSE_FIELDS,
        )
        self.assertTrue(result["handled"])
        self.assertEqual(result["resume_on"], "2026-08-01")
        self.assertTrue(result["scheduled"])
        self.assertEqual(sub["stripe_erpnext_pause_active"], 1)
        self.assertEqual(sub["end_date"], "2027-01-31")

    def test_resume_after_the_exact_boundary_hour_schedules_the_following_cycle(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_resume_after_boundary_hour",
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Paused",
                "stripe_pause_start": "2026-07-01",
                "stripe_resume_on": "2026-10-01",
                "stripe_pause_start_at": "1782889200",
                "stripe_resume_at": "1790838000",
                "stripe_pause_cycles": 3,
                "stripe_pause_cadence_snapshot": (
                    '{"billing_cycle_anchor":1782889200,'
                    '"follow_calendar_months":0,"interval":"month",'
                    '"interval_count":1,"version":1}'
                ),
            }
        )
        self.module.nowdate = lambda: "2026-08-01"
        self.module._utc_now_timestamp = lambda: 1785571200
        stripe_api = _StripeSubscriptionAPI(
            subscription_id="sub_resume_after_boundary_hour",
            pause_collection={"behavior": "void", "resumes_at": 1790838000},
            billing_cycle_anchor=1782889200,
        )
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        result = self.module._sync_subscription(sub, "resume")

        self.assertTrue(result["scheduled"])
        self.assertEqual(result["resume_on"], "2026-09-01")
        self.assertEqual(sub["stripe_resume_at"], "1788246000")
        self.assertEqual(
            stripe_api.modify.call_args.kwargs["pause_collection"],
            {"behavior": "void", "resumes_at": 1788246000},
        )

    def test_resume_rejects_non_void_remote_pause_behavior(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_resume_wrong_behavior",
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Paused",
                "stripe_pause_start": "2026-07-01",
                "stripe_resume_on": "2026-10-01",
                "stripe_pause_start_at": "1782889200",
                "stripe_resume_at": "1790838000",
                "stripe_pause_cycles": 3,
            }
        )
        stripe_api = _StripeSubscriptionAPI(
            subscription_id="sub_resume_wrong_behavior",
            pause_collection={"behavior": "keep_as_draft", "resumes_at": 1790838000},
        )
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        with self.assertRaisesRegex(Exception, "behavior is not void"):
            self.module._sync_subscription(sub, "resume")

        stripe_api.modify.assert_not_called()

    def test_resume_rejects_cadence_changed_after_pause_admission(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_changed_cadence",
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Paused",
                "stripe_pause_start": "2026-07-01",
                "stripe_resume_on": "2026-10-01",
                "stripe_pause_start_at": "1782889200",
                "stripe_resume_at": "1790838000",
                "stripe_pause_cycles": 3,
                "stripe_pause_cadence_snapshot": (
                    '{"billing_cycle_anchor":1782889200,'
                    '"follow_calendar_months":0,"interval":"month",'
                    '"interval_count":1,"version":1}'
                ),
            }
        )
        stripe_api = _StripeSubscriptionAPI(
            subscription_id="sub_changed_cadence",
            pause_collection={"behavior": "void", "resumes_at": 1790838000},
            current_period_end=1785567600,
            billing_cycle_anchor=1782889200,
            interval="year",
        )
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        with self.assertRaisesRegex(Exception, "cadence changed"):
            self.module._sync_subscription(sub, "resume")

        stripe_api.modify.assert_not_called()
        self.module._set_subscription_fields.assert_not_called()

    def test_manual_resume_on_due_boundary_completes_erp_and_creates_invoice(self):
        sub = self.subscription()
        invoice = _Obj(name="ACC-SINV-RESUME")

        def complete_pause():
            sub.current_invoice_start = "2026-08-01"
            sub.current_invoice_end = "2026-08-31"
            sub.stripe_erpnext_pause_active = 0

        def process_resumed_period(posting_date):
            sub.generate_invoice(
                from_date=sub.current_invoice_start,
                to_date=sub.current_invoice_end,
                posting_date=posting_date,
            )
            sub.current_invoice_start = "2026-09-01"
            sub.current_invoice_end = "2026-09-30"

        sub.update(
            {
                "stripe_subscription_id": "sub_due_resume",
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Paused",
                "stripe_pause_start": "2026-07-01",
                "stripe_resume_on": "2026-10-01",
                "stripe_pause_start_at": "1782889200",
                "stripe_resume_at": "1790838000",
                "stripe_pause_cycles": 3,
                "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
                "complete_billing_pause": Mock(side_effect=complete_pause),
                "generate_invoice": Mock(return_value=invoice),
                "_process_subscription": Mock(side_effect=process_resumed_period),
                "_set_lock_flag": Mock(),
            }
        )
        stripe_api = _StripeSubscriptionAPI(
            subscription_id="sub_due_resume",
            pause_collection=None,
        )
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()
        self.module.nowdate = lambda: "2026-08-01"
        self.module._utc_now_timestamp = lambda: 1785567600

        result = self.module._sync_subscription(sub, "resume")

        self.assertTrue(result["handled"])
        self.assertFalse(result["scheduled"])
        sub.complete_billing_pause.assert_called_once_with()
        sub._process_subscription.assert_called_once_with("2026-08-01")
        sub.generate_invoice.assert_called_once_with(
            from_date="2026-08-01",
            to_date="2026-08-31",
            posting_date="2026-08-01",
        )
        self.assertEqual(sub.current_invoice_start, "2026-09-01")
        self.assertEqual(sub.current_invoice_end, "2026-09-30")
        self.assertEqual(sub["stripe_erpnext_pause_active"], 0)

    def test_public_subscription_actions_are_serialized_per_subscription(self):
        sub = self.subscription()
        fresh_sub = self.subscription()
        fresh_sub["fresh_after_lock"] = True
        events = []

        class _RecordingLock:
            def __init__(self, name, timeout=30):
                events.append(("created", name, timeout))

            def __enter__(self):
                events.append(("entered",))
                return self

            def __exit__(self, exc_type, exc, traceback):
                events.append(("exited",))
                return False

        self.module.MariaDBNamedLock = _RecordingLock
        self.module._require_subscription_action_role = Mock()
        self.module._require_subscription_permission = Mock(return_value=sub)
        self.module.frappe.get_doc = Mock(return_value=fresh_sub)
        self.module._is_enabled = Mock(return_value=True)
        self.module._sync_subscription = Mock(return_value={"handled": True})

        result = self.module.sync_subscription_action(sub.name, "pause", pause_cycles=1)

        self.assertEqual(result, {"handled": True})
        self.assertEqual(
            events,
            [
                ("created", "stripe-subscription-action-ACC-SUB-0001", 30),
                ("entered",),
                ("exited",),
            ],
        )
        self.module.frappe.get_doc.assert_called_once_with("Subscription", sub.name)
        self.module._sync_subscription.assert_called_once_with(fresh_sub, "pause", pause_cycles=1)

    def test_resume_keeps_erp_blocked_until_boundary_when_stripe_is_already_unpaused(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_pause",
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Paused",
                "stripe_pause_start": "2026-07-01",
                "stripe_resume_on": "2026-08-01",
                "stripe_pause_start_at": "1782889200",
                "stripe_resume_at": "1785567600",
                "current_invoice_start": "2026-07-01",
                "current_invoice_end": "2026-07-31",
                "end_date": "2027-01-31",
                "follow_calendar_months": 0,
                "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
            }
        )
        stripe_api = _StripeSubscriptionAPI(pause_collection=None)
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        result = self.module._sync_subscription(sub, "resume")

        self.assertTrue(result["handled"])
        self.assertEqual(result["resume_on"], "2026-08-01")
        self.assertTrue(result["scheduled"])
        self.assertEqual(sub["stripe_erpnext_pause_active"], 1)
        self.assertEqual(sub["stripe_paused"], 0)
        stripe_api.modify.assert_not_called()

    def test_resume_preserves_legacy_stripe_only_pause_without_local_dates(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_legacy_pause",
                "stripe_erpnext_pause_active": 0,
                "stripe_paused": 1,
            }
        )
        stripe_api = _StripeSubscriptionAPI(
            subscription_id="sub_legacy_pause",
            pause_collection={"behavior": "void"},
        )
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        result = self.module._sync_subscription(sub, "resume")

        self.assertTrue(result["handled"])
        stripe_api.modify.assert_called_once_with(
            "sub_legacy_pause",
            pause_collection="",
            api_key="test-key",
            idempotency_key="erpnext-ACC-SUB-0001-resume_op_test-1",
        )
        self.module._set_subscription_fields.assert_any_call(
            sub.name,
            {
                "stripe_erpnext_pause_active": 0,
                "stripe_pause_state": "",
                "stripe_pause_operation_id": "",
                "stripe_pending_resume_on": None,
                "stripe_pending_resume_at": "",
                "stripe_resume_cancel_before_start": 0,
                "stripe_operation_attempt": 0,
                "stripe_pause_cycles": 0,
                "stripe_pause_cadence_snapshot": "",
                "stripe_pause_last_reconciled_at": None,
                "stripe_paused": 0,
            },
            required=self.module.COORDINATED_PAUSE_FIELDS,
        )

    def test_cancel_clears_coordinated_pause_and_sets_native_cancellation_date(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_pause",
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Paused",
                "stripe_pause_operation_id": "pause_existing",
                "stripe_pause_cycles": 1,
            }
        )
        stripe_api = _StripeSubscriptionAPI(pause_collection={"behavior": "void"})
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        result = self.module._sync_subscription(sub, "cancel")

        self.assertTrue(result["handled"])
        stripe_api.delete.assert_called_once_with(
            "sub_pause",
            api_key="test-key",
            idempotency_key="erpnext-ACC-SUB-0001-cancel_op_test-1",
        )
        self.module._set_subscription_fields.assert_any_call(
            sub.name,
            {
                "stripe_erpnext_pause_active": 0,
                "stripe_pause_state": "",
                "stripe_pause_operation_id": "",
                "stripe_pending_resume_on": None,
                "stripe_pending_resume_at": "",
                "stripe_resume_cancel_before_start": 0,
                "stripe_operation_attempt": 0,
                "stripe_pause_cycles": 0,
                "stripe_pause_cadence_snapshot": "",
                "stripe_pause_last_reconciled_at": None,
                "stripe_paused": 0,
                "status": "Cancelled",
                "cancelation_date": "2026-07-10",
            },
            required=self.module.COORDINATED_PAUSE_FIELDS,
            update_modified=True,
        )

    def test_cancel_retry_recovers_after_remote_success_and_local_failure(self):
        sub = self.subscription()
        sub["stripe_subscription_id"] = "sub_cancel_retry"
        stripe_api = _StripeSubscriptionAPI(subscription_id="sub_cancel_retry")
        self.module.stripe.Subscription = stripe_api
        writes = 0

        def fail_local_completion(*args, **kwargs):
            nonlocal writes
            writes += 1
            if writes == 3:
                raise RuntimeError("cancel local completion failed")

        self.module._set_subscription_fields = Mock(side_effect=fail_local_completion)

        with self.assertRaisesRegex(RuntimeError, "cancel local completion failed"):
            self.module._sync_subscription(sub, "cancel")

        self.assertEqual(sub["stripe_pause_state"], "Cancelling")
        self.assertEqual(stripe_api.remote.status, "canceled")

        self.module._set_subscription_fields = Mock()
        result = self.module._sync_subscription(sub, "cancel")

        self.assertTrue(result["handled"])
        self.assertEqual(stripe_api.delete.call_count, 1)
        self.assertEqual(sub["status"], "Cancelled")
        self.assertEqual(sub["stripe_pause_state"], "")

    def test_cancel_requires_canonical_terminal_confirmation(self):
        sub = self.subscription()
        sub["stripe_subscription_id"] = "sub_cancel_unconfirmed"
        stripe_api = _StripeSubscriptionAPI(subscription_id="sub_cancel_unconfirmed")
        stripe_api.remote.status = "active"
        stripe_api.delete = Mock(return_value={})
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        with self.assertRaisesRegex(Exception, "canonically confirm"):
            self.module._sync_subscription(sub, "cancel")

        self.assertEqual(sub["stripe_pause_state"], "Cancelling")
        self.assertNotEqual(sub.get("status"), "Cancelled")

    def test_cancel_does_not_treat_an_initial_wrong_account_404_as_confirmation(self):
        sub = self.subscription()
        sub["stripe_subscription_id"] = "sub_wrong_account"
        stripe_api = _StripeSubscriptionAPI(subscription_id="sub_wrong_account")
        stripe_api.retrieve = Mock(side_effect=_ResourceMissingError("No such subscription"))
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()
        self.module.upsert_event = Mock()
        self.module.frappe.db.commit.reset_mock()

        with self.assertRaisesRegex(Exception, "configured company account"):
            self.module._sync_subscription(sub, "cancel")

        stripe_api.delete.assert_not_called()
        self.assertNotEqual(sub.get("stripe_pause_state"), "Cancelling")
        self.assertNotEqual(sub.get("status"), "Cancelled")
        self.module.upsert_event.assert_not_called()
        self.module.frappe.db.commit.assert_not_called()

    def test_cancel_rejects_a_remote_subscription_for_another_customer(self):
        sub = self.subscription()
        sub["stripe_subscription_id"] = "sub_wrong_customer"
        stripe_api = _StripeSubscriptionAPI(
            subscription_id="sub_wrong_customer",
            customer="cus_someone_else",
        )
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        with self.assertRaisesRegex(Exception, "different Stripe customer"):
            self.module._sync_subscription(sub, "cancel")

        stripe_api.delete.assert_not_called()
        self.assertNotEqual(sub.get("stripe_pause_state"), "Cancelling")
        self.assertNotEqual(sub.get("status"), "Cancelled")
        self.module.upsert_event.assert_not_called()
        self.module.frappe.db.commit.assert_not_called()

    def test_cancel_treats_incomplete_expired_as_terminal(self):
        sub = self.subscription()
        sub["stripe_subscription_id"] = "sub_incomplete_expired"
        stripe_api = _StripeSubscriptionAPI(subscription_id="sub_incomplete_expired")
        stripe_api.remote.status = "incomplete_expired"
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        result = self.module._sync_subscription(sub, "cancel")

        self.assertTrue(result["handled"])
        stripe_api.delete.assert_not_called()
        self.assertEqual(sub["status"], "Cancelled")

    def test_resume_finishes_a_durable_cancel_instead_of_superseding_it(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_cancel_resume_race",
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Cancelling",
                "stripe_pause_operation_id": "cancel_pending",
            }
        )
        stripe_api = _StripeSubscriptionAPI(subscription_id="sub_cancel_resume_race")
        stripe_api.remote.status = "active"
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        result = self.module._sync_subscription(sub, "resume")

        self.assertEqual(result["action"], "cancel")
        self.assertEqual(sub["status"], "Cancelled")
        self.assertEqual(stripe_api.delete.call_count, 1)

    def test_resume_never_reactivates_a_terminal_stripe_subscription(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_terminal_resume",
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Paused",
                "stripe_pause_start": "2026-07-01",
                "stripe_resume_on": "2026-08-01",
            }
        )
        stripe_api = _StripeSubscriptionAPI(subscription_id="sub_terminal_resume")
        stripe_api.remote.status = "canceled"
        self.module.stripe.Subscription = stripe_api
        self.module._apply_subscription_state = Mock(return_value={})

        result = self.module._sync_subscription(sub, "resume")

        self.assertFalse(result["handled"])
        self.assertEqual(result["reason"], "stripe_subscription_terminal")
        self.module._apply_subscription_state.assert_called_once()
        stripe_api.modify.assert_not_called()

    def test_terminal_webhook_state_clears_the_pause_cadence_snapshot(self):
        self.module.frappe.db.get_value = Mock(
            return_value={
                "status": "Active",
                "stripe_status": "active",
                "stripe_paused": 1,
                "cancel_at_period_end": 0,
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Paused",
                "stripe_pause_cadence_snapshot": '{"version":1}',
            }
        )
        self.module.frappe.db.set_value = Mock()
        self.module._erp_status_options = lambda: {"Cancelled"}

        self.module._apply_subscription_state(
            "ACC-SUB-0001",
            {"status": "canceled", "pause_collection": None},
        )

        values = self.module.frappe.db.set_value.call_args.args[2]
        self.assertEqual(values["stripe_pause_cadence_snapshot"], "")
        self.assertEqual(values["stripe_erpnext_pause_active"], 0)
        self.assertEqual(values["status"], "Cancelled")

    def test_canonical_reconciliation_skips_a_noop_write(self):
        self.module.frappe.db.get_value = Mock(
            return_value={
                "status": "Active",
                "stripe_status": "active",
                "stripe_paused": 0,
                "cancel_at_period_end": 0,
                "cancelation_date": None,
            }
        )
        self.module.frappe.db.set_value = Mock()
        self.module.frappe.db.commit.reset_mock()
        self.module._erp_status_options = lambda: {"Active"}

        self.module._apply_subscription_state(
            "ACC-SUB-0001",
            {
                "status": "active",
                "pause_collection": None,
                "cancel_at_period_end": False,
            },
        )

        self.module.frappe.db.set_value.assert_not_called()
        self.module.frappe.db.commit.assert_not_called()

    def test_canonical_cancellation_bumps_modified_for_stale_form_protection(self):
        self.module.frappe.db.get_value = Mock(
            return_value={
                "status": "Active",
                "stripe_status": "active",
                "stripe_paused": 0,
                "cancel_at_period_end": 0,
                "cancelation_date": None,
            }
        )
        self.module.frappe.db.set_value = Mock()
        self.module._erp_status_options = lambda: {"Cancelled"}

        self.module._apply_subscription_state(
            "ACC-SUB-0001",
            {
                "status": "canceled",
                "pause_collection": None,
                "cancel_at_period_end": False,
            },
        )

        call = self.module.frappe.db.set_value.call_args
        self.assertTrue(call.kwargs["update_modified"])
        self.assertEqual(call.args[2]["status"], "Cancelled")
        self.assertEqual(call.args[2]["cancelation_date"], "2026-07-10")

    def test_active_webhook_cannot_undo_a_durable_cancellation_intent(self):
        self.module.frappe.db.get_value = Mock(
            return_value={
                "status": "Cancelled",
                "stripe_status": "active",
                "stripe_paused": 0,
                "cancel_at_period_end": 0,
                "cancelation_date": "2026-07-10",
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Cancelling",
                "stripe_pause_operation_id": "cancel_pending",
            }
        )
        self.module.frappe.db.set_value = Mock()
        self.module._erp_status_options = lambda: {"Active", "Cancelled"}

        self.module._apply_subscription_state(
            "ACC-SUB-0001",
            {
                "status": "active",
                "pause_collection": None,
                "cancel_at_period_end": False,
            },
        )

        if self.module.frappe.db.set_value.called:
            values = self.module.frappe.db.set_value.call_args.args[2]
            self.assertNotIn("status", values)
            self.assertNotIn("cancelation_date", values)

    def test_general_status_sync_ignores_pause_only_cadence_restrictions(self):
        sub = self.subscription()
        remote = _Obj(status="active", billing_mode=_Obj(type="flexible"))

        self.module._validate_cadence_for_status_sync(sub, remote)

        sub["stripe_erpnext_pause_active"] = 1
        sub["stripe_pause_state"] = "Paused"
        with self.assertRaisesRegex(Exception, "flexible billing"):
            self.module._validate_cadence_for_status_sync(sub, remote)

        remote.status = "canceled"
        self.module._validate_cadence_for_status_sync(sub, remote)

    def test_plan_change_is_rejected_while_pause_cycle_snapshot_is_active(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_pause",
                "stripe_erpnext_pause_active": 1,
            }
        )
        self.module._sync_subscription_plan = Mock()

        result = self.module._sync_subscription(sub, "plan_change")

        self.assertFalse(result["handled"])
        self.assertEqual(result["reason"], "pause_active")
        self.module._sync_subscription_plan.assert_not_called()

    def test_plan_change_validates_ownership_before_committing_its_event(self):
        sub = self.subscription()
        sub["stripe_subscription_id"] = "sub_plan_change"
        stripe_api = _StripeSubscriptionAPI(subscription_id="sub_plan_change")
        order = []

        def retrieve(subscription_id, **kwargs):
            order.append("ownership")
            return stripe_api.remote

        stripe_api.retrieve = Mock(side_effect=retrieve)
        self.module.stripe.Subscription = stripe_api
        self.module.upsert_event = Mock(side_effect=lambda *args, **kwargs: order.append("event"))
        self.module.frappe.db.commit = Mock(side_effect=lambda: order.append("commit"))
        self.module._sync_subscription_plan = Mock(side_effect=lambda *args: order.append("mutation"))

        self.module._sync_subscription(sub, "plan_change")

        self.assertEqual(order[:3], ["ownership", "event", "commit"])
        self.assertEqual(order[3], "mutation")

    def test_plan_change_rejects_a_wrong_owner_returned_after_mutation(self):
        sub = self.subscription()
        sub["stripe_subscription_id"] = "sub_plan_change"
        stripe_api = _StripeSubscriptionAPI(subscription_id="sub_plan_change")
        wrong_remote = _Obj(stripe_api.remote)
        wrong_remote.metadata = _Obj(
            doctype="Subscription",
            docname="ACC-SUB-OTHER",
            company="COEngine Service Inc.",
            company_abbr="COE",
        )
        stripe_api.modify = Mock(return_value=wrong_remote)
        self.module.stripe.Subscription = stripe_api
        self.module._build_stripe_subscription_items = Mock(return_value=[])
        self.module._build_subscription_pricing_params = Mock(return_value={})
        self.module._set_subscription_fields = Mock()

        with self.assertRaisesRegex(Exception, "different ERPNext Subscription"):
            self.module._sync_subscription_plan(sub, "sub_plan_change", "COE")

        self.module._set_subscription_fields.assert_not_called()

    def test_subscription_webhook_applies_canonical_remote_state_under_action_lock(self):
        sub = self.subscription()
        sub["stripe_subscription_id"] = "sub_pause"
        self.module.frappe.db.get_value = Mock(return_value=sub.name)
        self.module.frappe.get_doc = Mock(return_value=sub)
        stripe_api = _StripeSubscriptionAPI(pause_collection=None)
        self.module.stripe.Subscription = stripe_api
        self.module._apply_subscription_state = Mock(
            return_value={
                "prev_stripe_status": "active",
                "prev_paused": False,
                "stripe_status": "active",
                "paused": False,
            }
        )
        self.module._pick_lifecycle_kind = Mock(return_value=None)
        lock_names = []

        class _RecordingLock:
            def __init__(self, name, timeout=30):
                lock_names.append(name)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        self.module.MariaDBNamedLock = _RecordingLock
        stale_event = {
            "data": {
                "object": {
                    "id": "sub_pause",
                    "status": "active",
                    "pause_collection": {"behavior": "void"},
                }
            }
        }

        result = self.module.sync_subscription_from_webhook_event(stale_event)

        self.assertTrue(result["handled"])
        self.assertEqual(lock_names, ["stripe-subscription-action-ACC-SUB-0001"])
        applied_state = self.module._apply_subscription_state.call_args.args[1]
        self.assertIsNone(applied_state["pause_collection"])
        stripe_api.retrieve.assert_called_once_with("sub_pause", api_key="test-key")

    def test_subscription_webhook_rejects_a_canonical_remote_owned_by_another_document(self):
        sub = self.subscription()
        sub["stripe_subscription_id"] = "sub_pause"
        self.module.frappe.db.get_value = Mock(return_value=sub.name)
        self.module.frappe.get_doc = Mock(return_value=sub)
        stripe_api = _StripeSubscriptionAPI(
            metadata={
                "doctype": "Subscription",
                "docname": "ACC-SUB-OTHER",
                "company": "COEngine Service Inc.",
                "company_abbr": "COE",
            }
        )
        self.module.stripe.Subscription = stripe_api
        self.module._apply_subscription_state = Mock()

        class _NullLock:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        self.module.MariaDBNamedLock = _NullLock

        with self.assertRaisesRegex(Exception, "different ERPNext Subscription"):
            self.module.sync_subscription_from_webhook_event(
                {"data": {"object": {"id": "sub_pause"}}}
            )

        self.module._apply_subscription_state.assert_not_called()

    def test_manual_reconciliation_rebases_an_unexpected_early_unpause(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_early_unpause",
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Paused",
                "stripe_pause_operation_id": "",
                "stripe_pause_start": "2026-07-01",
                "stripe_resume_on": "2026-10-01",
                "stripe_pause_start_at": "1782889200",
                "stripe_resume_at": "1790838000",
                "stripe_pause_cycles": 3,
                "stripe_pause_cadence_snapshot": (
                    '{"billing_cycle_anchor":1782889200,'
                    '"follow_calendar_months":0,"interval":"month",'
                    '"interval_count":1,"version":1}'
                ),
            }
        )
        stripe_api = _StripeSubscriptionAPI(
            subscription_id="sub_early_unpause",
            pause_collection=None,
            current_period_start=1782889200,
            current_period_end=1785567600,
            billing_cycle_anchor=1782889200,
        )
        stripe_api.remote.status = "active"
        self.module.stripe.Subscription = stripe_api
        self.module._require_subscription_permission = Mock(return_value=sub)
        self.module.frappe.get_doc = Mock(return_value=sub)
        self.module.frappe.db.get_value = Mock(
            return_value={
                **dict(sub),
                "stripe_status": "active",
                "stripe_paused": 1,
                "cancel_at_period_end": 0,
            }
        )
        self.module.frappe.db.set_value = Mock()
        self.module._erp_status_options = lambda: {"Active"}
        self.module._utc_now_timestamp = lambda: 1784098800

        class _NullLock:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        self.module.MariaDBNamedLock = _NullLock

        result = self.module.reconcile_subscription_status(sub.name)

        self.assertFalse(result["paused"])
        values = self.module.frappe.db.set_value.call_args.args[2]
        self.assertEqual(values["stripe_resume_on"], "2026-08-01")
        self.assertEqual(values["stripe_resume_at"], "1785567600")
        self.assertEqual(values["stripe_pause_cycles"], 1)
        self.assertNotIn("stripe_pause_state", values)
        self.assertNotIn("stripe_erpnext_pause_active", values)
        self.assertEqual(sub["stripe_pause_state"], "Paused")
        self.assertEqual(sub["stripe_erpnext_pause_active"], 1)

    def test_manual_reconciliation_clears_an_unpause_before_the_hold_starts(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_unpaused_before_start",
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Paused",
                "stripe_pause_operation_id": "",
                "stripe_pause_start": "2026-08-01",
                "stripe_resume_on": "2026-10-01",
                "stripe_pause_start_at": "1785567600",
                "stripe_resume_at": "1790838000",
                "stripe_pause_cycles": 2,
                "stripe_pause_cadence_snapshot": (
                    '{"billing_cycle_anchor":1785567600,'
                    '"follow_calendar_months":0,"interval":"month",'
                    '"interval_count":1,"version":1}'
                ),
            }
        )
        stripe_api = _StripeSubscriptionAPI(
            subscription_id="sub_unpaused_before_start",
            pause_collection=None,
            current_period_start=1782889200,
            current_period_end=1785567600,
            billing_cycle_anchor=1785567600,
        )
        stripe_api.remote.status = "active"
        self.module.stripe.Subscription = stripe_api
        self.module._require_subscription_permission = Mock(return_value=sub)
        self.module.frappe.get_doc = Mock(return_value=sub)
        self.module.frappe.db.get_value = Mock(
            return_value={
                **dict(sub),
                "stripe_status": "active",
                "stripe_paused": 1,
                "cancel_at_period_end": 0,
            }
        )
        self.module.frappe.db.set_value = Mock()
        self.module._erp_status_options = lambda: {"Active"}
        self.module._utc_now_timestamp = lambda: 1784098800

        class _NullLock:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        self.module.MariaDBNamedLock = _NullLock

        self.module.reconcile_subscription_status(sub.name)

        values = self.module.frappe.db.set_value.call_args.args[2]
        self.assertEqual(values["stripe_erpnext_pause_active"], 0)
        self.assertEqual(values["stripe_pause_state"], "")
        self.assertEqual(values["stripe_pause_cycles"], 0)
        self.assertEqual(values["stripe_pause_cadence_snapshot"], "")

    def test_pause_retry_recovers_local_state_after_remote_success(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_pause",
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Pausing",
                "stripe_pause_operation_id": "pause_op_persisted",
                "stripe_pause_start": "2026-08-01",
                "stripe_resume_on": "2026-09-01",
                "stripe_pause_start_at": "1785567600",
                "stripe_resume_at": "1788246000",
                "stripe_pause_cycles": 1,
                "current_invoice_start": "2026-08-01",
                "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
            }
        )
        stripe_api = _StripeSubscriptionAPI(
            pause_collection={"behavior": "void", "resumes_at": 1788246000},
        )
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()
        self.module.nowdate = lambda: "2026-09-02"

        result = self.module._sync_subscription(sub, "pause", pause_cycles=1)

        self.assertTrue(result["handled"])
        stripe_api.modify.assert_not_called()
        self.module._set_subscription_fields.assert_called_once()
        self.assertEqual(sub["stripe_pause_state"], "Paused")

    def test_pause_does_not_accept_a_matching_hold_after_the_period_was_charged(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_billed_pause",
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Pausing",
                "stripe_pause_operation_id": "pause_billed",
                "stripe_pause_start": "2026-08-01",
                "stripe_resume_on": "2026-09-01",
                "stripe_pause_start_at": "1785567600",
                "stripe_resume_at": "1788246000",
                "stripe_pause_cycles": 1,
            }
        )
        stripe_api = _StripeSubscriptionAPI(
            subscription_id="sub_billed_pause",
            pause_collection={"behavior": "void", "resumes_at": 1788246000},
        )
        self.invoice_list.return_value = _Obj(
            data=[
                _Obj(
                    id="in_charged",
                    status="paid",
                    amount_paid=14900,
                    period_start=1785567600,
                    period_end=1788246000,
                    lines=_Obj(data=[]),
                )
            ],
            has_more=False,
        )
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        with self.assertRaisesRegex(Exception, "already billed or charged"):
            self.module._sync_subscription(sub, "pause", pause_cycles=1)

        stripe_api.modify.assert_not_called()
        self.assertEqual(sub["stripe_pause_state"], "Pausing")

    def test_pause_retry_after_boundary_clears_an_unestablished_local_hold(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_expired_pause",
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Pausing",
                "stripe_pause_operation_id": "pause_expired",
                "stripe_pause_start": "2026-08-01",
                "stripe_resume_on": "2026-10-01",
                "stripe_pause_start_at": "1785567600",
                "stripe_resume_at": "1790838000",
                "stripe_pause_cycles": 2,
                "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
            }
        )
        stripe_api = _StripeSubscriptionAPI(
            subscription_id="sub_expired_pause",
            pause_collection=None,
            current_period_end=1788246000,
        )
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()
        self.module.nowdate = lambda: "2026-08-02"
        self.module._utc_now_timestamp = lambda: 1785654000

        result = self.module._sync_subscription(sub, "pause", pause_cycles=2)

        self.assertFalse(result["handled"])
        self.assertEqual(result["reason"], "stripe_pause_not_established_before_boundary")
        stripe_api.modify.assert_not_called()
        self.assertEqual(sub["stripe_pause_state"], "")
        self.assertEqual(sub["stripe_erpnext_pause_active"], 0)

    def test_pause_retry_uses_a_new_idempotency_generation_after_cached_failure(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_retry_generation",
                "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
            }
        )
        stripe_api = _StripeSubscriptionAPI(
            subscription_id="sub_retry_generation",
            current_period_end=1785567600,
        )
        calls = 0

        def modify_with_cached_failure(subscription_id, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("cached Stripe 500")
            stripe_api.remote.pause_collection = kwargs["pause_collection"]
            return stripe_api.remote

        stripe_api.modify = Mock(side_effect=modify_with_cached_failure)
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        with self.assertRaisesRegex(RuntimeError, "cached Stripe 500"):
            self.module._sync_subscription(sub, "pause", pause_cycles=1)

        result = self.module._sync_subscription(sub, "pause", pause_cycles=1)

        self.assertTrue(result["handled"])
        self.assertEqual(
            [call.kwargs["idempotency_key"] for call in stripe_api.modify.call_args_list],
            [
                "erpnext-ACC-SUB-0001-pause_op_test-1",
                "erpnext-ACC-SUB-0001-pause_op_test-2",
            ],
        )

    def test_pause_retry_rejects_non_void_remote_behavior(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_wrong_pause_behavior",
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Pausing",
                "stripe_pause_operation_id": "pause_wrong_behavior",
                "stripe_pause_start": "2026-08-01",
                "stripe_resume_on": "2026-09-01",
                "stripe_pause_start_at": "1785567600",
                "stripe_resume_at": "1788246000",
                "stripe_pause_cycles": 1,
            }
        )
        stripe_api = _StripeSubscriptionAPI(
            subscription_id="sub_wrong_pause_behavior",
            current_period_end=1785567600,
            pause_collection={"behavior": "keep_as_draft", "resumes_at": 1788246000},
        )
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        with self.assertRaisesRegex(Exception, "different pause behavior"):
            self.module._sync_subscription(sub, "pause", pause_cycles=1)

        stripe_api.modify.assert_not_called()

    def test_pause_recovers_after_remote_success_and_local_completion_failure(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_pause",
                "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
            }
        )
        stripe_api = _StripeSubscriptionAPI(current_period_end=1785567600)
        self.module.stripe.Subscription = stripe_api
        local_writes = 0

        def fail_completed_write(*args, **kwargs):
            nonlocal local_writes
            local_writes += 1
            if local_writes == 3:
                raise RuntimeError("local completion failed")

        self.module._set_subscription_fields = Mock(side_effect=fail_completed_write)

        with self.assertRaisesRegex(RuntimeError, "local completion failed"):
            self.module._sync_subscription(sub, "pause", pause_cycles=1)

        self.assertEqual(sub["stripe_pause_state"], "Pausing")
        self.assertEqual(sub["stripe_pause_operation_id"], "pause_op_test")
        self.assertIsNotNone(stripe_api.remote.pause_collection)

        self.module.nowdate = lambda: "2026-09-02"
        self.module._set_subscription_fields = Mock()
        result = self.module._sync_subscription(sub, "pause", pause_cycles=1)

        self.assertTrue(result["handled"])
        self.assertEqual(sub["stripe_pause_state"], "Paused")
        self.assertEqual(stripe_api.modify.call_count, 1)

    def test_new_pause_after_cancel_uses_a_new_idempotency_operation(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_pause",
                "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
            }
        )
        operation_ids = iter(["pause_first", "resume_first", "pause_second"])
        self.module._new_operation_id = lambda action: next(operation_ids)
        stripe_api = _StripeSubscriptionAPI(current_period_end=1785567600)
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        self.module._sync_subscription(sub, "pause", pause_cycles=1)
        self.module._sync_subscription(sub, "resume")
        self.module._sync_subscription(sub, "pause", pause_cycles=1)

        idempotency_keys = [
            call.kwargs["idempotency_key"] for call in stripe_api.modify.call_args_list
        ]
        self.assertEqual(
            idempotency_keys,
            [
                "erpnext-ACC-SUB-0001-pause_first-1",
                "erpnext-ACC-SUB-0001-resume_first-1",
                "erpnext-ACC-SUB-0001-pause_second-1",
            ],
        )

    def test_resume_retry_keeps_persisted_boundary_after_clock_crosses_cycle(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_pause",
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Resuming",
                "stripe_pause_operation_id": "resume_persisted",
                "stripe_pending_resume_on": "2026-08-01",
                "stripe_pending_resume_at": "1785567600",
                "stripe_resume_cancel_before_start": 0,
                "stripe_pause_start": "2026-07-01",
                "stripe_pause_start_at": "1782889200",
                "stripe_resume_on": "2026-10-01",
                "stripe_pause_cycles": 1,
                "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
                "complete_billing_pause": Mock(),
                "_process_subscription": Mock(),
            }
        )
        self.module.nowdate = lambda: "2026-09-05"
        self.module._utc_now_timestamp = lambda: 1788591600
        stripe_api = _StripeSubscriptionAPI(pause_collection=None)
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        result = self.module._sync_subscription(sub, "resume")

        self.assertEqual(result["resume_on"], "2026-08-01")
        self.assertFalse(result["scheduled"])
        self.assertEqual(sub["stripe_resume_on"], "2026-08-01")
        self.assertEqual(sub["stripe_pause_cycles"], 0)
        sub.complete_billing_pause.assert_called_once_with()
        sub._process_subscription.assert_called_once_with("2026-08-01")
        stripe_api.modify.assert_not_called()

    def test_due_resume_rejects_a_nonvoid_stripe_invoice_in_the_pause_window(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_pause",
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Resuming",
                "stripe_pause_operation_id": "resume_persisted",
                "stripe_pending_resume_on": "2026-08-01",
                "stripe_pending_resume_at": "1785567600",
                "stripe_resume_cancel_before_start": 0,
                "stripe_pause_start": "2026-07-01",
                "stripe_pause_start_at": "1782889200",
                "stripe_pause_cycles": 1,
                "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
                "complete_billing_pause": Mock(),
                "_process_subscription": Mock(),
            }
        )
        self.module._utc_now_timestamp = lambda: 1788591600
        self.module.stripe.Subscription = _StripeSubscriptionAPI(pause_collection=None)
        self.invoice_list.return_value = _Obj(
            data=[
                _Obj(
                    status="paid",
                    period_start=1782889200,
                    period_end=1785567600,
                )
            ],
            has_more=False,
        )

        with self.assertRaisesRegex(Exception, "already billed or charged"):
            self.module._sync_subscription(sub, "resume")

        sub.complete_billing_pause.assert_not_called()

    def test_automatic_due_resume_checks_the_stripe_invoice_window(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_pause",
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Paused",
                "stripe_pause_start_at": "1782889200",
                "stripe_resume_at": "1785567600",
            }
        )
        self.module._utc_now_timestamp = lambda: 1788591600
        self.module.stripe.Subscription = _StripeSubscriptionAPI(pause_collection=None)
        self.invoice_list.return_value = _Obj(
            data=[
                _Obj(
                    status="open",
                    period_start=1782889200,
                    period_end=1785567600,
                )
            ],
            has_more=False,
        )

        with self.assertRaisesRegex(Exception, "already billed or charged"):
            self.module.retrieve_subscription_pause_state(sub)

    def test_terminal_pause_state_wins_before_cadence_validation(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_pause",
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Paused",
            }
        )
        stripe_api = _StripeSubscriptionAPI(pause_collection=None)
        stripe_api.remote.status = "canceled"
        stripe_api.remote.billing_mode = _Obj(type="flexible")
        self.module.stripe.Subscription = stripe_api

        result = self.module.retrieve_subscription_pause_state(sub)

        self.assertEqual(result["remote"].status, "canceled")
        self.invoice_list.assert_not_called()

    def test_late_pause_cancellation_retry_does_not_clear_stripe_after_boundary(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_late_cancel",
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Resuming",
                "stripe_pause_operation_id": "resume_cancel_before_start",
                "stripe_pending_resume_on": "2026-08-01",
                "stripe_pending_resume_at": "1785567600",
                "stripe_resume_cancel_before_start": 1,
                "stripe_pause_start": "2026-08-01",
                "stripe_resume_on": "2026-09-01",
                "stripe_pause_cycles": 0,
            }
        )
        self.module.nowdate = lambda: "2026-08-02"
        self.module._utc_now_timestamp = lambda: 1785654000
        stripe_api = _StripeSubscriptionAPI(
            subscription_id="sub_late_cancel",
            pause_collection={"behavior": "void", "resumes_at": 1788246000},
        )
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        with self.assertRaisesRegex(Exception, "requested resume boundary passed"):
            self.module._sync_subscription(sub, "resume")

        stripe_api.modify.assert_not_called()
        self.assertEqual(sub["stripe_pause_state"], "Resuming")

    def test_late_resume_retry_does_not_unpause_stripe_retroactively(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_late_resume",
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Resuming",
                "stripe_pause_operation_id": "resume_late",
                "stripe_pending_resume_on": "2026-08-01",
                "stripe_pending_resume_at": "1785567600",
                "stripe_resume_cancel_before_start": 0,
                "stripe_pause_start": "2026-07-01",
                "stripe_resume_on": "2026-10-01",
                "stripe_pause_cycles": 1,
            }
        )
        self.module.nowdate = lambda: "2026-09-05"
        self.module._utc_now_timestamp = lambda: 1788591600
        stripe_api = _StripeSubscriptionAPI(
            subscription_id="sub_late_resume",
            pause_collection={"behavior": "void", "resumes_at": 1790838000},
        )
        self.module.stripe.Subscription = stripe_api
        self.module._set_subscription_fields = Mock()

        with self.assertRaisesRegex(Exception, "requested resume boundary passed"):
            self.module._sync_subscription(sub, "resume")

        stripe_api.modify.assert_not_called()
        self.assertEqual(sub["stripe_pause_state"], "Resuming")

    def test_resume_recovers_after_remote_success_and_local_completion_failure(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_pause",
                "stripe_erpnext_pause_active": 1,
                "stripe_pause_state": "Paused",
                "stripe_pause_start": "2026-07-01",
                "stripe_resume_on": "2026-10-01",
                "stripe_pause_start_at": "1782889200",
                "stripe_resume_at": "1790838000",
                "stripe_pause_cycles": 3,
                "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
            }
        )
        stripe_api = _StripeSubscriptionAPI(
            pause_collection={"behavior": "void", "resumes_at": 1790838000}
        )
        self.module.stripe.Subscription = stripe_api
        local_writes = 0

        def fail_completed_write(*args, **kwargs):
            nonlocal local_writes
            local_writes += 1
            if local_writes == 3:
                raise RuntimeError("local resume completion failed")

        self.module._set_subscription_fields = Mock(side_effect=fail_completed_write)

        with self.assertRaisesRegex(RuntimeError, "local resume completion failed"):
            self.module._sync_subscription(sub, "resume")

        self.assertEqual(sub["stripe_pause_state"], "Resuming")
        self.assertEqual(sub["stripe_pending_resume_on"], "2026-08-01")
        self.module.nowdate = lambda: "2026-07-20"
        self.module._set_subscription_fields = Mock()

        result = self.module._sync_subscription(sub, "resume")

        self.assertEqual(result["resume_on"], "2026-08-01")
        self.assertEqual(sub["stripe_resume_on"], "2026-08-01")
        self.assertEqual(sub["stripe_pause_cycles"], 1)
        self.assertEqual(stripe_api.modify.call_count, 1)

    def test_pause_rejects_an_overdue_unprocessed_billing_period(self):
        sub = self.subscription()
        sub.update(
            {
                "stripe_subscription_id": "sub_overdue",
                "current_invoice_start": "2026-07-01",
                "get_billing_cycle_data": lambda: {"months": 1, "days": -1},
            }
        )
        retrieve = Mock(return_value=_Obj(pause_collection=None))
        self.module.stripe.Subscription = types.SimpleNamespace(retrieve=retrieve, modify=Mock())

        with self.assertRaisesRegex(Exception, "must be after today"):
            self.module._sync_subscription(sub, "pause", pause_cycles=1)

        retrieve.assert_not_called()

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
