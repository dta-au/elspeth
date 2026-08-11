"""Constrained evidence collection, link checking, and CLI dispatch."""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, cast

from .catalog import validate_catalog
from .common import (
    ContractError,
    _dict,
    _fail,
    _git_root,
    _list,
    _repository_path,
    _require,
    _strings,
    load_unique_json,
)
from .package import initialize_full, validate_package

COLLECTION_FLAGS = {
    "-q",
    "--quiet",
    "-x",
    "--exitfirst",
    "--strict-config",
    "--strict-markers",
    "--disable-warnings",
}
COLLECTION_SAFE_ENVIRONMENT = {"PYTHONHASHSEED", "TZ", "LANG", "LC_ALL"}
COLLECTION_INHERITED_ENVIRONMENT = {"PATH", "TZ", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "WINDIR"}


def _collect_argv(argv: list[Any], root: Path) -> list[str]:
    recorded = _strings(argv, "pytest argv")
    _require(
        recorded[:3] == [sys.executable, "-m", "pytest"],
        "pytest evidence argv must be a trusted pytest invocation using sys.executable -m pytest",
    )
    result = [sys.executable, "-m", "pytest", "--collect-only", "-o", "addopts="]
    selectors = 0
    index = 3
    while index < len(recorded):
        item = recorded[index]
        if item == "--junitxml":
            _require(index + 1 < len(recorded), "pytest --junitxml requires a path")
            index += 2
            continue
        if item.startswith("--junitxml="):
            index += 1
            continue
        if item in {"-n", "--numprocesses"}:
            _require(index + 1 < len(recorded) and recorded[index + 1] == "0", "pytest collection permits only -n 0")
            index += 2
            continue
        if item in {"-m", "-k"}:
            _require(index + 1 < len(recorded) and bool(recorded[index + 1]), f"pytest {item} requires an expression")
            result.extend((item, recorded[index + 1]))
            index += 2
            continue
        if item in COLLECTION_FLAGS:
            result.append(item)
            index += 1
            continue
        if re.fullmatch(r"--maxfail=[1-9][0-9]*", item) or re.fullmatch(r"--tb=(?:auto|long|short|line|native|no)", item):
            result.append(item)
            index += 1
            continue
        _require(not item.startswith("-"), f"pytest collection option is not permitted: {item}")
        path_text = item.split("::", 1)[0]
        _require(bool(path_text), f"pytest selector is invalid: {item}")
        selector_path = Path(path_text)
        _require(not selector_path.is_absolute(), f"pytest selector must be repository-relative: {item}")
        resolved = _repository_path(root, path_text, f"pytest selector {item}")
        _require(resolved.is_relative_to(root / "tests"), f"pytest selector must be under tests/: {item}")
        _require(resolved.exists(), f"pytest selector does not exist: {item}")
        result.append(item)
        selectors += 1
        index += 1
    _require(selectors > 0, "pytest collection requires at least one explicit tests/ selector")
    return result


def _collection_environment(root: Path, safe_environment: dict[str, Any], evidence_id: Any) -> dict[str, str]:
    environment = {name: value for name, value in os.environ.items() if name in COLLECTION_INHERITED_ENVIRONMENT}
    environment.update(
        {
            "PYTHONPATH": str(root / "src"),
            "PYTHONHASHSEED": "0",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    for name, value in safe_environment.items():
        _require(name in COLLECTION_SAFE_ENVIRONMENT, f"evidence {evidence_id} safe_environment name is not permitted: {name}")
        _require(value is None or isinstance(value, str), f"evidence {evidence_id} safe_environment value must be text or null")
        if value is None:
            environment.pop(name, None)
        else:
            environment[name] = value
    return environment


def _run_collection(
    argv: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
    evidence_id: Any,
) -> tuple[int, str, str]:
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            process.kill()
        process.communicate()
        _fail(f"pytest collection timed out after {timeout_seconds} seconds for {evidence_id}")
    return process.returncode, stdout, stderr


def collect_evidence(assessment_path: Path) -> int:
    root = _git_root(assessment_path)
    assessment = load_unique_json(assessment_path)
    count = 0
    for raw_record in _list(assessment.get("evidence"), "evidence"):
        record = _dict(raw_record, "evidence record")
        if record.get("kind") != "pytest":
            continue
        count += 1
        evidence_id = record.get("id")
        argv = _collect_argv(_list(record.get("argv"), f"evidence {evidence_id} argv"), root)
        cwd = _repository_path(root, record.get("cwd_relative"), "evidence cwd_relative")
        safe_environment = _dict(record.get("safe_environment"), "safe_environment")
        environment = _collection_environment(root, safe_environment, evidence_id)
        raw_timeout_seconds = record.get("timeout_seconds")
        _require(
            type(raw_timeout_seconds) is int and raw_timeout_seconds > 0,
            f"evidence {evidence_id} timeout_seconds must be a positive integer",
        )
        timeout_seconds = cast(int, raw_timeout_seconds)
        returncode, stdout, stderr = _run_collection(
            argv,
            cwd=cwd,
            environment=environment,
            timeout_seconds=timeout_seconds,
            evidence_id=evidence_id,
        )
        stderr_summary = next((line.strip() for line in stderr.splitlines() if line.strip()), "no stderr")
        _require(returncode == 0, f"pytest collection failed for {evidence_id}: {stderr_summary}")
        actual = [line for line in stdout.splitlines() if "::" in line]
        index = _dict(record.get("collected_node_index"), "collected_node_index")
        node_path = _repository_path(root, index.get("path"), "collected_node_index.path")
        expected = node_path.read_text(encoding="utf-8").splitlines()
        _require(actual == expected, f"pytest collection drift for {evidence_id}")
    return count


def _relative_path_uses_symlink(base: Path, relative: Path) -> bool:
    current = base
    for part in relative.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            current = current.parent
            continue
        current = current / part
        if current.is_symlink():
            return True
    return False


def check_links(root: Path) -> int:
    documents = sorted((root / "docs/architecture/state_engine").glob("**/*.md"))
    docs_readme = root / "docs/README.md"
    if docs_readme.is_file():
        documents.append(docs_readme)
    pattern = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
    checked = 0
    missing: list[str] = []
    for document in documents:
        _require(
            not document.is_symlink(),
            f"documentation input cannot be a symlink: {document.relative_to(root)}",
        )
        text = document.read_text(encoding="utf-8")
        for raw_target in pattern.findall(text):
            target = raw_target.split(maxsplit=1)[0].strip("<>")
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            checked += 1
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            relative_target = Path(path_part)
            _require(
                not relative_target.is_absolute(),
                f"link target must be repository-relative: {document.relative_to(root)} -> {target}",
            )
            candidate = document.parent / relative_target
            resolved = candidate.resolve()
            if not resolved.is_relative_to(root):
                if _relative_path_uses_symlink(document.parent, relative_target):
                    _fail(f"link target escapes through a symlink: {document.relative_to(root)} -> {target}")
                _fail(f"link target escapes the repository: {document.relative_to(root)} -> {target}")
            if not resolved.exists():
                missing.append(f"{document.relative_to(root)} -> {target}")
    _require(not missing, "missing documentation links:\n" + "\n".join(missing))
    return checked


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_catalog_parser = subparsers.add_parser("validate-catalog")
    validate_catalog_parser.add_argument("catalog", type=Path)
    init_parser = subparsers.add_parser("init-full")
    init_parser.add_argument("assessment_id")
    init_parser.add_argument("output_directory", type=Path)
    validate_package_parser = subparsers.add_parser("validate-package")
    validate_package_parser.add_argument("assessment", type=Path)
    collect_parser = subparsers.add_parser("collect-evidence")
    collect_parser.add_argument("assessment", type=Path)
    subparsers.add_parser("check-links")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "validate-catalog":
            catalog_path = arguments.catalog.resolve()
            validate_catalog(load_unique_json(catalog_path), catalog_path)
            print(f"state-engine catalog: valid ({catalog_path})")
        elif arguments.command == "init-full":
            output = initialize_full(arguments.assessment_id, arguments.output_directory)
            print(f"state-engine assessment initialized: {output}")
        elif arguments.command == "validate-package":
            leg_count, verdict = validate_package(arguments.assessment)
            print(f"state-engine assessment contract: valid ({leg_count} legs, {verdict})")
        elif arguments.command == "collect-evidence":
            count = collect_evidence(arguments.assessment)
            print(f"evidence collection: valid ({count} pytest records)")
        elif arguments.command == "check-links":
            count = check_links(_git_root(Path.cwd()))
            print(f"state-engine documentation links: valid ({count} links)")
        else:
            _fail(f"unknown command: {arguments.command}")
    except (ContractError, ValueError, KeyError, OSError, ET.ParseError) as error:
        print(f"state-engine assessment: invalid: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        summary = next((line.strip() for line in str(error).splitlines() if line.strip()), "no detail")
        print(f"state-engine assessment: internal error ({type(error).__name__}): {summary}", file=sys.stderr)
        return 1
    return 0
