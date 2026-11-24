"""Helper for scripts/examples-gate.sh: inventory, per-case preflight, and verdicts.

The driver (examples-gate.sh) owns process control: copying trees, starting
fixture servers, running the documented command and recording its exit code.
This module never runs an example. It reads the case data file
(examples-gate-cases.yaml), the exported tree, and -- after a case ran -- the
case's audit databases (opened read-only) and its log.

Subcommands
  validate   --cases F --tree T            data file covers every example, config and launcher
  inventory  --cases F --tree T [--out F]  render the inventory (classification + deciding line)
  list       --cases F [--only IDS]        one line per case for the driver (\x1f-separated)
  excluded   --cases F                     one SKIP line per config the gate does not run
  preflight  --cases F --tree T --case ID  offline guard + prerequisites for one case
  check      --cases F --root R --case ID --exit N --log L --evidence E
                                           verdict line for one finished case

Exit codes: 0 = ok/PASS, 1 = FAIL/refused, 2 = usage or data-file error.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import yaml

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
URL_RE = re.compile(r"https?://(\[[0-9a-fA-F:]+\]|[^/:\s'\"<>]+)")
# Plugins that talk to a remote service by construction, whatever their URL.
REMOTE_PLUGINS = frozenset(
    {
        "azure_blob",
        "azure_content_safety",
        "azure_prompt_shield",
        "azure_ai_search",
        "power_automate",
        "aws_textract_inline_analysis",
    }
)
URL_OPTION_KEYS = frozenset({"url", "base_url", "endpoint", "account_url", "vault_url", "trigger_url"})
CLASSES = frozenset({"NON-NETWORK", "NETWORK", "CONTAINER", "NOT-A-PIPELINE", "UNSURE"})


class DataError(Exception):
    """The case data file is malformed or does not cover the tree."""


# --------------------------------------------------------------------------- data


def load_cases(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise DataError(f"{path}: expected a mapping with version: 1")
    for key in ("examples", "cases"):
        if key not in data:
            raise DataError(f"{path}: missing top-level key {key!r}")
    ids = [case["id"] for case in data["cases"]]
    if len(ids) != len(set(ids)):
        raise DataError(f"{path}: duplicate case ids")
    for case in data["cases"]:
        for key in ("id", "example", "command", "expect_exit", "configs", "expectation_source"):
            if key not in case:
                raise DataError(f"case {case.get('id')!r}: missing {key!r}")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", case["id"]):
            raise DataError(f"case id {case['id']!r} is not filename-safe")
    for name, entry in data["examples"].items():
        if entry.get("class") not in CLASSES:
            raise DataError(f"example {name!r}: class {entry.get('class')!r} not in {sorted(CLASSES)}")
        for cfg, cfg_entry in (entry.get("configs") or {}).items():
            if cfg_entry.get("class") not in CLASSES:
                raise DataError(f"example {name}/{cfg}: class {cfg_entry.get('class')!r} not in {sorted(CLASSES)}")
    return cast("dict[str, Any]", data)


def case_by_id(data: dict[str, Any], case_id: str) -> dict[str, Any]:
    for case in data["cases"]:
        if case["id"] == case_id:
            return cast("dict[str, Any]", case)
    raise DataError(f"no case {case_id!r}")


def is_pipeline(doc: Any) -> bool:
    return isinstance(doc, dict) and "sinks" in doc and ("source" in doc or "sources" in doc)


def discover(tree: Path) -> dict[str, dict[str, list[str]]]:
    """Every example directory, its top-level pipeline configs and its launchers."""
    found: dict[str, dict[str, list[str]]] = {}
    for directory in sorted((tree / "examples").iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        configs = []
        for path in sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")]):
            if is_pipeline(yaml.safe_load(path.read_text(encoding="utf-8"))):
                configs.append(path.name)
        launchers = sorted(p.name for p in directory.glob("*.sh"))
        found[directory.name] = {"configs": configs, "launchers": launchers}
    return found


# ----------------------------------------------------------------- offline guard


def _walk(node: Any, path: str = "") -> Iterator[tuple[str, str, Any]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield path, key, value
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")


def _line_of(text: str, needle: str) -> tuple[int, str]:
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return number, line.strip()
    return 0, ""


def _source_paths(doc: dict[str, Any]) -> list[str]:
    sources = doc.get("sources") or ({"primary": doc["source"]} if "source" in doc else {})
    paths = []
    for source in sources.values():
        if isinstance(source, dict):
            value = (source.get("options") or {}).get("path")
            if isinstance(value, str):
                paths.append(value)
    return paths


def network_findings(tree: Path, config: Path) -> list[str]:
    """Reasons a config would reach beyond loopback. Empty list = offline by construction.

    Each finding is 'file:line: text -- reason', the deciding line.
    """
    text = config.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    rel = config.relative_to(tree)
    findings: list[str] = []

    def add(needle: str, reason: str) -> None:
        number, line = _line_of(text, needle)
        findings.append(f"{rel}:{number}: {line} -- {reason}")

    for match in URL_RE.finditer(text):
        host = match.group(1).lower()
        if host not in LOOPBACK_HOSTS:
            add(match.group(0), f"non-loopback URL host {host}")
    for _path, key, value in _walk(doc):
        if key == "plugin" and value in REMOTE_PLUGINS:
            add(f"plugin: {value}", f"remote-service plugin {value}")
        if key in URL_OPTION_KEYS and isinstance(value, str) and "${" in value:
            add(value, f"{key} taken from the environment")
        if key == "provider" and value in {"azure", "bedrock", "aws_bedrock"}:
            add(f"provider: {value}", f"cloud LLM provider {value}")
    # An LLM node (transform or source) with no loopback base_url goes to the provider's public API.
    for container in _llm_nodes(doc):
        options = container.get("options") or {}
        base_url = options.get("base_url")
        provider = options.get("provider")
        if provider in {"openrouter", "openai", "anthropic"}:
            if not isinstance(base_url, str):
                add(f"provider: {provider}", f"LLM provider {provider} with no base_url (public API)")
            else:
                base_match = URL_RE.match(base_url)
                if base_match is None or base_match.group(1).lower() not in LOOPBACK_HOSTS:
                    add(base_url, "LLM base_url is not loopback")
    for _path, key, _value in _walk(doc):
        if key == "vault_url":
            add("vault_url", "secret store URL")
    for raw in _source_paths(doc):
        if "${" in raw:
            raw = re.sub(r"\$\{[^}:]*:-([^}]*)\}", r"\1", raw)
        candidate = tree / raw
        if candidate.is_file() and candidate.stat().st_size < 50_000_000:
            data = candidate.read_text(encoding="utf-8", errors="replace")
            for match in URL_RE.finditer(data):
                host = match.group(1).lower()
                if host not in LOOPBACK_HOSTS:
                    number, line = _line_of(data, match.group(0))
                    findings.append(f"{candidate.relative_to(tree)}:{number}: {line[:120]} -- source row URL host {host}")
                    break
    # De-duplicate while keeping order.
    return list(dict.fromkeys(findings))


def _llm_nodes(doc: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = []
    for _path, _key, value in _walk(doc):
        if isinstance(value, dict) and value.get("plugin") == "llm":
            nodes.append(value)
        if isinstance(value, list):
            nodes.extend(v for v in value if isinstance(v, dict) and v.get("plugin") == "llm")
    unique: list[dict[str, Any]] = []
    for node in nodes:
        if all(node is not other for other in unique):
            unique.append(node)
    return unique


def offline_evidence(tree: Path, config: Path) -> str:
    """For an offline config: the deciding line (a loopback base_url/URL, else the source plugin)."""
    text = config.read_text(encoding="utf-8")
    rel = config.relative_to(tree)
    for match in URL_RE.finditer(text):
        number, line = _line_of(text, match.group(0))
        return f"{rel}:{number}: {line} -- loopback only"
    doc = yaml.safe_load(text)
    plugins = sorted({value for _p, key, value in _walk(doc) if key == "plugin" and isinstance(value, str)})
    number, line = _line_of(text, "plugin:")
    return f"{rel}:{number}: {line} -- plugins {', '.join(plugins)}; no URL, no remote-service plugin"


# --------------------------------------------------------------------- validate


def cmd_validate(args: argparse.Namespace) -> int:
    data = load_cases(Path(args.cases))
    tree = Path(args.tree)
    found = discover(tree)
    problems = []
    declared = data["examples"]
    for name in sorted(set(found) - set(declared)):
        problems.append(f"example directory {name!r} is not classified in the data file")
    for name in sorted(set(declared) - set(found)):
        problems.append(f"data file classifies {name!r}, which is not an example directory")
    case_ids = {case["id"] for case in data["cases"]}
    for name, entry in declared.items():
        if name not in found:
            continue
        configs = entry.get("configs") or {}
        for cfg in found[name]["configs"]:
            if cfg not in configs:
                problems.append(f"{name}/{cfg}: pipeline config not classified")
        for cfg in configs:
            if cfg not in found[name]["configs"]:
                problems.append(f"{name}/{cfg}: classified but not a pipeline config in the tree")
        launchers = entry.get("launchers") or {}
        for launcher in found[name]["launchers"]:
            if launcher not in launchers:
                problems.append(f"{name}/{launcher}: launcher not classified")
        for item in [*configs.values(), *launchers.values()]:
            if item.get("class") == "NON-NETWORK" and not item.get("case") and not item.get("covered_by"):
                problems.append(f"{name}: a NON-NETWORK item names no case")
            for ref in [item.get("case"), item.get("covered_by")]:
                if ref and ref not in case_ids:
                    problems.append(f"{name}: references unknown case {ref!r}")
    for case in data["cases"]:
        if case["example"] not in declared:
            problems.append(f"case {case['id']}: unknown example {case['example']!r}")
        for cfg in case["configs"]:
            if not (tree / cfg).is_file():
                problems.append(f"case {case['id']}: config {cfg} missing from tree")
    for line in problems:
        print(f"DATA: {line}")
    print(
        f"validate: {len(found)} example dirs, "
        f"{sum(len(v['configs']) for v in found.values())} pipeline configs, "
        f"{sum(len(v['launchers']) for v in found.values())} launchers, "
        f"{len(data['cases'])} cases, {len(problems)} problems"
    )
    return 2 if problems else 0


# -------------------------------------------------------------------- inventory


def cmd_inventory(args: argparse.Namespace) -> int:
    data = load_cases(Path(args.cases))
    tree = Path(args.tree)
    found = discover(tree)
    lines = [
        "| Example | Config / launcher | Declared | Computed by the offline guard | Deciding line | Case | Note |",
        "|---|---|---|---|---|---|---|",
    ]
    disagreements = 0
    counts: dict[str, int] = {}
    for name, entry in data["examples"].items():
        configs = entry.get("configs") or {}
        launchers = entry.get("launchers") or {}
        if not configs and not launchers:
            counts[entry["class"]] = counts.get(entry["class"], 0) + 1
            lines.append(f"| {name} | (none) | {entry['class']} | - | - | - | {entry.get('note', '')} |")
            continue
        for cfg, item in configs.items():
            path = tree / "examples" / name / cfg
            findings = network_findings(tree, path) if path.is_file() else ["missing"]
            computed = "NETWORK" if findings else "NON-NETWORK"
            declared_class = item["class"]
            counts[declared_class] = counts.get(declared_class, 0) + 1
            comparable = declared_class in {"NON-NETWORK", "NETWORK"}
            agree = (not comparable) or computed == declared_class
            if not agree:
                disagreements += 1
            deciding = "; ".join(findings[:2]) if findings else offline_evidence(tree, path)
            flag = "" if agree else " **DISAGREES**"
            lines.append(
                f"| {name} | {cfg} | {declared_class} | {computed}{flag} | `{deciding}` | "
                f"{item.get('case') or item.get('covered_by') or '-'} | {item.get('note', '')} |"
            )
        for launcher, item in launchers.items():
            declared_class = item["class"]
            lines.append(
                f"| {name} | {launcher} (launcher) | {declared_class} | - | - | "
                f"{item.get('case') or item.get('covered_by') or '-'} | {item.get('note', '')} |"
            )
    summary = (
        f"Configs+empty dirs by declared class: {json.dumps(counts, sort_keys=True)}; "
        f"guard disagreements: {disagreements}; example dirs: {len(found)}"
    )
    text = "\n".join([summary, "", *lines]) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(summary)
    return 1 if disagreements else 0


# ------------------------------------------------------------------------- list


def cmd_list(args: argparse.Namespace) -> int:
    data = load_cases(Path(args.cases))
    only = set(args.only.split(",")) if args.only else None
    for case in data["cases"]:
        if only is not None and case["id"] not in only:
            continue
        server = case.get("server") or {}
        env = " ".join(f"{k}={v}" for k, v in (case.get("env") or {}).items())
        fields = [
            case["id"],
            case["command"],
            server.get("command", ""),
            str(server.get("port", "")),
            ",".join(str(p) for p in case.get("ports", [])),
            env,
            "1" if case.get("fingerprint_key") else "0",
            case.get("skip", ""),
        ]
        # \x1f (unit separator): bash `read` collapses runs of whitespace IFS characters,
        # so a tab separator would silently shift every field after an empty one.
        if any("\x1f" in f or "\n" in f for f in fields):
            raise DataError(f"case {case['id']}: separator or newline in a driver field")
        print("\x1f".join(fields))
    return 0


def cmd_excluded(args: argparse.Namespace) -> int:
    """One SKIP line per config/dir the gate does not run because it is not NON-NETWORK."""
    data = load_cases(Path(args.cases))
    for name, entry in data["examples"].items():
        configs = entry.get("configs") or {}
        if not configs and entry["class"] != "NON-NETWORK":
            print(f"SKIP({entry['class'].lower()}) {name} -- {entry.get('note') or entry.get('table', '')}")
        for cfg, item in configs.items():
            if item["class"] in {"NETWORK", "NOT-A-PIPELINE", "UNSURE"}:
                print(f"SKIP({item['class'].lower()}) {name}/{cfg} -- {item.get('note') or entry.get('table', '')}")
    return 0


# -------------------------------------------------------------------- preflight


def cmd_preflight(args: argparse.Namespace) -> int:
    data = load_cases(Path(args.cases))
    case = case_by_id(data, args.case)
    tree = Path(args.tree)
    refusals = []
    for cfg in case["configs"]:
        for finding in network_findings(tree, tree / cfg):
            refusals.append(f"offline guard: {finding}")
    requires = case.get("requires") or {}
    for module in requires.get("python_modules", []):
        if importlib.util.find_spec(module) is None:
            refusals.append(f"prerequisite: python module {module} not importable")
    for raw in requires.get("files", []):
        if not Path(os.path.expanduser(raw)).is_file():
            refusals.append(f"prerequisite: file {raw} absent (would be downloaded; refusing)")
    for line in refusals:
        print(line)
    if not refusals:
        print(f"preflight ok: {len(case['configs'])} config(s) offline by construction")
    return 1 if refusals else 0


# ------------------------------------------------------------------------ check


def _ro(db: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _run_facts(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    q = connection.execute
    facts: dict[str, Any] = {}
    facts["source_rows"] = q("SELECT COUNT(*) FROM rows WHERE run_id=?", (run_id,)).fetchone()[0]
    facts["sinks"] = dict(
        q(
            "SELECT sink_name, COUNT(*) FROM token_outcomes WHERE run_id=? AND completed=1 AND sink_name IS NOT NULL GROUP BY sink_name",
            (run_id,),
        ).fetchall()
    )
    facts["outcomes"] = {
        f"{o}/{p}": n
        for o, p, n in q(
            "SELECT outcome, path, COUNT(*) FROM token_outcomes WHERE run_id=? AND completed=1 GROUP BY outcome, path", (run_id,)
        ).fetchall()
    }
    facts["transform_errors"] = q("SELECT COUNT(*) FROM transform_errors WHERE run_id=?", (run_id,)).fetchone()[0]
    # Rows whose tokens do not carry exactly one completed terminal outcome (1:1 pipelines only).
    facts["rows_without_exactly_one_terminal"] = q(
        "SELECT COUNT(*) FROM rows r WHERE r.run_id=? AND (SELECT COUNT(*) FROM token_outcomes o "
        "JOIN tokens t ON t.token_id=o.token_id AND t.run_id=o.run_id "
        "WHERE t.row_id=r.row_id AND o.run_id=r.run_id AND o.completed=1) != 1",
        (run_id,),
    ).fetchone()[0]
    return facts


def _check_run_spec(root: Path, spec: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return (failures, evidence) for one audit-DB expectation block."""
    failures: list[str] = []
    evidence: list[str] = []
    db = root / spec["db"]
    label = spec.get("label", Path(spec["db"]).name)
    if not db.is_file():
        return [f"{label}: audit DB {spec['db']} absent"], evidence
    with _ro(db) as connection:
        mode = spec.get("mode", "live")
        runs = connection.execute("SELECT run_id, status FROM runs WHERE run_mode=? ORDER BY started_at", (mode,)).fetchall()
        want_runs = spec.get("runs")
        if want_runs is not None and len(runs) != want_runs:
            failures.append(f"{label}: {len(runs)} {mode} run(s) != {want_runs}")
        index = spec.get("run_index", -1)
        if not runs or not (-len(runs) <= index < len(runs)):
            failures.append(f"{label}: no {mode} run at index {index}")
            return failures, evidence
        run_id, status = runs[index]
        facts = _run_facts(connection, run_id)
        evidence.append(f"{label}: status={status} rows={facts['source_rows']} sinks={facts['sinks']}")
        statuses = spec.get("status")
        if statuses is not None and status not in statuses:
            failures.append(f"{label}: status {status} not in {statuses}")
        if "source_rows" in spec and facts["source_rows"] != spec["source_rows"]:
            failures.append(f"{label}: source rows {facts['source_rows']} != {spec['source_rows']}")
        if facts["source_rows"] == 0 and not spec.get("allow_zero_rows"):
            failures.append(f"{label}: zero source rows")
        for sink, want in (spec.get("sinks") or {}).items():
            got = facts["sinks"].get(sink, 0)
            if got != want:
                failures.append(f"{label}: sink {sink} {got} != {want}")
        for key, want in (spec.get("outcomes") or {}).items():
            got = facts["outcomes"].get(key, 0)
            if got != want:
                failures.append(f"{label}: outcome {key} {got} != {want}")
        if spec.get("conservation"):
            bad = facts["rows_without_exactly_one_terminal"]
            evidence.append(
                f"{label}: conservation rows_without_exactly_one_terminal={bad} "
                f"transform_errors={facts['transform_errors']} outcomes={facts['outcomes']}"
            )
            if bad:
                failures.append(f"{label}: {bad} row(s) lack exactly one terminal outcome")
            failed = sum(n for k, n in facts["outcomes"].items() if k.startswith("failure/"))
            if failed and facts["transform_errors"] == 0:
                failures.append(f"{label}: {failed} failure outcome(s) but no transform_errors reason")
        for sql_check in spec.get("sql", []):
            rows = connection.execute(sql_check["query"], {"run_id": run_id}).fetchall()
            got = [list(r) for r in rows]
            evidence.append(f"{label}: {sql_check['name']}={got}")
            if got != sql_check["expect"]:
                failures.append(f"{label}: {sql_check['name']} {got} != {sql_check['expect']}")
    return failures, evidence


def cmd_check(args: argparse.Namespace) -> int:
    data = load_cases(Path(args.cases))
    case = case_by_id(data, args.case)
    root = Path(args.root)
    failures: list[str] = []
    evidence: list[str] = [f"exit={args.exit}"]
    expect_exit = case["expect_exit"]
    if args.exit not in expect_exit:
        failures.append(f"exit {args.exit} not in {expect_exit}")
    log_text = Path(args.log).read_text(encoding="utf-8", errors="replace") if Path(args.log).is_file() else ""
    for marker in case.get("log_markers", []):
        text, count = (marker, 1) if isinstance(marker, str) else (marker["text"], marker.get("count", 1))
        got = log_text.count(text)
        if got < count:
            failures.append(f"log marker {text!r} x{got} < {count}")
    for spec in case.get("audit", []):
        try:
            more_failures, more_evidence = _check_run_spec(root, spec)
        except (sqlite3.Error, OSError, KeyError) as error:  # evidence capture failed: that is a FAIL, not a skip
            more_failures, more_evidence = [f"{spec.get('db')}: capture error {type(error).__name__}: {error}"], []
        failures.extend(more_failures)
        evidence.extend(more_evidence)
    for rel, want in (case.get("files") or {}).items():
        path = root / rel
        if want == "absent_or_empty":
            ok = (not path.exists()) or path.stat().st_size == 0
            evidence.append(f"{rel}: {'absent' if not path.exists() else path.stat().st_size}")
            if not ok:
                failures.append(f"{rel} should be absent or empty")
    verdict = "PASS" if not failures else "FAIL"
    record = {
        "case": case["id"],
        "verdict": verdict,
        "failures": failures,
        "evidence": evidence,
        "expectation_source": case["expectation_source"],
    }
    Path(args.evidence).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    detail = "; ".join(failures) if failures else "; ".join(evidence)
    print(f"{verdict} {case['id']} -- {detail}")
    return 0 if verdict == "PASS" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("validate")
    p.add_argument("--cases", required=True)
    p.add_argument("--tree", required=True)
    p = sub.add_parser("inventory")
    p.add_argument("--cases", required=True)
    p.add_argument("--tree", required=True)
    p.add_argument("--out")
    p = sub.add_parser("list")
    p.add_argument("--cases", required=True)
    p.add_argument("--only")
    p = sub.add_parser("excluded")
    p.add_argument("--cases", required=True)
    p = sub.add_parser("preflight")
    p.add_argument("--cases", required=True)
    p.add_argument("--tree", required=True)
    p.add_argument("--case", required=True)
    p = sub.add_parser("check")
    p.add_argument("--cases", required=True)
    p.add_argument("--root", required=True)
    p.add_argument("--case", required=True)
    p.add_argument("--exit", type=int, required=True)
    p.add_argument("--log", required=True)
    p.add_argument("--evidence", required=True)
    args = parser.parse_args(argv)
    handlers = {
        "validate": cmd_validate,
        "inventory": cmd_inventory,
        "list": cmd_list,
        "excluded": cmd_excluded,
        "preflight": cmd_preflight,
        "check": cmd_check,
    }
    try:
        return handlers[args.command](args)
    except DataError as error:
        print(f"DATA ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
