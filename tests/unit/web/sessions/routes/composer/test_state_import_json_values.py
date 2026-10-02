"""``POST /state/yaml`` refuses option values that are not JSON values.

YAML's SafeLoader builds ``date``, ``datetime``, ``bytes`` (``!!binary``),
``set`` (``!!set``) and non-finite floats (``.nan``/``.inf``). None of them
survives JSON persistence faithfully: the first four fail ``json.dumps`` at
``INSERT composition_states`` (a bare 500 with the cause lost) and a NaN
persists as non-standard JSON while the response shows ``null``. The import
boundary must refuse them as a named 400 before anything is persisted.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient
from tests.unit.web.sessions.test_routes import _make_app

from elspeth.web.execution.schemas import ValidationResult

_DOC = """
sources:
  source:
    plugin: csv
    on_success: main
    options:
      schema:
        mode: observed
      note: {value}
sinks:
  main:
    plugin: csv
    options:
      path: outputs/out.csv
    on_write_failure: discard
"""


async def _pass_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
    return ValidationResult(is_valid=True, checks=[], errors=[])


@pytest.mark.asyncio
async def test_import_keeps_json_string_option_value(tmp_path: Path) -> None:
    """Control: a plain string option imports and round-trips."""
    app, service = _make_app(tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    session = await service.create_session("alice", "JSON values control", "local")

    with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
        response = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": _DOC.format(value="hello")})

    assert response.status_code == 200, response.text
    record = await service.get_current_state(session.id)
    assert record is not None
    assert record.sources["source"]["options"]["note"] == "hello"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("yaml_value", "type_name"),
    [
        ("2024-01-02", "date"),
        ("2024-01-02T03:04:05Z", "datetime"),
        ("!!binary aGVsbG8=", "bytes"),
        ("!!set {a: null}", "set"),
        (".nan", "float"),
        (".inf", "float"),
        ("[1, 2024-01-02]", "date"),
    ],
)
async def test_import_refuses_non_json_option_values(tmp_path: Path, yaml_value: str, type_name: str) -> None:
    app, service = _make_app(tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    session = await service.create_session("alice", "JSON values", "local")

    with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
        response = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": _DOC.format(value=yaml_value)})

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail.startswith("sources.source.options.note")
    assert f"must be a JSON value, got {type_name}" in detail
    assert await service.get_current_state(session.id) is None
