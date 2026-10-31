# zentail

Tailscale in a zenteru side panel. Open it with `@zentail` or from the ⌘⇧P palette.

- **Top:** this Mac (name, Tailscale IPs), tailnet, account, the current
  exit node and any health warnings. The toolbar has Connect / Disconnect
  (Disconnect asks first), Exit node off, Netcheck and Accounts
  (`tailscale switch`).
- **Rows:** one per peer: online state, IPv4, OS, direct or relayed, and
  traffic. Each has **SSH**, **SSH as…** (asks for a user and remembers it
  for that peer),
  **Ping** (`tailscale ping -c 3`), **Copy IP**, and **Use as exit node**
  (asks first, with or without LAN access).
- **Bottom:** command output.
- **Input box:** runs `tailscale <what you type>`, e.g. `ping neo`,
  `dns status`, `get`. Commands that need a terminal (`login`, `ssh`, `up`,
  `serve`, …) open in a new tab instead.

**SSH** opens a new tab running ssh to the name your `~/.ssh/config` knows.
ssh applies a `Host` stanza only to the name typed, so zentail checks with
`ssh -G` whether a stanza matches the peer's short name (`Host web`) or its
MagicDNS name, and uses that one; otherwise it uses the MagicDNS name. If
ssh fails, the tab stays open to show why.

The panel refreshes every 5 s while it's open, and redraws only when
something changed.

## Installing Tailscale

zentail is based on the current Tailscale CLI documentation
([kb/1080](https://tailscale.com/kb/1080/cli), client 1.102). On macOS the
CLI is the app's own binary, so zentail needs only the app:

- **Not installed:** the panel links to the
  [download page](https://tailscale.com/download/mac) and the Mac App
  Store. zentail never uses Homebrew; the CLI-only formula runs its own
  `tailscaled`, which fights with the app.
- **App installed, but no `tailscale` command:** zentail runs the app's
  binary directly (with `TAILSCALE_BE_CLI=1`, as Tailscale documents for
  scripts). **Add `tailscale` command** puts one in `~/.local/bin`. It asks
  first and needs no sudo.

  The command it adds is a two-line launcher (`exec` of the app's real
  binary), not a symlink. A symlink to
  `Tailscale.app/Contents/MacOS/Tailscale` aborts with *"The current
  bundleIdentifier is unknown to the registry"*, because the app finds its
  bundle from the path it was started as. The app's own Settings → CLI
  integration → Install Now writes the same kind of launcher, to
  `/usr/local/bin`.

`ZENTAIL_TAILSCALE=/path/to/tailscale` overrides how the CLI is found.
