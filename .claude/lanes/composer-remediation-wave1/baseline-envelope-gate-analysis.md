# Baseline envelope gate diagnosis

Read-only analysis, 2026-11-09. No production/test implementation, suppression,
inventory edit, or test execution performed by this investigator. The only write
is this lane note. Read repository AGENTS.md and CONTRIBUTING.md guidance, in
particular source-derived inventories, controlled measurement, whole-tree gates,
test provenance, and preserve/refuse contracts. The active remediation worktree
was not inspected as final source; source analysis below uses main checkout
`/workspace/elspeth` at `5e192342c3fb025fd9a91244bebe437158e9de8e`.

## Conclusion

The reported failures are our prior-remediation gate regression: commit
`f598aa43de0b69226fcb5ff3eb617bc46858de84` introduced a valid producer expression
that the existing scanner cannot read. This is pre-D1, not unrelated upstream
breakage. The fail-closed refusal is not evidence that the producer should emit
a different payload. The walker cannot enumerate a conditional inline mapping unpack even
though both alternatives are readable dictionary literals. Repair the walker
in `tests/unit/web/composer/test_tool_result_envelope_gate.py`; preserve the
production response and its omitted-versus-present `error_code` contract.

The minimum useful expansion is inline dictionary splats plus recursive
conditional splat alternatives, using the existing dictionary walker. Preserve
refusal on every unreadable alternative. Do not add a `_SPLAT_KEYS` attribution,
fence, skip, count change, or runtime rewrite to evade this refusal.

## Baseline evidence supplied by the state implementer

The state implementer owns testing and the current source freeze. Read its
completed artifacts directly:

- `.claude/lanes/composer-remediation-wave1/d1-state-envelope-baseline14.log`
- `.claude/lanes/composer-remediation-wave1/d1-state-envelope-baseline14.exit`

Recorded exit file is `1`. Final pytest line is:

```text
============================= 14 failed in 11.43s ==============================
```

The log records the worktree package import paths, immutable base
`66739807c9acbdae6f2d7d9f1de858b8e69c9793` substituted for the tool-batch
admission module in memory, and tool-batch AST reads using the same base. It
explicitly states every other path remains current/unchanged. This is an
instrumented base comparison on the affected selection, not a claim that this
investigator ran a fresh pristine-checkout suite.

Independent `git ls-tree` reads on main HEAD and that exact base show identical
source objects for the two files implicated here:

```text
c36c852c37510493d2f1bce6cc3441f8b5743f15 src/elspeth/web/composer/tools/generation.py
23aa56e8b9744ab77fe0555ad459067627211a2e tests/unit/web/composer/test_tool_result_envelope_gate.py
```

All 14 failures reach the same refusal:

```text
AssertionError: src/elspeth/web/composer/tools/generation.py:2215:
** splat of IfExp(test=Compare(left=Name(id='matched_code', ctx=Load()),
inside a shipped dict literal — attribute it in _SPLAT_KEYS
```

`2215` identifies the containing `return ToolResult(`; the actual unsupported
splat is at generation.py:2222:

```python
**({"error_code": matched_code} if matched_code is not None else {}),
```

The failing node suffixes (all in the envelope gate file) are:

```text
test_every_shipped_envelope_key_is_taught_or_fenced
test_no_nested_key_is_a_homonym_of_an_envelope_key
test_fence_entries_are_live_untaught_keys
test_review_blocked_payload_and_preflight_are_censused
test_review_blocked_census_rejects_an_untaught_payload_key
test_review_blocked_census_rejects_unadmitted_preflight
test_credential_builder_census_rejects_an_untaught_payload_key
test_credential_builder_owner_classification_is_required
test_every_registered_container_path_has_one_witnessed_producer
test_second_container_producer_refuses_even_with_existing_coverage[data.edge_contracts]
test_second_container_producer_refuses_even_with_existing_coverage[validation.errors]
test_second_container_producer_refuses_even_with_existing_coverage[validation.suggestions]
test_second_container_producer_refuses_even_with_existing_coverage[validation.warnings]
test_every_result_constructor_site_is_attributed
```

Several negative controls fail because they receive this unrelated refusal
instead of their expected planted-key or multiple-producer failure. Those are
masked controls, not independent producer defects or evidence they pass.

## Why the producer expression is legitimate

`_execute_explain_validation_error` in
`src/elspeth/web/composer/tools/generation.py:2171` selects a legacy regex
record in order. `matched_code` is selected only from codes whose catalogue
guidance is that exact record. A unique owner is usable without a code in the
text; a record with multiple owners and no matching code must omit the field.
The conditional splat expresses that omission without emitting null.

Existing production/contract tests explain the intended semantics:

- `test_validation_guidance_catalogue.py:37`,
  `test_unowned_match_does_not_borrow_a_code_from_elsewhere`: unrelated code
  text cannot authorize a code for unowned prose.
- `test_matching_prose_text_is_not_record_ownership`: equivalent prose is not
  record identity; its result omits `error_code`.
- `test_ambiguous_legacy_prose_does_not_invent_one_of_its_codes`: a shared regex
  record without a distinguishing code omits the field.
- `test_identifiable_legacy_prose_returns_its_indexed_code` and
  `test_multiple_explicit_codes_for_one_record_keep_index_precedence`: valid
  owned matches carry the canonical code and deterministic precedence.
- `test_public_prose_retains_first_match_and_expected_hint`: regex precedence
  and the appended expected-value hint remain observable behavior.

`ExplanationResponse` in `tools/_generation_responses.py:108` makes
`error_code` optional, and its `_code_presence` validator explicitly rejects
present-null. Unconditionally adding `error_code=None` is therefore wrong.
`test_generation_response_contracts.py` checks byte-exact real producer
responses, response admission, and declaration-selected egress. A scanner-only
repair should not require changing those frozen bytes.

`git show f598aa43d -- .../generation.py` proves the conditional splat was added
by our already-present canonical-code diagnostic fix. Before that commit, the
same legacy branch used a plain three-key dictionary with no conditional splat;
the git diff is a static before/after control, not a pre-f598 test run. This is
not a D1 state admission edit, but it remains our earlier remediation's gate
regression. The same-file object IDs at the D1 base prove independence from D1
only; they do not prove an upstream baseline defect. Do not claim item 12.07
fully closed until this scanner repair and its controls have passed.

## Exact scanner gap and bounded repair shape

The real route is `_tool_data_sites` -> `_expr_keys` -> `_dict_literal_keys`
-> `_splat_keys`. `_tool_data_sites` attributes this handler to
`explain_validation_error` and deduplicates keys per constructor site.

`_expr_keys` already unions both arms of an `ast.IfExp` when the entire data
expression is conditional (lines 2088-2091). `_nested_value_keys` does the same
for a conditional ordinary value (lines 488-493). `_splat_keys` (404-417)
supports owned-result `.data`, attributed names, resolved locals, and
attributed calls only. It refuses both `ast.IfExp` and inline `ast.Dict`, so
supporting only one of those forms cannot fix this expression.

Proposed minimal implementation, deferred to the next authorized implementer:

1. Pass dictionary context (`constants`, optional local resolver, and
   `dict_forwarded`) from `_dict_literal_keys` into `_splat_keys`.
2. For `ast.Dict`, recurse through `_dict_literal_keys` with empty prefix;
   its caller applies the outer prefix exactly once. This reuses string-key,
   dynamic-key, nested-shape, attribution, and cast refusals.
3. For `ast.IfExp`, enumerate BOTH `body` and `orelse` with the same context.
   Do not choose an arm from its condition or suppress a refusing alternative.
   The condition is a predicate, not a shipped mapping.
4. Retain existing ownership/attribution/consumer handling unchanged. Forward
   `dict_forwarded` through both recursion arms so introducing a readable
   splat beneath `dict(...)` does not weaken existing nested-cast checks.

No expansion of `_OPAQUE_REFERENCES`, `_SPLAT_KEYS`, `_DATA_HELPER_PAYLOADS`,
`_LOCAL_DATA_PAYLOADS`, constructor-file lists, or site classifications is
needed. Prefer this shared syntax support over rewriting production to suit
test syntax. Do not broaden support to arbitrary expressions or mappings.

## Required controls and inventory/mutation contracts

Before trusting a green gate, the repair needs these purposeful controls:

- Positive: enumerate an inline splat; conditional nonempty/empty and
  nonempty/nonempty splats; nested conditional splats; nested payload keys;
  prefixed data paths and emission order. Put distinct keys in each branch
  to prove that both were visited. Include an imported/module constant key
  and a handler-local or already-attributed helper branch to prove context
  survives recursion.
- Negative, parameterized for BOTH conditional arms: an unattributed helper,
  unresolved local where no resolver exists, a dynamic key, and a shape the
  splat walker still cannot read must refuse with the actual reason/site.
  Ensure an empty/readable sibling does not turn that refusal into success.
- Cast controls: a cast used as the mapping and a nested conditional cast
  payload value beneath `dict({**(...)})` must still refuse. Keep existing
  forwarding tests proving that casts only in predicate/helper inputs do
  not masquerade as shipped payload casts.
- Real-producer mutation oracle: establish the unmodified producer is
  enumerated first, then monkeypatch its parsed AST to plant a unique
  untaught key inside the actual conditional splat mapping. Assert the
  teaching gate fails for THAT key. Plant on both branches (the current
  false branch is `{}`), so visiting just one arm or silently skipping the
  entire splat cannot survive. An additional envelope homonym, e.g.
  `success`, can independently prove the nested-key backstop still sees
  the newly supported path. These are planned controls, not tests run here.

Run the full envelope gate file after those focused controls: it derives
shipped/taught/fenced agreement, forbids nested envelope homonyms, rejects
stale fences, checks admission from live redaction declarations, validates
every result constructor site against recursive source enumeration, checks
bare-name attribution injectivity, helper-derived owned types, payload
consumer non-mutation, and container producer identity/witness uniqueness.
Do not hand-pin a new census row count: newly visible keys must flow through
these existing authorities and any remaining legitimate red is a separate
finding, not a reason to weaken a pin.

Preserve `_subscript_assign_keys` / alias and result mutation checks,
`_dict_mutations`, consumer escape refusal, constructor ownership, and the
proposal/rejection exact-key and unapplied-state pins. This change concerns
enumeration of a newly readable construction, not permission to reshape a
payload after construction.

Verification scope once implemented: focused new controls and the entire
`test_tool_result_envelope_gate.py`, the repository gate-walker authority
test (`tests/unit/elspeth_lints/test_python_file_walker_authority.py`), lint
for the changed test, and a parent-owned appropriate broad gate because this
is test/scan infrastructure (AGENTS.md scope rule). Existing catalogue,
generation-response, and error-code-redaction tests are valuable runtime and
inventory compatibility controls if any producer/type/declaration changes
are proposed; this proposed scanner-only change does not authorize those
changes. Keep source/import provenance, lane-private completed logs, exit
codes, and the implementer's source freeze intact throughout.
