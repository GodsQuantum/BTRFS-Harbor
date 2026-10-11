#!/usr/bin/env bash
set -euo pipefail
repo="$(cd "$(dirname "$0")/../.." && pwd)"
temp="$(mktemp -d)"
server_pid=""
cleanup() { if [ -n "$server_pid" ]; then kill "$server_pid" 2>/dev/null || true; fi; rm -rf "$temp"; }
trap cleanup EXIT
cp -a "$repo/harbor/desktop/build/." "$temp/"
python3 - "$temp/index.html" <<'PYINJECT'
from pathlib import Path
import sys
p=Path(sys.argv[1])
s=p.read_text()
script="""<script>
globalThis.isTauri = true;
window.__TAURI_INTERNALS__ = {
  invoke: async (cmd) => {
    if (cmd === 'installation_state') return JSON.stringify({
      portable_appimage: true, helper_installed: false,
      service_unit_installed: false, service_available: false
    });
    if (cmd === 'system_identity') return JSON.stringify({
      hostname: 'ci-disposable', pretty_name: 'CI Linux',
      architecture: 'x86_64', root_fs: 'btrfs'
    });
    if (cmd === 'profiles') throw new Error('No Btrfs Harbor profile is configured.');
    if (cmd === 'configuration') throw new Error('/etc/btrfs-harbor/harbor.toml missing');
    if (cmd === 'portable_configuration') return null;
    throw new Error('CI mock unavailable command: ' + cmd);
  }
};
</script>"""
assert '</head>' in s
p.write_text(s.replace('</head>',script+'</head>',1))
PYINJECT
python3 -m http.server 8765 --bind 127.0.0.1 --directory "$temp" >"$temp/server.log" 2>&1 &
server_pid=$!
sleep 1
chrome="$(command -v google-chrome || command -v google-chrome-stable || command -v chromium || true)"
test -n "$chrome" || { echo "Chrome/Chromium not found on runner"; exit 1; }
"$chrome" --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage \
  --user-data-dir="$temp/chrome-profile" --virtual-time-budget=8000 \
  --dump-dom 'http://127.0.0.1:8765/' >"$temp/rendered.html" 2>"$temp/chrome.log" || {
    tail -25 "$temp/chrome.log"; exit 1;
  }
grep -Fq 'id="harbor-source-setup"' "$temp/rendered.html" || {
  echo 'First-run setup not rendered'; tail -25 "$temp/chrome.log"; exit 1;
}
grep -Fq 'data-testid="backup-now-primary"' "$temp/rendered.html" || {
  echo 'Backup-now button MISSING in rendered first-run setup'; exit 1;
}
grep -Fq 'Back up now' "$temp/rendered.html" || {
  echo 'Backup-now text MISSING in rendered first-run setup'; exit 1;
}
echo "PASS: real Chromium setup DOM renders primary Back up now button."
