"""Compatibility name for the shared Markdown helpers, which now live in md_notes.py in the repository root (so that
`hermind import` ships in the wheel). `import mdnotes` from tools/ keeps working and returns that same module."""
import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import md_notes  # noqa: E402

sys.modules[__name__] = md_notes
