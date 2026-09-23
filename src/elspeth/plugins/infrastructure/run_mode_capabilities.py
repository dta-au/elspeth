"""Preconstruction plugin admission for replay and verify runs.

The name check runs before registry discovery. The class check then compares
the registered class with the built-in class object, so a third-party plugin
cannot gain replay authority by using a built-in name.
"""

from __future__ import annotations

from collections.abc import Collection
from importlib import import_module
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from elspeth.contracts.errors import OrchestrationInvariantError

if TYPE_CHECKING:
    from elspeth.core.config import ElspethSettings
    from elspeth.engine.orchestrator.types import PipelineConfig
    from elspeth.plugins.infrastructure.manager import PluginManager


# These identities are a capability inventory, not a plugin discovery list.
# A new plugin remains unsupported until its effects and mode behavior are
# reviewed and this inventory is deliberately extended.
_SOURCE_CLASSES = {
    "aws_s3": ("elspeth.plugins.sources.aws_s3_source", "AWSS3Source"),
    "azure_blob": ("elspeth.plugins.sources.azure_blob_source", "AzureBlobSource"),
    "blob_rows": ("elspeth.plugins.sources.blob_rows", "BlobRowsSource"),
    "csv": ("elspeth.plugins.sources.csv_source", "CSVSource"),
    "dataverse": ("elspeth.plugins.sources.dataverse", "DataverseSource"),
    "json": ("elspeth.plugins.sources.json_source", "JSONSource"),
    "llm": ("elspeth.plugins.sources.llm.source", "LLMSource"),
    "null": ("elspeth.plugins.sources.null_source", "NullSource"),
    "text": ("elspeth.plugins.sources.text_source", "TextSource"),
}

_TRANSFORM_CLASSES = {
    "aws_bedrock_content_safety": ("elspeth.plugins.transforms.aws.bedrock_content_safety", "AWSBedrockContentSafety"),
    "aws_bedrock_prompt_shield": ("elspeth.plugins.transforms.aws.bedrock_prompt_shield", "AWSBedrockPromptShield"),
    "aws_textract_document_analysis": ("elspeth.plugins.transforms.aws.textract_document_analysis", "AWSTextractDocumentAnalysis"),
    "aws_textract_inline_analysis": ("elspeth.plugins.transforms.aws.textract_inline_analysis", "AWSTextractInlineAnalysis"),
    "azure_ai_search": ("elspeth.plugins.transforms.azure.ai_search", "AzureAISearchTransform"),
    "azure_content_safety": ("elspeth.plugins.transforms.azure.content_safety", "AzureContentSafety"),
    "azure_document_intelligence": ("elspeth.plugins.transforms.azure.document_intelligence", "AzureDocumentIntelligence"),
    "azure_prompt_shield": ("elspeth.plugins.transforms.azure.prompt_shield", "AzurePromptShield"),
    "batch_classifier_metrics": ("elspeth.plugins.transforms.batch_classifier_metrics", "BatchClassifierMetrics"),
    "batch_data_quality_report": ("elspeth.plugins.transforms.batch_data_quality_report", "BatchDataQualityReport"),
    "batch_distribution_profile": ("elspeth.plugins.transforms.batch_distribution_profile", "BatchDistributionProfile"),
    "batch_drift_compare": ("elspeth.plugins.transforms.batch_drift_compare", "BatchDriftCompare"),
    "batch_effect_size": ("elspeth.plugins.transforms.batch_effect_size", "BatchEffectSize"),
    "batch_experiment_compare": ("elspeth.plugins.transforms.batch_experiment_compare", "BatchExperimentCompare"),
    "batch_outlier_annotator": ("elspeth.plugins.transforms.batch_outlier_annotator", "BatchOutlierAnnotator"),
    "batch_paired_preference": ("elspeth.plugins.transforms.batch_paired_preference", "BatchPairedPreference"),
    "batch_replicate": ("elspeth.plugins.transforms.batch_replicate", "BatchReplicate"),
    "batch_stats": ("elspeth.plugins.transforms.batch_stats", "BatchStats"),
    "batch_threshold_summary": ("elspeth.plugins.transforms.batch_threshold_summary", "BatchThresholdSummary"),
    "batch_top_k": ("elspeth.plugins.transforms.batch_top_k", "BatchTopK"),
    "blob_csv_expand": ("elspeth.plugins.transforms.blob_csv_expand", "BlobCSVExpand"),
    "blob_fetch": ("elspeth.plugins.transforms.blob_fetch", "BlobFetch"),
    "blob_json_expand": ("elspeth.plugins.transforms.blob_json_expand", "BlobJSONExpand"),
    "blob_text_expand": ("elspeth.plugins.transforms.blob_text_expand", "BlobTextExpand"),
    "field_mapper": ("elspeth.plugins.transforms.field_mapper", "FieldMapper"),
    "json_explode": ("elspeth.plugins.transforms.json_explode", "JSONExplode"),
    "keyword_filter": ("elspeth.plugins.transforms.keyword_filter", "KeywordFilter"),
    "line_explode": ("elspeth.plugins.transforms.line_explode", "LineExplode"),
    "llm": ("elspeth.plugins.transforms.llm.transform", "LLMTransform"),
    "passthrough": ("elspeth.plugins.transforms.passthrough", "PassThrough"),
    "pdf_rasterize": ("elspeth.plugins.transforms.pdf_rasterize", "PDFRasterize"),
    "rag_retrieval": ("elspeth.plugins.transforms.rag.transform", "RAGRetrievalTransform"),
    "reference_join": ("elspeth.plugins.transforms.reference_join", "ReferenceJoin"),
    "report_assemble": ("elspeth.plugins.transforms.report_assemble", "ReportAssemble"),
    "truncate": ("elspeth.plugins.transforms.truncate", "Truncate"),
    "type_coerce": ("elspeth.plugins.transforms.type_coerce", "TypeCoerce"),
    "value_transform": ("elspeth.plugins.transforms.value_transform", "ValueTransform"),
    "web_scrape": ("elspeth.plugins.transforms.web_scrape", "WebScrapeTransform"),
}


class _NoTracingConfig(BaseModel):
    """The only tracing shape a non-live LLM may construct."""

    model_config = ConfigDict(extra="forbid", strict=True)

    provider: Literal["none"] = "none"


class _NonliveLLMOptions(BaseModel):
    """Parse the startup-relevant part of raw LLM options."""

    model_config = ConfigDict(extra="ignore", strict=True)

    tracing: _NoTracingConfig | None = None


class _RawPluginRef(BaseModel):
    """Owned projection of a plugin entry from untrusted YAML."""

    model_config = ConfigDict(extra="ignore", strict=True)

    plugin: str
    options: object = Field(default_factory=dict)


class _RawPluginSections(BaseModel):
    """Parse the five plugin-bearing sections before built-in imports."""

    model_config = ConfigDict(extra="ignore", strict=True)

    sources: dict[str, _RawPluginRef] = Field(default_factory=dict)
    transforms: list[_RawPluginRef] = Field(default_factory=list)
    aggregations: list[_RawPluginRef] = Field(default_factory=list)
    collectors: list[_RawPluginRef] = Field(default_factory=list)
    sinks: dict[str, _RawPluginRef] = Field(default_factory=dict)


def _admit_nonlive_llm_tracing(options: object) -> None:
    """Refuse tracer construction before a non-live LLM plugin is imported."""
    try:
        _NonliveLLMOptions.model_validate(options)
    except ValidationError as exc:
        raise OrchestrationInvariantError("Replay/verify LLM tracing is unsupported or options are malformed") from exc


_SINK_CLASSES = {
    "aws_s3": ("elspeth.plugins.sinks.aws_s3_sink", "AWSS3Sink"),
    "azure_blob": ("elspeth.plugins.sinks.azure_blob_sink", "AzureBlobSink"),
    "chroma_sink": ("elspeth.plugins.sinks.chroma_sink", "ChromaSink"),
    "csv": ("elspeth.plugins.sinks.csv_sink", "CSVSink"),
    "database": ("elspeth.plugins.sinks.database_sink", "DatabaseSink"),
    "dataverse": ("elspeth.plugins.sinks.dataverse", "DataverseSink"),
    "document": ("elspeth.plugins.sinks.document_sink", "DocumentSink"),
    "json": ("elspeth.plugins.sinks.json_sink", "JSONSink"),
    "text": ("elspeth.plugins.sinks.text_sink", "TextSink"),
}


def build_nonlive_plugin_manager(requested_names: Collection[str]) -> PluginManager:
    """Register only reviewed built-ins, without scanning plugin directories.

    This manager is scoped to one replay/verify invocation. The ordinary live
    singleton and its discovery behavior remain untouched.
    """
    from elspeth.plugins.infrastructure.discovery import create_dynamic_hookimpl
    from elspeth.plugins.infrastructure.manager import PluginManager, scoped_plugin_manager

    requested = frozenset(requested_names)
    unknown = requested.difference(_SOURCE_CLASSES, _TRANSFORM_CLASSES, _SINK_CLASSES)
    if unknown:
        raise OrchestrationInvariantError(f"Replay/verify plugin names are not reviewed built-ins: {sorted(unknown)}")
    manager = PluginManager()
    registries = (
        (_SOURCE_CLASSES, "elspeth_get_source"),
        (_TRANSFORM_CLASSES, "elspeth_get_transforms"),
        (_SINK_CLASSES, "elspeth_get_sinks"),
    )
    # An approved module's own imports must also see this scoped manager. A
    # nested registry lookup cannot accidentally initialize global discovery.
    with scoped_plugin_manager(manager):
        for supported, hook_name in registries:
            classes = []
            for name, (module_name, class_name) in supported.items():
                if name not in requested:
                    continue
                plugin_class = vars(import_module(module_name))[class_name]
                if plugin_class.name != name:
                    raise OrchestrationInvariantError(f"Reviewed built-in plugin identity drifted: {name!r}")
                classes.append(plugin_class)
            manager.register(create_dynamic_hookimpl(classes, hook_name))
    return manager


def precheck_nonlive_plugin_names(settings: ElspethSettings) -> None:
    """Refuse unsupported names before importing the plugin registry."""
    requested = (
        ("source", (source.plugin for source in settings.sources.values()), _SOURCE_CLASSES),
        (
            "transform",
            (plugin.plugin for collection in (settings.transforms, settings.aggregations, settings.collectors) for plugin in collection),
            _TRANSFORM_CLASSES,
        ),
        ("sink", (sink.plugin for sink in settings.sinks.values()), _SINK_CLASSES),
    )
    for kind, names, supported in requested:
        for name in names:
            if name not in supported:
                raise OrchestrationInvariantError(f"Replay/verify does not support {kind} plugin {name!r}")
    for source_plugin in settings.sources.values():
        if source_plugin.plugin == "llm":
            _admit_nonlive_llm_tracing(source_plugin.options)
    # LLM is a row transform; aggregate/collector validation rejects it as
    # non-batch-aware before those sections can construct a runtime plugin.
    for transform_plugin in settings.transforms:
        if transform_plugin.plugin == "llm":
            _admit_nonlive_llm_tracing(transform_plugin.options)


def precheck_nonlive_plugin_names_from_raw(raw_config: object) -> frozenset[str]:
    """Return reviewed YAML plugin names without importing the registry."""
    try:
        parsed = _RawPluginSections.model_validate(raw_config)
    except ValidationError as exc:
        raise ValueError("Replay/verify plugin sections have invalid shape") from exc
    sections = (
        ("sources", parsed.sources.values(), _SOURCE_CLASSES),
        ("transforms", parsed.transforms, _TRANSFORM_CLASSES),
        ("aggregations", parsed.aggregations, _TRANSFORM_CLASSES),
        ("collectors", parsed.collectors, _TRANSFORM_CLASSES),
        ("sinks", parsed.sinks.values(), _SINK_CLASSES),
    )
    names: set[str] = set()
    for section_name, entries, supported in sections:
        for entry in entries:
            name = entry.plugin
            if name not in supported:
                raise OrchestrationInvariantError(f"Replay/verify does not support {section_name} plugin {name!r}")
            if name == "llm":
                _admit_nonlive_llm_tracing(entry.options)
            names.add(name)
    return frozenset(names)


def admit_nonlive_plugin_classes(settings: ElspethSettings) -> None:
    """Require registered classes to be the exact reviewed built-in objects."""
    from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

    precheck_nonlive_plugin_names(settings)
    manager = get_shared_plugin_manager()
    requested = (
        ("source", (source.plugin for source in settings.sources.values()), _SOURCE_CLASSES, manager.get_source_by_name),
        (
            "transform",
            (plugin.plugin for collection in (settings.transforms, settings.aggregations, settings.collectors) for plugin in collection),
            _TRANSFORM_CLASSES,
            manager.get_transform_by_name,
        ),
        ("sink", (sink.plugin for sink in settings.sinks.values()), _SINK_CLASSES, manager.get_sink_by_name),
    )
    for kind, names, supported, registered_class in requested:
        for name in names:
            module_name, class_name = supported[name]
            expected = vars(import_module(module_name))[class_name]
            if registered_class(name) is not expected:
                raise OrchestrationInvariantError(f"Replay/verify {kind} plugin {name!r} is not the reviewed built-in class")


def admit_nonlive_runtime_plugin_instances(config: PipelineConfig, settings: ElspethSettings) -> None:
    """Refuse unreviewed instances passed directly to the orchestrator."""
    precheck_nonlive_plugin_names(settings)
    if set(config.sources) != set(settings.sources) or set(config.sinks) != set(settings.sinks):
        raise OrchestrationInvariantError("Replay/verify runtime plugin names disagree with settings")
    for name, source_instance in config.sources.items():
        module_name, class_name = _SOURCE_CLASSES[settings.sources[name].plugin]
        if type(source_instance) is not vars(import_module(module_name))[class_name]:
            raise OrchestrationInvariantError(f"Replay/verify source {name!r} is not the reviewed built-in class")
    for name, sink_instance in config.sinks.items():
        module_name, class_name = _SINK_CLASSES[settings.sinks[name].plugin]
        if type(sink_instance) is not vars(import_module(module_name))[class_name]:
            raise OrchestrationInvariantError(f"Replay/verify sink {name!r} is not the reviewed built-in class")
    requested_transform_names = {
        plugin.plugin for collection in (settings.transforms, settings.aggregations, settings.collectors) for plugin in collection
    }
    approved_transform_types = {
        vars(import_module(_TRANSFORM_CLASSES[name][0]))[_TRANSFORM_CLASSES[name][1]] for name in requested_transform_names
    }
    for transform_instance in config.transforms:
        if type(transform_instance) not in approved_transform_types:
            raise OrchestrationInvariantError(
                f"Replay/verify transform class {type(transform_instance).__qualname__!r} is not a reviewed built-in class"
            )
