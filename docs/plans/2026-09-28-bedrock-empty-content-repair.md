# Bedrock empty content repair implementation plan

**Target:** `release/0.8.1`; prepare the gateway and Composer change for canary qualification. This document is a working implementation plan, not a deployment or signing record.

**Scope update (2026-09-28):** The DTA adapter lives in a secure enclave and is unavailable in this checkout. The maintainer selected gateway changes only. Gateway canonicalization now maps exact-empty assistant text with tool calls to `None` before any adapter receives it, which avoids the reported DTA adapter's `content is not None` text-block branch. Tasks concerning the DTA source, image, and canary remain qualification work in that enclave; this checkout cannot establish the adapter's introduction date or a live replay result.

**Goal:** A Composer tool conversation, including a resumed tutorial session, reaches Bedrock Converse with no empty text content blocks; invalid empty text-only input fails before upstream dispatch; a Bedrock validation rejection is reported as a definite nonretryable provider failure rather than a timeout.

**Architecture:** The DTA adapter owns the conversion from the gateway's canonical messages to Bedrock blocks. The gateway canonicalizer represents empty assistant prose beside tool calls as `None` so adapter text-block predicates cannot emit an empty block. ELSPETH owns which persisted history rows become provider messages. The gateway's inbound contract owns rejection of messages that cannot carry meaningful content. Keep stored audit rows and tool identities unchanged. Do not add a tutorial branch or server-authored pipeline structure.

**Stack:** Python, Pydantic gateway contract, DTA Bedrock adapter, LiteLLM Composer client, Bedrock Converse, Docker canary.

**Prerequisites:**

- Obtain the current DTA adapter checkout, including tests and derived-image build files. The reported `ops-local/bedrock-gateway/adapter/src/dta_bedrock_adapter/adapter.py` is absent from this workspace. Do not infer its complete method, test names, version, or image recipe from the diagnostic excerpt.
- Establish the exact adapter revision, installed wheel/image digest, canary gateway revision, and tutorial session/request IDs from live read-only records. Keep account-specific identifiers and raw conversation content out of this tracked document.
- Capture a sanitized failing canonical request and the corresponding adapter-produced Converse request. Record the five empty blocks by parsing `messages[*].content[*]`, plus the Bedrock HTTP status, gateway request ID/code, and physical attempt count. Run the same counter against a known-valid tool request and a known-invalid empty-text fixture before trusting it.
- Verify the canary gateway is on its normal entrypoint and remove diagnostic monkeypatches from the candidate image/build context before qualification. Do not use a runtime bind mount as the fix.
- Read `CONTRIBUTING.md` whole-tree gates and the adapter repository's own `AGENTS.md`/build instructions before edits. Use isolated worktrees and a worktree-local environment; verify `elspeth` and `elspeth_lints` import from the candidate tree.

## Measured starting points and open questions

| Boundary | Evidence in this checkout | Required confirmation in DTA/canary |
| --- | --- | --- |
| In-process tool turn | `src/elspeth/web/composer/tool_batch.py:846-863` carries provider `content` unchanged beside tool calls. | Confirm whether each reported empty block came from these turns, from prior history, or both. |
| Gateway ingress | `gateway/src/elspeth_llm_gateway/core/contract.py:104-139` accepts `assistant content=""` with or without calls and empty tool results; `core/service.py:48-58` preserves it. | Record the actual inbound canonical shape at the canary gateway without logging customer text. |
| Persisted history | `src/elspeth/web/sessions/routes/_helpers.py:1852-1889` replays assistant role/content but not tool-call metadata. `:1654-1702` deliberately returns empty raw prose instead of an operator suffix. | Confirm how many empty historical assistant rows the failed replay includes. |
| Provider projection | `src/elspeth/web/composer/prompts.py:617-626` forwards all history rows. | Compare `send_message` and `recompose` on the same sanitized row sequence. |
| Error and retries | Gateway `core/service.py:222-228` converts adapter error `code` into `GatewayError`; `core/errors.py:75-89` makes `upstream_response_invalid` nonretryable. Composer `provider_errors.py:41-44` does not retry `BadGatewayError`. | Determine which layer made each of the reported repeated physical attempts. The adapter's `ErrorClassification.retryable` value alone does not control the HTTP envelope. |

The current source supports two independent invalid-message paths. In-process tool turns can have `assistant content=""` **with** tool calls. Recompose can receive a persisted empty assistant row **without** tool calls. The latter is a durable stock: each stored empty turn can be replayed on later requests. Repeated retries of an unchanged payload amplify upstream calls without changing that stock. The intervention points are outbound history projection, adapter block construction, and ingress validation; an error-label-only change does not remove either invalid payload.

## Required wire behavior

| Canonical message | Converse request | Gateway disposition |
| --- | --- | --- |
| Assistant `content=None` or `""`, one or more tool calls | One `toolUse` block per call, in order; no `text` block | Accepted |
| Assistant nonempty content and tool calls | Original text block plus all toolUse blocks, in existing order | Accepted |
| Assistant `content=""` or `None`, no calls | No upstream request | `invalid_request`, HTTP 400 at ingress |
| Tool result `content=""` | No upstream request | `invalid_request`, HTTP 400 at ingress |
| User/system `content=""` | No upstream request | `invalid_request`, HTTP 400 at ingress |
| Nonempty text, including whitespace-only text if currently allowed | Preserve bytes; do not `.strip()` or invent filler | Accepted under existing policy |

For a previously persisted empty assistant turn, storage and `_composer_chat_history` retain the row and its empty model prose. `build_messages` omits that **information-free assistant row from the provider request**. Do not replace it with the operator-facing suffix, fabricate text, rewrite the audit row, or replay orphan `role="tool"` rows. Validate the resulting role sequence against the actual DTA adapter and Bedrock canary. The existing prompt already contains adjacent user-role catalog, context, and user messages, so do not introduce message merging without a failing provider proof.

## Task 1: Freeze a sanitized reproduction and identify retry ownership

**Files:** DTA adapter's existing fixture/test locations once inspected; no production edits.

1. Record the failing gateway input and adapter output as sanitized fixtures. Assert the baseline produces exactly the invalid `{"text": ""}` shape for assistant/tool-call messages. Assert the control fixture does not.
2. Correlate one Composer call audit and gateway `request_id` to each Bedrock request. Distinguish LiteLLM internal retry, Composer retry, gateway retry, and distinct user operations. Verify whether `upstream_response_invalid` is the actual gateway code and whether the UI's “timeout” is a true timeout classification or elapsed-time wording.
3. Check the DTA adapter's complete `build_invoke`, tool-result mapping, `classify_error`, tests, and packaging. Record the exact source line and output shape before writing the test.

**Done when:** A failing local test reproduces the empty block and the three-attempt claim has a measured owner or remains explicitly unproven. No upstream credentials are needed for the local fixture.

## Task 2: Correct DTA Converse block construction

**Files:** `ops-local/bedrock-gateway/adapter/src/dta_bedrock_adapter/adapter.py` in the actual adapter checkout; its discovered test module and sanitized fixtures.

1. Add a failing adapter test for `assistant content=""` plus two tool calls. Assert there is no empty text block, both `toolUse` blocks remain in order with unchanged IDs/names/arguments, and the rest of the invoke plan is unchanged. Include `content=None` as a companion case.
2. Add negative controls for mixed nonempty prose plus tool calls, ordinary nonempty assistant/user/system text, and nonempty tool result. These must preserve their prior output bytes.
3. Change only the text-block append predicate for assistant tool turns, after inspecting the real method. It must omit `""` and `None` while retaining all toolUse blocks. Do not use `.strip()` to discard whitespace-only prose and do not synthesize substitute text.
4. Add a test that malformed text-only empty input cannot produce an empty Bedrock `Message.content` array. The gateway ingress task below owns the public 400; the adapter should defensively refuse any canonical request that bypasses ingress rather than silently emitting invalid Bedrock wire.

**Done when:** The previously failing adapter fixture is green, the known-valid fixtures retain their exact block shape, and a structural scan of every emitted `messages[*].content[*].text` finds no `""`. The scanner has a positive control with an intentionally inserted empty block.

## Task 3: Reject nonrepresentable inbound messages before adapter dispatch

**Files:** `gateway/src/elspeth_llm_gateway/core/contract.py`; `gateway/tests/test_contract.py`; `gateway/tests/test_app.py`. Check `gateway/conformance/` for an existing request-validation case before adding one.

1. Add failing `ChatMessage` cases for assistant `""`/`None` without calls, assistant `""` with an empty `tool_calls` list, empty tool result, and empty user/system text. Keep assistant `""` or `None` with nonempty tool calls valid. Preserve nonempty and whitespace-only bytes.
2. Add one HTTP test proving a text-only empty message returns the gateway's safe `invalid_request` HTTP 400 **before** `CompletionService`/adapter/upstream dispatch. This matters because `CompletionService` maps an exception from `adapter.build_invoke` to `internal_error` HTTP 500 (`core/service.py:205-209`).
3. Implement a single owned `ChatMessage` cross-field validator for “nonempty content or assistant tool calls”; keep the existing role/tool-call-ID rules. Do not make a blanket `content=""` rejection that breaks valid assistant tool turns.

**Done when:** Contract and HTTP tests pass; invalid input makes zero upstream calls; no raw upstream text appears in the response. No gateway SDK API-major change is needed for this validation.

## Task 4: Keep persisted empty prose out of Composer provider messages

**Files:** `src/elspeth/web/composer/prompts.py`; `tests/unit/web/composer/test_prompts.py`; focused route/recompose coverage in `tests/unit/web/sessions/test_routes.py` or the closest existing route fixture.

1. Add a failing `build_messages` test with an already-projected `ComposerHistoryMessage` sequence: a user request, two empty assistant entries, a nonempty assistant reply, and a later user request. Assert outbound messages omit both empty entries while preserving the nonempty assistant and authored user content/order. `build_messages` receives role/content history, not `raw_content` or persisted records.
2. Keep `_composer_chat_history`'s existing contract. `tests/unit/web/sessions/test_routes.py::test_augmented_assistant_history_treats_empty_raw_content_as_augmentation` intentionally returns `{"role":"assistant","content":""}` and must stay green: it prevents an operator-only suffix from being attributed to the model. Add a route-history test that projects one empty assistant tool-turn record and one operator-augmented record with `raw_content=""` to empty role/content entries without leaking either tool metadata or the suffix.
3. Filter only exact empty assistant history entries at the provider projection in `build_messages`. Leave the persisted rows, other roles, nonempty prose, and in-process tool protocol untouched. Confirm `send_message` and `recompose` both use this projection.
4. Add a route-level recompose test using persisted rows and a capturing provider/gateway stub. Assert the actual outbound request omits the empty historical assistant messages, includes the new user request once, and preserves successful tool/audit settlement. Test `send_message` on the same history shape as a control.

**Done when:** The persisted history still truthfully reports empty model prose, while neither HTTP route sends an empty text-only assistant message. No tutorial-specific branch is added.

## Task 5: Prove error classification, retry, and audit behavior

**Files:** DTA adapter's discovered error tests; `gateway/tests/test_service.py` or `gateway/tests/test_app.py` if needed; `tests/integration/web/composer/test_composer_against_gateway.py` or the closest existing provider-boundary test.

1. Feed a sanitized Bedrock validation HTTP 400 into `DTABedrockAdapter.classify_error`. Preserve any existing explicit context-length/content-policy classifications. For an unrecognized malformed outbound request, assert a gateway code whose central `RETRYABLE` table is false; `upstream_response_invalid` is the currently available nonretryable code. Do **not** change only `ErrorClassification.retryable`: `CompletionService` reconstructs `GatewayError` from `classification.code` and discards that boolean.
2. Assert the gateway's HTTP status, stable code, `retryable=false`, safe message, request ID, and structured log classification. Keep the Bedrock body out of the client. Separately assert timeout errors still map to timeout and 503 availability remains retryable.
3. Exercise a failed Composer provider call with the actual LiteLLM status class. Assert a terminal call audit and quota settlement for each admitted physical call, a failed progress event, and no mislabeled timeout. If one Composer call produces multiple gateway requests, first prove whether LiteLLM owns the retries; only then pin its retries to zero at the freeform request builder, matching the planner's existing explicit zero-retry ownership (`pipeline_planner.py:1567-1577`).

**Done when:** The classification observed by the client matches the tested gateway code, and the measured physical attempt count matches the call-audit count. An unchanged invalid payload is not retried by any hidden layer.

## Task 6: Verify frozen candidates, build immutable gateway, and qualify canary

**Files:** Candidate source and test files above; DTA adapter's actual image recipe and canary deployment files, once located. Do not add account-specific canary paths to tracked ELSPETH docs.

1. In each isolated candidate tree, run focused tests with explicit process exit codes and lane-private logs. For ELSPETH run `tests/unit/web/composer/test_prompts.py`, affected `tests/unit/web/sessions/test_routes.py` cases, `tests/unit/web/test_sessions_composer_attribute_contracts.py`, `tests/unit/web/composer/test_prompt_cache_layout.py`, and the relevant gateway integration test. For gateway run `gateway/tests/test_contract.py`, `gateway/tests/test_app.py`, its adapter tests, and `gateway/conformance/` against the candidate image. Run Ruff, mypy, and `scripts/check_contracts.py` as applicable; compare the keyless tier lint corpus before/after without claiming signed clearance.
2. Because the change touches shared Composer history and gateway contracts, run the canonical frozen full suite on the ELSPETH candidate before integration. If session persistence/SQL/lock code changes during implementation, also run the serial PostgreSQL testcontainer selection. Do not run broad suites concurrently on the shared host. Verify the tree hash and stage exit codes from `scripts/full-suite-gate.sh`.
3. Independently review the actual diff against the wire behavior table and the Composer invariants. Confirm no diagnostic monkeypatch, raw Bedrock payload logging, runtime source mount, tutorial branch, or unrelated edits are in the candidate.
4. Build the DTA wheel from its exact reviewed revision, verify its SHA-256, build a derived gateway image from a pinned base digest, verify offline adapter identity and image labels, run conformance against the image, then stage the canary deployment using its actual runbook. Keep previous image digest available for rollback. Do not reuse an image whose revision label does not match its build source.
5. Deploy only after the artifact and target are reviewable and deployment is authorized. Verify `/readyz`, normal entrypoint, image digest, and no active diagnostic wrapper. Replay the same tutorial session through the normal API. Capture parsed outbound Converse block types and counts, Bedrock status, gateway IDs, Composer progress, terminal call audits, and quota attempt settlement. Also run a fresh-session tool round trip and a nonempty mixed-text/tool call as controls. If the original session has unknown usage/pending quota evidence, inspect that separately before interpreting a replay refusal as a provider regression.
6. If canary acceptance fails, restore the prior gateway image digest and check readiness. Preserve sanitized request IDs/log correlation and report the remaining failure rather than treating a green rerun as resolution.

**Done when:** The exact canary replay and fresh control both complete without empty Converse text blocks or Bedrock validation 400; the provider outcome and attempt count are accurate; the integrated local branch is frozen-gate verified. Record local merge, image publication, deployment, and signing as separate statuses.

### ELSPETH and gateway verification commands

Set `PLAN_WT` to the absolute path of the candidate worktree and create its own `.venv` with the repository's frozen dependencies before running these. Set `PLAN_LOG_DIR` to a lane-private directory under `/tmp`. The DTA adapter commands must be filled from its actual checkout after Task 1; do not guess its runner.

```bash
cd "$PLAN_WT" && PYTHONPATH="$PLAN_WT/src:$PLAN_WT/elspeth-lints/src" .venv/bin/python -c 'import elspeth, elspeth_lints; print(elspeth.__file__); print(elspeth_lints.__file__)'
cd "$PLAN_WT" && PYTHONPATH="$PLAN_WT/src:$PLAN_WT/elspeth-lints/src" .venv/bin/python -m pytest tests/unit/web/composer/test_prompts.py tests/unit/web/composer/test_prompt_cache_layout.py tests/unit/web/sessions/test_routes.py::test_augmented_assistant_history_treats_empty_raw_content_as_augmentation tests/unit/web/test_sessions_composer_attribute_contracts.py -n 0 > "$PLAN_LOG_DIR/composer-focused.log" 2>&1; PLAN_STATUS=$?; printf 'composer_focused_exit=%s\n' "$PLAN_STATUS"
cd "$PLAN_WT/gateway" && PYTHONPATH="$PLAN_WT/gateway/src" "$PLAN_WT/.venv/bin/python" -m pytest tests/test_contract.py tests/test_app.py tests/test_service.py conformance -n 0 > "$PLAN_LOG_DIR/gateway.log" 2>&1; PLAN_STATUS=$?; printf 'gateway_exit=%s\n' "$PLAN_STATUS"
cd "$PLAN_WT" && scripts/full-suite-gate.sh --execute --detach --log-dir "$PLAN_LOG_DIR/full-gate"
```

Read each completed log and the full gate's `summary.txt`; terminal progress is not a pass. The full gate records the before/after frozen-tree hash. Run the DTA adapter and derived-image conformance checks separately in that adapter's own candidate tree. Before commit/rebase/merge, inspect the staged paths and run `scripts/branch-safety-check.sh` with the matching intent and named base.

## Implementation order and stop conditions

The first code change is the failing adapter fixture, followed by the adapter repair, gateway ingress validation, and Composer outbound history projection. Keep each behavioral change and its tests reviewable. If the DTA adapter checkout or canary build recipe cannot be obtained, complete the ELSPETH/gateway-owned work and report the adapter/deployment prerequisite as unmet; do not invent an adapter patch from the excerpt. If the sanitized replay demonstrates a different source for any empty block, update the hypothesis and tests before changing more code.
