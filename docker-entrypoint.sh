#!/bin/sh
set -eu

app_uid="${NUPSON_UID:-1000}"
app_gid="${NUPSON_GID:-1000}"

case "$app_uid" in
    ''|0|*[!0-9]*)
        echo "NUPSON_UID must be a positive numeric UID" >&2
        exit 1
        ;;
esac
case "$app_gid" in
    ''|0|*[!0-9]*)
        echo "NUPSON_GID must be a positive numeric GID" >&2
        exit 1
        ;;
esac

# Bind mounts retain host numeric ownership. Align the unprivileged NUT account
# with the host operator so persistent files do not appear to belong to an
# unrelated host service which happens to use the image's original UID/GID.
uid_owner="$(getent passwd "$app_uid" | cut -d: -f1 || true)"
gid_owner="$(getent group "$app_gid" | cut -d: -f1 || true)"
if [ -n "$uid_owner" ] && [ "$uid_owner" != "nut" ]; then
    echo "NUPSON_UID $app_uid is already used by container account $uid_owner" >&2
    exit 1
fi
if [ -n "$gid_owner" ] && [ "$gid_owner" != "nut" ]; then
    echo "NUPSON_GID $app_gid is already used by container group $gid_owner" >&2
    exit 1
fi

if [ "$(id -g nut)" != "$app_gid" ]; then
    groupmod --gid "$app_gid" nut
fi
if [ "$(id -u nut)" != "$app_uid" ]; then
    usermod --uid "$app_uid" --gid "$app_gid" nut
fi

mkdir -p /data /data/nut /run/nut
chown -R nut:nut /data /run/nut
if [ -d /var/lib/nut ]; then
    chown -R nut:nut /var/lib/nut
fi
chmod 0750 /data /data/nut /run/nut
if [ -f /data/nupson.db ]; then
    chmod 0600 /data/nupson.db
fi

# The host udev rule grants the UPS device to one dedicated group. Mirror that
# numeric GID in the container and grant it only to the unprivileged NUT user.
usb_gid="${NUPSON_USB_GID:-}"
case "$usb_gid" in
    '')
        echo "Warning: NUPSON_USB_GID is not set; USB UPS access is not configured" >&2
        ;;
    0|*[!0-9]*)
        echo "NUPSON_USB_GID must be a positive numeric GID" >&2
        exit 1
        ;;
    *)
        usb_group="$(getent group "$usb_gid" | cut -d: -f1 || true)"
        if [ -z "$usb_group" ]; then
            usb_group="nupson-usb"
            groupadd --system --gid "$usb_gid" "$usb_group"
        fi
        usermod --append --groups "$usb_group" nut

        if [ -d /dev/bus/usb ] && ! find /dev/bus/usb -type c -exec stat -c '%g' {} \; \
            2>/dev/null | grep -qx "$usb_gid"; then
            echo "Warning: no mapped USB device belongs to GID $usb_gid; check the host udev rule" >&2
        fi
        ;;
esac

umask 0077
exec gosu nut "$@"
