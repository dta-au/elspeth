# Planner guidance and palette parity — bounded design

Prepared 2026-11-09. Preparation only: no production/test edits, tests, scans,
provider calls, commits, Page writes or closure claims. Source evidence below
is pinned to `14fef5e5342ccbd79b4fdd69b276c20f77d66dc1` and was read with
`git show`/`git grep`, not inferred from the earlier audit baseline. The active
worktree is `/workspace/elspeth/.claude/worktrees/composer-remediation-wave1`;
the parent owns its frozen-tree gate and test capacity. Rebind this map to the
eventual integration commit before implementation.

## Intent and boundary

The next independent R8 slice is **planner-facing guidance that names the
planner's real authoring route**. A new-build planner must be taught to discover
facts, author its complete candidate through `emit_pipeline_proposal`, repair
that candidate after rejection, or use its existing conversation/recovery exit.
It must not be taught to call the freeform mutation, blob-writing, advisor or
review tools that it does not have. This is a correction to the instructions
delivered to the existing provider-authored planner, not a new authoring path.

Keep the complete canonical language, request fidelity, literal preservation,
review provenance and policy constraints. No server-generated candidate,
automatic repair sequence, tutorial exception, newly available tool, or fallback
graph is part of this work. In particular, remove a misleading instruction
without pretending that the unavailable operation became possible.

No D3–D38 ruling is needed for this boundary. D1's current admission/no-op
behavior remains authoritative; D2 and the portable MCP lifecycle are separate
work. The slice must not teach MCP filesystem admission before those land.

## Audit and checkbox mapping

Reference: `docs/architecture/composer-tool-assessment/remediation-plan.md`
R8, lines 574–626 and Q19; the `emit_pipeline_proposal` sheet §5.1/§6.1 item 7;
the `create_blob`, `wire_blob_inline_ref`, `get_plugin_assistance` and
`explain_validation_error` guidance sections. Canonical checkbox text was read
from `guidance-tracker-excerpt.md` in this lane, supplied by the parent from the
live Page. The audit documents are imported working documents in the main
checkout, absent from the pinned production commit; they are a separate audit
authority. Only the production citations below are immutable-source evidence.
This table is scope, not updated checkbox state.

| Checkbox | Contribution from this slice | What still limits closure |
| --- | --- | --- |
| 13.01 Render planner guidance using the actual palette | Planner system/interaction guidance and context use the planner route, with named discovery operations conditional on the advertised palette | Must prove the actual service-to-provider path, not just a helper rendering |
| 13.02 Remove or explicitly scope unavailable mutation instructions | Planner overlay, shared core, aids, declaration descriptions and public hints no longer issue unscoped unavailable commands | Other route scopes must remain explicit and non-executable on this route |
| 13.03 Scope plugin hints and catalog repair sequences | Planner-reachable list/schema/assistance hints and catalogue explanations attached to planner feedback | Whole checkbox also includes MCP/freeform ownership; do not close it from planner-only evidence |
| 13.04 Remove planner completion instructions requiring meaningless preview/diff | Teach candidate submission and existing validation/approval outcomes; no saved-state preview/diff prerequisite for an unsaved candidate | Do not remove/redefine `diff_pipeline` (D8) or alter preview semantics |
| 13.05 Teach supported planner inline-blob path | Teach `source.inline_blob` inside the complete terminal candidate and its existing provenance/refusal behavior | No new embedded-file resolver, plural inline custody or blob-writing tool exposure |
| 13.15 Palette-parity tests | Exercise system, aids, discovery hints, declarations and repair feedback on the actual planner route | MCP/freeform full parity remains a separate acceptance item |
| 13.17 Intentional text/hash updates | Regenerate only affected teaching/declaration/wire/planner pins through producers | Review exact byte deltas; a hash update alone proves no behavior |
| 13.16 Exact grammar operator token oracle | **Preserve already completed work** | No duplicate implementation or renewed completion claim |

13.06 (freeform manifest instructions), 13.07 (MCP blob/profile hints),
13.08–13.09 (D7 reread/discovery-cap policy), 13.10–13.14 (patch/schema/routing/
review/wire semantics) are not the deliverable. A shared sentence may acquire
an explicit route scope to make the planner rendering truthful; that does not
settle or close any of those behaviors. The retained false-reread policy is an
explicit separate D7 dependency, not something to silently change while moving
text. Grammar reference expansion is also outside this slice.

## Exact-source route and callsite inventory

All paths below are relative to the repository at the pinned commit. These are
source-established paths and examples, not new runtime counts or passing tests.

| Boundary | Source evidence | Consequence |
| --- | --- | --- |
| Static rendering | `web/composer/prompts.py:50–104`: loads `pipeline_composer` and `pipeline_capabilities`; `SYSTEM_PROMPT`, `render_system_prompt`, `build_system_prompt` prepend the core and append optional deployment `pipeline_composer` text | There is one current freeform rendering; title alone does not scope its use |
| Service custody | `web/composer/service.py:733–752,789–801`: verifies two source hashes, freezes `_composer_skill_text` and its hash, passes the same text to `PlanningApplication` | Route correction must include service construction, drift checks and hash custody |
| Planner entry | `web/composer/planning_application.py:1093,1179`: `rendered_skill = self._composer_skill_text`, then `plan_pipeline(rendered_skill=...)` | Fixing only the standalone planner default/test helper leaves production wrong |
| Provider requests | `web/composer/pipeline_planner.py:3392,3414,3457–3496`: hashes supplied skill, constructs discovery policy and its tools, sends skill as system text plus aids/reference and request instruction | Test these exact outbound messages and tool list together |
| Planner palette | `web/composer/capability_skill.py:16–41`; `pipeline_planner.py:355–397,1652–1674`: emitter accepts an ordered discovery subset, but production `initial` selects the full `PLANNER_DISCOVERY_TOOL_NAMES` tuple and `with_manifest` preserves it | Ordinary production-route tests cover that full policy. A narrowed tuple is only a synthetic test injection, not an existing production mode; still compare against captured outbound tools |
| Call hashing and hatch | `pipeline_planner.py:3595–3634,4243,5130`: active tools, dialect checks, exact-input manifest, hatch notice and policy-tool refresh | Cover primary, repair and hatch requests. Do not assume a documented `terminal_only` arm is actually selected here: this callsite passes `tool_surface="full"` |
| Core identity | `capability_skill.py:155–160,205+`: exact core prefix occurs once; canonical field inventory remains pinned independently of selected terminal | Keep the core surface-neutral and preserve its exact-prefix/field contract |
| Shared aids | `prompts.py:266` and `pipeline_planner.py:3414`: both routes call `build_planner_authoring_aids` | A planner fix must not erase the valid freeform variant |
| Source custody aids | `planner_authoring_aids.py:86,348–390`: placeholder and initial rules mention `create_blob`; later source-binding rules already distinguish ordinary/freeform from proposal planner | Remove the unscoped planner command, retain existing route-specific preservation rules; teach terminal `source.inline_blob` |
| Existing good route-scoping precedent | `planner_authoring_aids.py:909–975`: review staging versus review-tool calling are already distinguished by palette | Preserve staging and backend surfacing; do not decide D10's ownership disagreements |
| Freeform-only interaction currently sent to planner | `skills/pipeline_composer.md:1–11,51–55,175–216,334–369,635–651,1315–1385` | Build/edit batches, blob creation, advisor and review calls, and saved-state completion criteria need a freeform scope |
| Shared core operational leakage | `skills/pipeline_capabilities.md:13–75,417`: canonical shape called `set_pipeline`, information-manifest workflow, discovery calls, rejected `set_source`, compaction via ordinary tools then preview/diff | Distinguish schema-name references from commands. Scope operational instructions instead of changing handlers |
| Planner repair feedback | `pipeline_planner.py:2444–2446`: `_allowlisted_candidate_feedback` attaches `explain_validation_code` output; `generation.py:477,487,502–512,785,1124,1646,1651` have mutation-tool commands | Correct source-owned repair advice so it remains actionable as candidate correction, or explicitly label other-route commands |
| Direct explanation lookup | `tools/generation.py:2153+` / `_handle_explain_validation_error` and declaration at `2282+` | Changing catalogue wording also changes this discovery tool's result, so verify it through the real handler |
| Public hint production | `catalog/service.py:312,330–345,439`: discovery assistance supplies `composer_hints` to summaries and schema; `tools/generation.py:2363–2386` separately gets and projects assistance | A fix limited to one carrier misses the same text delivered through another |
| Profile projection | `catalog/policy_view.py:101–123,190–223`; `plugin_policy/profiles.py:1594–1730`: profile-visible summaries/schema/assistance can replace raw plugin guidance | Use request-projected objects in tests; testing only raw plugin classes misses replacement hints |
| Concrete raw hints needing scope | `plugins/sources/azure_blob_source.py:465`, `sources/dataverse.py:1159`, `sources/null_source.py:84`: `create_blob` + `set_source_from_blob`; `sources/blob_rows.py:148,176`: plural resolver; `sources/csv_source.py:713`: defaults across tools; `transforms/reference_join.py:972–975,994`: blob discovery/wiring | Source inventory examples, not an exhaustive registry census; implementation must enumerate live public producers |
| MCP boundary, held out | `composer_mcp/server.py:117–120,210–224` emits filtered tools plus session utilities; `catalog/policy_view.py:29–43` has trained-operator raw projection | MCP gets no web system skill here. No MCP transport/profile/path policy should be invented by a web planner fix |

The source search included known-positive `create_blob` and `upsert_node`
commands and showed their concrete locations; the exact declarations establish
why planner membership differs. No source-regex count is used as a registry
inventory. The future test must enumerate actual emitters and real registry
objects, including projected hints, rather than freezing this example list.

## Approach choice

Three feasible approaches were considered; the recommendation is an engineering
proposal for review, not an asserted user choice.

1. **Explicit planner interaction overlay, shared core, scoped shared guidance
   (recommended).** Keep the existing `pipeline_composer.md` as the freeform
   policy. Add a small planner overlay and render it through the existing core
   composition function. Repair shared source-owned prose at its owner. This
   avoids sending the planner a freeform tool inventory while keeping one
   canonical language and the existing tool/feedback schemas.
2. Scope the entire current freeform document as reference and append a planner
   override. This is smaller mechanically but retains contradictory imperative
   text and costs prompt bytes. It is not the recommended final state.
3. Introduce a generalized guidance DSL, new plugin assistance metadata or a
   route-rewriting catalog proxy. This can offer more dynamic rendering but is
   a larger public contract/cache/policy change than this defect requires.
   Avoid it in this slice. Never rewrite arbitrary strings or tool names by
   regex at delivery time.

### Planner overlay and shared policy

Add `skills/pipeline_planner.md` as the public planner interaction overlay.
`render_planner_system_prompt(data_dir: str | None = None) -> str` composes the
same exact capability core with that overlay; keep existing freeform helpers
and `SYSTEM_PROMPT` meanings stable. `ComposerService` freezes a separate
planner rendering, verifies its source drift, and supplies that rendering to
`PlanningApplication`. The planner continues deriving `composer_skill_hash`
from the exact text it actually received, and per-call manifests continue to
hash exact outbound messages/tools. Do not reuse the freeform hash for planner
bytes, globally replace the advisor's freeform skill, or weaken drift checks.

The overlay names only invariant planner controls: `emit_pipeline_proposal`,
`handoff_to_user`, `restore_parked_graph`. Detailed discovery instructions name
a tool only inside an explicit condition that it is advertised for the current
request. The advertised tool definitions are authoritative; do not copy a
second static planner inventory into the skill. Source-owned cross-route
reference text must identify that other surface locally, in the same sentence
or bounded paragraph, rather than relying on a global disclaimer.

Preserve the optional deployment overlay's established loading semantics. Do
not silently drop operator knowledge, add a new deployment filename requirement,
or infer deployment instructions from user content. The route policy must make
the current palette authoritative for deployment guidance too. Tests use a
controlled deployment fixture and include it in exact-text/hash checks. The
parity claim concerns shipped public instruction sources; an arbitrary
operator-authored overlay is not proof of curated public parity and must be
reported separately if it contains unavailable commands. This slice does not
introduce a new runtime refusal of operator text.

Use the preservation owner map below before replacing the planner's interaction
overlay. Keep existing semantics. Move principles into one shared owner only
when necessary; do not copy the large freeform review matrix into a second
authority or resolve D10 by omission.

### Preservation owner map

The old spans below were re-read at the pinned source after design review.
`PC` means `skills/pipeline_composer.md`, `CORE` means
`skills/pipeline_capabilities.md`, and `AIDS` means `planner_authoring_aids.py`.
The proposed new overlay headings are concrete content owners, not a production
metadata framework. Before implementation replaces the old overlay, its report
must fill in each new owner's actual final source span and exact test id. A
heading or keyword alone is not preservation evidence.

For every row, assert the complete reviewed instruction paragraph/rule in the
actual outbound provider carrier, including its trigger, obligation and limiting
clause. Add an in-memory deletion or polarity change of the decisive clause and
require that same assertion to fail. Where a runtime/custody boundary exists,
also use the production-path control in the last column. These controls establish
content preservation and behavior/custody reachability, not that the prompt
caused a live model to follow the instruction.

| Obligation and old source span | Retained/new owner | Meaningful assertion and control |
| --- | --- | --- |
| Authenticated request history versus assistant claims — PC:59–64 | New planner overlay, `Evidence and request authority` | Preserve the entire distinction: user requests establish requested work; assistant prose does not establish request, approval or saved effects. Capture a service request with contradictory earlier user/assistant messages and assert they remain in separate `prior_user_requests`/`prior_assistant_messages` collections with their ordered `history_index` values. Do not assert that an assistant claim becomes an approval. Source owners remain `planning_application.py:194–242` and `pipeline_planner.py:570–658`. |
| Supplied-but-unread references and model-authored judgement — PC:111–126 | New planner overlay, `Evidence and request authority` | Preserve the complete instruction to read supplied normative references before declaring a missing fact, and to have the model author every judgement. In a captured request with an unread supplied reference, retain its reference/custody data; exercise the real available read tool before scripted candidate submission. A genuinely absent reference uses the existing conversational/named-gap path without a fabricated candidate. The scripted read order is a reachability control, not model-compliance evidence. |
| Exact literals and adaptation disclosure — PC:258–262; AIDS:647–655 | Retain AIDS `_USER_DISCLOSURE_RULES`, literal/adaptation rules; new overlay's `Requested workflow integrity` refers to that rule | Assert the whole character-preservation rule and adaptation/user-decision condition, not just `literal`. Send a supplied prompt with significant whitespace/case/punctuation through a real scripted candidate path and compare actual authored prompt bytes; an alteration must fail that equality. Keep the disclosure distinction between unchanged text and added row variables/output instructions. |
| Single-variable A/B invariant — PC:264–273 | New planner overlay, `Requested workflow integrity` | Preserve all constrained dimensions and the exception for the user-selected variable. Compare the two scripted submitted branches' source/model/profile/sampling/other prompt/response schema/extraction values, allowing only the declared experimental variable and permitted distinct result names. A mutation to another sampling or schema value must fail the comparison. This checks the candidate fixture and preservation oracle, not a new runtime A/B gate. |
| Requested topology, no silent downgrade and mechanical repair — PC:275–332 | New planner overlay, `Requested workflow integrity`; retain CORE `[capability:structured-output-repair]` | Preserve the prohibition on dropping a requested producer, cleanup, branch or output to reach validation, and complete-document resubmission after rejection. In the real scripted reject/repair loop, compare submitted node/output sets and required edges before/after repair, retain pending review rows, and assert the missing producer is restored rather than a consumer requirement being erased. |
| Step descriptions and bounded user-facing outcomes — PC:219–254 | Retain CORE:266–270 for step descriptions; new overlay, `Descriptions and user-facing outcomes`, for conversational exits only | Assert purpose/refresh/informational-only clauses together. Verify descriptions survive actual candidate projection unchanged without altering routing. Capture a conversational exit's actual exposed text and assert it distinguishes validation status from authored intent; do not require ordinary terminal-turn prose to be shown when this route does not expose it. |
| Source custody, generated bytes and first-person provenance — PC:653–776; AIDS:348–408 | Retain AIDS `_SOURCE_CUSTODY_RULES` with the scoped fallback correction; existing source provenance/custody handlers remain owners | Preserve exact existing-binding and no-fabricated-id rules, generated-content exception only on explicit request, same-artifact repair, and first-person `I generated this content as you asked` rather than discovered/system-generated attribution. In real singular inline custody, assert the successfully admitted source bytes/hash and invented-source proof agree; after rejection, resubmit the same artifact bytes without treating the rejected call as a reusable blob receipt. Uploaded data retains its distinct provenance. Reading generated bytes back must not relabel them external. |
| Authored judgement staging and review authoring shell — PC:972–1030,1214–1313; AIDS:909–975 | Retain AIDS `review_registry` rules; new overlay, `Candidate review and completion`, supplies planner-only application of the existing authored shell and judgement rule | Preserve authorship-based staging rather than magic-word detection, exact `draft`/`user_term` semantics, same-node `interpretation_ref` wiring and the public authored field set. Through the real candidate finalizer, a valid staged/wired judgement remains admitted and an unwired or manually server-owned field case retains its current refusal. Preserve backend-owned review rows and no planner call to unavailable review tools. Do not adjudicate D10's existing ownership discrepancies. |
| Required versus recommended protective controls — PC:1155–1175; CORE:469–477; AIDS:497–640 | Retain CORE untrusted-content principles and AIDS conditional prompt-shield/content-safety rules; overlay points to live policy rather than duplicating plugin facts | Assert recommendation is not authority to add topology, a placeholder is not protection, required controls remain required, and policy-required auto-wire disclosure is server-owned. Exercise existing required/recommended/no-usable-control policy variants through the candidate finalizer and inspect actual candidate/auto-wire outcomes and review provenance; no policy bypass or synthetic substitute is introduced. |
| Required failure-report retention and honest evidence — PC:1177–1191; AIDS:659–670 | Retain AIDS `_USER_DISCLOSURE_RULES` failure-policy rule; add only obligations already present in PC but absent from that AIDS rule | Preserve identity plus carried-row retention, hash versus readable-cause distinction, explicit unsupported/missing-cause failure, no invented cause/no full-compliance claim, no rollback of prior publications and no claim that business-gate reasons are inferred. Assert the actual captured public rule contains these complete clauses. Pair with existing real report/validation fixtures for supported durable cause versus missing/unsupported cause; use the current sink's production schema/handler, not fabricated report output. Do not change retention policy. |
| Output minimization and review provenance — PC:1193–1212,1305–1313; CORE:479–486 | Retain CORE minimization rule and AIDS `review_registry`; new overlay's `Candidate review and completion` retains the implementing-node review instruction | Assert cleanup must really remove named fields after their last producer, preserve requested results, and cannot be replaced by labels/formats. A user-authorized retention choice still retains the required registered review row on the implementing cleanup node. Through the candidate path, inspect actual cleanup config, location/routing and preserved review record; a renamed passthrough or row placed on the producer must fail the preservation comparison. No new plugin guarantee is inferred. |

Existing tests are starting points, not unverified passing evidence:
`test_pipeline_planner.py` conversation-context cases at 10218+ and generated
source cases at 1197+, `test_planner_authoring_aids.py` user-disclosure cases at
2177+ and custody/provenance cases at 2679–2734, plus the real finalizer/policy
fixtures selected on the integration base. Keep expensive service-construction
coverage separate from exhaustive producer/projection fixtures, sharing the same
parity assertion, rather than rebuilding the service for every catalogue entry.

Shared core edits are limited to route scoping: e.g. `set_pipeline` when naming
the canonical schema is expressly a document shape, not an instruction to call
it; freeform mutation-echo behavior is scoped to mutation-capable surfaces;
planner manifest rules are scoped to requests that carry that manifest; the
compaction paragraph says author through the current surface's actual tool,
with preview/diff obligations confined to the applicable saved-state route.
Do not alter D7's reread/cap behavior or D8's diff semantics in the process.

### Candidate lifecycle and inline source instruction

Planner completion is submission of a complete candidate, not proof that an
empty saved draft's `preview_pipeline` or `diff_pipeline` validated that
candidate. Keep both tools in their existing palette. Explain that they inspect
saved state and cannot certify an unsaved proposal. After candidate rejection,
repair the submitted complete document and resubmit through the terminal.
Approval-pending and operational failure remain the existing downstream owner's
outcomes; no new success promise or review/execution bypass is taught.

Teach only the existing singular `source.inline_blob` path for source bytes
available in the request or explicitly requested generated data. Copy verbatim
bytes, retain the current `invented_source` provenance distinction, and do not
invent identifiers. Existing returned blob identifiers may be used through the
currently supported source binding. After rejection, correct and resubmit the
same literal when appropriate; do not instruct the planner to call
`create_blob`. Missing uploaded data remains a named selection/upload question,
not permission to fabricate data. Arbitrary embedded reference tables and
plural blob custody do not acquire a new authoring mechanism from this text.

### Shared hints and catalogue text

Prefer route-neutral **configuration corrections** in catalogue advice:
"Correct the missing node/edge field in the authored document" works on both
surfaces, while "call upsert_node" does not. Preserve code matching, explanation
meaning, precedence, Expected-hint augmentation, attribution, redaction and
withheld-feedback behavior. Do not create server-authored graph arguments or
modify `_duplicate_consumer_repair_suggestions` (D3). Pin that planner feedback
still excludes complete `tool_sequence` repair payloads.

Plugin owners keep their facts. Split mixed hints so route-neutral configuration
facts remain useful everywhere and instructions for web freeform blob creation/
wiring are explicitly labelled **Web freeform Composer only**. Add the supported
planner inline-source alternative to source-generation hints where applicable;
do not suggest inline source custody solves reference-table wiring. Apply source
edits consistently across discovery hints and assistance, then verify list,
schema and assistance results through their real policy projections. No new
plugin interface, response keys, profile projection or cache variant is needed.

## Acceptance and discriminating controls

The implementation brief names the tests. The principal oracle captures the
actual outbound planner provider request and compares all shipped instruction
carriers with that same request's emitted tool palette. Cover both `none` and
`openai_strict`, ordinary primary/repair/hatch requests with the production full
discovery policy, raw/direct and profile-projected plugins, and optional
deployment context. Keep prompt-guard trust classes and redaction intact.

Separately test a **synthetic narrowed-palette boundary injection**. At
`PlannerDiscoveryPolicy.initial`, have a test-only wrapper call the real
initializer and return an otherwise identical owned policy with one valid
discovery name removed, preserving order. Capture the resulting outbound
request and use its actual tool list in the oracle. This is a robustness/mutation
control against hardcoded default-palette assumptions. Production `initial`
selects the complete tuple and `with_manifest` preserves it at this source;
do not change production selection or claim a real narrowed production mode.

The checker is a small **test-only** identifier checker, not an English scope
interpreter or production renderer. Build its complete tool vocabulary from the
canonical declaration emitter and registered web/MCP/planner control emitters,
independently of the available palette captured for the call. Then compare each
recognized tool token against that captured palette. `create_blob` must remain
recognizable when it is absent from the planner tools. Check plain text,
backticks, JSON text leaves and declaration/argument descriptions; report origin
plus offending name. Historical user prompts and echoed user values are not
public instruction sources. Discovery results are instruction carriers only in
declared guidance fields, not arbitrary returned row/file content.

Use a finite reviewed test fixture for legitimate exceptions. Each entry records
`origin` (carrier plus stable source/plugin/field identity), `scope_kind`
(`schema_reference`, `other_route` or `conditional_presence`), exact
`expected_paragraph` text including its full scope/guard, exact surrounding
anchor text, and the specific `tool_names` permitted **inside that one span**.
No exception is inferred from the text being checked or grants a global waiver
for a name. For each encountered origin, require the anchor and expected
paragraph to select exactly one occurrence with the expected bounds. Zero
matches, multiple matches, drifted text, overlapping/ambiguous spans or a moved
guard fail closed and require review. Any second unscoped occurrence outside
the span fails normally. The complete producer/projection corpus must exercise
each fixture entry at least once; a policy-hidden origin's absence in one
particular request does not license silently deleting its corpus coverage.

Concrete permitted forms are: the reviewed paragraph defining `set_pipeline`
as a canonical **document shape**, a complete paragraph explicitly headed
`Web freeform Composer only:`, and a complete discovery sentence explicitly
conditioning its named call on that tool being advertised in **this request**.
Store the final exact paragraphs/anchors in the test fixture before accepting
the implementation; prose resemblance or the presence of words such as
`freeform`/`offered` is insufficient. This is test metadata only: add no public
plugin metadata, wire field, production prose parser or new renderer framework.

Required lasting mutation controls inject an unscoped `create_blob` command
independently into the system overlay, one aid, one raw hint, one projected
hint, and a catalogue fix that reaches candidate feedback. Each must fail the
real parity assertion. An unsupported name in an explicit other-route reference
must be accepted only within its bounded scope; remove/move the scope and it
must fail. Apply the same deletion and displacement controls to the conditional
discovery guard, retaining the tool token outside the valid guarded span. Also
repeat the same unavailable command outside an otherwise valid exception and
require failure for that second occurrence. Stale-anchor and duplicate-paragraph
controls must fail closed before any exemption is granted. Deleting all guidance
cannot pass: separate positive assertions pin
the terminal/candidate-repair path, inline custody instruction and preserved
policy principles from the owner map. In the synthetic narrowed-policy case,
remove an otherwise available discovery tool so a hardcoded default palette
fails; keep ordinary full-policy production-route coverage distinct.

Behavior controls use the real planner with scripted, fail-closed providers:
a complete valid inline-source candidate reaches existing custody/validation;
a rejected candidate is repaired/resubmitted without a blob-writing call;
missing content does not mint a blob or candidate; allowed conversational and
recovery exits retain current meaning. Successful freeform blob binding still
works and its valid instructions remain present. These prove the instructions
describe reachable behavior, not that an unconstrained live model always follows
them. No live-provider, MCP-native-platform or browser claim follows.

## Dependencies and material open decisions

There is no material product decision for the stated planner slice. The exact
placement of shared principles and test helper names is an implementation
choice, reviewed against the preservation inventory above. Implementation waits
for the parent's frozen-tree gate and uses the final integrated source; it must
not edit skills mid-suite. D2 path admission, portable process ownership,
resource identity and inventory pagination are independent lane plans; do not
duplicate or depend on their unimplemented behavior.

If a real defect discovered during inventory requires changing a handler,
public plugin contract or an unresolved D3–D38 policy, stop that expansion and
record the dependency. Do not mask it with an unsupported alternative or mark
whole R8/13.03/13.15 complete. Full MCP/freeform parity, policy rulings and their
separate tests remain open after this bounded slice.
