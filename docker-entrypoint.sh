#!/bin/sh
set -eu

mkdir -p /data /data/nut /run/nut
chown -R nut:nut /data /run/nut

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

exec gosu nut "$@"
