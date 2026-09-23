from __future__ import annotations

import threading
import time
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from .config import AppConfig
from .db import Database
from .nut import NutClient, NutError, NutSupervisor
from .state_machine import PowerState, RecoveryMachine, Transition
from .webhook import WebhookNotifier, webhook_scenarios
from .wol import ping_online, wake_and_check

DEFAULTS: dict[str, Any] = {
    "stabilization_seconds": 900,
    "minimum_charge": 40,
    "poll_seconds": 3,
    "outage_reminder_minutes": 15,
    "wake_attempts": 3,
    "wake_interval_seconds": 30,
    "wake_verification_seconds": 120,
    "wave_delay_seconds": 60,
    "event_retention_days": 180,
    "history_retention_days": 180,
    "energy_price_per_kwh": 0.0,
    "nominal_power_watts": 0,
    "client_shutdown_policy": "critical",
    "client_shutdown_delay_seconds": 600,
    "client_final_delay_seconds": 5,
    "ups_name": "ups",
    "nut_client_username": "upsmon",
    "webhook_url": "",
}

NUT_RESTART_SECONDS = 10
TELEMETRY_INTERVAL_SECONDS = 60
INITIAL_TELEMETRY_TIMEOUT_SECONDS = 5
INCOMPLETE_TELEMETRY_ERROR = (
    "Sterownik UPS udostępnił niepełną telemetrię. Trwa ponowna inicjalizacja USB."
)
OPERATIONAL_TELEMETRY_FIELDS = {
    "battery.charge",
    "battery.runtime",
    "input.voltage",
    "output.voltage",
    "ups.load",
    "ups.realpower",
}


def battery_health(values: dict[str, Any]) -> dict[str, Any]:
    status_tokens = set(str(values.get("ups.status", "")).upper().split())
    test_result = str(values.get("ups.test.result", "")).strip()
    reasons: list[str] = []
    if "RB" in status_tokens:
        reasons.append("UPS zgłasza konieczność wymiany baterii (RB)")
    try:
        if float(values.get("battery.packs.bad", 0)) > 0:
            reasons.append("UPS zgłasza uszkodzony pakiet baterii")
    except (TypeError, ValueError):
        pass
    lowered = test_result.lower()
    if lowered and any(word in lowered for word in ("bad", "fail", "replace", "weak")):
        reasons.append(f"Wynik testu baterii: {test_result}")
    if reasons:
        return {"status": "warning", "summary": "; ".join(reasons), "test_result": test_result}
    if lowered and any(word in lowered for word in ("pass", "good", "ok")):
        return {
            "status": "ok",
            "summary": (
                "Ostatni test UPS nie zgłosił błędu baterii; prosty autotest nie "
                "potwierdza jednak jej rzeczywistej pojemności"
            ),
            "test_result": test_result,
        }
    return {
        "status": "unknown",
        "summary": "UPS nie udostępnia bezpośredniej oceny kondycji baterii",
        "test_result": test_result,
    }


def battery_runtime_mismatch(
    start: dict[str, Any], end: dict[str, Any], elapsed_seconds: float
) -> dict[str, Any] | None:
    try:
        start_charge = float(start["battery_charge"])
        end_charge = float(end["battery_charge"])
        reported_runtime = float(start["runtime_seconds"])
    except (KeyError, TypeError, ValueError):
        return None
    if (
        start_charge < 80
        or end_charge > 10
        or start_charge - end_charge < 70
        or reported_runtime < 600
        or elapsed_seconds < 60
        or elapsed_seconds >= reported_runtime * 0.35
    ):
        return None
    return {
        "battery_charge_start": round(start_charge),
        "battery_charge_end": round(end_charge),
        "reported_runtime_seconds": round(reported_runtime),
        "observed_runtime_seconds": round(elapsed_seconds),
        "load_percent": start.get("load_percent"),
    }


def power_usage(
    values: dict[str, Any], fallback_nominal_watts: int | float | None = None
) -> tuple[int | None, bool]:
    try:
        if values.get("ups.realpower") not in {None, ""}:
            return round(float(values["ups.realpower"])), False
        load = float(values["ups.load"])
        nominal = float(values.get("ups.realpower.nominal") or fallback_nominal_watts)
        return round(nominal * load / 100), True
    except (KeyError, TypeError, ValueError):
        return None, False


class NupsonService:
    def __init__(self, config: AppConfig, database: Database):
        self.config = config
        self.database = database
        self.nut = NutClient(config.nut_host, config.nut_port)
        self.supervisor = NutSupervisor(config.nut_dir, config.manage_nut)
        self.webhook = WebhookNotifier(database)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._changed = threading.Condition()
        self._revision = 0
        self._worker: threading.Thread | None = None
        self._wake_worker: threading.Thread | None = None
        self._manual_wake_workers: set[int] = set()
        self._last_host_probe = 0.0
        self._last_nut_restart = 0.0
        self._last_telemetry_sample = 0.0
        self._last_telemetry_prune = 0.0
        self._outage_reminder_started = 0.0
        self._battery_warning_active = False
        self._outage_battery_sample: dict[str, Any] | None = None
        self._startup_notification_pending = True
        self._startup_telemetry_guard = False
        self.ups_data: dict[str, Any] = {}
        self.clients: list[str] = []
        self.connection_error: str | None = None
        self.last_update: str | None = None

        self._migrate_setting_defaults()
        settings = self.settings()
        open_outage = database.open_outage()
        initial = PowerState.NORMAL
        if open_outage:
            stored_state = open_outage.get("status")
            # After downtime NUPSON cannot prove that utility power was continuous,
            # so both recovery phases restart with the full confirmation interval.
            initial = (
                PowerState.RECOVERY_PENDING
                if stored_state in {"recovery_pending", "waiting_for_charge"}
                else PowerState.OUTAGE
            )
        self.machine = RecoveryMachine(
            settings["stabilization_seconds"],
            settings["minimum_charge"],
            initial,
            int(open_outage["id"]) if open_outage else None,
        )

    def settings(self) -> dict[str, Any]:
        persisted = self.database.settings()
        result = {key: persisted.get(key, default) for key, default in DEFAULTS.items()}
        result["webhook_secret_configured"] = bool(persisted.get("webhook_secret"))
        result["ups_config"] = persisted.get("ups_config")
        return result

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        previous = self.settings()
        validators = {
            "stabilization_seconds": (1, 86400),
            "minimum_charge": (0, 100),
            "poll_seconds": (1, 60),
            "outage_reminder_minutes": (1, 1440),
            "wake_attempts": (1, 10),
            "wake_interval_seconds": (1, 600),
            "wake_verification_seconds": (5, 1800),
            "wave_delay_seconds": (0, 3600),
            "event_retention_days": (1, 3650),
            "history_retention_days": (7, 3650),
            "nominal_power_watts": (0, 100000),
            "client_shutdown_delay_seconds": (1, 86400),
            "client_final_delay_seconds": (0, 300),
        }
        for key, value in values.items():
            if key in validators:
                number = int(value)
                low, high = validators[key]
                if not low <= number <= high:
                    raise ValueError(f"{key} must be between {low} and {high}")
                self.database.set_setting(key, number)
            elif key == "ups_name":
                name = str(value).strip()
                if not name or len(name) > 48:
                    raise ValueError("Invalid UPS name")
                self.database.set_setting(key, name)
            elif key == "client_shutdown_policy":
                policy = str(value)
                if policy not in {"critical", "timer", "immediate", "monitor_only"}:
                    raise ValueError("Invalid client shutdown policy")
                self.database.set_setting(key, policy)
            elif key == "energy_price_per_kwh":
                price = float(value)
                if not 0 <= price <= 10000:
                    raise ValueError("energy_price_per_kwh must be between 0 and 10000")
                self.database.set_setting(key, round(price, 4))
            elif key == "webhook_url":
                url = str(value).strip()
                if url:
                    parsed = urlsplit(url)
                    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                        raise ValueError("Webhook URL must use http:// or https://")
                    if parsed.username or parsed.password or len(url) > 2048:
                        raise ValueError("Invalid webhook URL")
                self.database.set_setting(key, url)
            elif key == "webhook_secret":
                secret = str(value)
                if secret:
                    if len(secret) > 256 or any(char in secret for char in "\r\n"):
                        raise ValueError("Invalid webhook secret")
                    self.database.set_setting(key, secret)
        settings = self.settings()
        client_policy_keys = {
            "client_shutdown_policy",
            "client_shutdown_delay_seconds",
            "client_final_delay_seconds",
        }
        if any(key in values and settings[key] != previous[key] for key in client_policy_keys):
            self.database.invalidate_client_configs(inherited_only=True)
        with self._lock:
            self.machine.stabilization_seconds = settings["stabilization_seconds"]
            self.machine.minimum_charge = settings["minimum_charge"]
        self.database.add_event("settings", "Automation settings updated")
        self.notify_changed()
        return settings

    def _migrate_setting_defaults(self) -> None:
        if not self.database.get_setting("settings_defaults_v2", False):
            if self.database.get_setting("event_retention_days") in {None, 30}:
                self.database.set_setting("event_retention_days", 180)
            if self.database.get_setting("history_retention_days") is None:
                self.database.set_setting("history_retention_days", 180)
            self.database.set_setting("settings_defaults_v2", True)
        if not self.database.get_setting("client_config_privilege_v1", False):
            self.database.invalidate_client_configs()
            self.database.set_setting("client_config_privilege_v1", True)

    def start(self) -> None:
        self.webhook.start()
        self.supervisor.start()
        if (
            self.config.manage_nut
            and (self.config.nut_dir / "ups.conf").exists()
            and not self.supervisor.startup_error
        ):
            self._startup_telemetry_guard = True
            self._guard_initial_telemetry()
        self.database.prune_events(self.settings()["event_retention_days"])
        self.database.prune_telemetry(self.settings()["history_retention_days"])
        self._worker = threading.Thread(target=self._run, name="nupson-monitor", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=5)
        self.supervisor.stop()
        self.webhook.stop()

    def notify_changed(self) -> None:
        with self._changed:
            self._revision += 1
            self._changed.notify_all()

    def clear_ups_configuration(self) -> None:
        with self._lock:
            self.ups_data = {}
            self.clients = []
            self.connection_error = None
            self.last_update = None
            self._startup_telemetry_guard = False
        self.notify_changed()

    def wait_for_change(self, revision: int, timeout: float = 15) -> int:
        with self._changed:
            if self._revision == revision:
                self._changed.wait(timeout)
            return self._revision

    @property
    def revision(self) -> int:
        with self._changed:
            return self._revision

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            settings = self.settings()
            wake_host_count = (
                len(self.database.wake_queue(self.machine.outage_id))
                if self.machine.outage_id
                else 0
            )
            watts, estimated = power_usage(self.ups_data, settings["nominal_power_watts"] or None)
            return {
                "power_state": self.machine.state.value,
                "remaining_seconds": self.machine.remaining_seconds,
                "outage_id": self.machine.outage_id,
                "ups": self.ups_data.copy(),
                "clients": self.clients.copy(),
                "connection_error": self.connection_error,
                "last_update": self.last_update,
                "stabilization_seconds": settings["stabilization_seconds"],
                "minimum_charge": settings["minimum_charge"],
                "wake_host_count": wake_host_count,
                "power_usage_watts": watts,
                "power_usage_estimated": estimated,
                "battery_health": battery_health(self.ups_data),
                "configured": self.database.get_setting("ups_config") is not None
                or self.config.demo,
            }

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                values = self._read_ups()
                self._observe(values)
            except Exception as error:  # monitoring must survive individual driver errors
                error_message = self.supervisor.startup_error or str(error)
                with self._lock:
                    disconnected = self.connection_error is None
                    self.connection_error = error_message
                    self.last_update = datetime.now(UTC).isoformat(timespec="seconds")
                self._observe({"ups.status": "COMMLOST"})
                if disconnected:
                    self._add_event(
                        "communication", f"UPS communication failed: {error_message}", "warning"
                    )
                if (
                    self.config.manage_nut
                    and (self.config.nut_dir / "ups.conf").exists()
                    and time.monotonic() - self._last_nut_restart > NUT_RESTART_SECONDS
                ):
                    self._last_nut_restart = time.monotonic()
                    self.supervisor.restart()
            if (
                self.machine.state == PowerState.NORMAL
                and time.monotonic() - self._last_host_probe > 60
            ):
                self._probe_hosts()
            self.notify_changed()
            interval = self.settings()["poll_seconds"]
            self._stop.wait(max(0.1, interval - (time.monotonic() - started)))

    def _read_ups(self) -> dict[str, str]:
        if self.config.demo:
            return self._demo_values()
        name = self.settings()["ups_name"]
        values = self.nut.variables(name)
        if self._startup_telemetry_guard:
            if not self._telemetry_complete(values):
                raise NutError(INCOMPLETE_TELEMETRY_ERROR)
            self._startup_telemetry_guard = False
        clients: list[str] = []
        with suppress(NutError):
            clients = self.nut.clients(name)
        with self._lock:
            restored = self.connection_error is not None
            self.clients = clients
            self.connection_error = None
        self.database.sync_client_connections(clients)
        if restored:
            self._add_event("communication", "UPS communication restored")
        return values

    @staticmethod
    def _telemetry_complete(values: dict[str, str]) -> bool:
        return bool(values.get("ups.status")) and any(
            values.get(key) not in {None, ""} for key in OPERATIONAL_TELEMETRY_FIELDS
        )

    def _wait_for_initial_telemetry(self, timeout: float) -> dict[str, str]:
        deadline = time.monotonic() + timeout
        last_values: dict[str, str] = {}
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                last_values = self.nut.variables(self.settings()["ups_name"])
                if self._telemetry_complete(last_values):
                    return last_values
            except NutError:
                pass
            self._stop.wait(0.5)
        return last_values

    def _guard_initial_telemetry(self) -> None:
        values = self._wait_for_initial_telemetry(INITIAL_TELEMETRY_TIMEOUT_SECONDS)
        if self._telemetry_complete(values):
            self._startup_telemetry_guard = False
            return
        if not values.get("ups.status"):
            return

        with self._lock:
            self.connection_error = INCOMPLETE_TELEMETRY_ERROR
            self.last_update = datetime.now(UTC).isoformat(timespec="seconds")
        self._last_nut_restart = time.monotonic()
        self.supervisor.restart()
        values = self._wait_for_initial_telemetry(INITIAL_TELEMETRY_TIMEOUT_SECONDS)
        if self._telemetry_complete(values):
            with self._lock:
                self.connection_error = None
            self._startup_telemetry_guard = False

    def _demo_values(self) -> dict[str, str]:
        return {
            "ups.status": "OL CHRG",
            "ups.model": "NUPSON Demo UPS",
            "battery.charge": "96",
            "battery.runtime": "2840",
            "ups.load": "31",
            "input.voltage": "230.4",
            "output.voltage": "230.1",
        }

    def _observe(self, values: dict[str, str]) -> None:
        status = values.get("ups.status", "UNKNOWN")
        try:
            charge = float(values["battery.charge"]) if "battery.charge" in values else None
        except ValueError:
            charge = None
        with self._lock:
            self.ups_data = values.copy()
            self.last_update = datetime.now(UTC).isoformat(timespec="seconds")
            transition = self.machine.observe(status, charge)
        if transition:
            self._handle_transition(transition)
        health = battery_health(values)
        if health["status"] == "warning" and not self._battery_warning_active:
            self._battery_warning_active = True
            self._add_event(
                "battery_health",
                "UPS zgłasza problem z baterią",
                "warning",
                health,
            )
        elif health["status"] == "ok":
            self._battery_warning_active = False
        if self._startup_notification_pending:
            self._startup_notification_pending = False
            startup_data = self._notification_context()
            startup_data.pop("stabilization_remaining_seconds", None)
            startup_data.update(
                {
                    "ups_status": status,
                    "ups_model": values.get("device.model") or values.get("ups.model"),
                    "power_state": self.machine.state.value,
                }
            )
            self._add_event(
                "startup",
                "NUPSON uruchomiony; monitoring UPS jest aktywny",
                data=startup_data,
            )
        now = time.monotonic()
        if now - self._last_telemetry_sample >= TELEMETRY_INTERVAL_SECONDS:
            self._last_telemetry_sample = now
            self._record_telemetry(values)
        if now - self._last_telemetry_prune >= 86400:
            self._last_telemetry_prune = now
            self.database.prune_telemetry(self.settings()["history_retention_days"])
        self._maybe_outage_reminder(now)

    def _maybe_outage_reminder(self, now: float) -> None:
        if self.machine.state != PowerState.OUTAGE:
            self._outage_reminder_started = 0.0
            return
        if not self._outage_reminder_started:
            self._outage_reminder_started = now
            return
        interval = self.settings()["outage_reminder_minutes"] * 60
        if now - self._outage_reminder_started < interval:
            return
        self._outage_reminder_started = now
        data = self._notification_context()
        outage = self.database.open_outage()
        if outage:
            try:
                started = datetime.fromisoformat(outage["started_at"])
                data["outage_duration_seconds"] = int((datetime.now(UTC) - started).total_seconds())
            except (TypeError, ValueError):
                pass
        self._add_event(
            "outage_reminder",
            "Awaria zasilania nadal trwa; UPS pracuje na baterii",
            "warning",
            data,
        )

    def _record_telemetry(self, values: dict[str, str]) -> None:
        settings = self.settings()
        watts, estimated = power_usage(values, settings["nominal_power_watts"] or None)

        def number(key: str) -> float | None:
            try:
                return float(values[key]) if values.get(key) not in {None, ""} else None
            except (TypeError, ValueError):
                return None

        sample = {
            "status": values.get("ups.status", ""),
            "battery_charge": number("battery.charge"),
            "runtime_seconds": number("battery.runtime"),
            "load_percent": number("ups.load"),
            "input_voltage": number("input.voltage"),
            "output_voltage": number("output.voltage"),
            "power_watts": watts,
            "power_estimated": estimated,
        }
        if any(value is not None for key, value in sample.items() if key != "status"):
            self.database.add_telemetry(sample)

    def history(self, start: int, end: int) -> dict[str, Any]:
        if end <= start:
            raise ValueError("History end must be later than start")
        if end - start > 3650 * 86400:
            raise ValueError("History range is too large")
        result = self.database.telemetry(start, end)
        energy = result["summary"].get("energy_kwh")
        price = float(self.settings()["energy_price_per_kwh"])
        result["summary"]["energy_cost"] = (
            round(float(energy) * price, 2) if energy is not None else None
        )
        result["summary"]["price_per_kwh"] = price
        result["summary"]["currency"] = "PLN"
        return result

    def _handle_transition(self, transition: Transition) -> None:
        event_data = {
            "from": transition.previous.value,
            "to": transition.current.value,
            **self._notification_context(),
        }
        self._add_event(
            "power_state",
            transition.reason,
            "warning" if transition.current == PowerState.OUTAGE else "info",
            event_data,
        )
        if transition.current == PowerState.OUTAGE:
            self._outage_battery_sample = {
                "started_monotonic": time.monotonic(),
                "battery_charge": event_data.get("battery_charge"),
                "runtime_seconds": event_data.get("runtime_seconds"),
                "load_percent": event_data.get("load_percent"),
            }
            if self.machine.outage_id is None:
                self.machine.outage_id = self.database.start_outage()
                self.database.prepare_wake_queue(self.machine.outage_id)
            else:
                self.database.update_outage(self.machine.outage_id, "outage")
        elif transition.current == PowerState.RECOVERY_PENDING and self.machine.outage_id:
            sample = self._outage_battery_sample
            self._outage_battery_sample = None
            if sample:
                mismatch = battery_runtime_mismatch(
                    sample,
                    event_data,
                    time.monotonic() - float(sample["started_monotonic"]),
                )
                if mismatch:
                    self._add_event(
                        "battery_health",
                        "Bateria wyczerpała się znacznie szybciej niż wskazywał UPS",
                        "warning",
                        mismatch,
                    )
            self.database.update_outage(self.machine.outage_id, "recovery_pending")
            self.supervisor.clear_fsd()
        elif transition.current == PowerState.WAITING_FOR_CHARGE and self.machine.outage_id:
            self.database.update_outage(self.machine.outage_id, "waiting_for_charge")
        elif transition.current == PowerState.WAKING:
            self._start_wake_worker()
        self.notify_changed()

    def _start_wake_worker(self) -> None:
        if self._wake_worker and self._wake_worker.is_alive():
            return
        self._wake_worker = threading.Thread(
            target=self._wake_sequence, name="nupson-wol", daemon=True
        )
        self._wake_worker.start()

    def _wake_sequence(self) -> None:
        outage_id = self.machine.outage_id
        if not outage_id:
            return
        settings = self.settings()
        queue = self.database.wake_queue(outage_id)
        current_wave: int | None = None
        for host in queue:
            if self.machine.state != PowerState.WAKING or self._stop.is_set():
                break
            wave = int(host["wave"])
            if (
                current_wave is not None
                and wave != current_wave
                and self._wait_cancelled(settings["wave_delay_seconds"])
            ):
                break
            current_wave = wave
            if host["status"] in {"online", "sent", "sent_unverified", "unreachable"}:
                continue
            if self._wait_cancelled(int(host["delay_seconds"])):
                break
            self._add_event(
                "wol",
                f"Sending Wake-on-LAN to {host['name']}",
                data={"host_id": host["id"]},
                notify=False,
            )
            status, attempts = wake_and_check(
                host,
                settings["wake_attempts"],
                settings["wake_interval_seconds"],
                settings["wake_verification_seconds"],
                cancelled=lambda: self.machine.state != PowerState.WAKING or self._stop.is_set(),
                sleeper=lambda seconds: self._stop.wait(seconds),
            )
            self.database.update_wake(outage_id, int(host["id"]), status, attempts)
            if host.get("address"):
                self.database.set_host_online(int(host["id"]), status == "online")
            level = "info" if status == "online" else "warning"
            result_messages = {
                "online": f"Host {host['name']} odpowiada po Wake-on-LAN",
                "unreachable": f"Host {host['name']} nie odpowiedział po Wake-on-LAN",
                "sent_unverified": (
                    f"Wysłano Wake-on-LAN do {host['name']}; brak konfiguracji weryfikacji"
                ),
                "cancelled": f"Budzenie hosta {host['name']} zostało przerwane",
            }
            self._add_event(
                "wol_result",
                result_messages.get(status, f"Wynik WoL dla {host['name']}: {status}"),
                level,
                {
                    "host_id": host["id"],
                    "host_name": host["name"],
                    "status": status,
                    "attempts": attempts,
                },
            )
            self.notify_changed()
        if self.machine.state == PowerState.WAKING:
            self.database.update_outage(outage_id, "completed")
            transition = self.machine.complete()
            self._add_event(
                "power_state",
                transition.reason,
                data={
                    "from": transition.previous.value,
                    "to": transition.current.value,
                    **self._notification_context(),
                },
            )
            self.notify_changed()

    def _wait_cancelled(self, seconds: int) -> bool:
        if seconds <= 0:
            return self.machine.state != PowerState.WAKING
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.machine.state != PowerState.WAKING or self._stop.wait(
                min(1, deadline - time.monotonic())
            ):
                return True
        return False

    def _probe_hosts(self) -> None:
        self._last_host_probe = time.monotonic()
        for host in self.database.hosts():
            if not host["enabled"] or host["policy"] == "disabled" or not host["address"]:
                continue
            online = ping_online(host["address"])
            self.database.set_host_online(int(host["id"]), online)

    def manual_wake(self, host_id: int) -> None:
        hosts = {int(host["id"]): host for host in self.database.hosts()}
        if host_id not in hosts:
            raise KeyError(host_id)
        host = hosts[host_id]
        if host["policy"] == "disabled" or not host["enabled"]:
            raise ValueError("Wake-on-LAN is disabled for this host")
        from .wol import send_magic_packet

        send_magic_packet(host["mac"], host["broadcast"])
        self.database.set_host_online(host_id, False)
        self._start_manual_verification(host)
        self._add_event("wol_manual", f"Manual Wake-on-LAN sent to {host['name']}")
        self.notify_changed()

    def _start_manual_verification(self, host: dict[str, Any]) -> None:
        host_id = int(host["id"])
        address = str(host.get("address") or "")
        if not address:
            return
        with self._lock:
            if host_id in self._manual_wake_workers:
                return
            self._manual_wake_workers.add(host_id)
        threading.Thread(
            target=self._verify_manual_wake,
            args=(host_id, address),
            name=f"nupson-manual-wol-{host_id}",
            daemon=True,
        ).start()

    def _verify_manual_wake(self, host_id: int, address: str) -> None:
        try:
            deadline = time.monotonic() + self.settings()["wake_verification_seconds"]
            while not self._stop.is_set() and time.monotonic() < deadline:
                if ping_online(address):
                    self.database.set_host_online(host_id, True)
                    self.notify_changed()
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._stop.wait(min(5, remaining)):
                    break
            self.database.set_host_online(host_id, False)
            self.notify_changed()
        finally:
            with self._lock:
                self._manual_wake_workers.discard(host_id)

    def wait_until_ready(self, ups_name: str, timeout: float = 15) -> tuple[bool, str | None]:
        deadline = time.monotonic() + timeout
        last_error: str | None = None
        while time.monotonic() < deadline:
            try:
                values = self.nut.variables(ups_name)
                if self._telemetry_complete(values):
                    self._observe(values)
                    return True, None
                if values.get("ups.status"):
                    last_error = INCOMPLETE_TELEMETRY_ERROR
            except NutError as error:
                last_error = str(error)
            time.sleep(0.5)
        return False, last_error or "Driver did not publish complete telemetry before timeout"

    def test_webhook(self) -> None:
        self.webhook.test()

    def webhook_test_scenarios(self) -> list[dict[str, str]]:
        return webhook_scenarios()

    def test_webhook_scenario(self, identifier: str) -> None:
        self.webhook.test_scenario(identifier)

    def _notification_context(self) -> dict[str, Any]:
        with self._lock:
            values = self.ups_data.copy()
            remaining = self.machine.remaining_seconds
        settings = self.settings()
        watts, estimated = power_usage(values, settings["nominal_power_watts"] or None)
        context: dict[str, Any] = {
            "battery_charge": values.get("battery.charge"),
            "runtime_seconds": values.get("battery.runtime"),
            "load_percent": values.get("ups.load"),
            "power_watts": watts,
            "power_estimated": estimated,
            "stabilization_remaining_seconds": remaining,
            "minimum_charge": settings["minimum_charge"],
        }
        return {key: value for key, value in context.items() if value is not None}

    def _add_event(
        self,
        kind: str,
        message: str,
        level: str = "info",
        data: Any = None,
        *,
        notify: bool = True,
    ) -> int:
        created_at = datetime.now(UTC).isoformat(timespec="seconds")
        event_id = self.database.add_event(kind, message, level, data)
        if notify:
            self.webhook.notify(
                {
                    "id": event_id,
                    "created_at": created_at,
                    "level": level,
                    "kind": kind,
                    "message": message,
                    "data": data or {},
                }
            )
        return event_id
