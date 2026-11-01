"""Pure checks for the RTP oracle; no sockets or external services."""

import math
import unittest

from rtp_check import TONE, carries_test_tone, encode_ulaw, media_address


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


if __name__ == "__main__":
    unittest.main()
