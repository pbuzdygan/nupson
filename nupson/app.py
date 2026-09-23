from __future__ import annotations

import json
import mimetypes
import re
import signal
import sqlite3
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from .auth import AuthManager
from .client_config import render_client_files, validate_client_profile, zip_client_files
from .config import (
    AppConfig,
    public_ups_config,
    read_nut_client_password,
    read_ups_secrets,
    remove_nut_files,
    validate_nut_username,
    validate_ups_config,
    write_nut_files,
)
from .db import Database
from .nut import NutError, scan_usb
from .service import INCOMPLETE_TELEMETRY_ERROR, NupsonService
from .wol import normalize_mac


class Application:
    def __init__(self, config: AppConfig):
        config.data_dir.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.database = Database(config.database_path)
        self.auth = AuthManager(self.database)
        self.service = NupsonService(config, self.database)


class Handler(BaseHTTPRequestHandler):
    server_version = "NUPSON/0.1"
    app: Application

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def do_GET(self) -> None:
        route = urlparse(self.path)
        if route.path == "/healthz":
            return self.json({"status": "ok", "version": __version__})
        if route.path == "/readyz":
            snapshot = self.app.service.snapshot()
            status = (
                HTTPStatus.OK
                if not snapshot["connection_error"]
                else HTTPStatus.SERVICE_UNAVAILABLE
            )
            return self.json({"ready": status == HTTPStatus.OK, **snapshot}, status)
        if route.path == "/api/auth/status":
            return self.json(
                {
                    "setup_required": self.app.auth.setup_required,
                    "authenticated": self.authenticated(),
                }
            )
        if route.path.startswith("/api/") and not self.require_auth():
            return
        if route.path == "/api/status":
            return self.json(self.app.service.snapshot())
        if route.path == "/api/settings":
            return self.json(self.app.service.settings())
        if route.path == "/api/webhook/scenarios":
            return self.json(self.app.service.webhook_test_scenarios())
        if route.path == "/api/history":
            query = parse_qs(route.query)
            try:
                end = int(query.get("to", [int(time.time())])[0])
                start = int(query.get("from", [end - 86400])[0])
                return self.json(self.app.service.history(start, end))
            except (TypeError, ValueError) as error:
                return self.error(str(error), HTTPStatus.BAD_REQUEST)
        if route.path == "/api/events":
            query = parse_qs(route.query)
            return self.json(self.app.database.events(int(query.get("limit", [100])[0])))
        if route.path == "/api/hosts":
            return self.json(self.app.database.hosts())
        if route.path == "/api/client-profiles":
            clients = self.app.service.snapshot()["clients"]
            unrecognized = self.app.database.sync_client_connections(clients)
            return self.json(
                {
                    "profiles": self.app.database.client_profiles(),
                    "unrecognized": unrecognized,
                }
            )
        if match := re.fullmatch(r"/api/client-profiles/(\d+)/config", route.path):
            try:
                profile_id = int(match.group(1))
                generated = self.client_configuration(profile_id)
                self.app.database.mark_client_config_generated(profile_id)
                return self.json(generated)
            except KeyError:
                return self.error("Client profile not found", HTTPStatus.NOT_FOUND)
            except ValueError as error:
                return self.error(str(error), HTTPStatus.BAD_REQUEST)
        if match := re.fullmatch(r"/api/client-profiles/(\d+)/download", route.path):
            try:
                profile_id = int(match.group(1))
                profile = self.app.database.client_profile(profile_id)
                archive = zip_client_files(self.client_configuration(profile_id)["files"])
                self.app.database.mark_client_config_generated(profile_id)
                filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", profile["name"]).strip("-")
                return self.binary(
                    archive,
                    "application/zip",
                    f"nupson-client-{filename or profile_id}.zip",
                )
            except KeyError:
                return self.error("Client profile not found", HTTPStatus.NOT_FOUND)
            except ValueError as error:
                return self.error(str(error), HTTPStatus.BAD_REQUEST)
        if route.path == "/api/ups/raw":
            return self.json(self.app.service.snapshot()["ups"])
        if route.path == "/api/stream":
            return self.event_stream()
        if route.path == "/api/ups/scan":
            try:
                return self.json(scan_usb())
            except NutError as error:
                return self.error(str(error), HTTPStatus.SERVICE_UNAVAILABLE)
        return self.static(route.path)

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if route == "/api/auth/setup":
            if not self.same_origin():
                return
            try:
                data = self.body()
                token = self.app.auth.setup(
                    str(data.get("username", "")), str(data.get("password", ""))
                )
                self.app.database.add_event("auth", "Administrator account created")
                return self.session_response(token)
            except (ValueError, KeyError, sqlite3.IntegrityError) as error:
                return self.error(str(error), HTTPStatus.BAD_REQUEST)
        if route == "/api/auth/login":
            if not self.same_origin():
                return
            try:
                data = self.body()
                token = self.app.auth.login(
                    str(data.get("username", "")), str(data.get("password", ""))
                )
            except ValueError as error:
                return self.error(str(error), HTTPStatus.BAD_REQUEST)
            if not token:
                return self.error("Invalid username or password", HTTPStatus.UNAUTHORIZED)
            return self.session_response(token)
        if not self.require_auth() or not self.same_origin():
            return
        if route == "/api/auth/logout":
            self.app.auth.logout(self.session_token())
            return self.json(
                {},
                headers={
                    "Set-Cookie": "nupson_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"
                },
            )
        if route == "/api/settings":
            try:
                return self.json(self.app.service.update_settings(self.body()))
            except (ValueError, TypeError) as error:
                return self.error(str(error), HTTPStatus.BAD_REQUEST)
        if route == "/api/webhook/test":
            try:
                self.app.service.test_webhook()
                return self.json({"sent": True})
            except (OSError, ValueError, RuntimeError) as error:
                return self.error(str(error), HTTPStatus.BAD_GATEWAY)
        if match := re.fullmatch(r"/api/webhook/test/([a-z_]+)", route):
            try:
                self.app.service.test_webhook_scenario(match.group(1))
                return self.json({"sent": True, "scenario": match.group(1)})
            except ValueError as error:
                status = (
                    HTTPStatus.BAD_REQUEST
                    if "Unknown webhook" in str(error)
                    else HTTPStatus.BAD_GATEWAY
                )
                return self.error(str(error), status)
            except (OSError, RuntimeError) as error:
                return self.error(str(error), HTTPStatus.BAD_GATEWAY)
        if route == "/api/hosts":
            return self.save_host(None)
        if route == "/api/client-profiles":
            return self.save_client_profile(None)
        if match := re.fullmatch(r"/api/client-profiles/(\d+)", route):
            return self.save_client_profile(int(match.group(1)))
        if match := re.fullmatch(r"/api/hosts/(\d+)/wake", route):
            try:
                self.app.service.manual_wake(int(match.group(1)))
                return self.json({"sent": True})
            except KeyError:
                return self.error("Host not found", HTTPStatus.NOT_FOUND)
            except ValueError as error:
                return self.error(str(error), HTTPStatus.BAD_REQUEST)
        if match := re.fullmatch(r"/api/hosts/(\d+)", route):
            return self.save_host(int(match.group(1)))
        if route == "/api/ups/configure":
            try:
                data = self.body()
                client_password = str(data.pop("client_password", ""))
                client_username = validate_nut_username(str(data.pop("client_username", "upsmon")))
                if not client_password:
                    current_username = self.app.database.get_setting(
                        "nut_client_username", "upsmon"
                    )
                    client_password = (
                        read_nut_client_password(self.app.config.nut_dir, str(current_username))
                        or ""
                    )
                if len(client_password) < 6:
                    raise ValueError("NUT client password must contain at least 6 characters")
                ups = validate_ups_config(data, read_ups_secrets(self.app.config.nut_dir))
                public_ups = public_ups_config(ups)

                def write_configuration() -> dict[str, str]:
                    return write_nut_files(
                        self.app.config.nut_dir,
                        ups,
                        client_password,
                        client_username=client_username,
                    )

                self.app.service.supervisor.reconfigure(write_configuration)
                self.app.database.set_setting("ups_config", public_ups)
                self.app.database.set_setting("ups_name", ups["name"])
                self.app.database.set_setting("nut_client_username", client_username)
                self.app.database.invalidate_client_configs()
                readiness_error = self.app.service.supervisor.startup_error
                if readiness_error:
                    ready = False
                else:
                    ready, readiness_error = self.app.service.wait_until_ready(ups["name"])
                level = "info" if ready else "warning"
                message = f"UPS {ups['name']} configured" + (
                    "" if ready else "; waiting for driver"
                )
                self.app.database.add_event("nut_config", message, level)
                return self.json(
                    {
                        "configured": True,
                        "ready": ready,
                        "initializing": readiness_error == INCOMPLETE_TELEMETRY_ERROR,
                        "readiness_error": readiness_error,
                        "ups": public_ups,
                        "client_username": client_username,
                    }
                )
            except (ValueError, OSError) as error:
                return self.error(str(error), HTTPStatus.BAD_REQUEST)
        return self.error("Not found", HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        route = urlparse(self.path).path
        if not self.require_auth() or not self.same_origin():
            return
        if match := re.fullmatch(r"/api/hosts/(\d+)", route):
            self.app.database.delete_host(int(match.group(1)))
            self.app.database.add_event("host", "Wake-on-LAN host removed")
            self.app.service.notify_changed()
            return self.json({}, HTTPStatus.NO_CONTENT)
        if match := re.fullmatch(r"/api/client-profiles/(\d+)", route):
            try:
                profile_id = int(match.group(1))
                self.app.database.delete_client_profile(profile_id)
                self.app.database.add_event("client", "NUT client profile removed")
                self.app.service.notify_changed()
                return self.json({}, HTTPStatus.NO_CONTENT)
            except KeyError:
                return self.error("Client profile not found", HTTPStatus.NOT_FOUND)
        if route == "/api/ups/configuration":
            self.app.service.supervisor.reconfigure(
                lambda: remove_nut_files(self.app.config.nut_dir)
            )
            self.app.database.delete_settings("ups_config", "ups_name", "nut_client_username")
            self.app.database.invalidate_client_configs()
            self.app.service.clear_ups_configuration()
            self.app.database.add_event("nut_config", "UPS configuration removed")
            return self.json({"configured": False})
        return self.error("Not found", HTTPStatus.NOT_FOUND)

    def save_client_profile(self, profile_id: int | None) -> None:
        try:
            profile = validate_client_profile(self.body())
            saved = self.app.database.save_client_profile(profile, profile_id)
            self.app.database.add_event("client", f"NUT client profile {saved['name']} saved")
            self.app.service.notify_changed()
            self.json(saved, HTTPStatus.OK if profile_id else HTTPStatus.CREATED)
        except KeyError:
            self.error("Client profile not found", HTTPStatus.NOT_FOUND)
        except (ValueError, TypeError, sqlite3.IntegrityError) as error:
            self.error(str(error), HTTPStatus.BAD_REQUEST)

    def client_configuration(self, profile_id: int) -> dict[str, object]:
        profile = self.app.database.client_profile(profile_id)
        settings = self.app.service.settings()
        password = read_nut_client_password(
            self.app.config.nut_dir, settings["nut_client_username"]
        )
        if not password:
            raise ValueError("Configure UPS and the NUT client password first")
        files = render_client_files(profile, settings, password)
        return {
            "profile": profile,
            "files": files,
            "warning": (
                "A connected client confirms an active upsmon session, not the exact "
                "configuration file version."
            ),
        }

    def save_host(self, host_id: int | None) -> None:
        try:
            data = self.body()
            data["name"] = str(data.get("name", "")).strip()
            if not data["name"] or len(data["name"]) > 80:
                raise ValueError("Host name is required")
            data["mac"] = normalize_mac(str(data.get("mac", "")))
            if data.get("policy", "was_online") not in {
                "was_online",
                "always",
                "manual",
                "disabled",
            }:
                raise ValueError("Invalid wake policy")
            data["address"] = str(data.get("address", "")).strip()
            if data["address"] and (
                len(data["address"]) > 253
                or not re.fullmatch(r"[A-Za-z0-9_.:%-]+", data["address"])
            ):
                raise ValueError("Invalid host address")
            if data.get("check_port") in {"", None}:
                data["check_port"] = None
            else:
                data["check_port"] = int(data["check_port"])
                if not 1 <= data["check_port"] <= 65535:
                    raise ValueError("Invalid check port")
            if data.get("policy", "was_online") == "was_online" and not data.get("address"):
                raise ValueError("Policy was_online requires an address for ping verification")
            host = self.app.database.save_host(data, host_id)
            self.app.database.add_event("host", f"Wake-on-LAN host {host['name']} saved")
            self.app.service.notify_changed()
            self.json(host, HTTPStatus.OK if host_id else HTTPStatus.CREATED)
        except (ValueError, TypeError, KeyError, sqlite3.IntegrityError) as error:
            self.error(str(error), HTTPStatus.BAD_REQUEST)

    def body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1_000_000:
                raise ValueError("Request is too large")
            value = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(value, dict):
                raise ValueError("JSON object expected")
            return value
        except (json.JSONDecodeError, ValueError) as error:
            raise ValueError("Invalid request body") from error

    def session_token(self) -> str | None:
        cookie = SimpleCookie(self.headers.get("Cookie"))
        return cookie["nupson_session"].value if "nupson_session" in cookie else None

    def authenticated(self) -> bool:
        return self.app.auth.valid(self.session_token())

    def require_auth(self) -> bool:
        if self.authenticated():
            return True
        self.error("Authentication required", HTTPStatus.UNAUTHORIZED)
        return False

    def same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if not origin:
            return True
        if origin not in {f"http://{host}", f"https://{host}"}:
            self.error("Invalid origin", HTTPStatus.FORBIDDEN)
            return False
        return True

    def session_response(self, token: str) -> None:
        session_cookie = f"nupson_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200"
        self.json(
            {"authenticated": True},
            headers={"Set-Cookie": session_cookie},
        )

    def event_stream(self) -> None:
        token = self.session_token()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        revision = -1
        try:
            while self.app.auth.valid(token):
                revision = self.app.service.wait_for_change(revision)
                payload = json.dumps(self.app.service.snapshot(), separators=(",", ":"))
                self.wfile.write(f"event: status\ndata: {payload}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def static(self, path: str) -> None:
        filename = "index.html" if path in {"/", ""} else path.removeprefix("/")
        if ".." in Path(filename).parts:
            return self.error("Not found", HTTPStatus.NOT_FOUND)
        resource = files("nupson.web").joinpath(filename)
        if not resource.is_file():
            if "." in Path(filename).name:
                return self.error("Not found", HTTPStatus.NOT_FOUND)
            resource = files("nupson.web").joinpath("index.html")
        content = resource.read_bytes()
        mime = mimetypes.guess_type(str(resource))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header(
            "Cache-Control", "no-cache" if filename == "index.html" else "public, max-age=3600"
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(content)

    def json(
        self, value: object, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None
    ) -> None:
        content = (
            b""
            if status == HTTPStatus.NO_CONTENT
            else json.dumps(value, separators=(",", ":")).encode()
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        for name, header_value in (headers or {}).items():
            self.send_header(name, header_value)
        self.end_headers()
        if content:
            self.wfile.write(content)

    def binary(self, content: bytes, mime: str, filename: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def error(self, message: str, status: int) -> None:
        self.json({"error": message}, status)


def main() -> None:
    config = AppConfig.from_env()
    app = Application(config)
    Handler.app = app
    server = ThreadingHTTPServer((config.http_host, config.http_port), Handler)
    stop_once = threading.Event()

    def shutdown(_signum: int, _frame: object) -> None:
        if not stop_once.is_set():
            stop_once.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    app.service.start()
    print(f"NUPSON {__version__} listening on {config.http_host}:{config.http_port}")
    try:
        server.serve_forever()
    finally:
        app.service.stop()
        server.server_close()


if __name__ == "__main__":
    main()
