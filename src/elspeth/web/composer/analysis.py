"""Explicit graph-analysis gateway; the raw validator remains the reference.

Only immutable summaries are retained. Authored content and public revisions
form keys; private lowered state is inspected for eligibility and discarded.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from functools import partial

from rfc8785 import CanonicalizationError

from elspeth.plugins.infrastructure.manager import PluginManager, get_shared_plugin_manager, scoped_plugin_manager
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.session_cache import ComposerSessionCache, RegistryRevision, ValidationKey
from elspeth.web.composer.state import CompositionState, ValidationSummary


def _audited_builtin_classes() -> frozenset[type]:
    """Resolve exact audited classes without importing optional plugin SDKs.

    Builtin discovery installs canonical module aliases. Only already-loaded
    canonical modules participate, and their actual class objects are compared
    with registrations. A new or replacement same-name class cannot inherit
    eligibility. The table is the reviewed Stage 1 constructor dependency list.
    """
    classes: set[type] = set()
    for module_name, class_name in _AUDITED_BUILTINS:
        module = sys.modules.get(module_name)
        if module is not None:
            candidate = vars(module).get(class_name)
            if isinstance(candidate, type):
                classes.add(candidate)
    return frozenset(classes)


_AUDITED_BUILTINS: tuple[tuple[str, str], ...] = (
    ("elspeth.plugins.sinks.aws_s3_sink", "AWSS3Sink"),
    ("elspeth.plugins.sinks.azure_blob_sink", "AzureBlobSink"),
    ("elspeth.plugins.sinks.chroma_sink", "ChromaSink"),
    ("elspeth.plugins.sinks.csv_sink", "CSVSink"),
    ("elspeth.plugins.sinks.dataverse", "DataverseSink"),
    ("elspeth.plugins.sinks.document_sink", "DocumentSink"),
    ("elspeth.plugins.sinks.json_sink", "JSONSink"),
    ("elspeth.plugins.sinks.text_sink", "TextSink"),
    ("elspeth.plugins.sources.aws_s3_source", "AWSS3Source"),
    ("elspeth.plugins.sources.azure_blob_source", "AzureBlobSource"),
    ("elspeth.plugins.sources.blob_rows", "BlobRowsSource"),
    ("elspeth.plugins.sources.csv_source", "CSVSource"),
    ("elspeth.plugins.sources.dataverse", "DataverseSource"),
    ("elspeth.plugins.sources.json_source", "JSONSource"),
    ("elspeth.plugins.sources.llm.source", "LLMSource"),
    ("elspeth.plugins.sources.null_source", "NullSource"),
    ("elspeth.plugins.sources.text_source", "TextSource"),
    ("elspeth.plugins.transforms.aws.bedrock_content_safety", "AWSBedrockContentSafety"),
    ("elspeth.plugins.transforms.aws.bedrock_prompt_shield", "AWSBedrockPromptShield"),
    ("elspeth.plugins.transforms.aws.textract_document_analysis", "AWSTextractDocumentAnalysis"),
    ("elspeth.plugins.transforms.aws.textract_inline_analysis", "AWSTextractInlineAnalysis"),
    ("elspeth.plugins.transforms.azure.ai_search", "AzureAISearchTransform"),
    ("elspeth.plugins.transforms.azure.content_safety", "AzureContentSafety"),
    ("elspeth.plugins.transforms.azure.document_intelligence", "AzureDocumentIntelligence"),
    ("elspeth.plugins.transforms.azure.prompt_shield", "AzurePromptShield"),
    ("elspeth.plugins.transforms.batch_classifier_metrics", "BatchClassifierMetrics"),
    ("elspeth.plugins.transforms.batch_data_quality_report", "BatchDataQualityReport"),
    ("elspeth.plugins.transforms.batch_distribution_profile", "BatchDistributionProfile"),
    ("elspeth.plugins.transforms.batch_drift_compare", "BatchDriftCompare"),
    ("elspeth.plugins.transforms.batch_duplicate_annotator", "BatchDuplicateAnnotator"),
    ("elspeth.plugins.transforms.batch_effect_size", "BatchEffectSize"),
    ("elspeth.plugins.transforms.batch_experiment_compare", "BatchExperimentCompare"),
    ("elspeth.plugins.transforms.batch_outlier_annotator", "BatchOutlierAnnotator"),
    ("elspeth.plugins.transforms.batch_paired_preference", "BatchPairedPreference"),
    ("elspeth.plugins.transforms.batch_rank", "BatchRank"),
    ("elspeth.plugins.transforms.batch_replicate", "BatchReplicate"),
    ("elspeth.plugins.transforms.batch_stats", "BatchStats"),
    ("elspeth.plugins.transforms.batch_threshold_summary", "BatchThresholdSummary"),
    ("elspeth.plugins.transforms.batch_top_k", "BatchTopK"),
    ("elspeth.plugins.transforms.blob_csv_expand", "BlobCSVExpand"),
    ("elspeth.plugins.transforms.blob_fetch", "BlobFetch"),
    ("elspeth.plugins.transforms.blob_json_expand", "BlobJSONExpand"),
    ("elspeth.plugins.transforms.blob_text_expand", "BlobTextExpand"),
    ("elspeth.plugins.transforms.field_mapper", "FieldMapper"),
    ("elspeth.plugins.transforms.json_explode", "JSONExplode"),
    ("elspeth.plugins.transforms.keyword_filter", "KeywordFilter"),
    ("elspeth.plugins.transforms.line_explode", "LineExplode"),
    ("elspeth.plugins.transforms.llm.transform", "LLMTransform"),
    ("elspeth.plugins.transforms.passthrough", "PassThrough"),
    ("elspeth.plugins.transforms.pdf_rasterize", "PDFRasterize"),
    ("elspeth.plugins.transforms.rag.transform", "RAGRetrievalTransform"),
    ("elspeth.plugins.transforms.reference_join", "ReferenceJoin"),
    ("elspeth.plugins.transforms.report_assemble", "ReportAssemble"),
    ("elspeth.plugins.transforms.truncate", "Truncate"),
    ("elspeth.plugins.transforms.type_coerce", "TypeCoerce"),
    ("elspeth.plugins.transforms.value_transform", "ValueTransform"),
    ("elspeth.plugins.transforms.web_scrape", "WebScrapeTransform"),
)


def registry_revision(manager: PluginManager) -> RegistryRevision:
    return RegistryRevision(manager, tuple(manager.get_sources()), tuple(manager.get_transforms()), tuple(manager.get_sinks()))


def validation_weight_bytes(summary: ValidationSummary) -> int:
    """Conservative retained weight including non-wire attribution fields.

    The repr of this owned immutable record covers errors, suggestions, edge
    contracts and semantic contracts, including their in-memory-only fields.
    No executable state or arbitrary object graph is traversed or retained.
    """
    return 256 + 2 * len(repr(summary).encode("utf-8", errors="backslashreplace"))


def _eligible(state: CompositionState, revision: RegistryRevision) -> bool:
    audited = _audited_builtin_classes()
    sources = {cls.name: cls for cls in revision.sources}
    transforms = {cls.name: cls for cls in revision.transforms}
    sinks = {cls.name: cls for cls in revision.sinks}
    if any(sources.get(source.plugin) not in audited for source in state.sources.values()):
        return False
    if any(sinks.get(output.plugin) not in audited for output in state.outputs):
        return False
    for node in state.nodes:
        if node.plugin is None:
            continue
        plugin = transforms.get(node.plugin)
        if plugin not in audited:
            return False
        # The registered class has already passed the exact audited-identity
        # check. This name selects an extra dependency restriction only.
        if node.plugin == "llm":
            tracing = node.options.get("tracing")
            if isinstance(tracing, Mapping) and tracing.get("provider") == "langfuse":
                return False
    return True


def _content_key(state: CompositionState) -> str | None:
    try:
        return composition_content_hash(state)
    except (CanonicalizationError, UnicodeEncodeError):
        # RFC8785 cannot represent all malformed draft options. Validation
        # still owns their established diagnostics, so omit this optimization.
        return None
    except ValueError as exc:
        if str(exc).startswith(("Cannot canonicalize", "NaN/Infinity found")):
            return None
        raise
    except TypeError as exc:
        if str(exc).startswith(("frozenset is not JSON-serializable", "unsupported type:")):
            return None
        raise


def _validate_with_manager(manager: PluginManager, state: CompositionState) -> ValidationSummary:
    with scoped_plugin_manager(manager):
        return state.validate()


def _build_validation_key(
    authored_state: CompositionState,
    manager: PluginManager,
    *,
    dependency_state: CompositionState | None = None,
    snapshot_hash: str = "",
    profile_generation: str | int = "",
    mode: str = "raw-v1",
) -> ValidationKey | None:
    revision = registry_revision(manager)
    if not _eligible(authored_state, revision) or (dependency_state is not None and not _eligible(dependency_state, revision)):
        return None
    content_hash = _content_key(authored_state)
    if content_hash is None:
        return None
    return ValidationKey(content_hash, revision, mode, snapshot_hash, profile_generation)


class ComposerAnalysis:
    def __init__(self, cache: ComposerSessionCache | None = None, *, manager: PluginManager | None = None) -> None:
        self.cache = cache
        self.manager = manager if manager is not None else get_shared_plugin_manager()

    async def run_in_worker[**P, T](self, func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        """Pin captured session ownership while keeping cancellation prompt."""
        from elspeth.web.async_workers import run_sync_in_worker, run_sync_in_worker_with_completion

        pin = self.cache.pin_worker() if self.cache is not None else None
        if pin is None:
            # A disposed facade is permanently uncached: its retired owner
            # cannot read, publish or be resurrected by a late invocation.
            return await run_sync_in_worker(func, *args, **kwargs)
        return await run_sync_in_worker_with_completion(pin.close, func, *args, **kwargs)

    def _compute(self, state: CompositionState) -> ValidationSummary:
        return _validate_with_manager(self.manager, state)

    def _key(
        self,
        authored_state: CompositionState,
        *,
        dependency_state: CompositionState | None = None,
        snapshot_hash: str = "",
        profile_generation: str | int = "",
        mode: str = "raw-v1",
    ) -> ValidationKey | None:
        if self.cache is None:
            return None
        return _build_validation_key(
            authored_state,
            self.manager,
            dependency_state=dependency_state,
            snapshot_hash=snapshot_hash,
            profile_generation=profile_generation,
            mode=mode,
        )

    def validate_raw(self, state: CompositionState) -> ValidationSummary:
        key = self._key(state)
        if key is None or self.cache is None:
            return self._compute(state)
        return self.cache.validations.get_or_compute(
            key,
            lambda: self._compute(state),
            key_weight=key.weight_bytes(),
            value_weight=validation_weight_bytes,
            retain=lambda _summary: registry_revision(self.manager) == key.registry,
        ).value

    async def validate_raw_async(self, state: CompositionState) -> ValidationSummary:
        from elspeth.web.async_workers import run_sync_in_worker

        if self.cache is None:
            return await run_sync_in_worker(_validate_with_manager, self.manager, state)
        # Graph hashing and dependency inspection are graph-sized CPU work.
        # This standalone callable captures no resident cache owner, so request
        # cancellation during key construction needs no owner-lifetime pin.
        key = await run_sync_in_worker(_build_validation_key, state, self.manager)
        if key is None:
            return await run_sync_in_worker(_validate_with_manager, self.manager, state)
        return (
            await self.cache.validations.get_or_compute_async(
                key,
                partial(_validate_with_manager, self.manager, state),
                key_weight=key.weight_bytes(),
                value_weight=validation_weight_bytes,
                retain=lambda _summary: registry_revision(self.manager) == key.registry,
            )
        ).value

    def validate_profile_summary(
        self,
        authored_state: CompositionState,
        compute: Callable[[], ValidationSummary],
        *,
        snapshot_hash: str,
        profile_generation: str | int,
        dependency_state: CompositionState | None = None,
    ) -> ValidationSummary:
        key = self._key(
            authored_state,
            dependency_state=dependency_state,
            snapshot_hash=snapshot_hash,
            profile_generation=profile_generation,
            mode="profile-v1",
        )

        def scoped_compute() -> ValidationSummary:
            with scoped_plugin_manager(self.manager):
                return compute()

        if key is None or self.cache is None:
            return scoped_compute()
        return self.cache.validations.get_or_compute(
            key,
            scoped_compute,
            key_weight=key.weight_bytes(),
            value_weight=validation_weight_bytes,
            retain=lambda _summary: registry_revision(self.manager) == key.registry,
        ).value
