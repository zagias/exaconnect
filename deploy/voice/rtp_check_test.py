"""Pure checks for the RTP oracle; no sockets or external services."""

import math
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from xml.etree import ElementTree

from rtp_check import (
    TONE,
    carries_test_tone,
    encode_ulaw,
    isolated_vars,
    legacy_gate_open,
    media_address,
    wait_for_call,
)


class MediaOracleTests(unittest.TestCase):
    def test_injected_tone_is_detected_but_silence_and_ringback_are_not(self):
        self.assertTrue(carries_test_tone(TONE))
        self.assertFalse(carries_test_tone(b"\xff" * 160))
        ringback = bytes(
            encode_ulaw(int(10000 * math.sin(2 * math.pi * 440 * n / 8000)))
            for n in range(160)
        )
        self.assertFalse(carries_test_tone(ringback))
        self.assertFalse(carries_test_tone(TONE[:40]))

    def test_media_must_stay_on_the_expected_container_and_published_rtp_range(self):
        sdp = "v=0\r\nc=IN IP4 172.30.1.3\r\nm=audio 16390 RTP/AVP 0\r\n"
        self.assertEqual(media_address(sdp, "172.30.1.3"), ("172.30.1.3", 16390))
        with self.assertRaises(AssertionError):
            media_address(sdp, "172.30.1.4")
        with self.assertRaises(AssertionError):
            media_address(sdp.replace("16390", "9999"), "172.30.1.3")


class OriginateFixtureTests(unittest.TestCase):
    def test_rpc_listening_does_not_imply_the_voice_policy_is_ready(self):
        reply = "{\n item: {\n  name: gate\n  value: open\n  flags: 2\n  expire: NEVER\n }\n}\n"
        self.assertTrue(legacy_gate_open(reply))
        for unsafe in (
            reply.replace("value: open", "value: closed"),
            reply.replace("name: gate", "name: unrelated"),
            reply.replace("value: open", "value: open\n  value: closed"),
            "500: Key name doesn't exist in htable.",
            "open",
        ):
            self.assertFalse(legacy_gate_open(unsafe))

    def test_internal_network_sets_explicit_address_before_production_variables(self):
        source = '<include>\n<X-PRE-PROCESS cmd="set" data="domain=$${local_ip_v4}"/>\n</include>'
        result = isolated_vars(source)
        ElementTree.fromstring(result)
        self.assertLess(
            result.index("local_ip_v4=echo $EXA_VOICE_PUBLIC_IP"),
            result.index("domain=$${local_ip_v4}"),
        )
        self.assertEqual(result.count("domain=$${local_ip_v4}"), 1)
        with self.assertRaises(ValueError):
            isolated_vars("unrecognised variables document")

    def test_originate_rejection_reports_cli_result_instead_of_waiting_for_invite(self):
        endpoint = SimpleNamespace(calls={}, error="", invites=0)
        process = SimpleNamespace(
            returncode=0,
            poll=Mock(return_value=0),
            communicate=Mock(return_value=("-ERR INVALID_GATEWAY", None)),
        )
        with self.assertRaisesRegex(
            AssertionError, "INVALID_GATEWAY.*endpoint received 0 INVITEs"
        ):
            wait_for_call(endpoint, set(), process, "baseline")
        process.communicate.assert_called_once_with(timeout=1)

    def test_invalid_sdp_reports_the_endpoint_error_and_completed_originate(self):
        endpoint = SimpleNamespace(
            calls={},
            error="AssertionError: unexpected production media address: 127.0.0.1:16390",
            invites=1,
        )
        process = SimpleNamespace(
            poll=Mock(return_value=0),
            communicate=Mock(return_value=("-ERR NORMAL_TEMPORARY_FAILURE", None)),
        )
        with self.assertRaisesRegex(
            AssertionError,
            "unexpected production media address.*NORMAL_TEMPORARY_FAILURE",
        ):
            wait_for_call(endpoint, set(), process, "baseline")

    def test_existing_call_does_not_count_as_the_new_origination(self):
        existing = object()
        endpoint = SimpleNamespace(calls={"old": existing}, error="", invites=1)
        process = SimpleNamespace(poll=Mock(return_value=None))
        with self.assertRaisesRegex(AssertionError, "did not succeed within 0s"):
            wait_for_call(endpoint, {"old"}, process, "recovery", timeout=0)
        fresh = object()
        endpoint.calls["fresh"] = fresh
        self.assertIs(wait_for_call(endpoint, {"old"}, process, "recovery"), fresh)


if __name__ == "__main__":
    unittest.main()
