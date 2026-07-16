from datetime import datetime, timezone

import frappe
from erpnext.accounts.doctype.subscription.subscription import Subscription
from frappe import _
from frappe.utils import add_days, add_to_date, getdate, nowdate

from stripe_integration.stripe_integration import subscription_pause
from stripe_integration.stripe_integration.accounting import MariaDBNamedLock
from stripe_integration.stripe_integration.subscription_pause import (
	CADENCE_SNAPSHOT_FIELD,
	OPERATION_ATTEMPT_FIELD,
	PAUSE_ACTIVE_FIELD,
	PAUSE_CYCLES_FIELD,
	PAUSE_LAST_RECONCILED_AT_FIELD,
	PAUSE_OPERATION_FIELD,
	PAUSE_START_FIELD,
	PAUSE_STATE_FIELD,
	PENDING_RESUME_AT_FIELD,
	PENDING_RESUME_FIELD,
	RESUME_AT_FIELD,
	RESUME_CANCEL_BEFORE_START_FIELD,
	RESUME_ON_FIELD,
	STATE_CANCELLING,
	STATE_PAUSING,
	STATE_RESUMING,
	count_pause_cycles,
	extend_end_date,
)

NO_INVOICE_FIELD = "custom_do_not_generate_invoices"
LOCK_FLAG = "stripe_subscription_action_lock_held"
INTERNAL_PAUSE_MUTATION_FLAG = "stripe_allow_coordinated_pause_mutation"
RECONCILIATION_FLAG = "stripe_allow_pre_pause_reconciliation"
TRUSTED_CATCH_UP_FLAG = "stripe_allow_trusted_catch_up"
TRANSACTION_LOCKS_ATTRIBUTE = "_stripe_subscription_transaction_locks"
MAX_CATCH_UP_PERIODS = 120
TERMINAL_STRIPE_STATUSES = {"canceled", "incomplete_expired"}
LIFECYCLE_MANAGER_ROLES = {"System Manager", "Accounts Manager"}
NATIVE_CANCEL_FIELDS = ("status", "cancelation_date", "cancel_at_period_end")
CANONICAL_STATE_FIELDS = (
	"stripe_paused",
	"current_invoice_start",
	"current_invoice_end",
	"end_date",
	*NATIVE_CANCEL_FIELDS,
)
ZERO_DEFAULT_COORDINATED_FIELDS = {
	PAUSE_ACTIVE_FIELD,
	PAUSE_CYCLES_FIELD,
	RESUME_CANCEL_BEFORE_START_FIELD,
	OPERATION_ATTEMPT_FIELD,
}
TERMINAL_NATIVE_STATUSES = {"cancelled", "canceled", "completed"}


def _plan_signature(subscription_doc) -> tuple:
	return tuple((row.get("plan"), row.get("qty")) for row in (subscription_doc.get("plans") or []))


def _coordinated_pause_fields() -> tuple[str, ...]:
	# Read this at validation time so new coordination fields join canonical refresh automatically.
	return tuple(subscription_pause.COORDINATED_PAUSE_FIELDS)


def _directly_protected_pause_fields() -> tuple[str, ...]:
	return tuple(
		fieldname
		for fieldname in _coordinated_pause_fields()
		if fieldname != PAUSE_LAST_RECONCILED_AT_FIELD
	)


def _normalized_value(value) -> str:
	return "" if value is None else str(value)


def _stripe_value(value, fieldname, default=None):
	if isinstance(value, dict):
		return value.get(fieldname, default)
	return getattr(value, fieldname, default)


def _utc_now_timestamp() -> int:
	return int(datetime.now(timezone.utc).timestamp())


def _retrieve_remote_pause_state(subscription_doc) -> dict:
	from stripe_integration.stripe_integration.subscription_sync import retrieve_subscription_pause_state

	return retrieve_subscription_pause_state(subscription_doc)


def _release_transaction_lock(lock_name: str, lock) -> None:
	registry = getattr(frappe.db, TRANSACTION_LOCKS_ATTRIBUTE, {})
	if registry.get(lock_name) is not lock:
		return
	registry.pop(lock_name, None)
	lock.__exit__(None, None, None)


def _acquire_transaction_lock(lock_name: str) -> None:
	registry = getattr(frappe.db, TRANSACTION_LOCKS_ATTRIBUTE, None)
	if registry is None:
		registry = {}
		setattr(frappe.db, TRANSACTION_LOCKS_ATTRIBUTE, registry)
	if lock_name in registry:
		return

	lock = MariaDBNamedLock(lock_name, timeout=30)
	lock.__enter__()
	registry[lock_name] = lock

	def release():
		_release_transaction_lock(lock_name, lock)

	try:
		frappe.db.after_commit.add(release)
		frappe.db.after_rollback.add(release)
	except Exception:
		_release_transaction_lock(lock_name, lock)
		raise


class StripeManagedSubscription(Subscription):
	def _flag_is_set(self, fieldname: str) -> bool:
		flags = getattr(self, "flags", None)
		return bool(getattr(flags, fieldname, False)) if flags is not None else bool(self.get(fieldname))

	def _set_flag(self, fieldname: str, value: bool) -> None:
		flags = getattr(self, "flags", None)
		if flags is not None:
			setattr(flags, fieldname, value)
		else:
			self.set(fieldname, value)

	def _lock_flag_is_set(self) -> bool:
		return self._flag_is_set(LOCK_FLAG)

	def _set_lock_flag(self, value: bool) -> None:
		self._set_flag(LOCK_FLAG, value)

	def _preserved_native_cancellation_intent(self, canonical_values) -> dict:
		if not canonical_values:
			return {}

		preserved = {}
		canonical_status = (canonical_values.get("status") or "").strip().lower()
		requested_status = (self.get("status") or "").strip().lower()
		canonical_is_terminal = canonical_status in TERMINAL_NATIVE_STATUSES
		if requested_status in {"cancelled", "canceled"} and not canonical_is_terminal:
			preserved["status"] = self.get("status")

		canonical_cancel_at_end = bool(int(canonical_values.get("cancel_at_period_end") or 0))
		requested_cancel_at_end = bool(int(self.get("cancel_at_period_end") or 0))
		if requested_cancel_at_end and not canonical_cancel_at_end and not canonical_is_terminal:
			preserved["cancel_at_period_end"] = self.get("cancel_at_period_end")

		if (
			self.get("cancelation_date")
			and not canonical_values.get("cancelation_date")
			and not canonical_is_terminal
		):
			preserved["cancelation_date"] = self.get("cancelation_date")
		return preserved

	def _refresh_canonical_state(self, *, reject_direct_pause_mutation: bool = False) -> None:
		fieldnames = tuple(dict.fromkeys((*_coordinated_pause_fields(), *CANONICAL_STATE_FIELDS)))
		values = frappe.db.get_value("Subscription", self.name, fieldnames, as_dict=True)
		if values:
			preserved_values = self._preserved_native_cancellation_intent(values)
			if reject_direct_pause_mutation and not self._flag_is_set(INTERNAL_PAUSE_MUTATION_FLAG):
				changed_fields = [
					fieldname
					for fieldname in _directly_protected_pause_fields()
					if fieldname in values
					and _normalized_value(self.get(fieldname))
					!= _normalized_value(values.get(fieldname))
				]
				if changed_fields:
					self._throw_direct_pause_mutation(changed_fields)
			for fieldname in fieldnames:
				if fieldname in values:
					self.set(fieldname, values.get(fieldname))
			for fieldname, value in preserved_values.items():
				self.set(fieldname, value)

	def _run_with_transaction_lock(
		self,
		callback,
		*,
		reject_direct_pause_mutation: bool = False,
		preserved_values=None,
	):
		if self._lock_flag_is_set() or not self.get("name"):
			return callback()

		_acquire_transaction_lock(f"stripe-subscription-action-{self.name}")
		self._refresh_canonical_state(
			reject_direct_pause_mutation=reject_direct_pause_mutation,
		)
		for fieldname, value in (preserved_values or {}).items():
			self.set(fieldname, value)
		self._set_lock_flag(True)
		try:
			return callback()
		finally:
			self._set_lock_flag(False)

	def _save(self, *args, **kwargs):
		parent_save = super()._save
		if self._lock_flag_is_set() or self.is_new() or not self.get("name"):
			return parent_save(*args, **kwargs)
		return self._run_with_transaction_lock(
			lambda: parent_save(*args, **kwargs),
			reject_direct_pause_mutation=True,
			preserved_values={
				"end_date": self.get("end_date"),
				"current_invoice_start": self.get("current_invoice_start"),
				"current_invoice_end": self.get("current_invoice_end"),
				**{fieldname: self.get(fieldname) for fieldname in NATIVE_CANCEL_FIELDS},
			},
		)

	def invoicing_is_disabled(self) -> bool:
		return bool(int(self.get(NO_INVOICE_FIELD) or 0))

	def billing_pause_is_active(self, posting_date=None) -> bool:
		return bool(int(self.get(PAUSE_ACTIVE_FIELD) or 0))

	def billing_pause_resume_on(self):
		if self.get(PAUSE_STATE_FIELD) == STATE_RESUMING and self.get(PENDING_RESUME_FIELD):
			return self.get(PENDING_RESUME_FIELD)
		return self.get(RESUME_ON_FIELD)

	def billing_pause_resume_at(self) -> int | None:
		fieldname = (
			PENDING_RESUME_AT_FIELD
			if self.get(PAUSE_STATE_FIELD) == STATE_RESUMING
			else RESUME_AT_FIELD
		)
		value = self.get(fieldname)
		if value in (None, ""):
			return None
		try:
			return int(value)
		except (TypeError, ValueError):
			frappe.throw(
				_("Subscription {0}: persisted Stripe resume timestamp is invalid").format(self.name),
				frappe.ValidationError,
			)

	def billing_pause_is_due(self, posting_date=None) -> bool:
		if not bool(int(self.get(PAUSE_ACTIVE_FIELD) or 0)):
			return False
		if self.get(PAUSE_STATE_FIELD) in {STATE_PAUSING, STATE_CANCELLING}:
			return False
		resume_at = self.billing_pause_resume_at()
		return bool(resume_at and _utc_now_timestamp() >= resume_at)

	def _validate_live_pause_cadence(self) -> None:
		cadence_snapshot = subscription_pause.load_cadence_snapshot(self)
		if not cadence_snapshot:
			return
		pause_start_at = self.get(subscription_pause.PAUSE_START_AT_FIELD)
		if not pause_start_at:
			frappe.throw(
				_("Subscription {0}: persisted Stripe pause anchor is missing").format(self.name),
				frappe.ValidationError,
			)
		subscription_pause.validate_cadence_alignment(
			self,
			cadence_snapshot,
			pause_start_at,
		)

	def complete_billing_pause(self) -> None:
		self._validate_live_pause_cadence()
		pause_start = getdate(self.get(PAUSE_START_FIELD))
		resume_on = getdate(self.billing_pause_resume_on())
		resume_at = self.billing_pause_resume_at()
		cycles = int(self.get(PAUSE_CYCLES_FIELD) or 0)
		if not cycles and resume_on > pause_start:
			cycles = count_pause_cycles(self, pause_start, resume_on)

		if self.end_date:
			self.set("end_date", extend_end_date(self, self.end_date, cycles))

		self.update_subscription_period(resume_on)
		values = {
			PAUSE_ACTIVE_FIELD: 0,
			RESUME_ON_FIELD: resume_on,
			RESUME_AT_FIELD: str(resume_at) if resume_at else "",
			PAUSE_STATE_FIELD: "",
			PAUSE_OPERATION_FIELD: "",
			PENDING_RESUME_FIELD: None,
			PENDING_RESUME_AT_FIELD: "",
			RESUME_CANCEL_BEFORE_START_FIELD: 0,
			OPERATION_ATTEMPT_FIELD: 0,
			PAUSE_CYCLES_FIELD: 0,
			PAUSE_LAST_RECONCILED_AT_FIELD: None,
			CADENCE_SNAPSHOT_FIELD: "",
			"stripe_paused": 0,
			"current_invoice_start": self.current_invoice_start,
			"current_invoice_end": self.current_invoice_end,
		}
		if self.end_date:
			values["end_date"] = self.end_date
		frappe.db.set_value("Subscription", self.name, values, update_modified=False)
		for fieldname, value in values.items():
			self.set(fieldname, value)
		self._doc_before_save = None

	def clear_billing_pause(self) -> None:
		values = {
			PAUSE_ACTIVE_FIELD: 0,
			PAUSE_STATE_FIELD: "",
			PAUSE_OPERATION_FIELD: "",
			PENDING_RESUME_FIELD: None,
			PENDING_RESUME_AT_FIELD: "",
			RESUME_CANCEL_BEFORE_START_FIELD: 0,
			OPERATION_ATTEMPT_FIELD: 0,
			PAUSE_CYCLES_FIELD: 0,
			PAUSE_LAST_RECONCILED_AT_FIELD: None,
			CADENCE_SNAPSHOT_FIELD: "",
			"stripe_paused": 0,
		}
		frappe.db.set_value("Subscription", self.name, values, update_modified=False)
		for fieldname, value in values.items():
			self.set(fieldname, value)

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

	def _coordinated_pause_fields_changed(self, before) -> list[str]:
		return [
			fieldname
			for fieldname in _directly_protected_pause_fields()
			if _normalized_value(self.get(fieldname)) != _normalized_value(before.get(fieldname))
		]

	def _coordinated_pause_fields_set_on_insert(self) -> list[str]:
		set_fields = []
		for fieldname in _directly_protected_pause_fields():
			value = self.get(fieldname)
			if fieldname in ZERO_DEFAULT_COORDINATED_FIELDS:
				if value not in (None, "", 0, "0", False):
					set_fields.append(fieldname)
			elif value not in (None, ""):
				set_fields.append(fieldname)
		return set_fields

	def _canonical_coordinated_pause_values(self):
		return frappe.db.get_value(
			"Subscription",
			self.name,
			_coordinated_pause_fields(),
			as_dict=True,
		)

	def _prepare_existing_save_for_validation(self, doc_before_save):
		if self.get("name") and not self._lock_flag_is_set():
			_acquire_transaction_lock(f"stripe-subscription-action-{self.name}")

		canonical_values = self._canonical_coordinated_pause_values()
		pause_values_before_save = doc_before_save or canonical_values
		if not self._flag_is_set(INTERNAL_PAUSE_MUTATION_FLAG):
			if pause_values_before_save and (
				changed_fields := self._coordinated_pause_fields_changed(pause_values_before_save)
			):
				self._throw_direct_pause_mutation(changed_fields)
			if canonical_values:
				for fieldname in _coordinated_pause_fields():
					if fieldname in canonical_values:
						self.set(fieldname, canonical_values.get(fieldname))

		return doc_before_save

	def _native_cancellation_changes(self, doc_before_save, *, is_new: bool) -> list[str]:
		baseline = doc_before_save
		if not baseline and not is_new and self.get("name"):
			baseline = frappe.db.get_value(
				"Subscription",
				self.name,
				NATIVE_CANCEL_FIELDS,
				as_dict=True,
			)
		baseline = baseline or {}

		changed = []
		old_cancel_at_end = bool(int(baseline.get("cancel_at_period_end") or 0))
		new_cancel_at_end = bool(int(self.get("cancel_at_period_end") or 0))
		if old_cancel_at_end != new_cancel_at_end:
			changed.append("cancel_at_period_end")

		old_status = (baseline.get("status") or "").strip().lower()
		new_status = (self.get("status") or "").strip().lower()
		if new_status in {"cancelled", "canceled"} and old_status not in {
			"cancelled",
			"canceled",
		}:
			changed.append("status")

		if _normalized_value(self.get("cancelation_date")) != _normalized_value(
			baseline.get("cancelation_date")
		):
			changed.append("cancelation_date")
		return changed

	def _native_cancellation_change_is_trusted(self) -> bool:
		if self._flag_is_set(INTERNAL_PAUSE_MUTATION_FLAG):
			return True
		if bool(getattr(getattr(frappe, "flags", None), "in_scheduler", False)):
			return True
		user = getattr(getattr(frappe, "session", None), "user", None)
		if user == "Administrator":
			return True
		return bool(LIFECYCLE_MANAGER_ROLES.intersection(frappe.get_roles(user)))

	def _validate_native_cancellation_permission(self, doc_before_save, *, is_new: bool) -> None:
		if not self._native_cancellation_changes(doc_before_save, is_new=is_new):
			return
		if self._native_cancellation_change_is_trusted():
			return
		frappe.throw(
			_("Only a System Manager or Accounts Manager can change Subscription cancellation"),
			frappe.PermissionError,
		)

	def _validate_linked_terminal_restart(self, doc_before_save) -> None:
		if not doc_before_save or not self.get("stripe_subscription_id"):
			return
		previous_status = (doc_before_save.get("status") or "").strip().lower()
		current_status = (self.get("status") or "").strip().lower()
		if previous_status in {"cancelled", "canceled"} and current_status not in {
			"cancelled",
			"canceled",
		}:
			frappe.throw(
				_(
					"Stripe-linked Subscription {0} cannot be restarted after cancellation; "
					"create a new Subscription instead"
				).format(self.name),
				frappe.ValidationError,
			)

	def _throw_direct_pause_mutation(self, fieldnames: list[str]) -> None:
		frappe.throw(
			_("Subscription {0}: Stripe pause fields cannot be changed directly ({1})").format(
				self.name,
				", ".join(fieldnames),
			),
			frappe.ValidationError,
		)

	def validate(self) -> None:
		doc_before_save = None
		is_new = self.is_new()
		if is_new and not self._flag_is_set(INTERNAL_PAUSE_MUTATION_FLAG):
			if set_fields := self._coordinated_pause_fields_set_on_insert():
				self._throw_direct_pause_mutation(set_fields)
		elif not is_new:
			doc_before_save = self.get_doc_before_save()
			doc_before_save = self._prepare_existing_save_for_validation(doc_before_save)

		self._validate_native_cancellation_permission(doc_before_save, is_new=is_new)
		self._validate_linked_terminal_restart(doc_before_save)

		if self.billing_pause_is_active() and doc_before_save and (
			_plan_signature(doc_before_save) != _plan_signature(self)
			or int(doc_before_save.get("follow_calendar_months") or 0)
			!= int(self.get("follow_calendar_months") or 0)
		):
			frappe.throw(_("Subscription {0}: billing cycle cannot change while paused").format(self.name))
		return super().validate()

	def can_generate_new_invoice(self, posting_date=None) -> bool:
		if self.invoicing_is_disabled() or self.billing_pause_is_active(posting_date):
			return False
		if self._flag_is_set(TRUSTED_CATCH_UP_FLAG):
			original_setting = self.get("generate_new_invoices_past_due_date")
			self.set("generate_new_invoices_past_due_date", 1)
			try:
				return super().can_generate_new_invoice(posting_date)
			finally:
				self.set("generate_new_invoices_past_due_date", original_setting)
		return super().can_generate_new_invoice(posting_date)

	def _pre_pause_reconciliation_is_allowed(self, kwargs) -> bool:
		flags = getattr(self, "flags", None)
		if not flags or not getattr(flags, RECONCILIATION_FLAG, False):
			return False
		pause_start = self.get(PAUSE_START_FIELD)
		to_date = kwargs.get("to_date")
		return bool(pause_start and to_date and getdate(to_date) < getdate(pause_start))

	def _create_invoice_checked(self, *args, **kwargs):
		if self.invoicing_is_disabled():
			frappe.throw(_("Subscription {0}: invoice generation is disabled").format(self.name))
		if self.billing_pause_is_active() and not self._pre_pause_reconciliation_is_allowed(kwargs):
			frappe.throw(_("Subscription {0}: invoice generation is paused").format(self.name))
		return super().create_invoice(*args, **kwargs)

	def create_invoice(self, *args, **kwargs):
		return self._run_with_transaction_lock(lambda: self._create_invoice_checked(*args, **kwargs))

	@frappe.whitelist()
	def process(self, posting_date=None) -> bool:
		return self._run_with_transaction_lock(lambda: self._process_subscription(posting_date))

	def _canonical_remote_pause_state(self) -> dict:
		try:
			return _retrieve_remote_pause_state(self)
		except Exception as exc:
			frappe.log_error(
				message=f"Subscription {self.name}: {exc}",
				title="Stripe subscription resume check failed",
			)
			frappe.throw(
				_("Subscription {0}: Stripe resume status could not be verified").format(self.name),
				frappe.ValidationError,
			)

	def _cancel_for_terminal_remote_state(self) -> None:
		self.clear_billing_pause()
		self.cancel_subscription_at_period_end()
		self._set_flag(INTERNAL_PAUSE_MUTATION_FLAG, True)
		try:
			self.save()
		finally:
			self._set_flag(INTERNAL_PAUSE_MUTATION_FLAG, False)

	def _native_status_is_terminal(self) -> bool:
		return (self.get("status") or "").strip().lower() in TERMINAL_NATIVE_STATUSES

	def _save_internal_pause_state(self) -> None:
		self._set_flag(INTERNAL_PAUSE_MUTATION_FLAG, True)
		try:
			self.save()
		finally:
			self._set_flag(INTERNAL_PAUSE_MUTATION_FLAG, False)

	def _complete_fixed_term_before_catch_up(self, period_start) -> bool:
		end_date = self.get("end_date")
		if not end_date or getdate(period_start) <= getdate(end_date):
			return False
		# Preserve ERPNext's native status precedence. An overdue final invoice can
		# remain Past Due/Unpaid even though no later service period may be invoiced.
		self.set_subscription_status(posting_date=getdate(nowdate()))
		self._save_internal_pause_state()
		return True

	def _cancel_after_first_catch_up_period(self) -> None:
		self.cancel_subscription_at_period_end()
		self._save_internal_pause_state()

	def _process_aligned_periods_through_today(self):
		last_result = False
		today = getdate(nowdate())
		for _period in range(MAX_CATCH_UP_PERIODS):
			if self._native_status_is_terminal():
				return last_result
			period_start = getdate(self.current_invoice_start)
			if period_start > today:
				return last_result
			if self._complete_fixed_term_before_catch_up(period_start):
				return last_result
			period_end = getdate(self.current_invoice_end)
			already_generated = self.is_current_invoice_generated(period_start, period_end)
			cancel_after_period = bool(int(self.get("cancel_at_period_end") or 0))
			catch_up_was_enabled = self._flag_is_set(TRUSTED_CATCH_UP_FLAG)
			self._set_flag(TRUSTED_CATCH_UP_FLAG, True)
			try:
				last_result = super().process(period_start)
			finally:
				self._set_flag(TRUSTED_CATCH_UP_FLAG, catch_up_was_enabled)
			if self._native_status_is_terminal():
				return last_result

			advanced_start = getdate(self.current_invoice_start)
			if advanced_start > period_start:
				if cancel_after_period:
					self._cancel_after_first_catch_up_period()
					return last_result
				continue
			if already_generated or self.is_current_invoice_generated(period_start, period_end):
				self.update_subscription_period(add_days(period_end, 1))
				if getdate(self.current_invoice_start) <= period_start:
					frappe.throw(
						_("Subscription {0}: catch-up period did not advance safely").format(self.name),
						frappe.ValidationError,
					)
				self._save_internal_pause_state()
				if cancel_after_period:
					self._cancel_after_first_catch_up_period()
					return last_result
				continue
			frappe.throw(
				_("Subscription {0}: catch-up period could not be invoiced or advanced safely").format(
					self.name
				),
				frappe.ValidationError,
			)

		if getdate(self.current_invoice_start) <= today:
			frappe.throw(
				_("Subscription {0}: too many overdue billing periods to resume safely").format(self.name),
				frappe.ValidationError,
			)
		return last_result

	def _process_subscription(self, posting_date=None) -> bool:
		if not self.invoicing_is_disabled():
			natively_cancelled = bool(
				self.get("cancelation_date")
				or (self.get("status") or "").strip().lower() in {"cancelled", "canceled"}
			)
			if natively_cancelled:
				if (
					self.billing_pause_is_active()
					and self.get(PAUSE_STATE_FIELD) != STATE_CANCELLING
				):
					self.clear_billing_pause()
				return False
			if self.billing_pause_is_due(posting_date):
				remote_state = self._canonical_remote_pause_state()
				remote_status = (_stripe_value(remote_state.get("remote"), "status", "") or "").lower()
				if remote_status in TERMINAL_STRIPE_STATUSES:
					self._cancel_for_terminal_remote_state()
					return False
				if remote_state.get("paused"):
					return False
				self.complete_billing_pause()
				return self._process_aligned_periods_through_today()
			elif self.billing_pause_is_active(posting_date):
				return False
			if self.get("current_invoice_start") and self._complete_fixed_term_before_catch_up(
				self.get("current_invoice_start")
			):
				return False
			return super().process(posting_date)

		self.set_subscription_status(posting_date=posting_date)
		self.save()
		return False


NonBillingSubscription = StripeManagedSubscription
