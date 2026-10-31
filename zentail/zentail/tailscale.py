"""Finding and driving the Tailscale CLI on macOS.

Based on Tailscale's CLI documentation (tailscale.com/kb/1080/cli, Tailscale
1.102). On macOS the CLI is the app's own binary,
``/Applications/Tailscale.app/Contents/MacOS/Tailscale``:

* run with ``TAILSCALE_BE_CLI=1`` it acts as the CLI (the documented way
  for scripts), and
* the app's Settings → CLI integration → "Install Now" adds a launcher at
  ``/usr/local/bin/tailscale``.

A plain symlink to the binary does **not** work: the app then aborts with
"The current bundleIdentifier is unknown to the registry", because it
locates its bundle from the path it was started as. zentail therefore puts
a two-line launcher (``exec`` of the real path) on the PATH, the same thing
"Install Now" does, without sudo.

zentail never installs Tailscale with Homebrew. Its CLI-only formula runs a
separate ``tailscaled`` that conflicts with the app.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Optional

APP_PATHS = ("/Applications/Tailscale.app", os.path.expanduser("~/Applications/Tailscale.app"))
APP_BINARY = "Contents/MacOS/Tailscale"
DOWNLOAD_URL = "https://tailscale.com/download/mac"
APP_STORE_URL = "macappstore://apps.apple.com/app/tailscale/id1475387142"
SEARCH_PATH = ("~/.local/bin", "/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin")
LAUNCHER_MARK = "# zentail launcher"


def app_binary() -> Optional[str]:
    for app in APP_PATHS:
        b = os.path.join(app, APP_BINARY)
        if os.access(b, os.X_OK):
            return b
    return None


def cli_on_path(extra: tuple[str, ...] = SEARCH_PATH) -> Optional[str]:
    path = os.environ.get("PATH", "")
    dirs = path.split(os.pathsep) + [os.path.expanduser(d) for d in extra]
    found = shutil.which("tailscale", path=os.pathsep.join(dict.fromkeys(d for d in dirs if d)))
    return found


@dataclass
class Cli:
    """How to run the CLI: ``argv0`` plus extra environment."""

    argv0: str
    env: dict[str, str] = field(default_factory=dict)
    #: "path" (a ``tailscale`` command exists) or "app" (the app binary directly).
    via: str = "path"


def find_cli() -> Optional[Cli]:
    override = os.environ.get("ZENTAIL_TAILSCALE")
    if override:
        return Cli(override, {"TAILSCALE_BE_CLI": "1"}, "path")
    p = cli_on_path()
    if p:
        real = os.path.realpath(p)
        if real.endswith("/" + APP_BINARY) and os.path.islink(p):
            # A raw symlink into the bundle crashes; run the real path instead.
            return Cli(real, {"TAILSCALE_BE_CLI": "1"}, "app")
        return Cli(p, {"TAILSCALE_BE_CLI": "1"}, "path")
    b = app_binary()
    if b:
        return Cli(b, {"TAILSCALE_BE_CLI": "1"}, "app")
    return None


async def run(cli: Cli, *args: str, timeout: float = 20) -> tuple[int, str]:
    """Run ``tailscale args…``; returns (exit status, stdout+stderr)."""
    env = dict(os.environ, **cli.env)
    try:
        p = await asyncio.create_subprocess_exec(
            cli.argv0, *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
        )
    except OSError as e:
        return 127, str(e)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        return 124, f"tailscale {' '.join(args)}: timed out after {timeout:.0f}s"
    except asyncio.CancelledError:
        p.kill()
        raise
    return p.returncode or 0, out.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# status


@dataclass
class Peer:
    id: str
    host: str
    dns: str
    ips: list[str]
    os: str
    online: bool
    active: bool = False
    exit_node: bool = False
    exit_option: bool = False
    relay: str = ""
    cur_addr: str = ""
    last_seen: str = ""
    rx: int = 0
    tx: int = 0
    is_self: bool = False

    @property
    def ipv4(self) -> str:
        return next((ip for ip in self.ips if ":" not in ip), self.ips[0] if self.ips else "")

    @property
    def short_dns(self) -> str:
        return self.dns.rstrip(".")

    @property
    def connection(self) -> str:
        if not self.online:
            return "offline"
        if self.cur_addr:
            return "direct"
        if self.relay:
            return f"relay {self.relay}"
        return "idle"


@dataclass
class Status:
    backend: str
    version: str = ""
    self_peer: Optional[Peer] = None
    peers: list[Peer] = field(default_factory=list)
    tailnet: str = ""
    magic_dns: str = ""
    health: list[str] = field(default_factory=list)
    auth_url: str = ""
    user: str = ""

    @property
    def running(self) -> bool:
        return self.backend == "Running"

    @property
    def exit_node(self) -> Optional[Peer]:
        return next((p for p in self.peers if p.exit_node), None)

    def signature(self) -> tuple:
        """Changes when something worth redrawing changes."""
        return (
            self.backend, self.tailnet, tuple(self.health),
            tuple(self.self_peer.ips) if self.self_peer else None,
            tuple((p.id, p.host, p.online, p.exit_node, p.exit_option, p.connection) for p in self.peers),
        )


def _peer(d: dict, is_self: bool = False) -> Peer:
    return Peer(
        id=str(d.get("ID") or d.get("PublicKey") or d.get("HostName")),
        host=d.get("HostName") or "",
        dns=d.get("DNSName") or "",
        ips=list(d.get("TailscaleIPs") or []),
        os=d.get("OS") or "",
        online=bool(d.get("Online", is_self)),
        active=bool(d.get("Active")),
        exit_node=bool(d.get("ExitNode")),
        exit_option=bool(d.get("ExitNodeOption")),
        relay=d.get("Relay") or "",
        cur_addr=d.get("CurAddr") or "",
        last_seen=d.get("LastSeen") or "",
        rx=int(d.get("RxBytes") or 0),
        tx=int(d.get("TxBytes") or 0),
        is_self=is_self,
    )


def parse_status(text: str) -> Status:
    d = json.loads(text)
    st = Status(backend=d.get("BackendState") or "Unknown", version=d.get("Version") or "")
    if d.get("Self"):
        st.self_peer = _peer(d["Self"], True)
    peers = [_peer(p) for p in (d.get("Peer") or {}).values()]
    st.peers = sorted(peers, key=lambda p: (not p.online, not p.exit_option, p.host.lower()))
    tn = d.get("CurrentTailnet") or {}
    st.tailnet = tn.get("Name") or ""
    st.magic_dns = tn.get("MagicDNSSuffix") or d.get("MagicDNSSuffix") or ""
    st.health = [str(h) for h in (d.get("Health") or [])]
    st.auth_url = d.get("AuthURL") or ""
    if st.self_peer is not None:
        uid = str((d.get("Self") or {}).get("UserID", ""))
        u = (d.get("User") or {}).get(uid) or {}
        st.user = u.get("LoginName") or ""
    return st


def parse_accounts(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    return [a for a in data if isinstance(a, dict) and a.get("id")] if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# putting `tailscale` on the PATH


def launcher_script(binary: str) -> str:
    return f'#!/bin/sh\n{LAUNCHER_MARK}: runs the Tailscale app\'s own CLI\nexec "{binary}" "$@"\n'


def launcher_status(dest: str, binary: str) -> str:
    """``missing``, ``ours`` (our launcher, same binary), ``ours-stale`` or ``other``."""
    if not os.path.lexists(dest):
        return "missing"
    if os.path.islink(dest):
        return "other"
    try:
        with open(dest, encoding="utf-8") as f:
            text = f.read(4096)
    except OSError:
        return "other"
    if LAUNCHER_MARK in text:
        return "ours" if text == launcher_script(binary) else "ours-stale"
    return "other"


def install_launcher(binary: str, bin_dir: str = "~/.local/bin", replace: bool = False) -> str:
    """Write the launcher; refuses to replace something that isn't ours."""
    d = os.path.expanduser(bin_dir)
    dest = os.path.join(d, "tailscale")
    state = launcher_status(dest, binary)
    if state == "other" and not replace:
        raise FileExistsError(dest)
    os.makedirs(d, exist_ok=True)
    tmp = dest + ".zentail-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(launcher_script(binary))
    os.chmod(tmp, 0o755)
    os.replace(tmp, dest)
    return dest


INTERACTIVE = {"login", "ssh", "up", "update", "web", "serve", "funnel", "nc", "configure", "lock", "bugreport"}
