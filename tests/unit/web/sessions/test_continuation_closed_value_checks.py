"""Hand-written Continuation closed-value CHECKs stay equal to their Literal owners.

``models.py`` writes the journal ``tag`` and the segment ``rollover_reason``
CHECKs as literal SQL so a contract edit cannot change what the database
admits without an epoch bump. These tests derive the expected value set from
``get_args`` and compare it with the values the live CHECK names, and they
prove each CHECK admits every value and refuses a value outside the set.
Control: add a value to either Literal, or drop one from either CHECK -> red.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest
from sqlalchemy import CheckConstraint, Column, MetaData, String, Table, insert
from sqlalchemy.exc import IntegrityError

from elspeth.web.composer.continuation_handoff import ContinuationYieldReason
from elspeth.web.sessions import models
from elspeth.web.sessions.continuation_events import ContinuationEventTag
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import composer_continuation_events_table, composer_operation_segments_table


def _quoted_values(sql: str) -> list[str]:
    return re.findall(r"'([^']*)'", sql)


def _live_check(table: Table, name: str) -> str:
    (constraint,) = [item for item in table.constraints if isinstance(item, CheckConstraint) and item.name == name]
    return str(constraint.sqltext)


@pytest.mark.parametrize(
    ("sql", "table", "name", "owner"),
    [
        (models._CONTINUATION_EVENT_TAG_CHECK, composer_continuation_events_table, "ck_composer_events_tag", ContinuationEventTag),
        (
            models._CONTINUATION_ROLLOVER_REASON_CHECK,
            composer_operation_segments_table,
            "ck_composer_segments_rollover_reason",
            ContinuationYieldReason,
        ),
    ],
    ids=["event-tag", "rollover-reason"],
)
def test_check_names_exactly_the_literal_values(sql, table, name, owner) -> None:
    values = _quoted_values(sql)
    assert len(values) == len(set(values))
    assert set(values) == set(get_args(owner))
    assert _live_check(table, name) == sql


@pytest.mark.parametrize(
    ("sql", "column", "owner", "nullable"),
    [
        (models._CONTINUATION_EVENT_TAG_CHECK, "tag", ContinuationEventTag, False),
        (models._CONTINUATION_ROLLOVER_REASON_CHECK, "rollover_reason", ContinuationYieldReason, True),
    ],
    ids=["event-tag", "rollover-reason"],
)
def test_check_admits_each_value_and_refuses_an_outside_value(tmp_path: Path, sql, column, owner, nullable) -> None:
    metadata = MetaData()
    probe = Table("probe", metadata, Column(column, String, nullable=True), CheckConstraint(sql, name="ck_probe"))
    engine = create_session_engine(f"sqlite:///{tmp_path / 'probe.db'}")
    metadata.create_all(engine)
    try:
        with engine.begin() as conn:
            for value in get_args(owner):
                conn.execute(insert(probe).values({column: value}))
            if nullable:
                conn.execute(insert(probe).values({column: None}))
        with engine.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(insert(probe).values({column: "not_a_member"}))
    finally:
        engine.dispose()
