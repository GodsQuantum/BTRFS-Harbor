#!/bin/sh
set -eu

systemctl disable --now btrfs-harbor-agent.service >/dev/null 2>&1 || true

exit 0
