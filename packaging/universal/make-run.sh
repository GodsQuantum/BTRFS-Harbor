#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
  echo "Usage: $0 PAYLOAD_DIR OUTPUT.run" >&2
  exit 2
fi

PAYLOAD=$1
OUTPUT=$2
ARCHIVE=$(mktemp)
HEADER=$(mktemp)
trap 'rm -f "$ARCHIVE" "$HEADER"' EXIT INT TERM

test -x "$PAYLOAD/install.sh"

tar -C "$PAYLOAD" -czf "$ARCHIVE" .

cat >"$HEADER" <<'EOF'
#!/bin/sh
set -eu
SELF=$0
LINE=$(awk '/^__BTRFS_HARBOR_ARCHIVE_BELOW__$/ { print NR + 1; exit }' "$SELF")
if [ -z "$LINE" ]; then
  echo "Corrupt Btrfs Harbor installer." >&2
  exit 1
fi
TMP=$(mktemp -d)
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT INT TERM
tail -n +"$LINE" "$SELF" | tar -xzf - -C "$TMP"
"$TMP/install.sh" "$@"
exit $?
__BTRFS_HARBOR_ARCHIVE_BELOW__
EOF

cat "$HEADER" "$ARCHIVE" >"$OUTPUT"
chmod 0755 "$OUTPUT"
printf 'Built %s\n' "$OUTPUT"
