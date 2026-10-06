import unittest

from ai_governance_foundation.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        self.assertEqual(1, result["records"])
        self.assertEqual("ready", result["campaign_state_before_start"])
        self.assertEqual("completed", result["campaign_status"])
        self.assertTrue(result["snapshot_frozen"])
        self.assertTrue(result["blocked_before_resolve"])
        self.assertTrue(result["release_approvable"])
        self.assertEqual("approved", result["release_decision"])


if __name__ == "__main__":
    unittest.main()
