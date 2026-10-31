"""zenobfus — browse and preview ``.obfus`` files in a side panel.

The browser (top of the panel) shows the session's working directory,
like zenfinder. Files whose first bytes are an obfus header are tagged
``[obfus]`` whatever their name. Selecting one unpacks it **in memory**
and the preview (bottom of the panel) shows what is inside: text, image
size, media type, a hex dump, or the listing of an archive. Archives
(zip, tar, tar.gz/bz2/xz), obfuscated or not, open as folders you can
browse into, and so do nested obfus layers.

Nothing is written unless you ask: "Unpack…" and "Obfuscate…" save a new
file next to the original after a file-name prompt, and "Quick Look"
writes a temporary copy into zenteru's per-launch private directory,
which zenteru removes at quit.

obfus is obfuscation, not encryption: the key is 4 bits and recovered by
trying all 16. The panel never calls it encryption, and neither should you.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from typing import Any, Optional

import zenteru
from zenteru import Command, CommandInvocation, Connection, Panel, RPCError

from . import obfus
from .browserpanel import BrowserPanel, Entry, human_size
from .contents import Kind, Tree, describe, open_tree, sniff

log = logging.getLogger("zenobfus")

TITLE = "zenobfus"
POLL_SECONDS = 1.5
#: Largest file unpacked in the panel.
MAX_BYTES = 512 * 1024 * 1024
#: Header checks per listing (one 6-byte read each).
MAX_SNIFF = 2000
ARCHIVE_EXT = (".zip", ".tar", ".tgz", ".tar.gz", ".tbz2", ".tar.bz2", ".txz", ".tar.xz", ".jar")
NOTICE = "obfus is obfuscation, not encryption: anyone can unpack these files."

HELP = """\
Select a file to preview it below. [obfus] files are unpacked in memory;
nothing is saved unless you press Unpack… or Obfuscate….
Type in the box:
  <name>       open a folder or archive / preview a file
  cd <path>    go to a folder on this Mac
  ..           up one level (out of an archive, too)
  hidden       show / hide dot files
  follow       follow the shell's directory again
""" + NOTICE


def data_dir() -> str:
    return os.environ.get("ZENOBFUS_DATA_DIR") or os.path.expanduser(
        "~/Library/Application Support/zenteru/plugin-data/zenobfus"
    )


def load_config() -> dict:
    try:
        with open(os.path.join(data_dir(), "config.json"), encoding="utf-8") as f:
            c = json.load(f)
            return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(c: dict) -> None:
    d = data_dir()
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".config-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=2)
    os.replace(tmp, os.path.join(d, "config.json"))


def display_path(p: str) -> str:
    h = os.path.expanduser("~")
    return "~" + p[len(h):] if p == h or p.startswith(h + "/") else p


def valid_name(name: str) -> str:
    name = name.strip()
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        raise ValueError("a file name may not be empty, '.', '..' or contain '/'")
    return name


@dataclass
class Layer:
    """An archive (or obfus layer) being browsed as a folder."""

    label: str
    tree: Tree
    sub: str = ""


@dataclass
class Opened:
    """Contents of the entry being previewed."""

    entry: Entry
    #: Name of the contents (``.obfus`` stripped once if it was unpacked).
    name: str
    data: bytes
    kind: Kind
    #: True if ``data`` came from unpacking an obfus blob.
    unpacked: bool
    #: Bytes as stored (before unpacking).
    raw_len: int


@dataclass
class View:
    window_id: Optional[str]
    panel: Panel
    bp: BrowserPanel
    fs_dir: Optional[str] = None
    stack: list[Layer] = field(default_factory=list)
    follow: bool = True
    visible: bool = True
    located: Optional[str] = None
    opened: Optional[Opened] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    poller: Optional[asyncio.Task] = None


class ZenObfus:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.config = load_config()
        self.views: dict[Optional[str], View] = {}
        self.ql_dir: Optional[str] = None
        self._ql_n = 0
        self.commands = [
            Command("zenobfus: show / hide the obfus browser", self.cmd_toggle,
                    identifier="zenobfus.toggle", name="zenobfus"),
        ]

    @property
    def show_hidden(self) -> bool:
        return bool(self.config.get("show_hidden", False))

    async def async_register(self) -> None:
        for c in self.commands:
            await c.async_register(self.connection)

    # ------------------------------------------------------------------
    # views and sessions

    async def view_for(self, window_id: Optional[str]) -> View:
        v = self.views.get(window_id)
        if v is None or v.panel.closed:
            panel = await Panel.async_create(self.connection, TITLE, window_id=window_id)
            v = View(window_id, panel, BrowserPanel(panel))
            panel.on("*", lambda ev, v=v: self.on_event(v, ev))
            self.views[window_id] = v
        await v.panel.async_show()
        v.visible = True
        if v.poller is None or v.poller.done():
            v.poller = self.connection.spawn(self.poll(v))
        return v

    async def active_session(self, window_id: Optional[str]) -> Optional[str]:
        app = await zenteru.async_get_app(self.connection)
        w = app.get_window_by_id(window_id) if window_id else app.current_window
        tab = w.current_tab if w is not None else None
        s = tab.current_session if tab is not None else None
        return s.session_id if s is not None else None

    async def session_cwd(self, session_id: str) -> str:
        s = zenteru.Session(self.connection, session_id)
        try:
            await s.async_refresh()
        except RPCError:
            return os.path.expanduser("~")
        return s.cwd or os.path.expanduser("~")

    async def poll(self, v: View) -> None:
        while not v.panel.closed and v.visible:
            await asyncio.sleep(POLL_SECONDS)
            if not v.follow or v.bp.asking or v.lock.locked():
                continue
            try:
                sid = await self.active_session(v.window_id)
                if not sid:
                    continue
                cwd = await self.session_cwd(sid)
                if cwd != v.located:
                    v.located = cwd
                    await self.go_fs(v, cwd)
            except (RPCError, zenteru.ConnectionClosed):
                return
            except Exception:
                log.exception("poll failed")

    # ------------------------------------------------------------------
    # listing

    async def go_fs(self, v: View, path: str, follow: Optional[bool] = None) -> None:
        if follow is not None:
            v.follow = follow
        path = os.path.realpath(os.path.expanduser(path))
        try:
            entries = await asyncio.to_thread(self.list_fs, path)
        except OSError as e:
            await v.panel.async_set_status(f"{display_path(path)}: {e.strerror or e}")
            return
        same = v.fs_dir == path and not v.stack
        v.fs_dir, v.stack = path, []
        if not same:
            v.opened = None
        await self.render(v, entries, keep_preview=same)

    def list_fs(self, path: str) -> list[Entry]:
        out = []
        with os.scandir(path) as it:
            for de in it:
                if not self.show_hidden and de.name.startswith("."):
                    continue
                try:
                    is_dir = de.is_dir()
                    st = de.stat()
                except OSError:
                    continue
                e = Entry(de.name, is_dir, None if is_dir else st.st_size, st.st_mtime, de.is_symlink(),
                          data=os.path.join(path, de.name))
                out.append(e)
        out.sort(key=lambda e: (not e.is_dir, e.name.lower(), e.name))
        for e in [e for e in out if not e.is_dir][:MAX_SNIFF]:
            try:
                with open(e.data, "rb") as f:
                    head = f.read(len(obfus.MAGIC))
            except OSError:
                continue
            if obfus.is_obfus(head):
                e.badge = "obfus"
            elif e.name.endswith(obfus.EXT):
                e.badge = "not obfus"
        return out

    def list_layer(self, v: View) -> list[Entry]:
        layer = v.stack[-1]
        out = []
        for e in layer.tree.entries(layer.sub):
            if not self.show_hidden and e.name.startswith("."):
                continue
            if not e.is_dir and e.name.endswith(obfus.EXT):
                e.badge = "obfus"
            out.append(e)
        return out

    async def render(self, v: View, entries: list[Entry], keep_preview: bool = False, page: int = 0) -> None:
        async with v.lock:
            await v.bp.render(self.header(v), self.toolbar(v), entries, lambda e: self.actions_for(v, e),
                              keep_preview=keep_preview, page=page)
            n_obfus = sum(1 for e in entries if e.badge == "obfus")
            await v.panel.async_set_status(f"{len(entries)} item(s), {n_obfus} obfus")

    async def refresh(self, v: View) -> None:
        if v.stack:
            await self.render(v, self.list_layer(v), keep_preview=True, page=v.bp.page)
        elif v.fs_dir:
            await self.go_fs(v, v.fs_dir)

    def where(self, v: View) -> str:
        parts = [display_path(v.fs_dir or "~")]
        for layer in v.stack:
            parts.append(layer.label + ("/" + layer.sub if layer.sub else ""))
        return " ▸ ".join(parts)

    def header(self, v: View) -> str:
        mode = "following the shell" if v.follow else "browsing"
        inside = " · inside an archive (read-only)" if v.stack else ""
        return f"{self.where(v)}\n{mode}{inside}\n{NOTICE}"

    def toolbar(self, v: View) -> list[tuple[str, str]]:
        acts = [("up", "↑ Up"), ("refresh", "⟳ Refresh"), ("home", "~ Home")]
        if not v.follow:
            acts.append(("follow", "Follow shell"))
        acts += [("hidden", "Hide dot files" if self.show_hidden else "Show dot files"), ("help", "?")]
        return acts

    @staticmethod
    def _archive_name(name: str) -> bool:
        n = obfus.inner_name(name).lower()
        return n.endswith(ARCHIVE_EXT)

    def actions_for(self, v: View, e: Entry) -> list[tuple[str, str]]:
        if e.is_dir:
            return [("open", "Open")]
        acts = [("preview", "Preview")]
        if e.badge == "obfus" or self._archive_name(e.name):
            acts.append(("open", "Open"))
        if v.stack:
            acts.append(("save", "Extract…"))
        elif e.badge == "obfus":
            acts.append(("save", "Unpack…"))
        else:
            acts.append(("pack", "Obfuscate…"))
        return acts

    # ------------------------------------------------------------------
    # opening entries

    async def read_entry(self, v: View, e: Entry) -> bytes:
        if v.stack:
            layer = v.stack[-1]
            if e.size is not None and e.size > MAX_BYTES:
                raise ValueError(f"{e.name} is larger than {human_size(MAX_BYTES)}")
            return await asyncio.to_thread(layer.tree.read, e.data)
        path = e.data

        def read() -> bytes:
            size = os.path.getsize(path)
            if size > MAX_BYTES:
                raise ValueError(f"{e.name} is {human_size(size)}; zenobfus unpacks up to {human_size(MAX_BYTES)}")
            with open(path, "rb") as f:
                return f.read()

        return await asyncio.to_thread(read)

    async def open_contents(self, v: View, e: Entry) -> Opened:
        raw = await self.read_entry(v, e)

        def work() -> Opened:
            if obfus.is_obfus(raw):
                plain = obfus.unpack(raw)
                name = obfus.inner_name(e.name)
                return Opened(e, name, plain, sniff(name, plain), True, len(raw))
            return Opened(e, e.name, raw, sniff(e.name, raw), False, len(raw))

        return await asyncio.to_thread(work)

    async def preview(self, v: View, e: Entry) -> None:
        await v.bp.set_preview(f"{e.name}\nunpacking…" if e.badge == "obfus" else f"{e.name}\nreading…", [("close", "Close")])
        try:
            o = await self.open_contents(v, e)
        except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError, KeyError) as err:
            await v.bp.set_preview(f"{e.name}\n{err}", [("close", "Close")])
            return
        v.opened = o
        if o.unpacked:
            title = f"{e.name}  →  {o.name}\nunpacked in memory: {human_size(o.raw_len)} → {human_size(len(o.data))}"
        else:
            title = f"{e.name}\nnot obfuscated"
        body = await asyncio.to_thread(describe, o.name, o.data, o.kind)
        acts = [("ql", "Quick Look")]
        if o.kind.openable:
            acts.append(("open", "Open"))
        if o.unpacked or v.stack:
            acts.append(("save", "Unpack…" if not v.stack else "Extract…"))
        else:
            acts.append(("pack", "Obfuscate…"))
        acts.append(("close", "Close"))
        await v.bp.set_preview(f"{title}\n{'─' * 24}\n{body}", acts)

    async def open_entry(self, v: View, e: Entry) -> None:
        if e.is_dir:
            if v.stack:
                layer = v.stack[-1]
                layer.sub = (layer.sub + "/" + e.name).strip("/")
                v.opened = None
                await self.render(v, self.list_layer(v))
            else:
                await self.go_fs(v, e.data, follow=False)
            return
        o = v.opened if v.opened is not None and v.opened.entry is e else None
        if o is None:
            try:
                o = await self.open_contents(v, e)
            except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError, KeyError) as err:
                await v.panel.async_set_status(f"{e.name}: {err}")
                return
        data, name, kind = o.data, o.name, o.kind
        # Peel further obfus layers (a.zip.obfus.obfus).
        while kind.family == "obfus":
            data = await asyncio.to_thread(obfus.unpack, data)
            name = obfus.inner_name(name)
            kind = sniff(name, data)
        if not kind.is_archive:
            v.opened = o
            await self.preview(v, e)
            return
        try:
            tree = await asyncio.to_thread(open_tree, e.name, kind, data)
        except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError) as err:
            await v.panel.async_set_status(f"{e.name}: {err}")
            return
        v.follow = False
        v.stack.append(Layer(e.name, tree))
        v.opened = None
        await self.render(v, self.list_layer(v))

    async def up(self, v: View) -> None:
        if v.stack:
            layer = v.stack[-1]
            if layer.sub:
                layer.sub = layer.sub.rpartition("/")[0]
            else:
                v.stack.pop()
            v.opened = None
            if v.stack:
                await self.render(v, self.list_layer(v))
            else:
                await self.go_fs(v, v.fs_dir or "~", follow=False)
        elif v.fs_dir:
            await self.go_fs(v, os.path.dirname(v.fs_dir) or "/", follow=False)

    # ------------------------------------------------------------------
    # writing (only on request)

    async def ask_name(self, v: View, question: str, default: str) -> Optional[str]:
        while True:
            ans = await v.bp.ask(question, [("cancel", "Cancel")], prefill=default)
            if ans is None or ans[0] == "action" or not ans[1].strip():
                await v.bp.set_preview("Cancelled — nothing was saved.", [("close", "Close")])
                return None
            try:
                return valid_name(ans[1])
            except ValueError as e:
                question = f"{e}.\n{question}"

    async def write_new(self, v: View, name: str, data: bytes, source: Optional[str]) -> Optional[str]:
        assert v.fs_dir is not None
        while True:
            dest = os.path.join(v.fs_dir, name)
            if source and os.path.realpath(dest) == os.path.realpath(source):
                name = await self.ask_name(v, "That would overwrite the original. Choose another name:", name + ".out") or ""
                if not name:
                    return None
                continue
            if os.path.lexists(dest):
                ans = await v.bp.ask(f"{name} already exists here. Overwrite it, or type another name:",
                                     [("overwrite", "Overwrite"), ("cancel", "Cancel")], prefill=name)
                if ans is None or ans == ("action", "cancel"):
                    await v.bp.set_preview("Cancelled — nothing was saved.", [("close", "Close")])
                    return None
                if ans[0] == "input":
                    try:
                        new = valid_name(ans[1])
                    except ValueError as e:
                        await v.panel.async_set_status(str(e))
                        continue
                    if new != name:
                        name = new
                        continue
            break

        def write() -> None:
            fd, tmp = tempfile.mkstemp(prefix=".zenobfus-", dir=v.fs_dir)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.chmod(tmp, 0o644)
                os.replace(tmp, dest)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

        try:
            await asyncio.to_thread(write)
        except OSError as e:
            await v.panel.async_set_status(f"couldn't save {name}: {e.strerror or e}")
            return None
        return dest

    async def save_unpacked(self, v: View, e: Entry) -> None:
        o = v.opened if v.opened is not None and v.opened.entry is e else None
        if o is None:
            try:
                o = await self.open_contents(v, e)
            except (OSError, ValueError, KeyError) as err:
                await v.panel.async_set_status(f"{e.name}: {err}")
                return
        verb = "Extract" if v.stack else "Unpack"
        name = await self.ask_name(
            v, f"{verb} {e.name} ({human_size(len(o.data))}) to {display_path(v.fs_dir or '')} as:\n"
               "Enter saves, empty or Cancel saves nothing.", o.name)
        if name is None:
            return
        dest = await self.write_new(v, name, o.data, None if v.stack else e.data)
        if dest:
            await self.after_write(v, f"Saved {display_path(dest)}.")

    async def save_packed(self, v: View, e: Entry) -> None:
        if v.stack:
            return
        try:
            data = await self.read_entry(v, e)
        except (OSError, ValueError) as err:
            await v.panel.async_set_status(f"{e.name}: {err}")
            return
        if obfus.is_obfus(data):
            ans = await v.bp.ask(f"{e.name} is already an obfus blob. Obfuscating it again means it needs two\n"
                                 "unpacks to read. Do it anyway?", [("again", "Obfuscate again"), ("cancel", "Cancel")])
            if ans != ("action", "again"):
                await v.bp.set_preview("Cancelled — nothing was saved.", [("close", "Close")])
                return
        name = await self.ask_name(
            v, f"Save an obfuscated copy of {e.name} to {display_path(v.fs_dir or '')} as:\n"
               f"The original is kept. {NOTICE}\nEnter saves, empty or Cancel saves nothing.", e.name + obfus.EXT)
        if name is None:
            return
        packed = await asyncio.to_thread(obfus.pack, data)
        dest = await self.write_new(v, name, packed, e.data)
        if dest:
            await self.after_write(v, f"Saved {display_path(dest)}.")

    async def after_write(self, v: View, msg: str) -> None:
        if not v.stack and v.fs_dir:
            await self.go_fs(v, v.fs_dir)
        await v.bp.set_preview(msg, [("close", "Close")])

    async def quick_look(self, v: View, e: Entry) -> None:
        o = v.opened if v.opened is not None and v.opened.entry is e else None
        if o is None:
            try:
                o = await self.open_contents(v, e)
            except (OSError, ValueError, KeyError) as err:
                await v.panel.async_set_status(f"{e.name}: {err}")
                return
        if not o.unpacked and not v.stack:
            subprocess.Popen(["/usr/bin/qlmanage", "-p", e.data], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        if self.ql_dir is None:
            try:
                base = await zenteru.app.async_transient_dir(self.connection)
            except RPCError:
                base = tempfile.mkdtemp(prefix="zenobfus-")
            self.ql_dir = os.path.join(base, "zenobfus")
            os.makedirs(self.ql_dir, mode=0o700, exist_ok=True)
        self._ql_n += 1
        d = os.path.join(self.ql_dir, str(self._ql_n))
        os.makedirs(d, mode=0o700)
        path = os.path.join(d, valid_name(os.path.basename(o.name)) if o.name else "contents")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(o.data)
        subprocess.Popen(["/usr/bin/qlmanage", "-p", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await v.panel.async_set_status("Quick Look uses a temporary copy that zenteru deletes when it quits.")

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
        except Exception as e:
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
                v.opened = None
                await v.bp.clear_preview()
            elif v.opened is not None:
                await self.on_entry(v, v.opened.entry, a)

    async def on_toolbar(self, v: View, a: Optional[str]) -> None:
        if a == "up":
            await self.up(v)
        elif a == "refresh":
            await self.refresh(v)
        elif a == "home":
            await self.go_fs(v, "~", follow=False)
        elif a == "follow":
            v.follow = True
            v.located = None
        elif a == "hidden":
            self.config["show_hidden"] = not self.show_hidden
            save_config(self.config)
            await self.refresh(v)
        elif a == "help":
            await v.bp.set_preview(HELP, [("close", "Close")])

    async def on_entry(self, v: View, e: Entry, a: Optional[str]) -> None:
        if a == "open":
            await self.open_entry(v, e)
        elif a == "preview":
            await self.preview(v, e)
        elif a == "save":
            await self.save_unpacked(v, e)
        elif a == "pack":
            await self.save_packed(v, e)
        elif a == "ql":
            await self.quick_look(v, e)

    async def on_input(self, v: View, text: str) -> None:
        if not text:
            return
        cmd, _, arg = text.partition(" ")
        if text in ("..", "up"):
            await self.up(v)
        elif cmd == "cd":
            target = arg.strip() or "~"
            if not target.startswith(("/", "~")):
                target = os.path.join(v.fs_dir or "~", target)
            await self.go_fs(v, target, follow=False)
        elif text in ("help", "?"):
            await self.on_toolbar(v, "help")
        elif text == "hidden":
            await self.on_toolbar(v, "hidden")
        elif text == "follow":
            await self.on_toolbar(v, "follow")
        else:
            e = v.bp.find(text)
            if e is None:
                await v.panel.async_set_status(f"no {text!r} here — type ? for help")
            elif e.is_dir or self._archive_name(e.name):
                await self.open_entry(v, e)
            else:
                await self.preview(v, e)

    # ------------------------------------------------------------------

    async def cmd_toggle(self, inv: CommandInvocation) -> None:
        v = self.views.get(inv.window_id)
        if v is not None and not v.panel.closed and v.visible and not inv.line.strip():
            await v.panel.async_hide()
            v.visible = False
            return
        v = await self.view_for(inv.window_id)
        arg = inv.line.strip()
        if arg:
            path = os.path.expanduser(arg)
            if os.path.isfile(path):
                await self.go_fs(v, os.path.dirname(os.path.abspath(path)), follow=False)
                e = v.bp.find(os.path.basename(path))
                if e is not None:
                    await self.preview(v, e)
                return
            await self.go_fs(v, path, follow=False)
            return
        sid = inv.session_id or await self.active_session(inv.window_id)
        if v.fs_dir is None or v.follow:
            cwd = await self.session_cwd(sid) if sid else os.path.expanduser("~")
            v.located = cwd
            await self.go_fs(v, cwd)

    def cleanup(self) -> None:
        if self.ql_dir:
            import shutil

            shutil.rmtree(self.ql_dir, ignore_errors=True)


async def main(connection: Connection) -> None:
    plugin = ZenObfus(connection)
    await plugin.async_register()
    try:
        await connection.async_wait_closed()
    finally:
        plugin.cleanup()
