"""Count composition_states rows whose authored content equals the immediately previous row (per session, by version)."""

import collections
import hashlib
import json
import sqlite3
import sys


def unwrap(v):
    if v is None:
        return None
    d = json.loads(v) if isinstance(v, str) else v
    assert isinstance(d, dict) and "_version" in d, d
    return d["data"]


def chash(row):
    payload = {k: unwrap(row[k]) for k in ("sources", "nodes", "edges", "outputs", "metadata_")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def full(row):
    payload = {k: unwrap(row[k]) for k in ("sources", "nodes", "edges", "outputs", "metadata_", "composer_meta")}
    payload["is_valid"] = row["is_valid"]
    payload["validation_errors"] = row["validation_errors"]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


tot = collections.Counter()
examples = {}
for path in sys.argv[1:]:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute("select * from composition_states order by session_id, version").fetchall()
    prev = {}
    seen = collections.defaultdict(set)
    per = collections.Counter()
    for r in rows:
        sid = r["session_id"]
        h = chash(r)
        f = full(r)
        per["rows"] += 1
        per["prov:" + r["provenance"]] += 1
        if sid in prev:
            ph, pf, pprov, pid = prev[sid]
            if h == ph:
                per["eq_prev_content"] += 1
                per[f"eq_prev:{pprov}->{r['provenance']}"] += 1
                if f == pf:
                    per["eq_prev_full_incl_meta_valid"] += 1
                examples.setdefault("identical", (path, pid, r["id"]))
            else:
                examples.setdefault("different", (path, pid, r["id"]))
                if h in seen[sid]:
                    per["eq_older_not_prev(A-B-A)"] += 1
        seen[sid].add(h)
        prev[sid] = (h, f, r["provenance"], r["id"])
    print(path.split("/instances/")[1].split("/")[0], dict(per))
    tot.update(per)
print("TOTAL", dict(tot))
print("EXAMPLES", examples)
