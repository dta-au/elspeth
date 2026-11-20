"""Safety properties of scripts/staging/repair_copied_prompt_reviews.py, against an in-memory API."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from scripts.staging import repair_copied_prompt_reviews as repair
from scripts.staging.opslib import AccountAuth, CheckpointStore, OperationFailed, UsageError, WebClient, run_tool

from tests.unit.scripts.staging.fake_deployment import BASE_URL, Blob, FakeDeployment, Session

SOURCE = "11111111-1111-4111-8111-111111111111"
TARGET = "22222222-2222-4222-8222-222222222222"
SYSTEM = b"You rate reviews. Reply with a number.\n"
USER = b"Rate this review: {{ row['review'] }}\n"


def graph(system_blob: str, user_blob: str) -> dict[str, Any]:
    return {
        "id": "state-1",
        "is_valid": True,
        "sources": {"reviews": {"plugin": "csv", "on_success": "rows", "options": {"path": "/data/x.csv"}}},
        "nodes": [
            {
                "id": "rate",
                "plugin": "llm",
                "node_type": "transform",
                "input": "rows",
                "on_success": "out",
                "options": {"system_prompt": {"blob_ref": system_blob}, "prompt_template": {"blob_ref": user_blob}, "model": "m"},
            }
        ],
        "edges": [],
        "outputs": [{"name": "main", "options": {}}],
        "metadata": {"name": "copy"},
    }


def binding(field_path: str, blob_id: str, data: bytes) -> dict[str, Any]:
    return {
        "field_path": field_path,
        "blob_id": blob_id,
        "content_sha256": hashlib.sha256(data).hexdigest(),
        "encoding": "utf-8",
        "filename": field_path.rsplit(".", 1)[-1] + ".txt",
        "text": data.decode(),
    }


def deployment(*, kind: str = "llm_prompt_template", target_system: bytes = SYSTEM, text: bytes = USER) -> FakeDeployment:
    source = Session(owner="source-user", state=graph("s-sys", "s-user"))
    source.blobs = {"s-sys": Blob("s-sys", "system.txt", SYSTEM), "s-user": Blob("s-user", "user.txt", USER)}
    target = Session(owner="target-user", state=graph("t-sys", "t-user"))
    target.blobs = {"t-sys": Blob("t-sys", "system.txt", target_system), "t-user": Blob("t-user", "user.txt", USER)}
    target.messages = [{"role": "user", "operation_id": None, "tool_calls": None, "content": "hello"}]
    surface = {"prompt_template": {"blob_ref": "t-user"}, "system_prompt": {"blob_ref": "t-sys"}}
    snapshot = {
        "node_id": "rate",
        "authored_surface": surface,
        "anchor": "a" * 64,
        "bindings": [
            binding("node:rate.options.system_prompt", "t-sys", target_system),
            {**binding("node:rate.options.prompt_template", "t-user", USER), "text": text.decode()},
        ],
    }
    target.reviews = {"event-1": {"event": {"id": "event-1", "kind": kind}, "snapshot": snapshot, "pending": True}}
    return FakeDeployment(
        passwords={"source-user": "source-pass", "target-user": "target-pass"},
        sessions={SOURCE: source, TARGET: target},
    )


def run(
    fake: FakeDeployment,
    tmp_path: Path,
    *,
    answer: str = "approve",
    shown: list[repair.ReviewEvidence] | None = None,
) -> dict[str, Any]:
    secrets = {"Password for source-user (hidden input): ": "source-pass", "Password for target-user (hidden input): ": "target-pass"}
    order: list[str] = []

    def present(evidence: repair.ReviewEvidence) -> None:
        order.append("shown")
        if shown is not None:
            shown.append(evidence)

    def confirm(question: str) -> str:
        assert order[-1:] == ["shown"], "the operator must see the text before being asked"
        return answer

    return repair.repair(
        target=WebClient(BASE_URL, transport=fake),
        source=WebClient(BASE_URL, transport=fake),
        target_auth=AccountAuth("target-user"),
        source_auth=AccountAuth("source-user"),
        session_id=TARGET,
        source_session_id=SOURCE,
        store=CheckpointStore(tmp_path / "state", repo_root=tmp_path / "repo"),
        present=present,
        confirm=confirm,
        prompt_secret=lambda prompt: secrets[prompt],
    )


def resolves(fake: FakeDeployment) -> list[object]:
    return fake.calls("POST", "/resolve")


def test_review_resolved_through_the_api_after_bytes_match_and_operator_approves(tmp_path: Path) -> None:
    fake = deployment()
    shown: list[repair.ReviewEvidence] = []
    result = run(fake, tmp_path, shown=shown)

    assert resolves(fake) == [{"choice": "accepted_as_drafted", "reviewed_anchor": "a" * 64}]
    assert {item["text"] for item in shown[0].files} == {SYSTEM.decode(), USER.decode()}
    assert result["complete"] is True and result["validation"]["is_valid"] is True
    writes = {path for verb, path, _ in fake.requests if verb == "POST"}
    assert writes == {
        "/api/auth/login",
        "/api/auth/logout",
        f"/api/sessions/{TARGET}/interpretations/event-1/resolve",
        f"/api/sessions/{TARGET}/validate",
    }


def test_target_file_bytes_differing_from_the_source_refuses(tmp_path: Path) -> None:
    fake = deployment(target_system=b"Edited in the target session.\n")
    with pytest.raises(OperationFailed, match="not in the source session"):
        run(fake, tmp_path)
    assert resolves(fake) == []


def test_downloaded_bytes_differing_from_the_binding_refuses(tmp_path: Path) -> None:
    fake = deployment()
    fake.sessions[TARGET].blobs["t-user"] = Blob("t-user", "user.txt", b"tampered\n")
    with pytest.raises(OperationFailed, match="differs from the source file"):
        run(fake, tmp_path)
    assert resolves(fake) == []


def test_snapshot_text_differing_from_the_file_refuses(tmp_path: Path) -> None:
    fake = deployment(text=b"Unseen different text\n")
    with pytest.raises(OperationFailed, match="text that differs"):
        run(fake, tmp_path)
    assert resolves(fake) == []


def test_review_surface_differing_from_the_node_refuses(tmp_path: Path) -> None:
    fake = deployment()
    fake.sessions[TARGET].state["nodes"][0]["options"]["queries"] = ["extra"]
    with pytest.raises(OperationFailed, match="current prompt options"):
        run(fake, tmp_path)
    assert resolves(fake) == []


def test_non_prompt_review_needs_separate_human_inspection(tmp_path: Path) -> None:
    fake = deployment(kind="field_interpretation")
    with pytest.raises(OperationFailed, match="separate human inspection"):
        run(fake, tmp_path)
    assert resolves(fake) == []


def test_operator_declining_resolves_nothing(tmp_path: Path) -> None:
    fake = deployment()
    with pytest.raises(OperationFailed, match="did not approve"):
        run(fake, tmp_path, answer="yes")
    assert resolves(fake) == []


def test_history_change_is_detected(tmp_path: Path) -> None:
    fake = deployment()

    def after(method: str, path: str) -> None:
        if path.endswith("/resolve"):
            fake.sessions[TARGET].messages.append({"role": "assistant", "operation_id": None, "tool_calls": None, "content": "x"})

    fake.on_request = after
    with pytest.raises(OperationFailed, match="chat history changed"):
        run(fake, tmp_path)


def test_complete_checkpoint_is_not_redone_and_malformed_one_refuses(tmp_path: Path) -> None:
    fake = deployment()
    run(fake, tmp_path)
    before = len(fake.requests)
    assert run(fake, tmp_path)["complete"] is True
    assert len(fake.requests) == before

    store = CheckpointStore(tmp_path / "state", repo_root=tmp_path / "repo")
    store.write(repair.checkpoint_name(TARGET), {"session_id": TARGET, "resolved": "not-a-list"})
    with pytest.raises(OperationFailed, match="not a complete, valid checkpoint"):
        run(fake, tmp_path)


def test_checkpoint_for_another_session_refuses(tmp_path: Path) -> None:
    fake = deployment()
    store = CheckpointStore(tmp_path / "state", repo_root=tmp_path / "repo")
    store.write(
        repair.checkpoint_name(TARGET),
        {"version": 1, "session_id": TARGET, "source_session_id": TARGET, "before": {}, "history_before": [], "resolved": []},
    )
    with pytest.raises(OperationFailed, match="not a complete, valid checkpoint"):
        run(fake, tmp_path)


def test_state_dir_inside_the_repository_is_refused(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="outside the repository"):
        CheckpointStore(tmp_path / "repo" / "receipts", repo_root=tmp_path / "repo")


def test_dry_run_contacts_nothing(tmp_path: Path) -> None:
    fake = deployment()
    argv = ["--base-url", BASE_URL, "--session", TARGET, "--source-session", SOURCE, "--account", "target-user"]
    argv += ["--source-account", "source-user", "--state-dir", str(tmp_path / "state")]
    assert repair.main(argv, client_factory=lambda url: WebClient(url, transport=fake)) == 0
    assert fake.requests == []


def test_missing_session_is_a_usage_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ELSPETH_OPS_SESSION", raising=False)
    argv = ["--base-url", BASE_URL, "--source-session", SOURCE, "--account", "a", "--source-account", "b", "--state-dir", str(tmp_path)]
    assert run_tool(repair.main, argv) == 2


def test_help_states_the_approval_authority_and_needs_no_environment() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "scripts.staging.repair_copied_prompt_reviews", "--help"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(repair.__file__).resolve().parents[2])},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "approval authority" in " ".join(result.stdout.split())
