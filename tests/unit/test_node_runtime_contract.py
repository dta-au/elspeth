"""Cross-surface Node.js and npm runtime contract.

The project builds JavaScript in local development, CI, Docker, and the
source-checkout deployment runbooks.  Keep those surfaces on one supported
major line so a green local build cannot hide an older production toolchain.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

NODE_VERSION = "24.13.0"
NODE_ENGINE = ">=24 <25"
NPM_ENGINE = ">=11 <12"
PACKAGE_MANAGER = "npm@11.6.2"
SETUP_NODE_REVISION = "820762786026740c76f36085b0efc47a31fe5020"
IMAGE_NODE_VERSION = "24.18.0"
IMAGE_NPM_VERSION = "11.6.2"
IMAGE_NODE_BASE = "node:24.18.0-bookworm-slim@sha256:6f7b03f7c2c8e2e784dcf9295400527b9b1270fd37b7e9a7285cf83b6951452d"
OLD_IMAGE_NODE_BASE = "node:24.13.0-bookworm-slim@sha256:4660b1ca8b28d6d1906fd644abe34b2ed81d15434d26d845ef0aced307cf4b6f"


def _json(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_manifests_and_locks_publish_the_node_24_contract() -> None:
    version_file = REPO_ROOT / ".node-version"
    assert version_file.is_file(), "repository must publish a root .node-version"
    assert version_file.read_text(encoding="utf-8").strip() == NODE_VERSION

    pairs = (
        (REPO_ROOT / "package.json", REPO_ROOT / "package-lock.json"),
        (
            REPO_ROOT / "src/elspeth/web/frontend/package.json",
            REPO_ROOT / "src/elspeth/web/frontend/package-lock.json",
        ),
    )
    expected_engines = {"node": NODE_ENGINE, "npm": NPM_ENGINE}
    for manifest_path, lock_path in pairs:
        manifest = _json(manifest_path)
        lock_root = _json(lock_path)["packages"][""]
        assert manifest["engines"] == expected_engines
        assert manifest["packageManager"] == PACKAGE_MANAGER
        assert lock_root["engines"] == expected_engines


def test_frontend_types_track_the_node_24_line() -> None:
    manifest = _json(REPO_ROOT / "src/elspeth/web/frontend/package.json")
    lock = _json(REPO_ROOT / "src/elspeth/web/frontend/package-lock.json")

    assert manifest["devDependencies"]["@types/node"].startswith("^24.")
    assert lock["packages"][""]["devDependencies"]["@types/node"].startswith("^24.")
    assert lock["packages"]["node_modules/@types/node"]["version"].startswith("24.")


def test_ci_and_release_image_build_with_node_24() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yaml").read_text(encoding="utf-8"))
    setup_steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", ())
        if str(step.get("uses", "")).startswith("actions/setup-node@")
    ]

    assert len(setup_steps) == 2
    assert {step["uses"] for step in setup_steps} == {f"actions/setup-node@{SETUP_NODE_REVISION}"}
    assert {step["with"]["node-version"] for step in setup_steps} == {"24"}

    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f"FROM {IMAGE_NODE_BASE} AS frontend-builder" in dockerfile
    assert OLD_IMAGE_NODE_BASE not in dockerfile
    assert "FROM node:22" not in dockerfile


def test_release_image_installs_and_verifies_exact_node_and_npm_versions() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    npm_install = f"npm install --global npm@{IMAGE_NPM_VERSION}"
    node_check = f'test "$(node --version)" = "v{IMAGE_NODE_VERSION}"'
    npm_check = f'test "$(npm --version)" = "{IMAGE_NPM_VERSION}"'

    assert npm_install in dockerfile
    assert node_check in dockerfile
    assert npm_check in dockerfile
    assert dockerfile.index(npm_install) < dockerfile.index("RUN npm ci")


def test_active_deployment_runbooks_require_node_24() -> None:
    aws = (REPO_ROOT / "docs/runbooks/aws-ecs-deployment.md").read_text(encoding="utf-8")
    ansible = (REPO_ROOT / "docs/runbooks/ansible-ubuntu-deployment.md").read_text(encoding="utf-8")
    redeploy = (REPO_ROOT / "docs/runbooks/aws-ecs-existing-service-redeploy.md").read_text(encoding="utf-8")
    caddy = (REPO_ROOT / "docs/runbooks/caddy-development-refresh.md").read_text(encoding="utf-8")

    assert "Node 24/npm 11" in aws
    assert "Node 22/npm" not in aws

    assert "Node.js 24" in ansible
    assert "NodeSource Node 24.x" in ansible
    assert "https://deb.nodesource.com/node_24.x" in ansible
    assert "Node.js 20.19" not in ansible
    assert "NodeSource Node 20.x" not in ansible
    assert "https://deb.nodesource.com/node_20.x" not in ansible

    for text in (redeploy, caddy):
        assert "Node.js 24" in text
        assert "npm 11" in text
        assert "npm --prefix src/elspeth/web/frontend ci" in text


def test_source_checkout_install_docs_use_locked_toolchains() -> None:
    paths = (
        REPO_ROOT / "README.md",
        REPO_ROOT / "CONTRIBUTING.md",
        REPO_ROOT / "docs/guides/telemetry.md",
        REPO_ROOT / "docs/guides/tier2-tracing.md",
        REPO_ROOT / "docs/guides/troubleshooting.md",
        REPO_ROOT / "docs/guides/your-first-pipeline.md",
        REPO_ROOT / "docs/guides/user-manual.md",
        REPO_ROOT / "docs/guides/landscape-mcp-analysis.md",
        REPO_ROOT / "docs/reference/web-scrape-transform.md",
    )

    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "Python 3.11+" not in text, path
        assert "\nnpm install\n" not in text, path
        assert "uv pip install -e" not in text, path
        assert "uv pip install elspeth[" not in text, path
        assert "uv pip install ddtrace" not in text, path

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    first_pipeline = (REPO_ROOT / "docs/guides/your-first-pipeline.md").read_text(encoding="utf-8")
    user_manual = (REPO_ROOT / "docs/guides/user-manual.md").read_text(encoding="utf-8")
    landscape_mcp = (REPO_ROOT / "docs/guides/landscape-mcp-analysis.md").read_text(encoding="utf-8")
    web_scrape = (REPO_ROOT / "docs/reference/web-scrape-transform.md").read_text(encoding="utf-8")

    assert "Node.js 24 and npm 11" in readme
    assert "npm --prefix src/elspeth/web/frontend ci" in readme
    assert "Node.js 24, and npm 11" in contributing
    assert "npm --prefix src/elspeth/web/frontend ci" in contributing
    assert "Python 3.12+" in first_pipeline
    assert "Node.js 24, npm 11" in first_pipeline
    assert "npm --prefix src/elspeth/web/frontend ci" in first_pipeline
    assert "uv sync --frozen --all-extras" in user_manual
    assert "uv sync --frozen --extra mcp" in landscape_mcp
    assert "there is no separate `web` extra" in web_scrape
    assert ".[web]" not in web_scrape
