Files made by the deployed obfus-react site (https://obfus.sjun.me, bundle
`index-CD6kdblc.js`, default config: ECC on, 5% overhead cap, auto chunk
size of 8 MiB), 2026-11-01:

| file | what the site did |
|---|---|
| `gradient.png.obfus` | packed a 240×240 PNG (173,118 B) as an ECC container, one chunk |
| `small.txt.obfus` | packed 1,000 B of text as a plain blob: too small for ECC under the 5% cap |

`gradient.png.obfus` was captured from the page's output blob and
checked byte for byte here (SHA-256 `07398821…a780`); `small.txt.obfus`
likewise (`39595052…349b`). tests/test_zenobfus.py rebuilds both inputs and
checks zenobfus detects, unpacks and previews them.
