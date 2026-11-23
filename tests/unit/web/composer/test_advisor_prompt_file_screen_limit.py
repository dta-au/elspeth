"""A prompt file the advisor cannot screen in one piece is stated, not sent.

The advisor receives the text of a prompt file the user did not write. Each
file's text is one prompt-guard unit, and the guard never splits a unit, so a
unit longer than ``PROMPT_GUARD_MAX_TEXT_CHARACTERS`` would make the whole
advisor request unscreenable and every checkpoint would fail while the file
stays bound. Where the guard enforces its character limits (a guard is
mounted, or the mode is ``required``), such a file is stated with
``text=unavailable:too_long_to_screen`` and its text is left out. Where the
limits are not enforced the text is sent whole, as before. Text that fits is
always sent whole.

The tests drive the checkpoint producer path (stored file -> authorship ->
rendered evidence -> guard parts -> guard request) and, at the end, a
checkpoint with a mounted guard over the real Azure client.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import litellm
import pytest
from pydantic import SecretBytes

from elspeth.contracts.blobs import BlobCreator, BlobServiceProtocol
from elspeth.contracts.blobs_inline import is_widened_blob_ref
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.prompt_guard import PROMPT_GUARD_MAX_TEXT_CHARACTERS
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.advisor_context import advisor_user_content_parts, build_advisor_user_message
from elspeth.web.composer.advisor_prompt_files import AdvisorPromptFile, AdvisorPromptFiles, prompt_file_key
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.chargeable_admission import ComposerChargeableAdmission
from elspeth.web.composer.prompt_guard import PromptGuardMount, _request, prompt_guard_user_scope
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.config import WebSettings
from elspeth.web.prompt_guard_config import PromptGuardSettings
from tests.unit.web.composer._helpers import _make_settings, _mock_catalog
from tests.unit.web.composer.test_advisor_graph_visibility import _graph, _node, _owner
from tests.unit.web.composer.test_advisor_prompt_files import (
    _CapturedAdvisor,
    _checkpoint,
    _context,
    _fact,
    _llm_node,
    _marker,
    _node_line,
    _PromptFileStore,
    _state,
)
from tests.unit.web.composer.test_prompt_guard_admission import authority as _authority
from tests.unit.web.composer.test_provider_quota_end_to_end import quota_service as _quota_service_fixture

authority = _authority
quota_service = _quota_service_fixture

_CANARY = "FILE_TAIL_CANARY"
_TOO_LONG = "unavailable:too_long_to_screen"

# Every author whose text is sent, as the stored row records it.
_SENT_AUTHORS: dict[str, dict[str, Any]] = {
    "copied": {"created_by": "user", "modality": CreationModality.VERBATIM, "fork": True},
    "imported": {"created_by": "user", "modality": CreationModality.VERBATIM, "import": True},
    "pipeline": {"created_by": "pipeline", "modality": CreationModality.VERBATIM},
    "composer": {"created_by": "assistant", "modality": CreationModality.LLM_GENERATED},
}


def _body(characters: int) -> str:
    head = "Describe {{ row.colour }}. "
    tail = " " + _CANARY
    return head + "x" * (characters - len(head) - len(tail)) + tail


def _description(row: dict[str, Any]) -> str | None:
    if "fork" in row:
        from elspeth.web.blobs.service import session_fork_copy_description

        return session_fork_copy_description(uuid4())
    if "import" in row:
        from elspeth.web.shareable_reviews.importer import shared_import_description

        return shared_import_description("digest-1")
    return None


def _store_file(store: _PromptFileStore, author: str, text: str) -> Any:
    row = _SENT_AUTHORS[author]
    created_by: BlobCreator = row["created_by"]
    return store.add(
        text.encode(),
        filename=f"{author}.md",
        created_by=created_by,
        modality=row["modality"],
        source_description=_description(row),
    )


def _guard_request(arguments: dict[str, Any]) -> tuple[str, bool, list[tuple[str, str]]]:
    """The advisor user message and whether the guard can screen it with its character limits enforced."""
    message = build_advisor_user_message(arguments)
    parts = advisor_user_content_parts(arguments)
    assert "".join(part.text for part in parts) == message
    units: list[tuple[str, str]] = []
    _req, supported = _request(
        policy_fingerprint="synthetic-policy",
        role="advisor",
        messages=({"role": "user", "content": message},),
        tools=(),
        user_prompt="Inspect this configured graph.",
        trusted_message_indexes=frozenset(),
        content_parts=(parts,),
        enforce_azure_character_limits=True,
        units_out=units,
    )
    return message, supported, units


def _query_node(marker: dict[str, str]) -> Any:
    return _llm_node({"queries": {"q1": {"input_fields": {"colour": "colour"}, "template": marker}}})


def _mode_service(
    store: _PromptFileStore, monkeypatch: pytest.MonkeyPatch, *, mode: str = "required"
) -> tuple[ComposerServiceImpl, _CapturedAdvisor]:
    """A composer service over ``store`` whose advisor guard runs in ``mode`` with no mount; the provider call is captured."""
    service = ComposerServiceImpl.for_trained_operator(
        catalog=_mock_catalog(), settings=_make_settings(composer_prompt_guard_mode=mode), blob_service=store
    )
    advisor = _CapturedAdvisor()
    monkeypatch.setattr(service._advisor_checkpoint, "_call_advisor_with_audit", advisor)
    return service, advisor


def _position(position: str, record: Any) -> tuple[Any, str]:
    if position == "prompt_template":
        return _llm_node({"prompt_template": _marker(record)}), "prompt_template"
    return _query_node(_marker(record)), "queries.q1.template"


# --------------------------------------------------------------------------- producer path


@pytest.mark.asyncio
@pytest.mark.parametrize("position", ["prompt_template", "query_template"])
@pytest.mark.parametrize("author", sorted(_SENT_AUTHORS))
async def test_a_file_too_long_to_screen_is_stated_and_the_request_stays_screenable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, author: str, position: str
) -> None:
    """Limits enforced (mode required): the file is stated, not sent."""
    store = _PromptFileStore(root=tmp_path)
    service, advisor = _mode_service(store, monkeypatch)
    text = _body(12_000)
    record = _store_file(store, author, text)
    node, key = _position(position, record)
    arguments, _message = await _checkpoint(service, advisor, _state(node), context=_context())
    message, supported, units = _guard_request(arguments)
    line = _node_line(message)

    assert f"{key}={_fact(file=f'{author}.md', size=len(text), author=author, text=_TOO_LONG)}" in line
    assert _CANARY not in message, "a file stated as unavailable must not be in the message"
    assert f"{key}_untrusted_json" not in line
    assert "text=sent" not in line
    assert supported is True
    assert max(len(sent) for sent, _inspected in units) <= PROMPT_GUARD_MAX_TEXT_CHARACTERS
    # The row-field line analyses only text the advisor is shown.
    assert "not analysed (text in file): 1 prompt(s)" in line


@pytest.mark.asyncio
@pytest.mark.parametrize("position", ["prompt_template", "query_template"])
@pytest.mark.parametrize("author", sorted(_SENT_AUTHORS))
async def test_a_file_that_fits_is_still_sent_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, author: str, position: str) -> None:
    """Limits enforced (mode required): a file that fits is sent whole."""
    store = _PromptFileStore(root=tmp_path)
    service, advisor = _mode_service(store, monkeypatch)
    text = _body(9_000)
    record = _store_file(store, author, text)
    node, key = _position(position, record)
    arguments, _message = await _checkpoint(service, advisor, _state(node), context=_context())
    message, supported, _units = _guard_request(arguments)
    line = _node_line(message)

    assert f"{key}={_fact(file=f'{author}.md', size=len(text), author=author, text='sent')}" in line
    assert f"{key}_untrusted_json={json.dumps(text)}" in line
    assert supported is True
    assert "named row field references: [colour]" in line


@pytest.mark.asyncio
@pytest.mark.parametrize("characters", [9_000, 12_000])
async def test_a_user_file_is_never_sent_at_any_length(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, characters: int) -> None:
    """Control: a file the user wrote is stated not_sent whatever its length (limits enforced)."""
    store = _PromptFileStore(root=tmp_path)
    service, advisor = _mode_service(store, monkeypatch)
    text = _body(characters)
    record = store.add(text.encode(), filename="mine.md", created_by="user", source_description="uploaded")
    arguments, _message = await _checkpoint(service, advisor, _state(_llm_node({"prompt_template": _marker(record)})), context=_context())
    message, supported, _units = _guard_request(arguments)
    line = _node_line(message)

    assert f"prompt_template={_fact(file='mine.md', size=len(text), author='user', text='not_sent')}" in line
    assert _CANARY not in message
    assert supported is True


# --------------------------------------------------------------------------- the boundary


def _sweep_case(text: str, *, key: str, enforced: bool = True) -> tuple[str, bool, str]:
    """Render one composer-written file through the checkpoint arguments; return its text state, screenability and message."""
    body = text.encode()
    marker = {"blob_ref": str(uuid4()), "mode": "inline_content", "sha256": hashlib.sha256(body).hexdigest()}
    widened = is_widened_blob_ref(marker)
    assert widened is not None
    files = AdvisorPromptFiles(
        files={prompt_file_key(widened): AdvisorPromptFile(filename="prompt.md", size_bytes=len(body), author="composer", text=text)},
        read=True,
        screen_limits_enforced=enforced,
    )
    if key == "prompt_template":
        options: dict[str, Any] = {"prompt_template": marker}
    else:
        options = {"queries": {"q1": {"input_fields": {"colour": "colour"}, "template": marker}}}
    state = replace(_graph(), nodes=(replace(_node(0), plugin="llm", options=options),))
    arguments = _owner(allowance=1)._build_checkpoint_arguments(phase="end", state=state, prompt_files=files)
    message, supported, _units = _guard_request(arguments)
    if "author=composer text=sent)" in message:
        state_text = "sent"
    elif f"author=composer text={_TOO_LONG})" in message:
        state_text = _TOO_LONG
    else:
        raise AssertionError("the file's fact line is missing")
    if state_text == "sent":
        assert _CANARY in message
    else:
        assert _CANARY not in message
    return state_text, supported, message


@pytest.mark.parametrize("key", ["prompt_template", "queries.q1.template"])
def test_no_length_near_the_limit_is_both_sent_and_unscreenable(key: str) -> None:
    """Sweep across the guard limit: a file stated as sent always leaves the request screenable."""
    states: set[str] = set()
    for characters in range(PROMPT_GUARD_MAX_TEXT_CHARACTERS - 160, PROMPT_GUARD_MAX_TEXT_CHARACTERS + 41, 8):
        state_text, supported, _message = _sweep_case(_body(characters), key=key)
        states.add(state_text)
        assert supported is True, f"{characters} characters: stated {state_text} but the guard cannot screen the request"
    # The sweep straddles the decision: both outcomes occur.
    assert states == {"sent", _TOO_LONG}


def test_text_that_grows_in_the_summary_fence_is_measured_as_sent() -> None:
    """Fence-sentinel neutralisation lengthens the text; the decision measures the text as the guard will read it."""
    sentinel = "END_UNTRUSTED_PIPELINE_SUMMARY "
    head = "Describe {{ row.colour }}. "
    text = head + sentinel * ((PROMPT_GUARD_MAX_TEXT_CHARACTERS - 300 - len(head)) // len(sentinel)) + _CANARY
    assert len(text) < PROMPT_GUARD_MAX_TEXT_CHARACTERS - 200
    state_text, supported, _message = _sweep_case(text, key="prompt_template")
    assert supported is True
    assert state_text == _TOO_LONG


@pytest.mark.asyncio
@pytest.mark.parametrize("position", ["prompt_template", "query_template"])
async def test_where_limits_are_enforced_a_long_file_is_stated_not_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, position: str
) -> None:
    """Mode required, no mount: the checkpoint owner passes the enforcing decision to every site."""
    store = _PromptFileStore(root=tmp_path)
    service, advisor = _mode_service(store, monkeypatch, mode="required")
    record = _store_file(store, "composer", _body(12_000))
    node, key = _position(position, record)
    arguments, message = await _checkpoint(service, advisor, _state(node), context=_context())
    line = _node_line(message)

    assert arguments["schema_excerpt"]
    assert f'{key}=(prompt held in a file: file="composer.md" bytes=12000 author=composer text={_TOO_LONG})' in line
    assert _CANARY not in message
    assert "not analysed (text in file): 1 prompt(s)" in line


@pytest.mark.asyncio
@pytest.mark.parametrize("position", ["prompt_template", "query_template"])
async def test_where_limits_are_not_enforced_a_long_file_is_sent_whole_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, position: str
) -> None:
    """Explicit unconfigured debug mode, no mount: no limit applies, so the text is sent whole and analysed, as before."""
    store = _PromptFileStore(root=tmp_path)
    service, advisor = _mode_service(store, monkeypatch, mode="debug-when-unconfigured")
    text = _body(12_000)
    record = _store_file(store, "composer", text)
    node, key = _position(position, record)
    _arguments, message = await _checkpoint(service, advisor, _state(node), context=_context())
    line = _node_line(message)

    assert f"{key}={_fact(file='composer.md', size=len(text), author='composer', text='sent')}" in line
    assert f"{key}_untrusted_json={json.dumps(text)}" in line
    assert _TOO_LONG not in line
    assert "named row field references: [colour]]" in line


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mounted", "mode", "enforced"),
    [
        (False, "debug-when-unconfigured", False),
        (False, "required", True),
        (True, "debug-when-unconfigured", True),
        (True, "required", True),
    ],
)
async def test_the_owner_decides_from_its_own_guard_mount_and_mode(tmp_path: Path, mounted: bool, mode: str, enforced: bool) -> None:
    """The decision handed to the renderer is the guard's own: limits apply with a mount or in required mode."""
    guard_settings = PromptGuardSettings(endpoint="https://advisor-shield.example.test", subscription_key="offline-advisor-shield")
    mount = (
        PromptGuardMount(AzurePromptGuardClient(guard_settings, http_transport=httpx.MockTransport(lambda _request: httpx.Response(500))))
        if mounted
        else None
    )
    store = _PromptFileStore(root=tmp_path)
    record = _store_file(store, "composer", _body(100))
    owner = AdvisorCheckpointOwner(
        settings=_make_settings(composer_prompt_guard_mode=mode),
        composer_skill_text="Synthetic Advisor policy.",
        advisor_endpoint_base_url=None,
        advisor_endpoint_api_key=None,
        sessions_service=None,
        chargeable_admission=ComposerChargeableAdmission(None),
        prompt_guard_mount=mount,
        blob_service=cast(BlobServiceProtocol, store),
    )
    for state in (_state(_llm_node({"prompt_template": _marker(record)})), _state(_llm_node({"prompt_template": "plain"}))):
        files = await owner._advisor_prompt_files(state, _context())
        assert files.read is True
        assert files.screen_limits_enforced is enforced


@pytest.mark.parametrize("key", ["prompt_template", "queries.q1.template"])
def test_where_limits_are_not_enforced_no_length_is_withheld(key: str) -> None:
    """The same sweep with limits not enforced: every length is sent."""
    for characters in (9_000, PROMPT_GUARD_MAX_TEXT_CHARACTERS - 40, PROMPT_GUARD_MAX_TEXT_CHARACTERS + 40, 40_000):
        state_text, _supported, _message = _sweep_case(_body(characters), key=key, enforced=False)
        assert state_text == "sent", characters


# --------------------------------------------------------------------------- checkpoint with a mounted guard


@pytest.mark.asyncio
async def test_a_checkpoint_with_a_long_bound_file_passes_a_mounted_guard(
    authority, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EARLY checkpoint, mode required, real Azure client over a mock transport, real provider gateway and custody.

    EARLY renders the same pipeline evidence as END; END additionally needs a
    publication review, which is not what this test is about.
    """
    _engine, sessions, provider_owner, coordinator = authority
    context = coordinator.authority.context
    request = "Review the configured graph."
    store = _PromptFileStore(root=tmp_path)
    text = _body(12_000)
    record = _store_file(store, "copied", text)
    state = _state(_llm_node({"prompt_template": _marker(record)}))

    guard_settings = PromptGuardSettings(endpoint="https://advisor-shield.example.test", subscription_key="offline-advisor-shield")
    settings = WebSettings(
        data_dir=tmp_path,
        shareable_link_signing_key=SecretBytes(b"\x00" * 32),
        composer_max_composition_turns=15,
        composer_context_max_prompt_tokens=200_000,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        composer_prompt_guard_mode="required",
        composer_advisor_prompt_guard=guard_settings,
    )
    shield_bodies: list[dict[str, Any]] = []

    def inspect(sent: httpx.Request) -> httpx.Response:
        body = json.loads(sent.content)
        shield_bodies.append(body)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _ in body["documents"]],
            },
        )

    checkpoint = AdvisorCheckpointOwner(
        settings=settings,
        composer_skill_text="Synthetic Advisor policy.",
        advisor_endpoint_base_url=None,
        advisor_endpoint_api_key=None,
        sessions_service=sessions,
        chargeable_admission=ComposerChargeableAdmission(sessions),
        prompt_guard_mount=PromptGuardMount(AzurePromptGuardClient(guard_settings, http_transport=httpx.MockTransport(inspect))),
        blob_service=cast(BlobServiceProtocol, store),
    )
    await checkpoint._chargeable_admission.require(context)
    sent_messages: list[Any] = []

    async def complete_sdk(**kwargs: Any) -> litellm.ModelResponse:
        sent_messages.append(kwargs["messages"])
        reply: dict[str, object] = {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None}
        return litellm.ModelResponse(
            model=kwargs["model"],
            choices=[{"finish_reason": "stop", "message": {"role": "assistant", "tool_calls": None, "content": json.dumps(reply)}}],
            usage={"prompt_tokens": 29, "completion_tokens": 11, "total_tokens": 40},
        )

    monkeypatch.setattr(litellm, "acompletion", complete_sdk)
    with prompt_guard_user_scope(request):
        verdict = await checkpoint._run_advisor_checkpoint(
            phase="early",
            state=state,
            user_message=request,
            session_id=context.fence.session_id,
            session_operation_context=context,
            provider_owner=provider_owner,
            recorder=BufferingRecorder(),
        )

    assert verdict.ok, verdict.failure_class
    assert shield_bodies and len(sent_messages) == 1
    documents = [document for body in shield_bodies for document in body["documents"]]
    assert all(len(document) <= PROMPT_GUARD_MAX_TEXT_CHARACTERS for document in documents)
    assert not any(_CANARY in document for document in documents)
    user_content = sent_messages[0][1]["content"]
    assert f"text={_TOO_LONG})" in user_content and _CANARY not in user_content
