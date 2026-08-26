"""Offline repro: why every retain_deferred_intent from the collector prompt is rejected."""

from __future__ import annotations

from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.guided.deferred_intents import (
    DeferredIntentAction,
    _message_requires_stated_constraint,
    validate_deferred_intent_action,
    validate_deferred_intent_structure,
)
from elspeth.web.composer.guided.protocol import GuidedStep
from elspeth.web.composer.guided.stage_subjects import (
    ComponentCountConstraint,
    PluginSubject,
    StableSubject,
    StatedGateRoutingConstraint,
    SubjectPresenceConstraint,
)
from elspeth.web.composer.guided.state_machine import GuidedSession, SourceIntent
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId

MSG = (
    "Read this synthetic multi-document JSON file, split each document into one row per section, "
    "have an LLM write a one-sentence gist of each section, then gather each document's section rows back "
    "together into a single batch per document (every section must make it back — fail the document if one is lost) "
    "and write one summary row per document to a JSON file.\n"
    "https://dta-au.github.io/elspeth/tutorial-site/multi-doc-sections.json"
)

SRC_ID = "d3b13956-4b80-45b8-ba8e-109b86d1e545"
NODE_ID = "99999999-9999-4999-8999-999999999999"


def view() -> PolicyCatalogView:
    available = frozenset(
        {
            PluginId("source", "json"),
            PluginId("sink", "json"),
            PluginId("transform", "llm"),
        }
    )
    snapshot = PluginAvailabilitySnapshot.create(
        policy_hash="a" * 64,
        principal_scope="local:test",
        available=available,
        unavailable=(),
        selected=(),
        usable_profile_aliases=(),
        selected_profile_aliases=(),
        binding_generation_fingerprint="b" * 64,
    )
    return PolicyCatalogView(create_catalog_service(), snapshot, profiles=None)


def live_guided() -> GuidedSession:
    """Mirror the live session: step_1_source, one pending json source intent."""
    return GuidedSession(
        step=GuidedStep.STEP_1_SOURCE,
        source_order=(SRC_ID,),
        pending_source_intents={
            SRC_ID: SourceIntent(
                name="source",
                phase="plugin_options",
                plugin="json",
                options=None,
                inspection_facts=None,
                observed_columns=(),
                sample_rows=(),
            )
        },
    )


def probe(label: str, action: DeferredIntentAction) -> None:
    guided = live_guided()
    structural = validate_deferred_intent_structure(action, receiving_stage="source")
    disposition = validate_deferred_intent_action(
        action,
        receiving_stage="source",
        catalog=view(),
        guided=guided,
        originating_message_content=MSG,
    )
    print(f"{label:<44} structural={structural!r:<48} disposition={disposition!r}")


def main() -> None:
    print("_message_requires_stated_constraint(MSG) =", _message_requires_stated_constraint(MSG))
    print()

    llm_subject = PluginSubject(kind="plugin", subject_id=NODE_ID, plugin_kind="transform", plugin_name="llm")
    node_subject = StableSubject(kind="stable", component_kind="node", stable_id=NODE_ID)
    sink_subject = PluginSubject(kind="plugin", subject_id=NODE_ID, plugin_kind="sink", plugin_name="json")

    probe(
        "A: topology / transform llm presence",
        DeferredIntentAction(
            target_stage="topology",
            catalog_kind="transform",
            catalog_name="llm",
            redacted_summary="LLM gist per section row.",
            constraints=(SubjectPresenceConstraint(kind="subject_presence", subject=llm_subject, present=True),),
        ),
    )
    probe(
        "B: topology / unnamed node presence (collector)",
        DeferredIntentAction(
            target_stage="topology",
            catalog_kind=None,
            catalog_name=None,
            redacted_summary="Gather section rows into one batch per document.",
            constraints=(SubjectPresenceConstraint(kind="subject_presence", subject=node_subject, present=True),),
        ),
    )
    probe(
        "C: output / sink json presence",
        DeferredIntentAction(
            target_stage="output",
            catalog_kind="sink",
            catalog_name="json",
            redacted_summary="One summary row per document to a JSON file.",
            constraints=(SubjectPresenceConstraint(kind="subject_presence", subject=sink_subject, present=True),),
        ),
    )
    probe(
        "D: topology / node count",
        DeferredIntentAction(
            target_stage="topology",
            catalog_kind=None,
            catalog_name=None,
            redacted_summary="At least one topology node.",
            constraints=(
                ComponentCountConstraint(
                    kind="component_count", component_kind="node", plugin_kind=None, plugin_name=None, operator="at_least", count=1
                ),
            ),
        ),
    )
    probe(
        "E: wire_review / edge-free presence",
        DeferredIntentAction(
            target_stage="wire_review",
            catalog_kind=None,
            catalog_name=None,
            redacted_summary="Every section must return to its document.",
            constraints=(SubjectPresenceConstraint(kind="subject_presence", subject=node_subject, present=True),),
        ),
    )

    print()
    print("--- control: identical actions against a message with no route-destination prose ---")
    plain = "Later add a step that summarises each section."
    print("_message_requires_stated_constraint(plain) =", _message_requires_stated_constraint(plain))
    action = DeferredIntentAction(
        target_stage="topology",
        catalog_kind="transform",
        catalog_name="llm",
        redacted_summary="LLM gist per section row.",
        constraints=(SubjectPresenceConstraint(kind="subject_presence", subject=llm_subject, present=True),),
    )
    print(
        "A against plain message ->",
        validate_deferred_intent_action(
            action,
            receiving_stage="source",
            catalog=view(),
            guided=live_guided(),
            originating_message_content=plain,
        ),
    )

    print()
    print("--- what the message demands: a StatedGateRoutingConstraint, which cannot be grounded ---")
    gate = StatedGateRoutingConstraint(
        kind="stated_gate_routing",
        subject=StableSubject(kind="stable", component_kind="source", stable_id=SRC_ID),
        column="section",
        operator="equals",
        value="lost",
        true_target="fail",
        false_target="kept",
    )
    probe(
        "F: topology + fabricated gate routing",
        DeferredIntentAction(
            target_stage="topology",
            catalog_kind=None,
            catalog_name=None,
            redacted_summary="Fail the document if a section is lost.",
            constraints=(gate,),
        ),
    )


if __name__ == "__main__":
    main()
