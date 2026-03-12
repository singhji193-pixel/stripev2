import importlib
import sys
import types
import unittest
from unittest.mock import Mock


class _Obj(dict):
    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc

    def __setattr__(self, key, value):
        self[key] = value


class RefundPartialAutomationTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)

        fake_frappe = types.ModuleType("frappe")
        fake_frappe.log_error = lambda *args, **kwargs: None
        fake_frappe.get_traceback = lambda: "traceback"

        fake_utils = types.ModuleType("frappe.utils")
        fake_utils.flt = lambda x: float(x or 0)
        fake_utils.nowdate = lambda: "2026-03-12"

        fake_frappe.utils = fake_utils

        fake_frappe.db = types.SimpleNamespace(
            get_value=Mock(),
            commit=Mock(),
        )

        self.orig_payment_entry = _Obj(name="PE-ORIG", docstatus=1, paid_amount=100.0, received_amount=100.0)
        self.orig_payment_entry.references = [_Obj(reference_doctype="Sales Invoice", reference_name="SINV-0001")]
        self.orig_payment_entry.cancel = Mock()

        self.invoice_doc = types.SimpleNamespace(add_comment=Mock())

        def _get_doc(doctype, name):
            if doctype == "Payment Entry" and name == "PE-ORIG":
                return self.orig_payment_entry
            if doctype == "Sales Invoice" and name == "SINV-0001":
                return self.invoice_doc
            raise AssertionError(f"Unexpected get_doc({doctype}, {name})")

        fake_frappe.get_doc = _get_doc

        sys.modules["frappe"] = fake_frappe
        sys.modules["frappe.utils"] = fake_utils

        # Stub ERPNext get_payment_entry import path.
        self.refund_pe = _Obj(name="PE-REFUND-0001", references=[_Obj(reference_doctype="Sales Invoice", reference_name="CN-0001")])
        self.refund_pe.insert = Mock()
        self.refund_pe.submit = Mock()

        payment_entry_mod = types.ModuleType("erpnext.accounts.doctype.payment_entry.payment_entry")
        payment_entry_mod.get_payment_entry = Mock(return_value=self.refund_pe)

        erpnext = types.ModuleType("erpnext")
        accounts = types.ModuleType("erpnext.accounts")
        doctype = types.ModuleType("erpnext.accounts.doctype")
        payment_entry_pkg = types.ModuleType("erpnext.accounts.doctype.payment_entry")

        sys.modules["erpnext"] = erpnext
        sys.modules["erpnext.accounts"] = accounts
        sys.modules["erpnext.accounts.doctype"] = doctype
        sys.modules["erpnext.accounts.doctype.payment_entry"] = payment_entry_pkg
        sys.modules["erpnext.accounts.doctype.payment_entry.payment_entry"] = payment_entry_mod

        self.refunds = importlib.import_module("stripe_integration.stripe_integration.refunds")
        self.frappe = fake_frappe
        self.get_payment_entry = payment_entry_mod.get_payment_entry

    def tearDown(self):
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    def test_partial_refund_auto_allocates_to_credit_note(self):
        self.frappe.db.get_value.side_effect = ["PE-ORIG", "CN-0001"]

        out = self.refunds.apply_refund_to_erp(
            stripe_payment_intent_id="pi_123",
            stripe_refund_id="re_123",
            refund_amount=40.0,
            currency="CAD",
            source="webhook.refund.updated",
        )

        self.assertTrue(out["handled"])
        self.assertEqual(out["mode"], "partial_refund_credit_note_allocated")
        self.assertEqual(out["credit_note"], "CN-0001")
        self.assertEqual(out["refund_payment_entry"], "PE-REFUND-0001")

        self.get_payment_entry.assert_called_once_with("Sales Invoice", "CN-0001")
        self.refund_pe.insert.assert_called_once_with(ignore_permissions=True)
        self.refund_pe.submit.assert_called_once()
        self.frappe.db.commit.assert_called_once()

    def test_partial_refund_without_credit_note_falls_back_to_manual(self):
        self.frappe.db.get_value.side_effect = ["PE-ORIG", None]

        out = self.refunds.apply_refund_to_erp(
            stripe_payment_intent_id="pi_123",
            stripe_refund_id="re_123",
            refund_amount=40.0,
            currency="CAD",
            source="webhook.refund.updated",
        )

        self.assertTrue(out["handled"])
        self.assertEqual(out["mode"], "partial_refund_manual_adjustment_required")
        self.assertIsNone(out["credit_note"])

        self.get_payment_entry.assert_not_called()
        self.frappe.db.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
