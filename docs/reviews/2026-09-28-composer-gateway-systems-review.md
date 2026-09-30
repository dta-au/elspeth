# Composer and gateway boundary review — 28 September 2026

Scope: read-only inspection of local `release/0.8.1` at `4c13b56d57e4e1a89bab2088d72ea3f108bce907`. This HEAD contains the gateway empty-content repair (`e72551a31`); the shared checkout advanced before this review began. The DTA Bedrock adapter and live canary are in a secure enclave and were unavailable. Findings below concern the in-repository code and explicitly separate local contract facts from unobserved Bedrock outcomes.

## System path and recurring mechanism

Composer builds provider messages from persisted role/content history (`src/elspeth/web/composer/prompts.py:567-593`), appends in-turn assistant tool calls and tool results (`tool_batch.py:845-861`, `2844-2850`), and sends them via LiteLLM (`provider_gateway.py:344-361`). The gateway admits Chat Completions JSON (`gateway/src/elspeth_llm_gateway/core/contract.py:104-139`, `194-286`), converts it to the adapter SDK shape (`core/service.py:48-60`, `89-111`), calls the adapter/upstream (`core/service.py:195-230`), then renders a fixed error envelope (`core/errors.py:41-119`).

The recurring failure mode is **locally valid fragments becoming invalid or misleading at the next boundary**. Per-message validation does not establish a valid conversation; per-request retry audit does not establish one physical provider dispatch; and an upstream validation rejection can become a generic gateway 502. More invalid requests can therefore produce more upstream calls and less precise local evidence. The highest-leverage checks are at the gateway's complete-conversation admission point and the single shared provider-dispatch function, where all relevant context exists.

## Findings

### 1. Advisor call omits the retry controls used by the other Composer calls — high, confirmed configuration gap

`build_advisor_request_options` starts with only `model` and `max_tokens` and never adds `num_retries` or `max_retries` (`src/elspeth/web/composer/advisor_request.py:11-45`). Runtime checkpoint calls pass those options directly to `_litellm_acompletion` (`advisor_checkpoint.py:358-372`), which calls LiteLLM after one quota admission and one dispatch callback (`provider_gateway.py:344-361`). The checkpoint owns a two-attempt outer loop (`advisor_checkpoint.py:1002-1015`) and records one LLM audit row per wrapper call (`472-490`). By contrast, the freeform and planner builders pin **both** retry spellings to zero (`provider_gateway.py:323-340`; `pipeline_planner.py:1360-1371`). The boot probe uses the same advisor builder (`boot_probe.py:230-248`), so probe parity does not expose this difference.

Reproducer: a direct call to `build_advisor_request_options(..., structured_output=True)` yielded `advisor_retry_kwargs {}` with exit 0. If the installed LiteLLM/provider SDK retries an advisor call internally, additional physical dispatches fall inside one quota admission, callback count, and audit row; the actual multiplier was **not** measured here. Pin both spellings in the shared advisor builder and assert them in runtime/probe parity tests; then use a fake transport or gateway request IDs to verify physical dispatch counts under a retryable response. This fixes the rule at the request builder rather than compensating in accounting.

### 2. Gateway admits incoherent tool conversations — high, confirmed admission gap; upstream impact conditional

`ChatMessage` checks role/field relationships and nonempty content (`gateway/src/elspeth_llm_gateway/core/contract.py:104-139`), while `ChatRequest` only checks nonempty message list, tool-choice syntax and named-tool membership (`194-286`). No validator matches a tool result's `tool_call_id` to a preceding assistant call, requires completion of outstanding calls, or rejects duplicate/blank IDs. A direct `ChatRequest.model_validate` probe accepted an orphan result, duplicate call IDs, and empty call ID/name; the valid matched pair also passed as a positive control. `to_canonical_request` retained these states, and the reference adapter emitted an orphan `operation_ref` on the fictional wire (`core/service.py:48-60`; `reference/adapter.py:137-150`). All probes exited 0.

This is **not presently shown as a Composer-produced malformed conversation**: freeform provider tool-call admission rejects blank and duplicate IDs before dispatch (`src/elspeth/web/composer/tool_batch.py:231-299`), the planner rejects both (`pipeline_planner.py:1582-1594`), and Composer serializes actual tool results as nonempty JSON (`discovery_cache.py:49-51`; `tool_batch.py:2827-2849`). The gap affects other gateway clients, corrupted/replayed message lists, or future Composer branches. Specific provider acceptance rules can vary, so a Bedrock 400 for these examples remains a hypothesis. Add a complete-conversation validator at gateway ingress, with valid multi-call and orphan/duplicate/unanswered controls; keep adapter-specific name and ID restrictions in adapter conformance tests.

Related cross-field case: `tool_choice="required"` with no `tools` is accepted (`contract.py:268-286`), and the reference adapter emits `directive_policy.mode="required"` without any `directives` (`reference/adapter.py:223-242`). This is internally contradictory even before a provider-specific wire rule; reject it at request admission.

### 3. Mixed prose and tool-call responses cannot cross the gateway SDK — medium, confirmed shape limitation; operational reach unknown

Composer intentionally retains both assistant prose and tool-call metadata in a turn (`src/elspeth/web/composer/tool_batch.py:845-861`; `service.py:2357-2365`). Gateway `CanonicalResponse` requires exactly one of non-`None` text or nonempty tool calls (`gateway/src/elspeth_llm_gateway/sdk/types.py:123-139`). A direct constructor with `text="I checked"`, one call, and `finish_reason=TOOL_CALLS` raised `ValidationError` (exit 0 of the probe script). The service maps adapter parse exceptions to `upstream_response_invalid` (`core/service.py:215-221`). The fictional reference adapter chooses either invocations or text by `halt` (`reference/adapter.py:257-305`), so its tests do not establish how the enclave adapter handles a real mixed response.

If the upstream returns both, a DTA adapter must currently discard prose, discard calls, or fail. Inspect the enclave adapter and captured upstream response shapes before changing this contract. If mixed responses are supported, permit both in `CanonicalResponse` and test rendering, Composer replay, and audit of both fields. If the deployment prohibits them, make that explicit in adapter conformance and error classification.

### 4. Unknown upstream validation failures lose their category at the gateway — medium, confirmed local behavior; DTA mapping unknown

The adapter classification vocabulary has no upstream-request-validation code (`gateway/src/elspeth_llm_gateway/sdk/protocol.py:34-42`). Non-401/non-429 upstream 4xx reach the adapter (`core/transport.py:25-32`; `core/service.py:224-230`). The reference adapter maps an unknown 400 fault to `upstream_response_invalid`, and the core renders that as HTTP 502 with `retryable=false` (`reference/adapter.py:114-121`, `307-316`; `core/errors.py:41-55`, `75-89`). A direct probe with status 400 and fault kind `validation` printed `upstream_response_invalid 502 False` (exit 0). The gateway ignores `ErrorClassification.retryable` when constructing `GatewayError` (`core/service.py:225-230`); a direct probe with `retryable=True` and `upstream_response_invalid` still rendered `False`.

This reproduces the **class** of the reported misleading 400→502 transformation in the local reference path. It does not prove the secure DTA adapter uses that fallback today. Define a safe, non-retryable upstream-request-rejected category and test adapter mappings for known upstream validation codes; keep upstream bodies out of client messages. Resolve whether `ErrorClassification.retryable` is meaningful or remove it from the SDK contract, since the current service cannot honor it independently of code. A gateway-specific error code lets operators separate malformed wire requests from invalid upstream responses without exposing sensitive bodies.

## Existing controls and limits

- The newly merged gateway fix **does close the exact in-repository empty assistant text boundary**: assistant `content=""` with tool calls becomes canonical `None` (`core/service.py:48-60`), and the reference adapter omits the text field (`reference/adapter.py:137-150`). Empty text-only messages are rejected (`core/contract.py:135-139`). Persisted empty assistant prose is omitted from provider history (`src/elspeth/web/composer/prompts.py:567-578`). The exact DTA `build_invoke` behavior and live Bedrock outcome remain unverified.
- `gateway/src/elspeth_llm_gateway/core/contract.py:3-19` explicitly rejects unsupported request fields including `stream`; the gateway does not expose response streaming. No streaming-specific empty-block path was found in the reviewed gateway surface.
- Gateway transport allows only one explicit 401 replay (`core/transport.py:136-196`); that authentication retry is separate from the advisor's unspecified LiteLLM retry behavior.
- The current `upstream_response_invalid` gateway envelope is non-retryable by its own JSON flag (`core/errors.py:75-89`, `109-119`). An SDK may still use HTTP 502 for its own retry decision; the main Composer and planner builders now pin retries off. No live SDK physical retry count was collected.

## Verification and audit limits

Commands used with explicit repository roots:

- `PYTHONPATH=/home/john/elspeth/src:/home/john/elspeth/elspeth-lints/src .venv/bin/python -m pytest tests/unit/web/composer/test_advisor_request.py -n 0 -q` from repository root: **exit 0, 10 passed**.
- `PYTHONPATH=/home/john/elspeth/gateway/src /home/john/elspeth/.venv/bin/python -m pytest tests/test_contract.py tests/test_service.py -n 0 -q` from `gateway/`: **exit 0, 111 passed**.
- Initial mixed-root test invocation from repository root: **exit 1, 104 passed and 17 async-fixture setup errors** because it read the root pytest config instead of `gateway/pyproject.toml` (`asyncio_mode="auto"`). The two correctly scoped runs above resolve that test invocation issue; no product-code failure was inferred from it.
- Read-only direct Python probes: gateway malformed/valid tool conversations, advisor retry kwargs, mixed response constructor, reference adapter wire projection, and fallback classification all exited 0 with the outputs described above. These establish contract behavior, not live Bedrock acceptance or physical SDK retry counts.

No production code, tests, enclave content, release state, or deployment was changed by this review. The shared checkout's unrelated dirty/untracked files were left untouched.
