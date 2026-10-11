#!/usr/bin/python3
\"\"\"Assert the *native* Tauri window exposes its primary backup CTA.

Runs only on an isolated CI virtual desktop; never starts any backup.
\"\"\"
import sys
import time

import pyatspi


def accessible_buttons():
    desktop = pyatspi.Registry.getDesktop(0)
    stack = [(child, 0) for child in desktop]
    names = []
    visited = 0
    while stack and visited < 12000:
        obj, depth = stack.pop()
        visited += 1
        try:
            name = (obj.name or '').strip()
            role = obj.getRoleName().lower()
            if role in ('push button', 'button') and name:
                names.append(name)
            if depth < 26:
                stack.extend((child, depth + 1) for child in obj)
        except (RuntimeError, LookupError, TypeError):
            continue
    return names


for _ in range(40):
    choices = accessible_buttons()
    if any('back up now' in item.casefold() for item in choices):
        print('NATIVE GUI PASS: accessible Back up now button in real Tauri first-run window.')
        sys.exit(0)
    time.sleep(0.5)

print('Native GUI backup action missing; accessible buttons:', choices[:50], file=sys.stderr)
sys.exit(1)
