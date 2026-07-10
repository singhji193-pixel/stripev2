import importlib
import sys
import types
import unittest

from module_isolation import restore_modules


class RefundSecurityAuditTrailTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)
        self.audit_events = []

        fake_frappe = types.ModuleType("frappe")

        class _PermissionError(Exception):
            pass

        class _ValidationError(Exception):
            pass

        class _AuthenticationError(Exception):
            pass

        fake_frappe.PermissionError = _PermissionError
        fake_frappe.ValidationError = _ValidationError
        fake_frappe.AuthenticationError = _AuthenticationError
        fake_frappe.session = types.SimpleNamespace(user="auditor@example.com")
        fake_frappe.local = types.SimpleNamespace(site="test.local")
        self._roles = ["Accounts Manager"]

        def _throw(msg, exc=None, title=None):
            exc_cls = exc or Exception
            raise exc_cls(f"{title}: {msg}" if title else str(msg))

        class _Logger:
            def __init__(self, sink):
                self._sink = sink

            def info(self, payload):
                self._sink.append(payload)

        fake_frappe.throw = _throw
        fake_frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)
        fake_frappe.get_roles = lambda _user: list(self._roles)
        fake_frappe.logger = lambda *_args, **_kwargs: _Logger(self.audit_events)
        fake_frappe.has_permission = lambda *args, **kwargs: True
        fake_frappe.get_doc = lambda *args, **kwargs: None
        fake_frappe.log_error = lambda *args, **kwargs: None
        fake_frappe.get_traceback = lambda: "traceback"
        fake_frappe.attach_print = lambda *args, **kwargs: None
        fake_frappe.sendmail = lambda *args, **kwargs: None

        self._auth_exists = False
        self._threshold = 0
        self._credit_note_exists = True

        def _db_get_value(doctype, filters, fieldname=None, as_dict=False):
            if doctype == "__Auth":
                return "AUTH-ROW" if self._auth_exists else None
            return None

        fake_frappe.db = types.SimpleNamespace(
            get_single_value=lambda *args, **kwargs: self._threshold,
            get_value=_db_get_value,
            exists=lambda *args, **kwargs: "SINV-CN-0001" if self._credit_note_exists else None,
            set_value=lambda *args, **kwargs: None,
            commit=lambda: None,
        )

        fake_utils = types.ModuleType("frappe.utils")
        fake_utils.flt = lambda x: float(x or 0)
        fake_utils.get_url = lambda: "http://localhost"
        fake_utils.now = lambda: "2026-03-12"
        fake_utils.fmt_money = lambda v: f"{float(v):.2f}"

        fake_password = types.ModuleType("frappe.utils.password")
        self._password_valid = True

        def _check_password(*args, **kwargs):
            if not self._password_valid:
                raise _AuthenticationError("bad password")
            return True

        fake_password.check_password = _check_password

        sys.modules["frappe"] = fake_frappe
        sys.modules["frappe.utils"] = fake_utils
        sys.modules["frappe.utils.password"] = fake_password
        sys.modules["stripe"] = types.ModuleType("stripe")

        fake_utils_mod = types.ModuleType("stripe_integration.stripe_integration.utils")
        fake_utils_mod.get_api_key = lambda *_args, **_kwargs: "sk_test"
        fake_utils_mod.get_company_abbr_from_company = lambda *_args, **_kwargs: "COE"

        fake_refunds_mod = types.ModuleType("stripe_integration.stripe_integration.refunds")
        fake_refunds_mod.apply_refund_to_erp = lambda **kwargs: {"handled": True}

        sys.modules["stripe_integration.stripe_integration.utils"] = fake_utils_mod
        sys.modules["stripe_integration.stripe_integration.refunds"] = fake_refunds_mod

        self.api = importlib.import_module("stripe_integration.stripe_integration.api")
        self.frappe = fake_frappe

    def tearDown(self):
        restore_modules(self._orig_modules)

    def _find_event(self, check, outcome):
        for event in self.audit_events:
            if event.get("check") == check and event.get("outcome") == outcome:
                return event
        return None

    def test_role_and_password_gate_logs_are_written_without_secret_values(self):
        self._auth_exists = True
        self.api._require_stripe_action_guard("super-secret", invoice_name="SINV-0001")

        role_event = self._find_event("role_guard", "passed")
        pw_event = self._find_event("password_gate", "passed")

        self.assertIsNotNone(role_event)
        self.assertIsNotNone(pw_event)
        self.assertNotIn("super-secret", str(self.audit_events))

    def test_password_gate_missing_password_is_logged_and_blocked(self):
        self._auth_exists = True

        with self.assertRaises(self.frappe.PermissionError):
            self.api._require_stripe_action_guard(None, invoice_name="SINV-0002")

        self.assertIsNotNone(self._find_event("password_gate", "blocked_missing_password"))

    def test_threshold_block_is_logged(self):
        self._threshold = 1000
        self._roles = ["Accounts Manager"]

        with self.assertRaises(self.frappe.PermissionError):
            self.api._enforce_refund_threshold_approval(1000, invoice_name="SINV-0003")

        event = self._find_event("threshold_guard", "blocked_non_system_manager")
        self.assertIsNotNone(event)
        self.assertEqual(event["details"]["threshold"], 1000.0)

    def test_credit_note_missing_is_logged(self):
        self._credit_note_exists = False

        with self.assertRaises(self.frappe.ValidationError):
            self.api._require_submitted_credit_note("SINV-0004")

        self.assertIsNotNone(self._find_event("credit_note_guard", "blocked_missing_credit_note"))


if __name__ == "__main__":
    unittest.main()
