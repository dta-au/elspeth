"""Tests for current per-step guided chat skills and sample redaction."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from elspeth.web.composer.guided.prompts import (
    _summarize_sample_row,
    load_step_chat_skill,
    load_step_planner_skill,
)
from elspeth.web.composer.guided.protocol import GuidedStep


@pytest.mark.parametrize("step", list(GuidedStep))
def test_each_step_chat_skill_loads_with_base_preamble(step: GuidedStep) -> None:
    text = load_step_chat_skill(step)

    assert "# Guided Pipeline Composer" in text
    assert "invent" in text.lower() or "anti-fabrication" in text.lower()


def test_step_chat_skills_are_scoped() -> None:
    markers = {
        GuidedStep.STEP_1_SOURCE: "### Step 1 — Source",
        GuidedStep.STEP_2_SINK: "### This stage: the output",
        GuidedStep.STEP_3_TRANSFORMS: "### This stage: the transforms",
        GuidedStep.STEP_4_WIRE: "### Step 4 — Wiring constraints",
    }
    for step, marker in markers.items():
        text = load_step_chat_skill(step)
        assert marker in text
        assert all(other_marker not in text for other_step, other_marker in markers.items() if other_step is not step)


@pytest.mark.parametrize("routing", ["quarantine", "discard"])
def test_step_1_fixed_schema_guidance_matches_source_row_rejection(tmp_path: Path, routing: str) -> None:
    from elspeth.plugins.infrastructure.config_base import PluginConfigError
    from elspeth.plugins.sources.csv_source import CSVSource
    from tests.fixtures.factories import make_source_context

    path = tmp_path / "extra-column.csv"
    path.write_text("id,extra\na,kept\n")
    fixed = CSVSource({"path": str(path), "schema": {"mode": "fixed", "fields": ["id: str"]}, "on_validation_failure": routing})
    rejected = list(fixed.load(make_source_context(plugin_name="csv")))
    if routing == "quarantine":
        assert len(rejected) == 1
        assert rejected[0].is_quarantined
        assert rejected[0].quarantine_destination == "quarantine"
        assert rejected[0].row == {"id": "a", "extra": "kept"}
    else:
        assert rejected == []

    observed = CSVSource({"path": str(path), "schema": {"mode": "observed"}, "on_validation_failure": routing})
    accepted = list(observed.load(make_source_context(plugin_name="csv")))
    assert len(accepted) == 1
    assert not accepted[0].is_quarantined
    assert accepted[0].row == {"id": "a", "extra": "kept"}
    with pytest.raises(PluginConfigError, match="on_validation_failure"):
        CSVSource({"path": str(path), "schema": {"mode": "observed"}})

    assistance = CSVSource.get_agent_assistance()
    assert assistance is not None
    hints = " ".join(assistance.composer_hints)
    assert "Set on_validation_failure deliberately" in hints
    assert "Raw CSV config requires it" in hints
    assert "silently drops rows" not in hints
    assert "Default is 'discard'" not in hints
    post_call_hints = CSVSource.get_post_call_hints(
        tool_name="set_source", config_snapshot={"schema": {"mode": "fixed"}, "on_validation_failure": routing}
    )
    assert len(post_call_hints) == 1
    assert "rejects nonconforming rows" in post_call_hints[0]
    assert "quarantine" in post_call_hints[0] and "discard" in post_call_hints[0]
    assert "drops every row" not in post_call_hints[0]
    assert CSVSource.get_post_call_hints(tool_name="set_source", config_snapshot={"schema": {"mode": "observed"}}) == ()

    for overlay in (load_step_chat_skill(GuidedStep.STEP_1_SOURCE), load_step_planner_skill(GuidedStep.STEP_1_SOURCE)):
        text = " ".join(overlay.split())
        assert "fixed` rejects rows with unexpected fields" in text
        assert "on_validation_failure" in text
        assert "quarantine" in text and "discard" in text
        assert "silently *drops*" not in text


def test_sample_row_projection_redacts_values() -> None:
    projection = _summarize_sample_row(
        {
            "email": "person@example.test",
            "api_key": "sk-test-secret-row-value",
            "profile_url": "https://example.test/private?token=secret",
            "note": "customer asked for refunds",
        }
    )

    rendered = repr(projection)
    assert "person@example.test" not in rendered
    assert "sk-test-secret-row-value" not in rendered
    assert "https://example.test/private" not in rendered
    assert "customer asked for refunds" not in rendered
    assert set(projection.values()) == {
        "<sample:email-like>",
        "<sample:secret-like>",
        "<sample:url>",
        "<sample:string:26-chars>",
    }
    assert tuple(projection) == ("field_1", "field_2", "field_3", "field_4")


def test_sample_row_projection_aliases_are_disjoint_from_raw_labels() -> None:
    projection = _summarize_sample_row({"field_2": "alpha", "customer": "beta"})

    assert tuple(projection) == ("field_1", "field_3")


class _CountingAliasMapping(Mapping[str, str]):
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values
        self.iteration_count = 0

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        self.iteration_count += 1
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def test_sample_row_projection_uses_read_only_alias_lookup_and_omits_missing_labels() -> None:
    aliases = _CountingAliasMapping({"known": "field_1"})

    projection = _summarize_sample_row(
        {"known": "person@example.test", "MISSING_IGNORE_SYSTEM": "raw sample"},
        field_aliases=aliases,
    )

    assert projection == {"field_1": "<sample:email-like>"}
    assert aliases.iteration_count == 0
    assert "MISSING_IGNORE_SYSTEM" not in repr(projection)


def test_step_3_skill_keeps_fail_closed_mapping_direction_rules() -> None:
    text = load_step_chat_skill(GuidedStep.STEP_3_TRANSFORMS)

    assert "source side" in text
    assert "mapping keys" in text
    assert "immediate-upstream fields" in text
    assert "target side" in text
    assert "mapping values" in text
    assert "emitted downstream fields" in text
    assert "never reverse" in text
    assert "Required downstream fields belong in output targets" in text
    assert "unproven source" in text


def test_step_3_skill_reads_an_outcome_goal_as_a_request_for_processing() -> None:
    """Pin both halves of the goal-first stage-timing clause (B-2.1/2.2).

    Since the goal-first change the step-2 finish runs the planner ONCE, from
    the session's opening goal: one sentence naming what should come out the
    other end. Rule 1 of the stage-timing section decides whether that intent
    needs transforms at all, so it is the pivot of the whole change, and it can
    fail in two directions. Without the first half the planner can classify a
    real outcome goal as "the user asked for no processing" and hand back an
    empty pass-through; without the second half — and without rule 1's original
    three-part conjunction, which this clause must leave intact — it can add
    steps to a goal that genuinely asked for none.

    The skill is model-facing prose whose line breaks are wrapping, not
    meaning, so the pins run over whitespace-normalized text: a future re-wrap
    must not turn this tripwire red, and must not be able to silence it either.
    Both overlays are checked because the chat solver reads the step skill and
    the planner reads it rendered with the capability core.
    """
    for overlay in (
        load_step_chat_skill(GuidedStep.STEP_3_TRANSFORMS),
        load_step_planner_skill(GuidedStep.STEP_3_TRANSFORMS),
    ):
        text = " ".join(overlay.split())

        # An outcome goal counts as asking for processing.
        assert "When the intent is an outcome goal" in text
        assert "read it as asking for processing" in text
        # ... and the empty set is still reachable, anchored on the user's own words.
        assert "Such a goal asks for no processing only when what it names is the source's own rows" in text
        # The three-part condition the clause qualifies survives verbatim.
        assert (
            "When the user asked for no processing, no deferred intents are pending, "
            "and the server named no `unproducible_output_fields`, "
            "the correct transform set is EMPTY" in text
        )


def test_step_4_skill_has_no_fixed_linear_topology_contract() -> None:
    text = load_step_chat_skill(GuidedStep.STEP_4_WIRE)

    assert "Exact reviewed graph" in text
    assert "chain_in" not in text
    assert "chain_{k}" not in text
    assert 'emits `"main"`' not in text
