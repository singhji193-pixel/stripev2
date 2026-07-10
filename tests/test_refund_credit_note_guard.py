import importlib
import sys
import types
import unittest
from unittest.mock import Mock

from module_isolation import restore_modules


class RefundCreditNoteGuardTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)

        fake_frappe = types.ModuleType("frappe")
        fake_frappe.session = types.SimpleNamespace(user="test@example.com")
        fake_frappe.local = types.SimpleNamespace(site="test.local")

        class ValidationError(Exception):
            pass

        class PermissionError(Exception):
            pass

        class AuthenticationError(Exception):
            pass

        fake_frappe.ValidationError = ValidationError
        fake_frappe.PermissionError = PermissionError
        fake_frappe.AuthenticationError = AuthenticationError

        def _throw(message, exc=None, title=None):
            exc_cls = exc or Exception
            raise exc_cls(f"{title}: {message}" if title else str(message))

        fake_frappe.throw = _throw
        fake_frappe.whitelist = lambda *args, **kwargs: (lambda f: f)
        fake_frappe.has_permission = lambda *args, **kwargs: True
        fake_frappe.get_roles = lambda *args, **kwargs: ["System Manager"]
        fake_frappe.get_doc = lambda *args, **kwargs: None
        fake_frappe.log_error = lambda *args, **kwargs: None
        fake_frappe.get_traceback = lambda: "traceback"
        fake_frappe.attach_print = lambda *args, **kwargs: None
        fake_frappe.sendmail = lambda *args, **kwargs: None

        fake_frappe.db = types.SimpleNamespace(
            exists=Mock(return_value=False),
            get_value=Mock(return_value=None),
            set_value=Mock(),
            commit=Mock(),
        )

        fake_utils = types.ModuleType("frappe.utils")
        fake_utils.flt = lambda x: float(x or 0)
        fake_utils.get_url = lambda: "http://localhost"
        fake_utils.now = lambda: "2026-03-12 00:00:00"
        fake_utils.fmt_money = lambda amount, currency=None: f"{currency or ''} {amount}"

        fake_password = types.ModuleType("frappe.utils.password")
        fake_password.check_password = lambda *args, **kwargs: True

        fake_stripe = types.ModuleType("stripe")

        sys.modules["frappe"] = fake_frappe
        sys.modules["frappe.utils"] = fake_utils
        sys.modules["frappe.utils.password"] = fake_password
        sys.modules["stripe"] = fake_stripe

        self.api = importlib.import_module("stripe_integration.stripe_integration.api")
        self.frappe = fake_frappe

    def tearDown(self):
        restore_modules(self._orig_modules)

    def test_has_submitted_credit_note_true_when_return_invoice_exists(self):
        self.frappe.db.exists.return_value = "SINV-CN-0001"

        self.assertTrue(self.api._has_submitted_credit_note("SINV-0001"))
        self.frappe.db.exists.assert_called_once_with(
            "Sales Invoice",
            {"docstatus": 1, "is_return": 1, "return_against": "SINV-0001"},
        )

    def test_require_submitted_credit_note_raises_with_clear_error_code(self):
        self.frappe.db.exists.return_value = None

        with self.assertRaises(self.frappe.ValidationError) as err:
            self.api._require_submitted_credit_note("SINV-0002")

        msg = str(err.exception)
        self.assertIn("credit_note_required_before_refund", msg)
        self.assertIn("Submit a Credit Note linked to this invoice", msg)


if __name__ == "__main__":
    unittest.main()
