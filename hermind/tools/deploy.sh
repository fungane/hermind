#!/usr/bin/env bash
# Copy this checkout's hermind/ folder into a profile's installed plugin dir (runtime files only). Restart the gateway afterwards.
#   hermind/tools/deploy.sh [PROFILE]   (default: default)
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
P="${1:-default}"
if [ "$P" = default ]; then H="$HOME/.hermes"; else H="$HOME/.hermes/profiles/$P"; fi
D="$H/plugins/hermind"; mkdir -p "$D/tools"
for f in __init__.py store*.py llm.py pii.py dbdriver.py vec.py textnorm.py screen.py embed_server.py vec_setup.py import_hindsight.py db_backup.py import_core.py import_readers.py md_notes.py text_blocks.py export_notes.py md_export.py reader_hermind_json.py reader_claude_code.py reader_exports.py synonyms.yaml plugin.yaml; do cp "$SRC"/$f "$D/"; done
cp "$SRC"/../README.md "$D/"
cp "$SRC"/tools/*.py "$SRC"/tools/*.sh "$D/tools/"
python3 -c "import ast,sys;[ast.parse(open('$D/'+f).read()) for f in ('__init__.py','store.py')]"
echo "deployed $(grep ^version: "$SRC/plugin.yaml") to $D"
