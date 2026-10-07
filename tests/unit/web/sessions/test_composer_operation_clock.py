from datetime import UTC, datetime, timedelta, timezone

import pytest

from elspeth.web.sessions.routes.composer.operations import composer_operation_deadline_remaining_ms


def test_database_clock_remaining_floors_and_clamps_across_offsets() -> None:
    deadline = datetime(2026, 9, 28, tzinfo=UTC)
    for microseconds, expected in ((85_000_000, 85_000), (1999, 1), (999, 0), (0, 0), (-1_000_000, 0)):
        assert (
            composer_operation_deadline_remaining_ms(deadline_at=deadline, db_now=deadline - timedelta(microseconds=microseconds))
            == expected
        )
    offset_now = (deadline - timedelta(seconds=2)).astimezone(timezone(timedelta(hours=10)))
    assert composer_operation_deadline_remaining_ms(deadline_at=deadline, db_now=offset_now) == 2000


@pytest.mark.parametrize("naive_field", ["deadline", "database"])
def test_naive_database_or_deadline_time_is_refused(naive_field: str) -> None:
    deadline = datetime(2026, 9, 28, tzinfo=UTC)
    database = deadline - timedelta(seconds=1)
    if naive_field == "deadline":
        deadline = deadline.replace(tzinfo=None)
    else:
        database = database.replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone-aware"):
        composer_operation_deadline_remaining_ms(deadline_at=deadline, db_now=database)
