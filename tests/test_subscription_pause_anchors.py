import calendar
import importlib
import sys
import types
import unittest
from datetime import date, datetime, timedelta, timezone

from module_isolation import restore_modules


class _Subscription(dict):
	name = "ACC-SUB-ANCHOR"

	def __getattr__(self, key):
		try:
			return self[key]
		except KeyError as exc:
			raise AttributeError(key) from exc


class SubscriptionPauseAnchorTests(unittest.TestCase):
	def setUp(self):
		self._orig_modules = dict(sys.modules)

		fake_frappe = types.ModuleType("frappe")
		fake_frappe._ = lambda message: message
		fake_frappe.throw = lambda message, exc=None: (_ for _ in ()).throw(RuntimeError(message))

		fake_utils = types.ModuleType("frappe.utils")
		fake_utils.getdate = lambda value=None: (
			value if isinstance(value, date) else date.fromisoformat(value)
		)
		fake_utils.nowdate = lambda: "2027-01-01"

		def add_months(value, months):
			current = fake_utils.getdate(value)
			month_index = current.year * 12 + current.month - 1 + int(months)
			target_year, target_month_index = divmod(month_index, 12)
			target_month = target_month_index + 1
			return current.replace(
				year=target_year,
				month=target_month,
				day=min(current.day, calendar.monthrange(target_year, target_month)[1]),
			)

		fake_utils.add_months = add_months
		fake_utils.add_days = lambda value, days: fake_utils.getdate(value) + timedelta(days=days)
		fake_utils.add_to_date = lambda value, **delta: (
			add_months(value, delta.get("months", 0) + 12 * delta.get("years", 0))
			+ timedelta(weeks=delta.get("weeks", 0), days=delta.get("days", 0))
		)
		fake_utils.get_last_day = lambda value: date(
			fake_utils.getdate(value).year,
			fake_utils.getdate(value).month,
			calendar.monthrange(
				fake_utils.getdate(value).year,
				fake_utils.getdate(value).month,
			)[1],
		)

		sys.modules["frappe"] = fake_frappe
		sys.modules["frappe.utils"] = fake_utils
		sys.modules.pop("stripe_integration.stripe_integration.subscription_pause", None)
		self.module = importlib.import_module("stripe_integration.stripe_integration.subscription_pause")

	def tearDown(self):
		restore_modules(self._orig_modules)

	def subscription(self, **values):
		defaults = {
			"generate_invoice_at": "Beginning of the current subscription period",
			"follow_calendar_months": 0,
			"current_invoice_start": "2027-01-31",
			"current_invoice_end": "2027-02-27",
			"is_current_invoice_generated": lambda start, end: False,
			"get_billing_cycle_data": lambda: {"months": 1, "days": -1},
		}
		defaults.update(values)
		return _Subscription(defaults)

	def test_original_monthly_anchor_preserves_month_end_boundaries(self):
		anchor = 1801382400  # 2027-01-31T08:00:00Z
		snapshot = self.module.build_cadence_snapshot(
			self.subscription(),
			billing_cycle_anchor=anchor,
			interval="month",
			interval_count=1,
		)

		self.assertEqual(self.module.cadence_boundary_timestamp(snapshot, 1), 1803801600)
		self.assertEqual(self.module.cadence_boundary_timestamp(snapshot, 2), 1806480000)
		self.assertEqual(
			self.module.advance_billing_timestamp(
				self.subscription(),
				anchor,
				2,
				cadence_snapshot=snapshot,
			),
			1806480000,
		)

	def test_pause_window_returns_anchor_derived_dates_and_exact_utc_timestamps(self):
		anchor = int(datetime(2027, 1, 28, 8, tzinfo=timezone.utc).timestamp())
		subscription = self.subscription(
			current_invoice_start="2027-01-28",
			current_invoice_end="2027-02-27",
		)
		snapshot = self.module.build_cadence_snapshot(
			subscription,
			billing_cycle_anchor=anchor,
			interval="month",
			interval_count=1,
			pause_start_at=anchor,
		)

		window = self.module.build_pause_window(
			subscription,
			billing_cycles=2,
			cadence_snapshot=snapshot,
			pause_start_at=anchor,
		)

		self.assertEqual(
			window,
			{
				"billing_cycles": 2,
				"pause_start": "2027-01-28",
				"resume_on": "2027-03-28",
				"pause_start_at": anchor,
				"resume_at": int(datetime(2027, 3, 28, 8, tzinfo=timezone.utc).timestamp()),
				"cadence_snapshot": self.module.serialize_cadence_snapshot(snapshot),
			},
		)

	def test_monthly_month_end_pause_is_rejected_before_later_boundaries_drift(self):
		anchor = 1801382400  # 2027-01-31T08:00:00Z

		with self.assertRaisesRegex(RuntimeError, "cannot stay aligned"):
			self.module.build_cadence_snapshot(
				self.subscription(),
				billing_cycle_anchor=anchor,
				interval="month",
				interval_count=1,
				pause_start_at=anchor,
			)

	def test_semiannual_month_end_pause_is_allowed_when_every_boundary_supports_anchor(self):
		anchor = 1801382400  # 2027-01-31T08:00:00Z
		subscription = self.subscription(
			current_invoice_end="2027-07-30",
			get_billing_cycle_data=lambda: {"months": 6, "days": -1},
		)

		snapshot = self.module.build_cadence_snapshot(
			subscription,
			billing_cycle_anchor=anchor,
			interval="month",
			interval_count=6,
			pause_start_at=anchor,
		)

		self.assertEqual(snapshot["interval_count"], 6)

	def test_coincident_first_boundary_does_not_hide_incompatible_cadence(self):
		anchor = int(datetime(2027, 4, 1, 8, tzinfo=timezone.utc).timestamp())
		subscription = self.subscription(
			current_invoice_start="2027-04-01",
			current_invoice_end="2027-04-30",
		)

		with self.assertRaisesRegex(RuntimeError, "incompatible"):
			self.module.build_cadence_snapshot(
				subscription,
				billing_cycle_anchor=anchor,
				interval="day",
				interval_count=30,
				pause_start_at=anchor,
			)

	def test_equivalent_interval_units_share_one_normalized_cadence(self):
		annual_anchor = int(datetime(2027, 8, 1, 7, tzinfo=timezone.utc).timestamp())
		annual = self.subscription(
			current_invoice_start="2027-08-01",
			current_invoice_end="2028-07-31",
			get_billing_cycle_data=lambda: {"years": 1, "days": -1},
		)
		weekly_anchor = int(datetime(2027, 8, 1, 7, tzinfo=timezone.utc).timestamp())
		weekly = self.subscription(
			current_invoice_start="2027-08-01",
			current_invoice_end="2027-08-07",
			get_billing_cycle_data=lambda: {"days": 6},
		)

		annual_snapshot = self.module.build_cadence_snapshot(
			annual,
			billing_cycle_anchor=annual_anchor,
			interval="month",
			interval_count=12,
			pause_start_at=annual_anchor,
		)
		weekly_snapshot = self.module.build_cadence_snapshot(
			weekly,
			billing_cycle_anchor=weekly_anchor,
			interval="day",
			interval_count=7,
			pause_start_at=weekly_anchor,
		)

		self.assertEqual(annual_snapshot["interval_count"], 12)
		self.assertEqual(weekly_snapshot["interval_count"], 7)

	def test_resume_after_a_boundary_timestamp_uses_the_following_boundary(self):
		anchor = int(datetime(2027, 1, 28, 8, tzinfo=timezone.utc).timestamp())
		february = int(datetime(2027, 2, 28, 8, tzinfo=timezone.utc).timestamp())
		march = int(datetime(2027, 3, 28, 8, tzinfo=timezone.utc).timestamp())
		snapshot = self.module.build_cadence_snapshot(
			self.subscription(
				current_invoice_start="2027-01-28",
				current_invoice_end="2027-02-27",
			),
			billing_cycle_anchor=anchor,
			interval="month",
			interval_count=1,
			pause_start_at=anchor,
		)
		subscription = self.subscription(
			current_invoice_start="2027-01-28",
			current_invoice_end="2027-02-27",
			stripe_pause_cadence_snapshot=self.module.serialize_cadence_snapshot(snapshot),
			stripe_pause_start="2027-01-28",
			stripe_resume_on="2027-04-28",
			stripe_pause_start_at=str(anchor),
			stripe_resume_at=str(int(datetime(2027, 4, 28, 8, tzinfo=timezone.utc).timestamp())),
		)

		target = self.module.build_resume_target(
			subscription,
			current_timestamp=february,
		)

		self.assertEqual(target["resume_at"], march)
		self.assertEqual(target["resume_on"], "2027-03-28")

	def test_pause_cycles_are_strict_integers_between_one_and_twelve(self):
		self.assertEqual(self.module.validate_pause_cycles(1), 1)
		self.assertEqual(self.module.validate_pause_cycles("12"), 12)
		for invalid in (True, 1.0, "1.5", " 1 ", 0, 13):
			with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
				self.module.validate_pause_cycles(invalid)

	def test_active_pause_uses_snapshot_after_subscription_plan_cadence_changes(self):
		anchor = 1801382400
		snapshot = self.module.build_cadence_snapshot(
			self.subscription(),
			billing_cycle_anchor=anchor,
			interval="month",
			interval_count=1,
		)
		subscription = self.subscription(
			stripe_pause_cadence_snapshot=self.module.serialize_cadence_snapshot(snapshot),
			stripe_pause_start="2027-01-31",
			stripe_resume_on="2027-05-31",
			stripe_pause_start_at=str(anchor),
			stripe_resume_at="1811750400",
			get_billing_cycle_data=lambda: {"months": 3, "days": -1},
		)

		target = self.module.build_resume_target(subscription, posting_date="2027-03-01")

		self.assertEqual(
			target,
			{
				"billing_cycles": 2,
				"resume_on": "2027-03-31",
				"resume_at": 1806480000,
				"cancel_before_start": False,
			},
		)
		self.assertEqual(
			self.module.count_pause_cycles(subscription, "2027-01-31", "2027-03-31"),
			2,
		)
		self.assertEqual(
			self.module.extend_end_date(subscription, "2027-01-31", 2),
			date(2027, 3, 31),
		)

	def test_calendar_month_partial_period_fails_when_stripe_cannot_align(self):
		anchor = int(datetime(2027, 8, 15, 7, tzinfo=timezone.utc).timestamp())
		subscription = self.subscription(
			follow_calendar_months=1,
			current_invoice_start="2027-08-15",
			current_invoice_end="2027-08-31",
			get_billing_cycle_and_interval=lambda: [
				{"billing_interval": "Month", "billing_interval_count": 1}
			],
		)

		with self.assertRaisesRegex(RuntimeError, "partial boundary cannot align"):
			self.module.build_cadence_snapshot(
				subscription,
				billing_cycle_anchor=anchor,
				interval="month",
				interval_count=1,
				pause_start_at=anchor,
			)

	def test_snapshot_reads_original_anchor_and_shared_cadence_from_stripe(self):
		anchor = int(datetime(2027, 1, 28, 8, tzinfo=timezone.utc).timestamp())
		remote = {
			"billing_cycle_anchor": anchor,
			"items": {
				"data": [
					{"price": {"recurring": {"interval": "month", "interval_count": 1}}},
					{"plan": {"interval": "month", "interval_count": 1}},
				]
			},
		}

		snapshot = self.module.build_stripe_cadence_snapshot(
			self.subscription(
				current_invoice_start="2027-01-28",
				current_invoice_end="2027-02-27",
			),
			remote,
			pause_start_at=anchor,
		)

		self.assertEqual(
			snapshot,
			{
				"version": 1,
				"billing_cycle_anchor": anchor,
				"interval": "month",
				"interval_count": 1,
				"follow_calendar_months": 0,
			},
		)


if __name__ == "__main__":
	unittest.main()
