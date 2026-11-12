#!/usr/bin/env bash
# One-time setup of the optional semantic-search worker, for a checkout or a deployed plugin folder: the same as
# `hermind setup-vectors PROFILE`
# (vec_setup.py: a private virtualenv plus one local ONNX model in $HERMIND_VEC_HOME, default ~/.hermes/hermind-vec;
# an existing ~/.hermes/dm-vec is kept). Profiles: default, light, balanced, quality (see `hermind setup-vectors -h`).
# Afterwards embed the existing notes with tools/embed_all.py DB '{"profile": "<name>"}' or `hermind embed-all`.
set -euo pipefail
exec python3 "$(cd "$(dirname "$0")/.." && pwd)/vec_setup.py" "${1:-default}"
