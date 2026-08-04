"""Prompt-cache layout: split catalog/state context and message-level markers.

elspeth-a79f1b2e6b: the round-3 acceptance battery showed the cached prefix
covered only tools + system while the ~21k-token dynamic context (98.4%
deployment-constant) and the growing history were re-billed at full input
price on every call. The fix splits the context into a deployment-constant
catalog message (cacheable, directly after system) and a session-varying
state message (after chat history, so history stays a stable cacheable
prefix), and adds an opt-in sliding marker on the last message for the
freeform tool loop.

See docs/acceptance/2026-08-05-compose-token-cost-addendum.md item 1.
"""

from __future__ import annotations

from elspeth.web.composer.llm_response_parsing import apply_anthropic_cache_markers
from elspeth.web.composer.prompts import (
    CATALOG_CONTEXT_PREFIX,
    STATE_CONTEXT_PREFIX,
    build_catalog_context_string,
)
from tests.unit.web.composer.test_prompts import (
    StubCatalog,
    _blob_source_state,
    _empty_state,
    _trained_policy_context,
    build_context_string,
    build_messages,
)

_CATALOG_KEYS = ('"available_plugins"', '"plugin_hints"', '"plugin_policy"', '"authoring_aids"')
_STATE_KEYS = ('"current_state"', '"composer_progress"', '"schema_contract_evidence"')


class TestContextSplit:
    def test_catalog_context_contains_catalog_blocks_only(self) -> None:
        view, snapshot = _trained_policy_context(StubCatalog())

        content = build_catalog_context_string(view, plugin_snapshot=snapshot)

        assert content.startswith(CATALOG_CONTEXT_PREFIX)
        assert "UNTRUSTED DATA" in content
        for key in _CATALOG_KEYS:
            assert key in content
        for key in _STATE_KEYS:
            assert key not in content

    def test_state_context_contains_session_blocks_only(self) -> None:
        content = build_context_string(_empty_state(), StubCatalog(), schemas_loaded=frozenset())

        assert content.startswith(STATE_CONTEXT_PREFIX)
        assert "UNTRUSTED DATA" in content
        for key in _STATE_KEYS:
            assert key in content
        for key in _CATALOG_KEYS:
            assert key not in content

    def test_catalog_context_is_byte_stable_across_calls(self) -> None:
        view, snapshot = _trained_policy_context(StubCatalog())

        first = build_catalog_context_string(view, plugin_snapshot=snapshot)
        second = build_catalog_context_string(view, plugin_snapshot=snapshot)

        assert first == second


class TestBuildMessagesCacheLayout:
    def test_layout_system_catalog_history_state_user(self) -> None:
        history = [
            {"role": "user", "content": "previous question"},
            {"role": "assistant", "content": "previous answer"},
        ]

        messages = build_messages(history, _empty_state(), "new question", StubCatalog())

        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert messages[1]["content"].startswith(CATALOG_CONTEXT_PREFIX)
        assert messages[2]["content"] == "previous question"
        assert messages[3]["content"] == "previous answer"
        assert messages[-2]["role"] == "user"
        assert messages[-2]["content"].startswith(STATE_CONTEXT_PREFIX)
        assert messages[-1] == {"role": "user", "content": "new question"}

    def test_state_change_leaves_catalog_message_byte_stable(self) -> None:
        catalog = StubCatalog()

        empty = build_messages([], _empty_state(), "q", catalog)
        sourced = build_messages([], _blob_source_state(), "q", catalog)

        assert empty[1]["content"] == sourced[1]["content"]
        assert empty[-2]["content"] != sourced[-2]["content"]


def _layout_messages() -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "stable skill"},
        {"role": "user", "content": CATALOG_CONTEXT_PREFIX + "\n{}"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": STATE_CONTEXT_PREFIX + "\n{}"},
        {"role": "user", "content": "new question"},
    ]


class TestCatalogAndTailMarkers:
    def test_catalog_message_receives_cache_control(self) -> None:
        marked, _ = apply_anthropic_cache_markers(_layout_messages(), None)

        assert marked[1]["cache_control"] == {"type": "ephemeral"}

    def test_plain_second_message_is_not_marked(self) -> None:
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "plain question"},
        ]

        marked, _ = apply_anthropic_cache_markers(messages, None)

        assert "cache_control" not in marked[1]

    def test_history_tail_marker_is_opt_in(self) -> None:
        default_marked, _ = apply_anthropic_cache_markers(_layout_messages(), None)
        assert "cache_control" not in default_marked[-1]

        tail_marked, _ = apply_anthropic_cache_markers(_layout_messages(), None, mark_history_tail=True)
        assert tail_marked[-1]["cache_control"] == {"type": "ephemeral"}

    def test_marker_budget_is_at_most_four_with_tools(self) -> None:
        tools = [
            {"type": "function", "function": {"name": "a", "parameters": {}}},
            {"type": "function", "function": {"name": "b", "parameters": {}}},
        ]

        marked, marked_tools = apply_anthropic_cache_markers(_layout_messages(), tools, mark_history_tail=True)

        assert marked_tools is not None
        message_marker_indexes = [index for index, message in enumerate(marked) if "cache_control" in message]
        tool_marker_count = sum(1 for tool in marked_tools if "cache_control" in tool)
        assert message_marker_indexes == [0, 1, len(marked) - 1]
        assert len(message_marker_indexes) + tool_marker_count <= 4

    def test_inputs_are_not_mutated(self) -> None:
        messages = _layout_messages()

        apply_anthropic_cache_markers(messages, None, mark_history_tail=True)

        assert all("cache_control" not in message for message in messages)
