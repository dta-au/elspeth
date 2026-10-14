"""Credential-filtered private authoring evidence, separate from chat display.

Only admitted JSON and eligible original wire are retained. This is a finite
credential policy, not arbitrary-secret detection or an encrypted raw store.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, cast

from pydantic import JsonValue
from rfc8785 import CanonicalizationError

from elspeth.contracts.credential_material import (
    CREDENTIAL_DETECTOR_VERSION,
    REDACTED_CREDENTIAL_TEXT,
    CredentialTraversalPolicy,
    find_credential_material,
    is_secret_field,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, canonical_json_loads
from elspeth.web.composer.authority_hashing import project_composer_authority_payload, restore_composer_authority_payload
from elspeth.web.composer.bounded_json import (
    JSON_MAX_DEPTH,
    JSON_MAX_ITEMS,
    JSON_MAX_TOTAL_UTF8_BYTES,
    JsonBoundaryError,
    bounded_json_loads,
)
from elspeth.web.secrets.ref_policy import allowed_secret_ref_fields

DIAGNOSTIC_EVIDENCE_KIND: Final = "composer_diagnostic_evidence"
_SCHEMA: Final = "composer.private-tool-evidence.v1"
_REASONS: Final = frozenset(
    {
        "credential_field",
        "recognized_credential",
        "unsupported",
        "bounds",
        "malformed_json",
        "duplicate_keys",
        "not_recorded",
        "canonical_sentinel",
        "semantic_mismatch",
    }
)
_DISPOSITIONS: Final = frozenset({"retained", "filtered", "unavailable", "not_returned"})
_POLICY = CredentialTraversalPolicy(
    max_depth=JSON_MAX_DEPTH,
    max_nodes=JSON_MAX_ITEMS,
    max_width=JSON_MAX_ITEMS,
    max_string_bytes=JSON_MAX_TOTAL_UTF8_BYTES,
)


class _DuplicateKeys(ValueError):
    """Closed duplicate-key signal without candidate material in its text."""


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeys("Duplicate JSON keys")
        result[key] = value
    return result


def _decode(text: str) -> JsonValue:
    return cast(JsonValue, bounded_json_loads(text, label="private diagnostic JSON", object_pairs_hook=_unique_object))


def _decode_canonical(text: str) -> JsonValue:
    # Check strict duplicate/structure/byte bounds before the canonical inverse.
    # RFC 8785 prints integral doubles in integer notation; its owned inverse
    # preserves that numeric domain where ordinary JSON decoding cannot.
    _decode(text)
    value = cast(JsonValue, canonical_json_loads(text))
    if canonical_json(value) != text:
        raise ValueError("Private evidence source JSON is not canonical")
    return value


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _mapping(value: object, keys: set[str]) -> dict[str, JsonValue]:
    if type(value) not in (dict, MappingProxyType):
        raise AuditIntegrityError("Private evidence object is malformed")
    result = dict(cast(Mapping[str, JsonValue], value))
    if set(result) != keys:
        raise AuditIntegrityError("Private evidence fields are malformed")
    return result


@dataclass(frozen=True, slots=True)
class DiagnosticPayload:
    disposition: str
    encoding: str
    content: str | None
    retained_sha256: str | None
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.disposition) is not str or self.disposition not in _DISPOSITIONS:
            raise AuditIntegrityError("Private evidence disposition is invalid")
        if type(self.encoding) is not str or self.encoding not in {"json", "wire_text"}:
            raise AuditIntegrityError("Private evidence encoding is invalid")
        if type(self.reasons) is not tuple or any(type(reason) is not str or reason not in _REASONS for reason in self.reasons):
            raise AuditIntegrityError("Private evidence reasons are invalid")
        if len(set(self.reasons)) != len(self.reasons) or len(self.reasons) > len(_REASONS):
            raise AuditIntegrityError("Private evidence reasons repeat")
        if self.disposition in {"retained", "filtered"}:
            if type(self.content) is not str or type(self.retained_sha256) is not str:
                raise AuditIntegrityError("Private evidence retained payload is absent")
            try:
                if len(self.content.encode("utf-8")) > JSON_MAX_TOTAL_UTF8_BYTES or _digest(self.content) != self.retained_sha256:
                    raise AuditIntegrityError("Private evidence retained payload hash or size is invalid")
                if self.encoding == "json":
                    _decode_canonical(self.content)
                else:
                    _decode(self.content)
            except (ValueError, TypeError) as exc:
                raise AuditIntegrityError("Private evidence retained payload is malformed") from exc
            if self.disposition == "retained" and self.reasons:
                raise AuditIntegrityError("Unfiltered private evidence has filtering reasons")
            if self.disposition == "filtered" and (not self.reasons or self.encoding == "wire_text"):
                raise AuditIntegrityError("Filtered private evidence has inconsistent disposition")
        elif self.content is not None or self.retained_sha256 is not None:
            raise AuditIntegrityError("Absent private evidence carries content")
        elif self.disposition == "unavailable" and not self.reasons:
            raise AuditIntegrityError("Unavailable private evidence lacks a reason")
        elif self.disposition == "not_returned" and self.reasons:
            raise AuditIntegrityError("Unreturned private evidence has filtering reasons")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "disposition": self.disposition,
            "encoding": self.encoding,
            "content": self.content,
            "retained_sha256": self.retained_sha256,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, value: object) -> DiagnosticPayload:
        data = _mapping(value, {"disposition", "encoding", "content", "retained_sha256", "reasons"})
        reasons = data["reasons"]
        if type(reasons) not in (list, tuple):
            raise AuditIntegrityError("Private evidence reasons are malformed")
        return cls(
            cast(str, data["disposition"]),
            cast(str, data["encoding"]),
            cast(str | None, data["content"]),
            cast(str | None, data["retained_sha256"]),
            tuple(cast("list[str] | tuple[str, ...]", reasons)),
        )


@dataclass(frozen=True, slots=True)
class ComposerDiagnosticEvidence:
    session_id: str
    operation_id: str
    operation_epoch: int
    tool_call_id: str
    tool_name: str
    status: str
    evidence_id: str
    arguments: DiagnosticPayload
    authority_arguments: DiagnosticPayload | None
    result: DiagnosticPayload
    wire_arguments: DiagnosticPayload
    schema_version: str = _SCHEMA
    detector_version: str = CREDENTIAL_DETECTOR_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not str
            or self.schema_version != _SCHEMA
            or type(self.detector_version) is not str
            or self.detector_version not in {"1"}
        ):
            raise AuditIntegrityError("Private evidence schema or detector version is invalid")
        for value in (self.session_id, self.operation_id, self.tool_call_id, self.tool_name, self.status, self.evidence_id):
            if type(value) is not str or not value or len(value) > 512 or find_credential_material(value, _POLICY) is not None:
                raise AuditIntegrityError("Private evidence correlation metadata is invalid")
        if type(self.operation_epoch) is not int or self.operation_epoch < 1:
            raise AuditIntegrityError("Private evidence operation epoch is invalid")
        for payload in (self.arguments, self.result, self.wire_arguments):
            if type(payload) is not DiagnosticPayload:
                raise AuditIntegrityError("Private evidence payload is not an owned record")
        if self.authority_arguments is not None and (
            type(self.authority_arguments) is not DiagnosticPayload
            or self.tool_name not in {"set_pipeline", "emit_pipeline_proposal"}
            or self.authority_arguments.encoding != "json"
        ):
            raise AuditIntegrityError("Private evidence authority payload is invalid")
        if self.arguments.encoding != "json" or self.result.encoding != "json" or self.wire_arguments.encoding != "wire_text":
            raise AuditIntegrityError("Private evidence payload encoding does not match its role")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "detector_version": self.detector_version,
            "session_id": self.session_id,
            "operation_id": self.operation_id,
            "operation_epoch": self.operation_epoch,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "evidence_id": self.evidence_id,
            "arguments": self.arguments.to_dict(),
            "authority_arguments": self.authority_arguments.to_dict() if self.authority_arguments else None,
            "result": self.result.to_dict(),
            "wire_arguments": self.wire_arguments.to_dict(),
        }

    def to_envelope(self) -> dict[str, JsonValue]:
        evidence = self.to_dict()
        return {
            "_kind": DIAGNOSTIC_EVIDENCE_KIND,
            "session_id": self.session_id,
            "operation_id": self.operation_id,
            "operation_epoch": self.operation_epoch,
            "tool_call_id": self.tool_call_id,
            "evidence_id": self.evidence_id,
            "evidence": evidence,
            "evidence_sha256": _digest(canonical_json(evidence)),
        }

    @classmethod
    def from_dict(cls, value: object) -> ComposerDiagnosticEvidence:
        data = _mapping(
            value,
            {
                "schema_version",
                "detector_version",
                "session_id",
                "operation_id",
                "operation_epoch",
                "tool_call_id",
                "tool_name",
                "status",
                "evidence_id",
                "arguments",
                "authority_arguments",
                "result",
                "wire_arguments",
            },
        )
        return cls(
            session_id=cast(str, data["session_id"]),
            operation_id=cast(str, data["operation_id"]),
            operation_epoch=cast(int, data["operation_epoch"]),
            tool_call_id=cast(str, data["tool_call_id"]),
            tool_name=cast(str, data["tool_name"]),
            status=cast(str, data["status"]),
            evidence_id=cast(str, data["evidence_id"]),
            arguments=DiagnosticPayload.from_dict(data["arguments"]),
            authority_arguments=DiagnosticPayload.from_dict(data["authority_arguments"])
            if data["authority_arguments"] is not None
            else None,
            result=DiagnosticPayload.from_dict(data["result"]),
            wire_arguments=DiagnosticPayload.from_dict(data["wire_arguments"]),
            schema_version=cast(str, data["schema_version"]),
            detector_version=cast(str, data["detector_version"]),
        )

    @classmethod
    def from_envelope(
        cls, value: object, *, expected_session_id: str, expected_operation_id: str | None = None, expected_tool_call_id: str | None = None
    ) -> ComposerDiagnosticEvidence:
        data = _mapping(
            value, {"_kind", "session_id", "operation_id", "operation_epoch", "tool_call_id", "evidence_id", "evidence", "evidence_sha256"}
        )
        if any(
            type(data[key]) is not str for key in ("_kind", "session_id", "operation_id", "tool_call_id", "evidence_id", "evidence_sha256")
        ):
            raise AuditIntegrityError("Private evidence envelope metadata is malformed")
        if type(data["operation_epoch"]) is not int or data["_kind"] != DIAGNOSTIC_EVIDENCE_KIND:
            raise AuditIntegrityError("Private evidence envelope kind is invalid")
        evidence = cls.from_dict(data["evidence"])
        if (
            data["session_id"] != evidence.session_id
            or evidence.session_id != expected_session_id
            or data["operation_id"] != evidence.operation_id
            or data["operation_epoch"] != evidence.operation_epoch
            or data["tool_call_id"] != evidence.tool_call_id
            or data["evidence_id"] != evidence.evidence_id
            or (expected_operation_id is not None and evidence.operation_id != expected_operation_id)
            or (expected_tool_call_id is not None and evidence.tool_call_id != expected_tool_call_id)
            or data["evidence_sha256"] != _digest(canonical_json(evidence.to_dict()))
        ):
            raise AuditIntegrityError("Private evidence correlation or integrity binding mismatch")
        return evidence


def is_diagnostic_evidence_envelope(value: object) -> bool:
    """Detect the private kind even when its remaining evidence is malformed."""
    return type(value) in (dict, MappingProxyType) and cast(Mapping[str, JsonValue], value).get("_kind") == DIAGNOSTIC_EVIDENCE_KIND


def _unavailable(reason: str, *, encoding: str = "json") -> DiagnosticPayload:
    return DiagnosticPayload("unavailable", encoding, None, None, (reason,))


def _retained(value: JsonValue, reasons: set[str]) -> DiagnosticPayload:
    content = canonical_json(value)
    try:
        _decode(content)
    except JsonBoundaryError:
        return _unavailable("bounds")
    return DiagnosticPayload("filtered" if reasons else "retained", "json", content, _digest(content), tuple(sorted(reasons)))


def _project(
    value: JsonValue,
    *,
    tool_name: str,
    additional_fields: tuple[str, ...],
    reasons: set[str],
    root: bool = False,
    path: tuple[str | int, ...] = (),
) -> JsonValue:
    """Preserve semantic JSON and remove classified leaf/credential values."""
    if type(value) is dict:
        projected: dict[str, JsonValue] = {}
        plugin = value["plugin"] if "plugin" in value else None
        sink_component = (root and tool_name == "set_output") or (
            len(path) >= 2
            and path[-2] == "outputs"
            and path[:-2]
            in {
                (),
                ("pipeline",),
                ("data",),
                ("data", "pipeline"),
                ("state",),
                ("data", "state"),
            }
        )
        plugin_fields = tuple(allowed_secret_ref_fields("sink", plugin)) if sink_component and type(plugin) is str else ()
        # Existing output patches may omit plugin identity. Conservatively
        # classify this one known declared option slot, without treating every
        # authored reference URL as a credential or propagating option metadata.
        option_fields = additional_fields if not root else ()
        if tool_name == "patch_output_options" and path == ("patch",):
            option_fields = (*option_fields, "url")
        for key, child in value.items():
            if find_credential_material(key, _POLICY) is not None:
                reasons.add("recognized_credential")
                continue
            structural = root and tool_name == "wire_secret_ref" and key == "option_key"
            if not structural and is_secret_field(key, option_fields):
                policy = CredentialTraversalPolicy(
                    additional_credential_fields=option_fields,
                    max_depth=JSON_MAX_DEPTH,
                    max_nodes=JSON_MAX_ITEMS,
                    max_width=JSON_MAX_ITEMS,
                    max_string_bytes=JSON_MAX_TOTAL_UTF8_BYTES,
                )
                if find_credential_material({key: child}, policy) is None:
                    projected[key] = child
                else:
                    projected[key] = REDACTED_CREDENTIAL_TEXT
                    reasons.add("credential_field")
            else:
                projected[key] = _project(
                    child,
                    tool_name=tool_name,
                    additional_fields=((additional_fields if root else ()) + plugin_fields)
                    if key == "options" or (root and key == "patch")
                    else (),
                    reasons=reasons,
                    path=(*path, key),
                )
        return projected
    if type(value) is list:
        return [
            _project(item, tool_name=tool_name, additional_fields=additional_fields, reasons=reasons, path=(*path, index))
            for index, item in enumerate(value)
        ]
    if type(value) is str and find_credential_material(value, _POLICY) is not None:
        reasons.add("recognized_credential")
        return REDACTED_CREDENTIAL_TEXT
    return value


def _canonical_payload(
    content: str | None, *, tool_name: str, additional_fields: tuple[str, ...], absent_result: bool = False
) -> DiagnosticPayload:
    if content is None:
        return DiagnosticPayload("not_returned", "json", None, None) if absent_result else _unavailable("not_recorded")
    try:
        value = _decode_canonical(content)
    except JsonBoundaryError:
        return _unavailable("bounds")
    except (TypeError, ValueError) as exc:
        raise AuditIntegrityError("Private evidence source canonical JSON is malformed") from exc
    if type(value) is dict and ("_unparseable_arguments" in value or "_canonicalization_error" in value):
        return _unavailable("canonical_sentinel")
    reasons: set[str] = set()
    return _retained(_project(value, tool_name=tool_name, additional_fields=additional_fields, reasons=reasons, root=True), reasons)


def diagnostic_evidence_identity(domain: str, correlation: str) -> str:
    """Keep owned evidence domains disjoint from arbitrary provider call IDs."""
    if domain not in {"tool-authoring", "tool-failure", "provider-failure", "provider-request", "planner-candidate", "operation-failure"}:
        raise AuditIntegrityError("Unknown private evidence identity domain")
    return f"{domain}:{hashlib.sha256(correlation.encode('utf-8')).hexdigest()}"


def build_private_tool_evidence(
    *,
    session_id: str,
    operation_id: str,
    operation_epoch: int,
    tool_call_id: str,
    tool_name: str,
    status: str,
    arguments_canonical: str | None,
    result_canonical: str | None,
    raw_arguments: str | None = None,
    authority_arguments_canonical: str | None = None,
    error_category: str | None = None,
    additional_credential_fields: tuple[str, ...] = (),
    evidence_id: str | None = None,
) -> ComposerDiagnosticEvidence:
    """Build filtered evidence from owned canonical authoring/result payloads.

    Callers validate existing dispatch hashes before this boundary and stamp
    correlation from their session-operation authority, never caller JSON.
    No input or credential-derived hashes are persisted by this builder.
    """
    arguments = _canonical_payload(arguments_canonical, tool_name=tool_name, additional_fields=additional_credential_fields)
    if error_category in {"wire_json_invalid", "wire_json_bounds", "canonicalization"}:
        arguments = _unavailable("canonical_sentinel")
    result = _canonical_payload(result_canonical, tool_name=tool_name, additional_fields=additional_credential_fields, absent_result=True)
    authority: DiagnosticPayload | None = None
    if authority_arguments_canonical is not None:
        if tool_name not in {"set_pipeline", "emit_pipeline_proposal"}:
            raise AuditIntegrityError("Private evidence authority belongs to a different tool")
        try:
            projection = _decode_canonical(authority_arguments_canonical)
            if type(projection) is not dict:
                raise ValueError("Invalid authority projection")
            if tool_name == "emit_pipeline_proposal":
                restored = dict(projection)
                pipeline = restored["pipeline"] if "pipeline" in restored else None
                if type(pipeline) is dict:
                    restored["pipeline"] = restore_composer_authority_payload(pipeline)
            else:
                restored = restore_composer_authority_payload(projection)
            if arguments_canonical is None or canonical_json(restored) != arguments_canonical:
                raise ValueError("Authority and arguments differ")
            reasons: set[str] = set()
            filtered = _project(restored, tool_name=tool_name, additional_fields=additional_credential_fields, reasons=reasons, root=True)
            assert type(filtered) is dict
            if tool_name == "emit_pipeline_proposal":
                filtered_pipeline = filtered["pipeline"] if "pipeline" in filtered else None
                if type(filtered_pipeline) is dict:
                    filtered["pipeline"] = project_composer_authority_payload(filtered_pipeline)
                authority = _retained(filtered, reasons)
            else:
                authority = _retained(project_composer_authority_payload(filtered), reasons)
        except JsonBoundaryError:
            authority = _unavailable("bounds")
        except (TypeError, ValueError) as exc:
            raise AuditIntegrityError("Private evidence authority projection is malformed") from exc
    wire = _unavailable("not_recorded", encoding="wire_text")
    if raw_arguments is not None:
        try:
            decoded = _decode(raw_arguments)
        except _DuplicateKeys:
            wire = _unavailable("duplicate_keys", encoding="wire_text")
        except JsonBoundaryError:
            wire = _unavailable("bounds", encoding="wire_text")
        except (TypeError, ValueError):
            wire = _unavailable("malformed_json", encoding="wire_text")
        else:
            findings: set[str] = set()
            _project(decoded, tool_name=tool_name, additional_fields=additional_credential_fields, reasons=findings, root=True)
            semantic = decoded
            if tool_name == "set_pipeline" and type(decoded) is dict and set(decoded) == {"pipeline"}:
                semantic = decoded["pipeline"]
            if findings:
                wire = DiagnosticPayload("unavailable", "wire_text", None, None, tuple(sorted(findings)))
            else:
                try:
                    wire_canonical = canonical_json(semantic)
                except CanonicalizationError:
                    wire = _unavailable("unsupported", encoding="wire_text")
                else:
                    if arguments_canonical is None or wire_canonical != arguments_canonical:
                        wire = _unavailable("semantic_mismatch", encoding="wire_text")
                    else:
                        wire = DiagnosticPayload("retained", "wire_text", raw_arguments, _digest(raw_arguments))
    return ComposerDiagnosticEvidence(
        session_id,
        operation_id,
        operation_epoch,
        tool_call_id,
        tool_name,
        status,
        diagnostic_evidence_identity("tool-authoring", tool_call_id) if evidence_id is None else evidence_id,
        arguments,
        authority,
        result,
        wire,
    )
