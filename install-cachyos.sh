#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
printf "%s\n" "install-cachyos.sh is deprecated; using the distro-neutral Linux installer." >&2
exec "$ROOT_DIR/install-linux.sh" "$@"
