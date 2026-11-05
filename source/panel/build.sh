#!/bin/sh
# Local development only. The .deb builder uses these reviewed compiled assets.
set -eu
cd -- "$(dirname -- "$0")"
modules="${FRAMEBRIDGE_PANEL_NODE_MODULES:-$PWD/node_modules}"
NODE_PATH="$modules" "$modules/.bin/esbuild" client.tsx --bundle --minify --jsx=automatic \
    --format=esm --target=es2022 --outfile=public/panel.js --define:process.env.NODE_ENV='"production"'
contract="$(mktemp)"
trap 'rm -f -- "$contract"' EXIT
NODE_PATH="$modules" "$modules/.bin/esbuild" contract.ts --bundle --platform=node --format=cjs > "$contract"
node --input-type=commonjs < "$contract" > contract.json
