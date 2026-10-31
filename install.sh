#!/bin/sh
# zenman installer (macOS).
#
#   curl -fsSL https://raw.githubusercontent.com/team-mirae/zenteru-plugins/main/install.sh | sh
#
# Installs zenman, the zenteru plugin manager, from the live release into
# ~/.local/bin (ZENMAN_BIN_DIR to change), verifying its SHA-256 against the
# live index. Then: `zenman list`, `zenman install zenfinder`.

set -eu

REPO="team-mirae/zenteru-plugins"
BASE="https://github.com/$REPO/releases/download/live"
BIN_DIR="${ZENMAN_BIN_DIR:-$HOME/.local/bin}"

say() { printf 'zenman: %s\n' "$*"; }
die() { printf 'zenman: error: %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "python3 is required (zenteru plugins need Python 3.11+)"

tmp="$(mktemp -d 2>/dev/null || mktemp -d -t zenman)"
trap 'rm -rf "$tmp"' EXIT INT TERM
fetch() { curl -fsSL --proto '=https' --tlsv1.2 -o "$2" "$1"; }

fetch "$BASE/index.json" "$tmp/index.json" || die "could not download the live index"
fetch "$BASE/zenman" "$tmp/zenman" || die "could not download zenman"
expected="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["zenman"]["sha256"])' "$tmp/index.json")"
actual="$(shasum -a 256 "$tmp/zenman" | cut -d ' ' -f 1)"
[ "$expected" = "$actual" ] || die "checksum mismatch for zenman"

mkdir -p "$BIN_DIR"
chmod 755 "$tmp/zenman"
mv -f "$tmp/zenman" "$BIN_DIR/zenman"
say "installed $("$BIN_DIR/zenman" --version) to $BIN_DIR/zenman"
case ":$PATH:" in
    *":$BIN_DIR:"*) say "run \`zenman list\` to see the plugins" ;;
    *)
        say "$BIN_DIR is not on your PATH. Add this to your shell profile:"
        printf '\n    export PATH="%s:$PATH"\n\n' "$BIN_DIR"
        ;;
esac
