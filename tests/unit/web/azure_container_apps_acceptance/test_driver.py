"""Execute the acceptance shell with bounded platform and facade command fakes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from elspeth.web._azure_container_apps_acceptance.evidence import connection_budget_details

DRIVER = Path(__file__).resolve().parents[4] / "deploy/azure-container-apps/scripts/acceptance.sh"
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64

# Each process records argv before replying. Real bash, timeout, jq and the
# filesystem exercise control flow; no source-text assertions substitute for it.
COMMAND_FAKE = r"""
import json
import os
import pathlib
import sys

name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
if os.environ.get("CHECK_FILE_LIMIT"):
    import resource
    assert resource.getrlimit(resource.RLIMIT_FSIZE)[0] == resource.RLIM_INFINITY
with open(os.environ["COMMAND_LOG"], "a") as log:
    log.write(json.dumps([name, *args]) + "\n")

def option(key):
    return args[args.index(key) + 1]

def emit(value):
    print(json.dumps(value))

if os.environ.get("FAIL_AT") and " ".join([name, *args]).startswith(os.environ["FAIL_AT"]):
    print("sensitive platform failure content", file=sys.stderr)
    sys.exit(17)

revision = "elspeth-web--r" + os.environ["CANDIDATE_SHA"][:12]
revision_file = pathlib.Path(os.environ["COMMAND_LOG"]).with_suffix(".revision")
if revision_file.exists():
    revision = revision_file.read_text()
app_id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/elspeth-acc-test/providers/Microsoft.App/containerApps/elspeth-web"
if name == "date":
    import subprocess
    if "-d" in args:
        sys.exit(subprocess.run(["/usr/bin/date", *args], check=False).returncode)
    base = "2026-09-05T10:00:37Z"
    if args[-1] == "+%s":
        counter = pathlib.Path(os.environ["COMMAND_LOG"]).with_suffix(".clock-count")
        count = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(count + 1))
        epoch = int(subprocess.check_output(["/usr/bin/date", "-d", base, "+%s"]))
        print(epoch + count * 600)
    else:
        sys.exit(subprocess.run(["/usr/bin/date", "-d", base, *args], check=False).returncode)
elif name == "git":
    if "rev-parse" in args:
        print(os.environ.get("CHECKOUT_SHA", os.environ["CANDIDATE_SHA"]))
    elif os.environ.get("DIRTY_CHECKOUT"):
        print(" M src/changed.py")
elif name == "python" and args[0] == "-c":
    sys.exit(0)
elif name == "python":
    module, command = args[1:3]
    if module == "pytest":
        xml = next(arg.split("=", 1)[1] for arg in args if arg.startswith("--junitxml="))
        pathlib.Path(xml).write_text('<testsuite tests="1"><testcase name="proof"/></testsuite>')
        sys.exit(int(os.environ.get("PYTEST_EXIT", "0")))
    elif module == "elspeth.web.azure_container_apps_single_revision":
        directory = pathlib.Path(option("--evidence-dir"))
        directory.mkdir(mode=0o700)
        actual_revision = option("--revision")
        (directory / "binding.json").write_text(json.dumps({
            "container_app_id": app_id, "revision": actual_revision, "replica": actual_revision + "-replica2",
        }))
        emit({"probe": option("--probe")})
        sys.exit(1 if option("--probe") == os.environ.get("FAIL_SINGLE_PROBE") else 0)
    elif module == "elspeth.web._acceptance_common.testcontainer_run":
        emit({"junit_sha256": "d" * 64})
    elif command == "verify-connection-budget":
        from datetime import datetime
        from elspeth.web._acceptance_common.errors import AcceptanceCheckError
        from elspeth.web._azure_container_apps_acceptance.evidence import connection_budget_details
        try:
            details = connection_budget_details(
                json.loads(pathlib.Path(option("--metrics")).read_text()),
                window_start=datetime.fromisoformat(option("--window-start")),
                acceptance_run_id=option("--acceptance-run-id"), server_id=option("--server-id"),
                max_connections=int(option("--max-connections")),
                approved_budget=int(option("--approved-budget")), safety_margin=int(option("--safety-margin")),
            )
        except AcceptanceCheckError:
            sys.exit(1)
        emit(details)
    elif command == "extract-exec-receipt":
        emit({"check": option("--check")})
    elif command == "receipt-store":
        print("c" * 64)
    elif command == "bundle-validate":
        emit({"passed": os.environ.get("FAIL_BUNDLE") != "1"})
        sys.exit(1 if os.environ.get("FAIL_BUNDLE") == "1" else 0)
    else:
        emit({"observation": command})
        if command == "replica-probes" and option("--probe") == os.environ.get("FAIL_PROBE"):
            sys.exit(1)
elif name == "bash":
    assert args[0].endswith("/scripts/bootstrap-acceptance.sh")
    output = pathlib.Path(args[3])
    output.mkdir()
    for file in ("workload.parameters.json", "workload-a.parameters.json", "workload-b.parameters.json"):
        (output / file).write_text('{"parameters":{"namePrefix":{"value":"elspeth"}}}')
    names = ("ELSPETH_ACCEPTANCE_PG_ADMIN_URL", "ELSPETH_ACCEPTANCE_PG_RUNTIME_A_URL",
             "ELSPETH_ACCEPTANCE_PG_RUNTIME_B_URL", "ELSPETH_ACCEPTANCE_SESSION_DB_URL",
             "ELSPETH_ACCEPTANCE_LANDSCAPE_URL", "ELSPETH_TEST_POSTGRES_URL")
    (output / "acceptance-env.json").write_text(json.dumps({key: "postgresql://example.test/postgres?sslmode=verify-full" for key in names}))
elif name == "curl":
    target = pathlib.Path(option("--output"))
    if args[-1].endswith(("/api/health", "/api/ready")):
        pending_file = pathlib.Path(os.environ["COMMAND_LOG"]).with_suffix(".http-counts.json")
        counts = json.loads(pending_file.read_text()) if pending_file.exists() else {}
        counts[args[-1]] = counts.get(args[-1], 0) + 1
        pending_file.write_text(json.dumps(counts))
        scope = os.environ.get("HTTP_PENDING_SCOPE", "")
        in_scope = not scope or scope in target.name
        if in_scope and (os.environ.get("HTTP_ALWAYS_UNREADY") or counts[args[-1]] <= int(os.environ.get("HTTP_PENDING_POLLS", "0"))):
            target.write_text('{"ready":false}')
            print("503")
            sys.exit(22)
    if args[-1].endswith("/api/sessions") or args[-1].endswith("/blobs/inline"):
        import uuid
        target.write_text(json.dumps({"id": str(uuid.uuid4())}))
    elif args[-1].endswith("/guided/start"):
        target.write_text(json.dumps({"next_turn": {"turn_token": "9" * 64}}))
    else:
        target.write_text('{"ready":true}')
    if "--write-out" in args:
        print("200")
elif name == "docker":
    emit({})
elif args[:2] == ["group", "exists"]:
    print(os.environ.get("GROUP_EXISTS", "false"))
elif args[:3] == ["deployment", "sub", "create"]:
    values = {
        "keyVaultName": "elspeth-kv-unique", "logAnalyticsCustomerId": "workspace-id",
        "postgresServerResourceId": "postgres-id", "environmentDefaultDomain": "example.test",
        "postgresFqdn": "example.test",
    }
    emit({"properties": {"outputs": {key: {"value": value} for key, value in values.items()}}})
elif args[:3] == ["deployment", "group", "create"]:
    if "deployWebApp=true" in args and "activeRevisionsMode=Single" in args:
        suffix = next(arg.split("=", 1)[1] for arg in args if arg.startswith("revisionSuffix="))
        revision_file.write_text("elspeth-web--" + suffix)
    emit({})
elif args[:3] == ["acr", "manifest", "show-metadata"]:
    print(os.environ.get("ACR_DIGEST", os.environ["CANDIDATE_IMAGE_DIGEST"]))
elif args[:3] == ["containerapp", "job", "start"]:
    print(option("--name") + "-execution")
elif args[:4] == ["containerapp", "job", "execution", "show"]:
    emit({"name": option("--job-execution-name"), "properties": {"status": os.environ.get("JOB_STATUS", "Succeeded")}})
elif args[:4] == ["containerapp", "job", "logs", "show"]:
    report = [{"name": "session_schema", "ok": True, "detail": "ok"}]
    emit({"Log": json.dumps(report)})
elif args[:2] == ["containerapp", "show"]:
    emit({"id": app_id, "properties": {
        "latestReadyRevisionName": revision, "latestRevisionName": revision,
        "configuration": {"activeRevisionsMode": "Single", "ingress": {
            "fqdn": "app.example.test", "stickySessions": {"affinity": "sticky"},
        }}, "template": {"scale": {"minReplicas": 2, "maxReplicas": 2}},
    }})
elif args[:3] == ["containerapp", "replica", "list"]:
    rev = option("--revision")
    emit([{"name": rev + "-replica1", "properties": {"runningState": "Running"}},
          {"name": rev + "-replica2", "properties": {"runningState": "Running"}}])
elif args[:3] == ["containerapp", "revision", "list"]:
    emit([{"name": revision, "traffic": 100, "state": "Running"}])
elif args[:2] == ["containerapp", "exec"]:
    print("1654 1654 700\n1654 1654 700\n1654 1654 700")
elif args[:3] in (["containerapp", "revision", "show"], ["containerapp", "job", "show"]):
    emit({"properties": {"template": {"containers": [{"image": "registry.azurecr.io/elspeth@" + os.environ["CANDIDATE_IMAGE_DIGEST"]}]}}})
elif args[:3] == ["monitor", "log-analytics", "query"]:
    emit([{"Lag": 1, "Log_s": "evidence"}])
elif args[:3] == ["monitor", "metrics", "list"]:
    from datetime import datetime, timedelta
    count_file = pathlib.Path(os.environ["COMMAND_LOG"]).with_suffix(".metrics-count")
    attempt = int(count_file.read_text()) + 1 if count_file.exists() else 1
    count_file.write_text(str(attempt))
    count = 9 if attempt <= int(os.environ.get("METRICS_PENDING_POLLS", "0")) else 10
    start = datetime.fromisoformat(option("--start-time"))
    data = [{"timeStamp": (start + timedelta(minutes=i)).isoformat(), "maximum": 10} for i in range(count)]
    emit({"value": [{"name": {"value": "active_connections"}, "timeseries": [{"data": data}]}]})
elif args[:2] == ["graph", "query"]:
    emit({"data": [{"Count": 0}]})
elif args[:2] == ["keyvault", "show-deleted"]:
    emit({"properties": {"scheduledPurgeDate": "2027-01-01T00:00:00Z"}})
elif args[:2] == ["keyvault", "list-deleted"]:
    emit([])
else:
    emit({})
"""


@dataclass
class DriverRun:
    environment: dict[str, str]
    evidence: Path
    command_log: Path

    def run(self, stage: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/bash", str(DRIVER), stage],
            env=self.environment,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )

    def commands(self) -> list[list[str]]:
        if not self.command_log.exists():
            return []
        return [json.loads(line) for line in self.command_log.read_text().splitlines()]


@pytest.fixture
def driver(tmp_path: Path) -> DriverRun:
    commands = tmp_path / "bin"
    commands.mkdir()
    for command in ("az", "python", "docker", "curl", "bash", "git", "date"):
        executable = commands / command
        executable.write_text(f"#!{sys.executable}\n{COMMAND_FAKE}")
        executable.chmod(0o700)
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    parameters = tmp_path / "parameters.json"
    parameters.write_text(json.dumps({"parameters": {"namePrefix": {"value": "elspeth"}}}))
    document = tmp_path / "input.json"
    document.write_text(json.dumps({"junit_sha256": "d" * 64}))
    source_blob = tmp_path / "source-blob.json"
    source_blob.write_text(json.dumps({"filename": "probe.csv", "content": "row_id\n1\n", "mime_type": "text/csv"}))
    message = tmp_path / "message.json"
    message.write_text(json.dumps({"content": "Explain the existing pipeline without changing it."}))
    guided_action = tmp_path / "guided-action.json"
    guided_action.write_text(json.dumps({"custom_inputs": ["Use the provided CSV source"]}))
    pipeline = tmp_path / "pipeline.yaml"
    pipeline.write_text("sources: {}\n")
    command_log = tmp_path / "commands.jsonl"
    environment = {
        **os.environ,
        "PATH": f"{commands}:{os.environ['PATH']}",
        "COMMAND_LOG": str(command_log),
        "ACCEPTANCE_RUN_ID": "test",
        "AZURE_SUBSCRIPTION_ID": "11111111-1111-1111-1111-111111111111",
        "AZURE_LOCATION": "australiaeast",
        "CANDIDATE_SHA": SHA,
        "CANDIDATE_IMAGE_DIGEST": DIGEST,
        "ACR_LOGIN_SERVER": "registry.azurecr.io",
        "MAIN_PARAMETERS": str(parameters),
        "WORKLOAD_PARAMETERS": str(parameters),
        "WORKLOAD_A_PARAMETERS": str(parameters),
        "WORKLOAD_B_PARAMETERS": str(parameters),
        "EVIDENCE_DIR": str(evidence),
        "ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS": "210",
        "ELSPETH_JOB_WAIT_SECONDS": "1",
        "ELSPETH_POLL_SECONDS": "1",
        "P1_TRIAL_REQUESTS": str(document),
        "P1_BODY": str(guided_action),
        "P1_INTENT": "Inspect the acceptance fixture",
        "P2_SESSION_IDS": str(document),
        "P3_SESSION_ID": "p3-session",
        "P4_SESSION_ID": "p4-session",
        "P3_SINK_PATH": str(tmp_path / "outputs/{session_id}/sink.csv"),
        "P3_SINK_KEY_FIELD": "row_id",
        "PROBE_YAML": str(pipeline),
        "P3_YAML": str(pipeline),
        "PROBE_SOURCE_BLOB": str(source_blob),
        "P4_MESSAGE_BODY": str(message),
        "ACCEPTANCE_SECRET_DIR": str(tmp_path),
        "BOOTSTRAP_PRINCIPAL_ID": "11111111-1111-1111-1111-111111111111",
        "BOOTSTRAP_PRINCIPAL_TYPE": "User",
        "PROVISION_STORAGE_IMAGE": "registry.example/provision@" + DIGEST,
        "PGSSLROOTCERT": str(document),
        "ELSPETH_ACCEPTANCE_CANARY_TOKEN": "acceptance-private-canary",
        "ELSPETH_ACCEPTANCE_BEARER_TOKEN": "test-acceptance-token",
        "SENTINEL_HASH": "e" * 64,
        "PG_MAX_CONNECTIONS": "100",
        "PG_APPROVED_BUDGET": "60",
        "PG_SAFETY_MARGIN": "20",
        "COMPATIBILITY_RECORD": str(document),
        "TESTCONTAINER_RECEIPT": str(document),
    }
    return DriverRun(environment, evidence, command_log)


def command_index(commands: list[list[str]], prefix: list[str]) -> int:
    return next(index for index, command in enumerate(commands) if command[: len(prefix)] == prefix)


def test_complete_driver_orders_jobs_probes_receipts_and_cleanup(driver: DriverRun) -> None:
    result = driver.run("all")
    assert result.returncode == 0, result.stderr
    commands = driver.commands()
    deployments = [command for command in commands if command[:4] == ["az", "deployment", "group", "create"]]
    assert len(deployments) == 6
    assert "deployWebApp=false" in deployments[0]
    assert "deployWebApp=false" in deployments[1]
    assert "deployWebApp=true" in deployments[2]
    assert f"candidateSourceSha={SHA}" in deployments[2]
    assert "activeRevisionsMode=Single" in deployments[-1]
    assert "minReplicas=2" in deployments[-1] and "maxReplicas=2" in deployments[-1]
    assert "stickySessionsAffinity=sticky" in deployments[-1]
    starts = [command[5] for command in commands if command[:4] == ["az", "containerapp", "job", "start"]]
    assert starts == ["provision-storage", "doctor-schema-init", "doctor-runtime-a", "doctor-runtime-b", "verify-blob-managed-identity"]
    first_job = command_index(commands, ["az", "containerapp", "job", "start"])
    assert commands.index(deployments[1]) < first_job < commands.index(deployments[2])
    probes = [command[command.index("--probe") + 1] for command in commands if "replica-probes" in command]
    assert probes == ["fence-conflict", "run-start", "progress", "lease-takeover"]
    p2 = next(command for command in commands if "run-start" in command)
    assert p2[p2.index("--session-ids") + 1] == str(driver.evidence / "p2-session-ids.json")
    sessions = json.loads((driver.evidence / "p2-session-ids.json").read_text())
    assert len(sessions) == len(set(sessions)) == 20
    assert p2[p2.index("--trials") + 1] == "20"
    p1_requests = json.loads((driver.evidence / "p1-trial-requests.json").read_text())
    assert len(p1_requests) == len({trial["session_id"] for trial in p1_requests}) == 20
    assert len({trial["body"]["operation_id"] for trial in p1_requests}) == 20
    assert all(trial["body"]["turn_token"] == "9" * 64 for trial in p1_requests)
    single_requests = json.loads((driver.evidence / "single-p1-trial-requests.json").read_text())
    single_sessions = {trial["session_id"] for trial in single_requests}
    assert len(single_requests) == len(single_sessions) == 20
    assert single_sessions.isdisjoint(trial["session_id"] for trial in p1_requests)
    singles = [command for command in commands if "elspeth.web.azure_container_apps_single_revision" in command]
    assert [command[command.index("--probe") + 1] for command in singles] == ["P1", "P4a"]
    takeover = next(command for command in commands if "takeover" in command)
    assert commands.index(takeover) < commands.index(deployments[-1]) < commands.index(singles[0])
    single_store = next(
        command for command in commands if "receipt-store" in command and str(driver.evidence / "single-p1.receipt.json") in command
    )
    assert single_store[single_store.index("--subject-id") + 1].endswith(f"r{SHA[:12]}-single-replica2")
    single_preparation = [command for command in commands if command[0] == "curl" and "/prepared-single-" in " ".join(command)]
    assert single_preparation and all(command[-1].startswith("https://app.example.test/") for command in single_preparation)
    storage = next(command for command in commands if "--owner-uid" in command)
    assert storage[storage.index("--owner-uid") + 1] == "1654"
    assert storage[storage.index("--mode") + 1] == "0700"
    assert command_index(commands, ["az", "group", "delete"]) < command_index(
        commands, ["python", "-m", "elspeth.web.azure_container_apps_acceptance", "bundle-validate"]
    )
    assert (driver.evidence / "takeover-observation.json").is_file()
    assert (driver.evidence / "resource-graph-cleanup.receipt.json").is_file()
    assert (driver.evidence / "bundle.json").read_text().strip() == '{"passed": true}'
    assert "test-acceptance-token" not in json.dumps(commands)
    requests = [command for command in commands if command[0] == "curl"]
    assert requests
    assert all(command[command.index("--max-filesize") + 1] == "2097152" for command in requests)


@pytest.mark.parametrize("trials", ["0", "1", "19", "-1", "1.5", "x"])
def test_weak_trials_fail_before_live_probe(driver: DriverRun, trials: str) -> None:
    driver.environment["PROBE_TRIALS"] = trials
    # environment stage produces the inventory consumed by the staged probe.
    assert driver.run("environment").returncode == 0
    assert driver.run("bootstrap").returncode == 0
    result = driver.run("probes")
    assert result.returncode != 0
    assert "probe_trials_insufficient" in result.stderr or "integer_input_invalid" in result.stderr
    assert not any("replica-probes" in command for command in driver.commands())


def test_job_failure_stops_rollout_and_cleans_up(driver: DriverRun) -> None:
    driver.environment["FAIL_AT"] = "az containerapp job start --name doctor-runtime-b"
    result = driver.run("all")
    assert result.returncode == 17
    assert "sensitive platform failure content" not in result.stderr
    commands = driver.commands()
    assert any(command[:3] == ["az", "group", "delete"] for command in commands)
    assert not any("deployWebApp=true" in command for command in commands)
    assert not any("bundle-validate" in command for command in commands)


def test_job_wait_has_a_deadline(driver: DriverRun) -> None:
    driver.environment["JOB_STATUS"] = "Running"
    result = driver.run("jobs")
    assert result.returncode != 0
    assert "job_execution_timeout" in result.stderr
    starts = [command for command in driver.commands() if command[:4] == ["az", "containerapp", "job", "start"]]
    assert len(starts) == 1


def test_probe_downgrade_keeps_receipt_collects_evidence_and_fails(driver: DriverRun) -> None:
    driver.environment["FAIL_PROBE"] = "lease-takeover"
    result = driver.run("all")
    assert result.returncode != 0
    assert (driver.evidence / "replica-lease-takeover.receipt.json").is_file()
    assert (driver.evidence / "log-analytics.receipt.json").is_file()
    assert (driver.evidence / "resource-graph-cleanup.receipt.json").is_file()


def test_interrupted_collector_restores_roles_before_cleanup(driver: DriverRun) -> None:
    driver.environment["FAIL_AT"] = "python -m elspeth.web.azure_container_apps_observations takeover"
    result = driver.run("all")
    assert result.returncode == 17
    commands = driver.commands()
    restores = [command for command in commands if "restore-owner" in command]
    assert [command[-1] for command in restores] == ["elspeth_runtime_a", "elspeth_runtime_b"]
    assert commands.index(restores[-1]) < command_index(commands, ["az", "group", "delete"])


def test_cleanup_failure_is_fatal(driver: DriverRun) -> None:
    driver.environment["FAIL_AT"] = "az group delete"
    result = driver.run("all")
    assert result.returncode == 17
    assert "cleanup_failed" in result.stderr
    assert not (driver.evidence / "bundle.json").exists()


def test_purge_refusal_records_real_scheduled_tombstone(driver: DriverRun) -> None:
    driver.environment["FAIL_AT"] = "az keyvault purge"
    result = driver.run("all")
    assert result.returncode == 0, result.stderr
    cleanup = next(command for command in driver.commands() if "resource-graph-cleanup-validate" in command)
    assert "--scheduled-purge-date" in cleanup
    assert cleanup[cleanup.index("--scheduled-purge-date") + 1] == "2027-01-01T00:00:00Z"


def test_existing_group_is_never_deleted(driver: DriverRun) -> None:
    driver.environment["GROUP_EXISTS"] = "true"
    result = driver.run("all")
    assert result.returncode != 0
    assert "acceptance_group_already_exists" in result.stderr
    assert not any(command[:3] == ["az", "group", "delete"] for command in driver.commands())


def test_resolved_parameter_file_reaches_what_if(driver: DriverRun) -> None:
    assert driver.run("environment").returncode == 0
    what_if = next(command for command in driver.commands() if "what-if" in command)
    assert what_if[what_if.index("--parameters") + 1] == "@" + driver.environment["MAIN_PARAMETERS"]


def test_placeholder_parameters_fail_before_azure(driver: DriverRun) -> None:
    Path(driver.environment["MAIN_PARAMETERS"]).write_text(
        json.dumps({"parameters": {"identity": {"value": "00000000-0000-0000-0000-000000000000"}}})
    )
    result = driver.run("environment")
    assert result.returncode != 0
    assert driver.commands() == []


def test_digest_mismatch_stops_before_jobs(driver: DriverRun) -> None:
    driver.environment["ACR_DIGEST"] = "sha256:" + "f" * 64
    result = driver.run("all")
    assert result.returncode != 0
    assert "image_copy_digest_mismatch" in result.stderr
    assert not any(command[:4] == ["az", "containerapp", "job", "start"] for command in driver.commands())


def test_failed_postgres_suite_keeps_receipt_and_stops_rollout(driver: DriverRun) -> None:
    driver.environment["PYTEST_EXIT"] = "1"
    result = driver.run("all")
    assert result.returncode == 1
    assert (driver.evidence / "testcontainer-pytest.exit").read_text().strip() == "1"
    assert (driver.evidence / "testcontainer-run.json").is_file()
    assert not any("deployWebApp=true" in command for command in driver.commands())
    assert any(command[:3] == ["az", "group", "delete"] for command in driver.commands())


@pytest.mark.parametrize("variable,value", [("CHECKOUT_SHA", "f" * 40), ("DIRTY_CHECKOUT", "yes")])
def test_receipts_cannot_attest_another_or_dirty_checkout(driver: DriverRun, variable: str, value: str) -> None:
    driver.environment[variable] = value
    result = driver.run("all")
    assert result.returncode != 0
    assert not any(command[:3] == ["python", "-m", "pytest"] for command in driver.commands())
    assert not (driver.evidence / "testcontainer-run.json").exists()


def test_bootstrap_failure_cleans_up_without_starting_jobs(driver: DriverRun) -> None:
    driver.environment["FAIL_AT"] = "bash "
    result = driver.run("all")
    assert result.returncode == 17
    assert not any(command[:4] == ["az", "containerapp", "job", "start"] for command in driver.commands())
    assert any(command[:3] == ["az", "group", "delete"] for command in driver.commands())


def test_capture_does_not_break_bicep_runtime_with_a_file_limit(driver: DriverRun) -> None:
    driver.environment["CHECK_FILE_LIMIT"] = "1"
    result = driver.run("image")
    assert result.returncode == 0, result.stderr


def test_missing_full_run_prerequisite_fails_before_cloud(driver: DriverRun) -> None:
    del driver.environment["BOOTSTRAP_PRINCIPAL_ID"]
    result = driver.run("all")
    assert result.returncode != 0
    assert driver.commands() == []


def test_probe_deployment_requires_inventory_before_cloud_mutation(driver: DriverRun) -> None:
    result = driver.run("workload-probes")
    assert result.returncode != 0
    assert driver.commands() == []


@pytest.mark.parametrize("scope", ["production", "label-a"])
def test_readiness_retries_observed_503_before_proceeding(driver: DriverRun, scope: str) -> None:
    driver.environment.update(HTTP_PENDING_SCOPE=scope, HTTP_PENDING_POLLS="1", ELSPETH_JOB_WAIT_SECONDS="5")
    result = driver.run("all")
    assert result.returncode == 0, result.stderr
    health = [command for command in driver.commands() if command[0] == "curl" and f"{scope}-health.json" in " ".join(command)]
    assert len(health) >= 2
    assert json.loads((driver.evidence / f"{scope}-ready.json").read_text()) == {"ready": True}


def test_expired_readiness_deadline_cleans_up_without_probes(driver: DriverRun) -> None:
    driver.environment["HTTP_ALWAYS_UNREADY"] = "1"
    result = driver.run("all")
    assert result.returncode != 0
    assert "http_readiness_timeout" in result.stderr
    assert not any("replica-probes" in command for command in driver.commands())
    assert any(command[:3] == ["az", "group", "delete"] for command in driver.commands())


def test_budget_window_is_minute_aligned_and_retries_incomplete_metrics(driver: DriverRun) -> None:
    driver.environment.update(METRICS_PENDING_POLLS="1", ELSPETH_LOG_QUERY_CEILING_SECONDS="5")
    result = driver.run("all")
    assert result.returncode == 0, result.stderr
    start = datetime.fromisoformat((driver.evidence / "budget-window-start.txt").read_text().strip())
    end = datetime.fromisoformat((driver.evidence / "budget-window-end.txt").read_text().strip())
    assert start == datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
    assert (end - start).total_seconds() == 600
    metrics_calls = [command for command in driver.commands() if command[:4] == ["az", "monitor", "metrics", "list"]]
    assert len(metrics_calls) == 2
    assert all(command[command.index("--start-time") + 1] == "2026-09-05T10:00:00Z" for command in metrics_calls)
    details = connection_budget_details(
        json.loads((driver.evidence / "active-connections.json").read_text()),
        window_start=start,
        acceptance_run_id="test",
        server_id="postgres-id",
        max_connections=100,
        approved_budget=60,
        safety_margin=20,
    )
    assert len(details["budget"]["points"]) == 10
    assert details["budget"]["ok"] is True


def test_incomplete_metric_window_times_out_without_receipt(driver: DriverRun) -> None:
    driver.environment.update(METRICS_PENDING_POLLS="100", ELSPETH_LOG_QUERY_CEILING_SECONDS="1")
    result = driver.run("all")
    assert result.returncode != 0
    assert "connection_budget_evidence_timeout" in result.stderr
    assert not (driver.evidence / "connection-budget.receipt.json").exists()


def test_single_revision_failed_probe_keeps_receipt_and_refuses_success(driver: DriverRun) -> None:
    driver.environment["FAIL_SINGLE_PROBE"] = "P1"
    result = driver.run("all")
    assert result.returncode != 0
    assert (driver.evidence / "single-p1.receipt.json").is_file()
    assert (driver.evidence / "single-p4.receipt.json").is_file()
    assert (driver.evidence / "resource-graph-cleanup.receipt.json").is_file()


def test_single_revision_discovery_failure_cleans_up_without_receipt(driver: DriverRun) -> None:
    driver.environment["FAIL_AT"] = "python -m elspeth.web.azure_container_apps_single_revision"
    result = driver.run("all")
    assert result.returncode != 0
    assert not (driver.evidence / "single-p1.receipt.json").exists()
    assert any(command[:3] == ["az", "group", "delete"] for command in driver.commands())


def test_standalone_single_revision_requires_persisted_database_context(driver: DriverRun) -> None:
    assert driver.run("environment").returncode == 0
    before = driver.commands()
    result = driver.run("single-revision")
    assert result.returncode != 0
    assert driver.commands() == before


def test_standalone_single_revision_rejects_missing_auth_before_deployment(driver: DriverRun) -> None:
    assert driver.run("environment").returncode == 0
    assert driver.run("bootstrap").returncode == 0
    del driver.environment["ELSPETH_ACCEPTANCE_BEARER_TOKEN"]
    before = driver.commands()
    result = driver.run("single-revision")
    assert result.returncode != 0
    assert "set existing acceptance bearer token" in result.stderr
    assert driver.commands() == before
