#!/usr/bin/env python3
"""Stream Claude Code transcripts and measure Loomweave usage. Read-only."""

import glob
import json
import os
import random
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime

D = "/home/john/.claude/projects/-home-john-elspeth"
OUT = os.path.dirname(os.path.abspath(__file__))

GREP_BASH_RE = re.compile(r"(^|[\s;&|(`])(git\s+grep|rg|grep|egrep|fgrep|find)\s")
PATH_RE = re.compile(r'/home/john/elspeth[^\s"\'\\,)\]]+')
PERSISTED_RE = re.compile(r"tool-results/[A-Za-z0-9_-]+\.txt|Output too large|persisted to|saved to")
FOLLOW_TOOLS = {"mcp__loomweave__entity_callers_list", "mcp__loomweave__entity_find", "mcp__loomweave__entity_semantic_search_list"}
LIST_KEYS = (
    "callers",
    "entities",
    "items",
    "results",
    "matches",
    "members",
    "sites",
    "call_sites",
    "paths",
    "relations",
    "findings",
    "neighbors",
    "nodes",
    "edges",
    "guidance",
    "tests",
    "commands",
    "routes",
    "hits",
    "candidates",
    "diff",
    "changes",
)


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.strptime(s[:23], "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=UTC).timestamp()
    except Exception:
        try:
            return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC).timestamp()
        except Exception:
            return None


def tool_class(name, inp):
    if name.startswith("mcp__loomweave__"):
        return "loomweave"
    if name.startswith("mcp__filigree__"):
        return "filigree"
    if name in ("Grep", "Glob"):
        return "grep"
    if name == "Bash":
        cmd = inp.get("command") if isinstance(inp, dict) else None
        if isinstance(cmd, str) and GREP_BASH_RE.search(cmd):
            return "grep"
    return "other"


def compact_input(name, inp):
    if not isinstance(inp, dict):
        return ""
    if name == "Bash":
        return str(inp.get("command", ""))[:400]
    if name in ("Grep", "Glob"):
        return f"{inp.get('pattern', '')} {inp.get('path', '')}"[:300]
    if name == "Read":
        return str(inp.get("file_path", ""))
    return json.dumps(inp)[:400]


def result_text(block):
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    return ""


def classify_loom(name, block, txt):
    """Return dict of classification flags for a loomweave tool_result."""
    f = {
        "status": "ok",
        "stale": False,
        "reindex": False,
        "briefing_blocked": False,
        "unresolved": 0,
        "n_callers": None,
        "n_resolved": None,
        "persisted": False,
    }
    low = txt.lower()
    if PERSISTED_RE.search(txt):
        f["persisted"] = True
    if block.get("is_error"):
        f["status"] = "error"
    if "stale" in low:
        f["stale"] = True
    if "reindex" in low or "re-index" in low or "analyze_start" in low:
        f["reindex"] = True
    if "briefing_blocked" in low and '"briefing_blocked": 0' not in low and '"briefing_blocked":0' not in low:
        f["briefing_blocked"] = True
    obj = None
    s = txt.strip()
    if s.startswith("{"):
        try:
            obj = json.loads(s)
        except Exception:
            obj = None
    if obj is None:
        if f["status"] != "error":
            if s.startswith("Error") or "MCP error" in s or s.startswith("error"):
                f["status"] = "error"
            else:
                f["status"] = "nonjson"
        return f
    if obj.get("ok") is False or obj.get("error") not in (None, "") or ("code" in obj and "error" in obj):
        f["status"] = "error"
        return f
    res = obj.get("result", obj)
    if isinstance(res, dict):
        for k in ("unresolved_name_matches", "unresolved_count"):
            if isinstance(res.get(k), int):
                f["unresolved"] += res[k]
        if isinstance(res.get("unresolved_candidates"), list):
            f["unresolved"] += len(res["unresolved_candidates"])
        if isinstance(res.get("unresolved"), list):
            f["unresolved"] += len(res["unresolved"])
        if name == "mcp__loomweave__entity_callers_list" and isinstance(res.get("callers"), list):
            callers = res["callers"]
            f["n_callers"] = len(callers)
            f["n_resolved"] = sum(1 for c in callers if not isinstance(c, dict) or c.get("confidence") in (None, "resolved", "static"))
        lists = [v for k, v in res.items() if k in LIST_KEYS and isinstance(v, list)]
        if (lists and all(len(v) == 0 for v in lists)) or (not lists and res.get("total") == 0) or (not lists and not res):
            f["status"] = "empty"
    elif isinstance(res, list) and len(res) == 0:
        f["status"] = "empty"
    return f


class Session:
    __slots__ = ("agent_name", "byid", "calls", "first_ts", "key", "kind")

    def __init__(self, key, kind):
        self.key = key
        self.kind = kind  # main | teammate | subagent
        self.agent_name = None
        self.calls = []  # list of dicts
        self.byid = {}
        self.first_ts = None


def stream(path, sess, agent_types, pending_agent_calls):
    with open(path, errors="replace") as fh:
        for line in fh:
            if "tool_use" not in line and "tool_result" not in line and "agentName" not in line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if not isinstance(r, dict):
                continue
            if sess.agent_name is None and r.get("agentName"):
                sess.agent_name = r["agentName"]
                if sess.kind == "main":
                    sess.kind = "teammate"
            ts = r.get("timestamp")
            if sess.first_ts is None and ts:
                sess.first_ts = ts
            m = r.get("message")
            if not isinstance(m, dict):
                continue
            content = m.get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "tool_use":
                    name = b.get("name") or "?"
                    inp = b.get("input") or {}
                    rec = {
                        "id": b.get("id"),
                        "name": name,
                        "ts": ts,
                        "cls": tool_class(name, inp),
                        "inp": compact_input(name, inp),
                        "lat": None,
                        "res": None,
                        "paths": None,
                    }
                    if name == "Agent" and isinstance(inp, dict):
                        pending_agent_calls[b.get("id")] = inp.get("subagent_type") or "unspecified"
                    sess.byid[rec["id"]] = len(sess.calls)
                    sess.calls.append(rec)
                elif bt == "tool_result":
                    tid = b.get("tool_use_id")
                    idx = sess.byid.get(tid)
                    if idx is None:
                        continue
                    rec = sess.calls[idx]
                    t0, t1 = parse_ts(rec["ts"]), parse_ts(ts)
                    if t0 is not None and t1 is not None and t1 >= t0:
                        rec["lat"] = t1 - t0
                    if rec["name"] == "Agent":
                        tr = r.get("toolUseResult")
                        aid = None
                        if isinstance(tr, dict):
                            aid = tr.get("agentId")
                        if aid:
                            agent_types[aid] = pending_agent_calls.get(tid, "unspecified")
                    if rec["cls"] == "loomweave":
                        txt = result_text(b)
                        rec["res"] = classify_loom(rec["name"], b, txt)
                        if rec["name"] in FOLLOW_TOOLS:
                            paths = []
                            for mm in PATH_RE.finditer(txt):
                                p = mm.group(0)
                                if p not in paths:
                                    paths.append(p)
                                if len(paths) >= 40:
                                    break
                            rec["paths"] = paths


def main():
    main_files = sorted(glob.glob(D + "/*.jsonl"))
    sub_files = sorted(glob.glob(D + "/*/subagents/*.jsonl"))
    sessions = []
    agent_types = {}
    pending = {}
    nbytes = 0
    for i, f in enumerate(main_files):
        s = Session(os.path.basename(f)[:-6], "main")
        stream(f, s, agent_types, pending)
        sessions.append(s)
        nbytes += os.path.getsize(f)
        if i % 200 == 0:
            print(f"main {i}/{len(main_files)} {nbytes / 1e9:.2f}GB", file=sys.stderr, flush=True)
    for i, f in enumerate(sub_files):
        parent = f.split("/")[-3]
        aid = os.path.basename(f)[:-6]
        s = Session(f"sub:{parent}/{aid}", "subagent")
        stream(f, s, agent_types, pending)
        sessions.append(s)
        if i % 400 == 0:
            print(f"sub {i}/{len(sub_files)}", file=sys.stderr, flush=True)

    # ---------- 1. VOLUME ----------
    months = ["2026-06", "2026-07", "2026-08"]
    vol = defaultdict(Counter)  # cls -> month -> n
    per_tool = defaultdict(Counter)  # tool -> month -> n
    total = Counter()
    sess_loom = Counter()
    sess_total = Counter()
    for s in sessions:
        used = False
        if s.calls:
            sess_total[s.kind] += 1
        for c in s.calls:
            mo = (c["ts"] or "")[:7]
            if mo not in months:
                mo = "other"
            total[mo] += 1
            vol[c["cls"]][mo] += 1
            if c["cls"] in ("loomweave", "grep", "filigree"):
                key = c["name"] if c["cls"] != "grep" or c["name"] != "Bash" else "Bash(grep/rg/find)"
                per_tool[key][mo] += 1
            if c["cls"] == "loomweave":
                used = True
        if used:
            sess_loom[s.kind] += 1

    # ---------- 2. RESULT QUALITY ----------
    qual = {}
    for s in sessions:
        for c in s.calls:
            if c["cls"] != "loomweave":
                continue
            q = qual.setdefault(c["name"], Counter())
            q["calls"] += 1
            r = c["res"]
            if r is None:
                q["no_result"] += 1
                continue
            q["with_result"] += 1
            q[r["status"]] += 1
            for k in ("stale", "reindex", "briefing_blocked", "persisted"):
                if r[k]:
                    q[k] += 1
            if r["unresolved"]:
                q["unresolved_calls"] += 1
                q["unresolved_sum"] += r["unresolved"]
            if r["n_callers"] is not None:
                q["callers_measured"] += 1
                if r["n_resolved"] == 0:
                    q["zero_resolved_callers"] += 1
                if r["n_callers"] == 0:
                    q["zero_callers"] += 1

    # ---------- 3. FOLLOW-THROUGH ----------
    cands = []
    for s in sessions:
        for i, c in enumerate(s.calls):
            if c["name"] in FOLLOW_TOOLS and c["res"] is not None:
                cands.append((s, i))
    random.seed(20260829)
    sample = random.sample(cands, min(40, len(cands)))

    def symbol_of(c):
        try:
            inp = json.loads(c["inp"]) if c["inp"].startswith("{") else {}
        except Exception:
            inp = {}
        for k in ("id", "name", "query", "pattern", "q", "text"):
            v = inp.get(k)
            if isinstance(v, str) and v:
                return v.split(".")[-1] if k == "id" else v
        return c["inp"][:40]

    follow = Counter()
    examples = defaultdict(list)
    for s, i in sample:
        c = s.calls[i]
        sym = symbol_of(c)
        toks = [t for t in re.split(r"[^A-Za-z0-9_]+", sym) if len(t) >= 4]
        paths = set(c["paths"] or [])
        bases = {os.path.basename(p) for p in paths}
        nxt = s.calls[i + 1 : i + 4]
        cats = []
        for n in nxt:
            inp = n["inp"]
            low = inp.lower()
            regrep = n["cls"] == "grep" and any(t.lower() in low for t in toks)
            used = n["name"] in ("Read", "Bash", "mcp__loomweave__entity_source_get") and (
                any(p in inp for p in paths) or any(bb and bb in inp for bb in bases)
            )
            if regrep:
                cats.append("regrep")
            elif used:
                cats.append("used")
            elif n["cls"] == "loomweave":
                cats.append("loom")
            else:
                cats.append("other")
        if "regrep" in cats:
            cat = "regrep_same_symbol"
        elif "used" in cats:
            cat = "read_result_path"
        elif cats and all(x == "loom" for x in cats):
            cat = "more_loomweave"
        elif not nxt:
            cat = "no_following_call"
        else:
            cat = "unrelated"
        follow[cat] += 1
        if len(examples[cat]) < 5:
            examples[cat].append(
                {
                    "tool": c["name"].replace("mcp__loomweave__", ""),
                    "symbol": sym[:60],
                    "status": c["res"]["status"],
                    "next": [n["name"] for n in nxt],
                    "kind": s.kind,
                }
            )

    # ---------- 4. LATENCY ----------
    lat = defaultdict(list)
    for s in sessions:
        for c in s.calls:
            if c["lat"] is None:
                continue
            if c["cls"] == "loomweave":
                lat["loomweave(all)"].append(c["lat"])
                lat[c["name"]].append(c["lat"])
            elif c["name"] in ("Grep", "Glob", "Read"):
                lat[c["name"]].append(c["lat"])
            elif c["cls"] == "grep":
                lat["Bash(grep/rg/find)"].append(c["lat"])
            elif c["cls"] == "filigree":
                lat["filigree(all)"].append(c["lat"])

    def pct(xs, p):
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(p * len(xs)))]

    latency = {
        k: {"n": len(v), "median": round(statistics.median(v), 2), "p90": round(pct(v, 0.9), 2)} for k, v in lat.items() if len(v) >= 10
    }

    # ---------- 5. WHO ----------
    who = defaultdict(Counter)  # kind -> cls -> n
    who_loom_tools = defaultdict(Counter)  # kind -> tool -> n
    sub_type_tools = defaultdict(Counter)  # subagent_type -> cls -> n
    sub_type_loom = defaultdict(Counter)
    for s in sessions:
        for c in s.calls:
            who[s.kind][c["cls"]] += 1
            if c["cls"] == "loomweave":
                who_loom_tools[s.kind][c["name"]] += 1
            if s.kind == "subagent":
                aid = s.key.split("/")[-1].replace("agent-", "")
                st = agent_types.get(aid, "unknown")
                sub_type_tools[st][c["cls"]] += 1
                if c["cls"] == "loomweave":
                    sub_type_loom[st][c["name"]] += 1

    out = {
        "files": {"main": len(main_files), "subagent": len(sub_files)},
        "total_calls_by_month": dict(total),
        "class_by_month": {k: dict(v) for k, v in vol.items()},
        "per_tool_by_month": {k: dict(v) for k, v in per_tool.items()},
        "sessions_total": dict(sess_total),
        "sessions_using_loomweave": dict(sess_loom),
        "quality": {k: dict(v) for k, v in qual.items()},
        "follow_through": {"n_candidates": len(cands), "n_sample": len(sample), "counts": dict(follow), "examples": dict(examples)},
        "latency": latency,
        "who": {k: dict(v) for k, v in who.items()},
        "who_loom_tools": {k: dict(v) for k, v in who_loom_tools.items()},
        "subagent_type_classes": {k: dict(v) for k, v in sub_type_tools.items()},
        "subagent_type_loom_tools": {k: dict(v) for k, v in sub_type_loom.items()},
        "agent_types_mapped": len(agent_types),
    }
    with open(os.path.join(OUT, "claude_summary.json"), "w") as fh:
        json.dump(out, fh, indent=1, sort_keys=True)
    print("done", file=sys.stderr)


if __name__ == "__main__":
    main()
