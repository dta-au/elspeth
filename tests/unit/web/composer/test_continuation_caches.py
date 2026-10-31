import pytest

from elspeth.contracts.errors import AuditIntegrityError, FrameworkBugError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.composer.continuation_caches import (
    capture_continuation_caches,
    decode_continuation_caches,
    encode_continuation_caches,
    restore_continuation_caches,
)
from elspeth.web.composer.continuation_codec import encode_private_chunk
from elspeth.web.composer.discovery_cache import CachedDiscoveryPayload, make_cache_key
from elspeth.web.composer.tools.generation import EXPRESSION_GRAMMAR_RESPONSE_CONTRACT
from elspeth.web.execution.runtime_preflight import RuntimePreflightFailure, RuntimePreflightKey
from elspeth.web.execution.schemas import ValidationReadiness, ValidationResult


def creator(session="session"):
    return SessionOperationContext(SessionOperationFence(session, "operation", "original-lease", 3), SessionOperationKind.COMPOSE)


def verdict():
    return ValidationResult(
        is_valid=True,
        checks=[],
        errors=[],
        warnings=[],
        semantic_contracts=[],
        readiness=ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[]),
    )


def capture(prior=None, last=True):
    context = creator()
    key = RuntimePreflightKey("scope", 4, "state", "settings", True, context)
    original = TimeoutError("do not serialize the original exception")
    normal = verdict()
    return capture_continuation_caches(
        discovery={
            make_cache_key("get_expression_grammar", {}): CachedDiscoveryPayload(
                True, (), None, EXPRESSION_GRAMMAR_RESPONSE_CONTRACT.admit({"grammar": "safe reference"})
            )
        },
        preflight={key: RuntimePreflightFailure(original), RuntimePreflightKey("scope", 4, "state", "settings"): normal},
        last_preflight=normal if last else None,
        creator=context,
        prior=prior,
    ), original


def test_history_retains_repeated_observations_and_cold_current_maps_are_empty():
    first, original = capture()
    assert restore_continuation_caches(first).last_preflight is None
    second, _ = capture(first, last=False)
    chunk = encode_continuation_caches(second)
    restored = decode_continuation_caches(chunk)
    assert restored == second
    assert restored.capture_count == 2
    assert len(restored.discovery) == 2
    assert restored.discovery[0].payload_json == restored.discovery[1].payload_json
    assert (restored.discovery[0].capture_number, restored.discovery[1].capture_number) == (1, 2)
    assert len(restored.preflight) == 4
    assert restored.preflight[0].failure == "timeout"
    assert restored.preflight[0].creator_operation_epoch == 3
    assert restored.preflight[0].creator_operation_kind == "compose"
    assert restored.preflight[0].was_context_bound is True
    assert restored.preflight[1].was_context_bound is False
    assert first.last_preflight_identity == first.preflight[1].entry_identity
    assert restored.previous_last_preflight_identities == (first.last_preflight_identity,)
    assert restored.last_preflight_json is None
    assert restored.previous_last_preflight_json == (first.last_preflight_json,)
    assert "original-lease" not in chunk.canonical
    assert str(original) not in chunk.canonical
    cold = restore_continuation_caches(restored)
    assert cold.history is restored
    assert cold.discovery == {}
    assert cold.preflight == {}
    assert cold.last_preflight is None
    assert restored.needs_current_preflight is True


def test_other_session_and_lost_nominal_response_refuse():
    first, _ = capture()
    assert decode_continuation_caches(encode_continuation_caches(first)) == first
    with pytest.raises(AuditIntegrityError):
        capture_continuation_caches(
            discovery={},
            preflight={RuntimePreflightKey("scope", 1, "state", "settings", False, creator("foreign")): verdict()},
            last_preflight=None,
            creator=creator(),
        )
    with pytest.raises(AuditIntegrityError):
        capture_continuation_caches(
            discovery={make_cache_key("get_expression_grammar", {}): CachedDiscoveryPayload(True, (), {"grammar": "safe"})},
            preflight={},
            last_preflight=None,
            creator=creator(),
        )


@pytest.mark.parametrize("change", ["missing", "unknown", "wire", "owner"])
def test_exact_cache_history_decode(change):
    normal, _ = capture()
    assert decode_continuation_caches(encode_continuation_caches(normal)) == normal
    wire = normal.model_dump(mode="json")
    if change == "missing":
        del wire["needs_current_preflight"]
    elif change == "unknown":
        wire["preflight"][1]["result_json"] = wire["preflight"][1]["result_json"][:-1] + ',"unowned":true}'
    elif change == "wire":
        wire["discovery"][0]["payload_json"] = "{}"
    else:
        wire["discovery"][0]["had_nominal_response"] = False
    with pytest.raises((AuditIntegrityError, FrameworkBugError, ValueError)):
        decode_continuation_caches(encode_private_chunk(codec="composer.caches.v1", canonical=canonical_json(wire)))


def test_actual_shared_and_equal_distinct_preflight_results_remain_distinct_history():
    shared = verdict()
    distinct = verdict()
    result = capture_continuation_caches(
        discovery={},
        preflight={
            RuntimePreflightKey("a", 1, "state", "settings"): shared,
            RuntimePreflightKey("b", 1, "state", "settings"): shared,
            RuntimePreflightKey("c", 1, "state", "settings"): distinct,
        },
        last_preflight=shared,
        creator=creator(),
    )
    restored = decode_continuation_caches(encode_continuation_caches(result))
    assert restored.preflight[0].entry_identity == restored.preflight[1].entry_identity
    assert restored.preflight[0].entry_identity != restored.preflight[2].entry_identity
    assert restored.last_preflight_identity == restored.preflight[0].entry_identity
    assert restored.preflight[0].result_json == restored.preflight[2].result_json
    wire = restored.model_dump(mode="json")
    wire["preflight"][1]["result_json"] = wire["preflight"][1]["result_json"].replace('"is_valid":true', '"is_valid":false')
    with pytest.raises(AuditIntegrityError):
        decode_continuation_caches(encode_private_chunk(codec="composer.caches.v1", canonical=canonical_json(wire)))


def test_actual_failure_owner_and_original_exception_identity_are_history_only():
    error = TimeoutError("original warm object")
    first = RuntimePreflightFailure(error)
    second = RuntimePreflightFailure(error)
    result = capture_continuation_caches(
        discovery={},
        preflight={
            RuntimePreflightKey("a", 1, "state", "settings"): first,
            RuntimePreflightKey("b", 1, "state", "settings"): first,
            RuntimePreflightKey("c", 1, "state", "settings"): second,
        },
        last_preflight=None,
        creator=creator(),
    )
    decoded = decode_continuation_caches(encode_continuation_caches(result))
    assert decoded.preflight[0].entry_identity == decoded.preflight[1].entry_identity
    assert decoded.preflight[0].entry_identity != decoded.preflight[2].entry_identity
    assert len({entry.exception_identity for entry in decoded.preflight}) == 1
    assert first.original_exc is error and second.original_exc is error
    assert restore_continuation_caches(decoded).preflight == {}


def test_schema_cache_retains_structural_labels_and_refuses_declared_values():
    from elspeth.web.composer.tools._generation_schema_response import PLUGIN_SCHEMA_RESPONSE_CONTRACT
    from elspeth.web.credential_guard import CredentialMaterialRefused
    from tests.unit.web.composer.test_generation_schema_response import schema

    source = schema()
    source.json_schema = {"type": "object", "properties": {"api_key": {"type": "string"}}, "required": ["api_key"]}
    arguments = {"plugin_type": source.plugin_type, "name": source.name}
    key = make_cache_key("get_plugin_schema", arguments)
    cache = {key: CachedDiscoveryPayload(True, (), None, PLUGIN_SCHEMA_RESPONSE_CONTRACT.admit(source))}
    retained = capture_continuation_caches(discovery=cache, preflight={}, last_preflight=None, creator=creator())
    assert '"api_key"' in retained.discovery[0].payload_json
    assert decode_continuation_caches(encode_continuation_caches(retained)) == retained
    source.json_schema["properties"]["api_key"]["default"] = "opaque-private-value"
    bad = {key: CachedDiscoveryPayload(True, (), None, PLUGIN_SCHEMA_RESPONSE_CONTRACT.admit(source))}
    with pytest.raises(CredentialMaterialRefused):
        capture_continuation_caches(discovery=bad, preflight={}, last_preflight=None, creator=creator())


def test_same_actual_preflight_dictionary_key_cannot_repeat_in_one_capture():
    normal, _ = capture()
    assert decode_continuation_caches(encode_continuation_caches(normal)) == normal
    wire = normal.model_dump(mode="json")
    wire["preflight"].append(dict(wire["preflight"][0]))
    with pytest.raises(AuditIntegrityError):
        decode_continuation_caches(encode_private_chunk(codec="composer.caches.v1", canonical=canonical_json(wire)))
