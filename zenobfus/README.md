# zenobfus

A browser for [`.obfus`](https://github.com/themrsung/file-obfuscation)
files, laid out like zenfinder. It isn't a file system: it lists a folder,
unpacks obfus blobs **in memory**, and previews what's inside. Open it
with `@zenobfus` (or `@zenobfus path/to/file.obfus`) or from the ⌘⇧P
palette.

```
┌ ~/samples ▸ bundle.zip.obfus ▸ docs ────────────── ↑ Up  ⟳  ~ Home ┐
│ obfus is obfuscation, not encryption: anyone can unpack these files. │
│ · guide.txt.obfus  [obfus]   812 B          [Preview] [Open] [Extract…]│
├──────────────────────────────────────────────────────────────────────┤
│ guide.txt.obfus  →  guide.txt                                        │
│ unpacked in memory: 812 B → 806 B                                     │
│ MD text, 806 B                                                       │
│ # Getting started …                                                  │
└──────────────────────────────────────────────────────────────────────┘
```

- **Top: the browser.** It shows the session's working directory and
  follows the shell like zenfinder does. Any file that starts with an obfus
  header is tagged `[obfus]`, whatever its name. A `.obfus` file without a
  valid header is tagged `[not obfus]`.
- **Bottom: the preview.** It shows the unpacked contents: text,
  image format and size, audio, video or PDF type, a hex dump, or an
  archive's listing. **Quick Look** shows pictures, media and PDFs from a
  temporary copy in zenteru's per-launch private folder, which zenteru
  deletes when it quits.
- **Archives as folders:** zip, tar, tar.gz, tar.bz2 and tar.xz,
  obfuscated or not, open as folders you can browse into. Nested obfus
  layers (`a.zip.obfus.obfus`) are peeled on Open.
- **obfus-react containers:** files written by
  [obfus-react](https://github.com/themrsung/obfus-react) with chunking or
  ECC are read too. Each chunk's CRC-32 is checked, and damage is repaired
  from the Reed–Solomon RS(255,243) parity (up to 6 bad bytes in every 255,
  including a damaged header). The preview says what was found, e.g.
  `ECC container, RS(255,243), 9 chunk(s) · repaired 2 byte(s) incl. the
  header`. A chunk past repair is zero-filled, as obfus-react does, and
  listed. **Save repaired copy…** writes a fresh container. Listing tags:
  `[obfus]` (plain blob), `[obfus ecc]`, `[obfus chunked]`.
- **Writing happens only on request.** **Unpack…** or **Extract…** saves
  the contents next to the original, and **Obfuscate…** saves a `.obfus`
  copy, either as a plain blob (obfus.py can read it) or with ECC
  (obfus-react's container, about 5% larger). Both ask for a file name
  first. They never overwrite the original or any existing file without
  asking, and they warn before obfuscating a blob a second time.

Files up to 512 MiB are unpacked; the port runs at about 100 MB/s.

## About the format

obfus is **obfuscation, not encryption**. The key is 4 bits and isn't
stored; unpacking means trying all 16. The panel says so, and never
describes files as encrypted, protected or private.

[`zenobfus/obfus.py`](zenobfus/obfus.py) is a port of the CC0
`obfus_core.py`, byte-compatible with it. It is checked against the README
test vectors and against the reference implementation in both directions.
It finds the key from the 6-byte header alone and XORs on big integers,
which makes it much faster than the reference with identical output.

[`zenobfus/container.py`](zenobfus/container.py) and
[`zenobfus/rs.py`](zenobfus/rs.py) port obfus-react's container and
Reed–Solomon codec (`src/engine/container.ts`, `ecc.ts`, `rs.ts`, same field,
generator and conventions). They are tested against files obfus-react wrote
([tests/fixtures/obfus-react](../tests/fixtures/obfus-react)), and in CI
obfus-react's engine also reads what zenobfus writes and damages. Two
differences from obfus-react, with the same output: a chunk whose CRC
matches skips the RS decoder, and with ECC on the header is checked
against its parity even when its magic is intact. That second check also
repairs damaged header fields, which obfus-react only does when the magic
is gone.

The file-obfuscation README listed MAGIC as `935163626631`, but the code's
`b"\x93Qobf1"` is `93516f626631`. The blob test vectors matched the
code; the README has since been fixed.
