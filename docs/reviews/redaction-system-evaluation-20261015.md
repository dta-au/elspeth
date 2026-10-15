# ELSPETH redaction: systems evaluation

**Verdict:** retain the security boundaries, but replace undifferentiated redaction with explicit projections for each destination. The evidence supports a real correctness problem and significant diagnostic costs. It does **not** establish that redaction as a whole causes more harm than benefit, or justify disabling it globally. The frozen advisor repair follows the right principle: preserve owned identity metadata, filter authored text, and keep the original evidence binding separate.

This is independent feedback for product decisions. During completion, John explicitly authorized incorporating confirmed findings from this and the feedback-accuracy review into 0.8.6, followed by a full local 12-worker green run before the next upload. The root integrator owns that triage, implementation and validation; this report does not turn design options or unmeasured hypotheses into release blockers. No production code, tests, Git state, provider configuration, runner state, or application state was changed by this reviewer. No tests, applications, providers, network requests, or registry imports were executed. The only deliverable written is this review.

## Reviewed boundary and evidence

The source baseline is `.claude/worktrees/release086-email-bugs-20261014`, HEAD `8ab3eae594e05b704a91615cd6ec11b586e17bae`, including the uncommitted frozen repairs present during review. Source references below are relative to that candidate, unless explicitly described as main-checkout receipts. The horizon is current behavior and feasible subsequent changes; no production incident rate, attack prevalence, latency distribution, or monetary loss was measured.

The three production files match the retained `CI386712-CSV-FREEZE.json` values, independently checked with `sha256sum`:

| File | SHA-256 |
|---|---|
| `src/elspeth/web/composer/turn_effects.py` | `6da15114ac1e807d8c205571dd4f87f6381bb041ee3ce9aba22e93506d7b4c70` |
| `src/elspeth/web/composer/advisor_checkpoint.py` | `9db29afb0ba246a93d966aa20db8b536f961c6db027d011bd117ea71ec22a56e` |
| `src/elspeth/web/composer/advisor_context.py` | `9add48bb2edac09caf9988b569c21430c2e36446fb159c5d33f8658d18b99935` |

Freeze digest: `53447bd0eec572b4ad6f0c10f8128538f0b1fdabdb29b5f2a2e10ee814a8a7ce`. Retained main-checkout evidence under `.claude/lanes/release086-email-bugs-20261014/` includes `CI386712-CORPUS-DIAGNOSIS.md`, `CI386712-CORPUS-SOURCE-MEASUREMENT.json`, `CI386712-CSV-IMPLEMENTATION.md`, `CI386712-CSV-FREEZE.json`, and `CI386712-CSV-FREEZE-READBACK.json`. The existing final focused receipt records **exit 0, 31 passed in 23.71s**; the expected-red receipt records **exit 1**. Those are retained local controls, not independently rerun evidence, production frequency, or release qualification.

The systems-thinking and SME protocols informed the method. Current `AGENTS.md`, ADR-046, the redaction policy guide, and security-assurance documentation were inspected. Older assurance documents explicitly describe older source baselines; current source governs this assessment. Prior memory was used as a pointer to diagnostic boundaries and then checked against current source.

## What “redaction” currently comprises

These mechanisms have different goals and should not share a single enable/disable decision:

| Mechanism | What it does | Appropriate truth claim |
|---|---|---|
| Rejection | Refuses recognized credentials or inadmissible structures before a control-plane effect/egress | This input was refused under a finite detector policy; not universal secret detection |
| Substring redaction | Replaces matched credential/PII shapes inside prose | This is a modified text view; neither exact original text nor complete de-identification |
| Omission / value-free projection | Removes bodies, unknown keys, arbitrary scalar values, or option contents | The field existed or had this shape; the withheld value cannot be reconstructed from this view |
| Hash / HMAC fingerprint | Preserves identity or correlation without carrying a raw value | Identity/correlation under a specified domain; not anonymity, completeness, semantic equivalence, or proof of inspection |
| Original evidence | Preserves source bytes, authoring facts, runtime payloads, or raw binding inputs in their intended trust domain | Exactness only for that retained object; not authority to disclose it elsewhere |

## Boundary map

| Boundary / trust domain | Current behavior and evidence | Decision |
|---|---|---|
| User/provider control input → Web/Composer | Versioned credential detector covers field names, token shapes, private keys and credential URLs, with bounded traversal and value-free refusals. Raw/decoded tool calls are guarded; exact references are supported. `contracts/credential_material.py:1–7,124–170,180–254,263–288`; `web/credential_guard.py:39–76,157–196` | **Keep refusal. Replace context-free field-name classification where a typed structural/data-key domain is available.** |
| Secret service → runtime destination | Composer receives secret names/references rather than resolved values. Configuration/header audit uses fingerprints, not raw credentials. `core/security/secret_loader.py:44–47`; `core/config.py:2857–2886`; `plugins/infrastructure/clients/fingerprinting.py:157–226`; `docs/security-assurance/07-secrets-and-key-management.md:399–407` | **Keep references, custody and HMAC fingerprints.** These are high-value controls with clear authority boundaries. |
| Primary Composer → configured provider | Current user text is appended directly; state is serialized after server-metadata/storage-path projection. Physical gateway rejects recognized credentials rather than generally removing PII. `web/composer/prompts.py:396–409,486–520,588–591`; `provider_gateway.py:451–460` | **Keep exact authoring semantics plus credential refusal. Configure provider suitability and content policy explicitly.** Do not claim ordinary PII is universally removed. |
| Composer → advisor/title provider | Advisor prose/request/schema excerpts are PII-scrubbed; pipeline evidence is allowlisted/truncated with withheld markers. Title excerpt is scrubbed before truncation. The frozen publication review preserves owned hashes and scrubs explicit text leaves. `advisor_context.py:147–230,906–983`; `turn_effects.py:427–486`; `sessions/_auto_title.py:164–183` | **Keep minimization for these secondary copies. Replace generic prose treatment of structured evidence with explicit projection; make omission limits part of the verdict.** Local or remote endpoint placement changes disclosure risk, not data ownership. |
| Tool invocation → public/chat audit projection | Open options collapse to shape counts; responses retain approved framing but arbitrary keys/scalars become sentinels; failed argument details become codes/counts. `redaction.py:371–407,460–551,1348–1399,1880–1898,4104–4234`; `audit_storage.py:375–430` | **Keep a narrow public view. Do not make this projection the only source for operator diagnosis or authority.** |
| Tool/provider evidence → private diagnostics | Credential-filtered semantic JSON and bounded prepared provider context are retained separately. Malformed, duplicate-key, out-of-bounds or mismatching wire evidence gets explicit unavailable reasons. Private reads require ownership and an access record; forks exclude private evidence. `diagnostic_evidence.py:322–392,419–503`; `provider_context.py:75–87,113–127,220–251`; `sessions/routes/diagnostics.py:27–69`; `sessions/routes/sessions.py:900` | **Keep and use this existing seam.** Retained ordinary PII is expected here; protect access/retention. It is neither an unfiltered raw store nor proof of physical delivery. |
| Source data / runtime LLM → Landscape and sink | Runtime text audit retains text; image audit omits submitted bytes and refuses recognized echoes. Credential control policy explicitly excludes rows, raw/inline blob bodies at admission, runtime model data, telemetry content and sink output. `contracts/chat_parts.py:155–175,284–329`; `plugins/infrastructure/clients/llm.py:310–328,559–632`; assurance 07:415–421 | **Keep original data/evidence semantics and targeted image safeguards.** Do not silently rewrite business data to make a privacy claim. Excerpt promotion into Composer remains a guarded boundary (`tools/blobs.py:2066–2068`). |
| Domain audit → telemetry backend / logs | Generic telemetry can carry external-call payloads at `full`; OTLP is a closed, bounded non-content projection. Defaults are telemetry disabled and lifecycle granularity. Logging setup formats events; it is not a general DLP filter. Web handlers retain correlation/classes while withholding sensitive error bodies. `telemetry/filtering.py:93–103`; `telemetry/serialization.py:73–107,110–195`; `telemetry/exporters/azure_monitor.py:462–464`; `core/config.py:2038–2044`; `core/logging.py:141–173`; `web/app.py:2474–2518,2620–2629` | **Configure destination/granularity, prefer content-free operational exports, retain call-site discipline.** Composer redaction cannot secure arbitrary log/telemetry content. |
| Audit → export / user error surface | Raw error rows are omitted from Landscape export unless opted in; row hashes and structured error metadata remain. Free-form audit error scrubber substitutes a whole matching string; public failures use fixed framing. `core/landscape/exporter.py:892–928`; `contracts/credential_material.py:360–373,484–507`; `web/composer/llm_response_parsing.py:628–654` | **Keep omission by default and deliberate privileged disclosure. Replace lost free-form diagnostic meaning with owned codes/facts where useful.** A hash cannot recover omitted evidence. |

## Ranked findings

### 1. P1, confirmed and corrected in the frozen candidate: whole-JSON PII substitution corrupted owned identity evidence

The retained measurement shows this exact graph hash:

`b2cd36a434811924267fb6c599ef8a989101a8680ab4cca7465fa23730d7414e`

became:

`b2cd36a<redacted-sensitive:phone>67fb6c599ef8a989101a8680ab4cca7465fa23730d7414e`.

The actual provider-facing in-memory value was altered; this was not just log presentation. The phone pattern has no token boundary (`web/validation.py:129–135`), and the shared helper applies it to any supplied string (`171–192`). The old renderer treated a serialized owned publication payload as prose. The frozen producer now constructs separate raw and provider projections, preserving hashes/UUIDs/counts while explicitly scrubbing candidate/request/changed-field/provider request-id text (`turn_effects.py:427–474`); the renderer nominally admits the owned review and only neutralizes JSON-encoded fences (`advisor_context.py:214–228`; checkpoint `723–726`). Raw binding still hashes original candidate/request/evidence (`turn_effects.py:475–486`).

**Implication:** identity corruption and diagnostic disagreement are established. Unauthorized publication, corrupted persisted graph state, and production prevalence are not established. Keeping the frozen narrow repair is justified; weakening equality assertions or exempting every user-supplied digest-shaped string would be unjustified. A preserved server-derived identity can still correlate sensitive content and needs destination authorization.

**Confidence:** High. **Advice risk:** Low for this narrow design principle; release acceptance remains with the integrator.

### 2. P2, static compatibility risk: credential field heuristics are applied beyond credential-owned domains

`credential_material.py:69–79,147–156` classifies suffixes such as `_key` and `_token`, with explicit exemptions for `data_key` and `alternate_key`. `_find` applies the classifier at every nested mapping key and refuses any nonempty, non-reference value (`191–197,205–229`). Whole-state admission walks the entire `state.to_dict()` (`web/credential_guard.py:207–225`). Private diagnostic projection applies similar classification (`diagnostic_evidence.py:355–391`).

**[Derived, not executed]** An otherwise ordinary nested mapping `{"customer_key":"customer_id"}` is a credential field under this authority, even if it represents a column mapping rather than a secret. Empty values and exact managed references behave differently. Existing tests explicitly demonstrate broad nonempty-field refusal and structural exemptions (`tests/unit/contracts/test_credential_material.py:67–84,136–149`). This is a predictable false-positive class, not a measured production defect count.

**Recommendation:** retain token/private-key/credential-URL checks across untrusted text, but let owned schema paths distinguish credential slots from data-field names, schemas and mapping labels. Do not globally allow `_key` or permit literal values in actual credential fields. Use the existing narrow structural exemptions as evidence that this distinction already matters, not as a reason to grow an unbounded exception list.

**Confidence:** High for the classifier behavior; Moderate for workload impact. **Advice risk:** Medium: misclassifying a true credential slot as structural creates a real leak/refusal bypass. This needs focused positive leakage and negative compatibility controls before change.

### 3. P2, deliberate diagnostic tradeoff: public persistence can erase the evidence needed to explain a failure

All plugin option contents and keys collapse to shape summaries (`redaction.py:1348–1399,1880–1898`). Validated response structure does not preserve arbitrary scalar meaning (`460–499`). Invalid arguments retain class/category and bounded counts rather than details (`371–407`). A type-driven required field over the projection budget collapses the **whole response row**, removing otherwise safe success/version framing (`4199–4206`); the behavior is explicitly specified by `tests/unit/web/composer/test_redaction_projection_budget_per_key.py::test_type_driven_required_key_over_budget_keeps_whole_row_stub`.

That is real information loss, but it protects against unknown secret formats and arbitrary PII inside open plugin options. The public projection is intentionally different from exact in-memory evidence (`audit_storage.py:375–384`), and the current private evidence service materially counters the claim that all diagnostic truth is destroyed (`diagnostic_evidence.py:419–503`; private route `27–69`). State/interpretation records also retain their own authoritative values; the manifest documents that distinction (`redaction.py:1630–1642`).

**Recommendation:** preserve the public/private split. For owned status/error/identity facts, use typed fields that survive independently of large payloads, rather than recovering meaning from redacted prose. Decide explicitly whether a required over-budget payload should withhold only that evidence or the entire framing. Never reinterpret an unavailable projection as successful inspection, replayable arguments, or proof that a constraint was absent. The separate feedback-accuracy review owns deeper retry-context conclusions.

**Confidence:** High for loss semantics; Moderate for support burden. **Advice risk:** Medium: richer public projections can disclose data; changing persisted projections can break readers and integrity comparisons.

### 4. P2, product-policy decision: ordinary PII filtering is selective and can remove semantic distinctions without providing end-to-end PII protection

The primary Composer message builder sends the current user message directly (`prompts.py:591`); the physical gateway applies credential refusal (`provider_gateway.py:451–454`). In contrast, advisor requests and title excerpts replace email/phone/SSN-like substrings (`advisor_context.py:154–203`; `_auto_title.py:175–183`). The interpretation path warns for ordinary email/phone shapes rather than universally rejecting them (`web/validation.py:124–136,195–220`); names/addresses are explicitly outside those patterns (`97–99`). Runtime text audit and configured sinks also preserve data under their own policy.

**[Derived]** Replacing two distinct email addresses with one sentinel removes the distinction if an advisor must judge a request involving those addresses. The frozen publication review preserves identity metadata, but its textual evidence is still modified, despite the surrounding “exact candidate/request” wording (`advisor_context.py:222–223`). That does not make the raw binding wrong; it limits what the advisor can verify from its view. If the same PII was already sent to the primary provider, later advisor scrubbing cannot undo that disclosure. It can still prevent an additional copy to a different advisor endpoint or title surface.

**Recommendation:** define the secondary-provider purpose explicitly: topic/title extraction usually needs no PII; graph/publication review may need distinctions. Use stable scoped aliases or typed facts when distinctions matter, and expose withheld/modified status to the reviewer. If the authorized provider must receive exact PII to perform an explicitly allowed task, make that a deployment/data-classification decision; do not silently change the task. Do not advertise these regexes as universal anonymization or DLP.

**Confidence:** High for asymmetry; Moderate for semantic impact across real tasks. **Advice risk:** Medium to High for making disclosure configurable without a clear destination policy. No new global bypass is recommended.

### 5. P2, evidence gap: the available metrics cannot decide whether net security value exceeds net operational harm

The walker emits dispatch, unknown-tool/key, summarizer-error and projection-budget counters (`redaction_telemetry.py:16–38`). The generic prose redactor simply returns substituted text, with no disposition report or false-positive adjudication (`web/validation.py:171–192`). Advisor-note URL/email counts are a useful narrower counterexample (`advisor_output.py:200–207`). Retained controls establish specific catches and the hash collision; they do not measure leaked credentials prevented in real use, user task damage, support time, or adaptation/workarounds.

**Recommendation:** before a broad policy change, use a bounded synthetic replay corpus and a small reviewed sample of already-retained, access-controlled diagnostics. Record the boundary, rule category, whether owned identity changed, what evidence became unavailable, and whether a legitimate task became unsatisfiable. Keep matched values out of telemetry. Add no large logging campaign or new store by default. A detector match is not evidence that the matched value was actually sensitive.

**Confidence:** High about the inspected instrumentation; Insufficient Data for an overall net-benefit estimate. **Advice risk:** Low for synthetic evaluation; Medium if real diagnostic sampling expands access or retention.

### 6. P3, confirmed workflow mismatch: the policy guide contradicts the current label checker

The guide says aggregate path-count reductions weaken, and equal-count hash changes strengthen; it explicitly labels same-coverage summarizer/editorial changes `policy-strengthen` (`docs/guides/redaction-policy-changes.md:64–75,104–106`). Actual `scripts/cicd/check_redaction_direction.py:93–101` classifies any per-entry decrease **or same nonzero count hash change** as weakening. Aggregate totals are diagnostic (`81–85`). The workflow only performs the direction-label assertion when the snapshot changes (`.github/workflows/composer-redaction-gate.yml:49–75,97–107`).

The adequacy guard admits scalar `str`/`bytes` without a Sensitive marker (`test_adequacy_guard.py:56–102,132–166`), while public response scalar projection separately removes arbitrary strings. Thus structural adequacy is not proof of semantic PII coverage. Snapshot summarizer identity is its fully qualified name, with documented closure-state limitations (`_adequacy_helpers.py:117–124`; `test_adequacy_guard.py:731–799`).

**Recommendation:** update the guide to the implemented conservative rule, and describe labels as review routing rather than semantic security verdicts. Keep registry parity, closed-schema and behavioral leakage controls. Review whether broad rationale prose still earns its cost under ADR-046; no safeguard removal is proposed here. The current checker is stricter than the guide, which is counterevidence against claiming a simple coverage-reduction bypass.

**Confidence:** High. **Advice risk:** Low for accurate documentation; Medium for any future gate simplification. No gate was executed or changed in this review.

## Protections worth keeping

The strongest controls prevent exposure by construction: no-value secret APIs and references; atomic value-free credential refusals; HMAC fingerprints for secret identity; storage-path omission; content-free operational telemetry; default omission of raw error rows in exports; and a separate ownership-checked private diagnostic channel. None depends on claiming that a general regex correctly understands every sensitive value.

The bounded malformed-data behavior is also valuable. Tool wire is decoded before scanning to catch JSON escapes; oversized wire remains the JSON boundary's responsibility (`credential_guard.py:170–204`). Audit scrubber failures return fixed sentinels rather than partial hostile containers (`credential_material.py:360–373,396–427`). Response projection bounds precede model validation (`redaction.py:4195–4206`). Blob discovery scans full decoded content before slicing, avoiding a cut-point bypass (`tools/blobs.py:2066–2068`). Title filtering also precedes truncation. These controls have real leakage consequences if removed, even though their effectiveness in production was not measured here.

Conversely, fences and sentinel neutralization address instruction framing, not secret detection; ordinary hashes support correlation, not anonymity. These protections must retain their own contracts rather than being counted as interchangeable “redaction coverage.”

## Small causal model

The observed local chain is sufficient to explain the confirmed defect:

`owned structured payload → serialize as text → phone-shaped digit match → altered identity → advisor receives inaccurate evidence`.

The retained controlled measurement establishes the match and altered evidence. It does not establish the downstream rate of false verdicts or retries. The narrow repair breaks the serialize-then-scrub link while keeping text filtering.

Two larger feedback loops are **hypotheses**, not incident findings:

1. More indiscriminate filtering → less discriminating evidence → more failed/uncertain authoring attempts → more pressure for exceptions or raw debugging copies → more possible exposure. Retry policies, model quality, truncation and missing schema context could independently produce the same symptoms.
2. More leak discoveries → broader filtering → fewer recognized leaks but more invisible semantic loss → positive safety counters while user-task failures rise. Existing private evidence and per-key projection limits can counteract this loop; it is not inevitable.

No stock-flow simulation is warranted without rates. The relevant accumulating stocks would be unresolved false-positive rules, unreadable retained evidence, and unresolved authoring failures; their units and rates are currently unmeasured. A bounded comparison of typed versus generic projections on identical inputs can discriminate the local mechanism without a prolonged campaign.

## Feasible options

| Option | Expected behavior | Main risks / limits | Recommendation |
|---|---|---|---|
| No further change after the frozen narrow repair | Fixes the confirmed hash collision; leaves broad credential-key heuristics, diagnostic loss and selective PII semantics intact | Future collisions/semantic ambiguity remain possible; no evidence supports a claim of overall optimization | Comparison baseline, not the latest authorized plan: John now requests confirmed findings be integrated into 0.8.6. |
| Narrow subsequent work | Align guide/checker; preserve typed owned status/identity facts; add targeted compatibility/leakage controls and explicit modified/withheld markers | Limited implementation churn, but wire/persisted readers need compatibility review | Preferred next product step, scoped by a concrete failure or decision. |
| Structural projection contract | Separate original evidence, credential admission, public audit, private diagnosis, provider task view and operational telemetry; disposition authored strings and dynamic keys by owned path/domain | Highest maintenance and compatibility cost; an incorrectly trusted field can leak; duplicate implementations can diverge | Worth a focused design only if bounded evaluation confirms recurring cross-boundary harm. Reuse existing carriers and private evidence rather than replacing the system wholesale. |
| Disable all redaction/refusal | Exposes option bodies, credential-bearing errors/URLs, private source locations and extra provider/export/telemetry copies; destroys deliberate no-value boundaries | High security risk; incompatible with product contracts and no demonstrated compensating control | Reject. A local/private provider does not make public audit, exports or logs safe. |

From 0.8.5 onward the project permits compatibility, rather than requiring destructive replacement. Future changes should preserve existing public reader/sentinel and evidence-binding semantics where needed, or explicitly version/refuse incompatible readers. This review authorizes no migration, reset, downgrade, or retroactive rewriting of retained evidence. New projection semantics cannot restore information already omitted.

## Confidence Assessment

**Overall Confidence: Moderate.** The source mechanisms and controlled collision evidence are strong; the net operational/security outcome is not measured.

| Finding | Confidence | Basis |
|---|---|---|
| Confirmed hash collision and current narrow separation | High | Retained controlled measurement, frozen hashes, producer/renderer source |
| Broad credential-key false-positive class | High mechanism; Moderate workload impact | Recursive classifier and existing explicit exemption controls; synthetic example not executed |
| Public diagnostic information loss | High mechanism; Moderate support impact | Closed projection and specified whole-row fallback; private evidence limits counterbalance it |
| Selective ordinary PII treatment | High mechanism; Moderate task harm | Primary/advisor/title paths differ; no measured real-task rate |
| Net value of all redaction | Insufficient Data | No production attack/false-positive/task-outcome sample |
| Guide/checker mismatch | High | Direct comparison of current source and guide |

## Risk Assessment

**Implementation Risk: Medium** for the recommended narrow follow-up; **High** for a global redesign or configurable raw egress. **Reversibility: Moderate** for code/config changes; **Difficult** for information already withheld or disclosed.

| Risk | Severity | Likelihood from available evidence | Mitigation |
|---|---|---|---|
| True credential slot admitted as structural | High | Unmeasured | Positive leakage controls for each changed path; preserve value-free refusal and reference semantics |
| Lossy text changes advisor task meaning | Medium/High | Confirmed identity case; ordinary-text frequency unmeasured | Typed facts/scoped aliases, exact owned identities, explicit withheld/modified evidence |
| Richer diagnostic/public projection exposes PII | High | Unmeasured | Keep private ownership/access logging and destination-specific disclosure; never reuse it as default public chat |
| Persisted projection change breaks readers/bindings | High | Plausible from current recomputed hashes and strict models | Trace consumers; retain/version wire contracts; distinguish raw and projected identity domains |
| More scrubbing work or repeated serialization harms performance | Medium | Unmeasured; no latency claim | Evaluate only changed projection paths with representative bounded inputs |
| Simplification weakens a real safeguard | High | Depends on proposal | Remove only redundancy demonstrated at the same boundary; retain leakage negative controls |

## Information Gaps

1. Actual configured primary/advisor/title endpoints, jurisdiction/classification, local-versus-remote placement and retention terms. Static source cannot determine deployment suitability or incremental disclosure value.
2. A reviewed sample of legitimate task failures and credential refusals, with enough original private evidence to adjudicate true/false positives. Existing test cases cannot establish incident frequency.
3. Operational use of private evidence, export raw-row opt-ins, telemetry exporter/granularity and log destinations. No live configuration, access logs or stores were queried.
4. Production leakage incidents prevented or missed, especially novel encodings and plugin-defined credential slots. The detector explicitly excludes arbitrary encodings/cross-field assembly and large data-plane categories.
5. Whether ordinary PII distinctions are required in actual advisor tasks. A synthetically obvious distinction loss is not a measured workload regression.
6. End-to-end acceptance and current protected CI/release state. These remain the root integrator's evidence, not this review's conclusion.

## Caveats & Required Follow-ups

Before relying on a broad policy change, perform a bounded comparison containing: the exact retained hash collision; real phone/credential positives; owned UUID/hash/count preservation; arbitrary user-supplied digest-shaped text; ordinary data keys ending `_key`/`_token` versus actual credential fields; two distinct PII values that matter to task meaning; malformed/duplicate/budgeted input; and public/private/export separation. Validate the instrument with known positives and negatives and mutate a guard where it decides a gate. Use the actual parser/registry for any coverage claim; none was measured here.

Verify fresh-input versus already-projected-input behavior separately. Several paths intentionally reproject data and recognize stable sentinels (`redaction.py:248–265,328–351`; `audit_storage.py:295–371`), but that does not prove all transformations commute or all combinations are idempotent. Applying prose substitutions to serialized values should be treated as a distinct hazard, not made safe by repeated passes.

Falsification criteria are concrete: if typed projections preserve identities and task distinctions yet do not improve the relevant outcomes, redaction is not the sufficient cause; inspect model behavior, retries, truncation and schema evidence. If a removed filter admits a recognized credential to a previously protected destination, the proposal fails. If private evidence already explains a suspected failure completely, public loss alone does not establish a supportability defect. If a deployment requires no ordinary PII egress, current primary/runtime behavior must be addressed as a deployment/product boundary; relaxing advisor filtering would not solve that requirement.

For the authorized 0.8.6 triage, retain the frozen repair and existing credential/secret boundaries, correct the confirmed guide/checker mismatch, and validate any promoted static compatibility concern before calling it a release defect. The root integrator owns the requested full local gate before upload. Structural redesign, configurable raw egress and workload sampling remain options requiring a concrete product decision, not automatic additions to this release. No separate approval chain or sealed plan package is warranted.

## Integrator disposition for the 0.8.6 candidate

The review above describes its recorded historical source boundary. Subsequent
candidate repairs preserve owned advisor identities (R1), admit validated
FieldMapper data labels through the actual Composer boundaries while scanning
credential strings and authoring metadata (R2), correct the redacted-text
evidence label (the narrow part of R4), and align policy guidance with the
existing label checker (R6). Unknown plugin identities, identity-changing
batches and context-free private patch diagnostics remain conservative.

R3 remains the intentional public/private diagnostic separation described in
the review. The broader PII-policy redesign in R4 and the net-benefit metrics
proposed by R5 remain design options; this release does not establish their
operational value or implement a global scrubber bypass. Regular source review
and finite checks are separate from final local, protected CI, PostgreSQL,
release and deployment qualification.
