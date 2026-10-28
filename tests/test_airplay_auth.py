"""Exercise installed authentication libraries without a receiver or real keys."""
import unittest


class AirPlayAuthenticationTests(unittest.TestCase):
    def test_legacy_airplay_session_can_initialize_srp(self):
        from pyatv.auth.hap_pairing import HapCredentials
        from pyatv.protocols.airplay.srp import LegacySRPAuthHandler
        credentials = HapCredentials(b'', bytes(range(32)), b'', b'offline-test')
        handler = LegacySRPAuthHandler(credentials)
        handler.initialize()
        handler.step1('offline-user', '1234')
        self.assertTrue(handler.session.public)

    def test_hap_session_completes_offline_srp_key_exchange(self):
        import hashlib
        from pyatv.auth.hap_srp import SRPAuthHandler
        from srptools import SRPContext, SRPServerSession, constants
        handler = SRPAuthHandler()
        handler.initialize()
        handler.step1('1234')
        context = SRPContext('Pair-Setup', '1234', prime=constants.PRIME_3072,
                             generator=constants.PRIME_3072_GEN, hash_func=hashlib.sha512)
        _, verifier, salt = context.get_user_data_triplet()
        server = SRPServerSession(context, verifier)
        client = handler._session
        client.process(server.public, salt)
        server.process(client.public, salt)
        self.assertEqual(client.key, server.key)
        self.assertTrue(server.verify_proof(client.key_proof))
        self.assertTrue(client.verify_proof(server.key_proof_hash))
