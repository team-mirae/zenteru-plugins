import asyncio
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

import _zt  # noqa: F401  (sys.path)
from zenfinder import backends, sshargs
from zenfinder.config import Config, normalize_ext, paste_name

FAKE_SSH = r"""#!/bin/sh
# A stand-in for ssh: `-G` prints a config, anything else runs the remote
# command (the last argument) locally, the way a login shell would.
for a; do last=$a; done
case " $* " in *" -G "*) echo "hostname fake.example"; echo "controlpath none"; exit 0;; esac
if [ -n "${FAKE_SSH_DENY:-}" ]; then echo "fake@host: Permission denied (publickey)." >&2; exit 255; fi
exec sh -c "$last"
"""


PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000002000000020802000000fdd49a730000001149444154789c63f8cfc000440c60f23f001bf203fdf3e74bb10000000049454e44ae426082")


def read(path: str) -> str:
    with open(path) as f:
        return f.read()


def fake_ssh_dir() -> str:
    d = tempfile.mkdtemp(prefix="zf-bin-")
    p = os.path.join(d, "ssh")
    with open(p, "w") as f:
        f.write(FAKE_SSH)
    os.chmod(p, 0o755)
    return d


class SshArgsTest(unittest.TestCase):
    def test_simple(self):
        t = sshargs.parse_ssh_argv(["ssh", "me@box"])
        self.assertEqual((t.destination, t.options, t.command), ("me@box", [], None))

    def test_keeps_connection_options_drops_session_ones(self):
        t = sshargs.parse_ssh_argv(
            ["ssh", "-p", "2222", "-i", "~/.ssh/k", "-L", "8080:localhost:80", "-A", "-t", "-o", "User=me",
             "-oLocalForward=1 a:2", "-J", "jump", "-4", "host", "tmux", "a"])
        self.assertEqual(t.destination, "host")
        self.assertEqual(t.options, ["-p", "2222", "-i", "~/.ssh/k", "-o", "User=me", "-J", "jump", "-4"])
        self.assertEqual(t.command, ["tmux", "a"])

    def test_glued_and_combined_flags(self):
        t = sshargs.parse_ssh_argv(["ssh", "-4Cp2200", "h"])
        self.assertEqual(t.options, ["-4", "-C", "-p", "2200"])

    def test_not_a_login(self):
        for argv in (["ssh"], ["ssh", "-V"], ["ssh", "-G", "h"], ["ssh", "-N", "-L", "1:a:2", "h"], ["ssh", "-W", "a:22", "j"], ["ssh", "-p"]):
            self.assertIsNone(sshargs.parse_ssh_argv(argv), argv)

    def test_foreground_ssh_from_ps(self):
        ps = "  501 Ss   -zsh\n  777 S+   ssh -p 22 me@box\n  780 S    ssh other\n"
        t = sshargs.foreground_ssh(sshargs.parse_ps(ps))
        self.assertEqual(t.destination, "me@box")
        self.assertIsNone(sshargs.foreground_ssh(sshargs.parse_ps("  501 S+  vim x\n")))

    def test_prompt_guess(self):
        g = sshargs.remote_cwd_from_prompt
        self.assertEqual(g("me@box:~/src/app$ "), "~/src/app")
        self.assertEqual(g("root@web-1:/etc/nginx# "), "/etc/nginx")
        self.assertEqual(g("[me@box ~/x]$ "), "~/x")
        self.assertEqual(g("me@box ~/y % "), "~/y")
        self.assertIsNone(g("[me@box x]$ "))
        self.assertIsNone(g("$ "))

    def test_connection_options(self):
        mine = sshargs.connection_options({"controlpath": "none"})
        self.assertIn(f"ControlPath={sshargs.control_dir()}/%C", mine)
        theirs = sshargs.connection_options({"controlpath": "/Users/me/.ssh/cm-abc"})
        self.assertFalse(any(o.startswith("ControlPath=") for o in theirs))
        self.assertIn("BatchMode=yes", theirs)
        auth = sshargs.auth_command(sshargs.SshTarget("h", ["-p", "1"]), {"controlpath": "none"})
        self.assertEqual(auth[-3:], ["-f", "-N", "h"])


class ConfigTest(unittest.TestCase):
    def test_ext(self):
        self.assertEqual(normalize_ext(" .md "), "md")
        for bad in ("", ".", "a/b", "x" * 20, "-x"):
            with self.assertRaises(ValueError):
                normalize_ext(bad)

    def test_paste_name(self):
        self.assertEqual(paste_name("notes", "txt"), "notes.txt")
        self.assertEqual(paste_name("notes.md", "txt"), "notes.md")
        self.assertEqual(paste_name(".env", "txt"), ".env.txt")

    def test_roundtrip(self):
        d = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"ZENFINDER_DATA_DIR": d}):
            c = Config(paste_ext="md", show_hidden=True)
            c.save()
            self.assertEqual(Config.load(), c)
        shutil.rmtree(d)


class ListScriptTest(unittest.TestCase):
    """The remote listing script, run here with the local sh/find/stat."""

    def test_listing(self):
        d = tempfile.mkdtemp()
        os.mkdir(os.path.join(d, "sub dir"))
        with open(os.path.join(d, "a\tb.txt"), "w") as f:
            f.write("hello")
        with open(os.path.join(d, ".hidden"), "w") as f:
            f.write("x")
        os.symlink("sub dir", os.path.join(d, "link"))
        raw = subprocess.run(["sh", "-c", backends.LIST_SCRIPT, "zenfinder", d], capture_output=True, check=True).stdout
        cwd, entries = backends.parse_listing(raw)
        self.assertEqual(os.path.realpath(cwd), os.path.realpath(d))
        by = {e.name: e for e in entries}
        self.assertEqual(set(by), {"sub dir", "a\tb.txt", ".hidden", "link"})
        self.assertTrue(by["sub dir"].is_dir)
        self.assertTrue(by["link"].is_dir and by["link"].is_link)
        self.assertEqual(by["a\tb.txt"].size, 5)
        self.assertIsNotNone(by["a\tb.txt"].mtime)
        shutil.rmtree(d)

    def test_tilde(self):
        raw = subprocess.run(["sh", "-c", backends.LIST_SCRIPT, "zenfinder", "~"], capture_output=True, check=True,
                             env=dict(os.environ, HOME=tempfile.gettempdir())).stdout
        self.assertEqual(os.path.realpath(backends.parse_listing(raw)[0]), os.path.realpath(tempfile.gettempdir()))


class RemoteBackendTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bin = fake_ssh_dir()
        self.env = mock.patch.dict(os.environ, {"PATH": self.bin + os.pathsep + os.environ["PATH"]})
        self.env.start()
        self.remote = tempfile.mkdtemp(prefix="zf-remote-")
        self.local = tempfile.mkdtemp(prefix="zf-local-")
        self.b = backends.RemoteBackend(sshargs.SshTarget("fake", ["-p", "2222"]))

    async def asyncTearDown(self):
        self.env.stop()
        for d in (self.bin, self.remote, self.local):
            shutil.rmtree(d, ignore_errors=True)

    async def test_round_trip(self):
        await self.b.write_bytes(self.remote, "it's.txt", b"one")
        with self.assertRaises(backends.Exists):
            await self.b.write_bytes(self.remote, "it's.txt", b"two")
        await self.b.write_bytes(self.remote, "it's.txt", b"two", overwrite=True)
        cwd, entries = await self.b.list(self.remote)
        self.assertEqual([e.name for e in entries], ["it's.txt"])
        self.assertEqual(await self.b.read_head(os.path.join(self.remote, "it's.txt")), b"two")
        self.assertTrue(await self.b.exists(os.path.join(self.remote, "it's.txt")))
        self.assertFalse(await self.b.exists(os.path.join(self.remote, "nope")))
        await self.b.mkdir(self.remote, "new")
        with self.assertRaises(backends.Exists):
            await self.b.mkdir(self.remote, "new")

    async def test_upload_and_download_files_and_dirs(self):
        src = os.path.join(self.local, "pkg")
        os.makedirs(os.path.join(src, "inner"))
        with open(os.path.join(src, "inner", "x.bin"), "wb") as f:
            f.write(os.urandom(5000))
        single = os.path.join(self.local, "single.dat")
        with open(single, "wb") as f:
            f.write(b"\0\1\2" * 1000)
        await self.b.upload(src, self.remote, "pkg")
        await self.b.upload(single, self.remote, "single.dat")
        with open(os.path.join(self.remote, "pkg", "inner", "x.bin"), "rb") as a, open(os.path.join(src, "inner", "x.bin"), "rb") as b:
            self.assertEqual(a.read(), b.read())
        with self.assertRaises(backends.Exists):
            await self.b.upload(src, self.remote, "pkg")
        dl = os.path.join(self.local, "dl")
        p1 = await self.b.download(os.path.join(self.remote, "single.dat"), False, dl)
        p2 = await self.b.download(os.path.join(self.remote, "single.dat"), False, dl)
        self.assertEqual(os.path.basename(p2), "single (2).dat")
        p3 = await self.b.download(os.path.join(self.remote, "pkg"), True, dl)
        with open(p1, "rb") as f:
            self.assertEqual(f.read(), b"\0\1\2" * 1000)
        self.assertTrue(os.path.isfile(os.path.join(p3, "inner", "x.bin")))
        self.assertEqual(sorted(os.listdir(dl)), ["pkg", "single (2).dat", "single.dat"])

    async def test_auth_failure(self):
        with mock.patch.dict(os.environ, {"FAKE_SSH_DENY": "1"}):
            with self.assertRaises(backends.BackendError) as cm:
                await self.b.list("~")
        self.assertTrue(cm.exception.auth)


class LocalBackendTest(unittest.IsolatedAsyncioTestCase):
    async def test_local(self):
        d = tempfile.mkdtemp()
        b = backends.LocalBackend()
        await b.write_bytes(d, "a.txt", b"x")
        with self.assertRaises(backends.Exists):
            await b.write_bytes(d, "a.txt", b"y")
        with self.assertRaises(ValueError):
            await b.write_bytes(d, "../evil", b"y")
        cwd, entries = await b.list(d)
        self.assertEqual([e.name for e in entries], ["a.txt"])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, "a.txt")).st_mode) & 0o600, 0o600)
        shutil.rmtree(d)


@_zt.needs_zenteru
class PanelTest(_zt.PanelTest if _zt.HAVE_ZENTERU else unittest.TestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from zenfinder.plugin import ZenFinder

        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="zf-panel-"))
        os.mkdir(os.path.join(self.dir, "docs"))
        with open(os.path.join(self.dir, "notes.txt"), "w") as f:
            f.write("first line\nsecond line\n")
        with open(os.path.join(self.dir, ".dot"), "w") as f:
            f.write("hidden")
        self.session()["cwd"] = self.dir
        self.data = tempfile.mkdtemp()
        self.envp = mock.patch.dict(os.environ, {"ZENFINDER_DATA_DIR": self.data})
        self.envp.start()
        self.plugin = ZenFinder(self.conn, Config())
        await self.plugin.async_register()

    async def asyncTearDown(self):
        self.envp.stop()
        for v in self.plugin.views.values():
            if v.poller:
                v.poller.cancel()
        await super().asyncTearDown()
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.data, ignore_errors=True)

    async def open(self):
        await self.invoke("zenfinder.toggle")
        await self.until(lambda: self.server.panels and any("notes.txt" in t for t in self.texts())
                         and "Select a file" in self.last()["text"])

    async def test_lists_cwd_with_preview_at_the_bottom(self):
        await self.open()
        texts = self.texts()
        self.assertIn(self.dir.replace(os.path.expanduser("~"), "~"), texts[0])
        self.assertTrue(any(t.startswith("▸ docs/") for t in texts))
        self.assertFalse(any(".dot" in t for t in texts))
        self.assertIn("Select a file", texts[-1])
        await self.click("notes.txt", "preview")
        await self.until(lambda: "second line" in self.last()["text"])
        self.assertIn("text", self.last()["text"])

    async def test_image_preview_shows_the_picture(self):
        png = os.path.join(self.dir, "pic.png")
        with open(png, "wb") as f:
            f.write(PNG_BYTES)
        await self.open()
        await self.click("pic.png", "preview")
        await self.until(lambda: self.last().get("blocks"))
        self.assertIn("PNG image", self.last()["text"])
        self.assertEqual(self.last()["blocks"], [{"type": "image", "alt": "pic.png", "max_height": 18, "path": png}])
        # zenteru can't decode it: the preview falls back to text
        self.server.reject_images = True
        await self.click("notes.txt", "preview")
        await self.until(lambda: "second line" in self.last()["text"])
        await self.click("pic.png", "preview")
        await self.until(lambda: "image not shown" in self.last()["text"])
        self.assertEqual(self.last()["blocks"], [])

    async def test_open_folder_and_insert_path(self):
        await self.open()
        await self.click("▸ docs/", "open")
        await self.until(lambda: "docs" in self.texts()[0] and "(empty)" in self.texts())
        self.assertIn("browsing", self.texts()[0])
        await self.server.panel_action(self.panel_id(), self.messages()[0][0], "up")
        await self.until(lambda: any("notes.txt" in t for t in self.texts()))
        await self.click("notes.txt", "insert")
        await self.until(lambda: self.session()["sent"])
        self.assertEqual(self.session()["sent"][-1]["text"], os.path.join(self.dir, "notes.txt") + " ")

    async def test_paste_text_prompts_for_a_name(self):
        await self.open()
        self.server.clipboard = "hello\nworld\n"
        await self.invoke("zenfinder.paste")
        await self.until(lambda: self.server.inputs)
        self.assertTrue(self.server.inputs[-1]["text"].startswith("paste-") and self.server.inputs[-1]["text"].endswith(".txt"))
        self.assertIn("Save clipboard text", self.last()["text"])
        await self.type("greeting")
        target = os.path.join(self.dir, "greeting.txt")
        await self.until(lambda: os.path.exists(target))
        with open(target) as f:
            self.assertEqual(f.read(), "hello\nworld\n")
        await self.until(lambda: any("greeting.txt" in t for t in self.texts()))
        # same name again: asks before overwriting
        self.server.clipboard = "changed"
        await self.invoke("zenfinder.paste")
        await self.until(lambda: len(self.server.inputs) >= 2)
        await self.type("greeting.txt")
        await self.until(lambda: "already exists" in self.last()["text"])
        await self.click_last("overwrite")
        await self.until(lambda: read(target) == "changed")

    async def test_paste_cancel_and_format(self):
        await self.open()
        await self.type("format md")
        await self.until(lambda: self.plugin.config.paste_ext == "md")
        self.assertEqual(Config.load().paste_ext, "md")
        self.server.clipboard = "x"
        await self.invoke("zenfinder.paste")
        await self.until(lambda: self.server.inputs)
        self.assertTrue(self.server.inputs[-1]["text"].endswith(".md"))
        await self.type("")
        await self.until(lambda: "Cancelled" in self.last()["text"])
        self.assertEqual(sorted(os.listdir(self.dir)), [".dot", "docs", "notes.txt"])

    async def test_drop_copies_into_the_shown_folder(self):
        await self.open()
        src = tempfile.mkdtemp()
        f = os.path.join(src, "dropped.bin")
        with open(f, "wb") as fh:
            fh.write(b"data")
        r = await self.invoke("zenfinder.drop", source="drop", items=[{"kind": "file", "path": f}])
        self.assertEqual(r, {"text": ""})
        await self.until(lambda: "Copy 1 item(s)" in self.last()["text"])
        await self.click_last("go")
        await self.until(lambda: os.path.exists(os.path.join(self.dir, "dropped.bin")))
        await self.until(lambda: "Copied 1 item(s)" in self.last()["text"])
        shutil.rmtree(src)

    async def test_pasted_image_asks_for_a_name(self):
        import base64

        await self.open()
        r = await self.invoke("zenfinder.drop", source="paste",
                              items=[{"kind": "data", "uti": "public.png", "base64": base64.b64encode(b"\x89PNG....").decode()}])
        self.assertEqual(r, {"text": ""})
        await self.until(lambda: self.server.inputs)
        self.assertTrue(self.server.inputs[-1]["text"].endswith(".png"))
        await self.type("shot.png")
        await self.until(lambda: os.path.exists(os.path.join(self.dir, "shot.png")))

    async def test_drop_declined_when_panel_is_closed(self):
        r = await self.invoke("zenfinder.drop", source="drop", items=[{"kind": "file", "path": "/etc/hosts"}])
        self.assertIsNone(r)

    async def test_ssh_session_browses_the_remote_host(self):
        bindir = fake_ssh_dir()
        remote = os.path.realpath(tempfile.mkdtemp(prefix="zf-r-"))
        with open(os.path.join(remote, "server.log"), "w") as f:
            f.write("remote!")
        with mock.patch.dict(os.environ, {"PATH": bindir + os.pathsep + os.environ["PATH"]}):
            self.server.job_names[self.session_id] = "ssh"
            self.session()["lines"] = [f"me@box:{remote}$ "]
            self.session()["cursor"] = {"x": 3, "y": 0}
            with mock.patch("zenfinder.plugin.ZenFinder._ssh_of_tty",
                            staticmethod(lambda tty: sshargs.SshTarget("me@box", []))):
                await self.invoke("zenfinder.toggle")
                await self.until(lambda: self.server.panels and any("server.log" in t for t in self.texts()))
                self.assertIn(f"me@box:{remote}", self.texts()[0])
                mid = self.find_row("server.log")
                actions = [a["id"] for a in self.server.panels[self.panel_id()]["messages"][mid]["actions"]]
                self.assertIn("get", actions)
                await self.click("server.log", "preview")
                await self.until(lambda: "remote!" in self.last()["text"])
                with open(os.path.join(remote, "big.png"), "wb") as f:
                    f.write(PNG_BYTES + os.urandom(100_000))  # more than the 64 KiB first read
                await self.type("cd " + remote)
                await self.until(lambda: any("big.png" in t for t in self.texts()) and self.messages()
                                 and self.last()["text"].startswith("server.log"))
                await self.click("big.png", "preview")
                await self.until(lambda: self.last().get("blocks"))
                import base64

                with open(os.path.join(remote, "big.png"), "rb") as f:
                    self.assertEqual(base64.b64decode(self.last()["blocks"][0]["base64"]), f.read())
        shutil.rmtree(bindir)
        shutil.rmtree(remote)


if __name__ == "__main__":
    unittest.main()
