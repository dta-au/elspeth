"""Python↔TypeScript parity for the frontend's connection-topology mirrors.

Three mirrors live in ``src/elspeth/web/frontend/src/lib/graphTopology.ts`` and
each is pinned here to its own named Python authority: the implicit
self-publisher set, ``FAN_IN_NODE_TYPES``, and the coalesce policy/merge
tuples. The first was gated before the module existed; the other two were
convention-only until the topology lift (elspeth-93f5621f18, Wave 3) gave them
a home worth gating.

``lib/graphTopology.ts`` restates, in TypeScript, the rule that
``_producer_resolver.published_success_connection`` owns in Python: which node
kinds publish their success output IMPLICITLY, under their own node id, when
they declare no ``on_success``. The diagram uses it to decide whether a node
has an outbound edge at all.

Python is the source of truth — ``core/dag/builder.py``'s ``register_producer``
calls are what actually resolve a downstream ``input`` at build time, and
``_IMPLICIT_SELF_PUBLISHING_NODE_TYPES`` is the single place that fact is
stated. The TS copy cannot import it, so nothing but this test stops the two
from drifting.

Drift here is not hypothetical. ``aggregation`` was missing from BOTH sides on
the first pass, and the Python miss made the composer reject a pipeline the
runtime builds and runs; the TS miss drew a working fork/coalesce pipeline as
two disconnected fragments (session 3f02c8fa). ``GraphView.test.tsx`` covers
the coalesce arm behaviourally, but a TS test cannot see the Python value, so
it would stay green through exactly the divergence that shipped.

When a kind is added to or removed from ``_IMPLICIT_SELF_PUBLISHING_NODE_TYPES``,
update ``IMPLICIT_SELF_PUBLISHING_NODE_TYPES`` in ``lib/graphTopology.ts`` in the
same commit.

Follows the parity pattern already established by
``tests/unit/web/catalog/test_audit_characteristic_vocabulary_parity.py``:
regex the TS literal, compare against the Python authority, and carry a smoke
assertion so a regex that matches nothing fails loudly instead of passing
vacuously.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import elspeth
from elspeth.core.config import CoalesceSettings
from elspeth.web.composer._producer_resolver import _IMPLICIT_SELF_PUBLISHING_NODE_TYPES
from elspeth.web.composer.guided.connection_consumers import (  # noqa: F401  (import proves the module path in the docstring resolves)
    _coalesce_branch_connections,
)

_PACKAGE_ROOT = Path(elspeth.__file__).parent
_TOPOLOGY_PATH = _PACKAGE_ROOT / "web" / "frontend" / "src" / "lib" / "graphTopology.ts"

# Anchored on the named const specifically — a 2000-line .tsx held many
# string arrays, and a loose "quoted strings near the word queue" regex would
# happily pin the wrong one. The `[^\]]*` body cannot span the closing bracket,
# so this matches exactly one declaration.
_SET_DECLARATION_RE = re.compile(
    r"const\s+IMPLICIT_SELF_PUBLISHING_NODE_TYPES\s*:\s*ReadonlySet<string>\s*=\s*new\s+Set\(\s*\[(?P<body>[^\]]*)\]\s*\)\s*;",
)
_MEMBER_RE = re.compile(r'"([^"]+)"')

_FAN_IN_DECLARATION_RE = re.compile(
    r"const\s+FAN_IN_NODE_TYPES\s*:\s*ReadonlySet<string>\s*=\s*new\s+Set\(\s*\[(?P<body>[^\]]*)\]\s*\)\s*;",
)
_TUPLE_RE_TEMPLATE = r"const\s+{name}\s*=\s*\[(?P<body>[^\]]*)\]\s*as\s+const\s*;"


def _ts_members(pattern: re.Pattern[str], label: str) -> list[str]:
    """Parse one declaration out of graphTopology.ts and return its members."""
    text = _TOPOLOGY_PATH.read_text(encoding="utf-8")
    matches = pattern.findall(text)
    assert len(matches) == 1, (
        f"Expected exactly one `{label}` declaration in {_TOPOLOGY_PATH.name}, matched {len(matches)}. "
        "The declaration moved, was renamed, or Prettier rewrote its shape — re-anchor this regex "
        "rather than deleting the parity assertion, which is the only thing pinning the TS copy to "
        "the Python authority."
    )
    return _MEMBER_RE.findall(matches[0])


def _ts_self_publishing_node_types() -> set[str]:
    """Parse graphTopology.ts and return the declared member set."""
    text = _TOPOLOGY_PATH.read_text(encoding="utf-8")
    matches = _SET_DECLARATION_RE.findall(text)
    assert len(matches) == 1, (
        f"Expected exactly one `IMPLICIT_SELF_PUBLISHING_NODE_TYPES` Set declaration in "
        f"{_TOPOLOGY_PATH.name}, matched {len(matches)}. The declaration moved, was renamed, or "
        "Prettier rewrote its shape — re-anchor this regex rather than deleting the parity test, "
        "which is the only thing pinning the TS copy to the Python authority."
    )
    return set(_MEMBER_RE.findall(matches[0]))


def test_self_publishing_set_matches_the_python_authority() -> None:
    py_kinds = set(_IMPLICIT_SELF_PUBLISHING_NODE_TYPES)
    ts_kinds = _ts_self_publishing_node_types()

    missing_in_ts = py_kinds - ts_kinds
    missing_in_py = ts_kinds - py_kinds

    assert not missing_in_ts, (
        "Node kinds that publish implicitly in Python but not in "
        f"{_TOPOLOGY_PATH.name}: {sorted(missing_in_ts)}. The diagram will draw a correctly-wired "
        "node of that kind as publishing nothing, splitting a working pipeline into disconnected "
        "fragments. Add them to IMPLICIT_SELF_PUBLISHING_NODE_TYPES in that file."
    )
    assert not missing_in_py, (
        f"Node kinds declared self-publishing in {_TOPOLOGY_PATH.name} but absent from "
        f"`_IMPLICIT_SELF_PUBLISHING_NODE_TYPES`: {sorted(missing_in_py)}. The diagram is inventing "
        "an outbound connection the DAG builder does not resolve. Either the Python authority is "
        "missing a kind (check `core/dag/builder.py`'s register_producer calls) or the TS copy is wrong."
    )


def test_the_ts_helper_consults_the_set_rather_than_on_success_alone() -> None:
    """The set is only load-bearing if ``publishedSuccessConnection`` reads it.

    Pinning the members alone would stay green if someone re-derived the rule
    from ``on_success`` inside the function and left the set as dead decoration
    — which is the precise defect (asking ``on_success`` directly) that this
    whole chain exists to prevent.
    """
    text = _TOPOLOGY_PATH.read_text(encoding="utf-8")
    assert "function publishedSuccessConnection(" in text, (
        f"`publishedSuccessConnection` is gone from {_TOPOLOGY_PATH.name}. It is the TS mirror of "
        "`_producer_resolver.published_success_connection`; if the diagram now derives publication "
        "some other way, that path needs its own pin to the Python authority."
    )
    assert "IMPLICIT_SELF_PUBLISHING_NODE_TYPES.has(node.node_type)" in text, (
        f"`publishedSuccessConnection` in {_TOPOLOGY_PATH.name} no longer tests membership of "
        "IMPLICIT_SELF_PUBLISHING_NODE_TYPES. Asking `node.on_success` directly reports a "
        "non-terminal coalesce, an aggregation that omits on_success, and every queue as publishing "
        "nothing — the defect that drew a working pipeline as two disconnected fragments."
    )


def test_topology_module_is_readable() -> None:
    """Smoke test: the anchor path resolves and the regex matched real members."""
    assert _TOPOLOGY_PATH.is_file(), f"Expected the topology module at {_TOPOLOGY_PATH} — anchor path is wrong."
    assert _ts_self_publishing_node_types(), (
        f"No members parsed from IMPLICIT_SELF_PUBLISHING_NODE_TYPES in {_TOPOLOGY_PATH}. "
        "The regex or the file format has drifted, and the parity assertion above would be vacuous."
    )


def test_fan_in_node_types_matches_the_canonical_consumer_projection() -> None:
    """`FAN_IN_NODE_TYPES` mirrors the arm in `connection_consumers.py`.

    That arm is the Python authority for "which node kinds declare their
    inbound wiring through `branches` rather than through the scalar `input`".
    Getting it wrong drew a coalesce with a single arm on a composition that
    validates green (elspeth-625e85c59b).
    """
    ts_kinds = set(_ts_members(_FAN_IN_DECLARATION_RE, "FAN_IN_NODE_TYPES"))
    assert ts_kinds, f"No members parsed from FAN_IN_NODE_TYPES in {_TOPOLOGY_PATH} — the assertion would be vacuous."
    assert ts_kinds == {"coalesce", "row_union"}, (
        f"FAN_IN_NODE_TYPES in {_TOPOLOGY_PATH.name} is {sorted(ts_kinds)}, but "
        "`connection_consumers.py`'s canonical consumer projection treats exactly "
        "('coalesce', 'row_union') as branch-wired. A kind in one list and not the other means the "
        "Spec tab and the Graph tab infer a different inbound topology than the runtime builds."
    )


def test_coalesce_member_tuples_match_the_backend_literals() -> None:
    """`COALESCE_POLICIES` / `COALESCE_MERGES` mirror `CoalesceSettings`'s Literals.

    The frontend closes its display maps against these tuples, so an unphrased
    member is a compile error THERE — but only this test can see a member added
    on the PYTHON side. Without it the new value degrades silently to
    title-cased machine text at the user.
    """
    py_policies = set(get_args(CoalesceSettings.model_fields["policy"].annotation))
    py_merges = set(get_args(CoalesceSettings.model_fields["merge"].annotation))
    for label, py_members in (("COALESCE_POLICIES", py_policies), ("COALESCE_MERGES", py_merges)):
        assert py_members, f"No members read from CoalesceSettings for {label} — the assertion would be vacuous."
        ts_members = set(_ts_members(re.compile(_TUPLE_RE_TEMPLATE.format(name=label)), label))
        assert ts_members, f"No members parsed from {label} in {_TOPOLOGY_PATH} — the assertion would be vacuous."
        assert ts_members == py_members, (
            f"{label} in {_TOPOLOGY_PATH.name} is {sorted(ts_members)}; `CoalesceSettings` declares "
            f"{sorted(py_members)}. Add the missing member to the tuple AND write its phrase in "
            "components/workspace/specRouting.ts, which closes a Record against it."
        )
