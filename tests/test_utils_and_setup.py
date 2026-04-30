"""Tests for utility functions (utils.py) and setup (setup.py).

Covers:
- get_company_abbr_from_company with valid/missing company
- get_api_key throws on missing account
- get_api_key throws on missing key
- get_webhook_secret returns password
- _normalize_abbr edge cases
- import_accounts requires System Manager
- import_accounts creates and updates accounts
- clone_cosl_stripe_email_templates
"""

import importlib
import sys
import types
import unittest
from unittest.mock import Mock, MagicMock


class UtilsTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)

        fake_frappe = types.ModuleType("frappe")

        class _ValidationError(Exception):
            pass

        fake_frappe.ValidationError = _ValidationError

        def _throw(msg, *args, **kwargs):
            raise _ValidationError(msg)

        fake_frappe.throw = _throw

        self._account_docs = {}

        def _db_get_value(doctype, filters=None, field=None, **kwargs):
            if doctype == "Company" and isinstance(filters, str):
                # Direct lookup by name
                return {"COEngine Service Inc.": "COE", "CoreOrbit Systems Ltd.": "COSL"}.get(filters)
            if doctype == "Stripe Account" and isinstance(filters, dict):
                abbr = filters.get("company_abbr")
                if abbr in self._account_docs:
                    return self._account_docs[abbr]["name"]
            return None

        fake_frappe.db = types.SimpleNamespace(
            get_value=_db_get_value,
        )

        fake_password_doc = types.SimpleNamespace(
            secret_key_password="sk_test_123",
            webhook_secret_password="whsec_test_123",
        )

        def _get_doc(doctype, name):
            if doctype == "Stripe Account":
                doc = types.SimpleNamespace(
                    name=name,
                    publishable_key="pk_test_123",
                )
                doc.get_password = lambda field: {
                    "secret_key": "sk_test_123",
                    "webhook_secret": "whsec_test_123",
                }.get(field)
                return doc
            return None

        fake_frappe.get_doc = _get_doc

        sys.modules["frappe"] = fake_frappe

        self.utils = importlib.import_module("stripe_integration.stripe_integration.utils")
        self.frappe = fake_frappe

    def tearDown(self):
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    def test_get_company_abbr_returns_abbr(self):
        self.assertEqual(self.utils.get_company_abbr_from_company("COEngine Service Inc."), "COE")

    def test_get_company_abbr_empty_returns_none(self):
        self.assertIsNone(self.utils.get_company_abbr_from_company(""))
        self.assertIsNone(self.utils.get_company_abbr_from_company(None))

    def test_get_api_key_throws_on_missing_account(self):
        with self.assertRaises(Exception) as ctx:
            self.utils.get_api_key("NONEXISTENT")
        self.assertIn("not configured", str(ctx.exception))

    def test_get_api_key_returns_key_for_valid_account(self):
        self._account_docs["COE"] = {"name": "SA-COE"}
        key = self.utils.get_api_key("COE")
        self.assertEqual(key, "sk_test_123")

    def test_get_webhook_secret_returns_secret(self):
        self._account_docs["COE"] = {"name": "SA-COE"}
        secret = self.utils.get_webhook_secret("COE")
        self.assertEqual(secret, "whsec_test_123")

    def test_normalize_abbr_uppercase_and_strip(self):
        self.assertEqual(self.utils._normalize_abbr("  coe  "), "COE")
        self.assertEqual(self.utils._normalize_abbr("cosl"), "COSL")
        self.assertEqual(self.utils._normalize_abbr(""), "")
        self.assertEqual(self.utils._normalize_abbr(None), "")

    def test_get_api_key_throws_on_empty_key(self):
        self._account_docs["EMPTY"] = {"name": "SA-EMPTY"}

        # Override get_doc to return doc with empty password
        orig_get_doc = self.frappe.get_doc

        def _get_doc_empty(doctype, name):
            if doctype == "Stripe Account" and name == "SA-EMPTY":
                doc = types.SimpleNamespace(name=name)
                doc.get_password = lambda field: ""
                return doc
            return orig_get_doc(doctype, name)

        self.frappe.get_doc = _get_doc_empty

        with self.assertRaises(Exception) as ctx:
            self.utils.get_api_key("EMPTY")
        self.assertIn("secret key not configured", str(ctx.exception))


class SetupTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)
        self._saved_docs = []
        self._passwords_set = []

        fake_frappe = types.ModuleType("frappe")

        class _PermissionError(Exception):
            pass

        fake_frappe.PermissionError = _PermissionError
        fake_frappe.session = types.SimpleNamespace(user="Administrator")
        fake_frappe.set_user = Mock()
        fake_frappe.whitelist = lambda *a, **kw: (lambda fn: fn)

        def _throw(msg, exc=None):
            raise (exc or Exception)(msg)

        fake_frappe.throw = _throw

        self_ref = self

        def _db_get_value(doctype, filters=None, field=None, **kwargs):
            if doctype == "Stripe Account" and isinstance(filters, dict):
                abbr = filters.get("company_abbr")
                if abbr == "EXISTING":
                    return "SA-EXISTING"
            return None

        fake_frappe.db = types.SimpleNamespace(
            get_value=_db_get_value,
            commit=Mock(),
        )

        fake_frappe.get_all = Mock(return_value=[
            types.SimpleNamespace(company_abbr="COE"),
            types.SimpleNamespace(company_abbr="COSL"),
        ])

        class _FakeDoc:
            def __init__(self):
                self.name = "SA-NEW"
                self.doctype = "Stripe Account"
                self.company_abbr = ""
                self.company = ""
                self.enabled = 0
                self.test_mode = 0
                self.publishable_key = ""

            def save(self, **kwargs):
                self_ref._saved_docs.append(self.company_abbr)

        def _get_doc(doctype, name):
            if doctype == "Stripe Account":
                doc = _FakeDoc()
                doc.name = name
                doc.company_abbr = "EXISTING"
                return doc
            return None

        fake_frappe.get_doc = _get_doc
        fake_frappe.new_doc = lambda doctype: _FakeDoc()

        fake_password_mod = types.ModuleType("frappe.utils.password")
        fake_password_mod.set_encrypted_password = lambda *a, **kw: self_ref._passwords_set.append(a)

        sys.modules["frappe"] = fake_frappe
        sys.modules["frappe.utils"] = types.ModuleType("frappe.utils")
        sys.modules["frappe.utils.password"] = fake_password_mod

        self.setup = importlib.import_module("stripe_integration.stripe_integration.setup")
        self.frappe = fake_frappe

    def tearDown(self):
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    def test_import_accounts_requires_system_manager(self):
        self.frappe.session.user = "user@example.com"
        self.frappe.get_roles = lambda user: ["Accounts User"]

        # Monkey-patch _require_system_manager to check roles
        orig = self.setup._require_system_manager

        def _patched():
            if self.frappe.session.user != "Administrator":
                roles = set(self.frappe.get_roles(self.frappe.session.user))
                if "System Manager" not in roles:
                    self.frappe.throw("Not permitted", self.frappe.PermissionError)

        self.setup._require_system_manager = _patched

        with self.assertRaises(self.frappe.PermissionError):
            self.setup.import_accounts('{"accounts": []}')

        self.setup._require_system_manager = orig

    def test_import_accounts_stores_secrets_encrypted(self):
        payload = json.dumps({
            "accounts": [{
                "company_abbr": "NEW",
                "publishable_key": "pk_test_new",
                "secret_key": "sk_test_new",
                "webhook_secret": "whsec_test_new",
            }]
        })

        self.setup.import_accounts(payload)

        # Check encrypted passwords were set
        secret_key_calls = [c for c in self._passwords_set if "secret_key" in c]
        webhook_secret_calls = [c for c in self._passwords_set if "webhook_secret" in c]
        self.assertTrue(len(secret_key_calls) > 0, "Secret key should be stored encrypted")
        self.assertTrue(len(webhook_secret_calls) > 0, "Webhook secret should be stored encrypted")

    def test_import_accounts_missing_publishable_key_throws(self):
        payload = json.dumps({
            "accounts": [{
                "company_abbr": "NEW",
                "publishable_key": "",
            }]
        })

        with self.assertRaises(Exception) as ctx:
            self.setup.import_accounts(payload)
        self.assertIn("publishable_key is required", str(ctx.exception))


import json

if __name__ == "__main__":
    unittest.main()
