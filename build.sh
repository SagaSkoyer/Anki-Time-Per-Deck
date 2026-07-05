#!/usr/bin/env bash
# Package the add-on as TimePerDeck.ankiaddon for upload to AnkiWeb.
# Per https://addon-docs.ankiweb.net/sharing.html the zip must contain the
# add-on files at its top level (no wrapping folder) and must not include
# meta.json or cache files.
set -euo pipefail
cd "$(dirname "$0")"

OUT="TimePerDeck.ankiaddon"
rm -f "$OUT"
zip -q "$OUT" __init__.py config.json config.md manifest.json
echo "Built $OUT:"
unzip -l "$OUT"
