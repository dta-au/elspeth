"""State-step DATA reuse preserves the original sequential guard boundaries."""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import MappingProxyType

import pytest

from elspeth.contracts.credential_material import CredentialTraversalPolicy
from elspeth.web import async_workers
from elspeth.web import credential_guard as guard
from elspeth.web.composer.state import CompositionState, SourceSpec
from elspeth.web.continuation_retention_memo import retention_stage_memo_scope
from elspeth.web.required_sql_outcomes import RequiredSQLReturned
from tests.unit.web.test_required_sql_finish_once import publication_ticket


def _state():
    return CompositionState.from_dict(
        {
            "sources": {
                "source": {
                    "plugin": "csv",
                    "options": {"path": "input.csv"},
                    "on_success": "rows",
                    "on_validation_failure": "discard",
                }
            },
            "nodes": [
                {
                    "id": "filter",
                    "node_type": "transform",
                    "plugin": "filter",
                    "input": "rows",
                    "on_success": "output",
                    "on_error": "discard",
                    "options": {"condition": "true"},
                }
            ],
            "outputs": [{"name": "output", "plugin": "csv", "options": {"path": "output.csv"}, "on_write_failure": "discard"}],
            "edges": [],
            "metadata": {"name": "State memo control", "description": ""},
            "version": 1,
        }
    )


@pytest.mark.asyncio
async def test_real_worker_hops_reuse_actual_owned_component_detector_work_and_repeat_policy_resolution(monkeypatch):
    state = _state()
    original = guard.find_credential_material
    original_policy = guard.allowed_secret_ref_fields
    work, policies, requests = [], [], []

    def detect(value, policy):
        work.append((type(value), threading.get_ident()))
        return original(value, policy)

    def policy(component, plugin):
        policies.append((component, plugin))
        return original_policy(component, plugin)

    def inspect_state():
        requests.append(threading.get_ident())
        guard.require_no_credential_material_in_state(state, surface="state-memo-control")

    monkeypatch.setattr(guard, "find_credential_material", detect)
    monkeypatch.setattr(guard, "allowed_secret_ref_fields", policy)
    tickets = [publication_ticket(), publication_ticket()]
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
        with retention_stage_memo_scope() as memo:
            first = await async_workers.run_required_sql_finish_once(tickets[0], inspect_state)
            second = await async_workers.run_required_sql_finish_once(tickets[1], inspect_state)
            assert isinstance(first, RequiredSQLReturned) and isinstance(second, RequiredSQLReturned)
            assert requests[0] == requests[1] != threading.get_ident()
            assert work == [(dict, requests[0])] + [(MappingProxyType, requests[0])] * 3
            assert policies == [("source", "csv"), ("transform", "filter"), ("sink", "csv")] * 2
            assert memo.cached_entry_count == 4
        assert not memo.active and memo.cached_entry_count == 0
    assert all(ticket.complete for ticket in tickets) and async_workers.outstanding_admissions() == 0


def test_state_options_detach_original_backing_and_replaced_options_get_new_actual_work(monkeypatch):
    backing = {"ordinary": "safe"}
    supplied = MappingProxyType(backing)
    owner = SourceSpec(plugin="csv", options=supplied, on_success="output", on_validation_failure="discard")
    assert owner.options is not supplied
    original = guard.find_credential_material
    work = []

    def detect(value, policy):
        work.append(value)
        return original(value, policy)

    monkeypatch.setattr(guard, "find_credential_material", detect)

    def inspect_options(component):
        return guard._retention_state_options_finding(
            component.options, owner=component, policy=CredentialTraversalPolicy(), surface="state-memo-control"
        )

    with retention_stage_memo_scope():
        assert inspect_options(owner) is None
        backing["api_key"] = "literal"
        assert inspect_options(owner) is None and len(work) == 1
        changed = replace(owner, options={"api_key": "literal"})
        finding = inspect_options(changed)
        assert finding is not None and finding.category == "credential_field"
        assert inspect_options(changed) is finding and len(work) == 2
        assert type(work[0]) is MappingProxyType and type(work[1]) is MappingProxyType


def test_state_options_policy_snapshot_changes_recompute_the_actual_detector(monkeypatch):
    owner = SourceSpec(plugin="csv", options={"api_key": "${CURRENT_KEY}"}, on_success="output", on_validation_failure="discard")
    names = set()
    original = guard.find_credential_material
    work = []

    def detect(value, policy):
        work.append(policy.env_ref_names)
        return original(value, policy)

    monkeypatch.setattr(guard, "find_credential_material", detect)
    with retention_stage_memo_scope():

        def inspect_options():
            return guard._retention_state_options_finding(
                owner.options, owner=owner, policy=CredentialTraversalPolicy(env_ref_names=names), surface="state-memo-control"
            )

        first = inspect_options()
        assert first is not None and inspect_options() is first
        names.add("CURRENT_KEY")
        assert inspect_options() is None and inspect_options() is None
    assert work == [frozenset(), frozenset({"CURRENT_KEY"})]


def test_state_options_no_carrier_does_not_build_keys_or_inspect_ownership(monkeypatch):
    state = _state()

    def forbidden(*args, **kwargs):
        raise AssertionError("ordinary state guard must not prepare memo descriptors")

    monkeypatch.setattr(guard, "_credential_scan_key", forbidden)
    monkeypatch.setattr(guard, "_current_owned_options", forbidden)
    guard.require_no_credential_material_in_state(state, surface="state-memo-control")


@pytest.mark.parametrize("owner_kind", ["different-value", "no-owner", "subclass"])
def test_state_options_mismatches_cannot_grant_foreign_proxy_access(owner_kind):
    class DerivedSource(SourceSpec):
        pass

    class ForeignBacking(dict):
        def __len__(self):
            accesses.append("native length")
            raise marker

    marker = RuntimeError("original detector proxy access")
    accesses = []
    component = SourceSpec(plugin="csv", options={}, on_success="output", on_validation_failure="discard")
    foreign = MappingProxyType(ForeignBacking())
    if owner_kind == "subclass":
        component = DerivedSource(plugin="csv", options={}, on_success="output", on_validation_failure="discard")
        value = component.options
    else:
        value = foreign
    owner = None if owner_kind == "no-owner" else component
    with retention_stage_memo_scope() as memo:
        if owner_kind == "subclass":
            for _ in range(2):
                assert (
                    guard._retention_state_options_finding(
                        value, owner=owner, policy=CredentialTraversalPolicy(), surface="state-memo-control"
                    )
                    is None
                )
            assert memo.cached_entry_count == 0
        else:
            with pytest.raises(RuntimeError) as caught:
                guard._retention_state_options_finding(value, owner=owner, policy=CredentialTraversalPolicy(), surface="state-memo-control")
            assert caught.value is marker and accesses == ["native length"]
            assert memo.cached_entry_count == 0


def test_owned_options_opaque_tail_does_not_run_before_the_original_early_finding():
    class ForeignValue:
        def __repr__(self):
            raise AssertionError("opaque tail must not be rendered")

    owner = SourceSpec(
        plugin="csv", options={"api_key": "literal", "later": ForeignValue()}, on_success="output", on_validation_failure="discard"
    )
    with retention_stage_memo_scope() as memo:
        for _ in range(2):
            finding = guard._retention_state_options_finding(
                owner.options, owner=owner, policy=CredentialTraversalPolicy(), surface="state-memo-control"
            )
            assert finding is not None and finding.category == "credential_field"
        assert memo.cached_entry_count == 0


def test_owned_options_tuple_subclass_remains_unsupported_before_iteration():
    deny = False

    class ForeignTuple(tuple):
        def __iter__(self):
            if deny:
                raise AssertionError("unsupported tuple subclass must not be traversed")
            return super().__iter__()

    supplied = ForeignTuple((1, 2))
    owner = SourceSpec(plugin="csv", options=supplied, on_success="output", on_validation_failure="discard")
    assert owner.options is supplied
    deny = True
    with retention_stage_memo_scope() as memo:
        finding = guard._retention_state_options_finding(
            owner.options, owner=owner, policy=CredentialTraversalPolicy(), surface="state-memo-control"
        )
        assert finding is not None and finding.category == "unsupported_value"
        assert memo.cached_entry_count == 0


def test_owned_options_later_opaque_type_does_not_gain_an_equality_callback():
    accesses = []

    class ForeignMeta(type):
        __hash__ = type.__hash__

        def __eq__(self, other):
            accesses.append("class equality")
            return type.__eq__(self, other)

    class ForeignValue(metaclass=ForeignMeta):
        pass

    owner = SourceSpec(
        plugin="csv", options={"api_key": "literal", "later": ForeignValue()}, on_success="output", on_validation_failure="discard"
    )
    accesses.clear()
    with retention_stage_memo_scope():
        finding = guard._retention_state_options_finding(
            owner.options, owner=owner, policy=CredentialTraversalPolicy(), surface="state-memo-control"
        )
        assert finding is not None and finding.category == "credential_field"
    assert accesses == []
