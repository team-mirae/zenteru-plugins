#!/usr/bin/env python3
"""Build the live channel: one tarball per plugin, zenman, and index.json.

    python3 scripts/build_live.py dist [--commit SHA]

A plugin is any top-level directory with a plugin.json. Archives are
deterministic (sorted, fixed mtimes and owners, gzip mtime 0), so an
unchanged plugin keeps its checksum and `zenman update` leaves it alone.

Also checks that every copy of shared/*.py inside a plugin is identical to
the canonical one.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {"tests", "__pycache__", ".pytest_cache"}
SKIP_FILES = {".DS_Store"}
SKIP_SUFFIXES = (".pyc", ".pyo")
VERSION_RE = re.compile(r"\d+\.\d+\.\d+([.-][0-9A-Za-z.-]+)?\Z")
NAME_RE = re.compile(r"[a-z][a-z0-9_-]*\Z")
EPOCH = 1700000000


def plugin_dirs() -> list[str]:
    return sorted(
        d for d in os.listdir(ROOT)
        if not d.startswith(".") and os.path.isfile(os.path.join(ROOT, d, "plugin.json"))
    )


def check_shared() -> list[str]:
    errors = []
    shared = os.path.join(ROOT, "shared")
    for f in sorted(os.listdir(shared)):
        if not f.endswith(".py"):
            continue
        with open(os.path.join(shared, f), "rb") as fh:
            canon = fh.read()
        for d in plugin_dirs():
            copy = os.path.join(ROOT, d, d, f)
            if os.path.exists(copy):
                with open(copy, "rb") as fh:
                    if fh.read() != canon:
                        errors.append(f"{d}/{d}/{f} differs from shared/{f} (run scripts/sync_shared.py)")
    return errors


def files_of(d: str) -> list[str]:
    out = []
    base = os.path.join(ROOT, d)
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.startswith("."))
        for f in sorted(files):
            if f in SKIP_FILES or f.endswith(SKIP_SUFFIXES):
                continue
            out.append(os.path.relpath(os.path.join(dirpath, f), ROOT))
    return sorted(out)


def tarball(d: str) -> tuple[bytes, int]:
    buf = io.BytesIO()
    files = files_of(d)
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=9) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tf:
            dirs = sorted({os.path.dirname(f) for f in files} | {d})
            all_dirs = set()
            for x in dirs:
                parts = x.split(os.sep)
                for i in range(1, len(parts) + 1):
                    all_dirs.add("/".join(parts[:i]))
            for x in sorted(all_dirs):
                ti = tarfile.TarInfo(x)
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
                ti.mtime = EPOCH
                tf.addfile(ti)
            for f in files:
                path = os.path.join(ROOT, f)
                with open(path, "rb") as fh:
                    data = fh.read()
                ti = tarfile.TarInfo(f.replace(os.sep, "/"))
                ti.size = len(data)
                ti.mode = 0o755 if os.access(path, os.X_OK) else 0o644
                ti.mtime = EPOCH
                tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue(), len(files)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def zenman_version(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        m = re.search(r'^__version__ = "([^"]+)"', f.read(), re.M)
    if not m:
        raise SystemExit("zenman/zenman has no __version__")
    return m.group(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--commit", default=None)
    args = ap.parse_args()

    errors = check_shared()
    plugins = []
    os.makedirs(args.out, exist_ok=True)
    for d in plugin_dirs():
        with open(os.path.join(ROOT, d, "plugin.json"), encoding="utf-8") as f:
            m = json.load(f)
        if m.get("name") != d:
            errors.append(f"{d}/plugin.json: name {m.get('name')!r} must match the directory")
        if not NAME_RE.match(d):
            errors.append(f"{d}: plugin names are lowercase letters, digits, - and _")
        if not VERSION_RE.match(str(m.get("version", ""))):
            errors.append(f"{d}/plugin.json: version {m.get('version')!r} is not x.y.z")
        if not os.path.isfile(os.path.join(ROOT, d, m.get("entry", "main.py"))):
            errors.append(f"{d}: entry {m.get('entry', 'main.py')} is missing")
        data, n = tarball(d)
        asset = f"{d}.tar.gz"
        with open(os.path.join(args.out, asset), "wb") as f:
            f.write(data)
        plugins.append({
            "name": d,
            "title": m.get("title", d),
            "version": m.get("version"),
            "description": m.get("description", ""),
            "asset": asset,
            "sha256": sha(data),
            "size": len(data),
            "files": n,
        })
    if errors:
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        return 1

    zpath = os.path.join(ROOT, "zenman", "zenman")
    with open(zpath, "rb") as f:
        zdata = f.read()
    with open(os.path.join(args.out, "zenman"), "wb") as f:
        f.write(zdata)
    with open(os.path.join(ROOT, "install.sh"), "rb") as f:
        idata = f.read()
    with open(os.path.join(args.out, "install.sh"), "wb") as f:
        f.write(idata)

    commit = args.commit
    if commit is None:
        try:
            commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                                    check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            commit = "unknown"
    index = {
        "schema": 1,
        "repo": "team-mirae/zenteru-plugins",
        "channel": "live",
        "commit": commit,
        "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "zenman": {"version": zenman_version(zpath), "asset": "zenman", "sha256": sha(zdata), "size": len(zdata)},
        "plugins": plugins,
    }
    with open(os.path.join(args.out, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)
        f.write("\n")
    for p in plugins:
        print(f"{p['name']:<10} {p['version']:<8} {p['files']:>3} files  {p['size']:>7} B  {p['sha256'][:12]}")
    print(f"zenman     {index['zenman']['version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
