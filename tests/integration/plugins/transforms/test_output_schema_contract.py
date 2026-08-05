"""Integration test for output schema contract enforcement.

Tests the full contract chain using real transform instances:
1. Transform construction populates _output_schema_config via helper
2. _validate_output_schema_contract passes for correctly-configured transforms
3. _validate_output_schema_contract raises FrameworkBugError when contract missing
4. Invariant 2: guaranteed_fields is superset of declared_output_fields

The Invariant 2 roster is DISCOVERED from a live ``PluginManager`` rather than
hand-written. It was six literal factories, which covered six of the twenty-three
transforms that actually declare output fields — and a literal roster cannot
grow with the registry, so every transform added after it was typed was outside
the only test asserting its output contract is coherent.
"""

import pytest

from elspeth.contracts.errors import FrameworkBugError
from elspeth.core.dag.builder import _validate_output_schema_contract
from elspeth.plugins.infrastructure.manager import PluginManager
from elspeth.plugins.transforms.field_mapper import FieldMapper
from elspeth.plugins.transforms.json_explode import JSONExplode
from elspeth.plugins.transforms.rag.transform import RAGRetrievalTransform

# The six transforms the hand-written Invariant 2 roster named. They are anchors,
# not the roster: discovery must keep reaching them, and a name may only leave
# this set when the transform genuinely stops declaring output fields.
_LITERAL_ROSTER_ANCHORS = frozenset(
    {
        "batch_replicate",
        "batch_stats",
        "field_mapper",
        "json_explode",
        "rag_retrieval",
        "web_scrape",
    }
)


@pytest.fixture(autouse=True)
def _set_fingerprint_key(monkeypatch):
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-fingerprint-key")


def _invariant2_transforms():
    """Every registered transform that Invariant 2 has something to say about.

    Inclusion is by CAPABILITY: a non-empty ``declared_output_fields``. Invariant
    2 asserts that set is covered by ``guaranteed_fields``, which is trivially
    true when the set is empty — so the nine transforms that declare no output
    fields are excluded because the assertion is VACUOUS for them, not because
    they are exempt from it.

    Instances are built from each transform's own ``probe_config()``. No override
    table is needed: every registered transform constructs from its probe config,
    and the hand-written configs this replaced differed only in the NAMES their
    options produced (``sci__rag_context`` vs ``policy__rag_context``,
    ``page_hash`` vs ``page_fingerprint``) — never in how many fields were
    declared. Invariant 2 is a subset relation over whatever names a config
    yields, so it is indifferent to that. An override belongs here only if some
    future transform cannot reach its field-declaring shape from ``probe_config()``
    at all; ``test_invariant2_roster_covers_the_transforms_it_used_to_name`` is
    what would catch that, by failing rather than by quietly covering less.

    Instantiation happens inside the test body, not at collection time, so the
    autouse fingerprint-key fixture is active.
    """
    manager = PluginManager()
    manager.register_builtin_plugins()
    discovered = []
    for plugin_cls in manager.get_transforms():
        transform = plugin_cls(plugin_cls.probe_config())
        if transform.declared_output_fields:
            discovered.append((getattr(plugin_cls, "name", plugin_cls.__name__), transform))
    return discovered


def _make_rag_transform(*, output_prefix: str = "sci", query_field: str = "q") -> RAGRetrievalTransform:
    """Construct RAG with the base-install provider path, not optional extras."""
    config = RAGRetrievalTransform.probe_config()
    config["output_prefix"] = output_prefix
    config["query_field"] = query_field
    return RAGRetrievalTransform(config)


class TestContractInvariantsAcrossAllTransforms:
    """Verify Invariant 2 (guaranteed_fields superset of declared_output_fields)
    holds for every field-adding transform with real instances."""

    def test_invariant2_roster_covers_the_transforms_it_used_to_name(self):
        """Guard the guard: discovery must not silently cover LESS than the literal list did.

        The roster below was six hand-written factories. Discovery is strictly
        better only while it still reaches them, and the way it could quietly stop
        is a ``probe_config()`` that builds a DEGENERATE instance — the flag that
        adds the fields left off, so ``declared_output_fields`` is empty, so the
        transform drops out of an inclusion predicate keyed on exactly that. The
        anchors below are the six the literal list named; if one leaves the roster,
        coverage regressed even though every remaining test still passes.
        """
        in_scope = {name for name, _ in _invariant2_transforms()}
        assert _LITERAL_ROSTER_ANCHORS.issubset(in_scope), (
            f"transforms the hand-written roster covered are no longer in scope: "
            f"{sorted(_LITERAL_ROSTER_ANCHORS - in_scope)}. A degenerate probe_config() that "
            f"declares no output fields drops a transform out of Invariant 2 silently."
        )

    def test_invariant2_guaranteed_superset_of_declared(self):
        """Every field-adding transform's guaranteed_fields contains all declared_output_fields.

        Failures accumulate: aborting on the first transform would hide every
        transform after it, and this is a registry-wide sweep whose value is
        knowing the full set.
        """
        in_scope = _invariant2_transforms()
        assert in_scope, "no field-declaring transform discovered — Invariant 2 would be vacuous"

        violations = {}
        for name, transform in in_scope:
            output_schema_config = transform._output_schema_config
            if output_schema_config is None:
                violations[name] = "_output_schema_config is None while declared_output_fields is non-empty"
                continue
            guaranteed = frozenset(output_schema_config.guaranteed_fields or ())
            missing = transform.declared_output_fields - guaranteed
            if missing:
                violations[name] = f"declared but not guaranteed: {sorted(missing)} (guaranteed={sorted(guaranteed)})"

        assert violations == {}, f"Invariant 2 violated — declared_output_fields not covered by guaranteed_fields: {violations}"

    @pytest.mark.parametrize(
        "transform_factory",
        [
            pytest.param(
                lambda: _make_rag_transform(),
                id="rag",
            ),
            pytest.param(
                lambda: JSONExplode({"array_field": "items", "output_field": "item", "schema": {"mode": "observed"}}),
                id="json_explode",
            ),
            pytest.param(
                lambda: FieldMapper({"mapping": {"a": "b"}, "strict": True, "schema": {"mode": "observed"}}),
                id="field_mapper",
            ),
        ],
    )
    def test_enforcement_passes_for_valid_transforms(self, transform_factory):
        """Transforms with declared_output_fields AND _output_schema_config pass validation."""
        transform = transform_factory()
        _validate_output_schema_contract(transform)  # Should not raise

    def test_enforcement_fires_on_missing_contract(self):
        """A real transform with cleared _output_schema_config triggers FrameworkBugError."""
        transform = _make_rag_transform()
        transform._output_schema_config = None

        with pytest.raises(FrameworkBugError, match="declares output fields"):
            _validate_output_schema_contract(transform)

    def test_rag_guaranteed_fields_exact(self):
        """RAG transform's guaranteed_fields contains exactly the 4 declared output fields."""
        transform = _make_rag_transform()
        assert frozenset(transform._output_schema_config.guaranteed_fields) == frozenset(
            {"sci__rag_context", "sci__rag_score", "sci__rag_count", "sci__rag_sources"}
        )
