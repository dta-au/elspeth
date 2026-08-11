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
_RESOURCE_BLOCK_START = re.compile(r'^resource "(?P<resource_type>aws_[a-z0-9_]+)" "(?P<resource_name>[a-z0-9_]+)" \{$')
_PROVIDER_ASSIGNMENT = re.compile(r"^\s*provider\s*=\s*(?P<provider>aws(?:\.[a-z0-9_]+)?)\s*$")
_REQUEST_TAG = "aws:RequestTag/ACCEPTANCE_RUN_ID"
_RESOURCE_TAG = "aws:ResourceTag/ACCEPTANCE_RUN_ID"

# Actions the RCA established as dual-purpose: each authorizes both the
# resource being created and a pre-existing run-tagged parent, so each needs
# a second arm keyed on the resource tag. The value is the Sid of that arm.
# Every entry is proved against the rendered policy by
# test_every_adjudicated_dual_purpose_action_has_both_tag_arms, which reads the
# whole map and not a filtered slice: recording an action here without adding
# its arm must be red, because this map is also what the RequestTag surface
# below is derived from, and an unproved entry would otherwise discharge both.
_DUAL_PURPOSE_PARENT_ARMS = {
    "ec2:CreateRouteTable": "CreateChildrenOfRunTaggedVpc",
    "ec2:CreateSecurityGroup": "CreateChildrenOfRunTaggedVpc",
    "ec2:CreateSubnet": "CreateChildrenOfRunTaggedVpc",
    "acm:ImportCertificate": "MutateRunTaggedRegionalResources",
    "cloudwatch:PutMetricAlarm": "MutateRunTaggedRegionalResources",
    "events:PutRule": "MutateRunTaggedRegionalResources",
}

# The complete set of actions granted under an aws:RequestTag condition.
# Pinned so a newly granted create cannot reach an operator un-adjudicated;
# see test_every_request_tag_conditioned_action_is_adjudicated for what to do
# when this assertion fires. Membership here is not a claim that an action is
# single-purpose — only that its tag arms were looked at.
_REQUEST_TAG_CONDITIONED_ACTIONS = frozenset(_DUAL_PURPOSE_PARENT_ARMS) | {
    "bedrock:CreateGuardrail",
    "cognito-idp:CreateUserPool",
    "ec2:AuthorizeSecurityGroupEgress",
    "ec2:AuthorizeSecurityGroupIngress",
    "ec2:CreateInternetGateway",
    "ec2:CreateTags",
    "ec2:CreateVpc",
    "ecr:CreateRepository",
    "ecr:TagResource",
    "ecs:CreateCluster",
    "ecs:CreateService",
    "ecs:RegisterTaskDefinition",
    "elasticfilesystem:CreateAccessPoint",
    "elasticfilesystem:CreateFileSystem",
    "elasticloadbalancing:AddTags",
    "elasticloadbalancing:CreateListener",
    "elasticloadbalancing:CreateLoadBalancer",
    "elasticloadbalancing:CreateRule",
    "elasticloadbalancing:CreateTargetGroup",
    "iam:CreatePolicy",
    "iam:CreateRole",
    "iam:TagPolicy",
    "iam:TagRole",
    "logs:CreateLogGroup",
    "rds:CreateDBCluster",
    "rds:CreateDBInstance",
    "rds:CreateDBSubnetGroup",
    "secretsmanager:CreateSecret",
    "sns:CreateTopic",
    "xray:CreateGroup",
    "xray:CreateSamplingRule",
}

# Wildcard action patterns the policies grant. Pinned for the same reason as
# the set above: a wildcard is the broadest grant in the file, and one added
# quietly widens the installer principal without any single line looking new.
_WILDCARD_ACTION_PATTERNS = frozenset(
    {
        "cognito-idp:Describe*",
        "cognito-idp:List*",
        "ec2:Describe*",
        "ecr:Describe*",
        "ecr:List*",
        "ecs:Describe*",
        "elasticfilesystem:Describe*",
        "elasticloadbalancing:Describe*",
        "events:List*",
        "iam:Get*",
        "iam:List*",
        "logs:Describe*",
        "logs:List*",
        "rds:Describe*",
        "s3:GetBucket*",
        "secretsmanager:List*",
        "sns:List*",
    }
)


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


def _statements_by_sid() -> dict[str, dict[str, Any]]:
    statements = _statements()
    sids: list[str] = []
    for statement in statements:
        sid = statement.get("Sid")
        assert isinstance(sid, str) and sid
        sids.append(sid)
    duplicates = sorted(sid for sid in set(sids) if sids.count(sid) > 1)
    assert not duplicates, f"duplicate IAM statement Sids: {duplicates}"
    return {cast(str, statement["Sid"]): statement for statement in statements}


def _statement_actions(statement: dict[str, Any]) -> list[str]:
    actions = statement["Action"]
    if isinstance(actions, str):
        return [actions]
    assert isinstance(actions, list)
    assert all(isinstance(action, str) for action in actions)
    return cast(list[str], actions)


def _action_patterns_for_policies(*, policy_names: set[str], effect: str) -> set[str]:
    documents = _render_policy_documents()
    return {
        action
        for name in policy_names
        for statement in cast(list[dict[str, Any]], documents[name]["Statement"])
        if statement["Effect"] == effect
        for action in _statement_actions(statement)
    }


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


def _action_matches(action: str, patterns: set[str]) -> bool:
    return any(fnmatchcase(action, pattern) for pattern in patterns)


def _resource_provider_aliases(module: str) -> dict[tuple[str, str], str]:
    aliases: dict[tuple[str, str], str] = {}
    resource: tuple[str, str] | None = None
    provider = "aws"
    for line in module.splitlines():
        if resource is None:
            match = _RESOURCE_BLOCK_START.fullmatch(line)
            if match is not None:
                resource = (match.group("resource_type"), match.group("resource_name"))
                provider = "aws"
            continue
        provider_match = _PROVIDER_ASSIGNMENT.fullmatch(line)
        if provider_match is not None:
            assert provider == "aws", f"multiple provider assignments for {resource}"
            provider = provider_match.group("provider")
        if line == "}":
            assert resource not in aliases
            aliases[resource] = provider
            resource = None
    assert resource is None, f"unterminated Terraform resource block: {resource}"
    return aliases


def _condition_tag_keys(statement: dict[str, Any]) -> set[str]:
    """Condition keys a statement tests, across every condition operator.

    Read structurally rather than by substring so that an ARN or a tag VALUE
    containing the literal `aws:RequestTag/...` cannot be mistaken for the
    statement testing that key. Callers own the two narrowings this does not
    do: it reports keys for Deny statements as readily as Allow, and it does
    not distinguish operator semantics, so a `ForAllValues:`-prefixed or
    `Null` operator is reported like any other. The policies use only
    StringEquals, ArnEquals and ArnLike today; filtering for shapes that do
    not exist would be speculative.
    """
    condition = statement.get("Condition", {})
    assert isinstance(condition, dict)
    keys: set[str] = set()
    for operands in cast(dict[str, Any], condition).values():
        assert isinstance(operands, dict)
        keys.update(cast(dict[str, Any], operands))
    return keys


def _statements_for_action(action: str) -> list[dict[str, Any]]:
    return [
        statement
        for statement in _statements()
        if statement["Effect"] == "Allow" and _action_matches(action, set(_statement_actions(statement)))
    ]


def _assert_exact_tag_arm(*, action: str, sid: str, tag_key: str, resource: str) -> None:
    statement = _statements_by_sid()[sid]
    assert statement["Effect"] == "Allow"
    assert _action_matches(action, set(_statement_actions(statement)))
    assert statement["Resource"] == resource
    assert statement["Condition"] == {
        "StringEquals": {
            tag_key: _TEMPLATE_VALUES["run_id"],
            "aws:RequestedRegion": _TEMPLATE_VALUES["aws_region"],
        }
    }


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


def test_granted_wildcard_action_patterns_are_pinned() -> None:
    """A new wildcard grant cannot arrive without review.

    The vocabulary test above checks a wildcard matches SOMETHING real, which
    is a far weaker claim than it looks and cannot stand in for coverage:
    `s3:GetBucket*` — the original defect — matches GetBucketAcl and friends,
    so it passes there while covering none of the seven de-prefixed S3 actions
    the module needs, `s3:GetAccelerateConfiguration` among them. Only the
    module coverage oracle catches that shape, and only for actions its
    reviewed set already names. What this pin adds is narrower and worth
    stating exactly: the breadth of the wildcard surface itself is fixed.
    """
    wildcards: set[str] = set()
    for pattern in _all_action_patterns():
        match = _ACTION_PATTERN.fullmatch(pattern)
        assert match is not None, f"malformed IAM action pattern: {pattern}"
        name = match.group("name")
        if "*" in name or "?" in name:
            wildcards.add(pattern)

    assert wildcards == _WILDCARD_ACTION_PATTERNS, (
        "the set of wildcard action patterns has changed.\n"
        f"  newly granted: {sorted(wildcards - _WILDCARD_ACTION_PATTERNS)}\n"
        f"  no longer granted: {sorted(_WILDCARD_ACTION_PATTERNS - wildcards)}\n"
        "A wildcard grants every present and FUTURE action matching it, including actions AWS "
        "has not shipped yet. Confirm the breadth is intended and that the specific actions the "
        "module needs are named literally where they must be provable, then update the set."
    )


def test_rendered_policy_statement_sids_are_nonempty_and_globally_unique() -> None:
    assert len(_statements_by_sid()) == len(_statements())


def test_every_module_aws_resource_type_has_reviewed_actions_and_rendered_grants() -> None:
    oracle = cast(dict[str, Any], json.loads(MODULE_ACTION_ORACLE.read_text(encoding="utf-8")))
    reviewed = cast(dict[str, list[str]], oracle["resources"])
    provider_sources = cast(dict[str, str], oracle["provider_sources"])
    provider_helper_sources = cast(dict[str, list[str]], oracle["provider_helper_sources"])
    shared_provider_sources = cast(list[str], oracle["shared_provider_sources"])
    declared = {
        match.group("resource_type")
        for path in sorted(MODULE_DIR.glob("*.tf"))
        for match in _RESOURCE_DECLARATION.finditer(path.read_text(encoding="utf-8"))
    }

    assert oracle["schema"] == "elspeth.aws-ecs-module-action-oracle.v2"
    assert oracle["reviewed_on"] == "2026-08-09"
    assert oracle["refresh_procedure"] == "tests/fixtures/aws_ecs_module_action_oracle.md"
    assert (REPO_ROOT / cast(str, oracle["refresh_procedure"])).is_file()
    review_basis = cast(list[str], oracle["review_basis"])
    assert len(review_basis) == 3
    assert all("docs-archive" not in basis for basis in review_basis)
    assert len(cast(list[str], oracle["limitations"])) >= 3
    assert set(reviewed) == declared
    assert set(provider_sources) == declared
    provider = cast(dict[str, Any], oracle["provider"])
    assert provider == {
        "repository": "https://github.com/hashicorp/terraform-provider-aws",
        "tag": "v6.54.0",
        "version": "6.54.0",
        "commit": "8ca5b8dac50747f51e02a122d058f6eebd58ba19",
        "lockfiles": [
            "deploy/aws-ecs/terraform/bootstrap/.terraform.lock.hcl",
            "deploy/aws-ecs/terraform/scenario-a/.terraform.lock.hcl",
            "deploy/aws-ecs/terraform/scenario-b/.terraform.lock.hcl",
            "deploy/aws-ecs/terraform/scenario-c/.terraform.lock.hcl",
        ],
    }
    for lockfile in cast(list[str], provider["lockfiles"]):
        lock_text = (REPO_ROOT / lockfile).read_text(encoding="utf-8")
        assert 'provider "registry.terraform.io/hashicorp/aws"' in lock_text
        assert 'version     = "6.54.0"' in lock_text
    assert all(re.fullmatch(r"internal/service/[a-z0-9]+/[a-z0-9_]+\.go", source) for source in provider_sources.values())
    assert all(".." not in source for source in provider_sources.values())
    assert set(provider_helper_sources).issubset(declared)
    assert all(sources == sorted(set(sources)) and sources for sources in provider_helper_sources.values())
    assert shared_provider_sources == sorted(set(shared_provider_sources))
    recorded_sources = [
        *provider_sources.values(),
        *(source for sources in provider_helper_sources.values() for source in sources),
        *shared_provider_sources,
    ]
    assert all(re.fullmatch(r"internal/(?:service|provider)/[a-z0-9]+/[a-z0-9_]+\.go", source) for source in recorded_sources)
    assert all(".." not in source for source in recorded_sources)
    assert all(actions == sorted(set(actions)) and actions for actions in reviewed.values())
    assert all(
        _ACTION_PATTERN.fullmatch(action) and "*" not in action and "?" not in action for actions in reviewed.values() for action in actions
    )
    _metadata, vocabulary = _load_vocabulary()
    for actions in reviewed.values():
        for action in actions:
            service, name = action.split(":", maxsplit=1)
            assert name in vocabulary[service], f"oracle names no real IAM action: {action}"

    policy_sets = {alias: set(names) for alias, names in cast(dict[str, list[str]], oracle["policy_sets"]).items()}
    assert set().union(*policy_sets.values()) == set(_render_policy_documents())
    assert sum(len(names) for names in policy_sets.values()) == len(set().union(*policy_sets.values()))
    default_alias = cast(str, oracle["default_provider_alias"])
    alias_overrides = cast(dict[str, str], oracle["provider_alias_overrides"])
    assert default_alias in policy_sets
    assert set(alias_overrides).issubset(declared)
    assert set(alias_overrides.values()).issubset(policy_sets)
    iam_module = (MODULE_DIR / "iam_observability.tf").read_text(encoding="utf-8")
    assert alias_overrides == {"aws_iam_role": "aws.iam_lifecycle"}
    aliases = _resource_provider_aliases(iam_module)
    assert {resource: alias for resource, alias in aliases.items() if resource[0].startswith("aws_iam_role")} == {
        ("aws_iam_role", "task"): "aws.iam_lifecycle",
        ("aws_iam_role", "execution"): "aws.iam_lifecycle",
        ("aws_iam_role_policy_attachment", "execution_managed"): "aws",
        ("aws_iam_role_policy", "execution_secrets"): "aws",
        ("aws_iam_role_policy", "task"): "aws",
    }
    allowed_by_alias = {alias: _action_patterns_for_policies(policy_names=names, effect="Allow") for alias, names in policy_sets.items()}
    denied_by_alias = {alias: _action_patterns_for_policies(policy_names=names, effect="Deny") for alias, names in policy_sets.items()}
    expected_denies = cast(list[dict[str, Any]], oracle["expected_denies"])
    assert expected_denies == [
        {
            "resource_type": "aws_iam_role",
            "provider_alias": "aws.iam_lifecycle",
            "actions": [
                "iam:DeleteRolePermissionsBoundary",
                "iam:PutRolePermissionsBoundary",
                "iam:UpdateAssumeRolePolicy",
            ],
            "issue": "elspeth-724e9cb5d5",
            "rationale": (
                "The dedicated lifecycle principal must not change a role trust policy or permissions boundary; "
                "replacement-safe drift semantics are tracked separately."
            ),
        }
    ]
    expected_denied_by_resource: dict[str, set[str]] = {}
    for record in expected_denies:
        resource_type = cast(str, record["resource_type"])
        alias = cast(str, record["provider_alias"])
        expected_actions = set(cast(list[str], record["actions"]))
        assert _ISSUE_PATTERN.fullmatch(cast(str, record["issue"]))
        assert alias == alias_overrides[resource_type]
        assert all(not _action_matches(action, allowed_by_alias[alias]) for action in expected_actions)
        assert all(_action_matches(action, denied_by_alias[alias]) for action in expected_actions)
        expected_denied_by_resource[resource_type] = expected_actions

    lifecycle_deny = _statements_by_sid()["DenyRolePermissionMutationOrActivation"]
    assert lifecycle_deny["Effect"] == "Deny"
    assert lifecycle_deny["Resource"] == "*"
    assert "Condition" not in lifecycle_deny
    assert expected_denied_by_resource["aws_iam_role"].issubset(_statement_actions(lifecycle_deny))

    missing = {}
    unexpectedly_denied = {}
    for resource_type, actions in reviewed.items():
        alias = alias_overrides.get(resource_type, default_alias)
        expected = expected_denied_by_resource.get(resource_type, set())
        absent = [action for action in actions if action not in expected and not _action_matches(action, allowed_by_alias[alias])]
        denied = [action for action in actions if action not in expected and _action_matches(action, denied_by_alias[alias])]
        if absent:
            missing[resource_type] = absent
        if denied:
            unexpectedly_denied[resource_type] = denied
    assert not missing and not unexpectedly_denied, (
        f"module lifecycle actions missing from provider-alias grants: {missing}; "
        f"unexpectedly denied by provider-alias policy: {unexpectedly_denied}"
    )


def test_pinned_provider_review_findings_remain_in_the_oracle() -> None:
    oracle = cast(dict[str, Any], json.loads(MODULE_ACTION_ORACLE.read_text(encoding="utf-8")))
    reviewed = {resource_type: set(actions) for resource_type, actions in cast(dict[str, list[str]], oracle["resources"]).items()}
    required = {
        "aws_bedrock_guardrail_version": {"bedrock:DeleteGuardrail"},
        "aws_cognito_user_pool": {"cognito-idp:GetUserPoolMfaConfig"},
        "aws_ecs_cluster": {"ecs:UpdateCluster"},
        "aws_efs_mount_target": {
            "ec2:DescribeSubnets",
            "elasticfilesystem:DescribeMountTargetSecurityGroups",
            "elasticfilesystem:ModifyMountTargetSecurityGroups",
        },
        "aws_iam_role": {
            "iam:GetRolePolicy",
            "iam:ListAttachedRolePolicies",
            "iam:ListInstanceProfilesForRole",
            "iam:ListRolePolicies",
            "iam:PutRolePermissionsBoundary",
            "iam:UpdateAssumeRolePolicy",
        },
        "aws_lb": {"elasticloadbalancing:SetSecurityGroups", "elasticloadbalancing:SetSubnets"},
        "aws_lb_listener": {"elasticloadbalancing:DescribeListenerAttributes"},
        "aws_lb_listener_rule": {"elasticloadbalancing:SetRulePriorities"},
        "aws_rds_cluster": {"rds:DescribeGlobalClusters"},
        "aws_rds_cluster_instance": {"rds:DescribeDBClusters", "rds:RebootDBInstance"},
        "aws_route_table": {"ec2:DisassociateRouteTable", "ec2:ReplaceRoute"},
        "aws_route_table_association": {"ec2:ReplaceRouteTableAssociation"},
        "aws_s3_bucket": {"s3:PutBucketTagging"},
        "aws_security_group": {"ec2:DescribeNetworkInterfaces"},
        "aws_subnet": {"ec2:DescribeNetworkInterfaces"},
        "aws_vpc": {
            "ec2:DescribeNetworkAcls",
            "ec2:DescribeRouteTables",
            "ec2:DescribeSecurityGroups",
            "ec2:DescribeVpcAttribute",
        },
        "aws_vpc_security_group_egress_rule": {"ec2:ModifySecurityGroupRules"},
        "aws_vpc_security_group_ingress_rule": {"ec2:ModifySecurityGroupRules"},
    }
    excluded = {
        "aws_acm_certificate": {"acm:GetCertificate"},
        "aws_cognito_user_pool": {"cognito-idp:ListTagsForResource"},
        "aws_ecs_cluster": {"ecs:UpdateClusterSettings"},
        "aws_ecs_service": {"ecs:ListTagsForResource"},
        "aws_ecs_task_definition": {"ecs:ListTagsForResource"},
        "aws_iam_role": {"iam:DeleteRolePermissionsBoundary", "iam:ListRoleTags"},
        "aws_lb": {"ec2:DescribeNetworkInterfaces", "elasticloadbalancing:DescribeCapacityReservation"},
        "aws_lb_target_group": {"elasticloadbalancing:DescribeTargetHealth"},
        "aws_rds_cluster": {"rds:ListTagsForResource"},
        "aws_rds_cluster_instance": {"rds:ListTagsForResource"},
        "aws_s3_bucket": {
            "s3:GetAnalyticsConfiguration",
            "s3:GetBucketLocation",
            "s3:GetBucketPublicAccessBlock",
            "s3:GetIntelligentTieringConfiguration",
            "s3:GetInventoryConfiguration",
            "s3:GetMetricsConfiguration",
            "s3:ListAllMyBuckets",
            "s3:ListTagsForResource",
            "s3:TagResource",
            "s3:UntagResource",
        },
        "aws_secretsmanager_secret": {"secretsmanager:ListSecretVersionIds", "secretsmanager:UpdateSecret"},
        "aws_secretsmanager_secret_version": {"secretsmanager:DescribeSecret", "secretsmanager:ListSecretVersionIds"},
        "aws_xray_group": {"xray:GetGroups"},
    }
    for resource_type, actions in required.items():
        assert actions.issubset(reviewed[resource_type])
    for resource_type, actions in excluded.items():
        assert actions.isdisjoint(reviewed[resource_type])


def test_pinned_provider_update_actions_have_reviewed_resource_and_condition_scopes() -> None:
    documents = _render_policy_documents()
    statements = _statements_by_sid()

    run_tagged = statements["MutateRunTaggedRegionalResources"]
    assert {
        "ecs:UpdateCluster",
        "elasticloadbalancing:SetSecurityGroups",
        "elasticloadbalancing:SetSubnets",
        "rds:RebootDBInstance",
    }.issubset(_statement_actions(run_tagged))
    assert run_tagged["Resource"] == "*"
    assert run_tagged["Condition"] == {
        "StringEquals": {
            _RESOURCE_TAG: _TEMPLATE_VALUES["run_id"],
            "aws:RequestedRegion": _TEMPLATE_VALUES["aws_region"],
        }
    }

    relationships = statements["ManageRunEc2NetworkRelationships"]
    assert {"ec2:ModifySecurityGroupRules", "ec2:ReplaceRoute", "ec2:ReplaceRouteTableAssociation"}.issubset(
        _statement_actions(relationships)
    )
    assert relationships["Resource"] == [
        "arn:aws:ec2:ap-southeast-1:123456789012:internet-gateway/*",
        "arn:aws:ec2:ap-southeast-1:123456789012:route-table/*",
        "arn:aws:ec2:ap-southeast-1:123456789012:security-group/*",
        "arn:aws:ec2:ap-southeast-1:123456789012:security-group-rule/*",
        "arn:aws:ec2:ap-southeast-1:123456789012:subnet/*",
        "arn:aws:ec2:ap-southeast-1:123456789012:vpc/*",
    ]
    assert relationships["Condition"] == {
        "StringEquals": {
            _RESOURCE_TAG: _TEMPLATE_VALUES["run_id"],
            "aws:RequestedRegion": _TEMPLATE_VALUES["aws_region"],
        }
    }

    tagless = statements["MutateRegionScopedProviderResources"]
    assert _statement_actions(tagless) == [
        "elasticfilesystem:ModifyMountTargetSecurityGroups",
        "elasticloadbalancing:SetRulePriorities",
    ]
    assert tagless["Resource"] == [
        "arn:aws:elasticfilesystem:ap-southeast-1:123456789012:file-system/*",
        "arn:aws:elasticloadbalancing:ap-southeast-1:123456789012:listener-rule/app/a-0123456789abcdefabcd-alb/*/*/*",
        "arn:aws:elasticloadbalancing:ap-southeast-1:123456789012:listener-rule/app/b-0123456789abcdefabcd-alb/*/*/*",
        "arn:aws:elasticloadbalancing:ap-southeast-1:123456789012:listener-rule/app/c-0123456789abcdefabcd-alb/*/*/*",
    ]
    assert tagless["Condition"] == {"StringEquals": {"aws:RequestedRegion": _TEMPLATE_VALUES["aws_region"]}}
    assert documents["installer-tagless-updates-policy.json.tftpl"]["Statement"] == [
        tagless,
        statements["ListTasksInRunScenarioClusters"],
        statements["ExecuteCommandsInRunScenarioTasks"],
    ]

    discovery = statements["ReadDiscovery"]
    assert "cognito-idp:GetUserPoolMfaConfig" in _statement_actions(discovery)
    assert discovery["Resource"] == "*"
    assert "Condition" not in discovery


def test_every_request_tag_conditioned_action_is_adjudicated() -> None:
    """Every action granted under `aws:RequestTag` carries a recorded verdict.

    A `aws:RequestTag` condition authorizes the resource being created. The
    D11 trap is an action that ALSO authorizes against a pre-existing parent
    carrying no request tag — `ec2:CreateSubnet` against its VPC — which
    denies at apply under a RequestTag-only grant. The sibling tests below
    pin the six such actions the RCA established, and could only ever pin
    those six: a newly granted dual-purpose action was invisible to this
    suite, which is the gap that let the original corpus ship.

    This test closes that for the RequestTag surface by pinning it whole
    instead of a chosen subset. It does not decide whether a given AWS API is
    dual-purpose — that is a fact about AWS, not about this tree, and no
    offline oracle can settle it. What it guarantees is that adding one to
    this surface cannot be silent.

    Two boundaries, so the green is not read as wider than it is. Create-
    shaped actions granted OUTSIDE a RequestTag condition — in a
    ResourceTag-only statement, or one with no `Condition` at all — are
    adjudicated by nothing here; `s3:CreateBucket` and
    `elasticfilesystem:CreateMountTarget` are in that position today. And
    membership pins the action SET, not the condition SHAPE: only
    `CreateRunTaggedRegionalResources` has its condition contents pinned,
    through the arm assertions.
    """
    granted = {
        action
        for statement in _statements()
        if statement["Effect"] == "Allow" and _REQUEST_TAG in _condition_tag_keys(statement)
        for action in _statement_actions(statement)
    }
    assert granted == _REQUEST_TAG_CONDITIONED_ACTIONS, (
        "the set of actions granted under an aws:RequestTag condition has changed.\n"
        f"  newly granted: {sorted(granted - _REQUEST_TAG_CONDITIONED_ACTIONS)}\n"
        f"  no longer granted: {sorted(_REQUEST_TAG_CONDITIONED_ACTIONS - granted)}\n"
        "For each newly granted action, decide whether the API also authorizes against a "
        "pre-existing resource that carries no request tag (a parent VPC, cluster, load "
        "balancer, file system). If it does, add the aws:ResourceTag/ACCEPTANCE_RUN_ID arm to "
        "the policy AND record it in _DUAL_PURPOSE_PARENT_ARMS — recording it without the arm "
        "is red, because test_every_adjudicated_dual_purpose_action_has_both_tag_arms proves "
        "every entry against the rendered policy. If it does not, add it to the literal set "
        "below. Do not update either set to make the test pass without answering that "
        "question: a wrong answer denies at apply, after terraform has already created "
        "earlier resources."
    )


def test_every_adjudicated_dual_purpose_action_has_both_tag_arms() -> None:
    """Every entry in the adjudication map is proved against the policy.

    Iterates the whole map, never a filtered slice. Filtering by the two
    currently-known resource-tag Sids left an escape hatch exactly where the
    gate is most load-bearing: the adjudication failure message tells an
    author to record a dual-purpose action here, and because
    `_REQUEST_TAG_CONDITIONED_ACTIONS` is derived from this map, doing so
    also satisfied the exact-set pin. An entry naming a Sid outside the
    filtered pair was therefore asserted by nothing — following the
    instruction produced a green suite with the arm absent from the policy
    and a recorded claim that it had been reviewed.

    The size pin is what stops the other direction: driving assertions from
    data means an emptied or shrunken map otherwise passes by having nothing
    left to check.
    """
    assert len(_DUAL_PURPOSE_PARENT_ARMS) == 6, (
        "the adjudicated dual-purpose set changed size. Adding one is expected when a new "
        "dual-purpose API is granted — update this count with it. Losing one means an action "
        "that authorizes against a pre-existing parent is no longer proved to have both arms."
    )
    for action, resource_tag_sid in sorted(_DUAL_PURPOSE_PARENT_ARMS.items()):
        _assert_exact_tag_arm(action=action, sid="CreateRunTaggedRegionalResources", tag_key=_REQUEST_TAG, resource="*")
        _assert_exact_tag_arm(action=action, sid=resource_tag_sid, tag_key=_RESOURCE_TAG, resource="*")


def test_known_tagless_authorization_paths_have_an_arm_without_resource_tags() -> None:
    statements = _statements_by_sid()

    # ECS does not publish task-definition tags to DeregisterTaskDefinition.
    deregister = statements["DeregisterTaskDefinitionsRegionScopedOnly"]
    assert deregister["Effect"] == "Allow"
    assert _statement_actions(deregister) == ["ecs:DeregisterTaskDefinition"]
    assert deregister["Resource"] == "*"
    assert deregister["Condition"] == {"StringEquals": {"aws:RequestedRegion": _TEMPLATE_VALUES["aws_region"]}}

    # Tag drift on task definitions must be region and family scoped without
    # depending on request or resource tags that ECS does not publish.
    task_definition_tags = statements["MutateTaskDefinitionTagsRegionScopedOnly"]
    assert task_definition_tags["Effect"] == "Allow"
    assert _statement_actions(task_definition_tags) == ["ecs:TagResource", "ecs:UntagResource"]
    assert task_definition_tags["Resource"] == [
        "arn:aws:ecs:ap-southeast-1:123456789012:task-definition/a-0123456789abcdefabcd-*:*",
        "arn:aws:ecs:ap-southeast-1:123456789012:task-definition/b-0123456789abcdefabcd-*:*",
        "arn:aws:ecs:ap-southeast-1:123456789012:task-definition/c-0123456789abcdefabcd-*:*",
    ]
    assert task_definition_tags["Condition"] == {"StringEquals": {"aws:RequestedRegion": _TEMPLATE_VALUES["aws_region"]}}

    orphan_logs = statements["DeleteUntaggedContainerInsightsOrphanLogGroups"]
    assert orphan_logs["Effect"] == "Allow"
    assert _statement_actions(orphan_logs) == ["logs:DeleteLogGroup"]
    assert "Condition" not in orphan_logs
    assert orphan_logs["Resource"] == [
        "arn:aws:logs:ap-southeast-1:123456789012:log-group:/aws/ecs/containerinsights/acceptance-a-0123456789abcdefabcd-cluster/performance",
        "arn:aws:logs:ap-southeast-1:123456789012:log-group:/aws/ecs/containerinsights/acceptance-a-0123456789abcdefabcd-cluster/performance:*",
        "arn:aws:logs:ap-southeast-1:123456789012:log-group:/aws/ecs/containerinsights/acceptance-b-0123456789abcdefabcd-cluster/performance",
        "arn:aws:logs:ap-southeast-1:123456789012:log-group:/aws/ecs/containerinsights/acceptance-b-0123456789abcdefabcd-cluster/performance:*",
        "arn:aws:logs:ap-southeast-1:123456789012:log-group:/aws/ecs/containerinsights/acceptance-c-0123456789abcdefabcd-cluster/performance",
        "arn:aws:logs:ap-southeast-1:123456789012:log-group:/aws/ecs/containerinsights/acceptance-c-0123456789abcdefabcd-cluster/performance:*",
    ]

    # The EFS-created ENI is untagged. D13 remains contingent, but if either
    # EC2 ENI mutation is ever granted it must gain a region-only/tagless arm.
    for action in ("ec2:CreateNetworkInterface", "ec2:DeleteNetworkInterface"):
        matching = _statements_for_action(action)
        if matching:
            assert any(
                statement["Resource"] == "*"
                and statement.get("Condition") == {"StringEquals": {"aws:RequestedRegion": _TEMPLATE_VALUES["aws_region"]}}
                for statement in matching
            )


def test_adjudicated_dual_purpose_actions_keep_their_established_arm_grouping() -> None:
    # The RCA established two distinct shapes: creates whose parent is the
    # run-tagged VPC, and create/update APIs that mutate an existing run-tagged
    # regional resource. Arm proof belongs to the test above, which reads every
    # entry; this pins only the grouping, so a silent reclassification between
    # the two shapes is visible in review.
    grouping: dict[str, list[str]] = {}
    for action, resource_tag_sid in sorted(_DUAL_PURPOSE_PARENT_ARMS.items()):
        grouping.setdefault(resource_tag_sid, []).append(action)
    assert grouping == {
        "CreateChildrenOfRunTaggedVpc": ["ec2:CreateRouteTable", "ec2:CreateSecurityGroup", "ec2:CreateSubnet"],
        "MutateRunTaggedRegionalResources": ["acm:ImportCertificate", "cloudwatch:PutMetricAlarm", "events:PutRule"],
    }
