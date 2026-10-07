"""UNRUN root-owned proposal. Entry guards only; no SQL/provider/full-flow claim."""

from uuid import UUID

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
    RequiredWorkSource,
)
from elspeth.web.sessions.pipeline_rejection import PipelineRejectionExpected
from elspeth.web.sessions.pipeline_rejection_custody import reject_pipeline_with_required_custody
from elspeth.web.sessions.routes.composer.pipeline_settlement import (
    _settle_pipeline_proposal_under_compose_lock,
    settle_pipeline_proposal_under_compose_lock,
)


class ReachedNextBoundary(Exception):
    """Intentional stop after native nominal admission, before SQL/provider."""


def owned_scope():
    context = SessionOperationContext(
        SessionOperationFence(
            session_id="00000000-0000-4000-8000-000000000001",
            operation_id="00000000-0000-4000-8000-000000000002",
            lease_token="root-native-entry-guard-lease",
            operation_epoch=1,
        ),
        SessionOperationKind.PROPOSAL,
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.MANUAL_PROPOSAL,
            context,
            proposal_id="00000000-0000-4000-8000-000000000003",
            invocation_id="00000000-0000-4000-8000-000000000004",
            tool_call_id="tool-entry-guard",
        )
    )
    return context, coordinator, RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN)


class StopBeforeSQL:
    async def reject_pipeline_composition_proposal_finish_once(self, **kwargs):
        # Actual nominal reserve registration happened. No service/SQL body runs.
        assert kwargs["rejection_work"] is kwargs["coordinator"].tickets[0]
        assert kwargs["rejection_projection_work"] is kwargs["coordinator"].tickets[1]
        raise ReachedNextBoundary("native helper reached service boundary")


class StopAuthority:
    @property
    def row(self):
        # Public owned binding/context validated; stop before argument evaluation
        # can invoke child registration or any actual SQL/provider operation.
        raise ReachedNextBoundary("public binding reached authority boundary")


class StopServices:
    @property
    def session_service(self):
        raise ReachedNextBoundary("no-work public path reached service boundary")


class ForeignCarrier:
    def __init__(self, events, coordinator, context):
        object.__setattr__(self, "_events", events)
        object.__setattr__(self, "_coordinator", coordinator)
        object.__setattr__(self, "_context", context)

    def __getattribute__(self, name):
        events = object.__getattribute__(self, "_events")
        events.append("foreign.read." + name)
        coordinator = object.__getattribute__(self, "_coordinator")
        context = object.__getattribute__(self, "_context")
        if name == "coordinator":
            return coordinator
        if name == "context":
            return context
        if name in ("transition_ordinal", "semantic_ordinal"):
            return 0
        if name == "role":
            return RequiredWorkRole.TURN
        if name == "running":
            return None
        if name == "validate_context":
            return lambda value: events.append("foreign.invoke.validate_context")
        if name == "reserve_pair":

            def reserve(source, projection):
                events.append("foreign.invoke.reserve_pair")
                return coordinator.reserve_pair(source, projection, transition_ordinal=0, semantic_ordinal=0)

            return reserve
        raise ReachedNextBoundary("unexpected foreign read: " + name)


def expected_at_entry(context):
    # Exact production nominal + actual context. Authority is intentionally not
    # valid SQL material: the test stops at service before reading it. This is
    # carrier/context admission evidence only, not full expected-field validity.
    return PipelineRejectionExpected(
        None,
        "operator_rejected",
        None,
        "user:entry-guard",
        "entry-guard",
        context,
        None,
    )


@pytest.mark.asyncio
async def test_native_helper_owned_nominals_stop_at_service():
    context, coordinator, binding = owned_scope()
    with pytest.raises(ReachedNextBoundary, match="service boundary"):
        await reject_pipeline_with_required_custody(StopBeforeSQL(), expected=expected_at_entry(context), binding=binding)
    assert [ticket.key.source for ticket in coordinator.tickets] == [
        RequiredWorkSource.PROPOSAL_REJECTION_SQL,
        RequiredWorkSource.PROPOSAL_REJECTION_PROJECTION,
    ]
    assert all(not ticket.complete for ticket in coordinator.tickets)


@pytest.mark.asyncio
@pytest.mark.parametrize("poison", ["binding", "expected", "both"])
async def test_native_helper_foreign_refused_before_fields(poison):
    context, coordinator, binding = owned_scope()
    events = []
    foreign = ForeignCarrier(events, coordinator, context)
    with pytest.raises(AuditIntegrityError):
        await reject_pipeline_with_required_custody(
            StopBeforeSQL(),
            expected=foreign if poison in ("expected", "both") else expected_at_entry(context),
            binding=foreign if poison in ("binding", "both") else binding,
        )
    assert events == []
    assert coordinator.tickets == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied", [False, True])
async def test_native_public_owned_or_none_stop_before_child(supplied):
    context, coordinator, binding = owned_scope()
    with pytest.raises(ReachedNextBoundary, match="authority boundary"):
        await settle_pipeline_proposal_under_compose_lock(
            services=StopServices(),
            user_id="entry-guard",
            authority=StopAuthority(),
            draft_hash="unused",
            session_operation_context=context,
            commit_timeout_seconds=1,
            required_work=coordinator,
            required_binding=binding if supplied else None,
        )
    assert coordinator.tickets == ()
    assert coordinator._proposal_children == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("with_work", [False, True])
async def test_native_public_foreign_before_effects_or_registration(with_work):
    context, coordinator, _binding = owned_scope()
    events = []
    with pytest.raises(AuditIntegrityError):
        await settle_pipeline_proposal_under_compose_lock(
            services=StopServices(),
            user_id="entry-guard",
            authority=StopAuthority(),
            draft_hash="unused",
            session_operation_context=context,
            commit_timeout_seconds=1,
            required_work=coordinator if with_work else None,
            required_binding=ForeignCarrier(events, coordinator, context),
        )
    assert events == []
    assert coordinator.tickets == ()
    assert coordinator._proposal_children == {}


@pytest.mark.asyncio
async def test_native_public_no_work_none_stops_at_service():
    context, _coordinator, _binding = owned_scope()
    with pytest.raises(ReachedNextBoundary, match="service boundary"):
        await settle_pipeline_proposal_under_compose_lock(
            services=StopServices(),
            user_id="entry-guard",
            authority=StopAuthority(),
            draft_hash="unused",
            session_operation_context=context,
            commit_timeout_seconds=1,
            required_work=None,
            required_binding=None,
        )


@pytest.mark.asyncio
async def test_native_binding_subclass_refused_at_public_entry():
    context, coordinator, _binding = owned_scope()

    class BindingSubclass(RequiredWorkBinding):
        pass

    supplied = BindingSubclass(coordinator, 0, 0, RequiredWorkRole.TURN)
    with pytest.raises(AuditIntegrityError):
        await settle_pipeline_proposal_under_compose_lock(
            services=StopServices(),
            user_id="entry-guard",
            authority=StopAuthority(),
            draft_hash="unused",
            session_operation_context=context,
            commit_timeout_seconds=1,
            required_work=coordinator,
            required_binding=supplied,
        )
    assert coordinator.tickets == ()
    assert coordinator._proposal_children == {}


@pytest.mark.asyncio
async def test_native_expected_subclass_refused_before_context_read():
    context, coordinator, binding = owned_scope()
    events = []

    class ExpectedSubclass(PipelineRejectionExpected):
        def __getattribute__(self, name):
            events.append("expected-subclass.read." + name)
            return super().__getattribute__(name)

    supplied = ExpectedSubclass(None, "operator_rejected", None, "user:entry-guard", "entry-guard", context, None)
    with pytest.raises(AuditIntegrityError):
        await reject_pipeline_with_required_custody(StopBeforeSQL(), expected=supplied, binding=binding)
    assert events == []
    assert coordinator.tickets == ()


@pytest.mark.asyncio
async def test_native_public_no_work_supplied_owned_stops_at_service():
    context, _coordinator, binding = owned_scope()
    with pytest.raises(ReachedNextBoundary, match="service boundary"):
        await settle_pipeline_proposal_under_compose_lock(
            services=StopServices(),
            user_id="entry-guard",
            authority=StopAuthority(),
            draft_hash="unused",
            session_operation_context=context,
            commit_timeout_seconds=1,
            required_work=None,
            required_binding=binding,
        )


class _InnerBindingFieldRead(AssertionError):
    """Mutation-only poison: supplied binding was read without admission."""


class _InnerRowAtEntry:
    # Inert collaborator row, only the two fields read before the inner guard.
    # This is not a durable row or a PipelineProposal authority proof.
    id = UUID("00000000-0000-4000-8000-000000000003")
    tool_call_id = "tool-entry-guard"


class _InnerAuthorityAtEntry:
    def __init__(self, events):
        self.events = events

    @property
    def row(self):
        self.events.append("collaborator.authority.row")
        return _InnerRowAtEntry()

    @property
    def proposal(self):
        self.events.append("collaborator.authority.proposal")
        raise ReachedNextBoundary("direct inner admitted binding before proposal collaborator")


class _InnerServicesAtEntry:
    def __init__(self, events):
        self.events = events

    @property
    def session_service(self):
        self.events.append("collaborator.services.session_service")
        return object()


class _InnerForeignBinding:
    def __init__(self, events):
        object.__setattr__(self, "_events", events)

    def __getattribute__(self, name):
        events = object.__getattribute__(self, "_events")
        events.append("inner-binding.read." + name)
        raise _InnerBindingFieldRead("direct inner supplied binding read: " + name)


class _InnerBindingSubclass(RequiredWorkBinding):
    def __getattribute__(self, name):
        try:
            events = object.__getattribute__(self, "_events")
        except AttributeError:
            # Actual owned constructor/post-init runs before the spy is armed.
            return super().__getattribute__(name)
        events.append("inner-binding.read." + name)
        raise _InnerBindingFieldRead("direct inner supplied binding read: " + name)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["supplied-owned", "implicit-owned", "no-work-none"])
async def test_native_direct_inner_owned_or_none_stops_after_admission(mode):
    context, coordinator, binding = owned_scope()
    events = []
    with pytest.raises(ReachedNextBoundary, match="direct inner admitted binding"):
        await _settle_pipeline_proposal_under_compose_lock(
            services=_InnerServicesAtEntry(events),
            user_id="entry-guard",
            authority=_InnerAuthorityAtEntry(events),
            draft_hash="unused",
            session_operation_context=context,
            commit_timeout_seconds=1,
            required_work=None if mode == "no-work-none" else coordinator,
            required_binding=binding if mode == "supplied-owned" else None,
        )
    assert events == [
        "collaborator.services.session_service",
        "collaborator.authority.row",
        "collaborator.authority.proposal",
    ]
    assert coordinator.tickets == ()
    assert coordinator._proposal_children == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["foreign", "subclass"])
async def test_native_direct_inner_refuses_before_supplied_binding_fields(kind):
    context, coordinator, _binding = owned_scope()
    binding_events = []
    collaborator_events = []
    if kind == "foreign":
        supplied = _InnerForeignBinding(binding_events)
    else:
        supplied = _InnerBindingSubclass(coordinator, 0, 0, RequiredWorkRole.TURN)
        object.__setattr__(supplied, "_events", binding_events)
    original = None
    try:
        await _settle_pipeline_proposal_under_compose_lock(
            services=_InnerServicesAtEntry(collaborator_events),
            user_id="entry-guard",
            authority=_InnerAuthorityAtEntry(collaborator_events),
            draft_hash="unused",
            session_operation_context=context,
            commit_timeout_seconds=1,
            required_work=coordinator,
            required_binding=supplied,
        )
    except BaseException as error:
        original = error
    # Under isolated inner-guard omission the intended wrong result is exactly
    # _InnerBindingFieldRead + ["inner-binding.read.coordinator"], not a fixture
    # constructor failure, public guard, SQL error, or unrelated nonzero exit.
    assert type(original) is AuditIntegrityError, (
        "direct-inner-entry-original",
        type(original).__name__,
        binding_events,
    )
    assert str(original) == "Settlement requires an owned rejection binding"
    assert binding_events == []
    assert collaborator_events == [
        "collaborator.services.session_service",
        "collaborator.authority.row",
    ]
    assert coordinator.tickets == ()
    assert coordinator._proposal_children == {}
