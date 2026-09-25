# Changelog

All notable changes to NUPSON are documented here. Unreleased entries describe
user-visible or operational effects rather than implementation details.

## 0.1.1

### New features

- Added beta Windows 10/11 and Windows Server client bundles using the official
  NUT service. Time-based and immediate shutdown policies use cancellable native
  Windows shutdown scheduling and do not depend on upssched or user logon.
- Added administrator install, update, diagnostic, and uninstall scripts for
  Windows clients, with a pinned NUT archive checksum, protected configuration,
  delayed automatic service startup, and recovery settings.
- Added a local PowerShell management menu to each Windows bundle for
  installation, diagnostics, updates, and removal while preserving direct
  access to the individual scripts.

### Improvements

- Reworked host cards to show current ping reachability and readable Wake-on-LAN
  policy labels, and continued reachability probes during outages after the wake
  queue snapshot has been created.
- Combined UPS connection and NUT client access into one collapsible section,
  with automatic expansion and clear selection feedback after USB discovery.
- Moved NUT client preview and editing panels directly below their profile card
  and added explicit save, discard, or continue-editing choices for unsaved data.
- Added local Tabler icons to host and client actions.
- Reset the UPS and NUT expandable sections to closed whenever the view is
  opened and placed the compact connection and client access panels side by side
  on desktop.
- Clarified whether dashboard runtime comes from the standard NUT
  `battery.runtime` field and when the UPS does not expose that value.
- Listed Fedora explicitly in the DNF-based NUT client platform option alongside
  RHEL, Rocky Linux, and AlmaLinux.

### Bug fixes

- Fixed the extended Windows diagnostic marker JSON and suppressed a misleading
  localized service-not-found message emitted by the successful NUT service
  unregister operation during updates and removal.
- Remembered power telemetry previously exposed by the UPS and required it after
  managed NUT startup or USB recovery. A partially initialized driver is retried
  before the dashboard accepts missing load, power, and voltage measurements.
- Discarded expired Windows shutdown markers and markers left by an earlier
  system boot, so a completed shutdown cannot block the next ONBATT timer after
  Windows starts again without an interactive user session.
- Detected an `upsd` or `upsmon` process that exits during startup, including a
  second host-networked NUPSON instance conflicting on TCP 3493. A managed
  instance no longer accepts telemetry or client sessions from another NUT
  server listening on the same host.
- Prevented the Windows client diagnostic from treating the informational NUT
  `Init SSL without certificate database` message as a PowerShell failure. The
  diagnostic now also requires a running `upsmon.exe` and verifies the active
  session reported by the NUT server, matching the status shown in the UI. The
  installer likewise rejects a running wrapper service without `upsmon.exe`.
- Normalized IPv4-mapped IPv6 client addresses before matching them to NUT
  client profiles.
- Kept the dashboard in the disconnected state while NUT still reports stale
  UPS data after a USB reconnection, and continued restarting the managed
  driver until fresh telemetry is available. The communication-restored event
  is now emitted only after a reliable UPS status is received.
- Made the dashboard power-state heading consistently show a communication
  failure when `ups.status` contains `COMMLOST` or `STALE`.

## 0.1.0

### New features

- Added editable UPS connection profiles for direct USB, remote NUT, and SNMP
  (v1, v2c, and v3), backed by the corresponding NUT drivers.
- Kept SNMP community and v3 authentication/privacy secrets out of SQLite and
  API responses while preserving them across profile edits in the protected NUT
  configuration file.
- Added grouped UPS diagnostics and battery-health warnings based on explicit
  firmware signals and observed runtime.
- Added a managed NUT server with USB discovery, manual configuration, and
  supervision of the driver, `upsd`, and primary `upsmon`.
- Added secondary NUT client accounts, connected-client visibility, and
  downloadable client configuration bundles.
- Added persistent outage, recovery, and Wake-on-LAN orchestration.
- Added ordered Wake-on-LAN policies, retries, reachability checks, waves, and
  delays.
- Added SQLite persistence for settings, events, outages, telemetry, sessions,
  and wake queues.
- Added REST endpoints, Server-Sent Events, health/readiness checks, telemetry
  history, energy estimates, and webhook notifications.
- Added a responsive dashboard, first-run administrator setup, diagnostics,
  client and host management, event history, and light/dark themes.
- Added demo mode and a hardware-independent end-to-end integration test.

### Improvements

- Split recovery into explicit phases: confirmation of continuous utility
  power, waiting for the configured battery reserve, and host startup. The
  dashboard and notifications no longer present `00:00` as an active
  stabilization timer while the battery is charging.
- Replaced the legacy USB-group setting with `NUPSON_USB_GID`. A
  dedicated host udev group now grants access only to the UPS instead of adding
  the application account to every detected USB device group.
- Added complete host group, udev rule, GID verification, and USB troubleshooting
  instructions.
- Prepared the source tree for public hosting by excluding runtime databases,
  credentials, local environment files, caches, backups, editor files, and
  build artifacts.
- Container images are now published only when a GitHub Release is published.
  Stable releases from `main` create `X.Y.Z` and `latest`; development releases
  from `dev` create `devX.Y.Z` and `dev_latest`.
- Based the image on Debian 13 and pinned the verified NUT 2.8.5 source archive.
- Restricted the container to USB device major 189, read-only udev metadata,
  host networking, and `no-new-privileges`, without the Docker socket.
- Added automated Ruff, unit, and Compose checks without building or publishing
  container images on ordinary pushes.
- Improved mobile layout, contextual settings, host/client live refresh, and
  background Wake-on-LAN verification.

### Bug fixes

- Aligned the container's unprivileged `nut` account with configurable host
  `NUPSON_UID` and `NUPSON_GID` values so bind-mounted persistent data no
  longer appears to belong to unrelated host services, and restricted the
  SQLite database to owner-only access.
- Persisted administrator sessions and made the dashboard recover cleanly after
  temporary backend disconnection.
- Changed USB passthrough from a fixed device mapping to a bind mount so device
  re-enumeration does not leave a stale path in the container.
- Prevented a restart during recovery from emitting a false utility-restored
  notification; recovery restarts with a full continuity confirmation period.
- Added the minimum sudoers rule required by early client shutdown policies.
- Corrected NUT scanner packaging and bypassed distribution wrappers that
  ignored the custom configuration directory.
- Made communication loss or renewed battery operation cancel confirmation and
  Wake-on-LAN work immediately.
- Removed deleted hosts from active wake queues to avoid foreign-key errors.
- Made the configured dashboard port apply consistently to the process and
  health check under host networking.
- Rejected incomplete startup telemetry before interpreting `OB` as a real
  outage.
