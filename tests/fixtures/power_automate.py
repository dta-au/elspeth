"""Credential-free HTTP flow emulator with an independently durable target.

This fixture is deliberately not an ELSPETH adapter. It parses actual wire
requests and commits SQLite business actions before returning HTTP receipts.
"""

from __future__ import annotations

import hashlib
import json
import socket
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
import respx
import rfc8785

PROTOCOL = "elspeth.power-automate.v1"
ORIGIN = "https://flows.example.test"
PUBLIC_IP = "93.184.216.34"
READ_URL = ORIGIN + "/read?sig=synthetic-read"
WRITE_URL = ORIGIN + "/write?sig=synthetic-write"


def independent_payload_hash(data: object) -> str:
    encoded = rfc8785.dumps(data)
    return hashlib.sha256(encoded).hexdigest()


class DurablePowerAutomateFlow:
    """Atomic delivery-ID creation and read-only authoritative status."""

    def __init__(self, path: Path, *, pages: Sequence[Sequence[object]] = (), deduplicate: bool = True) -> None:
        self.path = path
        self.pages = [list(page) for page in pages] or [[]]
        self.snapshot_id = "snapshot-fixture"
        self.headers = {"content-type": "application/json", "x-flow-request-id": "first"}
        self.pretty = False
        self.deduplicate = deduplicate
        self.reject_record_ids: set[str] = set()
        self.absent_state = "not_applied"
        self.after_durable_write: Callable[[], None] | None = None
        self.after_status: Callable[[dict[str, object]], None] | None = None
        self.drop_next_write_response = False
        self.drop_write_response_at: int | None = None
        self.fail_read_at: int | None = None
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS deliveries (
                    delivery_id TEXT PRIMARY KEY, payload_sha256 TEXT NOT NULL,
                    data_json TEXT NOT NULL, state TEXT NOT NULL,
                    receipt_id TEXT, flow_run_id TEXT, reason_code TEXT);
                CREATE TABLE IF NOT EXISTS actions (
                    action_id INTEGER PRIMARY KEY, delivery_id TEXT NOT NULL, data_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests (
                    request_id INTEGER PRIMARY KEY, operation TEXT NOT NULL, body_json TEXT NOT NULL);
            """)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def requests(self, operation: str | None = None) -> list[dict[str, object]]:
        with self._connection() as db:
            rows = db.execute("SELECT operation, body_json FROM requests ORDER BY request_id").fetchall()
        return [json.loads(row["body_json"]) for row in rows if operation is None or row["operation"] == operation]

    def actions(self) -> list[dict[str, object]]:
        with self._connection() as db:
            rows = db.execute("SELECT delivery_id, data_json FROM actions ORDER BY action_id").fetchall()
        return [{"delivery_id": row["delivery_id"], "data": json.loads(row["data_json"])} for row in rows]

    def set_state(self, delivery_id: str, state: str) -> None:
        with self._connection() as db:
            assert db.execute("UPDATE deliveries SET state=? WHERE delivery_id=?", (state, delivery_id)).rowcount == 1

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["protocol"] == PROTOCOL
        operation = body["operation"]
        with self._connection() as db:
            db.execute("INSERT INTO requests(operation, body_json) VALUES (?, ?)", (operation, json.dumps(body)))
        if operation == "read":
            if self.fail_read_at == len(self.requests("read")):
                raise httpx.ReadError("controlled read failure", request=request)
            assert body["snapshot_id"] in (None, self.snapshot_id)
            page = 0 if body["cursor"] is None else int(body["cursor"])
            result = {
                "protocol": PROTOCOL,
                "snapshot_id": self.snapshot_id,
                "rows": self.pages[page],
                "next_cursor": str(page + 1) if page + 1 < len(self.pages) else None,
            }
        else:
            result = self._effect(body)
            if operation == "status" and self.after_status is not None:
                self.after_status(body)
            if operation == "write":
                if self.after_durable_write is not None:
                    self.after_durable_write()
                if self.drop_next_write_response or self.drop_write_response_at == len(self.requests("write")):
                    self.drop_next_write_response = False
                    raise httpx.ReadError("controlled response loss", request=request)
        return httpx.Response(200, content=json.dumps(result, indent=2 if self.pretty else None).encode(), headers=self.headers)

    def _effect(self, body: dict[str, object]) -> dict[str, object]:
        operation, delivery_id, payload_hash = body["operation"], body["delivery_id"], body["payload_sha256"]
        result: dict[str, object] = {"protocol": PROTOCOL, "delivery_id": delivery_id, "payload_sha256": payload_hash}
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
            if existing is not None and existing["payload_sha256"] != payload_hash:
                return {**result, "state": "unknown"}
            if operation == "status" and existing is None:
                return {**result, "state": self.absent_state}
            if operation == "write":
                data = body["data"]
                assert independent_payload_hash(data) == payload_hash
                if existing is None or not self.deduplicate:
                    assert isinstance(data, dict)
                    rejected = data["record_id"] in self.reject_record_ids
                    data_json = json.dumps(data, sort_keys=True)
                    if not rejected:
                        db.execute("INSERT INTO actions(delivery_id, data_json) VALUES (?, ?)", (delivery_id, data_json))
                    if existing is None:
                        db.execute(
                            "INSERT INTO deliveries VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                delivery_id,
                                payload_hash,
                                data_json,
                                "rejected" if rejected else "applied",
                                None if rejected else "receipt-" + str(delivery_id)[:16],
                                None if rejected else "flow-" + str(delivery_id)[:16],
                                "policy_denied" if rejected else None,
                            ),
                        )
                    existing = db.execute("SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
            assert existing is not None
            state = existing["state"]
            if state in ("pending", "expired", "conflict"):
                return {**result, "state": "unknown"}
            result["state"] = state
            if state == "applied":
                result.update(receipt_id=existing["receipt_id"], flow_run_id=existing["flow_run_id"])
            elif state == "rejected":
                result["reason_code"] = existing["reason_code"]
            return result

    @contextmanager
    def transport(self) -> Iterator[respx.MockRouter]:
        def dns(_host: str, port: int, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port))]

        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router, patch("socket.getaddrinfo", side_effect=dns):
            router.post(f"https://{PUBLIC_IP}:443/read?sig=synthetic-read").mock(side_effect=self.handle)
            router.post(f"https://{PUBLIC_IP}:443/write?sig=synthetic-write").mock(side_effect=self.handle)
            yield router


def pipeline_settings(tmp_path: Path, *, snapshot_for_resume: bool = True) -> dict[str, object]:
    return {
        "sources": {
            "records": {
                "plugin": "power_automate",
                "on_success": "incoming",
                "options": {
                    "auth": {"method": "sas_url", "trigger_url_secret": "${POWER_AUTOMATE_READ_TRIGGER_URL}"},
                    "allowed_origin": ORIGIN,
                    "snapshot_id": "snapshot-fixture",
                    "snapshot_for_resume": snapshot_for_resume,
                    "page_size": 10,
                    "query": {"dataset": "fixture"},
                    "on_validation_failure": "discard",
                    "schema": {"mode": "fixed", "fields": ["record_id: str", "result: str"]},
                },
            }
        },
        "transforms": [
            {
                "name": "copy",
                "plugin": "passthrough",
                "input": "incoming",
                "on_success": "publish",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            }
        ],
        "sinks": {
            "publish": {
                "plugin": "power_automate",
                "on_write_failure": "discard",
                "options": {
                    "auth": {"method": "sas_url", "trigger_url_secret": "${POWER_AUTOMATE_WRITE_TRIGGER_URL}"},
                    "allowed_origin": ORIGIN,
                    "fields": ["record_id", "result"],
                    "schema": {"mode": "flexible", "fields": ["record_id: str", "result: str"]},
                },
            }
        },
        "landscape": {"url": f"sqlite:///{tmp_path / 'landscape.db'}"},
        "payload_store": {"base_path": str(tmp_path / "payloads")},
        "concurrency": {"max_workers": 1},
        "checkpoint": {"enabled": True, "frequency": "every_row"},
    }
