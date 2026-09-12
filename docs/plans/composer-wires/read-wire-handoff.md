# READ census implementation handoff

> **Resumption reference, promoted 2026-09-12.** The [campaign plan](../2026-09-08-composer-wires-campaign.md) controls current custody, scope, order and authorization.
> This document preserves September 10 preparation; source locations, measured counts, tests and active-writer restrictions describe that historical snapshot.
> Earlier scratch records named below are optional historical evidence, not prerequisites to recover from temporary storage. Re-measure the consolidated source before implementation.
> The current request covers consolidation and planning only. Its consolidation commits supersede the historical commit ban for that scope; this document does not initiate implementation or authorize provider calls, deployment, store deletion or signing.

Read-only preparation against composer-wires-campaign on 2026-09-10. Runtime registry inspection measured 42 shipped definitions: 40 declarations, one session-aware callable, and the advisor interception. No source edits or production executions were performed. Locations below are current observations and should be refreshed when implementing.

## Define the claim before implementing

MODEL establishes a model reached with original tool input. READ must establish a named input value reaches a non-diagnostic use. Neither proves every control-flow branch executes. A bare AST attribute, successful model validation, complete model serialization, or a local variable named `validated` establishes no semantic consumption by itself.

Keep `tool/shipped/read/site` as the public row if required, but retain structured per-field evidence and unresolved reasons internally. A partially known row must retain both its proven fields and its unresolved findings. Never encode unresolved as an empty successful READ set. The final parity gate must reject unresolved rows before set comparison. It should not silently exempt no-model tools: raw input reads remain measurable.

There is an unavoidable limit: a small AST reader can prove selected syntactic data dependencies, not arbitrary causal effects. Name that limitation. In particular, `if False: ...`, exception feasibility, mutually exclusive branches, and downstream callees that ignore a parameter require separate behavioral evidence; do not claim a universal semantic-consumption proof.

## Reuse and separation

Reuse the MODEL callable catalog and its duplicate/missing/interception checks, `_body_nodes` nested-scope exclusion, qualified identity, binding resolution with local-import shadowing refusal, recursion detection, and identity-bound `typing.cast`. Extract common catalog/source utilities rather than copying a second ownership universe.

Do not reuse `_model_for_handler` as READ's entire provenance engine. It returns a model identity and validation sites, not the local value receiving the result. A READ pass needs a small binding table recording which local holds original input or the particular model constructed from it. An unrelated instance of the same class must never inherit input provenance. Likewise, MODEL's generic transformed-forwarding refusal cannot blindly be applied to `NodeSpec(id=validated.id)`: that is exactly a field-use candidate, not an opaque whole-input transformation.

## Bounded reader

1. Start each actual handler with an explicit raw-input parameter. Do not assume parameter zero for unbound service methods: advisor methods have `self` first. Resolve direct module-bound helper calls using the actual callable and bind positional/keyword parameters through `inspect.signature`; unsupported varargs/kwargs are findings.
2. Recognize direct `Model.model_validate(raw)` and the identity-bound `_validate_mutation_arguments(Model, raw, ...)`, optionally wrapped by the actual `typing.cast`. A single simple assignment binds the result local to that model and origin. Support the immediate expression `Model.model_validate(raw).field` too. A type annotation, cast alone, constructor on independent data, or redaction manifest does not create that provenance.
3. On proven raw input, observe literal-string `raw['key']` and `raw.get('key', default)`. On a proven model instance, observe a declared `model.field`. Reject dynamic keys and dynamic attribute access on these roots. Root membership tests record presence evidence separately; they do not establish value consumption. Iterating keys, `len(raw)`, `keys()`, or generic validation does not expand to all shipped fields.
4. Attach each extracted field to its expression. Carry it through simple single-definition local assignments and ordinary expressions. A dead assignment or expression statement contributes no semantic READ. Resolve local assignment dependencies with a small expression graph, not general SSA: on reassignment, branch-dependent competing definitions, closure capture, or unsupported aliasing emit unresolved. This graph is needed for real `node_id = validated.id; NodeSpec(id=node_id)` code.
5. Distinguish candidate extraction from accepted use. Direct returned tool data/state construction, a branch controlling a result, and a field reaching an inspected operational helper are evidence. Logging/audit/diagnostic-only calls are not. An unknown call receiving a field is a candidate plus unresolved-use finding, not automatic proof. Do not infer diagnostics solely from a method spelling; use known callable/receiver ownership or report ambiguity. Where an owned helper is inspectable, forward the field dependency and verify use in that helper; restrict this to field-carrying paths rather than walking unrelated code.
6. Whole-model forwarding to an inspectable helper transfers provenance, not all its fields. `model_dump()` never yields READ = model_fields. At most it creates unresolved bulk-use evidence pending inspection of downstream literal selection. `validated.patch.model_dump()` begins with the top-level field `patch`; count `patch` only if its resulting value reaches an accepted use. Do not invent nested top-level keys `name` and `description`.
7. `model_copy(update=...)`, copied raw dictionaries, aliases, and mutable updates require explicit handling or unresolved evidence. For the first implementation, refuse the whole-root transformation while preserving other established field reads. Do not add broad exceptions to make every row complete.

This is a deliberately conservative implementation boundary. If the requirement instead accepts all executed-source extractions, expose that as `observed_reads`, not semantic READ; it cannot satisfy the requested dead-local/logging negatives.

## Representative live paths

| Tool/path | Evidence and implication |
|---|---|
| `upsert_node`: `tools/transforms.py:340` `_handle_upsert_node` -> `:583` `_execute_upsert_node` | Raw parameter changes from `arguments` to `args`. At :589 the mutation validator is cast to `_UpsertNodeArgumentsModel`; :590-593 extract `id`, `node_type`, `plugin`, `options`. These flow into branches, normalizers, and `NodeSpec` at :714. The handler also independently revalidates at :353 and uses `id` for post-call lookup. Do not attribute neighboring `state.nodes[*].id` to tool input. |
| Queue branch: `transforms.py:635` -> `_execute_upsert_queue_node` at :527 | The actual helper receives `validated.model_copy(update={'options': ...})`. This is not unchanged provenance. Initial refusal is honest; retain the main helper's independently measured reads. |
| `set_source`: `tools/sources.py:190` | Handler forwards raw arguments at :195, then immediately reads `SetSourceArgumentsModel.model_validate(arguments).source_name` at :198. An assignment-only model detector misses it. |
| `set_metadata`: `transforms.py:1391` | :1399 serializes `validated.patch`, then :1401 sends the patch to `state.with_metadata`. This consumes the single top-level `patch` field, not every metadata subfield and not arbitrary whole-model fields. |
| `get_blob_metadata`: `tools/blobs.py:411` | Literal `arguments['blob_id']` reaches UUID error checking at :420 and `_sync_get_blob` at :423. It is READ evidence without a handler input model. |
| `wire_blob_inline_ref`: `blobs.py:621` | Reads `field_path`, `blob_id`, conditional `encoding`, and hidden `sha256_override` at :665. Report the last as READ-not-SHIPPED evidence; do not silently filter observed keys to shipped keys. Public acceptance/reachability is a separate judgment. |
| `get_plugin_schema`: `tools/generation.py:249` | Raw literals `plugin_type` and `name` at :254-255 become local variables. This is a useful small positive-control path for assignment use tracking. |
| `set_pipeline`: `tools/sessions.py:1848` -> :1813 -> `build_set_pipeline_candidate` | The build helper validates at :810 after preparation/copying and contains nested helper closures. Keep unresolved until origin-preserving preparation and invoked-closure handling are explicitly supported; do not borrow the manifest model. |
| `request_interpretation_review`: `sessions.py:2859` | Async registered callable; `parsed` binds the cast mutation-validator result at :2888. Fields feed credential rejection and draft validation at :2902 and :2912-13. |

## Advisor: explicit public adapter, not a tool-name-shaped fake handler

`tool_batch.py:1641` selects `request_advisor_hint`; :1699 invokes `ctx.service._validate_advisor_arguments(arguments)`; :1796 invokes `_call_advisor_for_tool(arguments, ...)`. On `ComposerServiceImpl`, `service.py:7680` forwards to `_call_advisor_with_audit` at :7710; :7761 builds the provider message using `_build_advisor_user_message` at :9013. The message builder reads `problem_summary`, `trigger`, `recent_errors`, `attempted_actions`, and `schema_excerpt`. Validation-only reads at :7240 are useful evidence but cannot alone prove the planner knob reaches the provider path.

Represent these actual functions plus explicit input parameter names in a small source-checked interception adapter. Prove the tool-name branch still calls those methods with original `arguments`; changing/removing the calls must fail a probe. Resolve class-owned methods by actual class identity, not arbitrary method-name matching. Avoid scanning the entire service/tool batch as if every input belonged to advisor.

Important measured complication: `_build_advisor_user_message` also reads `user_message` at :9047/:9054. This helper is shared with backend checkpoint calls. `service.py:472-477` explains that `_ADVISOR_ARGUMENT_KEYS` excludes this backend-produced key; the public validator rejects it. A raw helper union will report it. Preserve it as a source READ-not-SHIPPED observation with public-boundary exclusion evidence; do not present it as a public accepted knob or remove it by intersection with SHIPPED. Any eventual disposition must bind to the actual admission exclusion and fail if that exclusion disappears.

## Required discriminating probes

Positive controls:

- Arbitrarily named registered handler -> renamed raw helper parameter -> literal subscript and literal `.get` whose values reach returned output.
- Direct model validation assignment and cast-wrapped mutation validation assignment -> used declared field.
- Immediate `Model.model_validate(raw).field` -> actual use.
- `field_local = validated.field` -> returned construction; genuine `upsert_node` includes `id/node_type/plugin/options` with origin and use sites.
- Raw/model forwarding to one helper whose field read is used; module-qualified and aliased callable identities.
- `validated.patch.model_dump()` followed by metadata update proves only top-level `patch`.
- Catalog includes all live zero-knob tools and the source-checked advisor adapter; missing/duplicate/intercepted ownership refuses.
- Advisor helper observation includes backend-only `user_message` while recording why public admission excludes it.

Negative controls:

- Unregistered namesake function, unused nested function, unrelated `Other.model_validate({})`, and unrelated instance of the SAME model cannot add reads.
- `unused = validated.field`, bare `validated.field`, `logger.info(validated.field)`, and assignment followed only by logging cannot satisfy READ.
- `validated.model_dump()` alone or passed only to logging cannot satisfy all fields; bulk forwarding to an opaque callable is unresolved.
- `args[key]`, `args.get(key)`, dynamic `getattr`, key iteration, and unknown mapping unpacking refuse or create explicit unresolved rows; presence-only tests do not count as value use.
- Local import shadowing, raw/model reassignment, competing branch bindings, transformed whole-input forwarding, opaque model copy, invoked closure capture, and helper cycles cannot silently become absent or complete.
- Whole-model forward to a helper reading just one field never credits untouched siblings.
- Foreign state object `.id` next to `validated.id` must not confer provenance; forwarding a model of the same type constructed from independent data is also foreign.
- Removing a helper's actual use while leaving the extraction/validation/logging makes parity fail.
- Removing advisor public dispatch calls or changing original arguments to a transformed mapping breaks adapter proof.

Do not add a general CFG/SSA engine, semantic sink names guessed from spelling, source-count assertions, or broad fences. Ship the bounded inventory with unresolved findings first; complete the final parity gate only when each unresolved path has real evidence or an explicitly approved narrow disposition.
