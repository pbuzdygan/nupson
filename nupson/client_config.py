from __future__ import annotations

import io
import ipaddress
import json
import re
import zipfile
from importlib.resources import files as resource_files
from typing import Any

from .config import validate_nut_username

PLATFORMS = {"debian", "rhel", "alpine", "generic", "windows"}
POLICIES = {"inherit", "critical", "timer", "immediate", "monitor_only"}
SERVER_ADDRESS = re.compile(r"^[A-Za-z0-9_.:%-]{1,253}$")
WINDOWS_NUT_VERSION = "2.8.5-1-fixNSS"
WINDOWS_SHUTDOWN_BACKEND = "windows-native-timer"
CLIENT_BUNDLE_SCHEMA = 7


def validate_client_profile(values: dict[str, Any]) -> dict[str, Any]:
    name = str(values.get("name", "")).strip()
    if not name or len(name) > 80 or any(char in name for char in "\r\n"):
        raise ValueError("Client name is required")
    try:
        address = str(ipaddress.ip_address(str(values.get("address", "")).strip()))
    except ValueError as error:
        raise ValueError("A valid client IP address is required") from error
    server_address = str(values.get("server_address", "")).strip()
    if not SERVER_ADDRESS.fullmatch(server_address):
        raise ValueError("A valid NUPSON server address is required")
    platform = str(values.get("platform", "debian"))
    policy = str(values.get("policy", "inherit"))
    if platform not in PLATFORMS:
        raise ValueError("Unsupported client platform")
    if policy not in POLICIES:
        raise ValueError("Unsupported shutdown policy")
    delay = int(values.get("delay_seconds", 600))
    final_delay = int(values.get("final_delay_seconds", 5))
    if not 1 <= delay <= 86400:
        raise ValueError("Shutdown delay must be between 1 and 86400 seconds")
    if not 0 <= final_delay <= 300:
        raise ValueError("Final delay must be between 0 and 300 seconds")
    return {
        "name": name,
        "address": address,
        "server_address": server_address,
        "platform": platform,
        "policy": policy,
        "delay_seconds": delay,
        "final_delay_seconds": final_delay,
    }


def render_client_files(
    profile: dict[str, Any],
    settings: dict[str, Any],
    password: str,
) -> dict[str, str]:
    if not password or any(char.isspace() or char in '"\\' for char in password):
        raise ValueError("The current NUT client password cannot be safely exported")
    policy, delay, final_delay = _effective_policy(profile, settings)
    if profile["platform"] == "windows":
        return _render_windows_client_files(profile, settings, password, policy, delay, final_delay)
    return _render_linux_client_files(profile, settings, password, policy, delay, final_delay)


def _effective_policy(profile: dict[str, Any], settings: dict[str, Any]) -> tuple[str, int, int]:
    policy = profile["policy"]
    if policy == "inherit":
        return (
            str(settings["client_shutdown_policy"]),
            int(settings["client_shutdown_delay_seconds"]),
            int(settings["client_final_delay_seconds"]),
        )
    return policy, int(profile["delay_seconds"]), int(profile["final_delay_seconds"])


def _monitor_line(profile: dict[str, Any], settings: dict[str, Any], password: str) -> str:
    return (
        f"MONITOR {settings['ups_name']}@{profile['server_address']}:3493 "
        f"1 {validate_nut_username(str(settings.get('nut_client_username', 'upsmon')))} "
        f"{password} secondary"
    )


def _render_linux_client_files(
    profile: dict[str, Any],
    settings: dict[str, Any],
    password: str,
    policy: str,
    delay: int,
    final_delay: int,
) -> dict[str, str]:
    command = "/sbin/poweroff" if profile["platform"] == "alpine" else "/usr/sbin/shutdown -h now"
    shutdown_command = "/bin/true" if policy == "monitor_only" else command
    minimum_supplies = 0 if policy == "monitor_only" else 1
    monitor = _monitor_line(profile, settings, password)
    scheduled = policy in {"timer", "immediate"}
    notify_flags = "SYSLOG+WALL+EXEC" if scheduled else "SYSLOG+WALL"
    upsmon_lines = [
        monitor,
        f"MINSUPPLIES {minimum_supplies}",
        f'SHUTDOWNCMD "{shutdown_command}"',
        "POLLFREQ 5",
        "POLLFREQALERT 2",
        "DEADTIME 25",
        "HOSTSYNC 30",
        f"FINALDELAY {final_delay}",
        f"NOTIFYFLAG ONLINE {notify_flags}",
        f"NOTIFYFLAG ONBATT {notify_flags}",
        "NOTIFYFLAG LOWBATT SYSLOG+WALL",
        "NOTIFYFLAG FSD SYSLOG+WALL",
        "NOTIFYFLAG COMMOK SYSLOG",
        "NOTIFYFLAG COMMBAD SYSLOG+WALL",
    ]
    files = {"nut.conf": "MODE=netclient\n"}
    if scheduled:
        upsmon_lines.extend(
            [
                "RUN_AS_USER nut",
                "NOTIFYCMD /usr/sbin/upssched",
            ]
        )
        action = (
            f"START-TIMER nupson-shutdown {delay}"
            if policy == "timer"
            else "EXECUTE nupson-shutdown"
        )
        files["upssched.conf"] = "\n".join(
            [
                "CMDSCRIPT /usr/local/sbin/nupson-upssched",
                "PIPEFN /run/nut/upssched.pipe",
                "LOCKFN /run/nut/upssched.lock",
                f"AT ONBATT * {action}",
                "AT ONLINE * CANCEL-TIMER nupson-shutdown",
                "",
            ]
        )
        files["nupson-upssched"] = "\n".join(
            [
                "#!/bin/sh",
                'if [ "$1" = "nupson-shutdown" ]; then',
                f"    exec /usr/bin/sudo -n {command}",
                "fi",
                "",
            ]
        )
        files["nupson-nut-shutdown.sudoers"] = "\n".join(
            [
                "# Generated by NUPSON for an early local shutdown from upssched.",
                f"nut ALL=(root) NOPASSWD: {command}",
                "",
            ]
        )
    files["upsmon.conf"] = "\n".join(upsmon_lines) + "\n"
    files["README.txt"] = _readme(profile, policy, scheduled)
    return files


def _render_windows_client_files(
    profile: dict[str, Any],
    settings: dict[str, Any],
    password: str,
    policy: str,
    delay: int,
    final_delay: int,
) -> dict[str, str]:
    scheduled = policy in {"timer", "immediate"}
    minimum_supplies = 0 if policy == "monitor_only" else 1
    notify_flags = "SYSLOG+EXEC" if scheduled else "SYSLOG"
    upsmon_lines = [
        _monitor_line(profile, settings, password),
        f"MINSUPPLIES {minimum_supplies}",
        'SHUTDOWNCMD "C:\\\\Windows\\\\System32\\\\shutdown.exe /s /f /t 0"',
        "POLLFREQ 5",
        "POLLFREQALERT 2",
        "DEADTIME 25",
        "HOSTSYNC 30",
        f"FINALDELAY {final_delay}",
        f"NOTIFYFLAG ONLINE {notify_flags}",
        f"NOTIFYFLAG ONBATT {notify_flags}",
        "NOTIFYFLAG LOWBATT SYSLOG",
        "NOTIFYFLAG FSD SYSLOG",
        "NOTIFYFLAG COMMOK SYSLOG",
        "NOTIFYFLAG COMMBAD SYSLOG",
    ]
    if scheduled:
        upsmon_lines.append('NOTIFYCMD "C:\\\\ProgramData\\\\NUPSON\\\\Client\\\\nupson-event.cmd"')

    manifest = {
        "schema": CLIENT_BUNDLE_SCHEMA,
        "platform": "windows",
        "nutVersion": WINDOWS_NUT_VERSION,
        "shutdownBackend": WINDOWS_SHUTDOWN_BACKEND,
        "profileName": profile["name"],
        "clientAddress": profile["address"],
        "serverAddress": profile["server_address"],
        "upsName": settings["ups_name"],
        "policy": policy,
        "delaySeconds": delay,
        "finalDelaySeconds": final_delay,
    }
    generated = {
        "nut.conf": "MODE=netclient\n",
        "upsmon.conf": "\n".join(upsmon_lines) + "\n",
        "nupson-client.json": json.dumps(manifest, ensure_ascii=True, indent=2) + "\n",
        "nupson-client.ps1": _windows_template("nupson-client.ps1"),
        "install-nupson-client.ps1": _windows_template("install-nupson-client.ps1"),
        "update-nupson-client.ps1": _windows_template("update-nupson-client.ps1"),
        "uninstall-nupson-client.ps1": _windows_template("uninstall-nupson-client.ps1"),
        "test-nupson-client.ps1": _windows_template("test-nupson-client.ps1"),
        "README.txt": _windows_readme(profile, policy, delay),
    }
    if scheduled:
        generated["nupson-event.ps1"] = _windows_template("nupson-event.ps1")
        generated["nupson-event.cmd"] = _windows_template("nupson-event.cmd")
    return generated


def _windows_template(name: str) -> str:
    return (
        resource_files("nupson").joinpath("templates", "windows", name).read_text(encoding="utf-8")
    )


def _windows_readme(profile: dict[str, Any], policy: str, delay: int) -> str:
    timing = f" after {delay} seconds on battery" if policy == "timer" else ""
    return "".join(
        [
            f"NUPSON Windows client profile: {profile['name']}\n",
            f"Expected client IP: {profile['address']}\n",
            f"NUPSON server: {profile['server_address']}\n",
            f"Shutdown policy: {policy}{timing}\n\n",
            "Requirements: 64-bit Windows 10/11 or Windows Server and an ",
            "administrator account. Internet access is required during installation.\n\n",
            "1. Extract this ZIP to a local directory.\n",
            "2. Open PowerShell as Administrator in that directory.\n",
            "3. Run the menu: powershell.exe -ExecutionPolicy Bypass ",
            "-File .\\nupson-client.ps1\n",
            "4. Select installation and then the connection/service test.\n",
            "5. Confirm that the client changes to Connected in NUPSON.\n\n",
            "The install, test, update, and uninstall scripts can still be run directly.\n",
            "The Network UPS Tools service starts before user logon. For timer policies, ",
            "return of utility power cancels only a shutdown scheduled by NUPSON. A critical ",
            "LOWBATT/FSD state can shut the machine down earlier than the configured timer.\n\n",
            "The bundle contains the NUT password. Delete the extracted bundle after a ",
            "successful installation and keep any backups protected. On older Windows ",
            "builds, install 7-Zip if the built-in tar.exe cannot unpack the NUT archive.\n",
        ]
    )


def zip_client_files(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            info = zipfile.ZipInfo(name)
            mode = {
                "nupson-upssched": 0o755,
                "nupson-nut-shutdown.sudoers": 0o440,
            }.get(name, 0o640)
            info.external_attr = mode << 16
            archive.writestr(info, content)
    return buffer.getvalue()


def _readme(profile: dict[str, Any], policy: str, scheduled: bool) -> str:
    directory = "/etc/nut" if profile["platform"] != "rhel" else "/etc/ups"
    service = "nut-monitor" if profile["platform"] != "rhel" else "upsmon"
    package = {
        "debian": "apt install nut-client" + (" sudo" if scheduled else ""),
        "rhel": "dnf install nut-client" + (" sudo" if scheduled else ""),
        "alpine": "apk add nut" + (" sudo" if scheduled else ""),
        "generic": "Install the NUT client package"
        + (" and sudo" if scheduled else "")
        + " for this system",
    }[profile["platform"]]
    lines = [
        f"NUPSON client profile: {profile['name']}\n"
        f"Expected client IP: {profile['address']}\n"
        f"Shutdown policy: {policy}\n\n"
        f"1. Install NUT: {package}\n"
        f"2. Copy nut.conf and upsmon.conf to {directory}.\n"
        f"3. Protect upsmon.conf: chown root:nut {directory}/upsmon.conf && "
        f"chmod 640 {directory}/upsmon.conf\n"
    ]
    step = 4
    if scheduled:
        lines.extend(
            [
                f"{step}. Install upssched.conf: install -o root -g nut -m 0640 "
                f"upssched.conf {directory}/upssched.conf\n",
                f"{step + 1}. Install the root-owned command script: install -o root "
                "-g root -m 0755 nupson-upssched "
                "/usr/local/sbin/nupson-upssched\n",
                f"{step + 2}. Install the restricted privilege rule: install -o root "
                "-g root -m 0440 nupson-nut-shutdown.sudoers "
                "/etc/sudoers.d/nupson-nut-shutdown\n",
                f"{step + 3}. Validate it: visudo -cf /etc/sudoers.d/nupson-nut-shutdown\n",
                "The sudoers rule permits user nut to run only the exact local shutdown "
                "command generated for this profile. It is required only by the timer "
                "and immediate policies; standard critical shutdown uses upsmon's "
                "privileged process and does not need this rule.\n",
            ]
        )
        step += 4
    lines.extend(
        [
            f"{step}. Restart the service: systemctl restart {service}\n",
            f"{step + 1}. Confirm the connection in NUPSON.\n",
        ]
    )
    return "".join(lines)
