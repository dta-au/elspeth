"""Contract tests for the supported AWS ECS Terraform source package."""

from __future__ import annotations

import inspect
import json
import re
import subprocess
from pathlib import Path

import yaml

from elspeth.web._aws_ecs_acceptance import scenario_inventory, task_definition

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
    "iam/installer-policy.json.tftpl",
    "iam/lifecycle-policy.json.tftpl",
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
    "scenario-a/codeblind-compatibility.json",
    "scenario-a/codeblind.tftest.hcl",
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
    return [
        path
        for path in PACKAGE.rglob("*")
        if path.is_file()
        and ".terraform" not in path.relative_to(PACKAGE).parts
        and not path.name.endswith((".tfstate", ".tfplan"))
        and ".tfstate." not in path.name
    ]


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


def _hcl_variable_names(text: str) -> frozenset[str]:
    return frozenset(re.findall(r'^variable "([^"]+)"', text, re.MULTILINE))


def _statement_body(text: str, sid: str) -> str:
    """Slice one `statement { ... }` block by its `sid`, tolerant of fmt's column alignment."""

    match = re.search(rf'sid\s*=\s*"{re.escape(sid)}"', text)
    assert match is not None, f"statement not found: {sid}"
    end = text.index("statement {", match.start())
    return text[match.start() : end]


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


def test_scenario_web_plugin_allowlist_exposes_textract_to_composer() -> None:
    locals_text = _text("modules/scenario/locals.tf")
    allowlist_match = re.search(
        r"plugin_allowlist\s*=\s*jsonencode\(\[(?P<body>.*?)\]\)",
        locals_text,
        re.DOTALL,
    )

    assert allowlist_match is not None
    assert '"transform:aws_textract_document_analysis"' in allowlist_match.group("body")


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
        'aws --profile "$AWS_PROFILE" --region "$AWS_REGION" sts get-caller-identity',
        "explicit AWS profile, account, and region",
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
    canonical_root = "/etc/elspeth/rds/global-bundle.pem"
    assert storage.count("sslmode=verify-full") == 5
    assert storage.count("sslrootcert=${local.rds_ca_bundle_path}") == 5
    assert canonical_root in _text("modules/scenario/locals.tf")
    assert 'rds_ca_identifier = "rds-ca-rsa2048-g1"' in _text("modules/scenario/locals.tf")
    assert re.search(
        r"ca_cert_identifier\s+=\s+local\.rds_ca_identifier",
        storage,
    )
    assert "truststore.pki.rds.amazonaws.com" not in _all_text()
    assert "urllib.request" not in _text("modules/scenario/database_bootstrap.tf")
    assert "/tmp/rds-global-bundle.pem" not in _all_text()
    assert "${local.data_dir}/rds-global-bundle.pem" not in _all_text()
    assert '?sslmode=require"' not in storage
    assert re.search(r"posix_user\s*{\s*uid\s*=\s*1654\s*gid\s*=\s*1654", storage, re.DOTALL)
    assert re.search(r"creation_info\s*{\s*owner_uid\s*=\s*1654\s*owner_gid\s*=\s*1654", storage, re.DOTALL)
    assert 'path = "/elspeth-${local.namespace}"' in storage
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


def test_read_only_root_filesystem_matches_the_container_contract() -> None:
    ecs = _text("modules/scenario/ecs.tf")
    bootstrap = _text("modules/scenario/database_bootstrap.tf")
    assert ecs.count("readonlyRootFilesystem = true") == 4
    assert "readonlyRootFilesystem = true" in bootstrap
    assert "readonlyRootFilesystem" not in ecs[ecs.index("cloudwatch_agent_container = {") : ecs.index("candidate_web_container = {")]
    assert "readonlyRootFilesystem" not in ecs[ecs.index("candidate_web_container = {") : ecs.index("schema_init_doctor_container = {")]
    assert "rollback_web_container = merge(local.candidate_web_container" in ecs
    assert "rollback_doctor_container = merge(local.runtime_doctor_container" in ecs


def test_database_bootstrap_uses_the_image_trust_verifier() -> None:
    bootstrap = _text("modules/scenario/database_bootstrap.tf")
    assert "verify_aws_rds_trust_bundle" in bootstrap
    assert "urllib.request" not in bootstrap
    assert "Path(" not in bootstrap


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
    assert "AWS_PROFILE" not in runtime
    assert "AWS_ENDPOINT_URL" not in all_text
    assert "AGENTCORE" not in terraform_text.upper()
    assert '"bedrock:InvokeModel"' in iam
    assert "var.bedrock_inference_profile_arns" in iam
    assert "var.bedrock_foundation_model_arns" in iam
    assert '"Resource" = ["*"]' not in iam


def test_cross_region_bedrock_profile_gets_wildcard_region_foundation_model_grant() -> None:
    module_locals = _text("modules/scenario/locals.tf")
    iam = _text("modules/scenario/iam_observability.tf")

    # A cross-region ("global."/"us."/"eu."/"apac.") composer_model or composer_advisor_model
    # profile is authorized by Bedrock against the underlying foundation model in whichever
    # region it routes to, and that authorization check reports a region-less resource ARN. A
    # single region-pinned foundation-model grant can never match that check, so the module must
    # derive a wildcard-region grant for every configured model that carries one of these
    # prefixes.
    assert re.search(r'bedrock_cross_region_prefixes\s*=\s*\["global\.", "us\.", "eu\.", "apac\."\]', module_locals)
    assert re.search(r'trimprefix\(var\.composer_model, "bedrock/"\)', module_locals)
    assert re.search(r'trimprefix\(var\.composer_advisor_model, "bedrock/"\)', module_locals)
    assert "bedrock_cross_region_foundation_model_arns" in module_locals
    assert '"arn:aws:bedrock:*::foundation-model/${trimprefix(model_id, prefix)}"' in module_locals

    # Pin the conditional's negative direction: a region-pinned model must map to a null
    # prefix and be excluded from the wildcard grant. Losing either fragment would silently
    # turn the wildcard grant into a blanket grant for every configured model.
    assert "if startswith(model_id, prefix)" in module_locals
    assert "if prefix != null" in module_locals

    bedrock_statement = _statement_body(iam, "InvokeConfiguredBedrockModels")
    # Retention: the region-pinned inference-profile and foundation-model grants must stay —
    # the wildcard grant is additive, not a replacement.
    assert "var.bedrock_inference_profile_arns" in bedrock_statement
    assert "var.bedrock_foundation_model_arns" in bedrock_statement
    assert "local.bedrock_cross_region_foundation_model_arns" in bedrock_statement

    # The runtime task-role grant alone is not enough: it is intersected with the run-scoped
    # permissions boundary bootstrap creates, so the boundary must independently allow the same
    # wildcard-region foundation-model resource or the effective permission is still denied.
    bootstrap = _text("bootstrap/main.tf")
    assert '"arn:aws:bedrock:*::foundation-model/*"' in bootstrap


def test_task_role_can_list_the_acceptance_bucket_so_head_object_can_report_missing() -> None:
    iam = _text("modules/scenario/iam_observability.tf")
    bootstrap = _text("bootstrap/main.tf")

    # Without bucket-scoped s3:ListBucket, S3 cannot distinguish "object does not exist" from
    # "caller lacks permission" and returns 403 uniformly, which the acceptance harness's missing
    # -object check does not recognize as "not created yet." Grant ListBucket scoped to this
    # run's own acceptance bucket.
    assert '"s3:ListBucket"' in iam
    list_bucket_statement = _statement_body(iam, "ListAcceptanceBucket")
    assert '"s3:ListBucket"' in list_bucket_statement
    assert "aws_s3_bucket.acceptance.arn" in list_bucket_statement
    # No s3:prefix (or any other) condition: S3 evaluates the missing-vs-forbidden outcome for
    # HeadObject/GetObject with an implicit ListBucket check that runs outside the triggering
    # request's own context, so a condition here would never match and 403 would persist.
    assert "condition" not in list_bucket_statement

    # Same boundary-intersection requirement as the Bedrock grant: bucket-level (not
    # object-level) ListBucket must also be allowed by the permissions boundary.
    assert '"arn:aws:s3:::elspeth-*"' in bootstrap
    assert re.search(r'actions\s*=\s*\["s3:ListBucket"\]', bootstrap)


def test_composer_boot_probe_exercises_primary_and_advisor_models() -> None:
    module_locals = _text("modules/scenario/locals.tf")
    assert '{ name = "ELSPETH_WEB__COMPOSER_BOOT_PROBE_ENABLED", value = "true" }' in module_locals
    assert '{ name = "ELSPETH_WEB__COMPOSER_MODEL", value = var.composer_model }' in module_locals
    assert '{ name = "ELSPETH_WEB__COMPOSER_ADVISOR_MODEL", value = var.composer_advisor_model }' in module_locals


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
    assert "sha256(local.cw_agent_json)" in locals
    assert "sha256(local.cw_agent_otel)" in locals
    assert "elspeth.cloudwatch-agent.v1.json" in locals
    assert "elspeth.cloudwatch-agent.v1.otel.yaml" in locals
    assert "resolved_inventory" in outputs


def test_scenario_a_codeblind_inputs_have_no_acceptance_coordinator_dependencies() -> None:
    main = _text("scenario-a/main.tf")
    variables = _text("scenario-a/variables.tf")
    example = _text("examples/scenario-a.tfvars.example")
    readme_scenario_a = _text("README.md").split("## Scenario B", 1)[0]
    scenario_b_variables = _text("scenario-b/variables.tf")
    compatibility_path = PACKAGE / "scenario-a" / "codeblind-compatibility.json"
    assert compatibility_path.is_file()
    compatibility = json.loads(compatibility_path.read_text(encoding="utf-8"))

    forbidden = {
        "rollback_baseline_image",
        "rollback_baseline_sha",
        "scenario_tf_dir",
        "scenario_tf_vars",
        "scenario_tf_binding_file",
        "transaction_search_baseline_sha256",
    }
    assert not forbidden.intersection(_hcl_variable_names(variables))
    for name in forbidden:
        assert name not in example
        assert name not in readme_scenario_a
    assert forbidden.intersection(_hcl_variable_names(scenario_b_variables)) == forbidden

    assert "rollback_baseline_image            = var.candidate_image" in main
    assert "rollback_baseline_sha              = var.candidate_sha" in main
    expected_assignments = {
        "codeblind_compatibility_file": 'abspath("${path.root}/codeblind-compatibility.json")',
        "scenario_tf_dir": "abspath(path.root)",
        "scenario_tf_vars": 'abspath("${path.root}/../examples/scenario-a.tfvars.example")',
        "scenario_tf_binding_file": "local.codeblind_compatibility_file",
        "scenario_tf_binding_sha": "local.codeblind_compatibility_sha256",
        "transaction_search_baseline_sha256": "local.codeblind_transaction_search_sha256",
    }
    for name, value in expected_assignments.items():
        assert re.search(rf"\b{re.escape(name)}\s*=\s*{re.escape(value)}", main)
    assert compatibility == {
        "schema": "elspeth.aws-ecs-codeblind-compatibility.v1",
        "scenario_id": "A",
        "purpose": "Tracked package facts for standalone cold-install inventory compatibility; not acceptance evidence.",
        "terraform_root": "scenario-a",
        "tfvars_example": "examples/scenario-a.tfvars.example",
        "transaction_search_baseline": {
            "state": "not-captured",
            "reason": "Standalone first-account installation has no pre-existing transaction-search baseline.",
        },
    }


def test_installer_policy_is_renderable_scoped_and_boundary_enforced() -> None:
    template_path = PACKAGE / "iam" / "installer-policy.json.tftpl"
    assert template_path.is_file()
    template = template_path.read_text(encoding="utf-8")
    rendered = template
    values = {
        "aws_account_id": "123456789012",
        "aws_region": "ap-southeast-1",
        "run_id": "12345678-1234-4123-8123-123456789abc",
        "backend_state_bucket": "elspeth-state-example",
        "ecr_repository": "elspeth-web-example",
        "cloudwatch_agent_ecr_repository": "elspeth-agent-example",
        "scenario_a_bucket": "elspeth-a-example",
        "scenario_b_bucket": "elspeth-b-example",
    }
    for name, value in values.items():
        rendered = rendered.replace(f"${{{name}}}", value)
    assert "${" not in rendered
    policy = json.loads(rendered)
    statements = {statement["Sid"]: statement for statement in policy["Statement"]}

    assert "ManageElspethInfrastructure" not in statements
    assert "ReadDiscovery" in statements
    assert all(
        action.startswith(("sts:Get", "acm:Describe", "bedrock:Get", "cloudwatch:Describe", "cognito-idp:Describe"))
        or any(verb in action for verb in (":Get", ":List", ":Describe"))
        for action in statements["ReadDiscovery"]["Action"]
    )
    mutation_actions = {action for sid, statement in statements.items() if sid != "ReadDiscovery" for action in statement["Action"]}
    assert not any(action.endswith(":*") for action in mutation_actions)
    assert not {
        "iam:CreatePolicy",
        "iam:CreatePolicyVersion",
        "iam:DeletePolicy",
        "iam:DeletePolicyVersion",
        "iam:SetDefaultPolicyVersion",
        "iam:TagPolicy",
        "iam:UntagPolicy",
    }.intersection(mutation_actions)
    assert "CreateRunPermissionsBoundary" not in statements
    assert "ManageRunPermissionsBoundary" not in statements

    push_images = statements["PushAndCleanElspethImages"]
    assert {
        "ecr:BatchDeleteImage",
        "ecr:CompleteLayerUpload",
        "ecr:InitiateLayerUpload",
        "ecr:PutImage",
        "ecr:UploadLayerPart",
    }.issubset(push_images["Action"])
    assert push_images["Resource"] == [
        "arn:aws:ecr:ap-southeast-1:123456789012:repository/elspeth-web-example",
        "arn:aws:ecr:ap-southeast-1:123456789012:repository/elspeth-agent-example",
    ]
    assert all(sid == "PushAndCleanElspethImages" for sid, statement in statements.items() if "ecr:BatchDeleteImage" in statement["Action"])
    assert statements["AuthenticateForEcrPush"]["Action"] == ["ecr:GetAuthorizationToken"]
    assert statements["AuthenticateForEcrPush"]["Condition"]["StringEquals"]["aws:RequestedRegion"] == values["aws_region"]

    run_tasks = statements["RunScenarioTasks"]
    assert run_tasks["Action"] == ["ecs:RunTask"]
    assert run_tasks["Resource"] == [
        "arn:aws:ecs:ap-southeast-1:123456789012:task-definition/a-*:*",
        "arn:aws:ecs:ap-southeast-1:123456789012:task-definition/b-*:*",
    ]
    assert run_tasks["Condition"]["ArnLike"]["ecs:cluster"] == [
        "arn:aws:ecs:ap-southeast-1:123456789012:cluster/acceptance-a-*",
        "arn:aws:ecs:ap-southeast-1:123456789012:cluster/acceptance-b-*",
    ]
    assert statements["ReadScenarioSecretValues"]["Action"] == ["secretsmanager:GetSecretValue"]

    dashboards = statements["ManageRunDashboards"]
    assert dashboards["Resource"] == [
        "arn:aws:cloudwatch::123456789012:dashboard/a-*",
        "arn:aws:cloudwatch::123456789012:dashboard/b-*",
    ]
    assert {"cloudwatch:PutDashboard", "cloudwatch:DeleteDashboards"} == set(dashboards["Action"])
    assert not {"cloudwatch:PutDashboard", "cloudwatch:DeleteDashboards"}.intersection(
        statements["MutateRunTaggedRegionalResources"]["Action"]
    )
    named_buckets = statements["ManageElspethNamedBuckets"]
    assert {"s3:ListBucketVersions", "s3:DeleteBucketPublicAccessBlock"}.issubset(named_buckets["Action"])
    assert named_buckets["Resource"] == [
        "arn:aws:s3:::elspeth-state-example",
        "arn:aws:s3:::elspeth-a-example",
        "arn:aws:s3:::elspeth-b-example",
    ]
    named_objects = statements["ManageElspethNamedBucketObjects"]
    assert "s3:DeleteObjectVersion" in named_objects["Action"]
    assert named_objects["Resource"] == [
        "arn:aws:s3:::elspeth-state-example/*",
        "arn:aws:s3:::elspeth-a-example/*",
        "arn:aws:s3:::elspeth-b-example/*",
    ]
    assert "acm:GetCertificate" in statements["ReadDiscovery"]["Action"]

    assert "CreateRunScopedRolesWithBoundary" not in statements
    pass_role = statements["PassRunScopedRolesToEcsTasksOnly"]
    assert pass_role["Condition"]["StringEquals"]["iam:PassedToService"] == "ecs-tasks.amazonaws.com"
    inline_roles = statements["ManageRunScopedInlinePolicies"]
    assert set(inline_roles["Action"]) == {"iam:DeleteRolePolicy", "iam:PutRolePolicy"}
    managed_attachment = statements["ManageKnownExecutionRoleAttachment"]
    assert set(managed_attachment["Action"]) == {"iam:AttachRolePolicy", "iam:DetachRolePolicy"}
    assert managed_attachment["Resource"] == [
        "arn:aws:iam::123456789012:role/a-*-execution-role",
        "arn:aws:iam::123456789012:role/b-*-execution-role",
    ]
    assert managed_attachment["Condition"]["ArnEquals"]["iam:PolicyARN"] == (
        "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
    )
    assert not {
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:DeleteRolePermissionsBoundary",
        "iam:PutRolePermissionsBoundary",
        "iam:TagRole",
        "iam:UntagRole",
        "iam:UpdateAssumeRolePolicy",
    }.intersection(mutation_actions)
    assert "iam:CreateServiceLinkedRole" in statements["CreateRequiredServiceLinkedRoles"]["Action"]
    assert statements["CreateRequiredServiceLinkedRoles"]["Condition"]["StringEquals"]["iam:AWSServiceName"] == [
        "ecs.amazonaws.com",
        "elasticloadbalancing.amazonaws.com",
        "rds.amazonaws.com",
    ]

    for relative in ("scenario-a/variables.tf", "scenario-b/variables.tf"):
        root_variables = _text(relative)
        assert 'variable "iam_permissions_boundary_arn"' in root_variables
        assert 'variable "iam_lifecycle_aws_profile"' in root_variables
    for relative in ("examples/scenario-a.tfvars.example", "examples/scenario-b.tfvars.example"):
        example = _text(relative)
        assert "iam_permissions_boundary_arn" in example
        assert "iam_lifecycle_aws_profile" in example
    iam = _text("modules/scenario/iam_observability.tf")
    assert iam.count("permissions_boundary = var.iam_permissions_boundary_arn") == 2
    assert '"${aws_cloudwatch_log_group.operator.arn}:log-stream:*"' in iam
    network = _text("modules/scenario/network.tf")
    for resource_type, name in (
        ("ingress", "alb_https"),
        ("ingress", "task_from_alb"),
        ("egress", "alb_to_task"),
        ("egress", "task_all"),
        ("ingress", "database_from_task"),
        ("ingress", "efs_from_task"),
    ):
        assert re.search(
            rf'resource "aws_vpc_security_group_{resource_type}_rule" "{name}" \{{'
            rf'(?:(?!\nresource ").)*\n  tags = local\.tags',
            network,
            re.DOTALL,
        )
    bootstrap = _text("bootstrap/main.tf")
    bootstrap_outputs = _text("bootstrap/outputs.tf")
    assert 'resource "aws_iam_policy" "ecs_permissions_boundary"' in bootstrap
    assert 'output "iam_permissions_boundary_arn"' in bootstrap_outputs
    assert '"${aws_cloudwatch_log_group.operator.arn}:log-stream:*"' in iam
    assert '"arn:aws:bedrock:*::foundation-model/*"' in bootstrap
    assert statements["DedicatedAccountOnlyUntaggedMutations"]["Action"] == [
        "logs:DeleteResourcePolicy",
        "logs:PutResourcePolicy",
    ]
    network_relationships = statements["ManageRunEc2NetworkRelationships"]
    assert network_relationships["Resource"] != "*"
    assert network_relationships["Condition"]["StringEquals"] == {
        "aws:ResourceTag/ACCEPTANCE_RUN_ID": values["run_id"],
        "aws:RequestedRegion": values["aws_region"],
    }
    assert not {
        "ec2:CreateSecurityGroupEgressRule",
        "ec2:CreateSecurityGroupIngressRule",
        "ec2:DeleteSecurityGroupRule",
    }.intersection(mutation_actions)
    assert {
        "AuthorizeOnRunSecurityGroups",
        "CreateRunTaggedSecurityGroupRules",
        "ManageRunCognitoChildren",
        "ManageRunEfsMountTargets",
        "ManageRunEventTargets",
    }.issubset(statements)
    assert "repository/elspeth-*" not in template
    assert "arn:aws:s3:::elspeth-*" not in template
    readme = _text("README.md")
    for name in values:
        assert f"${{{name}}}" in readme
    assert "dedicated empty account" in readme
    assert "not supported in a shared account" in readme


def test_iam_lifecycle_policy_and_provider_cannot_mutate_or_activate_role_permissions() -> None:
    template = _text("iam/lifecycle-policy.json.tftpl")
    values = {
        "aws_account_id": "123456789012",
        "run_id": "12345678-1234-4123-8123-123456789abc",
        "iam_permissions_boundary_arn": ("arn:aws:iam::123456789012:policy/elspeth-12345678-1234-4123-8123-123456789abc-ecs-boundary"),
    }
    rendered = template
    for name, value in values.items():
        rendered = rendered.replace(f"${{{name}}}", value)
    assert "${" not in rendered
    policy = json.loads(rendered)
    statements = {statement["Sid"]: statement for statement in policy["Statement"]}

    create_boundary = statements["CreateRunBoundary"]
    manage_boundary = statements["ManageRunBoundary"]
    assert create_boundary["Resource"] == values["iam_permissions_boundary_arn"]
    assert manage_boundary["Resource"] == values["iam_permissions_boundary_arn"]
    assert {
        "iam:CreatePolicyVersion",
        "iam:DeletePolicy",
        "iam:DeletePolicyVersion",
        "iam:SetDefaultPolicyVersion",
    }.issubset(manage_boundary["Action"])

    role_resources = [
        "arn:aws:iam::123456789012:role/a-*-task-role",
        "arn:aws:iam::123456789012:role/a-*-execution-role",
        "arn:aws:iam::123456789012:role/b-*-task-role",
        "arn:aws:iam::123456789012:role/b-*-execution-role",
    ]
    create_roles = statements["CreateRunRolesWithNarrowBoundary"]
    assert create_roles["Resource"] == role_resources
    assert create_roles["Condition"]["StringEquals"] == {
        "aws:RequestTag/ACCEPTANCE_RUN_ID": values["run_id"],
        "iam:PermissionsBoundary": values["iam_permissions_boundary_arn"],
    }
    delete_roles = statements["DeleteRunRoleLifecycle"]
    assert delete_roles["Resource"] == role_resources
    assert delete_roles["Condition"]["StringEquals"] == {"aws:ResourceTag/ACCEPTANCE_RUN_ID": values["run_id"]}
    assert {
        "iam:DeleteRole",
        "iam:DeleteRolePermissionsBoundary",
        "iam:TagRole",
        "iam:UntagRole",
    }.issubset(delete_roles["Action"])

    forbidden = {
        "ecs:RunTask",
        "iam:AttachRolePolicy",
        "iam:PassRole",
        "iam:PutRolePolicy",
        "iam:PutRolePermissionsBoundary",
        "iam:UpdateAssumeRolePolicy",
        "sts:AssumeRole",
    }
    allowed_actions = {action for statement in policy["Statement"] if statement["Effect"] == "Allow" for action in statement["Action"]}
    denied_actions = {action for statement in policy["Statement"] if statement["Effect"] == "Deny" for action in statement["Action"]}
    assert not forbidden.intersection(allowed_actions)
    assert forbidden.issubset(denied_actions)

    bootstrap_versions = _text("bootstrap/versions.tf")
    module_versions = _text("modules/scenario/versions.tf")
    iam_resources = _text("modules/scenario/iam_observability.tf")
    assert 'alias               = "iam_lifecycle"' in bootstrap_versions
    assert "configuration_aliases = [aws.iam_lifecycle]" in module_versions
    assert iam_resources.count("provider             = aws.iam_lifecycle") == 2
    for scenario in ("scenario-a", "scenario-b"):
        versions = _text(f"{scenario}/versions.tf")
        main = _text(f"{scenario}/main.tf")
        assert 'alias               = "iam_lifecycle"' in versions
        assert "aws.iam_lifecycle = aws.iam_lifecycle" in main
    readme = _text("README.md")
    assert "separate IAM lifecycle principal" in readme
    assert "terraform destroy" in readme
    assert "-target" not in readme
    assert "terraform state rm" not in readme


def test_scenario_a_native_mock_plan_uses_only_documented_minimal_inputs() -> None:
    native_test = PACKAGE / "scenario-a" / "codeblind.tftest.hcl"
    assert native_test.is_file()
    content = native_test.read_text(encoding="utf-8")
    for provider in ("aws", "random", "tls"):
        assert f'mock_provider "{provider}"' in content
    assert 'alias = "iam_lifecycle"' in content
    assert "iam_lifecycle_aws_profile" in content
    assert "iam_permissions_boundary_arn" in content
    for forbidden in (
        "rollback_baseline_image",
        "rollback_baseline_sha",
        "scenario_tf_dir",
        "scenario_tf_vars",
        "scenario_tf_binding_file",
        "transaction_search_baseline_sha256",
    ):
        assert forbidden not in content

    result = subprocess.run(
        ["terraform", f"-chdir={PACKAGE / 'scenario-a'}", "test", "-filter=codeblind.tftest.hcl", "-no-color"],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Success!" in result.stdout


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


def test_cloudwatch_agent_sidecar_exactly_matches_runtime_admission_constants() -> None:
    ecs = _text("modules/scenario/ecs.tf")
    module_locals = _text("modules/scenario/locals.tf")
    sidecar = ecs[ecs.index("cloudwatch_agent_container = {") : ecs.index("candidate_web_container = {")]
    observed_env_names = frozenset(re.findall(r'\{ name = "([^"]+)", value = ', sidecar))

    assert observed_env_names == task_definition._CLOUDWATCH_AGENT_ENV_NAMES
    command_line = next(line for line in module_locals.splitlines() if line.strip().startswith("cw_agent_command"))
    assert json.loads(command_line.split("=", 1)[1].strip()) == task_definition._CLOUDWATCH_AGENT_COMMAND

    expected_health = task_definition._CLOUDWATCH_AGENT_HEALTH_CHECK
    assert json.dumps(expected_health["command"][1]) in sidecar
    for name in ("interval", "timeout", "retries", "startPeriod"):
        assert re.search(rf"\b{name}\s*=\s*{expected_health[name]}\b", sidecar)

    assert "cw_agent_config_json_sha256 = sha256(local.cw_agent_json)" in module_locals
    assert "cw_agent_otel_yaml_sha256   = sha256(local.cw_agent_otel)" in module_locals
    assert '{ name = "ELSPETH_CW_AGENT_CONFIG_JSON_SHA256", value = local.cw_agent_config_json_sha256 }' in sidecar
    assert '{ name = "ELSPETH_CW_AGENT_OTEL_YAML_SHA256", value = local.cw_agent_otel_yaml_sha256 }' in sidecar


def test_cold_install_pauses_service_until_two_stage_doctor_and_explicit_enable() -> None:
    ecs = _text("modules/scenario/ecs.tf")
    readme = _text("README.md")

    assert re.search(r'resource "aws_ecs_service" "web"\s*\{.*?desired_count\s*=\s*0', ecs, re.DOTALL)
    schema_init = readme.index("doctor aws-ecs --init-schema --json")
    ordinary_doctor = readme.index("doctor aws-ecs --json", schema_init)
    explicit_enable = readme.index("service_enable_command", ordinary_doctor)
    assert schema_init < ordinary_doctor < explicit_enable


def test_schema_init_and_runtime_doctors_have_separate_credentials_and_commands() -> None:
    ecs = _text("modules/scenario/ecs.tf")
    outputs = _text("modules/scenario/outputs.tf")
    root_outputs = "\n".join((_text("scenario-a/outputs.tf"), _text("scenario-b/outputs.tf")))
    readme = _text("README.md")

    schema_container = ecs[ecs.index("schema_init_doctor_container = {") : ecs.index("runtime_doctor_container = {")]
    runtime_container = ecs[ecs.index("runtime_doctor_container = {") : ecs.index("payload_container = {")]
    assert 'command                = ["doctor", "aws-ecs", "--init-schema", "--json"]' in schema_container
    assert "secrets                = local.schema_owner_secrets" in schema_container
    assert 'command                = ["doctor", "aws-ecs", "--json"]' in runtime_container
    assert "secrets                = local.runtime_secrets" in runtime_container
    assert 'resource "aws_ecs_task_definition" "schema_init_doctor"' in ecs
    assert 'resource "aws_ecs_task_definition" "runtime_doctor"' in ecs
    assert "DOCTOR_TASK_DEFINITION                          = aws_ecs_task_definition.runtime_doctor.arn" in outputs

    for name in (
        "schema_init_doctor_task_definition_arn",
        "schema_init_doctor_network_configuration",
        "schema_init_doctor_overrides",
        "runtime_doctor_task_definition_arn",
        "runtime_doctor_network_configuration",
        "runtime_doctor_overrides",
    ):
        assert f'output "{name}"' in outputs
        assert f'output "{name}"' in root_outputs
        assert name in readme
    schema_init = readme.index("doctor aws-ecs --init-schema --json")
    runtime_doctor = readme.index("doctor aws-ecs --json", schema_init)
    both_exit_zero = readme.index("Both task exit codes must be `0`", runtime_doctor)
    explicit_enable = readme.index("service_enable_command", both_exit_zero)
    assert schema_init < runtime_doctor < both_exit_zero < explicit_enable


def test_acceptance_verifier_containers_use_the_live_published_identity() -> None:
    validator_source = inspect.getsource(task_definition.validate_task_definition_policy_binding)
    published_user_match = re.search(r'expected_user\s*!=\s*"([^"]+)"', validator_source)
    assert published_user_match is not None
    published_user = published_user_match.group(1)

    ecs = _text("modules/scenario/ecs.tf")
    payload = ecs[ecs.index("payload_container = {") : ecs.index("local_auth_container = {")]
    local_auth = ecs[ecs.index("local_auth_container = {") : ecs.index("rollback_environment = [")]
    for container in (payload, local_auth):
        assert re.search(rf'\buser\s*=\s*"{re.escape(published_user)}"', container)


def test_scenario_b_inventory_has_a_validated_nonempty_cognito_subject() -> None:
    module_variables = _text("modules/scenario/variables.tf")
    outputs = _text("modules/scenario/outputs.tf")
    scenario_a_variables = _text("scenario-a/variables.tf")
    scenario_b_variables = _text("scenario-b/variables.tf")
    scenario_b_example = _text("examples/scenario-b.tfvars.example")

    assert 'variable "cognito_subject_sub"' in module_variables
    assert 'var.scenario_id == "A" ? var.cognito_subject_sub == ""' in module_variables
    assert 'cognito_subject_sub             = var.scenario_id == "B" ? var.cognito_subject_sub : ""' in outputs
    assert re.search(r'variable "cognito_subject_sub".*?default\s*=\s*""', scenario_a_variables, re.DOTALL)
    assert re.search(
        r'variable "cognito_subject_sub".*?condition\s*=\s*\(\s*'
        r"length\(trimspace\(var\.cognito_subject_sub\)\)\s*>\s*0",
        scenario_b_variables,
        re.DOTALL,
    )
    assert re.search(
        r'cognito_subject_sub\s*=\s*"REPLACE_WITH_SCENARIO_B_COGNITO_SUBJECT"',
        scenario_b_example,
    )


def test_explicit_aws_profile_is_bound_across_provider_backend_and_local_cli() -> None:
    module_variables = _text("modules/scenario/variables.tf")
    module_outputs = _text("modules/scenario/outputs.tf")
    database_bootstrap = _text("modules/scenario/database_bootstrap.tf")
    readme = _text("README.md")

    assert 'variable "aws_profile"' in module_variables
    for root in ("bootstrap", "scenario-a", "scenario-b"):
        variables = _text(f"{root}/variables.tf")
        provider = _text(f"{root}/versions.tf")
        example = _text(f"examples/{root}.tfvars.example")
        assert 'variable "aws_profile"' in variables
        assert "profile             = var.aws_profile" in provider
        assert "aws_profile" in example
    for scenario in ("scenario-a", "scenario-b"):
        assert re.search(
            r'profile\s*=\s*"REPLACE_WITH_AWS_PROFILE"',
            _text(f"examples/{scenario}.s3.tfbackend.example"),
        )
        assert re.search(r"\baws_profile\s+=\s+var\.aws_profile\b", _text(f"{scenario}/main.tf"))

    assert re.search(r"\bAWS_PROFILE\s*=\s*var\.aws_profile\b", database_bootstrap)
    assert database_bootstrap.count('--profile "$AWS_PROFILE"') == 3
    assert 'aws --profile "$AWS_PROFILE" --region "$AWS_REGION"' in readme
    assert "--profile ${jsonencode(var.aws_profile)}" in module_outputs
    assert "--region ${jsonencode(var.aws_region)}" in module_outputs


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
        "schema_init_doctor_task_definition_arn",
        "schema_init_doctor_network_configuration",
        "runtime_doctor_task_definition_arn",
        "runtime_doctor_network_configuration",
        "service_enable_command",
        "resolved_inventory",
        "teardown",
    ):
        assert name in combined


def test_examples_and_policy_are_parseable_and_contain_no_local_or_account_data() -> None:
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
