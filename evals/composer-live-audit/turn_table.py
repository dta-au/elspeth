"""Turn table and performance summary for live-audit session directories.

Audit harness (not product code). Deterministic labels taken from the API terminal and the
backend's own notices, never from keyword matching on model prose:

- FAILED          operation status was not ``completed`` (error type shown)
- WITHHELD        the reply body was replaced by the saved-scope receipt
                  (starts with "Verified checkpoint evidence covers")
- GATE-NOT-CLEARED the reply carries the completion-advisory "did not clear" notice
- PROPOSAL        a planner turn that called ``emit_pipeline_proposal`` successfully staged/committed
- REPLY           any other completed turn

Turn surface is the surface that actually ran: ``planner`` if any request offered the planner
palette (including the escape hatch on the advisor model), else ``tool_loop`` if any request offered
``set_pipeline``, else ``none``. Percentiles use the nearest-rank method.

usage: turn_table.py <out.md> <out.json> <sessions dir> [<sessions dir> ...]
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path


def nearest_rank(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)]


def surface_of_request(tool_names: list[str]) -> str:
    offered = set(tool_names)
    if "emit_pipeline_proposal" in offered and "set_pipeline" not in offered:
        return "planner"
    if "set_pipeline" in offered:
        return "tool_loop"
    if offered:
        return "reply_stage"
    return "advisor"


def main() -> int:
    out_md, out_json, *roots = sys.argv[1:]
    rows = []
    for root in roots:
        for summary in sorted(Path(root).glob("*/summary.json")):
            s = json.loads(summary.read_text())
            if s["scenario"].startswith("smoke"):
                continue  # harness smoke test, not part of the battery
            for t in s["turns"]:
                tdir = summary.parent / f"turn{t['turn']:02d}"
                calls_path = tdir / "llm_calls.json"
                calls = json.loads(calls_path.read_text()) if calls_path.exists() else []
                surfaces = [surface_of_request(c["tool_names"]) for c in calls]
                turn_surface = "planner" if "planner" in surfaces else "tool_loop" if "tool_loop" in surfaces else "none"
                reply = t["assistant_reply"] or ""
                emitted = False
                for c in calls:
                    message = (((c.get("response") or {}).get("choices") or [{}])[0]).get("message") or {}
                    if any(tc["function"]["name"] == "emit_pipeline_proposal" for tc in message.get("tool_calls") or []):
                        emitted = True
                if t["operation_status"] != "completed":
                    err = t.get("error") or {}
                    label = "FAILED: " + str(err.get("error_type") or err.get("failure_code") or t["operation_status"])
                elif reply.startswith("Verified checkpoint evidence covers"):
                    label = "WITHHELD"
                elif "did not clear" in reply:
                    label = "GATE-NOT-CLEARED"
                elif turn_surface == "planner" and emitted:
                    label = "PROPOSAL"
                else:
                    label = "REPLY"
                usage = [((c.get("response") or {}).get("usage") or {}) for c in calls]
                rows.append(
                    {
                        "root": root,
                        "scenario": s["scenario"],
                        "turn": t["turn"],
                        "user": t["user"],
                        "surface": turn_surface,
                        "seconds": t["compose_seconds"],
                        "provider_calls": len(calls),
                        "advisor_calls": surfaces.count("advisor"),
                        "prompt_tokens": sum(u.get("prompt_tokens") or 0 for u in usage),
                        "cached_tokens": sum(((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0) for u in usage),
                        "valid_after_driver_accepts": t["validation_is_valid"],
                        "label": label,
                        "reply": reply,
                        "checkpoint_receipt": "Checkpoint save status:" in reply,
                    }
                )
    stats: dict[str, dict] = {}
    for surf in ("planner", "tool_loop"):
        xs = [r for r in rows if r["surface"] == surf]
        if not xs:
            continue
        secs = [r["seconds"] for r in xs]
        calls = [r["provider_calls"] for r in xs]
        stats[surf] = {
            "turns": len(xs),
            "seconds_p50": nearest_rank(secs, 0.5),
            "seconds_p90": nearest_rank(secs, 0.9),
            "seconds_max": max(secs),
            "calls_p50": nearest_rank(calls, 0.5),
            "calls_max": max(calls),
            "turns_with_advisor_call": sum(1 for r in xs if r["advisor_calls"]),
            "checkpoint_receipt": sum(1 for r in xs if r["checkpoint_receipt"]),
            "cache_hit": round(sum(r["cached_tokens"] for r in xs) / max(1, sum(r["prompt_tokens"] for r in xs)), 3),
        }
    labels: dict[str, int] = {}
    for r in rows:
        labels[r["label"]] = labels.get(r["label"], 0) + 1
    summary = {"turns": len(rows), "sessions": len({(r["root"], r["scenario"]) for r in rows}), "labels": labels, "by_surface": stats}
    Path(out_json).write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))

    def clean(x: str) -> str:
        return x.replace("|", "\\|").replace("\n", " ")

    md = [
        "# Live battery turn table\n",
        "Generated by `evals/composer-live-audit/turn_table.py` (labels from API terminals and backend notices; "
        "surface = the surface that actually ran; nearest-rank percentiles). `valid` is `POST /validate` on the head "
        "state AFTER the driver auto-accepted pending review cards and proposals.\n",
        "```json\n" + json.dumps(summary, indent=1) + "\n```\n",
        "| Scenario | Turn | Surface | User | s | Calls | Valid | Label | Reply (first 140 chars) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        md.append(
            f"| {r['scenario']} | {r['turn']} | {r['surface']} | {clean(r['user'][:70])} | {r['seconds']} | "
            f"{r['provider_calls']} | {r['valid_after_driver_accepts']} | {r['label']} | {clean(r['reply'][:140])} |"
        )
    Path(out_md).write_text("\n".join(md) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
