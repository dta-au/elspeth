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


def test_cold_install_container_insights_cleanup_is_bounded_and_fails_closed() -> None:
    text = _read(RUNBOOK)
    cleanup = text[text.index("### Container Insights log-group orphan") : text.index("Finally, query the run tag:")]

    for marker in (
        "ELSPETH_CONTAINER_INSIGHTS_MAX_WAIT_SECONDS",
        "ELSPETH_CONTAINER_INSIGHTS_POLL_INTERVAL_SECONDS",
        "ELSPETH_CONTAINER_INSIGHTS_QUIET_SECONDS",
        "describe-log-groups",
        "delete-log-group",
        "container_insights_log_group_stable",
        "container_insights_log_group_not_stabilized",
    ):
        assert marker in cleanup
    assert "|| true" not in cleanup
    assert cleanup.index("describe-log-groups") < cleanup.index("delete-log-group")
    assert "full quiet window" in cleanup


def test_cold_install_replans_to_enable_log_group_adoption_before_saved_plan_apply() -> None:
    text = _read(RUNBOOK)
    cleanup = text[text.index("### Container Insights log-group orphan") : text.index("Finally, query the run tag:")]
    normalized = " ".join(cleanup.split())

    plan = "terraform -chdir=scenario-a plan"
    variable = "-var='adopt_container_insights_log_group=true'"
    plan_file = "-out=.terraform/scenario-a-adopt.tfplan"
    apply = "terraform -chdir=scenario-a apply .terraform/scenario-a-adopt.tfplan"
    for marker in (plan, variable, plan_file, apply):
        assert marker in cleanup
    assert "Do not reuse the failed saved plan" in normalized
    assert cleanup.index(plan) < cleanup.index(variable) < cleanup.index(plan_file) < cleanup.index(apply)
    assert not re.search(r"terraform\s+-chdir=scenario-a\s+apply[^\n]*-var", cleanup)
    assert "on that apply" not in cleanup


def test_cold_install_refuses_root_or_collapsed_terraform_profiles() -> None:
    text = _read(RUNBOOK)
    identity = text[text.index("## 1. Select and prove the AWS identity") : text.index("## 2.")]

    assert ': "${AWS_ROOT_PROFILE:?set the administrator profile Terraform must never use}"' in identity
    assert 'test "$AWS_PROFILE" != "$IAM_LIFECYCLE_AWS_PROFILE"' in identity
    assert 'test "$AWS_PROFILE" != "$AWS_ROOT_PROFILE"' in identity
    assert 'test "$IAM_LIFECYCLE_AWS_PROFILE" != "$AWS_ROOT_PROFILE"' in identity
    assert "arn:aws:iam::${AWS_ACCOUNT_ID}:root" in identity


def test_cold_install_checks_all_five_live_default_installer_policy_action_sets() -> None:
    text = _read(RUNBOOK)
    policies = text[text.index("## 2. Install the installer and lifecycle policies") : text.index("## 3.")]

    policy_names = (
        "control-plane",
        "regional-resources",
        "relationships",
        "runtime-observation",
        "tagless-updates",
    )
    policy_arn_variables = (
        "INSTALLER_CONTROL_PLANE_POLICY_ARN",
        "INSTALLER_REGIONAL_RESOURCES_POLICY_ARN",
        "INSTALLER_RELATIONSHIPS_POLICY_ARN",
        "INSTALLER_RUNTIME_OBSERVATION_POLICY_ARN",
        "INSTALLER_TAGLESS_UPDATES_POLICY_ARN",
    )
    for name in policy_names:
        assert name in policies
    assert "bootstrap/.terraform/installer-${name}-policy.json" in policies
    for variable in policy_arn_variables:
        assert f': "${{{variable}:?set the recorded ' in policies
        assert f'"${variable}"' in policies

    render_loop = re.search(r"for policy in (?P<names>[a-z -]+); do", policies)
    assert render_loop is not None
    assert tuple(render_loop.group("names").split()) == policy_names
    names_block = policies[policies.index("INSTALLER_POLICY_NAMES=(") : policies.index("\n)", policies.index("INSTALLER_POLICY_NAMES=("))]
    arns_block = policies[policies.index("INSTALLER_POLICY_ARNS=(") : policies.index("\n)", policies.index("INSTALLER_POLICY_ARNS=("))]
    assert tuple(re.findall(r"^  ([a-z-]+)$", names_block, re.MULTILINE)) == policy_names
    assert tuple(re.findall(r'^  "\$([A-Z_]+)"$', arns_block, re.MULTILINE)) == policy_arn_variables

    for marker in (
        "render_installer_policies",
        "verify_installer_policy_currency",
        "await_installer_policy_currency",
        "DefaultVersionId",
        "aws iam get-policy",
        "aws iam get-policy-version",
        "verify-iam-policy-actions.py",
        "IAM_POLICY_SETTLE_MAX_SECONDS",
        "IAM_POLICY_SETTLE_QUIET_SECONDS",
        "IAM_POLICY_SETTLE_POLL_SECONDS",
    ):
        assert marker in policies
    assert policies.index("aws iam get-policy") < policies.index("aws iam get-policy-version")
    assert policies.index("render_installer_policies") < policies.index("await_installer_policy_currency")
    assert 'for index in "${!INSTALLER_POLICY_NAMES[@]}"; do' in policies
    assert "name=${INSTALLER_POLICY_NAMES[$index]}" in policies
    assert "arn=${INSTALLER_POLICY_ARNS[$index]}" in policies
    assert "if verify_installer_policy_currency; then" in policies
    assert 'test "${#INSTALLER_POLICY_NAMES[@]}" = 5' in policies
    assert 'test "${#INSTALLER_POLICY_ARNS[@]}" = 5' in policies
    assert 'test "$unique_count" = 5' in policies
    assert "Inspect all six JSON files" in policies
    assert "Attach the five" in policies
    assert "Record all six policy ARNs" in policies
    assert "account-level limitations" in policies
    assert "one account-level CloudWatch Logs limitation" not in policies
    assert "bounded quiet window" in policies
    assert "fail closed" in policies

    teardown = text[text.index("## Teardown") :]
    assert "remove the six policies recorded" in teardown
    assert "detach the five `installer-*` policies" in teardown


def test_policy_currency_helpers_propagate_each_failure_even_when_polled_as_a_condition() -> None:
    text = _read(RUNBOOK)
    policies = text[text.index("## 2. Install the installer and lifecycle policies") : text.index("## 3.")]
    render = policies[policies.index("render_installer_policies()") : policies.index("render_installer_policies\n")]
    verify = policies[policies.index("verify_installer_policy_currency()") : policies.index("IAM_POLICY_SETTLE_MAX_SECONDS")]

    assert render.count("|| return") >= 4
    for marker in (
        "render_installer_policies || return",
        ") || return\n    jq -e --arg arn",
        '<<<"$metadata" >/dev/null || return',
        '<<<"$metadata") || return',
        '>"$live_path" || return',
        '--label "$name" || return',
    ):
        assert marker in verify


def test_every_saved_plan_json_is_profile_verified_before_apply() -> None:
    text = _read(RUNBOOK)
    expected_plans = {
        "bootstrap",
        "scenario-a",
        "scenario-a-destroy",
        "bootstrap-destroy",
        "scenario-a-adopt",
    }
    recorded_plans = set(re.findall(r"-out=\.terraform/([a-z0-9-]+)\.tfplan", text))
    applied_plans = set(re.findall(r"apply \.terraform/([a-z0-9-]+)\.tfplan", text))
    assert recorded_plans == applied_plans == expected_plans

    for plan in recorded_plans:
        plan_file = f".terraform/{plan}.tfplan"
        json_file = f".terraform/{plan}.json"
        plan_at = text.index(f"-out={plan_file}")
        json_at = text.index(f"show -json {plan_file} >{json_file}", plan_at)
        verify_at = text.index(f"--plan-json {json_file}", json_at)
        apply_at = text.index(f"apply {plan_file}", verify_at)
        assert plan_at < json_at < verify_at < apply_at

    assert text.count("verify-terraform-profiles.py plan") == 5
    assert text.count('--installer-profile "$AWS_PROFILE"') >= 6
    assert text.count('--iam-lifecycle-profile "$IAM_LIFECYCLE_AWS_PROFILE"') == 5
    assert text.count('--forbidden-profile "$AWS_ROOT_PROFILE"') >= 6


def test_teardown_reconfigures_and_verifies_the_initialized_backend_before_state_reads() -> None:
    text = _read(RUNBOOK)
    teardown = text[text.index("## Teardown") :]

    init = teardown.index("terraform -chdir=scenario-a init -reconfigure")
    backend_config = teardown.index("-backend-config=../examples/scenario-a.s3.tfbackend", init)
    verify = teardown.index("verify-terraform-profiles.py backend", backend_config)
    backend_state = teardown.index("--backend-state scenario-a/.terraform/terraform.tfstate", verify)
    expected_profile = teardown.index('--installer-profile "$AWS_PROFILE"', backend_state)
    workspace = teardown.index("terraform -chdir=scenario-a workspace show", expected_profile)
    first_output = teardown.index("terraform -chdir=scenario-a output", workspace)

    assert init < backend_config < verify < backend_state < expected_profile < workspace < first_output


def test_terraform_package_reference_points_qualification_to_the_fail_closed_evidence_gates() -> None:
    text = _read(PACKAGE_README)

    for marker in (
        "AWS_ROOT_PROFILE",
        'test "$AWS_PROFILE" != "$IAM_LIFECYCLE_AWS_PROFILE"',
        "verify-terraform-profiles.py plan",
        "verify-terraform-profiles.py backend",
        "verify-iam-policy-actions.py",
        "live default policy version",
        "installer-tagless-updates-policy.json.tftpl",
        "Render all five installer policies",
        "attach all five installer documents",
        "aws-ecs-cold-install.md",
    ):
        assert marker in text
    assert "may set both provider variables" not in text
