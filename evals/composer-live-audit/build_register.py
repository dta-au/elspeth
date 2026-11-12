"""Build the composer tool-call register from live registries + live battery captures.

Audit harness (not product code). Every column comes from an imported live object or a
captured provider request/response; prose mention counts use a word-boundary matcher
controlled against a known positive and a known negative before use.

usage: build_register.py <out.json> [capture.jsonl ...]

Run from the repository root with the project venv. See README.md in this directory.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from elspeth.web.composer import tools as T
from elspeth.web.composer.pipeline_planner import planner_terminal_tool_definition, planner_tool_definitions
from elspeth.web.composer.prompts import render_system_prompt
from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions
from elspeth.web.composer.recovery_controls import recovery_tool_definitions
from elspeth.web.composer.redaction import MANIFEST
from elspeth.web.composer.strict_transport import ToolContractDialect
from elspeth.web.composer.tools import _registry as R

D = ToolContractDialect.OPENAI_STRICT


def fn(d: dict) -> dict:
    return d.get("function", d)


def by_name(defs: list[dict]) -> dict[str, dict]:
    return {fn(d)["name"]: fn(d) for d in defs}


loop = by_name(composer_loop_tool_definitions(D))
planner = by_name(planner_tool_definitions(dialect=D))
hatch = by_name([*recovery_tool_definitions(D), planner_terminal_tool_definition(dialect=D)])
registry = by_name(T.get_tool_definitions())
names = sorted(set(loop) | set(planner) | set(hatch) | set(registry))

skill = render_system_prompt(None)


def mentions(text: str, name: str) -> int:
    return len(re.findall(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", text))


assert mentions("call `set_pipeline` now", "set_pipeline") == 1  # known positive
assert mentions("build_set_pipeline_candidate", "set_pipeline") == 0  # known negative


def kind(n: str) -> str:
    if n in ("emit_pipeline_proposal",):
        return "planner-terminal"
    if n in ("handoff_to_user", "restore_parked_graph"):
        return "recovery-control"
    if n == "request_advisor_hint":
        return "advisor"
    if T.is_session_aware_tool(n):
        return "session-aware"
    if T.is_discovery_tool(n):
        return "discovery"
    if T.is_mutation_tool(n):
        return "mutation"
    return "other"


def args(d: dict | None) -> tuple[list[str], list[str]]:
    if not d:
        return [], []
    p = d.get("parameters") or {}
    props = list((p.get("properties") or {}).keys())
    return props, list(p.get("required") or [])


rows = []
for n in names:
    d = loop.get(n) or planner.get(n) or hatch.get(n) or registry.get(n)
    # required/optional from the non-strict registry definition where one exists (strict wire marks every key required)
    props, req = args(registry.get(n) or d)
    if not registry.get(n) and (d or {}).get("strict"):
        req = [a for a in req if "null" not in json.dumps(((d.get("parameters") or {}).get("properties") or {}).get(a, {}).get("type", ""))]
    desc = (d or {}).get("description") or ""
    man = MANIFEST.get(n)
    rows.append(
        {
            "tool": n,
            "kind": kind(n),
            "on_tool_loop": n in loop,
            "on_planner": n in planner,
            "on_escape_hatch": n in hatch,
            "strict": bool((loop.get(n) or planner.get(n) or hatch.get(n) or {}).get("strict")),
            "args": props,
            "required": req,
            "cacheable_discovery": n in R._CACHEABLE_DISCOVERY_TOOL_NAMES,
            "approval_required_blob_mutation": T.is_approval_required_blob_store_only_mutation_tool(n),
            "redaction_mode": type(man).__name__ if man is not None else None,
            "description_chars": len(desc),
            "description_first_sentence": desc.split(". ")[0][:220],
            "skill_mentions": mentions(skill, n),
        }
    )

# ---- live captures -------------------------------------------------------------------
live: dict[str, Counter] = defaultdict(Counter)
results: dict[str, Counter] = defaultdict(Counter)
unoffered_calls: list[dict] = []
error_samples: dict[str, list[str]] = defaultdict(list)
surface_of = lambda tool_names: (  # noqa: E731
    "planner"
    if "emit_pipeline_proposal" in tool_names and "set_pipeline" not in tool_names
    else "tool_loop"
    if "set_pipeline" in tool_names
    else "escape_hatch"
    if tool_names
    else "advisor/no-tools"
)
for cap in sys.argv[2:]:
    for line in Path(cap).read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        surf = surface_of(rec["tool_names"])
        # tool results the model received in THIS request: map tool_call_id -> name from prior assistant turns
        id2name: dict[str, str] = {}
        for m in rec["messages"] or []:
            for tc in m.get("tool_calls") or []:
                id2name[tc.get("id")] = tc["function"]["name"]
        resp = rec.get("response") or {}
        msg = ((resp.get("choices") or [{}])[0]).get("message") or {}
        for tc in msg.get("tool_calls") or []:
            name = tc["function"]["name"]
            live[name][surf] += 1
            if name not in rec["tool_names"]:
                unoffered_calls.append({"tool": name, "surface": surf, "seq": rec["seq"], "capture": cap})
        # only count each tool result once: the LAST message list containing it is the longest; count results of the
        # most recent tool messages (those after the last assistant message) to avoid recounting history
        msgs = rec["messages"] or []
        last_asst = max((i for i, m in enumerate(msgs) if m.get("role") == "assistant"), default=-1)
        for m in msgs[last_asst + 1 :]:
            if m.get("role") != "tool":
                continue
            name = id2name.get(m.get("tool_call_id"), "?")
            body = m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content"))
            ok = None
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    ok = parsed.get("success")
                    if ok is None and "error" in parsed:
                        ok = False
            except (ValueError, TypeError):
                pass
            results[name]["ok" if ok is True else "fail" if ok is False else "unparsed"] += 1
            if ok is False and len(error_samples[name]) < 4:
                error_samples[name].append(body[:700])

for r in rows:
    r["live_calls"] = dict(live.get(r["tool"], {}))
    r["live_results"] = dict(results.get(r["tool"], {}))
    r["live_error_samples"] = error_samples.get(r["tool"], [])

extra_live = sorted(set(live) - set(names))
out = {
    "counts": {"tool_loop": len(loop), "planner": len(planner), "escape_hatch": len(hatch), "registry": len(registry)},
    "skill_chars": len(skill),
    "rows": rows,
    "taught_not_offered_planner": {r["tool"]: r["skill_mentions"] for r in rows if r["skill_mentions"] and not r["on_planner"]},
    "taught_not_offered_loop": {r["tool"]: r["skill_mentions"] for r in rows if r["skill_mentions"] and not r["on_tool_loop"]},
    "offered_not_taught_planner": [r["tool"] for r in rows if r["on_planner"] and not r["skill_mentions"]],
    "offered_not_taught_loop": [r["tool"] for r in rows if r["on_tool_loop"] and not r["skill_mentions"]],
    "live_calls_to_unoffered_tools": unoffered_calls,
    "live_tool_names_not_in_any_registry": extra_live,
}
Path(sys.argv[1]).write_text(json.dumps(out, indent=1))
print(json.dumps({k: v for k, v in out.items() if k != "rows"}, indent=1)[:4000])
