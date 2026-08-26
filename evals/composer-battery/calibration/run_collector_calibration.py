"""Collector-authoring calibration via the FREEFORM composer battery.

The ADR-031 collector scenario measures how much provider work collector
authoring costs. That measurement does not need a browser: the guided
Playwright walk spent five firings answering wizard turn types (select,
schema form) that have nothing to do with the thing being measured, at
~15 minutes and real provider spend per firing. This runner drives the
same canonical prompt through the freeform planner using the real
Battery.run_prompt path, and grades the collector shape plus the
efficiency numbers the spec's COLLECTOR_BASELINE needs.

Repeats are the point: a baseline set from one sample is a coin flip, so
the recorded ceiling is the observed maximum across runs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "/home/john/elspeth/evals/composer-battery")
import drive_battery as db

ROUND = "2026-08-26-collector-calibration-freeform"
RUNS_DIR = Path("/home/john/elspeth/evals/composer-battery/runs")
BASE = "unix:///run/elspeth/uvicorn.sock"
REPEATS = 3

# Byte-identical to COLLECTOR_SCENARIO_PROMPT in
# tests/e2e/tutorial-reliability.staging.spec.ts. On the freeform surface this
# prompt does NOT author: the planner has no authoring-time URL fetch, and
# correctly refuses to guess field names it has not seen (measured 0/3, 3
# provider calls each, with the collector design named in prose). Kept as a
# graded case so that refusal stays pinned rather than rediscovered.
URL_PROMPT = (
    "Read this synthetic multi-document JSON file, split each document into one "
    "row per section, have an LLM write a one-sentence gist of each section, "
    "then gather each document's section rows back together into a single "
    "batch per document (every section must make it back — fail the document "
    "if one is lost) and write one summary row per document to a JSON file.\n"
    "https://dta-au.github.io/elspeth/tutorial-site/multi-doc-sections.json"
)

# The same scenario in the corpus.md register (operator voice, task never
# implementation, INVENTED data with named fields, explicit output). This is
# the case the freeform baseline is set from: it asks for the identical
# collector shape without depending on bytes the planner cannot read.
CORPUS_PROMPT = (
    "Make up a few documents in JSON, each with a document id, a title and a "
    "list of sections of text. Split every document into one row per section, "
    "have the LLM write a one-sentence gist of each section, then gather each "
    "document's section rows back together into a single batch per document — "
    "a document only counts if every one of its sections made it back. Write "
    "one summary row per document out as JSON."
)

CASES = (
    ("collector-authoring-corpus-register", CORPUS_PROMPT, True),
    ("collector-authoring-url-prompt", URL_PROMPT, False),
)


def grade(run_dir: Path) -> dict:
    state = json.loads((run_dir / "state.json").read_text()) if (run_dir / "state.json").exists() else {}
    nodes = (state or {}).get("nodes") or []
    collectors = [n for n in nodes if isinstance(n, dict) and n.get("node_type") == "collector"]
    complete = [n for n in collectors if n.get("scope_name") and n.get("scope_opener") and n.get("scope_policy")]
    validate = json.loads((run_dir / "validate.json").read_text()) if (run_dir / "validate.json").exists() else {}
    messages = json.loads((run_dir / "messages.json").read_text()) if (run_dir / "messages.json").exists() else []

    provider_calls = 0
    repair_turns = 0
    attempt_phases: list[str] = []
    tool_names: list[str] = []
    rejection_codes: list[str] = []
    for m in messages:
        role = m.get("role")
        if role == "audit":
            try:
                body = json.loads(m.get("content") or "{}")
            except json.JSONDecodeError:
                body = {}
            kind = body.get("_kind")
            if kind == "llm_call_audit":
                provider_calls += 1
            elif kind == "planner_attempt_audit":
                # phase is TOP-LEVEL on this envelope (verified against
                # persisted rows) — reading it under an "attempt" key silently
                # yields zero repairs forever, which reads as a clean run.
                phase = body.get("phase")
                if isinstance(phase, str):
                    attempt_phases.append(phase)
                    if phase == "repair":
                        repair_turns += 1
        elif role == "tool":
            for source in (m.get("raw_content"), m.get("content")):
                if not source:
                    continue
                try:
                    body = json.loads(source)
                except json.JSONDecodeError:
                    continue
                validation = body.get("validation") if isinstance(body, dict) else None
                if isinstance(validation, dict):
                    for err in validation.get("errors") or []:
                        code = err.get("error_code") if isinstance(err, dict) else None
                        if code and not code.startswith("<redacted"):
                            rejection_codes.append(code)
                break
        elif role == "assistant":
            for tc in m.get("tool_calls") or []:
                name = tc.get("name") or (tc.get("function") or {}).get("name")
                if name:
                    tool_names.append(name)

    return {
        "collector_nodes": [n.get("id") for n in collectors],
        "complete_scope_bindings": [{k: n.get(k) for k in ("id", "scope_name", "scope_opener", "scope_policy")} for n in complete],
        "authored_scoped_collector": len(collectors) > 0 and len(collectors) == len(complete),
        "is_valid": (validate or {}).get("is_valid"),
        "node_kinds": sorted({n.get("node_type") for n in nodes if isinstance(n, dict) and n.get("node_type")}),
        "provider_calls": provider_calls,
        "repair_turns": repair_turns,
        # Empty when the run recorded no planner attempts at all: that makes
        # "0 repairs" distinguishable from "never looked" (the S1-S5 rounds
        # captured llm_call_audit rows but no attempt rows).
        "attempt_phases": attempt_phases,
        "tool_names": tool_names,
        "rejection_codes": rejection_codes,
    }


def main() -> int:
    client = db.build_client(BASE)
    env_budgets = db.read_env_budgets(Path("/home/john/elspeth/.env"))
    b = db.Battery(
        client,
        base=BASE,
        round_name=ROUND,
        runs_dir=RUNS_DIR,
        corpus_version=0,
        env_budgets=env_budgets,
        repeats=REPEATS,
    )
    user, pw = db._load_credentials(Path.home() / ".elspeth-battery")
    b.login(user, pw)
    print(f"logged in as {user} via {BASE}", flush=True)

    runs = []
    for case, prompt, expects_collector in CASES:
        # The URL case is a single pinned observation, not a baseline sample:
        # repeating a known refusal three times buys nothing but spend.
        repeats = REPEATS if expects_collector else 1
        for repeat in range(1, repeats + 1):
            run_dir = RUNS_DIR / ROUND / case / f"run-{repeat:02d}"
            print(f"=== {case} repeat {repeat}/{repeats} ===", flush=True)
            excluded = b.run_prompt(
                label=f"battery/{ROUND}/{case}/{repeat}",
                prompt=prompt,
                run_dir=run_dir,
                case=case,
                repeat=repeat,
            )
            rec = grade(run_dir)
            rec["case"] = case
            rec["expects_collector"] = expects_collector
            rec["repeat"] = repeat
            rec["excluded"] = excluded
            rec["shape_pass"] = rec["authored_scoped_collector"] == expects_collector
            runs.append(rec)
            print(json.dumps(rec, indent=2), flush=True)

    baseline_runs = [r for r in runs if r["expects_collector"] and not r["excluded"]]
    graded = baseline_runs
    authored = [r for r in graded if r["authored_scoped_collector"]]
    report = {
        "round": ROUND,
        "prompts": {"corpus_register": CORPUS_PROMPT, "url": URL_PROMPT},
        "runs": runs,
        # The ceiling is the observed MAXIMUM across clean runs, not a mean:
        # the scenario grades "no worse than calibrated", so the baseline must
        # cover ordinary model variance or it fires on noise.
        "proposed_baseline": {
            "max_provider_calls": max((r["provider_calls"] for r in graded), default=None),
            # Only a real number when attempt rows were actually observed;
            # otherwise null, so the spec keeps recording rather than grading
            # a metric this harness never measured.
            "max_repair_turns": (
                max((r["repair_turns"] for r in graded), default=None) if any(r["attempt_phases"] for r in graded) else None
            ),
        },
        "repair_signal_observed": any(r["attempt_phases"] for r in graded),
        "authored_scoped_collector": f"{len(authored)}/{len(graded)} clean runs",
    }
    out = RUNS_DIR / ROUND / "collector-calibration-report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"report: {out}", flush=True)
    print(json.dumps(report["proposed_baseline"], indent=2), flush=True)
    print(f"AUTHORED SCOPED COLLECTOR: {report['authored_scoped_collector']}", flush=True)
    failures = [f"{r['case']}#{r['repeat']}" for r in runs if not r["shape_pass"] and not r["excluded"]]
    print(f"SHAPE: {'PASS' if not failures else 'FAIL ' + ','.join(failures)}", flush=True)
    return 0 if graded and not failures else 1


if __name__ == "__main__":
    sys.exit(main())
