"""One classifier for a declared-input miss, shared by the transform and batch input seams.

ADR-013 Amendment 2026-09-27 (elspeth-5887fb7928 R2). A node's input
declaration names fields every arriving row must carry. The build settles
what it can (``core.dag.schema_validation.declared_input_disposition``): it
REFUSES a pipeline whose upstream certainly omits a declared field, and it
publishes the fields it PROVED present (``ExecutionGraph.get_declared_input_proof``).
Everything else — a field behind an ``observed`` or otherwise open upstream —
is settled on the row, and a row that misses declared fields is one of three
cases:

- ``proven``: some missing field is one the build proved present. Every such
  field is backed by an upstream val (ADR-016 source guarantees, ADR-011
  declared outputs, ADR-008/009 pass-through), so a row without it can only be
  our bug — a contract-propagation, merge or restore defect. Tier 1.
- ``divergent``: some missing field is absent from the row's CONTRACT but
  present in its PAYLOAD. The data has the field and the contract lost it —
  also our bug, and the case an unproven-looking miss would otherwise hide.
  Tier 1.
- ``absent``: every missing field is unproven and absent from the payload.
  The declaration was derived from options the build could not settle against
  the upstream, so the miss is a fact about this row. Routed via ``on_error``
  (``DeclaredInputFieldAbsentViolation``, reason ``missing_field``).

The test is over the WHOLE missing set: a row with any proven or divergent
field among its misses is Tier 1 on the full set, never split into a routed
and an aborting part.

The two seams feed it the same inputs — the missing set against the row's
effective fields (contract names ∩ payload keys,
``contracts.declaration_contracts.derive_effective_input_fields``), the node's
proof entry and the payload's own keys — so a miss has one disposition
whichever seam sees it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.types import NodeID

DeclaredInputMissKind = Literal["proven", "divergent", "absent"]


def classify_declared_input_miss(
    *,
    missing: frozenset[str],
    proven: frozenset[str],
    payload_keys: frozenset[str],
) -> DeclaredInputMissKind:
    """Classify a non-empty declared-input miss (see the module docstring).

    Args:
        missing: The declared fields absent from the row's effective fields.
        proven: The node's proof entry — the declared fields the build proved
            present on every arriving row.
        payload_keys: The row payload's own keys (``PipelineRow.keys()``), the
            same key set the effective-field derivation intersects.

    Raises:
        OrchestrationInvariantError: ``missing`` is empty — there is no miss to
            classify, and a caller asking is wired wrong.
    """
    if not missing:
        raise OrchestrationInvariantError("classify_declared_input_miss called with no missing field; there is no miss to classify.")
    if missing & proven:
        return "proven"
    if missing & payload_keys:
        return "divergent"
    return "absent"


def declared_input_proof_entry(
    proof: Mapping[NodeID, frozenset[str]],
    *,
    node_id: NodeID,
    declared: frozenset[str],
    component: str,
) -> frozenset[str]:
    """The node's entry in the build's declared-input proof, verified against its declaration.

    The builder publishes an entry for every node that enforces an input
    declaration (``compute_declared_input_proof``). A missing entry is never
    read as "proves nothing": that default would route every miss, including
    the proven ones that expose engine defects.

    Raises:
        OrchestrationInvariantError: the node has no entry, or its entry names
            a field outside the node's own declaration (the proof and the
            running plugin disagree about what the node declares).
    """
    if node_id not in proof:
        raise OrchestrationInvariantError(
            f"{component} (node {node_id!r}) declares input fields {sorted(declared)} but the build's declared-input "
            "proof has no entry for it; the proof is published for every transform, aggregation and collector node "
            "of the executed graph."
        )
    proven = proof[node_id]
    if not proven <= declared:
        raise OrchestrationInvariantError(
            f"{component} (node {node_id!r}): the build's declared-input proof names fields {sorted(proven - declared)} "
            f"that the node does not declare (declared {sorted(declared)}); the graph and the running plugin disagree."
        )
    return proven
