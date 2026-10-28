"""Reconnect telemetry must not attribute an old device fault to a new test."""
from datetime import datetime, timedelta, timezone
import tempfile
import unittest

from jarvis.device import Device, parse_diagnostics
from jarvis.server import Bench


def diagnostics(boot='a' * 16, capture=3):
    root = {'sequence':1, 'reason':'pcm_send_failed', 'uptime_ms':34000,
            'connection_generation':1, 'capture_generation':capture}
    return {'schema':1, 'boot_id':boot, 'uptime_ms':40000,
            'connection_generation':2, 'capture_generation':4, 'fault_sequence':2,
            'fault_counts':{'pcm_send_failed':1, 'websocket_disconnected':1},
            'root_fault':root, 'last_fault':{**root, 'sequence':2, 'reason':'websocket_disconnected'}}


class FirmwareDiagnosticsTests(unittest.TestCase):
    def test_parser_keeps_only_bounded_known_fields(self):
        value = diagnostics()
        value['token'] = 'do-not-retain'
        value['root_fault']['url'] = 'do-not-retain'
        parsed = parse_diagnostics(value)
        self.assertNotIn('token', parsed)
        self.assertNotIn('url', parsed['root_fault'])
        for bad in ({**value, 'schema':2}, {**value, 'boot_id':'invalid'},
                    {**value, 'uptime_ms':-1}, {**value, 'fault_counts':{'x':True}}):
            self.assertIsNone(parse_diagnostics(bad))

    def test_capture_acknowledgment_records_exact_generations(self):
        device = Device()
        device.capture_id = 'capture'
        device.capture_session = {'boot_id':'a' * 16, 'connection_generation':1}
        device.receive_json({'type':'capture_started', 'capture_id':'capture',
                             'connection_generation':1, 'capture_generation':3})
        device.receive_json({'type':'status', 'diagnostics':diagnostics()})
        self.assertEqual(device.capture_session['capture_generation'], 3)
        self.assertEqual(device.capture_session['connection_generation'], 1)
        self.assertEqual(device.info['diagnostics']['connection_generation'], 2)

    def observe(self, value, age=1):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        bench = Bench(folder.name)
        bench.results = [{'started_at':(datetime.now(timezone.utc) - timedelta(seconds=34 + age)).isoformat(),
                          'elapsed_seconds':34, 'interruption':{'reason':'device_disconnected'},
                          'device_session':{'boot_id':'a' * 16, 'connection_generation':1, 'capture_generation':3}}]
        bench.device.info['diagnostics'] = parse_diagnostics(value)
        bench.observe_firmware()
        return bench

    def test_reconnect_links_root_only_to_same_boot_connection_and_capture(self):
        bench = self.observe(diagnostics())
        followup = bench.results[0]['firmware_followup']
        self.assertEqual(followup['linked_root_fault']['reason'], 'pcm_send_failed')
        self.assertTrue(followup['same_boot'])
        bench.observe_firmware()
        self.assertEqual(len(bench.firmware_observations), 1)

    def test_unrelated_capture_fault_is_not_claimed_as_cause(self):
        bench = self.observe(diagnostics(capture=2))
        self.assertIsNone(bench.results[0]['firmware_followup']['linked_root_fault'])

    def test_early_reconnect_observation_can_link_when_failure_finishes(self):
        bench = self.observe(diagnostics())
        result = bench.results.pop()
        result.pop('firmware_followup')
        bench.observe_firmware()
        bench.results.append(result)
        bench.observe_firmware()
        self.assertIsNotNone(result['firmware_followup']['linked_root_fault'])
        self.assertEqual(len(bench.firmware_observations), 1)

    def test_new_boot_is_reported_without_inventing_a_fault_cause(self):
        bench = self.observe(diagnostics(boot='b' * 16))
        followup = bench.results[0]['firmware_followup']
        self.assertFalse(followup['same_boot'])
        self.assertIsNone(followup['linked_root_fault'])

    def test_stale_failure_does_not_receive_new_reconnect_evidence(self):
        bench = self.observe(diagnostics(), age=121)
        self.assertNotIn('firmware_followup', bench.results[0])

    def test_observation_history_is_bounded(self):
        bench = self.observe(diagnostics())
        for sequence in range(3, 30):
            value = diagnostics()
            value['fault_sequence'] = sequence
            bench.device.info['diagnostics'] = value
            bench.observe_firmware()
        self.assertEqual(len(bench.firmware_observations), 20)
