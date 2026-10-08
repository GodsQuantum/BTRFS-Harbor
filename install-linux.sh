#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

shopt -s nullglob
candidates=(
  "$ROOT_DIR"/BTRFS-Harbor-*-linux-x86_64.run
  "$ROOT_DIR"/dist/BTRFS-Harbor-*-linux-x86_64.run
)
shopt -u nullglob

if (( ${#candidates[@]} > 0 )); then
  exec "${candidates[0]}" "$@"
fi

cat >&2 <<'EOF'
Btrfs Harbor uses the universal .run package for persistent Linux installation.

No universal .run package was found beside this source tree.
Download the current BTRFS-Harbor-*-linux-x86_64.run release artifact, make it
executable, then run it. The installer detects pacman, apt, dnf or zypper and
requires systemd only for persistent automatic scheduling.

The Portable AppImage does not need installation for manual backup/recovery.
EOF
exit 2
