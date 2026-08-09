"""Offline IAM vocabulary, module coverage, and known-condition regression gates."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE = REPO_ROOT / "deploy" / "aws-ecs" / "terraform"
IAM_DIR = PACKAGE / "iam"
MODULE_DIR = PACKAGE / "modules" / "scenario"
FIXTURES = REPO_ROOT / "tests" / "fixtures"
VOCABULARY = FIXTURES / "aws_iam_action_vocabulary_2026-08-09.jsonl"
MODULE_ACTION_ORACLE = FIXTURES / "aws_ecs_module_action_oracle.json"

_TEMPLATE_VALUES = {
    "aws_account_id": "123456789012",
    "aws_region": "ap-southeast-1",
    "run_id": "12345678-1234-4123-8123-123456789abc",
    "backend_state_bucket": "elspeth-state-example",
    "ecr_repository": "elspeth-web-example",
    "cloudwatch_agent_ecr_repository": "elspeth-agent-example",
    "gateway_ecr_repository": "elspeth-gateway-example",
    "scenario_a_namespace": "a-0123456789abcdefabcd",
    "scenario_b_namespace": "b-0123456789abcdefabcd",
    "scenario_c_namespace": "c-0123456789abcdefabcd",
    "scenario_a_bucket": "elspeth-a-example",
    "scenario_b_bucket": "elspeth-b-example",
    "scenario_c_bucket": "elspeth-c-example",
    "iam_permissions_boundary_arn": ("arn:aws:iam::123456789012:policy/elspeth-12345678-1234-4123-8123-123456789abc-ecs-boundary"),
}
_ACTION_PATTERN = re.compile(r"^(?P<service>[a-z0-9-]+):(?P<name>[A-Za-z0-9*?]+)$")
_ISSUE_PATTERN = re.compile(r"^elspeth-[0-9a-f]{10}$")
_RESOURCE_DECLARATION = re.compile(r'^resource\s+"(?P<resource_type>aws_[a-z0-9_]+)"', re.MULTILINE)
_REQUEST_TAG = "aws:RequestTag/ACCEPTANCE_RUN_ID"
_RESOURCE_TAG = "aws:ResourceTag/ACCEPTANCE_RUN_ID"


def _render_policy_documents() -> dict[str, dict[str, Any]]:
    documents: dict[str, dict[str, Any]] = {}
    for path in sorted(IAM_DIR.glob("*.json.tftpl")):
        rendered = path.read_text(encoding="utf-8")
        for name, value in _TEMPLATE_VALUES.items():
            rendered = rendered.replace(f"${{{name}}}", value)
        assert "${" not in rendered, f"unrendered substitution in {path.name}"
        documents[path.name] = cast(dict[str, Any], json.loads(rendered))
    return documents


def _statements() -> list[dict[str, Any]]:
    return [
        cast(dict[str, Any], statement)
        for document in _render_policy_documents().values()
        for statement in cast(list[object], document["Statement"])
    ]


def _statement_actions(statement: dict[str, Any]) -> list[str]:
    actions = statement["Action"]
    if isinstance(actions, str):
        return [actions]
    assert isinstance(actions, list)
    assert all(isinstance(action, str) for action in actions)
    return cast(list[str], actions)


def _granted_action_patterns() -> set[str]:
    return {action for statement in _statements() if statement["Effect"] == "Allow" for action in _statement_actions(statement)}


def _all_action_patterns() -> set[str]:
    return {action for statement in _statements() for action in _statement_actions(statement)}


def _load_vocabulary() -> tuple[dict[str, Any], dict[str, set[str]]]:
    records = [cast(dict[str, Any], json.loads(line)) for line in VOCABULARY.read_text(encoding="utf-8").splitlines()]
    assert records
    metadata, *services = records
    vocabulary: dict[str, set[str]] = {}
    for record in services:
        service = record["service"]
        actions = record["actions"]
        assert isinstance(service, str)
        assert isinstance(actions, list)
        assert all(isinstance(action, str) for action in actions)
        vocabulary[service] = set(cast(list[str], actions))
    return metadata, vocabulary


def _action_is_granted(action: str, patterns: set[str]) -> bool:
    return any(fnmatchcase(action, pattern) for pattern in patterns)


def _statements_for_action(action: str) -> list[dict[str, Any]]:
    return [
        statement
        for statement in _statements()
        if statement["Effect"] == "Allow" and _action_is_granted(action, set(_statement_actions(statement)))
    ]


def _condition_keys(statement: dict[str, Any]) -> set[str]:
    condition = statement.get("Condition", {})
    assert isinstance(condition, dict)
    return {
        key
        for operator_values in condition.values()
        if isinstance(operator_values, dict)
        for key in operator_values
        if isinstance(key, str)
    }


def _statement_targets_task_definitions(statement: dict[str, Any]) -> bool:
    resources = statement["Resource"]
    if isinstance(resources, str):
        resources = [resources]
    assert isinstance(resources, list)
    assert all(isinstance(resource, str) for resource in resources)
    return any(resource == "*" or ":task-definition/" in resource for resource in resources)


def _has_exclusive_tag_arm(action: str, *, required: str, excluded: str) -> bool:
    return any(
        required in _condition_keys(statement) and excluded not in _condition_keys(statement)
        for statement in _statements_for_action(action)
    )


def test_vendored_iam_action_vocabulary_covers_every_policy_service_and_action() -> None:
    metadata, vocabulary = _load_vocabulary()
    patterns = _all_action_patterns()
    policy_services = set()
    malformed_patterns: list[str] = []
    for pattern in patterns:
        match = _ACTION_PATTERN.fullmatch(pattern)
        if match is None:
            malformed_patterns.append(pattern)
            continue
        policy_services.add(match.group("service"))

    assert not malformed_patterns
    assert metadata["schema"] == "elspeth.aws-iam-action-vocabulary.v1"
    assert metadata["captured_on"] == "2026-08-09"
    assert metadata["catalogue_schema_version"] == "v1.4"
    assert metadata["catalogue_documentation"] == (
        "https://docs.aws.amazon.com/service-authorization/latest/reference/service-reference.html"
    )
    assert metadata["catalogue_index"] == "https://servicereference.us-east-1.amazonaws.com/"
    assert metadata["refresh_procedure"] == "tests/fixtures/aws_iam_action_vocabulary.md"
    assert set(vocabulary) == policy_services

    records = [cast(dict[str, Any], json.loads(line)) for line in VOCABULARY.read_text(encoding="utf-8").splitlines()[1:]]
    services = [cast(str, record["service"]) for record in records]
    assert services == sorted(set(services))
    for record in records:
        service = cast(str, record["service"])
        actions = cast(list[str], record["actions"])
        assert record["source_url"] == f"https://servicereference.us-east-1.amazonaws.com/v1/{service}/{service}.json"
        assert record["version"] == metadata["catalogue_schema_version"]
        assert actions == sorted(set(actions))
        assert actions
        assert all(re.fullmatch(r"[A-Z][A-Za-z0-9]+", action) for action in actions)

    escapes = cast(list[dict[str, str]], metadata["escapes"])
    escaped_actions: set[str] = set()
    captured_on = date.fromisoformat(cast(str, metadata["captured_on"]))
    for escape in escapes:
        action = escape["action"]
        assert "*" not in action and "?" not in action
        assert _ACTION_PATTERN.fullmatch(action)
        assert _ISSUE_PATTERN.fullmatch(escape["issue"])
        added_on = date.fromisoformat(escape["added_on"])
        review_by = date.fromisoformat(escape["review_by"])
        assert captured_on <= added_on <= review_by
        assert datetime.now(tz=UTC).date() <= review_by, f"expired vocabulary escape for {action}: {escape['issue']}"
        assert escape["rationale"].strip()
        assert action in patterns
        escaped_actions.add(action)

    unknown_literals: set[str] = set()
    catalogue_misses: set[str] = set()
    unmatched_wildcards: set[str] = set()
    for pattern in patterns:
        match = _ACTION_PATTERN.fullmatch(pattern)
        assert match is not None
        service = match.group("service")
        name = match.group("name")
        if "*" in name or "?" in name:
            if not any(fnmatchcase(action, name) for action in vocabulary[service]):
                unmatched_wildcards.add(pattern)
        elif name not in vocabulary[service]:
            catalogue_misses.add(pattern)
            if pattern not in escaped_actions:
                unknown_literals.add(pattern)

    assert not unmatched_wildcards, f"wildcards match no real IAM action: {sorted(unmatched_wildcards)}"
    assert not unknown_literals, f"unknown IAM action literals: {sorted(unknown_literals)}"
    assert len(escaped_actions) == len(escapes)
    assert escaped_actions == catalogue_misses


def test_every_module_aws_resource_type_has_reviewed_actions_and_rendered_grants() -> None:
    oracle = cast(dict[str, Any], json.loads(MODULE_ACTION_ORACLE.read_text(encoding="utf-8")))
    reviewed = cast(dict[str, list[str]], oracle["resources"])
    declared = {
        match.group("resource_type")
        for path in sorted(MODULE_DIR.glob("*.tf"))
        for match in _RESOURCE_DECLARATION.finditer(path.read_text(encoding="utf-8"))
    }

    assert oracle["schema"] == "elspeth.aws-ecs-module-action-oracle.v1"
    assert oracle["reviewed_on"] == "2026-08-09"
    assert len(cast(list[str], oracle["review_basis"])) == 3
    assert len(cast(list[str], oracle["limitations"])) >= 3
    assert set(reviewed) == declared
    assert all(actions == sorted(set(actions)) and actions for actions in reviewed.values())
    assert all(
        _ACTION_PATTERN.fullmatch(action) and "*" not in action and "?" not in action for actions in reviewed.values() for action in actions
    )
    _metadata, vocabulary = _load_vocabulary()
    for actions in reviewed.values():
        for action in actions:
            service, name = action.split(":", maxsplit=1)
            assert name in vocabulary[service], f"oracle names no real IAM action: {action}"

    patterns = _granted_action_patterns()
    missing = {
        resource_type: [action for action in actions if not _action_is_granted(action, patterns)]
        for resource_type, actions in reviewed.items()
    }
    missing = {resource_type: actions for resource_type, actions in missing.items() if actions}
    assert not missing, f"module resource lifecycle actions missing from rendered IAM grants: {missing}"


def test_known_parent_resource_create_actions_have_request_and_resource_tag_arms() -> None:
    # These module-declared creates authorize both the new child and an existing
    # tagged VPC. This is the exact D11 trap; it is not a general IAM solver.
    actions = {
        "ec2:CreateRouteTable",
        "ec2:CreateSecurityGroup",
        "ec2:CreateSubnet",
    }
    assert all(_has_exclusive_tag_arm(action, required=_REQUEST_TAG, excluded=_RESOURCE_TAG) for action in actions)
    assert all(_has_exclusive_tag_arm(action, required=_RESOURCE_TAG, excluded=_REQUEST_TAG) for action in actions)


def test_known_tagless_authorization_paths_have_an_arm_without_resource_tags() -> None:
    # ECS does not publish task-definition tags to DeregisterTaskDefinition.
    assert any(
        not {_REQUEST_TAG, _RESOURCE_TAG}.intersection(_condition_keys(statement))
        for statement in _statements_for_action("ecs:DeregisterTaskDefinition")
    )

    # Tag drift on task definitions must have an authorization arm that does
    # not depend on a task-definition ResourceTag being present in context.
    for action in ("ecs:TagResource", "ecs:UntagResource"):
        assert any(
            _statement_targets_task_definitions(statement) and not {_REQUEST_TAG, _RESOURCE_TAG}.intersection(_condition_keys(statement))
            for statement in _statements_for_action(action)
        )

    statements = {cast(str, statement["Sid"]): statement for statement in _statements()}
    orphan_logs = statements["DeleteUntaggedContainerInsightsOrphanLogGroups"]
    assert not _condition_keys(orphan_logs)

    # The EFS-created ENI is untagged. D13 remains contingent, but if either
    # EC2 ENI mutation is ever granted it must gain a region-only/tagless arm.
    for action in ("ec2:CreateNetworkInterface", "ec2:DeleteNetworkInterface"):
        matching = _statements_for_action(action)
        if matching:
            assert any(not {_REQUEST_TAG, _RESOURCE_TAG}.intersection(_condition_keys(statement)) for statement in matching)


def test_known_create_and_update_apis_have_request_and_resource_tag_arms() -> None:
    # Regression protection for the three dual-purpose APIs established by
    # the RCA. It cannot discover a newly introduced dual-purpose API.
    actions = {
        "acm:ImportCertificate",
        "cloudwatch:PutMetricAlarm",
        "events:PutRule",
    }
    assert all(_has_exclusive_tag_arm(action, required=_REQUEST_TAG, excluded=_RESOURCE_TAG) for action in actions)
    assert all(_has_exclusive_tag_arm(action, required=_RESOURCE_TAG, excluded=_REQUEST_TAG) for action in actions)
