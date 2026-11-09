# Composer remediation checklist

Snapshot of the live checklist after the confirmed Page sequence 46 edit. See [pickup notes](RESUME.md) for the paused checkpoint and verification limits.

Working remediation checklist for ELSPETH on `fix/composer-tool-remediation`, agreed in chat on 9 November 2026. Paused at the user's request after integration checkpoint `dd3b3524c`. Ruff, mypy and contracts passed on that frozen tree; the lint scan completed with 5,454 findings and its delta still needs review. Broad branch review and combined behavior, proof and PostgreSQL gates remain pending. Detailed pickup notes are in `docs/architecture/composer-remediation/RESUME.md` on the remediation branch. Shared fixes appear once, with affected tools named; verify each affected route before closing a shared fix.

Audit source: `audit/composer-orchestration-tools-20261109`, assessed against `3b28d537`. Receiving branch was last verified at `5e192342c`. Audit source files live under `docs/architecture/composer-tool-assessment/` in `dta-au/elspeth`. References such as XC-02, R4 and D1 retain the audit identifiers.

Unchecked remediation items remain open. Checked items carry verification evidence or an explicit disposition; a resolved decision does not by itself complete its dependent implementation.

# Tracker maintenance

Use this Page as the working tracker throughout remediation. Update it as we make decisions, implement changes and verify results.

Keep each item ID stable. Leave the checkbox unchecked while an item is open, in progress, blocked, or implemented but awaiting verification. Mark it checked only when verified or explicitly dispositioned as already fixed, not applicable, or deferred by an agreed decision; label that disposition so it is not mistaken for an implemented fix.

Append concise evidence to changed items: status, relevant decision, commit or PR, tests and route coverage, and remaining limitations. Record decision outcomes beside D1–D38 and update their dependent tasks. Preserve source references and historical decisions; do not silently remove scope. Every changed behavior must be pinned by regression tests before closure: observable success and relevant refusal, no-op, retry/replay, and no-write outcomes across each affected route. Record the pre-fix failure or mutation control, the passing run, and independent review; helper-only assertions do not establish end-to-end behavior.

Read the current Page before each update and preserve user edits. Add newly discovered work under the appropriate group with a new stable ID. Keep the activity log current. This is maintenance during our work, not a scheduled background scan.

# Execution order

Baseline reconciliation → approval/context failures and identity/retention defects → crash and integrity handling → graph/review consistency → progress/replay contracts → remaining discovery, guidance and presentation work. Guidance fixes describing settled behavior may proceed alongside early code fixes.

# 1 Establish the current baseline

- [x] **01.01** Re-measure the tools exposed on freeform, planner and MCP routes. **Verified at 6825994b4, 9 November 2026:** live registry 41, canonical 43, freeform loop 45, planner 23, distinct Web 46; MCP producer and shipped tools/list both 32 (26 shared Web tools plus 6 local utilities). Strict/non-strict palette checks and actual MCP allowed/unknown-tool controls passed. Evidence: .claude/lanes/composer-remediation-wave1/recon.md.

- [ ] **01.02** Compare the audited handlers, callers, contracts and tests with our current branch.

- [ ] **01.03** Classify each finding as remaining, changed, already corrected, or unverified.

- [ ] **01.04** Re-run each selected finding’s positive and negative controls before changing its implementation.

- [ ] **01.05** Resolve outstanding browser, PostgreSQL, deployed-service and live-provider reproductions separately from locally demonstrated defects.

- [ ] **01.06** Preserve refuted findings as regression controls where useful; exclude them from the fix queue.

- [ ] **01.07** Define completion evidence for each item: implementation commit, regression test, affected routes, and associated documentation changes.

# 2 Invocation context and approval lifecycle

First priority. These address advertised tools that cannot execute correctly and proposals users cannot successfully accept. R1; GS-01; XC-02/03.

- [ ] **02.01** Declare each tool’s required invocation dependencies explicitly.

- [ ] **02.02** Construct and validate dependencies consistently for immediate execution, approval preview, proposal acceptance and MCP.

- [x] **02.03** Pass secret_wiring_policy into proposal acceptance so authorized wire_secret_ref proposals can succeed. **Verified — 9 November 2026, commit b8921de06:** six real HTTP regression cases pin exact allowlist success, absent policy and each mismatched field, persisted marker, terminal status, unchanged graph on refusal, and terminal accepted replay. The authorized case failed before the fix. Focused authority/static gate: 567 passed, 3 skipped, 1 existing expected failure; final combined check: 783 passed. Astra high approved; integrated on fix/composer-tool-remediation.

- [ ] **02.04** Supply a meaningful freeform diff_pipeline baseline.

- [ ] **02.05** Change the MCP diff baseline only after successful session creation/loading.

- [ ] **02.06** Remove or redefine the planner’s current-state-versus-itself diff.

- [ ] **02.07** Give blob and secret mutations validation-only admission before proposal publication. **In progress — 9 November 2026:** state-only secret/blob markers now use actual validation-only dispatch before staging (local commit ef14c2fc1, Astra high approved). Shared update/delete prerequisites, quota and custody checks are locally committed in cf23f38d8 after Astra high approval; no blob-store effect handler is executed as a preview. Final checks: 271 behavior, 295 error-code/envelope, 145 response-consumer and seven real PostgreSQL contention tests pass; authority module: 298 pass, one existing generic expected failure. Source-derived line bindings were refreshed without changing writer authority or admitting existing unreviewed sites. Combined full backend/PostgreSQL gates and branch integration remain outstanding.

- [ ] **02.08** Handle refused previews according to the agreed policy; do not describe an unchecked or refused proposal as successfully validated. **In progress — 9 November 2026:** ef14c2fc1 returns actual refusal/argument feedback without ordinary proposals, preserves partial-draft repairs, and uses refusal disposition for truthful progress. Astra high approved; final affected run: 112 passed. Store admission (cf23f38d8) and publication races (e3d07d59d) are also locally committed after focused tests and Astra approval. Canonical/planner settlement is locally committed in 14fef5e53 and approved by Astra. Combined backend/PostgreSQL verification and branch integration remain outstanding.

- [ ] **02.09** Handle no-op previews consistently rather than staging proposals that cannot apply. **In progress — 9 November 2026:** ef14c2fc1 detects finalized content-equal explicit-approval changes, preserves original observations, and applies proposal budgets after admission. Real-loop no-op/change, marker, ordering, control-finalization and cap tests pass; Astra high approved. Canonical settlement, planner publication and complete canonical auto-mode handling are locally committed as 14fef5e53 after Astra approval. Final focused run: 714 passed; 58 restored-code boundary checks passed after four deliberate shortcut/order mutations were detected. The review’s missing auto-mode finalization/identity tests are resolved: 97 affected tests pass, and targeted premature-equality/identity-erasure mutations fail as intended. Final cross-task Astra review is approved with no findings. Ruff, mypy and contract gates passed on the frozen revision. The first full backend run exhausted this environment’s memory at 12 workers and was stopped; it does not count as a passing run. The frozen four-worker backend run finished with 71,776 passed, 161 failed, 139 setup errors, 227 skipped and one expected failure; serial PostgreSQL finished with 1,068 passed, one failed and one skipped, with a healthy source freeze. The failure exposed the confirmed cross-dialect Landscape defect tracked in 02.18. All failures/errors are recorded and routed: 81 reviewed-binding failures, six Composer fixture/expectation failures, three real gateway failures, 52 Chroma cache failures, 18 missing-Caddy failures, one missing-sqlite3 failure and 139 Terraform-cache setup errors. The fixture and narrowly scoped binding repair received Astra approval, including inherited endpoint/gateway mismatches confirmed on the base revision. Its progress-test review finding is resolved: 102 affected tests pass, six final cap checks pass, and deliberate false-completion/applied-progress mutations fail as intended. The complete endpoint/gateway follow-up passes 25 tests. The source-analysis run on the earlier candidate has finished: 209 passed and two fixture assertions failed. Both failures are covered by the corrected 102-test run; the later combined production revision still needs its own required proof checks. The reviewed test/evidence repair is locally committed as e192aa135. With a writable Terraform cache and the real sqlite3 CLI, focused verification passed 118 tests; 23 existing Terraform-dependent tests skipped because Terraform is unavailable. Real Chroma verification passed all 171 tests across the five affected modules, using the verified model in a writable cache. All 18 TLS/React acceptance tests passed with the pinned Caddy and Chromium. Each dependency follow-up exited successfully with unchanged tracked source. The 02.18 production repair is locally committed and reviewed. Combined ordinary tests, long-running source proofs and PostgreSQL verification remain required before integration and closure. These focused results do not turn the original combined run into a passing run. All five added lint findings were reviewed individually and accepted as intentional integrity checks or error-handling paths; no suppressions or signatures were changed. Branch integration remains outstanding.

- [ ] **02.10** Convert proposal-staging blob-reference failures into coded tool results instead of uncaught ValueError. **In progress — 9 November 2026:** implementation covers both freeform publishers, promoted canonical proposals and direct/required planner publication. Known unavailable resources return safe feedback; corrupted owned data, original cancellation and post-SQL faults retain distinct outcomes. Scoped checks passed: 202 producer, 44 session-proposal and 301 contract tests. Astra’s same-batch cap coverage finding is resolved: all three publisher cases succeed with a refused creation plus ten valid proposals, and an erroneous counter increment makes all three fail. The restored affected module passes 88 tests. Spec and quality rereview approved; locally committed as e3d07d59d. Combined release gates and branch integration remain outstanding.

- [ ] **02.11** Make accept-time ToolArgumentError a handled refusal and settle the proposal instead of repeatedly returning 500. **In progress — 9 November 2026:** legacy acceptance and frontend refresh are locally committed in 66739807c after Sol 6.1 high implementation and Astra high approval. Evidence: 30 real-route cases plus 63 required-control cases pass; 45 existing acceptance cases and 190 frontend store cases pass; pre-fix failures and an early-equality mutation pin the changed behavior. Canonical settlement, planner publication and complete canonical auto-mode handling are locally committed as 14fef5e53 after Astra approval. Final focused run: 714 passed; 58 restored-code boundary checks passed after four deliberate shortcut/order mutations were detected. The review’s missing auto-mode finalization/identity tests are resolved: 97 affected tests pass, and targeted premature-equality/identity-erasure mutations fail as intended. Final cross-task Astra review is approved with no findings; Ruff, mypy and contract gates passed on the frozen revision. Combined backend/PostgreSQL verification and branch integration remain outstanding.

- [ ] **02.12** Give existing no-op proposals a terminal disposition instead of leaving them pending after repeated 409 responses. **In progress — 9 November 2026:** legacy acceptance and frontend refresh are locally committed in 66739807c after Sol 6.1 high implementation and Astra high approval. Evidence: 30 real-route cases plus 63 required-control cases pass; 45 existing acceptance cases and 190 frontend store cases pass; pre-fix failures and an early-equality mutation pin the changed behavior. Canonical settlement, planner publication and complete canonical auto-mode handling are locally committed as 14fef5e53 after Astra approval. Final focused run: 714 passed; 58 restored-code boundary checks passed after four deliberate shortcut/order mutations were detected. The review’s missing auto-mode finalization/identity tests are resolved: 97 affected tests pass, and targeted premature-equality/identity-erasure mutations fail as intended. Final cross-task Astra review is approved with no findings; Ruff, mypy and contract gates passed on the frozen revision. Combined backend/PostgreSQL verification and branch integration remain outstanding.

- [ ] **02.13** Resolve graph-version staleness semantics for blob-only proposals.

- [ ] **02.14** Align argument-error handling between immediate calls and accept replay.

- [ ] **02.15** Use a single finalized candidate through bulk-authoring admission and approval, avoiding inconsistent repeated candidate construction.

- [ ] **02.16** Define proposal dependencies for multi-step edits so independently staged changes do not silently strand one another.

- [ ] **02.17** Initialize the required worker recovery lifecycle for fresh-process MCP stdio preview. **Newly verified — 9 November 2026, HEAD 5e192342c:** the public elspeth-composer entry initializes successfully and accepts legitimate tool calls, but preview_pipeline returns Tool error: AuditIntegrityError because the shared executor recovery callback is absent. Pytest’s autouse callback setup masks this startup defect. Add real stdio subprocess regression and lifecycle/cleanup controls; preserve required-work ownership rather than installing a no-op recovery bypass. Evidence: d2-mcp-path-map.md public-entry probe. This is separate from D2 path admission and remains open. **User decision — 9 November 2026:** preserve existing platform support with substantive portable process supervision; do not restrict standalone MCP to Linux. Astra completed the portable lifecycle design and three serial implementation briefs covering recovery foundations, supervision and public-entry integration. The first recovery-foundation candidate passed 52 focused tests, including the real watchdog deadline, Uvicorn shutdown and a fresh real-worker child; scoped Ruff, mypy and contracts passed with unchanged tested inputs. Astra identified and verified fixes for completed-task retry races, unbounded abandoned-failure retention, and default logging that could corrupt MCP stdout. Deterministic retry and release tests pass; fresh unconfigured children now report once to stderr with empty stdout, while configured Web JSON logging remains compatible. The final coordinator/logging cohort passed 48 tests. Astra approved the foundations, now locally committed as b4064d270. The private portable supervisor/control candidate passed 78 focused tests, including both actual 30-second grace faults; a subsequent executable exit-status repair passed 44 focused tests. Scoped checks passed. Astra reproduced a startup handoff race where a legitimate shutdown frame arriving with the handshake acknowledgment is rejected or partially discarded. The repair retains decoder state and shutdown intent through the handoff; 92 focused tests passed, including coalesced/fragmented frames and eight actual early-stop subprocess cases. Astra approved the repair, now locally integrated as 8c35a2e2a. The public CLI candidate now passes real fresh-process SDK preview, D2 file-path admission and export controls, actual shutdown/deadline/reaping tests, and embedded-host separation checks. The frozen focused cohort passed 600 tests; two strengthened public controls passed afterward with production unchanged. Astra found one remaining descriptor-lifetime defect: closing Python wrappers left the supervisor’s underlying input/output descriptors open. The repair closes the actual redundant descriptors after child inheritance. A real subprocess regression now observes EOF and loss of the input reader while the supervisor remains alive, with inherited child stdio preserved; 85 bounded tests and the final closure control passed. Astra approved the repair with no new actionable finding. The complete public lifecycle patch is now locally integrated as dc67bdd2f, unchanged during rebase. Combined regression gates remain outstanding; Linux checks do not establish native Windows/macOS/BSD qualification.

- [ ] **02.18** Preserve Landscape schema metadata across PostgreSQL and SQLite initialization. **Newly reproduced — 9 November 2026:** the full PostgreSQL gate exposed missing SQLite audit-store foreign keys after PostgreSQL Landscape creation in the same process. All seven admission tests pass alone; a two-test production sequence reproduces the failure with one pass and one failure. SQLAlchemy leaves temporary creation rules on shared foreign-key metadata. Sessions already protects this mechanism; four Landscape creation paths do not. The established shared-lock and exact snapshot/restoration pattern is implemented across all four creation paths. Thirteen real regressions went from failing to passing; final focused verification passed 190 tests with unchanged source, including real PostgreSQL-to-SQLite, failure-cleanup and concurrent-initialization cases. Scoped Ruff, formatting and mypy passed. Astra high approved with no findings. The repair is locally committed as f9c23c59c on top of the reviewed D1 fixture repair. Schema constraints and stale-store refusal are preserved; existing stores are not migrated or repaired. Combined verification and branch integration remain open.

# 3 Canonical identity pagination and discovery completeness

Prevent false not-found results, missed retention guards and premature planner completion. R2; BOS-01/03; XC-08/13.

- [ ] **03.01** Admit blob IDs into one canonical representation used by lookup, binding, reading, writing, proposal custody and retention checks. **Reviewed and locally integrated — 9 November 2026, commit 05bcab939:** accepted UUID aliases now share lookup, binding, retention, planner facts and proposal custody; successful ID echoes are canonical. Original proposal arguments and exact receipt hashes remain intact, and strict graph-marker grammar is unchanged. Astra passed spec and quality review. Evidence includes 950 affected tests, 97 targeted planner/admission tests, 171 inspection tests, 31 real PostgreSQL cases, and eight discriminating mutation controls. The 1,089-test gate recorded 1,083 passes, three skips, one existing xfail and two baseline locator failures; five reviewed locator-only repairs then passed both failed gates and four negative controls. Three additional lint parse-refusal diagnostics were reviewed and accepted; the raw scan remains nonzero. The patch was unchanged during rebase into the integration tree. Parked-graph identity is also reviewed and integrated at dd3b3524c. Combined gates remain pending; work is paused at the user's request.

- [ ] **03.02** Fix delete/update retention guards to compare canonical IDs. **Reviewed and locally integrated in 05bcab939:** canonical and accepted-alias admission/execution guards are pinned with real persistence, PostgreSQL contention and mutation controls. Evidence is recorded under 03.01; combined gates remain pending.

- [ ] **03.03** Return canonical blob IDs from content and attachment readers. **Reviewed and locally integrated in 05bcab939:** both readers echo authoritative canonical identity, with real read and negative-control coverage. Evidence is recorded under 03.01; combined gates remain pending.

- [ ] **03.04** Canonicalize accepted park IDs consistently. **Reviewed and integrated at checkpoint dd3b3524c:** the owned recovery intent canonicalizes parseable UUID selectors after existing raw type/length checks; non-UUID text and messages remain unchanged. All 42 successful provider route/dialect/spelling cases and 28 real PostgreSQL cases passed. Removing normalization produces 48 intended alias failures while eight canonical controls pass. The affected gate recorded 604 passes and one existing xfail. Astra's requested test-only repair now pins complete public-refusal equality and non-disclosure for foreign versus unknown aliases: 28 cases passed, and the raw-alias leak mutant fails the strengthened oracle. Astra passed the scoped spec and quality review. Combined verification remains pending; work is paused at the user's request.

- [ ] **03.05** Normalize planner information keys so equivalent resource spellings share one identity. **Reviewed and locally integrated in 05bcab939:** actual argument models gate canonical keys, invalid selectors provide no fact, and a scripted planning loop verifies canonical fact closure. Offsets, hashes and distinct resources remain separate. Evidence is recorded under 03.01; combined gates remain pending.

- [ ] **03.06** Add bounded pagination and explicit completeness to list_blobs.

- [ ] **03.07** Add equivalent pagination/completeness to list_composer_blobs, preserving its eligibility rules.

- [ ] **03.08** Update the browser file panel to traverse beyond its current first-page window.

- [ ] **03.09** Use deterministic ordering with an ID tie-breaker.

- [ ] **03.10** Define cursor scope and behavior under creation, deletion and readiness changes.

- [ ] **03.11** Reject malformed, stale or foreign-session cursors safely.

- [ ] **03.12** Keep planner information gaps open when a listing is truncated.

- [ ] **03.13** Prevent invalid discovery arguments from satisfying information requirements.

- [ ] **03.14** Include every result-changing argument in information keys, including inspect_source encoding.

- [ ] **03.15** Distinguish omitted and empty assistance selectors where they have different semantics.

- [ ] **03.16** Provide complete or explicitly bounded parked-graph discovery.

- [ ] **03.17** Update result schemas, redaction allowlists, browser decoders and persisted readers alongside new completeness fields.

# 4 Blob reads custody and error ownership

Fix crashes, inconsistent integrity handling and misleading diagnoses. R3; SG-04; BOS-02/05; XC-05/14.

- [ ] **04.01** Route content reads, attachments, source inspection and preview through a shared fenced snapshot-reading contract.

- [ ] **04.02** Require session, storage-root, operation authority and permitted operation kind before reading.

- [ ] **04.03** Enforce the read-operation allowlist in the shared reader.

- [ ] **04.04** Make inspect_source participate in custody locking and interrupted-update recovery.

- [ ] **04.05** Keep metadata, content hash and bytes bound to the same snapshot.

- [ ] **04.06** Preserve streaming source inspection rather than replacing it with whole-file materialization.

- [ ] **04.07** Classify missing ready bytes consistently across readers, preview and blob-backed source binding.

- [ ] **04.08** Handle ESTALE explicitly rather than allowing an untyped plugin crash.

- [ ] **04.09** Distinguish storage-integrity failures from malformed content, unsupported content and ordinary lifecycle refusals.

- [ ] **04.10** Check size limits before credential inspection and report size failures honestly.

- [ ] **04.11** Share size-limit definitions so attachment and credential-guard limits cannot drift.

- [ ] **04.12** Resolve the content-page budget problem where standing validation consumes the entire response allowance.

- [ ] **04.13** Bound page envelopes after transformations determining their actual serialized size.

- [ ] **04.14** Measure actual I/O before claiming paging or preview work is bounded.

- [ ] **04.15** Keep any verification cache bound to the exact immutable content version and applicable policy.

- [ ] **04.16** Replace hash-mismatch advice asking the model to perform forbidden hash computation with an actionable recovery path.

- [ ] **04.17** Make source inspection’s parser options and disclosure promises match what it actually reads and returns.

# 5 Blob creation mutation and inline references

- [ ] **05.01** Validate blob filenames, including NUL and invalid empty/whitespace cases, before filesystem operations.

- [ ] **05.02** Convert storage-accounting unavailability during create_blob into a coded operational refusal.

- [ ] **05.03** Report the correct quota scope instead of labelling identity-wide limits as session limits.

- [ ] **05.04** Align declared content limits and runtime limits for blob creation and update.

- [ ] **05.05** Refuse update_blob on pending/error blobs before integrity-replacement checks.

- [ ] **05.06** Define update behavior when an interrupted deletion has left a deletion ledger.

- [ ] **05.07** Give create/update/delete calls explicit durable effect evidence even when the graph version does not change.

- [ ] **05.08** Close the fresh freeform retry/replay gap using existing authoritative receipt machinery.

- [ ] **05.09** Define retention or release of blobs created before a proposal is accepted, rejected or becomes stale.

- [ ] **05.10** Define the relationship between REST blob deletion and Composer’s live-reference guard.

- [ ] **05.11** Reject reserved identity keys in inline-reference paths.

- [ ] **05.12** Reject traversal into existing blob-reference markers.

- [ ] **05.13** Validate inline-reference destinations against plugin-owned deferred-value capabilities.

- [ ] **05.14** Refuse unsupported binary MIME content at the appropriate boundary and turn existing invalid markers into structured validation failures.

- [ ] **05.15** Resolve whether latin-1 remains an allowed inline decoding choice.

- [ ] **05.16** Correct wire_blob_inline_ref batch dependencies by deriving the component from its field-path prefix.

- [ ] **05.17** Report replacement of an existing value or marker without exposing its contents.

- [ ] **05.18** Provide useful approval summaries for blob updates, including filename and size/hash change.

- [ ] **05.19** Test cross-session, missing-component, invalid-prefix, repeated-wire and malformed-marker cases.

- [ ] **05.20** Distinguish credential-like stored metadata from ordinary inventory failures so a withheld listing gives useful recovery guidance.

# 6 Plugin catalogs schemas and model discovery

Make discovery authoritative enough to guide a successful edit. R2; SG-05; XC-11/18/19/24.

- [ ] **06.01** Decide whether plugin listings are selection indexes or sufficiently complete configuration summaries; align claims with that choice.

- [ ] **06.02** Correct lossy summary handling of referenced enums.

- [ ] **06.03** Preserve relevant union alternatives.

- [ ] **06.04** Distinguish missing defaults from explicit null.

- [ ] **06.05** Expose accurate field placement and authoring constraints.

- [ ] **06.06** Preserve relevant examples and placeholders without implying unsupported authoring forms.

- [ ] **06.07** Represent conditional credential prerequisites through plugin-owned semantics.

- [ ] **06.08** Restore or retire the unused prohibited mechanism consistently across producers, contracts, skills and advisor consumers.

- [ ] **06.09** Teach the read-only named get_plugin_schema probe for closed availability reasons.

- [ ] **06.10** Make planner schema responses declare their bounded projection or retain promised authority/secret facts.

- [ ] **06.11** Deduplicate repeated planner schema contracts by the appropriate plugin/schema identity.

- [ ] **06.12** Correct schema-tracker cleanup/eviction behavior.

- [ ] **06.13** Reconcile MCP schema descriptions with its actual profile capabilities.

- [ ] **06.14** Carry the requirement code and consumer identity needed for planner failure-time plugin assistance, or explicitly choose another supported channel.

- [ ] **06.15** Preserve issue-specific meaning when projecting assistance through operator profiles.

- [ ] **06.16** Add public assistance projection or fail-closed behavior for unsupported profiled providers.

- [ ] **06.17** Apply the same projection policy to validation-suggestion callers.

- [ ] **06.18** Resolve orphan assistance fact_code guidance and distinguish unknown-code results.

- [ ] **06.19** Keep assistance observations bound to the request’s policy/plugin identity.

- [ ] **06.20** Route OpenRouter model discovery through the catalog authority and return IDs accepted by the corresponding validator.

- [ ] **06.21** Correct Bedrock discovery/validation expectations for supported configuration forms.

- [ ] **06.22** Make model-summary keys round-trip through model filters.

- [ ] **06.23** Report catalog provenance and empty/unavailable catalog outcomes accurately.

- [ ] **06.24** Remove guidance pointing to a model_catalog aid that is never supplied.

# 7 Secret discovery validation and wiring

- [ ] **07.01** Introduce a value-free secret inspection operation returning scope, availability and observed reason coherently.

- [ ] **07.02** Make list and point-validation results use compatible availability semantics.

- [ ] **07.03** Support scoped validation matching the question execution asks.

- [ ] **07.04** Avoid inventing a decryption failure from stale inventory plus a failed boolean lookup.

- [ ] **07.05** Test rotated/deleted references and both user/server scope directions.

- [ ] **07.06** Validate secret names consistently in argument models and schemas.

- [ ] **07.07** Include reference scope in missing-secret diagnostics.

- [ ] **07.08** Distinguish absent references from present-but-unresolvable references.

- [ ] **07.09** Keep availability separate from permission to wire a particular destination.

- [ ] **07.10** Share typed destination descriptors across discovery, wiring, authoring and execution.

- [ ] **07.11** Define an unambiguous encoding for literal dotted keys versus nested option paths.

- [ ] **07.12** Validate credential destinations against plugin-supported fields.

- [ ] **07.13** Correct target-ID round trips between component-prefixed results and wiring arguments.

- [ ] **07.14** Correct omitted-source batch dependencies using the actual single source.

- [ ] **07.15** Report replaced secret references without exposing literal values.

- [ ] **07.16** Show reference name, target and option in the approval summary.

- [ ] **07.17** Correct wiring progress text so mutation is not presented as a read-only check.

- [ ] **07.18** Make whole-batch credential refusals produce the agreed model feedback and credential-specific user guidance.

# 8 Graph validity routing and endpoint reconciliation

Address valid-looking edits that fail at runtime or display a different graph. R5; SG-01/07; XC-15/16.

- [ ] **08.01** Preserve queue scope fields until their owning invariant validates them. **Reviewed local fix — 9 November 2026:** three explicit fields now reach the existing queue guard. The corrected unchanged-source regressions recorded 89 failures with 33 passing controls; the fix passed 122 new behavior tests and 499 affected/contract tests. Each independent field-omission mutant failed as expected. Exact refusal fields, private graph preservation, real MCP readback, explicit-approval admission and legacy proposal acceptance are covered. Ruff, mypy and contracts passed; the keyless finding corpus is unchanged. Astra approved specification and quality. Locally integrated as ec079378e; final combined integration gates remain pending.

- [ ] **08.02** Define admissible fields per node kind and reject or explicitly handle wrong-kind fields consistently.

- [ ] **08.03** Align Stage-1 connection producers with runtime lowering.

- [ ] **08.04** Correct gate on_success handling and other routing fields that currently appear valid but are discarded.

- [ ] **08.05** Apply one staged candidate-admission sequence across upsert, patch, splice and bulk replacement.

- [ ] **08.06** Make candidate validity identify configuration, policy, graph, runtime and review verdicts separately.

- [ ] **08.07** Run edge/scalar reconciliation after every endpoint-changing operation.

- [ ] **08.08** Resolve the literal source endpoint consistently in single- and multi-source graphs.

- [ ] **08.09** Remove or diagnose default-root edges after clear_source.

- [ ] **08.10** Remove edges targeting a deleted sink.

- [ ] **08.11** Stop node deletion from deleting unrelated edges whose endpoint names collide with source/output names.

- [ ] **08.12** Validate endpoint direction so edges from sinks or into sources do not become misleading unknown endpoints.

- [ ] **08.13** Reconcile forward-referenced sink edges when their endpoints are subsequently created or changed.

- [ ] **08.14** Prevent unsupported visual-edge removal from silently changing executable routing.

- [ ] **08.15** Resolve edge removal that writes an unauthored discard destination.

- [ ] **08.16** Distinguish executable relations from visual annotations in contracts and presentation.

- [ ] **08.17** Report residual dangling dependants after deletions without automatically inventing reconnections.

- [ ] **08.18** Show actual routing changes in mutation results and proposal diffs.

# 9 Source output option and metadata editing

- [ ] **09.01** Type-check source/sink path and file values before resolving them.

- [ ] **09.02** Reject NUL-containing source, sink and nested provider paths with typed results.

- [ ] **09.03** Establish consistent MCP authoring-time path policy and provide recovery guidance using tools MCP actually has. **In progress — 9 November 2026:** D2 retains file authoring under the configured data root. Sol has threaded the owned root through all eight path writers and both runtime and compiled-identity resolution. The broader diagnostic cohort passed 1,576 tests with three Python 3.13-only skips; after moving the shared authority into contracts, 579 focused tests passed with unchanged inputs. Ruff, formatting, mypy, contracts and inventory checks passed. The three-rule boundary scan retains the same 205 baseline findings; its four location-derived ID changes were verified, with no semantic additions. Astra found and verified the fix for a legacy-state gap where explicit null source/sink paths became filenames. Loaded-state and pre-existing escape-symlink tests now require refusal before materialization or plugin setup, while missing keys and nullable provider paths retain their intended behavior. The new controls failed before the fix and passed afterward; the final affected cohort passed 950 tests with unchanged inputs. Astra approved the scoped fix, now locally integrated as 1bc91710f. Combined fresh-process acceptance and final integration gates remain open.

- [ ] **09.04** Standardize naming admission or standing-error behavior across sources, outputs and nodes.

- [ ] **09.05** Disclose naming rules on every authoring route.

- [ ] **09.06** Preserve shallow patch semantics: top-level replacement, null deletion, whole replacement of nested values.

- [ ] **09.07** Reject node-level fields in option patches with instructions to use the correct edit tool.

- [ ] **09.08** Reject source-level routing/binding fields in source-option patches with appropriate guidance.

- [ ] **09.09** Give output interpretation_requirements edits a specific refusal naming the supported alternative.

- [ ] **09.10** Name the actual pre-existing component when an unrelated invariant blocks an edit.

- [ ] **09.11** Resolve the invalid-draft policy consistently across splice, patch, metadata and explicit-approval paths.

- [ ] **09.12** Make splice refusals actionable when no new diagnostic entries are returned.

- [ ] **09.13** Compare structured diagnostic identities rather than error-message strings when distinguishing standing and newly introduced failures.

- [ ] **09.14** Correct splice repair suggestions for each refusal path.

- [ ] **09.15** Resolve splice replay identity after required-control insertion.

- [ ] **09.16** Pass component-level facts into sink hints so JSON on_write_failure advice is accurate.

- [ ] **09.17** Attach useful post-call hints to set_output.

- [ ] **09.18** Correct the CSV blob-binding hint that claims an unsupported discard default.

- [ ] **09.19** Give named blob-backed sources an appropriate description route.

- [ ] **09.20** Provide a custody-safe full-document round trip for plural blob sources.

- [ ] **09.21** Align component-scoped state export with the authoring form accepted by upsert_node.

- [ ] **09.22** Avoid unrelated whole-document export failures blocking a safe component read.

- [ ] **09.23** Normalize absolute/relative in-subtree path treatment during source export.

- [ ] **09.24** Make set_pipeline metadata replacement/reset behavior explicit.

- [ ] **09.25** Resolve imported output-level review requirements so YAML imports cannot wedge subsequent edits.

- [ ] **09.26** Resolve metadata length limits and report oversize input as size failure rather than credential material.

# 10 Interpretation reviews and stored evidence

These govern whether accepted evidence remains valid, separately from ordinary graph validation. XC-17/23; SG-06.

- [ ] **10.01** Adopt the new persisted head produced by interpretation opt-out so the turn can settle successfully.

- [ ] **10.02** Bind same-batch interpretation requests to a state containing their actual review site.

- [ ] **10.03** Prevent legacy-placeholder cards from binding to a state that cannot resolve them.

- [ ] **10.04** Decide whether legacy interpretation placeholders remain supported.

- [ ] **10.05** Reconcile authoritative reviews on queue insertion and replacement.

- [ ] **10.06** Prevent a queue edit from retaining resolved reviews with drifted hashes.

- [ ] **10.07** Provide an agreed recovery path for already-wedged review state.

- [ ] **10.08** Apply one policy to corrupt stored approval digests across patch, upsert and bulk replacement.

- [ ] **10.09** Distinguish corrupt-evidence faults from retryable user-correctable refusals.

- [ ] **10.10** Define whether acknowledgements may survive rebinding to a different blob.

- [ ] **10.11** Prevent a retained guarantee stamp from silently losing its supporting resolved row.

- [ ] **10.12** Reconcile direct rebind and clear-then-rebind behavior.

- [ ] **10.13** Decide whether blob_rows can meaningfully carry a source-data-contract review.

- [ ] **10.14** Decide whether an empty requirement list may remove resolved in-graph provenance.

- [ ] **10.15** Choose one authority for which review kinds are surfaced automatically.

- [ ] **10.16** Preserve writer error codes and specific recovery causes instead of collapsing all failures into stale-state errors.

- [ ] **10.17** Report all components changed by review reconciliation.

# 11 Semantic no ops progress effects and audit

R4; SG-03; BOS-04; XC-20/21/25.

- [ ] **11.01** Establish one explicit no-change result contract.

- [ ] **11.02** Apply it to metadata edits.

- [ ] **11.03** Apply it to source replacement and singular/plural blob-source binding.

- [ ] **11.04** Apply it to node and output option patches.

- [ ] **11.05** Apply it to node/edge upserts and full-pipeline replacement.

- [ ] **11.06** Apply it to identical blob updates.

- [ ] **11.07** Apply it to recovery restores equal to the current head.

- [ ] **11.08** Preserve distinctions such as boolean/integer/float changes in semantic comparison.

- [ ] **11.09** Ensure no-ops do not count as graph progress or reset repair accounting.

- [ ] **11.10** Correct set_metadata’s declared effect domain.

- [ ] **11.11** Audit attempted actions independently of whether graph content changed.

- [ ] **11.12** Report review reconciliation and finalization as effects of the triggering call.

- [ ] **11.13** Refresh applied_component or provide explicit finalization disclosure so it describes the committed result.

- [ ] **11.14** Keep discovery failures out of the next mutation’s prior_validation and resolved-error delta.

- [ ] **11.15** Record the actual model-visible envelope on cache hits, including version, validation and affected components. **Reviewed and locally committed — 9 November 2026:** Sol replaced the separate slim cache audit mapping with the same owned, admitted envelope delivered to the model. Seven real producer regressions failed before the fix; 237 focused tests now pass, including current-state replay, Pydantic serialization, historical slim-record readability and malformed-cache controls. Scoped Ruff, formatting, mypy and contracts pass. Astra high approved with no findings. Local commit: 205c56fe3. Combined verification and integration into fix/composer-tool-remediation remain outstanding.

- [ ] **11.16** Make cancellation before/during/after a store effect settle as applied, refused or ambiguous using existing authorities.

- [ ] **11.17** Ensure completed-operation replay performs no new writes.

- [ ] **11.18** Require outstanding approval, review and effect obligations to be satisfied before claiming completion.

- [ ] **11.19** Correct planner recovery-control audit classification. **Triage confirmed — 9 November 2026:** recovery controls are declared planner tools but omitted from the received-response known-tool vocabulary, so successful planner `restore_parked_graph` calls can be categorized as `unknown_tool`. Private facts still retain the exact provider name, call ID and arguments. The bounded next-wave fix will include the existing recovery names in that vocabulary and pin main/hatch, rejected-control, genuine-unknown and persisted-evidence behavior. Preserve existing receipt-stage semantics, private filtering and historical records. This is a classification defect, not evidence of failed recovery or public disclosure; implementation and verification remain open.

# 12 Preview diagnostic codes and repair guidance

GS-02; XC-05/06/07.

- [ ] **12.01** Correct all JSON proof-diagnostic branches to satisfy PreviewResponse; disposition the branch not reached by the audit.

- [ ] **12.02** Add closed error codes to uncoded blob, source and state-read refusals.

- [ ] **12.03** Split error codes that currently collapse failures requiring different repairs.

- [ ] **12.04** Preserve structured runtime-preflight codes instead of uncoded [?:...] messages.

- [ ] **12.05** Standardize MCP rejection envelopes with the supported web contract.

- [ ] **12.06** Stop labelling discovery refusals as rejected mutations.

- [ ] **12.07** Return the promised diagnostic code on exact, regex and fuzzy explanation branches. **Behavior verified — 9 November 2026, commit f598aa43d:** all 168 closed codes tested in exact/lowercase-noisy/uppercase-noisy forms, plus record ownership, first-match precedence, Expected hints, ambiguous prose, unknown and runtime-only controls. Ambiguous prose retains optional-code omission rather than inventing a diagnosis. Pre-fix run: 290 failed, 258 passed; final affected selection: 642 passed. Producer-regenerated fixture changes only the two intended code additions. Astra high approved; final combined check: 783 passed; integrated. **Gate repair verified locally — 9 November 2026, commit ff755b12b:** the first tranche missed conditional-splat support in the envelope checker and two reviewed-forwarder census entries. The checker now reads both branches and rejects planted untaught fields; exact census entries retain closed catalogue provenance and near-miss refusals. Full envelope/authority checks pass, followed by 664 passing census/catalogue/response tests; Astra high approved. Combined backend verification and integration onto the task branch remain outstanding, so this item stays open.

- [ ] **12.08** Make diagnostic code and emitted-message lookup behavior consistent where both are advertised.

- [ ] **12.09** Add catalog coverage or suppress explanation pointers for unsupported codes.

- [ ] **12.10** Carry useful repair pointers into planner feedback where supported.

- [ ] **12.11** Remove instructions to create a node when the requested operation was deletion.

- [ ] **12.12** Correct inline-source comparison advice pointing to a listing unable to show the relevant candidate.

- [ ] **12.13** Correct shared-sink removal and duplicate-producer repair advice.

- [ ] **12.14** Remove executable-verbatim guarantee-stamp patches that the target source cannot legally accept.

- [ ] **12.15** Distinguish unavailable storage, missing credentials, destination denial and remote authentication failure.

- [ ] **12.16** Teach every supported blocking preview code and its valid recovery path.

- [ ] **12.17** Define preview’s treatment of pending approvals and imported/hand-edited MCP blob references.

# 13 Planner and route specific guidance

R8; XC-01/18/24.

- [ ] **13.01** Render planner guidance using the planner’s actual tool palette.

- [ ] **13.02** Remove or explicitly scope instructions to call unavailable mutation tools.

- [ ] **13.03** Scope plugin hints and catalog repair sequences to the receiving route.

- [ ] **13.04** Remove planner completion instructions requiring an unavailable or meaningless preview/diff.

- [ ] **13.05** Teach the supported planner inline-blob authoring path.

- [ ] **13.06** Remove freeform instructions to inspect a planner-only information manifest.

- [ ] **13.07** Correct MCP hints recommending unavailable blob-binding or profile operations.

- [ ] **13.08** Correct the claim that answered discovery rereads are refused.

- [ ] **13.09** Align discovery-cap settings, validation warnings and skills with actual route behavior.

- [ ] **13.10** Correct full-replacement-options-object wording for merge-patch tools.

- [ ] **13.11** Correct listing/schema guidance discouraging a schema read required by admission.

- [ ] **13.12** Correct claims about sink edges and scalar routing.

- [ ] **13.13** Correct review guidance about an exact draft argument versus omission.

- [ ] **13.14** Declare planner response variants and strict required-null argument behavior accurately.

- [ ] **13.15** Add palette-parity tests covering system prompts, aids, plugin hints and repair text.

- [x] **13.16** Tokenize expression-operator parity tests so removing < or > makes them fail. **Verified — 9 November 2026, commit 5e192342c:** exact entry parsing preserves multiword operators and existing AST/constant checks. Six enduring mutation controls remove <, >, /, ==, is not and not in, then call the real parity assertion. Original oracle missed four removals; repaired oracle detects all six. Full discovery module: 109 passed; final combined check: 783 passed. Astra high approved; integrated.

- [ ] **13.17** Update declaration, wire, skill and planner hashes with each intentional contract/text change.

# 14 Advisor recovery and terminal controls

R6; XC-04/12/17.

- [ ] **14.01** Give malformed recovery controls a consistent owned failure category across freeform, planner and malformed-head routes.

- [ ] **14.02** Decide when malformed or mixed terminal batches receive a repair round.

- [ ] **14.03** Resolve planner proposal-terminal behavior when batched with recovery versus discovery calls.

- [ ] **14.04** Remove or reuse the dead missing-source diagnostic branch.

- [ ] **14.05** Make recovery receipts disclose all affected pending proposals.

- [ ] **14.06** Apply one agreed disposition to incremental and full-pipeline proposals after head-changing recovery.

- [ ] **14.07** Ensure safe-checkpoint recovery selects the intended checkpoint rather than duplicating the current state.

- [ ] **14.08** Keep editable recovery, handoff, staged approval and execution readiness distinct.

- [ ] **14.09** Describe park discovery limits and repeated park creation accurately.

- [ ] **14.10** Measure repeated recovery-context preparation before optimizing it.

- [ ] **14.11** Decide whether advisor success should clear every tool’s failure window.

- [ ] **14.12** Declare advisor response keys so meaningful fields are not silently collapsed.

- [ ] **14.13** Resolve budget charging when advisor evidence is unavailable.

- [ ] **14.14** Give oversized abandonment context an actionable result rather than a generic retryable advisor error.

- [ ] **14.15** Replace budget-exhaustion advice referencing a nonexistent cheat sheet.

- [ ] **14.16** Ensure advisor/provider failures cannot count as successful stopping or completion.

- [ ] **14.17** Preserve cancellation evidence and durable replay behavior for async and terminal tools.

# 15 User visible tool text and approval presentation

GS-03; XC-21/22.

- [ ] **15.01** Correct model-listing claims implying credential checking or unsupported availability guarantees.

- [ ] **15.02** Describe get_audit_info as policy information rather than run lineage.

- [ ] **15.03** Correct Web audit-export guidance: the current Web path cannot enable the referenced setting.

- [ ] **15.04** Correct list/blob/secret descriptions to disclose their actual scope and completeness.

- [ ] **15.05** Correct source-inspection claims about raw row content.

- [ ] **15.06** Correct metadata copy advertising unsupported tags.

- [ ] **15.07** Correct preview, grammar, transform-list and diff descriptions.

- [ ] **15.08** Correct progress labels for list_models, advisor requests and secret wiring.

- [ ] **15.09** Show moved producer routing in upsert_edge approvals.

- [ ] **15.10** Show edge removals and dependent effects in node/output deletion approvals.

- [ ] **15.11** Show required controls introduced when accepting blob-source bindings.

- [ ] **15.12** Replace generic summaries for side-effecting tools with concrete actions.

- [ ] **15.13** Show blob replacement and secret destination changes without exposing content or secret values.

- [ ] **15.14** Test settled ribbons, information bubbles, live tool logs and approval cards, not just a tooltip string.

# 16 Shared response contracts and test infrastructure

- [ ] **16.01** Apply discovery-result admission consistently on MCP and web.

- [ ] **16.02** Test malformed producer outputs, including non-finite numbers, rather than assuming valid-payload equality proves parity.

- [ ] **16.03** Validate inventory kind, unique names and disjoint result sets where required.

- [ ] **16.04** Preserve safe readers for existing recorded responses, proposals and operations.

- [ ] **16.05** Fix the real-session Composer fixture’s missing session engine.

- [ ] **16.06** Assert actual blob creation in tests currently only exercising the call path.

- [ ] **16.07** Preserve control_modes in static plugin snapshots so tests exercise accept-time required-control wiring.

- [ ] **16.08** Add route-level tests for immediate execution, approval preview, accept replay and MCP.

- [ ] **16.09** Cover strict/non-strict transports, missing context, denied policy, stale state, no-op, refusal and cancellation.

- [ ] **16.10** Add mutation controls for decisive wiring and parity tests.

- [ ] **16.11** Run affected whole-tree gates alongside focused tests.

- [ ] **16.12** Run the full default suite where shared runtime/graph/contracts warrant it.

- [ ] **16.13** Run the separate serial PostgreSQL selection for persistence, sessions, locks and Landscape changes.

- [ ] **16.14** Complete real-browser acceptance for presentation paths previously covered only by source tracing or jsdom.

- [ ] **16.15** Measure provider calls per transition on rootless and tutorial paths.

- [ ] **16.16** Preserve provider authorship, plugin ownership, custody, fencing and durable replay throughout the changes.

# Decisions

Resolve these before their dependent fixes. Record the chosen behavior and date beside each decision, then update its dependent tasks.

- [x] **D1** Whether refused/no-op/unchecked mutations may be staged, and how acceptance settles them. **Decided by user, 9 November 2026:** Return known refusals and no-ops directly to the model; stage only changes that pass validation-only checks. Existing no-op proposals must receive a terminal outcome. Implementation remains open under group 2. **Test requirements recorded:** real-loop refusal/no-op/changed controls; incremental and both canonical staging publishers; explicit approval, canonical auto-commit and mode downgrade; legacy/canonical terminal acceptance, replay and cancellation; both proposal caps; unchanged progress/validation on no-op; UI refresh; non-writing blob admission including all quota, custody and recovery prerequisites. Preserve ordinary partial-draft repairs and immediate ordinary auto-mutation behavior. These are planned pins, not completed tests; D1 closes only after implementation, Astra review, full regression and PostgreSQL gates.

- [x] **D2** Whether MCP path refusal occurs at admission or preview, and which data root authorizes paths. **Decided by user, 9 November 2026:** Keep file-backed source authoring; use the configured data directory and reject out-of-root paths at admission. Implementation remains open. Pin allowed in-root authoring and immediate refusal of unauthorized paths across the relevant MCP transports.

- [ ] **D3** Whether server-generated duplicate-consumer repair sequences comply with provider-authorship rules.

- [ ] **D4** Whether corrupt approval evidence is a fault or refusal; prohibit inconsistent silent repair.

- [ ] **D5** Whether REST deletion must enforce Composer’s live-reference retention guard.

- [ ] **D6** Restore categorical prohibitions or retire the unused mechanism.

- [ ] **D7** Freeform discovery limits, reread refusal policy and configuration naming.

- [ ] **D8** Remove planner diff or redefine its baseline.

- [ ] **D9** Terminal/recovery batch failure policy and dead missing-source feedback.

- [ ] **D10** The authority for automatically surfaced interpretation-review kinds.

- [ ] **D11** Whether empty requirements may remove resolved in-graph source provenance.

- [ ] **D12** Reject invalid component names immediately or retain standing errors.

- [ ] **D13** Graph-version staleness semantics for blob-only proposals.

- [ ] **D14** Whether Web Composer should support audit export as a product capability.

- [ ] **D15** Recovery’s pending-proposal dispositions and malformed-control repair policy.

- [ ] **D16** ENOENT/ESTALE fault ownership and shared read-operation admission.

- [ ] **D17** Whether blob_rows supports source-data-contract reviews.

- [ ] **D18** Review acknowledgement and supporting-row behavior when rebinding blobs.

- [ ] **D19** Inline encoding/MIME policy, destination prevalidation and staging error handling.

- [ ] **D20** Recover or refuse interrupted-delete ledgers during blob update.

- [ ] **D21** Align auto-commit schema-repair feedback with explicit approval.

- [ ] **D22** Correct test-fixture gaps and establish acceptable probe forms.

- [ ] **D23** Advisor failure-window resets, response shape and evidence-unavailable budget charging.

- [ ] **D24** Schema-contract deduplication and MCP profile capability/description policy.

- [ ] **D25** Diagnostic catalog coverage, pointer suppression and message/code equivalence.

- [ ] **D26** Retain or release planner-created inline blobs after rejected/stale proposals.

- [ ] **D27** Whether content-page envelopes carry full standing validation and how their size is bounded.

- [ ] **D28** Refuse required-control removal or disclose subsequent restoration explicitly.

- [ ] **D29** Queue insertion’s review effects and recovery of already-wedged drafts.

- [ ] **D30** Exclude discovery failures from mutation validation history.

- [ ] **D31** Remove or replace guarantee-stamp advice invalid for non-LLM sources.

- [ ] **D32** Disposition the third JSON proof branch, MCP imported blob references and approval-pending preview semantics.

- [ ] **D33** Retain or reject legacy interpretation placeholders.

- [ ] **D34** Reject or strip imported output review requirements; define metadata limits.

- [ ] **D35** Splice replay identity after server-owned controls are interposed.

- [ ] **D36** Component-scoped authoring projection and source-path normalization.

- [ ] **D37** Planner requirement-code feedback and profiled-provider assistance behavior.

- [ ] **D38** Model feedback and progress copy for whole-batch credential refusals.

# Activity log

- 9 November 2026 — Created the grouped tracker, reconciled the live inventory (01.01), recorded the user’s D1 ruling, and integrated the first three fixes (02.03, 12.07, 13.16) on fix/composer-tool-remediation at 5e192342c. Sol 6.1 high implemented; Astra high approved all three. Final frozen-tree regression: 783 passed. Keyless all-rule lint scan exits 1 both before and after with the same 5,424 findings after checkout-path normalization; all 12 changed fingerprints match inserted line offsets. Scoped Ruff, mypy and contracts pass. Full Python and PostgreSQL suites were not run for this bounded tranche; D1 implementation remains open and will require them. Audit source documents remain preserved locally.
