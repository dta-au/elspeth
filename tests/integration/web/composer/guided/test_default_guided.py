"""Guided endpoints initialize an independent wizard checkpoint.

Freeform endpoints use a separate initializer without guided metadata;
persisting a latent wizard there would incorrectly change the reload mode.
"""

from __future__ import annotations

from elspeth.web.composer.guided.protocol import GuidedStep


def test_helper_attaches_initial_guided_session() -> None:
    """The factory helper returns CompositionState with GuidedSession.initial()."""
    from elspeth.web.sessions.routes import _initial_composition_state_with_guided_session

    state = _initial_composition_state_with_guided_session()

    assert state.sources.get("source") is None
    assert state.nodes == ()
    assert state.edges == ()
    assert state.outputs == ()
    assert state.version == 1
    assert state.guided_session is not None
    assert state.guided_session.step is GuidedStep.STEP_1_SOURCE
    assert state.guided_session.terminal is None
    assert state.guided_session.history == ()


def test_helper_is_deterministic() -> None:
    """Two calls produce equal (but distinct) GuidedSession instances."""
    from elspeth.web.sessions.routes import _initial_composition_state_with_guided_session

    a = _initial_composition_state_with_guided_session()
    b = _initial_composition_state_with_guided_session()

    # Equal: same shape, same step
    assert a.guided_session == b.guided_session
    # Distinct CompositionState instances (no shared state)
    assert a is not b
