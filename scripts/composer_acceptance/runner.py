"""Drive synthetic scenarios through public Composer APIs; no graph authoring."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from typing import Any

from scripts.composer_acceptance.artifacts import current_artifacts
from scripts.composer_acceptance.checks import check_case
from scripts.composer_acceptance.serve import fingerprint


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


class ApiClient:
    """Promoted JSON HTTP primitives from evals/lib/common.sh's public API flow."""

    def __init__(self, base: str, token: str = ""):
        if not base.startswith("http://127.0.0.1:"):
            raise ValueError("Acceptance runner connects only to the isolated loopback service")
        self.base = base.rstrip("/")
        self.token = token

    def raw(self, path: str, body: dict[str, Any] | None = None) -> bytes:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=950 if body is not None else 15) as response:
                content = response.read()
                if not isinstance(content, bytes):
                    raise ValueError("HTTP response body is not bytes")
                return content
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code, exc.read().decode()) from None

    def json(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return json.loads(self.raw(path, body))


def save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def authenticate(api: ApiClient, output: Path) -> None:
    credentials_path = output / "local-account.json"
    if credentials_path.exists():
        credentials = json.loads(credentials_path.read_text())
    else:
        credentials = {"username": "battery_" + secrets.token_hex(6), "password": secrets.token_urlsafe(32)}
        api.json("/api/auth/register", {**credentials, "display_name": "Synthetic acceptance battery"})
        save(credentials_path, credentials)
        credentials_path.chmod(0o600)
    api.token = api.json("/api/auth/login", credentials)["access_token"]


def compose(api: ApiClient, prefix: str, prompt: str, out: Path, index: int) -> dict[str, Any]:
    save(out / f"turn-{index}.request.json", {"content": prompt})
    samples = []
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(api.json, prefix + "/messages", {"content": prompt})
        while True:
            try:
                result = future.result(timeout=5)
                break
            except TimeoutError:
                if future.done():
                    result = future.result()
                    break
                samples.append(api.json(prefix + "/composer-progress"))
                save(out / f"turn-{index}.progress.json", samples)
    save(out / f"turn-{index}.response.json", result)
    save(out / f"turn-{index}.timing.json", {"elapsed_seconds": time.monotonic() - started})
    save(out / f"turn-{index}.messages.json", api.json(prefix + "/messages?include_raw_content=true"))
    if not isinstance(result, dict):
        raise ValueError("Composer response must be an object")
    return result


def _pending_reviews(api: ApiClient, prefix: str, out: Path, scenario: dict[str, Any], accepted: set[str]) -> list[dict[str, Any]]:
    events = api.json(prefix + "/interpretations?status=pending")["events"]
    save(out / "pending-reviews.json", events)
    allowed = set(scenario["review_policy"]["allowed_types"])
    remaining = []
    for event in events:
        if event["id"] not in accepted:
            remaining.append(event)
            continue
        if event["kind"] not in allowed:
            raise ValueError(f"Review kind {event['kind']} is not authorized by this scenario")
        result = api.json(prefix + "/interpretations/" + event["id"] + "/resolve", {"choice": "accepted_as_drafted"})
        save(out / f"review-{event['id']}.resolved.json", result)
    return remaining


def _preview_seen(messages: list[dict[str, Any]], state: dict[str, Any], versions: list[dict[str, Any]]) -> bool:
    """Bind successful previews to executable content, including ordered maps.

    Turn settlement and metadata updates create new state IDs without changing
    the executable graph. Assistant messages bind the start of a tool batch;
    an applied tool's version advances that binding for subsequent calls.
    """
    from elspeth.web.composer.authority_hashing import composer_authority_canonical_json

    def executable(snapshot: dict[str, Any]) -> str:
        return composer_authority_canonical_json({key: snapshot[key] for key in ("sources", "nodes", "edges", "outputs")})

    by_id = {snapshot["id"]: snapshot for snapshot in versions}
    by_version = {snapshot["version"]: snapshot for snapshot in versions}
    if len(by_id) != len(versions) or len(by_version) != len(versions):
        raise ValueError("Composition history contains duplicate identities")
    if any(snapshot["session_id"] != state["session_id"] for snapshot in versions):
        raise ValueError("Composition history belongs to another session")
    current = by_id.get(state["id"])
    if current is None or executable(current) != executable(state):
        return False
    expected = executable(state)
    for message in messages:
        bound = by_id.get(message.get("composition_state_id"))
        for call in message.get("tool_calls") or []:
            if call.get("outcome") == "applied":
                bound = by_version.get(call.get("applied_state_version"))
            if (
                bound is not None
                and call.get("function", {}).get("name") == "preview_pipeline"
                and call.get("outcome") == "completed"
                and call.get("wire_conformant") is True
                and executable(bound) == expected
            ):
                return True
    return False


def _state_versions(api: ApiClient, prefix: str) -> list[dict[str, Any]]:
    versions: list[dict[str, Any]] = []
    while True:
        page = api.json(prefix + f"/state/versions?limit=200&offset={len(versions)}")
        versions.extend(page)
        if len(page) < 200:
            return versions


def assert_frozen(output: Path) -> str:
    before = json.loads((output / "source-before.json").read_text())
    current = fingerprint(Path(__file__).resolve().parents[2])
    if current["content_sha256"] != before["content_sha256"]:
        raise ValueError("Production or acceptance source changed since service launch")
    return str(current["content_sha256"])


def capture_session_audit(output: Path, session_id: str, out: Path) -> None:
    """Read private test session audit through a consistent SQLite backup."""
    source_path = output / "data" / "sessions.db"
    snapshot_path = out / "sessions-snapshot.db"
    with sqlite3.connect(f"file:{source_path}?mode=ro", uri=True) as source, sqlite3.connect(snapshot_path) as snapshot:
        source.backup(snapshot)
    with sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True) as snapshot:
        rows = snapshot.execute("SELECT tool_calls FROM chat_messages WHERE session_id=? ORDER BY sequence_no", (session_id,)).fetchall()
    calls = []
    tools = []
    for (encoded,) in rows:
        for record in json.loads(encoded) if encoded and encoded != "null" else []:
            if record.get("_kind") == "llm_call_audit":
                calls.append(record["call"])
            elif "function" in record:
                tools.append(
                    {
                        "name": record["function"]["name"],
                        "strict_sent": record.get("strict_sent"),
                        "wire_conformant": record.get("wire_conformant"),
                    }
                )
    save(out / "provider-audit.json", calls)
    save(out / "tool-audit.json", tools)


def runtime_evidence(output: Path, out: Path, scenario: dict[str, Any], state: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    audit_path = output / "data" / "runs" / "audit.db"
    snapshot_path = out / "landscape-snapshot.db"
    source = sqlite3.connect(f"file:{audit_path}?mode=ro", uri=True)
    snapshot = sqlite3.connect(snapshot_path)
    try:
        source.backup(snapshot)
    finally:
        source.close()
        snapshot.close()
    db = sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    run_id = run["landscape_run_id"]

    def rows(query: str) -> list[dict[str, Any]]:
        return [dict(row) for row in db.execute(query, (run_id,))]

    try:
        evidence: dict[str, Any] = {
            "sink_effects": rows(
                "SELECT effect_id,artifact_id,sink_node_id,stream_id,stream_sequence,predecessor_effect_id,state "
                "FROM sink_effects WHERE run_id=?"
            ),
            "source_rows": rows("SELECT row_id,row_index FROM rows WHERE run_id=?"),
            "tokens": rows("SELECT token_id,row_id,run_id,join_group_id FROM tokens WHERE run_id=?"),
            "token_parents": rows("SELECT token_id,parent_token_id,run_id,ordinal FROM token_parents WHERE run_id=?"),
            "token_outcomes": rows("SELECT token_id,outcome,path,completed,sink_name FROM token_outcomes WHERE run_id=?"),
            "lineage_frames": rows("SELECT token_id,depth,kind,group_id,member_key FROM token_lineage_frames WHERE run_id=?"),
            "nodes": rows("SELECT node_id,plugin_name,node_type FROM nodes WHERE run_id=?"),
            "runtime_calls": rows(
                "SELECT c.call_id,c.state_id,c.call_type,c.status,c.prompt_tokens,c.completion_tokens,c.request_ref,"
                "s.token_id,s.run_id,s.node_id,t.row_id,n.plugin_name FROM calls c "
                "JOIN node_states s ON s.state_id=c.state_id JOIN tokens t ON t.token_id=s.token_id "
                "JOIN nodes n ON n.node_id=s.node_id AND n.run_id=s.run_id WHERE s.run_id=? AND c.call_type='llm'"
            ),
        }
    finally:
        db.close()
    from elspeth.core.payload_store import FilesystemPayloadStore

    payloads = FilesystemPayloadStore(output / "data" / "payloads")
    for call in evidence["runtime_calls"]:
        payload = json.loads(payloads.retrieve(call.pop("request_ref")))
        call["model"] = payload["model"]
    evidence["expected_runtime_model"] = json.loads((output / "service-config.json").read_text())["runtime_model"]
    capture_session_audit(output, run["session_id"] if "session_id" in run else state["session_id"], out)
    db = sqlite3.connect(f"file:{out / 'sessions-snapshot.db'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    bound = {source["options"]["blob_ref"] for source in state["sources"].values() if "blob_ref" in source["options"]}
    blobs = []
    try:
        executed = db.execute("SELECT id,session_id,state_id,landscape_run_id FROM runs WHERE id=?", (run["run_id"],)).fetchone()
        if executed is None:
            raise ValueError("Executed session run is absent from the audit snapshot")
        evidence.update(
            session_run_id=executed["id"],
            session_id=executed["session_id"],
            composition_state_id=executed["state_id"],
            landscape_run_id=executed["landscape_run_id"],
        )
        if executed["session_id"] != state["session_id"]:
            raise ValueError("Executed run belongs to another session")
        for index, fixture in enumerate(scenario["inputs"]):
            uploaded = json.loads((out / f"input-{index}.json").read_text())
            blob = db.execute("SELECT id,filename,content_hash,storage_path,status FROM blobs WHERE id=?", (uploaded["id"],)).fetchone()
            if blob is None:
                blobs.append({"id": uploaded["id"], "filename": fixture["filename"], "status": "missing"})
                continue
            path = Path(blob["storage_path"]).resolve()
            if not path.is_relative_to((output / "data" / "blobs").resolve()):
                raise ValueError("Audit source blob path escaped the private data directory")
            blobs.append(
                {
                    "id": blob["id"],
                    "filename": blob["filename"],
                    "before_hash": hashlib.sha256(fixture["content"].encode()).hexdigest(),
                    "after_hash": blob["content_hash"],
                    "physical_after_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "status": blob["status"],
                    "source_bound": blob["id"] in bound,
                }
            )
    finally:
        db.close()
    evidence["input_blobs"] = blobs
    evidence["source_bindings_by_turn"] = [
        [source["options"]["blob_ref"] for source in json.loads(path.read_text())["sources"].values() if "blob_ref" in source["options"]]
        for path in sorted(out.glob("turn-*.state.json"))
        if json.loads(path.read_text()) is not None
    ]
    save(out / "runtime-evidence.json", evidence)
    return evidence


def run_case(
    api: ApiClient, scenario: dict[str, Any], output: Path, accepted: set[str], fixture_path: Path | None = None
) -> dict[str, Any]:
    out = output / "cases" / scenario["id"]
    out.mkdir(parents=True, exist_ok=True)
    scenario_path = out / "scenario.json"
    if scenario_path.exists() and json.loads(scenario_path.read_text()) != scenario:
        raise ValueError("Scenario changed since this case started; use a fresh output directory")
    save(scenario_path, scenario)
    assert_frozen(output)
    checkpoint_path = out / "checkpoint.json"
    if checkpoint_path.exists():
        checkpoint: dict[str, Any] = json.loads(checkpoint_path.read_text())
        if checkpoint["status"] in {"passed", "failed", "runtime_failed", "harness_error", "budget_stopped"}:
            return checkpoint
    else:
        session = api.json("/api/sessions", {"title": "acceptance/" + scenario["id"]})
        save(out / "session.json", session)
        checkpoint = {
            "session_id": session["id"],
            "next_turn": 0,
            "user_turns": 0,
            "repair_turns": 0,
            "preview_requested": False,
            "status": "created",
        }
        prefix = "/api/sessions/" + session["id"]
        for index, blob in enumerate(scenario["inputs"]):
            uploaded = api.json(
                prefix + "/blobs/inline",
                {
                    "filename": blob["filename"],
                    "mime_type": blob["media_type"],
                    "content": blob["content"],
                },
            )
            save(out / f"input-{index}.json", uploaded)
        save(checkpoint_path, checkpoint)
    prefix = "/api/sessions/" + checkpoint["session_id"]

    def publish(status: str, **extra: Any) -> dict[str, Any]:
        if status == "passed":
            extra["source_sha256"] = assert_frozen(output)
            if fixture_path is not None and json.loads(fixture_path.read_text()) != scenario:
                raise ValueError("Scenario fixture changed during acceptance")
        checkpoint.update(status=status, **extra)
        save(checkpoint_path, checkpoint)
        capture_session_audit(output, checkpoint["session_id"], out)
        return checkpoint

    def send(prompt: str) -> None:
        if checkpoint["user_turns"] >= scenario["max_user_turns"]:
            raise ValueError("Scenario user-turn budget exhausted")
        checkpoint["user_turns"] += 1
        save(checkpoint_path, checkpoint)
        compose(api, prefix, prompt, out, checkpoint["user_turns"])
        save(out / f"turn-{checkpoint['user_turns']}.state.json", api.json(prefix + "/state"))

    try:
        execution_path = out / "execute.json"
        execution = json.loads(execution_path.read_text()) if execution_path.exists() else None
        if checkpoint["status"] == "executing":
            if execution is not None and execution["run_id"] != checkpoint["run_id"]:
                raise ValueError("Execution receipt differs from checkpoint run ID")
            execution = {"run_id": checkpoint["run_id"]}
        if checkpoint["status"] == "execute_requested" and execution is None:
            return publish(
                "harness_error",
                failures=["Execution request was interrupted before its receipt; inspect session run history before resuming"],
            )
        while execution is None:
            pending = _pending_reviews(api, prefix, out, scenario, accepted)
            if pending:
                return publish("needs_review", pending_event_ids=[e["id"] for e in pending])
            if checkpoint["next_turn"] < len(scenario["turns"]):
                prompt = scenario["turns"][checkpoint["next_turn"]]
                checkpoint["next_turn"] += 1
                send(prompt)
                continue
            state = api.json(prefix + "/state")
            save(out / "state.json", state)
            if state is None:
                validation = {"is_valid": False, "errors": ["No pipeline state was authored."]}
            else:
                validation = api.json(prefix + "/validate", {})
            save(out / "validation.json", validation)
            if not validation["is_valid"]:
                if checkpoint["repair_turns"]:
                    return publish("failed", failures=["Strict validation failed after the one permitted repair turn"])
                checkpoint["repair_turns"] += 1
                send(
                    "Please repair the pipeline while preserving every requirement I gave you. The application reports these actual blockers: "
                    + json.dumps(validation["errors"])
                    + ". Use the normal tools, then validate and preview it."
                )
                continue
            messages = api.json(prefix + "/messages?include_raw_content=true")
            save(out / "messages.json", messages)
            versions = _state_versions(api, prefix)
            save(out / "state-versions.json", versions)
            preview_seen = _preview_seen(messages, state, versions)
            if not preview_seen and not checkpoint["preview_requested"]:
                checkpoint["preview_requested"] = True
                send("Call preview_pipeline to verify this finished pipeline. Do not change its structure or options.")
                continue
            if not preview_seen:
                return publish("failed", failures=["No successful preview of the final pipeline state"])
            publish("execute_requested")
            execution = api.json(prefix + "/execute", {})
            save(execution_path, execution)
        run_id = execution["run_id"]
        publish("executing", run_id=run_id)
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            run = api.json("/api/runs/" + run_id)
            save(out / "run.json", run)
            if run["status"] in {"completed", "completed_with_failures", "failed", "empty", "interrupted"}:
                break
            time.sleep(2)
        else:
            return publish("runtime_failed", failures=["Execution did not finish within 600 seconds"])
        manifest = api.json("/api/runs/" + run_id + "/outputs")
        save(out / "outputs.json", manifest)
        state = api.json(prefix + "/state")
        save(out / "state.json", state)
        evidence = runtime_evidence(output, out, scenario, state, run)
        outputs = {}
        for artifact in current_artifacts(manifest["artifacts"], evidence["sink_effects"]):
            filename = Path(artifact["path_or_uri"]).name
            if filename in outputs:
                raise ValueError("Duplicate artifact basenames cannot be matched to the scenario")
            content = api.raw("/api/runs/" + run_id + "/outputs/" + artifact["artifact_id"] + "/content")
            outputs[filename] = content
            (out / "artifacts").mkdir(exist_ok=True)
            (out / "artifacts" / filename).write_bytes(content)
        failures = check_case(scenario, state=state, run=run, outputs=outputs, evidence=evidence)
        return publish(
            "failed" if failures else "passed",
            failures=failures,
            runtime_call_count=len(evidence["runtime_calls"]),
            runtime_success_count=sum(call["status"] == "success" for call in evidence["runtime_calls"]),
            source_row_count=len(evidence["source_rows"]),
        )
    except ApiError as exc:
        save(out / "http-error.json", {"status": exc.status, "body": exc.body})
        return publish("harness_error", failures=[str(exc)])
    except Exception as exc:
        return publish("harness_error", failures=[type(exc).__name__ + ": " + str(exc)])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--scenarios", type=Path, default=Path(__file__).resolve().parents[2] / "tests/fixtures/composer_convergence")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:18473")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument(
        "--accept-reviewed",
        action="append",
        default=[],
        metavar="EVENT_UUID",
        help="Resolve only the exact already-inspected interpretation event",
    )
    args = parser.parse_args()
    fixture_paths = sorted(args.scenarios.glob("*.json"))
    scenarios = [json.loads(path.read_text()) for path in fixture_paths]
    paths_by_id = {scenario["id"]: path for scenario, path in zip(scenarios, fixture_paths, strict=True)}
    if len(paths_by_id) != len(scenarios):
        parser.error("Duplicate scenario IDs")
    if not scenarios:
        parser.error("No scenario fixtures found")
    if args.case:
        scenarios = [s for s in scenarios if s["id"] in args.case]
        if set(args.case) != {s["id"] for s in scenarios}:
            parser.error("Unknown scenario id")
    if not args.execute:
        print(json.dumps([{"id": s["id"], "title": s["title"], "planned_turns": len(s["turns"])} for s in scenarios], indent=2))
        return
    if args.output is None:
        parser.error("--execute requires --output matching the isolated service")
    output = args.output.resolve()
    config = json.loads((output / "service-config.json").read_text())
    assert_frozen(output)
    if config["base_url"] != args.base_url:
        parser.error("Base URL differs from the isolated service configuration")
    api = ApiClient(args.base_url)
    authenticate(api, output)
    results = json.loads((output / "results.json").read_text()) if (output / "results.json").exists() else {}
    for scenario in scenarios:
        result = run_case(api, scenario, output, set(args.accept_reviewed), paths_by_id[scenario["id"]])
        results[scenario["id"]] = result
        print(json.dumps({"case": scenario["id"], **result}), flush=True)
        save(output / "results.json", results)
    if any(r["status"] not in {"passed", "needs_review"} for r in results.values()):
        raise SystemExit(1)
    if any(r["status"] == "needs_review" for r in results.values()):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
