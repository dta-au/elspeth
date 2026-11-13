"""S2 per-arm analysis of driven single-turn sessions.

usage: python analyse.py <out.json> <arm>=<sessions dir> [...] [--corpus <dir with */turnNN/llm_calls.json>]

Per turn: label (turn_table.py rules), seconds, provider calls, prompt tokens, cost (OpenRouter usage.cost),
emit calls, emit rejections (tool result for that tool_call_id with success false, read from any later request),
staged interpretation_requirements in the LAST emitted proposal (kind:user_term per node),
review cards the driver resolved after acceptance (kinds), strict palette-confusion calls.

Strict palette-confusion instrument (per sentence of reasoning_content):
  names one of the planner's unoffered tools (word boundary)  AND  a negation  AND  an availability word.
Controlled below (known positives / negatives) before use; aborts if a control fails.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

UNOFFERED = [
    "clear_source",
    "create_blob",
    "delete_blob",
    "patch_node_options",
    "patch_output_options",
    "patch_source_options",
    "remove_edge",
    "remove_node",
    "remove_output",
    "request_advisor_hint",
    "request_interpretation_review",
    "set_metadata",
    "set_output",
    "set_pipeline",
    "set_source",
    "set_source_from_blob",
    "set_source_from_blobs",
    "splice_transform",
    "update_blob",
    "upsert_edge",
    "upsert_node",
    "wire_blob_inline_ref",
    "wire_secret_ref",
]
TOOL_RE = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(sorted(map(re.escape, UNOFFERED), key=len, reverse=True)) + r")(?![A-Za-z0-9_])")
NEG_RE = re.compile(r"\b(no|not|without|lack|lacks|cannot|never|none)\b|n't\b", re.I)
AVAIL_RE = re.compile(r"\b(available|unavailable|offered|advertised|palette|have|has|exist|exists|access|call)\b", re.I)
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

POSITIVE_CONTROLS = [
    "On proposal-only surface, there is no request_interpretation_review tool advertised.",
    "I don't have set_source_from_blobs in this palette, so I cannot bind both.",
    "That tool isn't available: create_blob is not on my palette.",
]
NEGATIVE_CONTROLS = [
    "The csv source is available in the digest and list_blobs returned two blobs.",
    "I will emit the proposal now with the set_pipeline object shape.",
    "The set_pipeline document shape is available to me through the terminal.",
    "No proposal-only surface tool is missing here.",
]


def strict_hits(text: str) -> list[str]:
    return [s.strip() for s in SENT_SPLIT.split(text or "") if TOOL_RE.search(s) and NEG_RE.search(s) and AVAIL_RE.search(s)]


def check_controls() -> dict:
    res = {"positives": [bool(strict_hits(s)) for s in POSITIVE_CONTROLS], "negatives": [bool(strict_hits(s)) for s in NEGATIVE_CONTROLS]}
    assert all(res["positives"]) and not any(res["negatives"]), res
    return res


def msg_of(call):
    return (((call.get("response") or {}).get("choices") or [{}])[0]).get("message") or {}


def label_of(t, calls):
    surfaces = []
    for c in calls:
        names = set(c["tool_names"])
        surfaces.append(
            "planner"
            if "emit_pipeline_proposal" in names and "set_pipeline" not in names
            else "tool_loop"
            if "set_pipeline" in names
            else "other"
        )
    surface = "planner" if "planner" in surfaces else "tool_loop" if "tool_loop" in surfaces else "none"
    reply = t["assistant_reply"] or ""
    emitted = any(tc["function"]["name"] == "emit_pipeline_proposal" for c in calls for tc in msg_of(c).get("tool_calls") or [])
    if t["operation_status"] != "completed":
        err = t.get("error") or {}
        body = ((err.get("body") or {}).get("detail") or {}) if isinstance(err.get("body"), dict) else {}
        return surface, "FAILED: " + str(body.get("failure_code") or err.get("error_type") or t["operation_status"])
    if reply.startswith("Verified checkpoint evidence covers"):
        return surface, "WITHHELD"
    if "did not clear" in reply:
        return surface, "GATE-NOT-CLEARED"
    if surface == "planner" and emitted:
        return surface, "PROPOSAL"
    return surface, "REPLY"


def staged_requirements(args: dict) -> list[str]:
    out = []
    pipe = args.get("pipeline") or {}
    src = pipe.get("source") or {}
    for r in (src.get("options") or {}).get("interpretation_requirements") or []:
        out.append(f"source:{r.get('kind')}:{r.get('user_term')}")
    for name, s in (pipe.get("sources") or {}).items():
        for r in (s.get("options") or {}).get("interpretation_requirements") or []:
            out.append(f"source:{name}:{r.get('kind')}:{r.get('user_term')}")
    for n in pipe.get("nodes") or []:
        for r in (n.get("options") or {}).get("interpretation_requirements") or []:
            out.append(f"{n.get('node_type')}:{n.get('plugin')}:{r.get('kind')}:{r.get('user_term')}")
    return sorted(out)


def analyse_turn(tdir: Path, t: dict) -> dict:
    calls = json.loads((tdir / "llm_calls.json").read_text())
    surface, label = label_of(t, calls)
    tool_results = {}
    for c in calls:
        for m in c["messages"] or []:
            if m.get("role") == "tool" and m.get("tool_call_id"):
                tool_results[m["tool_call_id"]] = m.get("content")
    emits, rejections, last_args = 0, [], None
    confusion_calls, confusion_sentences = 0, []
    for c in calls:
        msg = msg_of(c)
        hits = strict_hits(msg.get("reasoning_content") or msg.get("reasoning") or "")
        if hits:
            confusion_calls += 1
            confusion_sentences += hits
        for tc in msg.get("tool_calls") or []:
            if tc["function"]["name"] != "emit_pipeline_proposal":
                continue
            emits += 1
            try:
                last_args = json.loads(tc["function"]["arguments"])
            except ValueError:
                last_args = None
            res = tool_results.get(tc["id"])
            if res is not None:
                body = json.loads(res) if isinstance(res, str) else res
                if isinstance(body, dict) and body.get("success") is False:
                    codes = [e.get("error_code") for e in ((body.get("validation") or {}).get("errors") or [])]
                    rejections.append(codes or [body.get("error") or "rejected"])
    actions = json.loads((tdir / "post_turn_actions.json").read_text()) if (tdir / "post_turn_actions.json").exists() else []
    usage = [((c.get("response") or {}).get("usage") or {}) for c in calls]
    return {
        "surface": surface,
        "label": label,
        "seconds": t["compose_seconds"],
        "provider_calls": len(calls),
        "failed_provider_calls": sum(1 for c in calls if c["kind"] != "success"),
        "models": sorted({(c["model"] or "?").split("/")[-1] for c in calls}),
        "prompt_tokens": sum(u.get("prompt_tokens") or 0 for u in usage),
        "first_call_prompt_tokens": (usage[0].get("prompt_tokens") if usage else None),
        "completion_tokens": sum(u.get("completion_tokens") or 0 for u in usage),
        "cost_usd": round(sum(u.get("cost") or 0 for u in usage), 6),
        "emit_calls": emits,
        "emit_rejections": rejections,
        "staged_requirements": staged_requirements(last_args) if isinstance(last_args, dict) else None,
        "review_cards_resolved": sorted(a.get("kind") for a in actions if a.get("action") == "resolve_review"),
        "proposal_accept_status": [a.get("status") for a in actions if a.get("action") == "accept_proposal"],
        "valid_after_accepts": t["validation_is_valid"],
        "strict_confusion_calls": confusion_calls,
        "strict_confusion_sentences": confusion_sentences,
        "reply": t["assistant_reply"],
    }


def main() -> int:
    out = Path(sys.argv[1])
    controls = check_controls()
    args = sys.argv[2:]
    corpus = None
    if "--corpus" in args:
        i = args.index("--corpus")
        corpus = Path(args[i + 1])
        args = args[:i] + args[i + 2 :]
    result = {"controls": controls, "arms": {}}
    for spec in args:
        arm, root = spec.split("=", 1)
        rows = []
        for summary in sorted(Path(root).glob("*/summary.json")):
            s = json.loads(summary.read_text())
            for t in s["turns"]:
                tdir = summary.parent / f"turn{t['turn']:02d}"
                row = {"run": summary.parent.name, "turn": t["turn"]} | analyse_turn(tdir, t)
                rows.append(row)
        result["arms"][arm] = rows
    if corpus is not None:
        # Calibration on the audit's original live corpus: planner-palette calls only.
        n = hits = 0
        for f in sorted(corpus.glob("*/turn*/llm_calls.json")):
            for c in json.loads(f.read_text()):
                names = set(c["tool_names"])
                if "emit_pipeline_proposal" in names and "set_pipeline" not in names and c["kind"] == "success":
                    n += 1
                    msg = msg_of(c)
                    hits += bool(strict_hits(msg.get("reasoning_content") or msg.get("reasoning") or ""))
        result["corpus_calibration"] = {"corpus": str(corpus), "planner_calls": n, "strict_hit_calls": hits}
    out.write_text(json.dumps(result, indent=1))
    for arm, rows in result["arms"].items():
        print(f"== {arm}")
        for r in rows:
            print(
                f"{r['run']:<28} {r['label']:<32} s={r['seconds']:<6} calls={r['provider_calls']} pt={r['prompt_tokens']:<7} "
                f"pt1={r['first_call_prompt_tokens']} ct={r['completion_tokens']:<6} ${r['cost_usd']:<9} emits={r['emit_calls']} "
                f"rej={r['emit_rejections']} conf={r['strict_confusion_calls']} cards={r['review_cards_resolved']} "
                f"staged={r['staged_requirements']} valid={r['valid_after_accepts']}"
            )
    if corpus is not None:
        print(result["corpus_calibration"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
