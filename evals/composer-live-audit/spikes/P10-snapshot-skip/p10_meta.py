import collections
import json
import sqlite3
import sys


def unwrap(v):
    if v is None:
        return None
    d = json.loads(v) if isinstance(v, str) else v
    return d["data"]


K = ("sources", "nodes", "edges", "outputs", "metadata_")
agg = collections.Counter()
for path in sys.argv[1:]:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute("select * from composition_states order by session_id, version").fetchall()
    prev = {}
    for r in rows:
        p = prev.get(r["session_id"])
        if p is not None and all(unwrap(p[k]) == unwrap(r[k]) for k in K):
            ma = unwrap(p["composer_meta"]) or {}
            mb = unwrap(r["composer_meta"]) or {}
            diffkeys = sorted(k for k in set(ma) | set(mb) if ma.get(k) != mb.get(k))
            agg[
                (
                    p["provenance"],
                    r["provenance"],
                    f"valid {p['is_valid']}->{r['is_valid']}",
                    f"errs_eq={p['validation_errors'] == r['validation_errors']}",
                    tuple(diffkeys),
                )
            ] += 1
        prev[r["session_id"]] = r
for k, v in agg.items():
    print(v, k)
