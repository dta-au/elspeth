import json
import sqlite3
import sys


def unwrap(v):
    if v is None:
        return None
    d = json.loads(v) if isinstance(v, str) else v
    return d["data"]


K = ("sources", "nodes", "edges", "outputs", "metadata_")
n = 0
for path in sys.argv[1:]:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    prev = {}
    for r in c.execute("select * from composition_states order by session_id, version"):
        p = prev.get(r["session_id"])
        if p is not None and r["provenance"] == "post_compose" and all(unwrap(p[k]) == unwrap(r[k]) for k in K) and n < 4:
            n += 1
            print("A", p["is_valid"], (p["validation_errors"] or "")[:300])
            print("B", r["is_valid"], (r["validation_errors"] or "")[:300])
            mb = unwrap(r["composer_meta"])
            print("B.completion_gates", mb.get("completion_gates"), "repair", mb.get("repair_turns_used"))
            print()
        prev[r["session_id"]] = r
