import importlib
import sys
import types
import unittest
from unittest.mock import Mock


class _JournalEntry:
    def __init__(self):
        self.name = "JV-TEST-0001"
        self.accounts = []

    def append(self, fieldname, value):
        if fieldname == "accounts":
            self.accounts.append(value)

    def insert(self, **kwargs):
        return self

    def submit(self):
        return self


class PayoutAccountingTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)
        self.journal_entry = _JournalEntry()

        fake_frappe = types.ModuleType("frappe")
        fake_frappe.utils = types.SimpleNamespace(nowdate=lambda: "2026-07-10")
        fake_frappe.new_doc = lambda doctype: self.journal_entry
        fake_frappe.get_doc = lambda *args, **kwargs: types.SimpleNamespace(
            company="COEngine Service Inc.",
            stripe_clearing_account="Stripe Clearing - COE",
            bank_account="Bank - COE",
            stripe_fee_account="Stripe Fees - COE",
        )
        fake_frappe.throw = lambda message: (_ for _ in ()).throw(Exception(message))
        fake_frappe.db = types.SimpleNamespace(
            get_single_value=lambda *args, **kwargs: 1,
            exists=lambda *args, **kwargs: False,
            commit=lambda: None,
        )

        fake_stripe = types.ModuleType("stripe")
        fake_stripe.BalanceTransaction = types.SimpleNamespace(list=lambda **kwargs: None)
        fake_stripe.Payout = types.SimpleNamespace(retrieve=lambda payout_id: None)

        fake_utils = types.ModuleType("stripe_integration.stripe_integration.utils")
        fake_utils.get_api_key = lambda company_abbr: "test-key"
        fake_utils.get_company_abbr_from_company = lambda company: "COE"

        fake_event_log = types.ModuleType("stripe_integration.stripe_integration.event_log")
        fake_event_log.upsert_event = Mock()
        fake_event_log.mark_event_status = Mock()

        sys.modules["frappe"] = fake_frappe
        sys.modules["stripe"] = fake_stripe
        sys.modules["stripe_integration.stripe_integration.utils"] = fake_utils
        sys.modules["stripe_integration.stripe_integration.event_log"] = fake_event_log
        sys.modules.pop("stripe_integration.stripe_integration.payout_sync", None)

        self.module = importlib.import_module("stripe_integration.stripe_integration.payout_sync")

    def tearDown(self):
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    def test_payout_created_does_not_post_a_journal_entry(self):
        self.module._make_journal_entry = Mock(return_value="JV-TEST-0001")

        result = self.module.sync_payout_from_webhook_event(
            {
                "id": "evt_payout_created",
                "type": "payout.created",
                "data": {"object": {"id": "po_test", "status": "pending"}},
            },
            company_abbr_hint="COE",
        )

        self.assertFalse(result["handled"])
        self.assertEqual(result["reason"], "payout_not_paid")
        self.module._make_journal_entry.assert_not_called()

    def test_payout_je_settles_net_without_posting_fees_twice(self):
        self.module._make_journal_entry(
            company="COEngine Service Inc.",
            payout_id="po_test",
            net=97.0,
            accounts={
                "bank": "Bank - COE",
                "fee": "Stripe Fees - COE",
                "clearing": "Stripe Clearing - COE",
            },
        )

        self.assertEqual(
            self.journal_entry.accounts,
            [
                {"account": "Bank - COE", "debit_in_account_currency": 97.0},
                {"account": "Stripe Clearing - COE", "credit_in_account_currency": 97.0},
            ],
        )

    def test_payout_audit_blocks_external_transactions(self):
        self.module.stripe.BalanceTransaction.list = lambda **kwargs: {
            "data": [
                {
                    "id": "txn_external",
                    "type": "adjustment",
                    "source": "src_external",
                    "currency": "cad",
                    "net": 9700,
                }
            ],
            "has_more": False,
        }

        out = self.module._audit_payout_transactions(
            "po_test",
            9700,
            "CAD",
            {
                "company": "COEngine Service Inc.",
                "clearing": "Stripe Clearing - COE",
            },
            "test-key",
        )

        self.assertFalse(out["reconciled"])
        self.assertEqual(out["reason"], "payout_contains_unreconciled_transactions")
        self.assertEqual(out["unmatched"][0]["reason"], "unsupported_or_external_stripe_transaction")

    def test_payout_audit_accepts_a_matched_charge_and_fee(self):
        self.module.stripe.BalanceTransaction.list = lambda **kwargs: {
            "data": [
                {
                    "id": "txn_charge",
                    "type": "charge",
                    "source": "ch_test",
                    "currency": "cad",
                    "amount": 10000,
                    "fee": 300,
                    "net": 9700,
                }
            ],
            "has_more": False,
        }
        self.module.stripe.Charge = types.SimpleNamespace(
            retrieve=lambda charge_id, **kwargs: {"payment_intent": "pi_test"}
        )
        self.module._find_payment_entry_by_pi = Mock(
            return_value={
                "name": "PE-0001",
                "company": "COEngine Service Inc.",
                "paid_to": "Stripe Clearing - COE",
            }
        )
        self.module._fee_je_exists = Mock(return_value=True)

        out = self.module._audit_payout_transactions(
            "po_test",
            9700,
            "CAD",
            {
                "company": "COEngine Service Inc.",
                "clearing": "Stripe Clearing - COE",
            },
            "test-key",
        )

        self.assertTrue(out["reconciled"])
        self.assertEqual(out["transaction_count"], 1)
        self.assertEqual(out["net_cents"], 9700)

    def test_manual_payout_audit_is_flagged_for_review(self):
        def reject_manual_payout(**kwargs):
            raise Exception("Balance transaction history can only be filtered on automatic transfers, not manual")

        self.module.stripe.BalanceTransaction.list = reject_manual_payout

        out = self.module._audit_payout_transactions(
            "po_manual",
            9700,
            "CAD",
            {
                "company": "COEngine Service Inc.",
                "clearing": "Stripe Clearing - COE",
            },
            "test-key",
        )

        self.assertFalse(out["reconciled"])
        self.assertEqual(out["reason"], "manual_payout_requires_review")


if __name__ == "__main__":
    unittest.main()
