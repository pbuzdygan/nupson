import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nupson.db import Database
from nupson.webhook import WebhookNotifier, webhook_scenarios


class Response:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class WebhookTests(unittest.TestCase):
    def test_scenario_catalog_is_complete_and_identifiers_are_unique(self):
        scenarios = webhook_scenarios()
        identifiers = [scenario["id"] for scenario in scenarios]
        self.assertEqual(len(identifiers), 16)
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertIn("power_outage", identifiers)
        self.assertIn("host_unreachable", identifiers)
        self.assertIn("battery_degraded", identifiers)
        self.assertIn("startup", identifiers)

    def test_startup_notification_is_not_described_as_power_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "nupson.db")
            database.set_setting("webhook_url", "https://discord.com/api/webhooks/example/token")
            notifier = WebhookNotifier(database)
            with patch("nupson.webhook.urlopen", return_value=Response()) as open_url:
                notifier.test_scenario("startup")
            embed = json.loads(open_url.call_args.args[0].data)["embeds"][0]
            self.assertIn("rozpoczął monitoring", embed["title"])
            self.assertIn("nie informacja o powrocie zasilania", embed["description"])

    def test_battery_warning_compares_reported_and_observed_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "nupson.db")
            database.set_setting("webhook_url", "https://discord.com/api/webhooks/example/token")
            notifier = WebhookNotifier(database)
            with patch("nupson.webhook.urlopen", return_value=Response()) as open_url:
                notifier.test_scenario("battery_degraded")
            embed = json.loads(open_url.call_args.args[0].data)["embeds"][0]
            self.assertIn("Sprawdź baterię UPS", embed["title"])
            self.assertIn(
                {
                    "name": "Zaobserwowany czas rozładowania",
                    "value": "4 min",
                    "inline": True,
                },
                embed["fields"],
            )

    def test_scenario_test_is_clearly_marked_without_changing_payload_variant(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "nupson.db")
            database.set_setting("webhook_url", "https://discord.com/api/webhooks/example/token")
            notifier = WebhookNotifier(database)
            with patch("nupson.webhook.urlopen", return_value=Response()) as open_url:
                notifier.test_scenario("power_outage")
            payload = json.loads(open_url.call_args.args[0].data)
            embed = payload["embeds"][0]
            self.assertEqual(embed["title"], "🧪 TEST · 🔋 Brak zasilania sieciowego")
            self.assertIn(
                {"name": "Czas pracy", "value": "39 min", "inline": True},
                embed["fields"],
            )

    def test_unknown_scenario_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            notifier = WebhookNotifier(Database(Path(directory) / "nupson.db"))
            with self.assertRaisesRegex(ValueError, "Unknown webhook"):
                notifier.test_scenario("does_not_exist")

    def test_sends_json_with_hmac_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "nupson.db")
            database.set_setting("webhook_url", "https://example.test/hook")
            database.set_setting("webhook_secret", "test-secret")
            notifier = WebhookNotifier(database)
            with patch("nupson.webhook.urlopen", return_value=Response()) as open_url:
                notifier.test()
            request = open_url.call_args.args[0]
            payload = json.loads(request.data)
            self.assertEqual(payload["source"], "nupson")
            self.assertEqual(payload["event"]["kind"], "webhook_test")
            self.assertEqual(payload["notification"]["title"], "✅ Powiadomienia działają")
            self.assertTrue(request.headers["X-nupson-signature"].startswith("sha256="))

    def test_sends_discord_compatible_embed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "nupson.db")
            database.set_setting("webhook_url", "https://discord.com/api/webhooks/example/token")
            notifier = WebhookNotifier(database)
            with patch("nupson.webhook.urlopen", return_value=Response()) as open_url:
                notifier.test()
            payload = json.loads(open_url.call_args.args[0].data)
            self.assertEqual(payload["username"], "NUPSON")
            self.assertEqual(payload["embeds"][0]["title"], "✅ Powiadomienia działają")
            self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_power_event_is_human_readable_and_includes_runtime(self):
        event = {
            "kind": "power_state",
            "level": "warning",
            "message": "UPS is running on battery",
            "created_at": "2026-09-20T00:00:00+00:00",
            "data": {
                "to": "outage",
                "battery_charge": "96",
                "runtime_seconds": "2340",
                "load_percent": "16",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "nupson.db")
            database.set_setting("webhook_url", "https://discord.com/api/webhooks/example/token")
            notifier = WebhookNotifier(database)
            with patch("nupson.webhook.urlopen", return_value=Response()) as open_url:
                notifier._send(event)
            embed = json.loads(open_url.call_args.args[0].data)["embeds"][0]
            self.assertEqual(embed["title"], "🔋 Brak zasilania sieciowego")
            self.assertIn(
                {"name": "Czas pracy", "value": "39 min", "inline": True},
                embed["fields"],
            )

    def test_failed_wol_is_reported_clearly(self):
        event = {
            "kind": "wol_result",
            "level": "warning",
            "message": "Host serwer nie odpowiedział po Wake-on-LAN",
            "created_at": "2026-09-20T00:00:00+00:00",
            "data": {"host_name": "serwer", "status": "unreachable", "attempts": 3},
        }
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "nupson.db")
            database.set_setting("webhook_url", "https://discord.com/api/webhooks/example/token")
            notifier = WebhookNotifier(database)
            with patch("nupson.webhook.urlopen", return_value=Response()) as open_url:
                notifier._send(event)
            embed = json.loads(open_url.call_args.args[0].data)["embeds"][0]
            self.assertEqual(embed["title"], "❌ Host nie odpowiedział")
            self.assertIn("3 próbach", embed["description"])

    def test_waiting_for_charge_explains_the_second_recovery_step(self):
        event = {
            "kind": "power_state",
            "level": "info",
            "message": "Power is stable; waiting for the battery charge threshold",
            "created_at": "2026-09-20T00:15:00+00:00",
            "data": {"to": "waiting_for_charge", "battery_charge": 68, "minimum_charge": 80},
        }
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "nupson.db")
            database.set_setting("webhook_url", "https://discord.com/api/webhooks/example/token")
            notifier = WebhookNotifier(database)
            with patch("nupson.webhook.urlopen", return_value=Response()) as open_url:
                notifier._send(event)
            embed = json.loads(open_url.call_args.args[0].data)["embeds"][0]
            self.assertEqual(embed["title"], "🔋 Zasilanie stabilne — trwa ładowanie baterii")
            self.assertIn(
                {"name": "Wymagany poziom baterii", "value": "80%", "inline": True},
                embed["fields"],
            )

    def test_outage_reminder_contains_current_battery_runtime(self):
        event = {
            "kind": "outage_reminder",
            "level": "warning",
            "message": "Awaria nadal trwa",
            "created_at": "2026-09-20T00:15:00+00:00",
            "data": {
                "battery_charge": 72,
                "runtime_seconds": 1800,
                "outage_duration_seconds": 900,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "nupson.db")
            database.set_setting("webhook_url", "https://discord.com/api/webhooks/example/token")
            notifier = WebhookNotifier(database)
            with patch("nupson.webhook.urlopen", return_value=Response()) as open_url:
                notifier._send(event)
            embed = json.loads(open_url.call_args.args[0].data)["embeds"][0]
            self.assertEqual(embed["title"], "🔋 Awaria zasilania nadal trwa")
            self.assertIn(
                {"name": "Czas pracy", "value": "30 min", "inline": True},
                embed["fields"],
            )


if __name__ == "__main__":
    unittest.main()
