"""Base classes for plugin implementations.

These provide common functionality and ensure proper interface compliance.
Plugins MUST subclass these base classes (BaseSource, BaseTransform, BaseSink).
Gate routing is handled by config-driven GateSettings + ExpressionParser, not plugin classes.

Why base class inheritance is required:
- Plugin discovery uses issubclass() checks against base classes
- Python's Protocol with non-method members (name, determinism, etc.) cannot
  support issubclass() - only isinstance() on already-instantiated objects
- Base classes enforce self-consistency via __init_subclass__ hooks
- Per CLAUDE.md "Plugin Ownership", all plugins are system code, not user extensions

The protocol definitions (SourceProtocol, TransformProtocol, SinkProtocol) exist
for type-checking purposes only - they define the interface contract but cannot
be used for runtime discovery.

Lifecycle Contract (all hooks called on main thread by orchestrator):
    on_start(ctx) -> [process/load/write] -> on_complete(ctx) -> close()

- on_start: Per-run initialization (acquire resources, capture context).
  If on_start raises, neither on_complete nor close is called.
- on_complete: Processing finished (success or error). Receives LifecycleContext
  for landscape/telemetry interaction. Called even on pipeline crash.
- close: Pure resource teardown (no context). Called even on pipeline
  crash. Each plugin's cleanup is individually protected.
- Call order across plugin types (normal run):
  source.on_start -> transforms.on_start -> sinks.on_start -> [processing]
  -> transforms.on_complete -> sinks.on_complete -> source.on_complete
  -> source.close -> transforms.close -> sinks.close
- Resume runs skip source lifecycle entirely (NullSource is used).
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, cast

from elspeth.contracts import (
    DeclaredAuditCharacteristics,
    Determinism,
    PluginSchema,
    SourceRow,
)
from elspeth.contracts.diversion import RowDiversion, SinkWriteResult
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.plugin_capabilities import (
    CapabilityDeclaration,
    ControlRole,
    PluginCapability,
    WebConfigAuthority,
)
from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract

if TYPE_CHECKING:
    from elspeth.contracts.contexts import LifecycleContext, SinkContext, SourceContext, TransformContext
    from elspeth.contracts.header_modes import HeaderMode
    from elspeth.contracts.plugin_assistance import PluginAssistance
    from elspeth.contracts.plugin_semantics import (
        InputSemanticRequirements,
        OutputSemanticDeclaration,
    )
    from elspeth.contracts.schema import SchemaConfig
    from elspeth.contracts.schema_contract import SchemaContract
    from elspeth.contracts.sink import OutputValidationResult
    from elspeth.plugins.infrastructure.config_base import PluginConfig, TransformDataConfig

from elspeth.contracts.sink_effects import (
    AuditExportFormat,
    ResolvedSinkEffectMode,
    SinkEffectContract,
    SinkEffectExecutionPurpose,
    SinkEffectInputKind,
)
from elspeth.plugins.infrastructure.results import (
    TransformResult,
)


def is_column_naming_config_option(key: str) -> bool:
    """Whether a config option names a ROW COLUMN rather than an ordinary value.

    Shared by ``BaseTransform.consumed_input_fields`` and the architecture test
    that checks every such option is classified read-or-write, so the two cannot
    drift apart. Naming alone cannot say WHICH: ``batch_top_k.field`` reads a
    column and ``web_scrape.content_field`` writes one, with identical shape.
    That classification is the plugin's job via ``output_naming_config_keys``.
    """
    return key in ("field", "fields", "group_by") or key.endswith(("_field", "_fields"))


def _demote_required_model_fields(
    model: type[PluginSchema],
    field_names: frozenset[str],
) -> type[PluginSchema]:
    """Return ``model`` with ``field_names`` no longer REQUIRED — and nothing else.

    ``field_names`` are fields the transform CREATES, so demanding them on input
    is unsatisfiable (elspeth-d6eeb3a71d). Only the presence requirement is
    dropped: each field keeps its declared annotation AND its FieldInfo
    metadata, so a value that IS supplied is validated exactly as declared.

    Preserving metadata is not incidental. ``FIELD_TYPE_MAP["float"]`` is
    ``FiniteFloat``, i.e. ``Annotated[float, Field(allow_inf_nan=False)]``, and
    pydantic hoists that constraint OUT of the annotation into the FieldInfo.
    Rebuilding a field from ``.annotation`` alone therefore silently accepts
    NaN and Infinity — in a codebase that maintains a dedicated
    ``_find_non_finite_value_path`` walker to reject exactly those.

    The field is deliberately KEPT in the model rather than removed: ``mode:
    fixed`` builds ``extra="forbid"``, so dropping it would reject rows that
    legitimately carry it.

    Every other field, the extra-field mode, and strictness are inherited
    unchanged. Returns ``model`` itself when nothing needs demoting, preserving
    schema class identity for transforms that create no declared fields.
    """
    from pydantic import create_model

    demoted = {name for name in field_names if name in model.model_fields}
    if not demoted:
        return model

    overrides: dict[str, Any] = {}
    for name in sorted(demoted):
        field_info = model.model_fields[name]
        # An unannotated field is already unconstrained; Any keeps that honest.
        annotation: Any = Any if field_info.annotation is None else field_info.annotation
        if field_info.metadata:
            # Re-attach constraints pydantic hoisted out of the annotation.
            annotation = Annotated[(annotation, *field_info.metadata)]
        overrides[name] = (annotation, None)

    # cast: __base__ is a variable, so mypy's pydantic plugin cannot see that the
    # generated model subclasses `model` and is therefore a PluginSchema.
    return cast(
        "type[PluginSchema]",
        create_model(
            # Distinct name: a demoted model and its declared original are two
            # classes, and an edge error naming both 'FooInput' would be unreadable.
            f"{model.__name__}DemotedInput",
            __base__=model,
            __module__=model.__module__,
            **overrides,
        ),
    )


class BaseTransform(ABC):
    """Base class for all row transforms.

    Execution Models
    ----------------
    Transforms use one of three execution models depending on concurrency needs.
    The TransformExecutor in engine/executors/transform.py dispatches automatically
    based on isinstance checks (BatchTransformMixin) and the is_batch_aware flag.

    **1. Synchronous (process) -- standard row-by-row processing**

        The engine calls process() once per row and receives a TransformResult
        synchronously. Use this for CPU-bound, fast, or deterministic transforms
        (field mapping, truncation, validation, etc.).

        class MyTransform(BaseTransform):
            name = "my_transform"
            input_schema = InputSchema      # redirected; see note below
            output_schema = OutputSchema

    ``input_schema`` is a property, so a class-body assignment like the one
    above is moved by ``__init_subclass__`` into the property's backing store.
    The pattern stays valid and reads still return your schema — with fields
    the transform CREATES demoted, which is the point (elspeth-d6eeb3a71d).
    Assigning ``self.input_schema = ...`` in ``__init__`` works identically.

            def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
                return TransformResult.success(
                    {**row.to_dict(), "new_field": "value"},
                    success_reason={"action": "processed"},
                )

    **2. Streaming (accept) -- row-level pipelining via BatchTransformMixin**

        The engine calls accept() per row. Processing happens asynchronously in a
        worker pool; results are emitted in FIFO order through an OutputPort. The
        engine blocks until each row's result arrives (sequential across rows,
        concurrent within each row). Use this for I/O-bound transforms that benefit
        from concurrency (LLM calls, HTTP APIs, multi-query evaluation).

        Requires inheriting both BaseTransform and BatchTransformMixin:

        class MyLLMTransform(BaseTransform, BatchTransformMixin):
            name = "my_llm"

            def accept(self, row: PipelineRow, ctx: TransformContext) -> None:
                self.accept_row(row, ctx, self._do_work)

            def connect_output(self, output: OutputPort, max_pending: int = 30) -> None:
                self.init_batch_processing(max_pending=max_pending, output=output)

            def _do_work(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
                # Runs in worker thread
                return TransformResult.success(...)

        Streaming transforms override process() to raise NotImplementedError,
        directing callers to use accept(). The TransformExecutor detects the mixin
        via isinstance(transform, BatchTransformMixin) and routes to accept()
        automatically -- plugin authors never need to worry about dispatch.

    **3. Batch-aware (process with is_batch_aware=True) -- aggregation batches**

        The engine buffers rows until an aggregation trigger fires, then calls
        process() with list[PipelineRow]. Use this for transforms inside
        aggregation nodes (batch LLM calls, statistical aggregations).

        The engine dispatches single-row vs. batch from the class-level
        ``is_batch_aware`` flag; the transform never inspects the runtime
        argument shape. A runtime ``isinstance(row, list)`` branch in
        process() is a forbidden defensive type-check — it duplicates a
        decision the dispatcher has already made.

        class MyBatchTransform(BaseTransform):
            name = "my_batch"
            is_batch_aware = True

            def process(self, rows: list[PipelineRow], ctx: TransformContext) -> TransformResult:
                return self._process_batch(rows, ctx)

    When to Use Which
    -----------------
    - process() alone: Simple, fast transforms (field mapping, filtering, etc.)
    - accept() + BatchTransformMixin: I/O-bound per-row work needing concurrency
      (LLM API calls, HTTP requests, multi-query evaluation)
    - process() + is_batch_aware: Aggregation-stage transforms that receive
      pre-buffered batches from the engine's trigger system
    """

    name: str
    # input_schema is a property (see below): assignment stores the declared
    # model, reads return it with self-created fields demoted to optional.
    _declared_input_schema: type[PluginSchema] | None = None
    # The validated config, captured by _initialize_declared_input_fields so that
    # consumed_input_fields sees option DEFAULTS, not only authored keys.
    _validated_config: TransformDataConfig | None = None
    _input_schema_cache: tuple[type[PluginSchema], frozenset[str], type[PluginSchema]] | None = None
    # Memo for _config_named_input_columns, keyed on the config object identity.
    _config_columns_cache: tuple[object, frozenset[str]] | None = None
    output_schema: type[PluginSchema]
    node_id: str | None = None  # Set by orchestrator after registration

    # Audit metadata
    determinism: Determinism = Determinism.DETERMINISTIC
    plugin_version: str = "0.0.0"
    source_file_hash: str | None = None

    # ── Catalogue reference content ─────────────────────────────────────
    # These fields populate the catalog's reference cards. They are
    # documentation, not configuration — authors fill them in to explain
    # to a human reader (compliance, research, ops) what this plugin
    # does, when it's the right choice, when it isn't, and what audit
    # characteristics it has. Base defaults remain optional for third-party
    # and legacy compatibility; repository tests require every registered
    # built-in to be complete. See
    # docs/contracts/plugin-catalogue-reference-content.md.

    usage_when_to_use: str | None = None
    """Persona-facing prose. One short paragraph answering "when should I
    pick this plugin?" — written for compliance / research / ops readers,
    not for plugin developers. Avoid restating the technical
    description; that's what the docstring is for."""

    usage_when_not_to_use: str | None = None
    """Persona-facing prose. One short paragraph answering "when should I
    *not* pick this plugin?" State a hard limitation or unsafe fit and
    redirect the reader to a concrete alternative where one exists."""

    example_use: str | None = None
    """One bounded YAML component fragment with realistic options and
    non-secret values. Use top-level ``sources`` for sources, ``transform``
    for ordinary transforms, ``aggregations`` for batch-aware transforms,
    and ``sinks`` for sinks. Preserve whitespace for preformatted rendering."""

    capability_tags: tuple[str, ...] = ()
    web_config_authority: WebConfigAuthority = WebConfigAuthority.USER_CONFIGURABLE
    policy_capabilities: frozenset[CapabilityDeclaration] = frozenset()

    """Short lowercase tags that drive catalog filter chips and fuzzy
    search. Examples: ("csv", "file", "batch") for csv_source;
    ("http", "network", "scraping") for a web-scrape transform. Tags
    are non-exhaustive; pick the two to six most useful for a user
    who is searching the catalog.

    **Open vocabulary — deliberate.** ``capability_tags`` is typed as
    bare ``tuple[str, ...]`` rather than a closed-vocabulary enum (cf.
    ``audit_characteristics`` below, which uses ``AuditCharacteristic``).
    The asymmetry is intentional:

      - ``audit_characteristics`` drives compliance-relevant rendering
        (the chip vocabulary an auditor reads). A typo silently
        degrades the audit signal, so the vocabulary is closed and
        typos fail at the type-check + registration boundary.
      - ``capability_tags`` drives discovery affordances (filter chips,
        fuzzy search ranking) for an operator browsing for a plugin
        that fits a use case. A new tag like ``"streaming"`` or
        ``"webhook"`` is meaningful to a human reader without a
        centrally-coordinated enum bump, and an "unrecognised" tag
        renders fine — it just doesn't join an existing filter cluster.

    Keep tags lowercase, short, and aligned with existing tags
    (``grep`` ``capability_tags`` for current usage) so the filter
    chip strip clusters related plugins. A new tag is fine; a typo on
    an existing tag fragments the chip strip and is the failure mode
    to avoid.

    The BaseSink and BaseSource declarations of this attribute share
    the same open-vocabulary rationale; treat this docstring as
    canonical."""

    audit_characteristics: DeclaredAuditCharacteristics = frozenset()
    """Declared audit characteristics that the framework cannot derive
    from other attributes. The catalog service composes this set with
    the characteristic derived from `determinism` at summary-build time.
    Declare members of the :class:`~elspeth.contracts.enums.AuditCharacteristic`
    enum (e.g. ``AuditCharacteristic.SIGNED``, ``AuditCharacteristic.CREDENTIALS``,
    ``AuditCharacteristic.QUARANTINE``, ``AuditCharacteristic.PROVENANCE``) —
    the enum itself is the closed vocabulary; typos fail mypy at the
    declaration site rather than disappearing silently from the rendered
    catalog card. The build-time test
    ``tests/unit/web/catalog/test_audit_characteristics_declaration_typed.py``
    additionally rejects bare-string members that pass mypy under
    StrEnum/str inference."""

    discovery_secret_requirements: Mapping[str, tuple[str, ...]] = {}
    """Credential-bearing config fields that must have a configured secret ref
    before composer discovery advertises the plugin.

    Keys are config field names such as ``api_key``. Values are browser-safe
    candidate secret-reference names, never secret values. An empty tuple means
    the field requires some available secret, but the plugin has no canonical
    inventory name.
    """

    @classmethod
    def is_effective_blocking_control(
        cls,
        *,
        capability: PluginCapability,
        role: ControlRole,
        options: Mapping[str, object],
    ) -> bool:
        """Evaluate whether this concrete config can enforce a declared control."""
        if "detect_only" in options and options["detect_only"] is True:
            return False
        return any(
            declaration.capability is capability and declaration.control_role is role and declaration.blocks_positive_detection
            for declaration in cls.policy_capabilities
        )

    # Config model — each subclass sets this to its Pydantic config class.
    # get_config_model() is the public API; override it for dynamic dispatch
    # (e.g. provider-based LLM config selection).
    config_model: ClassVar[type[PluginConfig] | None] = None

    @classmethod
    def get_config_model(cls, config: dict[str, Any] | None = None) -> type[PluginConfig] | None:
        """Return the Pydantic config model for this plugin type.

        Override in subclasses that need dynamic dispatch (e.g. LLMTransform
        selects a provider-specific model based on config["provider"]).
        """
        return cls.config_model

    @classmethod
    def get_config_schema(cls) -> dict[str, Any]:
        """Default ``get_config_schema`` — renders a single Pydantic model.

        See :meth:`~elspeth.contracts.plugin_protocols.TransformProtocol.get_config_schema`
        for the canonical contract, including the MUST-override rule for
        plugins whose effective configuration is a discriminated union.
        """
        if cls.config_model is None:
            return {}
        schema: dict[str, Any] = cls.config_model.model_json_schema()
        return schema

    # Batch support - override to True for batch-aware transforms
    # When True, engine may pass list[dict] instead of single dict to process()
    is_batch_aware: bool = False

    # Batch-aware transforms are aggregation-stage by default. A plugin that
    # intentionally supports both list-mode and row-mode dispatch must opt in
    # explicitly so composer/tool validation can reject accidental placement.
    supports_row_mode_when_batch_aware: bool = False

    # Token creation flag for deaggregation transforms
    # When True AND process() returns success_multi(), the processor creates
    # new token_ids for each output row with parent linkage to input token.
    # When False AND success_multi() is returned, the processor expects
    # passthrough mode (same number of outputs as inputs, preserve token_ids).
    # Default: False (most transforms don't create new tokens)
    creates_tokens: bool = False

    # Pass-through contract flag (ADR-007).
    # True iff process() UNCONDITIONALLY emits rows containing every field
    # present on the input row plus declared_output_fields, regardless of
    # row content or runtime state. False if the transform may drop, rename,
    # filter, or conditionally omit input fields. Conditional drops based on
    # row content are forbidden under this annotation — they would pass static
    # and Hypothesis tests and crash production via PassThroughContractViolation.
    # Annotation is verified at runtime by TransformExecutor's pass-through
    # cross-check; mis-annotation raises PassThroughContractViolation (TIER_1).
    passes_through_input: bool = False

    # Empty-emission governance declaration (ADR-012).
    # True means the transform may intentionally emit zero rows on success.
    # False means empty success output is governance-significant for
    # passes_through_input=True transforms and is checked by the
    # can_drop_rows declaration contract.
    can_drop_rows: bool = False

    # Config options that NAME AN OUTPUT FIELD rather than a consumed input
    # column. `consumed_input_fields` treats every other column-naming option
    # (`field`, `group_by`, `*_field`) as an input the transform READS, so its
    # value is never demoted. Declare an option here only after checking the
    # config field's own description says it chooses where the plugin WRITES;
    # leaving a genuine output option undeclared makes the parity probe fail,
    # and mis-declaring a consumed one silently drops a real requirement.
    output_naming_config_keys: frozenset[str] = frozenset()

    # Field collision enforcement (centralized in TransformExecutor).
    # Transforms that add fields to the output row declare WHAT fields they add
    # at init time. The executor checks these against input keys BEFORE running
    # the transform. Empty frozenset = no fields added = no check needed.
    declared_output_fields: frozenset[str] = frozenset()

    # Input-field declaration for ADR-013.
    # Normalized from TransformDataConfig.required_input_fields at construction
    # time via _initialize_declared_input_fields(). Empty frozenset means the
    # transform declares no pre-emission required-input contract.
    declared_input_fields: frozenset[str] = frozenset()

    # Fail-closed string-scan declaration (elspeth-b19dfe41fb).
    # Transforms that quarantine a row when an explicitly configured scan field
    # is missing or non-string set this to those field names at construction.
    # Consumed only at build time by
    # validate_transform_string_typed_input_fields, which rejects a pipeline
    # whose producer schema provably types such a field int/float/bool. Empty
    # frozenset means the transform makes no string-typed input claim.
    declared_string_input_fields: frozenset[str] = frozenset()

    # Runtime preflight opt-in. Transforms that need an engine-time external
    # readiness check before source iteration set this True and override
    # runtime_preflight(). The default remains closed and side-effect free.
    requires_runtime_preflight: bool = False

    # Error routing configuration.
    # Transforms extending TransformDataConfig override this from config.
    # Always non-None at runtime (TransformSettings requires on_error).
    # Base class default is None because injection happens post-construction
    # via runtime_factory bridge (set from TransformSettings.on_error).
    on_error: str | None = None

    # Success routing configuration
    # Terminal transforms set this to the output sink name.
    # Always non-None at runtime (TransformSettings requires on_success).
    # Base class default is None because injection happens post-construction
    # via runtime_factory bridge (set from TransformSettings.on_success).
    on_success: str | None = None

    # DAG contract for output field validation (centralized in DAG builder).
    # Transforms that add fields must set this via _build_output_schema_config()
    # so the DAG builder can validate downstream required_input_fields.
    # None = no output contract provided (acceptable for shape-preserving transforms).
    _output_schema_config: SchemaConfig | None

    # The transform's INPUT schema config. Captured centrally by
    # `_initialize_declared_input_fields` from the validated config, so every
    # transform on that path has it; a transform that rewrites its schema config
    # assigns over the capture afterwards. The base default keeps it nominally
    # present so `consumed_input_fields` can read `required_fields` without
    # probing for the attribute (ADR-032). `None` therefore means a transform
    # that never validated a config — not "declares no schema block", which is
    # unrepresentable: `TransformDataConfig.schema_config` is required.
    _schema_config: SchemaConfig | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # Enforces the contract documented in contracts/enums.py:Determinism —
        # every plugin MUST declare a Determinism value at registration. The
        # class-level default on BaseTransform exists only to satisfy typing
        # (descendants assign on top of it); the inheritance chain is not a
        # legitimate way to acquire a determinism classification. An author who
        # adds a new transform that makes external calls and forgets to write
        # `determinism = Determinism.EXTERNAL_CALL` would otherwise silently
        # record the wrong determinism in the Landscape legal record, misclassify
        # the node in the readiness panel, render the wrong audit-characteristic
        # chips in the catalog, and produce a wrong ReproducibilityGrade.
        # Intermediate ABCs (e.g. BaseAzureSafetyTransform) redeclare too —
        # the contract is uniform, not "concrete classes only".
        super().__init_subclass__(**kwargs)

        # `input_schema` is a property on BaseTransform, but the documented
        # authoring pattern assigns it in the subclass BODY (see this class's
        # docstring). A class-body assignment lands in `cls.__dict__`, which
        # wins the MRO lookup ahead of the property — so reads would return the
        # UNDEMOTED model, silently reintroducing elspeth-d6eeb3a71d through the
        # path the docs recommend. Redirect the declaration to the property's
        # backing store so the documented pattern stays valid AND correct.
        #
        # Only plain values are moved: a descriptor here is a subclass
        # deliberately overriding the property, which must be left alone.
        # Annotation-only declarations never reach `__dict__` and need nothing.
        # Presence, not truthiness: `input_schema = None` in a class body would
        # otherwise be skipped and left shadowing the property, so reads would
        # return None and defer the failure to `None.model_validate(...)`.
        if "input_schema" in cls.__dict__:
            declared_schema = cls.__dict__["input_schema"]
            if not hasattr(type(declared_schema), "__get__"):
                delattr(cls, "input_schema")
                cls._declared_input_schema = declared_schema

        # The redirect above only sees `cls.__dict__`. A MIXIN ahead of
        # BaseTransform in the MRO shadows the property without ever appearing
        # there, so reads would silently return the UNDEMOTED model. Assert the
        # property still wins, so that shape fails at class creation rather than
        # per row — the same posture as the determinism check below.
        resolved = inspect.getattr_static(cls, "input_schema", None)
        if not isinstance(resolved, property):
            raise TypeError(
                f"{cls.__qualname__} resolves `input_schema` to "
                f"{type(resolved).__name__} instead of BaseTransform's property, so "
                f"self-created fields would never be demoted (elspeth-d6eeb3a71d). A "
                f"base or mixin ahead of BaseTransform in the MRO is shadowing it. "
                f"Assign the schema on the instance (`self.input_schema = ...`) or in "
                f"this class's own body, and list BaseTransform before any mixin that "
                f"declares input_schema."
            )

        if "determinism" not in cls.__dict__:
            raise TypeError(
                f"{cls.__qualname__} inherits from BaseTransform but does not "
                f"explicitly declare a `determinism` class attribute. Every "
                f"plugin MUST declare its determinism classification at "
                f"registration; there is no default (see "
                f"elspeth.contracts.enums.Determinism). Add one of: "
                f"`determinism = Determinism.DETERMINISTIC`, "
                f"`Determinism.SEEDED`, `Determinism.IO_READ`, "
                f"`Determinism.IO_WRITE`, `Determinism.EXTERNAL_CALL`, or "
                f"`Determinism.NON_DETERMINISTIC` to the class body. "
                f"Redeclaring the same value as the parent is acceptable — "
                f"the point is explicit author declaration, so that an audit "
                f"reader can read the source and see which value the author "
                f"chose, rather than tracing inheritance to find it."
            )

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize with configuration.

        Args:
            config: Plugin configuration
        """
        self.config = config
        # Per-instance lifecycle-guard initialization.
        #
        # `_on_start_called` and `_on_complete_called` are the
        # falsifiable post-conditions that contract tests use to detect
        # missing-super() bugs in subclass lifecycle overrides: a stub
        # `on_start()` that forgets to call `super().on_start()` leaves
        # the flag False after construction-then-invocation, breaking
        # the contract test. The flags MUST be per-instance — declaring
        # them at class level (`_on_start_called: bool = False`) would
        # make the True/False state shared across every plugin instance
        # of the same class, so a single completed run would mask
        # missing super() calls in every subsequent instance.
        #
        # Mirrored on BaseSink and BaseSource; see those `__init__`
        # comments referring back here for the lifecycle-guard
        # rationale.
        self._on_start_called: bool = False
        self._on_complete_called: bool = False
        self.declared_input_fields = frozenset()
        self._output_schema_config: SchemaConfig | None = None

    def _initialize_declared_input_fields(self, validated_config: TransformDataConfig) -> None:
        """Populate ADR-013's runtime input-field declaration from config.

        Call from the transform's authoritative config-validation path —
        immediately after ``<Config>.from_dict(...)`` succeeds. This preserves
        existing per-plugin validation/error semantics while centralizing the
        runtime normalization and batch-aware fail-closed guard.

        Also captures the INPUT ``schema:`` block, for the same reason and at the
        same seam. ``consumed_input_fields`` reads ``_schema_config``'s
        ``required_fields`` as one of its two NON-NEGOTIABLE members, so a
        transform that passed its ``schema_config`` to the schema factory without
        storing it kept the ``None`` class default and lost that limb — demoting
        at runtime a field ``get_raw_node_required_fields`` still enforced at
        build time (elspeth-3790106260). Capturing here rather than per plugin
        makes the limb unconditional on every transform that validates a config.

        ``TransformDataConfig.schema_config`` is required and non-None, and this
        runs BEFORE any plugin-specific assignment, so a transform that rewrites
        its schema config — ``BatchStats`` unions ``group_by`` into
        ``required_fields`` — still overwrites this with the wider set.
        """
        declared_input_fields = validated_config.declared_input_fields
        if declared_input_fields and self.is_batch_aware:
            raise FrameworkBugError(
                f"Transform {self.name!r} declares declared_input_fields "
                f"{sorted(declared_input_fields)!r} but is batch-aware. No "
                f"batch-pre-execution dispatch site exists; ADR-013 scopes "
                f"DeclaredRequiredFieldsContract to non-batch transforms until "
                f"an ADR-010 amendment lands."
            )
        self._validated_config = validated_config
        self.declared_input_fields = declared_input_fields
        self._schema_config = validated_config.schema_config
        self._reject_fixed_schema_omitting_consumed_fields()

    def _reject_fixed_schema_omitting_consumed_fields(self) -> None:
        """Reject a fixed input schema that forbids an AUTHORED input column.

        An input column the author explicitly configured this transform to
        read, omitted from ``mode: fixed`` fields, is incoherent: the input
        model is ``extra='forbid'`` over the declared fields, so any row
        carrying the column is rejected before the transform runs — the
        configured read can never succeed (elspeth-d3958d90f5).

        Only AUTHORED option values participate, read from the raw config
        rather than the validated model. Option DEFAULTS are deliberately
        excluded: ``batch_replicate.copies_field`` defaults to ``"copies"``
        with documented row-absence fallback semantics, so a user who never
        mentioned the option must not be forced to declare its default
        column. This is the inverse of ``_config_named_input_columns``'s
        choice — demotion protection wants defaults visible (fail-closed
        toward requiredness); construction rejection must not fire on intent
        the author never expressed. ADR-013 ``declared_input_fields`` always
        participates: it is a requiredness claim wherever it came from.

        Fires only for fixed mode. Flexible schemas admit the column as an
        extra and observed schemas declare nothing to contradict — odd
        configs, not incoherent ones. Dotted paths and other non-identifier
        spellings resolve at runtime only (the source boundary rejects such
        headers as schema fields), so they are exempt.

        This runs at the one seam every registered transform crosses right
        after config validation — BEFORE the batch family injects its
        consumed column into ``required_fields`` on a rebuilt SchemaConfig,
        which is why the check reads the authored-option surface rather than
        ``required_fields``: the same fact is visible earlier there.
        """
        schema_config = self._schema_config
        if schema_config is None or schema_config.mode != "fixed" or schema_config.fields is None:
            return

        authored_by: dict[str, list[str]] = {}
        for key, value in self.config.items():
            if key in self.output_naming_config_keys or not is_column_naming_config_option(key):
                continue
            values = [value] if isinstance(value, str) else list(value) if isinstance(value, (list, tuple)) else []
            for item in values:
                if isinstance(item, str):
                    authored_by.setdefault(item, []).append(key)
        for name in self.declared_input_fields:
            authored_by.setdefault(name, []).append("required_input_fields")

        declared = {field.name for field in schema_config.fields}
        missing = sorted(name for name in authored_by if name not in declared and name.isidentifier())
        if not missing:
            return

        from elspeth.plugins.infrastructure.config_base import PluginConfigError

        described = "; ".join(f"{name!r} (named by {', '.join(sorted(set(authored_by[name])))})" for name in missing)
        raise PluginConfigError(
            f"Transform {self.name!r} is configured to read input field(s) its fixed schema "
            f"forbids: {described}. A 'mode: fixed' schema rejects every row carrying an "
            f"undeclared field, so the configured read can never succeed. Declare the "
            f"field(s) in schema.fields, or use 'mode: flexible' / 'mode: observed'."
        )

    def effective_static_contract(self) -> frozenset[str]:
        """Return the transform's public static output guarantee surface.

        Runtime declaration checks record this value in audit evidence. Missing
        output schema config is acceptable only for shape-preserving transforms
        that declare no added fields. A field-adding transform without a schema
        config would falsely state its static guarantees and must crash.
        """
        output_schema_config = self._output_schema_config
        if output_schema_config is None:
            if not self.declared_output_fields:
                return frozenset()
            raise FrameworkBugError(
                f"Cannot derive effective static contract for transform {self.name!r}: "
                "_output_schema_config is missing. Concrete transforms must "
                "initialize their output schema config during construction "
                "before the engine can run declaration-contract checks."
            )
        return output_schema_config.get_effective_guaranteed_fields()

    def _align_output_contract(self, contract: SchemaContract) -> SchemaContract:
        """Normalize emitted contract mode/lock state to declared output semantics.

        ADR-014 compares emitted ``PipelineRow.contract`` semantics to the
        transform's ``_output_schema_config`` declaration. Once a contract is
        attached to an emitted row it is expected to be locked, even for
        ``flexible``/``observed`` config modes whose pre-emission builders may
        begin unlocked.
        """
        output_schema_config = self._output_schema_config
        if output_schema_config is None:
            return contract

        from elspeth.contracts.schema_contract import SchemaContract
        from elspeth.contracts.schema_contract_factory import expected_runtime_output_contract

        expected_mode, expected_locked = expected_runtime_output_contract(output_schema_config)
        if contract.mode == expected_mode and contract.locked == expected_locked:
            return contract

        return SchemaContract(
            mode=expected_mode,
            fields=contract.fields,
            locked=expected_locked,
        )

    def _apply_declared_output_field_contracts(self, contract: SchemaContract) -> SchemaContract:
        """Apply declared output field metadata to an emitted row contract.

        Contract propagation infers newly added fields from runtime values, which
        marks them as ``source="inferred"`` and ``required=False``. When a
        transform has an explicit ``_output_schema_config`` field declaration,
        ADR-014 expects emitted contracts to carry that declared metadata.
        """
        output_schema_config = self._output_schema_config
        if output_schema_config is None or output_schema_config.fields is None:
            return contract

        from elspeth.contracts.schema_contract_factory import create_contract_from_config

        declared_fields = {field.normalized_name: field for field in create_contract_from_config(output_schema_config).fields}
        fields: list[FieldContract] = []
        changed = False
        for field in contract.fields:
            if field.normalized_name in declared_fields:
                fields.append(declared_fields[field.normalized_name])
                changed = True
            else:
                fields.append(field)

        if not changed:
            return contract

        return SchemaContract(
            mode=contract.mode,
            fields=tuple(fields),
            locked=contract.locked,
        )

    def _align_output_row_contract(self, row: PipelineRow) -> PipelineRow:
        """Return ``row`` with contract semantics aligned to this transform."""
        if row.contract is None:
            raise FrameworkBugError(f"Transform {self.name!r} emitted PipelineRow with no contract. Framework invariant violated.")

        aligned_contract = self._align_output_contract(row.contract)
        if aligned_contract is row.contract:
            return row
        return PipelineRow(row.to_dict(), aligned_contract)

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        """Return a minimal config dict sufficient to instantiate this transform
        for invariant probing (ADR-009 §Clause 4).

        Transforms annotated with ``passes_through_input=True`` MUST override
        this method. The default raises ``NotImplementedError`` so that a
        transform that later gains the annotation is caught immediately by
        the skip-rate budget test, not silently excluded from governance.

        The returned dict is passed directly to ``cls(...)`` using the same
        positional constructor contract as ``PluginManager.create_transform()``.
        It must not require external services, network calls, or credentials —
        the invariant harness runs in CI and probes transforms in isolation.
        """
        raise NotImplementedError(
            f"{cls.__name__}.probe_config() is not implemented. "
            "Transforms with passes_through_input=True must declare how to "
            "instantiate in isolation. Implement probe_config() or remove the annotation."
        )

    def forward_invariant_probe_rows(self, probe: PipelineRow) -> list[PipelineRow]:
        """Return representative input rows for ADR-009's forward invariant.

        The default harness drives annotated pass-through transforms with a
        single scalar ``probe`` row. Config-sensitive transforms can override
        this to add the specific fields/values their ``probe_config()``
        requires while preserving the randomized background row shape.
        """
        return [probe]

    def backward_invariant_probe_rows(self, probe: PipelineRow) -> list[PipelineRow]:
        """Return representative input rows for ADR-009's backward invariant.

        The default harness drives non-pass-through transforms with a single
        scalar ``probe`` row. Batch-aware transforms whose non-pass-through
        semantics only appear under mixed-validity batches can override this
        to supply a more representative shape.
        """
        return [probe]

    def execute_forward_invariant_probe(
        self,
        probe_rows: list[PipelineRow],
        ctx: Any,
    ) -> TransformResult:
        """Execute the forward invariant probe using the production path.

        Default behavior mirrors runtime dispatch:
        - batch-aware transforms receive the full probe row list
        - single-row transforms must receive exactly one probe row

        Transforms with transport or concurrency seams that cannot be exercised
        via plain ``process()`` override this hook rather than teaching the
        invariant harness about plugin-specific internals.
        """
        if self.is_batch_aware:
            return self.process(probe_rows, ctx)  # type: ignore[arg-type]
        if len(probe_rows) != 1:
            raise FrameworkBugError(
                f"{self.__class__.__name__}.execute_forward_invariant_probe() received {len(probe_rows)} rows for a non-batch transform."
            )
        return self.process(probe_rows[0], ctx)

    def execute_backward_invariant_probe(
        self,
        probe_rows: list[PipelineRow],
        ctx: Any,
    ) -> TransformResult:
        """Execute the backward invariant probe.

        Defaults to the same execution path as the forward probe. Non-pass-through
        transforms can override this when their representative drop path needs a
        special local seam.
        """
        return self.execute_forward_invariant_probe(probe_rows, ctx)

    @staticmethod
    def _augment_invariant_probe_row(
        probe: PipelineRow,
        *,
        field_name: str,
        value: Any,
    ) -> PipelineRow:
        """Return ``probe`` plus one guaranteed field for invariant helpers."""
        from elspeth.contracts.contract_propagation import propagate_contract

        output = probe.to_dict().copy()
        output[field_name] = value
        contract = propagate_contract(
            probe.contract,
            output,
            transform_adds_fields=True,
        )
        return PipelineRow(output, contract)

    @property
    def self_created_input_fields(self) -> frozenset[str]:
        """Fields this transform CREATES, which must never be required on input.

        A transform's ``schema:`` block is its INPUT contract, but authors also
        use it to name the shape the transform emits. Requiring a field the
        transform exists to create is unsatisfiable: every row is rejected at
        ``TransformExecutor``'s ``input_schema.model_validate(..., strict=True)``
        (elspeth-d6eeb3a71d).

        ``input_schema`` demotes these to optional in the DERIVED INPUT pydantic
        model only. The ``SchemaConfig`` is left untouched, so the OUTPUT
        contract still guarantees them and ``guaranteed_fields`` stays legal at
        ``contracts/schema.py``.

        Note the limit of that: a single ``SchemaConfig`` still cannot be
        DECLARED input-optional and output-guaranteed — ``contracts/schema.py``
        rejects ``required: false`` alongside ``guaranteed_fields``. The
        framework DERIVES the combination here, below that layer; an author
        writing the config cannot express it directly and must declare
        ``required: true``.

        Defaults to ``declared_output_fields``. Override when the emitted set is
        not the right demotion set — ``ValueTransform`` keeps
        ``declared_output_fields`` empty (its targets may be overwrites, and a
        non-empty value would force the executor's collision check) yet still
        creates its operation targets.

        OVERRIDE AS A PROPERTY, not an assignment. This is a property, so
        ``self.self_created_input_fields = frozenset(...)`` in ``__init__``
        raises ``AttributeError: property has no setter``. It fails fast, but
        the shape is easy to get wrong, so: compute the set in ``__init__``,
        store it on a private attribute, and return that from a property
        override. ``FieldMapper`` is the reference shape. (``BatchStats`` and
        ``BatchDistributionProfile`` return an existing module constant or
        precomputed attribute directly, which is the same pattern without the
        extra field.)

        This answers "what does the transform WRITE", which on its own does not
        say whether the transform also READS the field. ``consumed_input_fields``
        supplies that half, and is subtracted before anything is demoted.
        """
        return self.declared_output_fields

    def _reject_input_options_naming_created_fields(self, input_naming_options: Mapping[str, str | None]) -> None:
        """Reject config options that point an INPUT column at a field this transform creates.

        An option like ``web_scrape.url_field`` names a column the transform
        READS; the transform then writes its created fields onto the same row.
        Aiming one at the other makes the transform consume its own output —
        it reads the column for a URL and immediately overwrites it with the
        scraped content — which is never what an author means.

        Nothing else rejects the shape. ``TransformExecutor``'s collision check
        compares ``declared_output_fields`` against the input keys OF A ROW, so
        it cannot fire until a row actually carries the column, and under
        ``mode: observed`` there is no declared field for DAG validation to
        carry either. Both authoring surfaces accepted it silently
        (elspeth-09dc6407f1).

        Call at the END of ``__init__``, after ``declared_output_fields`` (or a
        ``self_created_input_fields`` override) is populated — the created set
        is read here, not captured, so an earlier call sees an empty set and
        passes vacuously. Pass the option NAME with its resolved column so the
        error can say which option to repoint; that is the actionable half, and
        the created field is rarely the one to rename. ``None`` values are
        skipped, so optional locator options can be passed unconditionally.

        Every offending option is reported at once: repointing one must not
        merely reveal the next.

        Raises:
            PluginConfigError: If any option names a created field. This is the
                type the composer's probe tolerance recognizes
                (``_is_config_probe_exception``), so a draft pipeline surfaces
                a validation error rather than crashing validation.
        """
        from elspeth.plugins.infrastructure.config_base import PluginConfigError

        created = self.self_created_input_fields
        offenders = sorted((option, column) for option, column in input_naming_options.items() if column is not None and column in created)
        if not offenders:
            return

        validated_config = self._validated_config
        cause = (
            "; ".join(f"{option} names {column!r}, which {self.name} itself creates" for option, column in offenders)
            + ". Point "
            + " and ".join(option for option, _ in offenders)
            + " at a column that ARRIVES on the row, or rename the created field."
        )
        raise PluginConfigError(
            f"Invalid configuration for {self.name}: {cause}",
            cause=cause,
            plugin_class=None if validated_config is None else type(validated_config).__name__,
            plugin_name=self.name,
            component_type="transform",
        )

    @property
    def consumed_input_fields(self) -> frozenset[str]:
        """Fields this transform READS from the row, which must stay required.

        A field can be both consumed and created — a second-stage aggregation
        reading an upstream ``mean`` while emitting its own is the canonical
        case, because batch output names are generic. Demoting such a field
        would silently drop a genuine input requirement, turning a contract
        violation that was caught and audited at the transform boundary into an
        untyped failure deeper in the pipeline. So consumption always wins:
        ``input_schema`` demotes only ``self_created_input_fields`` MINUS this.

        The default reads three surfaces. Two already existed for declaring
        consumption — ADR-013's ``declared_input_fields`` (from
        ``required_input_fields``) and ``SchemaConfig.required_fields``; eleven
        of the twelve batch transforms already route their configured input
        columns through the latter.

        THOSE TWO ARE NON-NEGOTIABLE MEMBERS, and the reason is a layering
        invariant that is easy to destroy by "simplifying" them out. They are
        exactly what ``get_raw_node_required_fields`` (contracts/schema.py:861)
        reads to build the DAG's build-time required-field contract, and
        ``fields[].required`` — the surface demotion touches — is deliberately
        NOT part of it (core/dag/guarantees.py:96). So demotion changes only the
        surface the DAG does not check, and leaves untouched exactly the surface
        it does. Compile-time and runtime contracts therefore cannot diverge. Drop
        either surface from this union and a field the DAG still enforces at build
        time becomes demotable at runtime, silently splitting the two layers.

        That holds only while both surfaces are POPULATED, which is the harder
        half. Deleting a member is visible; leaving one empty is not. Ten
        transforms passed their validated ``schema_config`` to the schema factory
        and never stored it, so ``_schema_config`` kept its ``None`` class default
        and this limb contributed an empty frozenset — the same split, reached
        without touching this method (elspeth-3790106260). Both surfaces are now
        populated centrally in ``_initialize_declared_input_fields``, and
        ``tests/invariants/test_input_schema_config_is_captured.py`` asserts the
        build-time and runtime required sets agree across the live registry.
        The third is this transform's own config:
        any option that NAMES A COLUMN (``field``, ``group_by``, ``*_field``)
        contributes its value, unless the option is listed in
        ``output_naming_config_keys``.

        That third surface is deliberately fail-closed in the safe direction. An
        unclassified column option is treated as CONSUMED, so the worst case is
        a field that stays required rather than one whose requirement vanishes —
        and if the option really named an output, the parity probe and the
        registry sweep fail loudly, because the created field is then still
        demanded on input. Silence is not a possible outcome either way.

        RESIDUAL TRADE, stated so it is not silent: for a field the transform
        creates and does NOT declare as consumed, an author's ``required: true``
        is overruled and the presence requirement is dropped. That is deliberate
        — honouring it is the original elspeth-d6eeb3a71d trap, where every row
        was rejected for missing the field the transform exists to create — but
        it does mean the author's declaration is not the last word. A plugin
        that reads such a field MUST surface it here; the registry sweep in
        ``tests/unit/plugins/infrastructure/test_self_created_input_demotion.py``
        fails closed when a configured input column is demoted, so a new plugin
        in this shape is forced to declare its intent rather than lose the
        requirement quietly.
        """
        schema_config = self._schema_config
        declared_required = frozenset(schema_config.required_fields or ()) if schema_config is not None else frozenset()
        return self.declared_input_fields | declared_required | self._config_named_input_columns()

    def _config_named_input_columns(self) -> frozenset[str]:
        """Column names this transform's own config options point at for READING.

        Reads the VALIDATED config when one has been captured, so an option the
        author omitted still contributes its default. Reading the raw authored
        dict instead would make a defaulted input column invisible — e.g.
        ``blob_csv_expand.blob_ref_field`` defaults to ``"blob_ref"`` — and a
        column nobody can see is a column that gets demoted.
        """
        cached = self._config_columns_cache
        source: object = self.config if self._validated_config is None else self._validated_config
        if cached is not None and cached[0] is source:
            return cached[1]

        validated = self._validated_config
        if validated is None:
            options: Mapping[str, Any] = self.config
        else:
            # Read declared model fields rather than __dict__: that resolves
            # aliases and defaults the way pydantic itself does, and does not
            # depend on how the instance happens to store its attributes.
            options = {name: getattr(validated, name, None) for name in type(validated).model_fields}
        named: set[str] = set()
        for key, value in options.items():
            if key in self.output_naming_config_keys or not is_column_naming_config_option(key):
                continue
            if isinstance(value, str):
                named.add(value)
            elif isinstance(value, (list, tuple)):
                # Plural options hold a LIST of column names (keyword_filter.fields,
                # batch_data_quality_report.inspect_fields). Skipping non-str values
                # made every one of them invisible to demotion.
                named.update(item for item in value if isinstance(item, str))
        resolved = frozenset(named)
        # Config is fixed once construction finishes, so this is recompute-once
        # work; without the memo it was rebuilt on EVERY input_schema read, i.e.
        # once per row at the executor's validation site.
        self._config_columns_cache = (source, resolved)
        return resolved

    @property
    def demoted_input_fields(self) -> frozenset[str]:
        """Fields whose declared input contract this transform overrides.

        The framework demotes a field the transform CREATES even when the author
        declared it required, because honouring that declaration rejects every
        row (elspeth-d6eeb3a71d). That is a deliberate disagreement with the
        authored config, so it must be inspectable rather than implicit.

        Deliberately NOT written to the audit trail, and this is not debt. The
        value is a pure function of (plugin class, plugin config), and the run
        config is already captured in the audit record — so the override is
        RECONSTRUCTIBLE from what is stored. Audit records what cannot be
        re-derived (a token's fate, an operation that did or did not happen);
        this can be. Recording it would also mean an audit-table change, an epoch
        bump and a store wipe, for a fact already implied by the record.
        """
        declared = self._declared_input_schema
        if declared is None:
            return frozenset()
        return self._effective_demoted_fields(declared)

    def _effective_demoted_fields(self, declared: type[PluginSchema]) -> frozenset[str]:
        """The fields demotion will ACTUALLY change on ``declared``.

        Created-minus-consumed intersected with the model's own fields. The
        intersection matters: an observed-mode schema declares no fields at all,
        so a transform can create plenty and still change nothing here. Every
        consumer — the identity guard, the demotion itself, and the audit
        accessor — uses this one quantity so they cannot disagree.
        """
        demote = self.self_created_input_fields - self.consumed_input_fields
        return frozenset(demote & declared.model_fields.keys())

    @property
    def input_schema(self) -> type[PluginSchema]:
        """The derived input model, with self-created fields demoted to optional.

        Demotion happens here rather than at schema-construction time because
        transforms build their input model by several routes — ``_create_schemas``,
        a direct ``create_schema_from_config`` call — and some populate
        ``declared_output_fields`` only AFTER building it. Resolving lazily on
        read makes the invariant independent of both construction path and
        ordering.

        Only fields the transform creates and does NOT read are demoted, and
        demotion drops the PRESENCE requirement alone — a supplied value is
        still validated against the declared annotation and constraints.

        Returns the assigned model unchanged when there is nothing to demote.
        That keeps schema class identity stable for the overwhelming majority of
        transforms, which matters because identity is what distinguishes a
        shape-preserving transform (input and output are literally the same
        object) from a shape-changing one — the invariant asserted below.
        """
        declared = self._declared_input_schema
        if declared is None:
            # Name the public attribute, not the property's backing store: an
            # author reading this never assigned `_declared_input_schema`.
            raise AttributeError(
                f"{type(self).__name__} has no input_schema. Assign one in __init__ "
                f"(`self.input_schema = ...`, usually via `self._create_schemas(...)`) "
                f"or declare it in the class body."
            )
        demote = self._effective_demoted_fields(declared)
        # Unset output_schema means nothing is shared yet, so nothing to violate.
        if demote and getattr(self, "output_schema", None) is declared:
            # Shape-preserving transforms (_create_schemas with adds_fields=False)
            # share ONE model between input and output. Demoting would hand back a
            # subclass for input while output kept the original, silently splitting
            # a contract the DAG compares by identity. No shipped transform is in
            # this shape — a transform that creates fields is not shape-preserving —
            # so fail loudly rather than rely on that staying true.
            raise FrameworkBugError(
                f"{type(self).__name__} shares one schema object between input and "
                f"output (shape-preserving) but declares self-created fields "
                f"{sorted(demote)}. A transform that creates fields must build its "
                f"output schema separately (_create_schemas(..., adds_fields=True))."
            )
        cached = self._input_schema_cache
        # Re-derive whenever either input changes: a transform may populate the
        # fields backing self_created_input_fields after assigning the schema.
        if cached is not None and cached[0] is declared and cached[1] == demote:
            return cached[2]
        resolved = _demote_required_model_fields(declared, demote)
        self._input_schema_cache = (declared, demote, resolved)
        return resolved

    @input_schema.setter
    def input_schema(self, schema: type[PluginSchema]) -> None:
        self._declared_input_schema = schema
        self._input_schema_cache = None

    @staticmethod
    def _create_schemas(
        schema_config: Any,
        name: str,
        *,
        adds_fields: bool = False,
    ) -> tuple[type[PluginSchema], type[PluginSchema]]:
        """Create input/output schema pair from config.

        Reduces boilerplate for the common two-schema pattern:
        - Shape-preserving transforms: input and output share the same schema.
        - Shape-changing transforms: output uses observed mode (accepts any fields).

        Args:
            schema_config: The plugin's SchemaConfig instance.
            name: Plugin name for schema class naming.
            adds_fields: If True, output schema uses observed mode
                (accepts any fields since output shape is dynamic).

        Returns:
            Tuple of (input_schema, output_schema).
        """
        from elspeth.contracts.schema import SchemaConfig
        from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config

        input_schema = create_schema_from_config(
            schema_config,
            f"{name}Input",
            allow_coercion=False,
        )
        if adds_fields:
            output_schema = create_schema_from_config(
                SchemaConfig.from_dict({"mode": "observed"}),
                f"{name}Output",
                allow_coercion=False,
            )
        else:
            output_schema = input_schema
        return input_schema, output_schema

    def _build_output_schema_config(self, schema_config: SchemaConfig) -> SchemaConfig:
        """Build output schema config for DAG contract propagation.

        Merges the transform's declared_output_fields into guaranteed_fields
        so the DAG builder can validate downstream field requirements.

        Default assumes output ⊇ input fields (additive transforms). Subclasses
        with reductive output — where emitted rows do NOT carry the user's
        input ``fields``/``required_fields``/``guaranteed_fields`` — MUST
        override this method to drop input-side declarations. See
        ``BatchStats._build_output_schema_config`` (elspeth-f5f798f797) for the
        canonical override pattern.

        Args:
            schema_config: The transform's input schema config (base fields).

        Returns:
            SchemaConfig with guaranteed_fields including declared output fields.
        """
        from elspeth.contracts.schema import SchemaConfig, declare_missing_guaranteed_fields

        base_guaranteed = set(schema_config.guaranteed_fields or ())
        output_fields = base_guaranteed | self.declared_output_fields

        # Preserve None-vs-empty-tuple semantics: None = abstain, () = explicitly empty.
        # If upstream declared guarantees or we computed non-empty output, declare explicitly.
        upstream_declared = schema_config.guaranteed_fields is not None
        if upstream_declared or output_fields:
            guaranteed_fields_result = tuple(sorted(output_fields))
        else:
            guaranteed_fields_result = None

        return SchemaConfig(
            mode=schema_config.mode,
            fields=declare_missing_guaranteed_fields(schema_config.fields, guaranteed_fields_result),
            guaranteed_fields=guaranteed_fields_result,
            audit_fields=schema_config.audit_fields,
            required_fields=schema_config.required_fields,
        )

    def process(
        self,
        row: PipelineRow,
        ctx: TransformContext,
    ) -> TransformResult:
        """Process a single row.

        Single-row transforms must override this method.
        Batch-aware transforms (is_batch_aware=True) should override with
        signature: process(self, rows: list[PipelineRow], ctx) -> TransformResult

        Args:
            row: Input row as PipelineRow (immutable, supports dual-name access)
            ctx: Plugin context

        Returns:
            TransformResult with processed row dict or error
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement process(). "
            f"Single-row transforms: process(row: PipelineRow, ctx) -> TransformResult. "
            f"Batch-aware transforms: process(rows: list[PipelineRow], ctx) -> TransformResult."
        )

    def close(self) -> None:  # noqa: B027 - optional override, not abstract
        """Release resources (connections, file handles, thread pools).

        Called once per run after on_complete(), inside a finally block.
        No context is available -- this is pure resource teardown.

        Guaranteed to be called if on_start() succeeded, even when the
        pipeline crashes mid-processing. NOT called if on_start() itself
        raises. Each plugin's close() is individually protected: one
        plugin's failure does not prevent others from closing.

        Called on the main thread.
        """
        pass

    # === Lifecycle Hooks ===
    # These are intentionally empty - optional hooks for subclasses to override.
    #
    # Call ordering (orchestrator, main thread):
    #   1. on_start(ctx)    -- before any rows are processed
    #   2. process(row, ctx) -- per row (or per batch)
    #   3. on_complete(ctx)  -- after all rows, or after pipeline error
    #   4. close()           -- resource teardown (always after on_complete)
    #
    # on_complete() and close() run inside a finally block, so they execute
    # even when the pipeline crashes. However, if on_start() raises, neither
    # on_complete() nor close() is called for ANY plugin.
    #
    # Resume path: on_start/on_complete/close are called normally for
    # transforms during resume runs.

    def on_start(self, ctx: LifecycleContext) -> None:
        """Called once before any rows are processed.

        Override for per-run initialization: capturing the recorder,
        acquiring rate limiters, initializing tracing, etc.

        Called on the main thread. If this raises, the pipeline aborts
        and neither on_complete() nor close() will be called.

        Subclasses MUST call super().on_start(ctx) to set the lifecycle flag.
        """
        self._on_start_called = True

    def on_complete(self, ctx: LifecycleContext) -> None:
        """Called after all rows are processed (or after pipeline error).

        Override for recording final metrics, flushing application-level
        buffers, or updating audit state. Always called before close().

        Called on the main thread. Receives LifecycleContext so it can
        interact with the landscape and telemetry. Individually protected:
        if this raises, other plugins still get their on_complete/close calls.

        Subclasses MUST call super().on_complete(ctx) to set the lifecycle flag.
        """
        self._on_complete_called = True

    def runtime_preflight(self, ctx: LifecycleContext) -> None:  # noqa: B027 - optional override, not abstract
        """Run an optional transform readiness check before source iteration.

        Only called when requires_runtime_preflight is True. Implementations may
        make bounded external calls through the injected audit context; failures
        abort the run before any source rows are loaded.
        """
        pass

    # ── Plugin-declared semantics (Phase 1: optional, default empty) ──
    # Override on a subclass to declare what the plugin emits / requires.
    # The generic semantic validator compares producer facts to consumer
    # requirements when the configured field names match.

    def output_semantics(self) -> OutputSemanticDeclaration:
        """Return semantic facts for the fields this transform emits.

        Default returns an empty declaration: the transform makes no
        semantic claims beyond what the schema contract already
        expresses. Override to declare ContentKind/TextFraming for
        configured output fields.
        """
        from elspeth.contracts.plugin_semantics import OutputSemanticDeclaration

        return OutputSemanticDeclaration()

    def input_semantic_requirements(self) -> InputSemanticRequirements:
        """Return semantic requirements for fields this transform consumes.

        Default returns no requirements. Override to declare that a
        configured input field must satisfy specific ContentKind /
        TextFraming acceptance sets.
        """
        from elspeth.contracts.plugin_semantics import InputSemanticRequirements

        return InputSemanticRequirements()

    @classmethod
    def get_agent_assistance(
        cls,
        *,
        issue_code: str | None = None,
    ) -> PluginAssistance | None:
        """Return deterministic guidance for this plugin.

        Dual-use by ``issue_code``:

        * ``issue_code is None`` — discovery-time guidance for an LLM
          or operator selecting this plugin. Override to return a
          ``PluginAssistance`` with a one-line ``summary`` of what the
          plugin does and 1-5 short ``composer_hints`` imperatives
          (≤140 chars each). ``suggested_fixes`` and ``examples`` may
          be empty.
        * ``issue_code is not None`` — failure-time guidance. The
          validator attached the code; the plugin owns the prose.
          Return a ``PluginAssistance`` with ``suggested_fixes`` and,
          where useful, before/after ``examples``.

        Default returns None for both branches: the plugin offers no
        guidance. The catalog uses the ``None`` return as a signal to
        emit empty ``composer_hints`` on the discovery DTO.
        """
        return None

    @classmethod
    def get_post_call_hints(
        cls,
        *,
        tool_name: str,
        config_snapshot: Mapping[str, object],
    ) -> tuple[str, ...]:
        """Return forward-looking hints conditional on the just-set config.

        Called by the composer MCP tool layer after a successful
        mutation (``set_source``, ``upsert_node``, ``patch_*_options``).
        The plugin inspects its *own* configuration and returns 0-N
        short imperatives the operator/LLM should consider before
        moving on. Examples: "you declared schema.mode: fixed — did
        you call inspect_source first?" or "your prompt contains a
        subjective term — did you call request_interpretation_review?"

        Two-parameter contract is deliberate. Plugins do not see
        composer state or sibling nodes — cross-node concerns belong
        in the validator subsystem. Hints are local to the plugin's
        own config.

        Default returns an empty tuple. Same audit-hash discipline as
        ``get_agent_assistance``: advisory coaching, not contract.
        """
        return ()


class BaseSink(ABC, SinkEffectContract):
    """Base class for sink plugins.

    Subclass and implement write(), flush(), close().

    Lifecycle (called by the orchestrator on the main thread):

        1. on_start(ctx)     -- per-run initialization (before any writes)
        2. write(rows, ctx)  -- called in batches as rows reach this sink
        3. flush()           -- called before checkpoints to guarantee durability
        4. on_complete(ctx)  -- all rows written (or pipeline errored)
        5. close()           -- release resources (file handles, connections)

    Guarantees:
        - on_start() is called once before any write() call.
        - on_complete() and close() run inside a finally block, so they
          execute even when the pipeline crashes mid-processing. However,
          if on_start() raises, neither on_complete() nor close() is called.
        - on_complete() is called before close(). Both are called regardless
          of whether processing succeeded or failed.
        - Each plugin's on_complete()/close() is individually protected: one
          plugin's cleanup failure does not prevent other plugins from
          cleaning up.

    on_complete vs close:
        - on_complete(ctx): "Processing is done." Use for finalizing output
          format (e.g., writing JSON array closing bracket), recording metrics,
          or updating audit state. Receives LifecycleContext.
        - close(): "Release all resources." Use for closing file handles or
          network connections. No context -- pure resource teardown.

    Example:
        class CSVSink(BaseSink):
            name = "csv"
            input_schema = RowSchema
            idempotent = False

            def write(self, rows: list[dict], ctx: SinkContext) -> SinkWriteResult:
                for row in rows:
                    self._writer.writerow(row)
                return SinkWriteResult(
                    artifact=ArtifactDescriptor.for_file(
                        path=self._path,
                        content_hash=self._compute_hash(),
                        size_bytes=self._file.tell(),
                    ),
                )

            def flush(self) -> None:
                self._file.flush()

            def close(self) -> None:
                self._file.close()
    """

    name: str
    input_schema: type[PluginSchema]
    idempotent: bool = False
    node_id: str | None = None  # Set by orchestrator after registration

    # Audit metadata
    determinism: Determinism = Determinism.IO_WRITE
    plugin_version: str = "0.0.0"
    source_file_hash: str | None = None

    # Recoverable publication is an explicit per-adapter opt-in. Legacy sinks
    # remain visibly unsupported until they implement the full effect protocol.
    effect_protocol_version: ClassVar[str | None] = None
    supported_effect_modes: ClassVar[frozenset[str]] = frozenset()
    supported_effect_input_kinds: ClassVar[frozenset[SinkEffectInputKind]] = frozenset()
    effect_mode_remediation: ClassVar[str | None] = None

    @classmethod
    def _resolve_sink_effect_mode(
        cls,
        config: Mapping[str, object],
        *,
        purpose: SinkEffectExecutionPurpose,
    ) -> ResolvedSinkEffectMode | None:
        """Adapter-owned, local mode resolution seam for runtime construction."""
        del cls, config, purpose
        return None

    def _validate_sink_effect_capability_configuration(
        self,
        *,
        mode: str,
        required_input_kind: SinkEffectInputKind,
    ) -> None:
        """Validate adapter-specific state against effect admission.

        Adapters whose live state can diverge from their resolved options
        override this hook. The base implementation is an explicit no-op for
        adapters whose validated configuration has no second representation.
        """
        del mode, required_input_kind

    def _resolve_audit_export_publication_preflight(
        self,
        export_format: AuditExportFormat,
    ) -> Callable[[], None] | None:
        """Bind any plugin-owned publication probe from validated instance state."""
        if type(export_format) is not AuditExportFormat:
            raise TypeError("audit export format must be an exact AuditExportFormat")
        return None

    # ── Catalogue reference content ─────────────────────────────────────
    # These fields populate the catalog's reference cards. They are
    # documentation, not configuration — authors fill them in to explain
    # to a human reader (compliance, research, ops) what this plugin
    # does, when it's the right choice, when it isn't, and what audit
    # characteristics it has. Base defaults remain optional for third-party
    # and legacy compatibility; repository tests require every registered
    # built-in to be complete. See
    # docs/contracts/plugin-catalogue-reference-content.md.

    usage_when_to_use: str | None = None
    """Persona-facing prose. One short paragraph answering "when should I
    pick this plugin?" — written for compliance / research / ops readers,
    not for plugin developers. Avoid restating the technical
    description; that's what the docstring is for."""

    usage_when_not_to_use: str | None = None
    """Persona-facing prose. One short paragraph answering "when should I
    *not* pick this plugin?" State a hard limitation or unsafe fit and
    redirect the reader to a concrete alternative where one exists."""

    example_use: str | None = None
    """One bounded YAML component fragment with realistic options and
    non-secret values. Use top-level ``sources`` for sources, ``transform``
    for ordinary transforms, ``aggregations`` for batch-aware transforms,
    and ``sinks`` for sinks. Preserve whitespace for preformatted rendering."""

    capability_tags: tuple[str, ...] = ()
    web_config_authority: WebConfigAuthority = WebConfigAuthority.USER_CONFIGURABLE
    policy_capabilities: frozenset[CapabilityDeclaration] = frozenset()
    """Short lowercase tags that drive catalog filter chips and fuzzy
    search. Examples: ("csv", "file", "batch") for csv_source;
    ("http", "network", "scraping") for a web-scrape transform. Tags
    are non-exhaustive; pick the two to six most useful for a user
    who is searching the catalog.

    See ``BaseTransform.capability_tags`` for the open-vocabulary
    rationale (why this is bare ``tuple[str, ...]`` rather than a
    closed-vocabulary enum like ``audit_characteristics`` below)."""

    audit_characteristics: DeclaredAuditCharacteristics = frozenset()
    """Declared audit characteristics that the framework cannot derive
    from other attributes. The catalog service composes this set with
    the characteristic derived from `determinism` at summary-build time.
    Declare members of the :class:`~elspeth.contracts.enums.AuditCharacteristic`
    enum (e.g. ``AuditCharacteristic.SIGNED``, ``AuditCharacteristic.CREDENTIALS``,
    ``AuditCharacteristic.QUARANTINE``, ``AuditCharacteristic.PROVENANCE``) —
    the enum itself is the closed vocabulary; typos fail mypy at the
    declaration site rather than disappearing silently from the rendered
    catalog card."""

    discovery_secret_requirements: Mapping[str, tuple[str, ...]] = {}
    """Credential-bearing config fields that must have a configured secret ref
    before composer discovery advertises the plugin. See BaseTransform."""

    # Config model — each subclass sets this to its Pydantic config class.
    config_model: ClassVar[type[PluginConfig] | None] = None

    @classmethod
    def get_config_model(cls, config: dict[str, Any] | None = None) -> type[PluginConfig] | None:
        """Return the Pydantic config model for this plugin type."""
        return cls.config_model

    @classmethod
    def get_config_schema(cls) -> dict[str, Any]:
        """Default ``get_config_schema`` — renders a single Pydantic model.

        See :meth:`~elspeth.contracts.plugin_protocols.SinkProtocol.get_config_schema`
        for the canonical contract, including the MUST-override rule for
        plugins whose effective configuration is a discriminated union.
        """
        if cls.config_model is None:
            return {}
        schema: dict[str, Any] = cls.config_model.model_json_schema()
        return schema

    # Default: sinks don't support resume. Override in subclasses that can append.
    supports_resume: bool = False

    # Required-field enforcement (centralized in SinkExecutor).
    # Sinks set this from schema_config.get_effective_required_fields() at init.
    # Empty frozenset = no required-field check.
    declared_required_fields: frozenset[str] = frozenset()

    # Failsink infrastructure — set by orchestrator from SinkSettings.on_write_failure.
    # None until injected at pipeline startup; "discard" or sink name at runtime.
    _on_write_failure: str | None

    def configure_for_resume(self) -> None:
        """Configure sink for resume mode (append instead of truncate).

        Called by engine when resuming a run. Override in sinks that support
        resume to switch from truncate mode to append mode.

        Default implementation raises NotImplementedError. Subclasses that
        set supports_resume=True MUST override this method.

        Raises:
            NotImplementedError: If sink cannot be resumed.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support resume. "
            f"To make this sink resumable, set supports_resume=True and "
            f"implement configure_for_resume()."
        )

    def validate_output_target(self) -> OutputValidationResult:
        """Validate existing output target matches configured schema.

        Called by engine/CLI before write operations in append/resume mode.
        Default returns valid=True (dynamic schema or no existing target).

        Sinks that set supports_resume=True SHOULD override this to validate
        that the existing output target (file/table) matches the schema.

        Returns:
            OutputValidationResult indicating compatibility.
        """
        from elspeth.contracts.sink import OutputValidationResult

        return OutputValidationResult.success()

    @property
    def needs_resume_field_resolution(self) -> bool:
        """Whether this sink needs field resolution mapping for resume.

        True when headers mode is ORIGINAL — the CLI resume path must
        provide the source field resolution mapping before validation.

        Set by init_display_headers(). Sinks that don't use display headers
        return False (the default).
        """
        return self._needs_resume_field_resolution

    def set_resume_field_resolution(self, resolution_mapping: dict[str, str]) -> None:
        """Set field resolution mapping for resume validation.

        Default is a no-op unless the sink declares that resume field
        resolution is required. Sinks with headers: original mode must
        override this to use the mapping for validation.

        Args:
            resolution_mapping: Dict mapping original header name -> normalized field name.
        """
        if self.needs_resume_field_resolution:
            raise NotImplementedError(
                f"{self.__class__.__name__} requires resume field resolution but does not implement set_resume_field_resolution()."
            )
        _ = resolution_mapping  # Explicitly consume the argument

    # Output contract for schema-aware sinks
    _output_contract: SchemaContract | None = None

    # Display header state — set by init_display_headers() in subclass __init__.
    # Declared here for mypy structural typing against DisplayHeaderHost protocol.
    _headers_mode: HeaderMode
    _headers_custom_mapping: dict[str, str] | None
    _resolved_display_headers: dict[str, str] | None
    _display_headers_resolved: bool
    _needs_resume_field_resolution: bool

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # Enforces the contract documented in contracts/enums.py:Determinism —
        # every plugin MUST declare a Determinism value at registration. See
        # BaseTransform.__init_subclass__ for the rationale; this hook is the
        # sink-side mirror. A sink that performs IO_WRITE inherits the right
        # value by accident today; one whose semantics differ (e.g. an
        # idempotent metrics sink that author classifies DETERMINISTIC, or
        # an EXTERNAL_CALL sink that posts to a webhook) would silently
        # mis-record without this guard.
        super().__init_subclass__(**kwargs)
        if "determinism" not in cls.__dict__:
            raise TypeError(
                f"{cls.__qualname__} inherits from BaseSink but does not "
                f"explicitly declare a `determinism` class attribute. Every "
                f"plugin MUST declare its determinism classification at "
                f"registration; there is no default (see "
                f"elspeth.contracts.enums.Determinism). Add one of: "
                f"`determinism = Determinism.DETERMINISTIC`, "
                f"`Determinism.SEEDED`, `Determinism.IO_READ`, "
                f"`Determinism.IO_WRITE`, `Determinism.EXTERNAL_CALL`, or "
                f"`Determinism.NON_DETERMINISTIC` to the class body. "
                f"Redeclaring the same value as the parent is acceptable — "
                f"the point is explicit author declaration, so that an audit "
                f"reader can read the source and see which value the author "
                f"chose, rather than tracing inheritance to find it."
            )

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize with configuration.

        Args:
            config: Plugin configuration
        """
        self.config = config
        # Per-instance lifecycle guards — see BaseTransform.__init__ for
        # the full rationale (lifecycle-guard contract, missing-super()
        # detection, why class-level state would mask bugs).
        self._on_start_called: bool = False
        self._on_complete_called: bool = False
        self._on_write_failure: str | None = None
        self._output_contract = None
        self._needs_resume_field_resolution = False
        self._diversion_log: list[RowDiversion] = []

    @abstractmethod
    def write(
        self,
        rows: list[dict[str, Any]],
        ctx: SinkContext,
    ) -> SinkWriteResult:
        """Write a batch of rows to the sink.

        Args:
            rows: List of row dicts to write
            ctx: Sink context with run identity and recording methods

        Returns:
            SinkWriteResult with artifact descriptor and optional diversions
        """
        ...

    @abstractmethod
    def flush(self) -> None:
        """Flush buffered data."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Release resources (file handles, connections).

        Called once per run after on_complete(), inside a finally block.
        Guaranteed to be called if on_start() succeeded, even on pipeline
        crash. NOT called if on_start() itself raises. Each plugin's
        close() is individually protected. Called on the main thread.
        """
        ...

    # === Output Contract Support ===

    def get_output_contract(self) -> SchemaContract | None:
        """Get the current output contract.

        Returns:
            SchemaContract if set, None otherwise
        """
        return self._output_contract

    def set_output_contract(self, contract: SchemaContract) -> None:
        """Set or update the output contract.

        Used for schema-aware sinks that need field metadata (e.g., for
        restoring original header names in CSV output).

        Args:
            contract: The schema contract to use for output operations
        """
        self._output_contract = contract

    # === Diversion Infrastructure ===

    def _divert_row(self, row_data: dict[str, Any], *, row_index: int, reason: str) -> None:
        """Record a row diversion during write().

        Called by concrete sinks when an individual row fails at the
        external system boundary (Tier 2 -> External). Both "discard"
        and failsink modes accumulate to _diversion_log. The executor
        reads the log after write() returns and handles the actual
        discard-vs-write decision.

        Args:
            row_data: The row dict that couldn't be written.
            row_index: Index in the original batch (for token correlation).
            reason: Human-readable reason for the diversion.

        Raises:
            FrameworkBugError: If _on_write_failure has not been set
                (plugin bug — calling _divert_row before orchestrator injection).
        """
        if self._on_write_failure is None:
            raise FrameworkBugError(
                f"Sink '{self.name}' called _divert_row() but _on_write_failure "
                f"is not set. Configure on_write_failure in pipeline YAML or "
                f"re-raise the exception to crash the pipeline."
            )
        self._diversion_log.append(RowDiversion(row_index=row_index, reason=reason, row_data=row_data))

    def _reset_diversion_log(self) -> None:
        """Clear the diversion log. Called by SinkExecutor before each write()."""
        self._diversion_log.clear()

    def _get_diversions(self) -> tuple[RowDiversion, ...]:
        """Return accumulated diversions as an immutable tuple."""
        return tuple(self._diversion_log)

    # === Lifecycle Hooks ===
    # Call ordering: on_start -> write/flush -> on_complete -> close
    # See class docstring for full lifecycle contract and guarantees.

    def on_start(self, ctx: LifecycleContext) -> None:
        """Called once before any write() call.

        Override for per-run initialization. Called on the main thread.
        If this raises, the pipeline aborts and neither on_complete()
        nor close() will be called.

        Subclasses MUST call super().on_start(ctx) to set the lifecycle flag.
        """
        self._on_start_called = True

    def on_complete(self, ctx: LifecycleContext) -> None:
        """Called after all rows are written (or after pipeline error), before close().

        Override for finalizing output format, recording metrics, or
        updating audit state. Called on the main thread. Individually
        protected: if this raises, other plugins still get their calls.

        Subclasses MUST call super().on_complete(ctx) to set the lifecycle flag.
        """
        self._on_complete_called = True

    @classmethod
    def get_agent_assistance(
        cls,
        *,
        issue_code: str | None = None,
    ) -> PluginAssistance | None:
        """Return deterministic guidance for this sink. See ``BaseTransform.get_agent_assistance``."""
        return None

    @classmethod
    def get_post_call_hints(
        cls,
        *,
        tool_name: str,
        config_snapshot: Mapping[str, object],
    ) -> tuple[str, ...]:
        """Return forward-looking hints conditional on the just-set sink config.

        See ``BaseTransform.get_post_call_hints`` for the full contract.
        Sinks typically hint on collision policy, write mode (insert /
        upsert / replace), and serialization format choices.
        """
        return ()


class BaseSource(ABC):
    """Base class for source plugins.

    Subclass and implement load() and close().

    Lifecycle (called by the orchestrator on the main thread):

        1. on_start(ctx)  -- per-run initialization (before load)
        2. load(ctx)      -- yields SourceRow instances
        3. on_complete(ctx) -- source exhausted (or pipeline errored)
        4. close()        -- release resources (file handles, connections)

    Guarantees:
        - on_start() is called once before load().
        - on_complete() and close() run inside a finally block, so they
          execute even when the pipeline crashes mid-processing. However,
          if on_start() raises, neither on_complete() nor close() is called.
        - on_complete() is called before close(). Both are called regardless
          of whether processing succeeded or failed.
        - Each plugin's on_complete()/close() is individually protected.

    Resume path: Source lifecycle hooks (on_start, on_complete, close) are
    skipped during resume runs because NullSource is used and row data comes
    from stored payloads, not from the original source.

    on_complete vs close:
        - on_complete(ctx): "Loading is done." Receives LifecycleContext.
        - close(): "Release all resources." No context -- pure teardown.

    Example:
        class CSVSource(BaseSource):
            name = "csv"
            output_schema = RowSchema

            def load(self, ctx: SourceContext) -> Iterator[SourceRow]:
                with open(self.config["path"]) as f:
                    reader = csv.DictReader(f)
                    for source_row_index, row in enumerate(reader):
                        yield SourceRow.valid(row, contract=contract, source_row_index=source_row_index)

            def close(self) -> None:
                pass  # File already closed by context manager
    """

    name: str
    output_schema: type[PluginSchema]
    node_id: str | None = None  # Set by orchestrator after registration

    # Audit metadata
    determinism: Determinism = Determinism.IO_READ
    plugin_version: str = "0.0.0"
    source_file_hash: str | None = None

    # ── Catalogue reference content ─────────────────────────────────────
    # These fields populate the catalog's reference cards. They are
    # documentation, not configuration — authors fill them in to explain
    # to a human reader (compliance, research, ops) what this plugin
    # does, when it's the right choice, when it isn't, and what audit
    # characteristics it has. Base defaults remain optional for third-party
    # and legacy compatibility; repository tests require every registered
    # built-in to be complete. See
    # docs/contracts/plugin-catalogue-reference-content.md.

    usage_when_to_use: str | None = None
    """Persona-facing prose. One short paragraph answering "when should I
    pick this plugin?" — written for compliance / research / ops readers,
    not for plugin developers. Avoid restating the technical
    description; that's what the docstring is for."""

    usage_when_not_to_use: str | None = None
    """Persona-facing prose. One short paragraph answering "when should I
    *not* pick this plugin?" State a hard limitation or unsafe fit and
    redirect the reader to a concrete alternative where one exists."""

    example_use: str | None = None
    """One bounded YAML component fragment with realistic options and
    non-secret values. Use top-level ``sources`` for sources, ``transform``
    for ordinary transforms, ``aggregations`` for batch-aware transforms,
    and ``sinks`` for sinks. Preserve whitespace for preformatted rendering."""

    capability_tags: tuple[str, ...] = ()
    web_config_authority: WebConfigAuthority = WebConfigAuthority.USER_CONFIGURABLE
    policy_capabilities: frozenset[CapabilityDeclaration] = frozenset()
    """Short lowercase tags that drive catalog filter chips and fuzzy
    search. Examples: ("csv", "file", "batch") for csv_source;
    ("http", "network", "scraping") for a web-scrape transform. Tags
    are non-exhaustive; pick the two to six most useful for a user
    who is searching the catalog.

    See ``BaseTransform.capability_tags`` for the open-vocabulary
    rationale (why this is bare ``tuple[str, ...]`` rather than a
    closed-vocabulary enum like ``audit_characteristics`` below)."""

    audit_characteristics: DeclaredAuditCharacteristics = frozenset()
    """Declared audit characteristics that the framework cannot derive
    from other attributes. The catalog service composes this set with
    the characteristic derived from `determinism` at summary-build time.
    Declare members of the :class:`~elspeth.contracts.enums.AuditCharacteristic`
    enum (e.g. ``AuditCharacteristic.SIGNED``, ``AuditCharacteristic.CREDENTIALS``,
    ``AuditCharacteristic.QUARANTINE``, ``AuditCharacteristic.PROVENANCE``) —
    the enum itself is the closed vocabulary; typos fail mypy at the
    declaration site rather than disappearing silently from the rendered
    catalog card."""

    discovery_secret_requirements: Mapping[str, tuple[str, ...]] = {}
    """Credential-bearing config fields that must have a configured secret ref
    before composer discovery advertises the plugin. See BaseTransform."""

    # Config model — each subclass sets this to its Pydantic config class.
    # NullSource sets this to None (no config validation needed).
    config_model: ClassVar[type[PluginConfig] | None] = None

    @classmethod
    def get_config_model(cls, config: dict[str, Any] | None = None) -> type[PluginConfig] | None:
        """Return the Pydantic config model for this plugin type.

        Returns None for sources with no config (e.g. NullSource).
        """
        return cls.config_model

    @classmethod
    def get_config_schema(cls) -> dict[str, Any]:
        """Default ``get_config_schema`` — renders a single Pydantic model.

        See :meth:`~elspeth.contracts.plugin_protocols.SourceProtocol.get_config_schema`
        for the canonical contract, including the MUST-override rule for
        plugins whose effective configuration is a discriminated union.
        """
        if cls.config_model is None:
            return {}
        schema: dict[str, Any] = cls.config_model.model_json_schema()
        return schema

    # Sink name for quarantined rows, or "discard" to drop invalid rows
    # All sources must set this - config-based sources get it from SourceDataConfig
    _on_validation_failure: str

    # Success routing: sink name for rows that pass source validation.
    # Always non-None at runtime (SourceSettings requires on_success).
    # Base class default is None because injection happens post-construction
    # via runtime_factory bridge (set from SourceSettings.on_success).
    on_success: str | None = None

    # Guaranteed-field enforcement (centralized in the source boundary contract).
    # Sources set this from schema_config.get_effective_guaranteed_fields() at init.
    # Empty frozenset = no guaranteed-field contract.
    declared_guaranteed_fields: frozenset[str] = frozenset()

    # Plugin-computed output contract, recorded by
    # _initialize_declared_guaranteed_fields(). The DAG builder prefers this
    # over re-parsing the raw options dict — the source-side mirror of
    # BaseTransform._output_schema_config — so source-specific schema rewrites
    # (e.g. the LLM source's guaranteed-field augmentation) reach build-time
    # graph validation, not just per-row enforcement (elspeth-db98d3f660).
    _output_schema_config: SchemaConfig | None

    # Schema contract for row validation
    _schema_contract: SchemaContract | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # Enforces the contract documented in contracts/enums.py:Determinism —
        # every plugin MUST declare a Determinism value at registration. See
        # BaseTransform.__init_subclass__ for the rationale; this hook is the
        # source-side mirror. A new source plugin that reads from a live REST
        # API (EXTERNAL_CALL) but inherits the IO_READ default would record
        # the wrong determinism per-node in Landscape and produce a wrong
        # ReproducibilityGrade for any run using it.
        super().__init_subclass__(**kwargs)
        if "determinism" not in cls.__dict__:
            raise TypeError(
                f"{cls.__qualname__} inherits from BaseSource but does not "
                f"explicitly declare a `determinism` class attribute. Every "
                f"plugin MUST declare its determinism classification at "
                f"registration; there is no default (see "
                f"elspeth.contracts.enums.Determinism). Add one of: "
                f"`determinism = Determinism.DETERMINISTIC`, "
                f"`Determinism.SEEDED`, `Determinism.IO_READ`, "
                f"`Determinism.IO_WRITE`, `Determinism.EXTERNAL_CALL`, or "
                f"`Determinism.NON_DETERMINISTIC` to the class body. "
                f"Redeclaring the same value as the parent is acceptable — "
                f"the point is explicit author declaration, so that an audit "
                f"reader can read the source and see which value the author "
                f"chose, rather than tracing inheritance to find it."
            )

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize with configuration.

        Args:
            config: Plugin configuration
        """
        self.config = config
        # Per-instance lifecycle guards — see BaseTransform.__init__ for
        # the full rationale (lifecycle-guard contract, missing-super()
        # detection, why class-level state would mask bugs).
        self._on_start_called: bool = False
        self._on_complete_called: bool = False
        self._schema_contract = None
        self.declared_guaranteed_fields = frozenset()
        self._output_schema_config: SchemaConfig | None = None

    @abstractmethod
    def load(self, ctx: SourceContext) -> Iterator[SourceRow]:
        """Load and yield rows from the source.

        Args:
            ctx: Source context with run metadata and recording methods

        Yields:
            SourceRow for each row - either SourceRow.valid() for rows that
            passed validation, or SourceRow.quarantined() for invalid rows.
        """
        ...

    @abstractmethod
    def close(self) -> None:
        """Release resources (file handles, connections).

        Called once per run after on_complete(), inside a finally block.
        Guaranteed to be called if on_start() succeeded, even on pipeline
        crash. NOT called if on_start() itself raises. Each plugin's
        close() is individually protected. Called on the main thread.

        Skipped during resume runs (source is not opened).
        """
        ...

    # === Schema Contract Support ===

    def get_schema_contract(self) -> SchemaContract | None:
        """Get the current schema contract.

        Returns:
            SchemaContract if set, None otherwise
        """
        return self._schema_contract

    def require_schema_contract(self) -> SchemaContract:
        """Return the current schema contract or crash on framework invariant failure."""
        contract = self.get_schema_contract()
        if contract is None:
            raise FrameworkBugError(
                f"{type(self).__name__} attempted to yield SourceRow.valid() before establishing "
                "a schema contract. Source plugins must call set_schema_contract() before "
                "emitting valid rows."
            )
        return contract

    def set_schema_contract(self, contract: SchemaContract) -> None:
        """Set or update the schema contract.

        Called during initialization for explicit schemas (FIXED/FLEXIBLE),
        or after first-row inference for OBSERVED mode.

        Args:
            contract: The schema contract to use for validation
        """
        self._schema_contract = contract

    def _initialize_declared_guaranteed_fields(self, schema_config: SchemaConfig) -> None:
        """Normalize the source's runtime guarantee declaration from SchemaConfig.

        Call this after any source-specific schema rewrite so the runtime
        contract surface matches the source's effective guarantees, not the
        caller's raw config dict. Also records the schema as the source's
        plugin-computed output contract, which the DAG builder prefers over
        re-parsing raw options (elspeth-db98d3f660).
        """
        self.declared_guaranteed_fields = schema_config.get_effective_guaranteed_fields()
        self._output_schema_config = schema_config

    # === Lifecycle Hooks ===
    # Call ordering: on_start -> load -> on_complete -> close
    # See class docstring for full lifecycle contract and guarantees.
    # Skipped entirely during resume runs (NullSource is used instead).

    def on_start(self, ctx: LifecycleContext) -> None:
        """Called once before load().

        Override for per-run initialization. Called on the main thread.
        If this raises, the pipeline aborts and neither on_complete()
        nor close() will be called.

        Skipped during resume runs.

        Subclasses MUST call super().on_start(ctx) to set the lifecycle flag.
        """
        self._on_start_called = True

    def on_complete(self, ctx: LifecycleContext) -> None:
        """Called after load() completes (or after pipeline error), before close().

        Override for recording final metrics or updating audit state.
        Called on the main thread. Individually protected: if this raises,
        other plugins still get their on_complete/close calls.

        Skipped during resume runs.

        Subclasses MUST call super().on_complete(ctx) to set the lifecycle flag.
        """
        self._on_complete_called = True

    # === Audit Trail Metadata ===

    def get_field_resolution(self) -> tuple[Mapping[str, str], str | None] | None:
        """Return field resolution mapping computed during load().

        Sources that perform field normalization (e.g., CSVSource with field normalization)
        should override this to return the mapping from original header names to final
        field names. This enables audit trail to recover original headers.

        Must be called AFTER load() has been invoked (resolution is computed lazily
        when file headers are read).

        Returns:
            Tuple of (resolution_mapping, normalization_version) if field resolution
            was performed, or None if no normalization occurred. The resolution_mapping
            is a dict mapping original header name → final field name.
        """
        return None  # Default: no field resolution metadata

    # === Composer assistance hooks ===

    @classmethod
    def get_agent_assistance(
        cls,
        *,
        issue_code: str | None = None,
    ) -> PluginAssistance | None:
        """Return deterministic guidance for this source. See ``BaseTransform.get_agent_assistance``."""
        return None

    @classmethod
    def get_post_call_hints(
        cls,
        *,
        tool_name: str,
        config_snapshot: Mapping[str, object],
    ) -> tuple[str, ...]:
        """Return forward-looking hints conditional on the just-set source config.

        See ``BaseTransform.get_post_call_hints`` for the full contract.
        Sources typically hint on Tier 3 absence-vs-fabrication semantics,
        schema-mode selection, and encoding handling.
        """
        return ()
