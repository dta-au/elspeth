"""The shared identifier policy has ONE owner, and every gate derives from it.

``identifiers.is_valid_field_name`` states which names are field names.
``validate_field_name`` decides acceptance by calling it and only then words a
rejection, and ``schema.get_raw_producer_guaranteed_fields``'s two heuristic
gates ask it instead of restating ``isidentifier()`` + ``iskeyword()``.

Why the restatement mattered. Those gates decide whether the COMPOSER credits a
producer with guarantees the RUNTIME will also credit it with. The text arm's
runtime twin is ``TextSourceConfig._validate_column``; the llm arm's is
``build_llm_source_output_schema_config``'s ``validate_field_name`` call. Both
runtime twins enforce the shared policy. While the gates restated it, the two
read identically — and the divergence, if the policy ever moved, is the bad
polarity: the gate declines, ``raw_participates`` goes False, the composer
ABSTAINS and publishes no verdict, while the runtime participates with the
field and rejects the pipeline at build. Composer green, runtime red.

Three pins, because no one of them is sufficient:

* ``TestPolicyTruth`` states the acceptance set against an explicit corpus.
  This is the pin that catches a change to the policy ITSELF — the parity pin
  below cannot, precisely because the raising path now derives from the
  predicate and so can no longer disagree with it.
* ``TestRaisingPathDerives`` pins that derivation, so re-inlining a second
  copy of the policy into ``validate_field_name`` fails here.
* ``TestGatesDerive`` drives the real gates and pins them to the predicate.
  This is the one that fails if a future edit re-inlines a DIFFERENT policy at
  either call site, which is the defect this whole file exists to prevent.
"""

from __future__ import annotations

import pytest

from elspeth.contracts.identifiers import is_valid_field_name, validate_field_name
from elspeth.contracts.schema import get_raw_producer_guaranteed_fields


class _StrSubclass(str):
    """A ``str`` subclass — rejected by the policy's ``type(name) is str`` gate."""

    __slots__ = ()


# (name, accepted) — the acceptance set, stated rather than computed.
# Written out on purpose: computing the expectation from the implementation is
# what makes a policy test vacuous.
POLICY_CORPUS: tuple[tuple[object, bool], ...] = (
    # Ordinary identifiers.
    ("url", True),
    ("user_id", True),
    ("_private", True),
    ("a1", True),
    ("__dunder__", True),
    # Keyword-adjacent but not keywords — the shapes a hand-written
    # `str.isidentifier`-only guard gets RIGHT and a "lowercase, no spaces"
    # guard gets wrong.
    ("class_", True),
    ("if_", True),
    ("_1", True),
    # Non-ASCII identifiers: Python says yes, so the policy says yes.
    ("naïve", True),
    # Python keywords — identifiers, but reserved.
    ("class", False),
    ("if", False),
    ("import", False),
    ("lambda", False),
    ("None", False),
    ("True", False),
    # Soft keywords are NOT keywords and stay accepted (`keyword.iskeyword`
    # is the authority, not `keyword.issoftkeyword`).
    ("match", True),
    ("type", True),
    # Not identifiers.
    ("first name", False),
    ("user-id", False),
    ("user.name", False),
    ("1abc", False),
    ("field!", False),
    ("", False),
    ("   ", False),
    # Not strings at all.
    (None, False),
    (5, False),
    (b"col", False),
    (["url"], False),
    (_StrSubclass("url"), False),
)


def _accepted() -> tuple[object, ...]:
    return tuple(name for name, ok in POLICY_CORPUS if ok)


def _rejected() -> tuple[object, ...]:
    return tuple(name for name, ok in POLICY_CORPUS if not ok)


class TestCorpusDiscriminates:
    """Probe validity: a corpus that stopped straddling the boundary proves nothing."""

    def test_corpus_holds_both_verdicts_in_quantity(self) -> None:
        assert len(_accepted()) >= 8, "too few accepted names to pin the policy"
        assert len(_rejected()) >= 8, "too few rejected names to pin the policy"

    def test_corpus_covers_every_rejection_reason(self) -> None:
        """Each clause of the policy must have at least one witness."""
        rejected = _rejected()
        assert "class" in rejected, "no keyword witness"
        assert "user-id" in rejected, "no non-identifier witness"
        assert "" in rejected, "no empty witness"
        assert any(not isinstance(n, str) for n in rejected), "no non-str witness"
        assert any(type(n) is not str and isinstance(n, str) for n in rejected), "no str-subclass witness"


class TestPolicyTruth:
    """The acceptance set itself. Mutating the policy fails HERE."""

    @pytest.mark.parametrize(("name", "accepted"), POLICY_CORPUS, ids=lambda v: repr(v)[:24])
    def test_predicate_matches_the_stated_acceptance_set(self, name: object, accepted: bool) -> None:
        assert is_valid_field_name(name) is accepted


class TestRaisingPathDerives:
    """``validate_field_name`` accepts exactly what the predicate accepts.

    Green by construction today — the raising path calls the predicate. That is
    the point: this pin fails the moment someone re-inlines a second copy of
    the policy into it, which is how the two drifted apart everywhere else.
    """

    @pytest.mark.parametrize(("name", "accepted"), POLICY_CORPUS, ids=lambda v: repr(v)[:24])
    def test_acceptance_sets_are_equal(self, name: object, accepted: bool) -> None:
        try:
            validate_field_name(name, "probe")
        except ValueError:
            raised = True
        else:
            raised = False
        assert raised is not is_valid_field_name(name)
        assert raised is not accepted

    def test_rejection_wording_still_discriminates_the_reason(self) -> None:
        """Deriving acceptance must not flatten the two rejection messages."""
        with pytest.raises(ValueError, match="is a Python keyword"):
            validate_field_name("class", "probe")
        with pytest.raises(ValueError, match="is not a valid Python identifier"):
            validate_field_name("user-id", "probe")

    def test_invalid_identifier_override_still_bypassed_for_keywords(self) -> None:
        """A keyword keeps its own message even when an override is supplied."""
        with pytest.raises(ValueError, match="is a Python keyword"):
            validate_field_name("class", "probe", invalid_identifier_message="CUSTOM")
        with pytest.raises(ValueError, match="CUSTOM"):
            validate_field_name("user-id", "probe", invalid_identifier_message="CUSTOM")


class TestGatesDerive:
    """The two ``get_raw_producer_guaranteed_fields`` heuristic gates.

    Behavioural, not structural: these drive the real function and compare the
    guarantees it credits against the predicate. Re-inlining a DIFFERENT policy
    at either call site fails here even though the source still parses fine.
    """

    @staticmethod
    def _text_guarantees(column: object) -> frozenset[str]:
        return get_raw_producer_guaranteed_fields(
            "text",
            {"column": column, "schema": {"mode": "observed"}},
            owner="probe",
        )

    @staticmethod
    def _llm_guarantees(response_field: object) -> frozenset[str]:
        return get_raw_producer_guaranteed_fields(
            "llm",
            {"response_field": response_field, "schema": {"mode": "observed"}},
            owner="probe",
        )

    @pytest.mark.parametrize(("name", "accepted"), POLICY_CORPUS, ids=lambda v: repr(v)[:24])
    def test_text_arm_fires_exactly_when_the_policy_accepts(self, name: object, accepted: bool) -> None:
        # Compared as "did the gate credit anything", not by building
        # ``frozenset({name})``: the corpus deliberately carries an unhashable
        # option value, which the gate itself must survive.
        credited = self._text_guarantees(name)
        fired = credited != frozenset()
        assert fired is is_valid_field_name(name)
        assert fired is accepted
        if accepted:
            assert isinstance(name, str)
            assert credited == frozenset({name})

    @pytest.mark.parametrize(("name", "accepted"), POLICY_CORPUS, ids=lambda v: repr(v)[:24])
    def test_llm_arm_fires_exactly_when_the_policy_accepts(self, name: object, accepted: bool) -> None:
        credited = self._llm_guarantees(name)
        fired = credited != frozenset()
        assert fired is is_valid_field_name(name)
        assert fired is accepted
        if accepted:
            assert isinstance(name, str)
            assert credited == frozenset({name, f"{name}_usage", f"{name}_model"})
