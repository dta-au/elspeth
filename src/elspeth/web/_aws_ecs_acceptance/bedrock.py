"""Bedrock and guardrail acceptance lane."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

from elspeth.contracts import (
    CallStatus,
    CallType,
    Determinism,
    NodeStateStatus,
    NodeType,
    RunStatus,
    TerminalOutcome,
    TerminalPath,
)
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.config.runtime import RuntimeTelemetryConfig
from elspeth.contracts.errors import ExecutionError
from elspeth.contracts.plugin_capabilities import ControlMode, PluginCapability
from elspeth.contracts.plugin_policy_audit import WebPluginPolicyEvidence
from elspeth.contracts.schema import SchemaConfig
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.engine.orchestrator import prepare_for_run
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.plugins.transforms.aws.guardrails_live_check import run_guardrail_live_check
from elspeth.plugins.transforms.llm.model_catalog import read_openrouter_catalog_snapshot_id
from elspeth.telemetry import create_telemetry_manager
from elspeth.web.audit_readiness.service import build_plugin_policy_readiness
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.composer.service import _litellm_acompletion
from elspeth.web.config import settings_from_env
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution.service import _build_web_plugin_policy_evidence
from elspeth.web.operator_telemetry import build_aws_operator_pipeline_telemetry
from elspeth.web.plugin_policy.availability import build_plugin_snapshot
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.models import PluginId
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig

from .capture import (
    _canonical_tutorial_policy_state,
)
from .contracts import (
    _SHA256_PATTERN,
    FORBIDDEN_AWS_OVERRIDE_ENV,
    AcceptanceCheckError,
    _resolve_aws_region,
    _sha256,
    _utc_timestamp,
    plugin_policy_binding_sha256,
)

_BEDROCK_TIMEOUT_SECONDS = 60.0

_BEDROCK_PROMPT = "Reply with exactly: Bedrock smoke passed."

_GUARDRAIL_INPUTS = (
    (
        "aws_bedrock_prompt_shield",
        "ELSPETH_LIVE_BEDROCK_PROMPT_PROFILE_ALIAS",
        "ELSPETH_LIVE_BEDROCK_PROMPT_SAFE_TEXT",
        "ELSPETH_LIVE_BEDROCK_PROMPT_BLOCKED_TEXT",
        "ELSPETH_LIVE_BEDROCK_PROMPT_EXPECTED_VERSION",
    ),
    (
        "aws_bedrock_content_safety",
        "ELSPETH_LIVE_BEDROCK_CONTENT_PROFILE_ALIAS",
        "ELSPETH_LIVE_BEDROCK_CONTENT_SAFE_TEXT",
        "ELSPETH_LIVE_BEDROCK_CONTENT_BLOCKED_TEXT",
        "ELSPETH_LIVE_BEDROCK_CONTENT_EXPECTED_VERSION",
    ),
)


@contextlib.contextmanager
def _suppress_process_output() -> Iterator[None]:
    """Redirect process file descriptors 1 and 2 to a non-persistent sink."""

    saved_stdout: int | None = None
    saved_stderr: int | None = None
    null_fd: int | None = None

    def restore() -> None:
        if saved_stdout is not None:
            with contextlib.suppress(OSError):
                os.dup2(saved_stdout, 1)
        if saved_stderr is not None:
            with contextlib.suppress(OSError):
                os.dup2(saved_stderr, 2)
        for descriptor in (null_fd, saved_stdout, saved_stderr):
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)

    with contextlib.suppress(Exception):
        sys.stdout.flush()
    with contextlib.suppress(Exception):
        sys.stderr.flush()
    try:
        saved_stdout = os.dup(1)
        saved_stderr = os.dup(2)
        null_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null_fd, 1)
        os.dup2(null_fd, 2)
    except OSError:
        restore()
        raise AcceptanceCheckError("bedrock_output_boundary") from None
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            sys.stdout.flush()
        with contextlib.suppress(Exception):
            sys.stderr.flush()
        restore()


def _bedrock_content(response: Any) -> str:
    try:
        choices = response.get("choices") if isinstance(response, Mapping) else response.choices
        if not isinstance(choices, (list, tuple)) or not choices:
            raise AcceptanceCheckError("bedrock_content")
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, Mapping) else choice.message
        if message is None:
            raise AcceptanceCheckError("bedrock_content")
        content = message.get("content") if isinstance(message, Mapping) else message.content
    except AcceptanceCheckError:
        raise
    except Exception:
        raise AcceptanceCheckError("bedrock_content") from None
    if type(content) is not str or not content.strip():
        raise AcceptanceCheckError("bedrock_content")
    return content


def _bedrock_receipt_projection(
    response: Any,
    *,
    model: str,
    messages: list[dict[str, str]],
    started_at: datetime,
    started_ns: int,
) -> dict[str, object]:
    _bedrock_content(response)
    try:
        record = build_llm_call_record(
            model_requested=model,
            messages=messages,
            tools=None,
            status=ComposerLLMCallStatus.SUCCESS,
            started_at=started_at,
            started_ns=started_ns,
            temperature=None,
            seed=None,
            response=response,
        )
    except Exception:
        raise AcceptanceCheckError("bedrock_metadata") from None
    if record.model_returned is None or record.provider_request_id is None:
        raise AcceptanceCheckError("bedrock_metadata")
    cost_sources = {
        "not_available": "unavailable",
        "response_usage.cost": "provider_reported",
        "_hidden_params.response_cost": "litellm_calculated",
    }
    cost_source = cost_sources[record.provider_cost_source]
    return {
        "returned_model_sha256": _sha256(record.model_returned.encode("utf-8")),
        "provider_request_id_sha256": _sha256(record.provider_request_id.encode("utf-8")),
        "prompt_tokens_present": record.prompt_tokens is not None,
        "completion_tokens_present": record.completion_tokens is not None,
        "cache_tokens_present": any(
            count is not None for count in (record.cached_prompt_tokens, record.cache_creation_input_tokens, record.cache_read_input_tokens)
        ),
        "cost": record.provider_cost,
        "cost_source": cost_source,
    }


async def verify_bedrock(
    env: Mapping[str, str],
    *,
    completion: Callable[..., Awaitable[Any]] = _litellm_acompletion,
) -> dict[str, object]:
    """Call Bedrock through the production composer boundary under FD suppression."""

    if any(name in env for name in FORBIDDEN_AWS_OVERRIDE_ENV):
        raise AcceptanceCheckError("bedrock_aws_override")
    model = env.get("ELSPETH_BEDROCK_LIVE_TEST_MODEL")
    if (
        type(model) is not str
        or not model.startswith("bedrock/")
        or len(model) > 512
        or not model.removeprefix("bedrock/")
        or any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in model)
    ):
        raise AcceptanceCheckError("bedrock_input")
    region = _resolve_aws_region(env, check="bedrock_input")
    messages = [{"role": "user", "content": _BEDROCK_PROMPT}]
    try:
        with _suppress_process_output():
            started_at = datetime.now(UTC)
            started_ns = time.monotonic_ns()
            response = await asyncio.wait_for(
                completion(
                    model=model,
                    messages=messages,
                    max_tokens=16,
                    aws_region_name=region,
                ),
                timeout=_BEDROCK_TIMEOUT_SECONDS,
            )
            receipt = _bedrock_receipt_projection(
                response,
                model=model,
                messages=messages,
                started_at=started_at,
                started_ns=started_ns,
            )
    except AcceptanceCheckError:
        raise
    except TimeoutError:
        raise AcceptanceCheckError("bedrock_timeout") from None
    except Exception:
        raise AcceptanceCheckError("bedrock_provider") from None
    return receipt


def _build_operator_profile_registry(settings: Any) -> OperatorProfileRegistry:
    runtime = RuntimeWebPluginConfig.from_settings(settings)
    policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime)
    return OperatorProfileRegistry(policy=policy, settings=runtime)


_GUARDRAIL_GATE_ENV = "ELSPETH_RUN_LIVE_BEDROCK_GUARDRAILS"

_MAX_GUARDRAIL_CONFIG_ENV_BYTES = 64 * 1024


def _guardrail_config_defaults(env: Mapping[str, str], plugin_id: str) -> tuple[str | None, str | None]:
    """Derive (alias, version) defaults from the rendered guardrail policy env.

    The deployment already renders ``ELSPETH_WEB__BEDROCK_GUARDRAIL_DEFAULT_PROFILES``
    (plugin -> approved alias) and ``ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES``
    (approved bindings including ``guardrail_version``) into the task
    definition, so the live check can default the PROFILE_ALIAS and
    EXPECTED_VERSION inputs from them.  The safe/blocked probe texts stay
    operator-supplied: the check needs a human-chosen attack string.
    Unparseable or non-matching config yields no default and the input is
    reported missing.
    """

    raw_defaults = env.get("ELSPETH_WEB__BEDROCK_GUARDRAIL_DEFAULT_PROFILES")
    raw_profiles = env.get("ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES")
    if (
        type(raw_defaults) is not str
        or type(raw_profiles) is not str
        or len(raw_defaults) > _MAX_GUARDRAIL_CONFIG_ENV_BYTES
        or len(raw_profiles) > _MAX_GUARDRAIL_CONFIG_ENV_BYTES
    ):
        return None, None
    try:
        defaults = json.loads(raw_defaults)
        profiles = json.loads(raw_profiles)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, None
    if not isinstance(defaults, dict) or not isinstance(profiles, list):
        return None, None
    alias = defaults.get(plugin_id)
    if type(alias) is not str or not alias:
        return None, None
    versions = [
        profile.get("guardrail_version")
        for profile in profiles
        if isinstance(profile, dict) and profile.get("plugin") == plugin_id and profile.get("alias") == alias
    ]
    if len(versions) != 1 or type(versions[0]) is not str:
        return alias, None
    return alias, versions[0]


def _guardrail_live_inputs(env: Mapping[str, str]) -> tuple[tuple[str, str, str, str, str], ...]:
    gate = env.get(_GUARDRAIL_GATE_ENV)
    if gate is None:
        raise AcceptanceCheckError("guardrails_live_inputs_missing", missing=(_GUARDRAIL_GATE_ENV,))
    if gate != "1":
        raise AcceptanceCheckError("guardrails_gate")
    missing: list[str] = []
    resolved: list[tuple[str, str | None, str | None, str | None, str | None]] = []
    for plugin_id, alias_name, safe_name, blocked_name, version_name in _GUARDRAIL_INPUTS:
        default_alias, default_version = _guardrail_config_defaults(env, plugin_id)
        alias = env.get(alias_name)
        if alias is None:
            alias = default_alias
        version = env.get(version_name)
        if version is None and env.get(alias_name) in {None, default_alias}:
            # The rendered-config version default only binds to the
            # rendered-config alias; an operator-supplied divergent alias
            # must supply its own expected version.
            version = default_version
        safe_text = env.get(safe_name)
        blocked_text = env.get(blocked_name)
        if alias is None:
            missing.append(alias_name)
        if version is None:
            missing.append(version_name)
        if safe_text is None:
            missing.append(safe_name)
        if blocked_text is None:
            missing.append(blocked_name)
        resolved.append((plugin_id, alias, safe_text, blocked_text, version))
    if missing:
        raise AcceptanceCheckError("guardrails_live_inputs_missing", missing=tuple(missing))
    values: list[tuple[str, str, str, str, str]] = []
    for plugin_id, alias, safe_text, blocked_text, version in resolved:
        if (
            type(alias) is not str
            or not alias
            or type(safe_text) is not str
            or not 1 <= len(safe_text) <= 1_000_000
            or type(blocked_text) is not str
            or not 1 <= len(blocked_text) <= 1_000_000
            or type(version) is not str
            or re.fullmatch(r"[1-9][0-9]{0,7}", version) is None
        ):
            raise AcceptanceCheckError("guardrails_input")
        values.append((plugin_id, alias, safe_text, blocked_text, version))
    return tuple(values)


class _AcceptanceSecretInventory:
    """Empty credential inventory for the keyless ECS Bedrock acceptance principal."""

    def has_ref(self, principal: str, name: str) -> bool:
        del principal, name
        return False

    def has_server_ref(self, name: str) -> bool:
        del name
        return False

    def has_user_ref(self, principal: str, name: str) -> bool:
        del principal, name
        return False

    def server_generation(self, name: str) -> str | None:
        del name
        return None

    def user_generation(self, principal: str, name: str) -> str | None:
        del principal, name
        return None


def build_plugin_policy_acceptance(
    settings: Any,
    env: Mapping[str, str],
) -> tuple[WebPluginPolicyEvidence, dict[str, object]]:
    """Build and validate the effective keyless Bedrock policy used by ECS."""

    live_inputs = _guardrail_live_inputs(env)
    expected_aliases = {plugin_name: alias for plugin_name, alias, _safe, _blocked, _version in live_inputs}
    prompt_id = PluginId("transform", "aws_bedrock_prompt_shield")
    content_id = PluginId("transform", "aws_bedrock_content_safety")
    llm_id = PluginId("transform", "llm")
    try:
        expected_binding_sha256 = env.get("ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256")
        if (
            type(expected_binding_sha256) is not str
            or _SHA256_PATTERN.fullmatch(expected_binding_sha256) is None
            or plugin_policy_binding_sha256(env) != expected_binding_sha256
        ):
            raise ValueError("policy binding mismatch")
        live_model = env.get("ELSPETH_BEDROCK_LIVE_TEST_MODEL")
        if type(live_model) is not str or not live_model.startswith("bedrock/"):
            raise ValueError("live model missing")
        live_region = _resolve_aws_region(env, check="plugin_policy_settings")
        runtime = RuntimeWebPluginConfig.from_settings(settings)
        policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime)
        profiles = OperatorProfileRegistry(policy=policy, settings=runtime)
        secret_key = settings.secret_key
        if type(secret_key) is not str or len(secret_key.encode("utf-8")) < 32:
            raise ValueError("invalid generation key")
        catalog = create_catalog_service()
        snapshot = build_plugin_snapshot(
            policy=policy,
            catalog=catalog,
            profiles=profiles,
            principal_scope="system:aws-ecs-acceptance",
            secret_inventory=_AcceptanceSecretInventory(),
            generation_key=secret_key.encode("utf-8"),
        )
        tutorial_state_profile = runtime.default_llm_profile
        if tutorial_state_profile is None:
            tutorial_state_profile = ""
        readiness = build_plugin_policy_readiness(
            policy=policy,
            snapshot=snapshot,
            tutorial_profile=runtime.default_llm_profile,
            tutorial_state=_canonical_tutorial_policy_state(profile_alias=tutorial_state_profile),
            profile_registry=profiles,
            catalog=catalog,
        )
    except Exception:
        raise AcceptanceCheckError("plugin_policy_settings") from None

    selected = dict(snapshot.selected)
    aliases = dict(snapshot.selected_profile_aliases)
    modes = dict(snapshot.control_modes)
    tutorial_alias = runtime.default_llm_profile
    llm_profiles = dict(runtime.llm_profiles)
    tutorial_profile = None
    if tutorial_alias is not None:
        tutorial_profile = llm_profiles[tutorial_alias]
    readiness_rows = {row.id: row for row in readiness.rows}
    profile_row = readiness_rows["tutorial_profile"]
    coverage_row = readiness_rows["tutorial_required_control_coverage"]
    tutorial_profile_region: object = None
    if tutorial_profile is not None:
        tutorial_profile_options = dict(tutorial_profile.provider_options)
        if "region_name" in tutorial_profile_options:
            tutorial_profile_region = tutorial_profile_options["region_name"]
    selected_llm = None
    if PluginCapability.LLM in selected:
        selected_llm = selected[PluginCapability.LLM]
    selected_prompt_shield = None
    if PluginCapability.PROMPT_SHIELD in selected:
        selected_prompt_shield = selected[PluginCapability.PROMPT_SHIELD]
    selected_content_safety = None
    if PluginCapability.CONTENT_SAFETY in selected:
        selected_content_safety = selected[PluginCapability.CONTENT_SAFETY]
    prompt_shield_mode = None
    if PluginCapability.PROMPT_SHIELD in modes:
        prompt_shield_mode = modes[PluginCapability.PROMPT_SHIELD]
    content_safety_mode = None
    if PluginCapability.CONTENT_SAFETY in modes:
        content_safety_mode = modes[PluginCapability.CONTENT_SAFETY]
    llm_alias = None
    if llm_id in aliases:
        llm_alias = aliases[llm_id]
    prompt_shield_alias = None
    if prompt_id in aliases:
        prompt_shield_alias = aliases[prompt_id]
    content_safety_alias = None
    if content_id in aliases:
        content_safety_alias = aliases[content_id]
    if (
        tutorial_alias is None
        or tutorial_profile is None
        or tutorial_profile.provider != "bedrock"
        or tutorial_profile.model != live_model
        or tutorial_profile_region != live_region
        or readiness.tutorial_ready is not False
        or profile_row.status == "error"
        or coverage_row.status != "error"
        or not {llm_id, prompt_id, content_id} <= snapshot.available
        or selected_llm != llm_id
        or selected_prompt_shield != prompt_id
        or selected_content_safety != content_id
        or prompt_shield_mode is not ControlMode.REQUIRED
        or content_safety_mode is not ControlMode.REQUIRED
        or llm_alias != tutorial_alias
        or prompt_shield_alias != expected_aliases["aws_bedrock_prompt_shield"]
        or content_safety_alias != expected_aliases["aws_bedrock_content_safety"]
    ):
        raise AcceptanceCheckError("plugin_policy_selection")

    evidence = _build_web_plugin_policy_evidence(snapshot=snapshot, policy=policy)
    receipt = {
        "policy_hash": evidence.policy_hash,
        "snapshot_hash": evidence.snapshot_hash,
        "binding_sha256": expected_binding_sha256,
        "tutorial_profile_ready": True,
        "tutorial_ready": False,
        "tutorial_blocker": "tutorial_required_control_coverage",
        "tutorial_profile_alias": tutorial_alias,
        "target_llm": str(llm_id),
        "selected_controls": [
            {
                "capability": PluginCapability.PROMPT_SHIELD.value,
                "plugin_id": str(prompt_id),
                "profile_alias": expected_aliases["aws_bedrock_prompt_shield"],
                "mode": ControlMode.REQUIRED.value,
            },
            {
                "capability": PluginCapability.CONTENT_SAFETY.value,
                "plugin_id": str(content_id),
                "profile_alias": expected_aliases["aws_bedrock_content_safety"],
                "mode": ControlMode.REQUIRED.value,
            },
        ],
    }
    return evidence, receipt


def verify_bedrock_guardrails(
    env: Mapping[str, str],
    *,
    settings_loader: Callable[[], Any] = settings_from_env,
    registry_factory: Callable[[Any], Any] = _build_operator_profile_registry,
    execution: Any,
    checker: Callable[..., Any] = run_guardrail_live_check,
    telemetry_emit: Callable[[Any], None] = lambda _event: None,
    run_id: str = "guardrail-acceptance-run",
    state_id: str = "guardrail-acceptance-state",
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Run both approved Guardrail controls through the shared live checker."""

    if any(name in env for name in FORBIDDEN_AWS_OVERRIDE_ENV):
        raise AcceptanceCheckError("guardrails_aws_override")
    live_inputs = _guardrail_live_inputs(env)
    try:
        settings = settings_loader()
        registry = registry_factory(settings)
    except AcceptanceCheckError:
        raise
    except Exception:
        raise AcceptanceCheckError("guardrails_settings") from None
    checked_at = _utc_timestamp(now())
    controls: list[dict[str, object]] = []
    for plugin_name, alias, safe_text, blocked_text, expected_version in live_inputs:
        try:
            profile = registry.approved_bedrock_guardrail_profile(
                PluginId("transform", plugin_name),
                alias=alias,
            )
        except Exception:
            raise AcceptanceCheckError("guardrails_profile") from None
        if profile.guardrail_version != expected_version:
            raise AcceptanceCheckError("guardrails_profile")
        try:
            receipt = checker(
                profile=profile,
                safe_text=safe_text,
                blocked_text=blocked_text,
                execution=execution,
                state_id=state_id,
                run_id=run_id,
                telemetry_emit=telemetry_emit,
            )
        except Exception:
            raise AcceptanceCheckError("guardrails_live_check") from None
        if (
            receipt.plugin_id != plugin_name
            or receipt.profile_alias != alias
            or receipt.safe_case_passed is not True
            or receipt.attack_case_blocked is not True
            or receipt.request_ids_present is not True
        ):
            raise AcceptanceCheckError("guardrails_receipt")
        controls.append(
            {
                "plugin_id": receipt.plugin_id,
                "profile_alias": receipt.profile_alias,
                "guardrail_version": expected_version,
                "safe_case_passed": True,
                "attack_case_blocked": True,
                "request_ids_present": True,
                "safe_text_sha256": _sha256(safe_text.encode("utf-8")),
                "blocked_text_sha256": _sha256(blocked_text.encode("utf-8")),
                "checked_at": checked_at,
            }
        )
    return {"controls": controls}


def _create_guardrail_telemetry_manager(settings: Any) -> Any:
    telemetry_settings = build_aws_operator_pipeline_telemetry(settings)
    runtime_config = RuntimeTelemetryConfig.from_settings(telemetry_settings)
    manager = create_telemetry_manager(runtime_config)
    if manager is None:
        raise RuntimeError("operator telemetry manager unavailable")
    return manager


def run_bedrock_guardrails_live(
    env: Mapping[str, str],
    *,
    settings_loader: Callable[[], Any] = settings_from_env,
    registry_factory: Callable[[Any], Any] = _build_operator_profile_registry,
    checker: Callable[..., Any] = run_guardrail_live_check,
    telemetry_manager_factory: Callable[[Any], Any] = _create_guardrail_telemetry_manager,
    policy_acceptance_factory: Callable[
        [Any, Mapping[str, str]], tuple[WebPluginPolicyEvidence, dict[str, object]]
    ] = build_plugin_policy_acceptance,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Own the real Landscape and OTLP resources for the Guardrail live proof."""

    try:
        prepare_for_run()
        settings = settings_loader()
        policy_evidence, policy_receipt = policy_acceptance_factory(settings, env)
        manager = telemetry_manager_factory(settings)
    except AcceptanceCheckError:
        # A named check failure (for example ``guardrails_live_inputs_missing``
        # or ``guardrails_gate``) must surface as itself, never be
        # re-labelled as a settings failure (F13).
        raise
    except Exception:
        raise AcceptanceCheckError("guardrails_settings") from None

    manager_failed = False
    result: dict[str, object] | None = None
    try:
        try:
            catalog_sha, catalog_source = read_openrouter_catalog_snapshot_id()
            landscape_url = settings.get_landscape_url()
            passphrase = settings.landscape_passphrase
        except Exception:
            raise AcceptanceCheckError("guardrails_landscape") from None
        try:
            with LandscapeDB.from_url(
                landscape_url,
                passphrase=passphrase,
                create_tables=False,
            ) as database:
                repositories = RecorderFactory.writable(database)
                run_id = str(uuid.uuid4())
                run = repositories.run_lifecycle.begin_run(
                    config={"acceptance": "bedrock-guardrails"},
                    canonical_version="v1",
                    run_id=run_id,
                    openrouter_catalog_sha256=catalog_sha,
                    openrouter_catalog_source=catalog_source,
                    web_plugin_policy_evidence=policy_evidence,
                )
                node = repositories.data_flow.register_node(
                    run.run_id,
                    "aws_bedrock_guardrails_acceptance",
                    NodeType.TRANSFORM,
                    "1.0.0",
                    {},
                    determinism=Determinism.EXTERNAL_CALL,
                    schema_config=SchemaConfig.from_dict({"mode": "observed"}),
                )
                _row, token = repositories.data_flow.create_row_with_token(
                    run.run_id,
                    node.node_id,
                    0,
                    {"check": "bedrock-guardrails"},
                    source_row_index=0,
                    ingest_sequence=0,
                )
                state = repositories.execution.begin_node_state(
                    token.token_id,
                    node.node_id,
                    run.run_id,
                    0,
                    {"check": "bedrock-guardrails"},
                )
                audit_proofs: list[bool] = []

                def emit_after_persisted_audit(event: Any) -> None:
                    calls = repositories.query.get_calls(state.state_id)
                    latest = calls[-1] if calls else None
                    expected_index = len(audit_proofs)
                    valid = bool(
                        latest is not None
                        and len(calls) == expected_index + 1
                        and latest.call_index == expected_index
                        and latest.state_id == event.state_id == state.state_id
                        and latest.call_type is event.call_type is CallType.HTTP
                        and latest.status is event.status is CallStatus.SUCCESS
                        and latest.request_hash == event.request_hash
                        and latest.response_hash is not None
                        and latest.response_hash == event.response_hash
                    )
                    audit_proofs.append(valid)
                    if valid:
                        manager.handle_event(event)

                started = time.monotonic()
                failure: AcceptanceCheckError | None = None
                try:
                    guardrail_result = verify_bedrock_guardrails(
                        env,
                        settings_loader=lambda: settings,
                        registry_factory=registry_factory,
                        execution=repositories.execution,
                        checker=checker,
                        telemetry_emit=emit_after_persisted_audit,
                        run_id=run.run_id,
                        state_id=state.state_id,
                        now=now,
                    )
                    persisted_policy = repositories.run_lifecycle.get_web_plugin_policy_evidence(run.run_id)
                    if persisted_policy != policy_evidence:
                        raise AcceptanceCheckError("guardrails_policy_evidence")
                    result = {
                        **guardrail_result,
                        "plugin_policy": {**policy_receipt, "landscape_evidence": True},
                    }
                    manager.flush()
                    if audit_proofs != [True, True, True, True]:
                        raise AcceptanceCheckError("guardrails_audit_order")
                except AcceptanceCheckError as exc:
                    failure = exc
                except Exception:
                    failure = AcceptanceCheckError("guardrails_live_check")
                duration_ms = max(0.0, (time.monotonic() - started) * 1000)
                token_ref = TokenRef(token_id=token.token_id, run_id=run.run_id)
                if failure is None:
                    repositories.execution.complete_node_state(
                        state.state_id,
                        NodeStateStatus.COMPLETED,
                        output_data={"check": "bedrock-guardrails", "ok": True},
                        duration_ms=duration_ms,
                    )
                    repositories.data_flow.record_token_outcome(
                        token_ref,
                        TerminalOutcome.SUCCESS,
                        TerminalPath.DEFAULT_FLOW,
                        sink_name="acceptance",
                    )
                    repositories.run_lifecycle.complete_run(run.run_id, RunStatus.COMPLETED)
                else:
                    error_hash = _sha256(b"bedrock-guardrails-acceptance-failed")
                    repositories.execution.complete_node_state(
                        state.state_id,
                        NodeStateStatus.FAILED,
                        error=ExecutionError(
                            exception="acceptance check failed",
                            exception_type="AcceptanceCheckError",
                        ),
                        duration_ms=duration_ms,
                    )
                    repositories.data_flow.record_token_outcome(
                        token_ref,
                        TerminalOutcome.FAILURE,
                        TerminalPath.UNROUTED,
                        error_hash=error_hash,
                    )
                    repositories.run_lifecycle.complete_run(run.run_id, RunStatus.FAILED)
                    raise failure
        except AcceptanceCheckError:
            raise
        except Exception:
            raise AcceptanceCheckError("guardrails_landscape") from None
    finally:
        try:
            manager.close()
        except Exception:
            manager_failed = True
    if manager_failed:
        raise AcceptanceCheckError("guardrails_telemetry")
    if result is None:
        raise AcceptanceCheckError("guardrails_live_check")
    return result
