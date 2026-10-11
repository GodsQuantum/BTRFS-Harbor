#!/usr/bin/python3
"""Native first-run GUI acceptance: locate *and activate* Back up now.

This runs only on a disposable CI runner with no configured backup source or
destination, so activating the CTA cannot start a production backup.
"""
import sys
import time
import subprocess

import pyatspi


def visible_accessibles():
    desktop = pyatspi.Registry.getDesktop(0)
    stack = [(child, 0) for child in desktop]
    visited = 0
    while stack and visited < 15000:
        obj, depth = stack.pop()
        visited += 1
        try:
            name = (obj.name or "").strip()
            role = obj.getRoleName().lower()
            if name:
                yield obj, role, name
            if depth < 30:
                stack.extend((child, depth + 1) for child in obj)
        except (RuntimeError, LookupError, TypeError):
            continue


def find_button():
    for obj, role, name in visible_accessibles():
        if role in ("push button", "button") and "back up now" in name.casefold():
            return obj
    return None


button = None
for _ in range(45):
    button = find_button()
    if button is not None:
        break
    time.sleep(0.5)

if button is None:
    print("NATIVE FAIL: Back up now inaccessible in first-run window", file=sys.stderr)
    sys.exit(1)

enabled = button.getState().contains(pyatspi.STATE_ENABLED)
print(f"NATIVE button exposed. Enabled: {enabled}", flush=True)
if not enabled:
    print("NATIVE FAIL: Back up now disabled and cannot be clicked.", file=sys.stderr)
    sys.exit(1)
print("NATIVE PASS: real Tauri first-run exposes an enabled Back up now button.", flush=True)
start = button.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
print(f"NATIVE CTA before click: y={start.y}, height={start.height}", flush=True)
try:
    action = button.queryAction()
    supported = [action.getName(i).lower() for i in range(action.nActions)]
    activation = next((i for i, name in enumerate(supported) if name in ("click", "press", "activate")), 0)
    if not action.doAction(activation):
        raise RuntimeError(f"Native button action returned false: {supported}")
except Exception as exc:
    print(f"NATIVE FAIL: could not activate Back up now: {exc}", file=sys.stderr)
    sys.exit(1)

def all_accessible_texts():
    values = []
    for obj, role, name in visible_accessibles():
        if name:
            values.append(name)
        try:
            text = obj.queryText()
            value = text.getText(0, min(text.characterCount, 2000)).strip()
            if value:
                values.append(value)
        except Exception:
            pass
    return values


def check_guidance_or_scroll():
    names = []
    for _ in range(18):
        names = all_accessible_texts()
        if any("select btrfs volumes and a backup destination below" in n.casefold() for n in names):
            return True, names, "configuration guidance displayed"
        try:
            current = button.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
            if abs(current.y - start.y) >= 60 and "Set up backups" in names:
                return True, names, f"scrolled from y={start.y} to y={current.y}"
        except Exception:
            pass
        time.sleep(0.25)
    return False, names, "no accessible guidance or scroll"


ok, names, evidence = check_guidance_or_scroll()
if ok:
    print(f"NATIVE PASS: first-run backup action activated: {evidence}", flush=True)
    sys.exit(0)

try:
    rect = button.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
    if rect.width <= 0 or rect.height <= 0:
        raise RuntimeError(f"Invalid native button extent: {rect}")
    x, y = rect.x + rect.width // 2, rect.y + rect.height // 2
    print(f"NATIVE retry: physical mouse click at ({x}, {y})", flush=True)
    subprocess.run(
        ["xdotool", "mousemove", "--sync", str(x), str(y), "click", "1"],
        check=True,
        timeout=8,
    )
except Exception as exc:
    print(f"NATIVE mouse click error: {exc}", file=sys.stderr)

ok, names, evidence = check_guidance_or_scroll()
if ok:
    print(f"NATIVE PASS: physical first-run click activated: {evidence}", flush=True)
    sys.exit(0)

print("NATIVE FAIL: no guidance or scroll after native and pointer clicks.", file=sys.stderr)
print("Accessible names/text:", names[:85], file=sys.stderr)
sys.exit(1)
