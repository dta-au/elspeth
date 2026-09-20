"""Every digest column in both audit stores must be guarded by a shape CHECK.

The defect this pins (elspeth-f99b16fc2f) was a CHECK that enforced
``length(content_hash) = 64`` and nothing else, one table away from a sibling
that enforced the whole ``^[a-f0-9]{64}$`` rule. A census then found the same
gap on four more session columns and no shape rule at all on 47 Landscape
columns -- SQLite ignores a declared ``VARCHAR`` width, so ``String(64)``
admitted any text. There are several ways to declare the rule (a helper, paired
dialect strings, a compiled element, a clause inside a larger state CHECK) and
nothing made any of them mandatory, so the weakest form was also the shortest.

This gate is behavioural, not textual: it evaluates each table's live SQLite
CHECK expressions against probe values and requires the accept/reject profile
of the column's declared shape, so it does not care which style produced the
constraint. Two obligations:

* a NEW column whose name looks like a digest fails until it is listed here
  with a shape, or excluded with a reason;
* a listed column whose CHECKs stop rejecting malformed values fails.

The PostgreSQL arms are proven against a real server in
``tests/testcontainer/web/test_schema_probe_postgres.py``; here each rejecting
constraint must at least exist under the same name for that dialect.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator

import pytest
from sqlalchemy import CheckConstraint, MetaData, Table
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from elspeth.core.landscape import schema as landscape_schema
from elspeth.web.sessions import models as session_models

_STORES: dict[str, MetaData] = {
    "sessions": session_models.metadata,
    "landscape": landscape_schema.metadata,
}

_DIGEST_NAME = re.compile(r"(^|_)(hash|sha256|digest|fingerprint|checksum)(_|$)|_hex$", re.IGNORECASE)

# shape -> (values the column must admit, values it must reject)
_PROFILES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "hex64": (("a" * 64, "0123456789abcdef" * 4), ("A" * 64, "g" * 64, "a" * 63, "a" * 65, "a" * 63 + " ")),
    "hex32": (("a" * 32,), ("A" * 32, "g" * 32, "a" * 31, "a" * 33, "a" * 64)),
    "hex16": (("a" * 16,), ("A" * 16, "g" * 16, "a" * 15, "a" * 17, "a" * 64)),
    "ref16": (("sha256:" + "a" * 16,), ("sha256:" + "A" * 16, "a" * 23, "sha256:" + "a" * 15, "sha256:" + "a" * 64)),
    "prefixed64": (("sha256:" + "a" * 64,), ("sha256:" + "A" * 64, "a" * 71, "sha256:" + "a" * 63, "a" * 64)),
}

# Columns whose shape rule only binds in one row state: the companion values
# that put the row in that state.
_CONTEXT: dict[tuple[str, str, str], dict[str, object]] = {
    ("sessions", "blobs", "content_hash"): {"status": "ready"},
}

# Digest-named columns that deliberately carry no lowercase-hex rule.
_EXCLUDED: dict[tuple[str, str, str], str] = {
    ("sessions", "interpretation_events", "hash_domain_version"): "a version label such as 'v2', not a digest",
    ("sessions", "web_instances", "image_digest"): "an OCI image reference supplied by the platform, not an ELSPETH digest",
    ("sessions", "user_preferences", "tutorial_source_data_hash"): (
        "echoed back by the client with no server-side format validation; a CHECK would turn a malformed request into a 500"
    ),
}

_INVENTORY: dict[tuple[str, str], dict[str, str]] = {
    ("sessions", "blob_deletion_cleanups"): {"blob_snapshot_hash": "hex64", "expected_file_hash": "hex64"},
    ("sessions", "blob_inline_resolutions"): {"content_hash": "hex64"},
    ("sessions", "blob_replacement_cleanups"): {
        "old_blob_snapshot_hash": "hex64",
        "replacement_blob_snapshot_hash": "hex64",
        "old_content_hash": "hex64",
        "replacement_content_hash": "hex64",
    },
    ("sessions", "blobs"): {"content_hash": "hex64", "creating_composer_skill_hash": "hex64", "creating_arguments_hash": "hex64"},
    ("sessions", "composer_completion_events"): {"payload_digest": "prefixed64"},
    ("sessions", "composition_proposals"): {"composer_skill_hash": "hex64", "tool_arguments_hash": "hex64"},
    ("sessions", "guided_operation_events"): {"request_hash": "hex64"},
    ("sessions", "guided_operations"): {"request_hash": "hex64", "response_hash": "hex64"},
    ("sessions", "interpretation_events"): {
        "composer_skill_hash": "hex64",
        "arguments_hash": "hex64",
        "approved_prompt_artifact_hash": "hex64",
    },
    ("sessions", "library_entries"): {"payload_digest": "hex64"},
    ("sessions", "proposal_blob_effect_receipts"): {"arguments_hash": "hex64", "result_blob_snapshot_hash": "hex64"},
    ("sessions", "rate_limit_buckets"): {"subject_digest": "hex64"},
    ("sessions", "rate_limit_events"): {"subject_digest": "hex64"},
    ("sessions", "review_attestations"): {"payload_digest": "prefixed64"},
    ("sessions", "run_execution_inputs"): {
        "canonical_input_digest": "hex64",
        "topology_digest": "hex64",
        "source_manifest_digest": "hex64",
        "application_fingerprint": "hex64",
        "plugin_registry_fingerprint": "hex64",
        "configuration_fingerprint": "hex64",
        "graph_fingerprint": "hex64",
        "runtime_fingerprint": "hex64",
        "implementation_fingerprint": "hex64",
    },
    ("sessions", "run_start_permits"): {
        "envelope_hash": "hex64",
        "topology_hash": "hex64",
        "source_manifest_hash": "hex64",
        "checkpoint_subject_hash": "hex64",
        "permit_subject_hash": "hex64",
        "admission_decision_hash": "hex64",
    },
    ("sessions", "skill_markdown_history"): {"hash": "hex64"},
    ("sessions", "sso_handoffs"): {"code_hash": "hex64"},
    ("sessions", "websocket_tickets"): {"ticket_digest": "hex64"},
    ("landscape", "aggregation_result_members"): {"error_hash": "hex16"},
    ("landscape", "aggregation_results"): {"output_hash": "hex64"},
    ("landscape", "artifacts"): {"content_hash": "hex64"},
    ("landscape", "audit_export_snapshot_chunks"): {"content_hash": "hex64", "predecessor_seal_hash": "hex64", "chunk_seal_hash": "hex64"},
    ("landscape", "audit_export_snapshots"): {
        "registry_key_hash": "hex64",
        "public_export_config_hash": "hex64",
        "manifest_hash": "hex64",
        "last_chunk_seal_hash": "hex64",
        "snapshot_hash": "hex64",
        "snapshot_seal_hash": "hex64",
        "signature_hex": "hex64",
        "final_hash": "hex64",
        "signed_manifest_hash": "hex64",
    },
    ("landscape", "calls"): {"request_hash": "hex64", "response_hash": "hex64", "approved_prompt_artifact_hash": "hex64"},
    ("landscape", "checkpoints"): {"upstream_topology_hash": "hex64"},
    ("landscape", "coalesce_effects"): {"parent_set_hash": "hex64", "effect_hash": "hex64"},
    ("landscape", "node_states"): {"input_hash": "hex64", "output_hash": "hex64"},
    ("landscape", "nodes"): {"source_file_hash": "ref16", "config_hash": "hex64", "schema_hash": "hex64", "output_contract_hash": "hex32"},
    ("landscape", "operations"): {"input_data_hash": "hex64", "output_data_hash": "hex64"},
    ("landscape", "routing_events"): {"reason_hash": "hex64"},
    ("landscape", "rows"): {"source_data_hash": "hex64"},
    ("landscape", "run_sources"): {"config_hash": "hex64", "schema_contract_hash": "hex32"},
    ("landscape", "run_start_admissions"): {"subject_hash": "hex64"},
    ("landscape", "run_web_plugin_policy"): {
        "policy_hash": "hex64",
        "snapshot_hash": "hex64",
        "binding_generation_fingerprint": "hex64",
        "admission_decision_hash": "hex64",
    },
    ("landscape", "runs"): {"config_hash": "hex64", "openrouter_catalog_sha256": "hex64"},
    ("landscape", "secret_resolutions"): {"fingerprint": "hex64"},
    ("landscape", "sink_effect_attempts"): {"request_hash": "hex64", "evidence_hash": "hex64"},
    ("landscape", "sink_effect_members"): {
        "lineage_hash": "hex64",
        "payload_hash": "hex64",
        "reason_hash": "hex64",
        "descriptor_hash": "hex64",
        "evidence_hash": "hex64",
    },
    ("landscape", "sink_effect_streams"): {"requested_target_hash": "hex64", "head_descriptor_hash": "hex64"},
    ("landscape", "sink_effects"): {
        "config_hash": "hex64",
        "membership_or_manifest_hash": "hex64",
        "group_payload_hash": "hex64",
        "plan_hash": "hex64",
        "expected_descriptor_hash": "hex64",
        "precondition_hash": "hex64",
        "reconcile_evidence_hash": "hex64",
        "result_descriptor_hash": "hex64",
    },
    ("landscape", "token_outcomes"): {"error_hash": "hex16"},
    ("landscape", "token_work_items"): {"pending_error_hash": "hex16"},
    ("landscape", "transform_errors"): {"row_hash": "hex64"},
    ("landscape", "validation_errors"): {"row_hash": "hex64"},
}


def _sqlite_checks(table: Table) -> list[tuple[str, str]]:
    dialect = sqlite.dialect()
    compiler = CreateTable(table).compile(dialect=dialect)
    return [
        (str(constraint.name), str(constraint.sqltext.compile(dialect=dialect, compile_kwargs={"literal_binds": True})))
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint._should_create_for_compiler(compiler)
    ]


def _postgres_check_names(table: Table) -> set[str]:
    compiler = CreateTable(table).compile(dialect=postgresql.dialect())
    return {
        str(constraint.name)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint._should_create_for_compiler(compiler)
    }


def _shape_checks(table: Table, column: str) -> list[tuple[str, str]]:
    """CHECKs that constrain the column's VALUE, not merely whether it is NULL."""
    mention = rf"\b{re.escape(column)}\b"
    nullness = rf"\b{re.escape(column)}\s+IS\s+(NOT\s+)?NULL\b"
    return [(name, text) for name, text in _sqlite_checks(table) if re.search(mention, re.sub(nullness, "", text))]


def _evaluate(conn: sqlite3.Connection, table: Table, text: str, row: dict[str, object]) -> object:
    names = [c.name for c in table.columns]
    projection = ", ".join(f'? AS "{name}"' for name in names)
    return conn.execute(f"SELECT ({text}) FROM (SELECT {projection})", [row.get(name) for name in names]).fetchone()[0]


def _referenced_columns(table: Table, text: str) -> set[str]:
    return {c.name for c in table.columns if re.search(rf"\b{re.escape(c.name)}\b", text)}


def _entries() -> Iterator[tuple[str, str, str, str]]:
    for (store, table_name), columns in _INVENTORY.items():
        for column, shape in columns.items():
            yield store, table_name, column, shape


@pytest.fixture(scope="module")
def conn() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    try:
        yield connection
    finally:
        connection.close()


def test_every_digest_named_column_is_inventoried_or_excluded() -> None:
    live = {
        (store, table.name, column.name)
        for store, metadata in _STORES.items()
        for table in metadata.tables.values()
        for column in table.columns
        if _DIGEST_NAME.search(column.name)
    }
    declared = {(store, table, column) for store, table, column, _shape in _entries()} | set(_EXCLUDED)
    assert live - declared == set(), "new digest-named columns need a shape in _INVENTORY or a reason in _EXCLUDED"
    assert declared - live == set(), "inventory names columns that no longer exist"
    assert not {(s, t, c) for s, t, c, _ in _entries()} & set(_EXCLUDED)


@pytest.mark.parametrize(("store", "table_name", "column", "shape"), list(_entries()), ids=lambda v: str(v))
def test_digest_column_checks_enforce_the_declared_shape(
    conn: sqlite3.Connection, store: str, table_name: str, column: str, shape: str
) -> None:
    table = _STORES[store].tables[table_name]
    context = _CONTEXT.get((store, table_name, column), {})
    checks = _shape_checks(table, column)
    assert checks, f"{table_name}.{column} has no CHECK constraining its value"
    good, bad = _PROFILES[shape]

    known = {column, *context}
    decidable = [(name, text) for name, text in checks if _referenced_columns(table, text) <= known]
    for value in good:
        for name, text in decidable:
            assert _evaluate(conn, table, text, {**context, column: value}) != 0, f"{name} rejects well-formed {value!r}"

    rejecting: set[str] = set()
    for value in bad:
        hits = {name for name, text in checks if _evaluate(conn, table, text, {**context, column: value}) == 0}
        assert hits, f"{table_name}.{column} admits malformed {value!r}"
        rejecting |= hits

    missing_on_postgres = rejecting - _postgres_check_names(table)
    assert not missing_on_postgres, f"no PostgreSQL CHECK named {sorted(missing_on_postgres)}"

    # A state-bound rule decides NULL by row state (a ready blob MUST carry a
    # hash), so the NULL probe only applies where no state context does.
    if table.columns[column].nullable and not context:
        for name, text in decidable:
            assert _evaluate(conn, table, text, {**context, column: None}) != 0, f"{name} rejects NULL on a nullable column"
