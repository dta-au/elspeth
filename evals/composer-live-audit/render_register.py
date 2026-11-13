"""Render the composer tool-call register JSON (build_register.py output) to markdown.

usage: render_register.py <register.json> <out.md>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

reg = json.loads(Path(sys.argv[1]).read_text())
rows = reg["rows"]


def yes(b: bool) -> str:
    return "✓" if b else "—"


KIND_ORDER = ["discovery", "mutation", "session-aware", "advisor", "planner-terminal", "recovery-control", "other"]
rows.sort(key=lambda r: (KIND_ORDER.index(r["kind"]) if r["kind"] in KIND_ORDER else 99, r["tool"]))

out: list[str] = []
c = reg["counts"]
out.append(
    f"Live tool counts: tool loop **{c['tool_loop']}**, planner **{c['planner']}**, escape-hatch turn **{c['planner_hatch']}** (the planner palette, sent to the advisor model), reply stage **{c['reply_stage']}**, "
    f"execute_tool registry **{c['registry']}**. Rendered skill: {reg['skill_chars']:,} characters.\n"
)
out.append("## Register\n")
out.append(
    "| Tool | Kind | Loop | Planner | Reply stage | Required args | Optional args | Skill mentions | Live calls (surface:n) | Results ok/fail (unseen)* |"
)
out.append("|---|---|---|---|---|---|---|---|---|---|")
for r in rows:
    opt = [a for a in r["args"] if a not in r["required"]]
    live = ", ".join(f"{k}:{v}" for k, v in sorted(r["live_calls"].items())) or "—"
    res = r["live_results"]
    resl = f"{res.get('ok', 0)}/{res.get('fail', 0)} ({r['live_results_unseen']})" if (res or r["live_results_unseen"]) else "—"
    flag = " ⚠" if (r["skill_mentions"] and not r["on_planner"] and r["kind"] not in ("planner-terminal",)) else ""
    out.append(
        f"| `{r['tool']}` | {r['kind']} | {yes(r['on_tool_loop'])} | {yes(r['on_planner'])}{flag} | {yes(r['on_reply_stage'])} | "
        f"{', '.join(f'`{a}`' for a in r['required']) or '—'} | {', '.join(f'`{a}`' for a in opt) or '—'} | "
        f"{r['skill_mentions']} | {live} | {resl} |"
    )
out.append(
    "\n*Each tool result is counted once (deduplicated by tool-call id). A result is visible only when a later provider request re-sends it, so results of a turn's final calls (notably `emit_pipeline_proposal`, `request_interpretation_review`) are often unseen; the bracketed number is calls whose result was never observed. Surfaces are classified from the offered tool set and the model (planner-palette calls on the advisor model are the escape hatch).\n\n⚠ = the tool is taught in the system prompt the planner receives, but the planner is not offered it "
    "(the planner is sent the full tool-loop skill).\n"
)
out.append("## Flags per tool\n")
out.append(
    "| Tool | Strict schema | Cacheable discovery | Approval-required blob mutation | Redaction policy | Description (first sentence) |"
)
out.append("|---|---|---|---|---|---|")
for r in rows:
    desc = r["description_first_sentence"].replace("|", "\\|")
    out.append(
        f"| `{r['tool']}` | {yes(r['strict'])} | {yes(r['cacheable_discovery'])} | {yes(r['approval_required_blob_mutation'])} | "
        f"{r['redaction_mode'] or '—'} | {desc} |"
    )
out.append("\n## Teaching mismatches\n")
out.append("Tools named in the planner's system prompt that the planner cannot call (name: mentions):\n")
for k, v in sorted(reg["taught_not_offered_planner"].items(), key=lambda kv: -kv[1]):
    out.append(f"- `{k}`: {v}")
out.append(
    f"\nTools offered to the planner but never named in its system prompt: {', '.join(f'`{t}`' for t in reg['offered_not_taught_planner']) or 'none'}"
)
out.append(f"\nTools named in the tool-loop system prompt that the loop cannot call: {', '.join(reg['taught_not_offered_loop']) or 'none'}")
out.append(f"\nTools offered to the tool loop but never named in its system prompt: {', '.join(reg['offered_not_taught_loop']) or 'none'}")
out.append(f"\nLive calls to a tool the surface did not offer: {len(reg['live_calls_to_unoffered_tools'])}")
out.append(f"\nLive tool names not in any registry: {', '.join(reg['live_tool_names_not_in_any_registry']) or 'none'}")
out.append("\n## Live failure samples\n")
for r in rows:
    if r["live_error_samples"]:
        out.append(f"\n### `{r['tool']}`\n")
        for s in r["live_error_samples"][:3]:
            out.append("```json\n" + s[:600] + ("…" if len(s) > 600 else "") + "\n```")
Path(sys.argv[2]).write_text("\n".join(out) + "\n", encoding="utf-8")
print(f"wrote {sys.argv[2]} ({len(rows)} tools)")
