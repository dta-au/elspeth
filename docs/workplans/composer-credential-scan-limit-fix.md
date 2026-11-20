# Composer credential-scan limit fix

> **Status, 2026-11-20:** Proposal (implementation plan), carried into the repository with 0.8.16 from the 0.8.15 staging hotfix window. Written against `main` at `8e126f994`; line references point at that tree. The design described here was **not implemented**: 0.8.16 instead raises the physical-boundary credential scan ceiling to 2 MiB (`src/elspeth/web/composer/provider_gateway.py`, commit `ddf6cf234`).

Status: proposed implementation only. Planning base: `main` at
`8e126f9943aa6cdca334de2cd8ee39d678428e33`.

The [Astra review](composer-credential-scan-limit-astra-review.md) approves this
revised plan with no remaining design blockers. This revision incorporates its
compatibility, Unicode, projection-work, and end-to-end test requirements.
Implementation and regression validation remain required.

This is a code design and validation plan. Staging records, prompts, telemetry,
and reproductions remain in the portal; none belong in this document or in
repository fixtures. No deployment or account mutation is authorized by this
plan. No release assignment is implied by the planning base.

## Problem and intended behavior

The physical Composer transport calls `require_no_credential_material()` over
its message tree using the generic 256 KiB maximum string size. A graph context
assembled from many individually small fields can exceed that size. The detector
then reports `traversal_limit`, but `CredentialMaterialRefused` presents the same
credential warning used for actual secrets.

Allow ordinary top-level provider message text up to a fixed 1 MiB UTF-8 extent,
scanning every byte with the existing credential detector. Preserve the normal
256 KiB string bound on the remaining message metadata and tool arguments.
Describe processing-limit refusals accurately without weakening credential
detection or changing the existing refusal payload structure.

The 1 MiB value comes from the existing Composer
`JSON_MAX_TOTAL_UTF8_BYTES` constant. Its use here is explicitly a per-message
text scanning ceiling, not a new aggregate request limit or a promise that the
upstream model will accept a request.

## Scope and compatibility

- Change the shared physical provider boundary, covering both normal tool-loop
  and text calls through `_litellm_acompletion()`.
- Keep the generic credential traversal policy, recognized credential patterns,
  field vocabulary, maximum depth, maximum width, and node budget intact.
- Keep the existing `PreparedAdvisorContent` custody checks and complete Advisor
  scan in their existing order. Apply the new bounded text handling to the
  residual message tree, after the Advisor's selected text has been inspected.
  Witnessed Advisor text retains its existing complete-extent allowance even
  beyond 1 MiB; never rescan its original body under the ordinary-text ceiling.
- Keep the original provider request and its provenance intact. Temporary scan
  projections are never provider messages, persisted transcript, or trusted
  prompt evidence.
- Preserve HTTP 422, the exception family, payload keys, error and failure codes,
  `category`, `surface`, and detector version. For `traversal_limit`, change only
  the human-readable detail and matching exception message.
  This prose clarification affects every Web/Composer surface using the shared
  exception, not only physical provider requests. `unsupported_value` wording
  is outside this change.
- Leave actual credential warnings byte-for-byte unchanged. Historical audit
  records retain their original text; there is no rewrite or migration.
- Make no graph, file, transcript, preference, secret-store, gateway-config,
  database-schema, frontend, or timeout change.
- Normal provider calls remain mandatory. No tutorial-specific branch, graph
  synthesis, replay shortcut, or provider bypass is introduced.

## Specific edits and estimates

These are planning estimates, not measured patch sizes.

| File | Edit | Approximate changed lines |
| --- | --- | ---: |
| `src/elspeth/web/composer/provider_gateway.py` | Separate bounded full scans of exact top-level text bodies from the ordinary residual-tree scan | 30–55 |
| `src/elspeth/web/credential_guard.py` | Select an accurate fixed processing-limit detail and use it consistently in the exception and payload | 15–25 |
| `tests/unit/web/composer/test_provider_message_credential_limits.py` | New physical-boundary regressions using existing custody fixtures and synthetic content | 210–330 |
| `tests/unit/web/test_credential_guard.py` | Resource-refusal wording, compatibility, and default-limit regressions | 40–70 |

Expected implementation footprint: two production files and two test files,
roughly 45–80 production lines and 250–400 test lines, plus this plan. The test
matrix is required even if its implementation exceeds these estimates.
Required gate metadata, if any, is additional and must be measured from the
actual patch; it is not assumed to be free of churn.

### 1. Physical provider text admission

In `_litellm_acompletion()`, retain the existing evidence detachment, thinking
handling, and prepared Advisor handling.

For the remaining message tree:

1. Check the admitted residual collection types and their width bounds before
   the new projection loops or copies. Introduce no new coercion of unsupported
   objects. Earlier evidence detachment already copies and normalizes messages;
   this local edit does not make every earlier ingress operation bounded.
2. Create a temporary residual projection in which only an exact string-valued
   top-level `content` is replaced with a fixed scan marker. Retain every other
   field, key, nesting relationship, and message entry.
3. Run the existing generic guard on that projection with its normal limits.
   Keep this guard call directly at the physical boundary so the existing
   credential-boundary ownership inventory remains truthful.
4. Scan each original selected text scalar in full with the same detector and
   a fixed `JSON_MAX_TOTAL_UTF8_BYTES` limit. Never set the limit dynamically to
   an arbitrary caller-provided length. Strictly encode the newly selected body
   as UTF-8 before scanning; the detector's replacement encoding alone is not
   strict validation. Catch `UnicodeEncodeError` and raise
   `ComposerAdmissionRefused("Provider message content contains invalid UTF-8 text.")`
   from `None`, following the existing native-thinking refusal pattern. This
   uses the existing general admission response, not a credential category or
   new payload family. Never expose encoder details. Preserve the existing
   prepared-Advisor encoding failure behavior without modification.
5. Continue into the existing prompt guard and SDK call only after all scans
   succeed. Send the original `evidence.messages`, not the residual projection.

Do not slice text, scan only a prefix, concatenate independently scanned chunks,
skip catalog/system text because it is trusted, or exempt graph-bearing text
from the credential detector.

Leave absent, `None`, and non-string `content` unchanged in the residual tree.
Retain the existing supported list/tuple text-block path and its nested string
limits, as well as rejection of unsupported modalities and objects. Do not add
a blanket string-only content requirement.

Copy only the selected message level; never recursively rebuild or deduplicate
containers. Keep cycle detection and repeated-occurrence node accounting. A
long string also reachable through metadata remains subject to the metadata
limit. When several defects coexist, retaining fail-closed behavior does not
require the same first finding or location.

The existing prompt-guard JSON parts remain independently inspected. This fix
addresses a composite graph message made of bounded parts; it does not grant an
unlimited size allowance to an individual prompt, decoded tool argument, or
prompt-guard document. Exercise that next boundary in the regression tests.

### 2. Honest processing-limit errors

Select a fixed detail when `finding.category == "traversal_limit"`:

> This control content exceeds the credential checker's processing limits.
> Reduce its size or complexity and retry.

All other categories retain `CREDENTIAL_REFUSAL_DETAIL`. Use the selected detail
for both `str(exception)` and `to_payload()["detail"]`. Retain the current
structured category so clients and operators can distinguish processing limits
from credentials without parsing prose.

The message must cover depth, width, node-count, and byte limits honestly. Do
not label every traversal limit as a too-long string, and do not include
user-controlled values, field names, prompt excerpts, or request content.

## Verification plan

Write failing synthetic regressions before modifying production behavior.
Use the existing provider-custody fixtures and invoke the actual gateway.
Mock `litellm.acompletion`, not `_litellm_acompletion`, so the tested request
really passes through both credential admission and the prompt guard. Keep real
`enforce_prompt_guard()` and evidence detachment, with synthetic custody and a
mock guard client returning allow. Assert the complete detached messages,
tools, and original-wire/provenance evidence rather than dispatch count alone.

| Case | Required result |
| --- | --- |
| Real `build_messages()` output with a graph context around 300 KiB, assembled from smaller synthetic fields | One mock SDK call; exact complete message and tail canary preserved |
| Ordinary top-level text at exactly 1,048,576 UTF-8 bytes and one byte beyond it | Bounded valid producer parts let the exact-ceiling case pass real downstream enforcement; oversized text refused before SDK dispatch |
| Multibyte text straddling the byte ceiling | Enforce UTF-8 bytes rather than Python character count |
| Newly selected ordinary text with a lone surrogate | Fixed value-free general admission refusal; no encoder details or SDK call |
| Synthetic recognized secret before and after the old 256 KiB boundary, and near the new ceiling | Refuse before dispatch; no candidate value in public refusal or audit metadata |
| Generic guard given a 300 KiB string outside the physical message-body allowance | Still reports `traversal_limit` |
| Nested ordinary metadata or serialized tool arguments above the generic string ceiling | Still refused; the larger allowance is confined to top-level text bodies |
| Excessive message width, nested width, depth, or node count; unsupported values | Remain fail-closed, including early collection bounds |
| Supported text-block list/tuple content and assistant `None` content with appropriate tool-call context | Existing valid behavior preserved; nested text retains generic limits |
| Unsupported modalities or malformed content objects | Established rejection retained; no permissive conversion |
| Self-referential metadata, cycles through the original message/list, and repeated shared metadata near the node budget | Cycles stay fail-closed; repeated-occurrence accounting retained; no deduplication |
| Long body also aliased into ordinary metadata | Metadata still refused under the generic string ceiling |
| Prepared Advisor body beyond 1 MiB with valid partitions, including a secret near its tail | Clean complete body passes; secret refused; existing witness custody and residual protections remain effective |
| Single oversized untrusted inspection part or genuine user prompt | Existing downstream generic limit still refused |
| Resource refusal through the gateway | HTTP-facing payload retains its category and codes, with accurate detail and matching exception text |
| Actual credential refusal | Existing detail, category, payload shape, and scrubbing remain unchanged |

Run existing Advisor-content, prompt-guard JSON, provider-admission, and pricing/
audit tests as well as the new cases. Confirm the synthetic secret controls fail
when secret scanning is deliberately removed, and the large-message positive
regression fails when the previous body limit is restored.
Retain wrong-role, changed-content, overlapping-position, and forged-witness
Advisor refusals. Do not disable the next boundary to make a size test pass.

Run relevant whole-tree credential-boundary ownership and Composer attribute
inventories. Run Ruff, formatting, mypy, contract alignment, and the applicable
whole-tree gates for the actual inputs changed. Capture keyless trust-tier
findings before and after and compare sets; never hand-edit signatures or add
suppressions to make the gate pass.

Because the physical transport is shared Composer runtime behavior, run the
default full Python suite before merging. First check host load and ownership
of test capacity; run only one broad suite, with one worker as requested for
this host. Use the canonical detached gate with explicit stages, for example:
first prepare a sanitized synthetic-test environment without staging credentials,
database URLs, state paths or live-test selectors. `PLAN_TEST_RUN` must be a new
lane-private directory under the worktree; audit other test/subprocess temporary
paths before execution. Verify that the path is nonempty, owned by this run and
inside the worktree before creating directories. Pytest may remove its basetemp
contents; that path must never contain source, staging state or another run.

```bash
cd "$PLAN_WORKTREE" && \
  mkdir -p "$PLAN_TEST_RUN/logs" "$PLAN_TEST_RUN/tmp"
cd "$PLAN_WORKTREE" && TMPDIR="$PLAN_TEST_RUN/tmp" \
  scripts/full-suite-gate.sh --execute --detach --root "$PLAN_WORKTREE" \
  --log-dir "$PLAN_TEST_RUN/logs" \
  --pytest-args "--basetemp '$PLAN_TEST_RUN/pytest-temp'" \
  --workers 1 --stages ruff,mypy,contracts,lints,pytest
```

Verify both import roots resolve into the worktree. Inspect the gate's recorded
exit codes and frozen-tree result, including each lint-stage result separately
(lints are nonfatal by default in this driver). PostgreSQL contention tests become required
if implementation expands into SQL, persistence, schema, or locks; none are
planned here. A signature finding comparison does not constitute operator
signature clearance.

## Execution and rollout

1. Create a dedicated worktree from the agreed current deployment commit,
   retaining the shared checkout and other worktrees. Recheck these source
   seams if HEAD has moved since the planning base.
2. Implement the regressions and bounded admission change; complete the focused
   checks and required full-suite gate. Review the final diff and measured size.
3. Present the concrete tested patch and rollback procedure before any live
   rollout. Planning does not authorize a service restart or deployment.
4. For a subsequently authorized rollout, retain the exact previous deployable
   application artifact. Exercise one normal Composer turn in an agreed session
   and observe the physical provider call; graph mutation requires separate
   explicit scope. A read-only configuration screenshot is not proof that the
   formerly rejected request now passes admission.
5. Validate application and gateway readiness according to the existing VM
   deployment procedure. Keep all staging validation artifacts in the portal
   or designated backup storage. If application restart is part of the agreed
   procedure, account for the gateway's shared network namespace.
6. If normal calls regress or credential controls fail, restore the previous
   application artifact using the agreed rollback procedure. No database or
   transcript rollback is needed for this patch.

## Risks and stopping conditions

The principal risk is accidentally replacing full inspection with a scan
exemption, or leaking the temporary residual projection into the actual request.
Exact-wire assertions and secrets beyond the old limit are mandatory controls.

Scanning larger strings increases finite work at this boundary. The new ceiling
is fixed, and residual structural limits remain enforced. Observe test runtime
and avoid algorithm changes or new scanner implementations in this fix.

The patch does not solve model context limits, aggregate HTTP body limits,
independently bounded prompt parts, long-running authoring operations, or future
transcript growth. If an end-to-end regression exposes another blocking boundary,
report it and update this plan before describing the defect as fixed.

Stop and reassess scope if implementation requires global traversal-policy
changes, permission exceptions, provider bypasses, data migrations, new
settings, or modifications to persisted history. Those are outside this plan.
