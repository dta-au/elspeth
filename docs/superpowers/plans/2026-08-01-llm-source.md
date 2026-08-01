# Source-Native Single-Prompt LLM Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a source-only `llm` plugin that makes one static-prompt provider call and emits one response/usage/model row, while closing every required-control and external-egress parity gap for source-kind LLMs.

**Architecture:** The source implements `BaseSource` directly and owns source lifecycle, schema validation, and a single emitted `SourceRow`; it shares provider adapters and pure LLM helpers without constructing a transform or fake row. Provider calls receive a discriminated `LLMAuditParent`, allowing transforms to retain state/token audit parentage and the source to use its real `source_load` operation. Web capability coverage and auto-wiring gain source-aware Content Safety post-domination, while Prompt Shield remains transform-input-only by explicit test.

**Tech Stack:** Python 3.13, Pydantic v2, Jinja2 sandboxing, pytest, ELSPETH plugin/runtime contracts, React 18, TypeScript, Vitest.

---

## File map

### New files

- `src/elspeth/plugins/sources/llm/__init__.py` — source package exports.
- `src/elspeth/plugins/sources/llm/config.py` — source-specific common and provider-discriminated config models.
- `src/elspeth/plugins/sources/llm/source.py` — source lifecycle, provider construction, prompt call, output validation, catalogue metadata.
- `tests/unit/plugins/sources/llm/{__init__,conftest,test_config,test_source}.py` — source configuration/runtime tests and fakes.
- `tests/integration/plugins/llm/test_source_pipeline.py` — real engine operation-parent/lineage proof.
- `examples/llm_source/{settings.yaml,README.md}` — bounded ChaosLLM-compatible example.
- `tests/golden/web/catalog/knob_schema/source__llm.json` — source catalogue schema pin.
- `tests/golden/web/catalog/policy_view/source__llm.json` — profiled web schema pin.

### Modified files

- `src/elspeth/plugins/transforms/llm/provider.py` and `providers/*.py` — state/operation-neutral provider calls.
- `src/elspeth/plugins/transforms/llm/{transform,templates,__init__}.py` — row-parent construction and shared pure helpers.
- `src/elspeth/core/config.py` — profile lowering for transform and source LLM components.
- `src/elspeth/web/plugin_policy/{compiler,profiles,coverage}.py` — source admission/profile and coverage.
- `src/elspeth/web/composer/required_controls.py` — source-output Content Safety splice.
- `src/elspeth/web/execution/validation.py` — source base-URL/tracing/profile policy parity.
- `src/elspeth/web/audit_readiness/{boundary_expectations,explain,service}.py` — source boundary and interpretation ruling.
- `src/elspeth/web/frontend/src/components/sidebar/{ExecuteButton.tsx,ExecuteButton.test.tsx}` — source LLM consent.
- Existing provider, catalogue, coverage, auto-wire, execution, and audit-readiness tests named below.

## Task 1: Add an operation-capable LLM audit parent

**Files:**

- Modify: `src/elspeth/plugins/transforms/llm/provider.py`
- Modify: `src/elspeth/plugins/transforms/llm/providers/{azure,bedrock,openrouter,gateway}.py`
- Modify: `src/elspeth/plugins/transforms/llm/transform.py`
- Test: `tests/unit/plugins/llm/test_provider_protocol.py`
- Test: `tests/unit/plugins/llm/test_provider_{azure,bedrock,openrouter,gateway}.py`
- Test: `tests/unit/plugins/llm/test_transform.py`
- Test: `tests/integration/plugins/llm/test_gateway_provider_e2e.py`

- [ ] **Step 1: Write failing audit-parent invariant tests**

```python
def test_llm_audit_parent_accepts_row_and_operation_forms() -> None:
    row = LLMAuditParent.for_row(state_id="state-1", token_id="token-1")
    operation = LLMAuditParent.for_operation(operation_id="operation-1")

    assert row.client_kwargs() == {
        "state_id": "state-1",
        "token_id": "token-1",
        "operation_id": None,
    }
    assert operation.client_kwargs() == {
        "state_id": None,
        "token_id": None,
        "operation_id": "operation-1",
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"state_id": "state-1"},
        {"token_id": "token-1"},
        {"operation_id": "operation-1", "state_id": "state-1", "token_id": "token-1"},
        {"operation_id": " "},
    ],
)
def test_llm_audit_parent_rejects_invalid_parentage(kwargs: dict[str, str]) -> None:
    with pytest.raises((TypeError, ValueError)):
        LLMAuditParent(**kwargs)
```

- [ ] **Step 2: Run the focused protocol tests and confirm red**

```bash
.venv/bin/python -m pytest tests/unit/plugins/llm/test_provider_protocol.py -q
```

Expected: collection/import failure because `LLMAuditParent` does not exist.

- [ ] **Step 3: Implement the owned discriminated parent and protocol signature**

Add this value object to `provider.py` and change `LLMProvider.execute_query()` to require `audit_parent`:

```python
@dataclass(frozen=True, slots=True)
class LLMAuditParent:
    state_id: str | None = None
    token_id: str | None = None
    operation_id: str | None = None

    def __post_init__(self) -> None:
        row_parent = self.state_id is not None or self.token_id is not None
        operation_parent = self.operation_id is not None
        if row_parent == operation_parent:
            raise ValueError("LLMAuditParent requires exactly one row or operation parent")
        if row_parent and (not self.state_id or not self.state_id.strip() or not self.token_id or not self.token_id.strip()):
            raise ValueError("row audit parent requires non-empty state_id and token_id")
        if operation_parent and (not self.operation_id or not self.operation_id.strip()):
            raise ValueError("operation audit parent requires a non-empty operation_id")

    @classmethod
    def for_row(cls, *, state_id: str, token_id: str) -> LLMAuditParent:
        return cls(state_id=state_id, token_id=token_id)

    @classmethod
    def for_operation(cls, *, operation_id: str) -> LLMAuditParent:
        return cls(operation_id=operation_id)

    @property
    def cache_key(self) -> str:
        if self.operation_id is not None:
            return f"operation:{self.operation_id}"
        if self.state_id is None:
            raise RuntimeError("validated row parent lost state_id")
        return f"state:{self.state_id}"

    def client_kwargs(self) -> dict[str, str | None]:
        return {"state_id": self.state_id, "token_id": self.token_id, "operation_id": self.operation_id}
```

For OpenRouter/Gateway semantic call rows, add parent-owned allocation/record helpers selecting `allocate_operation_call_index` + `record_operation_call` for operations and existing state methods for rows. Keep transport and logical records under the same parent.

Move the transform's current finish-reason classification into a pure helper in `provider.py` that returns a bounded failure description or `None`. Keep the transform's existing `TransformResult` mapping around that helper; the source will use the same verdict and fail its source load.

- [ ] **Step 4: Convert every transform/provider call site to explicit row parents**

Replace `state_id=` and `token_id=` keywords with:

```python
audit_parent=LLMAuditParent.for_row(
    state_id=state_id,
    token_id=token_id,
),
```

Update all test fakes to accept `audit_parent: LLMAuditParent`. Do not retain a legacy optional signature: test/type failures must expose every caller.

- [ ] **Step 5: Make all four providers parent-neutral**

Use `audit_parent.cache_key` only for cache/refcount identity and construct audited clients with `**audit_parent.client_kwargs()`. OpenRouter/Gateway logical recorders accept the parent rather than `state_id`. Preserve cleanup and underflow checks.

- [ ] **Step 6: Run provider/transform regressions**

```bash
.venv/bin/python -m pytest \
  tests/unit/plugins/llm/test_provider_protocol.py \
  tests/unit/plugins/llm/test_provider_azure.py \
  tests/unit/plugins/llm/test_provider_bedrock.py \
  tests/unit/plugins/llm/test_provider_openrouter.py \
  tests/unit/plugins/llm/test_provider_gateway.py \
  tests/unit/plugins/llm/test_transform.py \
  tests/integration/plugins/llm/test_gateway_provider_e2e.py -q
```

Expected: all pass and existing state/token assertions are unchanged.

- [ ] **Step 7: Commit**

```bash
git add src/elspeth/plugins/transforms/llm tests/unit/plugins/llm tests/integration/plugins/llm/test_gateway_provider_e2e.py
git commit -m "refactor: support operation-parented LLM provider calls"
```

## Task 2: Add source-specific config and static prompt rendering

**Files:**

- Create: `src/elspeth/plugins/sources/llm/{__init__,config}.py`
- Create: `tests/unit/plugins/sources/llm/{__init__,conftest,test_config}.py`
- Modify: `src/elspeth/plugins/transforms/llm/templates.py`
- Modify: `src/elspeth/plugins/transforms/llm/__init__.py`
- Test: `tests/unit/plugins/llm/test_templates.py`

- [ ] **Step 1: Write failing source-config tests**

```python
TRANSFORM_ONLY_FIELDS = {
    "required_input_fields",
    "queries",
    "pool_size",
    "min_dispatch_delay_ms",
    "max_dispatch_delay_ms",
    "backoff_multiplier",
    "recovery_step_ms",
    "max_capacity_retry_seconds",
    "resolved_prompt_template_hash",
}


@pytest.mark.parametrize("provider", ["azure", "openrouter", "bedrock", "gateway"])
def test_source_variants_publish_only_single_request_fields(provider: str) -> None:
    model = SOURCE_PROVIDER_CONFIGS[provider]
    assert {"prompt_template", "system_prompt", "temperature", "max_tokens", "response_field", "schema_config", "on_validation_failure"} <= set(model.model_fields)
    assert TRANSFORM_ONLY_FIELDS.isdisjoint(model.model_fields)


def test_source_prompt_rejects_row_access_but_accepts_lookup() -> None:
    with pytest.raises(PluginConfigError, match="row"):
        OpenRouterLLMSourceConfig.from_dict(openrouter_config(prompt_template="{{ row.text }}"), plugin_name="llm")

    cfg = OpenRouterLLMSourceConfig.from_dict(
        openrouter_config(prompt_template="Summarise {{ lookup.topic }}", lookup={"topic": "audit"}),
        plugin_name="llm",
    )
    assert cfg.prompt_template == "Summarise {{ lookup.topic }}"
```

Also test invalid provider, blank/invalid response fields, transform-only option rejection, and contradictory response/model/usage schema types.

- [ ] **Step 2: Run tests and confirm red**

```bash
.venv/bin/python -m pytest tests/unit/plugins/sources/llm/test_config.py -q
```

Expected: import failure because source config is absent.

- [ ] **Step 3: Implement source-rooted provider models**

Define `LLMSourceConfig(DataPluginConfig)` with `_plugin_component_type = "source"`, common request fields, explicit `on_validation_failure`, lookup/source metadata, syntax validation, and undeclared-name rejection allowing only `lookup` and Jinja globals. Define:

```python
SOURCE_PROVIDER_CONFIGS: dict[str, type[LLMSourceConfig]] = {
    "azure": AzureOpenAILLMSourceConfig,
    "openrouter": OpenRouterLLMSourceConfig,
    "bedrock": BedrockLLMSourceConfig,
    "gateway": GatewayLLMSourceConfig,
}
```

Mirror each transform provider's applicable model, endpoint, authentication, timeout, tracing, and `VALUE_SOURCES` fields while reusing pure validators. Do not inherit `LLMConfig` or transform provider config classes.

- [ ] **Step 4: Add static rendering without a row binding**

Refactor `PromptTemplate` around a private context renderer and add:

```python
def render_static_with_metadata(self) -> RenderedPrompt:
    prompt = self._render_context(
        {"lookup": self._lookup_data if self._lookup_data is not None else {}},
    )
    return RenderedPrompt(
        prompt=prompt,
        template_hash=self._template_hash,
        variables_hash=_sha256(canonical_json({})),
        rendered_hash=_sha256(prompt),
        template_source=self._template_source,
        lookup_hash=self._lookup_hash,
        lookup_source=self._lookup_source,
        contract_hash=None,
    )
```

Keep transform rendering byte-compatible and test that static rendering never injects `row`.

- [ ] **Step 5: Add a public source-output schema helper**

Add a helper that augments `SchemaConfig` with required response `str`, usage `any`, and model `str` fields, preserves the authored mode, merges guaranteed fields, and rejects contradictory authored definitions. Reuse `LLM_GUARANTEED_SUFFIXES` and `_SUFFIX_SCHEMA_TYPES`.

- [ ] **Step 6: Run tests**

```bash
.venv/bin/python -m pytest \
  tests/unit/plugins/sources/llm/test_config.py \
  tests/unit/plugins/llm/test_templates.py \
  tests/unit/plugins/llm/test_config_schema.py -q
```

Expected: all pass and transform schema output is unchanged.

- [ ] **Step 7: Commit**

```bash
git add src/elspeth/plugins/sources/llm src/elspeth/plugins/transforms/llm tests/unit/plugins/sources/llm tests/unit/plugins/llm/test_templates.py tests/unit/plugins/llm/test_config_schema.py
git commit -m "feat: define single-prompt LLM source configuration"
```

## Task 3: Implement the one-call, one-row source

**Files:**

- Create: `src/elspeth/plugins/sources/llm/source.py`
- Modify: `src/elspeth/plugins/sources/llm/__init__.py`
- Create: `tests/unit/plugins/sources/llm/test_source.py`
- Modify: `tests/unit/plugins/sources/llm/conftest.py`
- Modify: `tests/unit/contracts/source_contracts/test_source_protocol.py`

- [ ] **Step 1: Write failing lifecycle/output tests**

```python
def test_load_calls_provider_once_and_emits_one_transform_compatible_row(
    source: LLMSource,
    source_context: FakeSourceContext,
) -> None:
    provider = FakeProvider(
        LLMQueryResult(
            content="```text\nA careful answer\n```",
            usage=TokenUsage.known(prompt_tokens=7, completion_tokens=3),
            model="served-model",
            finish_reason=FinishReason.STOP,
        )
    )
    source._provider = provider

    rows = list(source.load(source_context))

    assert provider.calls == 1
    assert provider.audit_parents == [LLMAuditParent.for_operation(operation_id="source-load-1")]
    assert len(rows) == 1
    assert rows[0].source_row_index == 0
    assert rows[0].row == {
        "answer": "A careful answer",
        "answer_usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        "answer_model": "served-model",
    }
```

Add missing-operation, provider-not-started, double-load, provider-error, finish-reason, empty-content, generator cancellation, closure, and schema quarantine/discard tests.

- [ ] **Step 2: Run tests and confirm red**

```bash
.venv/bin/python -m pytest tests/unit/plugins/sources/llm/test_source.py -q
```

Expected: import failure because `LLMSource` is absent.

- [ ] **Step 3: Implement direct `BaseSource` lifecycle**

```python
class LLMSource(BaseSource):
    name = "llm"
    determinism = Determinism.NON_DETERMINISTIC
    plugin_version = "1.0.0"
    web_config_authority = WebConfigAuthority.OPERATOR_PROFILED
    policy_capabilities = frozenset({CapabilityDeclaration(PluginCapability.LLM)})
    capability_tags = ("llm", "generation", "single-row")
```

In `__init__`, dispatch config, build the static prompt, effective schema/contract, and guaranteed fields. In `on_start`, call `super()`, require a Landscape recorder, capture telemetry/rate limiter/run identity, construct one provider, and initialize tracing. In `load`, require an operation ID, render, build messages, call with an operation parent, enforce success finish reason, strip fences, populate output fields, validate, then yield `SourceRow.valid(validated_row, contract=contract, source_row_index=0)` or one configured quarantine outcome. Guard against a second load.

- [ ] **Step 4: Implement closure and catalogue hooks**

Close/clear the provider exactly once. Add catalogue prose, named-source example, output semantics, discriminated config methods, probe config, agent assistance, and secret requirements. Do not add runtime preflight: the one real request is authoritative.

- [ ] **Step 5: Run source contract tests**

```bash
.venv/bin/python -m pytest \
  tests/unit/plugins/sources/llm/test_source.py \
  tests/unit/plugins/sources/test_declared_guaranteed_fields.py \
  tests/unit/contracts/source_contracts/test_source_protocol.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/elspeth/plugins/sources/llm tests/unit/plugins/sources/llm tests/unit/plugins/sources/test_declared_guaranteed_fields.py tests/unit/contracts/source_contracts/test_source_protocol.py
git commit -m "feat: add source-native single-prompt LLM plugin"
```

## Task 4: Register the source and add profile/catalogue parity

**Files:**

- Modify: `src/elspeth/core/config.py`
- Modify: `src/elspeth/web/plugin_policy/{compiler,profiles}.py`
- Modify: `tests/unit/core/test_llm_profile_catalog.py`
- Modify: `tests/unit/web/plugin_policy/{test_compiler,test_profiles}.py`
- Modify: `tests/unit/plugins/sources/test_source_catalogue_metadata.py`
- Modify: `tests/unit/plugins/llm/test_plugin_registration.py`
- Modify: `tests/unit/web/catalog/{test_service,test_knob_schema_golden,test_policy_view_golden}.py`
- Create: `tests/golden/web/catalog/knob_schema/source__llm.json`
- Create: `tests/golden/web/catalog/policy_view/source__llm.json`

- [ ] **Step 1: Write failing discovery/profile tests**

```python
def test_llm_is_registered_for_both_component_kinds() -> None:
    manager = get_shared_plugin_manager()
    assert manager.get_source_by_name("llm").name == "llm"
    assert manager.get_transform_by_name("llm").name == "llm"


def test_profile_registry_serves_source_and_transform_llm() -> None:
    registry = OperatorProfileRegistry(policy=policy, settings=runtime_settings)
    source_schema = registry.public_schema(
        PluginId("source", "llm"),
        source_full_schema,
        available_aliases=("tutorial",),
    )
    transform_schema = registry.public_schema(
        PluginId("transform", "llm"),
        transform_full_schema,
        available_aliases=("tutorial",),
    )
    assert source_schema.json_schema["properties"]["profile"]["enum"] == ["tutorial"]
    assert transform_schema.json_schema["properties"]["profile"]["enum"] == ["tutorial"]
```

Also assert source and transform selectors lower to identical provider/model/credential bindings, while source public schema omits transform-only options and all private endpoint/credential fields.

- [ ] **Step 2: Run tests and confirm red**

```bash
.venv/bin/python -m pytest \
  tests/unit/plugins/llm/test_plugin_registration.py \
  tests/unit/web/plugin_policy/test_compiler.py \
  tests/unit/web/plugin_policy/test_profiles.py -q
```

Expected: source `llm` is absent.

- [ ] **Step 3: Extend profile lowering to source components**

Keep `_lower_llm_profile_node_options()` as the one profile-to-provider function. Expand `_lower_llm_profile_nodes()` to visit `transforms[index]` and `sources[name]`, issue component-specific errors, retain `profile_alias`, and preserve idempotence and server/user credential-scope rules for both.

- [ ] **Step 4: Register source policy/profile authority**

Add `PluginId("source", "llm")` to required web IDs and attach a second `_LLMProfileResolver`. Refactor public-schema filtering to derive provider definition names from the schema discriminator mapping, so source and transform config class names both work without importing `LLMTransform`.

- [ ] **Step 5: Update catalogue expectations and generated goldens**

Add `llm` to built-in source names/determinism expectations. Serialize live `CatalogServiceImpl` output using the golden tests' sorted/indented format; do not hand-invent JSON. Add a source-profile golden built with `PluginId("source", "llm")`.

- [ ] **Step 6: Run catalogue/profile tests**

```bash
.venv/bin/python -m pytest \
  tests/unit/core/test_llm_profile_catalog.py \
  tests/unit/plugins/sources/test_source_catalogue_metadata.py \
  tests/unit/plugins/llm/test_plugin_registration.py \
  tests/unit/web/plugin_policy/test_compiler.py \
  tests/unit/web/plugin_policy/test_profiles.py \
  tests/unit/web/catalog/test_service.py \
  tests/unit/web/catalog/test_knob_schema_golden.py \
  tests/unit/web/catalog/test_policy_view_golden.py -q
```

Expected: all pass and the live catalogue file set matches the golden directory.

- [ ] **Step 7: Commit**

```bash
git add src/elspeth/core/config.py src/elspeth/web/plugin_policy tests/unit/core/test_llm_profile_catalog.py tests/unit/plugins tests/unit/web/plugin_policy tests/unit/web/catalog tests/golden/web/catalog
git commit -m "feat: expose LLM source through profiles and catalogue"
```

## Task 5: Prove real engine parentage and one-row lineage

**Files:**

- Create: `tests/integration/plugins/llm/test_source_pipeline.py`
- Modify if required: `tests/integration/plugins/llm/conftest.py`

- [ ] **Step 1: Write failing engine integration tests**

Build `llm` source -> JSON sink through the real orchestrator, replace provider construction with a bounded fake, and assert:

```python
assert run.status is RunStatus.COMPLETED
assert output_rows == [
    {
        "generated_text": "engine result",
        "generated_text_usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
        "generated_text_model": "fake-model",
    }
]
assert source_calls[0].operation_id is not None
assert source_calls[0].state_id is None
assert emitted_tokens[0].source_row_index == 0
```

Add the failure counterpart: provider raises after an audited operation call, the run fails, no row state/token is fabricated, and the ERROR operation call remains recorded.

- [ ] **Step 2: Run and confirm red**

```bash
.venv/bin/python -m pytest tests/integration/plugins/llm/test_source_pipeline.py -q
```

Expected: failure until discovery, provider injection, and operation recording are connected.

- [ ] **Step 3: Make only runtime corrections exposed by the test**

Preserve this order: `source_load` operation opens, provider call records, source yields, then engine creates state/token. Do not move token creation before `load()` and do not add a preflight request.

- [ ] **Step 4: Re-run and commit**

```bash
.venv/bin/python -m pytest tests/integration/plugins/llm/test_source_pipeline.py -q
git add tests/integration/plugins/llm/test_source_pipeline.py src/elspeth/plugins/sources/llm
git commit -m "test: prove LLM source audit parentage and lineage"
```

Expected: success and failure cases pass.

## Task 6: Extend required-control coverage to LLM sources

**Files:**

- Modify: `src/elspeth/web/plugin_policy/coverage.py`
- Modify: `tests/unit/web/plugin_policy/test_coverage.py`
- Modify: `tests/unit/web/plugin_policy/test_validation.py`

- [ ] **Step 1: Write failing source coverage tests**

```python
def test_llm_source_requires_content_safety_on_every_downstream_path() -> None:
    state = llm_source_state(response_field="generated_text", branched=True, cover_one_branch=True)

    findings = control_coverage_findings(state, PluginCapability.CONTENT_SAFETY)

    assert [(f.component_id, f.role, f.reason) for f in findings] == [
        ("source:generator", ControlRole.OUTPUT, "output_not_post_dominated"),
    ]


def test_llm_source_does_not_require_prompt_shield() -> None:
    state = llm_source_state(response_field="generated_text")
    assert control_coverage_findings(state, PluginCapability.PROMPT_SHIELD) == ()
```

Also pin singular source ID `source`, named ID `source:<name>`, response-field-only scope, full coverage, multiple sources, and stable mixed source/transform ordering.

- [ ] **Step 2: Run and confirm red**

```bash
.venv/bin/python -m pytest tests/unit/web/plugin_policy/test_coverage.py -q
```

Expected: source-only Content Safety produces no finding.

- [ ] **Step 3: Implement component-appropriate capability lookup**

Keep transform lookup nominal and add source lookup through `get_source_by_name`. Add:

```python
def _llm_source_output_fields(source: SourceSpec) -> frozenset[str]:
    response_field = source.options.get("response_field", "llm_response")
    if not isinstance(response_field, str) or not response_field.strip():
        return frozenset()
    return frozenset({response_field})
```

For Content Safety, start `_stream_proves_output_control()` at capable sources' `on_success` streams and use the stable source component ID in `visited`. Do not check sources for Prompt Shield. Preserve node behavior and stable order.

- [ ] **Step 4: Run coverage/policy tests**

```bash
.venv/bin/python -m pytest \
  tests/unit/web/plugin_policy/test_coverage.py \
  tests/unit/web/plugin_policy/test_validation.py -q
```

Expected: all pass and generic policy validation blocks an uncovered required LLM source.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web/plugin_policy/coverage.py tests/unit/web/plugin_policy/test_coverage.py tests/unit/web/plugin_policy/test_validation.py
git commit -m "fix: require content safety coverage for LLM sources"
```

## Task 7: Auto-wire Content Safety after LLM sources

**Files:**

- Modify: `src/elspeth/web/composer/required_controls.py`
- Modify: `tests/unit/web/composer/test_required_control_autowire.py`
- Modify: `tests/integration/web/composer/guided/test_shared_planner_surfaces.py`

- [ ] **Step 1: Write failing source splice tests**

```python
def test_content_safety_is_spliced_after_named_llm_source(tmp_path: Path) -> None:
    view, snapshot = _guardrail_profile_view(tmp_path)
    candidate = bare_llm_source_candidate(source_name="generator", response_field="draft")
    original = copy.deepcopy(candidate)

    wired = wire_required_controls(candidate, snapshot, view)

    assert candidate == original
    source = wired["sources"]["generator"]
    safety = next(node for node in wired["nodes"] if node["plugin"] == "aws_bedrock_content_safety")
    assert source["on_success"] == safety["input"]
    assert safety["on_success"] == original["sources"]["generator"]["on_success"]
    assert safety["options"]["fields"] == ["draft"]
    assert safety["options"]["source"] == "OUTPUT"
```

Add singular-source, multi-source, covered/no-op, second-pass identity, malformed no-op, mixed source/transform, reserved-name collision, and no-Prompt-Shield tests.

- [ ] **Step 2: Run and confirm red**

```bash
.venv/bin/python -m pytest tests/unit/web/composer/test_required_control_autowire.py -q
```

Expected: source candidate remains uncovered.

- [ ] **Step 3: Implement source splice ownership**

Maintain mutable copies of authored source shape(s) and nodes. `_splice_source_output_control()` allocates an intermediate stream, rewrites only the target source's `on_success`, inserts a creditable Content Safety node whose success destination is the preserved stream, and stages source-specific disclosure. Resolve `source`/`source:<name>` IDs without treating them as nodes.

Use budget `2 * llm_transform_count + llm_source_count`; reparse combined working candidate after every splice. Return the input object by identity on every no-op path.

- [ ] **Step 4: Run auto-wire/finalizer regressions**

```bash
.venv/bin/python -m pytest \
  tests/unit/web/composer/test_required_control_autowire.py \
  tests/integration/web/composer/guided/test_shared_planner_surfaces.py -q
```

Expected: all pass, including candidate identity assertions.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web/composer/required_controls.py tests/unit/web/composer/test_required_control_autowire.py tests/integration/web/composer/guided/test_shared_planner_surfaces.py
git commit -m "fix: auto-wire content safety after LLM sources"
```

## Task 8: Enforce source LLM web execution policies

**Files:**

- Modify: `src/elspeth/web/execution/validation.py`
- Modify: `tests/unit/web/execution/test_static_llm_prompt_advisory.py`
- Modify: existing `tests/unit/web/execution/` base-URL/tracing/profile policy tests
- Modify: `tests/integration/web/test_execute_pipeline.py`

- [ ] **Step 1: Write failing source policy tests**

```python
def test_web_validation_rejects_llm_source_base_url_override(
    validation_harness: ValidationHarness,
) -> None:
    state = validation_harness.llm_source_state(
        options={"profile": "tutorial", "base_url": "https://author.example/v1"},
    )
    result = validation_harness.validate(state)
    assert result.is_valid is False
    assert result.errors[0].component_id == "source:generator"
    assert result.errors[0].component_type == "source"
    assert result.errors[0].error_code == "llm_base_url_not_allowed"


def test_static_prompt_advisory_does_not_fire_for_llm_source() -> None:
    state = llm_source_state(prompt_template="Generate one row")
    assert _find_static_llm_prompt_advisories(state) == []
```

Also test tracing rejection, source profile capture, uncovered/covered Content Safety, and retry-budget non-applicability because source config has no pool/query retry fields.

- [ ] **Step 2: Run and confirm red**

```bash
.venv/bin/python -m pytest tests/unit/web/execution -q
```

Expected: base URL/tracing checks ignore sources.

- [ ] **Step 3: Generalize applicable policy iteration**

Add a local projection yielding `(component_id, component_type, plugin, options)` for sources and transform nodes. Apply base-URL and tracing policies to both. Keep sequential multi-query retry checks transform-only with an explicit test. Include source profile aliases in operator-resolved model bookkeeping without creating interpretation requirements.

- [ ] **Step 4: Run execution tests**

```bash
.venv/bin/python -m pytest tests/unit/web/execution tests/integration/web/test_execute_pipeline.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web/execution/validation.py tests/unit/web/execution tests/integration/web/test_execute_pipeline.py
git commit -m "fix: enforce LLM policies for source components"
```

## Task 9: Explain LLM source audit readiness accurately

**Files:**

- Modify: `src/elspeth/web/audit_readiness/{boundary_expectations,explain,service}.py`
- Modify: `tests/unit/web/audit_readiness/{test_boundary_predicate_parity,test_explain,test_service}.py`

- [ ] **Step 1: Write failing boundary/narrative tests**

```python
def test_llm_source_narrative_describes_one_prompt_and_response() -> None:
    narrative = build_narrative(llm_source_state(), retention_days=30)
    assert "one authored prompt" in narrative
    assert "one generated row" in narrative
    assert "model and token usage" in narrative
    assert "each row from the llm source" not in narrative


def test_llm_source_does_not_make_interpretation_review_applicable() -> None:
    snapshot = build_snapshot(llm_source_state())
    row = next(row for row in snapshot.rows if row.id == "llm_interpretations")
    assert row.status == "not_applicable"
    assert row.summary == "No row-transform LLM interpretations in this composition"
```

- [ ] **Step 2: Run and confirm red**

```bash
.venv/bin/python -m pytest tests/unit/web/audit_readiness -q
```

Expected: source map lacks `llm` and generic prose is emitted.

- [ ] **Step 3: Add boundary/prose and explicit interpretation ruling**

Add `"llm": Determinism.NON_DETERMINISTIC` to source expectations. Give `_describe_source()` an LLM branch describing one prompt, full response, served model, usage, timestamp, and operation record. Rename the private predicate to `_composition_has_llm_interpretation_transform`, keep it transform-only, and document that interpretation events resolve row semantics unavailable to a rowless source.

- [ ] **Step 4: Run and commit**

```bash
.venv/bin/python -m pytest tests/unit/web/audit_readiness -q
git add src/elspeth/web/audit_readiness tests/unit/web/audit_readiness
git commit -m "feat: explain LLM source audit boundaries"
```

Expected: all pass.

## Task 10: Disclose source LLM egress before Run

**Files:**

- Modify: `src/elspeth/web/frontend/src/components/sidebar/ExecuteButton.tsx`
- Modify: `src/elspeth/web/frontend/src/components/sidebar/ExecuteButton.test.tsx`

- [ ] **Step 1: Write failing source consent tests**

```typescript
it("discloses an authored prompt for an LLM source without claiming row egress", () => {
  const lines = buildRunEgressSummary(
    llmSourceComposition("generator", "approved-model"),
    transformCatalog,
    sourceCatalog,
    false,
  );

  expect(lines).toContain(
    "Sends an authored prompt to the configured LLM: source:generator (model approved-model).",
  );
  expect(lines.join("\n")).not.toContain("Reads source data: source:generator");
  expect(lines.join("\n")).not.toContain("Sends rows to the configured LLM: source:generator");
});
```

Add loaded-catalog classification, failed-catalog uncertainty, ordinary-source unchanged, transform-LLM unchanged, and mixed source/transform tests.

- [ ] **Step 2: Run and confirm red**

```bash
cd src/elspeth/web/frontend
npm test -- src/components/sidebar/ExecuteButton.test.tsx
```

Expected: source LLM appears only as generic source data.

- [ ] **Step 3: Separate source generation from source reads**

Accept source catalogue summaries in `buildRunEgressSummary`. Partition LLM and non-LLM sources; keep ordinary sources on `Reads source data`, add authored-prompt disclosure, avoid duplicate network lines, and name unverifiable sources when catalogue loading fails. Pass both source and transform catalogues from the store.

- [ ] **Step 4: Run frontend gates**

```bash
cd src/elspeth/web/frontend
npm test -- src/components/sidebar/ExecuteButton.test.tsx
npm run typecheck
npm run lint
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web/frontend/src/components/sidebar/ExecuteButton.tsx src/elspeth/web/frontend/src/components/sidebar/ExecuteButton.test.tsx
git commit -m "fix: disclose LLM source prompt egress before runs"
```

## Task 11: Add a bounded example

**Files:**

- Create: `examples/llm_source/settings.yaml`
- Create: `examples/llm_source/README.md`
- Modify: `examples/AGENTS.md`
- Modify: `examples/README.md`
- Modify: `tests/e2e/examples/test_shipped_examples.py`
- Modify: `tests/unit/docs/test_examples_readme_index.py`

- [ ] **Step 1: Write a failing example inventory test**

Add `llm_source` to the ChaosLLM inventory and assert one LLM source, no LLM transform, custom response field, explicit validation-failure policy, and a JSON sink.

- [ ] **Step 2: Run and confirm red**

```bash
.venv/bin/python -m pytest tests/e2e/examples/test_shipped_examples.py tests/unit/docs/test_examples_readme_index.py -q
```

Expected: inventory or missing-file failure.

- [ ] **Step 3: Add the ChaosLLM example**

Use the OpenRouter-compatible loopback provider, one static prompt, `response_field: generated_text`, observed schema, explicit discard, and JSON sink. Document one-row output and the existing ChaosLLM launch command. If required Content Safety is unavailable locally, document that this CLI example assumes recommend mode; do not add a fake control.

- [ ] **Step 4: Validate and commit**

```bash
.venv/bin/python -m pytest tests/e2e/examples/test_shipped_examples.py tests/unit/docs/test_examples_readme_index.py -q
.venv/bin/elspeth validate --settings examples/llm_source/settings.yaml
git add examples/llm_source examples/AGENTS.md examples/README.md tests/e2e/examples/test_shipped_examples.py tests/unit/docs/test_examples_readme_index.py
git commit -m "docs: add single-row LLM source example"
```

Expected: tests and validation pass without making a provider request.

## Task 12: Run the parity scan and full verification

**Files:**

- Modify only files exposed by the checks below; do not broaden into unrelated existing findings.

- [ ] **Step 1: Scan every live LLM discriminator**

```bash
rg -n 'PluginId\("transform", "llm"\)|plugin == "llm"|plugin != "llm"|get_transform_by_name\(.*llm|LLMTransform|llm_node|llm transform|LLM transform' src tests
```

Classify every result as source parity added, deliberately transform-only with focused evidence, or unrelated type/import reference. Recheck profile lowering, capability walks, execution gates, interpretation handling, semantic validation, implicit decisions, recipes/tutorials, catalogue, audit readiness, and consent.

- [ ] **Step 2: Run focused Python and frontend tests**

```bash
.venv/bin/python -m pytest \
  tests/unit/plugins/sources/llm \
  tests/unit/plugins/llm \
  tests/integration/plugins/llm \
  tests/unit/web/plugin_policy \
  tests/unit/web/composer/test_required_control_autowire.py \
  tests/unit/web/execution \
  tests/unit/web/audit_readiness \
  tests/unit/web/catalog -q
cd src/elspeth/web/frontend
npm test -- src/components/sidebar/ExecuteButton.test.tsx
npm run typecheck
npm run lint
```

Expected: all pass.

- [ ] **Step 3: Run the complete repository suite**

```bash
.venv/bin/python -m pytest tests/
```

Expected: no failures. Compare with the clean pre-change baseline: 35,589 passed, 66 skipped, 1 xfailed.

- [ ] **Step 4: Run static/trust-boundary gates**

```bash
.venv/bin/elspeth-lints check
.venv/bin/python scripts/wardline_gate.py
```

Expected: Wardline exit 0 and non-inert. The documented package-wide signed trust-tier allowlist may remain fail-closed, but this branch must add no tier-model defects or drift.

- [ ] **Step 5: Inspect branch and update tracker evidence**

```bash
git status --short
git diff release/0.7.2...HEAD --stat
git log --oneline release/0.7.2..HEAD
```

Expected: clean feature-only worktree. Add exact evidence to `elspeth-0b075bdf2b` and `elspeth-b35b10722c`; close the P1 only after coverage, auto-wire, execution, readiness, and consent tests pass.

- [ ] **Step 6: Close verification without scope drift**

If verification exposes a defect, return to the numbered task that owns that file, add the failing regression there, and commit that exact task's source/test paths with its stated commit message. If verification is clean, create no empty closeout commit. Do not push, merge, or alter the shared checkout without a new user request.
