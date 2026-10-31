Files written by obfus-react's own engine (themrsung/obfus-react @ 72955c9,
`src/engine/container.ts`), to check zenobfus against it:

| file | made with |
|---|---|
| `plain.txt` | the original |
| `ecc-chunked.txt.obfus` | chunk 1024, ECC on (container, 9 chunks) |
| `chunked.txt.obfus` | chunk 1024, ECC off (container, CRC only) |
| `ecc-single.txt.obfus` | chunk 1 MiB, ECC on (container, 1 chunk) |
| `bare.txt.obfus` | chunk 1 MiB, ECC off (bare upstream blob) |

Regenerate with `scripts/obfus-react/run.sh /path/to/obfus-react pack plain.txt OUT CHUNK ECC`.
With `OBFUS_REACT_SRC` set (and Node 24+), tests/test_zenobfus.py also has
obfus-react unpack what zenobfus writes and damages.
