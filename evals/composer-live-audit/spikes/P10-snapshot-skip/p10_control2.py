import json
import sqlite3
import sys


def unwrap(v):
    d = json.loads(v) if isinstance(v, str) else v
    return None if d is None else d["data"]


def show(path, a, b):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    ra = c.execute("select * from composition_states where id=?", (a,)).fetchone()
    rb = c.execute("select * from composition_states where id=?", (b,)).fetchone()
    print("pair", path.split("/instances/")[1].split("/")[0], ra["version"], ra["provenance"], "->", rb["version"], rb["provenance"])
    for k in ("sources", "nodes", "edges", "outputs", "metadata_"):
        print(f"  {k}: equal={unwrap(ra[k]) == unwrap(rb[k])}")
    print("  is_valid", ra["is_valid"], rb["is_valid"], " verr_equal", ra["validation_errors"] == rb["validation_errors"])
    print("  composer_meta A", unwrap(ra["composer_meta"]) if ra["composer_meta"] else None)
    print("  composer_meta B", unwrap(rb["composer_meta"]) if rb["composer_meta"] else None)
    for sid in (a, b):
        msgs = c.execute(
            "select role, tool_call_id, substr(content,1,80) from chat_messages where composition_state_id=? order by sequence_no", (sid,)
        ).fetchall()
        print("  msgs bound to", sid[:8], [tuple(m) for m in msgs][:6])


show(*sys.argv[1:4])
show(*sys.argv[4:7])
