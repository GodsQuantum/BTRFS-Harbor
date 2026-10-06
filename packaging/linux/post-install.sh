#!/bin/sh
set -eu

install -d -m 0750 /etc/btrfs-harbor
install -d -m 0700 /etc/btrfs-harbor/credentials /var/lib/btrfs-harbor/generated /var/log/btrfs-harbor

systemctl daemon-reload || true
systemctl reload dbus.service 2>/dev/null || true
systemctl enable --now btrfs-harbor-agent.service || true

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
fi

exit 0
