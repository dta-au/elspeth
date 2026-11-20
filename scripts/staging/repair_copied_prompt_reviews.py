"""Re-establish the prompt-file reviews of a copied session through the normal review API.

When an operator copies a session (its graph and its files) into another
account, the copy's file-backed LLM prompts surface as pending prompt
reviews. This tool resolves them -- ``accepted_as_drafted`` with the reviewed
anchor, exactly the call the review card makes -- but only after it has proved,
for each review, that the text being approved is byte-for-byte the text of a
file in the SOURCE session, and after the operator has read that text and typed
``approve``.

THIS TOOL EXERCISES APPROVAL AUTHORITY on the target account. It is an
operator action for a copy the operator made, not a substitute for a reviewer:
never use it to clear reviews of text that was authored or edited in the
target session. It refuses any review that is not an ``llm_prompt_template``
review, any prompt whose file bytes do not match a source-session file, and any
review whose text the operator has not been shown and approved interactively.

Safety properties (named by tests in
``tests/unit/scripts/staging/test_repair_copied_prompt_reviews.py``):

* Approvals go only through ``POST .../interpretations/{id}/resolve``; nothing
  is written to a database.
* Before resolving a review it checks: the review is live; its authored surface
  equals the node's current prompt options; its bindings are exactly the file
  markers in that surface; each bound file downloads from the target session
  with the bytes of a ready, hash-verified file of the source session; and the
  snapshot text equals those bytes decoded. Any mismatch stops the run.
* The operator sees every bound file's text and must type ``approve`` for each
  review; anything else stops the run with nothing resolved for that review.
* Graph content and chat history must be unchanged afterwards.
* A run resumes only from a checkpoint that is complete and valid for the same
  session; a malformed checkpoint stops it.

Not carried from the delivered script: building the copy's YAML import body,
and an external display-restore step. Copy the session first by a supported
means; see ``docs/runbooks/staging-hardware-upgrade-recovery.md``.
"""

from __future__ import annotations

import argparse
import copy
import getpass
import hashlib
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.staging.opslib import (
    AccountAuth,
    CheckpointStore,
    JsonObject,
    OperationFailed,
    UsageError,
    WebClient,
    account_from,
    add_account_arguments,
    add_execute_flag,
    api_projection,
    blob_markers,
    differences,
    emit,
    list_pages,
    require_dict,
    require_list,
    require_uuid,
    run_tool,
    setting,
    sign_in,
)

PROMPT_KEYS = ("prompt_template", "system_prompt", "queries")
CHECKPOINT_KEYS = frozenset({"version", "session_id", "source_session_id", "before", "history_before", "resolved"})


def checkpoint_name(session_id: str) -> str:
    return f"review-repair-{session_id}"


def prompt_surface(options: Mapping[str, Any]) -> JsonObject:
    return {key: copy.deepcopy(options[key]) for key in PROMPT_KEYS if key in options}


def source_files(client: WebClient, session_id: str) -> dict[str, bytes]:
    """Every ready file of the source session by SHA-256, each verified against its recorded hash and size."""
    files: dict[str, bytes] = {}
    for raw in list_pages(client, f"/api/sessions/{session_id}/blobs"):
        blob = require_dict(raw, "blob")
        if blob["status"] != "ready" or blob["content_hash"] is None:
            continue
        data = client.download(f"/api/sessions/{session_id}/blobs/{blob['id']}/content")
        if hashlib.sha256(data).hexdigest() != blob["content_hash"] or len(data) != blob["size_bytes"]:
            raise OperationFailed(f"source file {blob['id']} does not match its recorded hash or size")
        files[blob["content_hash"]] = data
    return files


def all_messages(client: WebClient, session_id: str) -> list[Any]:
    return list_pages(client, f"/api/sessions/{session_id}/messages")


def load_checkpoint(store: CheckpointStore, session_id: str, source_session_id: str) -> JsonObject | None:
    """Return the saved checkpoint, refusing one that is malformed or belongs to another session."""
    saved = store.read(checkpoint_name(session_id))
    if saved is None:
        return None
    valid = (
        set(saved) >= CHECKPOINT_KEYS
        and saved["version"] == 1
        and saved["session_id"] == session_id
        and saved["source_session_id"] == source_session_id
        and type(saved["before"]) is dict
        and type(saved["history_before"]) is list
        and type(saved["resolved"]) is list
        and all(type(item) is str for item in saved["resolved"])
        and ("complete" not in saved or type(saved["complete"]) is bool)
    )
    if not valid:
        raise OperationFailed(f"checkpoint {checkpoint_name(session_id)} is not a complete, valid checkpoint for this session")
    return saved


def assert_parity(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> None:
    found = differences(api_projection(expected), api_projection(actual))
    if found:
        raise OperationFailed(f"graph content changed: {found[:20]}")


@dataclass(frozen=True, slots=True)
class ReviewEvidence:
    event_id: str
    node_id: str
    anchor: str
    files: tuple[JsonObject, ...]


def check_review(
    client: WebClient, session_id: str, event: Mapping[str, Any], nodes: Mapping[str, Any], files: Mapping[str, bytes]
) -> ReviewEvidence:
    """Prove that a pending prompt review shows exactly the copied source files' text."""
    if event["kind"] != "llm_prompt_template":
        raise OperationFailed(f"review {event['id']} is a {event['kind']} review; it needs separate human inspection")
    path = f"/api/sessions/{session_id}/interpretations/{event['id']}/prompt"
    prompt = require_dict(client.get_json(path), "prompt review snapshot")
    if prompt["is_live_review"] is not True:
        raise OperationFailed(f"review {event['id']} is not live")
    if prompt["node_id"] not in nodes:
        raise OperationFailed(f"review {event['id']} names a node that is not in the graph")
    surface = prompt_surface(nodes[prompt["node_id"]]["options"])
    if prompt["authored_surface"] != surface:
        raise OperationFailed(f"review {event['id']} does not show the node's current prompt options")
    markers = blob_markers(surface)
    bindings = require_list(prompt["bindings"], "bindings")
    if sorted(binding["blob_id"] for binding in bindings) != sorted(marker["blob_ref"] for marker in markers):
        raise OperationFailed(f"review {event['id']} bindings are not exactly the prompt's file markers")
    shown: list[JsonObject] = []
    for binding in bindings:
        sha256 = binding["content_sha256"]
        if sha256 not in files:
            raise OperationFailed(f"review {event['id']} binds a file that is not in the source session")
        data = files[sha256]
        downloaded = client.download(f"/api/sessions/{session_id}/blobs/{binding['blob_id']}/content")
        if downloaded != data or hashlib.sha256(downloaded).hexdigest() != sha256:
            raise OperationFailed(f"review {event['id']} file {binding['blob_id']} differs from the source file")
        if binding["text"] != data.decode(binding["encoding"]):
            raise OperationFailed(f"review {event['id']} shows text that differs from the file bytes")
        shown.append({"field_path": binding["field_path"], "filename": binding["filename"], "sha256": sha256, "text": binding["text"]})
    return ReviewEvidence(event_id=event["id"], node_id=prompt["node_id"], anchor=prompt["anchor"], files=tuple(shown))


def show_review(evidence: ReviewEvidence) -> None:
    """Print a review's full text to stderr for the operator; stdout stays a JSON event stream."""
    print(f"\n=== Prompt review {evidence.event_id} on node {evidence.node_id} ===", file=sys.stderr)
    for item in evidence.files:
        print(f"--- {item['field_path']}  file {item['filename']}  sha256 {item['sha256']}", file=sys.stderr)
        print(item["text"], file=sys.stderr)
    print("=== end of review ===", file=sys.stderr)


def ask_operator(question: str) -> str:
    if not sys.stdin.isatty():
        raise UsageError("approvals need an interactive terminal")
    return input(question)


def repair(
    *,
    target: WebClient,
    source: WebClient,
    target_auth: AccountAuth,
    source_auth: AccountAuth,
    session_id: str,
    source_session_id: str,
    store: CheckpointStore,
    present: Callable[[ReviewEvidence], None] = show_review,
    confirm: Callable[[str], str] = ask_operator,
    prompt_secret: Callable[[str], str] = getpass.getpass,
) -> JsonObject:
    saved = load_checkpoint(store, session_id, source_session_id)
    if saved is not None and "complete" in saved and saved["complete"]:
        emit("copy_reviews_already_repaired", session_id=session_id)
        return saved
    sign_in(source, source_auth, prompt_secret=prompt_secret)
    try:
        files = source_files(source, source_session_id)
    finally:
        source.close()
    sign_in(target, target_auth, prompt_secret=prompt_secret)
    try:
        if saved is None:
            before = require_dict(target.get_json(f"/api/sessions/{session_id}/state"), "state")
            negative = copy.deepcopy(before)
            negative["nodes"][0]["description"] = "parity negative control"
            if differences(api_projection(before), api_projection(copy.deepcopy(before))) or not differences(
                api_projection(before), api_projection(negative)
            ):
                raise OperationFailed("graph comparison failed its own positive or negative control")
            saved = {
                "version": 1,
                "session_id": session_id,
                "source_session_id": source_session_id,
                "before": before,
                "history_before": all_messages(target, session_id),
                "resolved": [],
            }
            store.write(checkpoint_name(session_id), saved)
        state = require_dict(target.get_json(f"/api/sessions/{session_id}/state"), "state")
        assert_parity(saved["before"], state)
        nodes = {node["id"]: node for node in state["nodes"]}
        pending = require_dict(target.get_json(f"/api/sessions/{session_id}/interpretations?status=pending"), "reviews")["events"]
        for event in pending:
            evidence = check_review(target, session_id, event, nodes, files)
            present(evidence)
            answer = confirm(f"Type approve to accept review {evidence.event_id} as drafted: ")
            if answer.strip() != "approve":
                raise OperationFailed(f"operator did not approve review {evidence.event_id}; nothing was resolved for it")
            target.post_json(
                f"/api/sessions/{session_id}/interpretations/{evidence.event_id}/resolve",
                {"choice": "accepted_as_drafted", "reviewed_anchor": evidence.anchor},
            )
            saved["resolved"].append(evidence.event_id)
            store.write(checkpoint_name(session_id), saved)
            emit("copy_prompt_review_reestablished", completed=len(saved["resolved"]), session_id=session_id)
        state = require_dict(target.get_json(f"/api/sessions/{session_id}/state"), "state")
        assert_parity(saved["before"], state)
        if all_messages(target, session_id) != saved["history_before"]:
            raise OperationFailed("chat history changed during the repair")
        remaining = require_dict(target.get_json(f"/api/sessions/{session_id}/interpretations?status=pending"), "reviews")["events"]
        if remaining:
            raise OperationFailed(f"{len(remaining)} reviews are still pending")
        validation = require_dict(target.post_json(f"/api/sessions/{session_id}/validate", {}), "validation")
        saved.update(complete=True, state=state, validation=validation)
        store.write(checkpoint_name(session_id), saved)
        emit(
            "copy_review_repair_complete",
            session_id=session_id,
            reviews=len(saved["resolved"]),
            graph_content_identical=True,
            history_preserved=True,
            runtime_valid=validation["is_valid"],
        )
        return saved
    finally:
        target.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.staging.repair_copied_prompt_reviews",
        description=(
            "Re-establish the pending prompt-file reviews of a COPIED session through the normal review API. "
            "This exercises approval authority on the target account: each review is resolved accepted_as_drafted "
            "only after its file bytes match a source-session file AND the operator has read the text and typed "
            "'approve'. Never use it for text authored or edited in the target session. Dry run unless --execute."
        ),
    )
    parser.add_argument("--base-url", help="deployment origin, https://... (env ELSPETH_OPS_BASE_URL)")
    parser.add_argument("--session", help="target (copied) session id (env ELSPETH_OPS_SESSION)")
    parser.add_argument("--source-session", help="session the copy was made from (env ELSPETH_OPS_SOURCE_SESSION)")
    add_account_arguments(parser, prefix="", role="target session owner")
    add_account_arguments(parser, prefix="source-", role="source session owner")
    parser.add_argument("--state-dir", help="private checkpoint directory outside the repository (env ELSPETH_OPS_STATE_DIR)")
    add_execute_flag(parser)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[str], WebClient] = WebClient,
    present: Callable[[ReviewEvidence], None] = show_review,
    confirm: Callable[[str], str] = ask_operator,
    prompt_secret: Callable[[str], str] = getpass.getpass,
) -> int:
    args = build_parser().parse_args(argv)
    base_url = setting(args.base_url, flag="--base-url", env="ELSPETH_OPS_BASE_URL")
    session_id = require_uuid(setting(args.session, flag="--session", env="ELSPETH_OPS_SESSION"), flag="--session")
    source_session_id = require_uuid(
        setting(args.source_session, flag="--source-session", env="ELSPETH_OPS_SOURCE_SESSION"), flag="--source-session"
    )
    target_auth = account_from(args, prefix="", env="ELSPETH_OPS_ACCOUNT")
    source_auth = account_from(args, prefix="source-", env="ELSPETH_OPS_SOURCE_ACCOUNT")
    store = CheckpointStore(Path(setting(args.state_dir, flag="--state-dir", env="ELSPETH_OPS_STATE_DIR")))
    if not args.execute:
        emit(
            "dry_run",
            base_url=base_url,
            session_id=session_id,
            source_session_id=source_session_id,
            steps=[
                f"read and hash-verify every file of the source session as {source_auth.username}",
                f"record the target graph and history as {target_auth.username}",
                "for each pending llm_prompt_template review: check surface, bindings and file bytes, show the text, ask for 'approve', resolve accepted_as_drafted",
                "check graph and history unchanged, no review pending, then run normal validation",
            ],
            checkpoint=str(store.root / (checkpoint_name(session_id) + ".json")),
            note="nothing was contacted; pass --execute to act",
        )
        return 0
    repair(
        target=client_factory(base_url),
        source=client_factory(base_url),
        target_auth=target_auth,
        source_auth=source_auth,
        session_id=session_id,
        source_session_id=source_session_id,
        store=store,
        present=present,
        confirm=confirm,
        prompt_secret=prompt_secret,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tool(main))
