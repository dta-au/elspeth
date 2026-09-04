"""Keep the operator token-outcome SQL aligned with the closed runtime contract."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, text

from elspeth.contracts.audit import _TERMINAL_PAIR_FIELD_CONSTRAINTS
from elspeth.contracts.enums import _LEGAL_TERMINAL_PAIRS, _NON_TERMINAL_PATHS
from elspeth.core.landscape.schema import metadata, token_outcomes_table, tokens_table

_ROOT = Path(__file__).parents[3]
_CONTRACT_PATH = _ROOT / "docs/contracts/token-outcomes/00-token-outcome-contract.md"
_SWEEP_PATH = _ROOT / "docs/contracts/token-outcomes/02-audit-sweep.md"
_PAIR_PATTERN = re.compile(r"\('([^']+)', '([^']+)'\)")


def _section(document: str, heading: str) -> str:
    return document.split(heading, 1)[1].split("\n## ", 1)[0]


def _sql_under_heading(document: str, heading: str) -> str:
    matches = re.findall(r"```sql\n(.*?)\n```", _section(document, heading), flags=re.DOTALL)
    assert len(matches) == 1, f"expected exactly one SQL block under {heading!r}"
    return matches[0]


def _sql_under_section_number(document: str, section_number: int) -> str:
    section = document.split(f"## {section_number}.", 1)[1].split("\n## ", 1)[0]
    matches = re.findall(r"```sql\n(.*?)\n```", section, flags=re.DOTALL)
    assert len(matches) == 1, f"expected exactly one SQL block in section {section_number}"
    return matches[0]


def _markdown_table(document: str, heading: str) -> tuple[list[str], list[list[str]]]:
    lines = [line for line in _section(document, heading).splitlines() if line.startswith("|")]
    assert len(lines) >= 2, f"expected a Markdown table under {heading!r}"
    cells = [[cell.strip() for cell in line.strip("|").split("|")] for line in lines]
    return cells[0], cells[2:]


def _field_set(cell: str) -> frozenset[str]:
    if cell == "none":
        return frozenset()
    return frozenset(part.strip().strip("`") for part in cell.split(","))


def _exact_values(cell: str) -> frozenset[tuple[str, str]]:
    if cell == "none":
        return frozenset()
    return frozenset(tuple(part.strip().strip("`").split("=", 1)) for part in cell.split(","))


def test_contract_table_lists_exactly_every_legal_pair() -> None:
    headers, rows = _markdown_table(_CONTRACT_PATH.read_text(), "## Legal Pairs")

    assert headers[:3] == ["completed", "outcome", "path"]
    documented_pairs = {(completed, outcome.strip("`"), path.strip("`")) for completed, outcome, path, *_ in rows}
    expected_pairs = {("1", outcome.value, path.value) for outcome, path in _LEGAL_TERMINAL_PAIRS} | {
        ("0", "NULL", path.value) for path in _NON_TERMINAL_PATHS
    }

    assert documented_pairs == expected_pairs


def test_contract_table_lists_exactly_every_discriminator_constraint() -> None:
    headers, rows = _markdown_table(_CONTRACT_PATH.read_text(), "## Legal Pairs")

    assert headers == ["completed", "outcome", "path", "required", "exact", "forbidden"]
    documented = {
        (None if outcome.strip("`") == "NULL" else outcome.strip("`"), path.strip("`")): (
            _field_set(required),
            _exact_values(exact),
            _field_set(forbidden),
        )
        for _completed, outcome, path, required, exact, forbidden in rows
    }
    expected = {
        (None if outcome is None else outcome.value, path.value): (
            frozenset(constraints.required),
            frozenset((field, str(value)) for field, value in constraints.exact.items()),
            frozenset(constraints.forbidden),
        )
        for (outcome, path), constraints in _TERMINAL_PAIR_FIELD_CONSTRAINTS.items()
    }

    assert documented == expected


def test_contract_schema_notes_list_exactly_the_current_token_outcome_columns() -> None:
    schema_notes = _section(_CONTRACT_PATH.read_text(), "## Schema Notes")
    column_inventory = schema_notes.split("\n\nFork/expand lineage", 1)[0]
    documented_columns = set(re.findall(r"`([a-z][a-z0-9_]*)`", column_inventory))

    assert documented_columns == set(token_outcomes_table.c.keys())


def test_audit_sweep_pair_queries_match_the_closed_pair_sets() -> None:
    sweep = _SWEEP_PATH.read_text()
    terminal_sql = _sql_under_heading(sweep, "## 4. Illegal Completed Rows")
    non_terminal_sql = _sql_under_heading(sweep, "## 3. Illegal Non-Terminal Rows")

    assert set(_PAIR_PATTERN.findall(terminal_sql)) == {(outcome.value, path.value) for outcome, path in _LEGAL_TERMINAL_PAIRS}
    assert set(re.findall(r"'([^']+)'", non_terminal_sql)) == {path.value for path in _NON_TERMINAL_PATHS}


def test_final_fate_sweep_reports_only_tokens_without_exactly_one_final_fate() -> None:
    sweep = _SWEEP_PATH.read_text()
    final_fate_sql = _sql_under_section_number(sweep, 2)
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    now = datetime.now(UTC)

    with engine.begin() as connection:
        for token_id in ("decided-only", "abandoned-only", "combined", "duplicate-abandoned", "no-fate"):
            connection.execute(
                tokens_table.insert().values(
                    token_id=token_id,
                    row_id=f"row-{token_id}",
                    run_id="run",
                    created_at=now,
                )
            )

        outcome_rows = [
            ("decided-only-1", "decided-only", "success", "filter_dropped", 1),
            ("abandoned-only-1", "abandoned-only", None, "abandoned", 0),
            ("combined-1", "combined", "success", "filter_dropped", 1),
            ("combined-2", "combined", None, "abandoned", 0),
            ("duplicate-abandoned-1", "duplicate-abandoned", None, "abandoned", 0),
            ("duplicate-abandoned-2", "duplicate-abandoned", None, "abandoned", 0),
        ]
        for outcome_id, token_id, outcome, path, completed in outcome_rows:
            connection.execute(
                token_outcomes_table.insert().values(
                    outcome_id=outcome_id,
                    run_id="run",
                    token_id=token_id,
                    outcome=outcome,
                    path=path,
                    completed=completed,
                    recorded_at=now,
                )
            )

        reported = {row.token_id: row.final_fate_count for row in connection.execute(text(final_fate_sql), {"run_id": "run"})}

    assert reported == {
        "combined": 2,
        "duplicate-abandoned": 2,
        "no-fate": 0,
    }


def test_audit_sweep_discriminator_query_matches_live_field_constraints() -> None:
    sweep = _SWEEP_PATH.read_text()
    discriminator_sql = _sql_under_heading(sweep, "## 5. Discriminator Constraint Violations")
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    valid_ids: set[str] = set()
    invalid_ids: set[str] = set()

    with engine.begin() as connection:
        for pair_index, ((outcome, path), constraints) in enumerate(_TERMINAL_PAIR_FIELD_CONSTRAINTS.items()):
            base_fields: dict[str, str | None] = {
                "sink_name": None,
                "batch_id": None,
                "error_hash": None,
            }
            for field in constraints.required:
                base_fields[field] = f"{field}-value"
            base_fields.update({field: str(value) for field, value in constraints.exact.items()})

            valid_id = f"valid-{pair_index}"
            valid_ids.add(valid_id)
            connection.execute(
                token_outcomes_table.insert().values(
                    outcome_id=valid_id,
                    run_id="run",
                    token_id=f"valid-token-{pair_index}",
                    outcome=None if outcome is None else outcome.value,
                    path=path.value,
                    completed=int(outcome is not None),
                    recorded_at=datetime.now(UTC),
                    **base_fields,
                )
            )

            violations: list[dict[str, str | None]] = []
            for field in constraints.required:
                values = dict(base_fields)
                values[field] = None
                violations.append(values)
            for field, expected in constraints.exact.items():
                values = dict(base_fields)
                values[field] = f"not-{expected}"
                violations.append(values)
            for field in constraints.forbidden:
                values = dict(base_fields)
                values[field] = f"unexpected-{field}"
                violations.append(values)

            for violation_index, fields in enumerate(violations):
                invalid_id = f"invalid-{pair_index}-{violation_index}"
                invalid_ids.add(invalid_id)
                connection.execute(
                    token_outcomes_table.insert().values(
                        outcome_id=invalid_id,
                        run_id="run",
                        token_id=f"invalid-token-{pair_index}-{violation_index}",
                        outcome=None if outcome is None else outcome.value,
                        path=path.value,
                        completed=int(outcome is not None),
                        recorded_at=datetime.now(UTC),
                        **fields,
                    )
                )

        reported_ids = {row.outcome_id for row in connection.execute(text(discriminator_sql), {"run_id": "run"})}

    assert reported_ids.isdisjoint(valid_ids)
    assert reported_ids == invalid_ids


def test_every_audit_sweep_query_executes_against_the_current_schema() -> None:
    sweep = _SWEEP_PATH.read_text()
    queries = re.findall(r"```sql\n(.*?)\n```", sweep, flags=re.DOTALL)
    assert len(queries) == 8
    engine = create_engine("sqlite://")
    metadata.create_all(engine)

    with engine.connect() as connection:
        for query in queries:
            connection.execute(text(query), {"run_id": "run"}).all()
