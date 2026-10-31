# zenteru plugins

Plugins for [zenteru](https://github.com/team-mirae/zenteru), plus
**zenman**, the command that installs and updates them.

| | What it is |
|---|---|
| [**zenman**](zenman/) | Plugin manager CLI. It installs, updates, removes and links plugins from the live channel. |
| [**zentail**](zentail/) | Tailscale in a side panel: status, peers, SSH, ping, exit nodes, accounts. It uses the Tailscale app's own CLI. |
| [**zenfinder**](zenfinder/) | File browser in a side panel for the session's directory, on this Mac or over ssh. Drop files to upload them, or paste clipboard text as a file. |
| [**zenobfus**](zenobfus/) | Browser for [`.obfus`](https://github.com/themrsung/file-obfuscation) files, laid out like zenfinder. Files are unpacked in memory and previewed, archives included. |

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/team-mirae/zenteru-plugins/main/install.sh | sh
zenman list
zenman install zenfinder zentail zenobfus     # or: zenman install --all
```

zenteru starts plugins when it launches, so quit and reopen it after you
install or update one. Each plugin's panel opens from Black Magic
(`@zenfinder`, `@zentail`, `@zenobfus`) or from the ⌘⇧P palette.

## The live channel

GitHub Actions ([`.github/workflows/live.yml`](.github/workflows/live.yml))
tests every push. On `main` it also rebuilds the **`live`** release. That
release always holds the newest build of every plugin and nothing older:

```
https://github.com/team-mirae/zenteru-plugins/releases/download/live/
  index.json          plugin names, versions, sha256, the commit built
  <plugin>.tar.gz     one per plugin (deterministic, so unchanged plugins keep their checksum)
  zenman              the CLI itself (zenman self-update)
  install.sh
```

`zenman update` replaces a plugin only when its checksum changed. Old
versions are not served. To use one, take it from git:

```sh
git clone https://github.com/team-mirae/zenteru-plugins
git -C zenteru-plugins checkout <commit>
zenman link zenteru-plugins/zenfinder        # symlinks the checkout into zenteru's plugins folder
```

`zenman link` is also how you develop a plugin: edit the checkout, then
reopen zenteru.

## Layout

```
zenman/       zenman (single-file Python CLI)
zentail/      plugin.json, main.py, zentail/  (package)
zenfinder/    plugin.json, main.py, zenfinder/
zenobfus/     plugin.json, main.py, zenobfus/
shared/       browserpanel.py: the browser/preview panel layout, vendored into each plugin
scripts/      build_live.py (builds the release), sync_shared.py (copies shared/ into plugins)
tests/        unit tests and panel tests against zenteru's mock API server
```

Plugins are Python 3.11+ and use only the standard library, like zenteru's
bundled plugins. Each one installs on its own, so `shared/browserpanel.py`
is copied into every plugin. Run `scripts/sync_shared.py` after you edit
it; CI fails if a copy drifts.

## Tests

```sh
python3 -m unittest discover -s tests
```

The panel tests drive each plugin through zenteru's mock API server. They
look for zenteru's source in `ZENTERU_SRC`, then `../zenteru`, then
`./_zenteru`, which is where CI checks it out. The ssh tests use a stand-in
`ssh` that runs the "remote" commands locally, so they don't need a server.

## License

MIT © 2026 Mirae Research. zenobfus's `.obfus` codec is a port of
[file-obfuscation](https://github.com/themrsung/file-obfuscation)'s
`obfus_core.py`, which is CC0.
