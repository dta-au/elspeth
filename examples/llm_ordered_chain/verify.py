"""Verify real audited query dependencies from the local example run."""

from __future__ import annotations

import csv
import json
import sqlite3
import sys
from pathlib import Path


def main() -> None:
    example = Path(__file__).resolve().parent
    with (example / "input.csv").open(newline="", encoding="utf-8") as stream:
        original_rows = list(csv.DictReader(stream))
        original = {row["expense_id"]: row for row in original_rows}
    assert len(original_rows) == 4
    assert len(original) == 4
    assert all(set(row) == {"expense_id", "receipt_text"} for row in original.values())
    outputs = [json.loads(line) for line in (example / "output/assessed_expenses.jsonl").read_text().splitlines()]
    quarantine = [json.loads(line) for line in (example / "output/quarantine.jsonl").read_text().splitlines()]
    expected = {
        "E101": (48.0, "transport", "approve"),
        "E102": (82.0, "meals", "manual_review"),
        "E103": (126.5, "equipment", "manual_review"),
    }
    assert len(outputs) == 3
    assert {row["expense_id"] for row in outputs} == set(expected)
    for row in outputs:
        assert (row["extract_amount_usd"], row["extract_expense_type"], row["apply_decision"]) == expected[row["expense_id"]]
        # Canonical persisted JSON may spell an integral float as an integer.
        assert type(row["extract_amount_usd"]) in (int, float)
    assert quarantine == [original["E104"]], quarantine

    def payload(ref: str) -> dict[str, object]:
        path = example / "runs/payloads" / ref[:2] / ref
        return json.loads(path.read_text())

    with sqlite3.connect(f"file:{example / 'runs/audit.db'}?mode=ro", uri=True) as conn:
        assert conn.execute("SELECT status FROM runs").fetchall() == [("completed_with_failures",)]
        assert conn.execute("SELECT count(*) FROM rows").fetchone()[0] == 4
        assert conn.execute("SELECT count(*) FROM nodes WHERE plugin_name='llm'").fetchone()[0] == 1
        calls = conn.execute(
            "SELECT r.source_data_ref, c.request_ref, c.response_ref, c.call_index "
            "FROM calls c JOIN node_states n ON n.state_id=c.state_id "
            "JOIN tokens t ON t.token_id=n.token_id JOIN rows r ON r.row_id=t.row_id "
            "WHERE c.call_type='llm' ORDER BY r.row_index,c.call_index"
        ).fetchall()
        by_expense: dict[str, list[tuple[dict[str, object], dict[str, object]]]] = {}
        for source_ref, request_ref, response_ref, _index in calls:
            expense_id = payload(source_ref)["expense_id"]
            by_expense.setdefault(expense_id, []).append((payload(request_ref), payload(response_ref)))
        assert {key: len(value) for key, value in by_expense.items()} == {"E101": 2, "E102": 2, "E103": 2, "E104": 1}
        for expense_id in expected:
            first, second = by_expense[expense_id]
            extracted = json.loads(first[1]["content"])
            prompt = second[0]["messages"][-1]["content"]
            assert "APPLY_REIMBURSEMENT_POLICY" in prompt
            assert f"Extracted amount_usd: {float(extracted['amount_usd'])}" in prompt
            assert f"Extracted expense_type: {extracted['expense_type']}" in prompt
            assert original[expense_id]["receipt_text"] not in prompt
        preflight = conn.execute(
            "SELECT count(*) FROM calls c JOIN operations o ON o.operation_id=c.operation_id "
            "WHERE c.call_type='llm' AND o.operation_type='runtime_preflight'"
        ).fetchone()[0]
        assert preflight == 1, preflight
        assert len(calls) == 7, len(calls)
        assert conn.execute("SELECT count(*) FROM transform_errors").fetchone()[0] == 1
    sys.stdout.write(
        "Verified: 4 source rows; 3 complete results; 1 original-row quarantine.\n"
        "Verified: one llm node; 7 row queries plus 1 runtime-preflight query.\n"
        "Verified: each apply prompt consumes its own extract response; invalid extract stops the consumer.\n"
    )


if __name__ == "__main__":
    main()
