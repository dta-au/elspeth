from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from uuid import UUID

import pytest

_FAKE_CURL = r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
args = sys.argv[1:]
out = Path(args[args.index('-o') + 1])
url = next(arg for arg in args if arg.startswith('https://'))
method = args[args.index('-X') + 1] if '-X' in args else 'GET'
root = Path(os.environ['EVALS_OUT_DIR'])
state_path = root / 'fake-state.json'
state = json.loads(state_path.read_text()) if state_path.exists() else {'submissions': [], 'committed': False}
body = json.loads(Path(args[args.index('--data') + 1][1:]).read_text()) if '--data' in args else None
with (root / 'fake-calls.jsonl').open('a') as calls:
    calls.write(json.dumps({'method': method, 'url': url, 'body': body}) + '\n')
status = '200'
response = None
if url.endswith('/state'):
    response = None
elif url.endswith('/messages'):
    state['submissions'].append(body)
    if len(state['submissions']) == 1:
        state['committed'] = os.environ['ACK_LOST_AFTER_COMMIT'] == '1'
        state_path.write_text(json.dumps(state))
        out.write_text('{}')
        print('000 0.00')
        sys.exit(7)
    state['committed'] = True
    response = {'operation_id': body['operation_id'], 'status': 'queued'}
    status = '202'
elif '/operations/' in url:
    if not state['committed']:
        status = '404'
        response = {'detail': 'Operation not found'}
    else:
        response = {'status': 'completed', 'result': {'message': {'content': 'durable answer'}}}
else:
    raise AssertionError(url)
state_path.write_text(json.dumps(state))
out.write_text(json.dumps(response))
print(status + (' 0.00' if '%{time_total}' in args[args.index('-w') + 1] else ''), end='')
"""


@pytest.mark.parametrize("committed", [False, True])
def test_shell_caller_reconciles_exact_body_and_retains_job_on_reentry(tmp_path: Path, committed: bool) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(_FAKE_CURL)
    curl.chmod(0o700)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("One immutable synthetic request")
    common = Path(__file__).resolve().parents[3] / "evals/lib/common.sh"
    script = """source "$COMMON_SCRIPT"
evals_login_if_needed() { return 0; }
_evals_auth_header_file() { mktemp; }
evals_post_message session-one 1 "$PROMPT_FILE"
evals_post_message session-one 1 "$PROMPT_FILE"
"""
    env = {
        **os.environ,
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "EVALS_OUT_DIR": str(tmp_path),
        "ELSPETH_EVAL_BASE_URL": "https://fake.invalid",
        "ELSPETH_EVAL_CURL_MAX_TIME": "30",
        "COMMON_SCRIPT": str(common),
        "PROMPT_FILE": str(prompt),
        "ACK_LOST_AFTER_COMMIT": "1" if committed else "0",
    }
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in (tmp_path / "fake-calls.jsonl").read_text().splitlines()]
    submissions = [call["body"] for call in calls if call["method"] == "POST"]
    assert len(submissions) == (1 if committed else 2)
    assert all(body == submissions[0] for body in submissions)
    request = json.loads((tmp_path / "msg.t1.req.json").read_text())
    assert request == submissions[0]
    assert str(UUID(request["operation_id"])) == request["operation_id"]
    assert request["state_id"] is None
    assert json.loads((tmp_path / "msg.t1.resp.json").read_text()) == {"message": {"content": "durable answer"}}
    assert (tmp_path / "msg.t1.curl_meta").read_text().split()[0] == "200"
    assert sum(call["url"].endswith("/state") for call in calls) == 1
    prompt.write_text("Changed prompt")
    mismatch = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=10)
    assert mismatch.returncode == 73
    assert (tmp_path / "fake-calls.jsonl").read_text().splitlines() == [json.dumps(call) for call in calls]
