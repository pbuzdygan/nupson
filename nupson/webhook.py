from __future__ import annotations

import hashlib
import hmac
import json
import queue
import threading
from contextlib import suppress
from datetime import UTC, datetime
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .db import Database

WEBHOOK_SCENARIOS: dict[str, dict] = {
    "startup": {
        "label": "Uruchomienie NUPSON",
        "group": "System",
        "kind": "startup",
        "level": "info",
        "message": "NUPSON uruchomiony; monitoring UPS jest aktywny",
        "data": {
            "ups_status": "OL CHRG",
            "ups_model": "Przykładowy UPS",
            "battery_charge": 96,
            "runtime_seconds": 2340,
            "load_percent": 16,
            "power_watts": 118,
        },
    },
    "power_outage": {
        "label": "Brak zasilania sieciowego",
        "group": "Zasilanie",
        "kind": "power_state",
        "level": "warning",
        "message": "Utility power lost",
        "data": {
            "to": "outage",
            "battery_charge": 96,
            "runtime_seconds": 2340,
            "load_percent": 16,
            "power_watts": 118,
        },
    },
    "outage_reminder": {
        "label": "Awaria nadal trwa",
        "group": "Zasilanie",
        "kind": "outage_reminder",
        "level": "warning",
        "message": "Awaria zasilania nadal trwa; UPS pracuje na baterii",
        "data": {
            "battery_charge": 72,
            "runtime_seconds": 1800,
            "load_percent": 18,
            "power_watts": 132,
            "outage_duration_seconds": 900,
        },
    },
    "recovery_started": {
        "label": "Zasilanie wróciło — potwierdzanie ciągłości",
        "group": "Zasilanie",
        "kind": "power_state",
        "level": "info",
        "message": "Utility power returned; stabilization started",
        "data": {
            "to": "recovery_pending",
            "battery_charge": 68,
            "runtime_seconds": 2100,
            "stabilization_remaining_seconds": 840,
        },
    },
    "waiting_for_charge": {
        "label": "Zasilanie stabilne — ładowanie baterii",
        "group": "Zasilanie",
        "kind": "power_state",
        "level": "info",
        "message": "Power is stable; waiting for the battery charge threshold",
        "data": {
            "to": "waiting_for_charge",
            "battery_charge": 68,
            "minimum_charge": 80,
            "runtime_seconds": 2100,
        },
    },
    "power_stable": {
        "label": "Zasilanie stabilne — sprawdzanie hostów",
        "group": "Zasilanie",
        "kind": "power_state",
        "level": "info",
        "message": "Power stable; checking hosts",
        "data": {"to": "waking", "battery_charge": 81, "runtime_seconds": 2700},
    },
    "recovery_complete": {
        "label": "Procedura powrotu zakończona",
        "group": "Zasilanie",
        "kind": "power_state",
        "level": "info",
        "message": "Wake sequence completed",
        "data": {"to": "normal", "battery_charge": 86, "runtime_seconds": 3000},
    },
    "communication_lost": {
        "label": "Utrata komunikacji z UPS",
        "group": "Komunikacja",
        "kind": "communication",
        "level": "warning",
        "message": "UPS communication failed: Data stale",
        "data": {},
    },
    "communication_restored": {
        "label": "Komunikacja z UPS przywrócona",
        "group": "Komunikacja",
        "kind": "communication",
        "level": "info",
        "message": "UPS communication restored",
        "data": {},
    },
    "battery_degraded": {
        "label": "Podejrzenie zużycia baterii",
        "group": "Bateria",
        "kind": "battery_health",
        "level": "warning",
        "message": "Bateria wyczerpała się znacznie szybciej niż wskazywał UPS",
        "data": {
            "battery_charge_start": 96,
            "battery_charge_end": 0,
            "reported_runtime_seconds": 2040,
            "observed_runtime_seconds": 240,
            "load_percent": 18,
        },
    },
    "host_already_online": {
        "label": "Host już działał",
        "group": "Wake-on-LAN",
        "kind": "wol_result",
        "level": "info",
        "message": "Host Serwer-01 już odpowiada",
        "data": {"host_name": "Serwer-01", "status": "online", "attempts": 0},
    },
    "host_woken": {
        "label": "Host uruchomiony przez WoL",
        "group": "Wake-on-LAN",
        "kind": "wol_result",
        "level": "info",
        "message": "Host Serwer-01 odpowiada po Wake-on-LAN",
        "data": {"host_name": "Serwer-01", "status": "online", "attempts": 2},
    },
    "host_unverified": {
        "label": "Nie można potwierdzić uruchomienia",
        "group": "Wake-on-LAN",
        "kind": "wol_result",
        "level": "warning",
        "message": "Wysłano Wake-on-LAN; brak konfiguracji weryfikacji",
        "data": {"host_name": "Serwer-01", "status": "sent_unverified", "attempts": 3},
    },
    "host_unreachable": {
        "label": "Host nie odpowiedział",
        "group": "Wake-on-LAN",
        "kind": "wol_result",
        "level": "warning",
        "message": "Host Serwer-01 nie odpowiedział po Wake-on-LAN",
        "data": {"host_name": "Serwer-01", "status": "unreachable", "attempts": 3},
    },
    "host_cancelled": {
        "label": "Budzenie hosta anulowane",
        "group": "Wake-on-LAN",
        "kind": "wol_result",
        "level": "warning",
        "message": "Budzenie hosta Serwer-01 zostało przerwane",
        "data": {"host_name": "Serwer-01", "status": "cancelled", "attempts": 1},
    },
    "manual_wol": {
        "label": "Ręcznie wysłano Wake-on-LAN",
        "group": "Wake-on-LAN",
        "kind": "wol_manual",
        "level": "info",
        "message": "Manual Wake-on-LAN sent to Serwer-01",
        "data": {},
    },
}


def webhook_scenarios() -> list[dict[str, str]]:
    return [
        {"id": identifier, "label": str(item["label"]), "group": str(item["group"])}
        for identifier, item in WEBHOOK_SCENARIOS.items()
    ]


class WebhookNotifier:
    def __init__(self, database: Database, timeout: float = 5):
        self.database = database
        self.timeout = timeout
        self._queue: queue.Queue[dict | None] = queue.Queue(maxsize=100)
        self._worker: threading.Thread | None = None

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._run, name="nupson-webhook", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        if not self._worker:
            return
        with suppress(queue.Full):
            self._queue.put_nowait(None)
        self._worker.join(timeout=self.timeout + 1)

    def notify(self, event: dict) -> None:
        if not self.database.get_setting("webhook_url", ""):
            return
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self.database.add_event(
                "webhook", "Webhook queue is full; notification dropped", "warning"
            )

    def test(self) -> None:
        self._send(
            {
                "id": None,
                "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "level": "info",
                "kind": "webhook_test",
                "message": "NUPSON webhook test",
                "data": {},
            }
        )

    def test_scenario(self, identifier: str) -> None:
        try:
            scenario = WEBHOOK_SCENARIOS[identifier]
        except KeyError as error:
            raise ValueError("Unknown webhook test scenario") from error
        self._send(
            {
                "id": None,
                "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "level": scenario["level"],
                "kind": scenario["kind"],
                "message": scenario["message"],
                "data": scenario["data"],
                "simulation": True,
            }
        )

    def _run(self) -> None:
        while True:
            event = self._queue.get()
            if event is None:
                return
            try:
                self._send(event)
            except Exception as error:
                self.database.add_event("webhook", f"Webhook delivery failed: {error}", "warning")

    def _send(self, event: dict) -> None:
        url = str(self.database.get_setting("webhook_url", "")).strip()
        if not url:
            raise ValueError("Webhook URL is not configured")
        body = json.dumps(_payload_for(url, event), separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "NUPSON/0.1"}
        secret = str(self.database.get_setting("webhook_secret", ""))
        if secret:
            digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            headers["X-NUPSON-Signature"] = f"sha256={digest}"
        request = Request(url, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"HTTP {response.status}")
        except HTTPError as error:
            details = _http_error_details(error)
            suffix = f": {details}" if details else ""
            raise RuntimeError(f"Webhook rejected request (HTTP {error.code}){suffix}") from error


def _payload_for(url: str, event: dict) -> dict:
    hostname = (urlsplit(url).hostname or "").lower()
    title, description, fields = _humanize_event(event)
    if event.get("simulation"):
        title = f"🧪 TEST · {title}"
    is_discord = hostname in {"discord.com", "discordapp.com"} or hostname.endswith(".discord.com")
    if is_discord:
        level = str(event.get("level", "info"))
        color = {"warning": 0xF4C95D, "error": 0xFF7C73}.get(level, 0x76E6A0)
        embed: dict = {
            "title": title,
            "description": description[:4096],
            "color": color,
            "timestamp": event.get("created_at"),
            "footer": {"text": "NUPSON · monitoring zasilania"},
        }
        if fields:
            embed["fields"] = fields
        return {
            "username": "NUPSON",
            "allowed_mentions": {"parse": []},
            "embeds": [embed],
        }
    return {
        "source": "nupson",
        "notification": {
            "title": title,
            "message": description,
            "facts": {str(field["name"]): field["value"] for field in fields},
        },
        "event": event,
    }


def _humanize_event(event: dict) -> tuple[str, str, list[dict[str, object]]]:
    kind = str(event.get("kind", "event"))
    message = str(event.get("message", "Zdarzenie NUPSON"))
    data = event.get("data") or {}
    target = data.get("to")

    if kind == "startup":
        title = "🟢 NUPSON rozpoczął monitoring"
        description = (
            "Usługa została uruchomiona i odbiera bieżące dane z UPS-a. "
            "To komunikat startowy, a nie informacja o powrocie zasilania."
        )
    elif kind == "webhook_test":
        title = "✅ Powiadomienia działają"
        description = "NUPSON może wysyłać tutaj najważniejsze zdarzenia zasilania."
    elif kind == "power_state" and target == "outage":
        title = "🔋 Brak zasilania sieciowego"
        description = (
            "UPS pracuje na baterii. Chronione urządzenia pozostają zasilane, "
            "a NUPSON obserwuje poziom baterii."
        )
    elif kind == "power_state" and target == "recovery_pending":
        title = "⚡ Zasilanie wróciło — trwa potwierdzanie"
        description = (
            "NUPSON obserwuje, czy zasilanie nie zaniknie ponownie. Po potwierdzeniu "
            "ciągłości sprawdzi wymagany poziom baterii przed uruchomieniem hostów."
        )
    elif kind == "power_state" and target == "waiting_for_charge":
        title = "🔋 Zasilanie stabilne — trwa ładowanie baterii"
        description = (
            "Ciągłość zasilania została potwierdzona. NUPSON rozpocznie sprawdzanie "
            "i budzenie hostów po osiągnięciu ustawionego poziomu baterii."
        )
    elif kind == "outage_reminder":
        title = "🔋 Awaria zasilania nadal trwa"
        description = (
            "UPS wciąż pracuje na baterii. Poniżej znajduje się aktualny stan zasilania awaryjnego."
        )
    elif kind == "power_state" and target == "waking":
        title = "✅ Zasilanie jest stabilne"
        description = "NUPSON sprawdza teraz hosty zakwalifikowane do uruchomienia."
    elif kind == "power_state" and target == "normal":
        title = "✅ Procedura powrotu zakończona"
        description = "Zasilanie działa stabilnie, a kolejka uruchamiania hostów jest zakończona."
    elif kind == "communication" and "restored" in message.lower():
        title = "✅ Łączność z UPS przywrócona"
        description = "NUPSON ponownie odbiera bieżące dane z UPS-a."
    elif kind == "communication":
        title = "❌ Utracono łączność z UPS"
        description = "Sprawdź kabel USB oraz stan sterownika NUT. Trwa automatyczna próba naprawy."
    elif kind == "battery_health":
        title = "⚠️ Sprawdź baterię UPS"
        description = (
            "UPS zgłosił problem z baterią albo rzeczywisty czas podtrzymania był "
            "znacznie krótszy od raportowanego. Naładuj baterię i wykonaj test "
            "serwisowy; możliwa jest konieczność jej wymiany."
        )
    elif kind == "wol_result":
        host = str(data.get("host_name", "Host"))
        status = data.get("status")
        try:
            attempts = int(data.get("attempts", 0))
        except (TypeError, ValueError):
            attempts = 0
        if status == "online" and attempts == 0:
            title = "✅ Host już działa"
            description = f"{host} odpowiadał — wysyłanie Wake-on-LAN nie było potrzebne."
        elif status == "online":
            title = "✅ Host został uruchomiony"
            description = f"{host} odpowiedział po {attempts} próbach Wake-on-LAN."
        elif status == "sent_unverified":
            title = "⚠️ Nie można potwierdzić uruchomienia hosta"
            description = (
                f"Wysłano Wake-on-LAN do {host}, ale host nie ma adresu do weryfikacji przez ping."
            )
        elif status == "cancelled":
            title = "⚠️ Uruchamianie hosta anulowane"
            description = f"Próby uruchomienia {host} przerwano z powodu zmiany stanu zasilania."
        else:
            title = "❌ Host nie odpowiedział"
            description = f"{host} nie odpowiedział po {attempts} próbach Wake-on-LAN."
    elif kind in {"wol", "wol_manual"}:
        title = "🖥️ Wysłano Wake-on-LAN"
        description = message.replace("Sending Wake-on-LAN to ", "Uruchamianie hosta ")
    else:
        title = "ℹ️ Zdarzenie NUPSON"
        description = message

    fields: list[dict[str, object]] = []
    _add_field(fields, "Bateria", _percent(data.get("battery_charge")), True)
    _add_field(fields, "Czas pracy", _duration(data.get("runtime_seconds")), True)
    _add_field(fields, "Obciążenie", _percent(data.get("load_percent")), True)
    watts = data.get("power_watts")
    if watts is not None:
        suffix = " (szacunek)" if data.get("power_estimated") else ""
        _add_field(fields, "Pobór mocy", f"{watts} W{suffix}", True)
    remaining = _duration(data.get("stabilization_remaining_seconds"))
    _add_field(fields, "Do potwierdzenia ciągłości zasilania", remaining, False)
    if target in {"recovery_pending", "waiting_for_charge"}:
        _add_field(
            fields,
            "Wymagany poziom baterii",
            _percent(data.get("minimum_charge")),
            True,
        )
    outage_duration = _duration(data.get("outage_duration_seconds"))
    _add_field(fields, "Czas trwania awarii", outage_duration, False)
    _add_field(fields, "Status UPS", data.get("ups_status"), True)
    _add_field(fields, "Model UPS", data.get("ups_model"), True)
    _add_field(
        fields,
        "Runtime raportowany na początku",
        _duration(data.get("reported_runtime_seconds")),
        True,
    )
    _add_field(
        fields,
        "Zaobserwowany czas rozładowania",
        _duration(data.get("observed_runtime_seconds")),
        True,
    )
    start_charge = _percent(data.get("battery_charge_start"))
    end_charge = _percent(data.get("battery_charge_end"))
    if start_charge and end_charge:
        _add_field(fields, "Spadek baterii", f"{start_charge} → {end_charge}", False)
    return title, description, fields


def _add_field(fields: list[dict[str, object]], name: str, value: str | None, inline: bool) -> None:
    if value:
        fields.append({"name": name, "value": value, "inline": inline})


def _percent(value: object) -> str | None:
    if value in {None, ""}:
        return None
    return f"{value}%"


def _duration(value: object) -> str | None:
    try:
        seconds = max(0, int(float(str(value))))
    except (TypeError, ValueError):
        return None
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    return f"{hours} godz. {minutes} min" if hours else f"{minutes} min"


def _http_error_details(error: HTTPError) -> str:
    try:
        content = error.read(2048).decode("utf-8", errors="replace")
        value = json.loads(content)
        if isinstance(value, dict) and value.get("message"):
            return str(value["message"])
        return content.strip()[:500]
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
