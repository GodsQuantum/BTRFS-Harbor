#!/bin/sh
set -eu

systemctl daemon-reload || true
systemctl reload dbus.service 2>/dev/null || true

exit 0
