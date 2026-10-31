"""Historical cache observations; cold active maps require fresh current work."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer.continuation import ContinuationValue
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_private_chunk, owned_json, verify_private_chunk
from elspeth.web.composer.continuation_result_credentials import require_retained_argument_credentials, require_retained_result_credentials
from elspeth.web.composer.discovery_cache import (
    CachedDiscoveryPayload,
    RuntimePreflightCache,
    make_cache_key,
)
from elspeth.web.composer.response_contracts import AdmittedResponse
from elspeth.web.composer.tools._registry import response_contract_for
from elspeth.web.composer.tools.discovery import is_cacheable_discovery_tool
from elspeth.web.credential_guard import require_no_credential_material
from elspeth.web.execution.runtime_preflight import RuntimePreflightFailure, RuntimePreflightKey
from elspeth.web.execution.schemas import ValidationResult


def _canonical_owned_json(value: object) -> str:
    return canonical_json(owned_json(deep_thaw(value)))


def _identity(objects: list[object], value: object) -> int:
    for index, existing in enumerate(objects):
        if existing is value:
            return index
    objects.append(value)
    return len(objects) - 1


class RetainedDiscoveryCacheEntry(ContinuationValue):
    capture_number: int = Field(ge=1)
    cache_identity: int = Field(ge=0)
    payload_identity: int = Field(ge=0)
    key: str
    tool_name: str
    arguments_json: str
    success: Literal[True] = True
    affected_nodes: tuple[str, ...]
    payload_json: str
    had_nominal_response: bool

    @model_validator(mode="after")
    def require_exact_key_and_wire(self) -> RetainedDiscoveryCacheEntry:
        arguments, error = parse_json_strict(self.arguments_json)
        payload, payload_error = parse_json_strict(self.payload_json)
        if (
            error is not None
            or type(arguments) is not dict
            or canonical_json(arguments) != self.arguments_json
            or payload_error is not None
            or canonical_json(payload) != self.payload_json
            or not is_cacheable_discovery_tool(self.tool_name)
            or make_cache_key(self.tool_name, arguments) != self.key
        ):
            raise ValueError("retained discovery history has an invalid exact key or payload")
        return self


class RetainedPreflightCacheEntry(ContinuationValue):
    capture_number: int = Field(ge=1)
    key_identity: int = Field(ge=0)
    entry_identity: int = Field(ge=0)
    exception_identity: int | None
    context_identity: int = Field(ge=0)
    session_scope: str
    state_version: int
    state_content_hash: str
    settings_hash: str
    interpretation_tolerant: bool
    creator_session_id: str
    creator_operation_id: str
    creator_operation_kind: str
    creator_operation_epoch: int
    was_context_bound: bool
    result_json: str | None
    failure: Literal["timeout", "permission_refused", "validation_failed", "preflight_failed"] | None

    @model_validator(mode="after")
    def require_completed_history(self) -> RetainedPreflightCacheEntry:
        if self.state_version < 0 or self.creator_operation_epoch <= 0 or (self.result_json is None) == (self.failure is None):
            raise ValueError("preflight history must retain exactly one completed result or closed failure")
        if self.result_json is not None:
            actual = ValidationResult.model_validate_json(self.result_json)
            if canonical_json(actual.model_dump(mode="json")) != self.result_json:
                raise ValueError("preflight history result is not exact canonical owned JSON")
        return self


class RetainedCacheCreator(ContinuationValue):
    operation_id: str
    operation_epoch: int = Field(ge=1)
    operation_kind: Literal["compose"] = "compose"


class ContinuationCachesSnapshot(ContinuationValue):
    schema_version: Literal["composer.caches.v1"] = "composer.caches.v1"
    session_id: str = Field(min_length=1)
    capture_count: int = Field(ge=1)
    capture_creators: tuple[RetainedCacheCreator, ...]
    discovery: tuple[RetainedDiscoveryCacheEntry, ...]
    preflight: tuple[RetainedPreflightCacheEntry, ...]
    last_preflight_json: str | None
    previous_last_preflight_json: tuple[str | None, ...]
    last_preflight_identity: int | None
    previous_last_preflight_identities: tuple[int | None, ...]
    needs_current_preflight: Literal[True] = True

    @model_validator(mode="after")
    def require_unique_history(self) -> ContinuationCachesSnapshot:
        if len(self.capture_creators) != self.capture_count or any(entry.creator_session_id != self.session_id for entry in self.preflight):
            raise ValueError("cache history changed its actual capture/session custody")
        if (
            len(self.previous_last_preflight_json) != self.capture_count - 1
            or len(self.previous_last_preflight_identities) != self.capture_count - 1
        ):
            raise ValueError("cache history lost a previous complete capture")
        for text, identity in zip(
            (*self.previous_last_preflight_json, self.last_preflight_json),
            (*self.previous_last_preflight_identities, self.last_preflight_identity),
            strict=True,
        ):
            if (text is None) != (identity is None) or (identity is not None and identity < 0):
                raise ValueError("cache history lost the actual last-result identity")
        for discovery_observation in self.discovery:
            if discovery_observation.capture_number > self.capture_count:
                raise ValueError("cache observation belongs to an absent capture")
        for preflight_observation in self.preflight:
            if preflight_observation.capture_number > self.capture_count:
                raise ValueError("cache observation belongs to an absent capture")
        _require_history_aliases(self)
        for text in (*self.previous_last_preflight_json, self.last_preflight_json):
            if text is not None:
                actual = ValidationResult.model_validate_json(text)
                if canonical_json(actual.model_dump(mode="json")) != text:
                    raise ValueError("last preflight history is not canonical owned JSON")
        return self


def capture_continuation_caches(
    *,
    discovery: dict[str, CachedDiscoveryPayload],
    preflight: RuntimePreflightCache,
    last_preflight: ValidationResult | None,
    creator: SessionOperationContext,
    prior: ContinuationCachesSnapshot | None = None,
    env_ref_names: frozenset[str] = frozenset(),
) -> ContinuationCachesSnapshot:
    if type(creator) is not SessionOperationContext or creator.operation_kind is not SessionOperationKind.COMPOSE:
        raise TypeError("cache history needs its actual creator context")
    if prior is not None and type(prior) is not ContinuationCachesSnapshot:
        raise TypeError("prior cache history must be its nominal snapshot")
    if (
        type(discovery) is not dict
        or type(preflight) is not dict
        or (last_preflight is not None and type(last_preflight) is not ValidationResult)
    ):
        raise TypeError("cache capture requires actual completed owned maps/result")
    if prior is not None and prior.session_id != creator.fence.session_id:
        raise AuditIntegrityError("prior cache history belongs to another session")
    identities: list[object] = []
    capture_number = 1 if prior is None else prior.capture_count + 1
    retained_discovery = list(prior.discovery) if prior is not None else []
    for discovery_key, cached in discovery.items():
        if type(cached) is not CachedDiscoveryPayload or cached.success is not True:
            raise AuditIntegrityError("Only an actual completed successful discovery result can be retained")
        tool_name, separator, arguments_text = discovery_key.partition(":")
        arguments, error = parse_json_strict(arguments_text)
        if not separator or error is not None or type(arguments) is not dict:
            raise AuditIntegrityError("Discovery cache lost its exact original arguments")
        nominal = cached.admitted_response
        if nominal is not None:
            if not isinstance(nominal, AdmittedResponse):
                raise AuditIntegrityError("discovery cache lost its actual nominal response")
            contract = response_contract_for(tool_name)
            if contract is None or cached.data is not None:
                raise AuditIntegrityError("Discovery cache nominal result lost its registered response owner")
            payload = nominal.readmit(contract).to_wire()
        else:
            if response_contract_for(tool_name) is not None:
                raise AuditIntegrityError("Registered discovery response lost its nominal producer")
            payload = cached.data
        discovery_entry = RetainedDiscoveryCacheEntry(
            capture_number=capture_number,
            cache_identity=_identity(identities, cached),
            payload_identity=_identity(identities, nominal if nominal is not None else cached.data),
            key=discovery_key,
            tool_name=tool_name,
            arguments_json=canonical_json(arguments),
            affected_nodes=cached.affected_nodes,
            payload_json=_canonical_owned_json(payload),
            had_nominal_response=nominal is not None,
        )
        retained_discovery.append(discovery_entry)
    retained_preflight = list(prior.preflight) if prior is not None else []
    for preflight_key, preflight_value in preflight.items():
        if type(preflight_key) is not RuntimePreflightKey:
            raise AuditIntegrityError("Preflight history lost its nominal completed key")
        source_context = preflight_key.session_operation_context or creator
        if type(source_context) is not SessionOperationContext:
            raise AuditIntegrityError("preflight history requires its actual context owner")
        if source_context.fence.session_id != creator.fence.session_id:
            raise AuditIntegrityError("Preflight cache history crossed its actual session")
        result_json = None
        failure: Literal["timeout", "permission_refused", "validation_failed", "preflight_failed"] | None = None
        if type(preflight_value) is ValidationResult:
            result_json = canonical_json(preflight_value.model_dump(mode="json"))
        elif type(preflight_value) is RuntimePreflightFailure:
            original = preflight_value.original_exc
            if isinstance(original, TimeoutError):
                failure = "timeout"
            elif isinstance(original, PermissionError):
                failure = "permission_refused"
            elif isinstance(original, (ValueError, ValidationError)):
                failure = "validation_failed"
            else:
                failure = "preflight_failed"
        else:
            raise AuditIntegrityError("Preflight cache entry is not an actual completed owned outcome")
        preflight_entry = RetainedPreflightCacheEntry(
            capture_number=capture_number,
            key_identity=_identity(identities, preflight_key),
            entry_identity=_identity(identities, preflight_value),
            exception_identity=_identity(identities, preflight_value.original_exc)
            if type(preflight_value) is RuntimePreflightFailure
            else None,
            context_identity=_identity(identities, source_context),
            session_scope=preflight_key.session_scope,
            state_version=preflight_key.state_version,
            state_content_hash=preflight_key.state_content_hash,
            settings_hash=preflight_key.settings_hash,
            interpretation_tolerant=preflight_key.interpretation_tolerant,
            creator_session_id=source_context.fence.session_id,
            creator_operation_id=source_context.fence.operation_id,
            creator_operation_kind=source_context.operation_kind.value,
            creator_operation_epoch=source_context.fence.operation_epoch,
            was_context_bound=preflight_key.session_operation_context is not None,
            result_json=result_json,
            failure=failure,
        )
        retained_preflight.append(preflight_entry)
    snapshot = ContinuationCachesSnapshot(
        session_id=creator.fence.session_id,
        capture_creators=(
            *(prior.capture_creators if prior is not None else ()),
            RetainedCacheCreator(operation_id=creator.fence.operation_id, operation_epoch=creator.fence.operation_epoch),
        ),
        capture_count=capture_number,
        last_preflight_identity=_identity(identities, last_preflight) if last_preflight is not None else None,
        previous_last_preflight_identities=(*prior.previous_last_preflight_identities, prior.last_preflight_identity)
        if prior is not None
        else (),
        discovery=tuple(retained_discovery),
        preflight=tuple(retained_preflight),
        previous_last_preflight_json=(*prior.previous_last_preflight_json, prior.last_preflight_json) if prior is not None else (),
        last_preflight_json=(canonical_json(last_preflight.model_dump(mode="json")) if last_preflight is not None else None),
    )
    require_cache_history(snapshot, env_ref_names=env_ref_names)
    return snapshot


def encode_continuation_caches(snapshot: ContinuationCachesSnapshot, *, env_ref_names: frozenset[str] = frozenset()) -> PrivateChunk:
    if type(snapshot) is not ContinuationCachesSnapshot:
        raise TypeError("cache history requires its nominal snapshot")
    require_cache_history(snapshot, env_ref_names=env_ref_names)
    return encode_private_chunk(
        codec="composer.caches.v1", canonical=canonical_json(snapshot.model_dump(mode="json")), env_ref_names=env_ref_names
    )


def decode_continuation_caches(chunk: PrivateChunk, *, env_ref_names: frozenset[str] = frozenset()) -> ContinuationCachesSnapshot:
    if type(chunk) is not PrivateChunk or chunk.reference.codec != "composer.caches.v1":
        raise AuditIntegrityError("cache history has the wrong codec")
    verify_private_chunk(chunk.reference, chunk.canonical)
    try:
        snapshot = ContinuationCachesSnapshot.model_validate_json(chunk.canonical)
        if encode_continuation_caches(snapshot, env_ref_names=env_ref_names).canonical != chunk.canonical:
            raise AuditIntegrityError("cache history omitted or normalized a field")
        return snapshot
    except ValidationError as validation_error:
        raise AuditIntegrityError("cache history violates its closed codec") from validation_error


@dataclass(slots=True)
class ColdComposerCaches:
    history: ContinuationCachesSnapshot
    discovery: dict[str, CachedDiscoveryPayload] = field(default_factory=dict)
    preflight: RuntimePreflightCache = field(default_factory=dict)
    last_preflight: ValidationResult | None = None


def restore_continuation_caches(snapshot: ContinuationCachesSnapshot, *, env_ref_names: frozenset[str] = frozenset()) -> ColdComposerCaches:
    if type(snapshot) is not ContinuationCachesSnapshot:
        raise TypeError("cold caches require their nominal completed history")
    require_cache_history(snapshot, env_ref_names=env_ref_names)
    return ColdComposerCaches(history=snapshot)


def require_cache_history(snapshot: ContinuationCachesSnapshot, *, env_ref_names: frozenset[str]) -> None:
    """Admit historical DATA; do not remint active response or preflight authority."""
    if type(snapshot) is not ContinuationCachesSnapshot:
        raise TypeError("cache history requires its exact owner")
    encoded = canonical_json(snapshot.model_dump(mode="json"))
    try:
        checked = ContinuationCachesSnapshot.model_validate_json(encoded)
    except ValidationError as validation_error:
        raise AuditIntegrityError("cache history violates its complete capture/identity contract") from validation_error
    if canonical_json(checked.model_dump(mode="json")) != encoded:
        raise AuditIntegrityError("cache history omitted or normalized a field")
    require_no_credential_material(
        (snapshot.session_id, tuple(item.model_dump(mode="json") for item in snapshot.capture_creators)),
        surface="cache capture history",
        env_ref_names=env_ref_names,
    )
    for discovery_entry in snapshot.discovery:
        arguments, error = parse_json_strict(discovery_entry.arguments_json)
        payload, payload_error = parse_json_strict(discovery_entry.payload_json)
        if error is not None or payload_error is not None:
            raise AuditIntegrityError("cache history contains invalid embedded JSON")
        require_no_credential_material(
            (discovery_entry.key.split(":", 1)[0], discovery_entry.affected_nodes), surface="cache metadata", env_ref_names=env_ref_names
        )
        require_retained_argument_credentials(discovery_entry.tool_name, arguments, env_ref_names=env_ref_names)
        require_retained_result_credentials(discovery_entry.tool_name, {"success": True, "data": payload}, env_ref_names=env_ref_names)
        if (response_contract_for(discovery_entry.tool_name) is not None) != discovery_entry.had_nominal_response:
            raise AuditIntegrityError("cache history changed its selected response owner")
    for preflight_entry in snapshot.preflight:
        if preflight_entry.creator_operation_kind not in SessionOperationKind:
            raise AuditIntegrityError("cache history changed operation vocabulary")
        require_no_credential_material(preflight_entry.model_dump(mode="json"), surface="preflight history", env_ref_names=env_ref_names)
        if preflight_entry.result_json is not None:
            result, error = parse_json_strict(preflight_entry.result_json)
            if error is not None:
                raise AuditIntegrityError("preflight result history is not strict JSON")
            require_no_credential_material(result, surface="preflight result history", env_ref_names=env_ref_names)
    for text in (*snapshot.previous_last_preflight_json, snapshot.last_preflight_json):
        if text is not None:
            value, error = parse_json_strict(text)
            if error is not None:
                raise AuditIntegrityError("last preflight is not strict JSON")
            require_no_credential_material(value, surface="last preflight history", env_ref_names=env_ref_names)


def _require_history_aliases(snapshot: ContinuationCachesSnapshot) -> None:
    """Check retained identity labels without inferring identity from equal bytes."""
    identities: dict[tuple[int, int], tuple[object, ...]] = {}

    def bind(capture: int, identity: int, value: tuple[object, ...]) -> None:
        key = (capture, identity)
        if key in identities and identities[key] != value:
            raise ValueError("cache history changed one retained object's complete DATA")
        identities[key] = value

    cache_keys: set[tuple[int, str]] = set()
    for discovery_entry in snapshot.discovery:
        discovery_identity = (discovery_entry.capture_number, discovery_entry.key)
        if discovery_identity in cache_keys:
            raise ValueError("one discovery capture repeats a dictionary key")
        cache_keys.add(discovery_identity)
        bind(
            discovery_entry.capture_number,
            discovery_entry.cache_identity,
            (
                "cache",
                discovery_entry.success,
                discovery_entry.affected_nodes,
                discovery_entry.payload_identity,
                discovery_entry.had_nominal_response,
            ),
        )
        bind(
            discovery_entry.capture_number,
            discovery_entry.payload_identity,
            ("response" if discovery_entry.had_nominal_response else "data", discovery_entry.tool_name, discovery_entry.payload_json),
        )
    preflight_keys: set[tuple[int, int]] = set()
    for preflight_entry in snapshot.preflight:
        key_identity = (preflight_entry.capture_number, preflight_entry.key_identity)
        if key_identity in preflight_keys:
            raise ValueError("one preflight capture repeats the same actual dictionary key")
        preflight_keys.add(key_identity)
        if (preflight_entry.failure is None) != (preflight_entry.exception_identity is None):
            raise ValueError("preflight history lost its actual failure-owner relation")
        bind(
            preflight_entry.capture_number,
            preflight_entry.key_identity,
            (
                "key",
                preflight_entry.session_scope,
                preflight_entry.state_version,
                preflight_entry.state_content_hash,
                preflight_entry.settings_hash,
                preflight_entry.interpretation_tolerant,
                preflight_entry.context_identity if preflight_entry.was_context_bound else None,
            ),
        )
        bind(
            preflight_entry.capture_number,
            preflight_entry.context_identity,
            (
                "context",
                preflight_entry.creator_session_id,
                preflight_entry.creator_operation_id,
                preflight_entry.creator_operation_epoch,
                preflight_entry.creator_operation_kind,
            ),
        )
        bind(
            preflight_entry.capture_number,
            preflight_entry.entry_identity,
            ("result", preflight_entry.result_json)
            if preflight_entry.failure is None
            else ("failure", preflight_entry.failure, preflight_entry.exception_identity),
        )
        if preflight_entry.exception_identity is not None:
            if preflight_entry.exception_identity < 0:
                raise ValueError("preflight failure identity must be nonnegative")
            bind(preflight_entry.capture_number, preflight_entry.exception_identity, ("exception", preflight_entry.failure))
    for capture, (text, identity) in enumerate(
        zip(
            (*snapshot.previous_last_preflight_json, snapshot.last_preflight_json),
            (*snapshot.previous_last_preflight_identities, snapshot.last_preflight_identity),
            strict=True,
        ),
        1,
    ):
        if text is not None and identity is not None:
            bind(capture, identity, ("result", text))
