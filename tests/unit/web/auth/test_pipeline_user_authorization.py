"""The Web workload boundary requires a live, unscoped human ``user`` grant."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import sleep
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from pydantic import SecretBytes
from sqlalchemy import Connection, event, insert, update

from elspeth.web.auth.middleware import require_pipeline_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.config import WebSettings
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.sessions.models import identities_table, identity_roles_table
from tests.fixtures.identities import ensure_test_identity

_PIPELINE_USER_ROUTES: dict[str, frozenset[str]] = {
    "src/elspeth/web/sessions/routes/sessions.py": frozenset(
        {
            "create_session",
            "list_sessions",
            "list_active_composer_requests",
            "get_session",
            "update_session",
            "delete_session",
            "fork_from_message",
        }
    ),
    "src/elspeth/web/sessions/routes/runs.py": frozenset({"list_session_runs", "get_run_audit_story"}),
    "src/elspeth/web/sessions/routes/messages.py": frozenset({"send_message", "get_messages"}),
    "src/elspeth/web/sessions/routes/interpretation.py": frozenset(
        {"resolve_interpretation", "list_interpretations", "opt_out_of_interpretations", "opt_out_summary"}
    ),
    "src/elspeth/web/sessions/routes/composer/compose.py": frozenset({"recompose"}),
    "src/elspeth/web/sessions/routes/composer/state.py": frozenset(
        {
            "get_composer_progress",
            "get_composer_preferences",
            "update_composer_preferences",
            "get_current_state",
            "get_state_versions",
            "revert_state",
            "import_state_yaml",
            "seed_state_for_e2e",
            "get_state_yaml",
        }
    ),
    "src/elspeth/web/sessions/routes/composer/proposals.py": frozenset(
        {
            "list_composition_proposals",
            "list_proposal_events",
            "accept_composition_proposal",
            "reject_composition_proposal",
        }
    ),
    "src/elspeth/web/execution/routes.py": frozenset(
        {
            "validate_session_pipeline",
            "execute_pipeline",
            "get_run_status",
            "get_run_diagnostics",
            "evaluate_run_diagnostics",
            "cancel_run",
            "get_run_results",
            "get_run_outputs",
            "get_run_output_content",
            "get_run_output_preview",
            "create_run_websocket_ticket",
        }
    ),
    "src/elspeth/web/blobs/routes.py": frozenset(
        {
            "create_blob_upload",
            "create_blob_inline",
            "list_blobs",
            "get_blob_metadata",
            "download_blob_content",
            "preview_blob_content",
            "delete_blob",
        }
    ),
    "src/elspeth/web/secrets/routes.py": frozenset({"list_secrets", "create_secret", "delete_secret", "validate_secret"}),
    "src/elspeth/web/preferences/routes.py": frozenset({"get_preferences", "update_preferences"}),
    "src/elspeth/web/shareable_reviews/routes.py": frozenset({"mark_ready_for_review", "get_shareable_link"}),
    "src/elspeth/web/composer/tutorial_run_routes.py": frozenset(
        {"get_tutorial_sample", "get_readiness", "run_tutorial", "cancel_tutorial", "delete_tutorial_orphans"}
    ),
    "src/elspeth/web/composer/tutorial_abandon_routes.py": frozenset({"abandon_tutorial"}),
    "src/elspeth/web/sessions/routes/workflow/approvals.py": frozenset({"request_approval", "withdraw"}),
    "src/elspeth/web/sessions/routes/workflow/reviews.py": frozenset({"request_review", "cancel_review"}),
    "src/elspeth/web/sessions/routes/workflow/library.py": frozenset({"publish_entry", "fork_entry"}),
}

# Every other registered route in a scanned module is deliberately classified.
# These are governance/capability surfaces with their own authorization, not
# owner-workspace author/run authority.
_ROUTE_EXCEPTIONS: dict[str, frozenset[str]] = {
    "src/elspeth/web/execution/routes.py": frozenset(),
    "src/elspeth/web/shareable_reviews/routes.py": frozenset({"get_shared_inspect"}),
    "src/elspeth/web/sessions/routes/workflow/approvals.py": frozenset({"inbox", "sent", "decide"}),
    "src/elspeth/web/sessions/routes/workflow/reviews.py": frozenset({"review_inbox", "attest_review"}),
    "src/elspeth/web/sessions/routes/workflow/library.py": frozenset(
        {"list_entries", "accept_entry", "reject_entry", "deprecate_entry", "recall_entry"}
    ),
}

_INLINE_USER_ROLE_ROUTES: dict[str, frozenset[str]] = {
    "src/elspeth/web/execution/routes.py": frozenset({"websocket_run_progress"}),
}


def _user_dependency(function: ast.AsyncFunctionDef) -> str:
    for argument in (*function.args.args, *function.args.kwonlyargs):
        if argument.arg in {"user", "_user"} and argument.annotation is not None:
            return ast.unparse(argument.annotation)
    return ""


def _decorated_route_functions(tree: ast.AST) -> dict[str, ast.AsyncFunctionDef]:
    methods = {"get", "post", "put", "patch", "delete", "websocket"}
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and any(
            isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and decorator.func.attr in methods
            for decorator in node.decorator_list
        )
    }


def test_every_scanned_route_is_classified_and_owner_workspaces_require_live_user() -> None:
    """New routes cannot silently bypass the live workload-role boundary."""
    root = Path(__file__).resolve().parents[4]
    measured: dict[str, frozenset[str]] = {}
    scanned_paths = set(_PIPELINE_USER_ROUTES) | set(_ROUTE_EXCEPTIONS) | set(_INLINE_USER_ROLE_ROUTES)
    for relative_path in scanned_paths:
        tree = ast.parse((root / relative_path).read_text())
        functions = _decorated_route_functions(tree)
        workload_names = _PIPELINE_USER_ROUTES.get(relative_path, frozenset())
        exception_names = _ROUTE_EXCEPTIONS.get(relative_path, frozenset())
        inline_names = _INLINE_USER_ROLE_ROUTES.get(relative_path, frozenset())
        classified = workload_names | exception_names | inline_names
        assert frozenset(functions) == classified
        assert not (workload_names & exception_names)
        assert not (workload_names & inline_names)
        measured[relative_path] = frozenset(functions)
        for function_name in workload_names:
            function = functions[function_name]
            dependency = _user_dependency(function)
            assert "Depends(require_pipeline_user)" in dependency or "Depends(_governed_pipeline_user)" in dependency
    assert set(measured) == scanned_paths

    execution_tree = ast.parse((root / "src/elspeth/web/execution/routes.py").read_text())
    websocket = _decorated_route_functions(execution_tree)["websocket_run_progress"]
    authority_members = {node.attr for node in ast.walk(websocket) if isinstance(node, ast.Attribute)}
    assert {"consume", "holds_active_human_role"} <= authority_members

    library_tree = ast.parse((root / "src/elspeth/web/sessions/routes/workflow/library.py").read_text())
    governed = next(
        node for node in ast.walk(library_tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "_governed_pipeline_user"
    )
    assert "Depends(require_pipeline_user)" in _user_dependency(governed)


def _grant(conn, *, identity_id: str, role: str, scope: str | None = None, expires_at: datetime | None = None) -> None:
    conn.execute(
        insert(identity_roles_table).values(
            role_id=str(uuid4()),
            identity_id=identity_id,
            role=role,
            scope=scope,
            expires_at=expires_at,
            granted_by_identity_id=identity_id,
            granted_at=datetime.now(UTC),
        )
    )


@pytest.mark.asyncio
async def test_auditor_oversight_scoped_expired_and_service_principals_cannot_author(engine, tmp_path) -> None:
    authority = RepositoryIdentityAuthority(engine, lifecycle_effect=lambda _token, _event: None)
    with engine.begin() as conn:
        for identity_id in (
            "none",
            "admin",
            "approver",
            "reviewer",
            "curator",
            "auditor",
            "oversight",
            "scoped",
            "expired",
            "revoked",
            "user",
        ):
            ensure_test_identity(conn, identity_id=identity_id)
        _grant(conn, identity_id="admin", role="admin")
        _grant(conn, identity_id="approver", role="approver")
        _grant(conn, identity_id="reviewer", role="reviewer")
        _grant(conn, identity_id="curator", role="curator")
        _grant(conn, identity_id="auditor", role="auditor")
        _grant(conn, identity_id="oversight", role="oversight")
        _grant(conn, identity_id="scoped", role="user", scope="one-session")
        _grant(conn, identity_id="expired", role="user", expires_at=datetime.now(UTC) - timedelta(days=1))
        _grant(conn, identity_id="revoked", role="user")
        _grant(conn, identity_id="user", role="user")
        conn.execute(
            update(identity_roles_table).where(identity_roles_table.c.identity_id == "revoked").values(revoked_at=datetime.now(UTC))
        )
        conn.execute(
            insert(identities_table).values(
                identity_id="service",
                provider="service",
                kind="service",
                subject="service",
                username="service",
                first_seen_at=datetime.now(UTC),
                access_state="active",
                activated_at=datetime.now(UTC),
            )
        )
        _grant(conn, identity_id="service", role="oversight")

    app = FastAPI()
    app.state.settings = WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=SecretBytes(b"0" * 32),
    )
    app.state.identity_authority = authority
    request = Request({"type": "http", "app": app, "headers": []})

    for identity_id in (
        "none",
        "admin",
        "approver",
        "reviewer",
        "curator",
        "auditor",
        "oversight",
        "scoped",
        "expired",
        "revoked",
        "service",
    ):
        with pytest.raises(HTTPException) as caught:
            await require_pipeline_user(request, UserIdentity(user_id=identity_id, username=identity_id))
        assert caught.value.status_code == 403
        assert caught.value.detail["error_type"] == "user_role_required"

    admitted = await require_pipeline_user(request, UserIdentity(user_id="user", username="user"))
    assert admitted.user_id == "user"

    with engine.begin() as conn:
        conn.execute(update(identity_roles_table).where(identity_roles_table.c.identity_id == "user").values(revoked_at=datetime.now(UTC)))
    with pytest.raises(HTTPException) as caught:
        await require_pipeline_user(request, UserIdentity(user_id="user", username="user"))
    assert caught.value.status_code == 403


@pytest.mark.asyncio
async def test_pipeline_user_expiry_is_checked_after_role_retrieval(engine, tmp_path) -> None:
    authority = RepositoryIdentityAuthority(engine, lifecycle_effect=lambda _token, _event: None)
    with engine.begin() as conn:
        for identity_id in ("expires-during-read", "future-control"):
            ensure_test_identity(conn, identity_id=identity_id)
        now = database_now(conn)
        _grant(conn, identity_id="expires-during-read", role="user", expires_at=now + timedelta(seconds=2))
        _grant(conn, identity_id="future-control", role="user", expires_at=now + timedelta(seconds=60))

    app = FastAPI()
    app.state.settings = WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=SecretBytes(b"0" * 32),
    )
    app.state.identity_authority = authority
    request = Request({"type": "http", "app": app, "headers": []})
    role_reads = 0

    def pause_before_role_retrieval(
        _conn: Connection,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _execution_context: Any,
        _many: bool,
    ) -> None:
        nonlocal role_reads
        if statement.startswith("SELECT") and "FROM identity_roles" in statement:
            role_reads += 1
            sleep(2.2)

    event.listen(engine, "before_cursor_execute", pause_before_role_retrieval)
    try:
        with engine.connect() as conn:
            assert database_now(conn) < now + timedelta(seconds=2)
        with pytest.raises(HTTPException) as caught:
            await require_pipeline_user(
                request,
                UserIdentity(user_id="expires-during-read", username="expires-during-read"),
            )
        assert caught.value.status_code == 403
        assert caught.value.detail["error_type"] == "user_role_required"

        admitted = await require_pipeline_user(request, UserIdentity(user_id="future-control", username="future-control"))
        assert admitted.user_id == "future-control"
    finally:
        event.remove(engine, "before_cursor_execute", pause_before_role_retrieval)

    assert role_reads == 2
