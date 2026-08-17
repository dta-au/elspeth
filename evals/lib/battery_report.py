"""Round aggregation for the composer battery (spec §5). Offline over score.json."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping
from itertools import pairwise
from pathlib import Path
from typing import Any

from evals.lib.battery_scenario import Scenario
from evals.lib.battery_score import EXCLUSION_KINDS, MEASUREMENT_KINDS, SEVERITY, Score, score_from_disk, write_score

RESERVED_DIRS = frozenset({"_tripwire", "_probe"})
CAVEATS = [
    "compose-loop surface only (planner covered by the tripwire table and the §7 probe, never pooled)",
    "operator-voice register only (prompts that classify EXPLICIT_MUTATION are excluded by construction)",
    "compose+validate only — no execute",
    "per-case rates are indicative at N=5 (±~44 pp); claims rest on the pooled aggregate",
    "deviation classes and excess are different currencies: a class can fire at zero excess (cached discovery repeats cost no provider call) and excess can occur with no class (unattributed_excess) — the histogram does not 'explain' the excess",
    "advisor model and turn budgets in the binding identity are operator-asserted from deploy/elspeth-web.env (recorded env_file_sha256), not observed from the server",
]
FORMULA = "sum(successes)/sum(n)"


class CompareRefused(RuntimeError):
    pass


def ci_half_width_pp(n: int) -> int:
    return 0 if n <= 0 else round(196 * math.sqrt(0.25 / n))


def _run_dirs(case_dir: Path) -> list[Path]:
    dirs = [d for d in case_dir.iterdir() if d.is_dir() and d.name.isdigit()]
    return sorted(dirs, key=lambda d: int(d.name))


class LateBinding(ValueError):
    """A captured run does not belong to the corpus version / prompt being scored against."""


def _guard_late_binding(run_dir: Path, case: str, *, corpus_version: int | None, prompt_hashes: Mapping[str, str] | None) -> None:
    meta_p = run_dir / "meta.json"
    if not meta_p.exists():
        return  # score_from_disk will record `capture`
    try:
        meta = json.loads(meta_p.read_text())
    except ValueError:
        return
    if corpus_version is not None and meta.get("corpus_version") != corpus_version:
        raise LateBinding(
            f"{run_dir}: captured at corpus_version {meta.get('corpus_version')}, scoring against {corpus_version} — refuse; floors moved under history"
        )
    if prompt_hashes is not None and case in prompt_hashes and meta.get("prompt_sha256") != prompt_hashes[case]:
        raise LateBinding(f"{run_dir}: prompt_sha256 differs from the current corpus prompt for {case!r} — refuse")


def collect_scores(
    round_dir: Path, scenarios: Mapping[str, Scenario], *, corpus_version: int | None = None, prompt_hashes: Mapping[str, str] | None = None
) -> tuple[list[Score], list[Score]]:
    corpus: list[Score] = []
    canary: list[Score] = []
    for case_dir in sorted(p for p in Path(round_dir).iterdir() if p.is_dir()):
        if case_dir.name in RESERVED_DIRS:
            continue
        if case_dir.name not in scenarios:
            raise ValueError(f"{case_dir}: no scenario named {case_dir.name!r} — not a battery case")
        for run_dir in _run_dirs(case_dir):
            _guard_late_binding(run_dir, case_dir.name, corpus_version=corpus_version, prompt_hashes=prompt_hashes)
            score = score_from_disk(run_dir, scenarios[case_dir.name])
            write_score(run_dir, score)
            (canary if case_dir.name == "canary" else corpus).append(score)
    return corpus, canary


_PER_RUN_BINDING = (
    "model_returned",
    "tools_spec_hash",
    "temperature",
    "seed",
)  # null on runs with no tool-bearing row; first non-null wins, non-null values must agree
_NULLABLE_BINDING = frozenset({"temperature", "seed"})  # legitimately unset on the deployment; null==null is a match, null vs value is not


def _identity(round_dir: Path) -> tuple[dict[str, Any], list[str]]:
    """Round identity: firing-level binding fields must be identical across runs; per-run fields take the first
    non-null value and every later non-null value must agree. Drift ⇒ a degraded reason."""
    merged: dict[str, Any] | None = None
    drift: list[str] = []
    for meta_path in sorted(Path(round_dir).rglob("meta.json")):
        if RESERVED_DIRS & set(meta_path.parts):
            continue
        ident = json.loads(meta_path.read_text()).get("identity") or {}
        b = dict(ident.get("binding") or {})
        if merged is None:
            merged = {"binding": b, "recorded": dict(ident.get("recorded") or {})}
            continue
        mb = merged["binding"]
        for k in set(b) | set(mb):
            if k in _PER_RUN_BINDING:
                if mb.get(k) is None:
                    mb[k] = b.get(k)
                elif b.get(k) is not None and b.get(k) != mb.get(k):
                    drift.append("binding identity drifted within round")
            elif b.get(k) != mb.get(k):
                drift.append("binding identity drifted within round")
        drift = drift[:1]
    return merged or {"binding": {}, "recorded": {}}, drift


def _rates(scores: list[Score]) -> dict[str, Any]:
    inc = [s for s in scores if s.excluded is None]
    n = len(inc)
    excluded_instrument = sum(1 for s in scores if s.excluded is not None and s.excluded not in MEASUREMENT_KINDS)
    excluded_measurement = sum(1 for s in scores if s.excluded in MEASUREMENT_KINDS)
    clean = sum(1 for s in inc if s.clean)
    optimal = sum(1 for s in inc if s.optimal)
    hard = sum(1 for s in inc if any(SEVERITY[d.cls] == "hard" for d in s.deviations))
    clean_ex_transport = sum(1 for s in inc if s.green and s.is_valid and all(d.cls == "retried_provider_error" for d in s.deviations))
    unattributed = sum(1 for s in inc if any(d.cls == "unattributed_excess" for d in s.deviations))
    below_floor = sum(1 for s in inc if s.below_floor)
    retried_runs = sum(1 for s in inc if s.retried_calls > 0)
    return {
        "n": n,
        "excluded": len(scores) - n,
        "excluded_instrument": excluded_instrument,
        "excluded_measurement": excluded_measurement,
        "clean": clean,
        "optimal": optimal,
        "hard": hard,
        "clean_ex_transport": clean_ex_transport,
        "unattributed_excess": unattributed,
        "below_floor": below_floor,
        "runs_with_retried_provider_error": retried_runs,
        "clean_rate": (clean / n) if n else None,
        "optimal_rate": (optimal / n) if n else None,
        "hard_rate": (hard / n) if n else None,
        "formula": FORMULA,
        "ci_half_width_pp": ci_half_width_pp(n),
    }


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _floors_sha(scenarios: Mapping[str, Scenario], cases: set[str]) -> str:
    rows = sorted(
        (c, scenarios[c].floor.tool_bearing_calls, json.dumps(scenarios[c].option_assertions, sort_keys=True))
        for c in cases
        if c in scenarios
    )
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def _taxonomy_sha() -> str:
    return hashlib.sha256(json.dumps({"severity": SEVERITY, "exclusions": list(EXCLUSION_KINDS)}, sort_keys=True).encode()).hexdigest()


def build_report(
    round_dir: Path,
    *,
    scenarios: Mapping[str, Scenario],
    corpus_version: int,
    prompt_hashes: Mapping[str, str] | None = None,
    compare_to: Path | None = None,
    force_compare: bool = False,
) -> dict[str, Any]:
    round_dir = Path(round_dir)
    corpus, canary = collect_scores(round_dir, scenarios, corpus_version=corpus_version, prompt_hashes=prompt_hashes)
    identity, degraded_reasons = _identity(round_dir)
    identity.setdefault("binding", {})["floors_sha256"] = _floors_sha(scenarios, {s.case for s in corpus} | {s.case for s in canary})
    identity["binding"]["taxonomy_sha256"] = _taxonomy_sha()
    firing = json.loads((round_dir / "firing.json").read_text()) if (round_dir / "firing.json").exists() else {}
    tripwire_path = round_dir / "_tripwire" / "tripwire.json"
    tripwire = json.loads(tripwire_path.read_text()) if tripwire_path.exists() else []

    canary_inc = [s for s in canary if s.excluded is None]
    canary_non_optimal = sum(1 for s in canary_inc if not s.optimal) + (len(canary) - len(canary_inc))
    canary_block = {"n": len(canary), "non_optimal": canary_non_optimal, "flag": canary_non_optimal > 1}
    if canary_block["flag"]:
        degraded_reasons.append("canary: >1/10 non-optimal")
    if len(canary) < 10:
        degraded_reasons.append("canary not fired at N=10")

    pooled = _rates(corpus)
    findings: list[str] = []  # corpus/product findings — reported, never "degraded"
    if corpus and pooled["excluded_instrument"] / len(corpus) > 0.15:
        degraded_reasons.append("exclusions above 15%")
    if corpus and pooled["excluded_measurement"] / len(corpus) > 0.15:
        findings.append(
            f"measurement exclusions (surface/no_calls) in {round(100 * pooled['excluded_measurement'] / len(corpus))}% of runs — the corpus routes to the planner or the model never calls a tool; a corpus/kit finding, not an instrument fault"
        )
    if pooled["n"] and pooled["unattributed_excess"] / pooled["n"] > 0.15:
        degraded_reasons.append("unattributed_excess above 15%")
    if pooled["n"] and pooled["runs_with_retried_provider_error"] / pooled["n"] > 0.10:
        degraded_reasons.append("provider retries in >10% of runs")
    if firing.get("aborted"):
        degraded_reasons.append(f"driver aborted: {firing.get('abort_reason')}")

    by_repeat: list[dict[str, Any]] = []
    for rep in sorted({s.repeat for s in corpus}):
        rs = [s for s in corpus if s.repeat == rep]
        r = _rates(rs)
        cached = [s.tokens.get("cached_prompt", 0) for s in rs if s.excluded is None]
        by_repeat.append(
            {
                "repeat": rep,
                "n": r["n"],
                "excluded": r["excluded"],
                "clean": r["clean"],
                "optimal": r["optimal"],
                "cached_prompt_tokens_median": _median(cached),
            }
        )

    by_case: list[dict[str, Any]] = []
    ledger_map: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    exclusions: list[dict[str, Any]] = []
    measurement_exclusions: list[dict[str, Any]] = []
    for case in sorted({s.case for s in corpus}):
        cs = sorted((s for s in corpus if s.case == case), key=lambda s: s.repeat)
        r = _rates(cs)
        hist: Counter[str] = Counter()
        for s in cs:
            if s.excluded is not None:
                (measurement_exclusions if s.excluded in MEASUREMENT_KINDS else exclusions).append(
                    {"case": case, "repeat": s.repeat, "kind": s.excluded, "evidence": s.exclusion_evidence}
                )
                continue
            for d in s.deviations:
                hist[d.cls] += 1
                ledger_map[(case, d.cls)].append(
                    {
                        "repeat": s.repeat,
                        "sequence_no": list(d.sequence_no),
                        "tool": d.tool,
                        "args_digest": d.args_digest,
                        "codes": list(d.codes),
                        "audit_ordinal": d.audit_ordinal,
                    }
                )
        streak = any(
            a.excluded is not None
            and a.excluded not in MEASUREMENT_KINDS
            and b.excluded is not None
            and b.excluded not in MEASUREMENT_KINDS
            and b.repeat == a.repeat + 1
            for a, b in pairwise(cs)
        )
        inc = [s for s in cs if s.excluded is None]
        by_case.append(
            {
                "case": case,
                "n": r["n"],
                "excluded": r["excluded"],
                "clean": r["clean"],
                "optimal": r["optimal"],
                "histogram": dict(sorted(hist.items())),
                "median_excess": _median([s.excess for s in inc]),
                "median_review_rounds": _median([s.review_rounds for s in inc]),
                "per_case_ci_pp": ci_half_width_pp(r["n"]),
                "exclusion_streak": streak,
            }
        )
    ledger = [{"case": c, "class": k, "severity": SEVERITY[k], "events": ev} for (c, k), ev in sorted(ledger_map.items())]

    report: dict[str, Any] = {
        "round": round_dir.name,
        "corpus_version": corpus_version,
        "identity": identity,
        "caveats": list(CAVEATS),
        "degraded": {"flag": bool(degraded_reasons), "reasons": degraded_reasons},
        "findings": findings,
        "canary": canary_block,
        "tripwire": tripwire,
        "pooled": pooled,
        "by_repeat": by_repeat,
        "by_case": by_case,
        "exclusions": exclusions,
        "measurement_exclusions": measurement_exclusions,
        "ledger": ledger,
        "compare": None,
    }
    if compare_to is not None:
        report["compare"] = _compare(report, Path(compare_to), force=force_compare)
    return report


def _compare(report: dict[str, Any], prev_dir: Path, *, force: bool = False) -> dict[str, Any]:
    prev_path = prev_dir / "report.json"
    if not prev_path.exists():
        raise CompareRefused(f"{prev_dir}: no report.json — run report.py on the previous round first")
    prev = json.loads(prev_path.read_text())
    if prev.get("corpus_version") != report["corpus_version"]:
        raise CompareRefused(f"corpus_version differs: prev {prev.get('corpus_version')} vs current {report['corpus_version']}")
    pb, cb = prev.get("identity", {}).get("binding", {}), report["identity"].get("binding", {})
    problems: list[str] = []
    for k in sorted(set(pb) | set(cb)):
        if k in _NULLABLE_BINDING and pb.get(k) is None and cb.get(k) is None:
            continue  # unset sampling knobs on both sides ARE a match
        if pb.get(k) is None or cb.get(k) is None:
            problems.append(
                f"{k} is null on {'both' if pb.get(k) is None and cb.get(k) is None else 'one'} side (a null binding is not a match)"
            )
        elif pb.get(k) != cb.get(k):
            problems.append(f"{k} ({pb.get(k)!r} → {cb.get(k)!r})")
    forced = False
    if problems:
        if not force:
            raise CompareRefused("binding identity mismatch on: " + ", ".join(problems))
        forced = True
        report["caveats"].insert(
            0, "FORCED COMPARE over a binding-identity mismatch: " + "; ".join(problems) + " — deltas below are NOT attributable to the kit"
        )
    pr, cr = prev.get("identity", {}).get("recorded", {}), report["identity"].get("recorded", {})
    recorded_deltas = {
        k: [pr.get(k), cr.get(k)]
        for k in ["composer_skill_hash", *sorted((set(pr) | set(cr)) - {"composer_skill_hash"})]
        if pr.get(k) != cr.get(k)
    }

    def pp(cur: float | None, old: float | None) -> float | None:
        return None if cur is None or old is None else round((cur - old) * 100, 1)

    pooled_delta = {
        "clean_pp": pp(report["pooled"]["clean_rate"], prev["pooled"].get("clean_rate")),
        "optimal_pp": pp(report["pooled"]["optimal_rate"], prev["pooled"].get("optimal_rate")),
        "hard_pp": pp(report["pooled"]["hard_rate"], prev["pooled"].get("hard_rate")),
    }
    prev_cases = {c["case"]: c for c in prev.get("by_case", [])}
    by_case_delta = []
    for c in report["by_case"]:
        p = prev_cases.get(c["case"])
        cur_rate = c["clean"] / c["n"] if c["n"] else None
        old_rate = (p["clean"] / p["n"]) if p and p["n"] else None
        by_case_delta.append({"case": c["case"], "clean_pp": pp(cur_rate, old_rate), "indicative": True})
    return {
        "prev_round": prev.get("round"),
        "forced": forced,
        "recorded_deltas": recorded_deltas,
        "pooled_delta": pooled_delta,
        "by_case_delta": by_case_delta,
    }


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def render_markdown(report: dict[str, Any]) -> str:
    p = report["pooled"]
    out: list[str] = [f"# Composer battery — round `{report['round']}` (corpus v{report['corpus_version']})", ""]
    out += ["> Caveats: " + "; ".join(report["caveats"]), ""]
    if report["degraded"]["flag"]:
        out += ["**DEGRADED FIRING:** " + "; ".join(report["degraded"]["reasons"]), ""]
    if report.get("findings"):
        out += ["**Findings:** " + "; ".join(report["findings"]), ""]
    b, r = report["identity"].get("binding", {}), report["identity"].get("recorded", {})
    out += [
        "## Identity",
        "",
        f"- binding: `{json.dumps(b, sort_keys=True)}`",
        f"- recorded: composer_skill_hash=`{r.get('composer_skill_hash')}` server_version=`{r.get('server_version')}` first_call_messages_hash=`{r.get('first_call_messages_hash')}`",
        "",
    ]
    if report.get("compare"):
        c = report["compare"]
        out += [
            f"## Compare vs `{c['prev_round']}`",
            "",
            "Recorded deltas (skill hash first): "
            + (", ".join(f"{k}: {v[0]!r} → {v[1]!r}" for k, v in c["recorded_deltas"].items()) or "none"),
            f"Pooled Δ: clean {c['pooled_delta']['clean_pp']} pp, optimal {c['pooled_delta']['optimal_pp']} pp, hard {c['pooled_delta']['hard_pp']} pp",
            "Per-case Δ (indicative, ±~44 pp at N=5): " + ", ".join(f"{d['case']} {d['clean_pp']}" for d in c["by_case_delta"]),
            "",
        ]
    out += [
        "## Headline",
        "",
        f"- clean {_pct(p['clean_rate'])} (n={p['n']}, excluded={p['excluded']}, formula {p['formula']}, 95% CI ±{p['ci_half_width_pp']} pp)",
        f"- optimal {_pct(p['optimal_rate'])} (n={p['n']}, excluded={p['excluded']}, formula {p['formula']})",
        f"- hard {_pct(p['hard_rate'])} (n={p['n']}, excluded={p['excluded']}, formula {p['formula']})",
        f"- clean excluding provider retries: {p['clean_ex_transport']}/{p['n']} (runs with a retried provider error: {p['runs_with_retried_provider_error']})",
        f"- unattributed_excess: {p['unattributed_excess']}/{p['n']} runs; below_floor: {p['below_floor']}/{p['n']} runs",
        f"- canary: n={report['canary']['n']} non_optimal={report['canary']['non_optimal']} flag={report['canary']['flag']}",
        "",
    ]
    out += [
        "## Tripwire",
        "",
        "| fixture | pass | staged_variant | surface | planner_calls | planner_codes | reason |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    out += [
        f"| {t['fixture']} | {t['pass']} | {t.get('staged_variant')} | {t.get('surface')} | {t.get('planner_calls')} | {json.dumps(t.get('planner_codes') or {})} | {t.get('reason')} |"
        for t in report["tripwire"]
    ] or ["| (none) | | | | | | |"]
    out += [
        "",
        "## Per-repeat",
        "",
        "| repeat | n | excluded | clean | optimal | cached_prompt_tokens_median |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    out += [
        f"| {x['repeat']} | {x['n']} | {x['excluded']} | {x['clean']} | {x['optimal']} | {x['cached_prompt_tokens_median']} |"
        for x in report["by_repeat"]
    ]
    out += [
        "",
        "## Per-case (indicative)",
        "",
        "| case | n | excluded | clean | optimal | ±pp | median_excess | median_review_rounds | histogram | streak |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    out += [
        f"| {c['case']} | {c['n']} | {c['excluded']} | {c['clean']} | {c['optimal']} | {c['per_case_ci_pp']} | {c['median_excess']} | {c['median_review_rounds']} | {json.dumps(c['histogram'])} | {c['exclusion_streak']} |"
        for c in report["by_case"]
    ]
    out += ["", "## Instrument exclusions (harness faults)", ""] + (
        [f"- {e['case']}/{e['repeat']}: `{e['kind']}` — {e['evidence']}" for e in report["exclusions"]] or ["- none"]
    )
    out += ["", "## Measurement exclusions (product findings — surface/no_calls; not scored by a loop-only instrument)", ""] + (
        [f"- {e['case']}/{e['repeat']}: `{e['kind']}` — {e['evidence']}" for e in report["measurement_exclusions"]] or ["- none"]
    )
    out += ["", "## Deviation ledger", ""]
    for entry in report["ledger"]:
        out.append(f"### {entry['case']} — `{entry['class']}` ({entry['severity']}, {len(entry['events'])} events)")
        out += [
            f"- repeat {e['repeat']}: seq {e['sequence_no']} tool={e['tool']} digest={e['args_digest']} codes={e['codes']} audit_ordinal={e['audit_ordinal']}"
            for e in entry["events"]
        ]
        out.append("")
    if not report["ledger"]:
        out.append("- none")
    return "\n".join(out) + "\n"


def write_report(round_dir: Path, report: dict[str, Any]) -> tuple[Path, Path]:
    j = Path(round_dir) / "report.json"
    m = Path(round_dir) / "report.md"
    j.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    m.write_text(render_markdown(report))
    return j, m


__all__ = [
    "CAVEATS",
    "CompareRefused",
    "LateBinding",
    "build_report",
    "ci_half_width_pp",
    "collect_scores",
    "render_markdown",
    "write_report",
]
