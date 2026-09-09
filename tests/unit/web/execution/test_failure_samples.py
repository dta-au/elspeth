"""Tests for the client-safe failure-category summary of run-level errors.

The module under test feeds three surfaces outside the audit boundary
(sessions DB ``runs.error``, ``RunStatusResponse.error``, and the SSE
``failed`` detail), so the central property here is a negative one: no
per-row free text may leave, whatever the Tier-3 audit payload contains.
The positive property is that the category it does emit is provably drawn
from a closed vocabulary.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime

import pytest

from elspeth.contracts import NodeType
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.schema import SchemaConfig
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import tokens_table, transform_errors_table
from elspeth.web.execution.failure_samples import (
    KNOWN_ERROR_CATEGORIES,
    NON_CANONICAL_CATEGORY,
    UNRECOGNIZED_CATEGORY,
    ClientSafeFailureSummary,
    _client_safe_category,
    format_failure_categories,
    load_top_failure_categories,
)

DYNAMIC_SCHEMA = SchemaConfig.from_dict({"mode": "observed"})

# Canaries stand in for the two Tier-3 sources the audit payload can carry:
# row-derived data, and provider/LLM error text.
CANARY_ROW = "CANARY_ROW_SECRET_9f3a"
CANARY_PROVIDER = "CANARY_PROVIDER_ERROR_7b21"


def _make_run_with_transform(transform_id: str = "fetch") -> tuple[LandscapeDB, str, str]:
    db = LandscapeDB.in_memory()
    factory = RecorderFactory(db)
    run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
    factory.data_flow.register_node(
        run_id=run.run_id,
        plugin_name="test_source",
        node_type=NodeType.SOURCE,
        plugin_version="1.0",
        config={},
        schema_config=DYNAMIC_SCHEMA,
        node_id="source_test",
        sequence=0,
    )
    factory.data_flow.register_node(
        run_id=run.run_id,
        plugin_name="web_scrape",
        node_type=NodeType.TRANSFORM,
        plugin_version="1.0",
        config={},
        schema_config=DYNAMIC_SCHEMA,
        node_id=transform_id,
        sequence=1,
    )
    return db, run.run_id, transform_id


def _record_error(
    db: LandscapeDB,
    run_id: str,
    transform_id: str,
    *,
    error_details: dict[str, object],
    token_id: str,
    row_index: int,
) -> None:
    factory = RecorderFactory(db)
    row = factory.data_flow.create_row(
        run_id=run_id,
        source_node_id="source_test",
        row_index=row_index,
        data={"url": f"row-{row_index}"},
        source_row_index=row_index,
        ingest_sequence=row_index,
    )
    with db.write_connection() as conn:
        conn.execute(
            tokens_table.insert().values(
                token_id=token_id,
                row_id=row.row_id,
                run_id=run_id,
                step_in_pipeline=0,
                created_at=datetime.now(UTC),
            )
        )
        conn.commit()
    factory.data_flow.record_transform_error(
        ref=TokenRef(token_id=token_id, run_id=run_id),
        transform_id=transform_id,
        row_data={"url": f"row-{row_index}"},
        error_details=error_details,  # type: ignore[arg-type]
        destination="discard",
    )


def _canary_details(reason: str = "decode_failed", *, suffix: str = "") -> dict[str, object]:
    """A canonical audit record whose every free-text field carries a canary."""
    return {
        "reason": reason,
        "error": f"gzip: incorrect header check for {CANARY_ROW}{suffix}",
        "message": f"secondary text {CANARY_ROW}{suffix}",
        "error_type": f"BadGzipFile: {CANARY_PROVIDER}",
    }


class TestClientSafeFailureSummaryType:
    """The formatter's input type is the boundary, not a convention."""

    def test_summary_type_has_no_free_text_field(self) -> None:
        """No field can carry the Tier-3 message, so none can render it.

        This is what makes the raw text unroutable to a client surface by
        accident: widening the client formatter would require changing the
        type, not just a call site.
        """
        field_names = {field.name for field in dataclasses.fields(ClientSafeFailureSummary)}
        assert field_names == {"transform_id", "category", "count"}

    def test_sentinels_are_not_members_of_the_closed_vocabulary(self) -> None:
        """Both sentinels must stay distinguishable from a real category.

        ``unknown_category`` IS a real member, so a sentinel colliding with
        the vocabulary would make "we could not validate this" indistinguishable
        from "the plugin reported an unknown category".
        """
        assert UNRECOGNIZED_CATEGORY not in KNOWN_ERROR_CATEGORIES
        assert NON_CANONICAL_CATEGORY not in KNOWN_ERROR_CATEGORIES
        assert "unknown_category" in KNOWN_ERROR_CATEGORIES


class TestClientSafeCategory:
    """Direct coverage of the value-admission boundary."""

    def test_member_reason_is_emitted_verbatim(self) -> None:
        assert _client_safe_category(_canary_details("decode_failed")) == "decode_failed"

    def test_non_member_reason_falls_back_to_the_sentinel(self) -> None:
        """An unknown token is not echoed — not even truncated or hashed."""
        details = {"reason": f"not_a_category_{CANARY_ROW}", "error": CANARY_ROW}
        assert _client_safe_category(details) == UNRECOGNIZED_CATEGORY

    @pytest.mark.parametrize(
        "reason",
        [
            pytest.param(42, id="int"),
            pytest.param(None, id="none"),
            pytest.param(["decode_failed"], id="unhashable-list"),
            pytest.param({"decode_failed": 1}, id="unhashable-dict"),
        ],
    )
    def test_non_string_reason_falls_back_without_raising(self, reason: object) -> None:
        """The ``str`` check is load-bearing: an unhashable value would
        otherwise raise ``TypeError`` from the membership test alone."""
        assert _client_safe_category({"reason": reason}) == UNRECOGNIZED_CATEGORY

    def test_error_type_is_never_used_as_the_category(self) -> None:
        """``error_type`` is free-form and validated nowhere, so it is ignored
        even when ``reason`` is a member and ``error_type`` looks plausible."""
        details = {"reason": "api_error", "error_type": "http_error"}
        assert _client_safe_category(details) == "api_error"

    def test_non_canonical_envelope_reads_neither_of_its_tier3_fields(self) -> None:
        details = {
            "__non_canonical__": True,
            "repr": f"{{'val': '{CANARY_ROW}'}}",
            "serialization_error": f"Out of range float {CANARY_PROVIDER}",
        }
        assert _client_safe_category(details) == NON_CANONICAL_CATEGORY

    def test_missing_required_reason_still_raises(self) -> None:
        """Shape corruption stays loud — the sentinel is for VALUES only."""
        with pytest.raises(KeyError):
            _client_safe_category({"error_type": "http_error", "error": "boom"})


class TestLoadTopFailureCategories:
    def test_no_row_or_provider_text_survives_the_load(self) -> None:
        db, run_id, transform_id = _make_run_with_transform()
        for index in range(3):
            _record_error(
                db,
                run_id,
                transform_id,
                error_details=_canary_details(suffix=f"-{index}"),
                token_id=f"tok_{index}",
                row_index=index,
            )

        summaries = load_top_failure_categories(db, run_id)
        rendered = format_failure_categories(summaries)

        assert summaries == [ClientSafeFailureSummary(transform_id=transform_id, category="decode_failed", count=3)]
        for canary in (CANARY_ROW, CANARY_PROVIDER, "incorrect header check", "BadGzipFile"):
            assert canary not in rendered, rendered
            assert all(canary not in str(summary) for summary in summaries), summaries

    def test_distinct_messages_in_one_category_aggregate_to_one_summary(self) -> None:
        """Regression: aggregation keys on (node, category), not on message.

        The previous form counted ``(transform_id, error_type, message)``, so
        three rows failing the same way with three distinct texts reported
        ``1x`` three times instead of ``3x`` once.
        """
        db, run_id, transform_id = _make_run_with_transform()
        for index in range(3):
            _record_error(
                db,
                run_id,
                transform_id,
                error_details={"reason": "decode_failed", "error": f"distinct text {index}"},
                token_id=f"tok_{index}",
                row_index=index,
            )

        summaries = load_top_failure_categories(db, run_id)

        assert summaries == [ClientSafeFailureSummary(transform_id=transform_id, category="decode_failed", count=3)]

    def test_top_n_is_taken_after_category_aggregation(self) -> None:
        """Regression: the top-N slice must not be taken over messages.

        Four rows share one category but have four distinct message texts;
        two rows share both category and text.  Slicing before aggregating
        ranked the 2x text above each 1x text and reported the wrong dominant
        failure mode.
        """
        db, run_id, transform_id = _make_run_with_transform()
        for index in range(4):
            _record_error(
                db,
                run_id,
                transform_id,
                error_details={"reason": "decode_failed", "error": f"spread-{index}"},
                token_id=f"spread_{index}",
                row_index=index,
            )
        for index in range(2):
            _record_error(
                db,
                run_id,
                transform_id,
                error_details={"reason": "rate_limited", "error": "identical"},
                token_id=f"same_{index}",
                row_index=10 + index,
            )

        summaries = load_top_failure_categories(db, run_id, limit=2)

        assert summaries == [
            ClientSafeFailureSummary(transform_id=transform_id, category="decode_failed", count=4),
            ClientSafeFailureSummary(transform_id=transform_id, category="rate_limited", count=2),
        ]

    def test_distinct_nodes_stay_distinct(self) -> None:
        db, run_id, first = _make_run_with_transform("fetch")
        factory = RecorderFactory(db)
        factory.data_flow.register_node(
            run_id=run_id,
            plugin_name="llm",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            schema_config=DYNAMIC_SCHEMA,
            node_id="summarise",
            sequence=2,
        )
        _record_error(
            db,
            run_id,
            first,
            error_details={"reason": "decode_failed"},
            token_id="t0",
            row_index=0,
        )
        _record_error(
            db,
            run_id,
            "summarise",
            error_details={"reason": "decode_failed"},
            token_id="t1",
            row_index=1,
        )

        summaries = load_top_failure_categories(db, run_id)

        assert {(s.transform_id, s.category) for s in summaries} == {
            ("fetch", "decode_failed"),
            ("summarise", "decode_failed"),
        }

    def test_returns_empty_when_no_errors_recorded(self) -> None:
        db, run_id, _ = _make_run_with_transform()
        assert load_top_failure_categories(db, run_id) == []

    def test_limit_truncates_to_top_n(self) -> None:
        db, run_id, transform_id = _make_run_with_transform()
        for index, reason in enumerate(["decode_failed", "rate_limited", "api_error", "missing_field"]):
            _record_error(
                db,
                run_id,
                transform_id,
                error_details={"reason": reason},
                token_id=f"t{index}",
                row_index=index,
            )

        assert len(load_top_failure_categories(db, run_id, limit=2)) == 2

    def test_rejects_invalid_limit(self) -> None:
        db, run_id, _ = _make_run_with_transform()
        with pytest.raises(ValueError, match="limit must be >= 1"):
            load_top_failure_categories(db, run_id, limit=0)

    def test_rejects_scan_cap_below_limit(self) -> None:
        db, run_id, _ = _make_run_with_transform()
        with pytest.raises(ValueError, match="scan_cap must be >= limit"):
            load_top_failure_categories(db, run_id, limit=5, scan_cap=2)

    def test_unrecognized_category_reaches_the_summary_without_the_value(self) -> None:
        """A stored category the write guard never saw fails closed on read.

        ``record_transform_error`` enforces ``TransformErrorCategory``
        membership at the Tier-1 write boundary, so a non-member cannot be
        recorded through the typed API — the column is rewritten directly
        here to model what the guard does not cover: rows written by an
        earlier schema or by any future second writer.  The read-side check
        exists so the egress property does not depend on that sibling
        invariant holding for every row ever written.
        """
        db, run_id, transform_id = _make_run_with_transform()
        _record_error(
            db,
            run_id,
            transform_id,
            error_details={"reason": "decode_failed"},
            token_id="t0",
            row_index=0,
        )
        with db.write_connection() as conn:
            conn.execute(
                transform_errors_table.update()
                .where(transform_errors_table.c.run_id == run_id)
                .values(error_details_json=json.dumps({"reason": f"invented_{CANARY_ROW}", "error": CANARY_PROVIDER}))
            )
            conn.commit()

        rendered = format_failure_categories(load_top_failure_categories(db, run_id))

        assert UNRECOGNIZED_CATEGORY in rendered
        assert CANARY_ROW not in rendered
        assert CANARY_PROVIDER not in rendered


class TestFormatFailureCategories:
    def test_empty_summaries_yields_empty_string(self) -> None:
        assert format_failure_categories([]) == ""

    def test_renders_count_node_and_category(self) -> None:
        rendered = format_failure_categories([ClientSafeFailureSummary(transform_id="fetch", category="decode_failed", count=3)])
        assert rendered == "  • 3x [fetch] decode_failed"

    def test_node_is_shown_even_for_a_single_node_run(self) -> None:
        """The failing node is half of what makes the category actionable."""
        rendered = format_failure_categories([ClientSafeFailureSummary(transform_id="fetch", category="rate_limited", count=1)])
        assert "[fetch]" in rendered

    def test_renders_one_bullet_per_summary(self) -> None:
        rendered = format_failure_categories(
            [
                ClientSafeFailureSummary(transform_id="fetch", category="decode_failed", count=2),
                ClientSafeFailureSummary(transform_id="summarise", category="rate_limited", count=1),
            ]
        )
        assert rendered.splitlines() == [
            "  • 2x [fetch] decode_failed",
            "  • 1x [summarise] rate_limited",
        ]

    def test_long_node_id_is_bounded(self) -> None:
        rendered = format_failure_categories([ClientSafeFailureSummary(transform_id="n" * 200, category="decode_failed", count=1)])
        assert len(rendered) < 120
