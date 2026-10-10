<!-- Durable copy: machine-specific prefixes normalized; private original retained. -->

# Astra dynamic-selector closure design adjudication

**Decision: proceed with a conservative, bounded repair; current successor 2 has no source GO. Successor 1 remains NO-GO.** Unresolved reflection selectors must cause refusal unless an actual finite producer/use proof or a reviewed, confined reader contract establishes the relevant exclusion. Do not turn current unknowns into exceptions by filename, helper name, annotation, or recipe hash.

This is internal Astra source/design review. No candidate or project import, runtime, collection, SQL, provider operation, repository edit, or external scan was performed. Only workspace evidence and this report were written.

## Evidence and defect

I read the phase-A/B source, supplied inventory, blocker, diagnostic source/results/raw log/exit record, and the relevant actual source producers, consumers, and reader bodies. The independent stdlib-only evidence instrument completed with exit 0, tool chunk `b684b5`; see [evidence](astra-dynamic-selector-closure-design-1-evidence.json), [instrument](astra-dynamic-selector-design-evidence.py), and [raw log](astra-dynamic-selector-design-evidence-1.log). It checks all five phase-B source-attribution hashes, a changed-byte negative, the phase-A/B changed method, and every supplied inventory row against the actual AST. Its 35 checked rows describe the supplied inventory, not a claim that the inventory is exhaustive.

The inspected phase-B canonical source SHA-256 is `ec353c2d06092b7079901c1c1babc5b8952037bbcd35b85f65b1a418ec86d466`; candidate SHA-256 is `06cddb46f91b64faeba8a9cebd7d56509662799991b7469ed7cb72c45f6c5dab`. These identify the reviewed bytes, not an immutable final package or clearance.

The owner's independently isolated ordinary positive and `getattr(foreign, name)` namespace-mutation case both receive a certificate with no public refusal, global failure, or public violation. Native exit 0 (`26b6b2`) means the diagnostic observed the defect. It is not a repaired-control pass. The bad selector can designate a function's execution namespace; mutating `_source_for_ordinal` there affects the required-work key supplier before reserve completes.

In `ReserveProof.dispatch_failures`, unresolved values become an empty result in `possible_namespace_values`. The recursive assignment/alternative search then loses uncertainty instead of preserving it. A union containing one known harmless alternative and one unresolved alternative must remain unresolved. `_resolved_callable_name` spelling fallbacks also do not prove operator identity: a method named `getattr` is not necessarily Python's builtin.

## Minimum scanner model

Use a small finite domain: **known finite selectors, UNKNOWN, and protected selection**. Preserve UNKNOWN through aliases, alternatives, unsupported expressions, argument expansion, and unresolved bindings. Refuse at the relevant reflection boundary when UNKNOWN survives. Do not implement general Python evaluation, arbitrary control flow, or symbolic execution to recover a green result.

Resolve the operator and its binding form before selecting its name argument:

- Builtin `getattr(receiver, name[, default])`: the second argument is the selector.
- Proved bound `receiver.__getattribute__(name)`: the first argument is the selector.
- Proved unbound `object.__getattribute__(receiver, name)` or the corresponding `BaseException` form: the second argument is the selector.
- Aliases must preserve the proved form. Ambiguous binding, shadowing, or expansion stays UNKNOWN.

The inventory's `null` for an unbound call's receiver is not an unresolved selector. For example, `object.__getattribute__(func, '__builtins__')` has an exact protected selector and needs its existing reader-confinement proof. The literal `_safe_*`, `__pydantic_private__`, and `__pydantic_extra__` calls likewise must not be rejected merely because their receiver is nonliteral. Literal `__dict__` still requires the module/owned-storage rules; selector finiteness does not make raw namespace access safe.

Admit only a few explicit proof forms initially: exact string literals; nonescaping local literal tuples with a directly related iteration/membership use; narrowly confined helper parameters supplied solely by such callers; and exact reviewed boundary guards. Seal each form's full producer, use, bindings, dependencies, and mutation/escape constraints. A module-level tuple binding can be rebound; a set or dictionary of literal values remains mutable.

## Actual families and minimum preservation conditions

| Family | Source-backed route; remaining boundary |
| --- | --- |
| Preferences | `update_composer_preferences` has the local four-name `progress_fields` tuple and nested `_resolve_progress` called by its comprehension. Preserve that complete lexical producer/call/helper closure. Its return annotation alone proves nothing. This preserves existing preferences behavior; it does not add a tutorial-specific composer path. |
| AWS/Azure facets and module export facades | AWS `FACET_NAMES`, Azure `_FACET_AZURE_KEYS`, and the security/engine/orchestrator export tables contain literal names. Preserve only with actual producer-binding/mutation closure and the guarded consumer, or an explicit selector check before reflection. Current literal table contents alone are insufficient. |
| Counter fields | The frozen effect object and finite mapping table explain ordinary names. `apply_counter_increments` nevertheless accepts an effect argument. Prove the accepted effect producer/call closure or add a real selector boundary; annotations and the shared table recipe do not constrain every argument. |
| SDK `_sdk_field` | Internal calls use literal SDK field names, but the helper returns raw objects. Preserve the complete helper and actual caller/alias/escape closure, or validate its selectors before lookup. An external `_sdk_field(foreign, name)` must not inherit permission from those internal calls. |
| Telemetry `_owned_method` | Preserve its actual normal-lookup, MRO descriptor, exact function/method/receiver checks and validator callers. `Literal['reserve', 'assert_process']` is documentation of the intended input, not an enforced boundary. This is a specific reviewed contract, not an exemption for private helpers. |
| Generic dataclass/schema serializers | `fields(...).name` and `model_fields` are metadata producers, not immutable safe-name facts. Either prove the concrete producer/class/metadata-writer closure, or introduce a reviewed selector boundary at the lookup. Do not whitelist serializer paths. |
| Open plugin/config readers | `register_value_source_plugin` checks a nonempty config attribute but does not exclude protected selectors. `find_value_source_config` and preflight `_read_field` return raw values. They need an actual registration/lookup selector contract or a finite closed producer proof; their current generic bodies do not justify admission. |
| Jinja wrapper | `super().getattr` is a distinct method supplier. The inspected installed sandbox obtains an attribute before applying its safety policy. Preserve only with exact supplier/dispatch/dependency review, or a real owned-wrapper check before delegation. The word “sandbox” and a method-name match are insufficient. |

For truly open APIs, a small explicit selector validation before lookup is often less invasive and more auditable than proving all metadata writers. The boundary must reject the protected namespace/accessor selections relevant to the existing policy and preserve ordinary names. It must not replace existing module-namespace, descriptor, alias, raw-reader, or global-supplier checks. Rejecting only `__globals__` and `__builtins__` does not resolve raw module `__dict__` or helper-export paths. Production changes require their own source review and controls; none is implemented or cleared here.

## When an exact recipe is sufficient

An exact source/hash recipe can preserve an already demonstrated semantic boundary. It cannot establish that boundary by itself. It must include the actual reader body and all suppliers that determine name provenance, lookup behavior, output normalization, and escapes.

The inspected `deep_thaw`, `serialize_datetime`, and `deep_freeze` retain some unknown objects unchanged. The event serializer has a `deepcopy` fallback. These bodies do not prove general detached data-only output. `_sdk_field`, `_read_field`, and the registry reader explicitly return raw values. “JSON-friendly,” copying, a type annotation, and an exact hash are therefore insufficient grounds to admit unknown selectors. A helper with genuinely normalized output needs every admitted branch to exclude raw namespace aliases, callable/type/descriptor leakage, and mutation or escape before normalization. Normalization also does not erase effects already caused by unchecked lookup or formatting callbacks. The inspected column-name extractor has a narrower string/container result, but its lookup and producer still need the corresponding bounded proof.

## Required controls and remaining blocks

Keep the independently isolated ordinary positive. The exact measured dynamic-getattr defect must refuse publicly, as must bound/unbound/aliased forms, a known-plus-UNKNOWN alternative, selector rebinding, and escaped raw helpers. Exercise the actual selector argument position so an unbound receiver UNKNOWN does not become a false refusal. For each admitted family, pair a real ordinary case with a changed producer/body/guard/alias escape that must refuse. Mutate a supposedly normalized helper to return the raw namespace and require the recipe/semantic boundary to go red. Preserve the previous public alias, accessor, module-storage, and reader-export regressions; no red result caused only by an unrelated instrument failure counts as a causal control.

Source-only review limits: I inspected the relevant complete functions/classes, producer tables, imports, and consumers in the listed source files, not every unrelated line of every large production file or all installed Jinja/SDK code. The evidence records file identities and selected-call AST attribution, not whole-file semantic clearance. This adjudication intentionally does not certify inventory completeness, a future implementation, or mutable successor-2 bytes.

Before any later GO, produce a frozen repair with a complete supported-form inventory, actual public positive/negative controls, and fresh coherent recipes. The baseline-nine-field epoch does not clear the final Coordinator-eleven-field assembly. Canonical closure, source-map admission, native/coherent application controls, runtime/SQL/PostgreSQL, nonempty handoff, signing, final manual scan, and merge remain held. The original 102 obligations, four collections, and two UNKNOWNs are unchanged; the external Daybreak denial remains stopped.
