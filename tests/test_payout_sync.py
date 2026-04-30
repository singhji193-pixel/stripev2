"""Tests for payout webhook handling (payout_sync.py).

Covers:
- Happy path: Journal Entry creation from payout event
- Correct account lookup by company_abbr filter (not doc name)
- Missing company_abbr returns appropriate error
- Existing JE dedup skips creation
- Payout sync disabled returns not handled
- Missing account mapping throws validation error
- Balance transaction aggregation
- Fallback to payout object amounts when BT listing fails
"""

import importlib
import json
import sys
import types
import unittest
from unittest.mock import Mock, MagicMock


class _Obj(dict):
    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc

    def __setattr__(self, key, value):
        self[key] = value


class PayoutSyncTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)
        self._je_submitted = []
        self._committed = []
        self._payout_sync_enabled = True
        self._je_exists = False

        fake_frappe = types.ModuleType("frappe")

        class _ValidationError(Exception):
            pass

        fake_frappe.ValidationError = _ValidationError
        fake_frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)
        fake_frappe.log_error = lambda *a, **kw: None
        fake_frappe.get_traceback = lambda: ""

        fake_frappe.utils = types.SimpleNamespace(nowdate=lambda: "2026-04-25")

        self_ref = self

        def _throw(msg, exc=None):
            raise (exc or _ValidationError)(msg)

        fake_frappe.throw = _throw

        def _db_get_single_value(doctype, field):
            if field == "enable_payout_sync":
                return 1 if self_ref._payout_sync_enabled else 0
            return 0

        def _db_get_value(doctype, filters=None, field=None, **kwargs):
            if doctype == "Stripe Account" and isinstance(filters, dict):
                abbr = filters.get("company_abbr", "").upper()
                if abbr in ("COE", "COSL"):
                    return f"SA-{abbr}"
            return None

        def _db_exists(doctype, filters=None, **kwargs):
            if doctype == "Journal Entry":
                return self_ref._je_exists
            return False

        def _db_commit():
            self_ref._committed.append(True)

        fake_frappe.db = types.SimpleNamespace(
            get_single_value=_db_get_single_value,
            get_value=_db_get_value,
            exists=_db_exists,
            commit=_db_commit,
            set_value=Mock(),
        )

        # Build a fake Stripe Account doc
        self._stripe_account = _Obj(
            company="COEngine Service Inc.",
            stripe_clearing_account="Stripe Clearing - COE",
            bank_account="Bank - COE",
            stripe_fee_account="Stripe Fees - COE",
        )

        def _get_doc(doctype, name):
            if doctype == "Stripe Account" and name.startswith("SA-"):
                return self._stripe_account
            return None

        fake_frappe.get_doc = _get_doc

        # Fake JE
        self._je_accounts = []

        class _FakeJE:
            def __init__(self):
                self.name = "JE-PAYOUT-001"
                self.voucher_type = None
                self.company = None
                self.posting_date = None
                self.cheque_no = None
                self.cheque_date = None
                self.user_remark = None
                self.accounts = []

            def append(self_, field, data):
                self_._accounts = getattr(self_, "_accounts", [])
                self_.accounts.append(data)
                self_ref._je_accounts.append(data)

            def insert(self_, **kwargs):
                pass

            def submit(self_):
                self_ref._je_submitted.append(True)

        fake_frappe.new_doc = lambda doctype: _FakeJE() if doctype == "Journal Entry" else None

        # Fake stripe module
        fake_stripe = types.ModuleType("stripe")
        fake_stripe.api_key = None

        # Stub balance transactions
        fake_stripe.BalanceTransaction = types.SimpleNamespace(
            list=Mock(return_value={
                "data": [
                    {"amount": 10000, "fee": 300, "net": 9700, "id": "txn_001"},
                    {"amount": 5000, "fee": 150, "net": 4850, "id": "txn_002"},
                ],
                "has_more": False,
            })
        )

        fake_stripe.Payout = types.SimpleNamespace(
            retrieve=Mock(return_value={"amount": 15000, "fee": 450})
        )

        sys.modules["frappe"] = fake_frappe
        sys.modules["frappe.utils"] = types.ModuleType("frappe.utils")
        sys.modules["stripe"] = fake_stripe

        # Stub internal modules
        fake_utils_mod = types.ModuleType("stripe_integration.stripe_integration.utils")
        fake_utils_mod.get_api_key = lambda abbr: "sk_test"

        fake_event_log = types.ModuleType("stripe_integration.stripe_integration.event_log")
        fake_event_log.upsert_event = lambda *a, **kw: None
        fake_event_log.mark_event_status = lambda *a, **kw: None

        sys.modules["stripe_integration.stripe_integration.utils"] = fake_utils_mod
        sys.modules["stripe_integration.stripe_integration.event_log"] = fake_event_log

        self.payout_sync = importlib.import_module("stripe_integration.stripe_integration.payout_sync")
        self.frappe = fake_frappe
        self.stripe = fake_stripe

    def tearDown(self):
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    def _make_event(self, payout_id="po_test_001", company_abbr="COE"):
        return {
            "id": "evt_payout_001",
            "type": "payout.paid",
            "data": {
                "object": {
                    "id": payout_id,
                    "metadata": {"company_abbr": company_abbr},
                }
            },
        }

    def test_happy_path_creates_journal_entry(self):
        event = self._make_event()
        out = self.payout_sync.sync_payout_from_webhook_event(event)

        self.assertTrue(out["handled"])
        self.assertEqual(out["payout_id"], "po_test_001")
        self.assertEqual(out["gross"], 150.0)  # (10000 + 5000) / 100
        self.assertEqual(out["fee"], 4.5)  # abs(300 + 150) / 100
        self.assertEqual(out["net"], 145.5)  # (9700 + 4850) / 100
        self.assertTrue(len(self._je_submitted) > 0)

    def test_disabled_returns_not_handled(self):
        self._payout_sync_enabled = False
        event = self._make_event()
        out = self.payout_sync.sync_payout_from_webhook_event(event)

        self.assertFalse(out["handled"])
        self.assertEqual(out["reason"], "payout_sync_disabled")

    def test_missing_company_abbr_returns_error(self):
        event = self._make_event(company_abbr="")
        out = self.payout_sync.sync_payout_from_webhook_event(event)

        self.assertFalse(out["handled"])
        self.assertEqual(out["reason"], "missing_payout_id_or_company_abbr")

    def test_missing_payout_id_returns_error(self):
        event = self._make_event(payout_id="")
        out = self.payout_sync.sync_payout_from_webhook_event(event)

        self.assertFalse(out["handled"])

    def test_existing_je_dedup(self):
        self._je_exists = True
        event = self._make_event()
        out = self.payout_sync.sync_payout_from_webhook_event(event)

        self.assertFalse(out["handled"])
        self.assertEqual(out["reason"], "already_posted")

    def test_account_lookup_uses_filter_not_doc_name(self):
        """Verify _get_accounts_for_company_abbr uses filter lookup, not direct doc name."""
        event = self._make_event()
        # This should work because our mock returns SA-COE from filter lookup
        out = self.payout_sync.sync_payout_from_webhook_event(event)
        self.assertTrue(out["handled"])

    def test_missing_account_mapping_throws(self):
        self._stripe_account.bank_account = None
        event = self._make_event()

        with self.assertRaises(Exception) as ctx:
            self.payout_sync.sync_payout_from_webhook_event(event)

        self.assertIn("Missing payout mapping", str(ctx.exception))

    def test_je_has_correct_account_structure(self):
        event = self._make_event()
        self.payout_sync.sync_payout_from_webhook_event(event)

        # Should have: bank (debit net), fee (debit fee), clearing (credit gross)
        self.assertEqual(len(self._je_accounts), 3)
        bank_entry = self._je_accounts[0]
        fee_entry = self._je_accounts[1]
        clearing_entry = self._je_accounts[2]

        self.assertEqual(bank_entry["account"], "Bank - COE")
        self.assertAlmostEqual(bank_entry["debit_in_account_currency"], 145.5, places=2)

        self.assertEqual(fee_entry["account"], "Stripe Fees - COE")
        self.assertAlmostEqual(fee_entry["debit_in_account_currency"], 4.5, places=2)

        self.assertEqual(clearing_entry["account"], "Stripe Clearing - COE")
        self.assertAlmostEqual(clearing_entry["credit_in_account_currency"], 150.0, places=2)

    def test_bt_listing_failure_falls_back_to_payout_amounts(self):
        """When balance transaction listing fails, use payout object amounts."""
        self.stripe.BalanceTransaction.list = Mock(side_effect=Exception("API error"))
        event = self._make_event()
        out = self.payout_sync.sync_payout_from_webhook_event(event)

        self.assertTrue(out["handled"])
        # Fallback uses payout.amount=15000, payout.fee=450
        self.assertEqual(out["gross"], 150.0)
        self.assertEqual(out["fee"], 4.5)
        self.assertEqual(out["net"], 145.5)


if __name__ == "__main__":
    unittest.main()
