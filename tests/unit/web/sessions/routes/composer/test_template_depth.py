"""Deep authored template expressions remain repairable YAML imports."""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from tests.unit.web.sessions.test_routes import _make_app

from elspeth.web.composer.state import _parse_template_names


def test_deep_template_name_scan_returns_typed_failure() -> None:
    names, error = _parse_template_names("{{ " + "*".join(["7"] * 300) + " }}{{ row.a }}")
    assert names is None
    assert error is not None and "nesting" in error
    control, control_error = _parse_template_names("{{ row.a }} and {{ row.b }}")
    assert control_error is None
    assert control is not None and control.row_fields == frozenset({"a", "b"})


@pytest.mark.asyncio
async def test_yaml_deep_template_does_not_return_server_error(tmp_path: Path) -> None:
    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "Deep template", "local")
    source = "{{ " + "*".join(["7"] * 300) + " }}{{ row.a }}"
    yaml_text = """
sources:
  source:
    plugin: csv
    on_success: score
    options:
      schema:
        mode: observed
transforms:
- name: score
  plugin: llm
  input: source
  on_success: main
  on_error: discard
  options:
    model: anthropic/claude-haiku-4.5
    prompt_template: 'TEMPLATE'
sinks:
  main:
    plugin: csv
    options:
      path: outputs/out.csv
    on_write_failure: discard
""".replace("TEMPLATE", source)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})
    assert response.status_code == 200, response.text
    assert response.json()["is_valid"] is False
