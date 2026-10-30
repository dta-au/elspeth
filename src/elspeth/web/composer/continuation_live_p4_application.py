"""Join the actual warm writer projection to the existing atomic P4 journal.

The witness has already been consumed by turn_audit. This read verifies that
projection; it never applies that checkpoint a second time or remints a witness.
"""

from __future__ import annotations

from dataclasses import replace

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ComposeFrame, ToolBatchFrame
from elspeth.web.composer.continuation_evidence_application_data import retain_portable_application_on_live_evidence, retained_effects
from elspeth.web.composer.continuation_live_p4 import ContinuationLiveP4
from elspeth.web.composer.continuation_p4_application import HistoricalP4Application, _project_recorder
from elspeth.web.composer.continuation_p4_outcome import decode_audit_outcome_data, encode_audit_outcome_data
from elspeth.web.composer.turn_effects import PersistedTurnEffects
from elspeth.web.sessions.continuation_p4_application_store import StoredP4Application


async def finish_live_p4_application(owner: ContinuationLiveP4, *, effects_before: PersistedTurnEffects) -> StoredP4Application:
    if (
        type(owner) is not ContinuationLiveP4
        or type(effects_before) is not PersistedTurnEffects
        or owner.prepared is None
        or owner.commit is None
        or owner.publication is None
        or owner.publication.audit_outcome is None
        or owner.committed_outcome is None
        or owner.application is not None
    ):
        raise AuditIntegrityError("warm P4 application lost its actual writer/preparation/projection")
    runtime, inventory = owner.runtime, owner.inventory
    evidence, recorder = runtime.live.turn_evidence, runtime.live.recorder
    effects_after = evidence.effects
    committed = owner.committed_outcome
    if committed.audit_outcome is not owner.publication.audit_outcome:
        raise AuditIntegrityError("warm P4 replaced its actual committed publication")
    value = committed.application
    result = value.bundle.result
    record = value.bundle.entry.applications[-1]
    if record.effect_key != owner.commit.effect_key or result.receipt.input_sha256 != owner.commit.input_sha256:
        raise AuditIntegrityError("warm P4 changed its actual committed application/result")
    actual = decode_audit_outcome_data(
        encode_audit_outcome_data(owner.publication.audit_outcome, prepared=owner.prepared, env_ref_names=inventory.names),
        prepared=owner.prepared,
        env_ref_names=inventory.names,
    )
    if actual != result.outcome:
        raise AuditIntegrityError("warm P4 projection replaced its actual physical writer outcome")
    snapshot = recorder.snapshot_for_continuation(drained=runtime.drained)
    following = _project_recorder(snapshot, owner.prepared, result, env_ref_names=inventory.names)
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot

    root = decode_continuation_snapshot(value.snapshot.payload_json)
    compose, frame = root.frames[0], root.frames[-1]
    if type(compose) is not ComposeFrame or type(frame) is not ToolBatchFrame:
        raise AuditIntegrityError("warm P4 application changed its actual phase family")
    application = HistoricalP4Application(
        record.checkpoint_id,
        owner.commit.effect_key,
        owner.data,
        result,
        recorder,
        snapshot,
        following,
        effects_before,
        effects_after,
        owner.publication.effect_verification_failure,
    )
    if (
        evidence.effects is not effects_after
        or recorder.snapshot_for_continuation(drained=runtime.drained) != snapshot
        or retained_effects(effects_after) != value.bundle.evidence_after.snapshot.effects
        or following != value.bundle.recorder_after
        or application.effect_key in evidence._historical_p4_applications
    ):
        raise AuditIntegrityError("warm P4 projection differs from its actual checked SQL application")
    evidence._historical_p4_applications[application.effect_key] = application
    data = retain_portable_application_on_live_evidence(
        owner=evidence,
        recorder=recorder,
        application=application,
        bundle=value.bundle,
        env_ref_names=inventory.names,
    )
    owner.data = data
    owner.application = value
    owner.runtime = replace(
        runtime,
        graph=replace(runtime.graph, batch=data, drained=following.drained),
        drained=following.drained,
    )
    return value
