"""Contract tests for the supported AWS ECS Terraform source package."""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml

from elspeth.web._aws_ecs_acceptance import scenario_inventory, task_definition
from elspeth.web.config import WebSettings
from elspeth.web.provider_config_policy import web_aws_s3_source_policy_error


def _require_terraform(reason: str) -> None:
    """Skip locally when terraform is absent; fail loudly in CI.

    A silent CI skip proved nothing about the shipped package
    (elspeth-af1efcb8d8). The workflow installs a checksum-pinned
    terraform, so absence under GITHUB_ACTIONS is a broken gate, not an
    environment quirk.
    """
    if shutil.which("terraform") is not None:
        return
    if os.environ.get("GITHUB_ACTIONS"):
        pytest.fail(f"terraform binary is missing in CI: {reason}")
    pytest.skip(f"terraform is not installed, so {reason}")


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
    "examples/scenario-c.s3.tfbackend.example",
    "examples/scenario-c.tfvars.example",
    "iam/installer-control-plane-policy.json.tftpl",
    "iam/installer-regional-resources-policy.json.tftpl",
    "iam/installer-relationships-policy.json.tftpl",
    "iam/installer-runtime-observation-policy.json.tftpl",
    "iam/lifecycle-policy.json.tftpl",
    "modules/scenario/database_bootstrap.tf",
    "modules/scenario/ecs.tf",
    "modules/scenario/image_provenance.tf",
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
    "scenario-a/web_policy.tftest.hcl",
    "scenario-a/main.tf",
    "scenario-a/outputs.tf",
    "scenario-a/variables.tf",
    "scenario-a/versions.tf",
    "scenario-b/.terraform.lock.hcl",
    "scenario-b/main.tf",
    "scenario-b/outputs.tf",
    "scenario-b/variables.tf",
    "scenario-b/versions.tf",
    "scenario-c/.terraform.lock.hcl",
    "scenario-c/codeblind-compatibility.json",
    "scenario-c/gateway_contract.tftest.hcl",
    "scenario-c/main.tf",
    "scenario-c/outputs.tf",
    "scenario-c/variables.tf",
    "scenario-c/versions.tf",
    "scripts/validate-ecs-run-task.py",
    "telemetry/elspeth.cloudwatch-agent.v1/elspeth.cloudwatch-agent.v1.json",
    "telemetry/elspeth.cloudwatch-agent.v1/elspeth.cloudwatch-agent.v1.otel.yaml",
}


def _source_files(root: Path = PACKAGE) -> list[Path]:
    """Enumerate the package as git would ship it.

    Deliberately NOT a filesystem walk. The package README instructs an
    operator to create their real `examples/*.tfvars` and `*.tfbackend`
    inputs inside this directory, and the package's own `.gitignore`
    excludes them. A walk counts those local files as package source, so
    every operator who follows the README turns this suite red — and the
    account-data assertion below fires on their own legitimately-ignored
    credentials rather than on a genuine leak. Asking git for tracked plus
    untracked-but-not-ignored files tests exactly the set that would be
    committed: a new package file an author forgot to `git add` is still
    caught, while operator inputs are correctly out of scope.
    """
    if (REPO_ROOT / ".git").exists():
        listing = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "."],
            cwd=root,
            capture_output=True,
            check=True,
            text=True,
        )
        candidates = (root / name for name in listing.stdout.split("\0") if name)
    else:
        # A source archive has no Git metadata and, by construction, contains
        # only tracked files. Walking it is therefore the exact shipped set,
        # not the ignored-operator-input hazard described above.
        candidates = root.rglob("*")
    return sorted(
        path
        for path in candidates
        if path.is_file()
        and ".terraform" not in path.relative_to(root).parts
        and not path.name.endswith((".tfstate", ".tfplan"))
        and ".tfstate." not in path.name
    )


def _text(relative: str) -> str:
    return (PACKAGE / relative).read_text(encoding="utf-8")


def _all_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in _source_files())


_INSTALLER_POLICY_VALUES = {
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
}


def _installer_policy_template_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted((PACKAGE / "iam").glob("installer*.json.tftpl")))


def _render_installer_policy_documents(
    values: dict[str, str] | None = None,
) -> list[tuple[Path, str, dict[str, Any]]]:
    substitutions = _INSTALLER_POLICY_VALUES if values is None else values
    documents: list[tuple[Path, str, dict[str, Any]]] = []
    for path in sorted((PACKAGE / "iam").glob("installer*.json.tftpl")):
        rendered = path.read_text(encoding="utf-8")
        for name, value in substitutions.items():
            rendered = rendered.replace(f"${{{name}}}", value)
        assert "${" not in rendered
        documents.append((path, rendered, json.loads(rendered)))
    return documents


_ECR_REPOSITORY_VARIABLES = (
    ("bootstrap/variables.tf", "ecr_repository"),
    ("bootstrap/variables.tf", "cloudwatch_agent_ecr_repository"),
    ("bootstrap/variables.tf", "gateway_ecr_repository"),
    ("modules/scenario/variables.tf", "candidate_ecr_repository"),
    ("modules/scenario/variables.tf", "cloudwatch_agent_ecr_repository"),
    ("modules/scenario/variables.tf", "gateway_ecr_repository"),
    ("scenario-a/variables.tf", "candidate_ecr_repository"),
    ("scenario-a/variables.tf", "cloudwatch_agent_ecr_repository"),
    ("scenario-b/variables.tf", "candidate_ecr_repository"),
    ("scenario-b/variables.tf", "cloudwatch_agent_ecr_repository"),
    ("scenario-c/variables.tf", "candidate_ecr_repository"),
    ("scenario-c/variables.tf", "cloudwatch_agent_ecr_repository"),
    ("scenario-c/variables.tf", "gateway_ecr_repository"),
)
_ECR_REPOSITORY_PATTERN = r"^elspeth-[a-z0-9]+((\\.|_|__|-+)[a-z0-9]+)*(/[a-z0-9]+((\\.|_|__|-+)[a-z0-9]+)*)*$"


def test_all_ecr_repository_inputs_enforce_the_same_aws_contract() -> None:
    expected_contract = f'length(var.repository) <= 256 && can(regex("{_ECR_REPOSITORY_PATTERN}", var.repository))'

    for relative, variable_name in _ECR_REPOSITORY_VARIABLES:
        match = re.search(rf'variable "{variable_name}" \{{.*?\n\}}\n', _text(relative), re.DOTALL)
        assert match is not None, f"{relative} no longer declares {variable_name}"
        condition = re.search(
            r"\bcondition\s*=\s*(?P<value>.*?)\n\s*error_message\s*=",
            match.group(0),
            re.DOTALL,
        )
        assert condition is not None
        normalized = " ".join(condition.group("value").split()).replace(f"var.{variable_name}", "var.repository")
        if relative == "modules/scenario/variables.tf" and variable_name == "gateway_ecr_repository":
            assert normalized == (f'( var.llm_backend == "custom_gateway" ? {expected_contract} : var.repository == "" )'), (
                f"{relative}:{variable_name} has drifted from the backend-gated ECR repository contract"
            )
        elif relative == "bootstrap/variables.tf" and variable_name == "gateway_ecr_repository":
            assert normalized == f'var.repository == "" || ({expected_contract})', (
                f"{relative}:{variable_name} has drifted from the optional bootstrap ECR repository contract"
            )
        else:
            assert normalized == expected_contract, f"{relative}:{variable_name} has drifted from the ECR repository contract"


@pytest.mark.parametrize(
    ("repository", "accepted"),
    [
        pytest.param("elspeth-" + ("a" * 248), True, id="maximum-length"),
        pytest.param("elspeth-" + ("a" * 249), False, id="over-maximum-length"),
        pytest.param("elspeth-app/agent_1", True, id="valid-segments"),
        pytest.param("elspeth-app/", False, id="trailing-slash"),
        pytest.param("elspeth-app//agent", False, id="empty-path-segment"),
        pytest.param("elspeth-app..agent", False, id="consecutive-dot"),
        pytest.param("elspeth-app._agent", False, id="mixed-separators"),
        pytest.param("elspeth-App", False, id="uppercase"),
    ],
)
def test_ecr_repository_validation_matches_aws_name_contract(tmp_path: Path, repository: str, accepted: bool) -> None:
    _require_terraform("the ECR repository validator must execute")
    variables = _text("bootstrap/variables.tf")
    match = re.search(r'variable "ecr_repository" \{.*?\n\}\n', variables, re.DOTALL)
    assert match is not None
    (tmp_path / "main.tf").write_text(match.group(0), encoding="utf-8")
    subprocess.run(
        ["terraform", f"-chdir={tmp_path}", "init", "-backend=false", "-input=false", "-no-color"],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    result = subprocess.run(
        ["terraform", f"-chdir={tmp_path}", "plan", "-input=false", "-no-color", f"-var=ecr_repository={repository}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("bucket", "accepted"),
    [
        pytest.param("elspeth-" + ("a" * 55), True, id="maximum-length"),
        pytest.param("elspeth-" + ("a" * 56), False, id="over-maximum-length"),
        pytest.param("elspeth-state..bucket", False, id="adjacent-periods"),
        pytest.param("elspeth-state-s3alias", False, id="access-point-alias-suffix"),
        pytest.param("elspeth-state--ol-s3", False, id="object-lambda-alias-suffix"),
        pytest.param("elspeth-state.mrap", False, id="multi-region-access-point-suffix"),
        pytest.param("elspeth-state--x-s3", False, id="directory-bucket-suffix"),
        pytest.param("elspeth-state--table-s3", False, id="table-bucket-suffix"),
    ],
)
def test_backend_state_bucket_validation_enforces_s3_length_limit(tmp_path: Path, bucket: str, accepted: bool) -> None:
    _require_terraform("the backend state bucket validator must execute")
    variables = _text("bootstrap/variables.tf")
    match = re.search(r'variable "backend_state_bucket" \{.*?\n\}\n', variables, re.DOTALL)
    assert match is not None
    assert "length(var.backend_state_bucket) <= 63" in match.group(0)
    (tmp_path / "main.tf").write_text(match.group(0), encoding="utf-8")
    subprocess.run(
        ["terraform", f"-chdir={tmp_path}", "init", "-backend=false", "-input=false", "-no-color"],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    result = subprocess.run(
        ["terraform", f"-chdir={tmp_path}", "plan", "-input=false", "-no-color", f"-var=backend_state_bucket={bucket}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr


def test_scenario_a_https_ingress_is_explicit_and_never_world_open() -> None:
    module_variables = _text("modules/scenario/variables.tf")
    network = _text("modules/scenario/network.tf")
    scenario_a = _text("scenario-a/main.tf")
    example = _text("examples/scenario-a.tfvars.example")
    alb_ingress = re.search(
        r'resource "aws_vpc_security_group_ingress_rule" "alb_https" \{(?P<body>.*?)\n\}',
        network,
        re.DOTALL,
    )

    assert 'variable "alb_https_ingress_cidrs"' in module_variables
    assert 'cidrnetmask(cidr) != "0.0.0.0"' in module_variables
    assert alb_ingress is not None
    assert re.search(r"for_each\s*=\s*toset\(var\.alb_https_ingress_cidrs\)", alb_ingress.group("body"))
    assert re.search(r"cidr_ipv4\s*=\s*each\.value", alb_ingress.group("body"))
    assert '"0.0.0.0/0"' not in alb_ingress.group("body")
    assert re.search(r"alb_https_ingress_cidrs\s*=\s*var\.alb_https_ingress_cidrs", scenario_a)
    assert 'alb_https_ingress_cidrs = ["REPLACE_WITH_OPERATOR_CIDR"]' in example


_ALB_INGRESS_ROOTS = (
    "modules/scenario/variables.tf",
    "scenario-a/variables.tf",
    "scenario-b/variables.tf",
)

# (candidate value, must terraform accept it)
_ALB_INGRESS_CASES: tuple[tuple[str, bool], ...] = (
    ('["203.0.113.5/32"]', True),
    ('["203.0.113.0/24", "198.51.100.0/24"]', True),
    ("[]", False),
    ('["nonsense"]', False),
    ('["203.0.113.5/32", "203.0.113.5/32"]', False),
    # Semantically identical networks written differently: both render the same
    # EC2 rule, so the second is a duplicate `distinct()` on raw strings misses.
    ('["10.0.0.5/8", "10.0.0.6/8"]', False),
    ('["0.0.0.0/0"]', False),
    # Leading zeros in the prefix. `cidrnetmask` parses these as /0 and EC2
    # canonicalises them back to 0.0.0.0/0, so a raw-suffix check on "/0" lets
    # them through and the fail-closed allowlist opens HTTPS to the internet.
    ('["0.0.0.0/00"]', False),
    ('["0.0.0.0/000"]', False),
    ('["10.0.0.0/0"]', False),
    ('["10.0.0.0/00"]', False),
    # IPv4-only is the enforced contract: `cidrnetmask` errors on IPv6, so
    # every IPv6 CIDR is rejected. Fail-closed, and the message says so.
    ('["::/0"]', False),
    ('["2001:db8::/32"]', False),
    # A union of broader-than-/8 prefixes reopens the world without any
    # single entry spelling /0: the guard enforces a /8 prefix floor.
    ('["0.0.0.0/1", "128.0.0.0/1"]', False),
    ('["64.0.0.0/2"]', False),
    ('["10.0.0.0/7"]', False),
)


def test_alb_ingress_guard_rejects_every_spelling_of_a_world_open_cidr(tmp_path: Path) -> None:
    """The `/0` guard must test the PARSED prefix, not the raw string suffix.

    `!endswith(cidr, "/0")` accepted `0.0.0.0/00` and `0.0.0.0/000`:
    `cidrnetmask` parses the leading zeros and EC2 canonicalises the rule back
    to `0.0.0.0/0`, so the explicit operator allowlist silently became a
    world-open HTTPS ingress rule. Uniqueness had the same shape of hole —
    `distinct()` over raw strings let `10.0.0.5/8` and `10.0.0.6/8` both
    through, though they build one and the same network.

    The condition is duplicated across the module and both scenario roots
    (each root validates the operator's own tfvars before the module sees
    them), so this asserts the three blocks are byte-identical and then
    exercises the one shared block for real: re-encoding the rule in three
    places is exactly how the drift starts.
    """
    blocks = {}
    for relative in _ALB_INGRESS_ROOTS:
        match = re.search(
            r'variable "alb_https_ingress_cidrs" \{.*?\n\}\n',
            _text(relative),
            re.DOTALL,
        )
        assert match is not None, f"{relative} no longer declares alb_https_ingress_cidrs"
        blocks[relative] = match.group(0)
    assert len(set(blocks.values())) == 1, (
        "the alb_https_ingress_cidrs guard has drifted between "
        + ", ".join(_ALB_INGRESS_ROOTS)
        + "; every root must reject exactly what the module rejects"
    )

    _require_terraform("the ingress guard cannot be exercised")

    (tmp_path / "main.tf").write_text(next(iter(blocks.values())), encoding="utf-8")
    init = subprocess.run(
        ["terraform", f"-chdir={tmp_path}", "init", "-backend=false", "-input=false", "-no-color"],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert init.returncode == 0, "terraform init failed:\n" + init.stdout + init.stderr

    for value, should_accept in _ALB_INGRESS_CASES:
        result = subprocess.run(
            [
                "terraform",
                f"-chdir={tmp_path}",
                "plan",
                "-input=false",
                "-no-color",
                f"-var=alb_https_ingress_cidrs={value}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        accepted = result.returncode == 0
        assert accepted == should_accept, (
            f"alb_https_ingress_cidrs={value} was "
            + ("accepted" if accepted else "rejected")
            + "; expected it to be "
            + ("accepted" if should_accept else "rejected")
            + "\n"
            + result.stdout
            + result.stderr
        )


def test_service_enable_command_pins_the_validated_task_revision() -> None:
    outputs = _text("modules/scenario/outputs.tf")
    command = re.search(r'output "service_enable_command" \{(?P<body>.*?)\n\}', outputs, re.DOTALL)

    assert command is not None
    assert "--task-definition" in command.group("body")
    assert "aws_ecs_task_definition.candidate_web.arn" in command.group("body")
    assert "--force-new-deployment" in command.group("body")


def test_database_names_are_safe_and_distinct_before_resource_creation() -> None:
    variables = _text("modules/scenario/variables.tf")

    for name in ("database_name", "session_database_name", "landscape_database_name"):
        block = re.search(rf'variable "{name}" \{{(?P<body>.*?)\n\}}', variables, re.DOTALL)
        assert block is not None
        assert "validation" in block.group("body")
        assert "^[A-Za-z_][A-Za-z0-9_]{0,62}$" in block.group("body")
    landscape = re.search(r'variable "landscape_database_name" \{(?P<body>.*?)\n\}', variables, re.DOTALL)
    assert landscape is not None
    assert "var.landscape_database_name != var.session_database_name" in landscape.group("body")


def test_database_bootstrap_waits_for_network_and_execution_role_policies() -> None:
    bootstrap = _text("modules/scenario/database_bootstrap.tf")
    resource = re.search(r'resource "terraform_data" "database_bootstrap" \{(?P<body>.*)', bootstrap, re.DOTALL)

    assert resource is not None
    for dependency in (
        "aws_route_table_association.public",
        "aws_iam_role_policy_attachment.execution_managed",
        "aws_iam_role_policy.execution_secrets",
    ):
        assert dependency in resource.group("body")


def test_database_bootstrap_admits_full_run_task_json_before_using_the_task_arn() -> None:
    bootstrap = _text("modules/scenario/database_bootstrap.tf")

    assert '--output json >"$work/run.json"' in bootstrap
    assert "--query 'tasks[0].taskArn'" not in bootstrap
    assert 'python3 "$RUN_TASK_VALIDATOR"' in bootstrap
    assert '<"$work/run.json"' in bootstrap
    assert 'RUN_TASK_VALIDATOR    = abspath("${path.module}/../../scripts/validate-ecs-run-task.py")' in bootstrap
    assert 'filesha256("${path.module}/../../scripts/validate-ecs-run-task.py")' in bootstrap
    assert "AWS_ACCOUNT_ID        = var.aws_account_id" in bootstrap
    assert 'AWS_PARTITION         = "aws"' in bootstrap
    assert 'test -n "$task_arn"' not in bootstrap


def test_ecs_identity_wrapper_delegates_to_the_strict_metadata_boundary() -> None:
    ecs = _text("modules/scenario/ecs.tf")
    locals_text = _text("modules/scenario/locals.tf")
    wrapper = ecs[ecs.index("ecs_identity_wrapper = <<-SHELL") : ecs.index("  SHELL", ecs.index("ecs_identity_wrapper"))]

    assert "python -m elspeth.web._aws_ecs_acceptance.ecs_metadata" in wrapper
    assert "urllib.request" not in wrapper
    assert "$${identity% *}" in wrapper
    assert "$${identity#* }" in wrapper
    assert 'name = "ELSPETH_ACCEPTANCE_AWS_ACCOUNT_ID", value = var.aws_account_id' in locals_text
    for family in ("schema_init_doctor_family", "runtime_doctor_family", "rollback_doctor_family"):
        assert f"value = local.{family}" in ecs


def test_container_insights_performance_log_group_is_terraform_owned() -> None:
    locals_text = _text("modules/scenario/locals.tf")
    observability = _text("modules/scenario/iam_observability.tf")
    ecs = _text("modules/scenario/ecs.tf")

    assert re.search(
        r'container_insights_log_group\s*=\s*"/aws/ecs/containerinsights/\$\{local\.cluster_name\}/performance"',
        locals_text,
    )
    assert 'resource "aws_cloudwatch_log_group" "container_insights"' in observability
    assert "depends_on = [aws_cloudwatch_log_group.container_insights]" in ecs

    # R2-D3 (elspeth-a229c247a1): ECS's service-linked role re-creates this
    # exact log-group name minutes after the cluster goes INACTIVE (a final
    # Container Insights flush), leaving an untagged, unmanaged orphan that
    # blocks the next apply's CreateLogGroup with ResourceAlreadyExistsException.
    # A depends_on ordering fix cannot help — the collision happens after
    # destroy completes. Each root offers a variable-gated `import` block so
    # an operator can formally adopt the orphan back into state on a
    # same-namespace redeploy retry, default false so fresh accounts are
    # unaffected.
    for scenario in ("scenario-a", "scenario-b", "scenario-c"):
        main = _text(f"{scenario}/main.tf")
        variables = _text(f"{scenario}/variables.tf")

        import_block = re.search(r"import \{(?P<body>.*?)\n\}", main, re.DOTALL)
        assert import_block is not None, f"{scenario}/main.tf is missing the container-insights import block"
        body = import_block.group("body")
        assert "to       = module.scenario.aws_cloudwatch_log_group.container_insights" in body
        assert "var.adopt_container_insights_log_group" in body
        assert "containerinsights" in body
        assert "performance" in body

        variable_block = re.search(
            r'variable "adopt_container_insights_log_group" \{(?P<body>.*?)\n\}',
            variables,
            re.DOTALL,
        )
        assert variable_block is not None, f"{scenario}/variables.tf is missing adopt_container_insights_log_group"
        assert re.search(r"type\s*=\s*bool", variable_block.group("body"))
        assert re.search(r"default\s*=\s*false", variable_block.group("body"))

    cold_install = (REPO_ROOT / "docs" / "runbooks" / "aws-ecs-cold-install.md").read_text(encoding="utf-8")
    teardown_section = re.search(r"\n## Teardown\n(?P<body>.*)", cold_install, re.DOTALL)
    assert teardown_section is not None, "cold-install runbook is missing its Teardown section"
    teardown_body = teardown_section.group("body")
    assert "containerinsights" in teardown_body
    assert "delete-log-group" in teardown_body
    assert "describe-log-groups" in teardown_body


def test_container_insights_adoption_guidance_requires_a_replacement_plan() -> None:
    cold_install = (REPO_ROOT / "docs" / "runbooks" / "aws-ecs-cold-install.md").read_text(encoding="utf-8")
    deployment = (REPO_ROOT / "docs" / "runbooks" / "aws-ecs-deployment.md").read_text(encoding="utf-8")
    readme = _text("README.md")

    for text in (
        cold_install,
        deployment,
        readme,
        _text("scenario-a/main.tf"),
        _text("scenario-b/main.tf"),
        _text("scenario-c/main.tf"),
    ):
        normalized = " ".join(text.split())
        assert "replacement plan" in normalized
        assert "on that apply" not in normalized
    assert "obtain a new signed approval" in deployment


def test_installer_secret_reads_are_bound_to_the_three_exact_run_namespaces() -> None:
    template = _installer_policy_template_text()
    secret_resources = re.search(r'"Sid": "ReadScenarioSecretValues"(?P<body>.*?)\n    \}', template, re.DOTALL)

    assert secret_resources is not None
    assert "secret:${scenario_a_namespace}-*" in secret_resources.group("body")
    assert "secret:${scenario_b_namespace}-*" in secret_resources.group("body")
    assert "secret:${scenario_c_namespace}-*" in secret_resources.group("body")
    assert "secret:a-*" not in secret_resources.group("body")
    assert "secret:b-*" not in secret_resources.group("body")
    assert "secret:c-*" not in secret_resources.group("body")


def test_live_composer_generation_is_not_a_cold_install_gate() -> None:
    readme = _text("README.md")

    assert "Optional non-gating Composer soak" in readme
    assert "does not invalidate the cold install" in readme


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
        r"default_plugin_allowlist\s*=\s*\[(?P<body>.*?)\]",
        locals_text,
        re.DOTALL,
    )

    assert allowlist_match is not None
    assert '"transform:aws_textract_document_analysis"' in allowlist_match.group("body")


def test_scenario_allowlist_keeps_the_s3_source_authorization_the_web_surface_declines() -> None:
    """``source:aws_s3`` stays in the default allowlist deliberately.

    The deployment authorizes S3 reads for its own runtime (batch/CLI, local
    trained-operator sessions). The web authoring surface refuses them, and
    ``build_plugin_snapshot`` now declines that authorization with
    ``WEB_SURFACE_PROHIBITED`` — so the declared entry surfaces as a visible
    declined authorization instead of a selectable plugin, and deleting the
    line would silently narrow the runtime posture to fix a web-surface
    problem the code already fixes.

    The pairing is what matters: if the code-side ban ever goes away, this
    allowlist entry becomes a live web-surface exposure and the decision must
    be revisited rather than inherited.
    """
    locals_text = _text("modules/scenario/locals.tf")
    allowlist_match = re.search(
        r"default_plugin_allowlist\s*=\s*\[(?P<body>.*?)\]",
        locals_text,
        re.DOTALL,
    )

    assert allowlist_match is not None
    body = allowlist_match.group("body")
    assert '"source:aws_s3"' in body
    assert '"sink:aws_s3"' in body
    assert web_aws_s3_source_policy_error("aws_s3") is not None


def test_web_plugin_policy_is_operator_configurable() -> None:
    """Every protected policy value must be reachable from a module variable.

    These seven settings decide the deployment's safety posture, its LLM
    offering, and which plugins exist at all. Hardcoding them in locals.tf made
    the AWS deployment's posture unchangeable without editing the module, while
    the reference docs described all seven as operator-owned settings.
    """
    variables = _text("modules/scenario/variables.tf")
    locals_text = _text("modules/scenario/locals.tf")

    for name in (
        "plugin_allowlist",
        "plugin_preferences",
        "plugin_control_modes",
        "llm_profiles",
        "default_llm_profile",
        "prompt_guardrail",
        "content_guardrail",
    ):
        assert f'variable "{name}"' in variables, f"{name} must be a module variable"

    # Each one defaults to null and is coalesced against a default_* local, so
    # an unset variable reproduces the previously hardcoded policy.
    for name in ("plugin_allowlist", "plugin_preferences", "plugin_control_modes"):
        assert f"var.{name} == null ? local.default_{name}" in locals_text

    assert "var.llm_profiles == null ? local.default_llm_profile_bindings" in locals_text
    assert 'var.default_llm_profile == null ? "standard"' in locals_text
    assert "var.prompt_guardrail == null ? local.default_prompt_guardrail" in locals_text
    assert "var.content_guardrail == null ? local.default_content_guardrail" in locals_text


def test_bedrock_invoke_grant_is_backend_gated_and_follows_the_configured_llm_profiles() -> None:
    """The IAM grant must be derived from the profiles, not just the Composer pair.

    Deriving it from composer_model/composer_advisor_model alone meant a profile
    naming any third model passed web startup validation and then failed at
    invoke time with AccessDenied. Including the profile models makes that
    failure unreachable instead of merely documented.
    """
    locals_text = _text("modules/scenario/locals.tf")
    grant = re.search(
        r"bedrock_configured_model_ids\s*=\s*local\.bedrock_backend\s*\?\s*distinct\((?P<body>.*?)\)\s*:\s*\[\]",
        locals_text,
        re.DOTALL,
    )

    assert grant is not None, "bedrock_configured_model_ids must be derived only for the Bedrock backend"
    body = grant.group("body")
    assert "var.composer_model" in body
    assert "var.composer_advisor_model" in body
    assert "effective_llm_profile_bindings" in body, "the grant must include every configured profile's model"


def test_guardrail_content_policies_are_rendered_from_variables() -> None:
    """Both Guardrails must take their filter list from configuration.

    The module creates these Guardrails; the operator never supplies an
    identifier. That makes the module the only place their content policy can be
    set, so the filters have to be variable-driven or the deployment's safety
    posture is unchangeable.
    """
    storage = _text("modules/scenario/storage_identity.tf")

    for guardrail in ("prompt", "content"):
        block = re.search(
            rf'resource "aws_bedrock_guardrail" "{guardrail}" \{{(?P<body>.*?)\n\}}',
            storage,
            re.DOTALL,
        )
        assert block is not None
        body = block.group("body")
        assert f"local.effective_{guardrail}_guardrail.filters" in body, f"{guardrail} filters must come from configuration"
        assert f"local.effective_{guardrail}_guardrail.blocked_input_messaging" in body
        assert 'dynamic "filters_config"' in body


def test_inconsistent_web_policy_fails_at_plan_not_at_startup() -> None:
    """A policy the web service rejects at boot must be rejected while planning.

    `expect_failures` in a root tftest cannot name a module-level variable
    validation or a module-internal resource precondition, so these rules are
    pinned here instead. They fire in practice: an invalid control mode, a
    dangling default_llm_profile, a non-Bedrock profile model, and an
    out-of-range Guardrail strength all abort `terraform plan`.
    """
    variables = _text("modules/scenario/variables.tf")
    ecs = _text("modules/scenario/ecs.tf")

    assert 'contains(["required", "recommend"], mode)' in variables
    assert 'startswith(profile.model, "bedrock/")' in variables
    assert 'contains(["NONE", "LOW", "MEDIUM", "HIGH"], filter.input_strength)' in variables
    # A dangling default profile alias is otherwise a web startup failure.
    assert 'var.llm_profiles == null ? ["standard", "fast"] : keys(var.llm_profiles)' in variables

    # Preconditions re-check every combination, including the shipped defaults,
    # which the variable validations deliberately skip.
    candidate = re.search(
        r'resource "aws_ecs_task_definition" "candidate_web" \{(?P<body>.*?)\n\}',
        ecs,
        re.DOTALL,
    )
    assert candidate is not None
    body = candidate.group("body")
    assert "precondition" in body
    assert "effective_plugin_control_modes" in body
    assert "effective_plugin_preferences" in body
    assert "contains(local.effective_authorized_plugin_ids, implementation)" in body


def test_terraform_web_policy_validation_matches_the_closed_runtime_contract() -> None:
    variables = _text("modules/scenario/variables.tf")
    module_locals = _text("modules/scenario/locals.tf")
    ecs = _text("modules/scenario/ecs.tf")

    preference_block = variables[variables.index('variable "plugin_preferences"') : variables.index('variable "plugin_control_modes"')]
    mode_block = variables[variables.index('variable "plugin_control_modes"') : variables.index('variable "llm_profiles"')]
    default_profile_block = variables[variables.index('variable "default_llm_profile"') : variables.index('variable "prompt_guardrail"')]
    closed_capabilities = '["llm", "prompt_shield", "content_safety"]'

    assert closed_capabilities in preference_block
    assert "for capability in keys(var.plugin_preferences)" in preference_block
    assert closed_capabilities in mode_block
    assert "for capability in keys(var.plugin_control_modes)" in mode_block

    assert 'coalesce(var.default_llm_profile, "standard")' in default_profile_block
    assert "var.default_llm_profile == null || contains(" not in default_profile_block

    assert "required_web_plugin_ids = toset([" in module_locals
    for plugin_id in (
        "source:csv",
        "source:json",
        "source:text",
        "sink:csv",
        "sink:json",
        "sink:text",
        "transform:field_mapper",
        "transform:llm",
        "transform:web_scrape",
    ):
        assert f'"{plugin_id}"' in module_locals
    assert "effective_authorized_plugin_ids = setunion(" in module_locals

    candidate = re.search(
        r'resource "aws_ecs_task_definition" "candidate_web" \{(?P<body>.*?)\n\}',
        ecs,
        re.DOTALL,
    )
    assert candidate is not None
    candidate_body = candidate.group("body")
    assert "for implementations in values(local.effective_plugin_preferences)" in candidate_body
    assert "contains(local.effective_authorized_plugin_ids, implementation)" in candidate_body

    authorization = candidate_body[candidate_body.index("for implementations in values") :]
    assert 'if mode == "required"' not in authorization
    assert "preferred implementation must be authorized" in authorization


def test_terraform_and_provider_versions_are_current_and_locked() -> None:
    for relative in (
        "bootstrap/versions.tf",
        "modules/scenario/versions.tf",
        "scenario-a/versions.tf",
        "scenario-b/versions.tf",
        "scenario-c/versions.tf",
    ):
        text = _text(relative)
        assert 'required_version = ">= 1.14, < 2.0"' in text

    for relative in (
        "bootstrap/.terraform.lock.hcl",
        "scenario-a/.terraform.lock.hcl",
        "scenario-b/.terraform.lock.hcl",
        "scenario-c/.terraform.lock.hcl",
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
    for scenario in ("scenario-a", "scenario-b", "scenario-c"):
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


def test_ownership_tags_are_mandatory_and_propagated_by_every_scenario() -> None:
    for scenario in ("scenario-a", "scenario-b", "scenario-c"):
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
        "scenario-c/variables.tf",
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
    assert ecs.count("readonlyRootFilesystem = true") == 5
    assert "readonlyRootFilesystem = true" in bootstrap
    cloudwatch_agent = ecs[ecs.index("cloudwatch_agent_container = {") : ecs.index("gateway_container = {")]
    gateway = ecs[ecs.index("gateway_container = {") : ecs.index("candidate_web_container = {")]
    assert "readonlyRootFilesystem" not in cloudwatch_agent
    assert "readonlyRootFilesystem = true" in gateway
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
    module_locals = _text("modules/scenario/locals.tf")
    assert "var.bedrock_inference_profile_arns" in module_locals
    assert "var.bedrock_foundation_model_arns" in module_locals
    assert "resources = local.bedrock_invoke_model_arns" in iam
    assert '"Resource" = ["*"]' not in iam


def test_cross_region_bedrock_profile_gets_wildcard_region_foundation_model_grant() -> None:
    module_locals = _text("modules/scenario/locals.tf")
    iam = _text("modules/scenario/iam_observability.tf")

    # A cross-region geography composer_model or composer_advisor_model profile is authorized by
    # Bedrock against the underlying foundation model in whichever region it routes to, and that
    # authorization check reports a region-less resource ARN. A single region-pinned
    # foundation-model grant can never match that check, so the module must derive a
    # wildcard-region grant for every configured model that carries one of these prefixes.
    #
    # Assert membership rather than an exact frozen list: the allowlist must grow as AWS ships
    # new geographies, so pinning the whole literal made every addition a test edit with no
    # safety value. Each prefix below is one AWS ships today; losing any of them is the
    # regression worth catching. A model id whose leading label matches neither the geography
    # list nor the provider allowlist no longer fails open at runtime — the precondition pinned
    # below fails the plan instead.
    prefixes_match = re.search(r"bedrock_cross_region_prefixes\s*=\s*\[([^\]]*)\]", module_locals)
    assert prefixes_match
    declared_prefixes = set(re.findall(r'"([^"]+)"', prefixes_match.group(1)))
    assert {"global.", "us.", "eu.", "apac.", "au."} <= declared_prefixes
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
    assert "resources = local.bedrock_invoke_model_arns" in bedrock_statement
    assert "var.bedrock_inference_profile_arns" in module_locals
    assert "var.bedrock_foundation_model_arns" in module_locals
    assert "local.bedrock_cross_region_foundation_model_arns" in module_locals

    # The runtime task-role grant alone is not enough: it is intersected with the run-scoped
    # permissions boundary bootstrap creates, so the boundary must independently allow the same
    # wildcard-region foundation-model resource or the effective permission is still denied.
    bootstrap = _text("bootstrap/main.tf")
    assert '"arn:aws:bedrock:*::foundation-model/*"' in bootstrap

    # A leading dotted label cannot be told apart structurally from a provider label, so the
    # module classifies every configured model id against the geography prefixes plus an
    # explicit provider-label allowlist. An unclassifiable id used to fail open: no wildcard
    # grant was derived, `terraform plan` validated cleanly, and invocation then denied
    # intermittently at runtime. A module-internal precondition (unnameable from a root
    # tftest's `expect_failures`, so pinned here) now fails the plan instead.
    assert "bedrock_known_provider_prefixes" in module_locals
    assert "bedrock_unclassified_model_ids" in module_locals
    task_policy = re.search(
        r'resource "aws_iam_role_policy" "task" \{(?P<body>.*?)\n\}',
        iam,
        re.DOTALL,
    )
    assert task_policy is not None
    task_policy_body = task_policy.group("body")
    assert "precondition" in task_policy_body
    assert "length(local.bedrock_unclassified_model_ids) == 0" in task_policy_body


def test_every_effective_llm_profile_derives_region_aware_exact_bedrock_resources() -> None:
    module_locals = _text("modules/scenario/locals.tf")
    iam = _text("modules/scenario/iam_observability.tf")
    bootstrap = _text("bootstrap/main.tf")

    assert "bedrock_profile_model_ids" in module_locals
    assert "bedrock_profile_cross_region_prefixes" in module_locals
    assert "bedrock_profile_foundation_model_arns" in module_locals
    assert ('"arn:aws:bedrock:${profile.region_name}::foundation-model/${local.bedrock_profile_model_ids[alias]}"') in module_locals
    assert "if local.bedrock_profile_cross_region_prefixes[alias] == null" in module_locals

    assert "bedrock_profile_inference_profile_arns" in module_locals
    assert (
        '"arn:aws:bedrock:${profile.region_name}:${var.aws_account_id}:inference-profile/${local.bedrock_profile_model_ids[alias]}"'
    ) in module_locals
    assert "if local.bedrock_profile_cross_region_prefixes[alias] != null" in module_locals

    assert "bedrock_invoke_model_arns = distinct(concat(" in module_locals
    for required in (
        "var.bedrock_inference_profile_arns",
        "var.bedrock_foundation_model_arns",
        "local.bedrock_profile_foundation_model_arns",
        "local.bedrock_profile_inference_profile_arns",
        "local.bedrock_cross_region_foundation_model_arns",
    ):
        assert required in module_locals

    bedrock_statement = _statement_body(iam, "InvokeConfiguredBedrockModels")
    assert "resources = local.bedrock_invoke_model_arns" in bedrock_statement
    assert '"arn:aws:bedrock:*:${var.aws_account_id}:inference-profile/*"' in bootstrap


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
    # object-level) ListBucket must also be allowed by the permissions boundary —
    # run-scoped to the derived scenario bucket names, never elspeth-* (which also
    # matched every sibling run's bucket and the Terraform state bucket).
    assert '[for bucket in local.scenario_buckets : "arn:aws:s3:::${bucket}"]' in bootstrap
    assert '"arn:aws:s3:::elspeth-*"' not in bootstrap
    assert re.search(r'actions\s*=\s*\["s3:ListBucket"\]', bootstrap)


def test_versioned_textract_inputs_are_allowed_by_task_role_boundary_and_operator_guidance() -> None:
    iam = _text("modules/scenario/iam_observability.tf")
    bootstrap = _text("bootstrap/main.tf")

    runtime_objects = _statement_body(iam, "UseAcceptanceObjects")
    boundary_objects = _statement_body(bootstrap, "UseElspethObjects")
    for statement in (runtime_objects, boundary_objects):
        assert '"s3:GetObject"' in statement
        assert '"s3:GetObjectVersion"' in statement

    configuration = (REPO_ROOT / "docs" / "reference" / "configuration.md").read_text(encoding="utf-8")
    runbook = (REPO_ROOT / "docs" / "runbooks" / "aws-ecs-deployment.md").read_text(encoding="utf-8")
    assert "`version_field`" in configuration
    assert "`s3:GetObjectVersion`" in configuration
    assert "`version_field`" in runbook
    assert "`s3:GetObjectVersion`" in runbook


def test_composer_boot_probe_exercises_primary_and_advisor_models() -> None:
    module_locals = _text("modules/scenario/locals.tf")
    assert '{ name = "ELSPETH_WEB__COMPOSER_BOOT_PROBE_ENABLED", value = "true" }' in module_locals
    assert '{ name = "ELSPETH_WEB__COMPOSER_MODEL", value = var.composer_model }' in module_locals
    assert '{ name = "ELSPETH_WEB__COMPOSER_ADVISOR_MODEL", value = var.composer_advisor_model }' in module_locals


def test_cloudwatch_agent_health_probe_checks_the_bounded_loopback_otlp_listener() -> None:
    ecs = _text("modules/scenario/ecs.tf")
    dockerfile = _text("cloudwatch-agent-image/Dockerfile")
    runbook = (REPO_ROOT / "docs" / "runbooks" / "aws-ecs-deployment.md").read_text(encoding="utf-8")
    probe = "import socket; socket.create_connection(('127.0.0.1', 4317), timeout=3).close()"

    assert 'command     = ["CMD", "python", "-c", "' + probe + '"]' in ecs
    assert "kill -0 1" not in ecs
    assert "FROM gcr.io/distroless/python3-debian13:debug-nonroot@" in dockerfile
    assert "ln -s /busybox/sh /usr/bin/sh" in dockerfile

    assert '"command": ["CMD", "python", "-c", "' + probe + '"]' in runbook
    assert "`127.0.0.1:4317`" in runbook
    assert "three-second socket timeout" in runbook
    assert "kill -0 1" not in runbook


def test_every_image_is_proven_before_any_credentialed_task_definition() -> None:
    provenance = _text("modules/scenario/image_provenance.tf")
    module_variables = _text("modules/scenario/variables.tf")
    credentialed_definitions = "\n".join((_text("modules/scenario/ecs.tf"), _text("modules/scenario/database_bootstrap.tf")))

    assert 'variable "candidate_ecr_repository"' in module_variables
    assert 'variable "cloudwatch_agent_ecr_repository"' in module_variables
    assert all(
        token in module_variables
        for token in (
            "var.aws_account_id",
            "var.aws_region",
            "var.candidate_ecr_repository",
            "var.cloudwatch_agent_ecr_repository",
            "var.gateway_ecr_repository",
            "amazonaws",
        )
    )
    assert 'resource "terraform_data" "candidate_image_provenance"' in provenance
    assert 'resource "terraform_data" "rollback_image_provenance"' in provenance
    assert 'resource "terraform_data" "cloudwatch_agent_image_provenance"' in provenance
    assert 'resource "terraform_data" "gateway_image_provenance"' in provenance
    assert 'docker pull --platform "$TARGET_PLATFORM" "$CANDIDATE_IMAGE"' in provenance
    assert 'docker pull --platform "$TARGET_PLATFORM" "$ROLLBACK_IMAGE"' in provenance
    assert 'docker pull --platform "$TARGET_PLATFORM" "$CLOUDWATCH_AGENT_IMAGE"' in provenance
    assert 'docker pull --platform "$TARGET_PLATFORM" "$GATEWAY_IMAGE"' in provenance
    assert "org.opencontainers.image.revision" in provenance
    assert 'test "$revision" = "$CANDIDATE_SHA"' in provenance
    assert 'test "$revision" = "$GATEWAY_SHA"' in provenance
    assert 'test "$revision" = "$ROLLBACK_SHA"' in provenance
    assert 'test "$revision" = "$CANDIDATE_SHA"' in provenance
    assert "terraform_data.candidate_image_provenance.output" in credentialed_definitions
    assert "terraform_data.rollback_image_provenance.output" in credentialed_definitions
    assert "terraform_data.cloudwatch_agent_image_provenance.output" in credentialed_definitions
    assert "terraform_data.gateway_image_provenance[*].output" in credentialed_definitions
    assert "image                  = var.candidate_image" not in credentialed_definitions
    assert "image      = var.candidate_image" not in credentialed_definitions
    assert "image = var.rollback_baseline_image" not in credentialed_definitions
    assert "image             = local.cloudwatch_agent_image" not in credentialed_definitions
    assert "image                  = var.gateway_image" not in credentialed_definitions

    for root in ("scenario-a", "scenario-b", "scenario-c"):
        assert 'variable "candidate_ecr_repository"' in _text(f"{root}/variables.tf")
        assert 'variable "cloudwatch_agent_ecr_repository"' in _text(f"{root}/variables.tf")
        assert re.search(r"candidate_ecr_repository\s+=\s+var\.candidate_ecr_repository", _text(f"{root}/main.tf"))
        assert re.search(
            r"cloudwatch_agent_ecr_repository\s+=\s+var\.cloudwatch_agent_ecr_repository",
            _text(f"{root}/main.tf"),
        )
        assert "candidate_ecr_repository" in _text(f"examples/{root}.tfvars.example")
        assert "cloudwatch_agent_ecr_repository" in _text(f"examples/{root}.tfvars.example")

    scenario_c_variables = _text("scenario-c/variables.tf")
    scenario_c_main = _text("scenario-c/main.tf")
    scenario_c_example = _text("examples/scenario-c.tfvars.example")
    assert 'variable "gateway_ecr_repository"' in scenario_c_variables
    assert re.search(r"gateway_ecr_repository\s+=\s+var\.gateway_ecr_repository", scenario_c_main)
    assert "gateway_ecr_repository" in scenario_c_example

    native_test = _text("scenario-a/codeblind.tftest.hcl")
    assert 'run "reject_foreign_registry_candidate"' in native_test
    assert 'run "reject_foreign_account_candidate"' in native_test
    assert 'run "reject_foreign_repository_candidate"' in native_test
    assert 'run "reject_dot_wildcard_repository_candidate"' in native_test

    agent_dockerfile = _text("cloudwatch-agent-image/Dockerfile")
    assert "ARG ELSPETH_RELEASE_SHA" in agent_dockerfile
    assert "org.opencontainers.image.revision=$ELSPETH_RELEASE_SHA" in agent_dockerfile


def test_application_image_contract_is_baked_verified_and_admitted() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    workflow = (REPO_ROOT / ".github" / "workflows" / "build-push.yaml").read_text(encoding="utf-8")
    provenance = _text("modules/scenario/image_provenance.tf")
    readme = _text("README.md")
    contract = "elspeth.aws-ecs.runtime.v1"

    assert f'LABEL io.elspeth.aws-ecs-config-contract="{contract}"' in dockerfile
    assert workflow.count('index .Config.Labels "io.elspeth.aws-ecs-config-contract"') == 2
    assert workflow.count(f'= "{contract}"') >= 2
    assert f'candidate_image_config_contract = "{contract}"' in provenance
    assert 'variable "candidate_image_config_contract"' not in _text("modules/scenario/variables.tf")
    assert provenance.count("io.elspeth.aws-ecs-config-contract") == 2
    assert provenance.count("EXPECTED_CONFIG_CONTRACT") == 4
    assert "candidate_image_config_contract_mismatch" in provenance
    assert "rollback_image_config_contract_mismatch" in provenance

    handoff_start = readme.index("### Promote the published candidate into bootstrap ECR")
    handoff = readme[handoff_start : readme.index("## Backend inputs", handoff_start)]
    verify = "cosign verify"
    assert verify in handoff
    assert "cosign version" in handoff
    assert '--certificate-oidc-issuer "https://token.actions.githubusercontent.com"' in handoff
    assert '--certificate-identity "https://github.com/johnm-dta/elspeth/.github/workflows/build-push.yaml@refs/tags/$GHCR_TAG"' in handoff
    assert "id-token: write" in workflow
    assert 'run: cosign sign --yes "$IMAGE_DIGEST"' in workflow
    assert 'GHCR_IMAGE="$GHCR_REPOSITORY@$GHCR_IMAGE_DIGEST"' in handoff
    assert handoff.index('GHCR_IMAGE="$GHCR_REPOSITORY@$GHCR_IMAGE_DIGEST"') < handoff.index(verify)
    assert handoff.index(verify) < handoff.index("docker buildx imagetools create")
    assert "--insecure" not in handoff
    assert "--key" not in handoff


@pytest.mark.parametrize("observed_contract", ["", "elspeth.aws-ecs.runtime.v0"])
def test_candidate_image_admission_rejects_missing_or_wrong_config_contract(observed_contract: str) -> None:
    provenance = _text("modules/scenario/image_provenance.tf")
    candidate = provenance[
        provenance.index('resource "terraform_data" "candidate_image_provenance"') : provenance.index(
            'resource "terraform_data" "rollback_image_provenance"'
        )
    ]
    command_match = re.search(r"command\s*=\s*<<-SHELL\n(?P<body>.*?)\n\s*SHELL", candidate, re.DOTALL)
    assert command_match is not None
    harness = f"""
aws() {{ printf '%s' password; }}
docker() {{
  case "$*" in
    *org.opencontainers.image.revision*) printf '%s\\n' "$CANDIDATE_SHA" ;;
    *io.elspeth.aws-ecs-config-contract*) printf '%s\\n' "$OBSERVED_CONFIG_CONTRACT" ;;
    *) return 0 ;;
  esac
}}
{command_match.group("body")}
"""
    result = subprocess.run(
        ["bash", "-ceu", harness],
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "AWS_ACCOUNT_ID": "123456789012",
            "AWS_PROFILE": "acceptance",
            "AWS_REGION": "ap-southeast-2",
            "CANDIDATE_IMAGE": "123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/elspeth@sha256:" + "d" * 64,
            "CANDIDATE_SHA": "a" * 40,
            "EXPECTED_CONFIG_CONTRACT": "elspeth.aws-ecs.runtime.v1",
            "OBSERVED_CONFIG_CONTRACT": observed_contract,
            "TARGET_PLATFORM": "linux/amd64",
        },
    )

    assert result.returncode != 0
    assert "candidate_image_config_contract_mismatch" in result.stderr


def test_repository_regexes_escape_dots_before_interpolation() -> None:
    for relative in (
        "modules/scenario/variables.tf",
        "scenario-a/variables.tf",
        "scenario-b/variables.tf",
    ):
        text = _text(relative)
        assert "${var.candidate_ecr_repository}@sha256" not in text
        assert "${var.cloudwatch_agent_ecr_repository}@sha256" not in text
        assert r'${replace(var.candidate_ecr_repository, ".", "\\.")}@sha256' in text
        assert r'${replace(var.cloudwatch_agent_ecr_repository, ".", "\\.")}@sha256' in text

    module_variables = _text("modules/scenario/variables.tf")
    assert "${var.gateway_ecr_repository}@sha256" not in module_variables
    assert r'${replace(var.gateway_ecr_repository, ".", "\\.")}@sha256' in module_variables


def test_image_provenance_isolates_docker_credentials_from_the_operator_default() -> None:
    provenance = _text("modules/scenario/image_provenance.tf")

    assert provenance.count('mkdir -m 700 "$work/docker-config"') == 4
    assert provenance.count('export DOCKER_CONFIG="$work/docker-config"') == 4

    resource_names = (
        "candidate_image_provenance",
        "rollback_image_provenance",
        "cloudwatch_agent_image_provenance",
        "gateway_image_provenance",
    )
    boundaries = [provenance.index(f'resource "terraform_data" "{name}"') for name in resource_names]
    for start, end in itertools.pairwise([*boundaries, len(provenance)]):
        block = provenance[start:end]
        export_index = block.index('export DOCKER_CONFIG="$work/docker-config"')
        assert export_index < block.index("trap ")
        assert export_index < block.index("docker login")
        assert export_index < block.index("docker pull")
        assert export_index < block.index("docker image inspect")


def test_image_provenance_binds_every_check_to_the_selected_target_platform() -> None:
    provenance = _text("modules/scenario/image_provenance.tf")
    resources = (
        ("candidate_image_provenance", "CANDIDATE_IMAGE"),
        ("rollback_image_provenance", "ROLLBACK_IMAGE"),
        ("cloudwatch_agent_image_provenance", "CLOUDWATCH_AGENT_IMAGE"),
        ("gateway_image_provenance", "GATEWAY_IMAGE"),
    )

    for index, (resource_name, image_name) in enumerate(resources):
        start = provenance.index(f'resource "terraform_data" "{resource_name}"')
        end = provenance.index('resource "terraform_data"', start + 1) if index < len(resources) - 1 else len(provenance)
        block = provenance[start:end]
        triggers_start = block.index("triggers_replace = [")
        triggers_end = block.index("]", triggers_start)
        triggers = block[triggers_start:triggers_end]

        assert "var.target_platform" in triggers
        assert "TARGET_PLATFORM" in block
        assert re.search(
            rf'docker pull\s+--platform "\$TARGET_PLATFORM"\s+"\${re.escape(image_name)}"',
            block,
        )
        assert block.count('--platform "$TARGET_PLATFORM"') == 1
        inspect = block[block.index("docker image inspect") : block.index('test "$revision"')]
        assert "--platform" not in inspect
        assert f'"${image_name}"' in inspect
        assert re.search(r"TARGET_PLATFORM\s*=\s*var\.target_platform", block)


def test_scenario_c_gateway_image_is_proven_and_bedrock_runtime_authority_is_absent() -> None:
    scenario_c = _text("scenario-c/main.tf")
    provenance = _text("modules/scenario/image_provenance.tf")
    ecs = _text("modules/scenario/ecs.tf")
    iam = _text("modules/scenario/iam_observability.tf")
    storage = _text("modules/scenario/storage_identity.tf")
    locals_text = _text("modules/scenario/locals.tf")

    assert re.search(r'\bllm_backend\s*=\s*"custom_gateway"', scenario_c)
    assert 'resource "terraform_data" "gateway_image_provenance"' in provenance
    gateway_start = provenance.index('resource "terraform_data" "gateway_image_provenance"')
    gateway = provenance[gateway_start:]
    assert re.search(r"\bcount\s*=\s*local\.bedrock_backend\s*\?\s*0\s*:\s*1", gateway)
    for trigger in (
        "var.gateway_image",
        "var.gateway_sha",
        "var.gateway_ecr_repository",
        "var.target_platform",
    ):
        assert trigger in gateway[gateway.index("triggers_replace = [") : gateway.index("]", gateway.index("triggers_replace = ["))]
    assert 'docker pull --platform "$TARGET_PLATFORM" "$GATEWAY_IMAGE"' in gateway
    assert 'test "$revision" = "$GATEWAY_SHA"' in gateway
    assert "image                  = one(terraform_data.gateway_image_provenance[*].output)" in ecs

    assert iam.count("for_each = local.bedrock_backend ? [1] : []") == 2
    for resource_name in ("prompt", "content"):
        guardrail = re.search(
            rf'resource "aws_bedrock_guardrail" "{resource_name}" \{{(?P<body>.*?)\n\}}',
            storage,
            re.DOTALL,
        )
        assert guardrail is not None
        assert re.search(r"\bcount\s*=\s*local\.bedrock_backend\s*\?\s*1\s*:\s*0", guardrail.group("body"))
    assert 'bedrock_litellm_model      = local.bedrock_backend ? var.composer_model : ""' in locals_text
    assert 'bedrock_fast_litellm_model = local.bedrock_backend ? var.composer_advisor_model : ""' in locals_text


def test_database_password_rotation_reexecutes_the_database_bootstrap() -> None:
    bootstrap = _text("modules/scenario/database_bootstrap.tf")
    triggers = bootstrap[bootstrap.index("triggers_replace = [") : bootstrap.index("]", bootstrap.index("triggers_replace = ["))]

    assert "aws_secretsmanager_secret_version.bootstrap.version_id" in triggers


def test_upgrade_command_selects_the_newly_registered_task_definition() -> None:
    readme = _text("README.md")
    start = readme.index("### Upgrading an existing install")
    upgrade = readme[start : readme.index("## Scenario B", start)]

    assert "CANDIDATE_TASK_DEFINITION=" in upgrade
    assert "terraform -chdir=scenario-a output" in upgrade
    assert "aws ecs update-service \\\n" in upgrade
    assert '--task-definition "$CANDIDATE_TASK_DEFINITION"' in upgrade
    assert "--force-new-deployment" in upgrade


def test_source_free_cold_install_runs_the_full_post_enable_acceptance() -> None:
    readme = _text("README.md")
    start = readme.index("### Source-free post-enable acceptance")
    section = readme[start : readme.index("## Immutable RDS trust-root admission", start)]

    for required in (
        "services-stable",
        "describe-target-health",
        "/api/health",
        "/api/ready",
        "provision-storage",
        "verify-local-auth",
        "capture --state-file",
        "verify-api --state-file",
        "verify-s3",
        "verify-bedrock",
        "verify-bedrock-guardrails",
        "/messages",
        '--task-definition "$CANDIDATE_TASK_DEFINITION"',
    ):
        assert required in section


def test_candidate_image_handoff_copies_the_authenticated_ghcr_index_into_bootstrap_ecr() -> None:
    readme = _text("README.md")
    producing_start = readme.index("### Producing the candidate image")
    producing = readme[producing_start : readme.index("### Installer policy and task-role boundary", producing_start)]
    handoff_heading = "### Promote the published candidate into bootstrap ECR"
    assert handoff_heading in readme
    handoff_start = readme.index(handoff_heading)
    section = readme[handoff_start : readme.index("## Backend inputs", handoff_start)]

    for required in (
        "GITHUB_USERNAME",
        "GITHUB_TOKEN",
        "docker login ghcr.io",
        "terraform -chdir=bootstrap output -raw ecr_repository_url",
        "aws ecr get-login-password",
        "docker buildx imagetools create",
        "GHCR_IMAGE_DIGEST",
        "ECR_IMAGE_DIGEST",
        'test "$ECR_IMAGE_DIGEST" = "$GHCR_IMAGE_DIGEST"',
        'CANDIDATE_IMAGE="$ECR_REPOSITORY_URL@$ECR_IMAGE_DIGEST"',
    ):
        assert required in section

    assert section.index("docker login ghcr.io") < section.index("docker buildx imagetools create")
    assert section.index("aws ecr get-login-password") < section.index("docker buildx imagetools create")
    assert section.index("docker buildx imagetools create") < section.index('CANDIDATE_IMAGE="$ECR_REPOSITORY_URL@$ECR_IMAGE_DIGEST"')
    assert readme.index("terraform -chdir=bootstrap apply") < handoff_start
    assert "put the GHCR digest reference in the tfvars" not in producing


def test_candidate_image_handoff_subshell_fails_before_emitting_an_unverified_reference() -> None:
    readme = _text("README.md")
    handoff_start = readme.index("### Promote the published candidate into bootstrap ECR")
    section = readme[handoff_start : readme.index("## Backend inputs", handoff_start)]
    subshell_start = section.index("(\n")
    subshell_end = section.index("\n)\nunset GITHUB_TOKEN", subshell_start)
    subshell = section[subshell_start:subshell_end]

    assert re.match(r"\(\n\s+set -Eeuo pipefail\n", subshell)
    assert subshell.index("set -Eeuo pipefail") < subshell.index("docker login ghcr.io")
    assert subshell.index('test "$ECR_IMAGE_DIGEST" = "$GHCR_IMAGE_DIGEST"') < subshell.index("printf 'candidate_image")


def test_candidate_image_handoff_proves_exact_selected_platform_child_digest() -> None:
    readme = _text("README.md")
    start = readme.index("### Promote the published candidate into bootstrap ECR")
    section = readme[start : readme.index("## Backend inputs", start)]

    for marker in (
        "resolve_target_manifest_digest() (",
        "linux/amd64|linux/arm64",
        '.platform.os == "linux"',
        ".platform.architecture == $architecture",
        'GHCR_PLATFORM_DIGEST=$(resolve_target_manifest_digest "$GHCR_IMAGE" "$TARGET_PLATFORM")',
        'ECR_PLATFORM_DIGEST=$(resolve_target_manifest_digest "$CANDIDATE_IMAGE" "$TARGET_PLATFORM")',
        'test "$ECR_PLATFORM_DIGEST" = "$GHCR_PLATFORM_DIGEST"',
        "candidate_platform_digest",
    ):
        assert marker in section


def test_service_rollout_proof_binds_primary_task_and_platform_digest() -> None:
    readme = _text("README.md")
    source_free_start = readme.index("### Source-free post-enable acceptance")
    source_free = readme[source_free_start : readme.index("## Immutable RDS trust-root admission", source_free_start)]

    for marker in (
        "resolve_target_ecr_digest() (",
        "verify_candidate_service() (",
        "TARGET_PLATFORM",
        "candidate_platform_digest",
        "(.services | length == 1)",
        'select(.status == "PRIMARY")',
        '.rolloutState == "COMPLETED"',
        ".failedTasks == 0",
        "(.tasks | length == 1)",
        ".taskDefinitionArn == $candidate_task_definition",
        ".imageDigest == $candidate_platform_digest",
    ):
        assert marker in source_free
    assert source_free.count("verify_candidate_service ") >= 2
    assert readme.count("verify_candidate_service ") >= 3


def test_teardown_drains_each_applied_service_before_destroy() -> None:
    readme = _text("README.md")
    start = readme.index("## Outputs and teardown")
    section = readme[start : readme.index("### Container Insights", start)]

    for marker in (
        "drain_scenario_service()",
        "--desired-count 0",
        "services-stable",
        "--desired-status RUNNING",
        ".taskArns | length == 0",
        "drain_scenario_service scenario-b",
        "drain_scenario_service scenario-a",
    ):
        assert marker in section
    assert section.index("drain_scenario_service scenario-b") < section.index("terraform -chdir=scenario-b destroy")
    assert section.index("drain_scenario_service scenario-a") < section.index("terraform -chdir=scenario-a destroy")


def test_fresh_machine_pulls_the_candidate_before_local_inspection() -> None:
    readme = _text("README.md")
    start = readme.index("Before promoting a candidate")
    section = readme[start : readme.index("### Upgrading an existing install", start)]

    assert 'docker pull "$CANDIDATE_IMAGE"' in section
    assert section.index('docker pull "$CANDIDATE_IMAGE"') < section.index("docker inspect")


def test_private_candidate_pulls_use_isolated_ecr_credentials_for_registry_commands() -> None:
    readme = _text("README.md")
    acceptance_start = readme.index("### Source-free post-enable acceptance")
    inspection_start = readme.index("Before promoting a candidate")
    auth_blocks = (
        (
            readme[acceptance_start : readme.index("## Immutable RDS trust-root admission", acceptance_start)],
            "candidate_pull_work",
            ('docker pull "$CANDIDATE_IMAGE"',),
        ),
        (
            readme[inspection_start : readme.index("### Upgrading an existing install", inspection_start)],
            "candidate_inspection_work",
            (
                'docker pull "$CANDIDATE_IMAGE"',
                'docker buildx imagetools inspect "$CANDIDATE_IMAGE"',
            ),
        ),
    )

    for section, work_variable, registry_commands in auth_blocks:
        registry = "CANDIDATE_ECR_REGISTRY=${CANDIDATE_IMAGE%%/*}"
        work_create = f"{work_variable}=$(mktemp -d -p /tmp "
        docker_config = f'export DOCKER_CONFIG="${work_variable}/docker-config"'
        cleanup = (
            f"""trap 'docker logout "$CANDIDATE_ECR_REGISTRY" >/dev/null 2>&1 || true; """
            f"""rm -rf -- "${work_variable}"' EXIT"""
        )
        login = 'docker login --username AWS --password-stdin "$CANDIDATE_ECR_REGISTRY"'

        assert registry in section
        assert work_create in section
        subshell_start = section.index("(\n", section.index(work_create))
        subshell_end = section.index("\n)", subshell_start)
        block = section[subshell_start:subshell_end]

        assert re.match(r"\(\n\s+set -e\n", block)
        assert f'mkdir -m 700 "${work_variable}/docker-config"' in section
        assert docker_config in block
        assert cleanup in block
        assert block.count("docker logout") == 1
        assert "aws ecr get-login-password" in block
        assert login in block
        assert block.index(docker_config) < block.index(cleanup) < block.index("aws ecr get-login-password")
        assert block.index("aws ecr get-login-password") < block.index(login)
        for command in registry_commands:
            assert command in block
            assert block.index(login) < block.index(command)


def test_inventory_v8_exactly_matches_the_runtime_validator_contract() -> None:
    locals = _text("modules/scenario/locals.tf")
    ecs = _text("modules/scenario/ecs.tf")
    outputs = _text("modules/scenario/outputs.tf")

    resolved_output = outputs[outputs.index('output "resolved_inventory"') :]
    assert _hcl_map_keys(resolved_output, "value") == frozenset(
        {
            "schema",
            "acceptance_run_id",
            "candidate_sha",
            "gateway_image",
            "gateway_sha",
            "gateway_adapter",
            "aws_account_id",
            "aws_region",
            "scenario_id",
            "phase",
            "values",
            "orphan_sweep",
        }
    )
    assert _hcl_map_keys(outputs, "scenario_values") == scenario_inventory.SCENARIO_VALUE_FIELDS
    assert _hcl_map_keys(outputs, "scenario_orphan_sweep") == scenario_inventory.ORPHAN_INVENTORY_FIELDS
    assert re.search(r'schema\s+=\s+"elspeth\.aws-ecs-scenario-inventory\.v8"', resolved_output)
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
    assert "terraform_data.cloudwatch_agent_image_provenance.output" in ecs
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


def test_installer_customer_managed_policy_documents_stay_within_iam_size_limit() -> None:
    documents = _render_installer_policy_documents(
        {
            **_INSTALLER_POLICY_VALUES,
            "backend_state_bucket": "elspeth-" + ("s" * 55),
            "ecr_repository": "elspeth-" + ("a" * 248),
            "cloudwatch_agent_ecr_repository": "elspeth-" + ("b" * 248),
            "gateway_ecr_repository": "elspeth-" + ("g" * 248),
            "scenario_a_bucket": "elspeth-a-" + ("c" * 33),
            "scenario_b_bucket": "elspeth-b-" + ("d" * 33),
            "scenario_c_bucket": "elspeth-c-" + ("e" * 33),
        }
    )
    assert documents

    rendered_sizes = {path.name: len(json.dumps(document, separators=(",", ":"))) for path, _rendered, document in documents}
    assert rendered_sizes == {
        "installer-control-plane-policy.json.tftpl": 6_017,
        "installer-regional-resources-policy.json.tftpl": 6_102,
        "installer-relationships-policy.json.tftpl": 5_865,
        "installer-runtime-observation-policy.json.tftpl": 5_767,
    }
    assert all(size <= 6_144 for size in rendered_sizes.values()), rendered_sizes
    assert len(documents) == 4

    statement_sids = [statement["Sid"] for _path, _rendered, document in documents for statement in document["Statement"]]
    assert len(statement_sids) == len(set(statement_sids))
    tag_on_create = [
        (path.name, statement)
        for path, _rendered, document in documents
        for statement in document["Statement"]
        if statement["Sid"] == "TagNamedEcrRepositoriesOnCreate"
    ]
    assert len(tag_on_create) == 1
    path_name, statement = tag_on_create[0]
    assert path_name == "installer-relationships-policy.json.tftpl"
    assert statement["Action"] == ["ecr:TagResource"]
    assert statement["Condition"]["StringEquals"] == {
        "aws:RequestTag/ACCEPTANCE_RUN_ID": _INSTALLER_POLICY_VALUES["run_id"],
        "aws:RequestedRegion": _INSTALLER_POLICY_VALUES["aws_region"],
    }


def test_installer_named_bucket_permissions_use_exact_s3_iam_actions() -> None:
    documents = _render_installer_policy_documents()
    statements = {statement["Sid"]: statement for _path, _rendered, document in documents for statement in document["Statement"]}

    assert statements["ManageElspethNamedBuckets"]["Action"] == [
        "s3:CreateBucket",
        "s3:DeleteBucket",
        "s3:DeleteBucketPolicy",
        "s3:DeleteBucketWebsite",
        "s3:ListBucket",
        "s3:ListBucketVersions",
        "s3:PutBucketOwnershipControls",
        "s3:PutBucketPolicy",
        "s3:PutEncryptionConfiguration",
        "s3:PutBucketPublicAccessBlock",
        "s3:PutBucketTagging",
        "s3:PutBucketVersioning",
    ]


def test_installer_policy_grants_only_run_tagged_guardrail_and_event_rule_updates() -> None:
    documents = _render_installer_policy_documents()
    statements = {statement["Sid"]: statement for _path, _rendered, document in documents for statement in document["Statement"]}
    mutating = statements["MutateRunTaggedRegionalResources"]

    assert mutating["Resource"] == "*"
    assert mutating["Condition"]["StringEquals"] == {
        "aws:ResourceTag/ACCEPTANCE_RUN_ID": _INSTALLER_POLICY_VALUES["run_id"],
        "aws:RequestedRegion": _INSTALLER_POLICY_VALUES["aws_region"],
    }
    assert {sid for sid, statement in statements.items() if "bedrock:UpdateGuardrail" in statement["Action"]} == {
        "MutateRunTaggedRegionalResources"
    }
    assert {sid for sid, statement in statements.items() if "events:PutRule" in statement["Action"]} == {
        "CreateRunTaggedRegionalResources",
        "MutateRunTaggedRegionalResources",
    }


def test_installer_policy_splits_ecs_tag_drift_by_available_condition_context() -> None:
    documents = _render_installer_policy_documents()
    statements = {statement["Sid"]: statement for _path, _rendered, document in documents for statement in document["Statement"]}
    tagged_resources = statements["MutateRunTaggedEcsResources"]
    task_definitions = statements["MutateTaskDefinitionTagsRegionScopedOnly"]

    assert not {"ecs:TagResource", "ecs:UntagResource"}.intersection(statements["MutateRunTaggedRegionalResources"]["Action"])
    assert tagged_resources["Action"] == ["ecs:TagResource", "ecs:UntagResource"]
    assert tagged_resources["Resource"] == [
        "arn:aws:ecs:ap-southeast-1:123456789012:cluster/acceptance-a-0123456789abcdefabcd-cluster",
        "arn:aws:ecs:ap-southeast-1:123456789012:service/acceptance-a-0123456789abcdefabcd-cluster/acceptance-a-0123456789abcdefabcd-service",
        "arn:aws:ecs:ap-southeast-1:123456789012:cluster/acceptance-b-0123456789abcdefabcd-cluster",
        "arn:aws:ecs:ap-southeast-1:123456789012:service/acceptance-b-0123456789abcdefabcd-cluster/acceptance-b-0123456789abcdefabcd-service",
        "arn:aws:ecs:ap-southeast-1:123456789012:cluster/acceptance-c-0123456789abcdefabcd-cluster",
        "arn:aws:ecs:ap-southeast-1:123456789012:service/acceptance-c-0123456789abcdefabcd-cluster/acceptance-c-0123456789abcdefabcd-service",
    ]
    assert tagged_resources["Condition"]["StringEquals"] == {
        "aws:ResourceTag/ACCEPTANCE_RUN_ID": _INSTALLER_POLICY_VALUES["run_id"],
        "aws:RequestedRegion": _INSTALLER_POLICY_VALUES["aws_region"],
    }
    assert task_definitions["Action"] == ["ecs:TagResource", "ecs:UntagResource"]
    assert task_definitions["Resource"] == [
        "arn:aws:ecs:ap-southeast-1:123456789012:task-definition/a-0123456789abcdefabcd-*:*",
        "arn:aws:ecs:ap-southeast-1:123456789012:task-definition/b-0123456789abcdefabcd-*:*",
        "arn:aws:ecs:ap-southeast-1:123456789012:task-definition/c-0123456789abcdefabcd-*:*",
    ]
    assert task_definitions["Condition"]["StringEquals"] == {
        "aws:RequestedRegion": _INSTALLER_POLICY_VALUES["aws_region"],
    }
    assert "aws:ResourceTag/ACCEPTANCE_RUN_ID" not in json.dumps(task_definitions)


def test_installer_policy_grants_exact_run_tagged_cognito_child_update_path() -> None:
    documents = _render_installer_policy_documents()
    statements = {statement["Sid"]: statement for _path, _rendered, document in documents for statement in document["Statement"]}
    cognito_children = statements["ManageRunCognitoChildren"]

    assert cognito_children["Action"] == [
        "cognito-idp:AdminCreateUser",
        "cognito-idp:AdminGetUser",
        "cognito-idp:CreateUserPoolClient",
        "cognito-idp:CreateUserPoolDomain",
        "cognito-idp:DeleteUserPoolClient",
        "cognito-idp:DeleteUserPoolDomain",
        "cognito-idp:UpdateUserPoolClient",
    ]
    assert cognito_children["Resource"] == "arn:aws:cognito-idp:ap-southeast-1:123456789012:userpool/*"
    assert cognito_children["Condition"]["StringEquals"] == {
        "aws:ResourceTag/ACCEPTANCE_RUN_ID": _INSTALLER_POLICY_VALUES["run_id"],
        "aws:RequestedRegion": _INSTALLER_POLICY_VALUES["aws_region"],
    }
    assert {sid for sid, statement in statements.items() if "cognito-idp:UpdateUserPoolClient" in statement["Action"]} == {
        "ManageRunCognitoChildren"
    }


def test_installer_policy_is_renderable_scoped_and_boundary_enforced() -> None:
    documents = _render_installer_policy_documents()
    template = _installer_policy_template_text()
    values = _INSTALLER_POLICY_VALUES
    statements = {statement["Sid"]: statement for _path, _rendered, document in documents for statement in document["Statement"]}

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
        "arn:aws:ecr:ap-southeast-1:123456789012:repository/elspeth-gateway-example",
    ]
    assert all(sid == "PushAndCleanElspethImages" for sid, statement in statements.items() if "ecr:BatchDeleteImage" in statement["Action"])
    assert statements["AuthenticateForEcrPush"]["Action"] == ["ecr:GetAuthorizationToken"]
    assert statements["AuthenticateForEcrPush"]["Condition"]["StringEquals"]["aws:RequestedRegion"] == values["aws_region"]

    run_tasks = statements["RunScenarioTasks"]
    assert run_tasks["Action"] == ["ecs:RunTask"]
    assert run_tasks["Resource"] == [
        "arn:aws:ecs:ap-southeast-1:123456789012:task-definition/a-0123456789abcdefabcd-*:*",
        "arn:aws:ecs:ap-southeast-1:123456789012:task-definition/b-0123456789abcdefabcd-*:*",
        "arn:aws:ecs:ap-southeast-1:123456789012:task-definition/c-0123456789abcdefabcd-*:*",
    ]
    assert run_tasks["Condition"]["ArnLike"]["ecs:cluster"] == [
        "arn:aws:ecs:ap-southeast-1:123456789012:cluster/acceptance-a-0123456789abcdefabcd-cluster",
        "arn:aws:ecs:ap-southeast-1:123456789012:cluster/acceptance-b-0123456789abcdefabcd-cluster",
        "arn:aws:ecs:ap-southeast-1:123456789012:cluster/acceptance-c-0123456789abcdefabcd-cluster",
    ]
    stop_tasks = statements["StopRunScenarioTasks"]
    assert stop_tasks["Action"] == ["ecs:StopTask"]
    assert stop_tasks["Resource"] == [
        "arn:aws:ecs:ap-southeast-1:123456789012:task/acceptance-a-0123456789abcdefabcd-cluster/*",
        "arn:aws:ecs:ap-southeast-1:123456789012:task/acceptance-b-0123456789abcdefabcd-cluster/*",
        "arn:aws:ecs:ap-southeast-1:123456789012:task/acceptance-c-0123456789abcdefabcd-cluster/*",
    ]
    assert stop_tasks["Condition"]["ArnEquals"]["ecs:cluster"] == [
        "arn:aws:ecs:ap-southeast-1:123456789012:cluster/acceptance-a-0123456789abcdefabcd-cluster",
        "arn:aws:ecs:ap-southeast-1:123456789012:cluster/acceptance-b-0123456789abcdefabcd-cluster",
        "arn:aws:ecs:ap-southeast-1:123456789012:cluster/acceptance-c-0123456789abcdefabcd-cluster",
    ]
    assert statements["ReadScenarioSecretValues"]["Action"] == ["secretsmanager:GetSecretValue"]
    assert statements["ReadScenarioSecretValues"]["Resource"] == [
        "arn:aws:secretsmanager:ap-southeast-1:123456789012:secret:a-0123456789abcdefabcd-*",
        "arn:aws:secretsmanager:ap-southeast-1:123456789012:secret:b-0123456789abcdefabcd-*",
        "arn:aws:secretsmanager:ap-southeast-1:123456789012:secret:c-0123456789abcdefabcd-*",
    ]

    dashboards = statements["ManageRunDashboards"]
    assert dashboards["Resource"] == [
        "arn:aws:cloudwatch::123456789012:dashboard/a-0123456789abcdefabcd-elspeth-aws-operator-v1",
        "arn:aws:cloudwatch::123456789012:dashboard/b-0123456789abcdefabcd-elspeth-aws-operator-v1",
        "arn:aws:cloudwatch::123456789012:dashboard/c-0123456789abcdefabcd-elspeth-aws-operator-v1",
    ]
    assert {"cloudwatch:GetDashboard", "cloudwatch:PutDashboard", "cloudwatch:DeleteDashboards"} == set(dashboards["Action"])
    assert not {"cloudwatch:PutDashboard", "cloudwatch:DeleteDashboards"}.intersection(
        statements["MutateRunTaggedRegionalResources"]["Action"]
    )
    named_buckets = statements["ManageElspethNamedBuckets"]
    assert {
        "s3:DeleteBucketPolicy",
        "s3:DeleteBucketWebsite",
        "s3:ListBucket",
        "s3:ListBucketVersions",
        "s3:PutBucketPublicAccessBlock",
    }.issubset(named_buckets["Action"])
    assert named_buckets["Resource"] == [
        "arn:aws:s3:::elspeth-state-example",
        "arn:aws:s3:::elspeth-a-example",
        "arn:aws:s3:::elspeth-b-example",
        "arn:aws:s3:::elspeth-c-example",
    ]
    named_objects = statements["ManageElspethNamedBucketObjects"]
    assert "s3:DeleteObjectVersion" in named_objects["Action"]
    assert named_objects["Resource"] == [
        "arn:aws:s3:::elspeth-state-example/*",
        "arn:aws:s3:::elspeth-a-example/*",
        "arn:aws:s3:::elspeth-b-example/*",
        "arn:aws:s3:::elspeth-c-example/*",
    ]
    assert "acm:GetCertificate" in statements["ReadDiscovery"]["Action"]
    assert {
        "bedrock:GetInferenceProfile",
        "bedrock:ListInferenceProfiles",
    }.issubset(statements["ReadDiscovery"]["Action"])
    create_vpc_children = statements["CreateChildrenOfRunTaggedVpc"]
    assert create_vpc_children["Action"] == [
        "ec2:CreateRouteTable",
        "ec2:CreateSecurityGroup",
        "ec2:CreateSubnet",
    ]
    assert create_vpc_children["Resource"] == "*"
    assert create_vpc_children["Condition"]["StringEquals"] == {
        "aws:ResourceTag/ACCEPTANCE_RUN_ID": values["run_id"],
        "aws:RequestedRegion": values["aws_region"],
    }

    assert "CreateRunScopedRolesWithBoundary" not in statements
    pass_role = statements["PassRunScopedRolesToEcsTasksOnly"]
    assert pass_role["Condition"] == {"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}}
    assert pass_role["Resource"] == [
        "arn:aws:iam::123456789012:role/elspeth/12345678-1234-4123-8123-123456789abc/a-0123456789abcdefabcd-task-role",
        "arn:aws:iam::123456789012:role/elspeth/12345678-1234-4123-8123-123456789abc/a-0123456789abcdefabcd-execution-role",
        "arn:aws:iam::123456789012:role/elspeth/12345678-1234-4123-8123-123456789abc/b-0123456789abcdefabcd-task-role",
        "arn:aws:iam::123456789012:role/elspeth/12345678-1234-4123-8123-123456789abc/b-0123456789abcdefabcd-execution-role",
        "arn:aws:iam::123456789012:role/elspeth/12345678-1234-4123-8123-123456789abc/c-0123456789abcdefabcd-task-role",
        "arn:aws:iam::123456789012:role/elspeth/12345678-1234-4123-8123-123456789abc/c-0123456789abcdefabcd-execution-role",
    ]
    iam_roles = _text("modules/scenario/iam_observability.tf")
    assert iam_roles.count('path                 = "/elspeth/${var.run_id}/"') == 2
    inline_roles = statements["ManageRunScopedInlinePolicies"]
    assert set(inline_roles["Action"]) == {"iam:DeleteRolePolicy", "iam:PutRolePolicy"}
    managed_attachment = statements["ManageKnownExecutionRoleAttachment"]
    assert set(managed_attachment["Action"]) == {"iam:AttachRolePolicy", "iam:DetachRolePolicy"}
    assert managed_attachment["Resource"] == [
        "arn:aws:iam::123456789012:role/elspeth/12345678-1234-4123-8123-123456789abc/a-0123456789abcdefabcd-execution-role",
        "arn:aws:iam::123456789012:role/elspeth/12345678-1234-4123-8123-123456789abc/b-0123456789abcdefabcd-execution-role",
        "arn:aws:iam::123456789012:role/elspeth/12345678-1234-4123-8123-123456789abc/c-0123456789abcdefabcd-execution-role",
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

    for relative in ("scenario-a/variables.tf", "scenario-b/variables.tf", "scenario-c/variables.tf"):
        root_variables = _text(relative)
        assert 'variable "iam_permissions_boundary_arn"' in root_variables
        assert 'variable "iam_lifecycle_aws_profile"' in root_variables
    for relative in (
        "examples/scenario-a.tfvars.example",
        "examples/scenario-b.tfvars.example",
        "examples/scenario-c.tfvars.example",
    ):
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
        "ManageRunEfsLifecycleConfiguration",
        "ManageRunEfsMountTargets",
        "ManageRunEventTargets",
    }.issubset(statements)
    efs_lifecycle = statements["ManageRunEfsLifecycleConfiguration"]
    assert efs_lifecycle["Action"] == ["elasticfilesystem:PutLifecycleConfiguration"]
    assert efs_lifecycle["Resource"] == "arn:aws:elasticfilesystem:ap-southeast-1:123456789012:file-system/*"
    assert efs_lifecycle["Condition"]["StringEquals"] == {
        "aws:ResourceTag/ACCEPTANCE_RUN_ID": values["run_id"],
        "aws:RequestedRegion": values["aws_region"],
    }
    assert "repository/elspeth-*" not in template
    assert "arn:aws:s3:::elspeth-*" not in template
    readme = _text("README.md")
    for name in values:
        assert f"${{{name}}}" in readme
    assert "four customer-managed policies" in readme
    assert "6,144" in readme
    assert "inline policies" in readme
    assert "dedicated empty account" in readme
    assert "not supported in a shared account" in readme


def test_installer_discovery_and_named_mutations_use_exact_scenario_resources() -> None:
    documents = _render_installer_policy_documents()
    statements = {statement["Sid"]: statement for _path, _rendered, document in documents for statement in document["Statement"]}
    discovery_actions = set(statements["ReadDiscovery"]["Action"])

    assert "ecs:Describe*" not in discovery_actions
    assert "ecs:List*" not in discovery_actions
    assert {"ecs:ListTaskDefinitionFamilies", "ecs:ListTaskDefinitions"}.issubset(discovery_actions)
    assert "logs:GetLogEvents" not in discovery_actions
    assert "s3:ListBucket" not in discovery_actions
    assert {action for action in discovery_actions if action.startswith("xray:")} == {
        "xray:GetGroups",
        "xray:GetSamplingRules",
    }

    assert statements["ReadScenarioEcsResources"]["Resource"] == [
        "arn:aws:ecs:ap-southeast-1:123456789012:cluster/acceptance-a-0123456789abcdefabcd-cluster",
        "arn:aws:ecs:ap-southeast-1:123456789012:cluster/acceptance-b-0123456789abcdefabcd-cluster",
        "arn:aws:ecs:ap-southeast-1:123456789012:cluster/acceptance-c-0123456789abcdefabcd-cluster",
        "arn:aws:ecs:ap-southeast-1:123456789012:service/acceptance-a-0123456789abcdefabcd-cluster/acceptance-a-0123456789abcdefabcd-service",
        "arn:aws:ecs:ap-southeast-1:123456789012:service/acceptance-b-0123456789abcdefabcd-cluster/acceptance-b-0123456789abcdefabcd-service",
        "arn:aws:ecs:ap-southeast-1:123456789012:service/acceptance-c-0123456789abcdefabcd-cluster/acceptance-c-0123456789abcdefabcd-service",
        "arn:aws:ecs:ap-southeast-1:123456789012:task-definition/a-0123456789abcdefabcd-*:*",
        "arn:aws:ecs:ap-southeast-1:123456789012:task-definition/b-0123456789abcdefabcd-*:*",
        "arn:aws:ecs:ap-southeast-1:123456789012:task-definition/c-0123456789abcdefabcd-*:*",
        "arn:aws:ecs:ap-southeast-1:123456789012:task/acceptance-a-0123456789abcdefabcd-cluster/*",
        "arn:aws:ecs:ap-southeast-1:123456789012:task/acceptance-b-0123456789abcdefabcd-cluster/*",
        "arn:aws:ecs:ap-southeast-1:123456789012:task/acceptance-c-0123456789abcdefabcd-cluster/*",
    ]
    assert statements["ReadRunLogs"]["Resource"] == [
        "arn:aws:logs:ap-southeast-1:123456789012:log-group:/aws/ecs/a-0123456789abcdefabcd-*:log-stream:*",
        "arn:aws:logs:ap-southeast-1:123456789012:log-group:/aws/ecs/b-0123456789abcdefabcd-*:log-stream:*",
        "arn:aws:logs:ap-southeast-1:123456789012:log-group:/aws/ecs/c-0123456789abcdefabcd-*:log-stream:*",
        "arn:aws:logs:ap-southeast-1:123456789012:log-group:/aws/ecs/containerinsights/acceptance-a-0123456789abcdefabcd-cluster/performance:log-stream:*",
        "arn:aws:logs:ap-southeast-1:123456789012:log-group:/aws/ecs/containerinsights/acceptance-b-0123456789abcdefabcd-cluster/performance:log-stream:*",
        "arn:aws:logs:ap-southeast-1:123456789012:log-group:/aws/ecs/containerinsights/acceptance-c-0123456789abcdefabcd-cluster/performance:log-stream:*",
    ]
    assert statements["ManageRunScopedInlinePolicies"]["Resource"] == [
        "arn:aws:iam::123456789012:role/elspeth/12345678-1234-4123-8123-123456789abc/a-0123456789abcdefabcd-task-role",
        "arn:aws:iam::123456789012:role/elspeth/12345678-1234-4123-8123-123456789abc/a-0123456789abcdefabcd-execution-role",
        "arn:aws:iam::123456789012:role/elspeth/12345678-1234-4123-8123-123456789abc/b-0123456789abcdefabcd-task-role",
        "arn:aws:iam::123456789012:role/elspeth/12345678-1234-4123-8123-123456789abc/b-0123456789abcdefabcd-execution-role",
        "arn:aws:iam::123456789012:role/elspeth/12345678-1234-4123-8123-123456789abc/c-0123456789abcdefabcd-task-role",
        "arn:aws:iam::123456789012:role/elspeth/12345678-1234-4123-8123-123456789abc/c-0123456789abcdefabcd-execution-role",
    ]
    assert statements["ManageRunDashboards"]["Resource"] == [
        "arn:aws:cloudwatch::123456789012:dashboard/a-0123456789abcdefabcd-elspeth-aws-operator-v1",
        "arn:aws:cloudwatch::123456789012:dashboard/b-0123456789abcdefabcd-elspeth-aws-operator-v1",
        "arn:aws:cloudwatch::123456789012:dashboard/c-0123456789abcdefabcd-elspeth-aws-operator-v1",
    ]
    assert statements["ManageRunEventTargets"]["Resource"] == [
        "arn:aws:events:ap-southeast-1:123456789012:rule/a-0123456789abcdefabcd-*",
        "arn:aws:events:ap-southeast-1:123456789012:rule/b-0123456789abcdefabcd-*",
        "arn:aws:events:ap-southeast-1:123456789012:rule/c-0123456789abcdefabcd-*",
    ]
    xray = statements["MutateRunTaggedXrayResources"]
    assert {"xray:GetGroup", "xray:ListTagsForResource"}.issubset(xray["Action"])
    assert xray["Resource"] == [
        "arn:aws:xray:ap-southeast-1:123456789012:group/a-0123456789abcdefabcd-*/*",
        "arn:aws:xray:ap-southeast-1:123456789012:group/b-0123456789abcdefabcd-*/*",
        "arn:aws:xray:ap-southeast-1:123456789012:group/c-0123456789abcdefabcd-*/*",
        "arn:aws:xray:ap-southeast-1:123456789012:sampling-rule/a-0123456789abcdefabcd-*",
        "arn:aws:xray:ap-southeast-1:123456789012:sampling-rule/b-0123456789abcdefabcd-*",
        "arn:aws:xray:ap-southeast-1:123456789012:sampling-rule/c-0123456789abcdefabcd-*",
    ]

    for _path, rendered, _document in documents:
        for generic_pattern in (
            "/a-*",
            "/b-*",
            "/c-*",
            "group/a-*",
            "group/b-*",
            "group/c-*",
            "rule/a-*",
            "rule/b-*",
            "rule/c-*",
        ):
            assert generic_pattern not in rendered


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
        "arn:aws:iam::123456789012:role/elspeth/12345678-1234-4123-8123-123456789abc/a-*-task-role",
        "arn:aws:iam::123456789012:role/elspeth/12345678-1234-4123-8123-123456789abc/a-*-execution-role",
        "arn:aws:iam::123456789012:role/elspeth/12345678-1234-4123-8123-123456789abc/b-*-task-role",
        "arn:aws:iam::123456789012:role/elspeth/12345678-1234-4123-8123-123456789abc/b-*-execution-role",
        "arn:aws:iam::123456789012:role/elspeth/12345678-1234-4123-8123-123456789abc/c-*-task-role",
        "arn:aws:iam::123456789012:role/elspeth/12345678-1234-4123-8123-123456789abc/c-*-execution-role",
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
    for scenario in ("scenario-a", "scenario-b", "scenario-c"):
        versions = _text(f"{scenario}/versions.tf")
        main = _text(f"{scenario}/main.tf")
        assert 'alias               = "iam_lifecycle"' in versions
        assert "aws.iam_lifecycle = aws.iam_lifecycle" in main
    readme = _text("README.md")
    assert "separate IAM lifecycle principal" in readme
    assert "terraform destroy" in readme
    assert not re.search(r"terraform[^\n]*\s-target(?:=|\s)", readme)
    assert "terraform state rm" not in readme


@pytest.fixture(scope="module")
def initialized_terraform_package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Validate an isolated copy of every Terraform root from shipped files.

    The copy excludes every pre-existing ``.terraform`` directory and ignored
    operator input. All four roots must initialise from their checked-in lock
    files and validate before the native Scenario A and C contracts run
    unfiltered.

    ``-backend=false`` is deliberate: scenario roots declare partial S3
    backends, and a contract test must never reach for remote state or AWS
    credentials. ``-lockfile=readonly`` prevents a test from repairing drift.
    """
    _require_terraform("the clean Terraform package contract cannot be exercised")
    isolated = tmp_path_factory.mktemp("aws-ecs-terraform") / "terraform"
    for source in _source_files():
        destination = isolated / source.relative_to(PACKAGE)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    formatted = subprocess.run(
        ["terraform", f"-chdir={isolated}", "fmt", "-check", "-recursive"],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert formatted.returncode == 0, "terraform fmt -check failed:\n" + formatted.stdout + formatted.stderr

    for root_name in ("bootstrap", "scenario-a", "scenario-b", "scenario-c"):
        directory = isolated / root_name
        result = subprocess.run(
            [
                "terraform",
                f"-chdir={directory}",
                "init",
                "-backend=false",
                "-input=false",
                "-lockfile=readonly",
                "-no-color",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, f"terraform init failed for {root_name}:\n" + result.stdout + result.stderr
        validated = subprocess.run(
            ["terraform", f"-chdir={directory}", "validate", "-no-color"],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert validated.returncode == 0, f"terraform validate failed for {root_name}:\n" + validated.stdout + validated.stderr
    return isolated


def test_scenario_a_native_mock_plans_use_only_documented_minimal_inputs(initialized_terraform_package: Path) -> None:
    scenario_a = initialized_terraform_package / "scenario-a"
    codeblind_test = scenario_a / "codeblind.tftest.hcl"
    web_policy_test = scenario_a / "web_policy.tftest.hcl"
    assert codeblind_test.is_file()
    assert web_policy_test.is_file()
    content = codeblind_test.read_text(encoding="utf-8")
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
        ["terraform", f"-chdir={scenario_a}", "test", "-no-color"],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "codeblind.tftest.hcl" in result.stdout
    assert "web_policy.tftest.hcl" in result.stdout
    assert "Success!" in result.stdout


def test_scenario_c_native_mock_plans_enforce_the_gateway_contract(initialized_terraform_package: Path) -> None:
    scenario_c = initialized_terraform_package / "scenario-c"
    gateway_test = scenario_c / "gateway_contract.tftest.hcl"
    assert gateway_test.is_file()
    content = gateway_test.read_text(encoding="utf-8")
    for provider in ("aws", "random", "tls"):
        assert f'mock_provider "{provider}"' in content
    assert 'alias = "iam_lifecycle"' in content
    for run_name in (
        "plans_as_first_custom_gateway_without_bedrock_inputs",
        "profiles_lower_to_gateway_bindings",
        "bedrock_composer_model_is_rejected",
        "malformed_gateway_sha_is_rejected",
    ):
        assert f'run "{run_name}"' in content

    result = subprocess.run(
        ["terraform", f"-chdir={scenario_c}", "test", "-no-color"],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "gateway_contract.tftest.hcl" in result.stdout
    assert "Success!" in result.stdout


def test_networking_has_long_request_support_and_correct_listener_target() -> None:
    network = _text("modules/scenario/network.tf")
    assert re.search(r"idle_timeout\s+=\s+var\.alb_idle_timeout_seconds", network)
    assert re.search(r'type\s+=\s+"forward"', network)
    assert re.search(r"target_group_arn\s+=\s+aws_lb_target_group\.web\.arn", network)


def test_scenario_a_and_c_are_cold_installs_while_b_owns_the_oidc_upgrade_surface() -> None:
    readme = _text("README.md")
    assert "Scenario A: cold install (recommended)" in readme
    assert "Scenario B: OIDC acceptance variant" in readme
    assert readme.index("Scenario A: cold install (recommended)") < readme.index("Scenario B: OIDC acceptance variant")
    assert re.search(r'\bdeployment_lifecycle\s*=\s*"first"', _text("scenario-a/main.tf"))
    assert re.search(r'\bdeployment_lifecycle\s*=\s*"upgrade"', _text("scenario-b/main.tf"))
    assert re.search(r'\bdeployment_lifecycle\s*=\s*"first"', _text("scenario-c/main.tf"))

    module_locals = _text("modules/scenario/locals.tf")
    assert "oidc_domain_prefix        = local.namespace" in module_locals
    assert 'oidc_domain_prefix        = "${local.namespace}-${' not in module_locals
    assert 'substr(sha256("${lower(var.run_id)}\\u0000${local.scenario_id_upper}"), 0, 20)' in module_locals
    assert (
        'oidc_authorization_origin = local.deployment_mode == "upgrade" ? '
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
    assert re.search(r"Deploy both\s+images\s+by\s+digest", readme)
    ecs = _text("modules/scenario/ecs.tf")
    observability = _text("modules/scenario/iam_observability.tf")
    assert re.search(
        r"cloudwatch_agent_container\s*=\s*\{.*?image\s*=\s*terraform_data\.cloudwatch_agent_image_provenance\.output",
        ecs,
        re.DOTALL,
    )
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
    sidecar = ecs[ecs.index("cloudwatch_agent_container = {") : ecs.index("gateway_container = {")]
    observed_env_names = frozenset(re.findall(r'\{ name = "([^"]+)", value = ', sidecar))

    assert observed_env_names == task_definition._CLOUDWATCH_AGENT_ENV_NAMES
    command_line = next(line for line in module_locals.splitlines() if line.strip().startswith("cw_agent_command"))
    assert json.loads(command_line.split("=", 1)[1].strip()) == task_definition._CLOUDWATCH_AGENT_COMMAND

    expected_health = task_definition.CLOUDWATCH_AGENT_HEALTH_CHECK
    assert json.dumps(expected_health.command[1]) in sidecar
    for name, value in (
        ("interval", expected_health.interval_seconds),
        ("timeout", expected_health.timeout_seconds),
        ("retries", expected_health.retries),
        ("startPeriod", expected_health.start_period_seconds),
    ):
        assert re.search(rf"\b{name}\s*=\s*{value}\b", sidecar)

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
    root_outputs = "\n".join((_text("scenario-a/outputs.tf"), _text("scenario-b/outputs.tf"), _text("scenario-c/outputs.tf")))
    readme = _text("README.md")

    schema_container = ecs[ecs.index("schema_init_doctor_container = {") : ecs.index("runtime_doctor_container = {")]
    runtime_container = ecs[ecs.index("runtime_doctor_container = {") : ecs.index("payload_container = {")]
    assert 'command                = ["doctor", "aws-ecs", "--init-schema", "--json"]' in schema_container
    assert re.search(r"\bsecrets\s*=\s*local\.schema_owner_secrets\b", schema_container)
    assert 'command                = ["doctor", "aws-ecs", "--json"]' in runtime_container
    assert re.search(r"\bsecrets\s*=\s*local\.runtime_secrets\b", runtime_container)
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
    published_user = task_definition.PUBLISHED_WEB_CONTAINER_USER

    ecs = _text("modules/scenario/ecs.tf")
    payload = ecs[ecs.index("payload_container = {") : ecs.index("local_auth_container = {")]
    local_auth = ecs[ecs.index("local_auth_container = {") : ecs.index("rollback_environment = [")]
    for container in (payload, local_auth):
        assert re.search(rf'\buser\s*=\s*"{re.escape(published_user)}"', container)


def test_upgrade_cognito_subject_binds_after_pool_creation_not_before() -> None:
    """The subject cannot exist before the apply that creates the pool.

    Requiring a nonempty subject on the fresh apply was a bootstrap
    deadlock: Terraform demanded a Cognito `sub` from a pool it had not
    created yet. Empty is the documented pre-bind state; the acceptance
    inventory check refuses to run while the pool has no bound subject.
    """
    module_variables = _text("modules/scenario/variables.tf")
    outputs = _text("modules/scenario/outputs.tf")
    scenario_a_variables = _text("scenario-a/variables.tf")
    scenario_b_variables = _text("scenario-b/variables.tf")
    scenario_c_variables = _text("scenario-c/variables.tf")
    scenario_b_example = _text("examples/scenario-b.tfvars.example")
    scenario_c_example = _text("examples/scenario-c.tfvars.example")

    assert 'variable "cognito_subject_sub"' in module_variables
    assert 'var.deployment_lifecycle == "first" ? var.cognito_subject_sub == ""' in module_variables
    assert 'var.cognito_subject_sub == "" || can(regex(' in module_variables
    assert 'cognito_subject_sub             = local.deployment_mode == "upgrade" ? var.cognito_subject_sub : ""' in outputs
    assert re.search(r'variable "cognito_subject_sub".*?default\s*=\s*""', scenario_a_variables, re.DOTALL)
    assert re.search(
        r'variable "cognito_subject_sub".*?default\s*=\s*""',
        scenario_b_variables,
        re.DOTALL,
    )
    assert re.search(
        r'variable "cognito_subject_sub".*?condition\s*=\s*\(\s*'
        r'var\.cognito_subject_sub\s*==\s*""',
        scenario_b_variables,
        re.DOTALL,
    )
    assert re.search(
        r'cognito_subject_sub\s*=\s*""',
        scenario_b_example,
    )
    assert "re-apply" in scenario_b_example
    assert 'variable "cognito_subject_sub"' not in scenario_c_variables
    assert "cognito_subject_sub" not in scenario_c_example


def test_scenario_b_reuses_the_single_bootstrap_run_identity() -> None:
    scenario_a_example = _text("examples/scenario-a.tfvars.example")
    scenario_b_example = _text("examples/scenario-b.tfvars.example")
    readme = _text("README.md")

    assert 'run_id                  = "REPLACE_WITH_LOWERCASE_UUID"' in scenario_a_example
    assert 'run_id                  = "REPLACE_WITH_LOWERCASE_UUID"' in scenario_b_example
    assert "REPLACE_WITH_DIFFERENT_LOWERCASE_UUID" not in scenario_b_example
    assert "`scenario_id` already isolates" in readme


def test_explicit_aws_profile_is_bound_across_provider_backend_and_local_cli() -> None:
    module_variables = _text("modules/scenario/variables.tf")
    module_outputs = _text("modules/scenario/outputs.tf")
    database_bootstrap = _text("modules/scenario/database_bootstrap.tf")
    readme = _text("README.md")

    assert 'variable "aws_profile"' in module_variables
    for root in ("bootstrap", "scenario-a", "scenario-b", "scenario-c"):
        variables = _text(f"{root}/variables.tf")
        provider = _text(f"{root}/versions.tf")
        example = _text(f"examples/{root}.tfvars.example")
        assert 'variable "aws_profile"' in variables
        assert "profile             = var.aws_profile" in provider
        assert "aws_profile" in example
    for scenario in ("scenario-a", "scenario-b", "scenario-c"):
        assert re.search(
            r'profile\s*=\s*"REPLACE_WITH_AWS_PROFILE"',
            _text(f"examples/{scenario}.s3.tfbackend.example"),
        )
        assert re.search(r"\baws_profile\s+=\s+var\.aws_profile\b", _text(f"{scenario}/main.tf"))

    assert re.search(r"\bAWS_PROFILE\s*=\s*var\.aws_profile\b", database_bootstrap)
    # run-task, wait, describe-tasks, and the wait-failure stop-task arm.
    assert database_bootstrap.count('--profile "$AWS_PROFILE"') == 4
    assert 'aws --profile "$AWS_PROFILE" --region "$AWS_REGION"' in readme
    assert "--profile ${jsonencode(var.aws_profile)}" in module_outputs
    assert "--region ${jsonencode(var.aws_region)}" in module_outputs


COLD_INSTALL_RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "aws-ecs-cold-install.md"


def test_cold_install_runbook_does_not_re_derive_the_namespace() -> None:
    """The runbook must reuse the namespace, not recover it by string surgery.

    `NAMESPACE=${ECS_CLUSTER%-cluster}` stripped only the suffix, but
    `cluster_name` is `acceptance-<namespace>-cluster` — prefixed as well. The
    log-group, dashboard, and X-Ray names are NOT prefixed, so on a perfectly
    healthy install the verification queries looked for `acceptance-<ns>-...`
    resources that do not exist, and the canonical cold-install procedure
    reported a false failure.

    String surgery on one name to recover another re-encodes the naming
    convention in a second place, which is the defect itself. This test pins
    that the surgery is gone, that the namespace is derived exactly once, and
    that the per-resource suffixes the runbook still spells out match the module
    that actually builds them.
    """
    locals_text = _text("modules/scenario/locals.tf")
    runbook = COLD_INSTALL_RUNBOOK.read_text(encoding="utf-8")

    # The relationship the string surgery got wrong: the cluster is the one
    # name carrying an `acceptance-` prefix on top of the namespace.
    assert re.search(r'cluster_name\s+=\s+"acceptance-\$\{local\.namespace\}-cluster"', locals_text)

    for name in ("namespace", "cluster_name"):
        for relative in (
            "modules/scenario/outputs.tf",
            "scenario-a/outputs.tf",
            "scenario-b/outputs.tf",
            "scenario-c/outputs.tf",
        ):
            assert f'output "{name}"' in _text(relative), f"{relative} must expose the {name} output"

    # The runbook reuses the namespace it already had to compute at install
    # time. `scenario_a_namespace` is exported before apply because it feeds the
    # tfvars, so there is no state to read an output from at that point; reusing
    # it here introduces no derivation the procedure did not already require.
    # (A `terraform output -raw namespace` read is drift-proof but only resolves
    # post-apply, which would add a second mechanism alongside a mandatory one.)
    assert 'NAMESPACE="$scenario_a_namespace"' in runbook
    assert runbook.count("export scenario_a_namespace=") == 1, "the namespace must be derived exactly once"
    assert "%-cluster" not in runbook, "the runbook must not re-derive the namespace from the cluster name"

    # Every namespace-derived literal the runbook still spells out has to match
    # the local that builds the real resource. The log-group query is a
    # deliberate prefix (it sweeps -web, -doctor and -operator-metrics in one
    # call); the dashboard and X-Ray queries name one resource exactly.
    for local_name, queried, exact in (
        ("web_log_group", "/aws/ecs/${NAMESPACE}", False),
        ("dashboard_name", "${NAMESPACE}-elspeth-aws-operator-v1", True),
        ("xray_group_name", "${NAMESPACE}-xray", True),
    ):
        declaration = re.search(rf'{local_name}\s+=\s+"(?P<template>[^"]+)"', locals_text)
        assert declaration is not None, f"{local_name} is no longer a simple template"
        built = declaration.group("template").replace("${local.namespace}", "${NAMESPACE}")
        if exact:
            assert built == queried, f"the runbook queries {queried!r} but the module builds {built!r}"
        else:
            assert built.startswith(queried), f"the runbook uses prefix {queried!r}, which no longer matches the built name {built!r}"
        assert queried in runbook


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
        "plan12",
        "raw-image-ref",
        "baseline-copy",
    ):
        assert forbidden not in all_text.lower()


def test_source_enumeration_sees_uncommitted_files_but_not_operator_inputs(tmp_path: Path) -> None:
    """The package census must track what git would ship, not the working tree.

    Operator inputs the README tells you to create here are gitignored and
    must NOT count as package source (they made every real cold install turn
    this suite red, and leaked the operator's own account id into the
    no-account-data assertion). A new package file that is merely uncommitted
    must still be counted, so a forgotten `git add` cannot slip a file past
    the census.

    Exercised against a throwaway repo carrying the real package's ignore
    rules: mutating the live package directory would race the other tests
    enumerating it in parallel.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text((PACKAGE / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "scenario-a.tfvars").write_text('aws_account_id = "000000000000"\n', encoding="utf-8")
    (tmp_path / "examples" / "scenario-a.s3.tfbackend").write_text('bucket = "operator-state"\n', encoding="utf-8")
    (tmp_path / "examples" / "scenario-a.tfvars.example").write_text('aws_account_id = "REPLACE"\n', encoding="utf-8")
    (tmp_path / "uncommitted.tf").write_text("# never git added\n", encoding="utf-8")

    listed = {path.relative_to(tmp_path).as_posix() for path in _source_files(tmp_path)}

    assert "uncommitted.tf" in listed
    assert "examples/scenario-a.tfvars.example" in listed
    assert "examples/scenario-a.tfvars" not in listed
    assert "examples/scenario-a.s3.tfbackend" not in listed


def test_dashboard_metrics_preserve_one_row_per_metric() -> None:
    """CloudWatch requires `metrics` to be an array OF ARRAYS of strings.

    Terraform's `flatten()` is recursive, so wrapping the
    per-identity/per-metric comprehension in it collapses every metric row
    into a single flat string list and CreateDashboard rejects the body with
    one "Should be array" error per element (1885 of them against the live
    account). The rows must be joined one level only — `concat(...)` with the
    spread operator — so each row stays its own array.
    """
    observability = _text("modules/scenario/iam_observability.tf")
    dashboard = observability[observability.index('resource "aws_cloudwatch_dashboard" "operator"') :]
    row_builder = re.search(
        r"metrics\s*=\s*(?P<joiner>\w+)\(\[\s*\n\s*for identity_dimensions in local\.cloudwatch_dimension_lists",
        dashboard,
    )
    assert row_builder is not None, "dashboard metric rows are no longer built per identity dimension list"
    assert row_builder.group("joiner") != "flatten", (
        "dashboard metric rows must not be recursively flattened; join one level with concat(...) instead"
    )


def test_composer_wall_clock_fits_under_the_app_guard_and_the_alb() -> None:
    """The composer wall clock must fail before the transport does.

    The envelope is a coupled three-leg chain driven by one variable
    (elspeth-09c91778f5): `var.alb_idle_timeout_seconds` sets the ALB
    idle_timeout in `network.tf`, `locals.tf` wires the app's transport
    ceiling env var to the same variable (so the boot guard validates
    against the real proxy limit, not the WebSettings default), and
    `var.composer_timeout_seconds`'s plan-time validation caps the wall
    clock at the ceiling minus the app's headroom. A wall clock past the
    proxy's patience would let the load balancer abort the connection
    before the backend's own timeout fired, replacing the discriminated
    422 (which persists the partial pipeline) with an opaque 504 (which
    does not). That is a live-only failure, so it is caught here.

    The chain replaced independent literals that had to agree by
    discipline: 300/240 (elspeth-f159d2394b) could not fund the shipped
    corpus — battery round-5 g03's first authoring call lands at t=413s
    and its compose settles at ~490-514s, so the ceiling itself was the
    defect, not the wall-clock's margin under it.

    Headroom is still read from the `WebSettings` field default, which is
    only the value the task actually boots with while this module ships no
    environment override for it — so the absence of a headroom override is
    asserted rather than assumed. The ceiling, by contrast, IS shipped as
    an override now, and this test asserts it is wired to the ALB variable
    verbatim rather than pinned to a divergent literal.
    """
    tf_sources = "\n".join(path.read_text(encoding="utf-8") for path in _source_files() if path.name.endswith(".tf"))
    assert "ELSPETH_WEB__COMPOSER_TRANSPORT_HEADROOM_" not in tf_sources, (
        "the module now overrides the composer transport headroom, so this test's headroom "
        "must be read from that override instead of from the WebSettings default"
    )
    headroom = WebSettings.model_fields["composer_transport_headroom_seconds"].default

    locals_text = _text("modules/scenario/locals.tf")
    assert re.search(
        r'\{\s*name\s*=\s*"ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS",\s*value\s*=\s*tostring\(var\.alb_idle_timeout_seconds\)\s*\}',
        locals_text,
    ), (
        "the scenario module no longer wires ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS "
        "to var.alb_idle_timeout_seconds; the boot guard would validate the wall clock against the "
        "WebSettings default ceiling instead of the real ALB idle timeout"
    )
    assert re.search(
        r'\{\s*name\s*=\s*"ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS",\s*value\s*=\s*tostring\(var\.composer_timeout_seconds\)\s*\}',
        locals_text,
    ), "the scenario module no longer wires ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS to var.composer_timeout_seconds"

    variables_text = _text("modules/scenario/variables.tf")
    alb_var_match = re.search(
        r'variable\s+"alb_idle_timeout_seconds"\s*\{.*?default\s*=\s*(?P<seconds>[\d.]+)',
        variables_text,
        re.DOTALL,
    )
    assert alb_var_match is not None, "var.alb_idle_timeout_seconds no longer declares a default; a stock install would prompt for it"
    alb_idle_seconds = float(alb_var_match.group("seconds"))

    var_match = re.search(
        r'variable\s+"composer_timeout_seconds"\s*\{.*?default\s*=\s*(?P<seconds>[\d.]+)',
        variables_text,
        re.DOTALL,
    )
    assert var_match is not None, "var.composer_timeout_seconds no longer declares a default; a stock install would prompt for it"
    timeout_seconds = float(var_match.group("seconds"))

    cap_match = re.search(
        r'variable\s+"composer_timeout_seconds"\s*\{.*?condition\s*=[^\n]*<=\s*var\.alb_idle_timeout_seconds\s*-\s*(?P<headroom>[\d.]+)',
        variables_text,
        re.DOTALL,
    )
    assert cap_match is not None, (
        "var.composer_timeout_seconds lost its plan-time validation cap against "
        "var.alb_idle_timeout_seconds; an over-limit value would be discovered at service roll "
        "instead of terraform plan"
    )
    assert float(cap_match.group("headroom")) == headroom, (
        f"the plan-time validation's headroom literal {cap_match.group('headroom')}s no longer "
        f"mirrors the WebSettings composer_transport_headroom_seconds default ({headroom}s); "
        f"a plan-clean value could still refuse to boot"
    )

    assert re.search(
        r"idle_timeout\s*=\s*var\.alb_idle_timeout_seconds",
        _text("modules/scenario/network.tf"),
    ), (
        "the scenario ALB idle_timeout is no longer wired to var.alb_idle_timeout_seconds; "
        "the transport ceiling env var would advertise a limit the proxy does not honour"
    )

    assert timeout_seconds + headroom <= alb_idle_seconds, (
        f"composer timeout {timeout_seconds}s plus {headroom}s headroom exceeds the ALB "
        f"idle_timeout {alb_idle_seconds}s; the ALB would abort with a 504 before the "
        f"composer returned its 422 and the partial pipeline would be lost."
    )

    # The floor matters as much as the ceiling now: the corpus needs ~490-514s
    # of compose wall (round-5 arm-B g03), so a default that drifts back under
    # that re-opens elspeth-09c91778f5 silently.
    assert timeout_seconds >= 840, (
        f"composer timeout default {timeout_seconds}s is below the 840s battery-proven envelope; "
        f"the shipped corpus (g03 ~490-514s compose) would no longer be fundable at package defaults "
        f"(elspeth-09c91778f5)"
    )


def test_composer_candidate_reasoning_effort_is_pinned_to_the_measured_value() -> None:
    """A cold install must boot on the effort the evidence was taken at.

    The candidate role's reasoning effort is the other half of the
    composer's wall-clock budget, and it was selected on measurement, not
    taste: at the code default `high` the acceptance graph g08 returned a
    422 at the 270s wall clock, its six calls summing to ~270s
    (45+50+123+29+17+6) with the worst a 123s thinking tail — the budget
    went to the chain, not to any one call. At `medium` the same graph
    completed in ~200s and the regression graph g01 was unchanged (184s vs
    192s) — `docs/acceptance/2026-08-05-compose-cost-measurement.md`,
    addendum 1, the operator decision recorded on elspeth-930a163c85.

    That decision was applied to the live task definition as an env-only
    revision and never reached this module, so every green acceptance
    result on this module was taken on a configuration a cold
    `terraform apply` could not reproduce (elspeth-52af290183). Pinning it
    here is what makes the shipped module and the measured evidence the
    same deployment. The measurement is Bedrock-specific and does not
    transfer to the other deployment surfaces; see the module README.

    The pin is checked against `WebSettings` rather than against a literal
    copy of the vocabulary, because the failure mode is asymmetric: an
    unknown `ELSPETH_WEB__` name or an out-of-vocabulary value does not
    degrade the composer, it stops the container from booting at all
    (`settings_from_env` raises, `ReasoningEffort` is a closed `Literal`).
    """
    field_name = "composer_candidate_reasoning_effort"
    assert field_name in WebSettings.model_fields, (
        f"WebSettings no longer has a {field_name} field; the module would ship an unbootable environment"
    )

    effort_match = re.search(
        r'\{\s*name\s*=\s*"ELSPETH_WEB__COMPOSER_CANDIDATE_REASONING_EFFORT",\s*value\s*=\s*"(?P<effort>[a-z]+)"\s*\}',
        _text("modules/scenario/locals.tf"),
    )
    assert effort_match is not None, (
        "the scenario module no longer pins ELSPETH_WEB__COMPOSER_CANDIDATE_REASONING_EFFORT, "
        "so a cold install would fall back to the code default and silently revert the one "
        "change measured to flip g08 from a 422 to a pass"
    )
    effort = effort_match.group("effort")

    legal_efforts = get_args(WebSettings.model_fields[field_name].annotation)
    assert effort in legal_efforts, f"pinned reasoning effort {effort!r} is not one of {legal_efforts}; the web task would refuse to boot"
    assert effort == "medium", (
        f"the scenario module pins reasoning effort {effort!r}, but every green acceptance "
        f"result was obtained at 'medium'. Changing this pin re-opens the measurement: "
        f"record the new evidence before changing the value."
    )


def test_every_shipped_web_environment_name_is_a_real_websettings_field() -> None:
    """An unknown `ELSPETH_WEB__` name in this module is a boot outage.

    `settings_from_env` raises `RuntimeError: Unknown ELSPETH_WEB__ setting`
    on any name that is not a `WebSettings` field, so a misspelt key here
    does not fall back to a default or get ignored — the web container
    fails to start, after terraform has already reported success. Terraform
    cannot see the Python model and the model cannot see terraform, so
    nothing else in either tree closes this seam.

    Asserted over the whole module rather than over one key: the cost of a
    typo is identical whichever line it lands on. The names are matched
    bare rather than only inside quotes, because the module puts a variable
    in front of the process in three shapes and only one of them quotes the
    name: `runtime_environment` in `locals.tf` emits
    `{ name = "ELSPETH_WEB__X", ... }`, the `plugin_policy_projection` map
    at the top of the same file uses each name as a bare HCL key that
    `policy_environment` then emits verbatim, and the `ecs_identity_wrapper`
    entrypoint in `ecs.tf` emits `export ELSPETH_WEB__X="$value"`. A
    quoted-only match reads the first and silently skips the other two,
    though all three reach the same `settings_from_env` call.

    Comment lines are removed before scanning. The bare pattern cannot tell
    a shipped name from one merely named in prose, so documenting a
    *removed* setting — the form `deploy/elspeth-web.env` already uses —
    would otherwise fail CI claiming the task cannot boot, while nothing is
    shipped at all. Only whole-line comments are stripped: truncating at
    every `#` would also cut string literals that legitimately contain one
    (`iam_observability.tf` has a markdown heading, `storage_identity.tf` a
    URL), and no name is reached only through a trailing comment.
    """
    prefix = "ELSPETH_WEB__"
    name_pattern = rf"\b({prefix}[A-Z0-9_]+)"
    tf_lines = itertools.chain.from_iterable(
        path.read_text(encoding="utf-8").splitlines() for path in _source_files() if path.name.endswith(".tf")
    )
    tf_sources = "\n".join(line for line in tf_lines if not line.lstrip().startswith(("#", "//")))
    shipped = sorted(set(re.findall(name_pattern, tf_sources)))
    assert shipped, "no ELSPETH_WEB__ environment names found in the module; this test is no longer reading the shipped environment"

    # Pinned directly rather than through a representative name: every name
    # the entrypoint exports is also shipped as a quoted `runtime_environment`
    # entry, so their presence proves nothing about which shapes are read.
    assert re.findall(name_pattern, 'export ELSPETH_WEB__PORT="$port"') == [f"{prefix}PORT"], (
        "the scan no longer matches an unquoted name, so the `export ELSPETH_WEB__...` lines "
        "in the ecs.tf entrypoint and the bare plugin-policy map keys in locals.tf are "
        "invisible to it again (elspeth-52af290183 review, F2/N1)"
    )
    assert re.findall(rf"export\s+({prefix}[A-Z0-9_]+)", tf_sources), (
        "the ecs.tf entrypoint no longer exports any ELSPETH_WEB__ names; confirm they moved "
        "into runtime_environment rather than out of this test's reach"
    )

    unknown = [name for name in shipped if name[len(prefix) :].lower() not in WebSettings.model_fields]
    assert not unknown, (
        f"the module ships ELSPETH_WEB__ names with no matching WebSettings field: {unknown}. settings_from_env raises on these, so the web task would fail to boot after a successful terraform apply."
    )

    # Being a real field is necessary but not sufficient: the deployment
    # region is deliberately taken from the ambient AWS_REGION, and
    # settings_from_env rejects the ELSPETH_WEB__ spelling by name.
    assert f"{prefix}DEPLOYMENT_AWS_REGION" not in shipped, (
        "the module sets the reserved ELSPETH_WEB__DEPLOYMENT_AWS_REGION; settings_from_env rejects it by name and the web task would fail to boot"
    )


def _quoted_plugin_ids(body: str) -> set[str]:
    """Plugin ids from an HCL list body, ignoring commented-out lines."""
    live = "\n".join(line for line in body.splitlines() if not line.strip().startswith("#"))
    return set(re.findall(r'"((?:source|sink|transform):[a-z0-9_]+)"', live))


def test_scenario_allowlist_authorizes_the_llm_source_like_every_other_llm_plugin() -> None:
    """``source:llm`` carries the same posture as ``transform:llm``.

    The plugin shipped and was discoverable from the first release, but was
    authorized in neither the always-authorized web core nor the default
    allowlist, so no cold install could author an LLM source at all. Two
    acceptance battery rounds recorded the resulting absence as the composer
    "authoring around" the mechanism before elspeth-ba6a8dff24 identified it
    as a configuration gap.

    Pinned as a pair with ``transform:llm``: the operator's decision was
    parity between the two, so a future edit that drops one and keeps the
    other is the regression this catches.
    """
    locals_text = _text("modules/scenario/locals.tf")

    allowlist_match = re.search(
        r"default_plugin_allowlist\s*=\s*\[(?P<body>.*?)\n  \]",
        locals_text,
        re.DOTALL,
    )
    assert allowlist_match is not None
    allowlist = _quoted_plugin_ids(allowlist_match.group("body"))
    assert {"source:llm", "transform:llm"} <= allowlist

    core_match = re.search(
        r"required_web_plugin_ids\s*=\s*toset\(\[(?P<body>.*?)\n  \]\)",
        locals_text,
        re.DOTALL,
    )
    assert core_match is not None
    assert {"source:llm", "transform:llm"} <= _quoted_plugin_ids(core_match.group("body"))


def test_scenario_allowlist_authorizes_the_document_sink_alongside_the_text_sink() -> None:
    """``sink:document`` carries the same posture as ``sink:text``.

    ``text`` refuses any value carrying CR or LF to hold its one-row-one-record
    invariant, so ``document`` is the only sink that can publish generated
    multiline text. Authorizing ``text`` alone leaves a composer asked to write
    a generated announcement to a file with no correct sink to choose — it
    authors the refusing one, which discards the row and publishes a zero-byte
    artifact (elspeth-afdf55a17c).

    Pinned as a pair for the same reason ``source:llm``/``transform:llm`` is:
    dropping one and keeping the other silently restores the gap.
    """
    locals_text = _text("modules/scenario/locals.tf")

    allowlist_match = re.search(
        r"default_plugin_allowlist\s*=\s*\[(?P<body>.*?)\n  \]",
        locals_text,
        re.DOTALL,
    )
    assert allowlist_match is not None
    assert {"sink:document", "sink:text"} <= _quoted_plugin_ids(allowlist_match.group("body"))

    core_match = re.search(
        r"required_web_plugin_ids\s*=\s*toset\(\[(?P<body>.*?)\n  \]\)",
        locals_text,
        re.DOTALL,
    )
    assert core_match is not None
    assert {"sink:document", "sink:text"} <= _quoted_plugin_ids(core_match.group("body"))


def test_scenario_allowlist_authorizes_the_remedies_the_sinks_name() -> None:
    """A remedy the surface does not authorize is a remedy nobody can take.

    ``text`` tells an author to route multiline values through ``line_explode``
    and ``document`` tells them to combine rows with ``report_assemble``. Both
    sinks are always authorized, so both remedies must be too — otherwise the
    guidance names a plugin the Composer cannot see, which is the dead end that
    produced elspeth-afdf55a17c: a prohibition with no reachable alternative.
    """
    locals_text = _text("modules/scenario/locals.tf")
    remedies = {"transform:line_explode", "transform:report_assemble"}

    allowlist_match = re.search(
        r"default_plugin_allowlist\s*=\s*\[(?P<body>.*?)\n  \]",
        locals_text,
        re.DOTALL,
    )
    assert allowlist_match is not None
    assert remedies <= _quoted_plugin_ids(allowlist_match.group("body"))

    core_match = re.search(
        r"required_web_plugin_ids\s*=\s*toset\(\[(?P<body>.*?)\n  \]\)",
        locals_text,
        re.DOTALL,
    )
    assert core_match is not None
    assert remedies <= _quoted_plugin_ids(core_match.group("body"))


def test_always_authorized_web_core_mirror_matches_the_locals_set() -> None:
    """variables.tf restates the web core because HCL validation cannot read a local.

    Two copies of one list drift silently, and the drift is invisible until an
    operator's plugin_preferences entry is rejected for naming something the
    deployment does authorize. Pin them equal.
    """
    locals_text = _text("modules/scenario/locals.tf")
    variables_text = _text("modules/scenario/variables.tf")

    core_match = re.search(
        r"required_web_plugin_ids\s*=\s*toset\(\[(?P<body>.*?)\n  \]\)",
        locals_text,
        re.DOTALL,
    )
    mirror_match = re.search(
        r"Hand-mirrors locals\.tf's required_web_plugin_ids.*?toset\(\[(?P<body>.*?)\]\)",
        variables_text,
        re.DOTALL,
    )
    assert core_match is not None
    assert mirror_match is not None
    assert _quoted_plugin_ids(core_match.group("body")) == _quoted_plugin_ids(mirror_match.group("body"))


def test_terraform_web_core_matches_the_python_required_set() -> None:
    """The HCL mirrors must equal REQUIRED_WEB_PLUGIN_IDS, the runtime authority.

    The test above pins the two HCL copies against EACH OTHER, and nothing
    compared either to Python. So all three could agree with one another and
    still disagree with the set the runtime actually authorizes
    (``compile_web_plugin_policy`` unions ``REQUIRED_WEB_PLUGIN_IDS`` in
    regardless of what Terraform says).

    Drift is not an access-control hole — Python stays authoritative — but the
    deployment record an operator reads becomes false, and the plan-time
    precondition in ecs.tf then rejects a plugin the deployment does authorize.

    This is the FOURTH instance of one archetype on this branch alone: an
    authorization list maintained by hand in more places than anything checks.
    Deriving the expectation from Python is what stops a fifth
    (elspeth-4efd6a7242).
    """
    from elspeth.web.plugin_policy.compiler import REQUIRED_WEB_PLUGIN_IDS

    core_match = re.search(
        r"required_web_plugin_ids\s*=\s*toset\(\[(?P<body>.*?)\n  \]\)",
        _text("modules/scenario/locals.tf"),
        re.DOTALL,
    )
    assert core_match is not None

    assert _quoted_plugin_ids(core_match.group("body")) == {str(plugin_id) for plugin_id in REQUIRED_WEB_PLUGIN_IDS}
