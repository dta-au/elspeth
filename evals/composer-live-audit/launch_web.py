"""Launch `elspeth web` with a litellm capture hook (audit harness; not product code).

Every provider call's full request (messages, tools, params) and full response
(content, reasoning, tool calls, usage) is appended to capture/llm_calls.jsonl.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import litellm
from litellm.integrations.custom_logger import CustomLogger

CAPTURE = Path(os.environ["AUDIT_CAPTURE_DIR"])
CAPTURE.mkdir(parents=True, exist_ok=True)
_LOCK = threading.Lock()
_SEQ = [0]


def _plain(obj):
    try:
        from pydantic import BaseModel

        if isinstance(obj, BaseModel):
            return obj.model_dump()
        return json.loads(json.dumps(obj, default=str))
    except Exception as exc:  # capture must never break a call
        return {"_unserialisable": type(obj).__name__, "_error": repr(exc)}


class Capture(CustomLogger):
    def _write(self, kind, kwargs, response_obj, start_time, end_time):
        with _LOCK:
            _SEQ[0] += 1
            seq = _SEQ[0]
        rec = {
            "seq": seq,
            "kind": kind,
            "ts": time.time(),
            "duration_s": (end_time - start_time).total_seconds() if start_time and end_time else None,
            "model": kwargs.get("model"),
            "messages": _plain(kwargs.get("messages")),
            "optional_params": _plain({k: v for k, v in (kwargs.get("optional_params") or {}).items() if k != "tools"}),
            "tool_names": [t.get("function", {}).get("name") for t in (kwargs.get("optional_params") or {}).get("tools") or []],
            "response": _plain(response_obj) if kind == "success" else None,
            "exception": repr(kwargs.get("exception")) if kind == "failure" else None,
        }
        with _LOCK, (CAPTURE / "llm_calls.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._write("success", kwargs, response_obj, start_time, end_time)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self._write("failure", kwargs, response_obj, start_time, end_time)


litellm.callbacks = [Capture()]


def main() -> None:
    # Imported only after the capture callback is installed, so every provider call is recorded.
    from elspeth.cli import app

    sys.argv = ["elspeth", "web", "--host", "127.0.0.1", "--port", os.environ.get("AUDIT_PORT", "8451")]
    app()


if __name__ == "__main__":
    main()
