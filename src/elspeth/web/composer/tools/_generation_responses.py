"""Producer-owned generation response shapes, before disclosure policy."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Literal, Self, cast

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    ModelWrapValidatorHandler,
    PlainSerializer,
    PlainValidator,
    PrivateAttr,
    SerializerFunctionWrapHandler,
    ValidationError,
    ValidationInfo,
    model_serializer,
    model_validator,
)

from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.plugin_assistance import PluginAssistanceExample
from elspeth.web.composer._response_json import encode_response_json, parse_frozen_response_json, parse_response_json
from elspeth.web.composer.response_contracts import SelectedResponseContract
from elspeth.web.composer.state import NodeType


def _sequence(value: object, info: ValidationInfo) -> tuple[object, ...]:
    if info.context == "owned-response" and type(value) is not tuple:
        raise FrameworkBugError("Corrupt owned response sequence")
    if type(value) not in (list, tuple):
        raise FrameworkBugError("Expected response sequence")
    return tuple(cast(list[object] | tuple[object, ...], value))


def _mapping(value: object) -> dict[str, object]:
    if type(value) not in (dict, MappingProxyType):
        raise FrameworkBugError("Expected response mapping")
    mapping = cast(Mapping[object, object], value)
    if any(type(key) is not str for key in mapping):
        raise FrameworkBugError("Expected response mapping")
    return {cast(str, key): item for key, item in mapping.items()}


type Strings = Annotated[tuple[str, ...], BeforeValidator(_sequence)]
type Integers = Annotated[tuple[int, ...], BeforeValidator(_sequence)]


class _ResponseRecord(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, revalidate_instances="always", populate_by_name=False)
    _wire_order: tuple[str, ...] = PrivateAttr(default=())

    @model_validator(mode="wrap")
    @classmethod
    def _admit_record(cls, value: object, handler: ModelWrapValidatorHandler[Self], info: ValidationInfo) -> Self:
        if type(value) is cls:
            order = value._wire_order
        else:
            if info.context == "owned-response":
                raise FrameworkBugError("Corrupt owned response record")
            value = _mapping(value)
            order = tuple(value)
        admitted = handler(value)
        # Order is serialization metadata only: the selected model still admits
        # every field, and only fields actually supplied may enter the wire.
        fields = {cls.model_fields[name].alias or name for name in admitted.model_fields_set}
        if set(order) != fields or len(order) != len(fields):
            raise FrameworkBugError("Invalid response field order")
        admitted._wire_order = order
        return admitted

    @model_serializer(mode="wrap")
    def _encode_record(self, handler: SerializerFunctionWrapHandler) -> dict[str, JsonValue]:
        encoded = handler(self)
        return {key: encoded[key] for key in self._wire_order}


class ChangeSet(_ResponseRecord):
    added: Strings
    removed: Strings
    modified: Strings


class DiffResponse(_ResponseRecord):
    from_version: int
    sources_changed: bool
    metadata_changed: bool
    nodes: ChangeSet
    edges: ChangeSet
    outputs: ChangeSet
    sources: ChangeSet | None = None
    warnings_introduced: Strings
    warnings_resolved: Strings
    total_changes: int

    @model_validator(mode="after")
    def _sources_presence(self) -> Self:
        if ("sources" in self.model_fields_set) != self.sources_changed or ("sources" in self.model_fields_set and self.sources is None):
            raise FrameworkBugError("Invalid source diff presence")
        return self


class ExplanationResponse(_ResponseRecord):
    error_text: str
    explanation: str
    suggested_fix: str
    error_code: str | None = None

    @model_validator(mode="after")
    def _code_presence(self) -> Self:
        if "error_code" in self.model_fields_set and self.error_code is None:
            raise FrameworkBugError("Explicit null explanation code")
        return self


def _example(value: object, info: ValidationInfo) -> PluginAssistanceExample:
    title: object
    before: object
    after: object
    if type(value) is PluginAssistanceExample:
        title, before, after = value.title, value.before, value.after
        parse_frozen_response_json(before)
        parse_frozen_response_json(after)
    else:
        if info.context == "owned-response":
            raise FrameworkBugError("Corrupt owned assistance example")
        raw = _mapping(value)
        if raw.keys() != {"title", "before", "after"}:
            raise FrameworkBugError("Malformed assistance example")
        title, before, after = raw["title"], raw["before"], raw["after"]
    if type(title) is not str:
        raise FrameworkBugError("Malformed assistance title")
    parsed_before = None if before is None else {key: parse_response_json(item) for key, item in _mapping(before).items()}
    parsed_after = None if after is None else {key: parse_response_json(item) for key, item in _mapping(after).items()}
    return PluginAssistanceExample(title=title, before=parsed_before, after=parsed_after)


def _encode_example(value: PluginAssistanceExample) -> JsonValue:
    return {
        "title": value.title,
        "before": encode_response_json(parse_frozen_response_json(value.before)),
        "after": encode_response_json(parse_frozen_response_json(value.after)),
    }


type AssistanceExample = Annotated[PluginAssistanceExample, PlainValidator(_example), PlainSerializer(_encode_example)]


class AssistanceResponse(_ResponseRecord):
    plugin_type: Literal["source", "transform", "sink"]
    plugin_name: str
    issue_code: str | None
    summary: str | None
    suggested_fixes: Strings
    examples: Annotated[tuple[AssistanceExample, ...], BeforeValidator(_sequence)]
    composer_hints: Strings


class ModelListResponse(_ResponseRecord):
    models: Strings
    count: int = Field(ge=0)
    truncated: bool


class ModelProvidersResponse(_ResponseRecord):
    providers: Annotated[dict[str, int], BeforeValidator(_mapping)]
    total_models: int = Field(ge=0)
    hint: str


class PreviewError(_ResponseRecord):
    component: Literal["pipeline"]
    message: str
    severity: Literal["high"]
    error_code: Literal["runtime_preflight_not_run"]


class PreviewEdgeContract(_ResponseRecord):
    from_id: str = Field(alias="from")
    to_id: str = Field(alias="to")
    producer_guarantees: Strings
    consumer_requires: Strings
    missing_fields: Strings
    satisfied: bool


class ProofEvidence(_ResponseRecord):
    source: Literal["blob", "pipeline"]
    blob_id: str | None = None
    source_name: str | None = None
    node_id: str | None = None
    output_name: str | None = None
    node_type: str | None = None
    plugin: str | None = None
    field: str | None = None
    fields: Strings | None = None
    declared_type: str | None = None
    observed_type: str | None = None
    inferred_sample_type: str | None = None
    source_runtime_type: str | None = None
    source_schema_mode: str | None = None
    source_plugin: str | None = None
    error_class: str | None = None
    observed_header_count: int | None = None
    observed_headers_redacted: bool | None = None
    declared_required_fields: Strings | None = None
    missing_column_count: int | None = None
    observed_column_count: int | None = None
    observed_columns_redacted: bool | None = None
    sample_row_index: int | None = None
    url_candidates: Strings | None = None
    duplicate_header_class_count: int | None = None
    duplicate_header_column_count: int | None = None
    duplicate_header_positions: Integers | None = None
    header_values_redacted: bool | None = None
    blob_source_count: int | None = None
    max_blob_sources: int | None = None

    @model_validator(mode="after")
    def _evidence_presence(self) -> Self:
        if self.source == "pipeline":
            if self.model_fields_set != {"source", "blob_source_count", "max_blob_sources"}:
                raise FrameworkBugError("Invalid pipeline proof evidence")
        elif self.blob_id is None or self.source_name is None:
            raise FrameworkBugError("Missing blob proof attribution")
        for name in self.model_fields_set - {"field", "plugin"}:
            if self.__dict__[name] is None:
                raise FrameworkBugError("Unexpected null proof evidence")
        return self


class ProofDiagnostic(_ResponseRecord):
    code: str
    severity: Literal["blocking", "info"]
    message: str
    suggested_repair: str | None
    evidence_locator: ProofEvidence


class PreviewSource(_ResponseRecord):
    plugin: str
    on_success: str | None
    has_schema_config: bool


class PreviewNode(_ResponseRecord):
    id: str
    node_type: NodeType
    plugin: str | None


class PreviewOutput(_ResponseRecord):
    name: str
    plugin: str


class StructuralCheck(_ResponseRecord):
    name: str
    detail: str
    outcome_code: str | None
    affected_nodes: Strings


class StructuralError(_ResponseRecord):
    component_id: str | None
    component_type: str | None
    message: str
    suggestion: str | None
    error_code: str | None


class StructuralPreview(_ResponseRecord):
    is_valid: bool
    confidence: Literal["equivalent", "provisional"]
    masking_applied: bool
    failing_checks: Annotated[tuple[StructuralCheck, ...], BeforeValidator(_sequence)]
    errors: Annotated[tuple[StructuralError, ...], BeforeValidator(_sequence)]
    note: str


class PreviewResponse(_ResponseRecord):
    preview_is_valid: bool
    preview_errors: Annotated[tuple[PreviewError, ...], BeforeValidator(_sequence)]
    edge_contracts: Annotated[tuple[PreviewEdgeContract, ...], BeforeValidator(_sequence)]
    proof_diagnostics: Annotated[tuple[ProofDiagnostic, ...], BeforeValidator(_sequence)]
    sources: Annotated[dict[str, PreviewSource], BeforeValidator(_mapping)]
    node_count: int = Field(ge=0)
    output_count: int = Field(ge=0)
    nodes: Annotated[tuple[PreviewNode, ...], BeforeValidator(_sequence)]
    outputs: Annotated[tuple[PreviewOutput, ...], BeforeValidator(_sequence)]
    structural_preview: StructuralPreview | None = None

    @model_validator(mode="after")
    def _structural_presence(self) -> Self:
        if "structural_preview" in self.model_fields_set and self.structural_preview is None:
            raise FrameworkBugError("Explicit null structural preview")
        return self


def _admit[ResponseT: _ResponseRecord](model: type[ResponseT], value: object) -> ResponseT:
    try:
        return model.model_validate(value, context="owned-response" if type(value) is model else None)
    except (ValidationError, ValueError, TypeError) as exc:
        raise FrameworkBugError("Malformed generation discovery response") from exc


def _encode(value: _ResponseRecord) -> JsonValue:
    return cast(JsonValue, value.model_dump(mode="json", by_alias=True))


def _admit_diff(value: object) -> DiffResponse:
    return _admit(DiffResponse, value)


def _admit_explanation(value: object) -> ExplanationResponse:
    return _admit(ExplanationResponse, value)


def _admit_assistance(value: object) -> AssistanceResponse:
    return _admit(AssistanceResponse, value)


def _admit_models(value: object) -> ModelListResponse | ModelProvidersResponse:
    if type(value) is ModelListResponse or (isinstance(value, Mapping) and "models" in value):
        return _admit(ModelListResponse, value)
    return _admit(ModelProvidersResponse, value)


def _admit_preview(value: object) -> PreviewResponse:
    return _admit(PreviewResponse, value)


DIFF_PIPELINE_RESPONSE_CONTRACT: SelectedResponseContract[DiffResponse] = SelectedResponseContract(_admit_diff, _encode)
EXPLAIN_VALIDATION_ERROR_RESPONSE_CONTRACT: SelectedResponseContract[ExplanationResponse] = SelectedResponseContract(
    _admit_explanation, _encode
)
PLUGIN_ASSISTANCE_RESPONSE_CONTRACT: SelectedResponseContract[AssistanceResponse] = SelectedResponseContract(_admit_assistance, _encode)
LIST_MODELS_RESPONSE_CONTRACT: SelectedResponseContract[ModelListResponse | ModelProvidersResponse] = SelectedResponseContract(
    _admit_models, _encode
)
PREVIEW_PIPELINE_RESPONSE_CONTRACT: SelectedResponseContract[PreviewResponse] = SelectedResponseContract(_admit_preview, _encode)
