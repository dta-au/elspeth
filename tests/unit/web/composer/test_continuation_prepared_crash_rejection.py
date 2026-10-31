"""Closed prepared crash diagnostics from actual failed Discovery/P4 owners.

Counterfeit values below are retained DATA oppositions after a lawful physical
capture. They do not supply a writer, receipt, native failure or dispatch grant.
"""

import json
from dataclasses import replace
from hashlib import sha256

import pytest

from elspeth.contracts.composer_audit import ComposerToolStatus, PrivateFailureFacts
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_prepared_p4 import (
    _P4_AUTHORINGS,
    _P4_INVOCATIONS,
    _P4_REJECTIONS,
    _dump_owned,
    _p4_evidence_json,
    decode_prepared_p4,
    encode_prepared_p4,
    prepared_p4_members,
)
from elspeth.web.composer.diagnostic_evidence import build_private_tool_evidence, diagnostic_evidence_identity
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.sessions._persist_payload import RejectionRecord
from tests.unit.web.composer.test_applied_component_echo import _empty_state, _run
from tests.unit.web.composer.test_continuation_historical_checkpoint import _actual_p4
from tests.unit.web.composer.test_continuation_prepared_p4 import actual_result_input
from tests.unit.web.sessions.test_continuation_live_phases import _advance
from tests.unit.web.sessions.test_continuation_live_phases import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_tool_failure import _advisor_case, _issued_argument_feedback_case

operation_store = _operation_store


def _rejection(value, rejection):
    return value.model_copy(update={"rejection_records_json": _dump_owned(_P4_REJECTIONS, (rejection,))})


def _source(value, members, invocation, *, authoring=None, refresh_private=False, refresh_failure=False):
    """Alter closed DATA, optionally retaining its real derivative producers."""
    original_authoring = members.authorings[0] if authoring is None else authoring
    invocations = (invocation, *members.invocations[1:])
    authorings = (original_authoring, *members.authorings[1:])
    evidence = members.private_tool_evidence
    failures = members.private_tool_failures
    if refresh_private:
        first = build_private_tool_evidence(
            session_id=value.session_id,
            operation_id=value.creator_operation_id,
            operation_epoch=value.creator_operation_epoch,
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            status=invocation.status.value,
            arguments_canonical=original_authoring.arguments_canonical,
            authority_arguments_canonical=original_authoring.authority_arguments_canonical,
            raw_arguments=original_authoring.raw_arguments,
            result_canonical=invocation.result_canonical,
            error_category=None if invocation.error_category is None else invocation.error_category.value,
        )
        evidence = (first, *evidence[1:])
    if refresh_failure:
        failures = tuple(
            build_private_tool_evidence(
                session_id=value.session_id,
                operation_id=value.creator_operation_id,
                operation_epoch=value.creator_operation_epoch,
                tool_call_id=item.tool_call_id,
                tool_name="composer_failure",
                status="tool_failure",
                arguments_canonical=None,
                result_canonical=item.private_failure.canonical,
                evidence_id=diagnostic_evidence_identity("tool-failure", item.tool_call_id),
            )
            for item in invocations
            if item.private_failure is not None
        )
    return value.model_copy(
        update={
            "source_audit_json": canonical_json(
                {
                    "invocations": json.loads(_dump_owned(_P4_INVOCATIONS, invocations)),
                    "authorings": json.loads(_dump_owned(_P4_AUTHORINGS, authorings)),
                }
            ),
            "private_tool_evidence_json": _p4_evidence_json(evidence),
            "private_tool_failures_json": _p4_evidence_json(failures),
        }
    )


def _actual_crash(phase):
    value = phase.prepared
    assert value is not None and phase.committed_outcome is not None
    members = prepared_p4_members(value, env_ref_names=phase.inventory.names)
    assert len(members.invocations) == len(members.rejection_records) == 1
    invocation, rejection = members.invocations[0], members.rejection_records[0]
    assert invocation.tool_name == rejection.tool_name == "get_pipeline_state"
    assert invocation.status is ComposerToolStatus.PLUGIN_CRASH
    assert invocation.result_canonical is invocation.result_hash is invocation.version_after is None
    assert invocation.private_failure is not None
    assert invocation.error_class == invocation.error_message == rejection.error_code == rejection.message == "RuntimeError"
    assert rejection.planner_payload == json.dumps({"error_class": "RuntimeError", "error_message": "RuntimeError"})
    return value, members, invocation, rejection


@pytest.mark.asyncio
async def test_actual_failed_discovery_prepared_diagnostic_round_trip(operation_store, tmp_path, monkeypatch):
    async with _actual_p4(operation_store, tmp_path, monkeypatch, "unavailable") as (owner, _running, phase, sdk, errors):
        value, members, invocation, rejection = _actual_crash(phase)
        encoded = encode_prepared_p4(value, env_ref_names=phase.inventory.names)
        cold = decode_prepared_p4(encoded, env_ref_names=phase.inventory.names)
        restored = prepared_p4_members(cold, env_ref_names=phase.inventory.names)
        assert cold == value and encode_prepared_p4(cold, env_ref_names=phase.inventory.names) == encoded
        assert restored.invocations == members.invocations and restored.private_tool_failures == members.private_tool_failures
        assert restored.rejection_records == (rejection,)
        assert owner.failure_custody.original is errors[0] and invocation is not owner.failure_custody.invocation
        assert phase.committed_outcome.audit_outcome.unwind_audit_failed and len(sdk) == 1


@pytest.mark.asyncio
async def test_actual_crash_diagnostic_requires_complete_original_source_joins(operation_store, tmp_path, monkeypatch):
    async with _actual_p4(operation_store, tmp_path, monkeypatch, "unavailable") as (_owner, _running, phase, sdk, _errors):
        value, members, invocation, _rejected = _actual_crash(phase)
        changed_authoring = replace(members.authorings[0], raw_arguments='{"changed":true}')
        assert invocation.private_authoring is not None
        foreign_call = replace(
            invocation, tool_call_id="foreign-call", private_authoring=replace(invocation.private_authoring, tool_call_id="foreign-call")
        )
        foreign_tool = replace(
            invocation, tool_name="set_metadata", private_authoring=replace(invocation.private_authoring, tool_name="set_metadata")
        )
        cases = (
            ("call", _source(value, members, foreign_call), "executed cohort"),
            ("tool", _source(value, members, foreign_tool), "another selected tool"),
            ("authoring", _source(value, members, invocation, authoring=changed_authoring), "source audit"),
            ("private", value.model_copy(update={"private_tool_evidence_json": "[]"}), "executed cohort"),
            ("missing_failure", _source(value, members, replace(invocation, private_failure=None)), "private failures"),
            (
                "private_failure",
                _source(value, members, replace(invocation, private_failure=PrivateFailureFacts(canonical_json({"detail": "other"})))),
                "private failures",
            ),
            ("arguments_hash", _source(value, members, replace(invocation, arguments_hash="0" * 64)), "hash"),
            ("result_hash", _source(value, members, replace(invocation, result_hash="0" * 64)), "absent result has a hash"),
            ("class", _source(value, members, replace(invocation, error_class="ValueError")), "crash diagnostic"),
            ("message", _source(value, members, replace(invocation, error_message="different public detail")), "crash diagnostic"),
            ("version", _source(value, members, replace(invocation, version_after=invocation.version_before)), "crash diagnostic"),
            (
                "coherent_missing_failure",
                _source(value, members, replace(invocation, private_failure=None), refresh_failure=True),
                "crash diagnostic",
            ),
            (
                "counterfeit_status",
                _source(value, members, replace(invocation, status=ComposerToolStatus.SUCCESS), refresh_private=True),
                "boolean success flag",
            ),
        )
        for _label, changed, diagnostic in cases:
            with pytest.raises(AuditIntegrityError, match=diagnostic):
                prepared_p4_members(changed, env_ref_names=phase.inventory.names)
        # A present, valid actual Discovery result still cannot describe a
        # thrown dispatch. The ordinary result contract is checked as well.
        lawful, selected, _display, _actual = actual_result_input("get_pipeline_state", {}, _run("get_pipeline_state", {}, _empty_state()))
        assert prepared_p4_members(lawful).invocations[0].result_canonical == selected
        present = replace(invocation, result_canonical=selected, result_hash=sha256(selected.encode()).hexdigest())
        with pytest.raises(AuditIntegrityError, match="crash diagnostic"):
            prepared_p4_members(_source(value, members, present, refresh_private=True), env_ref_names=phase.inventory.names)
        assert len(sdk) == 1


@pytest.mark.asyncio
async def test_actual_crash_diagnostic_has_one_closed_original_payload(operation_store, tmp_path, monkeypatch):
    async with _actual_p4(operation_store, tmp_path, monkeypatch, "unavailable") as (_owner, _running, phase, sdk, _errors):
        value, _members, _invocation, original = _actual_crash(phase)
        payloads = (
            ("malformed", "{"),
            ("duplicate", '{"error_class":"RuntimeError","error_class":"RuntimeError","error_message":"RuntimeError"}'),
            ("extra", json.dumps({"error_class": "RuntimeError", "error_message": "RuntimeError", "extra": None})),
            ("missing", json.dumps({"error_class": "RuntimeError"})),
            ("array", '["RuntimeError","RuntimeError"]'),
            ("type", json.dumps({"error_class": "RuntimeError", "error_message": None})),
            ("different_class", json.dumps({"error_class": "ValueError", "error_message": "RuntimeError"})),
            ("different_message", json.dumps({"error_class": "RuntimeError", "error_message": "other"})),
            ("different_format", canonical_json({"error_class": "RuntimeError", "error_message": "RuntimeError"})),
        )
        for label, payload in payloads:
            changed = _rejection(value, replace(original, planner_payload=payload))
            with pytest.raises(AuditIntegrityError, match="crash diagnostic"):
                prepared_p4_members(changed, env_ref_names=phase.inventory.names)
            assert original.planner_payload != payload, label
        for changed in (
            replace(original, tool_call_id="foreign-call"),
            replace(original, tool_name="set_metadata"),
        ):
            with pytest.raises(AuditIntegrityError, match="unexecuted call"):
                prepared_p4_members(_rejection(value, changed), env_ref_names=phase.inventory.names)
        for changed in (replace(original, error_code="ValueError"), replace(original, message="other")):
            with pytest.raises(AuditIntegrityError, match="crash diagnostic"):
                prepared_p4_members(_rejection(value, changed), env_ref_names=phase.inventory.names)
        duplicate = value.model_copy(update={"rejection_records_json": _dump_owned(_P4_REJECTIONS, (original, original))})
        with pytest.raises(AuditIntegrityError, match="unexecuted call"):
            prepared_p4_members(duplicate, env_ref_names=phase.inventory.names)
        assert len(sdk) == 1


@pytest.mark.asyncio
async def test_actual_crash_diagnostic_preserves_full_credentials_and_late_defects(operation_store, tmp_path, monkeypatch):
    async with _actual_p4(operation_store, tmp_path, monkeypatch, "unavailable") as (_owner, _running, phase, sdk, _errors):
        value, members, invocation, rejection = _actual_crash(phase)
        unsafe = canonical_json({"password": "synthetic-private-material"})
        for changed in (
            value.model_copy(update={"raw_content": unsafe}),
            _source(value, members, replace(invocation, actor=unsafe)),
            _rejection(value, replace(rejection, error_code=unsafe)),
            _rejection(value, replace(rejection, message=unsafe)),
            _rejection(
                value,
                replace(
                    rejection,
                    planner_payload=json.dumps(
                        {
                            "error_class": "RuntimeError",
                            "error_message": "RuntimeError",
                            "lawful_prefix": "x" * 16384,
                            "password": "synthetic-private-material",
                        }
                    ),
                ),
            ),
        ):
            with pytest.raises(CredentialMaterialRefused):
                prepared_p4_members(changed, env_ref_names=phase.inventory.names)
        # Unknown env references remain credential-policy failures even when
        # an earlier large lawful field and a closed diagnostic are present.
        reference = _source(value, members, replace(invocation, actor="${DB_URL}"))
        names = phase.inventory.names | frozenset({"DB_URL"})
        assert prepared_p4_members(reference, env_ref_names=names).invocations[0].actor == "${DB_URL}"
        with pytest.raises(CredentialMaterialRefused):
            prepared_p4_members(reference, env_ref_names=phase.inventory.names - frozenset({"DB_URL"}))
        assert len(sdk) == 1


def test_ordinary_discovery_rejection_still_requires_its_actual_response_contract():
    value, selected, _display, _actual = actual_result_input("get_pipeline_state", {}, _run("get_pipeline_state", {}, _empty_state()))
    members = prepared_p4_members(value)
    invocation = members.invocations[0]
    assert invocation.status is ComposerToolStatus.SUCCESS and invocation.result_canonical == selected
    diagnostic = RejectionRecord(
        invocation.tool_call_id,
        invocation.tool_name,
        "RuntimeError",
        "RuntimeError",
        json.dumps({"error_class": "RuntimeError", "error_message": "RuntimeError"}),
    )
    with pytest.raises(AuditIntegrityError, match="boolean success flag"):
        prepared_p4_members(_rejection(value, diagnostic))


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ("arg_error", "advisor"))
async def test_actual_noncrash_feedback_keeps_its_prepared_source_path(operation_store, tmp_path, monkeypatch, family):
    case = _issued_argument_feedback_case if family == "arg_error" else _advisor_case
    async with case(operation_store, tmp_path, monkeypatch) as values:
        owner = values[0]
        sdk = values[3] if family == "arg_error" else values[2]
        await _advance(owner)
        await _advance(owner)
        phase = owner.p4
        assert phase is not None and phase.prepared is not None and phase.committed_outcome is not None
        members = prepared_p4_members(phase.prepared, env_ref_names=phase.inventory.names)
        expected = ComposerToolStatus.ARG_ERROR if family == "arg_error" else ComposerToolStatus.SUCCESS
        assert members.invocations and all(item.status is expected for item in members.invocations)
        assert all(item.private_failure is None for item in members.invocations)
        assert all(
            item.tool_name == ("get_pipeline_state" if family == "arg_error" else "request_advisor_hint") for item in members.invocations
        )
        assert len(sdk) == (1 if family == "arg_error" else 2)
