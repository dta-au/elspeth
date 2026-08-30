"""Projection-input corpus for the guided custody hash-stability pin.

Every shape the base tree projected without raising must project
byte-identically after the custody fix (elspeth-201903a286 /
elspeth-4c442aaaa8): settled guided operations store
``guided_response_hash(_state_response(record))`` and replays compare
against it (``guided_replay.py``), so any drift on a non-raising shape
invalidates stored hashes. The corpus is the base test suite's inputs plus
the terminal variants a persisted checkpoint carries.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

EXITED_TERMINAL: dict[str, Any] = {"kind": "exited_to_freeform", "reason": "user_pressed_exit", "pipeline_yaml": None}
COMPLETED_TERMINAL: dict[str, Any] = {"kind": "completed", "reason": None, "pipeline_yaml": "sources: {}\n"}

_PRIVATE = "/home/u/elspeth/data/blobs/sess/abc_data.csv"
_PRIVATE_FIRST = "/internal/blobs/first.csv"
_PRIVATE_SECOND = "/internal/blobs/second.csv"
_SENTINEL_A = "blob:11111111-1111-4111-8111-111111111111"
_SENTINEL_B = "blob:22222222-2222-4222-8222-222222222222"
_INCIDENT_PRIVATE = "/srv/elspeth/data/blobs/s1/50f5b3e9-f52f-4c5f-98df-a20ec7b2627b_colours.csv"
_INCIDENT_BLOB_REF = "50f5b3e9-f52f-4c5f-98df-a20ec7b2627b"


def _guided(reviewed: dict[str, Any], pending: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {"guided_session": {"reviewed_sources": reviewed, "pending_source_intents": pending or {}}, **extra}


def _snapshot(name: str, options: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "plugin": "csv", "options": options}


BASE_CASES: dict[str, tuple[dict[str, Any] | None, dict[str, Any] | None]] = {
    "explicit_binding_live_without_blob_ref": (
        {"source": {"plugin": "csv", "options": {"path": _PRIVATE, "schema": {"mode": "observed"}}}},
        _guided(
            {
                "11111111-1111-4111-8111-111111111111": _snapshot(
                    "source", {"path": _PRIVATE, "blob_ref": "11111111-1111-4111-8111-111111111111", "schema": {"mode": "observed"}}
                )
            }
        ),
    ),
    "operator_typed_source": (
        {"source": {"options": {"path": "/tmp/user.csv"}}},
        _guided(
            {
                "11111111-1111-4111-8111-111111111111": {
                    "name": "source",
                    "options": {"path": "/tmp/user.csv", "schema": {"mode": "observed"}},
                }
            }
        ),
    ),
    "sentinel_exact_name_with_implicit_decisions": (
        {"source": {"options": {"path": "/internal/blobs/session/source.csv", "schema": {"mode": "observed"}}}},
        _guided(
            {"22222222-2222-4222-8222-222222222222": {"name": "source", "options": {"path": _SENTINEL_A, "schema": {"mode": "observed"}}}},
            implicit_decisions={
                "schema_version": 1,
                "entries": [
                    {"path": "source.path", "value": "/internal/blobs/session/source.csv", "category": "source"},
                    {"path": "source.file", "value": "/elsewhere.csv", "category": "source"},
                    {"path": "output.path", "value": "outputs/out.jsonl", "category": "output"},
                ],
                "normalization_events": [],
            },
        ),
    ),
    "plural_sentinels_path_and_file": (
        {
            "first": {"options": {"path": "/internal/blobs/session/first.csv"}},
            "second": {"options": {"file": "/internal/blobs/session/second.csv"}},
        },
        _guided(
            {
                "33333333-3333-4333-8333-333333333333": {"name": "first", "options": {"path": _SENTINEL_A}},
                "44444444-4444-4444-8444-444444444444": {"name": "second", "options": {"file": _SENTINEL_B}},
            }
        ),
    ),
    "fork_sentinel_with_matching_live_blob_ref": (
        {"source": {"options": {"path": "/internal/blobs/child/source.csv", "blob_ref": "11111111-1111-4111-8111-111111111111"}}},
        _guided(
            {
                "22222222-2222-4222-8222-222222222222": {
                    "name": "source",
                    "options": {"path": _SENTINEL_A, "blob_ref": "11111111-1111-4111-8111-111111111111"},
                }
            }
        ),
    ),
    "freeform_meta_none": ({"source": {"options": {"path": "/some/path.csv", "blob_ref": "x"}}}, None),
    "freeform_meta_without_guided": ({"source": {"options": {"path": "/some/path.csv", "blob_ref": "x"}}}, {"repair_turns_used": 0}),
    "explicit_file_carrier": (
        {"source": {"options": {"file": "/internal/blobs/sess/zzz_data.csv"}}},
        _guided(
            {
                "11111111-1111-4111-8111-111111111111": {
                    "name": "source",
                    "options": {"file": "/internal/blobs/sess/zzz_data.csv", "blob_ref": "11111111-1111-4111-8111-111111111111"},
                }
            }
        ),
    ),
    "plural_explicit_by_name": (
        {"first": {"options": {"path": _PRIVATE_FIRST}}, "second": {"options": {"path": _PRIVATE_SECOND}}},
        _guided(
            {
                "11111111-1111-4111-8111-111111111111": {
                    "name": "first",
                    "options": {"path": _PRIVATE_FIRST, "blob_ref": "11111111-1111-4111-8111-111111111111"},
                },
                "22222222-2222-4222-8222-222222222222": {
                    "name": "second",
                    "options": {"path": _PRIVATE_SECOND, "blob_ref": "22222222-2222-4222-8222-222222222222"},
                },
            }
        ),
    ),
    "two_names_share_one_blob_path": (
        {"first": {"options": {"path": "/internal/blobs/shared.csv"}}, "second": {"options": {"path": "/internal/blobs/shared.csv"}}},
        _guided(
            {
                "11111111-1111-4111-8111-111111111111": {
                    "name": "first",
                    "options": {"path": "/internal/blobs/shared.csv", "blob_ref": "abc12300-0000-4000-8000-000000000000"},
                },
                "22222222-2222-4222-8222-222222222222": {
                    "name": "second",
                    "options": {"path": "/internal/blobs/shared.csv", "blob_ref": "abc12300-0000-4000-8000-000000000000"},
                },
            }
        ),
    ),
    "empty_sources_sentinel_reviewed": ({}, _guided({"s": _snapshot("source", {"path": _SENTINEL_A})})),
    "empty_sources_explicit_reviewed": (
        {},
        _guided({"s": _snapshot("source", {"path": _PRIVATE, "blob_ref": "11111111-1111-4111-8111-111111111111"})}),
    ),
    "none_sources_explicit_reviewed": (
        None,
        _guided({"s": _snapshot("source", {"path": _PRIVATE, "blob_ref": "11111111-1111-4111-8111-111111111111"})}),
    ),
    "pending_intents_mixed": (
        {"source": {"options": {"path": "data.csv"}}},
        _guided(
            {},
            {
                "a": {
                    "name": "incoming",
                    "options": {"path": "/internal/blobs/pending.csv", "blob_ref": "11111111-1111-4111-8111-111111111111"},
                },
                "b": {"name": "typed", "options": {"path": "typed.csv"}},
                "c": {"name": "bare", "options": None},
            },
        ),
    ),
    "case_c_reauthored_plain_path": (
        {"source": {"plugin": "csv", "options": {"path": "data.csv"}}},
        _guided({"s": _snapshot("source", {"path": "blob:360e1583-ae3c-4135-9240-0a26a14cf22f"})}),
    ),
    "exact_blob_reuse_case_e": (
        {"source": {"plugin": "csv", "options": {"path": _INCIDENT_PRIVATE, "blob_ref": _INCIDENT_BLOB_REF}}},
        _guided({"s": _snapshot("source", {"path": f"blob:{_INCIDENT_BLOB_REF}"})}),
    ),
    # Raising at base (the defect shapes); recorded so the fix is visible, never
    # asserted byte-identical.
    "defect_fork_explicit_blob_ref_projection_order": (
        {"source": {"plugin": "csv", "options": {"path": _INCIDENT_PRIVATE, "blob_ref": _INCIDENT_BLOB_REF}}},
        _guided({"s": _snapshot("source", {"path": _INCIDENT_PRIVATE, "blob_ref": _INCIDENT_BLOB_REF})}),
    ),
    "defect_incident_v13": (
        {"source": {"plugin": "csv", "options": {"path": _INCIDENT_PRIVATE, "blob_ref": _INCIDENT_BLOB_REF}}},
        _guided({"s": _snapshot("source", {"path": "blob:360e1583-ae3c-4135-9240-0a26a14cf22f"})}),
    ),
    "defect_two_sources_one_repointed": (
        {
            "a": {"plugin": "csv", "options": {"path": "/srv/elspeth/data/blobs/s1/a.csv"}},
            "b": {"plugin": "csv", "options": {"path": _INCIDENT_PRIVATE, "blob_ref": _INCIDENT_BLOB_REF}},
        },
        _guided(
            {
                "sa": _snapshot("a", {"path": "blob:aaaaaaaa-0000-4000-8000-000000000001"}),
                "sb": _snapshot("b", {"path": "blob:bbbbbbbb-0000-4000-8000-000000000002"}),
            }
        ),
    ),
}


def corpus() -> dict[str, tuple[dict[str, Any] | None, dict[str, Any] | None]]:
    """Every base case in active form plus its exited and completed variants."""
    cases: dict[str, tuple[dict[str, Any] | None, dict[str, Any] | None]] = {}
    for name, (sources, meta) in BASE_CASES.items():
        cases[f"{name}[active]"] = (deepcopy(sources), deepcopy(meta))
        if meta is None or "guided_session" not in meta:
            continue
        for label, terminal in (("exited", EXITED_TERMINAL), ("completed", COMPLETED_TERMINAL)):
            variant = deepcopy(meta)
            variant["guided_session"]["terminal"] = deepcopy(terminal)
            cases[f"{name}[{label}]"] = (deepcopy(sources), variant)
    return cases
