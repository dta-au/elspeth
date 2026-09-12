"""Reviewed consumer obligations for the bounded exception-field inventory.

Each message pair changes one constructor input. Structured cases exercise
the declared serializer, not a claim that every caller invokes it. Richer
operator, audit and retry obligations use actual production consumers.
"""

import json
from collections.abc import Mapping
from contextlib import redirect_stderr
from functools import partial
from io import StringIO
from typing import Any

from elspeth.composer_mcp import session
from elspeth.contracts import errors
from elspeth.contracts.audit_evidence import AuditEvidenceBase
from elspeth.contracts.checkpoint import ResumeRefusalCause
from elspeth.contracts.coordination import RegisteredWorker
from elspeth.contracts.enums import FrameKind
from elspeth.core.checkpoint import recovery
from tests.fixtures.abandon_refusal_diagnostics import assert_abandon_refusal_cause_reaches_cli
from tests.fixtures.exception_diagnostic_consumers import (
    exercise_audit_failed_turn,
    exercise_capacity_status_code,
    exercise_coalesce_metadata,
    exercise_graceful_shutdown_summary,
    exercise_plugin_retryable_decision,
    exercise_plugin_status_code,
)
from tests.helpers.exception_diagnostics import DiagnosticCase


def _exercise_message(
    exception: type[BaseException],
    baseline: Mapping[str, Any],
    parameter: str,
    variants: tuple[tuple[Any, str], tuple[Any, str]],
) -> None:
    """Check content, not merely inequality (two arbitrary wrong texts differ)."""
    (first, first_fragment), (second, second_fragment) = variants
    first_message = str(exception(**dict(baseline, **{parameter: first})))
    second_message = str(exception(**dict(baseline, **{parameter: second})))
    assert first_fragment in first_message, "diagnostic fragment missing from first observation"
    assert second_fragment not in first_message, "diagnostic fragment leaked from second observation"
    assert second_fragment in second_message, "diagnostic fragment missing from second observation"
    assert first_fragment not in second_message, "diagnostic fragment leaked from first observation"


def _message_case(
    exception: type[BaseException],
    baseline: Mapping[str, Any],
    field: str,
    variants: tuple[tuple[Any, str], tuple[Any, str]],
    *,
    parameter: str | None = None,
) -> DiagnosticCase:
    return DiagnosticCase(
        exception,
        frozenset({field}),
        "message",
        "The real exception renderer preserves this independently varied observation.",
        partial(_exercise_message, exception, baseline, parameter or field, variants),
    )


def _text_cases(exception: type[BaseException], baseline: Mapping[str, Any], *fields: str) -> tuple[DiagnosticCase, ...]:
    return tuple(
        _message_case(
            exception,
            baseline,
            field,
            ((f"alpha_{field}_value", f"alpha_{field}_value"), (f"omega_{field}_value", f"omega_{field}_value")),
        )
        for field in fields
    )


def _exercise_structured(
    exception: type[BaseException],
    baseline: Mapping[str, Any],
    field: str,
    variants: tuple[Any, Any],
) -> None:
    for value in variants:
        error = exception(**dict(baseline, **{field: value}))
        assert isinstance(error, AuditEvidenceBase)
        payload = error.to_audit_dict()
        expected = sorted(value) if isinstance(value, frozenset) else value
        assert field in payload, f"diagnostic field missing: {field}"
        assert payload[field] == expected, f"diagnostic field differs: {field}"


def _structured_cases(
    exception: type[BaseException], baseline: Mapping[str, Any], variants: Mapping[str, tuple[Any, Any]]
) -> tuple[DiagnosticCase, ...]:
    return tuple(
        DiagnosticCase(
            exception,
            frozenset({field}),
            "structured",
            "The public audit serializer preserves this field; this alone does not prove caller invocation.",
            partial(_exercise_structured, exception, baseline, field, pair),
        )
        for field, pair in variants.items()
    )


def _exercise_private_value() -> None:
    for private_value in ("private-row-alpha@example.invalid", "private-row-omega@example.invalid"):
        error = errors.TypeMismatchViolation(
            normalized_name="amount", original_name="Amount", expected_type=int, actual_type=str, actual_value=private_value
        )
        # Retention is intentional debugging context; the user-data value is
        # explicitly excluded from message and quarantine reason by class Note.
        assert error.actual_value == private_value
        assert private_value not in str(error), "private diagnostic leaked"
        reason = error.to_error_reason()
        assert "actual_value" not in reason
        assert private_value not in json.dumps(reason)
        assert "int" in str(error) and "str" in str(error)


def _exercise_private_context() -> None:
    for private_value in ("private-context-alpha@example.invalid", "private-context-omega@example.invalid"):
        error = errors.CommencementGateFailedError(
            gate_name="readiness", condition="ready == true", reason="not ready", context_snapshot={"customer": private_value}
        )
        # The retained immutable context is for in-process diagnosis, not
        # generic exception/audit disclosure. The real CLI has a separate
        # producer-to-output privacy regression test.
        assert error.context_snapshot["customer"] == private_value
        assert private_value not in str(error), "private diagnostic leaked"
        payload = errors.ExecutionError(exception=str(error), exception_type=type(error).__name__).to_dict()
        assert private_value not in json.dumps(payload)
        assert "context_snapshot" not in payload


def _exercise_worker_roster() -> None:
    from elspeth.cli import _emit_write_lock_held

    # Equal-length rosters plus one-coordinate changes catch count-only,
    # constant PID, collapsed null, missing host, role and status projections.
    records = (
        RegisteredWorker("worker-alpha", "leader", "active", 17011, "host-alpha"),
        RegisteredWorker("worker-omega", "leader", "active", 17011, "host-alpha"),
        RegisteredWorker("worker-alpha", "follower", "active", 17011, "host-alpha"),
        RegisteredWorker("worker-alpha", "leader", "departed", 17011, "host-alpha"),
        RegisteredWorker("worker-alpha", "leader", "active", 27023, "host-alpha"),
        RegisteredWorker("worker-alpha", "leader", "active", None, "host-alpha"),
        RegisteredWorker("worker-alpha", "leader", "active", 17011, "host-omega"),
        RegisteredWorker("worker-alpha", "leader", "active", 17011, None),
    )
    for worker in records:
        error = errors.WriteLockHeldError(run_id="diagnostic-run", workers=(worker,))
        generic_message = str(error)
        assert worker.worker_id not in generic_message
        if worker.hostname is not None:
            assert worker.hostname not in generic_message
        if worker.pid is not None:
            assert str(worker.pid) not in generic_message
        output = StringIO()
        with redirect_stderr(output):
            _emit_write_lock_held(error, "json")
        payload = json.loads(output.getvalue())
        assert payload["registered_workers"] == [
            {"worker_id": worker.worker_id, "role": worker.role, "status": worker.status, "pid": worker.pid, "hostname": worker.hostname}
        ]
        assert payload["lock_owner_identified"] is False
    output = StringIO()
    with redirect_stderr(output):
        _emit_write_lock_held(errors.WriteLockHeldError(run_id="diagnostic-run", workers=()), "json")
    assert json.loads(output.getvalue())["registered_workers"] == []


def _exercise_resume_cause() -> None:
    from elspeth.cli import _emit_not_resumable_event

    for cause in ResumeRefusalCause:
        output = StringIO()
        with redirect_stderr(output):
            _emit_not_resumable_event(recovery.NonResumableRunError("same-run", "identical explanation", cause=cause), "json")
        assert json.loads(output.getvalue()) == {
            "event": "not_resumable",
            "run_id": "same-run",
            "reason": cause.value,
            "message": "identical explanation",
        }


def _exercise_preflight_retryable() -> None:
    from elspeth.engine.orchestrator.runtime_preflight import _runtime_preflight_is_retryable

    for retryable in (False, True):
        error = errors.RuntimePreflightFailedError(
            plugin_name="diagnostic-plugin",
            provider="diagnostic-provider",
            cause=errors.PluginRetryableError("same cause", retryable=retryable),
        )
        assert _runtime_preflight_is_retryable(error) is retryable
    assert (
        _runtime_preflight_is_retryable(
            errors.RuntimePreflightFailedError(
                plugin_name="diagnostic-plugin", provider="diagnostic-provider", cause=ValueError("permanent")
            )
        )
        is False
    )


def _exercise_group_members() -> None:
    # The declared message identifies the closer/group/member. Frame kind is
    # internal topology metadata, not promised as another message coordinate.
    for coordinate in ("closer_name", "group_id", "member_key"):
        baseline = {"closer_name": "closer-fixed", "group_id": "group-fixed", "member_key": "member-fixed", "kind": FrameKind.FORK}
        _exercise_message(
            recovery.GroupUnsatisfiableResumeError,
            {"run_id": "fixed-run"},
            "members",
            (
                ([recovery.UnsatisfiableGroupMember(**dict(baseline, **{coordinate: "coordinate-alpha"}))], "coordinate-alpha"),
                ([recovery.UnsatisfiableGroupMember(**dict(baseline, **{coordinate: "coordinate-omega"}))], "coordinate-omega"),
            ),
        )


def _exercise_inherited_message(exception: type[BaseException], baseline: Mapping[str, Any]) -> None:
    _exercise_message(exception, baseline, "message", (("diagnostic-alpha", "diagnostic-alpha"), ("diagnostic-omega", "diagnostic-omega")))


def _exercise_inherited_args(exception: type[BaseException]) -> None:
    for message in ("diagnostic-alpha", "diagnostic-omega"):
        assert str(exception(message)) == message


def _exercise_inherited_contract(exception: type[BaseException]) -> None:
    _exercise_message(
        exception,
        {"normalized_name": "fixed-normalized"},
        "original_name",
        (("Original Alpha", "Original Alpha"), ("Original Omega", "Original Omega")),
    )


def _inherited_cases() -> tuple[DiagnosticCase, ...]:
    explanation = "No locally owned constructor fields; execute the inherited renderer. Imported-base fields are outside discovery scope."
    cases = [
        DiagnosticCase(exception, frozenset(), "inherited", explanation, partial(_exercise_inherited_args, exception))
        for exception in (
            errors.SinkEffectCapabilityError,
            errors.GuidedCustodyIntegrityError,
            errors.PipelineLoweringError,
            errors.OrchestrationInvariantError,
            errors.PluginContractViolation,
            errors.SinkTransactionalInvariantError,
        )
    ]
    cases.extend(
        DiagnosticCase(exception, frozenset(), "inherited", explanation, partial(_exercise_inherited_contract, exception))
        for exception in (errors.MissingFieldViolation, errors.ExtraFieldViolation)
    )
    payloads = (
        (errors.DeclaredRequiredInputFieldsViolation, {"declared": ["a"], "effective_input_fields": [], "missing": ["a"]}),
        (
            errors.DeclaredOutputFieldsViolation,
            {
                "declared": ["a"],
                "violation_count": 1,
                "violations_truncated": False,
                "violations": [{"emitted_index": 0, "runtime_observed": [], "missing": ["a"]}],
            },
        ),
        (errors.SourceGuaranteedFieldsViolation, {"declared": ["a"], "runtime_observed": [], "missing": ["a"]}),
        (errors.SinkRequiredFieldsViolation, {"declared": ["a"], "runtime_observed": [], "missing": ["a"]}),
        (
            errors.SchemaConfigModeViolation,
            {"emitted_index": 0, "declared_mode": "fixed", "observed_mode": "observed", "declared_locked": True, "observed_locked": False},
        ),
        (errors.UnexpectedEmptyEmissionViolation, {"passes_through_input": True, "can_drop_rows": False, "emitted_count": 0}),
    )
    for exception, payload in payloads:
        cases.append(
            DiagnosticCase(
                exception,
                frozenset(),
                "inherited",
                explanation,
                partial(
                    _exercise_inherited_message,
                    exception,
                    {
                        "plugin": "diagnostic-plugin",
                        "node_id": "diagnostic-node",
                        "run_id": "diagnostic-run",
                        "row_id": "diagnostic-row",
                        "token_id": "diagnostic-token",
                        "payload": payload,
                    },
                ),
            )
        )
    return tuple(cases)


_PREFLIGHT = {"plugin_name": "fixed-plugin", "provider": "fixed-provider", "cause": ValueError("fixed cause")}
_TYPE_MISMATCH = {"normalized_name": "field", "original_name": "Field", "expected_type": int, "actual_type": str, "actual_value": "private"}


DIAGNOSTIC_CASES = (
    *_text_cases(errors.GracefulShutdownError, {"rows_processed": 7, "run_id": "fixed-run"}, "run_id"),
    DiagnosticCase(
        errors.GracefulShutdownError,
        frozenset(
            {
                "rows_processed",
                "rows_succeeded",
                "rows_failed",
                "rows_quarantined",
                "rows_routed_success",
                "rows_routed_failure",
                "routed_destinations",
            }
        ),
        "structured",
        "RunCeremony forwards counters and destination maps to the actual RunSummary subscriber.",
        exercise_graceful_shutdown_summary,
    ),
    _message_case(errors.MaxRetriesExceeded, {"attempts": 7, "last_error": ValueError("fixed")}, "attempts", ((107, "107"), (211, "211"))),
    _message_case(
        errors.MaxRetriesExceeded,
        {"attempts": 7},
        "last_error",
        ((ValueError("cause-alpha"), "cause-alpha"), (ValueError("cause-omega"), "cause-omega")),
    ),
    DiagnosticCase(
        errors.AuditIntegrityError,
        frozenset({"failed_turn"}),
        "structured",
        "Actual failed-turn producer to registered web response.",
        exercise_audit_failed_turn,
    ),
    *_text_cases(
        errors.SchedulerLeaseLostError,
        {"work_item_id": "fixed-work", "lease_owner": "fixed-owner", "run_id": "fixed-run"},
        "work_item_id",
        "lease_owner",
        "run_id",
    ),
    *_text_cases(
        errors.RunLeadershipLostError,
        {"run_id": "fixed-run", "worker_id": "fixed-worker", "leader_epoch": 7, "verb": "fixed-verb"},
        "run_id",
        "worker_id",
        "verb",
    ),
    _message_case(
        errors.RunLeadershipLostError,
        {"run_id": "fixed-run", "worker_id": "fixed-worker", "verb": "fixed-verb"},
        "leader_epoch",
        ((107, "107"), (211, "211")),
    ),
    *_text_cases(
        errors.RunMembershipLostError,
        {"run_id": "fixed-run", "worker_id": "fixed-worker", "verb": "fixed-verb"},
        "run_id",
        "worker_id",
        "verb",
    ),
    *_text_cases(
        errors.RunWorkerEvictedError,
        {"run_id": "fixed-run", "worker_id": "fixed-worker", "reason": "fixed-reason"},
        "run_id",
        "worker_id",
        "reason",
    ),
    *_text_cases(errors.JoinRefusedError, {"run_id": "fixed-run", "reason": "fixed-reason"}, "run_id", "reason"),
    *_text_cases(errors.FollowerSeatDeadError, {"run_id": "fixed-run", "worker_id": "fixed-worker"}, "run_id", "worker_id"),
    *_text_cases(
        errors.AbandonRefusedError,
        {"run_id": "fixed-run", "reason": "fixed-reason", "cause": ResumeRefusalCause.LEADER_LIVE},
        "run_id",
        "reason",
    ),
    DiagnosticCase(
        errors.AbandonRefusedError,
        frozenset({"cause"}),
        "structured",
        "Actual refusal race to CLI JSON, including changed database state after refusal.",
        assert_abandon_refusal_cause_reaches_cli,
    ),
    *_text_cases(errors.WriteLockHeldError, {"run_id": "fixed-run", "workers": ()}, "run_id"),
    DiagnosticCase(
        errors.WriteLockHeldError,
        frozenset({"workers"}),
        "operator",
        "The local CLI renders registration candidates, while generic text deliberately excludes identities.",
        _exercise_worker_roster,
    ),
    DiagnosticCase(
        errors.CoalesceCollisionError,
        frozenset({"metadata"}),
        "structured",
        "Actual coalesce failure cleanup persists metadata through Landscape.",
        exercise_coalesce_metadata,
    ),
    *_text_cases(errors.EmptyResumeStateError, {"run_id": "fixed-run"}, "run_id"),
    *_text_cases(errors.IncompleteSourceResumeError, {"run_id": "fixed-run", "source_states": {"source": "loading"}}, "run_id"),
    _message_case(
        errors.IncompleteSourceResumeError,
        {"run_id": "fixed-run"},
        "source_states",
        (({"source-alpha": "loading"}, "source-alpha=loading"), ({"source-omega": "failed"}, "source-omega=failed")),
    ),
    DiagnosticCase(
        errors.PluginRetryableError,
        frozenset({"retryable"}),
        "control",
        "Actual pool retry behavior differs for permanent and transient errors.",
        exercise_plugin_retryable_decision,
    ),
    DiagnosticCase(
        errors.PluginRetryableError,
        frozenset({"status_code"}),
        "structured",
        "Actual pool timeout result preserves exact status or absence.",
        exercise_plugin_status_code,
    ),
    *_text_cases(errors.RuntimePreflightFailedError, _PREFLIGHT, "plugin_name", "provider"),
    _message_case(
        errors.RuntimePreflightFailedError,
        _PREFLIGHT,
        "cause_type",
        ((ValueError("same"), "ValueError"), (RuntimeError("same"), "RuntimeError")),
        parameter="cause",
    ),
    DiagnosticCase(
        errors.RuntimePreflightFailedError,
        frozenset({"retryable"}),
        "control",
        "The actual runtime-preflight classifier consumes wrapper retryability.",
        _exercise_preflight_retryable,
    ),
    *_structured_cases(
        errors.ZeroEmissionSuccessContractViolation,
        {
            "transform": "fixed-transform",
            "transform_node_id": "fixed-node",
            "run_id": "fixed-run",
            "row_id": "fixed-row",
            "token_id": "fixed-token",
            "passes_through_input": True,
            "can_drop_rows": False,
            "emitted_count": 0,
            "message": "fixed-message",
        },
        {
            "transform": ("transform-alpha", "transform-omega"),
            "transform_node_id": ("node-alpha", "node-omega"),
            "run_id": ("run-alpha", "run-omega"),
            "row_id": ("row-alpha", "row-omega"),
            "token_id": ("token-alpha", "token-omega"),
            "passes_through_input": (False, True),
            "can_drop_rows": (False, True),
            "emitted_count": (0, 2),
        },
    ),
    *_structured_cases(
        errors.PassThroughContractViolation,
        {
            "transform": "fixed-transform",
            "transform_node_id": "fixed-node",
            "run_id": "fixed-run",
            "row_id": "fixed-row",
            "token_id": "fixed-token",
            "static_contract": frozenset({"a", "b"}),
            "runtime_observed": frozenset({"a"}),
            "divergence_set": frozenset({"b"}),
            "message": "fixed-message",
        },
        {
            "transform": ("transform-alpha", "transform-omega"),
            "transform_node_id": ("node-alpha", "node-omega"),
            "static_contract": (frozenset({"a", "alpha"}), frozenset({"a", "omega"})),
            "runtime_observed": (frozenset({"a", "alpha"}), frozenset({"a", "omega"})),
            "divergence_set": (frozenset({"alpha"}), frozenset({"omega"})),
        },
    ),
    *_text_cases(
        errors.ContractViolation,
        {"normalized_name": "fixed-normalized", "original_name": "fixed-original"},
        "normalized_name",
        "original_name",
    ),
    _message_case(errors.TypeMismatchViolation, _TYPE_MISMATCH, "expected_type", ((float, "'float'"), (bytes, "'bytes'"))),
    _message_case(errors.TypeMismatchViolation, _TYPE_MISMATCH, "actual_type", ((float, "'float'"), (bytes, "'bytes'"))),
    DiagnosticCase(
        errors.TypeMismatchViolation,
        frozenset({"actual_value"}),
        "private",
        "Class Note excludes raw row values (PII) from message and error reason.",
        _exercise_private_value,
    ),
    *_text_cases(
        errors.ContractMergeError, {"field": "fixed-field", "type_a": "fixed-a", "type_b": "fixed-b"}, "field", "type_a", "type_b"
    ),
    *_text_cases(
        errors.DependencyFailedError,
        {"dependency_name": "fixed-dependency", "run_id": "fixed-run", "reason": "fixed-reason"},
        "dependency_name",
        "run_id",
        "reason",
    ),
    *_text_cases(
        errors.CommencementGateFailedError,
        {"gate_name": "fixed-gate", "condition": "fixed-condition", "reason": "fixed-reason", "context_snapshot": {}},
        "gate_name",
        "condition",
        "reason",
    ),
    DiagnosticCase(
        errors.CommencementGateFailedError,
        frozenset({"context_snapshot"}),
        "private",
        "Class Note retains frozen context privately; it is not a generic disclosure contract.",
        _exercise_private_context,
    ),
    *_text_cases(errors.RetrievalNotReadyError, {"collection": "fixed-collection", "reason": "fixed-reason"}, "collection", "reason"),
    *_text_cases(errors.DuplicateDocumentError, {"collection": "fixed-collection", "duplicate_ids": ["fixed-document"]}, "collection"),
    _message_case(
        errors.DuplicateDocumentError,
        {"collection": "fixed-collection"},
        "duplicate_ids",
        ((["document-alpha"], "document-alpha"), (["document-omega"], "document-omega")),
    ),
    DiagnosticCase(
        errors.CapacityError,
        frozenset({"status_code"}),
        "structured",
        "Actual pool timeout result preserves the capacity HTTP status.",
        exercise_capacity_status_code,
    ),
    *_text_cases(
        errors.TelemetryExporterError, {"exporter_name": "fixed-exporter", "message": "fixed-message"}, "exporter_name", "message"
    ),
    *_text_cases(
        recovery.NonResumableRunError,
        {"run_id": "fixed-run", "reason": "fixed-reason", "cause": ResumeRefusalCause.LEADER_LIVE},
        "run_id",
        "reason",
    ),
    DiagnosticCase(
        recovery.NonResumableRunError,
        frozenset({"cause"}),
        "structured",
        "Actual CLI emitter preserves every observed cause with identical free text.",
        _exercise_resume_cause,
    ),
    *_text_cases(
        recovery.GroupUnsatisfiableResumeError,
        {"run_id": "fixed-run", "members": [recovery.UnsatisfiableGroupMember("closer", "group", "member", FrameKind.FORK)]},
        "run_id",
    ),
    DiagnosticCase(
        recovery.GroupUnsatisfiableResumeError,
        frozenset({"members"}),
        "message",
        "The message identifies each closer/group/member, not merely the number of members.",
        _exercise_group_members,
    ),
    *_text_cases(session.InvalidSessionIdError, {"session_id": "fixed-session"}, "session_id"),
    *_text_cases(session.CorruptSessionFileError, {"session_id": "fixed-session", "reason": "fixed-reason"}, "session_id", "reason"),
    *_text_cases(
        session.StaleSessionVersionError, {"session_id": "fixed-session", "incoming_version": 7, "on_disk_version": 9}, "session_id"
    ),
    _message_case(
        session.StaleSessionVersionError,
        {"session_id": "fixed-session", "on_disk_version": 9},
        "incoming_version",
        ((107, "107"), (211, "211")),
    ),
    _message_case(
        session.StaleSessionVersionError,
        {"session_id": "fixed-session", "incoming_version": 7},
        "on_disk_version",
        ((107, "107"), (211, "211")),
    ),
    *_text_cases(
        session.SessionCheckoutMismatchError,
        {"requested_session_id": "fixed-requested", "active_session_id": "fixed-active"},
        "requested_session_id",
        "active_session_id",
    ),
    *_text_cases(session.SessionNotFoundError, {"session_id": "fixed-session"}, "session_id"),
    *_inherited_cases(),
)
