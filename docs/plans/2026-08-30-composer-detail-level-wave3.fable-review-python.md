> Saved by team-lead from the reviewer's inline return (its own file write was harness-blocked). Truncated at "one of the four no" — remainder requested via SendMessage; PART 2 appended below when received.

**VERDICT: APPROVE** — 0 Critical, 0 Important, 3 Minor, 5 Info. Both Python test files run green in the worktree (64 passed), the parity gate fails loudly under every refactor I attacked, and the deleted GraphView parity test is fully superseded.

---

# Python Review — Composer Detail Level Wave 3 (`7cd2fc6db..8b85a9314`)

Reviewer scope: `tests/unit/web/composer/test_graph_topology_parity.py` (new), `tests/unit/web/composer/test_graph_view_self_publishing_parity.py` (deleted), `tests/unit/web/composer/test_prompts.py::TestReplyRegisterRule` (new), the `pipeline_composer.md` skill edit, and the Python authorities the parity gate reads (`_producer_resolver.py`, `guided/connection_consumers.py`, `core/config.py::CoalesceSettings`, `prompts.py`). Read-only review; both test files were run scoped in the worktree.

## Verdict: APPROVE

No Critical or Important findings. Three Minor findings and several informational observations below.

## Verification performed

- `pytest tests/unit/web/composer/test_graph_topology_parity.py tests/unit/web/composer/test_prompts.py -q` with worktree-first `PYTHONPATH` → **64 passed in 10.05s**. (The pass of `test_topology_module_is_readable` itself proves the worktree tree was measured — `graphTopology.ts` does not exist at the merge base, so a main-checkout import would have failed.)
- Confirmed the live tree matches HEAD `8b85a9314` for all three reviewed paths (empty `git diff`).
- Confirmed the Python authorities: exactly one `node.node_type in (...)` arm in `connection_consumers.py` (line 32); `_IMPLICIT_SELF_PUBLISHING_NODE_TYPES = frozenset({"queue", "coalesce", "aggregation"})` at `src/elspeth/web/composer/_producer_resolver.py:76`, consulted at `:114`; `CoalesceSettings.policy`/`.merge` are bare `Literal[...]` annotations (`src/elspeth/core/config.py:1008,1012`), so `get_args(model_fields[...].annotation)` yields exactly the member strings.
- Confirmed the TS side: single declarations of all four mirrored literals in `src/elspeth/web/frontend/src/lib/graphTopology.ts` (`:89,:92,:118,:165`), double-quoted members (matching `_MEMBER_RE`), and `publishedSuccessConnection` consulting the set at `:132`.
- Confirmed supersession of the deleted test: `GraphView.tsx:49-51` imports `publishedSuccessConnection` and `FAN_IN_NODE_TYPES` from `@/lib/graphTopology`; no `IMPLICIT_SELF_PUBLISHING_NODE_TYPES` declaration survives anywhere in the frontend outside `graphTopology.ts`.

## Regex-fragility analysis of the fan-in extraction (brief item 1)

`_CONSUMER_FAN_IN_ARM_RE` (`test_graph_topology_parity.py:76`) against realistic refactors of `connection_consumers.py:32`:

| Refactor | Outcome |
| --- | --- |
| tuple → set/frozenset literal (incl. a ruff PLR6201-style rewrite) | 0 matches → **loud fail** with re-anchor guidance |
| extracted module constant (`in _FAN_IN_KINDS`) | 0 matches → loud fail |
| multi-line tuple reflow | still matches — both `\s` and the negated class `[^)]` span newlines |
| walrus / restructured condition | 0 matches → loud fail |
| second `node.node_type in (...)` arm added in the module | 2 matches → loud fail (`len(matches)==1` guard) |
| real arm refactored away while a *different* single tuple-membership arm exists | wrong arm pinned once; the `>=2` guard may pass, but the set-equality against TS `{"coalesce","row_union"}` then fails loudly unless coincidentally equal |

There is no realistic path to a *silent* vacuous green; the worst case is a loud failure with a slightly misattributed message (M3). The same profile holds on the TS side: dropping the `ReadonlySet<string>` annotation, renaming, or a Prettier switch to single quotes all land in either the single-match assertion or one of the four no
[TRUNCATED — awaiting remainder]

---
## PART 2 (resent remainder; continues mid-sentence from "one of the four no")

n-empty-members vacuity guards — every extraction site carries one; none is missing.

Two design choices I checked and endorse: the Python authority is derived via `inspect.getsource` on the *imported* module and the TS path via `Path(elspeth.__file__)`, so the test cannot read a different checkout than the one it imports (the documented worktree trap); and `test_the_ts_helper_consults_the_set_rather_than_on_success_alone` pins mechanism, not just membership — the set cannot rot into dead decoration. Its exact-substring assertion (`IMPLICIT_SELF_PUBLISHING_NODE_TYPES.has(node.node_type)`) is Prettier-rewrap-sensitive, but a rewrap fails loudly with a self-describing message; acceptable for a parity gate.

## Findings

### Minor

**M1 — Reply Register content pins survive a polarity inversion.** `tests/unit/web/composer/test_prompts.py:1465` (`test_brief_names_the_three_identifier_classes_it_forbids_in_prose`) asserts six noun phrases occur inside the section, not the normative direction attached to them. Concrete scenario: edit `src/elspeth/web/composer/skills/pipeline_composer.md:145` from "Do not echo tool-argument keys…" to "Echo tool-argument keys where helpful…" — all three `TestReplyRegisterRule` tests stay green while the rule inverts. What the tests *do* catch is the realistic drift: deleting the section, renaming the heading, dropping a bullet's vocabulary, or losing the checklist line. This matches the project's "declaration tests pin existence, not truth" posture. Remedy (cheap, optional): pin the imperative stems — `"Do not echo tool-argument keys"`, `"never by node id"`, `"Do not paste an ASCII topology tree"` — forcing an inverting edit to delete pinned wording.

**M2 — Duplicated TS-extraction helper.** `tests/unit/web/composer/test_graph_topology_parity.py:94` (`_ts_members`) and `:106` (`_ts_self_publishing_node_types`) are the same read-file / assert-single-declaration / extract-members routine with near-identical assertion prose — residue of deriving the new file from the deleted `test_graph_view_self_publishing_parity.py` (the old helper survives verbatim, only the path renamed). Failure mode is maintenance drift: a future re-anchor edits one copy and not the other. Remedy: have `_ts_self_publishing_node_types` return `set(_ts_members(_SET_DECLARATION_RE, "IMPLICIT_SELF_PUBLISHING_NODE_TYPES"))`. (Same nit in miniature: `_ts_members` returns `list[str]` and every caller wraps it in `set` — return a set.)

**M3 — The `len(kinds) >= 2` guard conflates vacuity with a legitimate one-member state.** `tests/unit/web/composer/test_graph_topology_parity.py:172`. If the fan-in arm legitimately shrinks to one kind (e.g. `in ("coalesce",)`), the test fails claiming "the regex matched something other than the tuple literal" — false, sending the maintainer hunting a regex bug. Loud and safe, but misattributed. Remedy: assert non-empty for vacuity and reword the message to name both possibilities.

### Informational

**I1 — The "shared fan-in fixture cross-check" is TS↔TS, not Python↔TS.** `SHARED_FANIN_FIXTURE` lives in `src/elspeth/web/frontend/src/lib/graphTopology.test.ts:148` and compares `buildConnectionProducers` against GraphView's `buildProducerRegistry` — two *frontend* indexes over one fixture, with genuine vacuity guards (`graphViewKeys.size > 4`, both sentinels present). The cross-*language* link remains literal-level (regex vs imported constants), not behavioral: no fixture is run through Python's `canonical_connection_consumers` and a TS consumer projection side by side. The TS test's comments are honest about exactly this ("It is NOT the cross-language mirror"). Coverage composes — each side has behavioral tests over its own index and the literals are pinned cross-language — but this gate should not be described as a behavioral Python↔TS cross-check.

**I2 — Prompt tests measure the right authority.** `SYSTEM_PROMPT` is the *composed* prompt (`src/elspeth/web/composer/prompts.py:65` — `render_with_pipelin
[part 3 received — report complete]

---
## PART 3 (final; continues mid-sentence from "render_with_pipelin")

e_capabilities(_strip_advisor_disabled_fallback(_PIPELINE_SKILL))`), so the tests pin what the model actually receives; a future strip/render step that ate the Reply Register section would fail them. Split anchors are unique and real: `## Reply Register` at `pipeline_composer.md:134`, next heading at `:160`, `## Termination States` at `:875`, checklist lines at `:886-887`. If the heading were removed, the section test dies with `IndexError` rather than a clean assert — still red, slightly uglier.

**I3 — Skill-markdown internal consistency is good.** The new checklist line names its section ("(Reply Register)"); it does not collide with the pre-existing "user register" line (`:886` governs prompt-review vocabulary, `:887` identifier register — overlapping intent, distinct obligations); explicit user questions are exempted; and the failure-reporting clause pins that the rule governs *naming*, never *whether* an outcome is reported — the exact vagueness failure mode, and the test pins that clause. The bullet's `is_valid: true` examples do not conflict with Termination States' own checklist-internal use of `is_valid: true` (`:894`), which is not reply prose.

**I4 — `CoalesceSettings.union_collision_policy` (`src/elspeth/core/config.py:1016`) is a third Literal on the same model, not mirrored in `graphTopology.ts` and not pinned.** Correct for this wave (the frontend mirrors only policy/merge), but a future collision-policy display map needs the same tuple + parity treatment.

**I5 — Wrapping a mirrored Literal later (`Optional[...]`/`Annotated`) makes `get_args` return typing objects, failing the set-equality loudly rather than silently** — acceptable; the message would be confusing but red.

## Confidence

High on the parity test's fail-loud properties (each refactor scenario traced against the actual regexes and live sources; both files executed green — 64 passed — with verified import provenance: `test_topology_module_is_readable` could only pass against the worktree tree, since `graphTopology.ts` does not exist at the merge base). High on supersession of the deleted test (whole-frontend grep found no surviving private copy of the set; `GraphView.tsx:49-51` imports from `@/lib/graphTopology`). Medium-high on skill-markdown consistency — I read the touched sections and all headings, not all 900+ lines of the prompt document.

## Information Gaps

- TS suites not executed (out of my scope); their source reviewed only where the Python contract depends on them (I1).
- Live planner compliance with the new Reply Register instruction not verified — the fix is instructional; the ticket record (elspeth-4bf65fe149, session 39578c6f) was taken at face value from the test docstring.
- Full suite not re-run; whole-tree cross-cutting gates are attested by the executor's gate run per the brief.

*(End of report. Verdict: APPROVE — 0 Critical, 0 Important, 3 Minor, 5 Info.)*
