"""Scenario C least-privilege contracts for the AWS ECS Terraform package."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE = REPO_ROOT / "deploy" / "aws-ecs" / "terraform"

ACCOUNT_ID = "123456789012"
REGION = "ap-southeast-1"
RUN_ID = "12345678-1234-4123-8123-123456789abc"
NAMESPACES = {
    "a": "a-0123456789abcdefabcd",
    "b": "b-0123456789abcdefabcd",
    "c": "c-0123456789abcdefabcd",
}


def _text(relative: str) -> str:
    return (PACKAGE / relative).read_text(encoding="utf-8")


def _render_installer_policies(*, maximum_names: bool = False) -> dict[str, dict[str, Any]]:
    suffixes = {"a": "a", "b": "b", "c": "c"}
    if maximum_names:
        suffixes = {"a": "c", "b": "d", "c": "e"}
    values = {
        "aws_account_id": ACCOUNT_ID,
        "aws_region": REGION,
        "run_id": RUN_ID,
        "backend_state_bucket": "elspeth-" + ("s" * 55) if maximum_names else "elspeth-state-example",
        "ecr_repository": "elspeth-" + ("a" * 248) if maximum_names else "elspeth-web-example",
        "cloudwatch_agent_ecr_repository": ("elspeth-" + ("b" * 248) if maximum_names else "elspeth-agent-example"),
        "gateway_ecr_repository": "elspeth-" + ("g" * 248) if maximum_names else "elspeth-gateway-example",
    }
    for scenario, namespace in NAMESPACES.items():
        values[f"scenario_{scenario}_namespace"] = namespace
        values[f"scenario_{scenario}_bucket"] = (
            f"elspeth-{scenario}-" + (suffixes[scenario] * 33) if maximum_names else f"elspeth-{scenario}-example"
        )

    documents: dict[str, dict[str, Any]] = {}
    for path in sorted((PACKAGE / "iam").glob("installer-*.json.tftpl")):
        rendered = path.read_text(encoding="utf-8")
        for name, value in values.items():
            rendered = rendered.replace(f"${{{name}}}", value)
        assert "${" not in rendered, f"unrendered substitution in {path.name}"
        documents[path.name] = json.loads(rendered)
    return documents


def _statements(documents: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {statement["Sid"]: statement for document in documents.values() for statement in document["Statement"]}


def test_installer_policies_authorize_only_the_exact_scenario_c_resources() -> None:
    statements = _statements(_render_installer_policies())
    namespace = NAMESPACES["c"]
    cluster = f"acceptance-{namespace}-cluster"
    service = f"acceptance-{namespace}-service"

    expected_fragments = {
        "RunScenarioTasks": [
            f"task-definition/{namespace}-*:*",
            f"cluster/{cluster}",
        ],
        "StopRunScenarioTasks": [
            f"task/{cluster}/*",
            f"cluster/{cluster}",
        ],
        "ReadScenarioSecretValues": [f"secret:{namespace}-*"],
        "ManageRunScopedInlinePolicies": [
            f"/{namespace}-task-role",
            f"/{namespace}-execution-role",
        ],
        "ManageKnownExecutionRoleAttachment": [f"/{namespace}-execution-role"],
        "PassRunScopedRolesToEcsTasksOnly": [
            f"/{namespace}-task-role",
            f"/{namespace}-execution-role",
        ],
        "DeleteUntaggedContainerInsightsOrphanLogGroups": [f"containerinsights/{cluster}/performance"],
        "MutateRunTaggedXrayResources": [
            f"group/{namespace}-*/*",
            f"sampling-rule/{namespace}-*",
        ],
        "ManageElspethNamedBuckets": ["arn:aws:s3:::elspeth-c-example"],
        "ManageElspethNamedBucketObjects": ["arn:aws:s3:::elspeth-c-example/*"],
        "PushAndCleanElspethImages": [
            "repository/elspeth-gateway-example",
        ],
        "TagNamedEcrRepositoriesOnCreate": [
            "repository/elspeth-gateway-example",
        ],
        "ManageRunEventTargets": [f"rule/{namespace}-*"],
        "ReadScenarioEcsResources": [
            f"cluster/{cluster}",
            f"service/{cluster}/{service}",
            f"task-definition/{namespace}-*:*",
            f"task/{cluster}/*",
        ],
        "ReadRunLogs": [
            f"log-group:/aws/ecs/{namespace}-*:log-stream:*",
            f"containerinsights/{cluster}/performance:log-stream:*",
        ],
        "ManageRunDashboards": [f"dashboard/{namespace}-elspeth-aws-operator-v1"],
    }

    for sid, fragments in expected_fragments.items():
        serialized = json.dumps(statements[sid], sort_keys=True)
        for fragment in fragments:
            assert fragment in serialized, f"{sid} is missing exact Scenario C scope {fragment}"

    rendered = json.dumps(statements, sort_keys=True)
    for generic in ("/c-*", "group/c-*", "rule/c-*", "secret:c-*"):
        assert generic not in rendered


def test_bootstrap_boundary_admits_gateway_pull_and_exact_gateway_secrets() -> None:
    bootstrap = _text("bootstrap/main.tf")
    variables = _text("bootstrap/variables.tf")

    assert 'for scenario_id in ["A", "B", "C"]' in bootstrap
    assert "repository/${var.gateway_ecr_repository}" in bootstrap
    assert 'var.gateway_ecr_repository == "" ? []' in bootstrap
    assert "compact([" in bootstrap
    for variable in (
        "gateway_bearer_secret_arn",
        "gateway_oauth_client_id_secret_arn",
        "gateway_oauth_client_secret_secret_arn",
    ):
        assert f"var.{variable}" in bootstrap
        block = re.search(rf'variable "{variable}" \{{.*?\n\}}\n', variables, re.DOTALL)
        assert block is not None
        assert 'default     = ""' in block.group(0)
    gateway_repository = re.search(r'variable "gateway_ecr_repository" \{.*?\n\}\n', variables, re.DOTALL)
    assert gateway_repository is not None
    assert 'default     = ""' in gateway_repository.group(0)
    assert "Scenario C bootstrap inputs must be either all empty or all set" in variables


def test_scenario_c_repository_and_secret_inputs_are_exactly_bound() -> None:
    repository_contract = (
        r'length(var.{name}) <= 256 && can(regex("^elspeth-[a-z0-9]+((\\.|_|__|-+)[a-z0-9]+)*'
        r'(/[a-z0-9]+((\\.|_|__|-+)[a-z0-9]+)*)*$", var.{name}))'
    )
    repository_variables = {
        "bootstrap/variables.tf": ("ecr_repository", "cloudwatch_agent_ecr_repository", "gateway_ecr_repository"),
        "modules/scenario/variables.tf": (
            "candidate_ecr_repository",
            "cloudwatch_agent_ecr_repository",
            "gateway_ecr_repository",
        ),
        "scenario-c/variables.tf": (
            "candidate_ecr_repository",
            "cloudwatch_agent_ecr_repository",
            "gateway_ecr_repository",
        ),
    }
    for relative, names in repository_variables.items():
        text = _text(relative)
        for name in names:
            block = re.search(rf'variable "{name}" \{{.*?\n\}}\n', text, re.DOTALL)
            assert block is not None
            expected = repository_contract.format(name=name)
            if relative == "bootstrap/variables.tf" and name == "gateway_ecr_repository":
                assert expected in block.group(0)
            else:
                assert expected in block.group(0)

    exact_secret_pattern = (
        r"^arn:aws:secretsmanager:${var.aws_region}:${var.aws_account_id}:"
        r"secret:[A-Za-z0-9/_+=.@-]{1,519}$"
    )
    for relative in ("bootstrap/variables.tf", "modules/scenario/variables.tf", "scenario-c/variables.tf"):
        text = _text(relative)
        for name in (
            "gateway_bearer_secret_arn",
            "gateway_oauth_client_id_secret_arn",
            "gateway_oauth_client_secret_secret_arn",
        ):
            block = re.search(rf'variable "{name}" \{{.*?\n\}}\n', text, re.DOTALL)
            assert block is not None
            assert exact_secret_pattern in block.group(0)


def test_scenario_c_iam_documents_stay_within_managed_policy_limit() -> None:
    documents = _render_installer_policies(maximum_names=True)
    sizes = {name: len(json.dumps(document, separators=(",", ":"))) for name, document in documents.items()}
    assert len(documents) == 5
    assert all(size <= 6_144 for size in sizes.values()), sizes

    lifecycle = _text("iam/lifecycle-policy.json.tftpl")
    for suffix in ("task-role", "execution-role"):
        assert f"c-*-{suffix}" in lifecycle


def test_custom_gateway_task_role_keeps_the_bedrock_surface_absent() -> None:
    task_policy = _text("modules/scenario/iam_observability.tf")
    bedrock_statements = re.findall(
        r'dynamic "statement" \{(?P<body>.*?bedrock:.*?)\n  \}',
        task_policy,
        re.DOTALL,
    )
    assert bedrock_statements
    assert all("for_each = local.bedrock_backend ? [1] : []" in body for body in bedrock_statements)


def test_operator_render_guidance_names_every_scenario_c_input() -> None:
    for relative in ("README.md",):
        text = _text(relative)
        for name in (
            "gateway_ecr_repository",
            "gateway_bearer_secret_arn",
            "gateway_oauth_client_id_secret_arn",
            "gateway_oauth_client_secret_secret_arn",
            "scenario_c_namespace",
            "scenario_c_bucket",
        ):
            assert f"${{{name}}}" in text or f"export {name}=" in text

    runbook = (REPO_ROOT / "docs" / "runbooks" / "aws-ecs-cold-install.md").read_text(encoding="utf-8")
    for name in (
        "gateway_ecr_repository",
        "gateway_bearer_secret_arn",
        "gateway_oauth_client_id_secret_arn",
        "gateway_oauth_client_secret_secret_arn",
        "scenario_c_namespace",
        "scenario_c_bucket",
    ):
        assert f"${{{name}}}" in runbook or f"export {name}=" in runbook
