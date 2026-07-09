#!/usr/bin/env bash
# Builds the frontend and packages the plugin into out/egpu-switch.zip, laid
# out the way Decky Loader's "Install Plugin from ZIP File" (Settings ->
# Developer) expects: a single top-level folder containing plugin.json at
# its root, with dist/index.js alongside main.py and package.json.
set -euo pipefail
cd "$(dirname "$0")/.."

npx rollup -c

rm -rf out
mkdir -p out/egpu-switch/dist
cp plugin.json main.py package.json out/egpu-switch/
cp dist/index.js out/egpu-switch/dist/

(cd out && zip -r egpu-switch.zip egpu-switch)
rm -rf out/egpu-switch

echo "Created out/egpu-switch.zip"
