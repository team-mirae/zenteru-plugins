"""zentail — Tailscale in a zenteru side panel.

Top: this machine, the tailnet and a toolbar (connect / disconnect, exit
node, netcheck, accounts). Then one row per peer with SSH, Ping, Copy IP
and "Use as exit node". Bottom: command output.

The panel refreshes every few seconds while it is open. Anything typed in
the input box runs as ``tailscale <what you typed>`` (``ping neo``,
``dns status``, ``get``); commands that need a terminal (``login``,
``ssh``, ``up`` …) open in a new tab instead.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Any, Optional

import zenteru
from zenteru import Command, CommandInvocation, Connection, Panel, RPCError

from . import tailscale as ts
from .browserpanel import BrowserPanel, Entry, human_size

log = logging.getLogger("zentail")

TITLE = "zentail"
REFRESH_SECONDS = 5
URL_RE = re.compile(r"https://\S+")

NOT_INSTALLED = f"""\
Tailscale isn't installed.

Get the Tailscale app for macOS from
  {ts.DOWNLOAD_URL}
(the Standalone download) or from the Mac App Store. zentail uses the
app's own CLI, so there is nothing else to install. Please don't use the
Homebrew formula: it runs a separate tailscaled that fights with the app.

After installing, open Tailscale once, log in, then press Check again."""


@dataclass
class View:
    window_id: Optional[str]
    panel: Panel
    bp: BrowserPanel
    visible: bool = True
    status: Optional[ts.Status] = None
    signature: Any = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    poller: Optional[asyncio.Task] = None


class ZenTail:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.views: dict[Optional[str], View] = {}
        self.cli: Optional[ts.Cli] = None
        self.commands = [
            Command("zentail: show / hide Tailscale", self.cmd_toggle, identifier="zentail.toggle", name="zentail"),
        ]

    async def async_register(self) -> None:
        for c in self.commands:
            await c.async_register(self.connection)

    # ------------------------------------------------------------------

    async def view_for(self, window_id: Optional[str]) -> View:
        v = self.views.get(window_id)
        if v is None or v.panel.closed:
            panel = await Panel.async_create(self.connection, TITLE, window_id=window_id)
            v = View(window_id, panel, BrowserPanel(panel, page_size=80))
            panel.on("*", lambda ev, v=v: self.on_event(v, ev))
            self.views[window_id] = v
        await v.panel.async_show()
        v.visible = True
        if v.poller is None or v.poller.done():
            v.poller = self.connection.spawn(self.poll(v))
        return v

    async def poll(self, v: View) -> None:
        while not v.panel.closed and v.visible:
            await asyncio.sleep(REFRESH_SECONDS)
            if v.bp.asking or v.lock.locked():
                continue
            try:
                await self.refresh(v)
            except (RPCError, zenteru.ConnectionClosed):
                return
            except Exception:
                log.exception("refresh failed")

    async def refresh(self, v: View, force: bool = False) -> None:
        async with v.lock:
            self.cli = ts.find_cli()
            if self.cli is None:
                if v.signature != "missing" or force:
                    v.signature = "missing"
                    v.status = None
                    await v.bp.render(NOT_INSTALLED, [("download", "Download page"), ("appstore", "Mac App Store"),
                                                      ("refresh", "Check again")], [], lambda e: [])
                    await v.panel.async_set_status("Tailscale not installed")
                return
            code, out = await ts.run(self.cli, "status", "--json")
            try:
                st = ts.parse_status(out)
            except ValueError:
                sig = ("error", out.strip()[:200])
                if v.signature != sig or force:
                    v.signature = sig
                    v.status = None
                    await v.bp.render(
                        f"Tailscale isn't answering.\n{out.strip()[:600]}\n\nIs the Tailscale app running?",
                        [("openapp", "Open Tailscale"), ("refresh", "Check again")], [], lambda e: [])
                    await v.panel.async_set_status("tailscale status failed")
                return
            v.status = st
            sig = st.signature()
            if sig == v.signature and not force:
                return
            v.signature = sig
            rows = [Entry(p.host, False, data=p) for p in st.peers]
            await v.bp.render(self.header(st), self.toolbar(st), rows, lambda e: self.peer_actions(st, e.data),
                              keep_preview=True, formatter=lambda e: self.peer_row(e.data))
            online = sum(1 for p in st.peers if p.online)
            await v.panel.async_set_status(f"{st.backend} · {online}/{len(st.peers)} peers online · {st.version.split('-')[0]}")

    # ------------------------------------------------------------------
    # drawing

    def header(self, st: ts.Status) -> str:
        lines = [f"Tailscale · {st.backend}"]
        me = st.self_peer
        if me is not None:
            lines.append(f"{me.host} · {'  '.join(me.ips[:2])}")
        if st.tailnet:
            lines.append(f"tailnet {st.tailnet}" + (f" · {st.user}" if st.user else ""))
        ex = st.exit_node
        lines.append(f"exit node: {ex.host} ({ex.ipv4})" if ex else "exit node: none")
        for h in st.health[:3]:
            lines.append(f"⚠ {h}")
        if st.auth_url:
            lines.append(f"Log in: {st.auth_url}")
        if self.cli is not None and self.cli.via == "app":
            lines.append("The `tailscale` command isn't on your PATH (zentail runs the app's CLI directly).")
        return "\n".join(lines)

    def toolbar(self, st: ts.Status) -> list[tuple[str, str]]:
        acts = [("refresh", "⟳ Refresh")]
        acts.append(("down", "Disconnect") if st.running else ("up", "Connect"))
        if st.exit_node is not None:
            acts.append(("exitoff", "Exit node off"))
        acts += [("netcheck", "Netcheck"), ("accounts", "Accounts")]
        if st.auth_url:
            acts.append(("login", "Open login page"))
        if self.cli is not None and self.cli.via == "app":
            acts.append(("link", "Add `tailscale` command"))
        acts.append(("help", "?"))
        return acts

    @staticmethod
    def peer_row(p: ts.Peer) -> str:
        dot = "●" if p.online else "○"
        tags = []
        if p.exit_node:
            tags.append("exit node ✓")
        elif p.exit_option:
            tags.append("exit node")
        line = f"{dot} {p.host}  {p.ipv4}  {p.os}  {p.connection}"
        if p.online and (p.rx or p.tx):
            line += f"  ↓{human_size(p.rx)} ↑{human_size(p.tx)}"
        if tags:
            line += "  [" + ", ".join(tags) + "]"
        return line

    @staticmethod
    def peer_actions(st: ts.Status, p: ts.Peer) -> list[tuple[str, str]]:
        acts = [("ssh", "SSH"), ("ping", "Ping"), ("copy", "Copy IP")]
        if p.exit_node:
            acts.append(("exitoff", "Stop using as exit"))
        elif p.exit_option and p.online:
            acts.append(("exit", "Use as exit node"))
        return acts

    async def show_output(self, v: View, title: str, out: str, actions: Optional[list] = None) -> None:
        out = out.strip() or "(no output)"
        lines = out.splitlines()
        if len(lines) > 40:
            out = "\n".join(lines[:40]) + f"\n… {len(lines) - 40} more line(s)"
        await v.bp.set_preview(f"{title}\n{'─' * 24}\n{out}", (actions or []) + [("close", "Close")])

    # ------------------------------------------------------------------
    # events

    async def on_event(self, v: View, ev: zenteru.PanelEvent) -> None:
        if v.bp.feed(ev):
            return
        if ev.type == "closed":
            v.visible = False
            if self.views.get(v.window_id) is v:
                del self.views[v.window_id]
            return
        if ev.type == "hidden":
            v.visible = False
            return
        try:
            if ev.type == "action":
                part, entry = v.bp.classify(ev.message_id)
                if part == "header":
                    await self.on_toolbar(v, ev.action_id)
                elif part == "entry" and entry is not None:
                    await self.on_peer(v, entry.data, ev.action_id)
                elif part == "pager":
                    await v.bp.set_page(v.bp.page + (1 if ev.action_id == "next" else -1))
                elif part == "preview":
                    await self.on_preview_action(v, ev.action_id or "")
            elif ev.type == "input":
                await self.on_input(v, (ev.text or "").strip())
        except Exception as e:
            log.exception("event failed")
            await v.panel.async_set_status(f"error: {e}")

    async def run_shown(self, v: View, args: list[str], timeout: float = 30) -> tuple[int, str]:
        assert self.cli is not None
        await v.bp.set_preview(f"tailscale {shlex.join(args)}\nrunning…")
        code, out = await ts.run(self.cli, *args, timeout=timeout)
        acts = []
        m = URL_RE.search(out)
        if m:
            self._last_url = m.group(0)
            acts.append(("openurl", "Open link"))
        await self.show_output(v, f"tailscale {shlex.join(args)}" + (f"  (exit {code})" if code else ""), out, acts)
        return code, out

    _last_url: str = ""

    async def on_toolbar(self, v: View, a: Optional[str]) -> None:
        if a == "refresh":
            await self.refresh(v, force=True)
            return
        if a == "download":
            self.open_url(ts.DOWNLOAD_URL)
            return
        if a == "appstore":
            self.open_url(ts.APP_STORE_URL)
            return
        if a == "openapp":
            subprocess.Popen(["/usr/bin/open", "-a", "Tailscale"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        if a == "help":
            await self.show_output(v, "zentail", __doc__ or "")
            return
        if self.cli is None:
            await self.refresh(v, force=True)
            return
        st = v.status
        if a == "up":
            await self.run_shown(v, ["up"], timeout=20)
        elif a == "down":
            ans = await v.bp.ask(
                "Disconnect this Mac from the tailnet?\nssh sessions and connections over Tailscale will drop.",
                [("yes", "Disconnect"), ("no", "Cancel")])
            if ans != ("action", "yes"):
                await v.bp.clear_preview()
                return
            await self.run_shown(v, ["down"])
        elif a == "exitoff":
            await self.run_shown(v, ["set", "--exit-node="])
        elif a == "netcheck":
            await self.run_shown(v, ["netcheck"], timeout=30)
        elif a == "accounts":
            await self.show_accounts(v)
        elif a == "login" and st is not None and st.auth_url:
            self.open_url(st.auth_url)
        elif a == "link":
            await self.link_cli(v)
        await self.refresh(v, force=True)

    async def on_preview_action(self, v: View, a: str) -> None:
        if a == "close":
            await v.bp.clear_preview()
        elif a == "openurl" and self._last_url:
            self.open_url(self._last_url)
        elif a.startswith("switch:") and self.cli is not None:
            await self.run_shown(v, ["switch", a.split(":", 1)[1]])
            await self.refresh(v, force=True)

    async def on_peer(self, v: View, p: ts.Peer, a: Optional[str]) -> None:
        if self.cli is None:
            return
        target = p.short_dns or p.ipv4
        if a == "ssh":
            await self.new_tab(v, f"ssh {shlex.quote(target)}")
        elif a == "ping":
            await self.run_shown(v, ["ping", "-c", "3", p.ipv4 or target], timeout=40)
        elif a == "copy" and p.ipv4:
            await zenteru.clipboard.async_set_text(self.connection, p.ipv4)
            await v.panel.async_set_status(f"Copied {p.ipv4}")
        elif a == "exit":
            ans = await v.bp.ask(
                f"Send this Mac's internet traffic through {p.host}?",
                [("yes", "Use exit node"), ("lan", "Use, keep LAN access"), ("no", "Cancel")])
            if ans == ("action", "yes"):
                await self.run_shown(v, ["set", f"--exit-node={p.ipv4}"])
            elif ans == ("action", "lan"):
                await self.run_shown(v, ["set", f"--exit-node={p.ipv4}", "--exit-node-allow-lan-access"])
            else:
                await v.bp.clear_preview()
                return
            await self.refresh(v, force=True)
        elif a == "exitoff":
            await self.run_shown(v, ["set", "--exit-node="])
            await self.refresh(v, force=True)

    async def on_input(self, v: View, text: str) -> None:
        if not text:
            return
        if self.cli is None:
            await self.refresh(v, force=True)
            return
        try:
            args = shlex.split(text)
        except ValueError as e:
            await v.panel.async_set_status(str(e))
            return
        if args and args[0] == "tailscale":
            args = args[1:]
        if not args:
            return
        if args[0] in ts.INTERACTIVE:
            await self.new_tab(v, "tailscale " + shlex.join(args))
            return
        await self.run_shown(v, args, timeout=60)
        await self.refresh(v)

    async def show_accounts(self, v: View) -> None:
        assert self.cli is not None
        code, out = await ts.run(self.cli, "switch", "--list", "--json")
        accounts = ts.parse_accounts(out)
        if not accounts:
            await self.show_output(v, "Accounts", out)
            return
        lines, acts = [], []
        for a in accounts:
            mark = "●" if a.get("selected") else "○"
            lines.append(f"{mark} {a.get('account') or a.get('nickname')}  tailnet {a.get('tailnet')}")
            if not a.get("selected"):
                acts.append((f"switch:{a['id']}", f"Switch to {a.get('nickname') or a.get('account')}"))
        await self.show_output(v, "Accounts (tailscale switch)", "\n".join(lines), acts)

    async def link_cli(self, v: View) -> None:
        binary = ts.app_binary()
        if binary is None:
            await v.panel.async_set_status("Tailscale.app not found")
            return
        dest = "~/.local/bin/tailscale"
        state = ts.launcher_status(os.path.expanduser(dest), binary)
        if state == "ours":
            await self.show_output(v, "tailscale command", f"{dest} is already set up.")
            return
        q = (
            f"Put a `tailscale` command in {dest}?\n"
            "It is a two-line launcher for the app's own CLI (the same thing the\n"
            "app's Settings → CLI integration → Install Now does, without sudo):\n\n"
            + ts.launcher_script(binary)
            + "\nA plain symlink to the app binary doesn't work: Tailscale then can't find\n"
            "its bundle and aborts. Nothing is installed with Homebrew."
        )
        if state == "other":
            q += f"\n\n{dest} already exists and isn't zentail's. It will be replaced."
        ans = await v.bp.ask(q, [("yes", "Install"), ("no", "Cancel")])
        if ans != ("action", "yes"):
            await v.bp.clear_preview()
            return
        try:
            path = ts.install_launcher(binary, replace=True)
        except OSError as e:
            await self.show_output(v, "tailscale command", f"couldn't write {dest}: {e}")
            return
        on_path = await self.shell_finds(path)
        msg = f"Installed {path}."
        if not on_path:
            msg += ("\n~/.local/bin isn't on your shell's PATH. Add this to ~/.zshrc:\n"
                    '  export PATH="$HOME/.local/bin:$PATH"')
        await self.show_output(v, "tailscale command", msg)

    @staticmethod
    async def shell_finds(path: str) -> bool:
        shell = os.environ.get("SHELL") or "/bin/zsh"
        try:
            p = await asyncio.create_subprocess_exec(
                shell, "-l", "-c", "command -v tailscale", stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, stdin=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(p.communicate(), 10)
        except (OSError, asyncio.TimeoutError):
            return False
        found = out.decode().strip()
        return bool(found) and os.path.realpath(found) == os.path.realpath(path)

    async def new_tab(self, v: View, command: str) -> None:
        params: dict[str, Any] = {"command": command}
        if v.window_id:
            params["window_id"] = v.window_id
        try:
            await self.connection.async_call("tab.create", params)
        except RPCError as e:
            await v.panel.async_set_status(f"couldn't open a tab: {e.message}")

    @staticmethod
    def open_url(url: str) -> None:
        subprocess.Popen(["/usr/bin/open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # ------------------------------------------------------------------

    async def cmd_toggle(self, inv: CommandInvocation) -> None:
        v = self.views.get(inv.window_id)
        if v is not None and not v.panel.closed and v.visible and not inv.line.strip():
            await v.panel.async_hide()
            v.visible = False
            return
        v = await self.view_for(inv.window_id)
        await self.refresh(v, force=True)
        if inv.line.strip():
            await self.on_input(v, inv.line.strip())


async def main(connection: Connection) -> None:
    plugin = ZenTail(connection)
    await plugin.async_register()
    await connection.async_wait_closed()
