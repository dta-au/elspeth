"""Only the ordinary Composer routes are registered for sessions."""

import ast
from pathlib import Path

import elspeth
from elspeth.web.sessions.routes import create_session_router
from elspeth.web.sessions.routes.composer import pipeline_settlement


def test_session_router_registers_freeform_but_no_guided_authoring_routes() -> None:
    paths = {route.path for route in create_session_router().routes}

    assert "/api/sessions/{session_id}/messages" in paths
    assert "/api/sessions/{session_id}/guided" not in paths
    assert not any("/guided/" in path for path in paths)


def test_freeform_routes_do_not_pass_mode_transition_to_planner() -> None:
    root = Path(elspeth.__file__).parent / "web" / "sessions"
    planner_keywords: set[str] = set()
    for relative in ("routes/messages.py", "routes/composer/compose.py", "composer_turn.py"):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "compose":
                planner_keywords.update(keyword.arg for keyword in node.keywords if keyword.arg is not None)

    assert "session_id" in planner_keywords
    assert "guided_terminal" not in planner_keywords


def test_proposal_routes_use_one_freeform_settlement_contract() -> None:
    root = Path(elspeth.__file__).parent / "web" / "sessions" / "routes" / "composer"
    keywords: set[str] = set()
    for relative in ("pipeline_settlement.py", "proposals.py"):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                keywords.update(keyword.arg for keyword in node.keywords if keyword.arg is not None)

    assert "draft_hash" in keywords
    assert "reviewed_facts" not in keywords
    assert "settlement_surface" not in keywords
    assert "transition_assistant" not in keywords


def test_pipeline_settlement_has_no_guided_only_cancellation_path() -> None:
    members = vars(pipeline_settlement)
    assert "_await_with_deferred_cancellation" in members
    assert "_await_guided_atomic_settlement" not in members
