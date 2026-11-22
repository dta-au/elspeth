"""The large-graph fixture generator and the battery's setup step.

Setup runs through the battery's own :class:`~scripts.staging.opslib.WebClient`
against the real session, blob, import and review routes, with the real
plugin catalog and a real operator profile registry (one LLM profile). Only
authentication is stood in; the profile's server credential comes from the
real server secret store, set in the environment for each test.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from scripts.staging import composer_edit_battery as battery
from scripts.staging import large_graph_fixture as lgf
from scripts.staging import structural_cycle as cycle
from scripts.staging.fixture_setup import graph_document_bytes, setup_fixture
from scripts.staging.opslib import REPO_ROOT, ApiError, HttpResponse, OperationFailed, UsageError, WebClient

from elspeth.contracts.credential_material import CredentialTraversalPolicy, find_credential_material
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.blobs.routes import create_blobs_router
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.composer.yaml_importer import MAX_RUNTIME_YAML_IMPORT_CHARS, composition_state_from_runtime_yaml
from elspeth.web.config import WebSettings
from elspeth.web.credential_guard import CredentialMaterialRefused, require_no_credential_material_in_state
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
from elspeth.web.secrets.server_store import ServerSecretStore
from elspeth.web.secrets.service import ScopedSecretResolver, WebSecretService
from elspeth.web.secrets.user_store import UserSecretStore
from elspeth.web.sessions.converters import state_from_record
from tests.fixtures.composer_fakes import StaticPluginSnapshotFactory
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.sessions.test_routes import _make_app

BASE = "http://127.0.0.1:8451"
PROFILE = "task-role"
# The server's whole-state credential scan limit, read from the policy rather than restated.
SCAN_LIMIT = CredentialTraversalPolicy().max_nodes


SECRET_NAME = "TEST_LLM_KEY"


@pytest.fixture(autouse=True)
def _server_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server-scoped credential the test LLM profile names, read by the real server secret store."""
    monkeypatch.setenv(SECRET_NAME, "fake-provider-key")
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "fixture-test-fingerprint-key")


def _server(tmp_path: Path, *, raise_server_exceptions: bool = True) -> SyncASGITestClient:
    """The session, blob, import and review routes with the real catalog and one usable LLM profile."""
    app, service = _make_app(tmp_path)
    settings = WebSettings.model_validate(
        {
            **app.state.settings.model_dump(),
            "llm_profiles": {
                PROFILE: {"provider": "openrouter", "model": "test/model", "credential_scope": "server", "credential_ref": SECRET_NAME}
            },
            "default_llm_profile": PROFILE,
        }
    )
    runtime = RuntimeWebPluginConfig.from_settings(settings)
    policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime)
    unrestricted = PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service())
    llm = PluginId("transform", "llm")
    snapshot = PluginAvailabilitySnapshot.create(
        policy_hash=policy.policy_hash,
        principal_scope="local:alice",
        available=unrestricted.available,
        unavailable=(),
        selected=unrestricted.selected,
        usable_profile_aliases=((llm, (PROFILE,)),),
        selected_profile_aliases=((llm, PROFILE),),
        binding_generation_fingerprint="fixture-generation",
    )
    app.state.operator_profile_registry = OperatorProfileRegistry(policy=policy, settings=runtime)
    app.state.plugin_snapshot_factory = StaticPluginSnapshotFactory(snapshot)
    secrets = WebSecretService(
        user_store=UserSecretStore(engine=service._engine, master_key="fixture-master-key-32-chars-min!"),
        server_store=ServerSecretStore(allowlist=(SECRET_NAME,)),
    )
    app.state.scoped_secret_resolver = ScopedSecretResolver(secrets, auth_provider_type=app.state.settings.auth_provider)
    blobs = BlobServiceImpl(service._engine, tmp_path, session_operation_authority=service.session_operation_authority)
    app.state.blob_service = blobs
    service._inline_blob_read = lambda context, blob_id: blobs.read_blob_content_sync(blob_id, context)
    app.include_router(create_blobs_router())
    return SyncASGITestClient(app, raise_server_exceptions=raise_server_exceptions)


def _client(server: SyncASGITestClient, *, shown: Callable[[str, bytes], bytes] | None = None) -> WebClient:
    """A battery client whose requests reach ``server``; ``shown`` may rewrite a response body by path."""

    def send(method: str, url: str, *, headers: Any, body: bytes | None, timeout: float) -> HttpResponse:
        path = url.removeprefix(BASE)
        response = server.request(method, path, headers=dict(headers), content=body)
        content = response.content if shown is None else shown(path, response.content)
        return HttpResponse(status=response.status_code, body=content, headers=dict(response.headers))

    client = WebClient(BASE, transport=send)
    client.use_token("route-test-token")
    return client


class Events:
    def __init__(self) -> None:
        self.seen: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event: str, **fields: Any) -> None:
        self.seen.append((event, fields))


def _written(tmp_path: Path, spec: lgf.FixtureSpec, name: str = "fixture") -> lgf.LoadedFixture:
    return lgf.load_fixture(lgf.write_fixture(lgf.build_fixture(spec), tmp_path / name))


def _set_up(
    tmp_path: Path, spec: lgf.FixtureSpec, *, shown: Callable[[str, bytes], bytes] | None = None
) -> tuple[WebClient, str, Events, dict[str, Any] | OperationFailed]:
    server = _server(tmp_path / "server")
    client = _client(server, shown=shown)
    session = client.post_json("/api/sessions", {"title": "fixture"})["id"]
    loaded = _written(tmp_path, spec)
    events = Events()
    try:
        return client, session, events, setup_fixture(client, session, loaded, emit=events)
    except OperationFailed as exc:
        return client, session, events, exc


def _chain(pipeline: dict[str, Any]) -> list[dict[str, Any]]:
    steps = {step["name"]: step for step in [*pipeline["transforms"], *(pipeline["gates"] if "gates" in pipeline else [])]}
    route = next(iter(pipeline["sources"].values()))["on_success"]
    ordered: list[dict[str, Any]] = []
    while True:
        step = next((item for item in steps.values() if item["input"] == route), None)
        if step is None:
            return ordered
        ordered.append(step)
        route = step["on_success"] if "on_success" in step else step["routes"]["true"]


# ── generation: deterministic, on target ───────────────────────────────────


def test_the_same_seed_gives_the_same_bytes_and_another_seed_other_text_of_the_same_sizes() -> None:
    first = lgf.build_fixture(lgf.preset_spec("acceptance"))
    again = lgf.build_fixture(lgf.preset_spec("acceptance"))
    other = lgf.build_fixture(lgf.preset_spec("acceptance", seed=2))
    assert lgf.manifest(first) == lgf.manifest(again)
    assert first.yaml_text == again.yaml_text
    assert [file.content for file in first.files] == [file.content for file in again.files]
    assert [file.content for file in first.files] != [file.content for file in other.files]
    assert first.measures()["prompt_bytes_in_files"] == other.measures()["prompt_bytes_in_files"]
    assert first.measures()["steps"] == other.measures()["steps"]


def test_the_acceptance_preset_hits_its_targets() -> None:
    fixture = lgf.build_fixture(lgf.preset_spec("acceptance"))
    measures = fixture.measures()
    # Counts are exact; prompt bytes are exact by construction (the last file absorbs the rounding).
    assert measures["steps"] == 27
    assert measures["llm_steps"] == 12
    assert measures["multi_query_steps"] == 4
    # 16 single-step prompts, 4 rubrics, 17 query templates and 2 injected files, every one file-held.
    assert measures["files"] == 39
    assert measures["prompt_bytes_in_files"] == 200_000
    assert measures["inline_prompt_bytes"] == 0
    assert measures["sinks"] == 3
    sizes = {file.name: len(file.content) for file in fixture.files}
    # Tolerance, from the seeded +-15% spread (measured over seeds 1-20: 1.7-2.4 KB, 4.5-6.5 KB, 11-18 KB, 7.3-13 KB).
    assert all(1_000 <= size <= 5_000 for name, size in sizes.items() if name.endswith((".system.md", ".user.md")))
    assert all(3_000 <= size <= 10_000 for name, size in sizes.items() if name.endswith(".query.md"))
    assert all(10_000 <= size <= 30_000 for name, size in sizes.items() if name.endswith(".rubric.md"))
    assert all(5_000 <= size <= 20_000 for name, size in sizes.items() if name.startswith("attach_"))
    queries = {step["name"]: len(step["options"]["queries"]) for step in fixture.pipeline["transforms"] if "queries" in step["options"]}
    assert queries == {"cs1_scoring": 3, "cs2_scoring": 3, "risk_review": 3, "overall_rubric": 8}
    # A file-held query template is absent from the YAML and bound to its designed slot.
    slots = {file.slot for file in fixture.files}
    for step in fixture.pipeline["transforms"]:
        for name, query in step["options"]["queries"].items() if "queries" in step["options"] else []:
            assert "template" not in query
            assert f"node:{step['name']}.options.queries.{name}.template" in slots


def test_the_graph_is_one_wired_chain_whose_steps_read_only_what_earlier_steps_wrote() -> None:
    fixture = lgf.build_fixture(lgf.preset_spec("acceptance"))
    pipeline = fixture.pipeline
    source = pipeline["sources"]["submissions"]
    assert source["options"]["schema"] == {"mode": "fixed", "fields": [f"{name}: str" for name in lgf.SOURCE_FIELDS]}
    assert len(lgf.SOURCE_FIELDS) == 14
    assert source["on_validation_failure"] == "rejected_rows"
    chain = _chain(pipeline)
    assert len(chain) == fixture.measures()["steps"]
    assert chain[0]["plugin"] == "file_inject"
    assert chain[-1]["name"] == "output_mapper" and chain[-1]["on_success"] == "assessment_results"
    assert pipeline["sinks"]["assessment_results"]["options"]["schema"]["mode"] == "flexible"
    available = set(lgf.SOURCE_FIELDS)
    for step in chain:
        if "options" in step and "required_input_fields" in step["options"]:
            assert set(step["options"]["required_input_fields"]) <= available, step["name"]
        if step["name"] == "attach_framework":
            available |= {entry["output_field"] for entry in step["options"]["files"].values()}
        elif "plugin" in step and step["plugin"] == "llm":
            options = step["options"]
            assert options["required_input_fields"], step["name"]
            available.add(options["response_field"])
            for name, query in options["queries"].items() if "queries" in options else []:
                available |= {f"{name}_{field['suffix']}" for field in query["output_fields"]}
    slots = {file.slot for file in fixture.files}
    for step in chain:
        if "plugin" in step and step["plugin"] == "llm" and "queries" not in step["options"]:
            # A single-prompt step has its system and user prompts in files; the user prompt names what it reads.
            assert {f"node:{step['name']}.options.system_prompt", f"node:{step['name']}.options.prompt_template"} <= slots
            user = next(file for file in fixture.files if file.slot == f"node:{step['name']}.options.prompt_template")
            assert all(f"row['{name}']" in user.content.decode() for name in step["options"]["required_input_fields"])


@pytest.mark.parametrize(
    ("steps", "llm_steps", "total"), [(25, 10, 150_000), (30, 12, 250_000), (30, 14, 200_000), (20, 6, 60_000), (27, 12, 1_153_433)]
)
def test_other_sizes_are_hit_exactly(steps: int, llm_steps: int, total: int) -> None:
    measures = lgf.build_fixture(lgf.preset_spec("acceptance", steps=steps, llm_steps=llm_steps, total_prompt_bytes=total)).measures()
    assert (measures["steps"], measures["llm_steps"], measures["prompt_bytes_in_files"]) == (steps, llm_steps, total)


def test_a_prompt_file_over_the_server_s_per_file_cap_is_refused() -> None:
    per_file, _ = lgf._inline_blob_caps()
    with pytest.raises(UsageError, match=f"exceed the server's {per_file}-byte per-file cap"):
        lgf.build_fixture(lgf.preset_spec("single-node", queries_per_llm_step=2, total_prompt_bytes=4_000 + 2 * (per_file + 1)))


def test_too_few_steps_for_the_llm_steps_is_refused() -> None:
    with pytest.raises(UsageError, match="--steps must be at least 15"):
        lgf.build_fixture(lgf.preset_spec("acceptance", steps=14))


def test_the_single_node_preset_shape() -> None:
    fixture = lgf.build_fixture(lgf.preset_spec("single-node"))
    measures = fixture.measures()
    assert (measures["steps"], measures["llm_steps"], measures["queries"], measures["sinks"], measures["sources"]) == (1, 1, 32, 2, 1)
    # 32 file-held query templates of 15,600 bytes (499,200) and a 4,000-byte system prompt.
    queries = [file for file in fixture.files if ".options.queries." in str(file.slot)]
    assert [len(file.content) for file in queries] == [15_600] * 32
    assert (measures["files"], measures["prompt_bytes_in_files"], measures["inline_prompt_bytes"]) == (33, 503_200, 0)
    assert fixture.positions == (cycle.Position("source", "capacity_probe", "results", True),)


def test_the_inline_single_node_variant_fits_under_the_import_limit_only_as_literal_blocks() -> None:
    fixture = lgf.build_fixture(lgf.preset_spec("single-node", query_templates="inline"))
    assert fixture.measures()["inline_prompt_bytes"] == 32 * 15_600
    # Written as a person writes YAML (literal blocks), it fits under the import limit; the YAML reads back exactly.
    assert len(fixture.yaml_text) <= MAX_RUNTIME_YAML_IMPORT_CHARS
    assert yaml.safe_load(fixture.yaml_text) == fixture.pipeline


def test_query_names_go_past_the_criteria_without_repeating() -> None:
    fixture = lgf.build_fixture(lgf.preset_spec("acceptance", queries_per_llm_step=lgf.MAX_QUERIES, query_templates="inline"))
    queries = next(step for step in fixture.pipeline["transforms"] if step["name"] == "cs1_scoring")["options"]["queries"]
    assert len(queries) == lgf.MAX_QUERIES
    assert list(queries)[:2] == ["cs1_accuracy", "cs1_reasoning"]
    assert "cs1_accuracy_2" in queries
    with pytest.raises(UsageError, match="must be 1-500"):
        lgf.build_fixture(lgf.preset_spec("acceptance", queries_per_llm_step=lgf.MAX_QUERIES + 1, query_templates="inline"))


@pytest.mark.parametrize(("queries", "values"), [(309, 4085), (310, 4098)])
def test_the_scan_count_is_measured_with_the_server_s_own_scan(queries: int, values: int) -> None:
    spec = lgf.preset_spec("single-node", queries_per_llm_step=queries, total_prompt_bytes=4_000 + 400 * queries, query_templates="inline")
    yaml_text = lgf.build_fixture(spec).yaml_text
    assert lgf.state_scan_values(yaml_text) == values
    # Control: the server's state guard agrees with the measure on which side of its limit the graph is.
    state = composition_state_from_runtime_yaml(yaml_text)
    if values <= SCAN_LIMIT:
        require_no_credential_material_in_state(state, surface="fixture_test")
    else:
        with pytest.raises(CredentialMaterialRefused):
            require_no_credential_material_in_state(state, surface="fixture_test")


def test_the_acceptance_positions_rotate_through_the_graph() -> None:
    fixture = lgf.build_fixture(lgf.preset_spec("acceptance"))
    llm = set(fixture.llm_steps)
    first, middle, last = fixture.positions
    assert first.upstream == "submissions" and not first.downstream_is_sink
    assert middle.anchor in llm and middle.upstream in llm and middle.downstream in llm
    assert (last.anchor, last.downstream, last.downstream_is_sink) == ("output_mapper", "assessment_results", True)


# ── writing and reading ────────────────────────────────────────────────────


def test_a_written_fixture_reads_back_and_a_changed_file_is_refused(tmp_path: Path) -> None:
    root = lgf.write_fixture(lgf.build_fixture(lgf.preset_spec("acceptance")), tmp_path / "fx")
    loaded = lgf.load_fixture(root)
    assert loaded.manifest["measures"]["files"] == 39
    assert set(loaded.contents) == {entry["name"] for entry in [loaded.manifest["source"]["file"], *loaded.manifest["files"]]}
    target = root / "files" / loaded.manifest["files"][0]["name"]
    target.write_bytes(target.read_bytes() + b"x")
    with pytest.raises(UsageError, match="does not match the manifest"):
        lgf.load_fixture(root)


def test_the_generator_refuses_a_directory_inside_the_repository_or_one_that_is_not_empty(tmp_path: Path) -> None:
    fixture = lgf.build_fixture(lgf.preset_spec("single-node"))
    with pytest.raises(UsageError, match="outside the repository"):
        lgf.write_fixture(fixture, REPO_ROOT / "fixture-out")
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "x").write_text("x")
    with pytest.raises(UsageError, match="not empty"):
        lgf.write_fixture(fixture, tmp_path / "busy")


def test_the_generator_command_writes_the_fixture(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert lgf.main(["--preset", "acceptance", "--out", str(tmp_path / "cli"), "--steps", "25", "--llm-steps", "10"]) == 0
    event = json.loads(capsys.readouterr().out)
    assert (event["event"], event["steps"], event["llm_steps"]) == ("fixture_written", 25, 10)
    assert (tmp_path / "cli" / "pipeline.yaml").is_file()


# ── setup through the real routes ──────────────────────────────────────────


@pytest.mark.parametrize(
    "overrides",
    [{}, {"file_inject": "omit"}, {"query_templates": "inline"}],
    ids=["default-file-held-file-inject", "file-inject-omitted", "inline-query-templates"],
)
def test_the_acceptance_fixture_imports_validates_and_passes_its_reviews(tmp_path: Path, overrides: dict[str, str]) -> None:
    client, session, events, summary = _set_up(tmp_path, lgf.preset_spec("acceptance", **overrides))
    assert type(summary) is dict, summary
    assert summary["valid"] is True
    assert summary["reviews_left"] == []
    assert len(summary["prompt_reviews_approved"]) == 12
    assert summary["nodes"] == 27
    state = client.get_json(f"/api/sessions/{session}/state")
    assert state["is_valid"] is True and state["validation_errors"] in (None, [])
    assert client.get_json(f"/api/sessions/{session}/interpretations?status=pending")["events"] == []
    # The positions the manifest lists fit the imported graph, and the untouched graph passes the verifier.
    positions = [cycle.Position.from_json(entry) for entry in summary["positions"]]
    counts = cycle.check_fixture(state, positions=positions)
    assert (counts["llm_nodes"], counts["sinks"]) == (12, 3)
    for position in positions:
        assert cycle.verify_state(state, state, [position.anchor], position) == []
    assert [event for event, _ in events.seen] == ["fixture_files_uploaded", "fixture_setup"]


def test_the_single_node_fixture_imports_and_validates_at_its_default_size(tmp_path: Path) -> None:
    client, session, _, summary = _set_up(tmp_path, lgf.preset_spec("single-node"))
    assert type(summary) is dict, summary
    assert summary["valid"] is True and summary["prompt_reviews_approved"] == ["capacity_probe"]
    state = client.get_json(f"/api/sessions/{session}/state")
    # The query text is held in files: the graph document carries only their markers.
    assert graph_document_bytes(state) < 32 * 15_600 // 10
    assert cycle.check_fixture(state, positions=[cycle.Position("source", "capacity_probe", "results", True)])["prompt_queries"] == 32


def test_inline_queries_just_under_the_scan_limit_import_and_just_over_are_refused_at_import(tmp_path: Path) -> None:
    server = _server(tmp_path / "server")
    client = _client(server)
    session = client.post_json("/api/sessions", {"title": "under"})["id"]
    under = _written(
        tmp_path,
        lgf.preset_spec("single-node", queries_per_llm_step=309, total_prompt_bytes=4_000 + 400 * 309, query_templates="inline"),
        "under",
    )
    summary = setup_fixture(client, session, under, emit=Events())
    assert summary["valid"] is True
    assert summary["measures"]["state_scan_values_at_import"] == 4085
    # The state as the server stores it, the bound system prompt included, measured with the same scan.
    record = asyncio.run(server.app.state.session_service.get_current_state(UUID(session)))
    stored = lgf.scan_values_of_state(state_from_record(record))
    assert 4085 <= stored <= SCAN_LIMIT, stored
    other = client.post_json("/api/sessions", {"title": "over"})["id"]
    over = _written(
        tmp_path,
        lgf.preset_spec("single-node", queries_per_llm_step=310, total_prompt_bytes=4_000 + 400 * 310, query_templates="inline"),
        "over",
    )
    # The size refusal arrives as a credential refusal (category traversal_limit); the message says "credential".
    with pytest.raises(ApiError, match=r'/state/yaml returned HTTP 422.*"category":"traversal_limit"') as refused:
        setup_fixture(client, other, over, emit=Events())
    assert refused.value.status == 422
    assert "credential_material_rejected" in str(refused.value)


# Near the aggregate cap each query file must also stay under the template
# limit (every query is a template position), so these graphs spread the
# text over 100 file-held queries rather than the acceptance preset's 39 files.
_NEAR_CAP_QUERIES = 100


def test_file_held_text_just_under_the_aggregate_cap_validates(tmp_path: Path) -> None:
    _, aggregate = lgf._inline_blob_caps()
    spec = lgf.preset_spec("single-node", queries_per_llm_step=_NEAR_CAP_QUERIES, total_prompt_bytes=int(0.9 * aggregate))
    _, _, _, summary = _set_up(tmp_path, spec)
    assert type(summary) is dict, summary
    assert summary["valid"] is True


def test_file_held_text_just_over_the_aggregate_cap_is_refused_at_import_and_nothing_is_saved(tmp_path: Path) -> None:
    """Over the cap the import answers 413 ``prompt_file_too_large`` with the figures, and no graph is saved."""
    _, aggregate = lgf._inline_blob_caps()
    server = _server(tmp_path / "server", raise_server_exceptions=False)
    client = _client(server)
    session = client.post_json("/api/sessions", {"title": "over"})["id"]
    spec = lgf.preset_spec("single-node", queries_per_llm_step=_NEAR_CAP_QUERIES, total_prompt_bytes=aggregate + 100_000)
    over = _written(tmp_path, spec)
    with pytest.raises(ApiError, match=r"/state/yaml returned HTTP 413.*prompt_file_too_large") as refused:
        setup_fixture(client, session, over, emit=Events())
    assert refused.value.status == 413
    assert "Nothing was imported" in str(refused.value)
    assert client.get_json(f"/api/sessions/{session}/state") is None


def _stored_state(server: SyncASGITestClient, session: str) -> Any:
    record = asyncio.run(server.app.state.session_service.get_current_state(UUID(session)))
    return state_from_record(record)


def _file_held_queries(tmp_path: Path, queries: int) -> tuple[SyncASGITestClient, WebClient, str, lgf.LoadedFixture]:
    server = _server(tmp_path / "server")
    client = _client(server)
    session = client.post_json("/api/sessions", {"title": "bound"})["id"]
    spec = lgf.preset_spec("single-node", queries_per_llm_step=queries, total_prompt_bytes=4_000 + 200 * queries)
    return server, client, session, _written(tmp_path, spec)


def test_251_file_held_queries_keep_the_stored_state_within_the_scan_bound(tmp_path: Path) -> None:
    server, client, session, loaded = _file_held_queries(tmp_path, 251)
    summary = setup_fixture(client, session, loaded, emit=Events())
    assert summary["valid"] is True
    state = _stored_state(server, session)
    assert find_credential_material(state.to_dict()) is None
    require_no_credential_material_in_state(state, surface="fixture_test")


def test_252_file_held_queries_are_refused_at_import_and_nothing_is_saved(tmp_path: Path) -> None:
    """The import scans the state with its file markers bound, so a graph over the bound never reaches the store.

    The YAML alone is under the bound; the bound state is over it, and the
    import refuses it (``credential_material_rejected``, category
    ``traversal_limit``) instead of saving a graph that could not be opened.
    """
    _, client, session, loaded = _file_held_queries(tmp_path, 252)
    assert loaded.manifest["measures"]["state_scan_values_at_import"] <= SCAN_LIMIT
    with pytest.raises(ApiError, match=r'/state/yaml returned HTTP 422.*"category":"traversal_limit"') as refused:
        setup_fixture(client, session, loaded, emit=Events())
    assert "credential_material_rejected" in str(refused.value)
    assert client.get_json(f"/api/sessions/{session}/state") is None


def test_setup_refuses_a_session_that_already_has_a_graph(tmp_path: Path) -> None:
    client, session, _, summary = _set_up(tmp_path, lgf.preset_spec("single-node"))
    assert type(summary) is dict
    again = lgf.load_fixture(tmp_path / "fixture")
    with pytest.raises(OperationFailed, match="graph is empty"):
        setup_fixture(client, session, again, emit=Events())


def test_setup_never_approves_a_card_whose_text_is_not_the_fixture_s(tmp_path: Path) -> None:
    def other_text(path: str, body: bytes) -> bytes:
        # The server's card for one step shows text other than the file uploaded for it.
        if not path.endswith("/prompt"):
            return body
        snapshot = json.loads(body)
        if snapshot["node_id"] == "feedback_letter":
            snapshot["bindings"][0]["text"] += "changed"
        return json.dumps(snapshot).encode()

    client, session, _, outcome = _set_up(tmp_path, lgf.preset_spec("acceptance", file_inject="omit"), shown=other_text)
    assert isinstance(outcome, OperationFailed)
    assert "the prompt review for feedback_letter does not show exactly the fixture's files" in str(outcome)
    pending = client.get_json(f"/api/sessions/{session}/interpretations?status=pending")["events"]
    assert [event["affected_node_id"] for event in pending] == ["feedback_letter"]


# ── the battery's command line ─────────────────────────────────────────────


def test_setup_without_execute_contacts_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = lgf.write_fixture(lgf.build_fixture(lgf.preset_spec("single-node")), tmp_path / "fx")

    def no_network(url: str) -> WebClient:
        raise AssertionError("a dry run must not build a client")

    argv = ["setup", "--fixture", str(root), "--base-url", BASE, "--session", "11111111-2222-4333-8444-555555555555", "--account", "tester"]
    assert battery.main(argv, client_factory=no_network) == 0
    event = json.loads(capsys.readouterr().out.splitlines()[-1])
    # The source data, the system prompt and 32 query templates.
    assert event["event"] == "dry_run" and len(event["uploads"]) == 34


def test_setup_needs_a_fixture() -> None:
    with pytest.raises(UsageError, match="setup needs --fixture"):
        battery.main(["setup", "--base-url", BASE, "--session", "11111111-2222-4333-8444-555555555555", "--account", "tester"])


def test_the_workload_rotates_through_the_fixture_s_positions(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = lgf.write_fixture(lgf.build_fixture(lgf.preset_spec("acceptance")), tmp_path / "fx")
    assert battery.main(["plan", "--workload", "structural-cycle", "--steps", "40", "--fixture", str(root)]) == 0
    planned = [json.loads(line) for line in capsys.readouterr().out.splitlines() if '"planned_edit"' in line]
    first, middle, _ = lgf.load_fixture(root).positions
    assert planned[0]["message"].startswith(f"Add a passthrough transform named compact_before_a between submissions and {first.anchor}.")
    assert planned[11]["message"].startswith(
        f"Add a passthrough transform named cycle1_after_a between {middle.anchor} and {middle.downstream}."
    )
    assert planned[21]["message"].startswith(
        "Add a passthrough transform named cycle2_after_a between output_mapper and the assessment_results sink."
    )
    assert planned[30]["message"].startswith(f"Add a passthrough transform named cycle3_before_a between submissions and {first.anchor}.")


def test_a_fixture_and_explicit_names_together_are_refused(tmp_path: Path) -> None:
    root = lgf.write_fixture(lgf.build_fixture(lgf.preset_spec("single-node")), tmp_path / "fx")
    with pytest.raises(UsageError, match="not both"):
        battery.main(["plan", "--workload", "structural-cycle", "--fixture", str(root), "--anchor", "capacity_probe"])
