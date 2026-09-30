"""Read-only resume admission for HTTP search side effects."""

from __future__ import annotations

from elspeth.contracts.checkpoint import ResumeRefusalCause
from elspeth.contracts.enums import NodeType
from elspeth.core.checkpoint.recovery import check_http_effects_resumable
from elspeth.core.dag import ExecutionGraph


def _graph_with_search_method(method: str | None) -> ExecutionGraph:
    graph = ExecutionGraph()
    config = {} if method is None else {"method": method}
    graph.add_node("search", node_type=NodeType.TRANSFORM, plugin_name="web_scrape", config=config)
    return graph


def test_post_search_refuses_automatic_resume_before_remote_effect_reconciliation() -> None:
    check = check_http_effects_resumable(_graph_with_search_method("POST"))
    assert not check.can_resume
    assert check.cause is ResumeRefusalCause.UNCERTAIN_REMOTE_EFFECT
    assert check.reason is not None
    assert "POST" in check.reason


def test_get_search_remains_admitted_for_resume() -> None:
    assert check_http_effects_resumable(_graph_with_search_method("GET")).can_resume
    assert check_http_effects_resumable(_graph_with_search_method(None)).can_resume
