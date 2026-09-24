"""Start an isolated real service for the synthetic Composer acceptance battery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dotenv import dotenv_values
from pydantic import SecretBytes

from scripts.composer_acceptance.budget import ProviderBudget, observe_provider_requests

if TYPE_CHECKING:
    from elspeth.web.config import WebSettings

ACCEPTANCE_PLUGIN_ALLOWLIST = (
    "transform:batch_top_k",
    "transform:json_explode",
    "transform:keyword_filter",
    "transform:truncate",
    "transform:type_coerce",
    "transform:value_transform",
)


def check_fixture_availability(settings: WebSettings, scenarios: Path) -> None:
    """Fail before provider calls if the fixture requests disabled plugins."""
    from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
    from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
    from elspeth.web.plugin_policy.models import PluginId
    from elspeth.web.plugin_policy.profiles import RuntimeWebPluginConfig

    policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=RuntimeWebPluginConfig.from_settings(settings))
    needed = {
        PluginId.parse(f"{category}:{name}")
        for path in scenarios.glob("*.json")
        for category, names in json.loads(path.read_text())["expected_plugins"].items()
        for name in names
    }
    if not needed:
        raise ValueError("Acceptance fixtures declare no plugins")
    missing = sorted(map(str, needed - policy.authorized))
    if missing:
        raise ValueError("Acceptance fixture plugins are disabled: " + ", ".join(missing))


def load_authorized_key(path: Path) -> None:
    selected = []
    with path.open() as stream:
        for line in stream:
            candidate = line.lstrip()
            if candidate.startswith("export "):
                candidate = candidate[7:].lstrip()
            if candidate.split("=", 1)[0].strip() == "OPENROUTER_API_KEY":
                selected.append(candidate)
    key = dotenv_values(stream=StringIO("\n".join(selected)), interpolate=False).get("OPENROUTER_API_KEY")
    if not key:
        raise ValueError("Authorized env file has no OPENROUTER_API_KEY")
    os.environ["OPENROUTER_API_KEY"] = key
    for name in ("OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENROUTER_API_BASE", "AZURE_API_VERSION"):
        os.environ.pop(name, None)


def fingerprint(root: Path) -> dict[str, Any]:
    paths = subprocess.check_output(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            "src",
            "elspeth-lints/src",
            "scripts/composer_acceptance",
            "tests/fixtures/composer_convergence",
            "pyproject.toml",
            "uv.lock",
        ]
    ).split(b"\0")
    files = {}
    for encoded in paths:
        if encoded:
            name = os.fsdecode(encoded)
            path = root / name
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "MISSING"
    return {
        "head": subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip(),
        "content_sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
        "files": files,
    }


def validate_resume(output: Path, config: dict[str, Any], current: dict[str, Any]) -> None:
    source_path = output / "source-before.json"
    if source_path.exists():
        previous = json.loads(source_path.read_text())
        if previous["content_sha256"] != current["content_sha256"]:
            raise ValueError("Production or acceptance sources changed; choose a fresh output directory")
    config_path = output / "service-config.json"
    if config_path.exists() and json.loads(config_path.read_text()) != config:
        raise ValueError("Acceptance configuration changed; choose a fresh output directory")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Start real provider-backed service; otherwise print settings only")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--port", type=int, default=18473)
    parser.add_argument("--planner", default="openrouter/deepseek/deepseek-v4.1-flash")
    parser.add_argument("--advisor", default="openrouter/z-ai/glm-5.3")
    parser.add_argument("--runtime-model", default="anthropic/claude-sonnet-4.6")
    parser.add_argument("--max-provider-calls", type=int, default=200)
    parser.add_argument("--max-known-cost", type=float, default=10.0)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    config = {
        "root": str(root),
        "data_dir": str(output / "data"),
        "planner": args.planner,
        "advisor": args.advisor,
        "runtime_model": args.runtime_model,
        "plugin_allowlist": list(ACCEPTANCE_PLUGIN_ALLOWLIST),
        "max_provider_calls": args.max_provider_calls,
        "max_known_cost": args.max_known_cost,
        "base_url": f"http://127.0.0.1:{args.port}",
        "execute": args.execute,
    }
    print(json.dumps(config), flush=True)
    if not args.execute:
        return
    if args.env_file is None:
        parser.error("--execute requires --env-file; only OPENROUTER_API_KEY is read")
    validate_resume(output, config, fingerprint(root))
    load_authorized_key(args.env_file)
    output.mkdir(parents=True, exist_ok=True)
    secret_path = output / "local-auth-secret.txt"
    if not secret_path.exists():
        secret_path.write_text(secrets.token_urlsafe(48))
        secret_path.chmod(0o600)
    import uvicorn

    from elspeth.web.app import create_app
    from elspeth.web.config import WebSettings

    settings = WebSettings(
        host="127.0.0.1",
        port=args.port,
        data_dir=output / "data",
        composer_model=args.planner,
        composer_advisor_model=args.advisor,
        composer_max_composition_turns=12,
        composer_max_discovery_turns=6,
        composer_timeout_seconds=900,
        composer_transport_idle_ceiling_seconds=960,
        composer_planner_max_provider_calls=20,
        composer_planner_max_cumulative_provider_cost=str(args.max_known_cost),
        composer_rate_limit_per_minute=100,
        composer_strict_tools="preferred",
        plugin_allowlist=ACCEPTANCE_PLUGIN_ALLOWLIST,
        secret_key=secret_path.read_text(),
        shareable_link_signing_key=SecretBytes(hashlib.sha256(secret_path.read_bytes()).digest()),
        server_secret_allowlist=("OPENROUTER_API_KEY",),
        user_secrets_enabled=False,
        llm_profiles={
            "sonnet": {
                "provider": "openrouter",
                "model": args.runtime_model,
                "credential_scope": "server",
                "credential_ref": "OPENROUTER_API_KEY",
            }
        },
        default_llm_profile="sonnet",
        secret_wiring_allowlist=(
            {"secret": "OPENROUTER_API_KEY", "component_type": "transform", "plugin": "llm", "option_key": "api_key"},
        ),
        registration_mode="open",
    )
    check_fixture_availability(settings, root / "tests/fixtures/composer_convergence")
    before = fingerprint(root)
    (output / "source-before.json").write_text(json.dumps(before, indent=2) + "\n")
    (output / "service-config.json").write_text(json.dumps(config, indent=2) + "\n")
    budget = ProviderBudget(output / "provider-ledger.jsonl", args.max_provider_calls, args.max_known_cost)
    try:
        with observe_provider_requests(budget):
            uvicorn.run(create_app(settings=settings), host="127.0.0.1", port=args.port, log_level="info")
    finally:
        after = fingerprint(root)
        (output / "source-after.json").write_text(json.dumps(after, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "frozen": before["content_sha256"] == after["content_sha256"],
                    "provider_calls": budget.calls,
                    "known_cost": budget.cost,
                    "unpriced_calls": budget.unpriced,
                }
            ),
            flush=True,
        )
        if before["content_sha256"] != after["content_sha256"]:
            raise RuntimeError("Source changed during acceptance; results are not valid")


if __name__ == "__main__":
    main()
