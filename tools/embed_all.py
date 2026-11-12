#!/usr/bin/env python3
"""Embed every note that has no vector yet (run once after an import; afterwards new notes are embedded in the background).
usage: python3 tools/embed_all.py PATH/TO/hermind.db [config-json]   (about 90 ms per note on 2 CPU cores)
The same as `hermind embed-all --db DB --profile NAME` (vec_setup.embed_all)."""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from vec_setup import embed_all

if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
    print(__doc__)
    sys.exit(0 if len(sys.argv) > 1 else 2)
res = embed_all(sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv) > 2 else {},
                log=lambda m: print(f"\r{m}", end="", flush=True))
if not res["ok"]:
    sys.exit("\n" + res["error"])
print(f"\ndone: {res['embedded']} notes in {res['seconds']:.0f}s")
for n in res.get("notes", []):
    print("note:", n)
