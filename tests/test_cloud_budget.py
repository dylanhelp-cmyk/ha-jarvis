import json
from pathlib import Path
import tempfile
import unittest

from jarvis.cloud_settings import CloudBudgetError, CloudSettings, _backend_allowance


class CloudBudgetTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.settings = CloudSettings(self.folder.name)
        self.settings.set_key("sk-offline-test-" + "x" * 32)

    def test_reservation_is_durable_before_connection_and_blocks_stale_owner(self):
        stale = CloudSettings(self.folder.name)
        attempt = self.settings.reserve_attempt(transport="webrtc")
        saved = json.loads(self.settings.path.read_text())
        self.assertEqual(saved['attempt_count'], 1)
        self.assertEqual(CloudSettings(self.folder.name).public()['session_attempts'], 1)
        self.assertEqual(saved["pending"]["id"], attempt)
        self.assertAlmostEqual(saved["pending"]["reserved_usd"], .05)
        with self.assertRaises(CloudBudgetError):
            stale.reserve_attempt(transport="websocket")
        with self.assertRaises(CloudBudgetError):
            CloudSettings(self.folder.name).reserve_attempt(transport="webrtc")

    def test_cumulative_usage_is_not_added_and_initialization_not_double_counted(self):
        attempt = self.settings.reserve_attempt(transport="webrtc")
        self.settings.observe_usage(attempt, 12)
        self.settings.observe_usage(attempt, 15)
        self.settings.finalize(attempt, 45)
        self.assertAlmostEqual(self.settings.spent_usd, 45 / 1200)
        self.assertIsNone(CloudSettings(self.folder.name).pending)

    def test_webrtc_initialization_floor_and_websocket_actual_seconds(self):
        attempt = self.settings.reserve_attempt(transport="webrtc")
        self.settings.finalize(attempt, 3)
        self.assertAlmostEqual(self.settings.spent_usd, .0125)
        attempt = self.settings.reserve_attempt(transport="websocket")
        self.settings.finalize(attempt, 3)
        self.assertAlmostEqual(self.settings.spent_usd, .015)

    def test_unconfirmed_closure_keeps_reservation_and_key_update_cannot_reset_it(self):
        stale = CloudSettings(self.folder.name)
        attempt = self.settings.reserve_attempt(transport="webrtc")
        self.settings.mark_unconfirmed(attempt)
        stale.set_key("sk-another-offline-test-" + "y" * 32)
        loaded = CloudSettings(self.folder.name)
        self.assertEqual(loaded.pending["status"], "unconfirmed")
        with self.assertRaises(CloudBudgetError):
            loaded.reserve_attempt(transport="webrtc")
        public = loaded.public()
        self.assertTrue(public["paid_retry_blocked"])
        self.assertNotIn(loaded.api_key, json.dumps(public))
        self.assertFalse(public["enabled"])

    def test_budget_exhaustion_invalid_usage_and_foreign_attempt_fail_closed(self):
        self.settings.spent_usd = 4.96
        self.settings._save()
        with self.assertRaises(CloudBudgetError):
            self.settings.reserve_attempt(transport="websocket")
        self.settings.spent_usd = 0
        self.settings._save()
        attempt = self.settings.reserve_attempt(transport="websocket")
        for value in (-1, float("nan"), float("inf"), True, "12"):
            with self.assertRaises(CloudBudgetError):
                self.settings.observe_usage(attempt, value)
        with self.assertRaises(CloudBudgetError):
            self.settings.finalize("different-attempt", 5)

    def test_delegation_reserves_before_start_and_debits_each_response_once(self):
        model = "gpt-6-luna"
        attempt = self.settings.reserve_attempt(transport="websocket", backend_model=model)
        allowance = _backend_allowance(model)
        saved = CloudSettings(self.folder.name)
        self.assertAlmostEqual(saved.pending["reserved_usd"], .05 + 3 * allowance)
        self.settings.observe_backend_response(attempt, "completed")
        usage = {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 200}, "output_tokens": 50}
        self.settings.observe_backend_response(attempt, "completed", usage=usage, model=model)
        self.settings.observe_backend_response(attempt, "completed", usage=usage, model=model)
        self.settings.observe_backend_response(attempt, "missing")
        self.settings.finalize(attempt, 24)
        estimated = 1.1 * (800 * .25 + 200 * .01 + 50 * .5) / 1_000_000
        self.assertAlmostEqual(self.settings.spent_usd, .02 + allowance + estimated)

    def test_delegation_rejects_insufficient_headroom_and_bills_uncertain_usage(self):
        with self.assertRaises(CloudBudgetError):
            self.settings.reserve_attempt(transport="websocket", backend_model="gpt-6-sol")
        attempt = self.settings.reserve_attempt(transport="websocket", backend_model="gpt-6-luna")
        reserved = self.settings.pending["reserved_usd"]
        with self.assertRaises(CloudBudgetError):
            self.settings.observe_backend_response(attempt, "bad", usage={"input_tokens": -1})
        self.settings.mark_backend_unconfirmed(attempt)
        self.settings.finalize(attempt, 60)
        self.assertAlmostEqual(self.settings.spent_usd, reserved)

    def test_legacy_file_and_missing_key(self):
        path = Path(self.folder.name) / "openai-private.json"
        path.write_text(json.dumps({"api_key": "", "spent_usd": 1.25}))
        loaded = CloudSettings(self.folder.name)
        self.assertEqual(loaded.spent_usd, 1.25)
        with self.assertRaises(CloudBudgetError):
            loaded.reserve_attempt(transport="webrtc")
