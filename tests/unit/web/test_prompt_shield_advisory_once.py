"""The prompt-shield advisory is said once per distinct text, naming every node.

``CompositionState.validate()`` used to emit one near-identical advisory per
unshielded LLM node: 13,337 bytes on a 25-LLM chain, riding every tool result
and the turn-start state message. The validator now emits one entry per
distinct advisory (lead, producer set, draft) whose message names EVERY
affected node id — no cap — while a group of one node keeps today's exact
``node:<id>`` entry.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata, ValidationEntry
from elspeth.web.composer.tools import _compute_validation_delta
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    PROMPT_SHIELD_LOCAL_CONTENT_WARNING_DRAFT,
    PROMPT_SHIELD_USER_TERM,
    PROMPT_SHIELD_WARNING_DRAFT,
    prompt_shield_recommendation_warning_pairs,
)


def _node(node_id: str, plugin: str, *, input_stream: str, on_success: str, options: dict[str, object] | None = None) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin=plugin,
        input=input_stream,
        on_success=on_success,
        on_error="stop",
        options=options if options is not None else {"prompt_template": "Classify {{ row.content }}."},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _state(nodes: tuple[NodeSpec, ...]) -> CompositionState:
    return CompositionState(source=None, nodes=nodes, edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def _chain_nodes(n: int) -> tuple[NodeSpec, ...]:
    """``n`` LLM nodes ``llm_001``..., each consuming the previous node's output."""
    return tuple(
        _node(
            f"llm_{i:03d}",
            "llm",
            input_stream="rows" if i == 1 else f"s_{i - 1:03d}",
            on_success=f"s_{i:03d}",
        )
        for i in range(1, n + 1)
    )


def _llm_chain(n: int) -> CompositionState:
    return _state(_chain_nodes(n))


def _web_scrape(node_id: str, *, input_stream: str, on_success: str) -> NodeSpec:
    return _node(
        node_id,
        "web_scrape",
        input_stream=input_stream,
        on_success=on_success,
        options={"url_field": "url", "content_field": "content"},
    )


def _shield_warnings(state: CompositionState) -> list[ValidationEntry]:
    return [w for w in state.validate().warnings if "prompt-injection shield" in w.message]


def _size(entries: list[ValidationEntry]) -> int:
    return len(json.dumps([e.to_dict() for e in entries]))


def _named(ids: list[str]) -> str:
    return ", ".join(repr(node_id) for node_id in ids)


def test_shield_advisory_is_one_entry_naming_every_node_of_a_group() -> None:
    # llm_001 has no producer (local lead); llm_002.. are each fed by an llm
    # (untrusted producer "llm"): one entry for the first, ONE for the rest.
    first, group = _shield_warnings(_llm_chain(4))
    assert first.component == "node:llm_001"
    assert group.component == "pipeline"
    assert group.message.startswith(f"LLM nodes {_named(['llm_002', 'llm_003', 'llm_004'])} consume ")
    assert "produced by llm without an authorized prompt-injection shield between them. " in group.message
    assert group.message.endswith(PROMPT_SHIELD_WARNING_DRAFT)
    assert group.severity == "medium"


def test_local_content_group_names_every_node() -> None:
    state = _state(tuple(_node(f"rate_{i}", "llm", input_stream=f"rows_{i}", on_success=f"out_{i}") for i in range(3)))
    (entry,) = _shield_warnings(state)
    assert entry.component == "pipeline"
    assert entry.message == (
        f"LLM nodes {_named(['rate_0', 'rate_1', 'rate_2'])} have no authorized prompt-injection shield in front of them. "
        f"{PROMPT_SHIELD_LOCAL_CONTENT_WARNING_DRAFT}"
    )


def test_every_affected_node_is_named_at_3_and_30() -> None:
    for n in (3, 30):
        entries = _shield_warnings(_llm_chain(n))
        for i in range(1, n + 1):
            node_id = f"llm_{i:03d}"
            assert sum(repr(node_id) in e.message for e in entries) == 1, (n, node_id)
        assert all(" more" not in e.message.split(" consume ")[0] for e in entries)  # no cap


def test_shield_advisory_grows_only_by_the_node_id_per_llm_node() -> None:
    small = _shield_warnings(_llm_chain(6))
    large = _shield_warnings(_llm_chain(60))
    assert len(large) == len(small) == 2
    per_node = len("'llm_060', ")  # the quoted id and its separator
    assert _size(large) - _size(small) == 54 * per_node


def test_nodes_with_different_producers_are_not_merged() -> None:
    # scrape -> llm_a, llm_b (producer web_scrape); llm_a -> llm_c, llm_b -> llm_d (producer llm)
    state = _state(
        (
            _web_scrape("scrape", input_stream="rows", on_success="scraped"),
            _node("llm_a", "llm", input_stream="scraped", on_success="a_out"),
            _node("llm_b", "llm", input_stream="scraped", on_success="b_out"),
            _node("llm_c", "llm", input_stream="a_out", on_success="c_out"),
            _node("llm_d", "llm", input_stream="b_out", on_success="d_out"),
        )
    )
    entries = _shield_warnings(state)
    assert [e.component for e in entries] == ["pipeline", "pipeline"]
    by_producer = {e.message.split(" produced by ")[1].split(" without ")[0]: e.message for e in entries}
    assert set(by_producer) == {"web_scrape", "llm"}
    assert by_producer["web_scrape"].startswith(f"LLM nodes {_named(['llm_a', 'llm_b'])} consume ")
    assert by_producer["llm"].startswith(f"LLM nodes {_named(['llm_c', 'llm_d'])} consume ")


@pytest.mark.parametrize(
    ("status", "event_id", "accepted_value"),
    [
        # A staged review and one a rewire re-opened are both stored pending.
        ("pending", None, None),
        # An approved review: the node stays out of the advisory too.
        ("resolved", "evt-shield-llm_003", PROMPT_SHIELD_WARNING_DRAFT),
    ],
    ids=["pending-staged-or-reopened", "resolved"],
)
def test_a_node_with_a_staged_shield_review_is_left_out_of_the_group(status: str, event_id: str | None, accepted_value: str | None) -> None:
    staged = {
        "prompt_template": "Classify {{ row.content }}.",
        INTERPRETATION_REQUIREMENTS_KEY: [
            {
                "id": "prompt_injection_shield_review:llm_003",
                "kind": "pipeline_decision",
                "user_term": PROMPT_SHIELD_USER_TERM,
                "status": status,
                "draft": PROMPT_SHIELD_WARNING_DRAFT,
                "event_id": event_id,
                "accepted_value": accepted_value,
                "accepted_artifact_hash": None,
                "resolved_prompt_template_hash": None,
            }
        ],
    }
    nodes = tuple(
        _node(node.id, "llm", input_stream=node.input or "", on_success=node.on_success or "", options=staged)
        if node.id == "llm_003"
        else node
        for node in _chain_nodes(5)
    )
    _first, group = _shield_warnings(_state(nodes))
    assert group.message.startswith(f"LLM nodes {_named(['llm_002', 'llm_004', 'llm_005'])} consume ")
    assert "'llm_003'" not in group.message


def test_order_is_deterministic_under_node_insertion_order() -> None:
    forward = _chain_nodes(8)
    shuffled = forward[3:] + forward[:3][::-1]
    assert [e.to_dict() for e in _shield_warnings(_state(forward))] == [e.to_dict() for e in _shield_warnings(_state(shuffled))]


def test_delta_after_adding_one_unshielded_llm_is_one_new_and_one_resolved() -> None:
    delta = _compute_validation_delta(_llm_chain(25).validate(), _llm_chain(26).validate())
    new = [w for w in delta["new_warnings"] if "prompt-injection shield" in w["message"]]
    resolved = [w for w in delta["resolved_warnings"] if "prompt-injection shield" in w["message"]]
    assert len(new) == 1 and len(resolved) == 1
    assert "'llm_026'" in new[0]["message"]
    assert "'llm_026'" not in resolved[0]["message"]


# --- Controls: green on base -------------------------------------------------

# sha256 of json.dumps(entry.to_dict(), sort_keys=True), measured on the base
# tree 69ad78343 before the change.
_BASE_SINGLE_LOCAL_SHA256 = "fc051569319a9ef78b7ae8cbda1a3218d4d03b8ddecf481a87e98e1d842fd313"
_BASE_SINGLE_UNTRUSTED_SHA256 = "e745128a2c096173d22eb418907c6ec075372fe19a55ee01dc071e0a7e040bd5"


def _entry_sha256(entry: ValidationEntry) -> str:
    return hashlib.sha256(json.dumps(entry.to_dict(), sort_keys=True).encode()).hexdigest()


def test_single_llm_graph_advisory_is_byte_identical_to_base() -> None:
    state = _llm_chain(1)
    (entry,) = _shield_warnings(state)
    ((component, message),) = prompt_shield_recommendation_warning_pairs(state)
    assert (entry.component, entry.message) == (component, message) == ("node:llm_001", message)
    assert _entry_sha256(entry) == _BASE_SINGLE_LOCAL_SHA256


def test_single_untrusted_llm_advisory_is_byte_identical_to_base() -> None:
    state = _state(
        (
            _web_scrape("scrape", input_stream="rows", on_success="scraped"),
            _node(
                "summarise", "llm", input_stream="scraped", on_success="out", options={"prompt_template": "Summarise {{ row.content }}."}
            ),
        )
    )
    (entry,) = _shield_warnings(state)
    assert entry.component == "node:summarise"
    assert _entry_sha256(entry) == _BASE_SINGLE_UNTRUSTED_SHA256


def test_per_node_pairs_function_is_unchanged() -> None:
    pairs = prompt_shield_recommendation_warning_pairs(_llm_chain(25))
    assert [component for component, _message in pairs] == [f"node:llm_{i:03d}" for i in range(1, 26)]
