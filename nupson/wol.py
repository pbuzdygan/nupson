from __future__ import annotations

import re
import socket
import subprocess
import time
from collections.abc import Callable
from math import ceil

MAC_PATTERN = re.compile(r"^(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")


def normalize_mac(mac: str) -> str:
    if not MAC_PATTERN.fullmatch(mac):
        raise ValueError("MAC address must have six hexadecimal octets")
    return mac.replace("-", ":").upper()


def magic_packet(mac: str) -> bytes:
    raw = bytes.fromhex(normalize_mac(mac).replace(":", ""))
    return b"\xff" * 6 + raw * 16


def send_magic_packet(mac: str, broadcast: str = "255.255.255.255", port: int = 9) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(magic_packet(mac), (broadcast, port))


def ping_online(address: str, timeout: float = 1.5) -> bool:
    if not address:
        return False
    try:
        result = subprocess.run(
            ["ping", "-n", "-c", "1", "-W", str(max(1, ceil(timeout))), "--", address],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 1,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def wake_and_check(
    host: dict,
    attempts: int = 3,
    interval: float = 30,
    verification_seconds: float = 120,
    cancelled: Callable[[], bool] = lambda: False,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[str, int]:
    address = host.get("address")
    if address and ping_online(address):
        return "online", 0

    can_verify = bool(address)
    for attempt in range(1, attempts + 1):
        if cancelled():
            return "cancelled", attempt - 1
        send_magic_packet(host["mac"], host.get("broadcast") or "255.255.255.255")
        wait_seconds = interval if attempt < attempts else verification_seconds
        if not can_verify:
            if attempt < attempts:
                sleeper(interval)
            continue

        elapsed = 0.0
        while elapsed < wait_seconds:
            delay = min(5.0, wait_seconds - elapsed)
            sleeper(delay)
            elapsed += delay
            if cancelled():
                return "cancelled", attempt
            if ping_online(address):
                return "online", attempt

    return ("unreachable" if can_verify else "sent_unverified"), attempts
