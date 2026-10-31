#!/usr/bin/env python3
"""Copy shared/*.py into every plugin package that carries a copy."""

import os
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARED = os.path.join(ROOT, "shared")

#: plugin -> shared modules it vendors
USERS = {
    "zenfinder": ["browserpanel.py"],
    "zenobfus": ["browserpanel.py"],
    "zentail": ["browserpanel.py"],
}

for plugin, mods in USERS.items():
    for m in mods:
        dst = os.path.join(ROOT, plugin, plugin, m)
        shutil.copyfile(os.path.join(SHARED, m), dst)
        print(f"{plugin}/{plugin}/{m}")
