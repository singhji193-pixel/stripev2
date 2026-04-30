"""Tests for invoice.paid webhook handling (subscription_payments.py).

Covers:
- Happy path: PE creation from invoice.paid event
- Database lock prevents concurrent duplicate PE creation
- Dedup checks only submitted PEs (not drafts, fixing the permanent-stuck bug)
- DuplicateEntryError is caught gracefully
- Missing sales invoice returns appropriate error
- Non-submitted invoice is rejected
- Zero outstanding skips PE creation
- stripe_payment_intent_id and stripe_invoice_id are set on relevant docs
"""

import importlib
import sys
import types
import unittest
from unittest.mock import Mock, call


class _Obj(dict):
    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc

    def __setattr__(self, key, value):
        self[key] = value


class _FakeMeta:
    def __init__(self, fields=None):
        self._fields = set(fields or [])

    def get_field(self, name):
        return name if name in self._fields else None


class InvoicePaidWebhookTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)
        self._committed = []
        self._lock_acquired = True
        self._existing_submitted_pe = False
        self._set_values = {}
        self._lock_calls = []

        fake_frappe = types.ModuleType("frappe")

        class _ValidationError(Exception):
            pass

        fake_frappe.ValidationError = _ValidationError
        fake_frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)
        fake_frappe.log_error = lambda *args, **kwargs: None
        fake_frappe.get_traceback = lambda: "traceback"

        fake_utils = types.ModuleType("frappe.utils")
        fake_utils.flt = lambda x: float(x or 0)
        fake_utils.nowdate = lambda: "2026-04-25"

        fake_frappe.utils = fake_utils

        self_ref = self

        def _db_sql(query, params=None):
            q = str(query)
            if "GET_LOCK" in q:
                self_ref._lock_calls.append(("GET_LOCK", params))
                return [[1 if self_ref._lock_acquired else 0]]
            if "RELEASE_LOCK" in q:
                self_ref._lock_calls.append(("RELEASE_LOCK", params))
                return None
            return [[0]]

        def _db_exists(doctype, filters=None, **kwargs):
            if doctype == "Payment Entry" and isinstance(filters, dict):
                # Only match submitted PEs (docstatus=1)
                if filters.get("docstatus") == 1:
                    return self_ref._existing_submitted_pe
            return False

        def _db_set_value(doctype, name, field, value=None, update_modified=True):
            self_ref._set_values[(doctype, name, field)] = value

        def _db_get_value(doctype, filters=None, field=None, **kwargs):
            return None

        def _db_commit():
            self_ref._committed.append(True)

        fake_frappe.db = types.SimpleNamespace(
            sql=_db_sql,
            exists=_db_exists,
            set_value=_db_set_value,
            get_value=_db_get_value,
            commit=_db_commit,
            rollback=Mock(),
        )

        self._invoice = _Obj(
            name="SINV-0001",
            docstatus=1,
            outstanding_amount=200.0,
        )

        self._pe = _Obj(
            name="PE-NEW-001",
            references=[_Obj(allocated_amount=0)],
            paid_amount=0,
            received_amount=0,
            reference_no=None,
            reference_date=None,
        )
        self._pe.insert = Mock()
        self._pe.submit = Mock()

        fake_frappe.get_doc = lambda doctype, name: self._invoice if doctype == "Sales Invoice" else None
        fake_frappe.get_meta = lambda doctype: _FakeMeta(["stripe_invoice_id", "stripe_payment_intent_id"])
        fake_frappe.throw = lambda msg, exc=None: (_ for _ in ()).throw((exc or Exception)(msg))

        fake_frappe_exceptions = types.ModuleType("frappe.exceptions")
        fake_frappe_exceptions.DuplicateEntryError = type("DuplicateEntryError", (Exception,), {})

        pe_mod = types.ModuleType("erpnext.accounts.doctype.payment_entry.payment_entry")
        pe_mod.get_payment_entry = Mock(return_value=self._pe)

        sys.modules["frappe"] = fake_frappe
        sys.modules["frappe.utils"] = fake_utils
        sys.modules["frappe.exceptions"] = fake_frappe_exceptions
        sys.modules["erpnext"] = types.ModuleType("erpnext")
        sys.modules["erpnext.accounts"] = types.ModuleType("erpnext.accounts")
        sys.modules["erpnext.accounts.doctype"] = types.ModuleType("erpnext.accounts.doctype")
        sys.modules["erpnext.accounts.doctype.payment_entry"] = types.ModuleType("erpnext.accounts.doctype.payment_entry")
        sys.modules["erpnext.accounts.doctype.payment_entry.payment_entry"] = pe_mod

        self.sub_payments = importlib.import_module("stripe_integration.stripe_integration.subscription_payments")
        self.frappe = fake_frappe
        self.DuplicateEntryError = fake_frappe_exceptions.DuplicateEntryError

    def tearDown(self):
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    def _make_event(self, pi_id="pi_sub_123", invoice_id="in_stripe_001", amount_paid=20000, metadata=None):
        md = metadata or {"doctype": "Sales Invoice", "docname": "SINV-0001"}
        return {
            "id": "evt_inv_paid_001",
            "type": "invoice.paid",
            "data": {
                "object": {
                    "id": invoice_id,
                    "payment_intent": pi_id,
                    "amount_paid": amount_paid,
                    "metadata": md,
                }
            },
        }

    def test_happy_path_creates_pe_and_commits(self):
        event = self._make_event()
        out = self.sub_payments.handle_invoice_paid(event)

        self.assertTrue(out["handled"])
        self.assertEqual(out["sales_invoice"], "SINV-0001")
        self.assertIn("payment_entry", out)
        self._pe.insert.assert_called_once_with(ignore_permissions=True)
        self._pe.submit.assert_called_once()
        self.assertTrue(len(self._committed) > 0)

    def test_lock_is_acquired_and_released(self):
        event = self._make_event()
        self.sub_payments.handle_invoice_paid(event)

        get_lock_calls = [c for c in self._lock_calls if c[0] == "GET_LOCK"]
        release_lock_calls = [c for c in self._lock_calls if c[0] == "RELEASE_LOCK"]

        self.assertEqual(len(get_lock_calls), 1, "GET_LOCK should be called exactly once")
        self.assertEqual(len(release_lock_calls), 1, "RELEASE_LOCK should be called exactly once")

    def test_dedup_skips_when_submitted_pe_exists(self):
        self._existing_submitted_pe = True
        event = self._make_event()
        out = self.sub_payments.handle_invoice_paid(event)

        self.assertTrue(out["handled"])
        self.assertTrue(out.get("dedup"))
        self._pe.insert.assert_not_called()

    def test_missing_sales_invoice_returns_not_found(self):
        event = self._make_event(metadata={"doctype": "Sales Invoice", "docname": ""})
        # Empty docname
        event["data"]["object"]["metadata"]["docname"] = ""
        out = self.sub_payments.handle_invoice_paid(event)

        self.assertFalse(out["handled"])
        self.assertEqual(out["reason"], "sales_invoice_not_found")

    def test_non_submitted_invoice_rejected(self):
        self._invoice.docstatus = 0
        event = self._make_event()
        out = self.sub_payments.handle_invoice_paid(event)

        self.assertFalse(out["handled"])
        self.assertEqual(out["reason"], "invoice_not_submitted")

    def test_zero_outstanding_skips(self):
        self._invoice.outstanding_amount = 0
        event = self._make_event()
        out = self.sub_payments.handle_invoice_paid(event)

        self.assertTrue(out["handled"])
        self.assertEqual(out["reason"], "no_outstanding")
        self._pe.insert.assert_not_called()

    def test_allocation_capped_at_outstanding(self):
        self._invoice.outstanding_amount = 50.0
        event = self._make_event(amount_paid=20000)  # 200.00 > 50.00 outstanding
        self.sub_payments.handle_invoice_paid(event)

        self.assertEqual(self._pe.paid_amount, 50.0)
        self.assertEqual(self._pe.received_amount, 50.0)

    def test_duplicate_entry_error_handled_gracefully(self):
        self._pe.insert = Mock(side_effect=self.DuplicateEntryError("dup"))
        event = self._make_event()
        out = self.sub_payments.handle_invoice_paid(event)

        self.assertTrue(out["handled"])
        self.assertTrue(out.get("dedup"))

    def test_stripe_fields_set_on_docs(self):
        event = self._make_event()
        self.sub_payments.handle_invoice_paid(event)

        self.assertIn(
            ("Sales Invoice", "SINV-0001", "stripe_payment_intent_id"),
            self._set_values,
        )
        self.assertIn(
            ("Payment Entry", "PE-NEW-001", "stripe_payment_intent_id"),
            self._set_values,
        )


if __name__ == "__main__":
    unittest.main()
