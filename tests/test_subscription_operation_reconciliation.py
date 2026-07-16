import importlib
import sys
import types
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import Mock

from module_isolation import restore_modules


class SubscriptionOperationReconciliationTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)
        self.event_id = "local_outbound_ACC-SUB-0001_pause_persisted"
        self.row = {
            "name": "LOG-0001",
            "event_id": self.event_id,
            "event_type": "subscription.pause",
            "company_abbr": "COE",
            "stripe_object_id": "sub_pause",
            "retry_count": 0,
        }

        fake_frappe = types.ModuleType("frappe")
        fake_frappe.ValidationError = type("ValidationError", (Exception,), {})
        fake_frappe.set_user = Mock()
        fake_frappe.get_all = Mock(return_value=[self.row])
        fake_frappe.get_doc = Mock(
            return_value={
                "name": "ACC-SUB-0001",
                "stripe_pause_operation_id": "pause_persisted",
            }
        )
        fake_frappe.db = types.SimpleNamespace(
            get_value=Mock(side_effect=self._get_value),
            get_single_value=Mock(return_value=1),
            set_value=Mock(),
            sql=Mock(return_value=[]),
            commit=Mock(),
            rollback=Mock(),
        )
        fake_frappe.whitelist = lambda *args, **kwargs: lambda fn: fn
        fake_frappe.only_for = Mock()
        fake_frappe.conf = {}

        fake_utils = types.ModuleType("frappe.utils")
        fake_utils.now_datetime = lambda: datetime(2026, 7, 15, 12, 0)
        fake_utils.nowdate = lambda: date(2026, 7, 15)
        fake_utils.add_to_date = lambda value, minutes=0, **kwargs: value + timedelta(minutes=minutes)

        fake_stripe = types.ModuleType("stripe")
        fake_stripe.Event = types.SimpleNamespace(retrieve=Mock())

        fake_accounting = types.ModuleType("stripe_integration.stripe_integration.accounting")
        fake_accounting.MariaDBNamedLock = self._lock_class()

        fake_event_log = types.ModuleType("stripe_integration.stripe_integration.event_log")
        fake_event_log.mark_event_status = Mock()

        fake_fees = types.ModuleType("stripe_integration.stripe_integration.stripe_fees")
        fake_fees.audit_unposted_fee_entries = Mock(return_value={"missing": []})
        fake_fees.ensure_fee_posted = Mock()

        fake_pause = types.ModuleType("stripe_integration.stripe_integration.subscription_pause")
        fake_pause.PAUSE_ACTIVE_FIELD = "stripe_erpnext_pause_active"
        fake_pause.PAUSE_LAST_RECONCILED_AT_FIELD = "stripe_pause_last_reconciled_at"
        fake_pause.PAUSE_OPERATION_FIELD = "stripe_pause_operation_id"
        fake_pause.PAUSE_STATE_FIELD = "stripe_pause_state"
        fake_pause.PENDING_RESUME_AT_FIELD = "stripe_pending_resume_at"
        fake_pause.RESUME_AT_FIELD = "stripe_resume_at"
        fake_pause.STATE_CANCELLING = "Cancelling"
        fake_pause.STATE_PAUSING = "Pausing"
        fake_pause.STATE_RESUMING = "Resuming"

        fake_utils_module = types.ModuleType("stripe_integration.stripe_integration.utils")
        fake_utils_module.get_api_key = Mock(return_value="test-key")

        self.sync_action = Mock(return_value={"handled": True})
        fake_subscription_sync = types.ModuleType(
            "stripe_integration.stripe_integration.subscription_sync"
        )
        fake_subscription_sync._sync_subscription = self.sync_action

        self.dispatch_event = Mock(return_value={"handled": True})
        fake_webhook = types.ModuleType("stripe_integration.stripe_integration.webhook")
        fake_webhook._dispatch_verified_event = self.dispatch_event

        sys.modules["frappe"] = fake_frappe
        sys.modules["frappe.utils"] = fake_utils
        sys.modules["stripe"] = fake_stripe
        sys.modules["stripe_integration.stripe_integration.accounting"] = fake_accounting
        sys.modules["stripe_integration.stripe_integration.event_log"] = fake_event_log
        sys.modules["stripe_integration.stripe_integration.stripe_fees"] = fake_fees
        sys.modules["stripe_integration.stripe_integration.subscription_pause"] = fake_pause
        sys.modules["stripe_integration.stripe_integration.subscription_sync"] = fake_subscription_sync
        sys.modules["stripe_integration.stripe_integration.utils"] = fake_utils_module
        sys.modules["stripe_integration.stripe_integration.webhook"] = fake_webhook
        sys.modules.pop("stripe_integration.stripe_integration.reconciliation", None)

        self.module = importlib.import_module(
            "stripe_integration.stripe_integration.reconciliation"
        )
        self.module._set_retry_state = Mock()
        self.module._utc_now_timestamp = Mock(return_value=1784142000)
        self.mark_event_status = fake_event_log.mark_event_status
        self.frappe = fake_frappe

    def tearDown(self):
        restore_modules(self._orig_modules)

    def _get_value(self, doctype, name, fieldname, **kwargs):
        if doctype == "Stripe Event Log":
            return "Failed"
        if doctype == "Subscription":
            return "ACC-SUB-0001"
        return None

    @staticmethod
    def _lock_class():
        class _NoopLock:
            def __init__(self, name, timeout=30):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        return _NoopLock

    def test_hourly_reconciliation_replays_durable_pause_operation(self):
        result = self.module.reconcile_nonterminal_events()

        self.assertEqual(result[0]["status"], "Completed")
        subscription = self.frappe.get_doc.return_value
        self.sync_action.assert_called_once_with(subscription, "pause")
        self.mark_event_status.assert_called_with(self.event_id, "Completed", None)
        self.frappe.db.commit.assert_called()

    def test_hourly_reconciliation_keeps_paid_events_in_week_long_outage_window(self):
        self.row.update(
            {
                "event_id": "evt_invoice_paid",
                "event_type": "invoice.paid",
                "retry_count": 5,
            }
        )

        result = self.module.reconcile_nonterminal_events()

        self.assertEqual(result[0]["status"], "Completed")
        self.dispatch_event.assert_called_once()

    def test_exhausted_retry_rows_are_filtered_before_pagination(self):
        self.module.reconcile_nonterminal_events(limit=25)

        call = self.frappe.get_all.call_args
        self.assertEqual(
            call.kwargs["filters"]["retry_count"],
            ["<", self.module.MAX_EVENT_RETRIES],
        )
        self.assertEqual(call.kwargs["limit_page_length"], 25)
        self.assertEqual(call.kwargs["order_by"], "last_retry_at asc, modified asc")

    def test_hourly_reconciliation_ignores_superseded_operation(self):
        self.frappe.get_doc.return_value["stripe_pause_operation_id"] = "resume_newer"

        result = self.module.reconcile_nonterminal_events()

        self.assertEqual(result, [])
        self.sync_action.assert_not_called()
        self.mark_event_status.assert_called_once_with(
            self.event_id,
            "Ignored",
            "superseded_operation",
        )

    def test_completed_event_is_not_downgraded_after_subscription_lock_wait(self):
        statuses = iter(["Failed", "Completed"])

        def get_value(doctype, name, fieldname, **kwargs):
            if doctype == "Stripe Event Log":
                return next(statuses)
            return self._get_value(doctype, name, fieldname, **kwargs)

        self.frappe.db.get_value.side_effect = get_value
        self.frappe.get_doc.return_value["stripe_pause_operation_id"] = "resume_newer"

        result = self.module.reconcile_nonterminal_events()

        self.assertEqual(result, [])
        self.sync_action.assert_not_called()
        self.mark_event_status.assert_not_called()

    def test_hourly_reconciliation_replays_durable_cancel_operation(self):
        self.row.update(
            {
                "event_id": "local_outbound_ACC-SUB-0001_cancel_persisted",
                "event_type": "subscription.cancel",
            }
        )
        self.frappe.get_doc.return_value["stripe_pause_operation_id"] = "cancel_persisted"

        result = self.module.reconcile_nonterminal_events()

        self.assertEqual(result[0]["status"], "Completed")
        self.sync_action.assert_called_once_with(self.frappe.get_doc.return_value, "cancel")

    def test_unsupported_local_operation_is_terminal_instead_of_blocking_retries(self):
        self.row.update(
            {
                "event_id": "local_outbound_ACC-SUB-0001_plan_change_random",
                "event_type": "subscription.plan_change",
            }
        )

        result = self.module.reconcile_nonterminal_events()

        self.assertEqual(
            result,
            [
                {
                    "event_id": self.row["event_id"],
                    "status": "Ignored",
                    "reason": "unsupported_local_action",
                }
            ],
        )
        self.sync_action.assert_not_called()
        self.mark_event_status.assert_called_once_with(
            self.row["event_id"],
            "Ignored",
            "unsupported_local_action",
        )
        self.module._set_retry_state.assert_called_once_with("LOG-0001", 1)

    def test_invalid_local_operation_is_terminal_instead_of_blocking_retries(self):
        self.row["company_abbr"] = ""

        result = self.module.reconcile_nonterminal_events()

        self.assertEqual(
            result,
            [
                {
                    "event_id": self.event_id,
                    "status": "Ignored",
                    "reason": "missing_company_abbr",
                }
            ],
        )
        self.sync_action.assert_not_called()
        self.mark_event_status.assert_called_once_with(
            self.event_id,
            "Ignored",
            "missing_company_abbr",
        )
        self.module._set_retry_state.assert_called_once_with("LOG-0001", 1)

    def test_event_without_an_id_is_terminal_by_log_name(self):
        self.row.update(
            {
                "event_id": "",
                "event_type": "invoice.paid",
            }
        )

        result = self.module.reconcile_nonterminal_events()

        self.assertEqual(
            result,
            [
                {
                    "event_id": "",
                    "status": "Ignored",
                    "reason": "missing_event_id",
                }
            ],
        )
        self.mark_event_status.assert_not_called()
        self.frappe.db.set_value.assert_called_once_with(
            "Stripe Event Log",
            "LOG-0001",
            {"status": "Ignored", "error": "missing_event_id"},
            update_modified=False,
        )
        self.module._set_retry_state.assert_called_once_with("LOG-0001", 1)

    def test_stripe_event_without_a_company_is_terminal(self):
        self.row.update(
            {
                "event_id": "evt_invoice_paid",
                "event_type": "invoice.paid",
                "company_abbr": "",
            }
        )

        result = self.module.reconcile_nonterminal_events()

        self.assertEqual(
            result,
            [
                {
                    "event_id": "evt_invoice_paid",
                    "status": "Ignored",
                    "reason": "missing_company_abbr",
                }
            ],
        )
        self.mark_event_status.assert_called_once_with(
            "evt_invoice_paid",
            "Ignored",
            "missing_company_abbr",
        )
        self.module._set_retry_state.assert_called_once_with("LOG-0001", 1)

    def test_due_pause_reconciliation_isolates_each_subscription(self):
        successful = types.SimpleNamespace(process=Mock(return_value=True))
        validation_failure = types.SimpleNamespace(
            process=Mock(side_effect=self.frappe.ValidationError("remote pause still active"))
        )
        unchanged = types.SimpleNamespace(process=Mock(return_value=False))
        subscriptions = {
            "ACC-SUB-0001": successful,
            "ACC-SUB-0002": validation_failure,
            "ACC-SUB-0003": unchanged,
        }
        self.frappe.db.sql.return_value = [
            {"name": "ACC-SUB-0001"},
            {"name": "ACC-SUB-0002"},
            {"name": "ACC-SUB-0003"},
        ]
        self.frappe.get_doc.side_effect = lambda doctype, name: subscriptions[name]

        result = self.module.reconcile_due_subscription_pauses(limit=1000)

        query, values = self.frappe.db.sql.call_args.args
        self.assertIn("`docstatus` < 2", query)
        self.assertNotIn("`docstatus` = 1", query)
        self.assertIn("`stripe_erpnext_pause_active` = 1", query)
        self.assertIn("`stripe_pending_resume_at`", query)
        self.assertIn("`stripe_resume_at`", query)
        self.assertIn("NOT IN (%s, %s)", query)
        self.assertIn("AS UNSIGNED) <= %s", query)
        self.assertEqual(
            values,
            (
                "Pausing",
                "Cancelling",
                "Resuming",
                "Resuming",
                1784142000,
                "Resuming",
                100,
            ),
        )
        self.assertTrue(self.frappe.db.sql.call_args.kwargs["as_dict"])
        for subscription in subscriptions.values():
            subscription.process.assert_called_once_with(posting_date=date(2026, 7, 15))
        self.assertEqual(
            result,
            [
                {"subscription": "ACC-SUB-0001", "status": "Completed", "processed": True},
                {
                    "subscription": "ACC-SUB-0002",
                    "status": "Failed",
                    "error": "remote pause still active",
                },
                {"subscription": "ACC-SUB-0003", "status": "Completed", "processed": False},
            ],
        )
        self.assertEqual(self.frappe.db.commit.call_count, 3)
        self.frappe.db.rollback.assert_called_once()
        self.assertEqual(
            [call.args[1] for call in self.frappe.db.set_value.call_args_list],
            ["ACC-SUB-0001", "ACC-SUB-0002", "ACC-SUB-0003"],
        )
        self.assertTrue(
            all(
                call.args[2] == "stripe_pause_last_reconciled_at"
                for call in self.frappe.db.set_value.call_args_list
            )
        )

    def test_due_pause_failures_rotate_so_rows_after_the_page_cap_are_reached(self):
        names = [f"ACC-SUB-{index:04d}" for index in range(1, 102)]
        last_reconciled = {name: datetime(2026, 1, 1) for name in names}
        resume_at = {
            name: 1784141000 if name != "ACC-SUB-0101" else 1784141500
            for name in names
        }
        process_order = []

        def select_due_rows(query, values, as_dict=False):
            order_clause = query.split("ORDER BY", 1)[1]
            reconciliation_first = order_clause.index(
                "`stripe_pause_last_reconciled_at` ASC"
            ) < order_clause.index("CAST")
            if reconciliation_first:
                def key(name):
                    return (last_reconciled[name], resume_at[name], name)
            else:
                def key(name):
                    return (resume_at[name], last_reconciled[name], name)
            selected = sorted(names, key=key)[: values[-1]]
            return [{"name": name} for name in selected]

        def rotate_row(doctype, name, fieldname, value, update_modified=False):
            if doctype == "Subscription" and fieldname == "stripe_pause_last_reconciled_at":
                last_reconciled[name] = value

        def get_subscription(doctype, name):
            def process(posting_date=None):
                process_order.append(name)
                raise self.frappe.ValidationError("persistent poison row")

            return types.SimpleNamespace(process=process)

        self.frappe.db.sql.side_effect = select_due_rows
        self.frappe.db.set_value.side_effect = rotate_row
        self.frappe.get_doc.side_effect = get_subscription

        first = self.module.reconcile_due_subscription_pauses(limit=100)
        second = self.module.reconcile_due_subscription_pauses(limit=100)

        self.assertEqual(len(first), 100)
        self.assertEqual(len(second), 100)
        self.assertNotIn("ACC-SUB-0101", process_order[:100])
        self.assertEqual(process_order[100], "ACC-SUB-0101")

    def test_hourly_reconciliation_processes_due_pauses_before_events(self):
        call_order = []
        self.module.reconcile_due_subscription_pauses = Mock(
            side_effect=lambda: call_order.append("pauses") or ["pause-result"]
        )
        self.module.reconcile_nonterminal_events = Mock(
            side_effect=lambda: call_order.append("events") or ["event-result"]
        )
        self.module.reconcile_unposted_fees = Mock(
            side_effect=lambda: call_order.append("fees") or ["fee-result"]
        )

        result = self.module.run_hourly_reconciliation()

        self.assertEqual(call_order, ["pauses", "events", "fees"])
        self.assertEqual(
            result,
            {
                "pauses": ["pause-result"],
                "events": ["event-result"],
                "fees": ["fee-result"],
            },
        )


if __name__ == "__main__":
    unittest.main()
