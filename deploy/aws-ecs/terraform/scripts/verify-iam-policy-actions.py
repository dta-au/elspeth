#!/usr/bin/env python3
"""Compare complete flattened action sets in rendered and live IAM policies."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

MAX_POLICY_BYTES = 1024 * 1024

_ACTION = re.compile(r"(?:[a-z][a-z0-9-]{0,63}:[A-Za-z0-9*?]+|\*)")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class VerificationError(ValueError):
    """The operator input or IAM policy evidence was not admissible."""


def _reject(reason: str) -> NoReturn:
    raise VerificationError(reason)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    admitted: dict[str, object] = {}
    for key, value in pairs:
        if key in admitted:
            _reject("duplicate JSON object key")
        admitted[key] = value
    return admitted


def _reject_nonfinite(_value: str) -> NoReturn:
    _reject("non-finite JSON number")


def _read_json_file(raw_path: str, *, label: str) -> object:
    path = Path(raw_path)
    try:
        if path.is_symlink() or not path.is_file():
            _reject(f"{label} is not a regular non-symlink file")
        size = path.stat().st_size
        if size <= 0 or size > MAX_POLICY_BYTES:
            _reject(f"{label} byte limit or presence check failed")
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="strict")
        return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_nonfinite)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        if isinstance(exc, VerificationError):
            raise
        raise VerificationError(f"{label} is not strict bounded JSON") from exc


def _object(value: object, *, label: str) -> dict[str, object]:
    if type(value) is not dict:
        _reject(f"{label} is not an object")
    return value


def _action(value: object, *, label: str) -> str:
    if type(value) is not str or _ACTION.fullmatch(value) is None:
        _reject(f"{label} is not an IAM action string")
    return value


def _flatten_actions(document: object, *, label: str) -> set[str]:
    policy = _object(document, label=label)
    if "Statement" not in policy:
        _reject(f"{label} Statement is missing")
    statements = policy["Statement"]
    if type(statements) is not list or not statements:
        _reject(f"{label} Statement is not a non-empty array")
    flattened: set[str] = set()
    for index, raw_statement in enumerate(statements):
        statement = _object(raw_statement, label=f"{label} Statement[{index}]")
        if statement.get("Effect") != "Allow":
            _reject(f"{label} Statement[{index}] is not an Allow statement")
        if "NotAction" in statement:
            _reject(f"{label} Statement[{index}] uses unsupported NotAction")
        if "Action" not in statement:
            _reject(f"{label} Statement[{index}] Action is missing")
        raw_actions = statement["Action"]
        if type(raw_actions) is str:
            actions = [raw_actions]
        elif type(raw_actions) is list and raw_actions:
            actions = raw_actions
        else:
            _reject(f"{label} Statement[{index}] Action has an unsupported shape")
        for action_index, raw_action in enumerate(actions):
            flattened.add(_action(raw_action, label=f"{label} Statement[{index}] Action[{action_index}]"))
    if not flattened:
        _reject(f"{label} action set is empty")
    return flattened


def _live_document(payload: object) -> object:
    root = _object(payload, label="live policy version response")
    if "PolicyVersion" not in root:
        _reject("live PolicyVersion is missing")
    version = _object(root["PolicyVersion"], label="live PolicyVersion")
    if version.get("IsDefaultVersion") is not True:
        _reject("live policy version is not the default")
    if "Document" not in version:
        _reject("live default policy Document is missing")
    return version["Document"]


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rendered-policy", required=True)
    parser.add_argument("--live-policy-version", required=True)
    parser.add_argument("--label", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if type(args.label) is not str or _LABEL.fullmatch(args.label) is None:
            _reject("label is invalid")
        rendered = _read_json_file(args.rendered_policy, label="rendered policy")
        live_response = _read_json_file(args.live_policy_version, label="live policy version response")
        rendered_actions = _flatten_actions(rendered, label="rendered policy")
        live_actions = _flatten_actions(_live_document(live_response), label="live policy")
        missing = sorted(rendered_actions - live_actions)
        unexpected = sorted(live_actions - rendered_actions)
        if missing or unexpected:
            missing_text = ",".join(missing) or "-"
            unexpected_text = ",".join(unexpected) or "-"
            _reject(f"action-set drift missing_from_live={missing_text} unexpected_in_live={unexpected_text}")
    except VerificationError as exc:
        sys.stderr.write(f"verify-iam-policy-actions: refused {exc}\n")
        return 1
    sys.stdout.write(f"IAM policy action set verified: {args.label}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
