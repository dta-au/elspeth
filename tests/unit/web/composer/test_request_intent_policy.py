"""Closed request-intent policy for Composer lifecycle authority."""

from __future__ import annotations

import json
from pathlib import Path
from time import perf_counter

import pytest

from elspeth.web.composer.no_tool_policy import (
    PipelineMutationIntentDecision,
    classify_pipeline_mutation_intent,
    is_referential_pipeline_mutation_intent,
)

_PARITY_FIXTURE_DIR = Path(__file__).resolve().parents[4] / "evals" / "composer-parity" / "fixtures"
_COMPLETE_MULTI_CLAUSE_REQUESTS = (
    pytest.param(
        "Build a pipeline that reads customers.csv and writes results.jsonl.",
        id="ordinary-source-and-sink-clauses",
    ),
    pytest.param(
        "Build a pipeline that reads customers.csv and splits rows by country. Do not do that with a generic transform.",
        id="scoped-anaphoric-constraint",
    ),
    pytest.param(
        "Build a pipeline that reads customers.csv and writes results.jsonl. Do not add a generic transform.",
        id="scoped-negative-action-constraint",
    ),
    *(
        pytest.param(json.loads(path.read_text(encoding="utf-8"))["intent"], id=f"parity-{path.stem}")
        for path in sorted(_PARITY_FIXTURE_DIR.glob("*.json"))
    ),
)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        pytest.param(
            "Do not build or run a pipeline",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="negated-coordinated-actions",
        ),
        pytest.param(
            "Please don't create a workflow.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="contracted-negation",
        ),
        pytest.param("Never execute this pipeline.", PipelineMutationIntentDecision.AMBIGUOUS, id="never"),
        pytest.param(
            '"Build a pipeline" is an example command.',
            PipelineMutationIntentDecision.CONVERSATIONAL,
            id="quoted-command",
        ),
        pytest.param(
            "The example 'build a pipeline' is not an instruction.",
            PipelineMutationIntentDecision.CONVERSATIONAL,
            id="single-quoted-command",
        ),
        pytest.param(
            "If I asked you to build a pipeline, what would happen?",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="hypothetical",
        ),
        pytest.param(
            "The build pipeline is documented here.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="build-used-as-noun",
        ),
        pytest.param(
            "Can you explain how to build a pipeline?",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="explanation-containing-action",
        ),
        pytest.param(
            "Maybe build a pipeline someday.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="ambiguous-suggestion",
        ),
        pytest.param(
            "Can you explain how this pipeline works?",
            PipelineMutationIntentDecision.CONVERSATIONAL,
            id="informational-pipeline-noun",
        ),
        pytest.param(
            "Read the CSV documentation and explain how transforms work.",
            PipelineMutationIntentDecision.CONVERSATIONAL,
            id="informational-data-format-documentation",
        ),
        pytest.param(
            "Read the guide from CSV documentation and explain its transform examples.",
            PipelineMutationIntentDecision.CONVERSATIONAL,
            id="informational-from-format-documentation",
        ),
        pytest.param(
            "Read the CSV file documentation and explain its transform examples.",
            PipelineMutationIntentDecision.CONVERSATIONAL,
            id="informational-format-file-documentation",
        ),
        pytest.param(
            "Read customers.csv and explain how transforms work.",
            PipelineMutationIntentDecision.CONVERSATIONAL,
            id="informational-source-with-transform-noun",
        ),
        pytest.param(
            "Read customers.csv and explain the password policy.",
            PipelineMutationIntentDecision.CONVERSATIONAL,
            id="informational-source-with-operation-prefix-noun",
        ),
        pytest.param(
            "Read customers.csv and explain: write is a pipeline operation.",
            PipelineMutationIntentDecision.CONVERSATIONAL,
            id="informational-source-with-objectless-operation-definition",
        ),
        pytest.param(
            "Read customers.csv and summarize the data, then write a summary in this chat.",
            PipelineMutationIntentDecision.CONVERSATIONAL,
            id="informational-source-with-chat-writing-request",
        ),
        pytest.param(
            "Make this explanation shorter.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="non-pipeline-make-command",
        ),
        pytest.param(
            "Change the subject.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="non-pipeline-change-command",
        ),
        pytest.param(
            "Add more detail to your explanation.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="non-pipeline-add-command",
        ),
        pytest.param(
            "Do anything but build the pipeline.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="adversative-negation",
        ),
        pytest.param(
            "Build no pipeline.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="post-action-no",
        ),
        pytest.param(
            "Stop. Build nothing.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="post-action-nothing-after-boundary",
        ),
        pytest.param(
            "Can you explain? Build nothing.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="informational-then-build-nothing",
        ),
        pytest.param(
            "Tell me: build or run?",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="choice-after-colon",
        ),
        pytest.param(
            "Do not: build a pipeline.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="negation-before-colon",
        ),
        pytest.param(
            "Never: run the pipeline.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="never-before-colon",
        ),
        pytest.param(
            "Explain this pseudocode: BUILD pipeline",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="pseudocode-after-colon",
        ),
        pytest.param(
            "Here is an example:\n build a pipeline",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="example-after-colon",
        ),
        pytest.param(
            "If needed, build the pipeline.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="unevaluated-condition",
        ),
        pytest.param(
            "The operator said: build a pipeline.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="reported-command",
        ),
        pytest.param(
            "Tell me: then build the pipeline.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="meta-request-before-then",
        ),
        pytest.param(
            "Do not. Build the pipeline.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="bare-command-after-negative-sentence",
        ),
        pytest.param(
            "Can you explain? Build the pipeline.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="bare-command-after-informational-sentence",
        ),
        pytest.param(
            "Run the unit tests.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="unrelated-run",
        ),
        pytest.param(
            "Execute this shell command.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="unrelated-execute",
        ),
        pytest.param(
            "Process my refund.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="unrelated-process",
        ),
        pytest.param(
            "Route my support ticket.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="unrelated-route",
        ),
        pytest.param(
            "Save this document.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="unrelated-save",
        ),
        pytest.param(
            "Wire the payment.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="unrelated-wire",
        ),
        pytest.param(
            "Split the bill.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="unrelated-split",
        ),
        pytest.param(
            "Build the documentation.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="unrelated-build",
        ),
        pytest.param(
            "Build documentation for the pipeline.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="unrelated-build-before-pipeline-mention",
        ),
        pytest.param(
            "Build: no pipeline.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="post-action-negation-after-colon",
        ),
        pytest.param(
            "Build the pipeline; without changing anything.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="post-action-negation-after-semicolon",
        ),
        pytest.param(
            "Build or run the pipeline?",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="direct-choice-question",
        ),
        pytest.param(
            "Build the pipeline or run it.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="direct-choice-statement",
        ),
        pytest.param(
            "Build the pipeline if needed.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="post-action-unevaluated-condition",
        ),
        pytest.param(
            "Save this file.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="generic-file-target",
        ),
        pytest.param(
            "Process this data.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="generic-data-target",
        ),
        pytest.param(
            "Create a CSV.",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="generic-format-target",
        ),
        pytest.param(
            "Build!",
            PipelineMutationIntentDecision.EXPLICIT_MUTATION,
            id="punctuated-imperative",
        ),
        pytest.param(
            "Run!",
            PipelineMutationIntentDecision.AMBIGUOUS,
            id="context-free-run",
        ),
        pytest.param(
            "How does this work? Now build it.",
            PipelineMutationIntentDecision.EXPLICIT_MUTATION,
            id="later-explicit-imperative",
        ),
        pytest.param(
            "Please create a CSV to JSONL pipeline.",
            PipelineMutationIntentDecision.EXPLICIT_MUTATION,
            id="polite-imperative",
        ),
        pytest.param(
            "Can you set up a pipeline for this file?",
            PipelineMutationIntentDecision.EXPLICIT_MUTATION,
            id="polite-request",
        ),
        pytest.param(
            "Set this up to actually run from leads_q3.csv.",
            PipelineMutationIntentDecision.EXPLICIT_MUTATION,
            id="existing-set-this-up-command",
        ),
        pytest.param(
            "Just build the whole thing.",
            PipelineMutationIntentDecision.EXPLICIT_MUTATION,
            id="existing-just-build-command",
        ),
        pytest.param(
            "First inspect the file, then build a pipeline.",
            PipelineMutationIntentDecision.EXPLICIT_MUTATION,
            id="sequenced-imperative",
        ),
        pytest.param(
            "I want you to update the pipeline.",
            PipelineMutationIntentDecision.EXPLICIT_MUTATION,
            id="explicit-first-person-request",
        ),
        pytest.param(
            "Run the pipeline.",
            PipelineMutationIntentDecision.EXPLICIT_MUTATION,
            id="targeted-run-command",
        ),
        pytest.param(
            "Execute this pipeline.",
            PipelineMutationIntentDecision.EXPLICIT_MUTATION,
            id="targeted-execute-command",
        ),
        pytest.param(
            "Wire the source to the output.",
            PipelineMutationIntentDecision.EXPLICIT_MUTATION,
            id="targeted-wire-command",
        ),
        pytest.param(
            "Split the rows by country.",
            PipelineMutationIntentDecision.EXPLICIT_MUTATION,
            id="targeted-split-command",
        ),
        pytest.param(
            "Save the pipeline.",
            PipelineMutationIntentDecision.EXPLICIT_MUTATION,
            id="targeted-save-command",
        ),
        pytest.param(
            "Process the input rows.",
            PipelineMutationIntentDecision.EXPLICIT_MUTATION,
            id="targeted-process-command",
        ),
        pytest.param(
            "Build the CSV workflow now.",
            PipelineMutationIntentDecision.EXPLICIT_MUTATION,
            id="format-qualified-workflow",
        ),
        pytest.param(
            "Build the requested pipeline.",
            PipelineMutationIntentDecision.EXPLICIT_MUTATION,
            id="requested-pipeline",
        ),
        pytest.param(
            "create a workflow that rates how cool pages are",
            PipelineMutationIntentDecision.EXPLICIT_MUTATION,
            id="interpretation-review-workflow",
        ),
    ],
)
def test_user_request_requires_explicit_positive_mutation_intent(
    message: str,
    expected: PipelineMutationIntentDecision,
) -> None:
    assert classify_pipeline_mutation_intent(message) is expected


@pytest.mark.parametrize(
    ("message", "expected_referential"),
    [
        ("Build the requested pipeline.", True),
        ("Could you build the requested pipeline?", True),
        ("Can you please build the requested workflow?", True),
        ("I want you to build the requested pipeline.", True),
        ("Let's build the requested pipeline.", True),
        ("How does this work? Now build it.", True),
        ("Just build the whole thing.", True),
        ("Build!", True),
        ("Build this pipeline.", True),
        ("Update this pipeline.", True),
        ("Change this workflow.", True),
        ("Run this pipeline.", True),
        ("Build the pipeline.", True),
        ("Change this node.", True),
        ("Route this input.", True),
        ("Save this output.", True),
        ("Build a CSV to JSONL pipeline.", False),
        ("Build a pipeline that routes every row to both sinks.", False),
        ("Split the rows by country.", False),
    ],
)
def test_explicit_mutation_referential_context_decision(message: str, expected_referential: bool) -> None:
    assert classify_pipeline_mutation_intent(message) is PipelineMutationIntentDecision.EXPLICIT_MUTATION
    assert is_referential_pipeline_mutation_intent(message) is expected_referential


@pytest.mark.parametrize("message", _COMPLETE_MULTI_CLAUSE_REQUESTS)
def test_complete_multi_clause_pipeline_request_is_explicit(message: str) -> None:
    assert classify_pipeline_mutation_intent(message) is PipelineMutationIntentDecision.EXPLICIT_MUTATION


@pytest.mark.parametrize(
    "message",
    [
        "Build a pipeline that reads customers.csv and writes results.jsonl. Actually, do not.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Please do not do that.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Don't do it.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Please don\u2019t do that.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Please do not proceed.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. I do not want that.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Stop.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Cancel it.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Cancel that.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. I changed my mind.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Do not build it.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. It should not be built.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. I do not want you to build it.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Cancel the request.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Cancel my request.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Forget the build.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Disregard that request.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. I no longer want it.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. This is only an example, not a request.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Instead, explain what it would do.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Do not make any changes.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Don't change anything.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Do not process that request.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Do not save anything.",
        "Build a pipeline that reads customers.csv and writes results.jsonl. Do not update it.",
        "Build a pipeline that reads customers.csv and writes results.jsonl?",
        "If needed, build a pipeline that reads customers.csv and writes results.jsonl.",
        "The operator said: build a pipeline that reads customers.csv and writes results.jsonl.",
    ],
)
def test_multi_clause_pipeline_request_still_requires_positive_root_authority(message: str) -> None:
    assert classify_pipeline_mutation_intent(message) is PipelineMutationIntentDecision.AMBIGUOUS


@pytest.mark.parametrize(
    "message",
    [
        "Do not then build the pipeline.",
        "Don't then build the pipeline.",
        "Never then run the pipeline.",
        "Do anything but then build the pipeline.",
        "Build neither a pipeline nor a workflow.",
        "Create neither a pipeline nor a workflow.",
        "Build none of the pipeline.",
        "Create zero pipeline.",
        "Build anything but a pipeline.",
        "Build anything except a pipeline.",
        "Build anything other than a pipeline.",
        "Build anything besides a pipeline.",
        "Build anything rather than a pipeline.",
        "Build the pipeline. Actually, do not.",
        "Build the pipeline. Actually, don't.",
        "Build! Never mind.",
        "Build the pipeline. Cancel that.",
        "Build the pipeline. Scratch that.",
        "Build the pipeline. Forget it.",
        "Build the pipeline. \"Actually, don't.",
        "Set this up for the meeting.",
        "Set it up for payroll.",
        "Set this up?",
        "Save this document about the pipeline.",
        "Execute this shell script for the pipeline.",
        "Route this bug report to the pipeline team.",
        "Process this request using pipeline terminology.",
        "Build the pipeline?",
        "Run the pipeline?",
    ],
)
def test_whole_request_polarity_and_direct_object_binding_fail_closed(message: str) -> None:
    assert classify_pipeline_mutation_intent(message) is PipelineMutationIntentDecision.AMBIGUOUS


@pytest.mark.parametrize(
    "message",
    [
        "Build the pipeline. Undo that.",
        "Build the pipeline. Revert that.",
        "Build the pipeline. Abort.",
        "Build the pipeline. Belay that.",
        "Build the pipeline. Hold off.",
        "Build the pipeline. Ignore that.",
        "Build the pipeline. Withdraw that request.",
        "Build the pipeline. I take that back.",
        "Build the pipeline. I changed my mind.",
        "Build the pipeline. On second thought, leave it.",
        "Build the pipeline. Actually, skip it.",
        "Avoid then build the pipeline.",
        "Refrain, then build the pipeline.",
        "Document then build the pipeline.",
        "Say then build the pipeline.",
        "Mention then build the pipeline.",
    ],
)
def test_authorizing_action_must_be_terminal_with_approved_prefix(message: str) -> None:
    assert classify_pipeline_mutation_intent(message) is PipelineMutationIntentDecision.AMBIGUOUS


@pytest.mark.parametrize(
    "message",
    [
        'Please "do not" build the pipeline.',
        "Please 'do not' build the pipeline.",
        "Please \u201cdo not\u201d build the pipeline.",
        "Please \u2018do not\u2019 build the pipeline.",
        '"Never" build the pipeline.',
        'Build "no" pipeline.',
        'Can you "not" build the pipeline?',
    ],
)
def test_quoted_material_cannot_be_elided_to_create_authority(message: str) -> None:
    assert classify_pipeline_mutation_intent(message) is PipelineMutationIntentDecision.AMBIGUOUS


def test_classification_runtime_is_bounded_for_65536_byte_adversarial_quote_input() -> None:
    message = ('"' + '\\"' * 32_767 + "\\")[:65_536]
    assert len(message.encode("ascii")) == 65_536

    started = perf_counter()
    decision = classify_pipeline_mutation_intent(message)
    elapsed = perf_counter() - started

    assert decision is PipelineMutationIntentDecision.AMBIGUOUS
    assert elapsed < 0.5


def test_repeated_action_65536_byte_request_fails_closed_with_bounded_runtime() -> None:
    phrase = "then build the pipeline "
    message = (phrase * (65_536 // len(phrase) + 1))[:65_536]
    assert len(message.encode("ascii")) == 65_536

    started = perf_counter()
    decision = classify_pipeline_mutation_intent(message)
    elapsed = perf_counter() - started

    assert decision is PipelineMutationIntentDecision.AMBIGUOUS
    assert elapsed < 0.5


def test_oversized_classification_request_fails_closed() -> None:
    message = "Build the pipeline. " + "x" * 65_536

    assert classify_pipeline_mutation_intent(message) is PipelineMutationIntentDecision.AMBIGUOUS


def test_closed_intent_decision_cannot_be_used_as_a_truthy_authority_value() -> None:
    with pytest.raises(TypeError, match="compared to an explicit member"):
        bool(PipelineMutationIntentDecision.EXPLICIT_MUTATION)


# ── B1/B2: revocation fail-open fixes (THREAT-002 + F4) ──────────────────
#
# AMBIGUOUS is also the classifier's fall-through default, so every
# revocation case below is paired with a positive control: the same carrier
# minus the revocation clause must classify EXPLICIT_MUTATION, proving the
# carrier reaches a complete production at all.

_REVOCATION_CARRIER_BASE = "Build a pipeline that reads rows from data.csv"
_REVOCATION_POSITIVE_CONTROL = _REVOCATION_CARRIER_BASE + "."


def test_revocation_carrier_positive_control_reaches_explicit_mutation() -> None:
    assert classify_pipeline_mutation_intent(_REVOCATION_POSITIVE_CONTROL) is (PipelineMutationIntentDecision.EXPLICIT_MUTATION)


class TestApostropheConfusableClass:
    """THREAT-002: every apostrophe confusable suppresses revocation alike.

    elspeth-80feb17704 patched U+2019 as a codepoint, not the class. NFKC
    cannot fix this (only U+FF07 folds), so the class is pinned as a set.
    """

    def test_class_pinned_as_set(self) -> None:
        from elspeth.web.composer.no_tool_policy import _APOSTROPHE_CLASS

        assert set(_APOSTROPHE_CLASS) == {"'", "\u2019", "\u02bc", "\uff07", "\u00b4", "\u2032"}

    @pytest.mark.parametrize(
        "codepoint",
        ["'", "\u2019", "\u02bc", "\uff07", "\u00b4", "\u2032"],
        ids=["ascii", "u2019-right-quote", "u02bc-modifier", "uff07-fullwidth", "u00b4-acute", "u2032-prime"],
    )
    def test_confusable_apostrophe_revocation_is_ambiguous(self, codepoint: str) -> None:
        message = f"{_REVOCATION_CARRIER_BASE}, actually don{codepoint}t do that."
        assert classify_pipeline_mutation_intent(message) is PipelineMutationIntentDecision.AMBIGUOUS


class TestRevocationVocabularyUnification:
    """F4: the revocation vocabulary is built from the same canonical tuple
    as the authority vocabulary, so the subset relation cannot recur."""

    def test_property_every_authority_verb_is_revocable(self) -> None:
        from elspeth.web.composer.no_tool_policy import _MUTATION_ACTION_PHRASES

        for phrase in _MUTATION_ACTION_PHRASES:
            message = f"{_REVOCATION_CARRIER_BASE}, do not {phrase} anything."
            decision = classify_pipeline_mutation_intent(message)
            assert decision is not PipelineMutationIntentDecision.EXPLICIT_MUTATION, (
                f"revocation with verb {phrase!r} failed open: {decision}"
            )

    @pytest.mark.parametrize(
        "verb",
        ["wire", "add", "modify", "route", "split", "set up"],
        ids=["wire", "add", "modify", "route", "split", "set-up"],
    )
    def test_previously_leaking_verbs_now_revoke(self, verb: str) -> None:
        message = f"{_REVOCATION_CARRIER_BASE}, do not {verb} anything."
        assert classify_pipeline_mutation_intent(message) is PipelineMutationIntentDecision.AMBIGUOUS

    @pytest.mark.parametrize("verb", ["build", "run"], ids=["build", "run"])
    def test_pinned_true_positives_still_revoke(self, verb: str) -> None:
        message = f"{_REVOCATION_CARRIER_BASE}, do not {verb} anything."
        assert classify_pipeline_mutation_intent(message) is PipelineMutationIntentDecision.AMBIGUOUS

    def test_scoped_negative_constraint_still_explicit_mutation(self) -> None:
        """Widening must not turn scoped constraints into whole-request
        revocations — 'do not add a generic transform' constrains the build,
        it does not revoke it."""
        message = "Build a pipeline that reads customers.csv and writes results.jsonl. Do not add a generic transform."
        assert classify_pipeline_mutation_intent(message) is PipelineMutationIntentDecision.EXPLICIT_MUTATION


def test_intent_classifier_has_no_new_consumers() -> None:
    """B3 consumer-shape tripwire: the classifier is a surface selector.

    Every production consumer of classify_pipeline_mutation_intent today is
    router/disclosure-only (authoring-surface selection, one repair-prompt
    nudge, referential-context threading). Real mutation authority is
    trust_mode == "explicit_approve" (web/composer/tool_batch.py) — this
    classifier has 0/13 held-out recall on open phrasing and must NEVER
    acquire a consent-gate consumer. A new consuming module fails this pin:
    read the surface-selector doctrine in no_tool_policy.py before extending
    the set, and never wire mutation authority to this predicate.
    """
    import elspeth

    src_root = Path(elspeth.__file__).resolve().parent
    consumers = {
        path.relative_to(src_root).as_posix()
        for path in src_root.rglob("*.py")
        if path.name != "no_tool_policy.py" and "classify_pipeline_mutation_intent" in path.read_text(encoding="utf-8")
    }
    # service.py: the live consumer (surface routing + repair nudge, via the
    # module alias). no_tool_finalize.py: a comment-only historical reference
    # ("which this path no longer" consults) — not a call. The pin is a TEXT
    # search on purpose: any new reference, even in prose, warrants a look.
    assert consumers == {"web/composer/service.py", "web/composer/no_tool_finalize.py"}
