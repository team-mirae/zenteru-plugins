#!/bin/sh
# Run obfus-react's engine through bridge.ts.
#   scripts/obfus-react/run.sh /path/to/obfus-react pack in out 4096 1
# Needs Node 24+ (runs TypeScript by stripping types). The engine's
# extensionless imports are rewritten to ".ts" in a temporary copy.
set -eu
src="$1"; shift
here="$(cd "$(dirname "$0")" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cp "$here/bridge.ts" "$tmp/"
cp -R "$src/src/engine" "$tmp/engine"
rm -rf "$tmp/engine/__tests__"
for f in "$tmp"/engine/*.ts; do
  sed -E "s#from '\./([A-Za-z0-9_]+)'#from './\1.ts'#g" "$f" > "$f.new" && mv "$f.new" "$f"
done
node "$tmp/bridge.ts" "$@"
