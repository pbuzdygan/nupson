from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class PowerState(StrEnum):
    NORMAL = "normal"
    OUTAGE = "outage"
    RECOVERY_PENDING = "recovery_pending"
    WAITING_FOR_CHARGE = "waiting_for_charge"
    WAKING = "waking"


@dataclass
class Transition:
    previous: PowerState
    current: PowerState
    reason: str


class RecoveryMachine:
    def __init__(
        self,
        stabilization_seconds: int = 900,
        minimum_charge: int = 0,
        initial_state: PowerState = PowerState.NORMAL,
        outage_id: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.stabilization_seconds = max(1, stabilization_seconds)
        self.minimum_charge = max(0, min(minimum_charge, 100))
        self.state = initial_state
        self.outage_id = outage_id
        self.clock = clock
        self.recovery_started: float | None = (
            self.clock() if initial_state == PowerState.RECOVERY_PENDING else None
        )

    @property
    def remaining_seconds(self) -> int | None:
        if self.state != PowerState.RECOVERY_PENDING or self.recovery_started is None:
            return None
        elapsed = self.clock() - self.recovery_started
        return max(0, int(self.stabilization_seconds - elapsed + 0.999))

    def observe(self, status: str, charge: float | None = None) -> Transition | None:
        tokens = set(status.upper().split())
        previous = self.state
        unreliable = not status or bool(tokens & {"STALE", "COMMLOST", "UNKNOWN"})
        on_battery = "OB" in tokens
        online = "OL" in tokens and not unreliable

        if on_battery:
            self.recovery_started = None
            if self.state != PowerState.OUTAGE:
                self.state = PowerState.OUTAGE
                return Transition(previous, self.state, "UPS is running on battery")
            return None

        if self.state == PowerState.OUTAGE and online:
            self.state = PowerState.RECOVERY_PENDING
            self.recovery_started = self.clock()
            return Transition(previous, self.state, "Utility power returned; stabilization started")

        if self.state == PowerState.RECOVERY_PENDING:
            if not online:
                self.state = PowerState.OUTAGE
                self.recovery_started = None
                return Transition(
                    previous, self.state, "Recovery cancelled by unreliable power status"
                )
            enough_charge = self.minimum_charge == 0 or (
                charge is not None and charge >= self.minimum_charge
            )
            if self.remaining_seconds == 0:
                if enough_charge:
                    self.state = PowerState.WAKING
                    return Transition(
                        previous, self.state, "Power is stable and battery threshold is met"
                    )
                self.state = PowerState.WAITING_FOR_CHARGE
                self.recovery_started = None
                return Transition(
                    previous,
                    self.state,
                    "Power is stable; waiting for the battery charge threshold",
                )
        if self.state == PowerState.WAITING_FOR_CHARGE:
            if not online:
                self.state = PowerState.OUTAGE
                self.recovery_started = None
                return Transition(
                    previous, self.state, "Recovery cancelled by unreliable power status"
                )
            enough_charge = self.minimum_charge == 0 or (
                charge is not None and charge >= self.minimum_charge
            )
            if enough_charge:
                self.state = PowerState.WAKING
                return Transition(previous, self.state, "Battery threshold is met; checking hosts")
        if self.state == PowerState.WAKING and not online:
            self.state = PowerState.OUTAGE
            self.recovery_started = None
            return Transition(
                previous, self.state, "Wake sequence cancelled by unreliable power status"
            )
        return None

    def complete(self) -> Transition:
        previous = self.state
        self.state = PowerState.NORMAL
        self.recovery_started = None
        self.outage_id = None
        return Transition(previous, self.state, "Wake sequence completed")
