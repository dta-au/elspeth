#!/usr/bin/env python3
"""Admit one identity-bound task from ``aws ecs run-task --output json``."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from typing import NoReturn

MAX_RESPONSE_BYTES = 1024 * 1024

_PARTITION = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_ACCOUNT_ID = re.compile(r"[0-9]{12}")
_REGION = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_CLUSTER = re.compile(r"[A-Za-z0-9_-]{1,255}")
_TASK_DEFINITION_FAMILY = r"[A-Za-z0-9_-]{1,255}"
_TASK_ID = r"[0-9a-f]{32}"


class AdmissionError(ValueError):
    """The external response or caller-supplied identity was not admissible."""


def _reject(reason: str) -> NoReturn:
    raise AdmissionError(reason)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    admitted: dict[str, object] = {}
    for key, value in pairs:
        if key in admitted:
            _reject("duplicate JSON object key")
        admitted[key] = value
    return admitted


def _reject_nonfinite(_value: str) -> NoReturn:
    _reject("non-finite JSON number")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate an aws ecs run-task JSON response and print its admitted task ARN.",
    )
    parser.add_argument("--partition", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--task-definition", required=True)
    return parser.parse_args(argv)


def _validate_expected_identity(args: argparse.Namespace) -> tuple[str, str, str]:
    partition = args.partition
    account_id = args.account_id
    region = args.region
    cluster = args.cluster
    task_definition = args.task_definition
    if type(partition) is not str or _PARTITION.fullmatch(partition) is None:
        _reject("invalid expected partition")
    if type(account_id) is not str or _ACCOUNT_ID.fullmatch(account_id) is None:
        _reject("invalid expected account")
    if type(region) is not str or _REGION.fullmatch(region) is None:
        _reject("invalid expected region")
    if type(cluster) is not str or _CLUSTER.fullmatch(cluster) is None:
        _reject("invalid expected cluster")
    task_definition_pattern = re.compile(
        rf"arn:{re.escape(partition)}:ecs:{re.escape(region)}:{re.escape(account_id)}:"
        rf"task-definition/{_TASK_DEFINITION_FAMILY}:[1-9][0-9]*"
    )
    if type(task_definition) is not str or task_definition_pattern.fullmatch(task_definition) is None:
        _reject("invalid expected task definition")
    cluster_arn = f"arn:{partition}:ecs:{region}:{account_id}:cluster/{cluster}"
    task_arn_pattern = (
        rf"arn:{re.escape(partition)}:ecs:{re.escape(region)}:{re.escape(account_id)}:"
        rf"task/{re.escape(cluster)}/{_TASK_ID}"
    )
    return cluster_arn, task_definition, task_arn_pattern


def _read_response() -> object:
    raw = sys.stdin.buffer.read(MAX_RESPONSE_BYTES + 1)
    if raw == b"" or len(raw) > MAX_RESPONSE_BYTES:
        _reject("response byte limit or presence check failed")
    try:
        text = raw.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        if isinstance(exc, AdmissionError):
            raise
        raise AdmissionError("response is not strict bounded JSON") from exc


def _admit_task_arn(
    response: object,
    *,
    expected_cluster_arn: str,
    expected_task_definition_arn: str,
    task_arn_pattern: str,
) -> str:
    if type(response) is not dict:
        _reject("response is not an object")

    if "failures" not in response:
        _reject("failures is missing")
    failures = response["failures"]
    if type(failures) is not list or failures != []:
        _reject("failures is not an empty array")

    if "tasks" not in response:
        _reject("tasks is missing")
    tasks = response["tasks"]
    if type(tasks) is not list or len(tasks) != 1:
        _reject("tasks does not contain exactly one item")
    task = tasks[0]
    if type(task) is not dict:
        _reject("task is not an object")

    if not {"taskArn", "clusterArn", "taskDefinitionArn"} <= set(task):
        _reject("task identity fields are missing")
    task_arn = task["taskArn"]
    cluster_arn = task["clusterArn"]
    task_definition_arn = task["taskDefinitionArn"]
    if (
        type(task_arn) is not str
        or re.fullmatch(task_arn_pattern, task_arn) is None
        or type(cluster_arn) is not str
        or cluster_arn != expected_cluster_arn
        or type(task_definition_arn) is not str
        or task_definition_arn != expected_task_definition_arn
    ):
        _reject("task identity does not match the expected AWS boundary")
    return task_arn


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        cluster_arn, task_definition_arn, task_arn_pattern = _validate_expected_identity(args)
        response = _read_response()
        task_arn = _admit_task_arn(
            response,
            expected_cluster_arn=cluster_arn,
            expected_task_definition_arn=task_definition_arn,
            task_arn_pattern=task_arn_pattern,
        )
    except AdmissionError as exc:
        sys.stderr.write(f"validate-ecs-run-task: refused {exc}\n")
        return 1
    sys.stdout.write(f"{task_arn}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
