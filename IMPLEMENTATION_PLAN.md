# NUPSON implementation plan

This document tracks the product scope, completed work, and remaining release
criteria. Every functional change must also be recorded under `Unreleased` in
`changelog.md`.

## Status conventions

- `[ ]` pending;
- `[~]` in progress and must become `[x]` or `[ ]` before a change is closed;
- `[x]` implemented and verified;
- a milestone is complete only when every acceptance criterion is satisfied.

## Progress summary

| Milestone | Scope | Status | Progress |
| --- | --- | --- | --- |
| 0 | Project foundation | Complete | 8/8 |
| 1 | NUT core and UPS discovery | Complete | 9/9 |
| 2 | Network NUT server | Complete | 9/9 |
| 3 | Backend, persistence, and API | Complete | 9/9 |
| 4 | Outage and recovery state machine | Complete | 13/13 |
| 5 | Wake-on-LAN orchestration | Complete | 11/11 |
| 6 | Web interface | Complete | 11/11 |
| 7 | Container, security, and MVP release | In progress | 13/15 |

## Product scope

NUPSON has three primary responsibilities:

1. detect and operate a USB-connected UPS;
2. expose that UPS as a NUT server for protected `upsmon` clients;
3. after an outage, confirm continuous utility power, wait for the configured
   battery reserve, and wake selected hosts in a controlled order.

NUPSON coordinates client shutdown through standard NUT configuration. It does
not mount the Docker socket and cannot shut down its own Docker host directly.
An optional host helper remains outside the MVP scope.

## Milestone 0 — Project foundation

**Status: Complete**

- [x] Define the product scope and primary recovery scenario.
- [x] Create a tracked implementation plan and changelog.
- [x] Select the backend and frontend technology stack.
- [x] Create the project directory structure.
- [x] Configure formatting, linting, and tests.
- [x] Add continuous integration.
- [x] Add a Dockerfile and Compose configuration.
- [x] Add an MIT license and public-repository documentation.

Acceptance criteria: the project builds from a clean checkout, tests run both
locally and in CI, and the container exposes a health endpoint.

## Milestone 1 — NUT core and UPS discovery

**Status: Complete**

- [x] Build Network UPS Tools and the required USB drivers into the image.
- [x] Expose the USB bus without `privileged: true`.
- [x] Discover devices with `nut-scanner -U`.
- [x] Show manufacturer, model, VID/PID, serial number, and suggested driver.
- [x] Support manual configuration when discovery fails.
- [x] Write `ups.conf` atomically.
- [x] Start and supervise the selected driver.
- [x] Verify readiness using `ups.status`.
- [x] Handle USB reconnects and changed bus/device numbers.

Acceptance criteria: a supported UPS can be configured from the UI, persistent
configuration survives restart, and driver errors remain diagnosable without
restarting the whole application.

## Milestone 2 — Network NUT server

**Status: Complete**

- [x] Generate `upsd.conf`, `upsd.users`, and local primary `upsmon` settings.
- [x] Expose TCP 3493 to trusted LAN clients.
- [x] Support read-only and secondary `upsmon` accounts.
- [x] Start and supervise `upsd` and primary `upsmon`.
- [x] Test connections from a client perspective.
- [x] Display connected clients.
- [x] Generate installable secondary-client configuration bundles.
- [x] Document firewall and trusted-network requirements.
- [x] Keep administrative access disabled unless explicitly configured.

Acceptance criteria: external clients can query the UPS and react to
LOWBATT/FSD, credentials are absent from API responses and logs, and generated
configuration survives restart.

## Milestone 3 — Backend, persistence, and API

**Status: Complete**

- [x] Add SQLite persistence and additive schema migration.
- [x] Normalize NUT protocol values and status tokens.
- [x] Read status, charge, runtime, load, voltage, power, and model data.
- [x] Add configuration and current-state REST endpoints.
- [x] Add Server-Sent Events for live updates.
- [x] Persist operational and power events.
- [x] Add configurable event and telemetry retention.
- [x] Add separate liveness and readiness endpoints.
- [x] Keep the API available when the UPS is unavailable.

Acceptance criteria: restarts preserve configuration and history, individual
read failures never become false power transitions, and the UI distinguishes
`OL`, `OB`, `LB`, `FSD`, `STALE`, and `COMMLOST`.

## Milestone 4 — Outage and recovery state machine

**Status: Complete**

- [x] Implement `NORMAL`, `OUTAGE`, `RECOVERY_PENDING`,
  `WAITING_FOR_CHARGE`, and `WAKING` states.
- [x] Open an outage only after confirmed battery operation.
- [x] Start continuity confirmation only after confirmed `OL`.
- [x] Provide a configurable confirmation interval, defaulting to 15 minutes.
- [x] Reset confirmation after `OB`, `STALE`, `COMMLOST`, or unknown status.
- [x] Wait for an optional minimum battery reserve after continuity is confirmed.
- [x] Persist open outages and recovery progress.
- [x] Restart the full continuity interval after application downtime.
- [x] Prevent Wake-on-LAN after an ordinary application restart.
- [x] Separate application-start notifications from utility-restored events.
- [x] Prevent duplicate Wake-on-LAN work for the same outage.
- [x] Explain the active recovery step in the dashboard and notifications.
- [x] Cover transitions and edge cases with unit tests.

Acceptance criteria: brief power returns do not wake hosts, another outage
resets recovery, restart cannot cause accidental Wake-on-LAN, and every
automation decision leaves a readable event.

## Milestone 5 — Wake-on-LAN orchestration

**Status: Complete**

- [x] Add Wake-on-LAN host CRUD and manual wake.
- [x] Store MAC address, target address, broadcast, policy, wave, and delay.
- [x] Support `was_online`, `always`, `manual`, and `disabled` policies.
- [x] Snapshot eligible hosts for each outage.
- [x] Probe host reachability before sending Wake-on-LAN.
- [x] Add configurable attempts and retry intervals.
- [x] Verify startup with ICMP where an address is configured.
- [x] Process dependency waves with configurable delays.
- [x] Cancel remaining work when power becomes unreliable.
- [x] Persist attempts and results.
- [x] Send clear success, failure, and unverified notifications.

Acceptance criteria: manual and automatic wake work, `was_online` respects the
pre-outage snapshot, completed hosts are not repeated after restart, and a new
power failure stops the queue immediately.

## Milestone 6 — Web interface

**Status: Complete**

- [x] Add first-run administrator setup without a default password.
- [x] Add UPS discovery, manual setup, and diagnostics.
- [x] Add a responsive power-status dashboard.
- [x] Display charge, runtime, load, power, voltage, and communication state.
- [x] Display continuity confirmation, battery waiting, and wake progress.
- [x] Add Statistics, Hosts, NUT Clients, Events, and Settings views.
- [x] Add host management and manual Wake-on-LAN.
- [x] Group raw NUT diagnostics and battery-health signals.
- [x] Add light and dark themes.
- [x] Keep mobile forms and contextual settings usable.
- [x] Keep diagnostics available when the UPS or driver is unavailable.

Acceptance criteria: first-time setup requires no manual application-file
editing, critical status is understandable without NUT terminology, and the
reason for delayed Wake-on-LAN is always visible.

## Milestone 7 — Container, security, and MVP release

**Status: In progress**

- [x] Build `linux/amd64` and `linux/arm64` images.
- [x] Run application and NUT processes as the unprivileged `nut` account.
- [x] Avoid privileged mode, Docker socket access, and `CAP_SYS_BOOT`.
- [x] Restrict device cgroup access to USB character-device major 189.
- [x] Use a dedicated host udev group and explicit `NUPSON_USB_GID`.
- [x] Use host networking so LAN Wake-on-LAN broadcasts work.
- [x] Protect generated configuration and credentials with restrictive modes.
- [x] Add unit, lint, and Compose checks to pull-request and release workflows.
- [x] Document installation, client setup, upgrades, backup, and restore.
- [x] Exercise an emulated outage/recovery/Wake-on-LAN sequence.
- [x] Reject incomplete startup telemetry before accepting an outage state.
- [x] Separate stable and development image tags.
- [x] Publish images only from an explicitly published GitHub Release.
- [ ] Complete a release-candidate smoke test with a physical UPS and phone.
- [ ] Publish immutable version `0.1.0` and verify the `latest` manifest.

Acceptance criteria: a clean host needs only Docker, Compose, USB permissions,
and the documented GID; updates retain persistent state; the hardware smoke test
confirms discovery, client access, outage handling, recovery, and Wake-on-LAN.

## Post-MVP candidates

- [ ] Native ntfy integration.
- [ ] Prometheus metrics.
- [ ] Multiple UPS devices.
- [ ] Configuration import and export.
- [ ] TLS for NUT connections.
- [ ] Home Assistant, Homepage, and Glance integrations.
- [ ] Optional TCP service checks in addition to ICMP.
- [ ] Optional host helper for safely shutting down the Docker host.

## Decision log

| Date | Decision | Reason |
| --- | --- | --- |
| 2026-09-19 | Keep the host helper outside the MVP. | Standard NUT clients and recovery orchestration are the primary scope. |
| 2026-09-19 | Require a persisted outage before automatic Wake-on-LAN. | Prevents accidental waking after restart or deployment. |
| 2026-09-19 | Default continuity confirmation to 15 minutes. | Reduces risk from short-lived utility returns. |
| 2026-09-20 | Use host networking by default. | Docker bridge networking does not forward directed broadcasts to the physical LAN. |
| 2026-09-20 | Bind-mount `/dev/bus/usb`. | A bind mount follows USB re-enumeration while the cgroup rule restricts the device class. |
| 2026-09-22 | Require a dedicated udev group and explicit numeric GID. | Grants the container access to the UPS without broad USB-group discovery or privileged mode. |
| 2026-09-22 | Publish images only from GitHub Releases. | Keeps CI verification separate from intentional distribution and prevents stable/dev tag collisions. |
