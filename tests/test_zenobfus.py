import io
import os
import shutil
import tarfile
import tempfile
import unittest
import zipfile
from unittest import mock

import _zt  # noqa: F401
from zenobfus import container, contents, obfus, rs

FIXTURES = os.path.join(_zt.ROOT, "tests", "fixtures", "obfus-react")


def fixture(name: str) -> bytes:
    with open(os.path.join(FIXTURES, name), "rb") as f:
        return f.read()


def record_spans(blob: bytes, plain_len: int, chunk: int):
    """(start, length) of each chunk record (header+blob) in an ECC container."""
    off = container.HEADER_LEN + container.HEADER_PARITY_LEN
    for i in range(max(1, -(-plain_len // chunk))):
        n = container.RECORD_HEADER_LEN + min(chunk, plain_len - i * chunk) + obfus.MAGIC_LEN
        yield off, n
        off += n + rs.parity_len(n)


class ReedSolomonTest(unittest.TestCase):
    def test_corrects_up_to_half_the_parity(self):
        import random

        r = random.Random(11)
        for trial in range(60):
            data = bytes(r.randrange(256) for _ in range(r.randrange(1, 244)))
            block = bytearray(data + rs.encode(data, 12))
            n = r.randrange(0, 7)
            for p in r.sample(range(len(block)), n):
                block[p] ^= r.randrange(1, 256)
            self.assertEqual(rs.decode_block(block, 12), n)
            self.assertEqual(bytes(block[:len(data)]), data)

    def test_detects_too_many_errors(self):
        import random

        r = random.Random(5)
        caught = 0
        for _ in range(40):
            data = bytes(r.randrange(256) for _ in range(243))
            block = bytearray(data + rs.encode(data, 12))
            for p in r.sample(range(255), 9):
                block[p] ^= r.randrange(1, 256)
            try:
                rs.decode_block(block, 12)
                caught += bytes(block[:243]) != data  # miscorrection: the CRC catches it
            except rs.RsUncorrectable:
                caught += 1
        self.assertEqual(caught, 40)

    def test_encode_matches_reference_construction(self):
        # parity = remainder of data(x)·x^nsym divided by the generator
        data = b"obfus-react"
        g = rs.generator(12)
        msg = list(data) + [0] * 12
        for i in range(len(data)):
            c = msg[i]
            if c:
                for j in range(1, len(g)):
                    msg[i + j] ^= rs.mul(g[j], c)
        self.assertEqual(rs.encode(data, 12), bytes(msg[len(data):]))


class ContainerTest(unittest.TestCase):
    """obfus-react's chunked container, against files obfus-react wrote."""

    def setUp(self):
        self.plain = fixture("plain.txt")

    def test_reads_obfus_react_files(self):
        for name, chunks, ecc, is_container in (("ecc-chunked", 9, True, True), ("chunked", 9, False, True),
                                                ("ecc-single", 1, True, True), ("bare", 1, False, False)):
            blob = fixture(f"{name}.txt.obfus")
            u = container.unpack(blob)
            self.assertEqual(u.data, self.plain, name)
            self.assertEqual((u.container, u.ecc, u.chunk_count, u.repaired, u.damaged),
                             (is_container, ecc, chunks, 0, []), name)
            self.assertEqual(container.packed_size(len(self.plain), 1024 if "chunked" in name else 1 << 20, ecc), len(blob))

    def test_badges(self):
        tag = lambda n: container.describe_probe(fixture(n)[:container.PROBE_LEN])  # noqa: E731
        self.assertEqual(tag("ecc-chunked.txt.obfus"), "obfus ecc")
        self.assertEqual(tag("chunked.txt.obfus"), "obfus chunked")
        self.assertEqual(tag("bare.txt.obfus"), "obfus")
        self.assertIsNone(tag("plain.txt"))

    def test_repairs_damage_at_the_full_radius_and_a_destroyed_magic(self):
        import random

        r = random.Random(3)
        d = bytearray(fixture("ecc-chunked.txt.obfus"))
        for i in range(6):
            d[i] ^= 0xFF  # "OBFSCT" gone: the most the header's 12 parity bytes can fix
        hits = 6
        for start, n in record_spans(d, len(self.plain), 1024):
            for b in range(0, n, 243):
                e = min(b + 243, n)
                for p in r.sample(range(b, e), min(6, e - b)):
                    d[start + p] ^= r.randrange(1, 256)
                    hits += 1
        self.assertEqual(container.detect(bytes(d[:container.PROBE_LEN])), "container")
        u = container.unpack(bytes(d))
        self.assertEqual(u.data, self.plain)
        self.assertEqual((u.repaired, u.header_repaired, u.damaged), (hits, True, []))

    def test_damaged_header_fields_are_repaired_too(self):
        d = bytearray(fixture("ecc-chunked.txt.obfus"))
        d[14] ^= 0x40  # chunk size
        u = container.unpack(bytes(d))
        self.assertEqual(u.data, self.plain)
        self.assertTrue(u.header_repaired)

    def test_unrepairable_chunk_is_zero_filled_and_reported(self):
        d = bytearray(fixture("ecc-chunked.txt.obfus"))
        start, _ = list(record_spans(d, len(self.plain), 1024))[2]
        for p in range(start + 20, start + 40):
            d[p] ^= 0x55
        u = container.unpack(bytes(d))
        self.assertEqual(u.damaged, [2])
        self.assertEqual(len(u.data), len(self.plain))
        self.assertEqual(u.data[2048:3072], bytes(1024))
        self.assertEqual(u.data[:2048] + u.data[3072:], self.plain[:2048] + self.plain[3072:])

    def test_crc_catches_damage_without_ecc(self):
        d = bytearray(fixture("chunked.txt.obfus"))
        d[container.HEADER_LEN + 100] ^= 1
        self.assertEqual(container.unpack(bytes(d)).damaged, [0])

    def test_round_trip_and_bare_when_small_without_ecc(self):
        for size in (0, 1, 243, 1000, 5000):
            data = os.urandom(size)
            for chunk, ecc in ((700, True), (700, False), (1 << 20, True)):
                blob = container.pack(data, chunk, ecc)
                self.assertEqual(len(blob), container.packed_size(size, chunk, ecc))
                self.assertEqual(container.unpack(blob).data, data)
        self.assertFalse(container.is_container(container.pack(b"x", 1 << 20, False)))
        self.assertEqual(obfus.unpack(container.pack(b"x", 1 << 20, False)), b"x")

    def test_rejects_inconsistent_headers(self):
        blob = bytearray(container.pack(b"x" * 3000, 1024, False))
        blob[18] = 99  # chunk count
        with self.assertRaises(container.ContainerError):
            container.unpack(bytes(blob))
        with self.assertRaises(container.ContainerError):
            container.unpack(container.pack(b"x" * 3000, 1024, True), max_bytes=100)
        with self.assertRaises(ValueError):
            container.unpack(b"neither a blob nor a container")


@unittest.skipUnless(os.environ.get("OBFUS_REACT_SRC") and shutil.which("node"), "set OBFUS_REACT_SRC to an obfus-react checkout")
class ObfusReactInteropTest(unittest.TestCase):
    """obfus-react's engine reads what zenobfus writes, and agrees on damage."""

    def bridge(self, *args):
        import json
        import subprocess

        out = subprocess.run([os.path.join(_zt.ROOT, "scripts", "obfus-react", "run.sh"), os.environ["OBFUS_REACT_SRC"], *args],
                             check=True, capture_output=True, text=True).stdout
        return json.loads(out) if out.strip() else None

    def test_both_directions(self):
        d = tempfile.mkdtemp()
        plain = fixture("plain.txt")
        for chunk, ecc in ((700, True), (700, False), (1 << 20, False)):
            src = os.path.join(d, "z.obfus")
            with open(src, "wb") as f:
                f.write(container.pack(plain, chunk, ecc))
            self.bridge("unpack", src, os.path.join(d, "out"))
            with open(os.path.join(d, "out"), "rb") as f:
                self.assertEqual(f.read(), plain)
        damaged = bytearray(fixture("ecc-chunked.txt.obfus"))
        start, _ = list(record_spans(damaged, len(plain), 1024))[2]
        for p in range(start + 20, start + 40):
            damaged[p] ^= 0x55
        damaged[30] ^= 0xFF
        with open(os.path.join(d, "dmg"), "wb") as f:
            f.write(damaged)
        res = self.bridge("unpack", os.path.join(d, "dmg"), os.path.join(d, "out"))
        mine = container.unpack(bytes(damaged))
        self.assertEqual(res["damagedChunks"], mine.damaged)
        with open(os.path.join(d, "out"), "rb") as f:
            self.assertEqual(f.read(), mine.data)
        shutil.rmtree(d)

FIXTURE_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x03\x00\x00\x00\x02\x08\x02\x00\x00\x00"
)


class FormatTest(unittest.TestCase):
    """Test vectors from file-obfuscation's README."""

    def test_vectors(self):
        self.assertEqual(obfus.keystream(0, 8).hex(), "8855508aade16ec5")
        self.assertEqual(obfus.keystream(7, 8).hex(), "e16ab60aa1eeb707")
        # README lists MAGIC as 935163626631, but obfus_core.py's b"\x93Qobf1"
        # is 93516f626631 (the blob vectors below confirm the code's value).
        self.assertEqual(obfus.MAGIC, b"\x93Qobf1")
        self.assertEqual(obfus.MAGIC.hex(), "93516f626631")
        self.assertEqual(obfus.pack(b"hello", seed=0).hex(), "1b043fe8cbd006a01fbe71")
        self.assertEqual(obfus.pack(b"hello", seed=5).hex(), "dab98c4b1374a93fdacbfb")
        for blob in ("1b043fe8cbd006a01fbe71", "dab98c4b1374a93fdacbfb"):
            self.assertEqual(obfus.unpack(bytes.fromhex(blob)), b"hello")

    def test_round_trips_every_seed(self):
        data = os.urandom(1000)
        for s in range(16):
            blob = obfus.pack(data, seed=s)
            self.assertEqual(len(blob), len(data) + 6)
            self.assertEqual(obfus.unpack_with_seed(blob), (data, s))
        self.assertEqual(obfus.unpack(obfus.pack(b"")), b"")

    def test_rejects_non_blobs(self):
        with self.assertRaisesRegex(ValueError, "no key matched"):
            obfus.unpack(b"plain text, not a blob")
        self.assertFalse(obfus.is_obfus(b"\x93Q"))

    def test_matches_reference_implementation(self):
        # The reference: XOR the whole blob under every seed, byte by byte.
        def ref_unpack(blob):
            for seed in range(16):
                ks = obfus.keystream(seed, len(blob))
                cand = bytes(a ^ b for a, b in zip(blob, ks))
                if cand.startswith(obfus.MAGIC):
                    return cand[6:]
            raise ValueError
        for n in (0, 1, 31, 32, 33, 4097):
            data = os.urandom(n)
            blob = obfus.pack(data)
            self.assertEqual(obfus.unpack(blob), ref_unpack(blob))

    def test_inner_name(self):
        self.assertEqual(obfus.inner_name("a.pdf.obfus"), "a.pdf")
        self.assertEqual(obfus.inner_name(".obfus"), ".obfus")
        self.assertEqual(obfus.inner_name("x.bin"), "x.bin")


def make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.md", "# hi\n")
        z.writestr("docs/guide.txt", "guide")
        z.writestr("deep/er/secret.txt.obfus", obfus.pack(b"inside"))
        z.writestr("../escape.txt", "nope")
    return buf.getvalue()


def make_tgz() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        data = b"tar member"
        ti = tarfile.TarInfo("pkg/file.txt")
        ti.size = len(data)
        t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


class ContentsTest(unittest.TestCase):
    def test_sniff(self):
        s = contents.sniff
        self.assertEqual(s("a.txt", b"hello\n").family, "text")
        self.assertEqual(s("a", FIXTURE_PNG).label, "PNG image")
        self.assertEqual(s("a.zip", make_zip()).family, "zip")
        self.assertEqual(s("a.tgz", make_tgz()).family, "tar")
        self.assertEqual(s("x", obfus.pack(b"q")).family, "obfus")
        self.assertEqual(s("k.pem", b"-----BEGIN PRIVATE KEY-----\nabc\n").label, "PEM text")
        self.assertEqual(s("r.bin", os.urandom(300) + b"\0").family, "binary")
        self.assertEqual(s("a.mp4", b"\0\0\0\x18ftypisom" + b"\0" * 20).family, "video")

    def test_image_size(self):
        self.assertEqual(contents.image_size(FIXTURE_PNG), (3, 2))
        self.assertEqual(contents.image_size(b"GIF89a\x05\x00\x07\x00"), (5, 7))

    def test_zip_tree_drops_escaping_names(self):
        t = contents.open_tree("a.zip", contents.Kind("zip", ""), make_zip())
        root = [e.name for e in t.entries("")]
        self.assertEqual(root, ["deep", "docs", "escape.txt", "readme.md"])  # ../ normalized into the root
        self.assertEqual([e.name for e in t.entries("deep/er")], ["secret.txt.obfus"])
        member = t.entries("deep/er")[0].data
        self.assertEqual(obfus.unpack(t.read(member)), b"inside")

    def test_tar_tree(self):
        t = contents.open_tree("a.tgz", contents.Kind("tar", ""), make_tgz())
        self.assertEqual([e.name for e in t.entries("pkg")], ["file.txt"])
        self.assertEqual(t.read(t.entries("pkg")[0].data), b"tar member")

    def test_describe(self):
        self.assertIn("first", contents.describe("a.txt", b"first\nsecond", contents.sniff("a.txt", b"first")))
        self.assertIn("3×2", contents.describe("a.png", FIXTURE_PNG, contents.sniff("a.png", FIXTURE_PNG)))
        z = make_zip()
        self.assertIn("readme.md", contents.describe("a.zip", z, contents.sniff("a.zip", z)))


@_zt.needs_zenteru
class PanelTest(_zt.PanelTest if _zt.HAVE_ZENTERU else unittest.TestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from zenobfus.plugin import ZenObfus

        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="zo-"))
        with open(os.path.join(self.dir, "note.txt.obfus"), "wb") as f:
            f.write(obfus.pack("こんにちは obfus\nline two\n".encode()))
        with open(os.path.join(self.dir, "bundle.zip.obfus"), "wb") as f:
            f.write(obfus.pack(make_zip()))
        with open(os.path.join(self.dir, "renamed.dat"), "wb") as f:
            f.write(obfus.pack(b"no suffix"))
        with open(os.path.join(self.dir, "fake.obfus"), "wb") as f:
            f.write(b"not really")
        with open(os.path.join(self.dir, "plain.txt"), "w") as f:
            f.write("plain")
        self.session()["cwd"] = self.dir
        self.data = tempfile.mkdtemp()
        self.envp = mock.patch.dict(os.environ, {"ZENOBFUS_DATA_DIR": self.data})
        self.envp.start()
        self.plugin = ZenObfus(self.conn)
        await self.plugin.async_register()
        await self.invoke("zenobfus.toggle")
        await self.until(lambda: self.server.panels and any("note.txt.obfus" in t for t in self.texts())
                         and "Select a file" in self.last()["text"])

    async def asyncTearDown(self):
        self.envp.stop()
        for v in self.plugin.views.values():
            if v.poller:
                v.poller.cancel()
        self.plugin.cleanup()
        await super().asyncTearDown()
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.data, ignore_errors=True)

    async def test_badges_and_notice(self):
        texts = self.texts()
        self.assertIn("not encryption", texts[0])
        row = lambda n: next(t for t in texts if n in t)  # noqa: E731
        self.assertIn("[obfus]", row("note.txt.obfus"))
        self.assertIn("[obfus]", row("renamed.dat"))
        self.assertIn("[not obfus]", row("fake.obfus"))
        self.assertNotIn("[", row("plain.txt"))
        self.assertFalse(any("encrypted" in t.replace("not encryption", "") for t in texts))

    async def test_preview_unpacks_in_memory(self):
        await self.click("note.txt.obfus", "preview")
        await self.until(lambda: "こんにちは" in self.last()["text"])
        self.assertIn("note.txt.obfus  →  note.txt", self.last()["text"])
        self.assertEqual(sorted(os.listdir(self.dir)), ["bundle.zip.obfus", "fake.obfus", "note.txt.obfus", "plain.txt", "renamed.dat"])

    async def test_browse_into_an_obfuscated_archive(self):
        await self.click("bundle.zip.obfus", "open")
        await self.until(lambda: any("readme.md" in t for t in self.texts()))
        self.assertIn("bundle.zip.obfus", self.texts()[0])
        await self.click("▸ deep/", "open")
        await self.until(lambda: any("▸ er/" in t for t in self.texts()))
        await self.click("▸ er/", "open")
        await self.until(lambda: any("secret.txt.obfus" in t for t in self.texts()))
        await self.click("secret.txt.obfus", "preview")
        await self.until(lambda: "inside" in self.last()["text"])
        # extract it next to the archive, after the name prompt
        await self.click("secret.txt.obfus", "save")
        await self.until(lambda: self.server.inputs)
        self.assertEqual(self.server.inputs[-1]["text"], "secret.txt")
        await self.type("secret.txt")
        target = os.path.join(self.dir, "secret.txt")
        await self.until(lambda: os.path.exists(target))
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"inside")
        # Up three times: er → deep → archive root → the folder
        for _ in range(3):
            await self.server.panel_action(self.panel_id(), self.messages()[0][0], "up")
            await self.settle()
        await self.until(lambda: any("plain.txt" in t for t in self.texts()))

    async def test_obfuscate_a_copy(self):
        await self.click("plain.txt", "pack")
        await self.until(lambda: "With ECC" in str(self.last().get("actions")))
        await self.click_last("bare")
        await self.until(lambda: self.server.inputs)
        self.assertEqual(self.server.inputs[-1]["text"], "plain.txt.obfus")
        await self.type("plain.txt.obfus")
        target = os.path.join(self.dir, "plain.txt.obfus")
        await self.until(lambda: os.path.exists(target))
        with open(target, "rb") as f:
            self.assertEqual(obfus.unpack(f.read()), b"plain")
        self.assertTrue(os.path.exists(os.path.join(self.dir, "plain.txt")))

    async def test_obfuscate_with_ecc(self):
        await self.click("plain.txt", "pack")
        await self.until(lambda: "With ECC" in str(self.last().get("actions")))
        await self.click_last("ecc")
        await self.until(lambda: self.server.inputs)
        await self.type("plain.ecc.obfus")
        target = os.path.join(self.dir, "plain.ecc.obfus")
        await self.until(lambda: os.path.exists(target))
        with open(target, "rb") as f:
            blob = f.read()
        self.assertTrue(container.is_container(blob))
        self.assertEqual(container.unpack(blob).data, b"plain")
        await self.until(lambda: any("plain.ecc.obfus" in t and "[obfus ecc]" in t for t in self.texts()))

    async def test_damaged_container_preview_and_repaired_copy(self):
        d = bytearray(fixture("ecc-chunked.txt.obfus"))
        d[0] ^= 0xFF
        start, _ = list(record_spans(d, len(fixture("plain.txt")), 1024))[4]
        d[start + 50] ^= 0x11
        with open(os.path.join(self.dir, "hurt.txt.obfus"), "wb") as f:
            f.write(d)
        await self.server.panel_action(self.panel_id(), self.messages()[0][0], "refresh")
        await self.until(lambda: any("hurt.txt.obfus" in t and "[obfus ecc]" in t for t in self.texts()))
        await self.click("hurt.txt.obfus", "preview")
        await self.until(lambda: "repaired 2 byte(s) incl. the header" in self.last()["text"])
        self.assertIn("ECC container, RS(255,243), 9 chunk(s)", self.last()["text"])
        self.assertIn("repair", [a["id"] for a in self.last()["actions"]])
        await self.click_last("repair")
        await self.until(lambda: self.server.inputs)
        self.assertEqual(self.server.inputs[-1]["text"], "hurt.txt.repaired.obfus")
        await self.type("hurt.txt.repaired.obfus")
        target = os.path.join(self.dir, "hurt.txt.repaired.obfus")
        await self.until(lambda: os.path.exists(target))
        with open(target, "rb") as f:
            u = container.unpack(f.read())
        self.assertEqual((u.data, u.repaired), (fixture("plain.txt"), 0))

    async def test_unrepairable_chunks_are_reported(self):
        d = bytearray(fixture("ecc-chunked.txt.obfus"))
        start, _ = list(record_spans(d, len(fixture("plain.txt")), 1024))[1]
        for p in range(start + 10, start + 30):
            d[p] ^= 0x55
        with open(os.path.join(self.dir, "dead.txt.obfus"), "wb") as f:
            f.write(d)
        await self.server.panel_action(self.panel_id(), self.messages()[0][0], "refresh")
        await self.until(lambda: any("dead.txt.obfus" in t for t in self.texts()))
        await self.click("dead.txt.obfus", "save")
        await self.until(lambda: self.server.inputs)
        self.assertIn("1 chunk(s) couldn't be repaired", self.last()["text"])

    async def test_unpack_refuses_silent_overwrite(self):
        with open(os.path.join(self.dir, "note.txt"), "w") as f:
            f.write("keep me")
        await self.click("note.txt.obfus", "save")
        await self.until(lambda: self.server.inputs)
        await self.type("note.txt")
        await self.until(lambda: "already exists" in self.last()["text"])
        await self.click_last("cancel")
        await self.until(lambda: "Cancelled" in self.last()["text"])
        with open(os.path.join(self.dir, "note.txt")) as f:
            self.assertEqual(f.read(), "keep me")

    async def test_double_obfuscation_is_guarded(self):
        e = self.plugin.views[self.window_id].bp.find("renamed.dat")
        self.assertEqual(e.badge, "obfus")
        # an [obfus] file offers Unpack…, not Obfuscate…; force the action:
        await self.server.panel_action(self.panel_id(), self.find_row("renamed.dat"), "pack")
        await self.until(lambda: "already an obfus blob" in self.last()["text"])
        await self.click_last("cancel")
        await self.until(lambda: "Cancelled" in self.last()["text"])


if __name__ == "__main__":
    unittest.main()
