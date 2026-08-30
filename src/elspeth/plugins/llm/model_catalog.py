"""Provider-neutral LLM model catalog reader (L3).

Single source of truth for the OpenRouter model catalog. Both the
composer ``list_models`` tool and the value-source compliance walker
resolve catalog membership through this module so validate-time and
discovery-time agree on what "a known model" means.

Catalog population (two-tier):

* **Live snapshot, taken once at web-server startup.** The FastAPI
  lifespan calls :func:`prime_openrouter_catalog_from_live` with an
  httpx getter; the module records the snapshot in a process-global
  cache. The validate path remains network-free — it reads the
  populated module-level cache without further HTTP. This closes the
  validate/runtime drift bug: composer picks from the same set the
  walker checks against, and that set IS what OpenRouter currently
  serves (not what litellm bundled at a snapshot in time).

* **Bundled litellm fallback.** When the live snapshot is absent (this
  module imported outside a web-server lifespan — CLI invocations,
  unit tests, validate-only entry points; or the boot probe failed and
  logged a warning), :func:`_read_openrouter_catalog` returns the
  OpenRouter slice of :func:`read_litellm_model_list`. The fallback is
  intentional and load-bearing: staging must boot even if OpenRouter
  is briefly unreachable, and the CLI must validate offline.

The fallback is NOT a "best effort" — it is a documented design
choice. Do not remove it on the grounds that "we should always have
the live catalog"; the CLI does not run a lifespan, and tests must not
require network. The trade-off (offline = bundled, online = live) is
the *point*.

Re-validating a persisted run against a later boot's catalog is not
equivalent to original validation: the catalog snapshot at the time of
validation is part of the validation context. The Landscape ``runs``
table records ``openrouter_catalog_sha256`` and
``openrouter_catalog_source`` (see :mod:`elspeth.core.landscape`) so an
auditor can reconstruct which catalog blessed any historical decision.
The sha is canonical (sorted utf-8 ids joined on ``\\n``) so it is
invariant under prime-order and stable across processes;
:func:`read_openrouter_catalog_snapshot_id` exposes the current
process's ``(sha256_hex, source)`` tuple for the run-create writer to
persist.

If ``litellm`` is not installed, the bundled fallback is empty; the
walker translates an empty catalog into a structured "install
``elspeth[llm]``" message via :data:`MODEL_CATALOG_OPENROUTER` and the
``missing_dep_hint`` registered below.

Registry primitives live in :mod:`elspeth.contracts.value_source` (L0).
This module registers OpenRouter at import time so the walker (L2) can
look up the catalog without importing anything from the plugin layer.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import httpx
import structlog

from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.contracts.value_source import register_catalog_reader

# LiteLLM fetches its model-cost map from raw.githubusercontent.com at
# ``import litellm`` time unless told to use the copy bundled with the
# installed package. A deployment must not silently egress to third
# parties it never configured (elspeth-c67ba40e4a), so force the local
# map here — this module is ELSPETH's single point of truth for litellm
# access and is imported (via the web app / the LLM transform's provider
# modules) before any lazy ``import litellm`` in this repo runs.
# ``setdefault`` preserves an explicit operator override: setting
# ``LITELLM_LOCAL_MODEL_COST_MAP`` to anything other than ``"True"`` in
# the deployment environment re-enables litellm's remote fetch.
# Trade-off: litellm-calculated response costs are priced from the map
# bundled with the pinned litellm version rather than live prices —
# deterministic and audit-reproducible, at the cost of staleness for
# models newer than the installed litellm.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

__all__ = [
    "MODEL_CATALOG_OPENROUTER",
    "OPENROUTER_LITELLM_PREFIX",
    "OPENROUTER_MODELS_URL",
    "prime_openrouter_catalog_from_live",
    "read_litellm_model_list",
    "read_openrouter_catalog_snapshot_id",
    "reset_live_openrouter_catalog",
]


MODEL_CATALOG_OPENROUTER = "openrouter"
"""Catalog id for OpenRouter models (``openrouter/...`` prefix in litellm)."""


OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
"""Live OpenRouter catalog endpoint, unauthenticated GET.

OpenRouter exposes its full model list at this URL without an API key.
The web-server lifespan probes it once at boot to populate the live
snapshot used by the validate path.
"""


_slog = structlog.get_logger(__name__)


@lru_cache(maxsize=1)
def read_litellm_model_list() -> tuple[str, ...]:
    """Return the full sorted list of ``litellm.model_list`` entries.

    Single point of truth for ``litellm.model_list`` access. Both the
    composer ``list_models`` tool and the openrouter catalog reader use
    this — they cannot drift on what counts as "what litellm knows."

    Returns an empty tuple only when litellm itself is not installed (the
    LLM extra was not selected). The result is sorted so callers can rely
    on a stable order.

    A litellm that IS installed but fails its own import — a missing
    transitive dependency, a broken wheel — is not "litellm absent" and
    must not be reported as such: the walker's remediation hint for an
    empty catalog is "install ``elspeth[llm]``", which is the wrong
    instruction for a broken install. Only the absence of the ``litellm``
    module itself is tolerated here; every other import failure
    propagates. The same logic governs the catalog's SHAPE: a
    ``model_list`` that is not a list of str is litellm version drift on
    an installed package, so it raises rather than reading as an absent
    or narrowed catalog (the catalog feeds validate-time membership and a
    persisted digest — silent narrowing would corrupt both).
    """
    try:
        import litellm
    except ModuleNotFoundError as exc:
        if exc.name != "litellm":
            raise
        return ()
    return _parse_litellm_model_list(litellm.model_list)


@trust_boundary(
    tier=3,
    source=(
        "the installed litellm package's module-level `model_list` attribute — external package data whose shape litellm, not ELSPETH, owns"
    ),
    source_param="raw",
    suppresses=("R5",),
    invariant=(
        "raises TypeError when model_list is not a list or contains a non-str entry — "
        "version drift on an installed litellm is never read as an absent or narrowed catalog"
    ),
    test_ref="tests/unit/plugins/llm/test_model_catalog.py::test_parse_litellm_model_list_rejects_malformed_shapes",
    test_fingerprint="2561fb2cbc05b958e6be1972183864f2e669a20063abaa5d19f40074a6f00ffe",
)
def _parse_litellm_model_list(raw: Any) -> tuple[str, ...]:
    """Parse ``litellm.model_list`` into the owned sorted-tuple catalog form."""
    if not isinstance(raw, list):
        raise TypeError(
            f"litellm.model_list is {type(raw).__name__}, expected list — litellm version drift or a broken install, not an absent catalog"
        )
    for entry in raw:
        if not isinstance(entry, str):
            raise TypeError(
                f"litellm.model_list contains a {type(entry).__name__} entry, expected str — litellm version drift, not an absent catalog"
            )
    return tuple(sorted(raw))


OPENROUTER_LITELLM_PREFIX = "openrouter/"


# Module-global live snapshot. ``None`` = unprimed; the reader falls back
# to the bundled litellm slice. A ``frozenset`` instance = primed; the
# reader returns it directly. Mutation is guarded by ``_LIVE_CATALOG_LOCK``
# so the prime call and a concurrent reader from a validate request
# cannot race on a torn read.
_LIVE_CATALOG: frozenset[str] | None = None
# Canonical sha256 of the live catalog at prime time. Populated together
# with ``_LIVE_CATALOG`` under ``_LIVE_CATALOG_LOCK`` so the snapshot id
# never disagrees with the snapshot itself. ``None`` when unprimed; the
# reader exposes the bundled sha in that case.
_LIVE_CATALOG_SHA256: str | None = None
_LIVE_CATALOG_LOCK = threading.Lock()


def reset_live_openrouter_catalog() -> None:
    """Clear the live snapshot, returning the reader to fallback state.

    Test-only seam. Production code never calls this — the snapshot is
    populated once at boot and lives for the process lifetime (staging
    is restarted on deploys, which is when the catalog should refresh).
    Used by ``tests/unit/plugins/llm/test_model_catalog.py`` to enforce
    test-order independence.
    """
    _clear_live_openrouter_catalog()


def _clear_live_openrouter_catalog() -> None:
    """Clear the process-global live snapshot under the catalog lock."""
    global _LIVE_CATALOG, _LIVE_CATALOG_SHA256
    with _LIVE_CATALOG_LOCK:
        _LIVE_CATALOG = None
        _LIVE_CATALOG_SHA256 = None


def _canonical_catalog_sha256(ids: frozenset[str]) -> str:
    """Return the canonical sha256 of a catalog id set.

    Sorts the UTF-8-encoded ids and joins on ``\\n`` so the hash is
    invariant under insertion order. This is the canonical recipe used
    for both the live snapshot sha (computed at prime time) and the
    bundled snapshot sha (computed eagerly via the ``@lru_cache`` on
    :func:`_bundled_openrouter_slice_sha256`).
    """
    return hashlib.sha256(b"\n".join(sorted(id.encode("utf-8") for id in ids))).hexdigest()


@lru_cache(maxsize=1)
def _bundled_openrouter_slice() -> frozenset[str]:
    """Return the OpenRouter slice of the bundled litellm catalog.

    Strips the ``openrouter/`` routing prefix so the returned values
    match the slugs OpenRouter's API expects (and the slugs visible at
    https://openrouter.ai/models). This is the fallback the reader
    returns when the live snapshot is unprimed.

    ``@lru_cache`` is sound here: ``read_litellm_model_list`` itself is
    ``@lru_cache``'d and litellm's bundled list is import-time-static,
    so the bundled slice is invariant for the process lifetime. NOT
    applied to :func:`_read_openrouter_catalog` — that one must re-read
    ``_LIVE_CATALOG`` on every call so a prime that happens after the
    first read becomes visible.
    """
    prefix = OPENROUTER_LITELLM_PREFIX
    return frozenset(m[len(prefix) :] for m in read_litellm_model_list() if m.startswith(prefix))


@lru_cache(maxsize=1)
def _bundled_openrouter_slice_sha256() -> str:
    """Canonical sha256 of the bundled OpenRouter slice. Eager-cacheable."""
    return _canonical_catalog_sha256(_bundled_openrouter_slice())


def read_openrouter_catalog_snapshot_id() -> tuple[str, str]:
    """Return ``(sha256_hex, source)`` for the currently-active catalog.

    ``source`` is ``"live"`` when the lifespan has primed the live
    snapshot, ``"bundled"`` otherwise. The sha is canonical (sorted
    utf-8-encoded ids joined on ``\\n``) so two primes against the
    same id set hash identically regardless of insertion order.

    This reader feeds the Landscape ``runs`` table's
    ``openrouter_catalog_sha256`` and ``openrouter_catalog_source``
    columns at run-create time, so an auditor can reconstruct which
    catalog blessed any historical decision. Both fields are always
    populated — never ``None`` — because the bundled fallback is
    always available.
    """
    with _LIVE_CATALOG_LOCK:
        live_sha = _LIVE_CATALOG_SHA256
    if live_sha is not None:
        return live_sha, "live"
    return _bundled_openrouter_slice_sha256(), "bundled"


def _read_openrouter_catalog() -> frozenset[str]:
    """Read the OpenRouter catalog: live snapshot if primed, else bundled.

    ELSPETH's :class:`OpenRouterLLMProvider` calls OpenRouter's
    ``/chat/completions`` directly via httpx, and OpenRouter's API
    expects identifiers in their native un-prefixed form (e.g.
    ``openai/gpt-4o``, the same string visible on
    https://openrouter.ai/models). Both the live snapshot and the
    bundled fallback return values in that un-prefixed form so the
    walker's validate-time set and the HTTP runtime agree.

    Empty frozenset only when both the live snapshot is unprimed AND
    the bundled litellm catalog is empty (litellm not installed); the
    walker translates that into a structured "install the LLM extra"
    error via the registered ``missing_dep_hint``.
    """
    # Direct attribute read — no defensive ``.get()`` on data we own
    # (CLAUDE.md "Defensive Programming: Forbidden"). The lock guards
    # the write side; reads of an immutable ``frozenset`` reference
    # under the GIL are atomic on CPython, but we take the lock anyway
    # because the snapshot read happens once per validate request, not
    # in a hot path.
    with _LIVE_CATALOG_LOCK:
        snapshot = _LIVE_CATALOG
    if snapshot is not None:
        return snapshot
    return _bundled_openrouter_slice()


@dataclass(frozen=True, slots=True)
class _OpenRouterCatalogPayload:
    """What OpenRouter's ``/models`` body actually carried — an owned record.

    Constructed by :func:`_parse_openrouter_models_payload` so the prime
    branches on ELSPETH-owned fields instead of re-probing external data at
    every decision point, and so the two distinct fallback log events can be
    populated from a single parse.

    ``shape_ok`` is ``False`` for a body that is not ``{"data": [...]}``;
    ``ids`` is then empty and the counters are zero. ``entries_received``
    counts what OpenRouter sent, ``entries_skipped`` counts what per-entry
    coercion dropped, and ``ids`` holds the surviving model identifiers.
    """

    shape_ok: bool
    top_level_type: str
    ids: frozenset[str]
    entries_received: int
    entries_skipped: int


@trust_boundary(
    tier=3,
    source="OpenRouter GET https://openrouter.ai/api/v1/models response body, JSON-decoded",
    source_param="payload",
    suppresses=("R1", "R5"),
    invariant=(
        "Returns an owned _OpenRouterCatalogPayload for any input. shape_ok=False when the body is not "
        "a mapping carrying a list under 'data'; per-entry coercion drops entries that are not mappings "
        "carrying a non-empty string 'id' and counts them in entries_skipped. Never raises on external data."
    ),
    non_raising=True,
)
def _parse_openrouter_models_payload(payload: Any) -> _OpenRouterCatalogPayload:
    """Parse OpenRouter's ``/models`` body into an owned catalog record.

    Tier-3 admission boundary (ADR-032): the body is external data, so it is
    parsed — shape-checked at the top level and per entry — and the result is
    an ELSPETH-owned frozen record. Malformed entries are skipped rather than
    aborting the parse; OpenRouter occasionally ships entries with
    non-standard fields and the prime tolerates that. Whether an empty or
    malformed result is fatal is the caller's decision, not this function's:
    it reports, it does not act.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return _OpenRouterCatalogPayload(
            shape_ok=False,
            top_level_type=type(payload).__name__,
            ids=frozenset(),
            entries_received=0,
            entries_skipped=0,
        )

    entries: list[Any] = payload["data"]
    ids: set[str] = set()
    skipped = 0
    for entry in entries:
        # Per-entry Tier-3 row coercion: ``id`` must be a non-empty string.
        if not isinstance(entry, dict):
            skipped += 1
            continue
        candidate = entry.get("id")
        if not isinstance(candidate, str) or not candidate:
            skipped += 1
            continue
        ids.add(candidate)

    return _OpenRouterCatalogPayload(
        shape_ok=True,
        top_level_type=type(payload).__name__,
        ids=frozenset(ids),
        entries_received=len(entries),
        entries_skipped=skipped,
    )


async def prime_openrouter_catalog_from_live(
    *,
    http_get: Callable[[str], Awaitable[httpx.Response]],
) -> bool:
    """Probe OpenRouter's ``/models`` endpoint and populate the live snapshot.

    Called once from the FastAPI lifespan at boot. The ``http_get``
    dependency is injected (not imported as a global) so unit tests can
    supply a fixture without monkeypatching ``httpx``; the lifespan
    passes a bound method of an ``httpx.AsyncClient`` instance.

    If called again in the same process, a failed refresh must not leave a
    previous live snapshot active behind a "falling back to bundled"
    warning. Failed attempts therefore clear any existing live snapshot before
    returning ``False``; successful attempts publish the replacement snapshot
    atomically at the end.

    Tier-3 trust boundary discipline (CLAUDE.md "Data Manifesto"):
    OpenRouter's response is external. The body is admitted through
    :func:`_parse_openrouter_models_payload`, which shape-checks the top
    level (``{"data": [...]}``) and each entry (a dict with a non-empty
    string ``id``) and returns the owned
    :class:`_OpenRouterCatalogPayload` record this function branches on.
    On malformed shape or transport error, we log a structured warning
    and return ``False`` — the boot must continue, with the bundled
    fallback in effect. Per-entry coercion skips malformed entries
    without aborting the prime; OpenRouter occasionally ships entries
    with non-standard fields and we tolerate that.

    Returns ``True`` on successful prime, ``False`` on any failure mode.
    Never raises — the caller is the boot lifespan and a raise here
    would crash the web server on a transient OpenRouter outage, which
    is exactly what we are designing against.
    """
    global _LIVE_CATALOG, _LIVE_CATALOG_SHA256
    try:
        response = await http_get(OPENROUTER_MODELS_URL)
    except httpx.RequestError as exc:
        _slog.warning(
            "openrouter_catalog_prime_transport_error",
            url=OPENROUTER_MODELS_URL,
            exc_class=type(exc).__name__,
            action="falling back to bundled litellm catalog",
        )
        _clear_live_openrouter_catalog()
        return False

    if response.status_code != 200:
        _slog.warning(
            "openrouter_catalog_prime_http_error",
            url=OPENROUTER_MODELS_URL,
            status_code=response.status_code,
            action="falling back to bundled litellm catalog",
        )
        _clear_live_openrouter_catalog()
        return False

    try:
        payload: Any = response.json()
    except ValueError as exc:
        _slog.warning(
            "openrouter_catalog_prime_decode_error",
            url=OPENROUTER_MODELS_URL,
            exc_class=type(exc).__name__,
            action="falling back to bundled litellm catalog",
        )
        _clear_live_openrouter_catalog()
        return False

    parsed = _parse_openrouter_models_payload(payload)

    # A body that is not ``{"data": [...]}`` is a contract violation, not
    # row-level garbage — log and fall back.
    if not parsed.shape_ok:
        _slog.warning(
            "openrouter_catalog_prime_malformed_response",
            url=OPENROUTER_MODELS_URL,
            top_level_type=parsed.top_level_type,
            action="falling back to bundled litellm catalog",
        )
        _clear_live_openrouter_catalog()
        return False

    if not parsed.ids:
        _slog.warning(
            "openrouter_catalog_prime_empty_data",
            url=OPENROUTER_MODELS_URL,
            entries_received=parsed.entries_received,
            entries_skipped=parsed.entries_skipped,
            action="falling back to bundled litellm catalog",
        )
        _clear_live_openrouter_catalog()
        return False

    snapshot = parsed.ids
    snapshot_sha = _canonical_catalog_sha256(snapshot)
    with _LIVE_CATALOG_LOCK:
        _LIVE_CATALOG = snapshot
        _LIVE_CATALOG_SHA256 = snapshot_sha

    _slog.info(
        "openrouter_catalog_primed",
        url=OPENROUTER_MODELS_URL,
        count=len(snapshot),
        entries_skipped=parsed.entries_skipped,
    )
    return True


register_catalog_reader(
    MODEL_CATALOG_OPENROUTER,
    _read_openrouter_catalog,
    # The catalog is sourced from OpenRouter's live ``/models`` endpoint
    # when the web-server lifespan has primed the snapshot, and from
    # ``litellm.model_list`` otherwise. When BOTH the live snapshot is
    # unprimed AND the bundled list is empty (e.g. the operator is
    # running ELSPETH without the LLM extra), the walker surfaces this
    # hint verbatim as the actionable remediation.
    missing_dep_hint=(
        "install elspeth with the LLM extra (``uv pip install 'elspeth[llm]'``) "
        "to provide the litellm model catalog, or restart the web server so it "
        "can prime the live OpenRouter catalog"
    ),
)
