"""Actual shipped React chat/hook against the local TLS operation authority."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from starlette.responses import Response
from starlette.routing import Route

from elspeth.web.auth.local import LocalAuthProvider
from elspeth.web.auth.middleware import get_current_user
from tests.fixtures.identities import grant_test_pipeline_user
from tests.helpers.composer_https import local_composer_tls
from tests.helpers.composer_operations import build_composer_operation_app


@pytest.mark.parametrize("scenario", ["session-late-terminal", "principal-late-admission"])
def test_actual_react_chat_rejects_prior_actor_publication(tmp_path: Path, scenario: str) -> None:
    root = Path(__file__).parents[3]
    frontend = root / "src/elspeth/web/frontend"
    support = Path(__file__).with_name("composer_react_acceptance_support")
    harness = asyncio.run(build_composer_operation_app(tmp_path / "application", timeout_seconds=60))

    async def second_actor():
        if scenario == "session-late-terminal":
            second = await harness.app.state.session_service.create_session("transport-user", "React current winner", "local")
            return harness.token, "transport-user", str(second.id)
        provider = harness.app.state.auth_provider
        assert isinstance(provider, LocalAuthProvider)
        provider.create_user("react-winner", "owned offline React test password", "React Winner")
        token = await provider.login("react-winner", "owned offline React test password")
        user = await provider.authenticate(token)
        with harness.app.state.session_engine.begin() as connection:
            grant_test_pipeline_user(connection, identity_id=user.user_id)
        session = await harness.app.state.session_service.create_session(user.user_id, "React current principal", "local")
        return token, user.user_id, str(session.id)

    token_b, principal_b, session_b = asyncio.run(second_actor())
    assert harness.app.state.settings.composer_boot_probe_enabled is False
    bundled = tmp_path / "react.js"
    build = subprocess.run(
        [
            str(frontend / "node_modules/esbuild/bin/esbuild"),
            str(support / "react-entry.tsx"),
            "--bundle",
            "--format=iife",
            "--platform=browser",
            f"--alias:@={frontend}/src",
            f"--alias:@/components/chat/pipelineGloss={frontend}/src/components/chat/pipelineGloss.ts",
            f"--outfile={bundled}",
            f"--alias:react={frontend}/node_modules/react",
            f"--alias:react-dom={frontend}/node_modules/react-dom",
            f"--tsconfig={frontend}/tsconfig.app.json",
        ],
        cwd=frontend,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert build.returncode == 0, build.stderr

    async def script(_request):
        return Response(bundled.read_bytes(), media_type="application/javascript")

    async def release(request):
        user = await get_current_user(request)
        assert user.user_id in ("transport-user", principal_b)
        harness.composer.release.set()
        return Response(status_code=204)

    harness.app.router.routes.insert(0, Route("/__acceptance/react.js", script))
    harness.app.router.routes.insert(0, Route("/__acceptance/react-control/release", release, methods=["POST"]))
    with local_composer_tls(harness.app, tmp_path / "proxy") as tls:
        try:
            result = subprocess.run(
                ["node", "--input-type=module", "-e", (support / "react-browser.mjs").read_text()],
                cwd=frontend,
                input=json.dumps(
                    {
                        "scenario": scenario,
                        "baseUrl": tls.base_url,
                        "tokenA": harness.token,
                        "tokenB": token_b,
                        "sessionA": str(harness.session_id),
                        "sessionB": session_b,
                        "principalB": principal_b,
                    }
                ),
                capture_output=True,
                text=True,
                timeout=55,
                check=False,
            )
            assert result.returncode == 0, result.stderr
            evidence = json.loads(result.stdout)
            assert evidence["after"]["activeSessionId"] == session_b
            assert evidence["after"]["principalId"] == principal_b
            assert harness.composer.calls == 2
            # Both exact jobs remain queryable under their real principals.
            first_id = evidence["first"]["held"]["operationId"]
            after_posts = [r for r in evidence["after"]["requests"] if r["method"] == "POST" and r["path"].endswith("/messages")]
            assert len(after_posts) == 2 and after_posts[0]["operationId"] == first_id
            deadline = time.monotonic() + 10
            for token, sid, oid in [
                (harness.token, str(harness.session_id), first_id),
                (token_b, session_b, after_posts[1]["operationId"]),
            ]:
                with httpx.Client(
                    base_url=tls.base_url, verify=tls.verify, headers={"Authorization": f"Bearer {token}"}, timeout=3
                ) as client:
                    while True:
                        terminal = client.get(f"/api/sessions/{sid}/operations/{oid}")
                        assert terminal.status_code == 200
                        if terminal.json()["status"] in ("completed", "failed"):
                            break
                        assert time.monotonic() < deadline
                        time.sleep(0.02)
                    assert terminal.json()["status"] == "completed", terminal.text
            (tmp_path / "react-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
        finally:
            harness.composer.release.set()
