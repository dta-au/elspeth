"""Pins for the server-projected reviewed-component ledger (elspeth-f2a8550b3d).

``GuidedSessionResponse.reviewed_components`` replaced a client-side fold over
``next_turn`` (the frontend's only memory of what had been settled, empty after
any reload and on every completed session). Two properties make the projection
safe to build decision sheets on, and each has its own pin here:

* it is a REDACTION boundary — the closed field set is exactly what the
  ``review_components`` card already publishes, so no reviewed option value,
  inspected sample, storage path or content anchor rides along; and
* it cannot disagree with that card — both project
  :func:`reviewed_component_ledger`, so there is one derivation of "which
  components are settled, in what order, under which plugin".

The field-set pin and the value-leak pin catch different failures: adding
``on_write_failure: "discard"`` to the model would slip past a bytes assertion
(no fixture carries "discard" as a secret) but fails the field-set pin, while a
projector that started echoing a whole ``SourceResolved`` fails both.
"""

from __future__ import annotations

import json
from typing import Literal

import pytest

from elspeth.web.composer.guided import emitters
from elspeth.web.composer.guided.emitters import build_component_review_turn
from elspeth.web.composer.guided.errors import InvariantError
from elspeth.web.composer.guided.protocol import GuidedStep
from elspeth.web.composer.guided.resolved import SinkOutputResolved, SourceResolved
from elspeth.web.composer.guided.state_machine import (
    GuidedSession,
    ReviewedComponentEntry,
    SinkIntent,
    SourceIntent,
    TerminalKind,
    TerminalState,
    reviewed_component_ledger,
)
from elspeth.web.sessions.guided_replay import project_reviewed_components
from elspeth.web.sessions.schemas import GuidedReviewedComponentResponse, GuidedReviewedComponentsResponse

FIRST_SOURCE_ID = "11111111-1111-4111-8111-111111111111"
SECOND_SOURCE_ID = "22222222-2222-4222-8222-222222222222"
PENDING_SOURCE_ID = "44444444-4444-4444-8444-444444444444"
OUTPUT_ID = "33333333-3333-4333-8333-333333333333"
PENDING_OUTPUT_ID = "55555555-5555-4555-8555-555555555555"
# Identity carried by NO fixture component, so a card that re-derived its items
# from reviewed custody could not produce it.
SENTINEL_ID = "66666666-6666-4666-8666-666666666666"

# Values a reviewed component legitimately carries in custody and which must
# never reach the top-level wire ledger. Every one is a real shape: a resolved
# blob path, an operator-typed credential knob, inspected row content, and the
# content-identity anchor that revokes stale reviewed authority.
PRIVATE_SOURCE_PATH = "/srv/elspeth/data/blobs/s1/50f5b3e9-f52f-4c5f-98df-a20ec7b2627b_colours.csv"
PRIVATE_OUTPUT_PATH = "/srv/elspeth/exports/finance/q3-actuals.json"
PRIVATE_TOKEN = "sk-live-2f9c41d8e7b3a05f"  # secret-scan: allow-this-line
PRIVATE_SAMPLE_VALUE = "Ada Lovelace, 22 Maida Vale"
PRIVATE_ANCHOR = "9f8e7d6c5b4a3928"
PRIVATE_OBSERVED_COLUMN = "employee_home_address"


def _source(name: str, *, plugin: str = "csv") -> SourceResolved:
    return SourceResolved(
        name=name,
        plugin=plugin,
        options={"path": PRIVATE_SOURCE_PATH, "api_token": PRIVATE_TOKEN, "schema": {"mode": "observed"}},
        observed_columns=("colour_name", PRIVATE_OBSERVED_COLUMN),
        sample_rows=({"colour_name": "red", PRIVATE_OBSERVED_COLUMN: PRIVATE_SAMPLE_VALUE},),
        on_validation_failure="discard",
        content_hash_prefix=PRIVATE_ANCHOR,
    )


def _output(name: str, *, plugin: str = "json") -> SinkOutputResolved:
    return SinkOutputResolved(
        name=name,
        plugin=plugin,
        options={"path": PRIVATE_OUTPUT_PATH, "api_token": PRIVATE_TOKEN},
        required_fields=(PRIVATE_OBSERVED_COLUMN,),
        schema_mode="observed",
        on_write_failure="discard",
    )


def _reviewed_both_kinds(*, step: GuidedStep = GuidedStep.STEP_2_SINK) -> GuidedSession:
    """Two reviewed sources (authored second-first) and one reviewed output."""

    return GuidedSession(
        step=step,
        source_order=(SECOND_SOURCE_ID, FIRST_SOURCE_ID),
        reviewed_sources={
            FIRST_SOURCE_ID: _source("colours"),
            SECOND_SOURCE_ID: _source("orders", plugin="jsonl"),
        },
        output_order=(OUTPUT_ID,),
        reviewed_outputs={OUTPUT_ID: _output("report")},
    )


def _ledger_json(guided: GuidedSession) -> str:
    """The projected ledger's own bytes.

    Deliberately NOT the whole response: the schema-8 checkpoint nested under
    ``composition_state.composer_meta.guided_session`` carries the reviewed
    options in full by design (see ``test_respond.py::_full_guided_session``),
    so a whole-body assertion would be testing the wrong subtree.
    """

    return json.dumps(project_reviewed_components(guided).model_dump(mode="json"), sort_keys=True)


class TestClosedFieldSet:
    def test_reviewed_component_item_publishes_identity_and_nothing_else(self) -> None:
        """The future-field guard.

        A new field on this model is a new egress from reviewed custody. It is
        allowed — deliberately, with the redaction argument made in the review
        — but it cannot arrive as a quiet convenience: this pin names the
        closed set, so widening the model reddens the suite first.
        """
        assert set(GuidedReviewedComponentResponse.model_fields) == {"stable_id", "name", "plugin", "status"}

    def test_ledger_carries_exactly_the_two_component_kinds(self) -> None:
        assert set(GuidedReviewedComponentsResponse.model_fields) == {"sources", "outputs"}

    def test_status_is_the_constant_the_review_card_publishes(self) -> None:
        with pytest.raises(ValueError):
            GuidedReviewedComponentResponse(
                stable_id=FIRST_SOURCE_ID,
                name="colours",
                plugin="csv",
                status="pending",  # type: ignore[arg-type]
            )


class TestProjection:
    def test_projects_both_kinds_in_authored_order(self) -> None:
        ledger = project_reviewed_components(_reviewed_both_kinds())

        assert [item.name for item in ledger.sources] == ["orders", "colours"]
        assert [item.stable_id for item in ledger.sources] == [SECOND_SOURCE_ID, FIRST_SOURCE_ID]
        assert [item.plugin for item in ledger.sources] == ["jsonl", "csv"]
        assert [(item.name, item.plugin, item.status) for item in ledger.outputs] == [("report", "json", "reviewed")]

    def test_omits_a_pending_component_of_either_kind(self) -> None:
        """A pending component is absent, never half-named.

        The sheets say "decided"; a component still being configured has no
        settled decision to show, and its intent carries no plugin at all in
        the ``plugin_selection`` phase.
        """
        guided = GuidedSession(
            step=GuidedStep.STEP_1_SOURCE,
            source_order=(FIRST_SOURCE_ID, PENDING_SOURCE_ID),
            reviewed_sources={FIRST_SOURCE_ID: _source("colours")},
            pending_source_intents={
                PENDING_SOURCE_ID: SourceIntent(
                    name="orders",
                    phase="plugin_selection",
                    plugin=None,
                    options=None,
                    inspection_facts=None,
                    observed_columns=(),
                    sample_rows=(),
                )
            },
        )

        ledger = project_reviewed_components(guided)

        assert [item.name for item in ledger.sources] == ["colours"]
        assert ledger.outputs == []

    def test_omits_a_pending_output_beside_reviewed_outputs(self) -> None:
        guided = GuidedSession(
            step=GuidedStep.STEP_2_SINK,
            source_order=(FIRST_SOURCE_ID,),
            reviewed_sources={FIRST_SOURCE_ID: _source("colours")},
            output_order=(OUTPUT_ID, PENDING_OUTPUT_ID),
            reviewed_outputs={OUTPUT_ID: _output("report")},
            pending_output_intents={PENDING_OUTPUT_ID: SinkIntent(name="ledger", phase="plugin_options", plugin="csv", options=None)},
        )

        ledger = project_reviewed_components(guided)

        assert [item.name for item in ledger.sources] == ["colours"]
        assert [item.name for item in ledger.outputs] == ["report"]

    def test_an_empty_session_projects_two_empty_lists(self) -> None:
        ledger = project_reviewed_components(GuidedSession(step=GuidedStep.STEP_1_SOURCE))

        assert ledger.sources == []
        assert ledger.outputs == []

    def test_a_completed_session_still_names_what_it_committed(self) -> None:
        """The reason this field exists at all.

        A completed guided session has no ``next_turn``, so the retired
        client-side fold could never populate a ledger for it — and the
        graduation surface (``TutorialTurn7Graduation``) selects exactly that
        session. Commit clears ``active_proposal`` / ``active_edit_target``
        and nothing else, so reviewed custody survives and the read-only
        sheets have rows to show.
        """
        completed = GuidedSession(
            step=GuidedStep.STEP_4_WIRE,
            source_order=(FIRST_SOURCE_ID,),
            reviewed_sources={FIRST_SOURCE_ID: _source("colours")},
            output_order=(OUTPUT_ID,),
            reviewed_outputs={OUTPUT_ID: _output("report")},
            terminal=TerminalState(kind=TerminalKind.COMPLETED, reason=None, pipeline_yaml="sources: {}\n"),
        )

        ledger = project_reviewed_components(completed)

        assert [item.name for item in ledger.sources] == ["colours"]
        assert [item.name for item in ledger.outputs] == ["report"]


class TestRedactionBoundary:
    def test_no_reviewed_option_sample_path_or_anchor_reaches_the_ledger(self) -> None:
        """Adversarial: every private value a reviewed component carries.

        A path, an operator-typed credential knob, an inspected column name,
        an inspected cell value, and the content-identity anchor — all present
        on the session, none admissible on the top-level wire.
        """
        body = _ledger_json(_reviewed_both_kinds())

        for secret in (
            PRIVATE_SOURCE_PATH,
            PRIVATE_OUTPUT_PATH,
            PRIVATE_TOKEN,
            PRIVATE_SAMPLE_VALUE,
            PRIVATE_ANCHOR,
            PRIVATE_OBSERVED_COLUMN,
        ):
            assert secret not in body, f"reviewed_components leaked {secret!r}"

    def test_the_ledger_publishes_no_authored_knob_values(self) -> None:
        """``on_validation_failure`` / ``on_write_failure`` / ``schema.mode``.

        All three are schema-form knobs (``emitters._with_write_failure_knob``
        appends ``on_write_failure`` as a ``KnobField``), i.e. authored option
        values, not review-card facts. They are on the session; they must not
        be on the wire.
        """
        payload = project_reviewed_components(_reviewed_both_kinds()).model_dump(mode="json")

        for item in (*payload["sources"], *payload["outputs"]):
            assert set(item) == {"stable_id", "name", "plugin", "status"}


class TestOneDerivation:
    def test_the_review_card_projects_the_ledger_rather_than_re_deriving_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The CALL is the pin, because a value comparison cannot be one.

        ``build_component_review_turn`` rejects any session whose ``order``
        and ``reviewed`` keys differ, so on every session it accepts, a naive
        inline ``{"name": reviewed[stable_id].name, ...} for stable_id in
        order`` and :func:`reviewed_component_ledger` agree by construction:
        no fixture can discriminate them. That was measured, not assumed —
        the value-equality assertion this replaces passed with the production
        line reverted to the inline form.

        So the ledger is substituted and the card is asked what it published.
        Re-inlining the derivation reddens this; changing the ledger's order,
        naming, or plugin resolution moves the card with it.
        """
        guided = _reviewed_both_kinds(step=GuidedStep.STEP_1_SOURCE)
        substitute = (ReviewedComponentEntry(stable_id=SENTINEL_ID, name="ledger-authored", plugin="ledger_plugin"),)
        calls: list[tuple[GuidedSession, str]] = []

        def _ledger(session: GuidedSession, kind: Literal["source", "output"]) -> tuple[ReviewedComponentEntry, ...]:
            calls.append((session, kind))
            return substitute

        monkeypatch.setattr(emitters, "reviewed_component_ledger", _ledger)

        card = build_component_review_turn(guided, "source")

        assert calls == [(guided, "source")]
        assert card["payload"]["items"] == [
            {"stable_id": SENTINEL_ID, "name": "ledger-authored", "plugin": "ledger_plugin", "status": "reviewed"}
        ]

    def test_the_card_and_the_wire_ledger_name_the_same_components(self) -> None:
        """What the value comparison honestly pins: the two surfaces agree.

        Weaker than the call pin above and kept for a different failure —
        a projection that dropped ``status``, re-sorted the entries, or
        resolved a display name differently on its way to
        ``GuidedReviewedComponentResponse`` would still call the ledger and
        still fail here.
        """
        guided = _reviewed_both_kinds(step=GuidedStep.STEP_1_SOURCE)

        card = build_component_review_turn(guided, "source")
        wire = project_reviewed_components(guided)

        assert card["payload"]["items"] == [
            {"stable_id": item.stable_id, "name": item.name, "plugin": item.plugin, "status": item.status} for item in wire.sources
        ]

    def test_the_ledger_refuses_an_unknown_component_kind(self) -> None:
        with pytest.raises(InvariantError, match="kind must be"):
            reviewed_component_ledger(_reviewed_both_kinds(), "node")  # type: ignore[arg-type]

    def test_the_ledger_refuses_anything_but_an_exact_guided_session(self) -> None:
        """Validate by trust domain: an owned type checked nominally.

        The ledger reads reviewed custody; a look-alike carrying attacker-set
        ``reviewed_sources`` is not a GuidedSession and gets no projection.
        """

        class NotAGuidedSession:
            def __init__(self) -> None:
                self.source_order = (FIRST_SOURCE_ID,)
                self.reviewed_sources = {FIRST_SOURCE_ID: _source("impostor")}
                self.output_order: tuple[str, ...] = ()
                self.reviewed_outputs: dict[str, SinkOutputResolved] = {}

        with pytest.raises(TypeError, match="exact GuidedSession"):
            reviewed_component_ledger(NotAGuidedSession(), "source")  # type: ignore[arg-type]
