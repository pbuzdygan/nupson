import unittest
from unittest.mock import patch

from nupson.wol import magic_packet, normalize_mac, ping_online, wake_and_check


class WolTests(unittest.TestCase):
    def test_magic_packet(self):
        packet = magic_packet("AA:BB:CC:DD:EE:FF")
        self.assertEqual(len(packet), 102)
        self.assertEqual(packet[:6], b"\xff" * 6)
        self.assertEqual(packet[6:12], bytes.fromhex("AABBCCDDEEFF"))

    def test_normalize_mac(self):
        self.assertEqual(normalize_mac("aa-bb-cc-dd-ee-ff"), "AA:BB:CC:DD:EE:FF")
        with self.assertRaises(ValueError):
            normalize_mac("not-a-mac")

    def test_does_not_send_wol_when_host_is_already_online(self):
        host = {
            "mac": "AA:BB:CC:DD:EE:FF",
            "address": "192.0.2.10",
        }
        with (
            patch("nupson.wol.ping_online", return_value=True),
            patch("nupson.wol.send_magic_packet") as send,
        ):
            status, attempts = wake_and_check(host)
        self.assertEqual((status, attempts), ("online", 0))
        send.assert_not_called()

    def test_confirms_host_during_final_verification_window(self):
        host = {
            "mac": "AA:BB:CC:DD:EE:FF",
            "address": "192.0.2.10",
        }
        with (
            patch("nupson.wol.ping_online", side_effect=[False, False, True]),
            patch("nupson.wol.send_magic_packet") as send,
        ):
            status, attempts = wake_and_check(
                host, attempts=1, verification_seconds=10, sleeper=lambda _: None
            )
        self.assertEqual((status, attempts), ("online", 1))
        send.assert_called_once()

    def test_reports_unreachable_after_verification_deadline(self):
        host = {
            "mac": "AA:BB:CC:DD:EE:FF",
            "address": "192.0.2.10",
        }
        with (
            patch("nupson.wol.ping_online", return_value=False),
            patch("nupson.wol.send_magic_packet") as send,
        ):
            status, attempts = wake_and_check(
                host,
                attempts=2,
                interval=2,
                verification_seconds=6,
                sleeper=lambda _: None,
            )
        self.assertEqual((status, attempts), ("unreachable", 2))
        self.assertEqual(send.call_count, 2)

    def test_marks_result_unverified_without_address(self):
        host = {"mac": "AA:BB:CC:DD:EE:FF"}
        with patch("nupson.wol.send_magic_packet"):
            status, attempts = wake_and_check(
                host, attempts=1, verification_seconds=5, sleeper=lambda _: None
            )
        self.assertEqual((status, attempts), ("sent_unverified", 1))

    def test_ping_uses_one_icmp_request_without_a_shell(self):
        with patch("nupson.wol.subprocess.run") as run:
            run.return_value.returncode = 0
            self.assertTrue(ping_online("server.example.test"))
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["ping", "-n", "-c", "1"])
        self.assertEqual(command[-2:], ["--", "server.example.test"])


if __name__ == "__main__":
    unittest.main()
