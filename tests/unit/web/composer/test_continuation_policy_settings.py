"""U-38: the production continuation policy comes from WebSettings and is versioned.

Design: docs/architecture/design-notes/0.8.10-continuation-runtime-design.md §4.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation_policy import (
    CONTINUATION_POLICY_SCHEMA_VERSION,
    ContinuationAdmissionPolicy,
    decode_continuation_policy,
)
from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
from elspeth.web.config import ContinuationActionBoundsSettings, WebSettings, settings_from_env
from elspeth.web.prompt_guard_config import PromptGuardSettings
from tests.helpers.continuation_policy import continuation_test_settings, minimal_continuation_policy

_GOLDEN_V1_ENCODING = '{"schema_version":"composer.continuation.policy.v1","segment":{"nominal":420.0,"watermark":120.0,"quantum":180.0,"reserve":25.0},"actions":{"primary":{"preparation":5.0,"guard":15.0,"quota":10.0,"physical":240.0,"retention":5.0},"advisor":{"preparation":5.0,"guard":15.0,"quota":10.0,"physical":60.0,"retention":5.0},"title":{"preparation":5.0,"guard":15.0,"quota":10.0,"physical":30.0,"retention":5.0},"read":{"preparation":5.0,"guard":null,"quota":null,"physical":10.0,"retention":5.0},"session_sql":{"preparation":5.0,"guard":null,"quota":null,"physical":5.0,"retention":5.0},"blob":{"preparation":5.0,"guard":null,"quota":null,"physical":30.0,"retention":5.0},"external":{"preparation":5.0,"guard":null,"quota":null,"physical":30.0,"retention":5.0}}}'

_ROLES = ("primary", "advisor", "title", "read", "session_sql", "blob", "external")
_PROVIDER_ROLES = ("primary", "advisor", "title")

# Design §4.4 production defaults: (preparation, guard, quota, physical, retention).
_DESIGN_DEFAULTS: dict[str, tuple[float, float | None, float | None, float, float]] = {
    "primary": (5.0, 15.0, 10.0, 240.0, 5.0),
    "advisor": (5.0, 15.0, 10.0, 60.0, 5.0),
    "title": (5.0, 15.0, 10.0, 30.0, 5.0),
    "read": (5.0, None, None, 10.0, 5.0),
    "session_sql": (5.0, None, None, 5.0, 5.0),
    "blob": (5.0, None, None, 30.0, 5.0),
    "external": (5.0, None, None, 30.0, 5.0),
}


def _enabled(**overrides: Any) -> WebSettings:
    return continuation_test_settings(composer_continuation_enabled=True, **overrides)


def _policy(**overrides: Any) -> ContinuationAdmissionPolicy:
    policy = continuation_policy_from_settings(_enabled(**overrides))
    assert policy is not None
    return policy


def _bounds_with(role: str, stage: str, value: float) -> dict[str, Any]:
    bounds = ContinuationActionBoundsSettings().model_dump(mode="json")
    bounds[role][stage] = value
    return bounds


def test_v2_admission_stays_refused_with_default_settings() -> None:
    settings = continuation_test_settings()
    assert settings.composer_continuation_enabled is False
    assert continuation_policy_from_settings(settings) is None


def test_production_policy_from_settings_round_trips_and_is_versioned() -> None:
    policy = _policy()
    encoded = policy.model_dump_json()
    assert json.loads(encoded)["schema_version"] == CONTINUATION_POLICY_SCHEMA_VERSION == "composer.continuation.policy.v1"
    assert decode_continuation_policy(encoded, policy.policy_hash) == policy


def test_production_defaults_are_the_design_values() -> None:
    policy = _policy()
    assert (policy.segment.nominal, policy.segment.watermark, policy.segment.quantum, policy.segment.reserve) == (420.0, 120.0, 180.0, 25.0)
    actions = policy.actions.model_dump()
    for role, expected in _DESIGN_DEFAULTS.items():
        action = actions[role]
        assert (action["preparation"], action["guard"], action["quota"], action["physical"], action["retention"]) == expected, role


@pytest.mark.parametrize(
    ("field", "value", "path"),
    (
        ("composer_continuation_segment_nominal_seconds", 430.0, ("segment", "nominal")),
        ("composer_continuation_watermark_seconds", 110.0, ("segment", "watermark")),
        ("composer_continuation_quantum_seconds", 170.0, ("segment", "quantum")),
        ("composer_continuation_settlement_reserve_seconds", 20.0, ("segment", "reserve")),
    ),
)
def test_every_segment_setting_changes_the_policy(field: str, value: float, path: tuple[str, str]) -> None:
    changed = _policy(**{field: value})
    assert changed.model_dump()[path[0]][path[1]] == value
    assert changed != _policy()


@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("stage", ("preparation", "quota", "physical", "retention"))
def test_every_action_bound_setting_changes_the_policy(role: str, stage: str) -> None:
    role_settings = ContinuationActionBoundsSettings().model_dump(mode="json")[role]
    if stage not in role_settings:
        pytest.skip(f"{role}.{stage} is derived or absent, not an operator setting")
    default = role_settings[stage]
    changed = _policy(composer_continuation_action_bounds=_bounds_with(role, stage, default + 1.0))
    assert changed.actions.model_dump()[role][stage] == default + 1.0
    assert changed != _policy()


def test_advisor_physical_maps_from_composer_advisor_timeout() -> None:
    assert _policy().actions.advisor.physical == WebSettings.model_fields["composer_advisor_timeout_seconds"].default
    assert _policy(composer_advisor_timeout_seconds=45.0).actions.advisor.physical == 45.0
    assert "physical" not in ContinuationActionBoundsSettings().model_dump()["advisor"]


def test_guard_maps_from_the_prompt_guard_timeout_plus_margin() -> None:
    guard = PromptGuardSettings(
        endpoint="https://guard.example.test", subscription_key=SecretStr("synthetic-guard-key"), timeout_seconds=20.0
    )
    advisor_guard = PromptGuardSettings(
        endpoint="https://guard.example.test", subscription_key=SecretStr("synthetic-guard-key"), timeout_seconds=7.0
    )
    # A longer guard lengthens TITLE and PRIMARY, so the first segment must grow with them.
    policy = _policy(
        composer_prompt_guard=guard, composer_advisor_prompt_guard=advisor_guard, composer_continuation_segment_nominal_seconds=500.0
    )
    assert policy.actions.primary.guard == policy.actions.title.guard == 25.0
    assert policy.actions.advisor.guard == 12.0
    actions = policy.actions.model_dump()
    for role in ("read", "session_sql", "blob", "external"):
        assert actions[role]["guard"] is None
        assert actions[role]["quota"] is None


def test_watermark_must_be_below_nominal_at_settings_load() -> None:
    with pytest.raises(ValidationError, match="watermark"):
        _enabled(composer_continuation_segment_nominal_seconds=100.0, composer_continuation_watermark_seconds=100.0)


@pytest.mark.parametrize(
    "bounds",
    (
        {"read": {"quota": 1.0}},
        {"primary": {"quota": None}},
        {"primary": {"physical": 0.0}},
        {"session_sql": {"retention": float("inf")}},
        {"session_sql": {"retention": True}},
        {"advisor": {"physical": 60.0}},
        {"unknown_role": {"physical": 1.0}},
    ),
)
def test_invalid_action_bounds_refuse_at_settings_load(bounds: dict[str, dict[str, object]]) -> None:
    complete = ContinuationActionBoundsSettings().model_dump(mode="json")
    for role, stages in bounds.items():
        complete.setdefault(role, {}).update(stages)
    with pytest.raises(ValidationError):
        _enabled(composer_continuation_action_bounds=complete)


def test_action_bounds_read_from_one_json_environment_object(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS": "15",
        "ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS": "10",
        "ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS": "85",
        "ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE": "10",
        "ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY": "0" * 64,
        "ELSPETH_WEB__COMPOSER_CONTINUATION_ENABLED": "true",
        "ELSPETH_WEB__COMPOSER_CONTINUATION_ACTION_BOUNDS": json.dumps(_bounds_with("session_sql", "retention", 7.5)),
    }.items():
        monkeypatch.setenv(key, value)
    policy = continuation_policy_from_settings(settings_from_env())
    assert policy is not None and policy.actions.session_sql.retention == 7.5


@pytest.mark.parametrize(
    ("mutate", "refusal"),
    (
        pytest.param(lambda value: value.pop("schema_version"), "lost its schema version", id="missing-version"),
        pytest.param(
            lambda value: value.__setitem__("schema_version", "composer.continuation.policy.v0"),
            "unknown schema version",
            id="unknown-version",
        ),
        pytest.param(lambda value: value.__setitem__("schema_version", ["v1"]), "unknown schema version", id="non-string-version"),
        pytest.param(lambda value: value["segment"].__setitem__("added", 1.0), "complete owned contract", id="added-field"),
        pytest.param(lambda value: value["segment"].pop("reserve"), "complete owned contract", id="removed-field"),
    ),
)
def test_policy_decode_refuses_missing_unknown_added_and_removed_fields(mutate: Any, refusal: str) -> None:
    value = json.loads(_policy().model_dump_json())
    mutate(value)
    # The mutated payload's own hash, so the refusal can only come from the
    # version or shape check, never from a hash mismatch.
    with pytest.raises(AuditIntegrityError, match=refusal):
        decode_continuation_policy(canonical_json(value), stable_hash(value))


def test_v1_policy_golden_bytes_and_hash_are_pinned() -> None:
    policy = _policy()
    # Pinned v1 encoding and identity of the production defaults. A change here
    # is a new policy identity for every admitted operation: re-pin deliberately.
    assert policy.model_dump_json() == _GOLDEN_V1_ENCODING
    assert policy.policy_hash == "803319d4e1e6e84da9d4e609dd5743c2b6a88bde6433b9f09f38252a65f325c1"


def test_first_segment_must_fit_title_initialization_and_the_first_primary() -> None:
    # 275 PRIMARY + 25 reserve + 65 TITLE + 3 x 15 session SQL = 410 s.
    assert _policy(composer_continuation_segment_nominal_seconds=410.0).segment.nominal == 410.0
    with pytest.raises(ValueError, match="must cover the first segment"):
        _policy(composer_continuation_segment_nominal_seconds=409.0)


@pytest.mark.parametrize("field", ("composer_continuation_segment_nominal_seconds", "composer_continuation_quantum_seconds"))
def test_segment_seconds_stay_representable(field: str) -> None:
    with pytest.raises(ValidationError):
        _enabled(**{field: 1e20})


def test_stage_seconds_stay_representable() -> None:
    with pytest.raises(ValidationError):
        _enabled(composer_continuation_action_bounds=_bounds_with("primary", "physical", 1e20))


def test_fixture_is_the_production_policy() -> None:
    assert minimal_continuation_policy() == _policy()
    # A literal fixture ignores the production path: a non-default setting must flow through.
    assert minimal_continuation_policy(composer_continuation_quantum_seconds=170.0).segment.quantum == 170.0


def test_fixture_construction_path_is_the_settings_mapping() -> None:
    assert continuation_policy_from_settings(continuation_test_settings()) is None
    assert type(minimal_continuation_policy()) is ContinuationAdmissionPolicy


@pytest.mark.parametrize("enabled", (False, True))
def test_app_wires_the_settings_policy_into_the_composer_authority(tmp_path: Any, enabled: bool) -> None:
    from elspeth.web.app import create_app

    settings = continuation_test_settings(
        data_dir=tmp_path,
        landscape_url=f"sqlite:///{tmp_path}/runs/audit.db",
        payload_store_path=tmp_path / "payloads",
        composer_boot_probe_enabled=False,
        plugin_allowlist=("transform:passthrough",),
        composer_continuation_enabled=enabled,
    )
    app = create_app(settings=settings)
    try:
        retained = app.state.composer_async_operation_authority._continuation_policy
        assert retained == continuation_policy_from_settings(settings)
        assert (retained is None) is (not enabled)
    finally:
        app.state.session_engine.dispose()
