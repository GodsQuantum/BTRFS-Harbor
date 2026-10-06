#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OUT=${1:-"$ROOT/packaging/generated/btrfs-backup-ng.pyz"}
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT INT TERM

mkdir -p "$(dirname -- "$OUT")"

python3 -m pip install \
  --disable-pip-version-check \
  --no-cache-dir \
  --no-compile \
  --target "$STAGE" \
  "$ROOT"

python3 -m zipapp "$STAGE" \
  --python "/usr/bin/env python3" \
  --main "btrfs_backup_ng.__main__:main" \
  --output "$OUT"

chmod 0755 "$OUT"
python3 "$OUT" --help >/dev/null
printf 'Built %s\n' "$OUT"
