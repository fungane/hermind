#!/usr/bin/env python3
"""Run the Hermind command line from a git checkout: python3 hermind_cli.py --help

The code lives in hermind/; this file only starts hermind/hermind_cli.py, so the commands in the README work from the
top of the checkout. Installed with pip or uvx, use the `hermind` command instead."""
import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    pkg = Path(__file__).resolve().parent / "hermind"
    sys.path.insert(0, str(pkg))
    runpy.run_path(str(pkg / "hermind_cli.py"), run_name="__main__")
