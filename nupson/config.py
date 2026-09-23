from __future__ import annotations

import os
import re
import secrets
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,48}$")
SAFE_DRIVER = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SAFE_PORT = re.compile(r"^[A-Za-z0-9_./:@-]{1,256}$")
SAFE_DEVICE_VALUE = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")
SAFE_NUT_USERNAME = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
NUT_CONFIG_FILES = ("nut.conf", "ups.conf", "upsd.conf", "upsd.users", "upsmon.conf")


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path
    http_host: str
    http_port: int
    nut_host: str
    nut_port: int
    manage_nut: bool
    demo: bool

    @property
    def database_path(self) -> Path:
        return self.data_dir / "nupson.db"

    @property
    def nut_dir(self) -> Path:
        return self.data_dir / "nut"

    @classmethod
    def from_env(cls) -> AppConfig:
        return cls(
            data_dir=Path(os.getenv("NUPSON_DATA_DIR", "/data")),
            http_host=os.getenv("NUPSON_HTTP_HOST", "0.0.0.0"),
            http_port=int(os.getenv("NUPSON_HTTP_PORT", "8080")),
            nut_host=os.getenv("NUPSON_NUT_HOST", "127.0.0.1"),
            nut_port=int(os.getenv("NUPSON_NUT_PORT", "3493")),
            manage_nut=_boolean("NUPSON_MANAGE_NUT", True),
            demo=_boolean("NUPSON_DEMO", False),
        )


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.lower() in {"1", "true", "yes", "on"}


def atomic_write(path: Path, content: str, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)


def validate_ups_config(config: dict[str, str]) -> dict[str, str]:
    name = config.get("name", "ups").strip()
    driver = config.get("driver", "usbhid-ups").strip()
    port = config.get("port", "auto").strip()
    if not SAFE_NAME.fullmatch(name):
        raise ValueError("UPS name may contain only letters, numbers, dot, dash and underscore")
    if not SAFE_DRIVER.fullmatch(driver):
        raise ValueError("Invalid NUT driver name")
    if not SAFE_PORT.fullmatch(port):
        raise ValueError("Invalid driver port")
    result = {"name": name, "driver": driver, "port": port}
    for key in ("vendorid", "productid", "serial", "desc"):
        value = config.get(key, "").strip()
        if key != "desc" and value and not SAFE_DEVICE_VALUE.fullmatch(value):
            raise ValueError(f"Invalid {key}")
        if "\n" in value or "\r" in value or '"' in value:
            raise ValueError(f"Invalid {key}")
        if value:
            result[key] = value
    return result


def validate_nut_username(value: str) -> str:
    username = value.strip()
    if not SAFE_NUT_USERNAME.fullmatch(username):
        raise ValueError(
            "NUT client username may contain only letters, numbers, dot, dash and underscore"
        )
    return username


def render_nut_config(config: dict[str, str]) -> str:
    cfg = validate_ups_config(config)
    lines = [f"[{cfg['name']}]", f"    driver = {cfg['driver']}", f"    port = {cfg['port']}"]
    for key in ("vendorid", "productid", "serial"):
        if value := cfg.get(key):
            lines.append(f"    {key} = {value}")
    if value := cfg.get("desc"):
        lines.append(f'    desc = "{value}"')
    return "\n".join(lines) + "\n"


def write_nut_files(
    directory: Path,
    ups: dict[str, str],
    monitor_password: str | None = None,
    readonly_password: str | None = None,
    client_username: str = "upsmon",
) -> dict[str, str]:
    cfg = validate_ups_config(ups)
    client_username = validate_nut_username(client_username)
    monitor_password = monitor_password or secrets.token_urlsafe(24)
    readonly_password = readonly_password or secrets.token_urlsafe(24)
    if any(char in monitor_password + readonly_password for char in "\n\r]"):
        raise ValueError("Invalid NUT password")

    atomic_write(directory / "nut.conf", "MODE=netserver\n")
    atomic_write(directory / "ups.conf", render_nut_config(cfg))
    atomic_write(
        directory / "upsd.conf",
        "LISTEN 0.0.0.0 3493\nMAXAGE 25\n",
    )
    atomic_write(
        directory / "upsd.users",
        "\n".join(
            [
                "[monitor]",
                f"    password = {readonly_password}",
                "",
                f"[{client_username}]",
                f"    password = {monitor_password}",
                "    upsmon secondary",
                "",
                "[nupson-primary]",
                f"    password = {monitor_password}",
                "    upsmon primary",
                "",
            ]
        ),
        0o600,
    )
    atomic_write(
        directory / "upsmon.conf",
        "\n".join(
            [
                f"MONITOR {cfg['name']}@127.0.0.1 1 nupson-primary {monitor_password} primary",
                "MINSUPPLIES 1",
                'SHUTDOWNCMD "/bin/true"',
                "POWERDOWNFLAG /tmp/nupson-killpower",
                "POLLFREQ 5",
                "POLLFREQALERT 2",
                "HOSTSYNC 30",
                "DEADTIME 25",
                "FINALDELAY 5",
                "",
            ]
        ),
        0o600,
    )
    return {"monitor_password": readonly_password, "upsmon_password": monitor_password}


def remove_nut_files(directory: Path) -> None:
    for filename in NUT_CONFIG_FILES:
        (directory / filename).unlink(missing_ok=True)


def read_nut_client_password(directory: Path, username: str = "upsmon") -> str | None:
    """Read the existing secondary-client password without exposing it via the API."""
    username = validate_nut_username(username)
    path = directory / "upsd.users"
    try:
        content = path.read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    section = ""
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif section == username and line.lower().startswith("password") and "=" in line:
            return line.split("=", 1)[1].strip()
    return None
