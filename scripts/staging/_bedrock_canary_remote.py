"""The program ``direct_bedrock_canary`` runs inside the web container.

Its source is sent with ``python -c`` and its configuration, including the
key, arrives as one JSON document on stdin; the key is never in argv, the
environment or a file. It imports ELSPETH and LiteLLM only inside
:func:`main`, so the pure helpers here can be unit-tested outside a container.

Every request is pinned to ``bedrock-runtime.<region>.amazonaws.com`` and a
``/converse-stream`` path; anything else fails the probe. Error text has the
key removed, and an error that still carries signed-request or key material
is withheld entirely.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
SENSITIVE_MARKERS = ("bedrock-api-key-", "X-Amz-", "Authorization", "Bearer ")
ERROR_LIMIT = 1500
WITHHELD = "Provider error; sensitive detail withheld"

LONG_PROMPT = (
    "Design and rigorously test a distributed durable DAG executor from first principles. It has four workers, expiring "
    "fenced leases, at-least-once delivery, crashes between any two instructions, asynchronous cancellation, dynamic "
    "fan-out, joins, retries, and an append-only audit database separate from the result database. Define a complete "
    "executable state machine and exact invariants. Give a full Python reference implementation and a bounded exhaustive "
    "model checker. Explain every transaction boundary and the recovery algorithm for each partial commit. Then enumerate "
    "128 DISTINCT adversarial traces of at least 24 transitions each, with a table showing state, lease epoch, token "
    "identity, durable writes, and expected next action at EVERY transition. Include duplicate deliveries, stale lease "
    "holders, restarts, lost replies, reordered cancellation, and concurrent join completion. For each trace, state which "
    "invariant it exercises and show why it holds or supply and repair the counterexample. Do not replace the traces with "
    "a generator or abbreviate them by symmetry. Include complete runnable tests and all 128 trace tables. Use the "
    "available response budget for a comprehensive technical answer, not a short outline. This is a synthetic analysis "
    "exercise; do not call tools or access external systems."
)
SHORT_PROMPT = "Reply with OK only."


def bedrock_host(region: str) -> str:
    if not REGION.fullmatch(region):
        raise ValueError("region is not an AWS region name")
    return f"bedrock-runtime.{region}.amazonaws.com"


def safe_error_summary(message: str, key: str) -> str:
    """Remove the key from provider error text, and withhold text that still looks sensitive."""
    redacted = message.replace(key, "<redacted>") if key else message
    if any(marker in redacted for marker in SENSITIVE_MARKERS):
        return WITHHELD
    return redacted[:ERROR_LIMIT]


def check_request(host: str, path: str, *, expected_host: str) -> None:
    if host != expected_host or not path.endswith("/converse-stream"):
        raise RuntimeError("probe request left the pinned Bedrock streaming endpoint")


def consume_stream(
    chunks: Iterable[Mapping[str, Any]],
    *,
    started: float,
    observe_seconds: float,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Count a streamed completion: chunks, characters, timing and whether text arrived after five minutes."""
    result: dict[str, Any] = {
        "chunks": 0,
        "text_characters": 0,
        "reasoning_characters": 0,
        "first_chunk_seconds": None,
        "last_chunk_seconds": None,
        "delivered_after_300_seconds": False,
    }
    digest = hashlib.sha256()
    status = "completed"
    for data in chunks:
        elapsed = clock() - started
        result["chunks"] += 1
        result["last_chunk_seconds"] = round(elapsed, 3)
        if result["first_chunk_seconds"] is None:
            result["first_chunk_seconds"] = round(elapsed, 3)
        for choice in data["choices"]:
            delta = choice["delta"] if "delta" in choice and choice["delta"] is not None else {}
            text = delta["content"] if "content" in delta and delta["content"] is not None else ""
            reasoning = delta["reasoning_content"] if "reasoning_content" in delta and delta["reasoning_content"] is not None else ""
            result["text_characters"] += len(text)
            result["reasoning_characters"] += len(reasoning)
            digest.update(text.encode())
            if elapsed > 300 and (text or reasoning):
                result["delivered_after_300_seconds"] = True
            if "finish_reason" in choice and choice["finish_reason"]:
                result["finish_reason"] = choice["finish_reason"]
        if "usage" in data and data["usage"]:
            result["usage"] = data["usage"]
        if elapsed >= observe_seconds:
            status = "stopped_after_observation_window"
            break
    result["status"] = status
    result["answer_sha256"] = digest.hexdigest()
    return result


def delivered_late(result: Mapping[str, Any]) -> bool:
    return "delivered_after_300_seconds" in result and result["delivered_after_300_seconds"] is True


def main() -> None:  # pragma: no cover - runs inside the deployment container
    from elspeth.core.litellm_policy import configure_litellm_pricing

    configure_litellm_pricing()
    import logging

    import httpx
    import litellm

    logging.disable(logging.CRITICAL)
    config = json.load(sys.stdin)
    key: str = config["key"]
    region: str = config["region"]
    host = bedrock_host(region)
    root = Path(config["result_root"]) / str(uuid4())
    root.mkdir(parents=True, mode=0o700)
    report: dict[str, Any] = {"started_at": datetime.now(UTC).isoformat(), "region": region, "models": [], "requests": []}
    lock = threading.Lock()

    def emit(event: str, **values: Any) -> None:
        with lock:
            print(json.dumps({"event": event, **values}), flush=True)

    def save() -> None:
        path = root / "result.json"
        path.write_text(json.dumps(report, indent=2))
        path.chmod(0o600)

    original_send = httpx.Client.send

    def send(client: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        check_request(request.url.host, request.url.path, expected_host=host)
        record = {"host": request.url.host, "path": request.url.path, "started_at": datetime.now(UTC).isoformat()}
        report["requests"].append(record)
        emit("http_request_started", **record)
        response = original_send(client, request, *args, **kwargs)
        record["status"] = response.status_code
        emit("bedrock_response_headers", status=response.status_code)
        return response

    # Deliberately replace the class method for this short-lived probe process, so every
    # outbound request is pinned to the Bedrock streaming endpoint before it is sent.
    type.__setattr__(httpx.Client, "send", send)

    def run(model: str, label: str, *, long: bool) -> dict[str, Any]:
        started = time.monotonic()
        done = threading.Event()
        result: dict[str, Any] = {"model": model, "label": label, "started_at": datetime.now(UTC).isoformat()}
        report["models"].append(result)
        save()

        def heartbeat() -> None:
            while not done.wait(20):
                emit("waiting", model=model, elapsed_seconds=round(time.monotonic() - started, 1))

        threading.Thread(target=heartbeat, daemon=True).start()
        try:
            arguments: dict[str, Any] = {
                "model": "bedrock/converse/" + model,
                "messages": [{"role": "user", "content": LONG_PROMPT if long else SHORT_PROMPT}],
                "api_key": key,
                "aws_region_name": region,
                "aws_bedrock_runtime_endpoint": "https://" + host,
                "stream": True,
                "timeout": 900,
                "num_retries": 0,
                "max_tokens": config["long_max_tokens"] if long else 32,
            }
            if long:
                arguments["reasoning_effort"] = "high"
            emit("call_starting", model=model, label=label)
            stream = litellm.completion(**arguments)
            result.update(
                consume_stream((chunk.model_dump() for chunk in stream), started=started, observe_seconds=config["observe_seconds"])
            )
        except Exception as exc:
            message = str(exc)
            result.update(
                status="failed",
                error_type=type(exc).__name__,
                credential_expired="expired" in message.lower(),
                error_summary=safe_error_summary(message, key),
            )
        finally:
            done.set()
            result["elapsed_seconds"] = round(time.monotonic() - started, 3)
            save()
            emit("call_result", **result)
        return result

    run(config["quick_model"], "quick_auth_and_stream", long=False)
    long_result = run(config["long_model"], "long_call", long=True)
    if long_result["status"] == "completed" or delivered_late(long_result):
        run(config["long_model"], "credential_check_after_long_call", long=False)
    report["completed_at"] = datetime.now(UTC).isoformat()
    save()
    emit(
        "probe_finished",
        result_path=str(root / "result.json"),
        over_five_minutes=any(delivered_late(entry) for entry in report["models"]),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
