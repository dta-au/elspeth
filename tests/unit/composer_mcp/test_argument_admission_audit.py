"""TC-020 (#327) red-team follow-ups: the MCP argument admission that precedes audit capture.

``call_tool`` admits the caller's arguments before anything records them.
These cases pin the properties the red-team found unpinned on 67c2ec2d3:

- F2: admission precedes the canonicalization sentinel, whose
  ``_payload_keys`` would otherwise persist a credential-shaped key;
- F4: admission uses the working state, so a field_mapper patch on an
  existing mapping label is not falsely refused;
- F5: a guard defect other than a refusal is audited as a plugin crash,
  never as a success;
- F3: unregistered tool names reach audit and the SDK log only by digest;
- malformed argument keys cannot escape the audit envelope through its
  canonicalization fallback.

Synthetic values only; no real credential.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from mcp.server import Server
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult, TextContent

import elspeth.composer_mcp.server as server_module
from elspeth.composer_mcp.audit import events_sidecar_path, verify_events_sidecar_integrity
from elspeth.composer_mcp.server import create_server
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.web.credential_guard import CredentialMaterialRefused, require_no_credential_material_for_tool
from elspeth.web.dependencies import create_catalog_service

# Built from parts so the repository secret scanner does not read a literal.
SYNTHETIC_KEY_NAME = "AKIA" + "QWERTYUIOPASDFGH"


def _server(scratch: Path) -> Server:
    return create_server(create_catalog_service(), scratch, runtime_preflight=None, runtime_preflight_settings_hash=None)


async def _call(server: Server, name: str, arguments: dict[str, Any] | None) -> CallToolResult:
    result = await server.request_handlers[CallToolRequest](
        CallToolRequest(method="tools/call", params=CallToolRequestParams(name=name, arguments=arguments))
    )
    assert isinstance(result.root, CallToolResult)
    assert len(result.root.content) == 1
    assert isinstance(result.root.content[0], TextContent)
    return result.root


async def _active_session(server: Server) -> str:
    created = await _call(server, "new_session", {"name": "Active"})
    assert created.isError is False
    assert isinstance(created.content[0], TextContent)
    return str(json.loads(created.content[0].text)["data"]["session_id"])


def _rows(scratch: Path, sid: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in events_sidecar_path(scratch, sid).read_text().splitlines()]


@pytest.mark.asyncio
async def test_refused_key_with_uncanonicalizable_value_is_withheld_not_listed_in_the_sentinel(tmp_path: Path) -> None:
    """F2: a credential-shaped key must not reach audit through ``_payload_keys``.

    An integer above 2**53 fails canonicalization, whose sentinel records the
    sorted top-level keys. Admission runs first, so the refusal's value-free
    stand-in is recorded instead.
    """
    arguments = {SYNTHETIC_KEY_NAME: 2**60}
    with pytest.raises(CredentialMaterialRefused) as caught:
        require_no_credential_material_for_tool("list_sessions", arguments, surface="composer_mcp_tool_arguments")
    with pytest.raises(ValueError):
        canonical_json(arguments)
    withheld = {"_arguments_withheld": caught.value.to_payload()}
    server = _server(tmp_path)
    sid = await _active_session(server)

    refused = await _call(server, "list_sessions", arguments)

    assert refused.isError is True
    assert isinstance(refused.content[0], TextContent)
    assert SYNTHETIC_KEY_NAME not in refused.content[0].text
    row = _rows(tmp_path, sid)[-1]
    assert row["tool_name"] == "list_sessions"
    assert row["status"] == "plugin_crash" and row["error_class"] == "CredentialMaterialRefused"
    assert json.loads(row["arguments_canonical"]) == withheld
    assert row["arguments_hash"] == stable_hash(withheld)
    assert SYNTHETIC_KEY_NAME not in events_sidecar_path(tmp_path, sid).read_text()
    verify_events_sidecar_integrity(events_sidecar_path(tmp_path, sid))


@pytest.mark.asyncio
async def test_admission_uses_working_state_so_a_field_mapper_label_patch_is_not_refused(tmp_path: Path) -> None:
    """F4: the pre-audit admission must receive the working state.

    Without it, a patch to a field_mapper mapping whose existing label looks
    secret-like (``customer_key``) is refused as credential material.
    """
    server = _server(tmp_path)
    await _active_session(server)
    node = {
        "id": "rename_customer",
        "node_type": "transform",
        "plugin": "field_mapper",
        "input": "rows",
        "on_success": "output",
        "on_error": "discard",
        "options": {"mapping": {"customer_key": "customer_id"}, "schema": {"mode": "observed"}},
    }
    patch_arguments = {"node_id": "rename_customer", "patch": {"mapping": {"customer_key": "cust_id"}}}
    with pytest.raises(CredentialMaterialRefused):
        require_no_credential_material_for_tool("patch_node_options", patch_arguments, surface="stateless-control")

    upserted = await _call(server, "upsert_node", node)
    assert upserted.isError is False
    patched = await _call(server, "patch_node_options", patch_arguments)

    assert patched.isError is False
    assert isinstance(patched.content[0], TextContent)
    assert json.loads(patched.content[0].text)["success"] is True


@pytest.mark.asyncio
async def test_a_guard_defect_is_audited_as_a_plugin_crash_not_a_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F5: a non-refusal exception from admission must not be recorded as SUCCESS."""
    server = _server(tmp_path)
    sid = await _active_session(server)
    arguments = {"ordinary_label": "unadmitted ordinary input"}

    def defective_guard(*_args: object, **_kwargs: object) -> None:
        raise TypeError("injected guard defect")

    monkeypatch.setattr(server_module, "require_no_credential_material_for_tool", defective_guard)
    failed = await _call(server, "list_sessions", arguments)

    assert failed.isError is True
    row = _rows(tmp_path, sid)[-1]
    assert row["tool_name"] == "list_sessions"
    assert row["status"] == "plugin_crash"
    assert row["error_class"] == row["error_message"] == "TypeError"
    assert row["result_canonical"] is None and row["version_after"] is None
    unavailable = {"_arguments_unavailable": "evidence_not_completed"}
    assert row["arguments_canonical"] == canonical_json(unavailable)
    assert row["arguments_hash"] == stable_hash(unavailable) != stable_hash(arguments)
    verify_events_sidecar_integrity(events_sidecar_path(tmp_path, sid))


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True], ids=["buffered", "active"])
async def test_later_dispatch_fault_preserves_exact_admitted_argument_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, active: bool
) -> None:
    """A later owned fault retains the input that already passed credential admission."""
    server = _server(tmp_path)
    if active:
        sid = await _active_session(server)
        before = _rows(tmp_path, sid)
        saved_bytes = (tmp_path / f"{sid}.json").read_bytes()
    arguments = {"patch": {"name": "Admitted ordinary label"}}
    original_admission = server_module.require_no_credential_material_for_tool
    admitted = []
    failure = TypeError("injected later dispatch fault")

    def observe_admission(tool_name, values, **kwargs):
        original_admission(tool_name, values, **kwargs)
        admitted.append((tool_name, values))

    def defective_dispatch(*_args: object, **_kwargs: object) -> None:
        raise failure

    with monkeypatch.context() as controlled:
        controlled.setattr(server_module, "require_no_credential_material_for_tool", observe_admission)
        controlled.setattr(server_module, "_dispatch_tool", defective_dispatch)
        failed = await _call(server, "set_metadata", arguments)

    assert failed.isError is True and failed.content[0].text == str(failure)
    assert admitted == [("set_metadata", arguments)]
    if active:
        rows = _rows(tmp_path, sid)
        assert len(rows) == len(before) + 1
        row = rows[-1]
        assert (tmp_path / f"{sid}.json").read_bytes() == saved_bytes
    else:
        assert list(tmp_path.glob("*.events.jsonl")) == []
        sid = await _active_session(server)
        rows = _rows(tmp_path, sid)
        assert len(rows) == 2
        row, created = rows
        assert created["tool_name"] == "new_session" and created["status"] == "success"
    assert row["tool_name"] == "set_metadata" and row["status"] == "plugin_crash"
    assert row["error_class"] == row["error_message"] == "TypeError"
    assert row["arguments_canonical"] == canonical_json(arguments)
    assert row["arguments_hash"] == stable_hash(arguments)
    assert row["arguments_hash"] == hashlib.sha256(row["arguments_canonical"].encode("utf-8")).hexdigest()
    assert row["result_canonical"] is None and row["result_hash"] is None and row["version_after"] is None
    followup = await _call(server, "list_sessions", {})
    assert followup.isError is False and json.loads(followup.content[0].text)["success"] is True
    verify_events_sidecar_integrity(events_sidecar_path(tmp_path, sid))


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True], ids=["buffered", "active"])
async def test_unexpected_credential_admission_fault_withholds_unadmitted_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, active: bool
) -> None:
    """Unadmitted credential input stays private even when the admission owner fails."""
    original_call_tool = Server.call_tool
    registered_handlers = []

    def capture_registration(server, *, validate_input=True):
        register = original_call_tool(server, validate_input=validate_input)

        def capture(handler):
            registered_handlers.append(handler)
            return register(handler)

        return capture

    # Observe the actual registered callable so the original exception can be
    # checked before the SDK turns it into a text error. Registration itself
    # and the healthy setup/follow-up calls retain the real SDK wrapper.
    with monkeypatch.context() as construction:
        construction.setattr(Server, "call_tool", capture_registration)
        server = _server(tmp_path)
    assert len(registered_handlers) == 1
    if active:
        sid = await _active_session(server)
        before = _rows(tmp_path, sid)
        saved_bytes = (tmp_path / f"{sid}.json").read_bytes()
    arguments = {"ordinary_label": SYNTHETIC_KEY_NAME}
    with pytest.raises(CredentialMaterialRefused):
        require_no_credential_material_for_tool("list_sessions", arguments, surface="recognized-input-control")
    assert canonical_json(arguments) and stable_hash(arguments)
    failure = RuntimeError("injected credential admission fault")
    admission_calls = []
    dispatched = []

    def defective_admission(tool_name, values, **_kwargs):
        admission_calls.append((tool_name, values))
        raise failure

    def forbidden_dispatch(*_args: object, **_kwargs: object) -> None:
        dispatched.append(True)
        raise AssertionError("unadmitted credential input reached dispatch")

    with monkeypatch.context() as controlled:
        controlled.setattr(server_module, "require_no_credential_material_for_tool", defective_admission)
        controlled.setattr(server_module, "_dispatch_tool", forbidden_dispatch)
        with pytest.raises(RuntimeError) as caught:
            await registered_handlers[0]("list_sessions", arguments)

    assert caught.value is failure
    assert admission_calls == [("list_sessions", arguments)] and dispatched == []
    if active:
        rows = _rows(tmp_path, sid)
        assert len(rows) == len(before) + 1
        row = rows[-1]
        assert (tmp_path / f"{sid}.json").read_bytes() == saved_bytes
    else:
        assert list(tmp_path.glob("*.events.jsonl")) == []
        sid = await _active_session(server)
        rows = _rows(tmp_path, sid)
        assert len(rows) == 2
        row, created = rows
        assert created["tool_name"] == "new_session" and created["status"] == "success"
    assert row["tool_name"] == "list_sessions" and row["status"] == "plugin_crash"
    assert row["error_class"] == row["error_message"] == "RuntimeError"
    unavailable = {"_arguments_unavailable": "evidence_not_completed"}
    assert row["arguments_canonical"] == canonical_json(unavailable)
    assert row["arguments_hash"] == stable_hash(unavailable)
    assert row["arguments_hash"] == hashlib.sha256(row["arguments_canonical"].encode("utf-8")).hexdigest()
    assert row["arguments_hash"] != stable_hash(arguments)
    assert row["result_canonical"] is None and row["result_hash"] is None and row["version_after"] is None
    assert SYNTHETIC_KEY_NAME not in events_sidecar_path(tmp_path, sid).read_text()
    followup = await _call(server, "list_sessions", {})
    assert followup.isError is False and json.loads(followup.content[0].text)["success"] is True
    verify_events_sidecar_integrity(events_sidecar_path(tmp_path, sid))


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True], ids=["buffered", "active"])
@pytest.mark.parametrize("fault_stage", ["canonicalization", "hashing"])
async def test_refusal_evidence_fault_is_audited_before_propagation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, active: bool, fault_stage: str
) -> None:
    """An owned refusal-evidence fault retains its identity and a safe crash audit."""
    original_call_tool = Server.call_tool
    registered_handlers = []

    def capture_registration(server, *, validate_input=True):
        register = original_call_tool(server, validate_input=validate_input)

        def capture(handler):
            registered_handlers.append(handler)
            return register(handler)

        return capture

    with monkeypatch.context() as construction:
        construction.setattr(Server, "call_tool", capture_registration)
        server = _server(tmp_path)
    assert len(registered_handlers) == 1
    if active:
        sid = await _active_session(server)
        before = _rows(tmp_path, sid)
        saved_bytes = (tmp_path / f"{sid}.json").read_bytes()
    arguments = {"ordinary_label": SYNTHETIC_KEY_NAME}
    with pytest.raises(CredentialMaterialRefused) as control_refusal:
        require_no_credential_material_for_tool("list_sessions", arguments, surface="composer_mcp_tool_arguments")
    expected_withheld = {"_arguments_withheld": control_refusal.value.to_payload()}
    assert canonical_json(arguments) and stable_hash(arguments)
    original_canonical_json = server_module.canonical_json
    original_stable_hash = server_module.stable_hash
    failure = RuntimeError("injected refusal evidence defect")
    faults = []
    dispatched = []

    def defective_canonical_json(value: Any) -> str:
        if type(value) is dict and set(value) == {"_arguments_withheld"}:
            assert value == expected_withheld
            faults.append("canonicalization")
            raise failure
        return original_canonical_json(value)

    def defective_stable_hash(value: Any) -> str:
        if type(value) is dict and set(value) == {"_arguments_withheld"}:
            assert value == expected_withheld
            faults.append("hashing")
            raise failure
        return original_stable_hash(value)

    def forbidden_dispatch(*_args: object, **_kwargs: object) -> None:
        dispatched.append(True)
        raise AssertionError("refused credential input reached dispatch")

    with monkeypatch.context() as controlled:
        if fault_stage == "canonicalization":
            controlled.setattr(server_module, "canonical_json", defective_canonical_json)
        else:
            controlled.setattr(server_module, "stable_hash", defective_stable_hash)
        controlled.setattr(server_module, "_dispatch_tool", forbidden_dispatch)
        with pytest.raises(RuntimeError) as caught:
            await registered_handlers[0]("list_sessions", arguments)

    assert caught.value is failure and faults == [fault_stage] and dispatched == []
    refusal_context = caught.value.__context__
    assert type(refusal_context) is CredentialMaterialRefused
    assert refusal_context is not control_refusal.value
    assert refusal_context.to_payload() == control_refusal.value.to_payload()
    if active:
        rows = _rows(tmp_path, sid)
        assert len(rows) == len(before) + 1
        row = rows[-1]
        assert (tmp_path / f"{sid}.json").read_bytes() == saved_bytes
    else:
        assert list(tmp_path.glob("*.events.jsonl")) == []
        sid = await _active_session(server)
        rows = _rows(tmp_path, sid)
        assert len(rows) == 2
        row, created = rows
        assert created["tool_name"] == "new_session" and created["status"] == "success"
    assert row["tool_name"] == "list_sessions" and row["status"] == "plugin_crash"
    assert row["error_class"] == row["error_message"] == "RuntimeError"
    unavailable = {"_arguments_unavailable": "evidence_not_completed"}
    assert row["arguments_canonical"] == canonical_json(unavailable)
    assert row["arguments_hash"] == stable_hash(unavailable)
    assert row["arguments_hash"] == hashlib.sha256(row["arguments_canonical"].encode("utf-8")).hexdigest()
    assert row["arguments_hash"] not in {stable_hash(arguments), stable_hash(expected_withheld)}
    assert row["result_canonical"] is None and row["result_hash"] is None and row["version_after"] is None
    sidecar = events_sidecar_path(tmp_path, sid).read_text()
    assert SYNTHETIC_KEY_NAME not in sidecar and "injected refusal evidence defect" not in sidecar
    followup = await _call(server, "list_sessions", {})
    assert followup.isError is False and json.loads(followup.content[0].text)["success"] is True
    verify_events_sidecar_integrity(events_sidecar_path(tmp_path, sid))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_name",
    [
        pytest.param(SYNTHETIC_KEY_NAME, id="credential-shaped"),
        pytest.param("\ud800unknown", id="lone-surrogate"),
        pytest.param("unknown\nINJECTED log line", id="newline"),
    ],
)
async def test_an_unregistered_tool_name_is_audited_by_digest_and_never_echoed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, raw_name: str
) -> None:
    """The real SDK wrapper must preserve safe records without echoing caller names."""
    caplog.set_level(logging.DEBUG, logger="mcp.server.lowlevel.server")
    server = _server(tmp_path)
    sid = await _active_session(server)
    before = _rows(tmp_path, sid)
    caplog.clear()

    unknown = await _call(server, raw_name, {})
    registered = await _call(server, "list_sessions", {})

    assert isinstance(unknown.content[0], TextContent)
    assert json.loads(unknown.content[0].text)["success"] is False
    assert raw_name not in unknown.content[0].text
    escaped_name = json.dumps(raw_name)[1:-1]
    assert escaped_name not in unknown.content[0].text
    assert registered.isError is False
    rows = _rows(tmp_path, sid)
    assert len(rows) == len(before) + 2
    unknown_row, registered_row = rows[-2:]
    digest = hashlib.sha256(raw_name.encode("utf-8", "surrogatepass")).hexdigest()
    audited_name = f"<unregistered>:sha256:{digest}"
    assert unknown_row["tool_name"] == audited_name
    assert registered_row["tool_name"] == "list_sessions"
    assert registered_row["arguments_canonical"] == canonical_json({})
    assert registered_row["arguments_hash"] == stable_hash({})
    sidecar = events_sidecar_path(tmp_path, sid).read_text()
    assert raw_name not in sidecar and escaped_name not in sidecar
    verify_events_sidecar_integrity(events_sidecar_path(tmp_path, sid))
    logged = [record.getMessage() for record in caplog.records]
    assert logged.count(f"Tool cache miss for {audited_name}, refreshing cache") == 1
    assert logged.count(f"Tool '{audited_name}' not listed, no validation will be performed") == 1
    assert not any(raw_name in message or escaped_name in message for message in logged)
    for record in caplog.records:
        assert raw_name not in str(record.msg) and escaped_name not in str(record.msg)
        assert escaped_name not in json.dumps(record.args)


def test_sdk_tool_name_log_filter_preserves_registered_and_unrelated_records(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="mcp.server.lowlevel.server")
    sdk_logger = logging.getLogger("mcp.server.lowlevel.server")
    _server(tmp_path)
    installed_filters = tuple(sdk_logger.filters)
    _server(tmp_path)
    assert tuple(sdk_logger.filters) == installed_filters
    caplog.clear()

    sdk_logger.debug("Tool cache miss for %s, refreshing cache", "list_sessions")
    sdk_logger.warning("Tool '%s' not listed, no validation will be performed", "list_sessions")
    sdk_logger.warning("Ordinary SDK diagnostic: %s", "unchanged")

    assert [record.getMessage() for record in caplog.records] == [
        "Tool cache miss for list_sessions, refreshing cache",
        "Tool 'list_sessions' not listed, no validation will be performed",
        "Ordinary SDK diagnostic: unchanged",
    ]
    assert caplog.records[-1].args == ("unchanged",)


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True], ids=["buffered", "active"])
@pytest.mark.parametrize("recognized", [False, True], ids=["benign", "credential"])
@pytest.mark.parametrize("fault_stage", ["canonicalization", "hashing"])
async def test_canonicalizer_defect_is_audited_before_transport_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, active: bool, recognized: bool, fault_stage: str
) -> None:
    """A partial evidence pair cannot bypass credential admission or corrupt audit."""
    server = _server(tmp_path)
    if active:
        sid = await _active_session(server)
        before = _rows(tmp_path, sid)
        saved_bytes = (tmp_path / f"{sid}.json").read_bytes()
    arguments = {"ordinary_label": SYNTHETIC_KEY_NAME if recognized else "ordinary customer value"}
    if recognized:
        with pytest.raises(CredentialMaterialRefused) as refusal:
            require_no_credential_material_for_tool("list_sessions", arguments, surface="composer_mcp_tool_arguments")
        withheld = {"_arguments_withheld": refusal.value.to_payload()}
    else:
        require_no_credential_material_for_tool("list_sessions", arguments, surface="composer_mcp_tool_arguments")
    assert canonical_json(arguments) and stable_hash(arguments)
    original_canonical_json = server_module.canonical_json
    original_stable_hash = server_module.stable_hash
    faults = []
    dispatched = []

    def fail_evidence() -> None:
        faults.append(True)
        raise RuntimeError("injected argument evidence defect")

    def defective_canonical_json(value: Any) -> str:
        if type(value) is dict and value == arguments:
            fail_evidence()
        return original_canonical_json(value)

    def defective_stable_hash(value: Any) -> str:
        if type(value) is dict and value == arguments:
            fail_evidence()
        return original_stable_hash(value)

    def forbidden_dispatch(*_args: object, **_kwargs: object) -> None:
        dispatched.append(True)
        raise AssertionError("argument evidence defect reached dispatch")

    with monkeypatch.context() as controlled:
        if fault_stage == "canonicalization":
            controlled.setattr(server_module, "canonical_json", defective_canonical_json)
        else:
            controlled.setattr(server_module, "stable_hash", defective_stable_hash)
        controlled.setattr(server_module, "_dispatch_tool", forbidden_dispatch)
        failed = await _call(server, "list_sessions", arguments)

    assert failed.isError is True and faults == [True] and dispatched == []
    if active:
        rows = _rows(tmp_path, sid)
        assert len(rows) == len(before) + 1
        row = rows[-1]
        assert (tmp_path / f"{sid}.json").read_bytes() == saved_bytes
    else:
        assert list(tmp_path.glob("*.events.jsonl")) == []
        sid = await _active_session(server)
        rows = _rows(tmp_path, sid)
        assert len(rows) == 2
        row, created = rows
        assert created["tool_name"] == "new_session" and created["status"] == "success"
    assert row["tool_name"] == "list_sessions" and row["status"] == "plugin_crash"
    if recognized:
        assert row["error_class"] == row["error_message"] == "CredentialMaterialRefused"
        assert row["arguments_canonical"] == canonical_json(withheld)
        assert row["arguments_hash"] == stable_hash(withheld)
    else:
        assert row["error_class"] == row["error_message"] == "RuntimeError"
        unavailable = {"_arguments_unavailable": "evidence_not_completed"}
        assert row["arguments_canonical"] == canonical_json(unavailable)
        assert row["arguments_hash"] == stable_hash(unavailable)
    assert row["arguments_hash"] == hashlib.sha256(row["arguments_canonical"].encode("utf-8")).hexdigest()
    assert row["arguments_hash"] != stable_hash(arguments)
    assert row["result_canonical"] is None and row["result_hash"] is None and row["version_after"] is None
    assert SYNTHETIC_KEY_NAME not in failed.content[0].text
    assert SYNTHETIC_KEY_NAME not in events_sidecar_path(tmp_path, sid).read_text()
    followup = await _call(server, "list_sessions", {})
    assert followup.isError is False
    verify_events_sidecar_integrity(events_sidecar_path(tmp_path, sid))


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True], ids=["buffered", "active"])
@pytest.mark.parametrize("recognized", [False, True], ids=["benign-private-key", "credential-key"])
async def test_uncanonicalizable_argument_key_records_exactly_one_safe_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, active: bool, recognized: bool
) -> None:
    raw_key = (SYNTHETIC_KEY_NAME if recognized else "private-customer-column") + "\ud800"
    arguments = {raw_key: "ordinary value"}
    if recognized:
        with pytest.raises(CredentialMaterialRefused) as refusal:
            require_no_credential_material_for_tool("list_sessions", arguments, surface="composer_mcp_tool_arguments")
        withheld = {"_arguments_withheld": refusal.value.to_payload()}
    else:
        require_no_credential_material_for_tool("list_sessions", arguments, surface="composer_mcp_tool_arguments")
    with pytest.raises(ValueError) as canonical_failure:
        canonical_json(arguments)

    server = _server(tmp_path)
    if active:
        sid = await _active_session(server)
        edited = await _call(server, "set_metadata", {"patch": {"name": "Unsaved working name"}})
        before_state = json.loads(edited.content[0].text)["state"]
        before_diff = await _call(server, "diff_pipeline", {})
        before = _rows(tmp_path, sid)
        saved_bytes = (tmp_path / f"{sid}.json").read_bytes()

    dispatched = []

    def forbidden_dispatch(*_args: object, **_kwargs: object) -> None:
        dispatched.append(True)
        raise AssertionError("uncanonicalizable arguments reached dispatch")

    with monkeypatch.context() as controlled:
        controlled.setattr(server_module, "_dispatch_tool", forbidden_dispatch)
        failed = await _call(server, "list_sessions", arguments)

    assert dispatched == []
    assert failed.isError is True
    if active:
        rows = _rows(tmp_path, sid)
        assert len(rows) == len(before) + 1
        row = rows[-1]
        assert (tmp_path / f"{sid}.json").read_bytes() == saved_bytes
        after_state = await _call(server, "get_pipeline_state", {})
        assert json.loads(after_state.content[0].text)["state"] == before_state
        assert await _call(server, "diff_pipeline", {}) == before_diff
        assert json.loads((await _call(server, "save_session", {"session_id": sid})).content[0].text)["success"] is True
    else:
        assert list(tmp_path.glob("*.events.jsonl")) == []
        sid = await _active_session(server)
        rows = _rows(tmp_path, sid)
        assert len(rows) == 2
        row, created = rows
        assert created["tool_name"] == "new_session" and created["status"] == "success"

    assert row["tool_name"] == "list_sessions" and row["version_after"] is None
    recorded_arguments = json.loads(row["arguments_canonical"])
    assert row["arguments_hash"] == stable_hash(recorded_arguments)
    if recognized:
        assert row["status"] == "plugin_crash"
        assert row["error_class"] == row["error_message"] == "CredentialMaterialRefused"
        assert recorded_arguments == withheld
        assert row["result_canonical"] is None and row["result_hash"] is None
    else:
        assert row["status"] == "arg_error" and row["error_category"] == "canonicalization"
        assert row["error_class"] == row["error_message"] == type(canonical_failure.value).__name__
        assert recorded_arguments["_canonicalization_error"] == type(canonical_failure.value).__name__
        assert "_payload_keys" not in recorded_arguments
        assert json.loads(row["result_canonical"]) == {"error": failed.content[0].text, "isError": True}
        assert row["result_hash"] == stable_hash(json.loads(row["result_canonical"]))
    followup = await _call(server, "list_sessions", {})
    assert followup.isError is False and json.loads(followup.content[0].text)["success"] is True
    escaped_key = json.dumps(raw_key)[1:-1]
    sidecar = events_sidecar_path(tmp_path, sid).read_text()
    assert raw_key not in failed.content[0].text and escaped_key not in failed.content[0].text
    assert raw_key not in sidecar and escaped_key not in sidecar
    verify_events_sidecar_integrity(events_sidecar_path(tmp_path, sid))
