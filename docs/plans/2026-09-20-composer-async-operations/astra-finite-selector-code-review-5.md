<!-- Durable copy: machine paths normalized; private original retained. -->

# Astra finite-selector successor5 source review

**Verdict: NO-GO for scanner readiness. The specific successor4 F1 relevance repair is sound in the reviewed scope, but an inherited mixed-import operator-proof defect remains blocking.** The new `possible_reflection` closes the previously reported enclosing alias, mixed enclosing assignment and local-import conditional-rebind omissions. It does not repair `reflection_forms`, which can wrongly classify a conditionally selected foreign callable as a proved builtin. Credit for the narrow repair is separate from this newly demonstrated blocker.

This review concerns immutable `contracts-reserve-proof/finite-selector-successor-5`, canonical SHA `1f97892350696b6c63b3f28d8ca1045cd96ff54199b4c7ea0837c429eff4499d`, selected scanner-only patch SHA `7f19cca967d8bdebd5312af19ba38de39cc2a641fbb56ac086df7b757b346b07`. The historical complete.patch recipe changes remain unselected. Previous successor3/4 reports and packages remain unchanged.

## Scope and exact source evidence

I read the full successor report, review delta, supplied control source and final raw control logs/exit mapping, supported-forms declaration, complete `_Resolver` implementation and `_lexical_scope`, and the affected helper/call-loop dependencies retained from the preceding review. This is a bounded successor review, not a fresh review of every pre-existing canonical scanner rule.

All 16 frozen map entries match. Both selected patches reconstruct the exact replacement from their stated preimages using a controlled in-memory patch instrument. Its positive works and its changed-preimage negative refuses; a changed candidate byte also fails its hash guard. Replacing only `possible_reflection` with the predecessor definition restores the complete predecessor canonical AST; an independent comparison does the same for the standalone scanner AST. All predecessor4 frozen guards still match. The live canonical scanner remains exactly the selected preimage, and every supplied actual-source record remains internally hash-consistent and equal to its current live file at verification.

Reviewer execution was limited to stdlib byte/AST instruments and exact extracted scanner-analysis helpers plus the exact reflection-call branch on synthetic parsed source. No candidate/project module import, authored checker execution, pytest, collection, SQL, provider, native child, repository edit or application occurred.

## F1 from review4: repaired in the bounded source scope

`possible_reflection` now uses actual resolver imports and assignment events across lexical owners, rather than only immediate assignments or a module-level import spelling fallback. Captured class scopes are skipped in the same way as the resolver's binding search. Recursive provenance uses visited owner/name keys; mixed provenance only engages relevance. The new function does not itself convert a mixed origin into a known operator form.

Independent extracted-source probes confirm that enclosing `lookup=getattr`, enclosing `lookup=getattr if condition else foreign`, and local builtin import followed by conditional foreign assignment now reach the UNKNOWN refusal. Direct, imported and qualified builtin unknown selectors also refuse. A wildcard-shadowed alias remains UNKNOWN. A class-local assignment is correctly excluded from a method's lexical lookup, while an enclosing function alias remains visible across an intervening class. An unrelated foreign callable remains outside this narrow relevance classification.

The retained F2 definition-context repair remains unchanged. Reviewer source controls still preserve the ordinary native str/None helper and return UNKNOWN for decorator, default callback, annotation callback, foreign annotation, rebound str and unknown argument variants.

## F3: blocking inherited defect — conditional import is mistaken for operator identity

The requested mixed/source-binding review exposes a separate defect in the unchanged `reflection_forms`/resolver seam:

```python
def f(obj, condition):
    from foreign import lookup
    if condition:
        from builtins import getattr as lookup
    return lookup(obj, "ordinary")
```

When `condition` is false, the actual callable is the foreign import. The exact resolver nevertheless returns `builtins.getattr`: `_local_import` selects the greatest eligible import line without proving that branch ran. `reflection_forms` then accepts that qualified string as `{'getattr'}`. The selector is the known literal `ordinary`, so the actual extracted reflection-call branch emits **no failure**. Relevance correctly engages, but neither forms nor selector contains UNKNOWN.

The captured equivalent also fails:

```python
def outer(condition):
    from foreign import lookup
    if condition:
        from builtins import getattr as lookup
    def inner(obj):
        return lookup(obj, "ordinary")
    return inner
```

`_scoped_import` chooses the last enclosing import without a dominance proof. The same exact branch emits no failure. The operator's actual identity has not been proved, regardless of the literal selector. A foreign callable can implement a different calling convention or return an execution namespace; ordinary selector text is not a builtin identity certificate.

Evidence contains the exact sources, resolved names, forms, argument domains, relevance decisions and actual extracted branch failures. Ordinary direct builtin is a positive with no failure; direct unknown selector and all repaired F1 variants produce the intended refusal. These measurements are source-analysis evidence, not a full scoped-checker/public-gate or native execution claim.

This is **not a regression caused by successor5's single helper change**: the known-form weakness is inherited. It nevertheless blocks the combined scanner claim that mixed potential provenance remains UNKNOWN. The public recipe HOLD does not make that soundness defect harmless or prove another gate will reject it after recipes are repaired.

Required next step: independently prove builtin operator identity from the actual complete import/assignment binding events and their control-flow context, or retain UNKNOWN for unsupported/mixed contexts. Do not use resolver qualified-name output as dominance proof. Add separately attributable local and captured conditional-import negatives with a literal ordinary selector, plus unconditional builtin-import positives, so an UNKNOWN selector cannot mask the missing operator proof. Preserve possible-reflection relevance and its newly repaired cases.

## Supplied controls and remaining holds

The complete final raw scoped log and completed receipt identify author exit 0, chunk `640f53`. The JSON independently records 32 source cases, two ordinary positives and 30 own refusals, with consistent source hashes. The five new cases each have their own intended reflection refusal, not a stale recipe failure or an unrelated outer reserve. This substantiates F1 closure. None tests the foreign-import/conditional-builtin-import pattern with a finite selector above.

The definition helper log/receipt identify author exit 0, `1e0d85`, actual preferences finite and 11 UNKNOWN mutations. No final-byte public or baseline source-family cohort was run for successor5. Successor4's full public ordinary control remains a historical failing positive due stale transitive recipes; no successful final-byte public certificate is inferred.

The single-helper repair can be retained in a successor. Scanner readiness remains held for F3 and, independently, actual11/source51/52 recipes, global/source-family admission and supplier/effect/escape obligations. Original 102 obligations, four collection obligations and two UNKNOWNs remain. No runtime, PostgreSQL, merge, deployment, signature or Daybreak clearance is granted.

## Durable evidence

- `astra-finite-selector-code-review-5-evidence.json` and `...-guards.py`: exact guards, controlled patch reconstruction, current preimage/snapshot and canonical single-helper AST comparison; reviewer completed 0, `c6c689`. The same run verifies predecessor4 preservation.
- `astra-finite-selector-code-review-5-alias-evidence.json`: 14 parsed-source cases and the exact reflection branch results, including F3.
- `astra-finite-selector-code-review-5-definition-evidence.json`: preserved bounded definition controls.
- `astra-finite-selector-code-review-5-probe.py` and `.log`: final reviewer source-analysis instrument and complete output; completed 0, `e95ba6`, full output read in `309723`.
- Standalone AST and authored-case JSON verification completed 0, `587b13`.

An intermediate reviewer instrument edit accidentally renamed the definition-results variable and ended with NameError (`4836fc`, exit 1). Its script and partial stdout are preserved as `...-probe-failed.py`/`.log`; the error was in the reviewer harness after alias output, not a candidate failure or causal negative. The corrected final instrument completed and its complete output was read. No candidate bytes were changed.
