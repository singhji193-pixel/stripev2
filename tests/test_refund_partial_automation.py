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
            sql=Mock(side_effect=[[[1]], [[1]]]),
        )
        fake_frappe.ValidationError = Exception
        fake_frappe.throw = lambda message, exc=None: (_ for _ in ()).throw(Exception(message))

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
        self.refund_pe = _Obj(
            name="PE-REFUND-0001",
            company="COEngine Service Inc.",
            payment_type="Pay",
            references=[_Obj(reference_doctype="Sales Invoice", reference_name="CN-0001")],
        )
        self.refund_pe.meta = types.SimpleNamespace(get_field=lambda fieldname: object())
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
        self.refunds._find_existing_refund_payment_entry = Mock(return_value=None)
        self.refunds.route_payment_entry_to_stripe_clearing = Mock()
        self.refunds.validate_stripe_currency = Mock()
        self.refunds.get_company_abbr_from_company = Mock(return_value="COE")
        self.frappe = fake_frappe
        self.get_payment_entry = payment_entry_mod.get_payment_entry

    def tearDown(self):
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    def test_partial_refund_auto_allocates_to_credit_note(self):
        self.frappe.db.get_value.side_effect = ["PE-ORIG", "CAD", "CN-0001", -40.0, "CAD"]

        out = self.refunds.apply_refund_to_erp(
            stripe_payment_intent_id="pi_123",
            stripe_refund_id="re_123",
            refund_amount=40.0,
            currency="CAD",
            source="webhook.refund.updated",
        )

        self.assertTrue(out["handled"])
        self.assertEqual(out["mode"], "refund_credit_note_allocated")
        self.assertEqual(out["credit_note"], "CN-0001")
        self.assertEqual(out["refund_payment_entry"], "PE-REFUND-0001")

        self.get_payment_entry.assert_called_once_with("Sales Invoice", "CN-0001")
        self.refund_pe.insert.assert_called_once_with(ignore_permissions=True)
        self.refund_pe.submit.assert_called_once()
        self.frappe.db.commit.assert_called_once()

    def test_partial_refund_without_credit_note_falls_back_to_manual(self):
        self.frappe.db.get_value.side_effect = ["PE-ORIG", "CAD", None]

        out = self.refunds.apply_refund_to_erp(
            stripe_payment_intent_id="pi_123",
            stripe_refund_id="re_123",
            refund_amount=40.0,
            currency="CAD",
            source="webhook.refund.updated",
        )

        self.assertFalse(out["handled"])
        self.assertEqual(out["reason"], "credit_note_required")

        self.get_payment_entry.assert_not_called()
        self.frappe.db.commit.assert_not_called()

    def test_full_refund_pays_credit_note_without_cancelling_original_receipt(self):
        self.refunds._find_matching_payment_entry = Mock(return_value=self.orig_payment_entry)
        self.refunds._find_submitted_credit_note = Mock(return_value="CN-0001")
        self.refunds._create_refund_payment_entry = Mock(return_value="PE-REFUND-0001")
        self.frappe.db.get_value.side_effect = ["CAD", -100.0]

        out = self.refunds.apply_refund_to_erp(
            stripe_payment_intent_id="pi_123",
            stripe_refund_id="re_full",
            refund_amount=100.0,
            currency="CAD",
            source="webhook.refund.updated",
        )

        self.assertTrue(out["handled"])
        self.assertEqual(out["mode"], "refund_credit_note_allocated")
        self.assertEqual(out["refund_payment_entry"], "PE-REFUND-0001")
        self.orig_payment_entry.cancel.assert_not_called()

    def test_duplicate_refund_event_reuses_existing_refund_payment_entry(self):
        self.refunds._find_existing_refund_payment_entry = Mock(return_value="PE-REFUND-0001")
        self.refunds._find_matching_payment_entry = Mock()

        out = self.refunds.apply_refund_to_erp(
            stripe_payment_intent_id="pi_123",
            stripe_refund_id="re_123",
            refund_amount=40.0,
            currency="CAD",
        )

        self.assertTrue(out["handled"])
        self.assertTrue(out["dedup"])
        self.assertEqual(out["refund_payment_entry"], "PE-REFUND-0001")
        self.refunds._find_matching_payment_entry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
