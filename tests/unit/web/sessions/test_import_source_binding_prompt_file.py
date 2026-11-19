"""A source bound without ``mode`` beside a file-backed llm prompt.

``POST /api/sessions/{id}/state/yaml`` binds an uploaded source file by
writing ``path`` and ``blob_ref`` onto the source options, with no ``mode``;
the composer's own bind tool also writes ``mode: bind_source``. The inline
ref walk called the mode-less source options a malformed file marker, so no
inline snapshot was built for the state, the prompt card writer refused the
state's prompt file, and the route let ``PromptFileReviewStale`` escape (a 500
on a real server). Re-importing one's own YAML export produces the shape,
because the export strips ``mode: bind_source``.

Everything here runs against the real blob service and its custody reads: the
import route, the composer tool handler, the card writer and the approval
route.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from elspeth.contracts.composer_interpretation import InterpretationKind, InterpretationSurfaceOrigin
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.core.blobs_inline import _discover_blob_content_refs
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.composer._validation_probe import _PROBE_PROMPT_FILE_PLACEHOLDER, prepare_validation_probe_options
from elspeth.web.composer.interpretation_surfacing import surface_pending_interpretation_reviews_for_state
from elspeth.web.composer.yaml_generator import generate_pipeline_dict
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import prompt_file_approvals_table
from elspeth.web.sessions.prompt_file_review import PROMPT_FILE_CHANGED_MESSAGE, PromptFileReviewStale
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.routes.sessions import _rewrite_source_blob_options
from tests.helpers.prompt_review import accept_body
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient
from tests.unit.web.composer import test_blob_inline_tools as blob_tools
from tests.unit.web.sessions.test_routes import _close_app_session_engines as _close_app_session_engines
from tests.unit.web.sessions.test_routes import _make_app, _ready_blob_record

NODE = "rate_step"
CSV = b"review\nGreat product and fast delivery\nArrived broken and support was slow\n"
USER = b"Rate this customer review from 1 to 5. Review: {{ row['review'] }}\n"
SYSTEM = b"You rate customer product reviews. Reply with only the number.\n"
INLINE_SYSTEM = "    system_prompt: You rate customer product reviews.\n"
INLINE_USER = "    prompt_template: \"Rate this review 1 to 5. Review: {{ row['review'] }}\"\n"
PROMPT_SLOT = f"node:{NODE}.options.prompt_template"
SYSTEM_SLOT = f"node:{NODE}.options.system_prompt"
SINK_BINDING = {"main": {"path": {"folder": "outputs", "relative_path": "out.json"}}}
_SINK_PLUGIN_MODE = "      mode: write\n"


def _yaml(*, source_mode: bool = False, node_extra: str = "", sink_extra: str = "") -> str:
    """One csv source, one llm step, one json sink that declares its fields."""
    mode = "      mode: bind_source\n" if source_mode else ""
    return (
        "sources:\n  source:\n    plugin: csv\n    on_success: reviews\n    on_validation_failure: discard\n    options:\n"
        f"{mode}"
        "      schema:\n        mode: flexible\n        fields:\n        - 'review: str'\n"
        f"transforms:\n- name: {NODE}\n  plugin: llm\n  input: reviews\n  on_success: main\n  on_error: discard\n  options:\n"
        "    profile: task-role\n    required_input_fields:\n    - review\n    response_field: rating\n"
        "    schema:\n      mode: observed\n"
        f"{node_extra}"
        "sinks:\n  main:\n    plugin: json\n    on_write_failure: discard\n    options:\n      format: json\n      mode: write\n"
        "      collision_policy: auto_increment\n      schema:\n        mode: flexible\n        fields:\n"
        "        - 'review: str'\n        - 'rating: str'\n"
        f"{sink_extra}"
    )


class _Env:
    """Session routes over a REAL blob service, wired as ``create_app`` wires them."""

    def __init__(self, tmp_path: Path) -> None:
        self.app, self.service = _make_app(tmp_path)
        self.blobs = BlobServiceImpl(self.service._engine, tmp_path, session_operation_authority=self.service.session_operation_authority)
        self.app.state.blob_service = self.blobs
        # The same closure web/app.py hands the session service.
        self.service._inline_blob_read = lambda context, blob_id: self.blobs.read_blob_content_sync(blob_id, context)
        self.client = TestClient(self.app)
        self.data_dir = tmp_path

    async def session(self) -> uuid.UUID:
        session_id: uuid.UUID = (await self.service.create_session("alice", "Import", "local")).id
        return session_id

    async def upload(self, session_id: uuid.UUID, name: str, content: bytes, mime_type: str) -> Any:
        with fenced_operation_context(self.service._engine, str(session_id), operation_kind=SessionOperationKind.CREATE) as context:
            return await self.blobs.create_blob(session_id, name, content, mime_type, session_operation_context=context)

    def import_yaml(self, session_id: uuid.UUID, body: dict[str, Any]) -> Any:
        try:
            return self.client.post(f"/api/sessions/{session_id}/state/yaml", json=body)
        except PromptFileReviewStale as exc:
            pytest.fail(f"the import route let PromptFileReviewStale escape (a 500 on a real server): {exc}")

    async def source_options(self, session_id: uuid.UUID) -> dict[str, Any]:
        record = await self.service.get_current_state(session_id)
        assert record is not None
        return dict(record.sources["source"]["options"])

    async def pending(self, session_id: uuid.UUID) -> list[Any]:
        events: list[Any] = await self.service.list_interpretation_events(session_id, status="pending")
        return events

    async def approve_prompt_card(self, session_id: uuid.UUID) -> dict[str, Any]:
        """Approve the one pending prompt card the way the card does; return its snapshot."""
        cards = [event for event in await self.pending(session_id) if event.kind is InterpretationKind.LLM_PROMPT_TEMPLATE]
        assert len(cards) == 1, [(event.kind, event.affected_node_id) for event in await self.pending(session_id)]
        snapshot = self.client.get(f"/api/sessions/{session_id}/interpretations/{cards[0].id}/prompt")
        assert snapshot.status_code == 200, snapshot.text
        resolved = self.client.post(
            f"/api/sessions/{session_id}/interpretations/{cards[0].id}/resolve",
            json=accept_body(self.client, session_id, cards[0]),
        )
        assert resolved.status_code == 200, resolved.text
        body: dict[str, Any] = snapshot.json()
        return body

    def approval_rows(self, session_id: uuid.UUID) -> list[tuple[str, str, str]]:
        with self.service._engine.connect() as conn:
            rows = conn.execute(
                select(
                    prompt_file_approvals_table.c.node_id,
                    prompt_file_approvals_table.c.field_path,
                    prompt_file_approvals_table.c.trigger,
                ).where(prompt_file_approvals_table.c.session_id == str(session_id))
            ).all()
        return sorted((row.node_id, row.field_path, row.trigger) for row in rows)


# ── The import request shapes of the rehearsal (README "Rehearsal 3") ────────
#
# A: one prompt file + source_blob_ids, no sink binding.   V2: A plus a sink binding.
# D: one prompt file + a sink binding, no source binding.
# B: V2 with ``mode: bind_source`` written in the YAML.     C: B with two files.
# A and V2 are the defect; D, B and C imported before the fix and are controls.


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["A", "V2", "D", "B", "C"])
async def test_import_binds_a_source_file_beside_a_prompt_file(tmp_path: Path, shape: str) -> None:
    env = _Env(tmp_path)
    session_id = await env.session()
    csv = await env.upload(session_id, "reviews.csv", CSV, "text/csv")
    user = await env.upload(session_id, "user.txt", USER, "text/plain")
    bindings = {PROMPT_SLOT: {"blob_id": str(user.id)}}
    files = {"prompt_template": str(user.id)}
    node_extra = INLINE_SYSTEM
    if shape == "C":
        system = await env.upload(session_id, "system.txt", SYSTEM, "text/plain")
        bindings[SYSTEM_SLOT] = {"blob_id": str(system.id)}
        files["system_prompt"] = str(system.id)
        node_extra = ""
    body: dict[str, Any] = {"yaml": _yaml(source_mode=shape in ("B", "C"), node_extra=node_extra), "inline_blob_bindings": bindings}
    if shape != "D":
        body["source_blob_ids"] = {"source": str(csv.id)}
    if shape != "A":
        body["sink_path_bindings"] = SINK_BINDING

    response = env.import_yaml(session_id, body)

    assert response.status_code == 200, response.text
    options = await env.source_options(session_id)
    if shape == "D":
        assert "blob_ref" not in options
    else:
        # The import adds no ``mode``: the planner's shape exists only where the YAML wrote it.
        assert options["blob_ref"] == str(csv.id)
        assert options["path"] == csv.storage_path
        assert ("mode" in options) is (shape in ("B", "C"))
    snapshot = await env.approve_prompt_card(session_id)
    assert sorted((binding["field_path"], binding["blob_id"]) for binding in snapshot["bindings"]) == sorted(files.items())
    assert env.approval_rows(session_id) == sorted((NODE, field_path, "card") for field_path in files)
    assert [event.kind for event in await env.pending(session_id) if event.kind is InterpretationKind.LLM_PROMPT_TEMPLATE] == []


# ── Sessions that already hold the shape ────────────────────────────────────


async def _session_holding_unmoded_source(env: _Env) -> tuple[uuid.UUID, Any]:
    """Import a bound source with INLINE prompts: this always worked, so sessions hold the shape."""
    session_id = await env.session()
    csv = await env.upload(session_id, "reviews.csv", CSV, "text/csv")
    response = env.import_yaml(
        session_id,
        {
            "yaml": _yaml(node_extra=INLINE_SYSTEM + INLINE_USER),
            "source_blob_ids": {"source": str(csv.id)},
            "sink_path_bindings": SINK_BINDING,
        },
    )
    assert response.status_code == 200, response.text
    options = await env.source_options(session_id)
    assert options["blob_ref"] == str(csv.id) and options["path"] == csv.storage_path and "mode" not in options
    return session_id, csv


async def _wire_prompt_file_and_surface(env: _Env, session_id: uuid.UUID, blob_id: str) -> Any:
    """Bind a prompt file with the real ``wire_blob_inline_ref`` handler, save, and surface its card."""
    record = await env.service.get_current_state(session_id)
    assert record is not None
    with fenced_operation_context(env.service._engine, str(session_id)) as operation:
        result = blob_tools.execute_tool(
            "wire_blob_inline_ref",
            {"field_path": PROMPT_SLOT, "blob_id": blob_id},
            state_from_record(record),
            env.app.state.catalog_service,
            data_dir=str(env.data_dir),
            session_engine=env.service._engine,
            session_id=str(session_id),
            session_operation_context=operation,
            session_operation_authority=env.service.session_operation_authority,
        )
        assert result.success is True, [error.message for error in result.validation.errors]
        data = result.updated_state.to_dict()
        saved = await env.service.save_composition_state(
            session_id,
            CompositionStateData(
                sources=data["sources"],
                nodes=data["nodes"],
                edges=data["edges"],
                outputs=data["outputs"],
                metadata_=data["metadata"],
                is_valid=True,
            ),
            provenance="tool_call",
            session_operation_context=operation,
        )
        try:
            await surface_pending_interpretation_reviews_for_state(
                result.updated_state,
                sessions_service=env.service,
                session_id=str(session_id),
                current_state_id=str(saved.id),
                surface_origin=InterpretationSurfaceOrigin.COMPOSER_LLM,
                model_identifier="planner-model",
                model_version="1",
                provider="test",
                composer_skill_hash="a" * 64,
                session_operation_context=operation,
            )
        except PromptFileReviewStale as exc:
            pytest.fail(f"the card writer refused a prompt file because of the session's source binding: {exc}")
    return saved


@pytest.mark.asyncio
async def test_session_holding_an_unmoded_source_can_bind_and_approve_a_prompt_file(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    session_id, csv = await _session_holding_unmoded_source(env)
    user = await env.upload(session_id, "user.txt", USER, "text/plain")

    saved = await _wire_prompt_file_and_surface(env, session_id, str(user.id))

    # The tool handler leaves the source binding as it found it.
    options = await env.source_options(session_id)
    assert options["blob_ref"] == str(csv.id) and "mode" not in options
    cards = [event for event in await env.pending(session_id) if event.kind is InterpretationKind.LLM_PROMPT_TEMPLATE]
    assert [(event.affected_node_id, event.composition_state_id) for event in cards] == [(NODE, saved.id)]
    snapshot = await env.approve_prompt_card(session_id)
    assert [(binding["field_path"], binding["blob_id"]) for binding in snapshot["bindings"]] == [("prompt_template", str(user.id))]
    assert env.approval_rows(session_id) == [(NODE, "prompt_template", "card")]


@pytest.mark.asyncio
async def test_revert_restores_an_unmoded_source_and_resurfaces_its_prompt_card(tmp_path: Path) -> None:
    """Revert copies the source options as stored and writes the restored prompt card."""
    env = _Env(tmp_path)
    session_id, csv = await _session_holding_unmoded_source(env)
    inline = await env.service.get_current_state(session_id)
    assert inline is not None
    user = await env.upload(session_id, "user.txt", USER, "text/plain")
    wired = await _wire_prompt_file_and_surface(env, session_id, str(user.id))
    back = env.client.post(f"/api/sessions/{session_id}/state/revert", json={"operation_id": str(uuid.uuid4()), "state_id": str(inline.id)})
    assert back.status_code == 200, back.text

    try:
        response = env.client.post(
            f"/api/sessions/{session_id}/state/revert", json={"operation_id": str(uuid.uuid4()), "state_id": str(wired.id)}
        )
    except PromptFileReviewStale as exc:
        pytest.fail(f"revert let PromptFileReviewStale escape for a state holding an unmoded source binding: {exc}")

    assert response.status_code == 200, response.text
    restored = await env.service.get_current_state(session_id)
    assert restored is not None
    assert restored.id != wired.id
    assert dict(restored.sources["source"]["options"]) == dict(wired.sources["source"]["options"])
    assert "mode" not in restored.sources["source"]["options"]
    assert restored.sources["source"]["options"]["blob_ref"] == str(csv.id)
    cards = [event for event in await env.pending(session_id) if event.kind is InterpretationKind.LLM_PROMPT_TEMPLATE]
    assert [(event.affected_node_id, event.composition_state_id) for event in cards] == [(NODE, restored.id)]


def test_fork_rebases_an_unmoded_source_binding_without_adding_mode() -> None:
    parent_blob, child_blob = uuid.uuid4(), uuid.uuid4()
    child_session = uuid.uuid4()
    copied = _ready_blob_record(blob_id=child_blob, session_id=child_session, storage_path=f"/data/blobs/{child_session}/reviews.csv")
    parent_options = {"blob_ref": str(parent_blob), "path": "/data/blobs/parent/reviews.csv", "schema": {"mode": "observed"}}

    rebuilt, changed = _rewrite_source_blob_options(parent_options, {parent_blob: copied}, {}, field_path="sources.source.options")

    assert changed is True
    assert rebuilt == {"blob_ref": str(child_blob), "path": copied.storage_path, "schema": {"mode": "observed"}}
    assert _discover_blob_content_refs({"sources": {"source": {"plugin": "csv", "options": rebuilt}}}) == []


# ── The combined shape through the validation probe and the runtime YAML ─────


@pytest.mark.asyncio
async def test_combined_shape_through_the_validation_probe(tmp_path: Path) -> None:
    """Imported unmoded source + file-backed llm prompt + field-declaring sink."""
    env = _Env(tmp_path)
    session_id = await env.session()
    csv = await env.upload(session_id, "reviews.csv", CSV, "text/csv")
    user = await env.upload(session_id, "user.txt", USER, "text/plain")
    response = env.import_yaml(
        session_id,
        {
            "yaml": _yaml(node_extra=INLINE_SYSTEM),
            "source_blob_ids": {"source": str(csv.id)},
            "inline_blob_bindings": {PROMPT_SLOT: {"blob_id": str(user.id)}},
            "sink_path_bindings": SINK_BINDING,
        },
    )
    assert response.status_code == 200, response.text

    for approved in (False, True):
        if approved:
            await env.approve_prompt_card(session_id)
        record = await env.service.get_current_state(session_id)
        assert record is not None
        state = state_from_record(record)
        source, node, sink = state.sources["source"], state.nodes[0], state.outputs[0]

        # The probe prepares options, it does not interpret the source binding:
        # ``blob_ref`` and ``path`` leave it exactly as stored, and no ``mode`` appears.
        probed_source = prepare_validation_probe_options(source.options, plugin=source.plugin)
        assert probed_source["blob_ref"] == str(csv.id)
        assert probed_source["path"] == csv.storage_path
        assert "mode" not in probed_source
        assert probed_source["schema"] == {"mode": "flexible", "fields": ["review: str"]}

        # The file-backed prompt is a VALID marker, so the probe stands text in for it.
        assert node.options["prompt_template"]["blob_ref"] == str(user.id)
        probed_node = prepare_validation_probe_options(node.options, plugin=node.plugin)
        assert type(probed_node["prompt_template"]) is str
        assert probed_node["prompt_template"].startswith(_PROBE_PROMPT_FILE_PLACEHOLDER)
        assert "row['review']" in probed_node["prompt_template"]
        assert probed_node["system_prompt"] == "You rate customer product reviews."
        assert ("approved_prompt_artifact_hash" in probed_node) is approved

        probed_sink = prepare_validation_probe_options(sink.options, plugin=sink.plugin)
        assert probed_sink["schema"] == {"mode": "flexible", "fields": ["review: str", "rating: str"]}

        # Authoring validation of the whole state. BEFORE approval the llm
        # step's guarantees reach the sink. AFTER approval, on this base
        # (hotfix/0815-combined @ b29734fb5), the same state is reported
        # invalid with ``sink_contract_violation`` ("Producer (llm)
        # guarantees: [(none)]"): the known defect, owned by another session
        # and independent of the source binding (the rehearsal measured it
        # with ``mode: bind_source`` too).
        #
        # FLIPS WHEN THAT FIX LANDS: the ``approved`` arm below must then
        # read ``assert codes == []``. It is asserted, not xfailed, so the
        # merge that fixes it turns this line red and is seen.
        codes = [entry.error_code for entry in state.validate().errors]
        if approved:
            assert codes == ["sink_contract_violation"]
        else:
            assert codes == []


@pytest.mark.asyncio
async def test_runtime_yaml_drops_the_source_binding_either_way(tmp_path: Path) -> None:
    """Run admission reads the lowered pipeline, which never carried ``blob_ref`` or ``mode``."""
    env = _Env(tmp_path)
    lowered: dict[str, Any] = {}
    for source_mode in (False, True):
        session_id = await env.session()
        csv = await env.upload(session_id, "reviews.csv", CSV, "text/csv")
        user = await env.upload(session_id, "user.txt", USER, "text/plain")
        response = env.import_yaml(
            session_id,
            {
                "yaml": _yaml(source_mode=source_mode, node_extra=INLINE_SYSTEM),
                "source_blob_ids": {"source": str(csv.id)},
                "inline_blob_bindings": {PROMPT_SLOT: {"blob_id": str(user.id)}},
                "sink_path_bindings": SINK_BINDING,
            },
        )
        assert response.status_code == 200, response.text
        record = await env.service.get_current_state(session_id)
        assert record is not None
        pipeline = generate_pipeline_dict(state_from_record(record))
        options = pipeline["sources"]["source"]["options"]
        assert options["path"] == csv.storage_path
        assert "blob_ref" not in options and "mode" not in options
        assert [ref.field_path for ref in _discover_blob_content_refs(pipeline)] == [PROMPT_SLOT]
        lowered[str(source_mode)] = {key: value for key, value in options.items() if key != "path"}

    assert lowered["False"] == lowered["True"]


# ── What must stay refused, and how the route says so ───────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("node_extra", "sink_extra", "malformed_path"),
    [
        pytest.param("    system_prompt:\n      blob_ref: BLOB\n      path: /data/prompt.txt\n", "", SYSTEM_SLOT, id="prompt-slot"),
        pytest.param(INLINE_SYSTEM, "      blob_ref: BLOB\n      path: outputs/x.json\n", "output:main.options", id="sink-options"),
    ],
)
async def test_import_refuses_an_unmoded_blob_ref_outside_source_options(
    tmp_path: Path, node_extra: str, sink_extra: str, malformed_path: str
) -> None:
    """``blob_ref`` + ``path`` with no ``mode`` is a source binding ONLY on a source's own options.

    The sink is written without its plugin's own ``mode: write`` option, so its
    options hold exactly the keys a source binding holds: a walker that skipped
    the shape on any mapping would import this.
    """
    env = _Env(tmp_path)
    session_id = await env.session()
    csv = await env.upload(session_id, "reviews.csv", CSV, "text/csv")
    user = await env.upload(session_id, "user.txt", USER, "text/plain")
    stray = await env.upload(session_id, "stray.txt", SYSTEM, "text/plain")

    response = env.import_yaml(
        session_id,
        {
            "yaml": _yaml(node_extra=node_extra, sink_extra=sink_extra).replace("BLOB", str(stray.id)).replace(_SINK_PLUGIN_MODE, ""),
            "source_blob_ids": {"source": str(csv.id)},
            "inline_blob_bindings": {PROMPT_SLOT: {"blob_id": str(user.id)}},
        },
    )

    assert _SINK_PLUGIN_MODE in _yaml()
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error_code"] == "import_blob_reference_malformed"
    assert malformed_path in detail["message"]
    assert "source:source.options" not in detail["message"]
    assert PROMPT_FILE_CHANGED_MESSAGE not in detail["message"]
    assert str(stray.id) not in detail["message"]
    assert await env.service.get_current_state(session_id) is None
    assert await env.pending(session_id) == []


@pytest.mark.asyncio
async def test_import_reports_an_unverifiable_prompt_file_under_its_own_code(tmp_path: Path) -> None:
    """A pasted marker whose sha256 is not the file's: refused, and not as "review it again"."""
    env = _Env(tmp_path)
    session_id = await env.session()
    csv = await env.upload(session_id, "reviews.csv", CSV, "text/csv")
    user = await env.upload(session_id, "user.txt", USER, "text/plain")
    marker = f"    prompt_template:\n      blob_ref: {user.id}\n      mode: inline_content\n      sha256: {'c' * 64}\n"

    response = env.import_yaml(session_id, {"yaml": _yaml(node_extra=INLINE_SYSTEM + marker), "source_blob_ids": {"source": str(csv.id)}})

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error_code"] == "import_prompt_file_unverified"
    assert detail["error_code"] != PromptFileReviewStale.error_code
    assert PROMPT_FILE_CHANGED_MESSAGE not in detail["message"]
    assert await env.service.get_current_state(session_id) is None
    assert await env.pending(session_id) == []
