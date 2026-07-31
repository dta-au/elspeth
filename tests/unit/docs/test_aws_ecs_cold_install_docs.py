"""Executable documentation contract for the AWS ECS cold-install path."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "aws-ecs-cold-install.md"
PACKAGE_README = REPO_ROOT / "deploy" / "aws-ecs" / "terraform" / "README.md"
PROJECT_README = REPO_ROOT / "README.md"
DOCS_INDEX = REPO_ROOT / "docs" / "README.md"
RUNBOOK_INDEX = REPO_ROOT / "docs" / "runbooks" / "index.md"
PLATFORM_DOC = REPO_ROOT / "docs" / "reference" / "deployment-platforms.md"
DOCKER_GUIDE = REPO_ROOT / "docs" / "guides" / "docker.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_cold_install_is_linked_from_every_operator_entry_point() -> None:
    assert RUNBOOK.is_file()
    for path in (PROJECT_README, DOCS_INDEX, RUNBOOK_INDEX, PLATFORM_DOC, DOCKER_GUIDE, PACKAGE_README):
        assert "aws-ecs-cold-install.md" in _read(path), path


def test_cold_install_local_links_resolve() -> None:
    targets = re.findall(r"\[[^\]]+\]\(([^)]+)\)", _read(RUNBOOK))
    local_targets = [target.split("#", 1)[0] for target in targets if "://" not in target]

    assert local_targets
    for target in local_targets:
        assert (RUNBOOK.parent / target).resolve().exists(), target


def test_cold_install_orders_privilege_bootstrap_doctors_and_service_admission() -> None:
    text = _read(RUNBOOK)

    identity = text.index("## 1. Select and prove the AWS identity")
    policies = text.index("## 2. Install the installer and lifecycle policies")
    bootstrap = text.index("## 3. Bootstrap remote state and image repositories")
    images = text.index("## 4. Build, publish, and scan both images")
    bedrock = text.index("## 5. Confirm Bedrock inputs")
    scenario = text.index("## 6. Configure Scenario A")
    apply = text.index("## 7. Plan and create the stack")
    doctors = text.index("## 8. Initialize schemas, then prove runtime credentials")
    enable = text.index("## 9. Enable and verify the service")

    assert identity < policies < bootstrap < images < bedrock < scenario < apply < doctors < enable
    schema_doctor = text.index("schema_init_doctor_task_definition_arn", doctors)
    runtime_doctor = text.index("runtime_doctor_task_definition_arn", schema_doctor)
    service_enable = text.index("service_enable_command", runtime_doctor)
    assert schema_doctor < runtime_doctor < service_enable
    assert "Both task exit codes must be `0`" in text


def test_cold_install_covers_complete_runtime_capability() -> None:
    text = _read(RUNBOOK)
    normalized = " ".join(text.split())

    for phrase in (
        "Aurora PostgreSQL",
        "EFS",
        "S3",
        "CloudWatch",
        "X-Ray",
        "Bedrock guardrails",
        "cloudwatch-agent",
        "composer_available",
        'composer_provider == "bedrock"',
        "/api/health",
        "/api/ready",
        "/api/system/status",
    ):
        assert phrase in normalized

    assert '--build-arg INSTALL_EXTRAS="webui llm aws postgres"' in text
    assert "import psycopg2" in text
    assert "CANDIDATE_IMAGE=" in text
    assert "CLOUDWATCH_AGENT_IMAGE=" in text


def test_cold_install_fails_closed_on_identity_images_and_database_admission() -> None:
    text = _read(RUNBOOK)

    for phrase in (
        "set -Eeuo pipefail",
        "umask 077",
        'test "$AWS_ACCOUNT_ID" = "$IAM_ACCOUNT_ID"',
        "allowed_account_ids",
        "image-scan-complete",
        "findingSeverityCounts",
        "postgresql+psycopg://",
        "sslmode=verify-full",
        "runtime doctor",
    ):
        assert phrase in text

    assert "set -x" not in text
    assert "terraform state rm" not in text
    assert " -target" not in text
    assert "latest" not in text.lower()
    assert "Do not paste environment or secret arrays" in text


def test_cold_install_sets_the_agent_repository_for_scenario_a() -> None:
    text = _read(RUNBOOK)
    scenario = text[text.index("## 6. Configure Scenario A") : text.index("## 7. Plan and create the stack")]
    repository_input = '- `cloudwatch_agent_ecr_repository = "$AGENT_REPOSITORY"`;'
    placeholder_gate = "! rg -n 'REPLACE_WITH|2030-01-01'"

    assert repository_input in scenario
    assert scenario.index(repository_input) < scenario.index(placeholder_gate)


def test_cold_install_executes_terraform_service_enable_command() -> None:
    text = _read(RUNBOOK)
    enable = text[text.index("## 9. Enable and verify the service") : text.index("## Troubleshooting")]
    output_command = "terraform -chdir=scenario-a output -raw service_enable_command"
    capture = f'SERVICE_ENABLE_COMMAND="$({output_command})"'
    display = "printf '%s\\n' \"$SERVICE_ENABLE_COMMAND\""
    execute = 'bash -Eeuo pipefail -c "$SERVICE_ENABLE_COMMAND"'

    assert enable.count(output_command) == 1
    assert capture in enable
    assert display in enable
    assert execute in enable
    assert enable.index(capture) < enable.index(display) < enable.index(execute)


def test_cold_install_uses_the_scenario_namespace_for_monitoring() -> None:
    text = _read(RUNBOOK)
    verify = text[text.index("Verify the application and monitoring sidecar:") : text.index("Trust the temporary ALB")]

    assert 'export scenario_a_namespace="a-$(printf \'%s\\0A\' "$RUN_ID" | sha256sum | cut -c1-20)"' in text
    assert 'NAMESPACE="$scenario_a_namespace"' in verify
    assert "${ECS_CLUSTER%-cluster}" not in verify


def test_cold_install_teardown_is_scenario_then_bootstrap_with_orphan_check() -> None:
    text = _read(RUNBOOK)
    teardown = text[text.index("## Teardown") :]

    scenario_destroy = teardown.index("scenario-a plan -destroy")
    bootstrap_destroy = teardown.index("bootstrap plan -destroy")
    orphan_check = teardown.index("resourcegroupstaggingapi get-resources")

    assert scenario_destroy < bootstrap_destroy < orphan_check
    assert 'test -z "$(terraform -chdir=scenario-a state list)"' in teardown
    assert 'test -z "$(terraform -chdir=bootstrap state list)"' in teardown
    assert "no active ECS tasks, Aurora instances, ALBs, NAT gateways, EFS" in teardown
