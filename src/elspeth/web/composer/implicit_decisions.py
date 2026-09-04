"""Structured implicit-decision report for composer-authored states.

Layer: L3 (application).

The composer skill asks the model to tell the operator which choices it made
on their behalf. This module provides the persisted counterpart: a compact,
state-derived report that survives reload and can be inspected by auditors.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, TypedDict

from elspeth.contracts.composer_interpretation import InterpretationKind
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.guided.protocol import BLOB_REF_PATH_PREFIX
from elspeth.web.composer.guided_blob_refs import GUIDED_REVIEWED_BLOB_PATH_KEYS, validate_guided_reviewed_blob_ref
from elspeth.web.composer.redaction import REDACTED_BLOB_SOURCE_PATH
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, SourceSpec
from elspeth.web.interpretation_state import (
    REQUIRED_CONTROL_AUTO_WIRED_USER_TERM,
    SOURCE_AUTHORING_KEY,
    parse_interpretation_requirements,
    resolved_source_data_contract_fields,
)
from elspeth.web.plugin_policy.validation import _PROFILE_LOWERING_METADATA_OPTION_KEYS

DecisionCategory = Literal[
    "error_routing",
    "identity",
    "model",
    "output",
    "plugin_option",
    "policy_control",
    "source",
]
DecisionProvenance = Literal[
    "composer_selected",
    "default",
    "derived_from_content",
    "explicit_source_required",
    "picked",
    "policy_required",
    "server_stamped",
    "user_acknowledged",
]


class ImplicitDecisionEntry(TypedDict, total=False):
    path: str
    value: object
    category: DecisionCategory
    provenance: DecisionProvenance
    candidate_alternatives: list[object]
    note: str


class ImplicitDecisionsReport(TypedDict):
    schema_version: int
    entries: list[ImplicitDecisionEntry]
    normalization_events: list[dict[str, object]]


_FORMAT_ALTERNATIVES = ["html", "markdown", "text"]
_ALLOWED_HOSTS_ALTERNATIVES = ["public_only", "same_site", "explicit_allowlist"]
_COLLISION_POLICY_ALTERNATIVES = ["fail", "overwrite", "auto_increment"]
_ROUTING_ALTERNATIVES = ["discard", "named_sink"]
_MODEL_PROVIDER_ALTERNATIVES = ["openrouter", "azure_openai"]

# The blob storage-path carriers, shared verbatim with the guided reviewed-source
# reader and (as of elspeth-b5180a9630) ``redact_source_storage_path``, which
# previously kept its own ``("path", "file")`` literal: blob ownership detection
# and the fork rewrite treat ``path`` and ``file`` equivalently, so one carrier
# list has to serve every masking surface or the surface holding a stale copy
# leaks a source authored with the other shape.
_STORAGE_PATH_CARRIER_KEYS = GUIDED_REVIEWED_BLOB_PATH_KEYS


def build_implicit_decisions_report(state: CompositionState) -> ImplicitDecisionsReport:
    """Return a machine-readable disclosure report for a composer state.

    The report is intentionally derived from the final state rather than from
    model prose. That means it is conservative about provenance: when the final
    state alone cannot prove whether a value came from the operator or a
    deployment identity record, the entry says ``explicit_source_required``.
    """

    entries: list[ImplicitDecisionEntry] = []
    for source in state.sources.values():
        entries.extend(_source_entries(source))
    for node in state.nodes:
        entries.extend(_node_entries(node))
    for output in state.outputs:
        entries.extend(_output_entries(output))

    return {
        "schema_version": 1,
        "entries": entries,
        "normalization_events": [],
    }


def merge_implicit_decisions_meta(
    composer_meta: Mapping[str, Any] | None,
    state: CompositionState,
) -> dict[str, object]:
    """Merge the current implicit-decision report into ``composer_meta``."""

    merged: dict[str, object] = dict(deep_thaw(composer_meta)) if composer_meta is not None else {}
    merged["implicit_decisions"] = build_implicit_decisions_report(state)
    return merged


def _source_entries(source: SourceSpec) -> list[ImplicitDecisionEntry]:
    entries = [
        _entry(
            f"source.{field_path}",
            value,
            category=_category_for_source_option(source, field_path),
            provenance=_source_provenance(source, field_path, value),
            note=_note_for_source_option(source, field_path, value),
        )
        for field_path, value in _flatten_options(
            source.options,
            storage_path_sentinel=_blob_storage_path_sentinel(source.options),
        )
    ]
    entries.append(
        _entry(
            "source.on_validation_failure",
            source.on_validation_failure,
            category="error_routing",
            provenance=_routing_provenance(source.on_validation_failure),
            candidate_alternatives=_ROUTING_ALTERNATIVES,
        )
    )
    return entries


def _node_entries(node: NodeSpec) -> list[ImplicitDecisionEntry]:
    node_id = node.id
    entries = [
        _entry(
            f"node.{node_id}.options.{field_path}",
            value,
            category=_category_for_node_option(node, field_path),
            provenance=_provenance_for_path(f"node.{node_id}.options.{field_path}", value),
            candidate_alternatives=_candidate_alternatives(field_path),
            note=_note_for_node_option(node, field_path),
        )
        for field_path, value in _flatten_options(node.options)
    ]
    if node.on_error is not None:
        entries.append(
            _entry(
                f"node.{node_id}.on_error",
                node.on_error,
                category="error_routing",
                provenance=_routing_provenance(node.on_error),
                candidate_alternatives=_ROUTING_ALTERNATIVES,
            )
        )
    if _is_auto_wired_control(node):
        # Server-inserted required control (R2-F10): the whole NODE is a
        # decision made on the operator's behalf by deployment policy, so it
        # gets one dedicated entry over and above its per-option rows. The
        # detection is state-derived — the staged required_control_auto_wired
        # disclosure row — never a node-id naming convention.
        entries.append(
            _entry(
                f"node.{node_id}.auto_wired_control",
                node.plugin,
                category="policy_control",
                provenance="policy_required",
                note=(
                    "Control node inserted automatically because deployment policy REQUIRES "
                    "this control; acknowledged through the required_control_auto_wired "
                    "pipeline_decision review."
                ),
            )
        )
    return entries


def _is_auto_wired_control(node: NodeSpec) -> bool:
    """Detect a server-inserted required control by its staged disclosure row."""
    requirements = parse_interpretation_requirements(node.options)
    return requirements is not None and any(row["user_term"] == REQUIRED_CONTROL_AUTO_WIRED_USER_TERM for row in requirements)


def _output_entries(output: OutputSpec) -> list[ImplicitDecisionEntry]:
    output_name = output.name
    entries = [
        _entry(
            f"output.{output_name}.options.{field_path}",
            value,
            category="output",
            provenance=_provenance_for_path(f"output.{output_name}.options.{field_path}", value),
            candidate_alternatives=_candidate_alternatives(field_path),
        )
        for field_path, value in _flatten_options(output.options)
    ]
    entries.append(
        _entry(
            f"output.{output_name}.on_write_failure",
            output.on_write_failure,
            category="error_routing",
            provenance=_routing_provenance(output.on_write_failure),
            candidate_alternatives=_ROUTING_ALTERNATIVES,
        )
    )
    return entries


def _blob_storage_path_sentinel(options: Mapping[str, Any]) -> str | None:
    """Return the wire sentinel to record for a blob-backed source's path carriers.

    ``None`` means "not blob-backed": the source's ``path``/``file`` is
    operator-authored configuration (YAML, manual ``set_source``) and belongs in
    the disclosure report verbatim. The trigger is the structural ``blob_ref``
    marker, exactly as in the sibling
    :func:`~elspeth.web.composer.redaction.redact_source_storage_path`.

    ``options`` is composer/LLM-authored (Tier 3, ADR-032), so the marker's VALUE
    is parsed rather than trusted: it is interpolated only after
    :func:`validate_guided_reviewed_blob_ref` proves it is a canonical UUID
    string — the same shape every other consumer requires (the YAML-export guard
    in ``routes/composer/state.py``, the guided reviewed-source reader). A
    ``str`` check alone would not be enough: a *path-shaped* ``blob_ref`` would
    then ride out through the sentinel itself as
    ``blob:/var/lib/elspeth/blobs/...``, reopening the leak inside the very
    value that exists to close it. Anything that fails validation degrades to
    the generic redaction sentinel.

    Degrade, not escalate — deliberately divergent from ``tools/blobs.py``'s
    ``AuditIntegrityError`` on the same impossible shape, and the divergence is
    the point. That function is the write-side custodian: it decides whether a
    blob may be mutated, so suppressing an anomaly there would defeat a binding
    guard and it must fail closed. This is a read-side reporter on a disclosure
    projection whose sole job is to keep a path off the wire; raising here would
    convert a corrupt-state anomaly into a 500 on every state read, denying the
    operator the very report that would let them see the corruption. Both
    postures are fail-closed for their own surface: refuse the mutation there,
    refuse to echo the value here.
    """
    if "blob_ref" not in options:
        return None
    try:
        blob_ref = validate_guided_reviewed_blob_ref(options["blob_ref"])
    except AuditIntegrityError:
        return REDACTED_BLOB_SOURCE_PATH
    return f"{BLOB_REF_PATH_PREFIX}{blob_ref}"


def _flatten_options(
    options: Mapping[str, Any],
    prefix: str = "",
    *,
    storage_path_sentinel: str | None = None,
) -> list[tuple[str, object]]:
    """Flatten an option mapping into ``(dotted_path, value)`` disclosure pairs.

    When ``storage_path_sentinel`` is set, the TOP-LEVEL blob storage-path
    carriers are recorded as that sentinel instead of their filesystem value, so
    for a ``blob_ref``-bearing source an internal ``/var/lib/elspeth/blobs/...``
    path never enters ``composer_meta`` in the first place
    (elspeth-b5180a9630). Redacting at the write boundary rather than in each
    outbound serializer is what makes THAT class can't-regress: for those
    sources there is no raw path left downstream to forget to mask.

    The claim is scoped to blob_ref-bearing sources on purpose. A guided commit
    strips ``blob_ref`` from the executable source (it cannot prove
    ``path == storage_path``), so its raw path DOES still enter
    ``composer_meta`` and remains protected by the outbound projection in
    ``redact_guided_snapshot_storage_paths`` (``redaction.py``, the
    ``private_path_projections`` block). Two mechanisms, two populations —
    neither subsumes the other, and removing either reopens a leak.

    Only ``prefix == ""`` keys are substituted. A nested ``<group>.path`` is an
    unrelated plugin option, not a blob binding — the same top-level-only scope
    the sibling ``redact_source_storage_path`` uses, and the scope the guided
    projection's ``{"source.path", "source.file"}`` literals assume.
    """
    flattened: list[tuple[str, object]] = []
    for key in sorted(options):
        value = options[key]
        path = f"{prefix}.{key}" if prefix else str(key)
        if storage_path_sentinel is not None and not prefix and key in _STORAGE_PATH_CARRIER_KEYS:
            flattened.append((path, storage_path_sentinel))
        elif isinstance(value, Mapping):
            flattened.extend(_flatten_options(value, path))
        else:
            flattened.append((path, deep_thaw(value)))
    return flattened


def _entry(
    path: str,
    value: object,
    *,
    category: DecisionCategory,
    provenance: DecisionProvenance,
    candidate_alternatives: Sequence[object] | None = None,
    note: str | None = None,
) -> ImplicitDecisionEntry:
    entry: ImplicitDecisionEntry = {
        "path": path,
        "value": deep_thaw(value),
        "category": category,
        "provenance": provenance,
    }
    if candidate_alternatives is not None:
        entry["candidate_alternatives"] = list(candidate_alternatives)
    if note is not None:
        entry["note"] = note
    return entry


def _category_for_node_option(node: NodeSpec, field_path: str) -> DecisionCategory:
    if node.plugin == "web_scrape" and field_path in {"http.abuse_contact", "http.scraping_reason"}:
        return "identity"
    if node.plugin == "llm" and field_path in {"provider", "model", "temperature", "pool_size"}:
        return "model"
    return "plugin_option"


def _category_for_source_option(source: SourceSpec, field_path: str) -> DecisionCategory:
    if source.plugin == "llm" and field_path in {"provider", "model", "temperature", "pool_size"}:
        return "model"
    return "source"


def _is_content_derived_guarantee(source: SourceSpec, field_path: str) -> bool:
    """Detect the bind-time content-derived ``schema.guaranteed_fields`` stamp.

    The stamp exists only on LLM-AUTHORED blob-bound sources —
    ``SOURCE_AUTHORING_KEY`` is the structural marker the bind tools write for
    exactly that evidence class (John's ruling, 2026-08-27: an uploaded
    source's header is a sample and never auto-declares). The final state
    alone cannot distinguish a planner-authored guarantee on such a source
    from the derived one, so this attribution is conservative in the report's
    documented sense: for an authored bound blob the guarantee is evidenced by
    the file's content either way — the bind path refuses to stamp anything
    the content does not carry per row.
    """
    return field_path in ("schema.guaranteed_fields", "schema_config.guaranteed_fields") and SOURCE_AUTHORING_KEY in source.options


def _is_structural_blob_rows_guarantee(source: SourceSpec, field_path: str) -> bool:
    """Detect the blob_rows fixed-row-field guarantee stamped at bind time.

    blob_rows fabricates every row as exactly the plugin's five fixed custody
    fields (blob_rows.py ``load()`` row construction), so the stamped claim is
    plugin-contract truth — server-derived, never the planner's assertion.
    """
    return (
        field_path in ("schema.guaranteed_fields", "schema_config.guaranteed_fields")
        and source.plugin == "blob_rows"
        and "blobs" in source.options
    )


def _acknowledged_guarantee_fields(source: SourceSpec, field_path: str) -> frozenset[str]:
    """Return the coherent subset stamped by a data-contract answer.

    The structural marker is the resolved ``source_data_contract``
    interpretation requirement the resolve arm upserts beside the stamp
    (sessions/service.py::_resolve_source_data_contract): the user
    acknowledged the graph's demanded field set as a forward-looking promise
    about rows that will arrive (elspeth-da68332faf work item 2). Checked
    before the content arm — an acknowledged uploaded source never carries
    ``source_authoring``, so the two markers are disjoint today, but the
    ordering keeps the USER ANSWER attribution authoritative if that ever
    changes.
    """
    if field_path not in ("schema.guaranteed_fields", "schema_config.guaranteed_fields"):
        return frozenset()
    requirements = parse_interpretation_requirements(source.options)
    if requirements is None:
        return frozenset()
    for row in requirements:
        if InterpretationKind(row["kind"]) is not InterpretationKind.SOURCE_DATA_CONTRACT:
            continue
        acknowledged = resolved_source_data_contract_fields(row)
        if acknowledged is not None:
            return frozenset(acknowledged)
    return frozenset()


def _string_field_set(value: object) -> frozenset[str] | None:
    if not isinstance(value, (list, tuple)) or not all(isinstance(field, str) for field in value):
        return None
    return frozenset(value)


def _is_user_acknowledged_guarantee(source: SourceSpec, field_path: str, value: object) -> bool:
    acknowledged = _acknowledged_guarantee_fields(source, field_path)
    declared = _string_field_set(value)
    return bool(acknowledged) and declared == acknowledged


def _source_provenance(source: SourceSpec, field_path: str, value: object) -> DecisionProvenance:
    if _is_user_acknowledged_guarantee(source, field_path, value):
        return "user_acknowledged"
    if _is_content_derived_guarantee(source, field_path) or _is_structural_blob_rows_guarantee(source, field_path):
        return "derived_from_content"
    return _provenance_for_path(f"source.{field_path}", value)


def _note_for_source_option(source: SourceSpec, field_path: str, value: object) -> str | None:
    if _is_user_acknowledged_guarantee(source, field_path, value):
        return (
            "Guaranteed fields stamped from the user's data-contract "
            "acknowledgement — the user's answer, not the planner, is the "
            "evidence for this claim; enforced per-row at runtime (ADR-016)."
        )
    acknowledged = _acknowledged_guarantee_fields(source, field_path)
    declared = _string_field_set(value)
    if acknowledged and declared is not None and acknowledged < declared:
        acknowledged_text = ", ".join(sorted(acknowledged))
        authored_text = ", ".join(sorted(declared - acknowledged))
        return (
            f"The user's data-contract acknowledgement covers only: {acknowledged_text}. "
            f"Independently composer-authored guarantees: {authored_text}. Every declared guarantee "
            "is enforced per-row at runtime (ADR-016)."
        )
    if _is_content_derived_guarantee(source, field_path):
        return (
            "Guaranteed fields derived from the bound file's own content at bind "
            "time — the content, not the planner, is the evidence for this claim."
        )
    if _is_structural_blob_rows_guarantee(source, field_path):
        return (
            "Guaranteed fields derived from the blob_rows plugin's fixed row "
            "shape at bind time — the plugin contract, not the planner, is the "
            "evidence for this claim."
        )
    return None


def _is_server_stamped_option_path(path: str) -> bool:
    """Detect a disclosure path rooted at a server-owned options key.

    The key inventory derives from the same constants the write gates and the
    planner projection use (``AUTHORING_METADATA_OPTION_KEYS`` plus the
    profile-lowering superset carrying ``resolved_prompt_template_hash``) —
    never a hand-rolled list. Only the TOP-LEVEL options segment counts:
    source paths are ``source.<key>...``; node/output paths are
    ``<prefix>.options.<key>...``.
    """
    if path.startswith("source."):
        remainder = path.removeprefix("source.")
    else:
        _prefix, sep, remainder = path.partition(".options.")
        if not sep:
            return False
    root = remainder.split(".", 1)[0]
    return root in _PROFILE_LOWERING_METADATA_OPTION_KEYS


def _provenance_for_path(path: str, value: object) -> DecisionProvenance:
    if _is_server_stamped_option_path(path):
        # source_authoring.* / interpretation_requirements /
        # prompt_template_parts / resolved_prompt_template_hash are written by
        # ELSPETH's provenance and review machinery, never chosen by the
        # planner — attributing them to the composer misinforms the auditor
        # (elspeth-c67fbbbd83).
        return "server_stamped"
    if path.endswith(".http.abuse_contact") or path.endswith(".http.scraping_reason"):
        return "explicit_source_required"
    if path.endswith(".allowed_hosts") and value == "public_only":
        return "default"
    if path.endswith(".collision_policy") and value == "auto_increment":
        return "default"
    if path.endswith(".temperature") or path.endswith(".pool_size") or path.endswith(".model") or path.endswith(".provider"):
        return "picked"
    return "composer_selected"


def _routing_provenance(value: object) -> DecisionProvenance:
    # "discard" reaches NodeSpec/OutputSpec/SourceSpec through the composer
    # tool layer's default-fill (tools/transforms.py, tools/outputs.py,
    # tools/_common.py), which erases whether the planner asked for it — so
    # the disclosure layer cannot claim the value was picked. "default" is
    # the honest label until the default-fill is removed
    # (elspeth-0aace271b4 I4); a named sink can only come from the planner.
    if value == "discard":
        return "default"
    return "composer_selected"


def _candidate_alternatives(field_path: str) -> list[object] | None:
    if field_path == "format":
        return list(_FORMAT_ALTERNATIVES)
    if field_path.endswith("allowed_hosts"):
        return list(_ALLOWED_HOSTS_ALTERNATIVES)
    if field_path == "collision_policy":
        return list(_COLLISION_POLICY_ALTERNATIVES)
    if field_path == "provider":
        return list(_MODEL_PROVIDER_ALTERNATIVES)
    return None


def _note_for_node_option(node: NodeSpec, field_path: str) -> str | None:
    if node.plugin == "web_scrape" and field_path in {"http.abuse_contact", "http.scraping_reason"}:
        return (
            "Wire-visible identity value; must come from the operator, deployment identity, "
            "tool result, or the public-fetch fallback surfaced through a pipeline_decision "
            "review per pipeline_composer.md."
        )
    return None
