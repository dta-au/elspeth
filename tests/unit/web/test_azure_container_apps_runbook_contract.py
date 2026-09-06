"""Executable contract for the Azure Container Apps runbooks and skill.

Mirrors ``test_aws_ecs_runbook_contract.py`` for the ACA trio. The two rules
that matter most here are the ECS lessons: every epoch literal in prose or
JSON is byte-bound to the live constants (a drifted literal is exactly the
runbook hazard the ECS acceptance found), and every platform call in the
acceptance runbook goes through the protected capture wrappers so raw output
never reaches a receipt.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import yaml

from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH
from elspeth.web._aws_ecs_acceptance import receipt_contracts
from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNBOOK_DIR = REPO_ROOT / "docs" / "runbooks"
ACCEPTANCE_RUNBOOK = RUNBOOK_DIR / "azure-container-apps-deployment.md"
COLD_INSTALL_RUNBOOK = RUNBOOK_DIR / "azure-container-apps-cold-install.md"
REDEPLOY_RUNBOOK = RUNBOOK_DIR / "azure-container-apps-existing-service-redeploy.md"
RUNBOOKS = (ACCEPTANCE_RUNBOOK, COLD_INSTALL_RUNBOOK, REDEPLOY_RUNBOOK)
PLATFORM_FACTS = REPO_ROOT / "docs" / "plans" / "2026-09-05-phase6b-azure-container-apps-platform-facts.md"
SKILL_DIR = REPO_ROOT / ".agents" / "skills" / "operating-azure-container-apps"
SKILL_SYMLINK = REPO_ROOT / ".claude" / "skills" / "operating-azure-container-apps"
KEY_DERIVATION_MODULE = REPO_ROOT / "src" / "elspeth" / "web" / "key_derivation.py"
KEY_DERIVATION_TEST = REPO_ROOT / "tests" / "unit" / "web" / "test_key_derivation_wiring.py"

FACTS_LINK = "../plans/2026-09-05-phase6b-azure-container-apps-platform-facts.md"
RECEIPT_PATH = "docs/operator/evidence/azure-container-apps/0.8.0.json"
INGRESS_REQUEST_TIMEOUT_SECONDS = 240
MECHANISMS = (
    "session_operation_fence",
    "session_operation_fence_execute",
    "role_revocation_lease_expiry",
    "graceful_stop",
    "postgresql_and_nfs",
    "owner_affine",
)
CHECK_KINDS = (
    "verify-doctor-job",
    "verify-storage-job",
    "verify-blob-managed-identity",
    "verify-log-analytics",
    "verify-connection-budget",
    "compatibility-record",
    "revision-rollout",
    "replica-fence-conflict",
    "replica-run-start",
    "replica-lease-takeover",
    "replica-progress",
    "resource-graph-cleanup",
    "testcontainer-run",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _fences(text: str, language: str) -> list[str]:
    return re.findall(rf"```{language}\n(.*?)```", text, flags=re.DOTALL)


def _skill_texts() -> list[str]:
    paths = (SKILL_DIR / "SKILL.md", *sorted((SKILL_DIR / "references").glob("*.md")))
    return [_text(path) for path in paths]


def _compatibility_record() -> dict[str, object]:
    text = _text(ACCEPTANCE_RUNBOOK)
    heading = text.index("## Bound release/schema compatibility record")
    fence = text.index("```json\n", heading) + len("```json\n")
    record, _ = json.JSONDecoder().raw_decode(text[fence:])
    assert isinstance(record, dict)
    return record


def test_every_bash_fence_is_syntactically_valid() -> None:
    for runbook in RUNBOOKS:
        fences = _fences(_text(runbook), "bash")
        assert len(fences) >= 4, runbook.name
        for index, script in enumerate(fences, start=1):
            result = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True, check=False)
            assert result.returncode == 0, f"{runbook.name} bash fence {index}: {result.stderr}"


def test_acceptance_fences_route_platform_calls_through_protected_capture() -> None:
    for script in _fences(_text(ACCEPTANCE_RUNBOOK), "bash"):
        for line in script.splitlines():
            stripped = line.lstrip()
            for raw in ("az ", "psql ", "bicep ", "curl "):
                assert not stripped.startswith(raw), line


def test_protected_command_wrappers_are_bounded_and_redacted() -> None:
    text = _text(ACCEPTANCE_RUNBOOK)
    capture = text[text.index("### Protected command capture") : text.index("### Inputs")]

    for helper in ("az_capture", "az_deploy_capture", "az_exec_capture", "bicep_capture", "curl_capture", "psql_capture"):
        assert f"{helper}() {{" in capture, helper
    for marker in (
        "ELSPETH_COMMAND_OUTPUT_LIMIT_BYTES=2097152",
        "ulimit -f 4096",
        "timeout --signal=TERM --kill-after=5s",
        "trap 'rm -f -- \"$stderr_file\"' RETURN",
        "chmod 600",
        "az_command_failed",
        "az_deployment_failed",
        "psql_command_failed",
        "bicep_command_failed",
        "http_request_failed",
        "command_kind_invalid",
        "command_timeout_invalid",
        "--set=ON_ERROR_STOP=1",
        "AZURE_CORE_ONLY_SHOW_ERRORS=true",
    ):
        assert marker in capture, marker
    assert 'cat "$stderr_file"' not in capture
    for script in _fences(capture, "bash"):
        assert "PGPASSWORD" not in script
    assert "never receives a connection URI on its command line" in " ".join(capture.split())


def test_compatibility_record_is_byte_bound_to_the_live_derivation() -> None:
    record = _compatibility_record()

    assert record["schema"] == "elspeth.azure-container-apps-compatibility-receipt.v1"
    assert record["scenario_id"] == "A"
    assert record["schema_facts"] == receipt_contracts._expected_schema_facts("A")
    assert record["candidate_package_version"] == receipt_contracts._CANDIDATE_PACKAGE_VERSION
    for empty_field in (
        "previous_source_sha",
        "previous_image_digest",
        "previous_revision_sha256",
        "rollback_doctor_job_sha256",
        "previous_package_version",
    ):
        assert record[empty_field] == "", empty_field
    for subject in ("candidate_revision_sha256", "candidate_doctor_job_sha256"):
        assert subject in record, subject
    assert record["backward_compatible"] is False
    assert record["rollback_permitted"] is False
    assert record["forward_compatible"] is True
    for stale_subject in ("task_definition", "task_arn"):
        assert stale_subject not in json.dumps(record), stale_subject


def test_every_epoch_literal_matches_the_live_constants() -> None:
    texts = [_text(runbook) for runbook in RUNBOOKS] + _skill_texts()
    session_hits = 0
    landscape_hits = 0
    for text in texts:
        for match in re.finditer(r"session epoch (\d+)", text, flags=re.IGNORECASE):
            session_hits += 1
            assert int(match.group(1)) == SESSION_SCHEMA_EPOCH, match.group(0)
        for match in re.finditer(r"landscape epoch (\d+)", text, flags=re.IGNORECASE):
            landscape_hits += 1
            assert int(match.group(1)) == SQLITE_SCHEMA_EPOCH, match.group(0)
        for match in re.finditer(r'"session_epoch":\s*(\d+)', text):
            assert int(match.group(1)) == SESSION_SCHEMA_EPOCH, match.group(0)
        for match in re.finditer(r'"landscape_epoch":\s*(\d+)', text):
            assert int(match.group(1)) == SQLITE_SCHEMA_EPOCH, match.group(0)
    assert session_hits >= 3
    assert landscape_hits >= 3


def test_storage_contract_is_stated_identically_in_every_runbook() -> None:
    for runbook in RUNBOOKS:
        assert "Azure Files carries no database" in _text(runbook), runbook.name
    for runbook in (ACCEPTANCE_RUNBOOK, COLD_INSTALL_RUNBOOK):
        normalized = " ".join(_text(runbook).split())
        for phrase in (
            "NFS 4.1",
            "SMB Azure Files is not supported",
            "Azure Database for PostgreSQL Flexible Server",
            "`sqlite-single`",
            "no SQLite mode at replicas > 1",
            "/mnt/elspeth",
            "1654:1654",
            "NoRootSquash",
        ):
            assert phrase in normalized, (runbook.name, phrase)


def test_replica_probes_name_their_mechanism_and_run_in_order() -> None:
    text = _text(ACCEPTANCE_RUNBOOK)
    probes = text[text.index("## Replica probes") : text.index("## Connection budget")]
    normalized = " ".join(probes.split())

    assert "in the order **P1, P2, P4, P3**" in normalized
    for mechanism in MECHANISMS:
        assert f"`{mechanism}`" in probes, mechanism
    for phrase in (
        "ALTER ROLE elspeth_runtime_a NOLOGIN;",
        "pg_terminate_backend(pid)",
        "usename = current_user AND pid <> pg_backend_pid()",
        "ALTER ROLE elspeth_runtime_a LOGIN;",
        "terminationGracePeriodSeconds: 0",
        "downgrades to `graceful_stop`",
        "asserts that no `run_start_permits` row exists",
        "P4b (recorded, cannot pass)",
        "`kill -9 1`",
        "recorded, not used",
        "`mechanism: unreachable`",
        "https://elspeth-web---<label>.<defaultDomain>",
        "X-Elspeth-Instance",
        "stickySessions.affinity: none",
    ):
        assert phrase in normalized or phrase in probes, phrase
    assert "cannot grant `pg_signal_backend`" in normalized
    assert "GRANT pg_signal_backend" not in probes


def test_transport_ceiling_is_bound_below_the_ingress_timeout() -> None:
    for runbook in (ACCEPTANCE_RUNBOOK, COLD_INSTALL_RUNBOOK):
        text = _text(runbook)
        assert "ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS" in text, runbook.name
        assert f"{INGRESS_REQUEST_TIMEOUT_SECONDS} seconds" in text, runbook.name
        assert 'test "$ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS" -le 240' in text, runbook.name
        for match in re.finditer(r"COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS=(\d+)", text):
            assert int(match.group(1)) <= INGRESS_REQUEST_TIMEOUT_SECONDS, match.group(0)
    acceptance = _text(ACCEPTANCE_RUNBOOK)
    assert "the example parameter file uses 210" in " ".join(acceptance.split())


def test_runbooks_cite_the_measured_facts_and_the_bundle_and_declare_their_status() -> None:
    assert PLATFORM_FACTS.is_file()
    for runbook in RUNBOOKS:
        text = _text(runbook)
        assert FACTS_LINK in text, runbook.name
        assert "deploy/azure-container-apps" in text, runbook.name
        assert RECEIPT_PATH in text, runbook.name
        assert "**Status.**" in text, runbook.name
        assert "not a support claim" in " ".join(text.split()), runbook.name
        assert "**LIVE" in text, runbook.name


def test_image_publication_is_a_digest_preserving_copy_in_every_runbook() -> None:
    for runbook in RUNBOOKS:
        text = _text(runbook)
        assert "docker buildx imagetools create" in text, runbook.name
        assert "acr manifest show-metadata" in text, runbook.name
        assert 'test "$ACR_DIGEST" = "$' in text, runbook.name
        assert "@sha256:" in text or "@${ACR_DIGEST}" in text, runbook.name
        assert "docker build " not in text, runbook.name


def test_redeploy_pins_single_revision_rollout_and_conditional_rollback() -> None:
    text = _text(REDEPLOY_RUNBOOK)
    normalized = " ".join(text.split())
    for phrase in (
        'test -z "$(git status --porcelain)"',
        "activeRevisionsMode: Single",
        'Require `activeRevisionsMode == "Single"`',
        "--revision-suffix",
        "elspeth doctor deployment --json",
        "az containerapp update --name",
        "/api/health",
        "/api/ready",
        "/api/system/status",
        "X-Elspeth-Instance",
        "rollback_permitted: true",
        "repair forward",
        "cosign verify",
    ):
        assert phrase in normalized or phrase in text, phrase
    rollback = text[text.index("## Rollback") :]
    assert "revision activate" in rollback
    assert "revision deactivate" in rollback
    assert "--revision-weight" in rollback


def test_cold_install_orders_storage_schema_runtime_before_traffic() -> None:
    text = _text(COLD_INSTALL_RUNBOOK)
    headings = [line for line in text.splitlines() if re.match(r"^## \d\. ", line)]
    assert [heading[:5] for heading in headings] == [f"## {n}." for n in range(1, 10)]
    order = (
        "## 2. Deploy the environment",
        "## 3. Publish the image by digest",
        "## 4. Store secrets in Key Vault",
        "## 5. Provision storage",
        "## 6. Initialize schemas",
        "## 7. Prove runtime credentials",
        "## 8. Deploy the workload",
        "## 9. Verify",
    )
    positions = [text.index(heading) for heading in order]
    assert positions == sorted(positions)
    normalized = " ".join(text.split())
    for phrase in (
        "provision-storage",
        "doctor-schema-init",
        "elspeth doctor deployment --init-schema --json",
        "elspeth doctor deployment --json",
        "sslmode=verify-full&sslrootcert=system",
        "`STALE` is a stop",
        "supportsHttpsTrafficOnly: false",
        "445 and 2049",
        "privatelink.file.core.windows.net",
        "privatelink.postgres.database.azure.com",
        "purge protection",
        "failureThreshold` at 10",
    ):
        assert phrase in normalized, phrase


def test_secret_rotation_cites_the_key_derivation_authority() -> None:
    assert KEY_DERIVATION_MODULE.is_file()
    assert KEY_DERIVATION_TEST.is_file()
    text = _text(ACCEPTANCE_RUNBOOK)
    rotation = " ".join(text[text.index("## Secret rotation") : text.index("## Disposable acceptance cleanup")].split())
    assert "src/elspeth/web/key_derivation.py" in rotation
    assert "tests/unit/web/test_key_derivation_wiring.py" in rotation
    assert "rotating the SSO transaction secret does not invalidate user secrets or session tokens" in rotation
    assert "rotating `secret_key` itself invalidates all four derived keys" in rotation
    assert "do not restate the consumer list here" in rotation


def test_receipt_vocabulary_is_closed_and_named() -> None:
    text = _text(ACCEPTANCE_RUNBOOK)
    receipt = " ".join(text[text.index("## Receipt and docs flip") :].split())
    for kind in CHECK_KINDS:
        assert f"`{kind}`" in receipt, kind
    assert "in **one commit**" in receipt
    assert "the bar is a second clean run end to end" in receipt


def test_testcontainer_run_is_recorded_with_the_ci_selection_and_gated() -> None:
    """6b-4 option (b): the ACA driver records CI's exact testcontainer selection and the shared gate requires it."""
    from elspeth.web._acceptance_common.testcontainer_run import TESTCONTAINER_RUN_GATE_REASONS, TESTCONTAINER_SELECTION

    text = _text(ACCEPTANCE_RUNBOOK)
    section = text[text.index("## Testcontainer run") : text.index("## Evidence")]
    assert "uv run --frozen pytest " + " ".join(TESTCONTAINER_SELECTION) in section
    assert "python -m elspeth.web._acceptance_common.testcontainer_run" in section
    assert "--provider azure" in section
    assert "`testcontainer_run_gate`" in section
    for reason in sorted(TESTCONTAINER_RUN_GATE_REASONS - {"testcontainer_run_invalid"}):
        assert f"`{reason}`" in section, reason
    flat = " ".join(section.split())
    assert "no external-DSN seam" not in flat
    assert "`tests/helpers/postgres_target.py`" in flat and "`ELSPETH_TEST_POSTGRES_URL`" in flat
    assert "`database`, `database_identity_sha256`" in flat
    assert (
        'export ELSPETH_TEST_POSTGRES_URL="postgresql+psycopg://${PG_ADMIN_USER}:${PGPASSWORD}@${PGHOST}:5432/postgres?sslmode=verify-full&sslrootcert=${AZURE_PG_ROOTS_PEM}"'
        in section
    )
    assert ': "${AZURE_PG_ROOTS_PEM:?' in section and "unset ELSPETH_TEST_POSTGRES_URL" in section
    assert (
        section.index("export ELSPETH_TEST_POSTGRES_URL=")
        < section.index("uv run --frozen pytest ")
        < section.index("unset ELSPETH_TEST_POSTGRES_URL")
    )
    assert text.index("## Connection budget") < text.index("## Testcontainer run") < text.index("## Evidence")


def test_skill_mirrors_the_ecs_layout_and_worktree_guidance() -> None:
    assert (SKILL_DIR / "SKILL.md").is_file()
    assert (SKILL_DIR / "agents" / "openai.yaml").is_file()
    assert (SKILL_DIR / "references" / "command-cheatsheet.md").is_file()
    assert (SKILL_DIR / "references" / "test-and-triage.md").is_file()
    assert SKILL_SYMLINK.is_symlink()
    assert SKILL_SYMLINK.resolve() == SKILL_DIR.resolve()

    skill = _text(SKILL_DIR / "SKILL.md")
    frontmatter = yaml.safe_load(skill.split("---\n")[1])
    assert frontmatter["name"] == "operating-azure-container-apps"
    assert "Do not use for AWS ECS" in " ".join(frontmatter["description"].split())

    combined = "\n".join(_skill_texts())
    assert not re.search(r"(?m)^\s*uv sync\b", combined)
    assert not re.search(r"(?m)^\s*uv run\b", combined)
    assert "PYTHONPATH" in combined
    assert ".venv/bin/pytest" in combined
    assert "Azure Files carries no database" in combined
    assert "docs/plans/2026-09-05-phase6b-azure-container-apps-platform-facts.md" in combined

    agent = yaml.safe_load(_text(SKILL_DIR / "agents" / "openai.yaml"))
    assert agent["interface"]["display_name"] == "Operate Azure Container Apps"
    assert "$operating-azure-container-apps" in agent["interface"]["default_prompt"]
