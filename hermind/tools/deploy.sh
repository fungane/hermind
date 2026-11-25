#!/usr/bin/env bash
# Install the selected profile with the same staging, verification and rollback rules as the CLI.
set -euo pipefail
src="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
profile="${1:-default}"
exec python3 "$src/hermind_cli.py" install-hermes --profile "$profile"
