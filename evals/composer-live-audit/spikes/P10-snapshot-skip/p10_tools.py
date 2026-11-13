import json
import sqlite3
import sys


def unwrap(v):
    if v is None:
        return None
    d = json.loads(v) if isinstance(v, str) else v
    return d["data"]


K = ("sources", "nodes", "edges", "outputs", "metadata_")
for path in sys.argv[1:]:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    prev = {}
    for r in c.execute("select * from composition_states order by session_id, version"):
        p = prev.get(r["session_id"])
        if p is not None and r["provenance"] == "tool_call" and all(unwrap(p[k]) == unwrap(r[k]) for k in K):
            t = c.execute("select tool_call_id from chat_messages where role='tool' and composition_state_id=?", (r["id"],)).fetchone()
            name = None
            if t:
                for m in c.execute(
                    "select tool_calls from chat_messages where role='assistant' and tool_calls is not null and session_id=?",
                    (r["session_id"],),
                ):
                    for call in json.loads(m[0]) or []:
                        if call.get("id") == t[0]:
                            name = call.get("function", {}).get("name")
            print(path.split("/instances/")[1].split("/")[0], f"v{p['version']}->v{r['version']}", name)
        prev[r["session_id"]] = r
