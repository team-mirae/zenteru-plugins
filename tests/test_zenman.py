import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

import _zt

ROOT = _zt.ROOT


def load_zenman():
    path = os.path.join(ROOT, "zenman", "zenman")
    loader = importlib.machinery.SourceFileLoader("zenman_cli", path)
    spec = importlib.util.spec_from_loader("zenman_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


zm = load_zenman()


def build(out):
    subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "build_live.py"), out, "--commit", "abc123"],
                   check=True, capture_output=True)


class ZenmanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dist = tempfile.mkdtemp(prefix="zm-dist-")
        build(cls.dist)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dist)

    def setUp(self):
        self.plugins = tempfile.mkdtemp(prefix="zm-plugins-")
        self.env = mock.patch.dict(os.environ, {"ZENMAN_INDEX": os.path.join(self.dist, "index.json"),
                                                 "ZENTERU_PLUGINS_DIR": self.plugins})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.plugins)

    def run_zm(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = zm.main(list(argv))
        return code, out.getvalue() + err.getvalue()

    def test_install_list_update_remove(self):
        code, out = self.run_zm("install", "zenfinder")
        self.assertEqual(code, 0, out)
        d = os.path.join(self.plugins, "zenfinder")
        self.assertTrue(os.path.isfile(os.path.join(d, "plugin.json")))
        self.assertTrue(os.path.isfile(os.path.join(d, "zenfinder", "browserpanel.py")))
        self.assertFalse(os.path.exists(os.path.join(d, "tests")))
        receipt = json.load(open(os.path.join(d, ".zenman.json")))
        self.assertEqual(receipt["commit"], "abc123")
        code, out = self.run_zm("list")
        self.assertRegex(out, r"zenfinder\s+0\.1\.0\s+0\.1\.0")
        self.assertRegex(out, r"zentail\s+0\.1\.0\s+-")
        code, out = self.run_zm("update")
        self.assertIn("up to date", out)
        # simulate an older install
        receipt["sha256"] = "0" * 64
        json.dump(receipt, open(os.path.join(d, ".zenman.json"), "w"))
        code, out = self.run_zm("update")
        self.assertIn("updated zenfinder", out)
        code, out = self.run_zm("remove", "zenfinder")
        self.assertFalse(os.path.exists(d))
        self.assertEqual(os.listdir(self.plugins), [])

    def test_refuses_unmanaged_and_handles_links(self):
        os.makedirs(os.path.join(self.plugins, "zentail"))
        code, out = self.run_zm("install", "zentail")
        self.assertEqual(code, 1)
        self.assertIn("wasn't installed by zenman", out)
        code, out = self.run_zm("install", "--force", "zentail")
        self.assertEqual(code, 0, out)
        code, out = self.run_zm("link", os.path.join(ROOT, "zenobfus"))
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.islink(os.path.join(self.plugins, "zenobfus")))
        code, out = self.run_zm("install", "--all")
        self.assertEqual(code, 0, out)
        self.assertIn("zenobfus is linked", out)
        code, out = self.run_zm("remove", "zenobfus")
        self.assertTrue(os.path.isdir(os.path.join(ROOT, "zenobfus")))  # checkout untouched

    def test_checksum_mismatch(self):
        bad = tempfile.mkdtemp()
        shutil.copytree(self.dist, bad, dirs_exist_ok=True)
        with open(os.path.join(bad, "zentail.tar.gz"), "ab") as f:
            f.write(b"tampered")
        with mock.patch.dict(os.environ, {"ZENMAN_INDEX": os.path.join(bad, "index.json")}):
            code, out = self.run_zm("install", "zentail")
        self.assertEqual(code, 1)
        self.assertIn("checksum mismatch", out)
        self.assertEqual(os.listdir(self.plugins), [])
        shutil.rmtree(bad)

    def test_safe_extract_rejects_traversal_and_links(self):
        for name, kind in (("x/../../evil", tarfile.REGTYPE), ("other/file", tarfile.REGTYPE), ("x/link", tarfile.SYMTYPE)):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as t:
                ti = tarfile.TarInfo(name)
                ti.type = kind
                if kind == tarfile.SYMTYPE:
                    ti.linkname = "/etc/passwd"
                t.addfile(ti, io.BytesIO(b""))
            with self.assertRaises(zm.ZenmanError):
                zm.safe_extract(buf.getvalue(), tempfile.mkdtemp(), "x")

    def test_unknown_plugin(self):
        code, out = self.run_zm("install", "nope")
        self.assertEqual(code, 1)
        self.assertIn("available: zenfinder, zenobfus, zentail", out)


class BuildTest(unittest.TestCase):
    def test_deterministic_and_complete(self):
        a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
        build(a)
        build(b)
        ia = json.load(open(os.path.join(a, "index.json")))
        ib = json.load(open(os.path.join(b, "index.json")))
        self.assertEqual([p["sha256"] for p in ia["plugins"]], [p["sha256"] for p in ib["plugins"]])
        self.assertEqual([p["name"] for p in ia["plugins"]], ["zenfinder", "zenobfus", "zentail"])
        for p in ia["plugins"]:
            with open(os.path.join(a, p["asset"]), "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(), p["sha256"])
        with open(os.path.join(a, "zenman"), "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), ia["zenman"]["sha256"])
        self.assertTrue(os.path.isfile(os.path.join(a, "install.sh")))
        shutil.rmtree(a)
        shutil.rmtree(b)

    def test_shared_copies_are_in_sync(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import build_live

        self.assertEqual(build_live.check_shared(), [])


if __name__ == "__main__":
    unittest.main()
