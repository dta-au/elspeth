"""Prompt-file editor save: a file bound at a TEMPLATE position is refused over the 64 KiB template bound.

Both conditions hold for the refusal: the file is bound, and the binding is a
template position (``prompt_template`` or a query's ``template``). A system
prompt is plain text and keeps the 256 KiB file cap only; an unbound file and a
file used outside a prompt keep their own refusals. A refused save writes
nothing. The expected sentence is spelled here so the file collects on the tree
before the change, where such a save was accepted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from elspeth.web.blobs.service import content_hash
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.blobs.test_prompt_editor import (
    _admit,
    _approve_card,
    _bound,
    _live_prompt_setup,
    _save,
    _save_side_effects,
)
from tests.unit.web.blobs.test_routes import _create_session, _make_app, _upload_blob

LIMIT = 64 * 1024
TEMPLATE_POSITIONS = ["prompt_template", "queries.q.template", "queries.0.template"]


def _text(size: int) -> str:
    head = "Rate this review: {{ row.review }}. "
    return head + "x" * (size - len(head))


def _setup(tmp_path: Path, field_path: str) -> tuple[Any, Any, Any, str, dict[str, Any]]:
    client, service, blob_service, session_id, first, _second, events = _live_prompt_setup(
        tmp_path, field_path=field_path, multiple_files=True
    )
    _approve_card(service, blob_service, session_id, events["first"])
    return client, service, blob_service, session_id, first


@pytest.mark.parametrize("field_path", TEMPLATE_POSITIONS)
@pytest.mark.parametrize("size", [pytest.param(20_000, id="twenty-kb"), pytest.param(LIMIT, id="exactly-the-bound")])
def test_template_file_up_to_the_bound_saves(tmp_path: Path, field_path: str, size: int) -> None:
    client, _service, _blob_service, session_id, first = _setup(tmp_path, field_path)

    response = _save(client, session_id, first["id"], _text(size), first["content_hash"], field_path=field_path)

    assert response.status_code == 200, response.text
    assert response.json()["content_sha256"] == content_hash(_text(size).encode("utf-8"))


@pytest.mark.parametrize("field_path", TEMPLATE_POSITIONS)
def test_template_file_one_byte_over_the_bound_is_refused_and_writes_nothing(tmp_path: Path, field_path: str) -> None:
    client, service, blob_service, session_id, first = _setup(tmp_path, field_path)
    before = _save_side_effects(service, blob_service, session_id, first["id"])

    response = _save(client, session_id, first["id"], _text(LIMIT + 1), first["content_hash"], field_path=field_path)

    assert response.status_code == 413, response.text
    assert response.json()["detail"] == {
        "error_code": "prompt_file_too_large",
        "message": (
            f"{field_path} on step first is {LIMIT + 1} bytes; the limit for one prompt template is {LIMIT} bytes. "
            "Shorten it, or move reference text into a file_inject step and read it from the row."
        ),
    }
    assert _save_side_effects(service, blob_service, session_id, first["id"]) == before
    assert client.get(f"/api/sessions/{session_id}/blobs/{first['id']}/content").content == b"first before"


def test_multibyte_text_is_measured_in_bytes(tmp_path: Path) -> None:
    client, _service, _blob_service, session_id, first = _setup(tmp_path, "prompt_template")
    text = "{{ row.review }}" + "é" * ((LIMIT - 16) // 2) + "a"

    response = _save(client, session_id, first["id"], text, first["content_hash"])

    assert len(text) < LIMIT < len(text.encode("utf-8"))
    assert response.status_code == 413, response.text
    assert response.json()["detail"]["error_code"] == "prompt_file_too_large"


def test_system_prompt_file_is_not_a_template_and_keeps_the_file_cap_only(tmp_path: Path) -> None:
    client, service, blob_service, session_id, _first = _setup(tmp_path, "prompt_template")
    system = next(binding for binding in _admit(service, blob_service, session_id)[0].bindings if binding.field_path == "system_prompt")
    text = "You rate reviews. " + "y" * (2 * LIMIT)

    response = _save(client, session_id, str(system.blob_id), text, system.content_sha256, field_path="system_prompt")

    assert response.status_code == 200, response.text


def test_unbound_file_keeps_its_own_refusal(tmp_path: Path) -> None:
    app, _service, _ = _make_app(tmp_path)
    client = SyncASGITestClient(app)
    session_id = _create_session(client)
    blob = _upload_blob(client, session_id, b"before", "notes.txt", "text/plain")

    response = _save(client, session_id, blob["id"], _text(LIMIT + 1), blob["content_hash"])

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "prompt_file_unbound"


def test_file_used_outside_a_prompt_keeps_its_own_refusal(tmp_path: Path) -> None:
    app, service, _ = _make_app(tmp_path)
    client = SyncASGITestClient(app)
    session_id = _create_session(client)
    blob = _upload_blob(client, session_id, b"before", "notes.txt", "text/plain")
    _bound(client, session_id, blob["id"], blob["content_hash"], service, mixed=True)

    response = _save(client, session_id, blob["id"], _text(LIMIT + 1), blob["content_hash"])

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "prompt_file_mixed_use"
