import calendar
import json
import re
from datetime import datetime, timedelta, timezone
from math import gcd

import frappe
from frappe import _
from frappe.utils import add_days, add_months, add_to_date, get_last_day, getdate, nowdate

PAUSE_ACTIVE_FIELD = "stripe_erpnext_pause_active"
PAUSE_START_FIELD = "stripe_pause_start"
RESUME_ON_FIELD = "stripe_resume_on"
PAUSE_STATE_FIELD = "stripe_pause_state"
PAUSE_OPERATION_FIELD = "stripe_pause_operation_id"
PENDING_RESUME_FIELD = "stripe_pending_resume_on"
PAUSE_CYCLES_FIELD = "stripe_pause_cycles"
CADENCE_SNAPSHOT_FIELD = "stripe_pause_cadence_snapshot"
PAUSE_START_AT_FIELD = "stripe_pause_start_at"
RESUME_AT_FIELD = "stripe_resume_at"
PENDING_RESUME_AT_FIELD = "stripe_pending_resume_at"
RESUME_CANCEL_BEFORE_START_FIELD = "stripe_resume_cancel_before_start"
OPERATION_ATTEMPT_FIELD = "stripe_operation_attempt"
PAUSE_LAST_RECONCILED_AT_FIELD = "stripe_pause_last_reconciled_at"
MAX_PAUSE_CYCLES = 12
BEGINNING_OF_PERIOD = "Beginning of the current subscription period"
STATE_PAUSING = "Pausing"
STATE_PAUSED = "Paused"
STATE_RESUMING = "Resuming"
STATE_CANCELLING = "Cancelling"

COORDINATED_PAUSE_FIELDS = (
	PAUSE_ACTIVE_FIELD,
	PAUSE_START_FIELD,
	RESUME_ON_FIELD,
	PAUSE_STATE_FIELD,
	PAUSE_OPERATION_FIELD,
	PENDING_RESUME_FIELD,
	PAUSE_CYCLES_FIELD,
	CADENCE_SNAPSHOT_FIELD,
	PAUSE_START_AT_FIELD,
	RESUME_AT_FIELD,
	PENDING_RESUME_AT_FIELD,
	RESUME_CANCEL_BEFORE_START_FIELD,
	OPERATION_ATTEMPT_FIELD,
	PAUSE_LAST_RECONCILED_AT_FIELD,
)

CADENCE_SNAPSHOT_VERSION = 1
STRIPE_INTERVALS = {"day", "week", "month", "year"}


def _strict_integer(value, *, label: str, minimum: int = 1, maximum: int | None = None) -> int:
	if isinstance(value, bool):
		frappe.throw(_("{0} must be a whole number").format(label))
	if isinstance(value, int):
		parsed = value
	elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
		parsed = int(value)
	else:
		frappe.throw(_("{0} must be a whole number").format(label))
	if parsed < minimum or (maximum is not None and parsed > maximum):
		if maximum is None:
			frappe.throw(_("{0} must be at least {1}").format(label, minimum))
		frappe.throw(_("{0} must be between {1} and {2}").format(label, minimum, maximum))
	return parsed


def validate_pause_cycles(value) -> int:
	return _strict_integer(
		value,
		label=_("Pause cycles"),
		minimum=1,
		maximum=MAX_PAUSE_CYCLES,
	)


def _canonical_cadence_snapshot(value) -> dict:
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except (TypeError, ValueError):
			frappe.throw(_("Stored Stripe pause cadence is invalid JSON"))
	if not isinstance(value, dict):
		frappe.throw(_("Stored Stripe pause cadence is invalid"))

	version = value.get("version")
	if version != CADENCE_SNAPSHOT_VERSION:
		frappe.throw(_("Stored Stripe pause cadence version is unsupported"))
	anchor = _strict_integer(
		value.get("billing_cycle_anchor"),
		label=_("Stripe billing cycle anchor"),
	)
	interval = str(value.get("interval") or "").strip().lower()
	if interval not in STRIPE_INTERVALS:
		frappe.throw(_("Stripe billing interval is unsupported"))
	interval_count = _strict_integer(
		value.get("interval_count"),
		label=_("Stripe billing interval count"),
	)
	return {
		"version": CADENCE_SNAPSHOT_VERSION,
		"billing_cycle_anchor": anchor,
		"interval": interval,
		"interval_count": interval_count,
		"follow_calendar_months": 1 if int(value.get("follow_calendar_months") or 0) else 0,
	}


def serialize_cadence_snapshot(value) -> str:
	return json.dumps(_canonical_cadence_snapshot(value), sort_keys=True, separators=(",", ":"))


def load_cadence_snapshot(subscription_doc, *, required: bool = False) -> dict | None:
	raw = subscription_doc.get(CADENCE_SNAPSHOT_FIELD)
	if not raw:
		if required:
			frappe.throw(
				_("Subscription {0}: stored Stripe pause cadence is missing").format(subscription_doc.name)
			)
		return None
	return _canonical_cadence_snapshot(raw)


def _resolve_cadence_snapshot(subscription_doc, cadence_snapshot=None) -> dict | None:
	if cadence_snapshot is not None:
		return _canonical_cadence_snapshot(cadence_snapshot)
	return load_cadence_snapshot(subscription_doc)


def build_cadence_snapshot(
	subscription_doc,
	*,
	billing_cycle_anchor,
	interval,
	interval_count=1,
	pause_start_at=None,
) -> dict:
	snapshot = _canonical_cadence_snapshot(
		{
			"version": CADENCE_SNAPSHOT_VERSION,
			"billing_cycle_anchor": billing_cycle_anchor,
			"interval": interval,
			"interval_count": interval_count,
			"follow_calendar_months": subscription_doc.get("follow_calendar_months") or 0,
		}
	)
	if pause_start_at is not None:
		validate_cadence_alignment(subscription_doc, snapshot, pause_start_at)
	return snapshot


def _stripe_value(value, key):
	if isinstance(value, dict):
		return value.get(key)
	return getattr(value, key, None)


def build_stripe_cadence_snapshot(subscription_doc, stripe_subscription, *, pause_start_at) -> dict:
	anchor = _stripe_value(stripe_subscription, "billing_cycle_anchor")
	items = _stripe_value(_stripe_value(stripe_subscription, "items") or {}, "data") or []
	cadences = set()
	for item in items:
		price = _stripe_value(item, "price") or {}
		recurring = _stripe_value(price, "recurring") or _stripe_value(item, "plan") or {}
		interval = str(_stripe_value(recurring, "interval") or "").strip().lower()
		interval_count = _stripe_value(recurring, "interval_count") or 1
		if interval:
			cadences.add(
				(
					interval,
					_strict_integer(
						interval_count,
						label=_("Stripe billing interval count"),
					),
				)
			)
	if not cadences:
		plan = _stripe_value(stripe_subscription, "plan") or {}
		interval = str(_stripe_value(plan, "interval") or "").strip().lower()
		if interval:
			cadences.add(
				(
					interval,
					_strict_integer(
						_stripe_value(plan, "interval_count") or 1,
						label=_("Stripe billing interval count"),
					),
				)
			)
	if len(cadences) != 1:
		frappe.throw(_("Stripe subscription must expose one shared recurring cadence"))
	interval, interval_count = cadences.pop()
	return build_cadence_snapshot(
		subscription_doc,
		billing_cycle_anchor=anchor,
		interval=interval,
		interval_count=interval_count,
		pause_start_at=pause_start_at,
	)


def _cadence_months(snapshot: dict) -> int:
	if snapshot["interval"] == "month":
		return snapshot["interval_count"]
	if snapshot["interval"] == "year":
		return 12 * snapshot["interval_count"]
	return 0


def _cadence_days(snapshot: dict) -> int:
	if snapshot["interval"] == "day":
		return snapshot["interval_count"]
	if snapshot["interval"] == "week":
		return 7 * snapshot["interval_count"]
	return 0


def cadence_boundary_timestamp(cadence_snapshot, boundary_index) -> int:
	snapshot = _canonical_cadence_snapshot(cadence_snapshot)
	index = _strict_integer(
		boundary_index,
		label=_("Stripe billing boundary index"),
		minimum=0,
	)
	anchor = datetime.fromtimestamp(snapshot["billing_cycle_anchor"], tz=timezone.utc)
	shifted = _shift_from_anchor(
		anchor,
		months=_cadence_months(snapshot) * index,
		days=_cadence_days(snapshot) * index,
	)
	return int(shifted.timestamp())


def cadence_boundary_index(cadence_snapshot, boundary_timestamp) -> int:
	snapshot = _canonical_cadence_snapshot(cadence_snapshot)
	timestamp = _strict_integer(
		boundary_timestamp,
		label=_("Stripe billing boundary timestamp"),
	)
	anchor_timestamp = snapshot["billing_cycle_anchor"]
	if timestamp < anchor_timestamp:
		frappe.throw(_("Stripe timestamp precedes its billing cycle anchor"))

	days = _cadence_days(snapshot)
	if days:
		period_seconds = days * 24 * 60 * 60
		difference = timestamp - anchor_timestamp
		if difference % period_seconds:
			frappe.throw(_("Stripe timestamp is not an exact billing boundary"))
		return difference // period_seconds

	anchor = datetime.fromtimestamp(anchor_timestamp, tz=timezone.utc)
	target = datetime.fromtimestamp(timestamp, tz=timezone.utc)
	month_difference = (target.year - anchor.year) * 12 + target.month - anchor.month
	period_months = _cadence_months(snapshot)
	if month_difference < 0 or month_difference % period_months:
		frappe.throw(_("Stripe timestamp is not an exact billing boundary"))
	index = month_difference // period_months
	if cadence_boundary_timestamp(snapshot, index) != timestamp:
		frappe.throw(_("Stripe timestamp is not an exact billing boundary"))
	return index


def _cadence_timestamp_for_date(cadence_snapshot, value) -> int:
	snapshot = _canonical_cadence_snapshot(cadence_snapshot)
	boundary_date = getdate(value)
	anchor = datetime.fromtimestamp(snapshot["billing_cycle_anchor"], tz=timezone.utc)
	target = datetime.combine(boundary_date, anchor.timetz())
	return cadence_boundary_timestamp(snapshot, cadence_boundary_index(snapshot, int(target.timestamp())))


def _billing_boundary_delta(subscription_doc) -> tuple[int, int]:
	"""Return one complete billing interval as (calendar months, days)."""
	if int(subscription_doc.get("follow_calendar_months") or 0):
		billing_info = subscription_doc.get_billing_cycle_and_interval()
		if not billing_info:
			frappe.throw(
				_("Subscription {0}: billing cycle is required to pause").format(subscription_doc.name)
			)
		return int(billing_info[0]["billing_interval_count"] or 0), 0

	billing_cycle = dict(subscription_doc.get_billing_cycle_data() or {})
	if not billing_cycle:
		frappe.throw(_("Subscription {0}: billing cycle is required to pause").format(subscription_doc.name))

	months = int(billing_cycle.get("months") or 0) + (12 * int(billing_cycle.get("years") or 0))
	days = int(billing_cycle.get("days") or 0) + 1
	days += 7 * int(billing_cycle.get("weeks") or 0)
	return months, days


def _shift_from_anchor(value, *, months: int = 0, days: int = 0):
	"""Shift once from the original anchor so short months cannot compound clipping."""
	shifted = value
	if months:
		month_index = shifted.year * 12 + shifted.month - 1 + int(months)
		target_year, target_month_index = divmod(month_index, 12)
		target_month = target_month_index + 1
		shifted = shifted.replace(
			year=target_year,
			month=target_month,
			day=min(shifted.day, calendar.monthrange(target_year, target_month)[1]),
		)
	if days:
		shifted += timedelta(days=int(days))
	return shifted


def advance_billing_timestamp(
	subscription_doc,
	anchor_timestamp: int,
	billing_cycles: int,
	*,
	cadence_snapshot=None,
) -> int:
	cycles = _strict_integer(
		billing_cycles,
		label=_("Billing cycles"),
		minimum=0,
		maximum=MAX_PAUSE_CYCLES,
	)
	snapshot = _resolve_cadence_snapshot(subscription_doc, cadence_snapshot)
	if snapshot:
		anchor_index = cadence_boundary_index(snapshot, anchor_timestamp)
		return cadence_boundary_timestamp(snapshot, anchor_index + cycles)

	months, days = _billing_boundary_delta(subscription_doc)
	anchor = datetime.fromtimestamp(int(anchor_timestamp), tz=timezone.utc)
	shifted = _shift_from_anchor(anchor, months=months * cycles, days=days * cycles)
	return int(shifted.timestamp())


def _period_end(subscription_doc, period_start):
	start = getdate(period_start)
	if int(subscription_doc.get("follow_calendar_months") or 0):
		billing_info = subscription_doc.get_billing_cycle_and_interval()
		if not billing_info:
			frappe.throw(
				_("Subscription {0}: billing cycle is required to pause").format(subscription_doc.name)
			)
		interval_count = int(billing_info[0]["billing_interval_count"] or 0)
		return getdate(get_last_day(add_months(start, interval_count - 1)))

	billing_cycle = subscription_doc.get_billing_cycle_data()
	if not billing_cycle:
		frappe.throw(_("Subscription {0}: billing cycle is required to pause").format(subscription_doc.name))
	return getdate(add_to_date(start, **billing_cycle))


def next_billing_boundary(subscription_doc, period_start, *, cadence_snapshot=None):
	snapshot = _resolve_cadence_snapshot(subscription_doc, cadence_snapshot)
	if snapshot:
		start_timestamp = _cadence_timestamp_for_date(snapshot, period_start)
		start_index = cadence_boundary_index(snapshot, start_timestamp)
		return datetime.fromtimestamp(
			cadence_boundary_timestamp(snapshot, start_index + 1),
			tz=timezone.utc,
		).date()
	return getdate(add_days(_period_end(subscription_doc, period_start), 1))


def validate_cadence_alignment(subscription_doc, cadence_snapshot, pause_start_at) -> None:
	snapshot = _canonical_cadence_snapshot(cadence_snapshot)
	pause_timestamp = _strict_integer(
		pause_start_at,
		label=_("Stripe pause boundary timestamp"),
	)
	pause_index = cadence_boundary_index(snapshot, pause_timestamp)
	pause_date = datetime.fromtimestamp(pause_timestamp, tz=timezone.utc).date()
	erp_start = subscription_doc.get("current_invoice_start")
	if erp_start and pause_date != getdate(erp_start):
		frappe.throw(
			_("Stripe pause boundary {0} does not match ERPNext {1}").format(
				pause_date,
				getdate(erp_start),
			)
		)

	erp_months, erp_days = _billing_boundary_delta(subscription_doc)
	stripe_months = _cadence_months(snapshot)
	stripe_days = _cadence_days(snapshot)
	if (erp_months, erp_days) != (stripe_months, stripe_days):
		frappe.throw(
			_("Stripe cadence ({0}) is incompatible with ERPNext cadence ({1})").format(
				_cadence_description(stripe_months, stripe_days),
				_cadence_description(erp_months, erp_days),
			)
		)

	erp_next = getdate(add_days(_period_end(subscription_doc, pause_date), 1))
	stripe_next = datetime.fromtimestamp(
		cadence_boundary_timestamp(snapshot, pause_index + 1),
		tz=timezone.utc,
	).date()
	if erp_next != stripe_next:
		if int(subscription_doc.get("follow_calendar_months") or 0):
			frappe.throw(
				_(
					"Calendar-month partial boundary cannot align: Stripe resumes {0}, ERPNext resumes {1}"
				).format(stripe_next, erp_next)
			)
		frappe.throw(
			_("Stripe cadence resumes {0}, but ERPNext resumes {1}").format(
				stripe_next,
				erp_next,
			)
		)

	if not stripe_months:
		return

	# Stripe always derives month-based boundaries from its original anchor. ERPNext
	# advances from the preceding period, so a short month can silently clip every
	# later ERPNext boundary even when the first boundary happens to match. The
	# Gregorian month/day pattern repeats every 4,800 months; checking one complete
	# orbit proves that the two boundary sequences can remain aligned.
	erp_boundary = erp_next
	boundary_orbit = 4800 // gcd(4800, stripe_months)
	for boundary_offset in range(2, boundary_orbit + 1):
		erp_boundary = getdate(_shift_from_anchor(erp_boundary, months=erp_months))
		stripe_boundary = datetime.fromtimestamp(
			cadence_boundary_timestamp(snapshot, pause_index + boundary_offset),
			tz=timezone.utc,
		).date()
		if erp_boundary != stripe_boundary:
			frappe.throw(
				_(
					"Stripe cadence cannot stay aligned with ERPNext: "
					"Stripe reaches {0}, but ERPNext reaches {1}"
				).format(stripe_boundary, erp_boundary)
			)


def _cadence_description(months: int, days: int) -> str:
	if months > 0 and days == 0:
		return _("{0} calendar month(s)").format(months)
	if days > 0 and months == 0:
		return _("{0} day(s)").format(days)
	return _("an unsupported mixed interval")


def build_pause_window(
	subscription_doc,
	billing_cycles=1,
	posting_date=None,
	*,
	cadence_snapshot=None,
	pause_start_at=None,
) -> dict:
	cycles = validate_pause_cycles(billing_cycles)

	if subscription_doc.get("generate_invoice_at") != BEGINNING_OF_PERIOD:
		frappe.throw(
			_("Subscription {0}: save it with beginning-of-period billing before pausing").format(
				subscription_doc.name
			)
		)

	pause_start = subscription_doc.get("current_invoice_start")
	if not pause_start:
		frappe.throw(_("Subscription {0}: current billing period is missing").format(subscription_doc.name))
	pause_start = getdate(pause_start)
	if pause_start <= getdate(posting_date or nowdate()):
		frappe.throw(
			_("Subscription {0}: the pause boundary {1} must be after today").format(
				subscription_doc.name,
				pause_start,
			)
		)

	period_end = subscription_doc.get("current_invoice_end")
	if not period_end:
		frappe.throw(
			_("Subscription {0}: current billing period end is missing").format(subscription_doc.name)
		)
	if subscription_doc.is_current_invoice_generated(pause_start, period_end):
		frappe.throw(
			_("Subscription {0}: the billing period beginning {1} is already invoiced").format(
				subscription_doc.name,
				pause_start,
			)
		)

	snapshot = _resolve_cadence_snapshot(subscription_doc, cadence_snapshot)
	if snapshot:
		pause_timestamp = pause_start_at or subscription_doc.get(PAUSE_START_AT_FIELD)
		if not pause_timestamp:
			pause_timestamp = _cadence_timestamp_for_date(snapshot, pause_start)
		validate_cadence_alignment(subscription_doc, snapshot, pause_timestamp)
		resume_timestamp = advance_billing_timestamp(
			subscription_doc,
			pause_timestamp,
			cycles,
			cadence_snapshot=snapshot,
		)
		resume_on = datetime.fromtimestamp(resume_timestamp, tz=timezone.utc).date()
		return {
			"billing_cycles": cycles,
			"pause_start": str(pause_start),
			"resume_on": str(resume_on),
			"pause_start_at": int(pause_timestamp),
			"resume_at": resume_timestamp,
			"cadence_snapshot": serialize_cadence_snapshot(snapshot),
		}

	resume_on = pause_start
	for _cycle in range(cycles):
		resume_on = next_billing_boundary(subscription_doc, resume_on)

	return {
		"billing_cycles": cycles,
		"pause_start": str(pause_start),
		"resume_on": str(resume_on),
	}


def count_pause_cycles(
	subscription_doc,
	pause_start,
	resume_on,
	*,
	cadence_snapshot=None,
	pause_start_at=None,
	resume_at=None,
) -> int:
	snapshot = _resolve_cadence_snapshot(subscription_doc, cadence_snapshot)
	if snapshot:
		start_timestamp = pause_start_at or subscription_doc.get(PAUSE_START_AT_FIELD)
		if not start_timestamp:
			start_timestamp = _cadence_timestamp_for_date(snapshot, pause_start)
		end_timestamp = resume_at
		if not end_timestamp:
			stored_resume_at = subscription_doc.get(RESUME_AT_FIELD)
			if stored_resume_at:
				stored_date = datetime.fromtimestamp(int(stored_resume_at), tz=timezone.utc).date()
				if stored_date == getdate(resume_on):
					end_timestamp = stored_resume_at
		if not end_timestamp:
			end_timestamp = _cadence_timestamp_for_date(snapshot, resume_on)
		start_index = cadence_boundary_index(snapshot, start_timestamp)
		end_index = cadence_boundary_index(snapshot, end_timestamp)
		cycles = end_index - start_index
		if cycles < 0:
			frappe.throw(
				_("Subscription {0}: resume precedes the pause boundary").format(subscription_doc.name)
			)
		if cycles > MAX_PAUSE_CYCLES:
			frappe.throw(
				_("Subscription {0}: pause exceeds {1} billing cycles").format(
					subscription_doc.name,
					MAX_PAUSE_CYCLES,
				)
			)
		return cycles

	cursor = getdate(pause_start)
	resume = getdate(resume_on)
	cycles = 0
	while cursor < resume:
		cursor = next_billing_boundary(subscription_doc, cursor)
		cycles += 1
		if cycles > MAX_PAUSE_CYCLES:
			frappe.throw(
				_("Subscription {0}: pause exceeds {1} billing cycles").format(
					subscription_doc.name,
					MAX_PAUSE_CYCLES,
				)
			)
	if cursor != resume:
		frappe.throw(
			_("Subscription {0}: resume date is not a billing boundary").format(subscription_doc.name)
		)
	return cycles


def extend_end_date(subscription_doc, end_date, billing_cycles, *, cadence_snapshot=None):
	cycles = _strict_integer(
		billing_cycles,
		label=_("Billing cycles"),
		minimum=0,
		maximum=MAX_PAUSE_CYCLES,
	)
	snapshot = _resolve_cadence_snapshot(subscription_doc, cadence_snapshot)
	if snapshot:
		months = _cadence_months(snapshot)
		days = _cadence_days(snapshot)
	else:
		months, days = _billing_boundary_delta(subscription_doc)
	return getdate(
		_shift_from_anchor(
			getdate(end_date),
			months=months * cycles,
			days=days * cycles,
		)
	)


def build_resume_target(
	subscription_doc,
	posting_date=None,
	*,
	cadence_snapshot=None,
	current_timestamp=None,
) -> dict:
	pause_start = subscription_doc.get(PAUSE_START_FIELD)
	planned_resume = subscription_doc.get(RESUME_ON_FIELD)
	if not pause_start or not planned_resume:
		frappe.throw(_("Subscription {0}: pause dates are missing").format(subscription_doc.name))

	pause_start = getdate(pause_start)
	planned_resume = getdate(planned_resume)
	posting = getdate(posting_date or nowdate())
	snapshot = _resolve_cadence_snapshot(subscription_doc, cadence_snapshot)
	if snapshot:
		pause_timestamp = subscription_doc.get(PAUSE_START_AT_FIELD)
		if not pause_timestamp:
			pause_timestamp = _cadence_timestamp_for_date(snapshot, pause_start)
		pause_index = cadence_boundary_index(snapshot, pause_timestamp)

		planned_timestamp = subscription_doc.get(RESUME_AT_FIELD)
		if planned_timestamp:
			planned_timestamp = int(planned_timestamp)
			if datetime.fromtimestamp(planned_timestamp, tz=timezone.utc).date() != planned_resume:
				planned_timestamp = None
		if not planned_timestamp:
			planned_timestamp = _cadence_timestamp_for_date(snapshot, planned_resume)
		planned_index = cadence_boundary_index(snapshot, planned_timestamp)
		if planned_index - pause_index > MAX_PAUSE_CYCLES:
			frappe.throw(
				_("Subscription {0}: pause exceeds {1} billing cycles").format(
					subscription_doc.name,
					MAX_PAUSE_CYCLES,
				)
			)

		comparison_timestamp = int(current_timestamp) if current_timestamp is not None else None
		if comparison_timestamp is not None and comparison_timestamp < int(pause_timestamp):
			actual_timestamp = int(pause_timestamp)
			cancel_before_start = True
		elif comparison_timestamp is not None and comparison_timestamp >= int(planned_timestamp):
			actual_timestamp = int(planned_timestamp)
			cancel_before_start = False
		elif comparison_timestamp is not None:
			actual_timestamp = int(planned_timestamp)
			for boundary_index in range(pause_index + 1, planned_index + 1):
				candidate = cadence_boundary_timestamp(snapshot, boundary_index)
				if candidate > comparison_timestamp:
					actual_timestamp = candidate
					break
			cancel_before_start = False
		elif posting <= pause_start:
			actual_timestamp = int(pause_timestamp)
			cancel_before_start = posting < pause_start
		elif posting >= planned_resume:
			actual_timestamp = int(planned_timestamp)
			cancel_before_start = False
		else:
			actual_timestamp = int(pause_timestamp)
			for boundary_index in range(pause_index + 1, planned_index + 1):
				candidate = cadence_boundary_timestamp(snapshot, boundary_index)
				actual_timestamp = candidate
				if datetime.fromtimestamp(candidate, tz=timezone.utc).date() >= posting:
					break
			cancel_before_start = False
		actual_resume = datetime.fromtimestamp(actual_timestamp, tz=timezone.utc).date()
		cycles = count_pause_cycles(
			subscription_doc,
			pause_start,
			actual_resume,
			cadence_snapshot=snapshot,
			pause_start_at=pause_timestamp,
			resume_at=actual_timestamp,
		)
		return {
			"billing_cycles": cycles,
			"resume_on": str(actual_resume),
			"resume_at": actual_timestamp,
			"cancel_before_start": cancel_before_start,
		}

	if posting <= pause_start:
		actual_resume = pause_start
	elif posting >= planned_resume:
		actual_resume = planned_resume
	else:
		actual_resume = pause_start
		while actual_resume < posting:
			actual_resume = next_billing_boundary(subscription_doc, actual_resume)

	cycles = count_pause_cycles(subscription_doc, pause_start, actual_resume)
	return {
		"billing_cycles": cycles,
		"resume_on": str(actual_resume),
		"cancel_before_start": posting < pause_start,
	}
