#!/usr/bin/env python3
"""Generate and check the reviewed first-party plugin lifecycle matrix."""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, cast, get_args

from elspeth.contracts.plugin_capabilities import WebConfigAuthority
from elspeth.core.config import load_bounded_pipeline_yaml
from elspeth.plugins.infrastructure.azure_auth import AzureAuthMethod
from elspeth.plugins.infrastructure.base import BaseSink, BaseSource, BaseTransform
from elspeth.plugins.infrastructure.clients.dataverse import DataverseAuthConfig
from elspeth.plugins.infrastructure.clients.retrieval.azure_search import (
    AzureSearchAuthMode,
    AzureSearchProviderConfig,
)
from elspeth.plugins.infrastructure.clients.retrieval.chroma import ChromaSearchProviderConfig
from elspeth.plugins.infrastructure.clients.retrieval.connection import ChromaConnectionMode, ChromaSearchMode
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.infrastructure.discovery import discover_all_plugins
from elspeth.plugins.infrastructure.preflight import plugin_preflight_mode
from elspeth.plugins.sinks.azure_blob_sink import AzureBlobSinkConfig
from elspeth.plugins.sinks.chroma_sink import ChromaSinkConfig
from elspeth.plugins.sinks.dataverse import DataverseSinkConfig
from elspeth.plugins.sources.azure_blob_source import AzureBlobSourceConfig
from elspeth.plugins.sources.dataverse import DataverseSourceConfig
from elspeth.plugins.sources.llm.source import LLMSource
from elspeth.plugins.transforms.aws.textract_config_shared import AuthMode as TextractAuthMode
from elspeth.plugins.transforms.aws.textract_document_analysis import AWSTextractDocumentAnalysisConfig
from elspeth.plugins.transforms.aws.textract_inline_analysis import AWSTextractInlineAnalysisConfig
from elspeth.plugins.transforms.llm.transform import LLMTransform
from elspeth.plugins.transforms.rag.config import RAGRetrievalConfig, RetrievalProviderName

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
V3_CATALOG_PATH = REPOSITORY_ROOT / "docs/architecture/state_engine/proof-catalog/v3/catalog.json"
UNCLASSIFIED = "UNCLASSIFIED"
EXPECTED_COUNTS = {"source": 9, "transform": 33, "sink": 9}
EXPECTED_VARIANT_COUNT = 72
ALLOWED_PB_BOUNDARIES = frozenset({"PB-01", "PB-02", "PB-04", "PB-06", "PB-07", "PB-09"})
ALLOWED_LOCAL_FIXTURES = frozenset({"hermetic", "provider-contract-fake", "real-process-http"})
ALLOWED_RELEASE_LANES = frozenset({"local", "live-aws", "live-azure", "live-chroma", "live-dataverse", "live-provider"})
_SECRET_VALIDATION_SENTINEL = "elspeth-state-engine-matrix-secret-placeholder"
MECHANICAL_FIELDS = (
    "plugin_key",
    "kind",
    "module",
    "qualname",
    "source_path",
    "determinism",
    "execution_model",
    "lifecycle_owners",
    "source_hash_present",
    "sink_effect_capability",
    "sink_effect_modes",
)
REVIEWED_FIELDS = (
    "variants",
    "external_observation_required",
    "applicable_pb_boundaries",
    "local_fixture",
    "release_lane",
)
ENTRY_FIELDS = frozenset((*MECHANICAL_FIELDS, *REVIEWED_FIELDS))

PluginType = type[BaseSource] | type[BaseTransform] | type[BaseSink]
PluginKind = Literal["source", "transform", "sink"]


def _canonical_source_path(plugin_type: type[Any]) -> str:
    source = inspect.getsourcefile(plugin_type)
    if source is None:
        raise ValueError(f"cannot resolve source file for {plugin_type.__qualname__}")
    resolved = Path(source).resolve()
    try:
        return resolved.relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError as exc:
        raise ValueError(f"plugin source is outside the repository: {resolved}") from exc


def _canonical_module(plugin_type: type[Any]) -> str:
    source_path = Path(_canonical_source_path(plugin_type))
    if source_path.parts[0] != "src" or source_path.suffix != ".py":
        raise ValueError(f"plugin source is not a Python module under src/: {source_path}")
    return ".".join(source_path.with_suffix("").parts[1:])


def _owner_identity(plugin_type: PluginType, method_name: str) -> str:
    for owner in plugin_type.__mro__:
        if method_name in owner.__dict__:
            return f"{_canonical_module(owner)}:{owner.__qualname__}"
    raise ValueError(f"{plugin_type.__qualname__} has no MRO owner for {method_name}")


def _execution_model(kind: PluginKind, plugin_type: PluginType) -> str:
    if kind == "source":
        return "source-iterator"
    if kind == "sink":
        return "sink-batch"
    transform_type = cast("type[BaseTransform]", plugin_type)
    return "batch-transform" if transform_type.is_batch_aware else "row-transform"


def _sink_effect_projection(plugin_type: PluginType) -> tuple[str | None, list[str]]:
    if not issubclass(plugin_type, BaseSink):
        return None, []
    from elspeth.contracts.sink_effects import MemberSinkEffectCapability, RestagingSinkEffectCapability

    if issubclass(plugin_type, RestagingSinkEffectCapability):
        capability = "restaging"
    elif issubclass(plugin_type, MemberSinkEffectCapability):
        capability = "member"
    else:
        capability = "contract"
    return capability, sorted(plugin_type.supported_effect_modes)


def _mechanical_projection() -> list[dict[str, object]]:
    discovered = discover_all_plugins()
    kind_specs: tuple[tuple[str, PluginKind, tuple[str, ...]], ...] = (
        ("sources", "source", ("on_start", "load", "on_complete", "close")),
        ("transforms", "transform", ("on_start", "process", "on_complete", "close")),
        ("sinks", "sink", ("on_start", "write", "flush", "on_complete", "close")),
    )
    rows: list[dict[str, object]] = []
    for discovery_kind, kind, lifecycle_methods in kind_specs:
        plugins = discovered[discovery_kind]
        if len(plugins) != EXPECTED_COUNTS[kind]:
            raise ValueError(f"expected {EXPECTED_COUNTS[kind]} {kind}s, discovered {len(plugins)}")
        for discovered_type in plugins:
            plugin_type = cast("PluginType", discovered_type)
            source_hash = plugin_type.source_file_hash
            source_hash_present = type(source_hash) is str and source_hash.startswith("sha256:") and len(source_hash) == 23
            capability, effect_modes = _sink_effect_projection(plugin_type)
            rows.append(
                {
                    "plugin_key": f"{kind}:{plugin_type.name}",
                    "kind": kind,
                    "module": _canonical_module(plugin_type),
                    "qualname": plugin_type.__qualname__,
                    "source_path": _canonical_source_path(plugin_type),
                    "determinism": plugin_type.determinism.value,
                    "execution_model": _execution_model(kind, plugin_type),
                    "lifecycle_owners": {method: _owner_identity(plugin_type, method) for method in lifecycle_methods},
                    "source_hash_present": source_hash_present,
                    "sink_effect_capability": capability,
                    "sink_effect_modes": effect_modes,
                }
            )
    rows.sort(key=lambda row: str(row["plugin_key"]))
    keys = [row["plugin_key"] for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("live plugin discovery contains duplicate plugin keys")
    if len(rows) != sum(EXPECTED_COUNTS.values()):
        raise ValueError(f"expected 51 plugins, discovered {len(rows)}")
    return rows


def _owned_literal_values(model: type[Any], field_name: str) -> tuple[str, ...]:
    annotation = model.model_fields[field_name].annotation
    values = get_args(annotation)
    if not values or any(type(value) is not str for value in values):
        raise ValueError(f"{model.__name__}.{field_name} is not a closed string Literal")
    return values


def _variant_map() -> dict[str, tuple[str, ...]]:
    source_discriminator, source_llm = LLMSource.discriminated_variants()
    transform_discriminator, transform_llm = LLMTransform.discriminated_variants()
    if source_discriminator != "provider" or transform_discriminator != "provider":
        raise ValueError("LLM variant discriminator must remain provider")
    retrieval_providers = get_args(RetrievalProviderName)
    if set(retrieval_providers) != {"azure_search", "chroma"}:
        raise ValueError("RAG provider Literal drifted from its owned registry contract")
    dataverse_modes = _owned_literal_values(DataverseAuthConfig, "method")
    return {
        "source:azure_blob": tuple(get_args(AzureAuthMethod)),
        "source:dataverse": dataverse_modes,
        "source:llm": tuple(source_llm),
        "transform:aws_textract_document_analysis": tuple(get_args(TextractAuthMode)),
        "transform:aws_textract_inline_analysis": tuple(get_args(TextractAuthMode)),
        "transform:llm": tuple(transform_llm),
        "transform:rag_retrieval": (
            *(f"azure-search-{mode.replace('_', '-')}" for mode in get_args(AzureSearchAuthMode)),
            *(f"chroma-{mode}" for mode in get_args(ChromaSearchMode)),
        ),
        "sink:azure_blob": tuple(get_args(AzureAuthMethod)),
        "sink:chroma_sink": tuple(get_args(ChromaConnectionMode)),
        "sink:dataverse": dataverse_modes,
    }


def _azure_auth_fields(mode: str) -> dict[str, object]:
    if mode == "connection_string":
        return {"auth_mode": mode, "connection_string": "matrix-connection"}
    if mode == "sas_token":
        return {"auth_mode": mode, "sas_token": "matrix-sas", "account_url": "https://matrix.blob.core.windows.net"}
    if mode == "managed_identity":
        return {"auth_mode": mode, "use_managed_identity": True, "account_url": "https://matrix.blob.core.windows.net"}
    if mode == "service_principal":
        return {
            "auth_mode": mode,
            "tenant_id": "matrix-tenant",
            "client_id": "matrix-client",
            "client_secret": "matrix-secret",
            "account_url": "https://matrix.blob.core.windows.net",
        }
    raise ValueError(f"unsupported Azure auth mode {mode!r}")


def _llm_config(provider: str, *, source: bool) -> dict[str, object]:
    config: dict[str, object] = {
        "provider": provider,
        "prompt_template": "Summarise the audit topic.",
        "schema": {"mode": "observed"},
    }
    if source:
        config["on_validation_failure"] = "discard"
    if provider == "azure":
        config.update(
            deployment_name="matrix-deployment",
            endpoint="https://matrix.openai.azure.com",
            api_key="matrix-key",
        )
    elif provider == "openrouter":
        config.update(model="openai/gpt-4o", api_key="matrix-key")
    elif provider == "bedrock":
        config["model"] = "bedrock/anthropic.claude-3-haiku"
    elif provider == "gateway":
        config.update(model="matrix-model", endpoint="https://gateway.example/v1", api_key="matrix-key")
    else:
        raise ValueError(f"unsupported LLM provider {provider!r}")
    return config


def _dataverse_auth(mode: str) -> dict[str, object]:
    if mode == "managed_identity":
        return {"method": mode}
    if mode == "service_principal":
        return {"method": mode, "tenant_id": "matrix-tenant", "client_id": "matrix-client", "client_secret": "matrix-secret"}
    raise ValueError(f"unsupported Dataverse auth mode {mode!r}")


def _textract_auth(mode: str) -> dict[str, object]:
    if mode == "default_chain":
        return {"auth_mode": mode}
    if mode == "secret_refs":
        return {"auth_mode": mode, "aws_access_key_id": "matrix-access", "aws_secret_access_key": "matrix-secret"}
    raise ValueError(f"unsupported Textract auth mode {mode!r}")


def _find_declaring_plugin_nodes(value: object, *, plugin_name: str) -> list[Mapping[str, Any]]:
    nodes: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        if value.get("plugin") == plugin_name:
            nodes.append(cast("Mapping[str, Any]", value))
        for child in value.values():
            nodes.extend(_find_declaring_plugin_nodes(child, plugin_name=plugin_name))
    elif isinstance(value, list):
        for child in value:
            nodes.extend(_find_declaring_plugin_nodes(child, plugin_name=plugin_name))
    return nodes


def _replace_secret_refs_for_validation(value: object) -> object:
    if isinstance(value, Mapping):
        if set(value) == {"secret_ref"} and isinstance(value["secret_ref"], str) and value["secret_ref"].strip():
            return _SECRET_VALIDATION_SENTINEL
        return {key: _replace_secret_refs_for_validation(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_replace_secret_refs_for_validation(child) for child in value]
    return value


def _live_plugin_types_by_key() -> dict[str, PluginType]:
    discovered = discover_all_plugins()
    plugin_types: dict[str, PluginType] = {}
    for discovery_kind, kind in (("sources", "source"), ("transforms", "transform"), ("sinks", "sink")):
        for discovered_type in discovered[discovery_kind]:
            plugin_type = cast("PluginType", discovered_type)
            key = f"{kind}:{plugin_type.name}"
            if key in plugin_types:
                raise ValueError(f"live plugin discovery contains duplicate plugin key {key!r}")
            plugin_types[key] = plugin_type
    if len(plugin_types) != sum(EXPECTED_COUNTS.values()):
        raise ValueError(f"expected 51 plugins, discovered {len(plugin_types)}")
    return plugin_types


def _validate_default_config(plugin_key: str, plugin_type: PluginType) -> None:
    example = plugin_type.example_use
    if not isinstance(example, str) or not example.strip():
        raise ValueError(f"{plugin_key} example_use must be a nonblank YAML fragment")
    parsed = load_bounded_pipeline_yaml(example)
    declaring_nodes = _find_declaring_plugin_nodes(parsed, plugin_name=plugin_type.name)
    if len(declaring_nodes) != 1:
        raise ValueError(f"{plugin_key} example_use must declare plugin {plugin_type.name!r} exactly once; found {len(declaring_nodes)}")
    options = declaring_nodes[0].get("options", {})
    if not isinstance(options, Mapping):
        raise ValueError(f"{plugin_key} example_use options must be a mapping")
    if plugin_type.web_config_authority is WebConfigAuthority.OPERATOR_PROFILED and "profile" in options:
        if issubclass(plugin_type, BaseTransform):
            resolved: object = plugin_type.probe_config()
        elif issubclass(plugin_type, BaseSink):
            resolved = plugin_type.probe_config()
        else:
            raise ValueError(f"{plugin_key} has a profiled example but no production probe seam")
    else:
        resolved = _replace_secret_refs_for_validation(options)
    if not isinstance(resolved, dict):
        raise ValueError(f"{plugin_key} resolved example options must be a dictionary")
    config_model = plugin_type.get_config_model(resolved)
    if config_model is None:
        if resolved:
            raise ValueError(f"{plugin_key} supplies example options but has no config model")
        plugin_type(resolved)
        return
    config_model.from_dict(resolved, plugin_name=plugin_type.name)


def _validate_variant_configs(variants: Mapping[str, tuple[str, ...]]) -> set[tuple[str, str]]:
    validated_subjects: set[tuple[str, str]] = set()
    schema = {"mode": "observed"}
    with plugin_preflight_mode(True):
        for mode in variants["source:azure_blob"]:
            AzureBlobSourceConfig.from_dict(
                {
                    **_azure_auth_fields(mode),
                    "container": "matrix-container",
                    "blob_path": "input.jsonl",
                    "format": "jsonl",
                    "schema": schema,
                    "on_validation_failure": "discard",
                }
            )
            validated_subjects.add(("source:azure_blob", mode))
        for mode in variants["sink:azure_blob"]:
            AzureBlobSinkConfig.from_dict(
                {
                    **_azure_auth_fields(mode),
                    "container": "matrix-container",
                    "blob_path": "output.jsonl",
                    "format": "jsonl",
                    "schema": schema,
                }
            )
            validated_subjects.add(("sink:azure_blob", mode))
        for provider, config_model in LLMSource.discriminated_variants()[1].items():
            config_model.model_validate(_llm_config(provider, source=True))
            validated_subjects.add(("source:llm", provider))
        for provider, config_model in LLMTransform.discriminated_variants()[1].items():
            config_model.model_validate(_llm_config(provider, source=False))
            validated_subjects.add(("transform:llm", provider))
        for mode in variants["source:dataverse"]:
            DataverseSourceConfig.from_dict(
                {
                    "environment_url": "https://matrix.crm.dynamics.com",
                    "auth": _dataverse_auth(mode),
                    "entity": "contacts",
                    "schema": schema,
                    "on_validation_failure": "discard",
                }
            )
            validated_subjects.add(("source:dataverse", mode))
        for mode in variants["sink:dataverse"]:
            DataverseSinkConfig.from_dict(
                {
                    "environment_url": "https://matrix.crm.dynamics.com",
                    "auth": _dataverse_auth(mode),
                    "entity": "contacts",
                    "field_mapping": {"id": "contactid"},
                    "alternate_key": "contactid",
                    "schema": schema,
                }
            )
            validated_subjects.add(("sink:dataverse", mode))
        for mode in variants["transform:aws_textract_document_analysis"]:
            AWSTextractDocumentAnalysisConfig.from_dict(
                {
                    **_textract_auth(mode),
                    "region": "us-east-1",
                    "bucket": "matrix-bucket",
                    "key_field": "object_key",
                    "feature_types": ["TABLES"],
                    "text_field": "text",
                    "schema": schema,
                }
            )
            validated_subjects.add(("transform:aws_textract_document_analysis", mode))
        for mode in variants["transform:aws_textract_inline_analysis"]:
            AWSTextractInlineAnalysisConfig.from_dict(
                {
                    **_textract_auth(mode),
                    "region": "us-east-1",
                    "document_format": "png",
                    "feature_types": ["TABLES"],
                    "text_field": "text",
                    "schema": schema,
                }
            )
            validated_subjects.add(("transform:aws_textract_inline_analysis", mode))
        for mode in get_args(AzureSearchAuthMode):
            provider_config: dict[str, object] = {
                "endpoint": "https://matrix.search.windows.net",
                "index": "matrix-index",
                "auth_mode": mode,
            }
            if mode == "api_key":
                provider_config["api_key"] = "matrix-key"
            else:
                provider_config["use_managed_identity"] = True
            AzureSearchProviderConfig.model_validate(provider_config)
            RAGRetrievalConfig.from_dict(
                {
                    "output_prefix": "retrieval",
                    "query_field": "query",
                    "provider": "azure_search",
                    "provider_config": provider_config,
                    "schema": schema,
                }
            )
            validated_subjects.add(("transform:rag_retrieval", f"azure-search-{mode.replace('_', '-')}"))
        for mode in get_args(ChromaSearchMode):
            provider_config = {"collection": "matrix-collection", "mode": mode}
            if mode == "persistent":
                provider_config["persist_directory"] = ".state-engine-chroma"
            elif mode == "client":
                provider_config.update(host="localhost", ssl=False)
            ChromaSearchProviderConfig.model_validate(provider_config)
            RAGRetrievalConfig.from_dict(
                {
                    "output_prefix": "retrieval",
                    "query_field": "query",
                    "provider": "chroma",
                    "provider_config": provider_config,
                    "schema": schema,
                }
            )
            validated_subjects.add(("transform:rag_retrieval", f"chroma-{mode}"))
        for mode in variants["sink:chroma_sink"]:
            config: dict[str, object] = {
                "collection": "matrix-collection",
                "mode": mode,
                "field_mapping": {"document_field": "text", "id_field": "id"},
                "schema": schema,
            }
            if mode == "persistent":
                config["persist_directory"] = ".state-engine-chroma"
            else:
                config.update(host="localhost", ssl=False)
            ChromaSinkConfig.from_dict(config)
            validated_subjects.add(("sink:chroma_sink", mode))
        for plugin_key, plugin_type in _live_plugin_types_by_key().items():
            if plugin_key in variants:
                continue
            try:
                _validate_default_config(plugin_key, plugin_type)
            except (PluginConfigError, ValueError) as exc:
                raise ValueError(f"{plugin_key} default config validation failed: {exc}") from exc
            validated_subjects.add((plugin_key, "default"))
    return validated_subjects


def _contains_unclassified(value: object) -> bool:
    if value == UNCLASSIFIED:
        return True
    if isinstance(value, dict):
        return any(_contains_unclassified(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_unclassified(item) for item in value)
    return False


def _load_matrix(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("plugin matrix must be a JSON object")
    return value


def _validate_reviewed_boundary_references() -> None:
    catalog = json.loads(V3_CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict) or not isinstance(catalog.get("legs"), list):
        raise ValueError("v3 proof catalog must contain a legs list")
    catalog_ids = {leg["id"] for leg in catalog["legs"] if isinstance(leg, dict) and type(leg.get("id")) is str}
    missing = sorted(ALLOWED_PB_BOUNDARIES - catalog_ids)
    if missing:
        raise ValueError(f"reviewed applicable_pb_boundaries are absent from the v3 proof catalog: {missing}")


def _validate_reviewed_entry(entry: Mapping[str, object]) -> None:
    key = entry["plugin_key"]
    if set(entry) != ENTRY_FIELDS:
        raise ValueError(f"matrix entry {key!r} has an invalid field set")
    variants = entry["variants"]
    if not isinstance(variants, list) or not variants or any(type(item) is not str or not item for item in variants):
        raise ValueError(f"matrix entry {key!r} variants must be a non-empty string list")
    if type(entry["external_observation_required"]) is not bool:
        raise ValueError(f"matrix entry {key!r} external_observation_required must be boolean")
    boundaries = entry["applicable_pb_boundaries"]
    if not isinstance(boundaries, list) or not boundaries or any(type(item) is not str or not item for item in boundaries):
        raise ValueError(f"matrix entry {key!r} applicable_pb_boundaries must be a non-empty string list")
    if len(boundaries) != len(set(boundaries)) or not set(boundaries).issubset(ALLOWED_PB_BOUNDARIES):
        raise ValueError(f"matrix entry {key!r} applicable_pb_boundaries must be unique members of {sorted(ALLOWED_PB_BOUNDARIES)}")
    if entry["local_fixture"] not in ALLOWED_LOCAL_FIXTURES:
        raise ValueError(f"matrix entry {key!r} local_fixture must be one of {sorted(ALLOWED_LOCAL_FIXTURES)}")
    if entry["release_lane"] not in ALLOWED_RELEASE_LANES:
        raise ValueError(f"matrix entry {key!r} release_lane must be one of {sorted(ALLOWED_RELEASE_LANES)}")


def check(path: Path) -> None:
    matrix = _load_matrix(path)
    if set(matrix) != {"schema_version", "plugins"} or matrix["schema_version"] != 1:
        raise ValueError("plugin matrix must have exact schema_version=1 and plugins fields")
    entries = matrix["plugins"]
    if not isinstance(entries, list):
        raise ValueError("plugin matrix plugins must be a list")
    if _contains_unclassified(matrix):
        raise ValueError("plugin matrix contains UNCLASSIFIED reviewed fields")
    _validate_reviewed_boundary_references()
    mechanical = _mechanical_projection()
    if len(entries) != len(mechanical):
        raise ValueError(f"plugin matrix has {len(entries)} entries; expected {len(mechanical)}")
    expected_by_key = {str(entry["plugin_key"]): entry for entry in mechanical}
    actual_by_key: dict[str, Mapping[str, object]] = {}
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            raise ValueError("plugin matrix entries must be JSON objects")
        _validate_reviewed_entry(raw_entry)
        key = raw_entry["plugin_key"]
        if type(key) is not str or key in actual_by_key:
            raise ValueError(f"plugin matrix has invalid or duplicate plugin key {key!r}")
        actual_by_key[key] = raw_entry
    if set(actual_by_key) != set(expected_by_key):
        raise ValueError("plugin matrix keys do not match live discovery")
    for key, expected in expected_by_key.items():
        actual = actual_by_key[key]
        for field in MECHANICAL_FIELDS:
            if actual[field] != expected[field]:
                raise ValueError(f"plugin matrix mechanical field drift: {key}.{field}")
    variants = _variant_map()
    if not set(variants).issubset(actual_by_key):
        raise ValueError("variant registry contains a plugin absent from live discovery")
    variant_count = sum(len(variants.get(key, ("default",))) for key in actual_by_key)
    if variant_count != EXPECTED_VARIANT_COUNT:
        raise ValueError(f"expected {EXPECTED_VARIANT_COUNT} PB-09 variants, derived {variant_count}")
    expected_subjects = {(plugin_key, variant) for plugin_key in actual_by_key for variant in variants.get(plugin_key, ("default",))}
    validated_subjects = _validate_variant_configs(variants)
    if validated_subjects != expected_subjects:
        missing = sorted(expected_subjects - validated_subjects)
        unexpected = sorted(validated_subjects - expected_subjects)
        raise ValueError(f"production config validation subject drift: missing={missing}, unexpected={unexpected}")
    for key, entry in actual_by_key.items():
        expected_variants = list(variants.get(key, ("default",)))
        if entry["variants"] != expected_variants:
            raise ValueError(f"plugin matrix reviewed variants drift from owned production discriminators: {key}")
    if not all(entry["source_hash_present"] is True for entry in entries):
        raise ValueError("every first-party plugin must publish a source hash")


def render_skeleton(path: Path) -> bool:
    existing_entries: dict[str, Mapping[str, object]] = {}
    if path.exists():
        existing = _load_matrix(path)
        raw_entries = existing.get("plugins", [])
        if not isinstance(raw_entries, list):
            raise ValueError("plugin matrix plugins must be a list")
        for raw_entry in raw_entries:
            if isinstance(raw_entry, dict) and type(raw_entry.get("plugin_key")) is str:
                existing_entries[raw_entry["plugin_key"]] = raw_entry
    rendered: list[dict[str, object]] = []
    for mechanical in _mechanical_projection():
        key = str(mechanical["plugin_key"])
        old = existing_entries.get(key, {})
        row = dict(mechanical)
        for field in REVIEWED_FIELDS:
            row[field] = old[field] if field in old else UNCLASSIFIED
        rendered.append(row)
    document = {"schema_version": 1, "plugins": rendered}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return _contains_unclassified(document)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "render-skeleton"):
        command = subparsers.add_parser(name)
        command.add_argument("golden", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            check(args.golden)
            print(f"state-engine plugin matrix: valid (51 plugins, {EXPECTED_VARIANT_COUNT} PB-09 variants)")
            return 0
        has_unclassified = render_skeleton(args.golden)
        if has_unclassified:
            print("state-engine plugin matrix skeleton contains UNCLASSIFIED reviewed fields", file=sys.stderr)
            return 1
        check(args.golden)
        print("state-engine plugin matrix skeleton: complete")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"state-engine plugin matrix: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
