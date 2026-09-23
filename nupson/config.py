from __future__ import annotations

import os
import re
import secrets
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,48}$")
SAFE_DEVICE_VALUE = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")
SAFE_NUT_USERNAME = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
SAFE_HOST = re.compile(r"^[A-Za-z0-9_.:%-]{1,253}$")
SAFE_SECRET = re.compile(r"^[A-Za-z0-9_./+=:@-]{1,256}$")
CONNECTION_TYPES = {"usb", "remote_nut", "snmp"}
SNMP_MIBS = {"auto", "ietf", "apcc", "mge", "eaton_pw_nm2", "netvision"}
SNMP_AUTH_PROTOCOLS = {"MD5", "SHA", "SHA256", "SHA384", "SHA512"}
SNMP_PRIV_PROTOCOLS = {"DES", "AES", "AES192", "AES256"}
UPS_SECRET_KEYS = ("community", "auth_password", "priv_password")
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


def _clean_text(value: object, label: str, maximum: int = 256) -> str:
    result = str(value or "").strip()
    if len(result) > maximum or any(char in result for char in ("\r", "\n", '"', "\\")):
        raise ValueError(f"Invalid {label}")
    return result


def _port(value: object, default: int) -> int:
    try:
        result = int(value or default)
    except (TypeError, ValueError) as error:
        raise ValueError("Port must be a number") from error
    if not 1 <= result <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    return result


def _host(value: object) -> str:
    result = str(value or "").strip().strip("[]")
    if not SAFE_HOST.fullmatch(result):
        raise ValueError("Invalid host name or IP address")
    return result


def _secret(
    config: dict[str, object],
    existing: dict[str, str],
    key: str,
    required: bool,
    minimum: int = 1,
) -> str:
    value = str(config.get(key, "") or "").strip() or existing.get(key, "")
    if required and not value:
        raise ValueError(f"{key} is required")
    if value and len(value) < minimum:
        raise ValueError(f"{key} must contain at least {minimum} characters")
    if value and not SAFE_SECRET.fullmatch(value):
        raise ValueError(f"Invalid {key}")
    return value


def validate_ups_config(
    config: dict[str, object], existing_secrets: dict[str, str] | None = None
) -> dict[str, str]:
    existing_secrets = existing_secrets or {}
    connection_type = str(config.get("connection_type") or "usb").strip()
    if connection_type not in CONNECTION_TYPES:
        raise ValueError("Unsupported UPS connection profile")

    name = str(config.get("name", "ups")).strip()
    if not SAFE_NAME.fullmatch(name):
        raise ValueError("UPS name may contain only letters, numbers, dot, dash and underscore")
    result = {"connection_type": connection_type, "name": name}
    if desc := _clean_text(config.get("desc", ""), "description", 160):
        result["desc"] = desc

    if connection_type == "usb":
        result.update({"driver": "usbhid-ups", "port": "auto"})
        for key in ("vendorid", "productid", "serial"):
            value = str(config.get(key, "") or "").strip()
            if value and not SAFE_DEVICE_VALUE.fullmatch(value):
                raise ValueError(f"Invalid {key}")
            if value:
                result[key] = value
        return result

    if connection_type == "remote_nut":
        host = _host(config.get("remote_host"))
        remote_port = _port(config.get("remote_port"), 3493)
        remote_ups = str(config.get("remote_ups_name") or "").strip()
        if not SAFE_NAME.fullmatch(remote_ups):
            raise ValueError("Invalid remote UPS name")
        address = f"[{host}]" if ":" in host else host
        result.update(
            {
                "driver": "dummy-ups",
                "port": f"{remote_ups}@{address}:{remote_port}",
                "remote_host": host,
                "remote_port": str(remote_port),
                "remote_ups_name": remote_ups,
                "repeater_disable_strict_start": "true",
            }
        )
        return result

    host = _host(config.get("snmp_host"))
    snmp_port = _port(config.get("snmp_port"), 161)
    version = str(config.get("snmp_version") or "v3")
    if version not in {"v1", "v2c", "v3"}:
        raise ValueError("Unsupported SNMP version")
    mibs = str(config.get("mibs") or "auto")
    if mibs not in SNMP_MIBS:
        raise ValueError("Unsupported SNMP MIB profile")
    try:
        pollfreq = int(config.get("pollfreq") or 15)
    except (TypeError, ValueError) as error:
        raise ValueError("SNMP polling interval must be a number") from error
    if not 5 <= pollfreq <= 300:
        raise ValueError("SNMP polling interval must be between 5 and 300 seconds")
    address = f"[{host}]" if ":" in host else host
    result.update(
        {
            "driver": "snmp-ups",
            "port": f"{address}:{snmp_port}",
            "snmp_host": host,
            "snmp_port": str(snmp_port),
            "snmp_version": version,
            "mibs": mibs,
            "pollfreq": str(pollfreq),
        }
    )
    if version in {"v1", "v2c"}:
        result["community"] = _secret(config, existing_secrets, "community", True)
        return result

    sec_name = str(config.get("sec_name") or "").strip()
    if not SAFE_DEVICE_VALUE.fullmatch(sec_name):
        raise ValueError("SNMPv3 security name is required")
    sec_level = str(config.get("sec_level") or "authPriv")
    if sec_level not in {"noAuthNoPriv", "authNoPriv", "authPriv"}:
        raise ValueError("Unsupported SNMPv3 security level")
    result.update({"sec_name": sec_name, "sec_level": sec_level})
    if sec_level in {"authNoPriv", "authPriv"}:
        auth_protocol = str(config.get("auth_protocol") or "SHA256").upper()
        if auth_protocol not in SNMP_AUTH_PROTOCOLS:
            raise ValueError("Unsupported SNMPv3 authentication protocol")
        result["auth_protocol"] = auth_protocol
        result["auth_password"] = _secret(config, existing_secrets, "auth_password", True, 8)
    if sec_level == "authPriv":
        priv_protocol = str(config.get("priv_protocol") or "AES").upper()
        if priv_protocol not in SNMP_PRIV_PROTOCOLS:
            raise ValueError("Unsupported SNMPv3 privacy protocol")
        result["priv_protocol"] = priv_protocol
        result["priv_password"] = _secret(config, existing_secrets, "priv_password", True, 8)
    return result


def public_ups_config(config: dict[str, object] | None) -> dict[str, object] | None:
    if not config:
        return None
    result = {key: value for key, value in config.items() if key not in UPS_SECRET_KEYS}
    result.setdefault("connection_type", "usb")
    for key in UPS_SECRET_KEYS:
        result[f"{key}_configured"] = bool(config.get(key) or config.get(f"{key}_configured"))
    return result


def read_ups_secrets(directory: Path) -> dict[str, str]:
    try:
        content = (directory / "ups.conf").read_text(encoding="ascii")
    except FileNotFoundError:
        return {}
    names = {
        "community": "community",
        "authPassword": "auth_password",
        "privPassword": "priv_password",
    }
    result: dict[str, str] = {}
    for raw_line in content.splitlines():
        if "=" not in raw_line:
            continue
        key, value = (part.strip() for part in raw_line.split("=", 1))
        if mapped := names.get(key):
            result[mapped] = value.strip('"')
    return result


def validate_nut_username(value: str) -> str:
    username = value.strip()
    if not SAFE_NUT_USERNAME.fullmatch(username):
        raise ValueError(
            "NUT client username may contain only letters, numbers, dot, dash and underscore"
        )
    return username


def render_nut_config(config: dict[str, object]) -> str:
    cfg = validate_ups_config(config)
    lines = [f"[{cfg['name']}]", f"    driver = {cfg['driver']}", f"    port = {cfg['port']}"]
    options: tuple[tuple[str, str], ...]
    if cfg["connection_type"] == "usb":
        options = tuple((key, key) for key in ("vendorid", "productid", "serial"))
    elif cfg["connection_type"] == "remote_nut":
        options = (("repeater_disable_strict_start", ""),)
    else:
        options = (
            ("snmp_version", "snmp_version"),
            ("mibs", "mibs"),
            ("pollfreq", "pollfreq"),
            ("community", "community"),
            ("secLevel", "sec_level"),
            ("secName", "sec_name"),
            ("authProtocol", "auth_protocol"),
            ("authPassword", "auth_password"),
            ("privProtocol", "priv_protocol"),
            ("privPassword", "priv_password"),
        )
    for output_key, config_key in options:
        if not config_key:
            lines.append(f"    {output_key}")
        elif value := cfg.get(config_key):
            lines.append(f"    {output_key} = {value}")
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
    atomic_write(directory / "ups.conf", render_nut_config(cfg), 0o600)
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
