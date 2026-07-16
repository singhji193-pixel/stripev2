import calendar
import importlib
import sys
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock

from module_isolation import restore_modules


class _BaseSubscription(dict):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.flags = types.SimpleNamespace()

	def __getattr__(self, key):
		try:
			return self[key]
		except KeyError as exc:
			raise AttributeError(key) from exc

	def set(self, key, value):
		self[key] = value

	def process(self, posting_date=None):
		self["base_process_calls"] = self.get("base_process_calls", 0) + 1
		self.setdefault("base_process_posting_dates", []).append(posting_date)
		if self.get("current_invoice_start") and self.get("current_invoice_end"):
			period = (
				str(self.get("current_invoice_start")),
				str(self.get("current_invoice_end")),
			)
			if not self.is_current_invoice_generated(*period) and self.can_generate_new_invoice(
				posting_date
			):
				self.setdefault("generated_periods", []).append(period)
				if self.get("simulate_outstanding_after_invoice"):
					self["base_has_outstanding_invoice"] = True
				self.update_subscription_period(
					date.fromisoformat(period[1]) + timedelta(days=1)
				)
		if self.get("save_during_base_process"):
			self.save()
		if self.get("status_after_base_process"):
			self["status"] = self["status_after_base_process"]
		return "processed"

	def create_invoice(self, *args, **kwargs):
		self["base_create_invoice_calls"] = self.get("base_create_invoice_calls", 0) + 1
		return "invoice"

	def can_generate_new_invoice(self, posting_date=None):
		self["base_can_generate_calls"] = self.get("base_can_generate_calls", 0) + 1
		if not self.get("base_invoice_eligible", True):
			return False
		if self.get("base_has_outstanding_invoice") and not self.get(
			"generate_new_invoices_past_due_date"
		):
			return False
		return True

	def is_current_invoice_generated(self, start_date=None, end_date=None):
		period = (str(start_date), str(end_date))
		return period in self.get("generated_periods", [])

	def validate_end_date(self):
		self["base_validate_end_date_calls"] = self.get("base_validate_end_date_calls", 0) + 1

	def validate(self):
		self["base_validate_calls"] = self.get("base_validate_calls", 0) + 1

	def is_new(self):
		return bool(self.get("__islocal"))

	def get_doc_before_save(self):
		return self.get("doc_before_save")

	def get_billing_cycle_data(self):
		return self.get("billing_cycle_data", {"years": 1, "days": -1})

	def set_subscription_status(self, posting_date=None):
		self["status_posting_date"] = posting_date
		end_date = self.get("end_date")
		if (
			(self.get("status") or "Active") == "Active"
			and end_date
			and posting_date
			and date.fromisoformat(str(posting_date)) > date.fromisoformat(str(end_date))
		):
			self["status"] = "Completed"

	def cancel_subscription_at_period_end(self):
		self["status"] = "Cancelled"
		self["cancelation_date"] = self.get("fake_today", "2026-08-15")

	def update_subscription_period(self, posting_date):
		start = posting_date if isinstance(posting_date, date) else date.fromisoformat(posting_date)
		if start.month == 12:
			next_month = date(start.year + 1, 1, 1)
		else:
			next_month = date(start.year, start.month + 1, 1)
		self["current_invoice_start"] = start
		period_end = next_month - timedelta(days=1)
		if self.get("end_date") and period_end > date.fromisoformat(str(self["end_date"])):
			period_end = date.fromisoformat(str(self["end_date"]))
		self["current_invoice_end"] = period_end

	def save(self, *args, **kwargs):
		return self._save(*args, **kwargs)

	def _save(self, *args, **kwargs):
		if callback := self.get("before_base_save"):
			callback(self)
		if self.get("validate_during_base_save"):
			self.validate()
		if self.get("validate_set_only_end") and self["persisted_end_date"]() != self.get("end_date"):
			raise RuntimeError("CannotChangeConstantError: end_date")
		self["save_calls"] = self.get("save_calls", 0) + 1
		return self

	def reload(self):
		self["reload_calls"] = self.get("reload_calls", 0) + 1
		self.update(self.get("reload_values", {}))
		return self


class _CallbackManager:
	def __init__(self):
		self.callbacks = []

	def add(self, callback):
		self.callbacks.append(callback)

	def run(self):
		callbacks, self.callbacks = self.callbacks, []
		for callback in callbacks:
			callback()

	def reset(self):
		self.callbacks = []


class NonBillingSubscriptionTests(unittest.TestCase):
	def setUp(self):
		self._orig_modules = dict(sys.modules)
		self.today = "2026-08-15"
		self.now_timestamp = int(datetime(2026, 8, 15, 12, tzinfo=timezone.utc).timestamp())
		self.db_updates = []
		self.canonical_values = {}
		self.error_logs = []
		self.user_roles = {"Administrator": ["System Manager"]}

		class FakeValidationError(Exception):
			pass

		self.FakeValidationError = FakeValidationError

		fake_frappe = types.ModuleType("frappe")
		fake_frappe._ = lambda message: message
		fake_frappe.whitelist = lambda *args, **kwargs: lambda fn: fn
		fake_frappe.ValidationError = FakeValidationError
		fake_frappe.PermissionError = PermissionError
		fake_frappe.session = types.SimpleNamespace(user="Administrator")
		fake_frappe.flags = types.SimpleNamespace()
		fake_frappe.get_roles = lambda user=None: self.user_roles.get(
			user or fake_frappe.session.user,
			[],
		)

		def throw(message, exc=None):
			raise (exc or RuntimeError)(message)

		def set_value(doctype, name, values, **kwargs):
			self.db_updates.append((doctype, name, values, kwargs))
			self.canonical_values.setdefault(name, {}).update(values)

		def get_value(doctype, name, fieldnames, as_dict=False, **kwargs):
			values = self.canonical_values.get(name)
			if values is None:
				return None
			if isinstance(fieldnames, (list, tuple)):
				return {fieldname: values.get(fieldname) for fieldname in fieldnames if fieldname in values}
			return values.get(fieldnames)

		fake_frappe.throw = throw
		fake_frappe.log_error = lambda **kwargs: self.error_logs.append(kwargs)
		fake_frappe.db = types.SimpleNamespace(
			set_value=set_value,
			get_value=get_value,
			commit=Mock(),
			rollback=Mock(),
			after_commit=_CallbackManager(),
			after_rollback=_CallbackManager(),
		)

		fake_frappe_utils = types.ModuleType("frappe.utils")
		fake_frappe_utils.getdate = lambda value=None: (
			value if isinstance(value, date) else date.fromisoformat(value)
		)
		fake_frappe_utils.nowdate = lambda: self.today

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
		sys.modules.pop("stripe_integration.stripe_integration.subscription_pause", None)
		sys.modules.pop("stripe_integration.stripe_integration.subscription_override", None)

		self.module = importlib.import_module("stripe_integration.stripe_integration.subscription_override")

		class _NoopLock:
			def __init__(self, name, timeout=30):
				pass

			def __enter__(self):
				return self

			def __exit__(self, exc_type, exc, traceback):
				return False

		self.module.MariaDBNamedLock = _NoopLock
		self.module._utc_now_timestamp = lambda: self.now_timestamp
		self.module._retrieve_remote_pause_state = lambda subscription: {
			"paused": False,
			"remote": {"status": "active"},
		}

	def tearDown(self):
		restore_modules(self._orig_modules)

	def set_clock(self, value: str) -> None:
		current = datetime.fromisoformat(value.replace("Z", "+00:00"))
		self.now_timestamp = int(current.timestamp())
		self.today = str(current.date())

	def test_non_billing_subscription_process_never_runs_invoice_path(self):
		subscription = self.module.NonBillingSubscription(custom_do_not_generate_invoices=1)

		result = subscription.process(posting_date="2026-07-15")

		self.assertFalse(result)
		self.assertNotIn("base_process_calls", subscription)
		self.assertEqual(subscription["status_posting_date"], "2026-07-15")
		self.assertEqual(subscription["save_calls"], 1)

	def test_paused_subscription_process_never_runs_invoice_path(self):
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-DUE",
			custom_do_not_generate_invoices=0,
			stripe_erpnext_pause_active=1,
			stripe_pause_start="2026-08-01",
			stripe_resume_on="2026-09-01",
		)

		result = subscription.process(posting_date="2026-08-15")

		self.assertFalse(result)
		self.assertNotIn("base_process_calls", subscription)

	def test_paused_subscription_is_never_eligible_for_an_invoice(self):
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-LATE",
			custom_do_not_generate_invoices=0,
			stripe_erpnext_pause_active=1,
			stripe_pause_start="2026-08-01",
			stripe_resume_on="2026-09-01",
		)

		self.assertFalse(subscription.can_generate_new_invoice("2026-08-15"))
		self.assertNotIn("base_can_generate_calls", subscription)

	def test_paused_subscription_rejects_manual_invoice_generation(self):
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-PAUSED",
			custom_do_not_generate_invoices=0,
			stripe_erpnext_pause_active=1,
			stripe_pause_start="2026-08-01",
			stripe_resume_on="2026-09-01",
		)

		with self.assertRaisesRegex(RuntimeError, "paused"):
			subscription.create_invoice(posting_date="2026-08-15")

		self.assertNotIn("base_create_invoice_calls", subscription)

	def test_pause_must_be_completed_before_manual_invoice_on_resume_date(self):
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-PAUSED",
			custom_do_not_generate_invoices=0,
			stripe_erpnext_pause_active=1,
			stripe_pause_start="2026-08-01",
			stripe_resume_on="2026-09-01",
		)

		with self.assertRaisesRegex(RuntimeError, "paused"):
			subscription.create_invoice(posting_date="2026-09-01")

		self.assertNotIn("base_create_invoice_calls", subscription)

	def test_trusted_reconciliation_can_post_invoice_wholly_before_pause(self):
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-RECONCILE",
			custom_do_not_generate_invoices=0,
			stripe_erpnext_pause_active=1,
			stripe_pause_start="2026-08-01",
			stripe_resume_on="2026-09-01",
		)
		setattr(subscription.flags, self.module.LOCK_FLAG, True)
		setattr(subscription.flags, self.module.RECONCILIATION_FLAG, True)

		result = subscription.create_invoice(
			from_date="2026-07-01",
			to_date="2026-07-31",
			posting_date="2026-07-01",
		)

		self.assertEqual(result, "invoice")
		self.assertEqual(subscription["base_create_invoice_calls"], 1)

	def test_trusted_reconciliation_cannot_post_invoice_intersecting_pause(self):
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-RECONCILE",
			custom_do_not_generate_invoices=0,
			stripe_erpnext_pause_active=1,
			stripe_pause_start="2026-08-01",
			stripe_resume_on="2026-09-01",
		)
		setattr(subscription.flags, self.module.LOCK_FLAG, True)
		setattr(subscription.flags, self.module.RECONCILIATION_FLAG, True)

		with self.assertRaisesRegex(RuntimeError, "paused"):
			subscription.create_invoice(
				from_date="2026-08-01",
				to_date="2026-08-31",
				posting_date="2026-08-01",
			)

	def test_scheduler_refreshes_only_canonical_state_after_waiting_for_action_lock(self):
		class _NoopLock:
			def __init__(self, name, timeout=30):
				pass

			def __enter__(self):
				return self

			def __exit__(self, exc_type, exc, traceback):
				return False

		self.module.MariaDBNamedLock = _NoopLock
		self.canonical_values["ACC-SUB-STALE"] = {
			"stripe_erpnext_pause_active": 1,
			"stripe_pause_start": "2026-08-01",
			"stripe_resume_on": "2026-09-01",
		}
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-STALE",
			stripe_subscription_id="sub_stale",
			custom_do_not_generate_invoices=0,
			stripe_erpnext_pause_active=0,
		)

		result = subscription.process(posting_date="2026-08-15")

		self.assertFalse(result)
		self.assertNotIn("reload_calls", subscription)
		self.assertNotIn("base_process_calls", subscription)

	def test_manual_invoice_refreshes_canonical_pause_after_waiting_for_action_lock(self):
		self.canonical_values["ACC-SUB-STALE-INVOICE"] = {
			"stripe_erpnext_pause_active": 1,
			"stripe_pause_start": "2026-08-01",
			"stripe_resume_on": "2026-09-01",
		}
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-STALE-INVOICE",
			custom_do_not_generate_invoices=0,
			stripe_erpnext_pause_active=0,
		)

		with self.assertRaisesRegex(RuntimeError, "paused"):
			subscription.create_invoice(posting_date="2026-08-15")

		self.assertNotIn("reload_calls", subscription)
		self.assertNotIn("base_create_invoice_calls", subscription)

	def test_due_pause_resumes_on_boundary_and_extends_fixed_term_once(self):
		self.set_clock("2026-09-01T07:00:00Z")
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-DUE",
			custom_do_not_generate_invoices=0,
			stripe_erpnext_pause_active=1,
			stripe_pause_start="2026-08-01",
			stripe_pause_start_at="1785567600",
			stripe_resume_on="2026-09-01",
			stripe_resume_at=str(self.now_timestamp),
			stripe_pause_cadence_snapshot=(
				'{"billing_cycle_anchor":1785567600,"follow_calendar_months":0,'
				'"interval":"month","interval_count":1,"version":1}'
			),
			current_invoice_start="2026-08-01",
			current_invoice_end="2026-08-31",
			end_date="2027-01-31",
			billing_cycle_data={"months": 1, "days": -1},
		)

		result = subscription.process(posting_date="2026-09-01")

		self.assertEqual(result, "processed")
		self.assertEqual(subscription["base_process_calls"], 1)
		self.assertEqual(subscription["stripe_erpnext_pause_active"], 0)
		self.assertEqual(subscription["stripe_pause_cadence_snapshot"], "")
		self.assertEqual(subscription["current_invoice_start"], date(2026, 10, 1))
		self.assertEqual(subscription["current_invoice_end"], date(2026, 10, 31))
		self.assertEqual(subscription["end_date"], date(2027, 2, 28))

	def test_due_pause_fails_closed_when_live_plan_cadence_changed_in_place(self):
		self.set_clock("2026-09-01T07:00:00Z")
		pause_timestamp = int(datetime(2026, 8, 1, 7, tzinfo=timezone.utc).timestamp())
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-LIVE-CADENCE-CHANGED",
			custom_do_not_generate_invoices=0,
			stripe_erpnext_pause_active=1,
			stripe_pause_state="Paused",
			stripe_pause_start="2026-08-01",
			stripe_pause_start_at=str(pause_timestamp),
			stripe_resume_on="2026-09-01",
			stripe_resume_at=str(self.now_timestamp),
			stripe_pause_cycles=1,
			stripe_pause_cadence_snapshot=(
				'{"billing_cycle_anchor":1785567600,"follow_calendar_months":0,'
				'"interval":"month","interval_count":1,"version":1}'
			),
			current_invoice_start="2026-08-01",
			current_invoice_end="2026-08-31",
			end_date=None,
			billing_cycle_data={"years": 1, "days": -1},
		)

		with self.assertRaisesRegex(RuntimeError, "incompatible"):
			subscription.process(posting_date="2026-09-01")

		self.assertEqual(subscription["stripe_erpnext_pause_active"], 1)
		self.assertNotIn("base_process_calls", subscription)

	def test_resuming_state_uses_pending_boundary_after_remote_success(self):
		self.set_clock("2026-08-01T07:00:00Z")
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-PENDING-RESUME",
			custom_do_not_generate_invoices=0,
			stripe_erpnext_pause_active=1,
			stripe_pause_state="Resuming",
			stripe_pause_start="2026-07-01",
			stripe_resume_on="2026-10-01",
			stripe_resume_at=str(int(datetime(2026, 10, 1, 7, tzinfo=timezone.utc).timestamp())),
			stripe_pending_resume_on="2026-08-01",
			stripe_pending_resume_at=str(self.now_timestamp),
			stripe_pause_cycles=1,
			current_invoice_start="2026-07-01",
			current_invoice_end="2026-07-31",
			end_date="2027-01-31",
			billing_cycle_data={"months": 1, "days": -1},
		)

		result = subscription.process(posting_date="2026-08-01")

		self.assertEqual(result, "processed")
		self.assertEqual(subscription["base_process_posting_dates"], [date(2026, 8, 1)])
		self.assertEqual(subscription["current_invoice_start"], date(2026, 9, 1))
		self.assertEqual(subscription["end_date"], date(2027, 2, 28))
		self.assertEqual(subscription["stripe_erpnext_pause_active"], 0)
		self.assertEqual(subscription["stripe_resume_at"], str(self.now_timestamp))

	def test_fixed_end_is_persisted_before_native_save_validates_set_only_once(self):
		self.set_clock("2026-09-01T07:00:00Z")
		persisted = {"end_date": date(2027, 1, 31)}

		def set_value(doctype, name, values, **kwargs):
			if "end_date" in values:
				persisted["end_date"] = values["end_date"]

		self.module.frappe.db.set_value = set_value
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-SET-ONLY",
			custom_do_not_generate_invoices=0,
			stripe_erpnext_pause_active=1,
			stripe_pause_state="Paused",
			stripe_pause_start="2026-08-01",
			stripe_resume_on="2026-09-01",
			stripe_resume_at=str(self.now_timestamp),
			stripe_pause_cycles=1,
			current_invoice_start="2026-08-01",
			current_invoice_end="2026-08-31",
			end_date="2027-01-31",
			billing_cycle_data={"months": 1, "days": -1},
			save_during_base_process=True,
			validate_set_only_end=True,
			persisted_end_date=lambda: persisted["end_date"],
		)

		result = subscription.process(posting_date="2026-09-01")

		self.assertEqual(result, "processed")
		self.assertEqual(persisted["end_date"], date(2027, 2, 28))
		self.assertEqual(subscription["save_calls"], 1)

		subscription.process(posting_date="2026-09-02")

		self.assertEqual(subscription["end_date"], date(2027, 2, 28))

	def test_late_scheduler_processes_resume_on_the_aligned_boundary(self):
		self.set_clock("2026-09-03T18:00:00Z")
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-LATE",
			custom_do_not_generate_invoices=0,
			stripe_erpnext_pause_active=1,
			stripe_pause_start="2026-08-01",
			stripe_resume_on="2026-09-01",
			stripe_resume_at=str(int(datetime(2026, 9, 1, 7, tzinfo=timezone.utc).timestamp())),
			current_invoice_start="2026-08-01",
			current_invoice_end="2026-08-31",
			end_date="2027-01-31",
			billing_cycle_data={"months": 1, "days": -1},
		)

		subscription.process(posting_date="2026-09-03")

		self.assertEqual(subscription["base_process_posting_dates"], [date(2026, 9, 1)])

	def test_future_posting_date_cannot_force_resume_before_wall_clock_boundary(self):
		remote_check = Mock(return_value={"paused": False, "remote": {"status": "active"}})
		self.module._retrieve_remote_pause_state = remote_check
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-EARLY",
			custom_do_not_generate_invoices=0,
			stripe_erpnext_pause_active=1,
			stripe_pause_state="Paused",
			stripe_pause_start="2026-08-01",
			stripe_resume_on="2026-09-01",
			stripe_resume_at=str(int(datetime(2026, 9, 1, 7, tzinfo=timezone.utc).timestamp())),
		)

		result = subscription.process(posting_date="2026-09-01")

		self.assertFalse(result)
		remote_check.assert_not_called()
		self.assertEqual(subscription["stripe_erpnext_pause_active"], 1)

	def test_due_pause_stays_blocked_until_stripe_is_confirmed_resumed(self):
		self.set_clock("2026-09-01T07:00:00Z")
		self.module._retrieve_remote_pause_state = lambda subscription: {
			"paused": True,
			"remote": {"status": "active"},
		}
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-REMOTE-PAUSED",
			custom_do_not_generate_invoices=0,
			stripe_erpnext_pause_active=1,
			stripe_pause_state="Paused",
			stripe_pause_start="2026-08-01",
			stripe_resume_on="2026-09-01",
			stripe_resume_at=str(self.now_timestamp),
			stripe_pause_cycles=1,
			current_invoice_start="2026-08-01",
			current_invoice_end="2026-08-31",
			end_date="2027-01-31",
			billing_cycle_data={"months": 1, "days": -1},
		)

		result = subscription.process(posting_date="2026-09-01")

		self.assertFalse(result)
		self.assertEqual(subscription["stripe_erpnext_pause_active"], 1)
		self.assertEqual(subscription["end_date"], "2027-01-31")
		self.assertFalse(self.db_updates)

	def test_due_detection_uses_exact_pending_resume_timestamp(self):
		self.set_clock("2026-08-01T06:59:59Z")
		target = int(datetime(2026, 8, 1, 7, tzinfo=timezone.utc).timestamp())
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-EXACT-TIME",
			stripe_erpnext_pause_active=1,
			stripe_pause_state="Resuming",
			stripe_resume_at=str(int(datetime(2026, 10, 1, 7, tzinfo=timezone.utc).timestamp())),
			stripe_pending_resume_at=str(target),
		)

		self.assertFalse(subscription.billing_pause_is_due())
		self.now_timestamp += 1
		self.assertTrue(subscription.billing_pause_is_due())

	def test_remote_failure_is_logged_and_translated_to_validation_error(self):
		self.set_clock("2026-09-01T07:00:00Z")

		def fail_remote_check(subscription):
			raise ConnectionError("Stripe unavailable")

		self.module._retrieve_remote_pause_state = fail_remote_check
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-NETWORK",
			custom_do_not_generate_invoices=0,
			stripe_erpnext_pause_active=1,
			stripe_pause_state="Paused",
			stripe_pause_start="2026-08-01",
			stripe_resume_on="2026-09-01",
			stripe_resume_at=str(self.now_timestamp),
		)

		with self.assertRaisesRegex(self.FakeValidationError, "could not be verified"):
			subscription.process()

		self.assertEqual(len(self.error_logs), 1)
		self.assertNotIn("base_process_calls", subscription)

	def test_terminal_stripe_state_cancels_erp_instead_of_resuming(self):
		self.set_clock("2026-09-01T07:00:00Z")
		for remote_status in ("canceled", "incomplete_expired"):
			with self.subTest(remote_status=remote_status):
				self.module._retrieve_remote_pause_state = lambda subscription, status=remote_status: {
					"paused": False,
					"remote": {"status": status},
				}
				subscription = self.module.NonBillingSubscription(
					name=f"ACC-SUB-{remote_status}",
					custom_do_not_generate_invoices=0,
					stripe_erpnext_pause_active=1,
					stripe_pause_state="Paused",
					stripe_pause_start="2026-08-01",
					stripe_resume_on="2026-09-01",
					stripe_resume_at=str(self.now_timestamp),
					fake_today=self.today,
				)

				self.assertFalse(subscription.process())
				self.assertEqual(subscription["status"], "Cancelled")
				self.assertEqual(subscription["cancelation_date"], self.today)
				self.assertEqual(subscription["stripe_erpnext_pause_active"], 0)
				self.assertNotIn("base_process_calls", subscription)

	def test_native_canceled_subscription_clears_pause_without_restarting(self):
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-ALREADY-CANCELLED",
			custom_do_not_generate_invoices=0,
			status="Cancelled",
			cancelation_date="2026-08-15",
			stripe_erpnext_pause_active=1,
			stripe_pause_state="Paused",
		)

		self.assertFalse(subscription.process())

		self.assertEqual(subscription["status"], "Cancelled")
		self.assertEqual(subscription["stripe_erpnext_pause_active"], 0)
		self.assertNotIn("base_process_calls", subscription)

	def test_native_cancellation_preserves_pending_stripe_cancel_operation_for_retry(self):
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-CANCEL-RETRY",
			custom_do_not_generate_invoices=0,
			status="Cancelled",
			cancelation_date="2026-08-15",
			stripe_erpnext_pause_active=1,
			stripe_pause_state="Cancelling",
			stripe_pause_operation_id="cancel_persisted",
		)

		self.assertFalse(subscription.process())

		self.assertEqual(subscription["stripe_erpnext_pause_active"], 1)
		self.assertEqual(subscription["stripe_pause_state"], "Cancelling")
		self.assertEqual(subscription["stripe_pause_operation_id"], "cancel_persisted")
		self.assertFalse(self.db_updates)

	def test_late_resume_fails_closed_when_native_process_cannot_advance(self):
		self.set_clock("2026-09-01T07:00:00Z")
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-CATCH-UP-BLOCKED",
			custom_do_not_generate_invoices=0,
			stripe_erpnext_pause_active=1,
			stripe_pause_state="Paused",
			stripe_pause_start="2026-08-01",
			stripe_resume_on="2026-09-01",
			stripe_resume_at=str(self.now_timestamp),
			stripe_pause_cycles=1,
			current_invoice_start="2026-08-01",
			current_invoice_end="2026-08-31",
			end_date=None,
			billing_cycle_data={"months": 1, "days": -1},
			base_invoice_eligible=False,
		)

		with self.assertRaisesRegex(self.FakeValidationError, "could not be invoiced or advanced"):
			subscription.process()

		self.assertEqual(subscription["base_process_posting_dates"], [date(2026, 9, 1)])
		self.assertNotIn("generated_periods", subscription)

	def test_late_resume_honors_cancel_at_end_of_first_due_period(self):
		self.set_clock("2026-11-15T12:00:00Z")
		resume_at = int(datetime(2026, 9, 1, 7, tzinfo=timezone.utc).timestamp())
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-CATCH-UP-CANCEL",
			custom_do_not_generate_invoices=0,
			stripe_erpnext_pause_active=1,
			stripe_pause_state="Paused",
			stripe_pause_start="2026-08-01",
			stripe_resume_on="2026-09-01",
			stripe_resume_at=str(resume_at),
			stripe_pause_cycles=1,
			current_invoice_start="2026-08-01",
			current_invoice_end="2026-08-31",
			end_date=None,
			billing_cycle_data={"months": 1, "days": -1},
			cancel_at_period_end=1,
			fake_today="2026-11-15",
		)

		subscription.process()

		self.assertEqual(subscription["status"], "Cancelled")
		self.assertEqual(subscription["cancelation_date"], "2026-11-15")
		self.assertEqual(subscription["base_process_posting_dates"], [date(2026, 9, 1)])
		self.assertEqual(subscription["generated_periods"], [("2026-09-01", "2026-09-30")])

	def test_late_resume_stops_when_native_process_completes_subscription(self):
		self.set_clock("2026-11-15T12:00:00Z")
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-CATCH-UP-COMPLETED",
			custom_do_not_generate_invoices=0,
			stripe_erpnext_pause_active=1,
			stripe_pause_state="Paused",
			stripe_pause_start="2026-08-01",
			stripe_resume_on="2026-09-01",
			stripe_resume_at=str(int(datetime(2026, 9, 1, 7, tzinfo=timezone.utc).timestamp())),
			stripe_pause_cycles=1,
			current_invoice_start="2026-08-01",
			current_invoice_end="2026-08-31",
			end_date=None,
			billing_cycle_data={"months": 1, "days": -1},
			status_after_base_process="Completed",
		)

		subscription.process()

		self.assertEqual(subscription["status"], "Completed")
		self.assertEqual(subscription["base_process_posting_dates"], [date(2026, 9, 1)])
		self.assertEqual(subscription["generated_periods"], [("2026-09-01", "2026-09-30")])

	def test_late_resume_never_invoices_beyond_extended_fixed_end(self):
		self.set_clock("2026-12-15T12:00:00Z")
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-CATCH-UP-FIXED-END",
			custom_do_not_generate_invoices=0,
			stripe_erpnext_pause_active=1,
			stripe_pause_state="Paused",
			stripe_pause_start="2026-08-01",
			stripe_resume_on="2026-09-01",
			stripe_resume_at=str(int(datetime(2026, 9, 1, 7, tzinfo=timezone.utc).timestamp())),
			stripe_pause_cycles=1,
			current_invoice_start="2026-08-01",
			current_invoice_end="2026-08-31",
			end_date="2026-09-30",
			billing_cycle_data={"months": 1, "days": -1},
		)

		subscription.process()

		self.assertEqual(subscription["end_date"], date(2026, 10, 30))
		self.assertEqual(subscription["status"], "Completed")
		self.assertEqual(
			subscription["generated_periods"],
			[("2026-09-01", "2026-09-30"), ("2026-10-01", "2026-10-30")],
		)
		self.assertTrue(
			all(date.fromisoformat(period_end) <= subscription["end_date"] for _, period_end in subscription["generated_periods"])
		)

		subscription.process(posting_date="2026-12-16")

		self.assertEqual(
			subscription["generated_periods"],
			[("2026-09-01", "2026-09-30"), ("2026-10-01", "2026-10-30")],
		)

	def test_fixed_term_stop_preserves_native_past_due_status(self):
		self.set_clock("2026-12-15T12:00:00Z")
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-CATCH-UP-PAST-DUE",
			custom_do_not_generate_invoices=0,
			stripe_erpnext_pause_active=1,
			stripe_pause_state="Paused",
			stripe_pause_start="2026-08-01",
			stripe_resume_on="2026-09-01",
			stripe_resume_at=str(int(datetime(2026, 9, 1, 7, tzinfo=timezone.utc).timestamp())),
			stripe_pause_cycles=1,
			current_invoice_start="2026-08-01",
			current_invoice_end="2026-08-31",
			end_date="2026-09-30",
			billing_cycle_data={"months": 1, "days": -1},
			status="Past Due Date",
		)

		subscription.process()

		self.assertEqual(subscription["status"], "Past Due Date")
		self.assertEqual(subscription["status_posting_date"], date(2026, 12, 15))
		self.assertTrue(
			all(date.fromisoformat(period_end) <= subscription["end_date"] for _, period_end in subscription["generated_periods"])
		)

	def test_late_resume_catches_up_each_aligned_period_without_skipping(self):
		self.set_clock("2026-11-15T12:00:00Z")
		resume_at = int(datetime(2026, 9, 1, 7, tzinfo=timezone.utc).timestamp())
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-CATCH-UP",
			custom_do_not_generate_invoices=0,
			stripe_erpnext_pause_active=1,
			stripe_pause_state="Paused",
			stripe_pause_start="2026-08-01",
			stripe_resume_on="2026-09-01",
			stripe_resume_at=str(resume_at),
			stripe_pause_cycles=1,
			current_invoice_start="2026-08-01",
			current_invoice_end="2026-08-31",
			end_date=None,
			billing_cycle_data={"months": 1, "days": -1},
		)

		subscription.process()

		self.assertEqual(
			subscription["base_process_posting_dates"],
			[date(2026, 9, 1), date(2026, 10, 1), date(2026, 11, 1)],
		)
		self.assertEqual(
			subscription["generated_periods"],
			[
				("2026-09-01", "2026-09-30"),
				("2026-10-01", "2026-10-31"),
				("2026-11-01", "2026-11-30"),
			],
		)
		self.assertEqual(subscription["current_invoice_start"], date(2026, 12, 1))

	def test_late_resume_catch_up_bypasses_outstanding_guard_without_changing_setting(self):
		self.set_clock("2026-11-15T12:00:00Z")
		resume_at = int(datetime(2026, 9, 1, 7, tzinfo=timezone.utc).timestamp())
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-CATCH-UP-OUTSTANDING",
			custom_do_not_generate_invoices=0,
			stripe_erpnext_pause_active=1,
			stripe_pause_state="Paused",
			stripe_pause_start="2026-08-01",
			stripe_resume_on="2026-09-01",
			stripe_resume_at=str(resume_at),
			stripe_pause_cycles=1,
			current_invoice_start="2026-08-01",
			current_invoice_end="2026-08-31",
			end_date=None,
			billing_cycle_data={"months": 1, "days": -1},
			generate_new_invoices_past_due_date=0,
			simulate_outstanding_after_invoice=True,
		)

		subscription.process()

		self.assertEqual(
			subscription["generated_periods"],
			[
				("2026-09-01", "2026-09-30"),
				("2026-10-01", "2026-10-31"),
				("2026-11-01", "2026-11-30"),
			],
		)
		self.assertEqual(subscription["generate_new_invoices_past_due_date"], 0)

	def test_late_resume_fails_closed_when_catch_up_bound_is_exceeded(self):
		self.set_clock("2026-11-15T12:00:00Z")
		self.module.MAX_CATCH_UP_PERIODS = 2
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-CATCH-UP-BOUND",
			custom_do_not_generate_invoices=0,
			stripe_erpnext_pause_active=1,
			stripe_pause_state="Paused",
			stripe_pause_start="2026-08-01",
			stripe_resume_on="2026-09-01",
			stripe_resume_at=str(int(datetime(2026, 9, 1, 7, tzinfo=timezone.utc).timestamp())),
			stripe_pause_cycles=1,
			current_invoice_start="2026-08-01",
			current_invoice_end="2026-08-31",
			end_date=None,
			billing_cycle_data={"months": 1, "days": -1},
		)

		with self.assertRaisesRegex(self.FakeValidationError, "too many overdue"):
			subscription.process()

		self.assertEqual(
			subscription["base_process_posting_dates"],
			[date(2026, 9, 1), date(2026, 10, 1)],
		)

	def test_canonical_refresh_preserves_unsaved_native_cancel_fields(self):
		before = self.module.NonBillingSubscription(
			status="Active",
			cancelation_date=None,
			cancel_at_period_end=0,
		)
		self.canonical_values["ACC-SUB-NATIVE-CANCEL"] = {
			"status": "Active",
			"cancelation_date": None,
			"cancel_at_period_end": 0,
			"stripe_erpnext_pause_active": 0,
		}
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-NATIVE-CANCEL",
			custom_do_not_generate_invoices=0,
			status="Cancelled",
			cancelation_date="2026-08-15",
			cancel_at_period_end=1,
			doc_before_save=before,
		)

		subscription.process(posting_date="2026-08-15")

		self.assertEqual(subscription["status"], "Cancelled")
		self.assertEqual(subscription["cancelation_date"], "2026-08-15")
		self.assertEqual(subscription["cancel_at_period_end"], 1)

	def test_every_user_owned_coordinated_pause_field_rejects_direct_mutation(self):
		fields = tuple(
			fieldname
			for fieldname in self.module._coordinated_pause_fields()
			if fieldname != self.module.PAUSE_LAST_RECONCILED_AT_FIELD
		)
		for fieldname in fields:
			with self.subTest(fieldname=fieldname):
				before_values = {field: "" for field in fields}
				before_values["stripe_erpnext_pause_active"] = 0
				before = self.module.NonBillingSubscription(**before_values)
				current_values = dict(before_values)
				current_values[fieldname] = "changed"
				subscription = self.module.NonBillingSubscription(
					name="ACC-SUB-PROTECTED",
					doc_before_save=before,
					**current_values,
				)

				with self.assertRaisesRegex(self.FakeValidationError, "cannot be changed directly"):
					subscription.validate()

	def test_new_subscription_rejects_forged_coordinated_pause_values(self):
		for fieldname in self.module._coordinated_pause_fields():
			if fieldname == self.module.PAUSE_LAST_RECONCILED_AT_FIELD:
				continue
			with self.subTest(fieldname=fieldname):
				value = 1 if fieldname in self.module.ZERO_DEFAULT_COORDINATED_FIELDS else "forged"
				subscription = self.module.NonBillingSubscription(
					name="new-subscription-forged",
					__islocal=1,
					**{fieldname: value},
				)

				with self.assertRaisesRegex(self.FakeValidationError, "cannot be changed directly"):
					subscription.validate()

	def test_new_subscription_allows_empty_pause_defaults(self):
		values = {fieldname: "" for fieldname in self.module._coordinated_pause_fields()}
		for fieldname in self.module.ZERO_DEFAULT_COORDINATED_FIELDS:
			values[fieldname] = 0
		subscription = self.module.NonBillingSubscription(
			name="new-subscription-defaults",
			__islocal=1,
			**values,
		)

		subscription.validate()

		self.assertEqual(subscription["base_validate_calls"], 1)

	def test_internal_flag_allows_pause_values_on_new_subscription(self):
		subscription = self.module.NonBillingSubscription(
			name="new-subscription-internal",
			__islocal=1,
			stripe_erpnext_pause_active=1,
			stripe_pause_state="Pausing",
		)
		setattr(subscription.flags, self.module.INTERNAL_PAUSE_MUTATION_FLAG, True)

		subscription.validate()

		self.assertEqual(subscription["base_validate_calls"], 1)

	def test_internal_flag_allows_coordinated_pause_mutation(self):
		before = self.module.NonBillingSubscription(stripe_pause_state="Paused")
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-INTERNAL",
			stripe_pause_state="Resuming",
			doc_before_save=before,
		)
		setattr(subscription.flags, self.module.INTERNAL_PAUSE_MUTATION_FLAG, True)

		subscription.validate()

		self.assertEqual(subscription["base_validate_calls"], 1)

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

	def test_named_lock_is_held_until_caller_transaction_finishes_and_acquired_once(self):
		events = []

		class _RecordingLock:
			def __init__(self, name, timeout=30):
				self.name = name

			def __enter__(self):
				events.append(("enter", self.name))
				return self

			def __exit__(self, exc_type, exc, traceback):
				events.append(("exit", self.name))
				return False

		self.module.MariaDBNamedLock = _RecordingLock
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-TRANSACTION",
			custom_do_not_generate_invoices=0,
		)

		subscription.process(posting_date="2026-07-15")
		subscription.create_invoice(posting_date="2026-07-15")

		self.assertEqual(
			events,
			[("enter", "stripe-subscription-action-ACC-SUB-TRANSACTION")],
		)
		self.module.frappe.db.commit.assert_not_called()
		self.module.frappe.db.rollback.assert_not_called()

		self.module.frappe.db.after_commit.run()

		self.assertEqual(
			events,
			[
				("enter", "stripe-subscription-action-ACC-SUB-TRANSACTION"),
				("exit", "stripe-subscription-action-ACC-SUB-TRANSACTION"),
			],
		)

	def test_named_lock_is_released_after_caller_rolls_back(self):
		events = []

		class _RecordingLock:
			def __init__(self, name, timeout=30):
				self.name = name

			def __enter__(self):
				events.append("enter")
				return self

			def __exit__(self, exc_type, exc, traceback):
				events.append("exit")
				return False

		self.module.MariaDBNamedLock = _RecordingLock
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-ROLLBACK",
			custom_do_not_generate_invoices=0,
		)

		subscription.process()
		self.assertEqual(events, ["enter"])

		self.module.frappe.db.after_rollback.run()

		self.assertEqual(events, ["enter", "exit"])

	def test_ordinary_save_acquires_named_lock_before_native_save_starts(self):
		events = []

		class _RecordingLock:
			def __init__(self, name, timeout=30):
				self.name = name

			def __enter__(self):
				events.append(("named-lock", self.name))
				return self

			def __exit__(self, exc_type, exc, traceback):
				return False

		self.module.MariaDBNamedLock = _RecordingLock
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-SAVE-LOCK-ORDER",
			before_base_save=lambda _doc: events.append(("native-save", None)),
		)

		subscription.save()

		self.assertEqual(
			events,
			[
				("named-lock", "stripe-subscription-action-ACC-SUB-SAVE-LOCK-ORDER"),
				("native-save", None),
			],
		)

	def test_ordinary_save_preserves_a_user_end_date_edit(self):
		self.canonical_values["ACC-SUB-SAVE-END-DATE"] = {
			"end_date": "2027-01-31",
		}
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-SAVE-END-DATE",
			end_date="2027-02-28",
		)

		subscription.save()

		self.assertEqual(subscription["end_date"], "2027-02-28")

	def test_ordinary_save_rejects_direct_pause_mutation_before_canonical_refresh(self):
		self.canonical_values["ACC-SUB-SAVE-PROTECTED"] = {
			"stripe_erpnext_pause_active": 1,
			"stripe_pause_state": "Paused",
		}
		canonical = self.module.NonBillingSubscription(
			stripe_erpnext_pause_active=1,
			stripe_pause_state="Paused",
		)
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-SAVE-PROTECTED",
			stripe_erpnext_pause_active=0,
			stripe_pause_state="",
			doc_before_save=canonical,
			validate_during_base_save=True,
		)

		with self.assertRaisesRegex(self.FakeValidationError, "cannot be changed directly"):
			subscription.save()

	def test_unchanged_plan_rows_do_not_block_unrelated_paused_save(self):
		before = self.module.NonBillingSubscription(
			stripe_erpnext_pause_active=1,
			plans=[{"plan": "PLAN-149", "qty": 1}],
			follow_calendar_months=0,
		)
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-VALIDATE",
			stripe_erpnext_pause_active=1,
			plans=[{"plan": "PLAN-149", "qty": 1}],
			follow_calendar_months=0,
			doc_before_save=before,
		)

		subscription.validate()

		self.assertEqual(subscription["base_validate_calls"], 1)

		subscription["plans"] = [{"plan": "PLAN-149", "qty": 2}]
		with self.assertRaisesRegex(RuntimeError, "billing cycle cannot change"):
			subscription.validate()

	def test_existing_save_rechecks_pause_under_action_lock_without_losing_user_fields(self):
		events = []

		class _PauseWinsLock:
			def __init__(inner_self, name, timeout=30):
				inner_self.name = name

			def __enter__(inner_self):
				events.append(("enter", inner_self.name))
				self.canonical_values["ACC-SUB-PLAN-RACE"] = {
					"stripe_erpnext_pause_active": 1,
					"stripe_pause_state": "Paused",
					"end_date": "2027-01-31",
				}
				return inner_self

			def __exit__(inner_self, exc_type, exc, traceback):
				events.append(("exit", inner_self.name))
				return False

		self.module.MariaDBNamedLock = _PauseWinsLock
		before = self.module.NonBillingSubscription(
			stripe_erpnext_pause_active=0,
			stripe_pause_state="",
			plans=[{"plan": "PLAN-149", "qty": 1}],
			follow_calendar_months=0,
			end_date="2026-12-31",
		)
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-PLAN-RACE",
			stripe_erpnext_pause_active=0,
			stripe_pause_state="",
			plans=[{"plan": "PLAN-149", "qty": 2}],
			follow_calendar_months=0,
			end_date="2027-02-28",
			doc_before_save=before,
		)

		with self.assertRaisesRegex(RuntimeError, "billing cycle cannot change"):
			subscription.validate()

		self.assertEqual(
			events,
			[("enter", "stripe-subscription-action-ACC-SUB-PLAN-RACE")],
		)
		self.assertEqual(subscription["stripe_erpnext_pause_active"], 1)
		self.assertEqual(subscription["stripe_pause_state"], "Paused")
		self.assertEqual(subscription["end_date"], "2027-02-28")
		self.module.frappe.db.after_rollback.run()
		self.assertEqual(events[-1], ("exit", "stripe-subscription-action-ACC-SUB-PLAN-RACE"))

	def test_existing_save_refreshes_stale_scheduler_cursor_without_rejecting_user_edit(self):
		self.canonical_values["ACC-SUB-STALE-SCHEDULER-CURSOR"] = {
			"stripe_erpnext_pause_active": 0,
			"stripe_pause_last_reconciled_at": "2026-08-15 12:10:00",
		}
		before = self.module.NonBillingSubscription(
			stripe_erpnext_pause_active=0,
			stripe_pause_last_reconciled_at="2026-08-15 12:10:00",
			description="Before",
		)
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-STALE-SCHEDULER-CURSOR",
			stripe_erpnext_pause_active=0,
			stripe_pause_last_reconciled_at="2026-08-15 12:00:00",
			description="User edit",
			doc_before_save=before,
		)

		subscription.validate()

		self.assertEqual(subscription["base_validate_calls"], 1)
		self.assertEqual(subscription["stripe_pause_last_reconciled_at"], "2026-08-15 12:10:00")
		self.assertEqual(subscription["description"], "User edit")

	def test_accounts_user_cannot_schedule_native_subscription_cancellation(self):
		self.module.frappe.session.user = "accounts-user@example.com"
		self.user_roles["accounts-user@example.com"] = ["Accounts User"]
		before = self.module.NonBillingSubscription(
			status="Active",
			cancelation_date=None,
			cancel_at_period_end=0,
		)
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-UNAUTHORIZED-CANCEL",
			status="Active",
			cancelation_date=None,
			cancel_at_period_end=1,
			doc_before_save=before,
		)

		with self.assertRaisesRegex(PermissionError, "System Manager or Accounts Manager"):
			subscription.validate()

	def test_accounts_user_cannot_become_natively_cancelled(self):
		self.module.frappe.session.user = "accounts-user@example.com"
		self.user_roles["accounts-user@example.com"] = ["Accounts User"]
		before = self.module.NonBillingSubscription(
			status="Active",
			cancelation_date=None,
			cancel_at_period_end=0,
		)
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-UNAUTHORIZED-NATIVE-CANCEL",
			status="Cancelled",
			cancelation_date="2026-08-15",
			cancel_at_period_end=0,
			doc_before_save=before,
		)

		with self.assertRaisesRegex(PermissionError, "System Manager or Accounts Manager"):
			subscription.validate()

	def test_accounts_manager_can_change_native_subscription_cancellation(self):
		self.module.frappe.session.user = "accounts-manager@example.com"
		self.user_roles["accounts-manager@example.com"] = ["Accounts Manager"]
		before = self.module.NonBillingSubscription(
			status="Active",
			cancelation_date=None,
			cancel_at_period_end=0,
		)
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-MANAGER-CANCEL",
			status="Active",
			cancelation_date=None,
			cancel_at_period_end=1,
			doc_before_save=before,
		)

		subscription.validate()

		self.assertEqual(subscription["base_validate_calls"], 1)

	def test_manager_cancellation_intent_survives_canonical_refresh_during_save(self):
		self.module.frappe.session.user = "accounts-manager@example.com"
		self.user_roles["accounts-manager@example.com"] = ["Accounts Manager"]
		self.canonical_values["ACC-SUB-MANAGER-SAVE"] = {
			"status": "Active",
			"cancelation_date": None,
			"cancel_at_period_end": 0,
			"stripe_erpnext_pause_active": 0,
		}
		before = self.module.NonBillingSubscription(
			status="Active",
			cancelation_date=None,
			cancel_at_period_end=0,
			stripe_erpnext_pause_active=0,
		)
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-MANAGER-SAVE",
			status="Cancelled",
			cancelation_date="2026-08-15",
			cancel_at_period_end=1,
			stripe_erpnext_pause_active=0,
			doc_before_save=before,
			validate_during_base_save=True,
		)

		subscription.save()

		self.assertEqual(subscription["status"], "Cancelled")
		self.assertEqual(subscription["cancelation_date"], "2026-08-15")
		self.assertEqual(subscription["cancel_at_period_end"], 1)
		self.assertEqual(subscription["base_validate_calls"], 1)

	def test_manager_can_restart_an_unlinked_native_cancellation(self):
		self.module.frappe.session.user = "accounts-manager@example.com"
		self.user_roles["accounts-manager@example.com"] = ["Accounts Manager"]
		self.canonical_values["ACC-SUB-STALE-MANAGER"] = {
			"status": "Cancelled",
			"cancelation_date": "2026-08-20",
			"cancel_at_period_end": 1,
			"current_invoice_start": "2026-07-01",
			"current_invoice_end": "2026-07-31",
			"stripe_erpnext_pause_active": 0,
		}
		canonical_snapshot = self.module.NonBillingSubscription(
			status="Cancelled",
			cancelation_date="2026-08-20",
			cancel_at_period_end=1,
			stripe_erpnext_pause_active=0,
		)
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-STALE-MANAGER",
			status="Active",
			cancelation_date=None,
			cancel_at_period_end=0,
			current_invoice_start=date(2026, 8, 20),
			current_invoice_end=date(2026, 8, 31),
			stripe_erpnext_pause_active=0,
			doc_before_save=canonical_snapshot,
			validate_during_base_save=True,
		)

		subscription.save()

		self.assertEqual(subscription["status"], "Active")
		self.assertIsNone(subscription["cancelation_date"])
		self.assertEqual(subscription["cancel_at_period_end"], 0)
		self.assertEqual(subscription["current_invoice_start"], date(2026, 8, 20))
		self.assertEqual(subscription["current_invoice_end"], date(2026, 8, 31))
		self.assertEqual(subscription["base_validate_calls"], 1)

	def test_terminal_stripe_linked_subscription_cannot_use_native_restart(self):
		self.module.frappe.session.user = "accounts-manager@example.com"
		self.user_roles["accounts-manager@example.com"] = ["Accounts Manager"]
		self.canonical_values["ACC-SUB-LINKED-RESTART"] = {
			"status": "Cancelled",
			"cancelation_date": "2026-08-20",
			"cancel_at_period_end": 0,
			"current_invoice_start": "2026-07-01",
			"current_invoice_end": "2026-07-31",
			"stripe_erpnext_pause_active": 0,
		}
		before = self.module.NonBillingSubscription(
			status="Cancelled",
			cancelation_date="2026-08-20",
			cancel_at_period_end=0,
			stripe_erpnext_pause_active=0,
		)
		subscription = self.module.NonBillingSubscription(
			name="ACC-SUB-LINKED-RESTART",
			stripe_subscription_id="sub_terminal",
			status="Active",
			cancelation_date=None,
			cancel_at_period_end=0,
			current_invoice_start=date(2026, 8, 20),
			current_invoice_end=date(2026, 8, 31),
			stripe_erpnext_pause_active=0,
			doc_before_save=before,
			validate_during_base_save=True,
		)

		with self.assertRaisesRegex(self.FakeValidationError, "cannot be restarted"):
			subscription.save()

	def test_administrator_internal_and_scheduler_native_cancellation_remain_allowed(self):
		before = self.module.NonBillingSubscription(
			status="Active",
			cancelation_date=None,
			cancel_at_period_end=0,
		)
		administrator = self.module.NonBillingSubscription(
			name="ACC-SUB-ADMINISTRATOR-CANCEL",
			status="Cancelled",
			cancelation_date="2026-08-15",
			cancel_at_period_end=0,
			doc_before_save=before,
		)
		administrator.validate()

		self.module.frappe.session.user = "accounts-user@example.com"
		self.user_roles["accounts-user@example.com"] = ["Accounts User"]

		internal = self.module.NonBillingSubscription(
			name="ACC-SUB-INTERNAL-CANCEL",
			status="Cancelled",
			cancelation_date="2026-08-15",
			cancel_at_period_end=0,
			doc_before_save=before,
		)
		setattr(internal.flags, self.module.INTERNAL_PAUSE_MUTATION_FLAG, True)
		internal.validate()

		self.module.frappe.flags.in_scheduler = True
		scheduled = self.module.NonBillingSubscription(
			name="ACC-SUB-SCHEDULER-CANCEL",
			status="Cancelled",
			cancelation_date="2026-08-15",
			cancel_at_period_end=0,
			doc_before_save=before,
		)
		scheduled.validate()

		self.assertEqual(administrator["base_validate_calls"], 1)
		self.assertEqual(internal["base_validate_calls"], 1)
		self.assertEqual(scheduled["base_validate_calls"], 1)

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
