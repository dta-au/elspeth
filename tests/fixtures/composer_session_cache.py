"""Production-backed scenarios and work counters for Composer cache checks.

The raw validator remains the uncached reference. Callers may supply another
validator to ``run_validation`` to compare bypass, cold, and warmed analysis
without changing the graph or its expected result.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Lock
from typing import Any, Literal
from unittest.mock import patch

import yaml
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaValue

from elspeth.contracts.plugin_capabilities import ControlMode
from elspeth.contracts.plugin_protocols import TransformProtocol
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.composer.state import CompositionState, ValidationSummary
from elspeth.web.composer.yaml_importer import composition_state_from_runtime_yaml
from elspeth.web.config import WebSettings
from elspeth.web.plugin_policy.availability import build_plugin_snapshot
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig

ScenarioName = Literal["shipped_small", "chain_80", "invalid_same_version", "operator_profile"]
SCENARIO_NAMES: tuple[ScenarioName, ...] = ("shipped_small", "chain_80", "invalid_same_version", "operator_profile")
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROFILE_ALIAS = "fixture-prompt-guard"
_PRIVATE_GUARDRAIL = "fixtureguardrail"


@dataclass(frozen=True, slots=True)
class ComposerValidationScenario:
    """One graph and the production validation surface appropriate to it."""

    name: ScenarioName
    state: CompositionState
    expected_valid: bool
    policy_catalog: PolicyCatalogView | None = None

    def validate(self, state: CompositionState | None = None) -> ValidationSummary:
        current = self.state if state is None else state
        if self.policy_catalog is None:
            return current.validate()
        return self.policy_catalog.validate_composition_state(current).validation


@dataclass(slots=True)
class ComposerWorkCounts:
    """Actual production calls observed inside a bounded measurement."""

    validation_walks: int = 0
    transform_constructions: int = 0
    json_schema_generations: int = 0


class _AvailableFixtureSecrets:
    """Availability metadata only; this inventory never resolves secret bytes."""

    def has_server_ref(self, name: str) -> bool:
        del name
        return True

    def has_user_ref(self, principal: str, name: str) -> bool:
        del principal, name
        return True

    def has_ref(self, principal: str, name: str) -> bool:
        del principal, name
        return True

    def server_generation(self, name: str) -> str:
        del name
        return "fixture-generation"

    def user_generation(self, principal: str, name: str) -> str:
        del principal, name
        return "fixture-generation"


def _shipped_document() -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load((_REPO_ROOT / "examples/transform_pipeline/settings.yaml").read_text())
    # Deployment persistence is outside the runtime-YAML authoring surface.
    del document["landscape"]
    return document


def make_small_composer_state() -> CompositionState:
    """Import the shipped type-coerce/value-transform pipeline without running it."""
    return composition_state_from_runtime_yaml(yaml.safe_dump(_shipped_document()))


def make_chain_composer_state(node_count: int = 80) -> CompositionState:
    """Build a valid finite chain from the shipped type-coerce configuration."""
    if node_count < 1:
        raise ValueError("node_count must be positive")
    document = _shipped_document()
    document["sources"]["primary"]["on_success"] = "coerce_0"
    transform = document["transforms"][0]
    document["transforms"] = [
        {
            **deepcopy(transform),
            "name": f"coerce_{index}",
            "input": f"coerce_{index}",
            "on_success": f"coerce_{index + 1}" if index + 1 < node_count else "output",
        }
        for index in range(node_count)
    ]
    return composition_state_from_runtime_yaml(yaml.safe_dump(document))


def _profiled_scenario() -> ComposerValidationScenario:
    settings = WebSettings.model_validate(
        {
            "secret_key": "fixture-key-" * 6,
            "composer_max_composition_turns": 4,
            "composer_max_discovery_turns": 4,
            "composer_timeout_seconds": 60,
            "composer_rate_limit_per_minute": 20,
            "shareable_link_signing_key": bytes(range(32)),
            "plugin_allowlist": ("transform:aws_bedrock_prompt_shield",),
            "plugin_control_modes": {"prompt_shield": ControlMode.RECOMMEND.value},
            "bedrock_guardrail_profiles": (
                {
                    "alias": _PROFILE_ALIAS,
                    "plugin": "aws_bedrock_prompt_shield",
                    "guardrail_identifier": _PRIVATE_GUARDRAIL,
                    "guardrail_version": "7",
                    "region": "us-east-1",
                },
            ),
        }
    )
    manager = get_shared_plugin_manager()
    catalog = CatalogServiceImpl(manager)
    runtime = RuntimeWebPluginConfig.from_settings(settings)
    policy = compile_web_plugin_policy(registry=manager, settings=runtime)
    profiles = OperatorProfileRegistry(policy=policy, settings=runtime)
    snapshot = build_plugin_snapshot(
        policy=policy,
        catalog=catalog,
        profiles=profiles,
        principal_scope="local:composer-cache-fixture",
        secret_inventory=_AvailableFixtureSecrets(),
        generation_key=b"composer-cache-fixture-generation",
    )
    document = {
        "sources": {
            "primary": {
                "plugin": "csv",
                "on_success": "shield_in",
                "options": {"path": "fixture.csv", "schema": {"mode": "observed"}, "on_validation_failure": "discard"},
            }
        },
        "transforms": [
            {
                "name": "shield",
                "plugin": "aws_bedrock_prompt_shield",
                "input": "shield_in",
                "on_success": "output",
                "on_error": "discard",
                "options": {"profile": _PROFILE_ALIAS, "fields": ["prompt"], "schema": {"mode": "observed"}},
            }
        ],
        "sinks": {
            "output": {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {"path": "fixture.jsonl", "schema": {"mode": "observed"}},
            }
        },
    }
    state = composition_state_from_runtime_yaml(yaml.safe_dump(document))
    return ComposerValidationScenario("operator_profile", state, True, PolicyCatalogView(catalog, snapshot, profiles))


def make_composer_validation_scenario(name: ScenarioName) -> ComposerValidationScenario:
    if name == "shipped_small":
        return ComposerValidationScenario(name, make_small_composer_state(), True)
    if name == "chain_80":
        return ComposerValidationScenario(name, make_chain_composer_state(), True)
    if name == "invalid_same_version":
        original = make_small_composer_state()
        return ComposerValidationScenario(name, replace(original, sources={}, version=original.version), False)
    if name == "operator_profile":
        return _profiled_scenario()
    raise ValueError(f"Unknown Composer validation scenario: {name}")


def run_validation(
    scenario: ComposerValidationScenario,
    validator: Callable[[CompositionState], ValidationSummary] | None = None,
) -> ValidationSummary:
    """Run one fixed input through the supplied analysis mode or its reference."""
    return scenario.validate() if validator is None else validator(scenario.state)


@contextmanager
def count_composer_work() -> Iterator[ComposerWorkCounts]:
    """Observe real walks, transform constructions, and JSON-schema generation.

    Use one non-nested, non-overlapping context per process: the validator and
    JSON-schema hooks are process-wide, while the transform hook patches only
    the live manager selected on entry. Worker callers must use that same
    manager; this helper does not propagate scoped-manager context variables.
    Increments are thread-safe. Join all measured workers before inspecting
    final counts or leaving this context.
    """
    manager = get_shared_plugin_manager()
    counts = ComposerWorkCounts()
    counter_lock = Lock()
    validate = CompositionState.validate
    create_transform = manager.create_transform
    generate_schema = GenerateJsonSchema.generate

    def measured_validation(state: CompositionState) -> ValidationSummary:
        with counter_lock:
            counts.validation_walks += 1
        return validate(state)

    def measured_construction(transform_type: str, config: dict[str, Any]) -> TransformProtocol:
        with counter_lock:
            counts.transform_constructions += 1
        return create_transform(transform_type, config)

    def measured_schema(generator: GenerateJsonSchema, *args: Any, **kwargs: Any) -> JsonSchemaValue:
        with counter_lock:
            counts.json_schema_generations += 1
        return generate_schema(generator, *args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch.object(CompositionState, "validate", measured_validation))
        stack.enter_context(patch.object(manager, "create_transform", measured_construction))
        stack.enter_context(patch.object(GenerateJsonSchema, "generate", measured_schema))
        yield counts
