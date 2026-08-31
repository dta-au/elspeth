"""Demand backtrace for the source data-contract ask flow (elspeth-da68332faf).

Layer: L3 web application.

A graph requires certain fields from a source to exist at all. For a source
whose data cannot be preflight-checked (uploaded file, path-bound, external
fetch, continuous feed — anything without a ``source_authoring``-bound blob)
the guarantee cannot come from content: it is a forward-looking PROMISE the
user makes, surfaced as the ``source_data_contract`` interpretation card and
enforced per-row at runtime by ADR-016's ``SourceGuaranteedFieldsContract``.

This module computes the card's field set — the MINIMAL set of fields the
pipeline genuinely requires from one source — by DELTA-RUNNING the Stage-1
validator's own edge-contract accounting (``CompositionState.validate()``,
whose per-edge ``EdgeContract`` rows are produced by the composer twin of the
``core/dag/guarantees.py`` propagation walk). The demand is DERIVED, never
restated: a field is demanded exactly when it is missing on some edge today
AND stamping it into this source's ``schema.guaranteed_fields`` makes that
edge's miss go away through the transparent-node walk. By construction the
set can never contain a field no downstream consumer requires, and never
contains a field an intermediate node already guarantees or that this
source's guarantee cannot reach.

The helpers here are pure state math. The requirement-row-aware wrapper that
strips a previously acknowledged field set before recomputing lives in
``elspeth.web.interpretation_state`` (which owns the requirement-row
vocabulary); keeping this module free of requirement parsing avoids an
import cycle.
"""

from __future__ import annotations

import codecs
import csv
import io
import json
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.trust_boundary import observation_boundary
from elspeth.web.composer.state import SOURCE_AUTHORING_KEY, CompositionState, SourceSpec

# Stable user-facing label for the data-contract review row. The card's
# user-visible title ("Data contract") lives in the frontend renderer.
SOURCE_DATA_CONTRACT_USER_TERM: Final[str] = "source_data_contract"

# Wire/audit shape version for the canonical card draft JSON. Version 2
# re-binds acknowledgement authority after the user-facing consequence was
# corrected from quarantine-and-continue to ADR-016's fail-closed behaviour.
SOURCE_DATA_CONTRACT_DRAFT_VERSION: Final[int] = 2

# Stable semantic identity included in the acknowledgement artifact domain.
# A future consequence change must choose a new token and draft version: field
# equality alone cannot carry user authority across materially different
# consequences.
SOURCE_DATA_CONTRACT_ENFORCEMENT_SEMANTICS: Final[str] = "payload_and_emitted_contract_presence_fail_closed"

_SOURCE_DATA_CONTRACT_DRAFT_KEYS: Final[frozenset[str]] = frozenset(
    {"contract_version", "kind", "demanded_fields", "sample_header", "missing_from_sample"}
)
_LEGACY_SOURCE_DATA_CONTRACT_DRAFT_VERSION: Final[int] = 1

# Bounded read for the ILLUSTRATIVE sample header: the sample is evidence
# shown on the card, never the thing being ratified, so the read is
# best-effort and abstains (None) on any anomaly.
_SAMPLE_HEADER_READ_BYTES: Final[int] = 65536
_SAMPLE_HEADER_MAX_COLUMNS: Final[int] = 512
_SAMPLE_HEADER_MAX_CELL_CHARS: Final[int] = 512


def source_data_contract_artifact_hash(fields: Iterable[str]) -> str:
    """Canonical artifact hash binding current semantics and demand fields.

    Mirrors ``accepted_artifact_hash`` binding content for invented_source:
    the acknowledgement attests this contract kind, draft version,
    fail-closed consequence, and exact field set. A consequence/version or
    demand-set change after acknowledgement therefore re-opens the card. The
    sample header is deliberately NOT part of the domain — it is illustrative
    evidence, and a re-uploaded sample must not drift an otherwise identical
    accepted promise.
    """
    return stable_hash(
        {
            "review_kind": SOURCE_DATA_CONTRACT_USER_TERM,
            "contract_version": SOURCE_DATA_CONTRACT_DRAFT_VERSION,
            "enforcement_semantics": SOURCE_DATA_CONTRACT_ENFORCEMENT_SEMANTICS,
            "demanded_fields": sorted(fields),
        }
    )


def _legacy_source_data_contract_artifact_hash(fields: Iterable[str]) -> str:
    """Reproduce the v1 field-only hash for migration checks only."""
    return stable_hash({"review_kind": SOURCE_DATA_CONTRACT_USER_TERM, "demanded_fields": sorted(fields)})


@observation_boundary(
    tier=3,
    source="composer/LLM-authored source options mapping (Tier-3) whose schema block shape is unproven",
    source_param="options",
    suppresses=("R5",),
    invariant="returns a stamped copy or None to abstain; every malformed-shape branch abstains, never raises",
)
def stamp_source_options_with_guarantees(
    options: Mapping[str, Any],
    guaranteed_fields: Iterable[str],
) -> Mapping[str, Any] | None:
    """Merge ``guaranteed_fields`` into the options' schema block, or abstain.

    ``None`` means the source cannot honestly carry the stamp: an explicit
    ``schema.fields`` declaration or a non-observed mode is the author's own
    complete claim and this flow never rewrites it; a malformed schema block
    belongs to validation, not to this stamp. Unlike the bind-time
    auto-declare (``tools/sources.py``), an EXISTING ``guaranteed_fields``
    list is unioned rather than abstained over — the ask flow's stamp is an
    explicit user answer widening the source's promise, and re-acknowledging
    a re-opened card must be able to extend a previous acknowledgement.
    """
    schema_key = "schema" if "schema" in options else ("schema_config" if "schema_config" in options else "schema")
    raw_schema = options[schema_key] if schema_key in options else None
    if raw_schema is None:
        schema: dict[str, Any] = {"mode": "observed"}
    elif isinstance(raw_schema, Mapping):
        schema = dict(raw_schema)
    else:
        return None
    if "fields" in schema and schema["fields"]:
        return None
    if "mode" not in schema:
        schema["mode"] = "observed"
    if schema["mode"] != "observed":
        return None
    existing = schema["guaranteed_fields"] if "guaranteed_fields" in schema else ()
    if existing is None:
        existing = ()
    if not isinstance(existing, (list, tuple)) or not all(isinstance(field, str) for field in existing):
        return None
    merged = sorted({*existing, *guaranteed_fields})
    schema["guaranteed_fields"] = merged
    return {**options, schema_key: schema}


@observation_boundary(
    tier=3,
    source="composer/LLM-authored source options mapping (Tier-3) whose schema block shape is unproven",
    source_param="options",
    suppresses=("R5",),
    invariant="returns a stripped copy, or the input unchanged for any malformed shape — the demand walk "
    "abstains on malformed schema blocks, so passing them through never widens a claim",
)
def _source_options_without_guaranteed_fields(
    options: Mapping[str, Any],
    fields: frozenset[str],
) -> Mapping[str, Any]:
    """Return options with ``fields`` removed from ``schema.guaranteed_fields``.

    Used to recompute demand as if a previous acknowledgement had not been
    stamped, so a demand-set change is measured against the graph, not
    against the stamp the previous answer produced. An empty remainder stays
    as an explicit empty list: ``SchemaConfig`` distinguishes that
    participating vote from an absent-key abstention, which is material at a
    fan-in. Malformed shapes are returned unchanged — the demand walk abstains
    on them anyway.
    """
    if not fields:
        return options
    schema_key = "schema" if "schema" in options else ("schema_config" if "schema_config" in options else None)
    if schema_key is None:
        return options
    raw_schema = options[schema_key]
    if not isinstance(raw_schema, Mapping) or "guaranteed_fields" not in raw_schema:
        return options
    existing = raw_schema["guaranteed_fields"]
    if not isinstance(existing, (list, tuple)):
        return options
    remaining = [field for field in existing if not (isinstance(field, str) and field in fields)]
    schema = dict(raw_schema)
    schema["guaranteed_fields"] = remaining
    return {**options, schema_key: schema}


def _unsatisfied_edge_misses(state: CompositionState) -> dict[tuple[str, str], frozenset[str]]:
    """Per-edge missing required fields from Stage-1 validation's own ledger."""
    return {
        (contract.from_id, contract.to_id): frozenset(contract.missing_fields)
        for contract in state.validate().edge_contracts
        if not contract.satisfied
    }


def backtraced_source_demand(
    state: CompositionState,
    source_name: str,
    *,
    disregard_fields: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """Minimal field set the pipeline genuinely requires from ``source_name``.

    Delta derivation (see module docstring), generalized for fan-in (ruling
    on elspeth-da68332faf: queue/row_union fan-in is an AND over N
    INDEPENDENT per-source promises — every row comes from exactly one arm,
    so a requirement must be promised by EVERY feeding source, each for its
    own rows; the engine already intersects arms in
    ``walk_effective_guarantee_vote``):

    * baseline = the state with ``disregard_fields`` stripped from THIS
      source's guarantee (other sources keep their current guarantees);
    * H_all = every card-eligible source stamped with every baseline-missing
      field;
    * H_not_S = the same, except this source keeps its baseline options.

    A field is demanded of this source exactly when some edge misses it at
    baseline, the miss CLEARS under H_all (sufficiency: the eligible
    sources' promises, together, are what satisfies it through the
    transparent walk), and does NOT clear on that edge under H_not_S
    (necessity: this source's own promise is required — a source that does
    not feed the edge contributes nothing and is never asked). With a single
    eligible source this reduces exactly to the original solo-stamp delta:
    H_all is the solo stamp and H_not_S is the baseline.

    Fields the pipeline does not require never enter the baseline misses;
    fields an intermediate node guarantees are not missing; an INELIGIBLE
    source on an intersection path (explicit ``schema.fields``,
    non-observed mode, composer-authored content) is never stamped, so a
    miss behind it never clears and no card demands it — the shape stays
    fail-closed at validation with the ordinary edge-contract advice.

    Returns ``()`` when there is no demand or when this source cannot carry
    a guarantee stamp at all — a card that acknowledgement could not
    resolve must never be staged.
    """
    source = state.sources[source_name] if source_name in state.sources else None
    if source is None or SOURCE_AUTHORING_KEY in source.options:
        return ()
    baseline_options = _source_options_without_guaranteed_fields(source.options, disregard_fields)
    if stamp_source_options_with_guarantees(baseline_options, ()) is None:
        return ()
    baseline_state = _state_with_source_options(state, source_name, source, baseline_options)
    baseline_misses = _unsatisfied_edge_misses(baseline_state)
    if not baseline_misses:
        return ()
    all_missing = frozenset().union(*baseline_misses.values())

    # Card-eligible sources, judged on the BASELINE state (so this source's
    # eligibility reflects its stripped options).
    stamped_by_name: dict[str, Mapping[str, Any]] = {}
    for name, candidate in baseline_state.sources.items():
        if SOURCE_AUTHORING_KEY in candidate.options:
            continue
        stamped = stamp_source_options_with_guarantees(candidate.options, sorted(all_missing))
        if stamped is None:
            continue
        stamped_by_name[name] = stamped
    if source_name not in stamped_by_name:
        return ()

    h_all_misses = _unsatisfied_edge_misses(_state_with_stamped_sources(baseline_state, stamped_by_name))
    if len(stamped_by_name) == 1:
        # Single eligible source: H_not_S IS the baseline — skip the third
        # validation run and keep the original solo-delta cost.
        h_not_s_misses = baseline_misses
    else:
        without_s = {name: options for name, options in stamped_by_name.items() if name != source_name}
        h_not_s_misses = _unsatisfied_edge_misses(_state_with_stamped_sources(baseline_state, without_s))

    demand: set[str] = set()
    for edge, missing in baseline_misses.items():
        cleared_by_all = missing - (h_all_misses[edge] if edge in h_all_misses else frozenset())
        still_missing_without_s = h_not_s_misses[edge] if edge in h_not_s_misses else frozenset()
        demand |= cleared_by_all & still_missing_without_s
    return tuple(sorted(demand))


def _state_with_stamped_sources(
    state: CompositionState,
    stamped_by_name: Mapping[str, Mapping[str, Any]],
) -> CompositionState:
    if not stamped_by_name:
        return state
    sources = dict(state.sources)
    for name, options in stamped_by_name.items():
        sources[name] = replace(sources[name], options=options)
    return replace(state, sources=sources)


def _state_with_source_options(
    state: CompositionState,
    source_name: str,
    source: SourceSpec,
    options: Mapping[str, Any],
) -> CompositionState:
    if options is source.options:
        return state
    sources = dict(state.sources)
    sources[source_name] = replace(source, options=options)
    return replace(state, sources=sources)


@observation_boundary(
    tier=3,
    source="source options ('path'/'file', 'delimiter', 'encoding') persisted on composer state, and the "
    "bytes of the operator-uploaded file those options point at",
    source_param="source",
    suppresses=("R1", "R5"),
    invariant="returns a bounded header tuple or None to abstain; every unreadable/undecodable/over-limit "
    "branch abstains, never raises — the sample is illustrative card evidence, not a guarantee input",
)
def sample_header_for_source(source: SourceSpec) -> tuple[str, ...] | None:
    """Best-effort ILLUSTRATIVE sample header for a bound csv source.

    The card shows the sample beside the demanded fields and warns on any
    demanded field the sample does not show ("your data doesn't appear to
    have this — fix the data or change the pipeline"). The sample is never
    ratified and never feeds the guarantee stamp, so this read abstains on
    any anomaly rather than raising: a non-csv plugin, an unreadable or
    undecodable file, an over-limit row.
    """
    if source.plugin != "csv":
        return None
    options = source.options
    path_value = options.get("path") if "path" in options else options.get("file") if "file" in options else None
    if not isinstance(path_value, str) or not path_value:
        return None
    delimiter_value = options.get("delimiter")
    delimiter = delimiter_value if isinstance(delimiter_value, str) and len(delimiter_value) == 1 else ","
    encoding_value = options.get("encoding")
    encoding = encoding_value if isinstance(encoding_value, str) and encoding_value else "utf-8"
    try:
        with Path(path_value).open("rb") as handle:
            raw = handle.read(_SAMPLE_HEADER_READ_BYTES + 1)
    except OSError:
        return None
    truncated = len(raw) > _SAMPLE_HEADER_READ_BYTES
    try:
        decoder = codecs.getincrementaldecoder(encoding)()
        text = decoder.decode(raw[:_SAMPLE_HEADER_READ_BYTES], final=not truncated)
    except (LookupError, UnicodeDecodeError, ValueError):
        return None
    try:
        for row in csv.reader(io.StringIO(text), delimiter=delimiter, strict=True):
            if not row or all(not cell.strip() for cell in row):
                continue
            if len(row) > _SAMPLE_HEADER_MAX_COLUMNS or any(len(cell) > _SAMPLE_HEADER_MAX_CELL_CHARS for cell in row):
                return None
            return tuple(cell.strip() for cell in row)
    except csv.Error:
        return None
    return None


def build_source_data_contract_draft(
    demanded_fields: Iterable[str],
    sample_header: tuple[str, ...] | None,
) -> str:
    """Render the canonical, server-computed card draft.

    Deterministic compact JSON (sorted keys) so draft equality checks at the
    tool boundary and the writer boundary compare the same bytes. The
    commitment wording — "whatever I feed this pipeline will carry these
    columns" — and the fail-closed consequence of breaking that producer
    guarantee are frontend card copy, not draft payload: the draft carries the
    FACTS (demanded fields, sample evidence, per-field sample misses), the
    renderer carries the prose. Source-validation quarantine is a distinct,
    earlier path whose rows never reach ADR-016's boundary check.
    """
    demanded = sorted(demanded_fields)
    missing_from_sample = sorted(set(demanded) - set(sample_header)) if sample_header is not None else []
    payload = {
        "contract_version": SOURCE_DATA_CONTRACT_DRAFT_VERSION,
        "kind": SOURCE_DATA_CONTRACT_USER_TERM,
        "demanded_fields": demanded,
        "sample_header": list(sample_header) if sample_header is not None else None,
        "missing_from_sample": missing_from_sample,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


@observation_boundary(
    tier=3,
    source="a persisted interpretation requirement's accepted_value / a persisted interpretation event's "
    "accepted draft text, round-tripped through sessions.db storage",
    source_param="value",
    suppresses=("R5",),
    invariant="returns the parsed demanded-field tuple or None to abstain; every malformed branch abstains. "
    "Abstention strips nothing from the recomputed demand, so the card can stay closed only when "
    "the independently stored accepted_artifact_hash exactly matches the full current demand — the "
    "hash, never this parse, is the acknowledgement authority",
)
def parse_source_data_contract_accepted_fields(value: str) -> tuple[str, ...] | None:
    """Parse fields only from a complete, current contract draft."""
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    return _validated_source_data_contract_fields(payload, version=SOURCE_DATA_CONTRACT_DRAFT_VERSION)


def _validated_source_data_contract_fields(
    payload: Mapping[str, Any],
    *,
    version: int,
) -> tuple[str, ...] | None:
    """Validate the complete canonical draft shape for one known version."""
    if frozenset(payload) != _SOURCE_DATA_CONTRACT_DRAFT_KEYS:
        return None
    raw_version = payload["contract_version"]
    if type(raw_version) is not int or raw_version != version:
        return None
    if payload["kind"] != SOURCE_DATA_CONTRACT_USER_TERM:
        return None
    demanded = payload["demanded_fields"]
    if not isinstance(demanded, list) or not all(isinstance(field, str) for field in demanded):
        return None
    sample_header = payload["sample_header"]
    if sample_header is not None and (not isinstance(sample_header, list) or not all(isinstance(field, str) for field in sample_header)):
        return None
    missing_from_sample = payload["missing_from_sample"]
    if not isinstance(missing_from_sample, list) or not all(isinstance(field, str) for field in missing_from_sample):
        return None
    if not set(missing_from_sample) <= set(demanded):
        return None
    return tuple(demanded)


@observation_boundary(
    tier=3,
    source="a persisted pre-v2 source_data_contract event's immutable llm_draft",
    source_param="value",
    suppresses=("R5",),
    invariant="returns fields only for the complete legacy-v1 draft shape; malformed or current-version payloads "
    "return None, and callers may use the result only to supersede the old pending card with a current review",
)
def parse_legacy_source_data_contract_fields(value: str) -> tuple[str, ...] | None:
    """Parse exact v1 fields solely to migrate pending pre-v2 cards."""
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    return _validated_source_data_contract_fields(
        payload,
        version=_LEGACY_SOURCE_DATA_CONTRACT_DRAFT_VERSION,
    )


@observation_boundary(
    tier=3,
    source="a persisted resolved source_data_contract requirement's accepted_value and accepted_artifact_hash",
    source_param="value",
    suppresses=("R5",),
    invariant="returns fields only for a complete current-v2 artifact or an exact coherent legacy-v1 artifact; "
    "legacy fields are used solely to remove the old stamp while recomputing a resolvable current review demand, "
    "never to admit execution",
)
def source_data_contract_fields_for_demand_recompute(
    value: str,
    artifact_hash: str | None,
) -> tuple[str, ...] | None:
    """Recover current or coherent-v1 fields solely for demand recomputation.

    V1 evidence remains historically valid evidence of what the user saw; it
    is not corrupt and is not silently rewritten. Its field-only artifact did
    not bind today's fail-closed consequence, however, so the execution
    authority parser above rejects it. This migration-only parser lets the
    demand walk remove the v1 guarantee stamp and re-derive a new v2 card the
    user can actually resolve.
    """
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    current_fields = _validated_source_data_contract_fields(
        payload,
        version=SOURCE_DATA_CONTRACT_DRAFT_VERSION,
    )
    if current_fields is not None:
        return current_fields if artifact_hash == source_data_contract_artifact_hash(current_fields) else None
    legacy_fields = _validated_source_data_contract_fields(
        payload,
        version=_LEGACY_SOURCE_DATA_CONTRACT_DRAFT_VERSION,
    )
    if legacy_fields is None:
        return None
    return legacy_fields if artifact_hash == _legacy_source_data_contract_artifact_hash(legacy_fields) else None
