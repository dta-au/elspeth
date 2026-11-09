"""Run the final bounded selection and retain complete observed feedback records."""
from copy import deepcopy
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import select

from elspeth.web.sessions.models import chat_messages_table, composition_proposals_table

records = []
lane = Path('/workspace/elspeth/.claude/lanes/composer-remediation-wave1/d1-gate-repair-logs')
prefix = os.environ.get("D1_GATE_VERIFY_PREFIX", "fix1-final")

def snapshot():
    entries = subprocess.check_output(["git", "ls-files", "-s", "-z"]).decode().strip('\0').split('\0')
    result = {}
    for entry in entries:
        header, name = entry.split('\t', 1)
        mode, object_id, stage = header.split()
        assert stage == '0'
        path = Path(name)
        if mode == '160000':
            result[name] = {"gitlink": object_id}
        elif path.is_symlink():
            result[name] = {"symlink": os.readlink(path)}
        else:
            result[name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "mode": path.stat().st_mode}
    return result

class Evidence:
    def pytest_collection_modifyitems(self, items):
        modules = {item.module for item in items}
        for module in modules:
            if module.__name__ != "tests.unit.web.composer.test_compose_loop_persistence":
                continue
            original = module._capture_tool_batch_rejection

            async def observed(service, **kwargs):
                result = await original(service, **kwargs)
                if kwargs.get("model_feedback") is not None:
                    session_id = kwargs["session_id"]
                    with service._sessions_service._engine.connect() as connection:
                        tools = connection.execute(select(chat_messages_table.c.tool_call_id, chat_messages_table.c.content)
                            .where(chat_messages_table.c.session_id == session_id, chat_messages_table.c.role == "tool")
                            .order_by(chat_messages_table.c.sequence_no)).all()
                        proposals = connection.execute(select(composition_proposals_table.c.id, composition_proposals_table.c.tool_call_id,
                            composition_proposals_table.c.tool_name, composition_proposals_table.c.status)
                            .where(composition_proposals_table.c.session_id == session_id)).all()
                    records.append({
                        "session_id": session_id,
                        "outcome": type(result).__name__,
                        "provider_feedback": deepcopy(kwargs["model_feedback"]),
                        "stored_feedback": [{"tool_call_id": row.tool_call_id, "payload": json.loads(row.content)} for row in tools],
                        "proposals": [dict(row._mapping) for row in proposals],
                    })
                return result
            module._capture_tool_batch_rejection = observed

before = snapshot()
(lane/(prefix+'-before.json')).write_text(json.dumps(before, sort_keys=True, indent=2)+'\n')
(lane/(prefix+'-command.json')).write_text(json.dumps({"cwd": str(Path.cwd()), "evidence_prefix": prefix, "argv": [sys.executable, __file__, *sys.argv[1:]],
    "PYTHONPATH": os.environ["PYTHONPATH"], "head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()}, indent=2)+'\n')
code = pytest.main(sys.argv[1:], plugins=[Evidence()])
after = snapshot()
(lane/(prefix+'-after.json')).write_text(json.dumps(after, sort_keys=True, indent=2)+'\n')
freeze = {"frozen": before == after, "tracked_inputs": len(before)}
(lane/(prefix+'-freeze.json')).write_text(json.dumps(freeze, indent=2)+'\n')
destination = lane/('fix1-feedback-records.json' if prefix == 'fix1-final' else prefix+'-feedback-records.json')
destination.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n")
print(f"Observed complete provider/stored feedback for {len(records)} measured calls")
print(json.dumps(freeze))
assert before == after
raise SystemExit(int(code))
