"""browserpanel — a file-browser layout for zenteru side panels.

zenteru panels are a chat-style column: a list of messages, each with
optional buttons. This module arranges one as a browser:

    ┌───────────────────────────────┐
    │ header   path + toolbar       │  top: the browser
    │ entry    ▸ src/        [Open] │
    │ entry    · main.py  [Preview] │
    │ pager    61–120 of 300        │
    ├───────────────────────────────┤
    │ preview  (text, listing, …)   │  bottom: the preview
    └───────────────────────────────┘

A directory is rendered by clearing the panel and appending messages in
that order, so the preview is always the last message; selecting another
entry only updates the preview in place.

The canonical copy is ``shared/browserpanel.py`` in team-mirae/zenteru-plugins.
Plugins carry identical copies (they are installed independently); CI
fails if a copy drifts.
"""

from __future__ import annotations

import asyncio
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence

__all__ = [
    "Entry", "BrowserPanel", "human_size", "display_name", "format_entry",
    "decode_text", "text_preview", "hex_preview", "image_kind", "image_block",
]

PAGE_SIZE = 60
PREVIEW_IDLE = "Select a file to preview it here."


@dataclass
class Entry:
    """One row of the browser."""

    name: str
    is_dir: bool
    size: Optional[int] = None
    mtime: Optional[float] = None
    is_link: bool = False
    #: Short tag shown after the name, e.g. ``obfus`` or ``zip``.
    badge: str = ""
    #: Anything the backend wants to keep with the row.
    data: Any = None


def human_size(n: Optional[int]) -> str:
    if n is None:
        return ""
    size = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"  # pragma: no cover


def display_name(name: str) -> str:
    """Make a file name safe to draw: control characters become ``?``."""
    return "".join("?" if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") else c for c in name)


def _age(mtime: Optional[float], now: Optional[float] = None) -> str:
    if mtime is None:
        return ""
    now = time.time() if now is None else now
    t = time.localtime(mtime)
    if abs(now - mtime) < 180 * 86400:
        return time.strftime("%m-%d %H:%M", t)
    return time.strftime("%Y-%m-%d", t)


def format_entry(e: Entry, now: Optional[float] = None) -> str:
    name = display_name(e.name)
    if e.is_dir:
        line = f"▸ {name}/"
    else:
        line = f"· {name}"
    if e.is_link:
        line += " ↪"
    if e.badge:
        line += f"  [{e.badge}]"
    meta = "  ".join(x for x in (("" if e.is_dir else human_size(e.size)), _age(e.mtime, now)) if x)
    if meta:
        line += f"   {meta}"
    return line


# -- previews ---------------------------------------------------------------


def decode_text(data: bytes) -> Optional[str]:
    """``data`` as text if it looks like text (UTF-8, no NULs), else None.

    A multi-byte character cut off at the end of a partial read is fine.
    """
    if b"\0" in data[:8192]:
        return None
    for cut in range(0, 4):
        chunk = data[: len(data) - cut] if cut else data
        try:
            text = chunk.decode("utf-8")
            break
        except UnicodeDecodeError as e:
            if e.start < len(data) - 4:
                return None
    else:
        return None
    if not text:
        return text
    bad = sum(1 for c in text[:8192] if unicodedata.category(c) == "Cc" and c not in "\t\n\r\f\b\x1b")
    return None if bad > len(text[:8192]) * 0.02 else text


def text_preview(text: str, total: Optional[int] = None, max_lines: int = 40, width: int = 160) -> str:
    lines = text.splitlines()
    shown = []
    for line in lines[:max_lines]:
        line = display_name(line.expandtabs(4))
        shown.append(line if len(line) <= width else line[: width - 1] + "…")
    more = len(lines) > max_lines or (total is not None and total > len(text.encode("utf-8", "surrogatepass")))
    body = "\n".join(shown) if shown else "(empty)"
    if more:
        body += "\n…"
    return body


def hex_preview(data: bytes, n: int = 256) -> str:
    rows = []
    for off in range(0, min(len(data), n), 16):
        chunk = data[off:off + 16]
        hx = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        rows.append(f"{off:08x}  {hx:<47}  {asc}")
    if len(data) > n:
        rows.append("…")
    return "\n".join(rows) if rows else "(empty)"


# -- images (zenteru 0.1.17+ panel blocks) ----------------------------------

#: Largest image sent inline (base64 over the API socket). zenteru itself
#: decodes up to 64 MiB; files on this Mac are passed by path instead.
IMAGE_INLINE_MAX = 32 * 1024 * 1024
#: Preview images are at most this many cells tall, so the listing stays visible.
IMAGE_MAX_HEIGHT = 18


def image_kind(head: bytes) -> Optional[str]:
    """A label if ``head`` starts like an image macOS can decode, else None."""
    h = head[:32]
    if h.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG image"
    if h.startswith(b"\xff\xd8\xff"):
        return "JPEG image"
    if h[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF image"
    if h[:4] == b"RIFF" and h[8:12] == b"WEBP":
        return "WebP image"
    if h[:4] in (b"II*\0", b"MM\0*"):
        return "TIFF image"
    if h[:2] == b"BM" and len(h) >= 18 and int.from_bytes(h[14:18], "little") in (12, 40, 52, 56, 108, 124):
        return "BMP image"
    if h[4:8] == b"ftyp" and h[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"hevc", b"heim", b"heis"):
        return "HEIC image"
    if h[4:8] == b"ftyp" and h[8:12] in (b"avif", b"avis"):
        return "AVIF image"
    if h[:4] == b"\0\0\1\0":
        return "ICO image"
    if h.startswith(b"%PDF-"):
        return "PDF document"
    return None


def image_block(path: Optional[str] = None, data: Optional[bytes] = None, alt: str = "image",
                max_height: int = IMAGE_MAX_HEIGHT) -> dict:
    """A zenteru image block from a file on this Mac or from bytes."""
    import base64
    import os

    b: dict[str, Any] = {"type": "image", "alt": alt, "max_height": int(max_height)}
    if path is not None:
        b["path"] = os.path.abspath(path)
    elif data is not None:
        b["base64"] = base64.b64encode(bytes(data)).decode("ascii")
    else:
        raise ValueError("image_block needs a path or data")
    return b


ActionsFor = Callable[[Entry], Sequence[tuple[str, str]]]


class BrowserPanel:
    """Lays a directory listing and a preview out in a :class:`zenteru.Panel`.

    The owner handles panel events itself and asks :meth:`classify` which
    part of the layout a message id belongs to.
    """

    def __init__(self, panel: Any, page_size: int = PAGE_SIZE) -> None:
        self.panel = panel
        self.page_size = page_size
        self.header_id: Any = None
        self.pager_id: Any = None
        self.preview_id: Any = None
        self.notice_ids: list[Any] = []
        self.rows: dict[Any, Entry] = {}
        self.entries: list[Entry] = []
        self.page = 0
        self._header: tuple[str, list[tuple[str, str]]] = ("", [])
        self._actions_for: Optional[ActionsFor] = None
        self._preview: tuple[str, list[tuple[str, str]], list[dict]] = (PREVIEW_IDLE, [], [])
        self._question: Optional[asyncio.Future] = None
        self._formatter: Optional[Callable[[Entry], str]] = None
        #: Serializes panel writes: a redraw clears the panel, so an update
        #: that interleaved with it would target a message that is gone.
        self._lock = asyncio.Lock()

    # -- layout ---------------------------------------------------------

    @property
    def pages(self) -> int:
        return max(1, -(-len(self.entries) // self.page_size))

    async def render(
        self,
        header: str,
        header_actions: Iterable[tuple[str, str]],
        entries: Sequence[Entry],
        actions_for: ActionsFor,
        page: int = 0,
        keep_preview: bool = False,
        formatter: Optional[Callable[[Entry], str]] = None,
    ) -> None:
        """Replace the whole panel with a new listing.

        ``formatter`` draws a row (default :func:`format_entry`)."""
        self._formatter = formatter
        self._header = (header, list(header_actions))
        self.entries = list(entries)
        self._actions_for = actions_for
        self.page = min(max(page, 0), self.pages - 1)
        if not keep_preview and not self.asking:
            self._preview = (PREVIEW_IDLE, [], [])
        async with self._lock:
            await self._draw()

    async def set_page(self, page: int) -> None:
        if self._actions_for is None:
            return
        self.page = min(max(page, 0), self.pages - 1)
        async with self._lock:
            await self._draw()

    async def _draw(self) -> None:
        p = self.panel
        await p.async_clear()
        self.rows.clear()
        self.notice_ids.clear()
        self.pager_id = None
        header, actions = self._header
        self.header_id = await p.async_append("assistant", header, actions=actions)
        start = self.page * self.page_size
        shown = self.entries[start:start + self.page_size]
        if not self.entries:
            self.notice_ids.append(await p.async_append("tool", "(empty)"))
        now = time.time()
        assert self._actions_for is not None
        for e in shown:
            mid = await p.async_append("tool", self._format(e, now), actions=list(self._actions_for(e)))
            self.rows[mid] = e
        if self.pages > 1:
            pager_actions = []
            if self.page > 0:
                pager_actions.append(("prev", "‹ Prev"))
            if self.page < self.pages - 1:
                pager_actions.append(("next", "Next ›"))
            self.pager_id = await p.async_append(
                "system",
                f"{start + 1}–{start + len(shown)} of {len(self.entries)}",
                actions=pager_actions,
            )
        await self._send_preview(append=True)

    def _format(self, e: Entry, now: Optional[float] = None) -> str:
        return self._formatter(e) if self._formatter is not None else format_entry(e, now)

    async def set_header(self, header: str, actions: Optional[Iterable[tuple[str, str]]] = None) -> None:
        acts = list(actions) if actions is not None else self._header[1]
        self._header = (header, acts)
        async with self._lock:
            if self.header_id is not None:
                await self.panel.async_update(self.header_id, text=header, actions=acts)

    async def set_preview(self, text: str, actions: Iterable[tuple[str, str]] = (),
                          blocks: Sequence[dict] = ()) -> None:
        """Show ``text`` in the preview slot (the bottom message).

        ``blocks`` (zenteru 0.1.17+) adds rich content after the text, e.g.
        an :func:`image_block`. If zenteru can't show them (it can't decode
        the image, or it is older), the preview falls back to the text.
        """
        self._preview = (text, list(actions), list(blocks))
        async with self._lock:
            await self._send_preview(append=self.preview_id is None)

    async def _send_preview(self, append: bool) -> None:
        text, acts, blocks = self._preview
        params: dict[str, Any] = {"panel_id": self.panel.panel_id, "text": text,
                                  "actions": [{"id": a, "label": l} for a, l in acts], "blocks": blocks}
        if append:
            params["role"] = "system"
        else:
            params["message_id"] = self.preview_id
        method = "panel.append" if append else "panel.update"
        try:
            r = await self.panel.connection.async_call(method, params)
        except Exception as e:  # zenteru refused a block (e.g. an image it can't decode)
            if not blocks or getattr(e, "code", None) not in ("bad_request", "unsupported"):
                raise
            note = getattr(e, "message", None) or str(e)
            params["blocks"] = [b for b in blocks if b.get("type") != "image"]
            params["text"] = f"{text}\n(image not shown: {note})"
            self._preview = (params["text"], acts, params["blocks"])
            r = await self.panel.connection.async_call(method, params)
        if append:
            self.preview_id = (r or {}).get("message_id")

    async def clear_preview(self) -> None:
        await self.set_preview(PREVIEW_IDLE)

    async def update_row(self, entry: Entry) -> None:
        """Redraw one entry's row (after its badge or size changed)."""
        async with self._lock:
            for mid, e in self.rows.items():
                if e is entry:
                    assert self._actions_for is not None
                    await self.panel.async_update(mid, text=self._format(e), actions=list(self._actions_for(e)))
                    return

    # -- questions ------------------------------------------------------

    @property
    def asking(self) -> bool:
        return self._question is not None and not self._question.done()

    async def ask(
        self,
        text: str,
        actions: Iterable[tuple[str, str]] = (),
        prefill: Optional[str] = None,
    ) -> Optional[tuple[str, str]]:
        """Ask in the preview slot; returns ``("input", text)``,
        ``("action", id)`` or None (panel closed, or superseded by
        another question). The owner must pass events to :meth:`feed`.

        ``prefill`` puts an editable answer in the input box, so Enter
        accepts it: a question doubles as a confirmation step.
        """
        old = getattr(self, "_question", None)
        if old is not None and not old.done():
            old.set_result(None)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._question = fut
        try:
            await self.set_preview(text, actions)
            if prefill is not None:
                await self.panel.connection.async_call(
                    "panel.set_input", {"panel_id": self.panel.panel_id, "text": prefill}
                )
            return await fut
        finally:
            if self._question is fut:
                self._question = None

    def feed(self, event: Any) -> bool:
        """Offer a panel event to a pending question; True if it was the answer."""
        q = self._question
        if q is None or q.done():
            return False
        if event.type == "input":
            q.set_result(("input", event.text or ""))
            return True
        if event.type == "action" and event.message_id == self.preview_id:
            q.set_result(("action", event.action_id))
            return True
        if event.type == "closed":
            q.set_result(None)
        return False

    # -- events ---------------------------------------------------------

    def classify(self, message_id: Any) -> tuple[str, Optional[Entry]]:
        """``("header"|"entry"|"pager"|"preview"|"other", entry)``."""
        if message_id is None:
            return "other", None
        if message_id == self.header_id:
            return "header", None
        if message_id == self.pager_id:
            return "pager", None
        if message_id == self.preview_id:
            return "preview", None
        e = self.rows.get(message_id)
        if e is not None:
            return "entry", e
        return "other", None

    def find(self, name: str) -> Optional[Entry]:
        for e in self.entries:
            if e.name == name:
                return e
        return None
