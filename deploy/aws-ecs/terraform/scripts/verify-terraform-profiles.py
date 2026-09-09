#!/usr/bin/env python3
"""Fail closed unless Terraform evidence names the selected least-privilege profiles."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

MAX_JSON_BYTES = 64 * 1024 * 1024

_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class VerificationError(ValueError):
    """The operator input or Terraform evidence was not admissible."""


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
        if size <= 0 or size > MAX_JSON_BYTES:
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


def _profile(value: object, *, label: str) -> str:
    if type(value) is not str or _PROFILE.fullmatch(value) is None:
        _reject(f"{label} is not a valid AWS profile name")
    return value


def _require_expected_profiles(
    *,
    installer: object,
    lifecycle: object | None,
    forbidden: object,
) -> tuple[str, str | None, str]:
    installer_profile = _profile(installer, label="expected installer profile")
    forbidden_profile = _profile(forbidden, label="forbidden profile")
    if installer_profile == forbidden_profile:
        _reject("expected installer profile is the forbidden administrator profile")
    if lifecycle is None:
        return installer_profile, None, forbidden_profile
    lifecycle_profile = _profile(lifecycle, label="expected IAM lifecycle profile")
    if lifecycle_profile == forbidden_profile:
        _reject("expected IAM lifecycle profile is the forbidden administrator profile")
    if installer_profile == lifecycle_profile:
        _reject("expected Terraform profiles are collapsed")
    return installer_profile, lifecycle_profile, forbidden_profile


def _plan_variable(variables: dict[str, object], name: str) -> str:
    if name not in variables:
        _reject(f"plan variable {name} is missing")
    entry = _object(variables[name], label=f"plan variable {name}")
    if "value" not in entry:
        _reject(f"plan variable {name} value is missing")
    return _profile(entry["value"], label=f"plan variable {name} value")


def _verify_plan(args: argparse.Namespace) -> None:
    installer, lifecycle, forbidden = _require_expected_profiles(
        installer=args.installer_profile,
        lifecycle=args.iam_lifecycle_profile,
        forbidden=args.forbidden_profile,
    )
    if lifecycle is None:
        _reject("expected IAM lifecycle profile is missing")
    root = _object(_read_json_file(args.plan_json, label="plan JSON"), label="plan JSON")
    if "variables" not in root:
        _reject("plan variables are missing")
    variables = _object(root["variables"], label="plan variables")
    actual_installer = _plan_variable(variables, "aws_profile")
    actual_lifecycle = _plan_variable(variables, "iam_lifecycle_aws_profile")
    if actual_installer == forbidden or actual_lifecycle == forbidden:
        _reject("plan names the forbidden administrator profile")
    if actual_installer == actual_lifecycle:
        _reject("plan Terraform profiles are collapsed")
    if actual_installer != installer or actual_lifecycle != lifecycle:
        _reject("plan Terraform profiles do not match the selected principals")


def _verify_backend(args: argparse.Namespace) -> None:
    installer, _lifecycle, forbidden = _require_expected_profiles(
        installer=args.installer_profile,
        lifecycle=None,
        forbidden=args.forbidden_profile,
    )
    root = _object(_read_json_file(args.backend_state, label="backend state"), label="backend state")
    if "backend" not in root:
        _reject("initialized backend is missing")
    backend = _object(root["backend"], label="initialized backend")
    if backend.get("type") != "s3":
        _reject("initialized backend is not s3")
    if "config" not in backend:
        _reject("initialized backend config is missing")
    config = _object(backend["config"], label="initialized backend config")
    if "profile" not in config:
        _reject("initialized backend profile is missing")
    actual = _profile(config["profile"], label="initialized backend profile")
    if actual == forbidden:
        _reject("initialized backend names the forbidden administrator profile")
    if actual != installer:
        _reject("initialized backend profile does not match the selected installer")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)

    plan = subparsers.add_parser("plan", help="verify provider profiles recorded in terraform show -json")
    plan.add_argument("--plan-json", required=True)
    plan.add_argument("--installer-profile", required=True)
    plan.add_argument("--iam-lifecycle-profile", required=True)
    plan.add_argument("--forbidden-profile", required=True)

    backend = subparsers.add_parser("backend", help="verify the initialized S3 backend profile")
    backend.add_argument("--backend-state", required=True)
    backend.add_argument("--installer-profile", required=True)
    backend.add_argument("--forbidden-profile", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.mode == "plan":
            _verify_plan(args)
            success = "terraform plan profiles verified"
        else:
            _verify_backend(args)
            success = "terraform backend profile verified"
    except VerificationError as exc:
        sys.stderr.write(f"verify-terraform-profiles: refused {exc}\n")
        return 1
    sys.stdout.write(f"{success}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
