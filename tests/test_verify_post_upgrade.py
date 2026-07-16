import importlib
import sys
import types
import unittest
from unittest.mock import Mock

from module_isolation import restore_modules


class VerifyPostUpgradeTests(unittest.TestCase):
    def setUp(self):
        self._orig_modules = dict(sys.modules)
        self.pause_fields = (
            "stripe_erpnext_pause_active",
            "stripe_pause_start",
            "stripe_resume_on",
            "stripe_pause_state",
            "stripe_pause_operation_id",
            "stripe_pending_resume_on",
            "stripe_pause_cycles",
            "stripe_pause_cadence_snapshot",
            "stripe_pause_start_at",
            "stripe_resume_at",
            "stripe_pending_resume_at",
            "stripe_resume_cancel_before_start",
            "stripe_operation_attempt",
        )

        self.meta = types.SimpleNamespace(get_field=Mock(return_value=object()))
        fake_frappe = types.ModuleType("frappe")
        fake_frappe.get_meta = Mock(return_value=self.meta)
        fake_frappe.db = types.SimpleNamespace(
            exists=Mock(return_value=True),
            get_value=Mock(return_value="{{ stripe_setup_checkout_url }}"),
        )

        fake_pause = types.ModuleType("stripe_integration.stripe_integration.subscription_pause")
        fake_pause.COORDINATED_PAUSE_FIELDS = self.pause_fields

        sys.modules["frappe"] = fake_frappe
        sys.modules["stripe_integration.stripe_integration.subscription_pause"] = fake_pause
        sys.modules.pop("stripe_integration.stripe_integration.verify_post_upgrade", None)
        self.module = importlib.import_module(
            "stripe_integration.stripe_integration.verify_post_upgrade"
        )

    def tearDown(self):
        restore_modules(self._orig_modules)

    def test_requires_every_coordinated_pause_field(self):
        self.assertTrue(set(self.pause_fields).issubset(self.module.REQUIRED_SUB_FIELDS))

        result = self.module.run()

        self.assertTrue(result["ok"])
        checked_fields = {call.args[0] for call in self.meta.get_field.call_args_list}
        self.assertTrue(set(self.pause_fields).issubset(checked_fields))

    def test_missing_pause_field_fails_post_upgrade_verification(self):
        self.meta.get_field.side_effect = lambda fieldname: (
            None if fieldname == "stripe_resume_at" else object()
        )

        result = self.module.run()

        self.assertFalse(result["ok"])
        self.assertEqual(result["missing_subscription_fields"], ["stripe_resume_at"])


if __name__ == "__main__":
    unittest.main()
