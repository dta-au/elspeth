"""Bounded Power Automate source ingestion and original-row lineage."""

import json
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, create_autospec

import pytest

from elspeth.contracts import RunMode
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.plugin_context import PluginContext, ValidationErrorToken
from elspeth.contracts.sink_effect_http import SinkEffectHTTPPostResponse
from elspeth.core.landscape.plugin_audit_writer import PluginAuditWriterAdapter
from elspeth.plugins.infrastructure.clients.power_automate import PowerAutomateOperationClient


def options(**overrides: Any) -> dict[str, Any]:
    return {
        "auth": {"method": "managed_identity", "client_id": "approved-principal"},
        "trigger_url": "https://flows.example.org/read?api-version=1",
        "allowed_origin": "https://flows.example.org",
        "schema": {"mode": "observed"},
        "on_validation_failure": "quarantine",
        **overrides,
    }


def page(rows: list[object], cursor: str | None = None, snapshot: str = "snap") -> bytes:
    return json.dumps({"protocol": "elspeth.power-automate.v1", "snapshot_id": snapshot, "rows": rows, "next_cursor": cursor}).encode()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Mock:
    from elspeth.plugins.sources import power_automate

    fake = create_autospec(PowerAutomateOperationClient, instance=True, spec_set=True)
    monkeypatch.setattr(power_automate, "PowerAutomateOperationClient", create_autospec(PowerAutomateOperationClient, return_value=fake))
    return fake


def responses(client: Mock, *bodies: bytes) -> None:
    client.post_json.side_effect = [SinkEffectHTTPPostResponse(200, "application/json", b, "call", "a" * 64, "b" * 64) for b in bodies]


def context() -> PluginContext:
    ctx = PluginContext(
        config={},
        landscape=create_autospec(PluginAuditWriterAdapter, instance=True, spec_set=True),
        run_id="run",
        operation_id="operation",
        node_id="source",
        shutdown_event=threading.Event(),
        coordination_token=CoordinationToken(run_id="run", worker_id="source-test-worker", leader_epoch=1),
    )
    ctx.record_validation_error = create_autospec(
        ctx.record_validation_error,
        spec_set=True,
        return_value=ValidationErrorToken(row_id="invalid-row", node_id="source", destination="quarantine"),
    )
    return ctx


def source(**overrides: Any) -> Any:
    from elspeth.plugins.sources.power_automate import PowerAutomateSource

    return PowerAutomateSource(options(**overrides))


def test_one_page_locks_source_contract(client: Mock) -> None:
    responses(client, page([{"Record ID": "A", "count": 3}]))
    src = source()
    result = list(src.load(context()))
    assert result[0].row == {"record_id": "A", "count": 3}
    assert result[0].contract.locked
    assert result[0].source_row_index == 0
    assert src.get_field_resolution()[0]["Record ID"] == "record_id"
    client.close.assert_called_once()


def test_page_loop_preserves_order_and_snapshot(client: Mock) -> None:
    responses(client, page([{"id": 1}], "opaque"), page([{"id": 2}]))
    src = source(query={"dataset": "records"})
    assert [r.row["id"] for r in src.load(context())] == [1, 2]
    first, second = [c.args[0] for c in client.post_json.call_args_list]
    assert first == {
        "protocol": "elspeth.power-automate.v1",
        "operation": "read",
        "query": {"dataset": "records"},
        "snapshot_id": None,
        "cursor": None,
        "page_size": 100,
    }
    assert second == {**first, "snapshot_id": "snap", "cursor": "opaque"}


def test_empty_final_and_intermediate_pages(client: Mock) -> None:
    responses(client, page([], "next"), page([]))
    src = source()
    assert list(src.load(context())) == []
    assert client.post_json.call_count == 2
    assert src.require_schema_contract().locked


@pytest.mark.parametrize(
    "bodies,limits,code",
    [
        ([page([], "a"), page([], "b"), page([], "a")], {}, "cursor_cycle"),
        ([page([], "a")], {"max_pages": 1}, "page_limit"),
        ([page([{"id": 1}], "a")], {"max_rows": 1}, "row_limit"),
        ([page([{"id": 1}, {"id": 2}])], {"max_rows": 1}, "row_limit"),
        ([page([], snapshot="other")], {"snapshot_id": "expected"}, "snapshot_mismatch"),
    ],
)
def test_cycles_and_page_row_caps_abort_without_truncation(client: Mock, bodies: list[bytes], limits: dict[str, object], code: str) -> None:
    responses(client, *bodies)
    with pytest.raises(ValueError, match=code):
        list(source(**limits).load(context()))
    client.close.assert_called_once()


def test_all_candidate_rows_count_toward_limit(client: Mock) -> None:
    responses(client, page([None, 7], "next"))
    ctx = context()
    with pytest.raises(ValueError, match="row_limit"):
        list(source(max_rows=2, on_validation_failure="discard").load(ctx))
    assert ctx.record_validation_error.call_count == 0  # whole over-budget page refused before rows


def test_invalid_rows_have_exact_quarantine_lineage(client: Mock) -> None:
    candidates: list[object] = [None, [1, 2], 7, {"id": "bad"}, {"id": "42"}]
    responses(client, page(candidates))
    ctx = context()
    result = list(source(schema={"mode": "fixed", "fields": ["id: int"]}).load(ctx))
    assert [r.row for r in result[:-1]] == candidates[:-1]
    assert [r.source_row_index for r in result] == list(range(5))
    assert result[-1].row == {"id": 42}
    assert all(r.is_quarantined for r in result[:-1])
    assert ctx.record_validation_error.call_count == 4


def test_rejected_row_does_not_lock_observed_contract(client: Mock) -> None:
    responses(client, page([None, {"id": 1}, {"id": "wrong"}]))
    result = list(source().load(context()))
    assert [r.is_quarantined for r in result] == [True, False, True]
    assert result[1].contract.locked


def test_sparse_fields_and_mapping_collision_are_row_failures(client: Mock) -> None:
    responses(client, page([{"Name": "A"}, {"Name": "B", "New Value": 3}, {"name": "C"}]))
    src = source(field_mapping={"new_value": "amount"})
    result = list(src.load(context()))
    assert result[1].row == {"name": "B", "amount": 3}
    assert result[2].is_quarantined
    assert src.get_field_resolution()[0] == {"Name": "name", "New Value": "amount"}


def test_generator_close_and_cancel_stop_pages(client: Mock) -> None:
    responses(client, page([{"id": 1}, {"id": 2}], "next"))
    ctx = context()
    stream = source().load(ctx)
    assert next(stream).row == {"id": 1}
    stream.close()
    client.close.assert_called_once()
    assert client.post_json.call_count == 1


def test_shutdown_between_rows_aborts_and_closes(client: Mock) -> None:
    responses(client, page([{"id": 1}, {"id": 2}], "next"))
    ctx = context()
    stream = source().load(ctx)
    next(stream)
    ctx.shutdown_event.set()
    with pytest.raises(ValueError, match="source_cancelled"):
        next(stream)
    client.close.assert_called_once()


def test_malformed_envelope_fails_before_yield(client: Mock) -> None:
    responses(client, b'{"protocol":"elspeth.power-automate.v1","snapshot_id":"snap","rows":[{"id":1}],"next_cursor":null,"extra":1}')
    with pytest.raises(ValueError, match="invalid_envelope"):
        next(source().load(context()))
    client.close.assert_called_once()


def test_safe_live_initializer_preserves_authored_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "source-test-fingerprint-key")
    cfg = options(auth={"method": "sas_url", "trigger_url_secret": "https://flows.example.org/read?sig=private"})
    del cfg["trigger_url"]
    from elspeth.plugins.sources.power_automate import PowerAutomateSource

    src = PowerAutomateSource(cfg)
    assert "private" not in repr(src.config)
    assert "trigger_url_secret_fingerprint" in src.config["auth"]
    assert "page_size" not in src.config
    cfg["schema"]["mode"] = "fixed"
    assert src.config["schema"] == {"mode": "observed"}


def test_raw_secret_switch_cannot_bypass_required_hmac(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.contracts.security import SecretFingerprintError

    monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY", raising=False)
    monkeypatch.setenv("ELSPETH_ALLOW_RAW_SECRETS", "true")
    cfg = options(auth={"method": "sas_url", "trigger_url_secret": "https://flows.example.org/read?sig=private"})
    del cfg["trigger_url"]
    from elspeth.plugins.sources.power_automate import PowerAutomateSource

    with pytest.raises(SecretFingerprintError, match="ELSPETH_FINGERPRINT_KEY"):
        PowerAutomateSource(cfg)


@pytest.mark.parametrize("flag", ["true", "false", 0, 1, None])
@pytest.mark.parametrize("admission", ["config", "constructor"])
def test_snapshot_flag_rejects_non_boolean(flag: object, admission: str) -> None:
    from elspeth.plugins.infrastructure.config_base import PluginConfigError
    from elspeth.plugins.infrastructure.power_automate import PowerAutomateSourceConfig
    from elspeth.plugins.sources.power_automate import PowerAutomateSource

    cfg = options(snapshot_for_resume=flag)
    with pytest.raises(PluginConfigError, match="invalid_configuration"):
        if admission == "config":
            PowerAutomateSourceConfig.from_dict(cfg)
        else:
            PowerAutomateSource(cfg)


@pytest.mark.parametrize("flag", [True, False])
def test_snapshot_flag_preserves_exact_runtime_opt_in(flag: bool) -> None:
    src = source(snapshot_for_resume=flag)
    assert src.config["snapshot_for_resume"] is flag


def test_snapshot_flag_omission_keeps_authored_shape() -> None:
    assert "snapshot_for_resume" not in source().config


@pytest.mark.parametrize(
    "schema", [{"mode": "observed"}, {"mode": "flexible", "fields": ["id: int"]}, {"mode": "fixed", "fields": ["id: int"]}]
)
def test_all_rejected_pages_have_valid_empty_contract(client: Mock, schema: dict[str, object]) -> None:
    responses(client, page([None, ["raw"]]))
    src = source(schema=schema, on_validation_failure="discard")
    assert list(src.load(context())) == []
    assert src.require_schema_contract().locked


def test_originless_request_cursor_is_never_followed(client: Mock) -> None:
    cursor = "https://other.example.org/path?sig=secret"
    responses(client, page([], cursor), page([]))
    assert list(source().load(context())) == []
    assert client.post_json.call_args_list[1].args[0]["cursor"] == cursor


def test_over_budget_page_has_no_partial_row_yield(client: Mock) -> None:
    responses(client, page([{"id": 1}, {"id": 2}]))
    stream = source(max_rows=1).load(context())
    with pytest.raises(ValueError, match="row_limit"):
        next(stream)


def test_load_failure_and_close_are_idempotent(client: Mock) -> None:
    client.post_json.side_effect = RuntimeError("controlled transport failure")
    src = source()
    with pytest.raises(RuntimeError, match="controlled transport failure"):
        list(src.load(context()))
    src.close()
    client.close.assert_called_once()


def test_shutdown_before_load_allocates_no_client(client: Mock) -> None:
    ctx = context()
    ctx.shutdown_event.set()
    with pytest.raises(ValueError, match="source_cancelled"):
        list(source().load(ctx))
    client.post_json.assert_not_called()


def test_assistance_has_no_external_values() -> None:
    src = source(query={"dataset": "sensitive-private-dataset"})
    text = repr(src.get_agent_assistance())
    assert "sensitive-private-dataset" not in text
    assert "flows.example.org" not in text
    assert src.observed_value_type is None
    assert src.declared_guaranteed_fields == frozenset()


@pytest.mark.parametrize(
    "schema,expected",
    [
        ({"mode": "fixed", "fields": ["id: int"]}, [True, False]),
        ({"mode": "flexible", "fields": ["id: int"]}, [False, False]),
    ],
)
def test_declared_schema_modes_coerce_only_at_source_boundary(client: Mock, schema: dict[str, object], expected: list[bool]) -> None:
    responses(client, page([{"ID": "7", "extra": "raw"}, {"ID": "8"}]))
    src = source(schema=schema)
    result = list(src.load(context()))
    assert [r.is_quarantined for r in result] == expected
    assert result[-1].row == {"id": 8}
    assert src.declared_guaranteed_fields == frozenset({"id"})


def test_schema_failure_preserves_original_header_and_payload(client: Mock) -> None:
    original = {"Record ID": "not-an-int"}
    responses(client, page([original]))
    ctx = context()
    result = list(source(schema={"mode": "fixed", "fields": ["record_id: int"]}).load(ctx))
    assert result[0].row == original
    assert ctx.record_validation_error.call_args.kwargs["row"] == original
    assert "not-an-int" not in result[0].quarantine_error


def test_startup_never_creates_client_or_credentials(client: Mock, monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.plugins.sources import power_automate

    constructor = create_autospec(PowerAutomateOperationClient, spec_set=True, side_effect=AssertionError("client startup allocation"))
    monkeypatch.setattr(power_automate, "PowerAutomateOperationClient", constructor)
    src = source()
    src.on_start(context())
    src.close()
    constructor.assert_not_called()


def test_replay_direct_load_is_refused_before_client(client: Mock) -> None:
    from elspeth.contracts.errors import FrameworkBugError

    ctx = context()
    ctx.run_mode = RunMode.REPLAY
    with pytest.raises(FrameworkBugError, match="Replay restores"):
        list(source().load(ctx))
    client.post_json.assert_not_called()


def archived_options(cfg: dict[str, Any]) -> Any:
    from elspeth.contracts.hashing import stable_hash
    from elspeth.plugins.infrastructure.power_automate_nonlive import ArchivedPowerAutomateOptions, ArchivedPowerAutomateSourceConfig

    return ArchivedPowerAutomateOptions(
        "source", "input", "source-run", cfg, stable_hash(cfg), ArchivedPowerAutomateSourceConfig.model_validate(cfg)
    )


def test_safe_live_and_archived_initializers_match_without_secret_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.plugins.sources.power_automate import PowerAutomateSource

    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "archive-test-fingerprint-key")
    live_cfg = options(auth={"method": "sas_url", "trigger_url_secret": "https://flows.example.org/read?sig=private"})
    del live_cfg["trigger_url"]
    live = PowerAutomateSource(live_cfg)
    archive = archived_options(live.config)
    monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY")
    monkeypatch.delenv("UNUSED_READ_SECRET", raising=False)
    restored = PowerAutomateSource.from_archived_options(archive)
    assert restored.config == live.config
    assert restored.output_schema.model_json_schema() == live.output_schema.model_json_schema()
    assert restored.field_renames == live.field_renames
    assert "page_size" not in restored.config


def test_archived_source_cannot_dispatch_in_live_mode(client: Mock) -> None:
    from elspeth.contracts.errors import FrameworkBugError
    from elspeth.plugins.sources.power_automate import PowerAutomateSource

    restored = PowerAutomateSource.from_archived_options(archived_options(options()))
    with pytest.raises(FrameworkBugError, match="Archived Power Automate source"):
        list(restored.load(context()))
    client.post_json.assert_not_called()


def test_archived_source_refuses_structural_options_impostor() -> None:
    from elspeth.contracts.errors import FrameworkBugError
    from elspeth.plugins.sources.power_automate import PowerAutomateSource

    with pytest.raises(FrameworkBugError, match="nominal archived source options"):
        PowerAutomateSource.from_archived_options(SimpleNamespace(safe_options=options()))


@pytest.mark.parametrize(
    "candidate,schema",
    [
        ({"id": "sensitive-invalid-value"}, {"mode": "fixed", "fields": ["id: int"]}),
        ({"!!!sensitive-invalid-field": 1, "sensitive invalid field": 2}, {"mode": "observed"}),
    ],
)
def test_audit_failure_does_not_retain_external_validation_exception(
    client: Mock, candidate: dict[str, object], schema: dict[str, object]
) -> None:
    from elspeth.contracts.errors import FrameworkBugError

    responses(client, page([candidate]))
    ctx = context()
    ctx.record_validation_error.side_effect = FrameworkBugError("controlled audit failure")
    with pytest.raises(FrameworkBugError, match="controlled audit failure") as raised:
        list(source(schema=schema).load(ctx))
    assert raised.value.__context__ is None


@pytest.mark.parametrize("termination", ["exhausted", "transport_error", "generator_close", "cancelled"])
def test_operation_cleanup_preserves_engine_owned_lifecycle(client: Mock, termination: str) -> None:
    responses(client, page([{"id": 1}, {"id": 2}], None if termination == "exhausted" else "next"))
    src = source()
    ctx = context()
    src.on_start(ctx)
    stream = src.load(ctx)
    if termination == "transport_error":
        client.post_json.side_effect = RuntimeError("controlled transport failure")
        with pytest.raises(RuntimeError, match="controlled transport failure"):
            list(stream)
    elif termination == "exhausted":
        assert [row.row["id"] for row in stream] == [1, 2]
    else:
        assert next(stream).row == {"id": 1}
        if termination == "generator_close":
            stream.close()
        else:
            ctx.shutdown_event.set()
            with pytest.raises(ValueError, match="source_cancelled"):
                next(stream)

    client.close.assert_called_once()
    assert src._closed is False
    assert src._on_complete_called is False
    src.on_complete(ctx)
    assert src._on_complete_called is True
    src.close()
    src.close()
    assert src._closed is True
    client.close.assert_called_once()
