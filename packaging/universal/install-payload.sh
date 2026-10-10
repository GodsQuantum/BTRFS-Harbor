#!/bin/sh
set -eu

PAYLOAD=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
NO_DEPS=0

for arg in "$@"; do
  case "$arg" in
    --no-deps) NO_DEPS=1 ;;
    --help|-h)
      cat <<'EOF'
Btrfs Harbor universal installer

Usage: ./BTRFS-Harbor-*.run [--no-deps]

Installs Btrfs Harbor system integration and the native desktop application.
Supported target: x86_64 Linux with systemd and glibc.
EOF
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 2
      ;;
  esac
done

if [ "$(uname -s)" != "Linux" ] || [ "$(uname -m)" != "x86_64" ]; then
  echo "This installer currently supports x86_64 Linux only." >&2
  exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
  SELF="$PAYLOAD/install.sh"
  if command -v pkexec >/dev/null 2>&1; then
    exec pkexec "$SELF" "$@"
  elif command -v sudo >/dev/null 2>&1; then
    exec sudo "$SELF" "$@"
  fi
  echo "Root privileges are required. Re-run with sudo or install polkit/pkexec." >&2
  exit 1
fi

install_deps() {
  if [ "$NO_DEPS" -eq 1 ]; then
    return
  fi

  if command -v pacman >/dev/null 2>&1; then
    pacman -S --needed --noconfirm btrfs-progs dbus openssh polkit snapper util-linux zstd
  elif command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y btrfs-progs dbus openssh-client policykit-1 snapper util-linux zstd
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y btrfs-progs dbus openssh-clients polkit snapper util-linux zstd
  elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install btrfsprogs dbus-1 openssh polkit snapper util-linux zstd
  fi
}

missing_commands() {
  missing=""
  for cmd in btrfs dbus-send findmnt pkexec snapper ssh systemctl zstd; do
    command -v "$cmd" >/dev/null 2>&1 || missing="$missing $cmd"
  done
  printf '%s' "$missing"
}

missing=$(missing_commands)
if [ -n "$missing" ]; then
  echo "Installing required host tools:$missing"
  install_deps
  missing=$(missing_commands)
fi
if [ -n "$missing" ]; then
  echo "Missing required host commands:$missing" >&2
  echo "Install the corresponding Btrfs/systemd/polkit packages and retry." >&2
  exit 1
fi

for required in \
  BTRFS-Harbor.AppImage \
  btrfs-harbor-agent \
  btrfs-harborctl \
  btrfs-harbor-recovery \
  btrfs-backup-ng.pyz \
  btrfs-backup-ng \
  harbor-rear-restore \
  rear-local.conf \
  python/bin/python3 \
  btrfs-harbor-agent.service \
  io.github.GodsQuantum.BtrfsHarbor1.conf \
  io.github.GodsQuantum.BtrfsHarbor.policy \
  btrfs-harbor.desktop
do
  if [ ! -e "$PAYLOAD/$required" ]; then
    echo "Installer payload is incomplete: $required" >&2
    exit 1
  fi
done

PREFIX=/opt/btrfs-harbor
rm -rf "$PREFIX/app.new" "$PREFIX/python.new"
install -d -m 0755 "$PREFIX" "$PREFIX/app.new"

(
  cd "$PREFIX/app.new"
  "$PAYLOAD/BTRFS-Harbor.AppImage" --appimage-extract >/dev/null
)
rm -rf "$PREFIX/app"
mv "$PREFIX/app.new/squashfs-root" "$PREFIX/app"
rmdir "$PREFIX/app.new"

cp -a "$PAYLOAD/python" "$PREFIX/python.new"
rm -rf "$PREFIX/python"
mv "$PREFIX/python.new" "$PREFIX/python"

install -d -m 0755 /usr/lib/btrfs-harbor /usr/bin
install -m 0755 "$PAYLOAD/btrfs-harbor-agent" /usr/lib/btrfs-harbor/btrfs-harbor-agent
install -m 0755 "$PAYLOAD/btrfs-harborctl" /usr/bin/btrfs-harborctl
install -m 0755 "$PAYLOAD/btrfs-harbor-recovery" /usr/bin/btrfs-harbor-recovery
install -m 0755 "$PAYLOAD/btrfs-backup-ng.pyz" /usr/lib/btrfs-harbor/btrfs-backup-ng.pyz
install -m 0755 "$PAYLOAD/btrfs-backup-ng" /usr/lib/btrfs-harbor/btrfs-backup-ng
install -d -m 0755 /usr/lib/btrfs-harbor/recovery
install -m 0755 "$PAYLOAD/harbor-rear-restore" /usr/lib/btrfs-harbor/recovery/harbor-rear-restore
install -d -m 0755 /usr/share/btrfs-harbor/rear
install -m 0644 "$PAYLOAD/rear-local.conf" /usr/share/btrfs-harbor/rear/local.conf

cat >/usr/bin/btrfs-harbor <<'EOF'
#!/bin/sh
exec /opt/btrfs-harbor/app/AppRun "$@"
EOF
chmod 0755 /usr/bin/btrfs-harbor

install -d -m 0755 /usr/lib/systemd/system /usr/share/dbus-1/system.d /usr/share/polkit-1/actions /usr/share/applications
install -m 0644 "$PAYLOAD/btrfs-harbor-agent.service" /usr/lib/systemd/system/btrfs-harbor-agent.service
install -m 0644 "$PAYLOAD/io.github.GodsQuantum.BtrfsHarbor1.conf" /usr/share/dbus-1/system.d/io.github.GodsQuantum.BtrfsHarbor1.conf
install -m 0644 "$PAYLOAD/io.github.GodsQuantum.BtrfsHarbor.policy" /usr/share/polkit-1/actions/io.github.GodsQuantum.BtrfsHarbor.policy
install -m 0644 "$PAYLOAD/btrfs-harbor.desktop" /usr/share/applications/btrfs-harbor.desktop

for size in 32 64 128 256; do
  icon="$PAYLOAD/icons/${size}x${size}.png"
  if [ -f "$icon" ]; then
    install -Dm644 "$icon" "/usr/share/icons/hicolor/${size}x${size}/apps/btrfs-harbor.png"
  fi
done

install -d -m 0750 /etc/btrfs-harbor
install -d -m 0700 /etc/btrfs-harbor/credentials /var/lib/btrfs-harbor/generated /var/log/btrfs-harbor

systemctl daemon-reload
systemctl reload dbus.service 2>/dev/null || true
systemctl enable --now btrfs-harbor-agent.service

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
fi

echo "Btrfs Harbor installed successfully."
echo "Launch it from your application menu or run: btrfs-harbor"
