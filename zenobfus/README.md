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
- **Writing happens only on request.** **Unpack…** or **Extract…** saves
  the contents next to the original, and **Obfuscate…** saves a `.obfus`
  copy. Both ask for a file name first. They never overwrite the original
  or any existing file without asking, and they warn before obfuscating a
  blob a second time.

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

The file-obfuscation README lists MAGIC as `935163626631`, but the code's
`b"\x93Qobf1"` is `93516f626631`. The blob test vectors match the code.
