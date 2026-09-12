# Diagnostic catalogue — bounded implementation charter

> **Resumption reference, promoted 2026-09-12.** The [campaign plan](../2026-09-08-composer-wires-campaign.md) controls current custody, scope, order and authorization.
> This document preserves September 10 preparation; source locations, measured counts, tests and active-writer restrictions describe that historical snapshot.
> Earlier scratch records named below are optional historical evidence, not prerequisites to recover from temporary storage. Re-measure the consolidated source before implementation.
> The current request covers consolidation and planning only. Its consolidation commits supersede the historical commit ban for that scope; this document does not initiate implementation or authorize provider calls, deployment, store deletion or signing.

Prepared 2026-09-10 for elspeth-d83095ee87. Scratch-only; MODEL remains the sole repository implementer. No mutable module/gate imports, repository edits, providers, commits or deployment. Source consulted: saved `model-admission/before/src/elspeth/web/composer/tools/generation.py`, existing residue handoff, and immutable Git source for the diagnostic measurement described below. Implement only after root releases MODEL and captures its integrated frozen snapshot.

## Outcome

New validation guidance is authored directly against its machine code. Exact-code lookup is authoritative and needs no prose parsing. The legacy regex table cannot gain new patterns, replace patterns or reorder surviving patterns. Existing prose and fuzzy code-in-noise behavior remains available until an actual complete producer census shows no uncoded producers. This ticket does **not** authorize deleting that fallback now.

The small follow-on closes catalogue extension and lookup ownership; it does not migrate every validator, change rejection severity, rewrite historical guidance, add global suppression fences or establish that all validation producers already carry codes.

## Measurement correction and snapshot prerequisite

Earlier reports claimed 139 regex rows and 118 codes. These were AST top-level tuple-element counts; both tuples contain a starred generator over PluginUnavailableReason. Therefore the claimed expanded counts and any inference of growth from them were invalid. `remaining-residue-handoff.md:10` is corrected. Root was notified that `current-controller-state.md:51` also carried the inaccurate claim.

Saved generation.py SHA256: `07cfc17b655180c4365a22f6baa32aed837b791adfc6a606d2b5b963455a479a`. A read-only AST experiment expanded its constructor using PluginUnavailableReason from immutable Git commit `1278c5c215fc8f749d53fd16d57413e416b93a00` (enum-source SHA256 `14ba64ae216c7744d37b08669f56b52dffbac6e89efa42400d58eb038b9ebc1f`). This mixed-source experiment yields 144 pattern rows, 123 unique code entries and all those codes resolving; 25 patterns match none of those bare codes. **These are not accepted snapshot/current counts**, because equality of that enum and other imported guidance dependencies to the saved pre-MODEL environment was not established. The 25 unmatched-pattern observation is not an uncoded-producer count. Evidence is labelled accordingly in `diagnostic-catalogue-snapshot-census.json`.

Before implementation, capture dependency-complete post-MODEL code, import only that frozen state with both source-root provenance proved and cost-map remote loading disabled, then measure actual expanded ordered records. Include imported policy explanation/fix sources and generated enum members. Enumerate tuple row identities, code order, every code's selected first row, all multiple-match overlaps and unresolved codes. Refuse an unresolved constructor/splat rather than call its AST element a row. The accepted freeze fixture must come from this measurement, not the earlier numbers or the mixed-source experiment.

## Current authority and precise paths

Saved source authority: `src/elspeth/web/composer/tools/generation.py`:

- `_VALIDATION_ERROR_PATTERNS` near :363: immutable tuple of (regex, explanation, fix), including generated plugin-policy rows and referenced guidance constants.
- `_CLOSED_VALIDATION_ERROR_CODES` near :1442: ordered code tuple, including generated policy reason codes.
- `_augment_with_expected_hint` near :1664: only the prose-carrying tool path appends validator Expected hints.
- `explain_validation_code` near :1672: currently first regex match over its input, used by planner repair feedback.
- `_execute_explain_validation_error` near :1814: legacy ordered regex pass; then case-insensitive substring search over ordered known codes, resolving via explain_validation_code; then no-match usage guidance.

Other producer/reference paths to include in post-MODEL discovery: `src/elspeth/web/composer/pipeline_planner.py` (bare-code consumers); `src/elspeth/web/composer/tools/_common.py` (plugin unavailable explanations/fixes and source-destination note); `src/elspeth/web/plugin_policy/models.py` (PluginUnavailableReason); `src/elspeth/web/composer/skills/pipeline_composer.md`; `tests/unit/web/composer/test_planner_teaching_gate.py`; `tests/unit/web/composer/test_pipeline_planner.py` (terminal guidance test imports legacy table); the final test file owning explain_validation_error tests (locate from collected/source authority rather than assume `test_generation.py` exists).

Keep production change primarily in generation.py, with one small leaf catalogue module only if it improves dependency direction. Do not invent a framework or move the giant table merely for cosmetic separation. A new focused `tests/unit/web/composer/test_validation_guidance_catalogue.py` and frozen fixture such as `validation_guidance_legacy_patterns.json` are reasonable. Follow existing tree-gate walker authority if a whole-root producer check is added; no new rglob walker.

## Minimal record and lookup design

1. Retain the legacy ordered pattern records as fallback authority. Make their scope explicit in naming/documentation. Compile patterns once if useful, but preserve exact source regex and order for the membership fixture. Explanation/fix text remains attached to its record; do not duplicate that prose in a new dict.
2. Introduce an owned immutable direct-guidance record, e.g. `ValidationCodeGuidance(code, explanation, suggested_fix)`. Author **new codes only** as such records. Use a sequence of records before building the immutable mapping, so duplicate codes are detected rather than silently overwritten by dict construction. A grouped-code record can share genuinely identical guidance, provided expansion is derived and duplicate admission is checked.
3. Build an exact-code index from actual records: retain the frozen legacy code seed/order during migration; resolve each seed once to its historical first matching legacy record and index the **record reference**. Merge direct records only after asserting no accidental collision. This gives exact lookup for existing codes without reauthoring their guidance. A migrated legacy code may deliberately switch authority to a direct record only in a separately reviewed parity-preserving change; do not allow silent dict overwrite to choose priority.
4. Derive known-code vocabulary and public list from this resulting record index, preserving legacy code order and appending direct-record order. Do not maintain a second new-code Literal set, list or regex alternation. Each new record is the one code/guidance authority; the fuzzy route and no-match help derive their code inventory from it.
5. `explain_validation_code` returns the exact index entry's explanation/fix. Unknown or non-string/empty input returns None under its documented contract. It must not classify an unknown string merely because it resembles an unrelated regex. Before tightening this historical accidental behavior, inventory its actual callers/tests from the frozen snapshot; legitimate prose remains handled by the public tool path below.
6. In `_execute_explain_validation_error`, exact recognized code lookup gets priority. Preserve the existing successful exact-code output shape (error_text/explanation/suggested_fix); use that same shape for both legacy and new direct-code inputs. Do not make output presence depend on which generation of catalogue owns the code. For non-exact text, preserve legacy regex first-match order, then current case-insensitive code-in-noise fallback and its successful code-bearing form, with new direct-code records included in the derived vocabulary. Existing legacy substring precedence must not reorder during migration.
7. Preserve `_augment_with_expected_hint` on the public tool's prose path, but never fabricate an Expected hint in the bare-code accessor. Preserve the no-match usage message and string-based known-code rendering; the saved source explicitly avoids an oversized `known_codes` array that would collapse persisted redaction.

Exact-code authority and retained fallback are compatible: the index is code->guidance; prose fallback is regex->the same legacy guidance records. The index does not need to interpret new guidance through a regex, and fallback does not need to be deleted to make that true.

## Freezing actual legacy membership and order

Use a clearly labelled **historical compatibility fixture**, not a second live schema authority. Capture each expanded regex's exact spelling and its ordered occurrence identity from the accepted snapshot. The current ordered regex list must be a subsequence of that baseline, preserving multiplicity; no new regex, replacement or reorder can pass. A count-only maximum or set-only subset check is insufficient.

Explanation/fix prose may still be corrected in its authoritative record; the freeze is regex membership/order, not a ban on useful truthful teaching updates. Differential code/prose tests catch behavioral consequences. If two identical regex strings exist, preserve occurrence handling rather than using a set that loses duplicates.

The policy enum expansion is a specific escape: adding a new PluginUnavailableReason must not automatically create a new legacy regex row. Freeze the historical expansion membership as part of the legacy compatibility baseline, or migrate the generated policy records to direct code authority with preserved historical fallback records. New enum members derive new **direct** guidance from their owning policy records and fail if guidance is absent. Do not introduce another unreviewed shadow policy enum. A scoped historical subset is a compatibility fence with an explicit removal path, not a fresh vocabulary for production policy decisions.

Do not allow arbitrary removals that silently erase code/prose repair support. The subsequence rule permits shrinkage syntactically; behavior tests must still prove preserved required guidance or document the exact retired producer. Full fallback retirement remains blocked on exhaustive uncoded-producer zero, a distinct census not performed here.

## Coverage derived from records

Catalogue checks derive:

- expanded legacy ordered rows and their baseline membership;
- legacy code seeds and each code's selected first legacy record;
- direct code record keys and duplicate/collision detection;
- final index keys, fuzzy search vocabulary and no-match help vocabulary;
- all direct records being reachable through exact public/internal lookup;
- all previously resolving legacy codes retaining guidance.

Do not compare a manually copied “expected new codes” list with the same manually copied dict. Use actual new direct records as authority, plus a real new producer code as a consumer-path witness. For new producer coverage, derive the code from its owned enum/raise site/ValidationEntry constructor using the existing relevant gate reader, then require an index entry. If a source construct cannot be resolved, report/refuse it; do not guess through regex search. A complete global validation producer inventory is not required to land this small freeze, but all **newly introduced/touched** code producers must have their real lookup path pinned.

Prior teaching/terminal checks that read regex tuples must be reviewed: new direct-code guidance must not disappear from TAUGHT or terminal no-reemit checks merely because it lacks a regex. Repoint those readers to the unified guidance-record authority, preserving their fact-key/terminal semantics. Do not globally loosen teaching to accommodate the new catalogue.

## Meaningful tests and mutation ledger

1. Known legacy exact code returns byte-equal guidance to the frozen baseline's first matching record. Cover overlapping patterns explicitly, not only one nonoverlapping example. A public exact-code input retains its intended response fields/order.
2. Add one real direct guidance record (source-destination code is a useful first consumer below). Internal accessor and public tool both resolve it without a new regex row. Unknown exact code returns None internally; public no-match behavior remains helpful and truthful.
3. Legacy prose matching remains first-match, case-sensitive as before; uppercase/noisy code fallback still works with existing ordered substring behavior. New direct code in noise is resolvable. Keep Expected hint extraction and redaction-size behavior intact.
4. Fixture gate rejects inserting a new regex, replacing a regex at equal count and swapping two surviving regex records. Deleting a legacy row is accepted by the membership rule only if retained code/prose behavior or legitimate retirement is also accounted for.
5. Missing direct guidance for a real new/touched producer code fails. Missing direct record must not be hidden by a permissive regex. Duplicate direct code and accidental legacy/direct collision fail before index construction.
6. Final vocabulary derives from records: removing a direct record removes its inventory entry and makes the real producer coverage test fail; adding a record makes exact lookup/help/fuzzy coverage available without editing a second vocabulary.

Mutations, each with same baseline/mutant/restored test identities and explicit exits:

- Bypass direct exact lookup: the real new-code lookup test must fail, even with legacy table intact.
- Remove one real required direct record: producer-to-guidance coverage fails (not just a fixture-count assertion).
- Add a regex row while removing another to keep count fixed: membership gate fails.
- Swap two overlapping legacy rows: order gate fails; a discriminative prose sample must also show first-match drift where the baseline has such overlap.
- Remove legacy fallback: a genuine legacy prose fixture with no direct exact-code input fails.
- Restore per-code regex scanning in the internal exact accessor: an unknown string that accidentally matches a broad pattern must remain unrecognized, while real known-code resolution still passes.
- Omit new record keys from derived fuzzy/help inventory: direct and public noisy-code/help tests distinguish the broken wire.

Record replacement-applied proof, actual failure cause, collection parity, restored hashes and exit codes. A failed import or changed test selection is not semantic mutation evidence. Keep tests local/pure; no provider call is needed to prove lookup order.

## Source-destination-note follow-on and count applicability

The residue census found `_vf_destination_note` duplicating a real structured state error `quarantine_unknown_output`; preserve the existing error severity and validator authority. The follow-on can add/reuse a **direct** catalogue record for that exact existing code, prove the mutation result's real validation.errors code resolves to it, then remove redundant data.note and its teaching/helper-gate references. Do not create a second warning or new regex to carry that fact. Recheck the post-MODEL/twin-retirement source first: if guidance already exists, reuse it instead of duplicating its record.

Later expected-output-count applicability diagnostics should likewise register direct code-guidance records from their actual final producer authority. They should not grow the regex table or duplicate a new code vocabulary elsewhere. This charter does not determine the applicability policy or invent its code strings; those come from the separately authorized implementation.

## Completion and limits

After freeze: capture expanded membership/order with dependency provenance; implement direct exact-code authority plus derived vocabulary; update owning teaching/terminal readers; pass focused lookup, planner feedback, no-match/redaction and producer-consumer tests; reconcile mutations; run proportional lint/type gates and report baseline debt honestly. Parent owns integrated campaign verification.

Do not report uncoded producers zero, regex deletion readiness, current 139/118 counts, or provider repair-turn improvements from this work. No live trials, signing, publication or permission to widen disclosure is included. The compelling reason for the retained regex table is compatibility with uncoded legacy prose; the new-extension path has no such reason and closes directly on codes.
