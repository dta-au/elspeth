"""Mine live battery sessions for instruction confusion, tool failures and advisor vetoes.

Audit harness (not product code). Reads sessions/<scenario>/turnNN/llm_calls.json written by
drive_session.py and writes a markdown digest.

usage: mine_sessions.py <sessions dir> <out.md> <out.json>
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

CONFUSION = re.compile(
    r"(not (?:offered|available|in my (?:tool|palette))|isn'?t (?:offered|available)|don'?t have (?:a|the|any)? ?`?\w+`? tool|"
    r"no `?\w+`? tool|unknown tool|not (?:a|an) (?:offered|available) tool|"
    r"contradict|conflict(?:s|ing)? with|inconsisten|ambiguous|unclear|confus|"
    r"the (?:skill|contract|instructions?|policy) (?:says|said|states|requires|tells)|"
    r"but wait|hmm|wait —|wait,|actually,? (?:no|wait)|on second thought|i'?m not sure|not sure (?:if|whether)|"
    r"which (?:one|tool) (?:should|do) i|can'?t find|doesn'?t exist|does not exist|no such)",
    re.IGNORECASE,
)


ADVISOR_MODEL = os.environ.get("ELSPETH_WEB__COMPOSER_ADVISOR_MODEL", "").split("/", 1)[-1]
REPLY_STAGE_TOOLS = frozenset({"handoff_to_user", "restore_parked_graph"})


def surface(tool_names: list[str], model: str | None = None) -> str:
    """Classify a captured request by offered tools and model (see build_register.py)."""
    offered = frozenset(tool_names)
    if not offered:
        return "advisor"
    if offered == REPLY_STAGE_TOOLS:
        return "reply_stage"
    if "emit_pipeline_proposal" in offered and "set_pipeline" not in offered:
        return "planner_hatch" if ADVISOR_MODEL and model and model.endswith(ADVISOR_MODEL) else "planner"
    if "set_pipeline" in offered:
        return "tool_loop"
    return "other"


def sentences_with_markers(text: str) -> list[str]:
    hits = []
    for para in re.split(r"(?<=[.!?])\s+|\n+", text):
        if CONFUSION.search(para) and len(para) > 25:
            hits.append(para.strip()[:400])
    return hits


def main() -> int:
    root = Path(sys.argv[1])
    md = ["# Live battery digest\n"]
    data: dict = {"scenarios": {}, "totals": Counter(), "tool_fail": Counter(), "tool_calls": Counter(), "advisor": Counter()}
    for sdir in sorted(p for p in root.iterdir() if p.is_dir()):
        summ = sdir / "summary.json"
        if not summ.exists():
            continue
        s = json.loads(summ.read_text())
        md.append(f"\n## {s['scenario']}\n")
        for t in s["turns"]:
            tdir = sdir / f"turn{t['turn']:02d}"
            calls = json.loads((tdir / "llm_calls.json").read_text()) if (tdir / "llm_calls.json").exists() else []
            md.append(f"\n### T{t['turn']}: {t['user'][:160]!r}\n")
            md.append(
                f"- {t['compose_seconds']}s, status={t['operation_status']}, valid={t['validation_is_valid']}, provider_calls={len(calls)}"
            )
            reply = (t["assistant_reply"] or "") if t["assistant_reply"] else json.dumps(t["error"])[:600]
            md.append(f"- USER SAW: {reply[:700]!r}")
            for c in calls:
                surf = surface(c["tool_names"], c.get("model"))
                data["totals"][surf] += 1
                resp = c.get("response") or {}
                msg = ((resp.get("choices") or [{}])[0]).get("message") or {}
                reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
                content = msg.get("content") or ""
                tcs = [(x["function"]["name"], x["function"]["arguments"]) for x in msg.get("tool_calls") or []]
                for name, _ in tcs:
                    data["tool_calls"][f"{surf}:{name}"] += 1
                # tool results this call received (after the last assistant message)
                msgs = c["messages"] or []
                id2name = {tc.get("id"): tc["function"]["name"] for m in msgs for tc in (m.get("tool_calls") or [])}
                last_asst = max((i for i, m in enumerate(msgs) if m.get("role") == "assistant"), default=-1)
                fails = []
                for m in msgs[last_asst + 1 :]:
                    if m.get("role") != "tool":
                        continue
                    body = m["content"] if isinstance(m["content"], str) else json.dumps(m["content"])
                    try:
                        parsed = json.loads(body)
                    except ValueError:
                        parsed = None
                    if isinstance(parsed, dict) and (
                        parsed.get("success") is False or ("error" in parsed and parsed.get("success") is not True)
                    ):
                        name = id2name.get(m.get("tool_call_id"), "?")
                        data["tool_fail"][f"{surf}:{name}"] += 1
                        fails.append((name, body[:900]))
                verdict = None
                if surf == "advisor" and content.strip().startswith("{"):
                    try:
                        v = json.loads(content)
                        verdict = f"{v.get('verdict')}/{v.get('publication_verdict')}"
                        data["advisor"][verdict] += 1
                    except ValueError:
                        verdict = "unparsed"
                md.append(
                    f"\n- call seq={c['seq']} [{surf}] {c['duration_s'] and round(c['duration_s'], 1)}s "
                    f"in={((resp.get('usage') or {}).get('prompt_tokens'))} out={((resp.get('usage') or {}).get('completion_tokens'))} "
                    f"tools={[n for n, _ in tcs]}" + (f" ADVISOR={verdict}" if verdict else "")
                )
                for name, body in fails:
                    md.append(f"  - TOOL RESULT FAIL `{name}`: {body!r}")
                if verdict and not verdict.startswith("CLEAN"):
                    md.append(f"  - ADVISOR BODY: {content[:1500]!r}")
                hits = sentences_with_markers(reasoning)
                if hits:
                    md.append("  - REASONING MARKERS:")
                    md.extend(f"    - {h!r}" for h in hits[:12])
                if c["kind"] != "success":
                    md.append(f"  - PROVIDER FAILURE {c.get('exception')}")
    md.append("\n## Totals\n")
    md.append("```json\n" + json.dumps({k: dict(v) for k, v in data.items() if k != "scenarios"}, indent=1) + "\n```")
    Path(sys.argv[2]).write_text("\n".join(md), encoding="utf-8")
    Path(sys.argv[3]).write_text(json.dumps({k: dict(v) for k, v in data.items() if k != "scenarios"}, indent=1))
    print(json.dumps({k: dict(v) for k, v in data.items() if k != "scenarios"}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
