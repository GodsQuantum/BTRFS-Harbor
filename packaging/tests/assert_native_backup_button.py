#!/usr/bin/python3
"""Native first-run GUI acceptance: locate *and activate* Back up now.

This runs only on a disposable CI runner with no configured backup source or
destination, so activating the CTA cannot start a production backup.
"""
import sys
import time

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

print("NATIVE PASS: real Tauri first-run window exposes Back up now button.", flush=True)
try:
    action = button.queryAction()
    supported = [action.getName(i).lower() for i in range(action.nActions)]
    activation = next((i for i, name in enumerate(supported) if name in ("click", "press", "activate")), 0)
    if not action.doAction(activation):
        raise RuntimeError(f"Native button action returned false: {supported}")
except Exception as exc:
    print(f"NATIVE FAIL: could not activate Back up now: {exc}", file=sys.stderr)
    sys.exit(1)

for _ in range(35):
    names = [name for _obj, _role, name in visible_accessibles()]
    if any("select btrfs volumes and a backup destination below" in name.casefold() for name in names):
        print("NATIVE PASS: click displayed configuration guidance, without starting a backup.", flush=True)
        sys.exit(0)
    time.sleep(0.25)
print("NATIVE FAIL: button clicked but configuration guidance did not render.", file=sys.stderr)
print("Accessible names:", names[:70], file=sys.stderr)
sys.exit(1)
