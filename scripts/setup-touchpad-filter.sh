#!/usr/bin/env bash
# One-shot setup for the evdev touchpad filter:
#   - installs python-evdev
#   - installs a udev rule that grants the active-session user access to the
#     touchpad event device via systemd-logind's uaccess ACL mechanism
#   - reloads udev so the ACL takes effect on the live session (no re-login)
#
# Run with: sudo ./scripts/setup-touchpad-filter.sh

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "must be run as root (try: sudo $0)" >&2
    exit 1
fi

RULE_PATH="/etc/udev/rules.d/70-touchpad-uaccess.rules"

echo "==> installing python-evdev"
pacman -S --needed --noconfirm python-evdev

echo "==> writing udev rule to $RULE_PATH"
cat >"$RULE_PATH" <<'EOF'
# Grant the active session user ACL access to touchpad event devices so the
# Hyprland touchpad filter daemon can read raw evdev events without being in
# the 'input' group.
KERNEL=="event*", ATTRS{name}=="*Touchpad*", TAG+="uaccess"
EOF

echo "==> reloading udev and triggering input subsystem"
udevadm control --reload
udevadm trigger --subsystem-match=input --action=change

echo "==> verifying ACL on touchpad event nodes"
shopt -s nullglob
for n in /dev/input/by-path/*-event-mouse /dev/input/by-path/*touchpad*event*; do
    real=$(readlink -f "$n")
    echo "  $real"
    getfacl --omit-header "$real" 2>/dev/null | sed 's/^/    /'
done

echo "done."
