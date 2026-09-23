# Installation and operations

## Requirements

- a Linux host with Docker Engine and Docker Compose v2;
- a USB UPS supported by Network UPS Tools;
- permission to create a host group and udev rule;
- UDP broadcast connectivity to the Wake-on-LAN network;
- a persistent local directory for `/data`.

## 1. Identify the UPS

Connect the UPS and run:

```bash
lsusb
```

A line similar to the following contains the vendor ID (`0764`) and product ID
(`0501`):

```text
Bus 003 Device 007: ID 0764:0501 Cyber Power System, Inc. UPS
```

Use the values reported for your device. Do not copy the example IDs unless
they match.

## 2. Create a dedicated USB group

Create a system group on the Docker host:

```bash
sudo groupadd --system nupson-usb
```

If the group already exists, `groupadd` will report that fact and no further
action is required. Print the group record:

```bash
getent group nupson-usb
```

For example:

```text
nupson-usb:x:995:
```

The third field (`995` in this example) is the GID. It is host-specific; never
assume that the example value is correct on another machine.

## 3. Add the udev rule

Create `/etc/udev/rules.d/99-nupson-ups.rules` as root:

```bash
sudoedit /etc/udev/rules.d/99-nupson-ups.rules
```

Add one line with the actual vendor and product IDs from `lsusb`:

```udev
SUBSYSTEM=="usb", ATTR{idVendor}=="0764", ATTR{idProduct}=="0501", GROUP="nupson-usb", MODE="0660"
```

Reload the rules and retrigger udev:

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Physically disconnect and reconnect the UPS USB cable. Run `lsusb` again to get
the current bus and device numbers, then check the device node. For bus `003`
and device `007`:

```bash
ls -l /dev/bus/usb/003/007
```

The expected ownership and mode are:

```text
crw-rw---- 1 root nupson-usb ... /dev/bus/usb/003/007
```

If the group is still `root` or the mode is not `0660`, verify the IDs in the
rule, reload udev, and reconnect the cable before starting NUPSON.

## 4. Configure NUPSON

Copy the environment template:

```bash
cp .env.example .env
```

Set `NUPSON_USB_GID` to the actual numeric GID:

```dotenv
NUPSON_USB_GID=995
```

NUPSON passes that numeric group into the container, creates an equivalent
group there, and makes the unprivileged `nut` account a member. The entrypoint
does not modify host device ownership and does not grant access to unrelated
USB device groups.

Other common settings are:

```dotenv
NUPSON_HTTP_PORT=8480
NUPSON_MANAGE_NUT=true
NUPSON_DEMO=false
# NUPSON_IMAGE=ghcr.io/OWNER/REPOSITORY:1.2.3
```

No additional third-party appliance variables are required; NUPSON uses only
the settings documented in `.env.example`.

## 5. Start the service

Build locally and start:

```bash
docker compose up -d --build
docker compose logs -f nupson
```

Alternatively, set `NUPSON_IMAGE` in `.env` to a published immutable image and
run `docker compose pull && docker compose up -d` without `--build`.

Open `http://HOST:8480` or the port selected in `.env`. The first visit requires
creation of an administrator account. There is no default password.

The service uses host networking so Wake-on-LAN broadcasts can reach the
physical LAN. TCP 3493 is served directly by NUT.

## USB security model

The Compose file:

- bind-mounts `/dev/bus/usb` so reconnecting a device does not leave a stale
  device-node mapping;
- allows only USB character-device major 189 through the device cgroup;
- adds only the configured `NUPSON_USB_GID`;
- mounts `/run/udev` read-only;
- enables `no-new-privileges`;
- does not use `privileged: true`, the Docker socket, or `CAP_SYS_BOOT`.

If the host has no `/run/udev`, remove only that read-only mount. USB access
still uses `/dev/bus/usb`.

## Verification and troubleshooting

Show the effective Compose configuration:

```bash
docker compose config
```

Check the supplementary groups visible to the application account:

```bash
docker compose exec nupson id nut
```

The output must include the numeric `NUPSON_USB_GID`. Check the mapped device:

```bash
docker compose exec nupson sh -c 'ls -l /dev/bus/usb/*/*'
```

If automatic scanning does not find the UPS:

1. confirm that `lsusb` sees it on the host;
2. confirm that its device node is `root:nupson-usb` with mode `0660`;
3. confirm that `.env` contains the exact GID from `getent group nupson-usb`;
4. recreate the container after changing the GID with `docker compose up -d --force-recreate`;
5. inspect `docker compose logs nupson` for a GID or driver warning.

## Wake-on-LAN networking

Configure the target subnet broadcast address for each host, for example
`192.168.10.255`. Switches, routers, DNS, and storage required during recovery
should also be UPS-protected.

## Firewall

- allow the dashboard port (8480 by default) only from the management network;
- allow TCP 3493 only from systems protected by this UPS;
- allow outbound UDP port 9 to the configured LAN broadcasts;
- never expose NUT or the dashboard directly to the Internet.

## Backup and restore

All persistent state lives under `./data`, including credentials. Stop the
container before a filesystem backup:

```bash
docker compose stop nupson
tar -czf nupson-backup.tgz data
docker compose start nupson
```

Store the archive securely and never commit it. To restore, stop NUPSON,
replace `data/` with the backup, and start the service again.

## Upgrade

Back up `data/`, select a new immutable image version or pull new source, and
recreate the container:

```bash
docker compose pull
docker compose up -d
```

Database migrations are automatic and additive.

## Health endpoints

- `/healthz` confirms that the web process is alive;
- `/readyz` returns HTTP 200 only when the UPS connection is healthy.

## Demo mode

Demo mode needs no USB GID when the image is run directly:

```bash
docker run --rm -p 8480:8080 \
  -e NUPSON_DEMO=true \
  -e NUPSON_MANAGE_NUT=false \
  ghcr.io/OWNER/REPOSITORY:latest
```

The supplied hardware `compose.yaml` intentionally requires
`NUPSON_USB_GID`. Demo mode must not protect real systems.

## Incus USB passthrough

On the physical Incus host, identify the vendor and product IDs and attach the
device to the instance that runs Docker:

```bash
incus config device add INSTANCE-NAME ups-usb usb vendorid=051d productid=0002
```

Replace the instance name and IDs. Inside the instance, repeat the group and
udev setup described above, then verify `lsusb` and `/dev/bus/usb` before
starting NUPSON.
