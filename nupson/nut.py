from __future__ import annotations

import re
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')
NUT_BINARY_DIRS = (Path("/usr/sbin"), Path("/usr/libexec/nut"), Path("/lib/nut"))
T = TypeVar("T")


class NutError(RuntimeError):
    pass


def _nut_binary(name: str) -> str:
    """Locate NUT daemons across current and legacy Debian layouts."""
    if executable := shutil.which(name):
        return executable
    for directory in NUT_BINARY_DIRS:
        candidate = directory / name
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError(f"Nie znaleziono programu NUT: {name}")


def _unquote(value: str) -> str:
    return bytes(value, "utf-8").decode("unicode_escape")


class NutClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 3493, timeout: float = 3):
        self.host = host
        self.port = port
        self.timeout = timeout

    def _command(self, command: str) -> list[str]:
        try:
            with socket.create_connection((self.host, self.port), self.timeout) as connection:
                connection.settimeout(self.timeout)
                connection.sendall((command + "\n").encode("ascii"))
                stream = connection.makefile("r", encoding="utf-8", newline="\n")
                lines: list[str] = []
                is_list = command.startswith("LIST ")
                while True:
                    line = stream.readline()
                    if not line:
                        break
                    line = line.rstrip("\r\n")
                    if line.startswith("ERR "):
                        raise NutError(line[4:])
                    lines.append(line)
                    if not is_list or line.startswith("END LIST "):
                        break
                return lines
        except (OSError, TimeoutError) as error:
            raise NutError(str(error)) from error

    def list_ups(self) -> list[dict[str, str]]:
        result = []
        for line in self._command("LIST UPS"):
            if not line.startswith("UPS "):
                continue
            parts = line.split(" ", 2)
            quoted = QUOTED.search(parts[2]) if len(parts) > 2 else None
            result.append(
                {"name": parts[1], "description": _unquote(quoted.group(1)) if quoted else ""}
            )
        return result

    def variables(self, ups_name: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in self._command(f"LIST VAR {ups_name}"):
            if not line.startswith("VAR "):
                continue
            parts = line.split(" ", 3)
            if len(parts) == 4:
                quoted = QUOTED.search(parts[3])
                values[parts[2]] = _unquote(quoted.group(1)) if quoted else parts[3]
        return values

    def clients(self, ups_name: str) -> list[str]:
        clients = []
        for line in self._command(f"LIST CLIENT {ups_name}"):
            if line.startswith("CLIENT "):
                clients.append(line.split(" ", 2)[2])
        return clients


@dataclass
class ScanResult:
    name: str
    driver: str
    port: str
    vendorid: str = ""
    productid: str = ""
    serial: str = ""
    desc: str = ""

    def as_dict(self) -> dict[str, str]:
        return self.__dict__.copy()


def scan_usb(timeout: int = 20) -> list[dict[str, str]]:
    try:
        result = subprocess.run(
            ["nut-scanner", "-U", "-N"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as error:
        raise NutError("nut-scanner is not installed") from error
    except subprocess.TimeoutExpired as error:
        raise NutError("USB scan timed out") from error
    if result.returncode != 0:
        raise NutError(result.stderr.strip() or "USB scan failed")
    return parse_ups_conf(result.stdout)


def parse_ups_conf(content: str) -> list[dict[str, str]]:
    devices: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            if current:
                devices.append(current)
            current = {"name": line[1:-1]}
            continue
        if current is not None and "=" in line:
            key, value = (part.strip() for part in line.split("=", 1))
            current[key.lower()] = value.strip('"')
    if current:
        devices.append(current)
    return devices


class NutSupervisor:
    def __init__(self, nut_dir: Path, enabled: bool):
        self.nut_dir = nut_dir
        self.enabled = enabled
        self.upsd: subprocess.Popen[str] | None = None
        self.upsmon: subprocess.Popen[str] | None = None
        self.startup_error: str | None = None
        self._process_lock = threading.RLock()

    def _environment(self) -> dict[str, str]:
        return {**__import__("os").environ, "NUT_CONFPATH": str(self.nut_dir)}

    def _start_servers(self, environment: dict[str, str]) -> None:
        self.upsd = subprocess.Popen([_nut_binary("upsd"), "-F"], env=environment, text=True)
        time.sleep(0.5)
        # NUPSON already runs as the unprivileged NUT user. Avoid upsmon's
        # privileged parent/child split so that the process can be stopped
        # reliably during a restart.
        self.upsmon = subprocess.Popen(
            [_nut_binary("upsmon"), "-F", "-p"], env=environment, text=True
        )

    def _stop_servers(self) -> None:
        for process in (self.upsmon, self.upsd):
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    process.kill()
        self.upsmon = None
        self.upsd = None

    def start(self) -> None:
        with self._process_lock:
            self.startup_error = None
            if not self.enabled or not (self.nut_dir / "ups.conf").exists():
                return
            environment = self._environment()
            result = subprocess.run(
                ["upsdrvctl", "start"],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                output = "\n".join(
                    part.strip() for part in (result.stderr, result.stdout) if part.strip()
                )
                self.startup_error = _friendly_driver_error(output)
                return
            try:
                self._start_servers(environment)
            except FileNotFoundError as error:
                self.startup_error = str(error)
                self.stop()

    def stop(self) -> None:
        with self._process_lock:
            environment = self._environment()
            self._stop_servers()
            if self.enabled and (self.nut_dir / "ups.conf").exists():
                subprocess.run(
                    ["upsdrvctl", "stop"],
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                )

    def restart(self) -> None:
        with self._process_lock:
            self.stop()
            self.start()

    def reconfigure(self, write_configuration: Callable[[], T]) -> T:
        """Stop the driver using its old config before replacing that config."""
        with self._process_lock:
            self.stop()
            result = write_configuration()
            self.start()
            return result

    def reconfigure_clients(self, write_configuration: Callable[[], T]) -> T:
        """Reload NUT accounts without restarting the UPS driver."""
        with self._process_lock:
            self.startup_error = None
            self._stop_servers()
            result = write_configuration()
            if self.enabled and (self.nut_dir / "ups.conf").exists():
                try:
                    self._start_servers(self._environment())
                except FileNotFoundError as error:
                    self.startup_error = str(error)
                    self._stop_servers()
            return result

    def clear_fsd(self) -> None:
        """Restart upsd/upsmon after utility returns, clearing a latched FSD."""
        with self._process_lock:
            if not self.enabled:
                return
            environment = self._environment()
            self._stop_servers()
            self._start_servers(environment)


def _friendly_driver_error(output: str) -> str:
    """Turn terse upsdrvctl failures into actionable UI diagnostics."""
    if "Can't claim USB device" in output or "Resource busy" in output:
        return (
            "Nie można otworzyć urządzenia USB. UPS może być odłączony albo używany "
            "przez inny proces lub kontener. NUPSON automatycznie ponowi próbę."
        )
    if "Permission denied" in output or "Access denied" in output:
        return "Brak uprawnień do urządzenia USB. Sprawdź mapowanie urządzenia i grupę USB."
    details = next((line.strip() for line in reversed(output.splitlines()) if line.strip()), "")
    return f"Sterownik NUT nie uruchomił się: {details or 'nieznany błąd'}"
