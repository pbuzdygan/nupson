import tempfile
import unittest
from pathlib import Path

from nupson.db import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "test.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_settings_roundtrip(self):
        self.db.set_setting("minimum_charge", 50)
        self.assertEqual(self.db.get_setting("minimum_charge"), 50)

    def test_wake_queue_uses_outage_snapshot_policy(self):
        first = self.db.save_host(
            {
                "name": "online",
                "mac": "AA:BB:CC:DD:EE:01",
                "address": "host1",
                "check_port": 22,
                "policy": "was_online",
                "wave": 1,
                "delay_seconds": 0,
            }
        )
        self.db.save_host(
            {
                "name": "offline",
                "mac": "AA:BB:CC:DD:EE:02",
                "address": "host2",
                "check_port": 22,
                "policy": "was_online",
                "wave": 1,
                "delay_seconds": 0,
            }
        )
        self.db.save_host(
            {
                "name": "always",
                "mac": "AA:BB:CC:DD:EE:03",
                "policy": "always",
                "wave": 2,
                "delay_seconds": 0,
            }
        )
        self.db.set_host_online(first["id"], True)
        outage_id = self.db.start_outage()
        self.db.prepare_wake_queue(outage_id)
        self.assertEqual(
            [host["name"] for host in self.db.wake_queue(outage_id)], ["online", "always"]
        )

    def test_open_outage_survives_new_database_instance(self):
        outage_id = self.db.start_outage()
        reopened = Database(Path(self.temp.name) / "test.db")
        self.assertEqual(reopened.open_outage()["id"], outage_id)

    def test_telemetry_is_aggregated_and_energy_is_integrated(self):
        for timestamp, watts in ((1000, 100), (1060, 120), (1120, 140)):
            self.db.add_telemetry(
                {
                    "status": "OL",
                    "battery_charge": 90,
                    "load_percent": 20,
                    "power_watts": watts,
                },
                timestamp,
            )
        history = self.db.telemetry(1000, 1120, max_points=10)
        self.assertEqual(history["summary"]["sample_count"], 3)
        self.assertAlmostEqual(history["summary"]["average_power_watts"], 120)
        self.assertAlmostEqual(history["summary"]["energy_kwh"], 0.004333, places=5)
        self.assertEqual(len(history["points"]), 3)

    def test_client_profile_tracks_connection_and_configuration_version(self):
        profile = self.db.save_client_profile(
            {
                "name": "Proxmox-01",
                "address": "192.168.68.102",
                "server_address": "192.168.68.5",
                "platform": "debian",
                "policy": "inherit",
                "delay_seconds": 600,
                "final_delay_seconds": 5,
            }
        )
        self.assertEqual(self.db.client_profiles()[0]["status"], "waiting")
        self.db.mark_client_config_generated(profile["id"])
        self.assertTrue(self.db.client_profiles()[0]["config_current"])
        unknown = self.db.sync_client_connections(["192.168.68.102", "192.168.68.200"])
        self.assertEqual(unknown, ["192.168.68.200"])
        connected = self.db.client_profiles()[0]
        self.assertEqual(connected["status"], "connected")
        self.assertIsNotNone(connected["last_seen"])
        self.db.invalidate_client_configs(inherited_only=True)
        self.assertFalse(self.db.client_profiles()[0]["config_current"])
        self.db.sync_client_connections([])
        self.assertEqual(self.db.client_profiles()[0]["status"], "disconnected")


if __name__ == "__main__":
    unittest.main()
