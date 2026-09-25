# Configuring protected NUT clients

NUPSON exposes the UPS on TCP 3493. Every server powered by that UPS should run
its own `upsmon` process as a secondary client. When the UPS reaches a critical
state, `upsmon` performs a controlled local shutdown.

The recommended path is **NUT Clients → Add client** in the NUPSON dashboard.
Enter a descriptive name, the client's exact IP address, and the NUPSON address
reachable from that machine. Select a policy and download the generated ZIP.
After `upsmon` connects, the profile changes from **Waiting** to **Connected**.

Regenerate and reinstall the bundle whenever the dashboard marks its
configuration as outdated. Connection status alone cannot prove that a remote
machine has installed the newest files.

The examples below use:

- UPS name: `nutdev1`;
- NUPSON server: `192.168.1.10`;
- NUT user: `upsmon`;
- the password selected while configuring the UPS in NUPSON.

Use a fixed address for NUPSON. If DNS is used, its infrastructure must remain
available during a power failure.

## Windows client service

Select **Windows 10/11 / Windows Server** in a client profile to generate an
unattended Windows service bundle. It uses the official NUT 2.8.5 Windows
runtime in <code>MODE=netclient</code>; no interactive WinNUT session and no
logged-in user are required.

Extract the generated ZIP, open PowerShell as Administrator in that directory,
and start the local management menu:

    powershell.exe -ExecutionPolicy Bypass -File .\nupson-client.ps1

The menu provides installation, standard and extended diagnostics, update, and
uninstall actions. It executes only the scripts included in the same generated
bundle. The individual scripts remain available for direct use:

    powershell.exe -ExecutionPolicy Bypass -File .\install-nupson-client.ps1
    powershell.exe -ExecutionPolicy Bypass -File .\test-nupson-client.ps1

The diagnostic distinguishes a one-off read with <code>upsc.exe</code> from an
active <code>upsmon.exe</code> session. NUPSON marks the profile as connected
only when the server reports that persistent session through
<code>LIST CLIENT</code> and its source address matches the client profile.
A successful UPS status query alone does not confirm shutdown monitoring.

The installer downloads the pinned
<code>NUT-for-Windows-x86_64-RELEASE-2.8.5-1-fixNSS.7z</code> release, verifies
its SHA-256 digest, installs the complete runtime, protects the configuration
ACL, and registers **Network UPS Tools** with delayed automatic startup and
service recovery. Windows <code>tar.exe</code> is used to unpack the archive;
if that Windows version cannot read 7-Zip archives, an installed copy of 7-Zip
is used as a fallback.

Windows policies map to these actions:

- <code>critical</code>: standard <code>upsmon</code> shutdown on
  <code>LOWBATT</code> or <code>FSD</code>;
- <code>timer</code>: <code>ONBATT</code> schedules
  <code>shutdown.exe</code> with the configured delay and <code>ONLINE</code>
  cancels only the shutdown marked as created by NUPSON;
- <code>immediate</code>: <code>ONBATT</code> requests shutdown with zero
  delay;
- <code>monitor_only</code>: the UPS is monitored with power value zero and no
  event handler is installed.

The Windows timer intentionally does not use <code>upssched</code>. It is the
stable NUPSON backend and is recorded as <code>windows-native-timer</code> in
<code>nupson-client.json</code>. A future NUT upgrade does not change that
backend automatically.

A critical UPS state may shut down a timer-policy client before its timer
expires. If communication is lost after the UPS was known to be on battery,
the pending timer continues instead of assuming that utility power returned.
The timer marker records the Windows boot time and expiry. A marker left by a
completed shutdown or an interrupted timer is discarded on the next ONBATT
event instead of blocking shutdown after Windows starts again.

Run a safe handler check without scheduling a real shutdown:

    powershell.exe -ExecutionPolicy Bypass -File .\test-nupson-client.ps1 -DryRunEvents

NUT and its configuration are installed under
<code>C:\NUPSON\NUT</code>. The NUPSON policy manifest, event handler, timer
marker, and local log are stored under
<code>%ProgramData%\NUPSON\Client</code>. NUT messages are available in
Windows Event Viewer and timer actions are recorded in
<code>client-events.log</code>.

To update from a newly generated bundle:

    powershell.exe -ExecutionPolicy Bypass -File .\update-nupson-client.ps1

To uninstall:

    powershell.exe -ExecutionPolicy Bypass -File .\uninstall-nupson-client.ps1

The downloaded configuration contains the NUT client password. Delete the
extracted ZIP contents after successful installation and protect any backups.

## 1. Verify network access

The protected server must reach TCP 3493 on NUPSON:

```bash
nc -vz 192.168.1.10 3493
```

Allow TCP 3493 only from protected clients. Do not expose it to the Internet.
The switches and routers between the client and NUPSON must also be powered by
the UPS.

## 2. Install the NUT client

Debian, Ubuntu, and Proxmox VE:

```bash
sudo apt update
sudo apt install nut-client
```

Fedora, RHEL, Rocky Linux, and AlmaLinux:

```bash
sudo dnf install nut-client
```

Alpine Linux:

```bash
sudo apk add nut
```

Check the version:

```bash
upsmon -V
```

NUT 2.8 and later use the term `secondary`. NUT 2.7 and earlier use the legacy
term `slave` in the same `MONITOR` position.

The paths and systemd commands below target Debian-family systems. RHEL-family
packages may use `/etc/ups` instead of `/etc/nut` and a different unit name.

## 3. Query NUPSON before enabling shutdown

```bash
upsc -l 192.168.1.10:3493
upsc nutdev1@192.168.1.10:3493
```

The first command should list `nutdev1`; the second should return values such as:

```text
ups.status: OL
battery.charge: 100
ups.load: 15
```

`Init SSL without certificate database` is informational. `Connection refused`
or a timeout indicates networking/firewall trouble. `ERR UNKNOWN-UPS` usually
means that the UPS name is wrong.

## 4. Enable network-client mode

Edit `/etc/nut/nut.conf`:

```bash
sudoedit /etc/nut/nut.conf
```

Set:

```conf
MODE=netclient
```

## 5. Configure upsmon

Edit `/etc/nut/upsmon.conf`:

```bash
sudoedit /etc/nut/upsmon.conf
```

For NUT 2.8 or later, use:

```conf
MONITOR nutdev1@192.168.1.10:3493 1 upsmon YOUR_NUT_PASSWORD secondary
MINSUPPLIES 1
SHUTDOWNCMD "/usr/sbin/shutdown -h now"
POLLFREQ 5
POLLFREQALERT 2
DEADTIME 25
FINALDELAY 5

NOTIFYFLAG ONLINE SYSLOG+WALL
NOTIFYFLAG ONBATT SYSLOG+WALL
NOTIFYFLAG LOWBATT SYSLOG+WALL
NOTIFYFLAG FSD SYSLOG+WALL
NOTIFYFLAG COMMOK SYSLOG
NOTIFYFLAG COMMBAD SYSLOG+WALL
```

For NUT 2.7, replace only the last word of `MONITOR`:

```conf
MONITOR nutdev1@192.168.1.10:3493 1 upsmon YOUR_NUT_PASSWORD slave
```

Important values:

- power value `1` means this UPS supplies one required power source;
- `MINSUPPLIES 1` keeps the system running while that source is available;
- `SHUTDOWNCMD` is executed for a critical UPS state or FSD;
- `DEADTIME 25` controls when lost communication is considered persistent;
- `FINALDELAY 5` adds a short delay before shutdown.

Verify the shutdown command path:

```bash
command -v shutdown
```

Use the returned path if it differs from `/usr/sbin/shutdown`. Protect the file
because it contains the NUT password:

```bash
sudo chown root:nut /etc/nut/upsmon.conf
sudo chmod 640 /etc/nut/upsmon.conf
```

Configuration files should normally be mode `0640`, not executable. Only the
generated `/usr/local/sbin/nupson-upssched` helper is installed as mode `0755`.

## Critical and early-shutdown policies

The standard `critical` policy needs no custom sudoers entry. `upsmon` uses its
small privileged process to execute `SHUTDOWNCMD` after LOWBATT or FSD.

NUPSON's `immediate` and `timer` policies act earlier on `ONBATT` through
`NOTIFYCMD` and `upssched`. Those notifications run as the unprivileged `nut`
account, so generated bundles for these policies also contain:

- `nupson-upssched`, installed as `root:root` mode `0755`;
- `nupson-nut-shutdown.sudoers`, installed as `root:root` mode `0440` under
  `/etc/sudoers.d/`;
- a non-interactive `sudo -n` call.

The rule authorizes only the exact shutdown command in the bundle. Validate it:

```bash
sudo visudo -cf /etc/sudoers.d/nupson-nut-shutdown
```

## 6. Start monitoring

On Debian, Ubuntu, and Proxmox VE:

```bash
sudo systemctl enable --now nut-monitor
sudo systemctl restart nut-monitor
sudo systemctl status nut-monitor --no-pager
```

Some distributions call the service `upsmon`. Find available units with:

```bash
systemctl list-unit-files | grep -E 'nut|upsmon'
```

Inspect the journal:

```bash
sudo journalctl -u nut-monitor -n 100 --no-pager
```

A successful connection includes messages similar to:

```text
UPS: nutdev1@192.168.1.10 (secondary) (power value 1)
Communications with UPS nutdev1@192.168.1.10 established
```

## 7. Safe communication test

Watch the reported state:

```bash
watch -n 2 'upsc nutdev1@192.168.1.10:3493 ups.status'
```

Disconnect only utility input to the UPS, not its USB cable or the protected
server. The state should change from `OL` to `OB`, then back to `OL` (often with
`CHRG`) when utility power returns.

Plain `OB` should not shut down a client using the `critical` policy. Shutdown
starts when the UPS reports a critical state, normally `OB LB`, or the primary
NUT server sends FSD.

Do not use `upsmon -c fsd` for a casual test. It can start a real shutdown of
protected machines.

## 8. Controlled shutdown test

Perform the first full test on site during a maintenance window:

1. save data and stop sensitive workloads;
2. confirm that the client sees `ups.status: OL`;
3. follow the logs with `sudo journalctl -fu nut-monitor`;
4. disconnect UPS utility input and monitor the battery;
5. confirm that a critical state starts an orderly shutdown;
6. restore utility power;
7. confirm that NUPSON first verifies continuous power, then waits for the
   configured battery reserve, then checks and wakes only unavailable hosts.

Choose a recovery delay long enough for network, storage, and other dependencies
to become ready before dependent servers start.

## Troubleshooting

### Authentication failure

The user and password must match the credentials saved during UPS configuration
in NUPSON. If the password is lost, save a new one in NUPSON and update every
client bundle.

### Driver not connected or data stale

The problem is between NUPSON and the UPS. Check the dashboard, USB permissions,
cable, and container logs. This is not a secondary-client configuration error.

### UPS unavailable or communication lost

```bash
ping -c 3 192.168.1.10
nc -vz 192.168.1.10 3493
upsc nutdev1@192.168.1.10:3493
```

### Monitoring works but shutdown does not

Check the `SHUTDOWNCMD` path, `upsmon` privileges, sudoers validation for early
policies, and system logs. Distribution NUT packages normally provide the
privileged shutdown integration; custom containers may require additional host
integration.

## Upstream documentation

- [upsmon.conf](https://networkupstools.org/docs/man/upsmon.conf.html)
- [upsmon](https://networkupstools.org/docs/man/upsmon.html)
- [nut.conf](https://networkupstools.org/docs/man/nut.conf.html)
