# zenfinder

A file browser in a zenteru side panel for the session's directory, local
or over ssh. Open it with `@zenfinder` or from the ⌘⇧P palette.

```
┌ ~/src/app ─────────────── ↑ Up  ⟳  ~ Home  Paste text  cd shell here ┐
│ ▸ docs/                                         [Open] [Insert path]  │
│ · main.py   4.2 K  10-31 21:40       [Preview] [Insert path] [Reveal] │
├───────────────────────────────────────────────────────────────────────┤
│ main.py — text, 4.2 K                     [Quick Look] [Reveal] [Close]│
│ import zenteru …                                                      │
└───────────────────────────────────────────────────────────────────────┘
```

- **Local sessions** show the shell's working directory.
- **ssh sessions** are detected when the foreground job is `ssh`. zenfinder
  opens its own connection with the same destination and connection
  options (`-p`, `-i`, `-J`, `-l`, `-F`, `-o`), takes the starting
  directory from the remote prompt (`user@host:~/src$`), and falls back to
  the remote home.
- The panel **follows the shell** as you `cd`, until you navigate in the
  panel yourself. **Follow shell** turns it back on.
- The browser sits at the top and the **preview** at the bottom: the
  **picture itself** for images (PNG, JPEG, GIF, WebP, HEIC, TIFF, BMP,
  PDF…; zenteru 0.1.17+), text, or a hex dump for other binary files.
  Images on this Mac are passed to zenteru by path; images on an ssh host
  (up to 32 MiB) are fetched and sent inline. If zenteru can't decode one,
  the preview says so and falls back to text.
- **Insert path** types the quoted path into the session. **cd shell here**
  types `cd -- <dir>` without pressing Enter.
- **Download** (ssh) saves to `~/Downloads`, never overwriting. **Reveal**
  and **Quick Look** work on this Mac.

## Drag and drop, clipboard

While the panel is open in a window:

- **Files dropped** on the terminal, or **pasted** after copying them in
  Finder, go into the directory shown, after a confirmation. They are
  uploaded over ssh or copied locally, and folders work too. You can choose
  **Insert paths instead** to get zenteru's usual behaviour. When the panel
  is closed, zenteru behaves as before.
- **Pasted images** (screenshots) ask for a file name, prefilled with
  `paste-<time>.png`.
- **Clipboard text:** **Paste text** (⌘⇧V, or `@zenfinder-paste`) saves it as
  a file. The panel shows a preview and asks for the name, prefilled with
  `paste-<time>.txt`. That prompt is the confirmation: Enter saves, and an
  empty name or Cancel saves nothing. A name without an extension gets the
  default format. Set the default with `format md` in the panel or
  `@zenfinder-format md`; it's saved in
  `~/Library/Application Support/zenteru/plugin-data/zenfinder/config.json`.
- Existing files are never replaced silently. zenfinder asks first, and
  you can overwrite, rename, skip or stop.

## ssh authentication

zenfinder connects non-interactively (`BatchMode=yes`) with your keys or
agent. It reuses an OpenSSH control master when there is one:

- If `~/.ssh/config` sets a `ControlPath`, zenfinder uses it, so when your
  terminal's ssh is the master, the panel needs no login at all.
- Otherwise it keeps its own master in `/tmp/zenfinder-<uid>/` for 10
  minutes.

If it can't log in (password or 2FA hosts), the panel offers
**Authenticate in a new tab**: one interactive `ssh -f -N` that becomes
the master. It also shows the config snippet that lets your own sessions
share their login:

```
Host *
    ControlMaster auto
    ControlPath ~/.ssh/cm-%C
    ControlPersist 10m
```

The remote side needs a POSIX `sh`, plus `find`, `stat` or `ls`, `head`,
`cat` and `tar`.

## Panel commands

Type in the input box: a name (opens a folder or previews a file),
`cd <path>`, `..`, `mkdir <name>`, `get <name>`, `format <ext>`, `hidden`,
`follow`, `?`.

## Notes

- zenteru gives drop handlers only files and images. Typed or pasted text
  always goes straight to the shell, which is why text uses the Paste text
  command.
- zenteru asks drop handlers in registration order. When zenteru's bundled
  **fileholder** registers first, it keeps pasted screenshots and files
  from temporary folders for itself, and zenfinder sees only the rest.
