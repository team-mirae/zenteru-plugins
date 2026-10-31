import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import _zt  # noqa: F401
from zentail import tailscale as ts

STATUS = {
    "Version": "1.102.4-t3caf7d9e7-g084ee3b64",
    "BackendState": "Running",
    "AuthURL": "",
    "TailscaleIPs": ["100.64.0.2", "fd7a:115c:a1e0::2"],
    "Self": {"ID": "n1", "HostName": "mini", "DNSName": "mini.tail.example.", "OS": "macOS", "UserID": 1,
             "TailscaleIPs": ["100.64.0.2", "fd7a:115c:a1e0::2"], "Online": True},
    "Health": [],
    "MagicDNSSuffix": "tail.example",
    "CurrentTailnet": {"Name": "example.com", "MagicDNSSuffix": "tail.example", "MagicDNSEnabled": True},
    "Peer": {
        "k1": {"ID": "p1", "HostName": "web", "DNSName": "web.tail.example.", "OS": "linux", "Online": True,
               "TailscaleIPs": ["100.64.0.5", "fd7a::5"], "CurAddr": "1.2.3.4:41641", "Relay": "tok",
               "ExitNodeOption": True, "ExitNode": False, "RxBytes": 2048, "TxBytes": 10},
        "k2": {"ID": "p2", "HostName": "neo", "DNSName": "neo.tail.example.", "OS": "macOS", "Online": False,
               "TailscaleIPs": ["100.64.0.9"], "Relay": "tok"},
        "k3": {"ID": "p3", "HostName": "phone", "DNSName": "phone.tail.example.", "OS": "android", "Online": True,
               "TailscaleIPs": ["100.64.0.7"], "Relay": "tok"},
    },
    "User": {"1": {"ID": 1, "LoginName": "me@example.com"}},
}

FAKE_TS = r"""#!/bin/sh
# fake tailscale CLI for tests
echo "$@" >> "$FAKE_TS_LOG"
case "$1" in
  status) cat "$FAKE_TS_STATUS" ;;
  ping) echo "pong from $4 via 1.2.3.4:41641 in 6ms" ;;
  set) echo "" ;;
  switch) echo '[{"id":"a1","nickname":"me","tailnet":"example.com","account":"me@example.com","selected":true},{"id":"b2","nickname":"work","tailnet":"corp.com","account":"me@corp.com","selected":false}]' ;;
  netcheck) printf 'Report:\n\t* UDP: true\n' ;;
  *) echo "unknown $1"; exit 1 ;;
esac
"""


class ParseTest(unittest.TestCase):
    def test_status(self):
        st = ts.parse_status(json.dumps(STATUS))
        self.assertTrue(st.running)
        self.assertEqual(st.tailnet, "example.com")
        self.assertEqual(st.user, "me@example.com")
        self.assertEqual([p.host for p in st.peers], ["web", "phone", "neo"])  # online, exit-capable first
        web = st.peers[0]
        self.assertEqual((web.ipv4, web.connection, web.short_dns), ("100.64.0.5", "direct", "web.tail.example"))
        self.assertEqual(st.peers[1].connection, "relay tok")
        self.assertEqual(st.peers[2].connection, "offline")
        self.assertIsNone(st.exit_node)

    def test_signature_ignores_traffic_counters(self):
        a = ts.parse_status(json.dumps(STATUS))
        s2 = json.loads(json.dumps(STATUS))
        s2["Peer"]["k1"]["RxBytes"] = 99999
        self.assertEqual(a.signature(), ts.parse_status(json.dumps(s2)).signature())
        s2["Peer"]["k2"]["Online"] = True
        self.assertNotEqual(a.signature(), ts.parse_status(json.dumps(s2)).signature())

    def test_accounts(self):
        self.assertEqual(len(ts.parse_accounts('[{"id":"x"},{"no":1}]')), 1)
        self.assertEqual(ts.parse_accounts("garbage"), [])


class SshTargetTest(unittest.TestCase):
    """The ssh destination follows the name the user's ssh config knows."""

    def configs(self, known):
        base = {"user": "me", "port": "22"}
        return lambda name: dict(base, user="ec2-user") if name in known else dict(base)

    def peer(self):
        return ts.Peer("p", "web", "web.tail.example.", ["100.64.0.5"], "linux", True)

    def test_short_name_stanza(self):
        with mock.patch.object(ts, "_ssh_G", self.configs({"web"})):
            self.assertEqual(ts.ssh_destination(self.peer()), "web")

    def test_fqdn_stanza(self):
        with mock.patch.object(ts, "_ssh_G", self.configs({"web.tail.example"})):
            self.assertEqual(ts.ssh_destination(self.peer()), "web.tail.example")

    def test_no_stanza_uses_magicdns(self):
        with mock.patch.object(ts, "_ssh_G", self.configs(set())):
            self.assertEqual(ts.ssh_destination(self.peer()), "web.tail.example")

    def test_command_keeps_failed_tabs_open(self):
        import subprocess

        out = subprocess.run(["/bin/sh", "-c", ts.ssh_command("x").replace("ssh x", "false", 1)],
                             input="\n", capture_output=True, text=True).stdout
        self.assertIn("ssh exited with status 1", out)
        self.assertEqual(ts.ssh_command("h", "u").split(";")[0], "ssh -l u h")


class LauncherTest(unittest.TestCase):
    def test_install_and_refuse_foreign_file(self):
        d = tempfile.mkdtemp()
        b = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
        path = ts.install_launcher(b, d)
        with open(path) as f:
            text = f.read()
        self.assertIn(f'exec "{b}" "$@"', text)
        self.assertTrue(os.access(path, os.X_OK))
        self.assertEqual(ts.launcher_status(path, b), "ours")
        self.assertEqual(ts.launcher_status(path, "/other"), "ours-stale")
        os.unlink(path)
        with open(path, "w") as f:
            f.write("#!/bin/sh\necho mine\n")
        with self.assertRaises(FileExistsError):
            ts.install_launcher(b, d)
        os.unlink(path)
        os.symlink(b, path)
        self.assertEqual(ts.launcher_status(path, b), "other")
        shutil.rmtree(d)

    def test_raw_symlink_into_the_app_is_not_used_as_is(self):
        d = tempfile.mkdtemp()
        app = os.path.join(d, "Tailscale.app", "Contents", "MacOS")
        os.makedirs(app)
        binary = os.path.join(app, "Tailscale")
        with open(binary, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(binary, 0o755)
        bindir = os.path.join(d, "bin")
        os.mkdir(bindir)
        os.symlink(binary, os.path.join(bindir, "tailscale"))
        with mock.patch.dict(os.environ, {"PATH": bindir}, clear=False), \
             mock.patch.object(ts, "SEARCH_PATH", ()):
            os.environ.pop("ZENTAIL_TAILSCALE", None)
            cli = ts.find_cli()
        self.assertEqual(cli.argv0, os.path.realpath(binary))
        self.assertEqual(cli.via, "app")
        self.assertEqual(cli.env, {"TAILSCALE_BE_CLI": "1"})
        shutil.rmtree(d)


@_zt.needs_zenteru
class PanelTest(_zt.PanelTest if _zt.HAVE_ZENTERU else unittest.TestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from zentail.plugin import ZenTail

        self.tmp = tempfile.mkdtemp()
        cli = os.path.join(self.tmp, "tailscale")
        with open(cli, "w") as f:
            f.write(FAKE_TS)
        os.chmod(cli, 0o755)
        self.status_file = os.path.join(self.tmp, "status.json")
        with open(self.status_file, "w") as f:
            json.dump(STATUS, f)
        self.log = os.path.join(self.tmp, "log")
        self.envp = mock.patch.dict(os.environ, {"ZENTAIL_TAILSCALE": cli, "FAKE_TS_STATUS": self.status_file,
                                                  "FAKE_TS_LOG": self.log,
                                                  "ZENTAIL_DATA_DIR": os.path.join(self.tmp, "data")})
        self.envp.start()
        self.plugin = ZenTail(self.conn)
        await self.plugin.async_register()

    async def asyncTearDown(self):
        self.envp.stop()
        for v in self.plugin.views.values():
            if v.poller:
                v.poller.cancel()
        await super().asyncTearDown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def calls(self):
        with open(self.log) as f:
            return f.read().splitlines()

    async def open(self):
        await self.invoke("zentail.toggle")
        await self.until(lambda: self.server.panels and any("○ neo" in t for t in self.texts())
                         and "peers online" in self.status())

    async def test_status_and_peers(self):
        await self.open()
        texts = self.texts()
        self.assertIn("Tailscale · Running", texts[0])
        self.assertIn("mini · 100.64.0.2", texts[0])
        self.assertIn("exit node: none", texts[0])
        self.assertTrue(any(t.startswith("● web  100.64.0.5  linux  direct") and "[exit node]" in t for t in texts))
        self.assertTrue(any(t.startswith("○ neo") and "offline" in t for t in texts))
        await self.until(lambda: "2/3 peers online" in self.status())

    async def test_ping_copy_ssh(self):
        await self.open()
        await self.click("● web", "ping")
        await self.until(lambda: "pong from 100.64.0.5" in self.last()["text"])
        await self.click("● web", "copy")
        await self.until(lambda: self.server.clipboard == "100.64.0.5")
        await self.click("● web", "ssh")
        await self.until(lambda: any(m == "tab.create" for m, _ in self.server.calls))
        cmd = self.server.last_call("tab.create")["command"]
        self.assertTrue(cmd.startswith("ssh web.tail.example; s=$?;"), cmd)
        self.assertIn("read _", cmd)  # a failed login keeps the tab open

    async def test_ssh_as_remembers_the_user(self):
        await self.open()
        await self.click("● web", "sshas")
        await self.until(lambda: "as which user" in self.last()["text"])
        await self.type("ec2-user")
        await self.until(lambda: any(m == "tab.create" for m, _ in self.server.calls))
        self.assertTrue(self.server.last_call("tab.create")["command"].startswith("ssh -l ec2-user web.tail.example;"))
        with open(os.path.join(self.tmp, "data", "ssh-users.json")) as f:
            self.assertEqual(json.load(f), {"web": "ec2-user"})
        n = len(self.server.calls)
        await self.click("● web", "ssh")
        await self.until(lambda: any(m == "tab.create" for m, _ in self.server.calls[n:]))
        self.assertTrue(self.server.last_call("tab.create")["command"].startswith("ssh -l ec2-user "))

    async def test_exit_node_asks_first(self):
        await self.open()
        await self.click("● web", "exit")
        await self.until(lambda: "internet traffic through web" in self.last()["text"])
        await self.click_last("lan")
        await self.until(lambda: any(c.startswith("set ") for c in self.calls()))
        self.assertIn("set --exit-node=100.64.0.5 --exit-node-allow-lan-access", self.calls())

    async def test_disconnect_needs_confirmation(self):
        await self.open()
        await self.server.panel_action(self.panel_id(), self.messages()[0][0], "down")
        await self.until(lambda: "Disconnect this Mac" in self.last()["text"])
        await self.click_last("no")
        await _zt.asyncio.sleep(0.1)
        self.assertNotIn("down", self.calls())

    async def test_accounts_and_typed_commands(self):
        await self.open()
        await self.server.panel_action(self.panel_id(), self.messages()[0][0], "accounts")
        await self.until(lambda: "me@corp.com" in self.last()["text"]
                         and "switch:b2" in [a["id"] for a in self.last().get("actions") or []])
        await self.type("netcheck")
        await self.until(lambda: "UDP: true" in self.last()["text"])
        await self.type("login")
        await self.until(lambda: any(m == "tab.create" for m, _ in self.server.calls))
        self.assertTrue(self.server.last_call("tab.create")["command"].endswith("tailscale login"))

    async def test_not_installed(self):
        with mock.patch("zentail.tailscale.find_cli", return_value=None):
            await self.invoke("zentail.toggle")
            await self.until(lambda: self.server.panels and "isn't installed" in self.texts()[0])
        self.assertIn("tailscale.com/download/mac", self.texts()[0])
        self.assertIn("Homebrew", self.texts()[0])
        acts = [a["id"] for a in self.messages()[0][1]["actions"]]
        self.assertEqual(acts, ["download", "appstore", "refresh"])


if __name__ == "__main__":
    unittest.main()
