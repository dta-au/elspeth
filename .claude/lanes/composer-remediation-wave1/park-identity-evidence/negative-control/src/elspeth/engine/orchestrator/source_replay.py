"""Admission and source-row reconstruction for replay and verification runs.

The source run is terminal and immutable.  Capture its rows before plugin
startup so missing payloads or incomplete quarantine evidence cannot turn a
replay request into a partly live execution.
"""

from __future__ import annotations

import copy
import itertools
import json
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from elspeth.contracts import SourceRow
from elspeth.contracts.audit import NodeStateFailed, ValidationErrorRecord
from elspeth.contracts.enums import NodeType, RunMode, TerminalPath
from elspeth.contracts.errors import (
    AuditIntegrityError,
    ContractViolation,
    ExtraFieldViolation,
    MissingFieldViolation,
    OrchestrationInvariantError,
    TypeMismatchViolation,
    VerificationMismatchError,
)
from elspeth.contracts.freeze import deep_thaw, freeze_fields
from elspeth.contracts.hashing import canonical_json_loads
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.contracts.source_read_verification import SourceReadVerificationPolicy
from elspeth.contracts.type_normalization import CONTRACT_TYPE_MAP
from elspeth.contracts.types import NodeID
from elspeth.core.canonical import sanitize_for_canonical, stable_hash
from elspeth.core.checkpoint.serialization import checkpoint_loads
from elspeth.core.landscape.row_data import RowDataState
from elspeth.core.landscape.schema import SOURCE_COMPLETE_LIFECYCLE_STATES
from elspeth.core.operations import track_operation
from elspeth.engine.orchestrator.quarantine_router import _bound_quarantine_error
from elspeth.engine.orchestrator.schema_reconstruction import reconstruct_schema_from_json
from elspeth.engine.orchestrator.source_read_verification import source_load_input_data, source_read_verification_policy

if TYPE_CHECKING:
    from elspeth.contracts import SourceProtocol
    from elspeth.contracts.coordination import CoordinationToken
    from elspeth.contracts.plugin_context import PluginContext
    from elspeth.core.landscape.factory import LandscapeReadRepositories, RecorderFactory


@dataclass(frozen=True, slots=True)
class AuditedSource:
    """One source's complete, validated input and metadata snapshot."""

    name: str
    source_run_node_id: str
    rows: tuple[SourceRow, ...]
    schema_contract: SchemaContract | None
    source_schema_json: str
    field_resolution: Mapping[str, str] | None
    normalization_version: str | None
    validation_errors: tuple[ValidationErrorRecord, ...] = ()

    def __post_init__(self) -> None:
        freeze_fields(self, "field_resolution")


def _validation_signature(error: ValidationErrorRecord) -> tuple[str | None, ...]:
    """Compare retained decisions independently of run-local IDs and timestamps."""
    return (
        error.row_hash,
        error.row_data_json,
        error.error,
        error.schema_mode,
        error.destination,
        error.violation_type,
        error.original_field_name,
        error.normalized_field_name,
        error.expected_type,
        error.actual_type,
    )


def _restore_contract_violation(error: ValidationErrorRecord, original: Any) -> ContractViolation | None:
    """Reconstruct owned violation metadata without resolving arbitrary classes."""
    fields = (error.original_field_name, error.normalized_field_name, error.expected_type, error.actual_type)
    failure = f"Source replay validation error {error.error_id}: structured violation cannot be reconstructed"
    if error.violation_type is None:
        if any(value is not None for value in fields):
            raise AuditIntegrityError(failure)
        return None
    if type(error.original_field_name) is not str or type(error.normalized_field_name) is not str:
        raise AuditIntegrityError(failure)
    if error.violation_type in {"missing_field", "extra_field"}:
        if error.expected_type is not None or error.actual_type is not None:
            raise AuditIntegrityError(failure)
        violation_class = MissingFieldViolation if error.violation_type == "missing_field" else ExtraFieldViolation
        return violation_class(normalized_name=error.normalized_field_name, original_name=error.original_field_name)
    if error.violation_type != "type_mismatch" or error.expected_type not in CONTRACT_TYPE_MAP or type(original) is not dict:
        raise AuditIntegrityError(failure)
    actual_types = {**CONTRACT_TYPE_MAP, "list": list, "dict": dict, "tuple": tuple}
    if error.actual_type not in actual_types:
        raise AuditIntegrityError(failure)
    if error.normalized_field_name in original:
        actual_value = original[error.normalized_field_name]
    elif error.original_field_name in original:
        actual_value = original[error.original_field_name]
    else:
        raise AuditIntegrityError(failure)
    return TypeMismatchViolation(
        normalized_name=error.normalized_field_name,
        original_name=error.original_field_name,
        expected_type=CONTRACT_TYPE_MAP[error.expected_type],
        actual_type=actual_types[error.actual_type],
        actual_value=actual_value,
    )


def replay_source_rows(audited: AuditedSource, ctx: PluginContext) -> Iterator[SourceRow]:
    """Restore every admitted validation decision with new run-local links."""
    replay_error_ids: dict[str, str] = {}
    for error in audited.validation_errors:
        if error.row_data_json is None:
            raise AuditIntegrityError(f"Source replay validation error {error.error_id}: payload missing")
        original = canonical_json_loads(error.row_data_json)
        token = ctx.record_validation_error(
            row=original,
            error=error.error,
            schema_mode=error.schema_mode,
            destination=error.destination,
            contract_violation=_restore_contract_violation(error, original),
        )
        if error.destination != "discard":
            if token.error_id is None or ctx.pop_pending_quarantine_validation_error_id(original) != token.error_id:
                raise OrchestrationInvariantError("Source replay quarantine validation error was not recorded")
            replay_error_ids[error.error_id] = token.error_id
    for row in audited.rows:
        if row.validation_error_id is not None:
            yield replace(row, validation_error_id=replay_error_ids[row.validation_error_id])
        else:
            yield row


def _verified_rows(source: SourceProtocol, ctx: PluginContext, audited: AuditedSource) -> tuple[SourceRow, ...]:
    """Read one live source to EOF and reject every source-level difference."""
    rows: list[SourceRow] = []
    missing = object()
    for ordinal, (live, recorded) in enumerate(itertools.zip_longest(source.load(ctx), audited.rows, fillvalue=missing)):
        if live is missing or recorded is missing:
            raise VerificationMismatchError(f"Verify source {audited.name!r}: row count differs at ordinal {ordinal}")
        if type(live) is not SourceRow or type(recorded) is not SourceRow:
            raise OrchestrationInvariantError(f"Verify source {audited.name!r}: source yielded a non-SourceRow value")
        if (
            live.source_row_index != recorded.source_row_index
            or live.is_quarantined != recorded.is_quarantined
            or stable_hash(sanitize_for_canonical(live.row) if live.is_quarantined else live.row) != stable_hash(recorded.row)
            or (_bound_quarantine_error(live.quarantine_error) if live.quarantine_error is not None else None) != recorded.quarantine_error
            or live.quarantine_destination != recorded.quarantine_destination
            or (live.contract.version_hash() if live.contract is not None else None)
            != (recorded.contract.version_hash() if recorded.contract is not None else None)
        ):
            raise VerificationMismatchError(f"Verify source {audited.name!r}: row {ordinal} differs from audited run")
        # SourceRow is frozen, but its payload may be a mutable object reused
        # by the source generator. Preserve the exact value we compared before
        # advancing the generator to its next yield.
        rows.append(replace(live, row=copy.deepcopy(live.row)))
    return tuple(rows)


def prepare_verified_sources(
    factory: RecorderFactory,
    run_id: str,
    audited_sources: Mapping[str, AuditedSource],
    sources: Mapping[str, SourceProtocol],
    ctx: PluginContext,
    coordination_token: CoordinationToken,
) -> Mapping[str, tuple[SourceRow, ...]]:
    """Verify every complete live source stream before transform startup.

    Each source.load executes exactly once under its own source_load audit
    operation. The caller invokes this after source.on_start and before any
    transform or sink on_start, then passes the cached rows to the driver.
    """
    if coordination_token.run_id != run_id or ctx.run_id != run_id:
        raise OrchestrationInvariantError("Verify source preflight run identity mismatch")
    if ctx.run_mode is not RunMode.VERIFY:
        raise OrchestrationInvariantError("Verify source preflight requires verify run mode")
    if set(audited_sources) != set(sources):
        raise AuditIntegrityError("Verify source preflight declarations differ from audited snapshot")
    verified: dict[str, tuple[SourceRow, ...]] = {}
    saved_node_id = ctx.node_id
    saved_operation_id = ctx.operation_id
    try:
        for name, source in sources.items():
            audited = audited_sources[name]
            if type(audited) is not AuditedSource:
                raise OrchestrationInvariantError(f"Verify source {name!r}: audited snapshot has wrong type")
            if source.node_id is None:
                raise OrchestrationInvariantError(f"Verify source {name!r}: node ID is unassigned")
            source_id = NodeID(source.node_id)
            ctx.node_id = source_id
            with track_operation(
                recorder=factory.execution,
                run_id=run_id,
                node_id=source_id,
                operation_type="source_load",
                ctx=ctx,
                input_data=source_load_input_data(source),
            ) as operation:
                ctx.operation_id = operation.operation.operation_id
                policy_scope: AbstractContextManager[None] = nullcontext()
                if source_read_verification_policy(source) is not None:
                    if ctx.call_mode_session is None:
                        raise OrchestrationInvariantError("Canonical source verification requires a call-mode session")
                    policy_scope = ctx.call_mode_session.source_read_scope(source=source, current_operation_id=ctx.operation_id)
                with policy_scope:
                    verified[name] = _verified_rows(source, ctx, audited)
                live_errors = tuple(
                    error for error in factory.data_flow.get_validation_errors_for_run(run_id) if error.node_id == source_id
                )
                if tuple(map(_validation_signature, live_errors)) != tuple(map(_validation_signature, audited.validation_errors)):
                    raise VerificationMismatchError(f"Verify source {name!r}: validation decisions differ from audited run")
            ctx.operation_id = None
    finally:
        ctx.node_id = saved_node_id
        ctx.operation_id = saved_operation_id
    return verified


def _quarantine_details(
    factory: RecorderFactory | LandscapeReadRepositories, run_id: str, row_id: str, source_node_id: str
) -> tuple[str, str] | None:
    """Recover the source quarantine decision from its durable audit path."""
    source_failures = [
        state
        for token in factory.query.get_tokens(row_id)
        for state in factory.query.get_node_states_for_token(token.token_id)
        if type(state) is NodeStateFailed and state.node_id == source_node_id and state.step_index == 0
    ]
    if not source_failures:
        return None
    if len(source_failures) != 1:
        raise AuditIntegrityError(f"Source replay row {row_id}: source validation error evidence missing")
    source_failure = source_failures[0]
    if source_failure.error_json is None:
        raise AuditIntegrityError(f"Source replay row {row_id}: source validation error evidence missing")
    outcomes = factory.data_flow.get_token_outcomes_for_row(run_id, row_id)
    quarantined = [
        outcome
        for outcome in outcomes
        if outcome.path is TerminalPath.QUARANTINED_AT_SOURCE and outcome.token_id == source_failure.token_id
    ]
    if not quarantined:
        raise AuditIntegrityError(f"Source replay row {row_id}: failed source state has no quarantine outcome")
    if len(quarantined) != 1 or len(outcomes) != 1:
        raise AuditIntegrityError(f"Source replay row {row_id}: ambiguous quarantine outcomes")
    outcome = quarantined[0]
    if outcome.sink_name is None:
        raise AuditIntegrityError(f"Source replay row {row_id}: quarantine destination missing")
    error_record = json.loads(source_failure.error_json)
    if type(error_record) is not dict or "exception" not in error_record:
        raise AuditIntegrityError(f"Source replay row {row_id}: malformed source validation error evidence")
    exception = error_record["exception"]
    if type(exception) is not str or not exception:
        raise AuditIntegrityError(f"Source replay row {row_id}: malformed source validation error evidence")
    return exception, outcome.sink_name


def prepare_audited_sources(
    factory: RecorderFactory | LandscapeReadRepositories,
    replay_from: str,
    sources: Mapping[str, SourceProtocol],
) -> Mapping[str, AuditedSource]:
    """Validate and snapshot every audited source row before plugin effects.

    The caller checks the source run's terminal status before this function.
    The audit source names, plugin identity, config, row payload hashes and
    quarantine decisions must all be reconstructable.  No source plugin hook
    is called here.
    """
    lifecycle = factory.run_lifecycle.get_run_source_lifecycle_records(replay_from)
    by_name = {record.source_name: record for record in lifecycle.values()}
    if len(by_name) != len(lifecycle) or set(by_name) != set(sources):
        raise AuditIntegrityError(
            f"Source replay run {replay_from}: source declarations differ from audited run "
            f"(audited={sorted(by_name)}, configured={sorted(sources)})"
        )
    nodes = {node.node_id: node for node in factory.data_flow.get_nodes(replay_from) if node.node_type is NodeType.SOURCE}
    resolutions = factory.run_lifecycle.get_source_field_resolutions(replay_from)
    rows_by_source: dict[str, list[SourceRow]] = {name: [] for name in sources}
    schema_by_source: dict[str, type[Any]] = {}
    contract_by_source: dict[str, SchemaContract | None] = {}
    schema_json_by_source: dict[str, str] = {}
    mandatory_quarantine_ledger_sources: set[str] = set()

    for name, source in sources.items():
        record = by_name[name]
        if record.lifecycle_state not in SOURCE_COMPLETE_LIFECYCLE_STATES:
            raise AuditIntegrityError(f"Source replay run {replay_from}: source {name!r} was not exhausted")
        if record.source_node_id not in nodes:
            raise AuditIntegrityError(f"Source replay run {replay_from}: source {name!r} node is missing")
        node = nodes[record.source_node_id]
        if node.plugin_name != source.name:
            raise AuditIntegrityError(f"Source replay run {replay_from}: source {name!r} plugin identity differs")
        audited_config = json.loads(node.config_json)
        if type(audited_config) is not dict or stable_hash(audited_config) != node.config_hash:
            raise AuditIntegrityError(f"Source replay run {replay_from}: source {name!r} audited config is corrupt")
        if "source_name" not in audited_config:
            raise AuditIntegrityError(f"Source replay run {replay_from}: source {name!r} audited name is missing")
        audited_name = audited_config.pop("source_name")
        if (
            audited_name != name
            or stable_hash(audited_config) != stable_hash(source.config)
            or node.plugin_version != source.plugin_version
            or node.source_file_hash != source.source_file_hash
        ):
            raise AuditIntegrityError(f"Source replay run {replay_from}: source {name!r} implementation or configuration differs")
        if source_read_verification_policy(source) is SourceReadVerificationPolicy.POWER_AUTOMATE_CANONICAL_JSON_V1:
            # L3 issues this nominal policy only for the exact reviewed
            # builtin, which records every rejected candidate before yield.
            # Its quarantine decision must survive even for ordinary dicts
            # whose payload shape alone can be reconstructed unambiguously.
            mandatory_quarantine_ledger_sources.add(name)
        _, contract = factory.data_flow.get_node_contracts(replay_from, record.source_node_id)
        contract_by_source[name] = contract
        if record.source_schema_json is None:
            raise AuditIntegrityError(f"Source replay run {replay_from}: source {name!r} has no stored schema")
        try:
            source_schema = json.loads(record.source_schema_json)
        except json.JSONDecodeError as exc:
            raise AuditIntegrityError(f"Source replay run {replay_from}: source {name!r} has malformed schema JSON") from exc
        if type(source_schema) is not dict:
            raise AuditIntegrityError(f"Source replay run {replay_from}: source {name!r} schema is not an object")
        schema_json_by_source[name] = record.source_schema_json

    if len(sources) > 1 and any(nodes[record.source_node_id].sequence_in_pipeline is None for record in by_name.values()):
        raise AuditIntegrityError(f"Source replay run {replay_from}: source order cannot be established from audit nodes")

    ordered_names = [
        name for name, record in sorted(by_name.items(), key=lambda item: nodes[item[1].source_node_id].sequence_in_pipeline or 0)
    ]
    if ordered_names != list(sources):
        raise AuditIntegrityError(
            f"Source replay run {replay_from}: source order differs from audited run (audited={ordered_names}, configured={list(sources)})"
        )
    node_name = {record.source_node_id: name for name, record in by_name.items()}
    errors_by_source: dict[str, list[ValidationErrorRecord]] = {name: [] for name in sources}
    quarantine_errors_by_row: dict[str, ValidationErrorRecord] = {}
    error_ids: set[str] = set()
    for error in factory.data_flow.get_validation_errors_for_run(replay_from):
        if error.run_id != replay_from or error.error_id in error_ids:
            raise AuditIntegrityError(f"Source replay validation error {error.error_id}: duplicate or foreign run evidence")
        error_ids.add(error.error_id)
        if error.node_id not in node_name:
            raise AuditIntegrityError(f"Source replay validation error {error.error_id}: undeclared source node {error.node_id}")
        if error.row_data_json is None or (error.destination == "discard") != (error.row_id is None):
            raise AuditIntegrityError(f"Source replay validation error {error.error_id}: payload or linkage is invalid")
        try:
            original = canonical_json_loads(error.row_data_json)
            restored_hash = stable_hash(original)
        except (ValueError, TypeError) as exc:
            raise AuditIntegrityError(f"Source replay validation error {error.error_id}: payload cannot be reconstructed") from exc
        if restored_hash != error.row_hash:
            raise AuditIntegrityError(f"Source replay validation error {error.error_id}: payload hash mismatch or noncanonical evidence")
        _restore_contract_violation(error, original)
        if error.row_id is not None:
            if error.row_id in quarantine_errors_by_row:
                raise AuditIntegrityError(f"Source replay row {error.row_id}: ambiguous quarantine validation evidence")
            quarantine_errors_by_row[error.row_id] = error
        errors_by_source[node_name[error.node_id]].append(error)
    linked_quarantine_rows: set[str] = set()
    for batch in factory.query.iter_rows_for_run(replay_from):
        for row in batch:
            if row.source_node_id not in node_name:
                raise AuditIntegrityError(f"Source replay row {row.row_id}: undeclared source node {row.source_node_id}")
            row_source_name = node_name[row.source_node_id]
            payload = factory.query.get_row_data(row.row_id)
            if payload.state is not RowDataState.AVAILABLE or payload.data is None:
                raise AuditIntegrityError(
                    f"Source replay row {row.row_id}: payload is {payload.state.value}; exact replay requires stored data"
                )
            # RowDataResult freezes nested JSON arrays to tuples. Restore
            # their JSON shape before comparing with original validation
            # evidence or emitting rows back into the pipeline.
            data = deep_thaw(payload.data)
            quarantine = _quarantine_details(factory, replay_from, row.row_id, row.source_node_id)
            validation_error = quarantine_errors_by_row.get(row.row_id)
            if quarantine is not None:
                if validation_error is None and row_source_name in mandatory_quarantine_ledger_sources:
                    raise AuditIntegrityError(f"Source replay row {row.row_id}: quarantine validation evidence missing")
                if stable_hash(data) != row.source_data_hash:
                    raise AuditIntegrityError(f"Source replay row {row.row_id}: quarantine payload hash mismatch")
                raw_data: Any = data
                if validation_error is not None:
                    if (
                        validation_error.node_id != row.source_node_id
                        or validation_error.destination != quarantine[1]
                        or _bound_quarantine_error(validation_error.error) != quarantine[0]
                    ):
                        raise AuditIntegrityError(f"Source replay row {row.row_id}: quarantine decision disagrees with validation evidence")
                    linked_quarantine_rows.add(row.row_id)
                if set(data) == {"_raw"}:
                    # Token ingestion wraps non-dict quarantined values in
                    # {"_raw": value}; a genuine dict with that sole key has
                    # identical stored bytes. The linked validation error is
                    # the only evidence that distinguishes their original
                    # shapes. Refuse if the source never recorded it.
                    if validation_error is None or validation_error.row_data_json is None:
                        raise AuditIntegrityError(f"Source replay row {row.row_id}: ambiguous _raw quarantine payload")
                    original = canonical_json_loads(validation_error.row_data_json)
                    if original == data:
                        raw_data = data
                    elif original == data["_raw"]:
                        raw_data = data["_raw"]
                    else:
                        raise AuditIntegrityError(f"Source replay row {row.row_id}: quarantine payload disagrees with validation evidence")
                if validation_error is not None and stable_hash(raw_data) != validation_error.row_hash:
                    raise AuditIntegrityError(f"Source replay row {row.row_id}: quarantine payload disagrees with validation evidence")
                source_row = SourceRow.quarantined(
                    raw_data,
                    quarantine[0],
                    quarantine[1],
                    source_row_index=row.source_row_index,
                    validation_error_id=validation_error.error_id if validation_error is not None else None,
                )
            else:
                if validation_error is not None:
                    raise AuditIntegrityError(f"Source replay row {row.row_id}: validation evidence has no quarantine decision")
                if row_source_name not in schema_by_source:
                    if by_name[row_source_name].source_schema_json is None:
                        raise AuditIntegrityError(f"Source replay run {replay_from}: source {row_source_name!r} has no schema record")
                    schema_by_source[row_source_name] = reconstruct_schema_from_json(json.loads(schema_json_by_source[row_source_name]))
                if row.source_contract_json is None:
                    raise AuditIntegrityError(f"Source replay row {row.row_id}: source contract missing")
                try:
                    contract_data = checkpoint_loads(row.source_contract_json)
                except (UnicodeDecodeError, ValueError) as exc:
                    raise AuditIntegrityError(f"Source replay row {row.row_id}: malformed source contract") from exc
                if type(contract_data) is not dict:
                    raise AuditIntegrityError(f"Source replay row {row.row_id}: malformed source contract")
                contract = SchemaContract.from_checkpoint(contract_data)
                validated = schema_by_source[row_source_name].model_validate(data).to_row()
                if stable_hash(validated) != row.source_data_hash:
                    raise AuditIntegrityError(f"Source replay row {row.row_id}: restored payload hash mismatch")
                source_row = SourceRow.valid(validated, contract=contract, source_row_index=row.source_row_index)
            rows_by_source[row_source_name].append(source_row)

    if linked_quarantine_rows != set(quarantine_errors_by_row):
        raise AuditIntegrityError(f"Source replay run {replay_from}: quarantine validation evidence has missing rows")

    result: dict[str, AuditedSource] = {}
    for name, record in by_name.items():
        resolution = resolutions[record.source_node_id] if record.source_node_id in resolutions else None
        result[name] = AuditedSource(
            name=name,
            source_run_node_id=record.source_node_id,
            rows=tuple(rows_by_source[name]),
            schema_contract=contract_by_source[name],
            source_schema_json=schema_json_by_source[name],
            field_resolution=resolution.resolution_mapping if resolution is not None else None,
            normalization_version=record.normalization_version,
            validation_errors=tuple(errors_by_source[name]),
        )
    return result
