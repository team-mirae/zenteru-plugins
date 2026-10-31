"""Test scaffolding: puts each plugin package and zenteru's Python client on sys.path.

The panel tests drive the plugins through zenteru's own mock API server
(``python/tests/mock_server.py`` in team-mirae/zenteru). It is found via
``ZENTERU_SRC``, else ``../zenteru`` next to this checkout, else
``_zenteru`` inside it (where CI checks it out). Without it those tests
are skipped; the pure-logic tests still run.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from typing import Any, Callable, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for plugin in ("zenfinder", "zenobfus", "zentail"):
    p = os.path.join(ROOT, plugin)
    if p not in sys.path:
        sys.path.insert(0, p)


def _zenteru_src() -> Optional[str]:
    for c in (os.environ.get("ZENTERU_SRC"), os.path.join(os.path.dirname(ROOT), "zenteru"), os.path.join(ROOT, "_zenteru")):
        if c and os.path.isfile(os.path.join(c, "python", "zenteru", "__init__.py")):
            return c
    return None


ZENTERU_SRC = _zenteru_src()
if ZENTERU_SRC:
    for p in (os.path.join(ZENTERU_SRC, "python"), os.path.join(ZENTERU_SRC, "python", "tests")):
        if p not in sys.path:
            sys.path.insert(0, p)

try:
    import zenteru  # noqa: F401
    from mock_server import MockError, MockServer

    HAVE_ZENTERU = True
except ImportError:  # pragma: no cover
    HAVE_ZENTERU = False
    MockServer = object  # type: ignore
    MockError = Exception  # type: ignore

needs_zenteru = unittest.skipUnless(HAVE_ZENTERU, "zenteru source not found (set ZENTERU_SRC)")

import logging  # noqa: E402

for name in ("zenteru", "zenfinder", "zenobfus", "zentail"):
    logging.getLogger(name).setLevel(logging.CRITICAL)


if HAVE_ZENTERU:

    class Server(MockServer):  # type: ignore[misc, valid-type]
        """The zenteru mock plus what these plugins use that it lacks."""

        def __init__(self) -> None:
            super().__init__()
            self.job_names: dict[str, str] = {}
            self.inputs: list[dict] = []

        def m_panel_set_input(self, c: Any, p: dict) -> dict:
            self._panel(p["panel_id"])
            self.inputs.append(p)
            return {}

        #: Set to make image blocks fail like an undecodable image does in zenteru.
        reject_images = False

        def _check_blocks(self, p: dict) -> None:
            for b in p.get("blocks") or []:
                if b.get("type") == "image" and self.reject_images:
                    raise MockError("bad_request", "image: can't decode")

        def m_panel_append(self, c: Any, p: dict) -> dict:
            self._check_blocks(p)
            r = super().m_panel_append(c, p)
            self._panel(p["panel_id"])["messages"][r["message_id"]]["blocks"] = list(p.get("blocks") or [])
            return r

        def m_panel_update(self, c: Any, p: dict) -> dict:
            self._check_blocks(p)
            r = super().m_panel_update(c, p)
            if "blocks" in p:
                self._panel(p["panel_id"])["messages"][p["message_id"]]["blocks"] = list(p["blocks"] or [])
            return r

        def m_session_get_variable(self, c: Any, p: dict) -> dict:
            if p.get("name") == "session.jobName" and p.get("session_id") in self.job_names:
                return {"value": self.job_names[p["session_id"]]}
            return super().m_session_get_variable(c, p)

    class PanelTest(unittest.IsolatedAsyncioTestCase):
        async def asyncSetUp(self) -> None:
            self.server = Server()
            await self.server.start()
            self.conn = await zenteru.Connection.async_create(self.server.socket_path, self.server.cookie, name="test")
            self.window_id = self.server.current_window_id
            win = self.server._window(self.window_id)
            self.tab_id = win["tabs"][0]
            self.session_id = self.server._tab(self.tab_id)["sessions"][0]

        async def asyncTearDown(self) -> None:
            await self.conn.async_close()
            await self.server.stop()

        def session(self) -> dict:
            return self.server.sessions[self.session_id]

        def args(self, **extra: Any) -> dict:
            return {"session_id": self.session_id, "window_id": self.window_id, "tab_id": self.tab_id, **extra}

        async def invoke(self, handler: str, **extra: Any) -> Any:
            return await self.server.invoke(handler, self.args(**extra))

        async def until(self, predicate: Callable[[], Any], timeout: float = 5) -> None:
            await self.server.wait_for(predicate, timeout)

        def panel_id(self) -> str:
            ids = [pid for pid, p in self.server.panels.items() if not p["destroyed"]]
            assert ids, "no panel"
            return ids[-1]

        def texts(self) -> list[str]:
            return self.server.panel_texts(self.panel_id())

        def messages(self) -> list[tuple[Any, dict]]:
            p = self.server.panels[self.panel_id()]
            return [(m, p["messages"][m]) for m in p["order"]]

        def find_row(self, needle: str) -> Any:
            for mid, m in self.messages():
                if needle in m["text"]:
                    return mid
            raise AssertionError(f"no row with {needle!r} in {self.texts()}")

        def last(self) -> dict:
            """The bottom message ({} while the panel is being redrawn)."""
            m = self.messages()
            return m[-1][1] if m else {"text": "", "actions": []}

        def status(self) -> str:
            return self.server.panels[self.panel_id()]["status"]

        async def click(self, needle: str, action: str) -> None:
            await self.server.panel_action(self.panel_id(), self.find_row(needle), action)

        async def click_last(self, action: str) -> None:
            await self.server.panel_action(self.panel_id(), self.messages()[-1][0], action)

        async def type(self, text: str) -> None:
            await self.server.panel_input(self.panel_id(), text)

        async def settle(self, n: int = 10) -> None:
            for _ in range(n):
                await asyncio.sleep(0.01)
