"""Public member publication and recovery against real local Landscape state."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path

import pytest

from elspeth.contracts import NodeType, TerminalOutcome, TerminalPath
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.sink_effects import (
    SinkEffectAttemptAction,
    SinkEffectAttemptRequest,
    SinkEffectAttemptResult,
    SinkEffectAttemptState,
    SinkEffectCommitResult,
    SinkEffectMemberCandidate,
    SinkEffectPipelineMembersInput,
    SinkEffectReconcileResult,
    SinkEffectState,
)
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.execution.sink_effect_attempt_results import encode_sink_effect_returned_result
from elspeth.core.landscape.execution.sink_effect_finalization import SinkEffectFinalizationMember
from elspeth.core.landscape.execution.sink_effect_identity import resolve_sink_effect_members
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.engine.executors.sink_effects import (
    SinkEffectCoordinator,
    SinkEffectExecutionRequest,
    SinkEffectExecutionSeam,
    SinkEffectUnknownError,
)
from elspeth.engine.orchestrator import prepare_for_run
from elspeth.plugins.llm.model_catalog import read_openrouter_catalog_snapshot_id
from elspeth.plugins.sinks.chroma_sink import ChromaSink
from elspeth.plugins.sinks.dataverse import DataverseSink
from tests.fixtures.landscape import leader_coordination_token, make_factory, register_test_node
from tests.fixtures.stores import MockPayloadStore
from tests.unit.core.landscape.test_sink_effect_reservation import _pipeline_request
from tests.unit.plugins.sinks import test_chroma_sink as chroma
from tests.unit.plugins.sinks import test_dataverse_sink as dataverse


class _Crash(RuntimeError):
    pass


@dataclass
class _Environment:
    factory: RecorderFactory
    token: CoordinationToken
    request: SinkEffectExecutionRequest
    sink: ChromaSink | DataverseSink
    target: chroma._RecoverableChromaCollection | dataverse._RecoverableDataverseClient
    adapter: str

    def set_target(self, state: str, ordinal: int = 0) -> None:
        if self.adapter == "chroma":
            assert isinstance(self.target, chroma._RecoverableChromaCollection)
            if state == "missing":
                self.target.documents.pop(f"d{ordinal + 1}", None)
            else:
                self.target.documents[f"d{ordinal + 1}"] = ("new" if state == "equal" else "old", {"topic": "alpha"})
        else:
            assert isinstance(self.target, dataverse._RecoverableDataverseClient)
            if state == "missing":
                self.target.records.pop(chr(97 + ordinal), None)
            else:
                key = chr(97 + ordinal)
                self.target.records[key] = {"emailaddress1": key, "fullname": "New" if state == "equal" else "Old"}

    def writes(self) -> int:
        if self.adapter == "chroma":
            assert isinstance(self.target, chroma._RecoverableChromaCollection)
            return sum(self.target.upsert_count_by_id.values())
        assert isinstance(self.target, dataverse._RecoverableDataverseClient)
        return sum(self.target.patch_count_by_key.values())

    def assert_desired_target(self, ordinal: int = 0) -> None:
        if self.adapter == "chroma":
            assert isinstance(self.target, chroma._RecoverableChromaCollection)
            assert self.target.documents[f"d{ordinal + 1}"] == ("new", {"topic": "alpha"})
        else:
            assert isinstance(self.target, dataverse._RecoverableDataverseClient)
            key = chr(97 + ordinal)
            assert self.target.records[key] == {"emailaddress1": key, "fullname": "New"}

    def assert_changed_target(self, ordinal: int = 0) -> None:
        if self.adapter == "chroma":
            assert isinstance(self.target, chroma._RecoverableChromaCollection)
            assert self.target.documents[f"d{ordinal + 1}"] == ("old", {"topic": "alpha"})
        else:
            assert isinstance(self.target, dataverse._RecoverableDataverseClient)
            key = chr(97 + ordinal)
            assert self.target.records[key] == {"emailaddress1": key, "fullname": "Old"}

    def execute(self, *, crash_at: SinkEffectExecutionSeam | None = None, wait: bool = False):
        def crash(seam: SinkEffectExecutionSeam) -> None:
            if seam is crash_at:
                raise _Crash(seam.value)

        coordinator = SinkEffectCoordinator(
            factory=self.factory, worker_id=self.token.worker_id, coordination_token=self.token, fault_hook=crash
        )
        return coordinator.execute_with_lease_wait(self.request, self.sink) if wait else coordinator.execute(self.request, self.sink)

    def attempts(self, effect_id: str):
        return self.factory.execution.sink_effects.get_attempts(effect_id)

    def durable_receipt(self) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        assert isinstance(self.request.effect_input, SinkEffectPipelineMembersInput)
        token_ids = [member.token_id for member in self.request.effect_input.members]
        states = self.factory.query.get_node_states_for_tokens(self.token.run_id, token_ids)
        outcomes = self.factory.query.get_token_outcomes_for_tokens(self.token.run_id, token_ids)
        artifacts = self.factory.execution.get_artifacts(self.token.run_id, sink_node_id=self.request.reservation.sink_node_id)
        return (
            tuple(sorted(state.state_id for state in states)),
            tuple(sorted(outcome.outcome_id for outcome in outcomes)),
            tuple(sorted(artifact.artifact_id for artifact in artifacts)),
        )

    def assert_terminal_outcomes(self, expected: tuple[TerminalOutcome, ...]) -> None:
        assert isinstance(self.request.effect_input, SinkEffectPipelineMembersInput)
        token_ids = [member.token_id for member in self.request.effect_input.members]
        outcomes = self.factory.query.get_token_outcomes_for_tokens(self.token.run_id, token_ids)
        assert len(outcomes) == len(token_ids) == len(expected)
        assert {outcome.token_id: (outcome.outcome, outcome.completed) for outcome in outcomes} == {
            token_id: (outcome, True) for token_id, outcome in zip(token_ids, expected, strict=True)
        }


def _subset_request(env: _Environment, ordinals: tuple[int, ...]) -> SinkEffectExecutionRequest:
    assert isinstance(env.request.effect_input, SinkEffectPipelineMembersInput)
    members = tuple(replace(env.request.effect_input.members[old], ordinal=new) for new, old in enumerate(ordinals))
    return SinkEffectExecutionRequest(
        reservation=_pipeline_request(env.token.run_id, env.request.reservation.sink_node_id, members),
        effect_input=SinkEffectPipelineMembersInput(members, members, len(members)),
        finalization_members=tuple(replace(env.request.finalization_members[old], ordinal=new) for new, old in enumerate(ordinals)),
    )


@contextmanager
def _environment(root: Path, adapter: str, *, count: int = 1, divert_last: bool = False) -> Iterator[_Environment]:
    if adapter == "chroma":
        rows = [{"doc_id": f"d{index + 1}", "text": "new", "topic": "alpha"} for index in range(count)]
        if divert_last:
            rows[-1].pop("doc_id")
        target = chroma._RecoverableChromaCollection()
        sink = ChromaSink(chroma._make_config())
        sink._collection = target
    else:
        rows = [{"email": chr(97 + index), "name": "New"} for index in range(count)]
        target = dataverse._RecoverableDataverseClient()
        sink = DataverseSink(dataverse._config())
        sink._client = target
    sink._on_write_failure = "discard"
    with LandscapeDB(f"sqlite:///{root / 'landscape.db'}") as db:
        prepare_for_run()
        catalog_sha, catalog_source = read_openrouter_catalog_snapshot_id()
        factory = make_factory(db, payload_store=MockPayloadStore())
        run = factory.run_lifecycle.begin_run(
            config={}, canonical_version="v1", openrouter_catalog_sha256=catalog_sha, openrouter_catalog_source=catalog_source
        )
        source_id = register_test_node(factory.data_flow, run.run_id, "source", node_type=NodeType.SOURCE, plugin_name="source")
        sink_id = register_test_node(factory.data_flow, run.run_id, "sink", node_type=NodeType.SINK, plugin_name=sink.name)
        token = leader_coordination_token(factory, run.run_id)
        candidates = []
        for ordinal, row_data in enumerate(rows):
            _, durable = factory.data_flow.create_row_with_token(
                source_node_id=source_id,
                row_index=ordinal,
                data=row_data,
                source_row_index=ordinal,
                ingest_sequence=ordinal,
                coordination_token=token,
            )
            factory.execution.begin_node_state(
                token_id=durable.token_id, node_id=sink_id, step_index=0, input_data=row_data, member_token=token.membership
            )
            candidates.append(SinkEffectMemberCandidate(token_id=durable.token_id, row=row_data))
        members = resolve_sink_effect_members(factory, candidates)
        request = SinkEffectExecutionRequest(
            reservation=_pipeline_request(run.run_id, sink_id, members),
            effect_input=SinkEffectPipelineMembersInput(members, members, count),
            finalization_members=tuple(
                SinkEffectFinalizationMember(
                    ordinal=ordinal,
                    output_data=row_data,
                    duration_ms=0.0,
                    outcome=TerminalOutcome.SUCCESS,
                    path=TerminalPath.DEFAULT_FLOW,
                    sink_name="output",
                )
                for ordinal, row_data in enumerate(rows)
            ),
        )
        yield _Environment(factory, token, request, sink, target, adapter)


@pytest.mark.parametrize("adapter", ["chroma", "dataverse"])
@pytest.mark.parametrize("state", ["missing", "equal", "changed"])
def test_fresh_member_upserts_once_without_reconciliation(tmp_path: Path, adapter: str, state: str) -> None:
    with _environment(tmp_path, adapter) as env:
        env.set_target(state)
        result = env.execute(wait=True)
        attempts = env.attempts(result.effect.effect_id)
        assert [attempt.state.value for attempt in attempts if attempt.action.value == "commit"] == ["returned"]
        assert [attempt for attempt in attempts if attempt.action.value == "reconcile"] == []
        assert env.writes() == 1
        env.assert_desired_target()
        assert result.effect.state is SinkEffectState.FINALIZED
        assert [m.member_state for m in env.factory.execution.sink_effects.get_members(result.effect.effect_id)] == [
            SinkEffectState.FINALIZED
        ]
        assert len(result.state_ids) == len(result.outcome_ids) == 1
        assert result.artifact.artifact_id
        receipt = env.durable_receipt()
        assert receipt == (tuple(sorted(result.state_ids)), tuple(sorted(result.outcome_ids)), (result.artifact.artifact_id,))
        env.assert_terminal_outcomes((TerminalOutcome.SUCCESS,))
        again = env.execute()
        assert again.effect.effect_id == result.effect.effect_id
        assert again.artifact.artifact_id == result.artifact.artifact_id
        assert again.state_ids == again.outcome_ids == ()
        assert env.durable_receipt() == receipt
        assert env.attempts(result.effect.effect_id) == attempts
        assert env.writes() == 1


@pytest.mark.parametrize("adapter", ["chroma", "dataverse"])
@pytest.mark.parametrize("initial", ["missing", "equal"])
def test_plan_bound_without_commit_still_permits_first_upsert(tmp_path: Path, adapter: str, initial: str) -> None:
    with _environment(tmp_path, adapter) as env:
        env.set_target(initial)
        with pytest.raises(_Crash):
            env.execute(crash_at=SinkEffectExecutionSeam.AFTER_PLAN_CAS)
        assert env.writes() == 0
        reservation = env.factory.execution.sink_effects.reserve(env.request.reservation, coordination_token=env.token)
        assert reservation.open_effect_ids
        effect = env.factory.execution.sink_effects.get_effect(reservation.open_effect_ids[0])
        assert effect is not None
        members_before = env.factory.execution.sink_effects.get_members(effect.effect_id)
        assert not [a for a in env.attempts(effect.effect_id) if a.action is SinkEffectAttemptAction.COMMIT]
        env.set_target("changed")
        result = env.execute()
        assert result.effect.effect_id == effect.effect_id
        assert result.effect.plan_hash == effect.plan_hash
        assert [m.member_effect_id for m in env.factory.execution.sink_effects.get_members(effect.effect_id)] == [
            m.member_effect_id for m in members_before
        ]
        assert len([a for a in env.attempts(result.effect.effect_id) if a.action.value == "commit"]) == 1
        assert not [a for a in env.attempts(result.effect.effect_id) if a.action is SinkEffectAttemptAction.RECONCILE]
        assert env.writes() == 1
        env.assert_desired_target()


@pytest.mark.parametrize("adapter", ["chroma", "dataverse"])
def test_commit_intent_before_dispatch_never_certifies_freshness(tmp_path: Path, adapter: str) -> None:
    with _environment(tmp_path, adapter) as env:
        with pytest.raises(_Crash):
            env.execute(crash_at=SinkEffectExecutionSeam.BEFORE_EFFECT)
        assert env.writes() == 0
        env.set_target("changed")
        with pytest.raises(SinkEffectUnknownError) as raised:
            env.execute()
        commits = [a for a in env.attempts(raised.value.effect_id) if a.action.value == "commit"]
        assert [a.state.value for a in commits] == ["response_lost"]
        assert [
            (a.action.value, a.state.value) for a in env.attempts(raised.value.effect_id) if a.action is SinkEffectAttemptAction.RECONCILE
        ] == [("reconcile", "returned")]
        env.assert_changed_target()
        assert env.writes() == 0


@pytest.mark.parametrize("adapter", ["chroma", "dataverse"])
@pytest.mark.parametrize("divergent", [False, True])
def test_lost_commit_response_requires_reconciliation_without_duplicate_write(tmp_path: Path, adapter: str, divergent: bool) -> None:
    with _environment(tmp_path, adapter) as env:
        with pytest.raises(_Crash):
            env.execute(crash_at=SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN)
        assert env.writes() == 1
        if divergent:
            env.set_target("changed")
            with pytest.raises(SinkEffectUnknownError) as raised:
                env.execute()
            effect_id = raised.value.effect_id
            env.assert_changed_target()
        else:
            result = env.execute()
            effect_id = result.effect.effect_id
            assert result.effect.state is SinkEffectState.FINALIZED
            env.assert_desired_target()
        attempts = env.attempts(effect_id)
        assert len([a for a in attempts if a.action is SinkEffectAttemptAction.COMMIT]) == 1
        assert [a.state for a in attempts if a.action is SinkEffectAttemptAction.RECONCILE] == [SinkEffectAttemptState.RETURNED]
        assert env.writes() == 1


@pytest.mark.parametrize("adapter", ["chroma", "dataverse"])
def test_returned_commit_with_unfinished_member_is_reused(tmp_path: Path, adapter: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with _environment(tmp_path, adapter) as env:
        effects = env.factory.execution.sink_effects
        original = effects.complete_member_result

        def fail_before_completion(attempt_id, result, *, lease, coordination_token):
            if isinstance(result, SinkEffectCommitResult):
                raise _Crash("before_member_completion")
            return original(attempt_id, result, lease=lease, coordination_token=coordination_token)

        with monkeypatch.context() as patch:
            patch.setattr(effects, "complete_member_result", fail_before_completion)
            with pytest.raises(_Crash, match="before_member_completion"):
                env.execute()
        reservation = effects.reserve(env.request.reservation, coordination_token=env.token)
        effect = effects.get_effect(reservation.open_effect_ids[0])
        assert effect is not None
        commits = [a for a in env.attempts(effect.effect_id) if a.action is SinkEffectAttemptAction.COMMIT]
        assert [a.state for a in commits] == [SinkEffectAttemptState.RETURNED]
        assert effects.get_members(effect.effect_id)[0].member_state is not SinkEffectState.FINALIZED
        assert env.writes() == 1
        result = env.execute()
        assert effects.get_members(effect.effect_id)[0].member_state is SinkEffectState.FINALIZED
        assert [a for a in env.attempts(effect.effect_id) if a.action is SinkEffectAttemptAction.COMMIT] == commits
        assert not [a for a in env.attempts(effect.effect_id) if a.action is SinkEffectAttemptAction.RECONCILE]
        assert result.effect.state is SinkEffectState.FINALIZED
        assert len(result.outcome_ids) == 1
        assert env.writes() == 1


@pytest.mark.parametrize("adapter", ["chroma", "dataverse"])
def test_lost_commit_missing_target_reconciles_before_retry(tmp_path: Path, adapter: str) -> None:
    with _environment(tmp_path, adapter) as env:
        with pytest.raises(_Crash):
            env.execute(crash_at=SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN)
        env.set_target("missing")
        result = env.execute()
        actions = [a.action for a in env.attempts(result.effect.effect_id) if a.member_ordinal == 0]
        assert actions == [SinkEffectAttemptAction.COMMIT, SinkEffectAttemptAction.RECONCILE, SinkEffectAttemptAction.COMMIT]
        assert env.writes() == 2
        env.assert_desired_target()


@pytest.mark.parametrize("adapter", ["chroma", "dataverse"])
def test_mixed_members_reconcile_only_attempted_member(tmp_path: Path, adapter: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with _environment(tmp_path, adapter, count=3) as env:
        original = env.sink.commit_member_effect

        def lose_second(plan, member, effect_input, ctx):
            result = original(plan, member, effect_input, ctx)
            if member.ordinal == 1:
                raise _Crash("second_response_lost")
            return result

        with monkeypatch.context() as patch:
            patch.setattr(env.sink, "commit_member_effect", lose_second)
            with pytest.raises(_Crash, match="second_response_lost"):
                env.execute()
        env.set_target("changed", 2)
        result = env.execute()
        attempts = env.attempts(result.effect.effect_id)
        assert [(a.member_ordinal, a.action.value, a.state.value) for a in attempts if a.member_ordinal is not None] == [
            (0, "commit", "returned"),
            (1, "commit", "response_lost"),
            (1, "reconcile", "returned"),
            (2, "commit", "returned"),
        ]
        assert env.writes() == 3
        assert all(
            m.member_state is SinkEffectState.FINALIZED for m in env.factory.execution.sink_effects.get_members(result.effect.effect_id)
        )
        assert len(result.outcome_ids) == 3
        for ordinal in range(3):
            env.assert_desired_target(ordinal)
        env.assert_terminal_outcomes((TerminalOutcome.SUCCESS,) * 3)
        receipt = env.durable_receipt()
        again = env.execute()
        assert again.artifact.artifact_id == result.artifact.artifact_id
        assert again.state_ids == again.outcome_ids == ()
        assert env.durable_receipt() == receipt
        assert env.attempts(result.effect.effect_id) == attempts
        assert env.writes() == 3


def test_diverted_member_has_no_attempt_while_fresh_sibling_updates(tmp_path: Path) -> None:
    with _environment(tmp_path, "chroma", count=2, divert_last=True) as env:
        env.set_target("changed")
        result = env.execute()
        members = env.factory.execution.sink_effects.get_members(result.effect.effect_id)
        assert [m.prepared_disposition for m in members] == ["accepted", "diverted"]
        assert [(a.member_ordinal, a.action.value) for a in env.attempts(result.effect.effect_id) if a.member_ordinal is not None] == [
            (0, "commit")
        ]
        assert env.writes() == 1
        assert len(result.outcome_ids) == 1
        assert result.effect.state is SinkEffectState.FINALIZED
        assert [m.member_state for m in members] == [SinkEffectState.FINALIZED] * 2
        env.assert_desired_target()
        assert isinstance(env.request.effect_input, SinkEffectPipelineMembersInput)
        accepted, diverted = env.request.effect_input.members
        outcomes = env.factory.query.get_token_outcomes_for_tokens(env.token.run_id, [accepted.token_id, diverted.token_id])
        assert [(outcome.token_id, outcome.outcome, outcome.completed) for outcome in outcomes] == [
            (accepted.token_id, TerminalOutcome.SUCCESS, True)
        ]
        # The coordinator finalizes accepted tokens and returns diversion
        # custody to SinkExecutor, which records the diverted token's fate.
        assert env.factory.data_flow.get_token_outcome(diverted.token_id) is None
        (diversion,) = env.sink._get_diversions()
        assert diversion.row_index == 1
        assert diversion.row_data == {"text": "new", "topic": "alpha"}
        assert diversion.reason == "Chroma effect member is missing required field 'doc_id'"


@pytest.mark.parametrize("adapter", ["chroma", "dataverse"])
def test_overlapping_reservation_uses_stored_member_ordinals(tmp_path: Path, adapter: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with _environment(tmp_path, adapter, count=3) as env:
        complete_request = env.request
        env.request = _subset_request(env, (1, 2))
        original = env.sink.commit_member_effect

        def lose_second(plan, member, effect_input, ctx):
            result = original(plan, member, effect_input, ctx)
            if member.ordinal == 1:
                raise _Crash("overlap_response_lost")
            return result

        with monkeypatch.context() as patch:
            patch.setattr(env.sink, "commit_member_effect", lose_second)
            with pytest.raises(_Crash, match="overlap_response_lost"):
                env.execute()
        old = env.factory.execution.sink_effects.reserve(env.request.reservation, coordination_token=env.token)
        old_id = old.open_effect_ids[0]
        assert isinstance(complete_request.effect_input, SinkEffectPipelineMembersInput)
        stored = env.factory.execution.sink_effects.get_members(old_id)
        assert [(m.ordinal, m.token_id) for m in stored] == [
            (0, complete_request.effect_input.members[1].token_id),
            (1, complete_request.effect_input.members[2].token_id),
        ]
        env.request = complete_request
        env.set_target("changed", 0)
        result = env.execute()
        recovered = env.factory.execution.sink_effects.get_effect(old_id)
        assert recovered is not None and recovered.state is SinkEffectState.FINALIZED
        assert result.effect.state is SinkEffectState.FINALIZED
        effects = env.factory.execution.sink_effects.get_effects_for_run(env.token.run_id)
        assert len(effects) == 2
        assert all(effect.state is SinkEffectState.FINALIZED for effect in effects)
        (new_effect,) = [effect for effect in effects if effect.effect_id != old_id]
        old_members = env.factory.execution.sink_effects.get_members(old_id)
        assert [(m.token_id, m.member_effect_id, m.member_state) for m in old_members] == [
            (m.token_id, m.member_effect_id, SinkEffectState.FINALIZED) for m in stored
        ]
        new_members = env.factory.execution.sink_effects.get_members(new_effect.effect_id)
        assert len(new_members) == 1
        assert new_members[0].token_id == complete_request.effect_input.members[0].token_id
        assert new_members[0].member_effect_id is not None
        assert new_members[0].member_effect_id not in {m.member_effect_id for m in stored}
        assert new_members[0].member_state is SinkEffectState.FINALIZED
        assert [(a.member_ordinal, a.action.value) for a in env.attempts(old_id) if a.member_ordinal is not None] == [
            (0, "commit"),
            (1, "commit"),
            (1, "reconcile"),
        ]
        assert result.effect.effect_id in {old_id, new_effect.effect_id}
        assert [(a.member_ordinal, a.action.value) for a in env.attempts(new_effect.effect_id) if a.member_ordinal is not None] == [
            (0, "commit")
        ]
        assert env.writes() == 3
        for ordinal in range(3):
            env.assert_desired_target(ordinal)
        env.assert_terminal_outcomes((TerminalOutcome.SUCCESS,) * 3)


@pytest.mark.parametrize("adapter", ["chroma", "dataverse"])
@pytest.mark.parametrize("history_variant", ["old_generation", "different_hash", "error", "whole_effect"])
def test_repository_history_variants_still_require_reconciliation(
    tmp_path: Path, adapter: str, history_variant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Substitute every repository history read, not a real persisted takeover/ERROR lifecycle."""
    with _environment(tmp_path, adapter) as env:
        with pytest.raises(_Crash):
            env.execute(crash_at=SinkEffectExecutionSeam.BEFORE_EFFECT)
        env.set_target("changed")
        effects = env.factory.execution.sink_effects
        original = effects.get_attempts
        reservation = effects.reserve(env.request.reservation, coordination_token=env.token)
        effect = effects.get_effect(reservation.open_effect_ids[0])
        assert effect is not None
        before = original(effect.effect_id)
        commits = [a for a in before if a.action is SinkEffectAttemptAction.COMMIT]
        assert len(commits) == 1
        assert commits[0].state is SinkEffectAttemptState.INTENT
        assert commits[0].generation == effect.generation > 0

        def varied_history(effect_id):
            attempts = original(effect_id)
            varied = []
            for attempt in attempts:
                if attempt.action is not SinkEffectAttemptAction.COMMIT:
                    varied.append(attempt)
                elif history_variant == "old_generation":
                    assert attempt.generation > 0
                    varied.append(replace(attempt, generation=0))
                elif history_variant == "different_hash":
                    varied.append(replace(attempt, request_hash="d" * 64))
                elif history_variant == "error":
                    varied.append(replace(attempt, state=SinkEffectAttemptState.ERROR, completed_at=attempt.started_at, latency_ms=0.0))
                else:
                    varied.append(replace(attempt, member_ordinal=None))
            return tuple(varied)

        with monkeypatch.context() as patch:
            patch.setattr(effects, "get_attempts", varied_history)
            with pytest.raises(SinkEffectUnknownError) as raised:
                env.execute()
        attempts = original(raised.value.effect_id)
        assert len([a for a in attempts if a.action is SinkEffectAttemptAction.COMMIT]) == 1
        assert [a.state for a in attempts if a.action is SinkEffectAttemptAction.RECONCILE] == [SinkEffectAttemptState.RETURNED]
        env.assert_changed_target()
        assert env.writes() == 0


@pytest.mark.parametrize("adapter", ["chroma", "dataverse"])
def test_returned_member_finalizes_without_duplicate_write(tmp_path: Path, adapter: str) -> None:
    with _environment(tmp_path, adapter) as env:
        with pytest.raises(_Crash):
            env.execute(crash_at=SinkEffectExecutionSeam.AFTER_RETURN_BEFORE_FINALIZE)
        assert env.writes() == 1
        result = env.execute()
        attempts = env.attempts(result.effect.effect_id)
        assert len([a for a in env.attempts(result.effect.effect_id) if a.action.value == "commit"]) == 1
        env.assert_desired_target()
        assert env.writes() == 1
        receipt = env.durable_receipt()
        again = env.execute()
        assert again.artifact.artifact_id == result.artifact.artifact_id
        assert again.state_ids == again.outcome_ids == ()
        assert env.durable_receipt() == receipt
        assert env.attempts(result.effect.effect_id) == attempts
        assert env.writes() == 1


@pytest.mark.parametrize("adapter", ["chroma", "dataverse"])
@pytest.mark.parametrize("legacy_state", ["unknown", "applied-unfinished", "applied-finalized"])
def test_legacy_reconciliation_without_commit_preserves_first_publication_boundary(tmp_path: Path, adapter: str, legacy_state: str) -> None:
    """Seed an older read-only result through fenced repositories, then execute publicly.

    This is a compatibility state fixture, not a full old/new deployment upgrade.
    """
    with _environment(tmp_path, adapter) as env:
        env.set_target("changed" if legacy_state == "unknown" else "equal")
        with pytest.raises(_Crash):
            env.execute(crash_at=SinkEffectExecutionSeam.AFTER_PLAN_CAS)
        effects = env.factory.execution.sink_effects
        (effect,) = effects.get_effects_for_run(env.token.run_id)
        (member,) = effects.get_members(effect.effect_id)
        assert effect.plan_hash is not None
        lease = effects.acquire_lease(effect.effect_id, owner=env.token.worker_id, ttl=timedelta(minutes=5), coordination_token=env.token)
        attempt = effects.begin_attempt(
            SinkEffectAttemptRequest(
                effect_id=effect.effect_id,
                member_ordinal=member.ordinal,
                generation=lease.generation,
                action=SinkEffectAttemptAction.RECONCILE,
                call_kind=env.sink.effect_call_type,
                request_hash=stable_hash(
                    {
                        "effect_id": effect.effect_id,
                        "member_effect_id": member.member_effect_id,
                        "member_ordinal": member.ordinal,
                        "plan_hash": effect.plan_hash,
                        "schema": "sink-member-effect-reconcile-request-v1",
                    }
                ),
            ),
            coordination_token=env.token,
        )
        if legacy_state == "unknown":
            legacy = SinkEffectReconcileResult.unknown(evidence={"legacy_initial_read": True})
        else:
            # Only decode the stored plan for this fixture's exact descriptor;
            # the publication/recovery path under test is public execute().
            descriptor = SinkEffectCoordinator._load_plan(effect).expected_descriptor
            assert descriptor is not None
            legacy = SinkEffectReconcileResult.applied(descriptor, evidence={"legacy_initial_read": True})
        returned = effects.record_attempt_result(
            SinkEffectAttemptResult(attempt.attempt_id, encode_sink_effect_returned_result(legacy), latency_ms=0.0),
            coordination_token=env.token,
        )
        if legacy_state != "applied-unfinished":
            effects.complete_member_result(attempt.attempt_id, legacy, lease=lease, coordination_token=env.token)
        assert not [a for a in env.attempts(effect.effect_id) if a.action is SinkEffectAttemptAction.COMMIT]
        assert env.writes() == 0

        result = env.execute()
        expected_writes = int(legacy_state != "applied-finalized")
        assert result.effect.effect_id == effect.effect_id
        assert result.effect.state is SinkEffectState.FINALIZED
        assert env.writes() == expected_writes
        assert [a for a in env.attempts(effect.effect_id) if a.action is SinkEffectAttemptAction.RECONCILE] == [returned]
        commits = [a for a in env.attempts(effect.effect_id) if a.action is SinkEffectAttemptAction.COMMIT]
        assert [a.state for a in commits] == [SinkEffectAttemptState.RETURNED] * expected_writes
        env.assert_desired_target()
        env.assert_terminal_outcomes((TerminalOutcome.SUCCESS,))
