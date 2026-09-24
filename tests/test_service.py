import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nupson.config import AppConfig
from nupson.db import Database
from nupson.nut import NutError
from nupson.service import (
    NupsonService,
    battery_health,
    battery_runtime_mismatch,
    power_usage,
)
from nupson.state_machine import PowerState


class ServiceTests(unittest.TestCase):
    def test_recovery_pending_restart_does_not_emit_power_return_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            database = Database(path / "nupson.db")
            outage_id = database.start_outage()
            database.update_outage(outage_id, "recovery_pending")
            config = AppConfig(path, "127.0.0.1", 8080, "127.0.0.1", 3493, False, False)
            service = NupsonService(config, database)
            self.assertEqual(service.machine.state, PowerState.RECOVERY_PENDING)
            with patch.object(service, "_add_event") as add_event:
                service._observe(
                    {
                        "ups.status": "OL CHRG",
                        "battery.charge": "90",
                        "battery.runtime": "1800",
                        "ups.load": "20",
                    }
                )
            kinds = [call.args[0] for call in add_event.call_args_list]
            self.assertEqual(kinds, ["startup"])

    def test_waiting_for_charge_restarts_power_confirmation_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            database = Database(path / "nupson.db")
            outage_id = database.start_outage()
            database.update_outage(outage_id, "recovery_pending")
            database.update_outage(outage_id, "waiting_for_charge")
            config = AppConfig(path, "127.0.0.1", 8080, "127.0.0.1", 3493, False, False)
            service = NupsonService(config, database)
            self.assertEqual(service.machine.state, PowerState.RECOVERY_PENDING)
            self.assertEqual(service.machine.remaining_seconds, 900)

    def test_second_power_loss_persists_outage_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            database = Database(path / "nupson.db")
            outage_id = database.start_outage()
            database.update_outage(outage_id, "recovery_pending")
            config = AppConfig(path, "127.0.0.1", 8080, "127.0.0.1", 3493, False, False)
            service = NupsonService(config, database)
            service._startup_notification_pending = False
            service._observe({"ups.status": "OB DISCHRG", "battery.charge": "50"})
            self.assertEqual(database.open_outage()["status"], "outage")

    def test_battery_health_uses_explicit_replace_and_test_signals(self):
        self.assertEqual(battery_health({"ups.status": "OL RB"})["status"], "warning")
        self.assertEqual(
            battery_health({"ups.status": "OL", "ups.test.result": "Done and passed"})["status"],
            "ok",
        )
        self.assertEqual(battery_health({"ups.status": "OL"})["status"], "unknown")

    def test_detects_severe_runtime_mismatch_after_outage(self):
        mismatch = battery_runtime_mismatch(
            {"battery_charge": 96, "runtime_seconds": 2040, "load_percent": 18},
            {"battery_charge": 0},
            240,
        )
        self.assertIsNotNone(mismatch)
        self.assertEqual(mismatch["observed_runtime_seconds"], 240)
        self.assertIsNone(
            battery_runtime_mismatch(
                {"battery_charge": 96, "runtime_seconds": 1200},
                {"battery_charge": 70},
                300,
            )
        )

    def test_uses_reported_real_power_when_available(self):
        self.assertEqual(power_usage({"ups.realpower": "123"}), (123, False))

    def test_estimates_power_from_load_and_nominal_power(self):
        values = {"ups.load": "15", "ups.realpower.nominal": "425"}
        self.assertEqual(power_usage(values), (64, True))

    def test_estimates_power_from_configured_fallback(self):
        self.assertEqual(power_usage({"ups.load": "25"}, 600), (150, True))

    def test_initial_incomplete_telemetry_restarts_driver_before_monitoring(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            service = NupsonService(
                AppConfig(path, "127.0.0.1", 8080, "127.0.0.1", 3493, True, False),
                Database(path / "nupson.db"),
            )
            service._startup_telemetry_guard = True
            incomplete = {"ups.status": "OB", "ups.model": "Test UPS"}
            complete = {"ups.status": "OL", "battery.charge": "100"}
            with (
                patch.object(
                    service,
                    "_wait_for_initial_telemetry",
                    side_effect=[incomplete, complete],
                ),
                patch.object(service.supervisor, "restart") as restart,
            ):
                service._guard_initial_telemetry()
            restart.assert_called_once()
            self.assertFalse(service._startup_telemetry_guard)
            self.assertIsNone(service.connection_error)
            self.assertEqual(service.machine.state, PowerState.NORMAL)

    def test_initial_incomplete_ob_is_not_observed_as_an_outage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            service = NupsonService(
                AppConfig(path, "127.0.0.1", 8080, "127.0.0.1", 3493, True, False),
                Database(path / "nupson.db"),
            )
            service._startup_telemetry_guard = True
            with (
                patch.object(
                    service.nut,
                    "variables",
                    return_value={"ups.status": "OB", "ups.model": "Test UPS"},
                ),
                self.assertRaises(NutError),
            ):
                service._read_ups()
            self.assertEqual(service.machine.state, PowerState.NORMAL)

    def test_webhook_secret_is_write_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = AppConfig(path, "127.0.0.1", 8080, "127.0.0.1", 3493, False, False)
            service = NupsonService(config, Database(path / "nupson.db"))
            settings = service.update_settings(
                {"webhook_url": "https://example.test/hook", "webhook_secret": "secret"}
            )
            self.assertNotIn("webhook_secret", settings)
            self.assertTrue(settings["webhook_secret_configured"])

    def test_history_defaults_to_six_months_and_calculates_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            database = Database(path / "nupson.db")
            config = AppConfig(path, "127.0.0.1", 8080, "127.0.0.1", 3493, False, False)
            service = NupsonService(config, database)
            self.assertEqual(service.settings()["history_retention_days"], 180)
            self.assertEqual(service.settings()["event_retention_days"], 180)
            service.update_settings({"energy_price_per_kwh": 1.25})
            database.add_telemetry({"status": "OL", "power_watts": 100}, 1000)
            database.add_telemetry({"status": "OL", "power_watts": 100}, 1060)
            summary = service.history(1000, 1060)["summary"]
            self.assertAlmostEqual(summary["energy_kwh"], 1 / 600)
            self.assertEqual(summary["energy_cost"], 0.0)

    def test_global_client_policy_invalidates_inherited_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            database = Database(path / "nupson.db")
            config = AppConfig(path, "127.0.0.1", 8080, "127.0.0.1", 3493, False, False)
            service = NupsonService(config, database)
            profile = database.save_client_profile(
                {
                    "name": "server",
                    "address": "192.0.2.10",
                    "server_address": "192.0.2.1",
                    "platform": "debian",
                    "policy": "inherit",
                    "delay_seconds": 600,
                    "final_delay_seconds": 5,
                }
            )
            database.mark_client_config_generated(profile["id"])
            service.update_settings({"client_shutdown_delay_seconds": 1200})
            self.assertFalse(database.client_profiles()[0]["config_current"])

    def test_generator_privilege_migration_invalidates_existing_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            database = Database(path / "nupson.db")
            profile = database.save_client_profile(
                {
                    "name": "server",
                    "address": "192.0.2.10",
                    "server_address": "192.0.2.1",
                    "platform": "debian",
                    "policy": "immediate",
                    "delay_seconds": 600,
                    "final_delay_seconds": 5,
                }
            )
            database.mark_client_config_generated(profile["id"])
            self.assertTrue(database.client_profiles()[0]["config_current"])
            config = AppConfig(path, "127.0.0.1", 8080, "127.0.0.1", 3493, False, False)
            NupsonService(config, database)
            self.assertFalse(database.client_profiles()[0]["config_current"])

    def test_outage_reminder_is_sent_after_configured_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            database = Database(path / "nupson.db")
            config = AppConfig(path, "127.0.0.1", 8080, "127.0.0.1", 3493, False, False)
            service = NupsonService(config, database)
            service.machine.state = PowerState.OUTAGE
            service.ups_data = {"battery.charge": "72", "battery.runtime": "1800"}
            database.start_outage()
            with patch.object(service, "_add_event") as add_event:
                service._maybe_outage_reminder(100)
                service._maybe_outage_reminder(999)
                add_event.assert_not_called()
                service._maybe_outage_reminder(1000)
            args = add_event.call_args.args
            self.assertEqual(args[0], "outage_reminder")
            self.assertEqual(args[2], "warning")
            self.assertEqual(args[3]["runtime_seconds"], "1800")

    def test_manual_hosts_are_included_in_online_probes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            database = Database(path / "nupson.db")
            config = AppConfig(path, "127.0.0.1", 8080, "127.0.0.1", 3493, False, False)
            service = NupsonService(config, database)
            host = database.save_host(
                {
                    "name": "manual-host",
                    "mac": "AA:BB:CC:DD:EE:FF",
                    "address": "192.0.2.10",
                    "broadcast": "192.0.2.255",
                    "policy": "manual",
                }
            )
            with patch("nupson.service.ping_online", return_value=True):
                service._probe_hosts()
            current = next(item for item in database.hosts() if item["id"] == host["id"])
            self.assertTrue(current["last_online"])

    def test_manual_wake_verification_marks_host_online(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            database = Database(path / "nupson.db")
            config = AppConfig(path, "127.0.0.1", 8080, "127.0.0.1", 3493, False, False)
            service = NupsonService(config, database)
            host = database.save_host(
                {
                    "name": "manual-host",
                    "mac": "AA:BB:CC:DD:EE:FF",
                    "address": "192.0.2.10",
                    "broadcast": "192.0.2.255",
                    "policy": "manual",
                }
            )
            with patch("nupson.service.ping_online", return_value=True):
                service._verify_manual_wake(int(host["id"]), host["address"])
            current = next(item for item in database.hosts() if item["id"] == host["id"])
            self.assertTrue(current["last_online"])


if __name__ == "__main__":
    unittest.main()
