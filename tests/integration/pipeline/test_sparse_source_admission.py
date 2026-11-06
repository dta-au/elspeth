"""Public CLI and local audit controls for rejected sparse aliases."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import select
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.contracts.contract_records import ContractAuditRecord
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import run_sources_table


def _run(root: Path, *, mode: str, policy: str, rejected_new_field: bool) -> dict[str, Any]:
    root.mkdir()
    rejected = {"known": 0, "id": "bad"}
    if rejected_new_field:
        rejected["Full Name"] = 123
    rows = [{"known": 0, "id": 1}, rejected, {"known": 0, "id": 2, "full_name": "Ada"}]
    (root / "input.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    schema = {"mode": mode}
    if mode == "flexible":
        schema["fields"] = ["known: int"]
    settings = {
        "sources": {
            "primary": {
                "plugin": "json",
                "on_success": "output",
                "options": {"path": str(root / "input.jsonl"), "format": "jsonl", "schema": schema, "on_validation_failure": policy},
            }
        },
        "sinks": {
            name: {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {"path": str(root / f"{name}.jsonl"), "format": "jsonl", "schema": {"mode": "observed"}},
            }
            for name in (("output", "quarantine") if policy == "quarantine" else ("output",))
        },
        "landscape": {"url": f"sqlite:///{root / 'landscape.db'}"},
        "payload_store": {"base_path": str(root / "payloads")},
        "concurrency": {"max_workers": 1},
    }
    path = root / "settings.yaml"
    path.write_text(yaml.safe_dump(settings))
    result = CliRunner().invoke(app, ["--no-dotenv", "run", "--settings", str(path), "--execute", "--format", "json"])
    assert result.exit_code in (0, 1), (result.output, result.exception)
    summary = json.loads(result.output.strip().splitlines()[-1])
    run_id = summary.pop("run_id")
    with LandscapeDB.from_url(f"sqlite:///{root / 'landscape.db'}", create_tables=False) as db:
        with db.read_only_connection() as conn:
            source = conn.execute(select(run_sources_table).where(run_sources_table.c.run_id == run_id)).mappings().one()
        errors = RecorderFactory.read_only(db).data_flow.get_validation_errors_for_run(run_id)
    contract = ContractAuditRecord.from_json(source["schema_contract_json"])
    resolution = json.loads(source["field_resolution_json"])["resolution_mapping"]
    return {
        "summary": summary,
        "output": [json.loads(line) for line in (root / "output.jsonl").read_text().splitlines()],
        "contract": contract,
        "resolution": resolution,
        "errors": len(errors),
        "lifecycle": source["lifecycle_state"],
    }


@pytest.mark.parametrize("mode", ("observed", "flexible"))
@pytest.mark.parametrize("policy", ("quarantine", "discard"))
def test_rejected_sparse_alias_has_no_public_pipeline_authority(tmp_path: Path, mode: str, policy: str) -> None:
    control = _run(tmp_path / "control", mode=mode, policy=policy, rejected_new_field=False)
    poisoned = _run(tmp_path / "poisoned", mode=mode, policy=policy, rejected_new_field=True)
    expected = [{"known": 0, "id": 1}, {"known": 0, "id": 2, "full_name": "Ada"}]
    assert control["output"] == expected
    assert control["errors"] == 1
    assert poisoned["output"] == expected
    assert poisoned["errors"] == 1
    assert poisoned["lifecycle"] == control["lifecycle"] == "exhausted"
    assert poisoned["summary"] == control["summary"]
    assert poisoned["resolution"] == control["resolution"] == {"known": "known", "id": "id", "full_name": "full_name"}
    for outcome in (control, poisoned):
        assert {field.normalized_name for field in outcome["contract"].fields} == {"known", "id", "full_name"}
        field = next(field for field in outcome["contract"].fields if field.normalized_name == "full_name")
        assert (field.original_name, field.python_type) == ("full_name", "str")
