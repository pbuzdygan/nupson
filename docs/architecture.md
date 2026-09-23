# Architecture

NUPSON uses a single application container and persistent `/data` volume.

```text
USB UPS -> NUT driver -> upsd :3493 -> remote upsmon clients
                         |
                         +-> NUPSON monitor -> recovery state machine
                                                |
                                                +-> ordered Wake-on-LAN
```

The Python process provides the web API, embedded static UI, SQLite storage,
NUT protocol client, event loop and Wake-on-LAN scheduler. It starts NUT drivers,
`upsd`, and a coordinating primary `upsmon` when hardware configuration exists.
All processes run under the container's unprivileged `nut` account.

## Telemetry history

NUPSON records one UPS telemetry sample per minute in SQLite. Samples include
battery charge and runtime, load, input and output voltage, status, and reported
or estimated real power. The default retention is 180 days. API responses are
aggregated to at most a few hundred points, independently of the selected time
range.

Energy is integrated from stored power samples. Gaps longer than five minutes
are capped so container downtime or lost UPS communication cannot be counted as
continuous consumption. Cost is an estimate based on the configured PLN/kWh
price. If the UPS does not expose watts or nominal real power, an optional
configured nominal watt value can be combined with `ups.load`.

The Statistics view opens in a live one-hour range and refreshes silently every
30 seconds; the underlying samples remain one minute apart. Longer presets and
a custom date range use the same aggregated API.

## Recovery states

- `normal`: utility power is healthy and no outage is open.
- `outage`: confirmed `OB`; recovery timers are cleared.
- `recovery_pending`: confirmed `OL`; NUPSON is timing a continuous period of
  healthy utility power (it observes continuity; it does not stabilize power).
- `waiting_for_charge`: power continuity is confirmed; NUPSON waits until the
  configured minimum battery charge is reached.
- `waking`: eligible hosts are processed in waves.

During `waking`, NUPSON first checks the configured host address with one ICMP
echo request. If the host is offline, it sends Wake-on-LAN packets at the
configured interval. After the last packet it continues sending individual
ICMP checks for `wake_verification_seconds`. The result is recorded and
notified as confirmed online, unreachable, or sent without verification when
the host has no address. NUPSON does not probe service ports for this check.

`OB`, `STALE`, `COMMLOST`, or an unknown status during recovery cancels the
timer. An outage and its wake queue are persisted. A process restart never
creates a new outage and conservatively restarts the full power-continuity timer.
While an outage remains active, NUPSON emits a periodic webhook reminder with
the current battery charge, UPS runtime estimate, load, power and elapsed outage
time. The interval defaults to 15 minutes and is configurable in Automation.

## Security boundaries

NUPSON does not mount the Docker socket and cannot shut down its host. The web
administrator password is stored with scrypt. Session cookies are HTTP-only and
same-site; mutating requests enforce same-origin checks. NUT configuration files
containing passwords are mode 0600.

USB access is limited by three layers: the host udev rule assigns only the UPS
to a dedicated group, Compose passes that numeric `NUPSON_USB_GID`, and the
device cgroup admits only USB character-device major 189. The entrypoint mirrors
that one group inside the container and grants it to the unprivileged `nut`
account. It does not discover or add unrelated host USB groups. Network access
must additionally be limited with the host firewall.
