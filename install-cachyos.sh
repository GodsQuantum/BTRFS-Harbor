#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/GodsQuantum/btrfs-harbor.git"
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PKGBUILD_SOURCE="$ROOT_DIR/packaging/arch/PKGBUILD"

if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
  printf '%s\n' "Do not run this installer as root. Run it as your normal desktop user."
  exit 1
fi

if ! command -v pacman >/dev/null 2>&1; then
  printf '%s\n' "This installer targets Arch Linux and CachyOS."
  printf '%s\n' "Other Linux distributions can build from source; see README.md."
  exit 1
fi

if [[ ! -r "$PKGBUILD_SOURCE" ]]; then
  printf '%s\n' "Cannot find packaging/arch/PKGBUILD."
  printf '%s\n' "Clone $REPO_URL and run ./install-cachyos.sh from the repository."
  exit 1
fi

printf '%s\n' "==> Installing Arch build prerequisites"
sudo pacman -S --needed base-devel git

BUILD_DIR="$(mktemp -d --tmpdir btrfs-harbor-build.XXXXXX)"
cleanup() {
  rm -rf -- "$BUILD_DIR"
}
trap cleanup EXIT INT TERM

install -m 0644 "$PKGBUILD_SOURCE" "$BUILD_DIR/PKGBUILD"

printf '%s\n' "==> Building and installing Btrfs Harbor with makepkg"
(
  cd "$BUILD_DIR"
  makepkg --syncdeps --install --clean --cleanbuild
)

printf '%s\n' "==> Enabling the native Btrfs Harbor system service"
sudo systemctl daemon-reload
sudo systemctl enable --now btrfs-harbor-agent.service

printf '\n%s\n' "Btrfs Harbor is installed."
printf '%s\n' "Launch it from your application menu or run: btrfs-harbor"
printf '%s\n' "Configuration is created from the GUI and stored under /etc/btrfs-harbor."
