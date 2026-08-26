"""Python↔TypeScript parity for the implicit self-publisher set.

``GraphView.tsx`` restates, in TypeScript, the rule that
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
update ``IMPLICIT_SELF_PUBLISHING_NODE_TYPES`` in ``GraphView.tsx`` in the same
commit.

Follows the parity pattern already established by
``tests/unit/web/catalog/test_audit_characteristic_vocabulary_parity.py``:
regex the TS literal, compare against the Python authority, and carry a smoke
assertion so a regex that matches nothing fails loudly instead of passing
vacuously.
"""

from __future__ import annotations

import re
from pathlib import Path

import elspeth
from elspeth.web.composer._producer_resolver import _IMPLICIT_SELF_PUBLISHING_NODE_TYPES

_PACKAGE_ROOT = Path(elspeth.__file__).parent
_GRAPH_VIEW_PATH = _PACKAGE_ROOT / "web" / "frontend" / "src" / "components" / "inspector" / "GraphView.tsx"

# Anchored on the named const specifically — a 2000-line .tsx holds many
# string arrays, and a loose "quoted strings near the word queue" regex would
# happily pin the wrong one. The `[^\]]*` body cannot span the closing bracket,
# so this matches exactly one declaration.
_SET_DECLARATION_RE = re.compile(
    r"const\s+IMPLICIT_SELF_PUBLISHING_NODE_TYPES\s*:\s*ReadonlySet<string>\s*=\s*new\s+Set\(\s*\[(?P<body>[^\]]*)\]\s*\)\s*;",
)
_MEMBER_RE = re.compile(r'"([^"]+)"')


def _ts_self_publishing_node_types() -> set[str]:
    """Parse GraphView.tsx and return the declared member set."""
    text = _GRAPH_VIEW_PATH.read_text(encoding="utf-8")
    matches = _SET_DECLARATION_RE.findall(text)
    assert len(matches) == 1, (
        f"Expected exactly one `IMPLICIT_SELF_PUBLISHING_NODE_TYPES` Set declaration in "
        f"{_GRAPH_VIEW_PATH.name}, matched {len(matches)}. The declaration moved, was renamed, or "
        "Prettier rewrote its shape — re-anchor this regex rather than deleting the parity test, "
        "which is the only thing pinning the TS copy to the Python authority."
    )
    return set(_MEMBER_RE.findall(matches[0]))


def test_graph_view_self_publishing_set_matches_the_python_authority() -> None:
    py_kinds = set(_IMPLICIT_SELF_PUBLISHING_NODE_TYPES)
    ts_kinds = _ts_self_publishing_node_types()

    missing_in_ts = py_kinds - ts_kinds
    missing_in_py = ts_kinds - py_kinds

    assert not missing_in_ts, (
        "Node kinds that publish implicitly in Python but not in "
        f"{_GRAPH_VIEW_PATH.name}: {sorted(missing_in_ts)}. The diagram will draw a correctly-wired "
        "node of that kind as publishing nothing, splitting a working pipeline into disconnected "
        "fragments. Add them to IMPLICIT_SELF_PUBLISHING_NODE_TYPES in that file."
    )
    assert not missing_in_py, (
        f"Node kinds declared self-publishing in {_GRAPH_VIEW_PATH.name} but absent from "
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
    text = _GRAPH_VIEW_PATH.read_text(encoding="utf-8")
    assert "function publishedSuccessConnection(" in text, (
        f"`publishedSuccessConnection` is gone from {_GRAPH_VIEW_PATH.name}. It is the TS mirror of "
        "`_producer_resolver.published_success_connection`; if the diagram now derives publication "
        "some other way, that path needs its own pin to the Python authority."
    )
    assert "IMPLICIT_SELF_PUBLISHING_NODE_TYPES.has(node.node_type)" in text, (
        f"`publishedSuccessConnection` in {_GRAPH_VIEW_PATH.name} no longer tests membership of "
        "IMPLICIT_SELF_PUBLISHING_NODE_TYPES. Asking `node.on_success` directly reports a "
        "non-terminal coalesce, an aggregation that omits on_success, and every queue as publishing "
        "nothing — the defect that drew a working pipeline as two disconnected fragments."
    )


def test_graph_view_file_is_readable() -> None:
    """Smoke test: the anchor path resolves and the regex matched real members."""
    assert _GRAPH_VIEW_PATH.is_file(), f"Expected GraphView at {_GRAPH_VIEW_PATH} — anchor path is wrong."
    assert _ts_self_publishing_node_types(), (
        f"No members parsed from IMPLICIT_SELF_PUBLISHING_NODE_TYPES in {_GRAPH_VIEW_PATH}. "
        "The regex or the file format has drifted, and the parity assertion above would be vacuous."
    )
