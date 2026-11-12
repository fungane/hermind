#!/usr/bin/env python3
"""Read-only export of every memory of one Hindsight bank through its local REST API (GET only; nothing is changed).
usage: python3 tools/export_hindsight.py BANK OUT.json [--url http://127.0.0.1:9177]
OUT.json can be fed to import_hindsight.py and doubles as a full backup of the bank's facts and observations."""
import json
import sys
import urllib.error
import urllib.request

if len(sys.argv) < 3 or sys.argv[1] in ("-h", "--help"):
    print(__doc__)
    sys.exit(0 if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help") else 2)
bank, out = sys.argv[1], sys.argv[2]
url = sys.argv[sys.argv.index("--url") + 1] if "--url" in sys.argv else "http://127.0.0.1:9177"
items, off = [], 0
try:
    while True:
        d = json.load(urllib.request.urlopen(f"{url}/v1/default/banks/{bank}/memories/list?limit=500&offset={off}", timeout=120))
        items += d["items"]
        off += 500
        if off >= d["total"]:
            break
    json.dump({"bank": bank, "total": len(items), "items": items}, open(out, "w"), ensure_ascii=False)
except urllib.error.HTTPError as exc:
    sys.exit(f"export_hindsight: the server at {url} answered HTTP {exc.code} for bank '{bank}'")
except urllib.error.URLError as exc:
    sys.exit(f"export_hindsight: cannot reach {url}: {exc.reason}")
except (OSError, ValueError, KeyError, TypeError) as exc:      # a timeout, a file that cannot be written, an answer that is not the expected JSON
    sys.exit(f"export_hindsight: failed ({type(exc).__name__}: {str(exc)[:160]})")
print(f"{len(items)} memories of bank '{bank}' -> {out}")
