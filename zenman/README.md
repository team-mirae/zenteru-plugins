# zenman

The zenteru plugin manager: a single Python file with no dependencies.

```sh
curl -fsSL https://raw.githubusercontent.com/team-mirae/zenteru-plugins/main/install.sh | sh
```

| Command | |
|---|---|
| `zenman list` | Available plugins, live versions, and what is installed |
| `zenman install NAME… \| --all` | Install. `--force` reinstalls, or takes over a folder zenman didn't create |
| `zenman update [NAME…]` | Update installed plugins whose live build changed (all by default) |
| `zenman remove NAME…` | Uninstall. A linked plugin is only unlinked |
| `zenman info NAME` | Version, checksum, and the source commit |
| `zenman link PATH` | Symlink a plugin folder from a git checkout (development, old versions) |
| `zenman self-update` | Update zenman from the live release |

Plugins go to `~/Library/Application Support/zenteru/plugins/<name>/`. Each
one zenman installs gets a `.zenman.json` receipt recording its version,
sha256 and commit. zenman won't overwrite a folder that has no receipt
unless you pass `--force`. Downloads come from the `live` GitHub release
and are checked against the sha256 in `index.json` before anything is
unpacked. Archive members must stay inside the plugin folder; links and
device files are rejected.

**Only the newest build is served.** For an older version, clone the
repository, check out the commit you want, and run `zenman link` on the
plugin folder.

Environment: `ZENMAN_INDEX` (index URL or local path), `ZENTERU_PLUGINS_DIR`.
