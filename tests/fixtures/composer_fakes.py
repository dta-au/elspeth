"""Delayed composer seam for real app/worker/transport proofs."""

from __future__ import annotations

import asyncio
from threading import Event

from elspeth.contracts.composer_progress import ComposerProgressEvent, ComposerProgressSink
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer.protocol import ComposerHistoryMessage, ComposerResult
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.state import CompositionState
from elspeth.web.execution.completion_gates import CompletionGateFacts
from elspeth.web.required_work import RequiredWorkBinding


class DelayedComposerFake:
    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self.calls = 0
        self.messages: list[str] = []
        self.history: list[list[ComposerHistoryMessage]] = []

    async def compose(
        self,
        message: str,
        messages: list[ComposerHistoryMessage],
        state: CompositionState,
        session_id: str | None = None,
        current_state_id: str | None = None,
        user_id: str | None = None,
        progress: ComposerProgressSink | None = None,
        user_message_id: str | None = None,
        session_operation_context: SessionOperationContext | None = None,
        completion_gates: CompletionGateFacts | None = None,
        budget_seconds: float | None = None,
        required_work: RequiredWorkBinding | None = None,
        provider_owner: ProviderInvocationOwner | None = None,
    ) -> ComposerResult:
        self.calls += 1
        self.messages.append(message)
        self.history.append(messages)
        self.entered.set()
        if progress is not None:
            await progress(
                ComposerProgressEvent(
                    phase="calling_model",
                    headline="The composer is preparing the response.",
                    evidence=("The delayed test provider started.",),
                    likely_next="Wait for the completed response.",
                )
            )
        while not self.release.is_set():
            await asyncio.sleep(0.02)
        return ComposerResult(message="Completed delayed response.", state=state)


class StaticPluginSnapshotFactory:
    """Deterministic test policy with explicit principal-ID resolution."""

    def __init__(self, snapshot) -> None:
        self.snapshot = snapshot

    def __call__(self, user: UserIdentity | str):
        if isinstance(user, str):
            return self.for_user_id(user)
        return self.for_user_id(user.user_id)

    def for_user_id(self, user_id: str):
        from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

        return PluginAvailabilitySnapshot.create(
            policy_hash=self.snapshot.policy_hash,
            principal_scope=f"local:{user_id}",
            available=self.snapshot.available,
            unavailable=self.snapshot.unavailable,
            selected=self.snapshot.selected,
            usable_profile_aliases=self.snapshot.usable_profile_aliases,
            selected_profile_aliases=self.snapshot.selected_profile_aliases,
            binding_generation_fingerprint=self.snapshot.binding_generation_fingerprint,
        )


def install_restricted_plugin_policy(app, *hidden):
    from unittest.mock import MagicMock

    from elspeth.web.dependencies import create_catalog_service
    from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
    from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry
    from tests.fixtures.audit_hashing import fake_sha256

    catalog = create_catalog_service()
    unrestricted = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    snapshot = PluginAvailabilitySnapshot.create(
        policy_hash=fake_sha256("session-route-policy"),
        principal_scope="local:alice",
        available=unrestricted.available - set(hidden),
        unavailable=(),
        selected=unrestricted.selected,
        usable_profile_aliases=(),
        selected_profile_aliases=(),
        binding_generation_fingerprint=fake_sha256("session-route-policy-generation"),
    )
    app.state.catalog_service = catalog
    profiles = MagicMock(spec=OperatorProfileRegistry)
    profiles.public_schema.side_effect = lambda _plugin_id, full_schema, *, available_aliases: full_schema
    app.state.operator_profile_registry = profiles
    app.state.plugin_snapshot_factory = StaticPluginSnapshotFactory(snapshot)
    return snapshot


def interpretation_surfacing_stub():
    from unittest.mock import create_autospec

    from elspeth.web.composer.interpretation_surfacing import InterpretationSurfacing

    return create_autospec(InterpretationSurfacing, instance=True)
