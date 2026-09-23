"""Preconstruction plugin admission for replay and verify runs.

The name check runs before registry discovery. The class check then compares
the registered class with the built-in class object, so a third-party plugin
cannot gain replay authority by using a built-in name.
"""

from __future__ import annotations

from collections.abc import Iterable
from importlib import import_module
from typing import TYPE_CHECKING

from elspeth.contracts.errors import OrchestrationInvariantError

if TYPE_CHECKING:
    from elspeth.core.config import ElspethSettings
    from elspeth.engine.orchestrator.types import PipelineConfig


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
    "field_mapper": ("elspeth.plugins.transforms.field_mapper", "FieldMapper"),
    "json_explode": ("elspeth.plugins.transforms.json_explode", "JSONExplode"),
    "keyword_filter": ("elspeth.plugins.transforms.keyword_filter", "KeywordFilter"),
    "line_explode": ("elspeth.plugins.transforms.line_explode", "LineExplode"),
    "passthrough": ("elspeth.plugins.transforms.passthrough", "PassThrough"),
    "report_assemble": ("elspeth.plugins.transforms.report_assemble", "ReportAssemble"),
    "truncate": ("elspeth.plugins.transforms.truncate", "Truncate"),
    "type_coerce": ("elspeth.plugins.transforms.type_coerce", "TypeCoerce"),
    "value_transform": ("elspeth.plugins.transforms.value_transform", "ValueTransform"),
}

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


def precheck_nonlive_plugin_names_from_raw(raw_config: object) -> None:
    """Reject unsupported YAML names without importing the plugin registry."""
    if not isinstance(raw_config, dict):
        raise ValueError("Replay/verify settings must be a YAML mapping")
    sections = (
        ("sources", _SOURCE_CLASSES, True),
        ("transforms", _TRANSFORM_CLASSES, False),
        ("aggregations", _TRANSFORM_CLASSES, False),
        ("collectors", _TRANSFORM_CLASSES, False),
        ("sinks", _SINK_CLASSES, True),
    )
    for section_name, supported, named in sections:
        section = raw_config.get(section_name, {} if named else [])
        entries: Iterable[object]
        if named:
            if not isinstance(section, dict):
                raise ValueError(f"Replay/verify {section_name} must be a mapping")
            entries = section.values()
        else:
            if not isinstance(section, list):
                raise ValueError(f"Replay/verify {section_name} must be a list")
            entries = section
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"Replay/verify {section_name} entries must be mappings")
            name = entry.get("plugin")
            if not isinstance(name, str) or name not in supported:
                raise OrchestrationInvariantError(f"Replay/verify does not support {section_name} plugin {name!r}")


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
