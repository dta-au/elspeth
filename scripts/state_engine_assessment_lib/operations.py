"""Static retained-evidence validation, link checking, and CLI dispatch."""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from .catalog import validate_catalog
from .common import (
    ContractError,
    _fail,
    _git_root,
    _require,
    load_unique_json,
)
from .package import initialize_full, validate_evidence_artifacts, validate_package


def collect_evidence(assessment_path: Path) -> int:
    return validate_evidence_artifacts(assessment_path)


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
    inline_pattern = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
    reference_pattern = re.compile(r"^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*(?:<([^>\n]+)>|(\S+))", re.MULTILINE)
    checked = 0
    missing: list[str] = []
    for document in documents:
        _require(
            not document.is_symlink(),
            f"documentation input cannot be a symlink: {document.relative_to(root)}",
        )
        text = document.read_text(encoding="utf-8")
        reference_targets = [angle or bare for angle, bare in reference_pattern.findall(text)]
        for raw_target in [*inline_pattern.findall(text), *reference_targets]:
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
            print(f"retained evidence validation: valid ({count} pytest records)")
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
