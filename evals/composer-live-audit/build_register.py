"""Build the composer tool-call register from live registries + live battery captures.

Audit harness (not product code). Every static column comes from an imported live object;
live columns come from captured provider requests/responses. Prose mention counts use a
word-boundary matcher controlled against a known positive and a known negative before use.

Surfaces (as sent in production):
- tool_loop    composer model, ``provider_gateway.composer_loop_tool_definitions`` (registry tools
               plus the two recovery controls)
- planner      composer model, ``pipeline_planner.planner_tool_definitions`` (discovery palette,
               recovery controls and the ``emit_pipeline_proposal`` terminal)
- planner_hatch  the planner's escape-hatch turn: the SAME planner palette, sent to the ADVISOR
               model (``pipeline_planner`` ~L4275 / ``planning_application`` ~L1152)
- reply_stage  composer model after review cards are staged: only the recovery controls
               (``recovery_controls.recovery_tool_definitions``)
- advisor      advisor model, no tools

usage: build_register.py <out.json> [capture.jsonl ...]

Run from the repository root with the project venv. The advisor model is read from
``ELSPETH_WEB__COMPOSER_ADVISOR_MODEL`` (provider prefix ignored) to tell hatch calls apart.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from elspeth.web.composer import tools as T
from elspeth.web.composer.pipeline_planner import planner_tool_definitions
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
reply_stage = by_name(recovery_tool_definitions(D))
registry = by_name(T.get_tool_definitions())
names = sorted(set(loop) | set(planner) | set(reply_stage) | set(registry))
LOOP_SET, PLANNER_SET, REPLY_SET = frozenset(loop), frozenset(planner), frozenset(reply_stage)

skill = render_system_prompt(None)


def mentions(text: str, name: str) -> int:
    return len(re.findall(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", text))


assert mentions("call `set_pipeline` now", "set_pipeline") == 1  # known positive
assert mentions("build_set_pipeline_candidate", "set_pipeline") == 0  # known negative


def kind(n: str) -> str:
    if n == "emit_pipeline_proposal":
        return "planner-terminal"
    if n in REPLY_SET:
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
    return list((p.get("properties") or {}).keys()), list(p.get("required") or [])


rows = []
for n in names:
    d = loop.get(n) or planner.get(n) or reply_stage.get(n) or registry.get(n)
    # Required/optional come from the non-strict registry definition where one exists
    # (the strict wire marks every key required). For tools outside the registry, a strict
    # key whose type admits null is optional.
    props, req = args(registry.get(n) or d)
    if n not in registry and (d or {}).get("strict"):
        properties = (d.get("parameters") or {}).get("properties") or {}
        req = [a for a in req if "null" not in json.dumps(properties.get(a, {}).get("type", ""))]
    desc = (d or {}).get("description") or ""
    man = MANIFEST.get(n)
    rows.append(
        {
            "tool": n,
            "kind": kind(n),
            "on_tool_loop": n in loop,
            "on_planner": n in planner,
            "on_reply_stage": n in reply_stage,
            "strict": bool((loop.get(n) or planner.get(n) or reply_stage.get(n) or {}).get("strict")),
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
ADVISOR_MODEL = os.environ.get("ELSPETH_WEB__COMPOSER_ADVISOR_MODEL", "").split("/", 1)[-1]


def surface_of(model: str | None, tool_names: list[str]) -> str:
    offered = frozenset(t for t in tool_names if t)
    if not offered:
        return "advisor"
    if offered == REPLY_SET:
        return "reply_stage"
    if "emit_pipeline_proposal" in offered and "set_pipeline" not in offered:
        if ADVISOR_MODEL and model and model.endswith(ADVISOR_MODEL):
            return "planner_hatch"
        return "planner"
    if "set_pipeline" in offered:
        return "tool_loop"
    return "other"


live: dict[str, Counter] = defaultdict(Counter)
results_by_id: dict[str, tuple[str, str]] = {}  # tool_call_id -> (tool name, ok|fail|unparsed); each result once
call_ids_by_tool: dict[str, set[str]] = defaultdict(set)
error_samples: dict[str, list[str]] = defaultdict(list)
unoffered_calls: list[dict] = []
n_requests = 0
for cap in sys.argv[2:]:
    for line in Path(cap).read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        n_requests += 1
        surf = surface_of(rec.get("model"), rec["tool_names"])
        msgs = rec["messages"] or []
        id2name: dict[str, str] = {}
        for m in msgs:
            for tc in m.get("tool_calls") or []:
                id2name[tc.get("id")] = tc["function"]["name"]
        for m in msgs:
            if m.get("role") != "tool" or m.get("tool_call_id") in results_by_id:
                continue
            name = id2name.get(m.get("tool_call_id"), "?")
            body = m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content"))
            ok = "unparsed"
            try:
                parsed = json.loads(body)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                if parsed.get("success") is True:
                    ok = "ok"
                elif parsed.get("success") is False or "error" in parsed:
                    ok = "fail"
            results_by_id[m.get("tool_call_id")] = (name, ok)
            if ok == "fail" and len(error_samples[name]) < 4:
                error_samples[name].append(body[:700])
        resp = rec.get("response") or {}
        msg = ((resp.get("choices") or [{}])[0]).get("message") or {}
        for tc in msg.get("tool_calls") or []:
            name = tc["function"]["name"]
            live[name][surf] += 1
            call_ids_by_tool[name].add(tc.get("id"))
            if name not in rec["tool_names"]:
                unoffered_calls.append({"tool": name, "surface": surf, "seq": rec["seq"]})

results: dict[str, Counter] = defaultdict(Counter)
for _cid, (name, ok) in results_by_id.items():
    results[name][ok] += 1
for r in rows:
    r["live_calls"] = dict(live.get(r["tool"], {}))
    r["live_results"] = dict(results.get(r["tool"], {}))
    r["live_results_unseen"] = max(0, sum(r["live_calls"].values()) - sum(r["live_results"].values()))
    r["live_error_samples"] = error_samples.get(r["tool"], [])

out = {
    "counts": {
        "tool_loop": len(loop),
        "planner": len(planner),
        "planner_hatch": len(planner),
        "reply_stage": len(reply_stage),
        "registry": len(registry),
    },
    "captured_requests": n_requests,
    "skill_chars": len(skill),
    "rows": rows,
    "taught_not_offered_planner": {r["tool"]: r["skill_mentions"] for r in rows if r["skill_mentions"] and not r["on_planner"]},
    "taught_not_offered_loop": {r["tool"]: r["skill_mentions"] for r in rows if r["skill_mentions"] and not r["on_tool_loop"]},
    "offered_not_taught_planner": [r["tool"] for r in rows if r["on_planner"] and not r["skill_mentions"]],
    "offered_not_taught_loop": [r["tool"] for r in rows if r["on_tool_loop"] and not r["skill_mentions"]],
    "live_calls_to_unoffered_tools": unoffered_calls,
    "live_tool_names_not_in_any_registry": sorted(set(live) - set(names)),
}
Path(sys.argv[1]).write_text(json.dumps(out, indent=1))
print(json.dumps({k: v for k, v in out.items() if k != "rows"}, indent=1)[:3000])
