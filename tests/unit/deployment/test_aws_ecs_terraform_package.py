"""Contract tests for the supported AWS ECS Terraform source package."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from elspeth.web._aws_ecs_acceptance import scenario_inventory

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE = REPO_ROOT / "deploy" / "aws-ecs" / "terraform"

EXPECTED_FILES = {
    ".gitignore",
    "README.md",
    "bootstrap/.terraform.lock.hcl",
    "bootstrap/main.tf",
    "bootstrap/outputs.tf",
    "bootstrap/variables.tf",
    "bootstrap/versions.tf",
    "cloudwatch-agent-image/Dockerfile",
    "examples/bootstrap.tfvars.example",
    "examples/scenario-a.s3.tfbackend.example",
    "examples/scenario-a.tfvars.example",
    "examples/scenario-b.s3.tfbackend.example",
    "examples/scenario-b.tfvars.example",
    "iam/installer-policy.json",
    "modules/scenario/database_bootstrap.tf",
    "modules/scenario/ecs.tf",
    "modules/scenario/iam_observability.tf",
    "modules/scenario/locals.tf",
    "modules/scenario/network.tf",
    "modules/scenario/outputs.tf",
    "modules/scenario/storage_identity.tf",
    "modules/scenario/variables.tf",
    "modules/scenario/versions.tf",
    "scenario-a/.terraform.lock.hcl",
    "scenario-a/main.tf",
    "scenario-a/outputs.tf",
    "scenario-a/variables.tf",
    "scenario-a/versions.tf",
    "scenario-b/.terraform.lock.hcl",
    "scenario-b/main.tf",
    "scenario-b/outputs.tf",
    "scenario-b/variables.tf",
    "scenario-b/versions.tf",
    "telemetry/elspeth.cloudwatch-agent.v1/elspeth.cloudwatch-agent.v1.json",
    "telemetry/elspeth.cloudwatch-agent.v1/elspeth.cloudwatch-agent.v1.otel.yaml",
}


def _source_files() -> list[Path]:
    return [path for path in PACKAGE.rglob("*") if path.is_file() and ".terraform" not in path.relative_to(PACKAGE).parts]


def _text(relative: str) -> str:
    return (PACKAGE / relative).read_text(encoding="utf-8")


def _all_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in _source_files())


def _hcl_map_keys(text: str, assignment: str) -> frozenset[str]:
    """Return the direct keys of one conventionally formatted HCL map."""

    assignment_pattern = re.compile(rf"^(?P<indent>\s*){re.escape(assignment)}\s*=\s*\{{\s*$")
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = assignment_pattern.match(line)
        if match is None:
            continue
        base_indent = len(match.group("indent"))
        key_pattern = re.compile(rf"^\s{{{base_indent + 2}}}([A-Za-z0-9_]+)\s*=")
        keys: set[str] = set()
        for candidate in lines[index + 1 :]:
            if candidate == f"{' ' * base_indent}}}":
                return frozenset(keys)
            key_match = key_pattern.match(candidate)
            if key_match is not None:
                keys.add(key_match.group(1))
        raise AssertionError(f"unterminated HCL map: {assignment}")
    raise AssertionError(f"HCL map not found: {assignment}")


def test_package_contains_only_the_supported_source_and_operator_inputs() -> None:
    actual = {path.relative_to(PACKAGE).as_posix() for path in _source_files()}
    assert actual == EXPECTED_FILES

    ignored = _text(".gitignore")
    for pattern in (
        ".terraform/",
        "*.tfstate",
        "*.tfstate.*",
        "*.tfplan",
        "crash.log",
        "*.tfvars",
        "*.tfbackend",
        "*.pem",
        "*.key",
        "__pycache__/",
    ):
        assert pattern in ignored
    assert "!*.tfvars.example" in ignored
    assert "!*.tfbackend.example" in ignored


def test_terraform_and_provider_versions_are_current_and_locked() -> None:
    for relative in (
        "bootstrap/versions.tf",
        "modules/scenario/versions.tf",
        "scenario-a/versions.tf",
        "scenario-b/versions.tf",
    ):
        text = _text(relative)
        assert 'required_version = ">= 1.14, < 2.0"' in text

    for relative in (
        "bootstrap/.terraform.lock.hcl",
        "scenario-a/.terraform.lock.hcl",
        "scenario-b/.terraform.lock.hcl",
    ):
        text = _text(relative)
        assert 'provider "registry.terraform.io/hashicorp/aws"' in text
        assert 'version     = "6.54.0"' in text
        if "bootstrap" not in relative:
            assert 'provider "registry.terraform.io/hashicorp/random"' in text
            assert 'version     = "3.9.0"' in text
            assert 'provider "registry.terraform.io/hashicorp/tls"' in text
            assert 'version     = "4.3.0"' in text


def test_scenario_backends_are_partial_locked_encrypted_and_isolated() -> None:
    for scenario in ("scenario-a", "scenario-b"):
        versions = _text(f"{scenario}/versions.tf")
        backend_block = re.search(r'backend\s+"s3"\s*\{(?P<body>.*?)\}', versions, re.DOTALL)
        assert backend_block is not None
        assert not backend_block.group("body").strip()

        backend = _text(f"examples/{scenario}.s3.tfbackend.example")
        assert 'bucket         = "REPLACE_WITH_BOOTSTRAP_STATE_BUCKET"' in backend
        assert 'region         = "REPLACE_WITH_AWS_REGION"' in backend
        assert "encrypt        = true" in backend
        assert "use_lockfile   = true" in backend
        assert f'key            = "elspeth/{scenario}/terraform.tfstate"' in backend

    readme = _text("README.md")
    for phrase in (
        "aws sts get-caller-identity",
        "explicit AWS account",
        "explicit AWS region",
        "workspace show",
        "-backend-config",
        "scenario-a.s3.tfbackend",
        "scenario-b.s3.tfbackend",
    ):
        assert phrase in readme


def test_ownership_tags_are_mandatory_and_propagated_by_both_scenarios() -> None:
    for scenario in ("scenario-a", "scenario-b"):
        variables = _text(f"{scenario}/variables.tf")
        main = _text(f"{scenario}/main.tf")
        provider = _text(f"{scenario}/versions.tf")
        for name in ("owner", "purpose", "run_id", "cleanup_deadline"):
            assert f'variable "{name}"' in variables
            assert re.search(rf"\b{name}\s+=\s+var\.{name}\b", main)

        assert "default_tags" in provider
        for tag, variable in (
            ("Owner", "owner"),
            ("Purpose", "purpose"),
            ("RunId", "run_id"),
            ("CleanupDeadline", "cleanup_deadline"),
            ("ACCEPTANCE_RUN_ID", "run_id"),
        ):
            assert re.search(rf"\b{tag}\s+=\s+var\.{variable}\b", provider)

    bootstrap_provider = _text("bootstrap/versions.tf")
    module_locals = _text("modules/scenario/locals.tf")
    assert re.search(r"\bACCEPTANCE_RUN_ID\s+=\s+var\.run_id\b", bootstrap_provider)
    assert re.search(r"\bACCEPTANCE_RUN_ID\s+=\s+var\.run_id\b", module_locals)


def test_database_topology_is_aurora_with_separate_databases_and_roles() -> None:
    storage = _text("modules/scenario/storage_identity.tf")
    bootstrap = _text("modules/scenario/database_bootstrap.tf")
    variables = _text("modules/scenario/variables.tf")

    assert re.search(r'engine\s+=\s+"aurora-postgresql"', storage)
    assert re.search(r"engine_version\s+=\s+var\.aurora_engine_version", storage)
    assert "aws_rds_cluster" in storage
    assert "aws_rds_cluster_instance" in storage
    assert "aws_db_instance" not in storage
    assert 'default = "elspeth_session"' in variables
    assert 'default = "elspeth_landscape"' in variables
    for role in ("bootstrap", "schema", "runtime"):
        assert f"database_{role}" in bootstrap
    assert "ALTER ROLE" in bootstrap
    assert "ALTER ROLE IF EXISTS" not in bootstrap
    assert "aws_secretsmanager_secret_version.bootstrap" in bootstrap
    assert "landscape_passphrase" not in _all_text().lower()
    for relative in (
        "modules/scenario/variables.tf",
        "scenario-a/variables.tf",
        "scenario-b/variables.tf",
    ):
        text = _text(relative)
        assert re.search(r'variable "aurora_engine_version".*?default\s*=\s*"16\.13"', text, re.DOTALL)
        assert re.search(r'condition\s*=\s*var\.aurora_engine_version\s*==\s*"16\.13"', text, re.DOTALL)
    for relative in (
        "examples/scenario-a.tfvars.example",
        "examples/scenario-b.tfvars.example",
        "README.md",
    ):
        assert "16.13" in _text(relative)
    assert "`ap-southeast-1`" in _text("README.md")


def test_bedrock_composer_uses_the_task_role_without_static_credentials() -> None:
    variables = _text("modules/scenario/variables.tf")
    ecs = _text("modules/scenario/ecs.tf")
    iam = _text("modules/scenario/iam_observability.tf")
    all_text = _all_text()
    terraform_text = "\n".join(path.read_text(encoding="utf-8") for path in _source_files() if path.suffix == ".tf")

    for name in ("composer_model", "composer_advisor_model"):
        assert f'variable "{name}"' in variables
        assert f"var.{name}" in "\n".join((ecs, _text("modules/scenario/locals.tf")))
        assert "bedrock/" in variables
    assert "var.composer_model != var.composer_advisor_model" in variables
    runtime = "\n".join((ecs, _text("modules/scenario/locals.tf")))
    assert "ELSPETH_WEB__COMPOSER_MODEL" in runtime
    assert "ELSPETH_WEB__COMPOSER_ADVISOR_MODEL" in runtime
    assert "OPENROUTER_API_KEY" not in all_text
    assert "openrouter" not in all_text.lower()
    assert "AWS_ACCESS_KEY_ID" not in all_text
    assert "AWS_SECRET_ACCESS_KEY" not in all_text
    assert "AWS_PROFILE" not in all_text
    assert "AWS_ENDPOINT_URL" not in all_text
    assert "AGENTCORE" not in terraform_text.upper()
    assert '"bedrock:InvokeModel"' in iam
    assert "var.bedrock_inference_profile_arns" in iam
    assert "var.bedrock_foundation_model_arns" in iam
    assert '"Resource" = ["*"]' not in iam


def test_inventory_v7_exactly_matches_the_runtime_validator_contract() -> None:
    locals = _text("modules/scenario/locals.tf")
    ecs = _text("modules/scenario/ecs.tf")
    outputs = _text("modules/scenario/outputs.tf")

    resolved_output = outputs[outputs.index('output "resolved_inventory"') :]
    assert _hcl_map_keys(resolved_output, "value") == frozenset(
        {
            "schema",
            "acceptance_run_id",
            "candidate_sha",
            "aws_account_id",
            "aws_region",
            "scenario_id",
            "phase",
            "values",
            "orphan_sweep",
        }
    )
    assert _hcl_map_keys(outputs, "scenario_values") == scenario_inventory._SCENARIO_VALUE_FIELDS
    assert _hcl_map_keys(outputs, "scenario_orphan_sweep") == scenario_inventory._ORPHAN_INVENTORY_FIELDS
    assert re.search(r'schema\s+=\s+"elspeth\.aws-ecs-scenario-inventory\.v7"', resolved_output)
    assert re.search(r"acceptance_run_id\s+=\s+var\.run_id", resolved_output)
    assert re.search(r'tag_key\s+=\s+"ACCEPTANCE_RUN_ID"', outputs)
    assert re.search(r"cleanup_owner\s+=\s+var\.owner", outputs)
    assert re.search(
        r"transaction_search_baseline_sha256\s+=\s+var\.transaction_search_baseline_sha256",
        outputs,
    )
    for name in (
        "SCENARIO_TF_DIR",
        "SCENARIO_TF_VARS",
        "SCENARIO_TF_BINDING_SHA",
        "SCENARIO_TF_BINDING_FILE",
    ):
        assert name in _hcl_map_keys(outputs, "scenario_values")
    assert "@sha256:[0-9a-f]{64}$" in _text("modules/scenario/variables.tf")
    assert "local.cloudwatch_agent_image" in ecs
    assert "var.cloudwatch_agent_image" in _text("modules/scenario/iam_observability.tf")
    assert "filesha256(" in locals
    assert "elspeth.cloudwatch-agent.v1.json" in locals
    assert "elspeth.cloudwatch-agent.v1.otel.yaml" in locals
    assert "resolved_inventory" in outputs


def test_networking_has_long_request_support_and_correct_listener_target() -> None:
    network = _text("modules/scenario/network.tf")
    assert re.search(r"idle_timeout\s+=\s+300", network)
    assert re.search(r'type\s+=\s+"forward"', network)
    assert re.search(r"target_group_arn\s+=\s+aws_lb_target_group\.web\.arn", network)


def test_scenario_a_is_the_obvious_cold_install_and_b_hostname_matches_runtime_namespace() -> None:
    readme = _text("README.md")
    assert "Scenario A: cold install (recommended)" in readme
    assert "Scenario B: OIDC acceptance variant" in readme
    assert readme.index("Scenario A: cold install (recommended)") < readme.index("Scenario B: OIDC acceptance variant")

    module_locals = _text("modules/scenario/locals.tf")
    assert "oidc_domain_prefix        = local.namespace" in module_locals
    assert 'oidc_domain_prefix        = "${local.namespace}-${' not in module_locals
    assert 'substr(sha256("${lower(var.run_id)}\\u0000${local.scenario_id_upper}"), 0, 20)' in module_locals
    assert (
        'oidc_authorization_origin = var.scenario_id == "B" ? '
        '"https://${aws_cognito_user_pool_domain.web[0].domain}.auth.${var.aws_region}.amazoncognito.com" : ""'
    ) in module_locals
    assert "domain       = local.oidc_domain_prefix" in _text("modules/scenario/storage_identity.tf")


def test_monitoring_resources_use_a_retained_digest_pinned_collector() -> None:
    bootstrap = _text("bootstrap/main.tf")
    bootstrap_outputs = _text("bootstrap/outputs.tf")
    variables = _text("modules/scenario/variables.tf")
    readme = _text("README.md")

    assert "aws_ecr_repository.cloudwatch_agent" in bootstrap
    assert 'resource "aws_ecr_lifecycle_policy" "cloudwatch_agent"' in bootstrap
    assert "tagStatus" in bootstrap
    assert '"untagged"' in bootstrap
    assert "cloudwatch_agent_repository_url" in bootstrap_outputs
    assert "@sha256:" in variables
    assert re.search(r"Deploy the\s+CloudWatch agent by digest", readme)
    ecs = _text("modules/scenario/ecs.tf")
    observability = _text("modules/scenario/iam_observability.tf")
    assert re.search(r"cloudwatch_agent_container\s*=\s*\{.*?image\s*=\s*local\.cloudwatch_agent_image", ecs, re.DOTALL)
    for resource in (
        'resource "aws_cloudwatch_log_group" "operator"',
        'resource "aws_cloudwatch_dashboard" "operator"',
        'resource "aws_cloudwatch_metric_alarm" "operator_direct"',
        'resource "aws_cloudwatch_metric_alarm" "operator_export_failures"',
        'resource "aws_xray_group" "scenario"',
        'resource "aws_xray_sampling_rule" "scenario"',
        'resource "aws_cloudwatch_event_rule" "deployments"',
    ):
        assert resource in observability


def test_cold_install_pauses_service_until_two_stage_doctor_and_explicit_enable() -> None:
    ecs = _text("modules/scenario/ecs.tf")
    readme = _text("README.md")

    assert re.search(r'resource "aws_ecs_service" "web"\s*\{.*?desired_count\s*=\s*0', ecs, re.DOTALL)
    schema_init = readme.index("doctor aws-ecs --init-schema --json")
    ordinary_doctor = readme.index("doctor aws-ecs --json", schema_init)
    explicit_enable = readme.index("service_enable_command", ordinary_doctor)
    assert schema_init < ordinary_doctor < explicit_enable


def test_certificate_limit_and_code_blind_outputs_are_explicit() -> None:
    module_outputs = _text("modules/scenario/outputs.tf")
    scenario_a_outputs = _text("scenario-a/outputs.tf")
    readme = _text("README.md")
    combined = "\n".join((module_outputs, scenario_a_outputs, readme))

    assert "self-signed" in combined.lower()
    assert "24 hours" in combined
    for name in (
        "public_url",
        "cluster_name",
        "service_name",
        "task_role_arn",
        "runtime_database_secret_arn",
        "doctor_task_definition_arn",
        "doctor_network_configuration",
        "service_enable_command",
        "resolved_inventory",
        "teardown",
    ):
        assert name in combined


def test_examples_and_policy_are_parseable_and_contain_no_local_or_account_data() -> None:
    json.loads(_text("iam/installer-policy.json"))
    json.loads(_text("telemetry/elspeth.cloudwatch-agent.v1/elspeth.cloudwatch-agent.v1.json"))
    otel = yaml.safe_load(_text("telemetry/elspeth.cloudwatch-agent.v1/elspeth.cloudwatch-agent.v1.otel.yaml"))
    assert isinstance(otel, dict)

    all_text = _all_text()
    assert "/home/" not in all_text
    assert not re.search(r"\b\d{12}\b", all_text)
    assert "BEGIN PRIVATE KEY" not in all_text
    for forbidden in (
        "approval-require",
        "receipt",
        "plan12",
        "raw-image-ref",
        "baseline-copy",
    ):
        assert forbidden not in all_text.lower()
