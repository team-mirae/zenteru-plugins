"""zenfinder — a side-panel file browser for the session's directory.

* In a local session it shows the shell's working directory.
* In an ssh session (the foreground job is ``ssh``) it shows the remote
  host, starting from the directory in the remote prompt
  (``user@host:~/src$``) or the remote home.

The panel follows the shell as it moves until you navigate yourself;
"Follow shell" turns that back on.

While the panel is open in a window, files dropped on the window's
terminal, or pasted from the clipboard (files copied in Finder,
screenshots), go into the directory shown: uploaded over ssh or copied
locally, after a confirmation. Clipboard **text** is saved as a file with
"Paste text" (⌘⇧V): the panel asks for the file name, prefilled with
``paste-<time>.<format>``, and Enter saves it. The default format is
``txt``; ``format md`` (or ``@zenfinder-format md``) changes it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import zenteru
from zenteru import Command, CommandInvocation, Connection, DropEvent, DropHandler, Panel, RPCError

from .backends import Backend, BackendError, Exists, LocalBackend, RemoteBackend, valid_name, visible
from .browserpanel import BrowserPanel, Entry, decode_text, hex_preview, human_size, text_preview
from .config import Config, normalize_ext, paste_name
from .sshargs import CONFIG_SNIPPET, auth_command, control_dir, foreground_ssh, parse_ps, remote_cwd_from_prompt

log = logging.getLogger("zenfinder")

TITLE = "zenfinder"
POLL_SECONDS = 1.5
SSH_JOBS = ("ssh", "autossh")

UTI_EXT = {
    "public.png": "png", "public.jpeg": "jpg", "public.tiff": "tiff", "public.heic": "heic",
    "com.compuserve.gif": "gif", "public.webp": "webp", "org.webmproject.webp": "webp",
    "com.adobe.pdf": "pdf", "public.html": "html", "public.json": "json",
    "public.utf8-plain-text": "txt", "public.plain-text": "txt",
}

HELP = """\
Type in the box below:
  <name>         open a folder / preview a file in this listing
  cd <path>      go to a path (~, /, .. work)
  mkdir <name>   create a folder here
  get <name>     download (ssh) or reveal in Finder (local)
  format <ext>   default format for pasted text (now .{ext})
  hidden         show / hide dot files
  follow         follow the shell's directory again
Drop files on the terminal (or paste files / images) to put them here."""


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


@dataclass
class Location:
    backend: Backend
    #: Directory to show (may be ``~``-relative for ssh).
    path: str
    session_id: str
    #: Identifies "the same place" for follow mode.
    key: tuple = field(default_factory=tuple)


@dataclass
class View:
    window_id: Optional[str]
    panel: Panel
    bp: BrowserPanel
    backend: Optional[Backend] = None
    cwd: Optional[str] = None
    session_id: Optional[str] = None
    follow: bool = True
    visible: bool = True
    located_key: Optional[tuple] = None
    previewed: Optional[Entry] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    poller: Optional[asyncio.Task] = None
    #: Shown instead of a listing when ssh couldn't log in.
    auth_failed: bool = False


class ZenFinder:
    def __init__(self, connection: Connection, config: Optional[Config] = None) -> None:
        self.connection = connection
        self.config = config or Config.load()
        self.views: dict[Optional[str], View] = {}
        self.remotes: dict[tuple, RemoteBackend] = {}
        self.local = LocalBackend()
        self.commands = [
            Command("zenfinder: show / hide", self.cmd_toggle, identifier="zenfinder.toggle", name="zenfinder"),
            Command("zenfinder: paste clipboard text as a file", self.cmd_paste,
                    identifier="zenfinder.paste", name="zenfinder-paste", keybinding="cmd+shift+v"),
            Command("zenfinder: set the pasted-text format", self.cmd_format,
                    identifier="zenfinder.format", name="zenfinder-format"),
        ]
        self.drop_handler = DropHandler(self.on_drop, "zenfinder.drop", title=TITLE, timeout_ms=3000)

    async def async_register(self) -> None:
        for c in self.commands:
            await c.async_register(self.connection)
        await self.drop_handler.async_register(self.connection)

    # ------------------------------------------------------------------
    # views

    async def view_for(self, window_id: Optional[str], show: bool = True) -> View:
        v = self.views.get(window_id)
        if v is None or v.panel.closed:
            panel = await Panel.async_create(self.connection, TITLE, window_id=window_id)
            v = View(window_id, panel, BrowserPanel(panel))
            panel.on("*", lambda ev, v=v: self.on_event(v, ev))
            self.views[window_id] = v
        if show:
            await v.panel.async_show()
            v.visible = True
            if v.poller is None or v.poller.done():
                v.poller = self.connection.spawn(self.poll(v))
        return v

    async def window_of(self, session_id: Optional[str]) -> Optional[str]:
        if not session_id:
            return None
        app = await zenteru.async_get_app(self.connection)
        s = app.get_session_by_id(session_id)
        if s is None:
            await app.async_refresh()
            s = app.get_session_by_id(session_id)
        w = s.window if s is not None else None
        return w.window_id if w is not None else None

    async def active_session(self, window_id: Optional[str]) -> Optional[str]:
        app = await zenteru.async_get_app(self.connection)
        w = app.get_window_by_id(window_id) if window_id else app.current_window
        tab = w.current_tab if w is not None else None
        s = tab.current_session if tab is not None else None
        return s.session_id if s is not None else None

    # ------------------------------------------------------------------
    # where is the session?

    async def locate(self, session_id: str) -> Location:
        session = zenteru.Session(self.connection, session_id)
        try:
            await session.async_refresh()
        except RPCError:
            return Location(self.local, self.local.home(), session_id, ("local", self.local.home()))
        job = None
        try:
            job = await session.async_get_variable("session.jobName")
        except RPCError:
            pass
        if job in SSH_JOBS and session.tty:
            target = await asyncio.to_thread(self._ssh_of_tty, session.tty)
            if target is not None:
                backend = self.remotes.get(target.key())
                if backend is None:
                    backend = self.remotes[target.key()] = RemoteBackend(target)
                guess = None
                try:
                    screen = await session.async_get_screen_contents()
                    guess = remote_cwd_from_prompt(screen.line(screen.cursor_coord.y).string)
                except (RPCError, IndexError):
                    pass
                return Location(backend, guess or "~", session_id, ("ssh", target.key(), guess))
        cwd = session.cwd or self.local.home()
        return Location(self.local, cwd, session_id, ("local", cwd))

    @staticmethod
    def _ssh_of_tty(tty: str):
        name = tty[len("/dev/"):] if tty.startswith("/dev/") else tty
        try:
            out = subprocess.run(
                ["ps", "-o", "pid=,stat=,args=", "-t", name],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        return foreground_ssh(parse_ps(out))

    async def poll(self, v: View) -> None:
        """Follow the active session while the panel is open."""
        while not v.panel.closed and v.visible:
            await asyncio.sleep(POLL_SECONDS)
            if not v.follow or v.bp.asking or v.lock.locked():
                continue
            try:
                sid = await self.active_session(v.window_id)
                if not sid:
                    continue
                loc = await self.locate(sid)
                if loc.key != v.located_key:
                    await self.go_location(v, loc)
            except (RPCError, zenteru.ConnectionClosed):
                return
            except Exception:
                log.exception("poll failed")

    # ------------------------------------------------------------------
    # rendering

    async def go_location(self, v: View, loc: Location) -> None:
        v.located_key = loc.key
        v.session_id = loc.session_id
        await self.navigate(v, loc.path, backend=loc.backend, follow=v.follow)

    async def navigate(self, v: View, path: str, backend: Optional[Backend] = None,
                       follow: Optional[bool] = None, page: int = 0) -> None:
        async with v.lock:
            b = backend or v.backend or self.local
            if follow is not None:
                v.follow = follow
            await v.panel.async_set_status(f"Loading {b.display(path)}…")
            try:
                cwd, entries = await b.list(path)
            except BackendError as e:
                v.backend = b
                if e.auth and isinstance(b, RemoteBackend):
                    v.auth_failed = True
                    await self.render_auth(v, b, str(e))
                else:
                    await v.panel.async_set_status(str(e))
                return
            v.auth_failed = False
            same_dir = v.backend is b and v.cwd == cwd
            v.backend, v.cwd = b, cwd
            if not same_dir:
                v.previewed = None
            shown = visible(entries, self.config.show_hidden)
            await v.bp.render(self.header(v, shown), self.toolbar(v), shown,
                              lambda e, b=b: self.actions_for(b, e), page=page, keep_preview=same_dir)
            hidden = len(entries) - len(shown)
            await v.panel.async_set_status(
                f"{len(shown)} item(s)" + (f", {hidden} hidden" if hidden else "") + f" · {b.label}"
            )

    async def refresh(self, v: View) -> None:
        if v.cwd is not None:
            await self.navigate(v, v.cwd, page=v.bp.page)

    def header(self, v: View, shown: list[Entry]) -> str:
        assert v.backend is not None and v.cwd is not None
        where = v.backend.display(v.cwd)
        mode = "following the shell" if v.follow else "browsing"
        return f"{where}\n{mode} · text pastes as .{self.config.paste_ext}"

    def toolbar(self, v: View) -> list[tuple[str, str]]:
        acts = [("up", "↑ Up"), ("refresh", "⟳ Refresh"), ("home", "~ Home")]
        if not v.follow:
            acts.append(("follow", "Follow shell"))
        acts += [("paste", "Paste text"), ("shellcd", "cd shell here"),
                 ("hidden", "Hide dot files" if self.config.show_hidden else "Show dot files"), ("help", "?")]
        return acts

    @staticmethod
    def actions_for(b: Backend, e: Entry) -> list[tuple[str, str]]:
        if e.is_dir:
            return [("open", "Open"), ("insert", "Insert path")]
        acts = [("preview", "Preview"), ("insert", "Insert path")]
        acts.append(("get", "Download") if isinstance(b, RemoteBackend) else ("reveal", "Reveal"))
        return acts

    async def render_auth(self, v: View, b: RemoteBackend, err: str) -> None:
        text = (
            f"{b.label}: zenfinder couldn't log in on its own.\n{err}\n\n"
            "It connects in the background with your keys or agent, and reuses an\n"
            "OpenSSH control master when there is one. Either authenticate once in a\n"
            "new tab (the connection then stays open for 10 minutes), or add this to\n"
            "~/.ssh/config so your own ssh sessions share their login:\n\n" + CONFIG_SNIPPET
        )
        await v.bp.render(text, [("auth", "Authenticate in a new tab"), ("retry", "Retry"), ("local", "Show this Mac")],
                          [], lambda e: self.actions_for(b, e))
        await v.panel.async_set_status(f"{b.label}: not connected")

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
                await self.on_action(v, ev)
            elif ev.type == "input":
                await self.on_input(v, (ev.text or "").strip())
        except Exception as e:  # keep the panel alive
            log.exception("event failed")
            await v.panel.async_set_status(f"error: {e}")

    async def on_action(self, v: View, ev: zenteru.PanelEvent) -> None:
        part, entry = v.bp.classify(ev.message_id)
        a = ev.action_id
        if part == "header":
            await self.on_toolbar(v, a)
        elif part == "pager":
            await v.bp.set_page(v.bp.page + (1 if a == "next" else -1))
        elif part == "entry" and entry is not None:
            await self.on_entry(v, entry, a)
        elif part == "preview":
            if a == "close":
                v.previewed = None
                await v.bp.clear_preview()
            elif v.previewed is not None:
                await self.on_entry(v, v.previewed, a)

    async def on_toolbar(self, v: View, a: Optional[str]) -> None:
        b = v.backend or self.local
        if a == "up" and v.cwd:
            await self.navigate(v, b.parent(v.cwd), follow=False)
        elif a == "refresh":
            await self.refresh(v)
        elif a == "home":
            await self.navigate(v, b.home(), follow=False)
        elif a in ("follow", "retry"):
            v.follow = True
            v.located_key = None
            sid = await self.active_session(v.window_id)
            if sid:
                await self.go_location(v, await self.locate(sid))
        elif a == "paste":
            await self.paste_text(v)
        elif a == "shellcd" and v.cwd:
            await self.type_into_shell(v, "cd -- " + shlex.quote(v.cwd))
        elif a == "hidden":
            self.config.show_hidden = not self.config.show_hidden
            self.config.save()
            await self.refresh(v)
        elif a == "help":
            await v.bp.set_preview(HELP.format(ext=self.config.paste_ext), [("close", "Close")])
        elif a == "auth" and isinstance(b, RemoteBackend):
            await self.open_auth_tab(v, b)
        elif a == "local":
            await self.navigate(v, self.local.home(), backend=self.local, follow=False)

    async def on_entry(self, v: View, e: Entry, a: Optional[str]) -> None:
        b = v.backend
        if b is None or v.cwd is None:
            return
        path = b.join(v.cwd, e.name)
        if a == "open" and e.is_dir:
            await self.navigate(v, path, follow=False)
        elif a == "preview":
            await self.preview(v, e)
        elif a == "insert":
            await self.type_into_shell(v, shlex.quote(path) + " ")
        elif a == "get" and isinstance(b, RemoteBackend):
            await v.panel.async_set_status(f"Downloading {e.name}…")
            try:
                dest = await b.download(path, e.is_dir, self.config.download_path)
            except (BackendError, OSError) as err:
                await v.panel.async_set_status(f"download failed: {err}")
                return
            await v.panel.async_set_status(f"Saved to {LocalBackend().display(dest)}")
            await zenteru.ui.async_notify(self.connection, TITLE, f"Downloaded {e.name}")
        elif a == "reveal" and isinstance(b, LocalBackend):
            subprocess.Popen(["/usr/bin/open", "-R", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif a == "ql" and isinstance(b, LocalBackend):
            subprocess.Popen(["/usr/bin/qlmanage", "-p", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    async def on_input(self, v: View, text: str) -> None:
        if not text:
            return
        cmd, _, arg = text.partition(" ")
        arg = arg.strip()
        b = v.backend or self.local
        cwd = v.cwd or b.home()
        if cmd == "cd":
            await self.navigate(v, self.resolve(b, cwd, arg or b.home()), follow=False)
        elif text in ("..", "up"):
            await self.navigate(v, b.parent(cwd), follow=False)
        elif cmd == "mkdir" and arg:
            try:
                await b.mkdir(cwd, arg)
            except (BackendError, ValueError) as e:
                await v.panel.async_set_status(f"mkdir: {e}")
                return
            await self.refresh(v)
        elif cmd == "format":
            await self.set_format(v, arg)
        elif text == "hidden":
            await self.on_toolbar(v, "hidden")
        elif text == "follow":
            await self.on_toolbar(v, "follow")
        elif text in ("help", "?"):
            await self.on_toolbar(v, "help")
        elif text == "paste":
            await self.paste_text(v)
        elif cmd == "get" and arg:
            e = v.bp.find(arg)
            if e is None:
                await v.panel.async_set_status(f"no {arg!r} here")
            else:
                await self.on_entry(v, e, "get" if isinstance(b, RemoteBackend) else "reveal")
        else:
            e = v.bp.find(text)
            if e is not None:
                await self.on_entry(v, e, "open" if e.is_dir else "preview")
            elif text.startswith(("/", "~")):
                await self.navigate(v, self.resolve(b, cwd, text), follow=False)
            else:
                await v.panel.async_set_status(f"no {text!r} here — type ? for help")

    @staticmethod
    def resolve(b: Backend, cwd: str, arg: str) -> str:
        if arg.startswith("/") or arg == "~" or arg.startswith("~/"):
            path = os.path.expanduser(arg) if isinstance(b, LocalBackend) else arg
        else:
            path = b.join(cwd, arg)
        if path.startswith("/"):
            path = os.path.normpath(path)
        return path

    # ------------------------------------------------------------------
    # preview

    async def preview(self, v: View, e: Entry) -> None:
        b = v.backend
        assert b is not None and v.cwd is not None
        v.previewed = e
        path = b.join(v.cwd, e.name)
        extra = [("get", "Download")] if isinstance(b, RemoteBackend) else [("ql", "Quick Look"), ("reveal", "Reveal")]
        await v.bp.set_preview(f"{e.name}\nloading…", [("close", "Close")])
        try:
            data = await b.read_head(path)
        except BackendError as err:
            await v.bp.set_preview(f"{e.name}\n{err}", [("close", "Close")])
            return
        if v.previewed is not e:
            return
        size = f"{human_size(e.size)}" if e.size is not None else ""
        text = decode_text(data)
        if text is not None:
            body = text_preview(text, e.size)
            kind = "text"
        else:
            body = hex_preview(data)
            kind = "binary"
        await v.bp.set_preview(f"{e.name} — {kind}{', ' + size if size else ''}\n{'─' * 24}\n{body}",
                               [*extra, ("close", "Close")])

    # ------------------------------------------------------------------
    # shell

    async def type_into_shell(self, v: View, text: str) -> None:
        sid = await self.active_session(v.window_id) or v.session_id
        if not sid:
            return
        try:
            await zenteru.Session(self.connection, sid).async_send_text(text)
        except RPCError as e:
            await v.panel.async_set_status(f"couldn't type into the session: {e.message}")

    async def open_auth_tab(self, v: View, b: RemoteBackend) -> None:
        cmd = auth_command(b.target, await b.effective_config())
        os.makedirs(control_dir(), mode=0o700, exist_ok=True)
        script = (
            f"echo {shlex.quote('zenfinder: logging in to ' + b.label + ' for the file panel')}; "
            + shlex.join(cmd)
            + " && echo 'zenfinder: connected. Press Retry in the panel.' || echo 'zenfinder: login failed.'; "
            "printf 'Press Enter to close this tab. '; read _"
        )
        params: dict[str, Any] = {"command": script}
        if v.window_id:
            params["window_id"] = v.window_id
        try:
            await self.connection.async_call("tab.create", params)
        except RPCError as e:
            await v.panel.async_set_status(f"couldn't open a tab: {e.message}")
            return
        await v.panel.async_set_status("Log in in the new tab, then press Retry.")

    # ------------------------------------------------------------------
    # writing files here

    async def put_bytes(self, v: View, name: str, data: bytes) -> Optional[str]:
        """Write ``data`` as ``name`` into the shown directory, asking before
        overwriting. Returns the path written, or None."""
        b, cwd = v.backend, v.cwd
        assert b is not None and cwd is not None
        overwrite = False
        while True:
            try:
                return await b.write_bytes(cwd, name, data, overwrite=overwrite)
            except Exists:
                ans = await v.bp.ask(
                    f"{name} already exists in {b.display(cwd)}.\nOverwrite it, or type another name:",
                    [("overwrite", "Overwrite"), ("cancel", "Cancel")], prefill=name,
                )
                if ans is None or ans == ("action", "cancel"):
                    return None
                if ans == ("action", "overwrite"):
                    overwrite = True
                    continue
                try:
                    new = valid_name(ans[1])
                except ValueError as e:
                    await v.panel.async_set_status(str(e))
                    continue
                overwrite = False
                name = new
            except (BackendError, ValueError) as e:
                await v.panel.async_set_status(f"couldn't save {name}: {e}")
                return None

    async def ask_name(self, v: View, question: str, default: str, ext: Optional[str]) -> Optional[str]:
        """The filename prompt that doubles as the confirmation."""
        while True:
            ans = await v.bp.ask(question, [("cancel", "Cancel")], prefill=default)
            if ans is None or ans[0] == "action" or not ans[1].strip():
                await v.bp.set_preview("Cancelled — nothing was saved.", [("close", "Close")])
                return None
            name = paste_name(ans[1], ext) if ext else ans[1].strip()
            try:
                return valid_name(name)
            except ValueError as e:
                question = f"{e}.\n{question}"

    async def paste_text(self, v: View) -> None:
        if v.backend is None or v.cwd is None:
            await v.panel.async_set_status("Nothing is shown yet.")
            return
        text = await zenteru.clipboard.async_get_text(self.connection)
        if not text:
            await v.panel.async_set_status("The clipboard has no text.")
            return
        ext = self.config.paste_ext
        lines = text.splitlines()
        sample = text_preview(text, max_lines=8, width=100)
        q = (
            f"Save clipboard text ({len(text):,} characters, {len(lines)} line(s)) to\n"
            f"{v.backend.display(v.cwd)} as:\n{'─' * 24}\n{sample}\n{'─' * 24}\n"
            f"Edit the name below and press Enter to save (.{ext} is added when there is no\n"
            "extension). Empty or Cancel saves nothing."
        )
        name = await self.ask_name(v, q, f"paste-{stamp()}.{ext}", ext)
        if name is None:
            return
        data = text.encode("utf-8")
        written = await self.put_bytes(v, name, data)
        if written:
            await self.refresh(v)
            await v.bp.set_preview(f"Saved {v.backend.display(written)} ({human_size(len(data))}).", [("close", "Close")])

    async def set_format(self, v: Optional[View], arg: str) -> None:
        try:
            ext = normalize_ext(arg)
        except ValueError as e:
            if v is not None:
                await v.panel.async_set_status(str(e) if arg else f"pasted text is saved as .{self.config.paste_ext}")
            return
        self.config.paste_ext = ext
        self.config.save()
        if v is not None:
            if v.cwd is not None:
                await v.bp.set_header(self.header(v, v.bp.entries))
            await v.panel.async_set_status(f"Pasted text will be saved as .{ext}")

    # ------------------------------------------------------------------
    # drops and pasted files

    async def on_drop(self, event: DropEvent) -> Optional[str]:
        wid = await self.window_of(event.session_id)
        v = self.views.get(wid)
        if v is None or v.panel.closed or not v.visible or v.backend is None or v.cwd is None or v.auth_failed:
            return None
        self.connection.spawn(self.receive(v, event))
        return ""  # handled: type nothing

    async def receive(self, v: View, event: DropEvent) -> None:
        b, cwd = v.backend, v.cwd
        assert b is not None and cwd is not None
        files = [i.path for i in event.items if i.kind == "file" and i.path]
        blobs = [i for i in event.items if i.kind == "data" and i.base64]
        where = b.display(cwd)
        verb = "Upload" if isinstance(b, RemoteBackend) else "Copy"
        if files:
            names = "\n".join(f"  {os.path.basename(p.rstrip('/'))}" for p in files[:12])
            if len(files) > 12:
                names += f"\n  … and {len(files) - 12} more"
            ans = await v.bp.ask(
                f"{verb} {len(files)} item(s) to {where}?\n{names}",
                [("go", verb), ("insert", "Insert paths instead"), ("cancel", "Cancel")],
            )
            if ans == ("action", "insert"):
                await self.type_into_shell(v, " ".join(shlex.quote(p) for p in files) + " ")
                await v.bp.clear_preview()
            elif ans in (("action", "go"), ("input", "")):
                await self.copy_files(v, files)
            else:
                await v.bp.set_preview("Cancelled — nothing was copied.", [("close", "Close")])
        for item in blobs:
            ext = UTI_EXT.get((item.uti or "").lower()) or (item.name.rsplit(".", 1)[1] if item.name and "." in item.name else "bin")
            name = await self.ask_name(
                v, f"Save the {event.source}d {ext.upper()} ({human_size(len(item.data))}) to {where} as:\n"
                   "Enter saves, empty or Cancel saves nothing.",
                f"{event.source}-{stamp()}.{ext}", None,
            )
            if name is None:
                continue
            written = await self.put_bytes(v, name, item.data)
            if written:
                await self.refresh(v)
                await v.bp.set_preview(f"Saved {b.display(written)}.", [("close", "Close")])

    async def copy_files(self, v: View, files: list[str]) -> None:
        b, cwd = v.backend, v.cwd
        assert b is not None and cwd is not None
        done, skipped = 0, 0
        overwrite_all = False
        for i, p in enumerate(files, 1):
            name = os.path.basename(p.rstrip("/"))
            await v.panel.async_set_status(f"{i}/{len(files)}: {name}…")
            overwrite = overwrite_all
            while True:
                try:
                    await b.upload(p, cwd, name, overwrite=overwrite)
                    done += 1
                    break
                except Exists:
                    ans = await v.bp.ask(
                        f"{name} already exists in {b.display(cwd)}.",
                        [("overwrite", "Overwrite"), ("all", "Overwrite all"), ("skip", "Skip"), ("cancel", "Stop")],
                    )
                    if ans == ("action", "overwrite"):
                        overwrite = True
                    elif ans == ("action", "all"):
                        overwrite = overwrite_all = True
                    elif ans == ("action", "skip"):
                        skipped += 1
                        break
                    else:
                        await self.refresh(v)
                        await v.bp.set_preview(f"Stopped after {done} item(s).", [("close", "Close")])
                        return
                except (BackendError, ValueError, OSError) as e:
                    skipped += 1
                    await v.panel.async_set_status(f"{name}: {e}")
                    break
        await self.refresh(v)
        msg = f"Copied {done} item(s) to {b.display(cwd)}." + (f" {skipped} skipped." if skipped else "")
        await v.bp.set_preview(msg, [("close", "Close")])

    # ------------------------------------------------------------------
    # commands

    async def show_for(self, inv: CommandInvocation) -> View:
        v = await self.view_for(inv.window_id)
        sid = inv.session_id or await self.active_session(inv.window_id)
        if sid and (v.cwd is None or v.follow):
            v.located_key = None
            await self.go_location(v, await self.locate(sid))
        return v

    async def cmd_toggle(self, inv: CommandInvocation) -> None:
        v = self.views.get(inv.window_id)
        if v is not None and not v.panel.closed and v.visible and not inv.line:
            await v.panel.async_hide()
            v.visible = False
            return
        v = await self.show_for(inv)
        if inv.line.strip():
            await self.on_input(v, inv.line.strip())

    async def cmd_paste(self, inv: CommandInvocation) -> None:
        v = await self.show_for(inv)
        await self.paste_text(v)

    async def cmd_format(self, inv: CommandInvocation) -> None:
        v = self.views.get(inv.window_id)
        arg = inv.line.strip() or (inv.argv[0] if inv.argv else "")
        if not arg:
            v = await self.show_for(inv)
            ans = await v.bp.ask(
                f"Default format for pasted text (now .{self.config.paste_ext}). Type an extension:",
                [("cancel", "Cancel")], prefill=self.config.paste_ext,
            )
            if ans is None or ans[0] == "action":
                await v.bp.clear_preview()
                return
            arg = ans[1]
            await v.bp.clear_preview()
        await self.set_format(v, arg)
        if v is None:
            await zenteru.ui.async_notify(self.connection, TITLE, f"Pasted text will be saved as .{self.config.paste_ext}")


async def main(connection: Connection) -> None:
    plugin = ZenFinder(connection)
    await plugin.async_register()
    await connection.async_wait_closed()
