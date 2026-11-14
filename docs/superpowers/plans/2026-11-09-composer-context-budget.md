# Composer Context Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Before every Web Composer provider call, shed context in a fixed order until the request fits an operator-set token ceiling and a byte ceiling, or refuse with a recorded reason.

**Architecture:** One pure module decides which sheddable segments to drop from sizes alone. The two send points (compose loop, planner) label their messages as segments, apply the decision to a copy of the request, and record it on the call record. A small reader derives the bytes-per-token ratio and the previously shed set from the session's stored call envelopes, so reduced mode is a function of persisted facts.

**Tech Stack:** Python 3.12, pydantic settings, pytest (`-n 0` for single files), React/TypeScript frontend (vitest).

**Spec:** `docs/superpowers/specs/2026-11-09-composer-context-budget-design.md`. Read it and `docs/development/composer-context-budget-brief.md` (Phase 2 constraints) before any task.

## Global Constraints

- Read `AGENTS.md` and `CONTRIBUTING.md § Whole-tree gates` before writing code. No `getattr` additions, no `# noqa`, no `# type: ignore`, no `git stash`, no `sed`/`awk` multi-line edits.
- The budgeter adds no provider call and writes no content derived from user or model text. The only server-authored strings are the constant stub and integer counts.
- No tutorial-only branch. No per-process flag or cache may decide what is shed.
- The system skill message, catalog message and tool definitions are never altered.
- Exact values: fallback ratio `3.0` bytes per token; `composer_context_shed_above` default `0.85`; `composer_context_shed_to` default `0.60`; `composer_planner_max_request_bytes` default `960 * 1024`; `composer_context_max_prompt_tokens` has no default.
- Shed order: `tool_result`, `assistant_prose`, `hatch_event`, `user_request`; oldest first within each.
- Policy hash schema string: `composer.context-budget.v1`.
- Stub text, exact: `[Result omitted by ELSPETH to fit the context budget. Call the tool again if you need it.]`
- Tests: run one file with `.venv/bin/python -m pytest <path> -n 0 > <lane-private log> 2>&1; echo exit=$?` and read the exit code. Never pipe a test run.
- Before each commit: `git status --short`, then `scripts/branch-safety-check.sh --intent commit`. Commit on `claude/lucid-lamport-wank6h`.
- Never hand-edit a `judge_metadata_signature`. Compare the keyless lint corpus before and after (Task 9).
- The container this plan was written in had no `.venv`. Create one per `CONTRIBUTING.md` before Task 1; if that is not possible, stop and report it.

## Review Focus

1. **Legacy session with no usable ratio** (prior call records lack `request_byte_split`, or `prompt_tokens` is `None` or `0`): expect the `3.0` fallback, no crash, no division by zero. Test in Task 4.
2. **Previously shed keys that no longer exist** (forked or edited session): expect them to be ignored. Test in Task 1.
3. **A tool result smaller than the stub**: expect it never to be stubbed (no negative saving). Test in Task 5.
4. **Parallel tool calls in the latest round trip**: expect every result of the last assistant tool-call message to be protected, not just the last one. Test in Task 5.
5. **A single oversized current user message**: expect a refusal naming `current_message` as the largest component, not an attempt to shed it. Test in Task 5 and Task 6.

## File Structure

| File | Responsibility |
|---|---|
| `src/elspeth/web/composer/context_budget.py` (create) | Pure decision: policy, segments, decision, refusal. No I/O, no message shapes. |
| `src/elspeth/web/composer/context_budget_history.py` (create) | Derive ratio and previously shed keys from stored session records. |
| `src/elspeth/web/composer/context_budget_apply.py` (create) | Shared helpers both call sites use: measure, stub a tool result, name the largest component. |
| `src/elspeth/contracts/composer_llm_audit.py` (modify) | `ComposerContextBudgetRecord` and the `context_budget` field on `ComposerLLMCall`. |
| `src/elspeth/web/config.py`, `src/elspeth/web/composer/protocol.py` (modify) | Three new settings, one changed default. |
| `src/elspeth/web/composer/service.py`, `prompts.py` (modify) | Compose-loop call site. |
| `src/elspeth/web/composer/pipeline_planner.py`, `planning_application.py`, `advisor_transcript.py` (modify) | Planner call site, conversation context omissions, hatch transcript. |
| `src/elspeth/web/sessions/composer_turn.py`, `sessions/routes/_helpers.py` (modify) | Thread history in; map refusals to a failure code. |
| Frontend `components/chat/ComposerOperationOutcomeNotice.tsx`, `config/composer.ts`, `types/index.ts`, `api/composerOperationDecoder.ts` (modify) | Refusal message; progress reason. |

---

### Task 1: Pure decision module

**Files:**
- Create: `src/elspeth/web/composer/context_budget.py`
- Test: `tests/unit/web/composer/test_context_budget.py`

**Interfaces:**
- Produces:

```python
CONTEXT_BUDGET_FALLBACK_BYTES_PER_TOKEN: Final[float] = 3.0
CONTEXT_BUDGET_TOOL_RESULT_STUB: Final[str] = "[Result omitted by ELSPETH to fit the context budget. Call the tool again if you need it.]"

class SheddableKind(StrEnum):          # definition order IS the shed order
    TOOL_RESULT = "tool_result"
    ASSISTANT_PROSE = "assistant_prose"
    HATCH_EVENT = "hatch_event"
    USER_REQUEST = "user_request"

@dataclass(frozen=True, slots=True)
class ContextBudgetPolicy:
    max_prompt_tokens: int             # > 0
    max_request_bytes: int             # > 0
    shed_above: float                  # 0 < shed_to < shed_above <= 1
    shed_to: float
    fallback_bytes_per_token: float = CONTEXT_BUDGET_FALLBACK_BYTES_PER_TOKEN
    @property
    def audit_hash(self) -> str: ...   # stable_hash({"schema": "composer.context-budget.v1", ...all five, floats as repr strings})

@dataclass(frozen=True, slots=True)
class SheddableSegment:
    kind: SheddableKind
    key: str                           # "history:<int>" | "tool:<tool_call_id>" | "hatch:<int>"
    saving_bytes: int                  # > 0; request bytes saved if shed

@dataclass(frozen=True, slots=True)
class ContextBudgetDecision:
    policy_hash: str
    bytes_per_token: float
    request_bytes_before: int
    estimated_tokens_before: int
    request_bytes_after: int
    estimated_tokens_after: int
    shed_keys: tuple[str, ...]         # in shed order

class ContextBudgetRefusal(Exception):
    ceiling: Literal["tokens", "bytes"]
    estimated_tokens: int
    request_bytes: int

def decide_context_budget(
    *,
    policy: ContextBudgetPolicy,
    request_bytes: int,
    segments: Sequence[SheddableSegment],
    bytes_per_token: float | None,
    previously_shed: frozenset[str],
) -> ContextBudgetDecision: ...
```

- [ ] **Step 1: Write the failing tests.** Use a helper `_policy(cap=1000, max_bytes=10**9, above=0.85, to=0.60)` and `ratio=1.0` so bytes equal tokens. Tests and their assertions:

```python
def test_below_high_mark_sheds_nothing():
    d = decide_context_budget(policy=_policy(), request_bytes=850, segments=_segs(), bytes_per_token=1.0, previously_shed=frozenset())
    assert d.shed_keys == () and d.estimated_tokens_after == 850

def test_crossing_high_mark_sheds_in_kind_order_oldest_first_to_low_mark():
    segs = [_seg(USER_REQUEST, "history:0", 100), _seg(TOOL_RESULT, "tool:a", 100), _seg(TOOL_RESULT, "tool:b", 100),
            _seg(ASSISTANT_PROSE, "history:1", 100), _seg(HATCH_EVENT, "hatch:0", 100)]
    d = decide_context_budget(policy=_policy(), request_bytes=900, segments=segs, bytes_per_token=1.0, previously_shed=frozenset())
    assert d.shed_keys == ("tool:a", "tool:b", "history:1")      # 900 -> 600, stops at <= 600
    assert d.estimated_tokens_after == 600

def test_byte_ceiling_overflow_sheds_even_under_token_high_mark():
    d = decide_context_budget(policy=_policy(cap=10**9, max_bytes=500), request_bytes=560,
                              segments=[_seg(TOOL_RESULT, "tool:a", 100)], bytes_per_token=1.0, previously_shed=frozenset())
    assert d.shed_keys == ("tool:a",) and d.request_bytes_after == 460

def test_previously_shed_stays_shed_between_marks():
    d = decide_context_budget(policy=_policy(), request_bytes=700, segments=[_seg(ASSISTANT_PROSE, "history:1", 50)],
                              bytes_per_token=1.0, previously_shed=frozenset({"history:1"}))
    assert d.shed_keys == ("history:1",)

def test_previously_shed_is_released_at_or_below_low_mark():
    d = decide_context_budget(policy=_policy(), request_bytes=600, segments=[_seg(ASSISTANT_PROSE, "history:1", 50)],
                              bytes_per_token=1.0, previously_shed=frozenset({"history:1"}))
    assert d.shed_keys == ()

def test_unknown_previously_shed_keys_are_ignored():           # Review Focus 2
    d = decide_context_budget(policy=_policy(), request_bytes=700, segments=[], bytes_per_token=1.0,
                              previously_shed=frozenset({"history:99", "tool:gone"}))
    assert d.shed_keys == ()

def test_everything_shed_but_under_ceiling_is_allowed():
    d = decide_context_budget(policy=_policy(), request_bytes=990, segments=[_seg(TOOL_RESULT, "tool:a", 100)],
                              bytes_per_token=1.0, previously_shed=frozenset())
    assert d.shed_keys == ("tool:a",) and d.estimated_tokens_after == 890   # above low mark, under cap: allowed

def test_protected_overflow_refuses_naming_the_ceiling():
    with pytest.raises(ContextBudgetRefusal) as e:
        decide_context_budget(policy=_policy(), request_bytes=1200, segments=[_seg(TOOL_RESULT, "tool:a", 100)],
                              bytes_per_token=1.0, previously_shed=frozenset())
    assert e.value.ceiling == "tokens" and e.value.estimated_tokens == 1100
    with pytest.raises(ContextBudgetRefusal) as e:
        decide_context_budget(policy=_policy(cap=10**9, max_bytes=500), request_bytes=700, segments=[], bytes_per_token=1.0, previously_shed=frozenset())
    assert e.value.ceiling == "bytes"

def test_missing_ratio_uses_fallback_and_rounds_tokens_up():
    d = decide_context_budget(policy=_policy(cap=10**6), request_bytes=10, segments=[], bytes_per_token=None, previously_shed=frozenset())
    assert d.bytes_per_token == 3.0 and d.estimated_tokens_before == 4

def test_same_input_same_decision_and_policy_hash_changes_with_each_parameter(): ...
def test_policy_rejects_marks_out_of_order_and_non_positive_ceilings(): ...   # ValueError for shed_to >= shed_above, shed_above > 1, cap <= 0
def test_segment_rejects_non_positive_saving(): ...
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/unit/web/composer/test_context_budget.py -n 0`. Expected: collection error, module not found.

- [ ] **Step 3: Implement the module.** The algorithm, which the signature does not determine:

```text
ratio  = bytes_per_token if bytes_per_token is not None and bytes_per_token > 0 else policy.fallback_bytes_per_token
tokens(b) = ceil(b / ratio)
high   = policy.shed_above * policy.max_prompt_tokens
low    = policy.shed_to    * policy.max_prompt_tokens
sticky = segments whose key is in previously_shed (input order)
over   = tokens(request_bytes) > high or request_bytes > policy.max_request_bytes

if not over:
    shed = sticky if tokens(request_bytes) > low else ()
else:
    queue = sticky, then remaining segments sorted by (SheddableKind definition order, input position)
    shed  = take from queue until tokens(after) <= low and after <= policy.max_request_bytes, or queue is empty
after = request_bytes - sum(saving of shed)
if tokens(after) > policy.max_prompt_tokens: raise ContextBudgetRefusal(ceiling="tokens", ...)
if after > policy.max_request_bytes:         raise ContextBudgetRefusal(ceiling="bytes", ...)
```

`shed_keys` lists keys in the order taken. The module imports nothing from `web/composer` except `stable_hash`'s home module.

- [ ] **Step 4: Run** the same command. Expected: exit 0.
- [ ] **Step 5: Commit** `feat(composer): pure context budget decision`.

---

### Task 2: Call record field

**Files:**
- Modify: `src/elspeth/contracts/composer_llm_audit.py` (near `ComposerLLMRequestByteSplit` :97 and `ComposerLLMCall` :173, `to_dict` :422)
- Modify: `src/elspeth/web/composer/llm_response_parsing.py` (`build_llm_call_record` :706)
- Modify: `src/elspeth/web/composer/audit.py` (`_LLM_CALL_PUBLIC_AUDIT_FIELDS` :435)
- Test: `tests/unit/web/composer/test_llm_context_budget_audit.py` (create; model it on `test_llm_request_byte_split_audit.py`)

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True, slots=True)
class ComposerContextBudgetRecord:
    policy_hash: str
    bytes_per_token: str                       # repr of the float used
    estimated_tokens_before: int
    estimated_tokens_after: int
    shed_history_indices: tuple[int, ...]      # sorted ascending
    stubbed_tool_call_ids: tuple[str, ...]     # in request order
    shed_hatch_event_indices: tuple[int, ...]  # sorted ascending
    def to_dict(self) -> dict[str, Any]: ...   # tuples as lists

    @classmethod
    def from_shed_keys(cls, *, policy_hash, bytes_per_token: float, estimated_tokens_before, estimated_tokens_after, shed_keys: Sequence[str]) -> Self
```

- `ComposerLLMCall.context_budget: ComposerContextBudgetRecord | None = None`, placed directly after `request_byte_split`. `to_dict()` emits `"context_budget"` as the record's dict or `None`.
- `build_llm_call_record(..., context_budget: ComposerContextBudgetRecord | None = None)`.
- `"context_budget"` appended to `_LLM_CALL_PUBLIC_AUDIT_FIELDS` after `"request_byte_split"`.

`ComposerContextBudgetRecord` lives in `contracts/`, so it must not import from `web/`; `from_shed_keys` parses the `history:` / `tool:` / `hatch:` prefixes itself and raises `ValueError` on any other prefix.

- [ ] **Step 1: Write failing tests:** `from_shed_keys(shed_keys=("tool:b", "history:3", "hatch:1", "history:1"))` yields `shed_history_indices == (1, 3)`, `stubbed_tool_call_ids == ("b",)`, `shed_hatch_event_indices == (1,)`; an unknown prefix raises `ValueError`; a call built with a record exposes it in the public audit payload under `"context_budget"` with empty lists when nothing was shed; a call built without one exposes `None`.
- [ ] **Step 2: Run** the new file. Expected: fail on import.
- [ ] **Step 3: Implement.** Validate types in `__post_init__` in the same exact-type style as `ComposerLLMRequestByteSplit`.
- [ ] **Step 4: Update the pinned field-set tests** the change breaks: `tests/property/web/composer/test_compose_loop_invariants.py:957`, `tests/unit/web/composer/test_llm_request_byte_split_audit.py`, `test_llm_tool_contract_audit.py:104-108`, `test_llm_provider_served_audit.py:186-192`, `test_llm_finish_reason_audit.py`. Then check the strict envelope readers still pass unchanged: `provider_telemetry.py:163-190`, `quota_authority.py:480-505`, `advisor_transcript.py:238-269`.
- [ ] **Step 5: Run** `tests/unit/web/composer/ -k "llm_ and audit"` and the property file. Expected: exit 0.
- [ ] **Step 6: Commit** `feat(composer): record the context budget decision on each call`.

---

### Task 3: Settings

**Files:**
- Modify: `src/elspeth/web/config.py` (:369-381 fields; validators near :1286)
- Modify: `src/elspeth/web/composer/protocol.py` (`ComposerSettings` :1606)
- Modify: every deploy file, doc and test constructor listed below
- Test: `tests/unit/web/test_config.py`

**Interfaces:**
- Produces on `WebSettings` and as read-only properties on `ComposerSettings`:

```python
composer_context_max_prompt_tokens: int = Field(..., strict=True, ge=1)
composer_context_shed_above: float = Field(default=0.85, strict=True, gt=0, le=1)
composer_context_shed_to: float = Field(default=0.60, strict=True, gt=0, lt=1)
composer_planner_max_request_bytes: int = Field(default=960 * 1024, strict=True, ge=1)
```

- A `@model_validator(mode="after")` named `_validate_composer_context_shed_marks` raising `ValueError("composer_context_shed_to must be below composer_context_shed_above")`.
- Env names: `ELSPETH_WEB__COMPOSER_CONTEXT_MAX_PROMPT_TOKENS`, `..._SHED_ABOVE`, `..._SHED_TO`.

- [ ] **Step 1: Write failing tests** in `test_config.py`: settings without the token cap fail validation naming the field; `shed_to >= shed_above` fails; defaults are `0.85`, `0.60` and `983040`; the env loader accepts the three new names.
- [ ] **Step 2: Run** `tests/unit/web/test_config.py -n 0`. Expected: the new tests fail.
- [ ] **Step 3: Implement** the fields, validator and protocol properties.
- [ ] **Step 4: Add the required setting everywhere `composer_max_composition_turns` is set.** There is no central fixture; it appears about 287 times in about 170 test files. Use the value `200_000` in tests. Find the sites with Grep for `composer_max_composition_turns` and edit each with the Edit tool. Known hubs: `tests/unit/web/conftest.py:193,207`, `tests/integration/web/conftest.py:169,264`, `tests/helpers/composer_operations.py:36`, `tests/helpers/composer_pg_http.py:51`, `tests/unit/web/test_config.py:1557` (`_COMPOSER_DEFAULTS`) and the env-dict tests at `:25,:1977,:1999,:2060`, `tests/unit/web/test_app.py:2880`, `tests/unit/cli/test_web_command.py:221,275`.
- [ ] **Step 5: Add it to deployment surfaces**, value `200000` as a placeholder with a comment that the operator must set it to their gateway's limit: `deploy/linux-systemd/elspeth-web.env.example`, `deploy/compose/web-postgres.yaml`, `deploy/azure-container-apps/workload.bicep` (param and env list), both `.bicepparam` files, `application.example.json`, `scripts/validate-workload-parameters.jq`, `scripts/resolve-workload-parameters.sh`, `deploy/azure-container-apps/README.md`, `deploy/aws-ecs/terraform/modules/scenario/locals.tf`, `src/elspeth/web/frontend/playwright.config.ts:56`, `scripts/composer_acceptance/serve.py:164`. Update `tests/unit/web/test_azure_container_apps_runbook_contract.py:91-100` for the new bicep parameter.
- [ ] **Step 6: Update docs:** `docs/reference/environment-variables.md` (three new rows, changed default), `docs/guides/docker.md`, `docs/guides/web-local-development.md`, `docs/guides/your-first-pipeline.md`, `docs/runbooks/azure-container-apps-cold-install.md`, and the 2 MiB statement at `docs/security-assurance/11-ai-llm-risk-assessment.md:187`.
- [ ] **Step 7: Run** `tests/unit/web/ -n 12` to a log and read the exit code. Any test that relied on the 2 MiB default must set the value explicitly; do not weaken the test. Expected: exit 0.
- [ ] **Step 8: Commit** `feat(web)!: required composer context token cap; request byte default 960 KiB`.

---

### Task 4: History reader and threading

**Files:**
- Create: `src/elspeth/web/composer/context_budget_history.py`
- Modify: `src/elspeth/web/sessions/composer_turn.py` (:979 `records`, :1038 history projection)
- Modify: `src/elspeth/web/composer/protocol.py`, `service.py` (`compose` :1110, `_compose_loop` :2352), `planning_application.py`, `pipeline_planner.py` (`_plan_pipeline_inner` :3364)
- Test: `tests/unit/web/composer/test_context_budget_history.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True, slots=True)
class ContextBudgetHistory:
    bytes_per_token: float | None
    previously_shed_history: frozenset[str]     # "history:<int>" keys

    @classmethod
    def empty(cls) -> ContextBudgetHistory
    @classmethod
    def from_records(cls, records: Sequence[...]) -> ContextBudgetHistory
```

- `from_records` takes the same record objects `get_messages(limit=None)` returns at `composer_turn.py:979`. It walks `role="audit"` rows whose `tool_calls` is an `llm_call_audit` envelope, newest first.
  - `bytes_per_token`: from the newest call with success status, a `request_byte_split` whose `total_bytes > 0`, and `prompt_tokens > 0`; value `total_bytes / prompt_tokens`. `None` if there is none.
  - `previously_shed_history`: from the newest call that has a non-null `context_budget`; its `shed_history_indices` as `history:<i>` keys. Empty if there is none.
- These rows are ELSPETH's own stored data: a present-but-malformed envelope raises, it is not skipped.
- `compose(..., context_budget_history: ContextBudgetHistory | None = None)` on the service protocol and implementation, passed unchanged to `_compose_loop` and through `_plan_and_stage_empty_pipeline` to `_plan_pipeline_inner`. `None` means `ContextBudgetHistory.empty()`.

- [ ] **Step 1: Write failing tests:** newest successful call wins for the ratio; a failed call is skipped; a call without a byte split or with `prompt_tokens` of `None` or `0` is skipped and, if none qualifies, the ratio is `None` (Review Focus 1); the previously shed set comes from the newest call carrying `context_budget` even when that call failed; records with no audit rows give `empty()`; a malformed envelope raises.
- [ ] **Step 2: Run.** Expected: import failure.
- [ ] **Step 3: Implement** the reader. Reuse the envelope-walking shape of `AdvisorTranscriptHistory.from_records` (`advisor_transcript.py:126`).
- [ ] **Step 4: Thread it:** build it from `records` in `composer_turn.py` next to the `_composer_chat_history` call and pass it to `compose`. Add the keyword to the protocol and both downstream signatures. No behaviour changes yet.
- [ ] **Step 5: Run** `tests/unit/web/composer/test_context_budget_history.py`, `tests/unit/web/sessions/` and `tests/unit/web/composer/test_service.py`. Expected: exit 0.
- [ ] **Step 6: Commit** `feat(composer): derive context budget history from stored call records`.

---

### Task 5: Compose-loop call site

**Files:**
- Create: `src/elspeth/web/composer/context_budget_apply.py`
- Modify: `src/elspeth/web/composer/prompts.py` (`build_context_string` ~:400-501, `build_messages` :504)
- Modify: `src/elspeth/web/composer/service.py` (`_prepare_primary_delivery` :1423, `_call_model_turn` :1452, policy construction in `__init__`)
- Test: `tests/unit/web/composer/test_compose_loop_context_budget.py`

**Interfaces:**
- Consumes: Task 1 decision API; Task 2 `ComposerContextBudgetRecord.from_shed_keys` and `build_llm_call_record(context_budget=...)`; Task 4 `ContextBudgetHistory`.
- Produces in `context_budget_apply.py`:

```python
def message_bytes(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> ComposerLLMRequestByteSplit
    # detaches prompt-guard keys, then delegates to _hash_and_measure_request so sizes match the recorded split
def stubbed_tool_message(message: dict[str, Any]) -> dict[str, Any]
    # copy with content = CONTEXT_BUDGET_TOOL_RESULT_STUB; role and tool_call_id unchanged
def latest_round_trip_start(messages: list[dict[str, Any]]) -> int | None
    # index of the last assistant message that carries tool_calls
def largest_component(named_bytes: Mapping[str, int]) -> str
```

- Produces in `prompts.py`: `history_message_indices(chat_history) -> tuple[int, ...]` (the `chat_history` index of each history message `build_messages` emits, using the same empty-assistant filter), and a keyword `conversation_omissions: Mapping[str, int] | None = None` on `build_context_string`, emitted as a top-level `"conversation_omissions": {"prior_user_requests_omitted": n, "prior_assistant_messages_omitted": m}` only when either count is above zero.
- Refusal: `ComposerConvergenceError.capture(..., reason="context_budget_exceeded", evidence={"ceiling": ..., "estimated_tokens": ..., "request_bytes": ..., "max_prompt_tokens": ..., "max_request_bytes": ..., "largest_component": ...})`, raised before the provider call, in the style of `tool_call_cap_exceeded` (`service.py:1408`).

Segment labelling for the compose loop, on the `llm_messages` list as it stands at each call:

| Message | Segment |
|---|---|
| index 0 (system), index 1 (catalog), tool definitions | protected |
| history message with the user-authorship marker | `USER_REQUEST`, key `history:<chat_history index>` |
| history message with role `assistant` | `ASSISTANT_PROSE`, key `history:<chat_history index>` |
| any other history row | protected |
| state message, current user message, recovery and retained-operation system messages | protected |
| role `tool` message before `latest_round_trip_start` | `TOOL_RESULT`, key `tool:<tool_call_id>`, saving = its bytes minus the stubbed message's bytes; omitted when that is not positive |
| everything from `latest_round_trip_start` on, and any other appended message | protected |

`previously_shed` is `history.previously_shed_history` united with every key recorded in `context_budget` on this turn's earlier calls (`recorder.llm_calls`).

Applying a decision, to the deep copy made in `_prepare_primary_delivery` and before cache markers are added: remove shed history messages; replace shed tool messages with `stubbed_tool_message`; if any history was shed, re-render the state message with `conversation_omissions`. The unshed `llm_messages` list and the transcript owner are untouched. After markers are added, measure once more and raise the same refusal with `ceiling="bytes"` if the marked request exceeds `max_request_bytes`. Component names for `largest_component`: `system`, `catalog`, `tools`, `state`, `current_message`, `latest_tool_round`, `other`.

- [ ] **Step 1: Write failing tests** with the `litellm.acompletion` recording fake from `test_compose_loop_llm_audit.py:582-591` and a settings helper with a small cap:
  - a long history over the high mark: the request sent contains no assistant history message, still contains every user request, the state message carries `prior_assistant_messages_omitted`, and the call record's `context_budget.shed_history_indices` equals the removed indices;
  - a three-round tool turn over the high mark: the first round's tool message content is the stub, its `tool_call_id` is unchanged, the latest round's results are intact, and `stubbed_tool_call_ids` matches;
  - a latest round with two parallel tool calls keeps both results (Review Focus 4);
  - a tool result shorter than the stub is never stubbed (Review Focus 3);
  - a current user message larger than the cap raises `ComposerConvergenceError` with `reason == "context_budget_exceeded"` and `evidence["largest_component"] == "current_message"`, and the fake recorded no provider call (Review Focus 5);
  - under the high mark the request is byte-identical to the request built without a budget history, and the record has empty lists;
  - `stable_hash` of the sent messages equals the recorded `messages_hash`;
  - negative control: the same long history with `max_prompt_tokens` set very high sends every message.
- [ ] **Step 2: Run.** Expected: fail.
- [ ] **Step 3: Implement** `context_budget_apply.py` and the `prompts.py` additions.
- [ ] **Step 4: Implement the call site.** Build one `ContextBudgetPolicy` in the service constructor from settings. Do the labelling, decision and application in a new private method called from `_prepare_primary_delivery`; pass the resulting `ComposerContextBudgetRecord` down so the call record carries it.
- [ ] **Step 5: Run** the new file plus `tests/unit/web/composer/test_service.py`, `test_compose_loop_llm_audit.py` and `test_prompts.py`. Expected: exit 0.
- [ ] **Step 6: Commit** `feat(composer): shed compose-loop context to the budget before each call`.

---

### Task 6: Planner call site

**Files:**
- Modify: `src/elspeth/web/composer/pipeline_planner.py` (`PlannerConversationContext` :620, request build :3487-3513, `call_model_body` :3570-3638, outcome set :1446-1456)
- Modify: `src/elspeth/contracts/composer_planner_audit.py` (`ComposerPlannerCode` :54)
- Modify: `src/elspeth/web/composer/planning_application.py` (:1165 policy construction)
- Modify: `src/elspeth/web/composer/skills/pipeline_composer.md` (:59)
- Test: `tests/unit/web/composer/test_planner_context_budget.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 4 and the Task 5 helpers.
- Produces:
  - `ComposerPlannerCode.CONTEXT_BUDGET_EXHAUSTED = "CONTEXT_BUDGET_EXHAUSTED"`, added to the `BUDGET_EXHAUSTED` outcome set at `pipeline_planner.py:1446-1456`.
  - `PlannerConversationContext.prior_assistant_messages_omitted: int = 0`; `to_dict()` emits it only when above zero. Two invariants change: the context may hold no retained entries when either omitted count is above zero, and the "omitted requires two retained requests" rule at `:648` is removed.
  - `PlannerConversationContext.without(history_indices: frozenset[int]) -> PlannerConversationContext`, removing those entries and adding to the matching omitted count.
  - `_plan_pipeline_inner(..., context_policy: ContextBudgetPolicy, context_budget_history: ContextBudgetHistory)`.
  - Refusal: `PipelinePlannerError("planner context budget exhausted", code="CONTEXT_BUDGET_EXHAUSTED", detail_codes=("context_ceiling_tokens" | "context_ceiling_bytes", "largest_component_<name>"))`. Component names: `system`, `reference`, `tools`, `request`, `latest_tool_round`, `transcript`, `other`.

The existing `REQUEST_BYTES_EXHAUSTED` check at `:3636-3638` stays as the final guard on the marked request.

Segment labelling in `call_model_body`, on `messages` before cache markers:

| Message | Segment |
|---|---|
| index 0 (skill), index 1 (reference block), tools | protected |
| the request message (index 2) | protected, except its `conversation_context` entries: each prior assistant message is `ASSISTANT_PROSE`, each prior user request is `USER_REQUEST`, key `history:<history_index>`, saving = bytes of the entry's canonical JSON plus one |
| role `tool` continuation message before the latest round trip | `TOOL_RESULT`, as in Task 5 |
| everything else | protected |

`previously_shed` is composed exactly as in Task 5: `context_budget_history.previously_shed_history` united with every key recorded in `context_budget` on this request's earlier calls (`recorder.llm_calls`).

Applying: if history keys are shed, re-render the request message from `provider_request` with `conversation_context.without(...)` (same `canonical_json` and `json_content_parts` call as `:3501-3512`); stub shed tool messages. Do this on a copy; `messages` itself keeps growing unshed. The manifest, the call record and the provider all receive the shed copy.

- [ ] **Step 1: Write failing tests** using `_ScriptedCompletion`, `_response` and `_plan` from `tests/unit/web/composer/test_pipeline_planner.py:313-792`:
  - a conversation context over the high mark: the request message sent has no `prior_assistant_messages`, `prior_assistant_messages_omitted` equals the number removed, user requests are intact;
  - pushed further: oldest user requests are removed and `additional_prior_user_requests_omitted` counts them, including the case where none remain;
  - a multi-call run with large tool results: earlier results are stubbed in later requests, the latest round is intact, and each call record's `context_budget` matches its request;
  - an oversized intent raises `PipelinePlannerError` with `code == "CONTEXT_BUDGET_EXHAUSTED"` and `detail_codes == ("context_ceiling_tokens", "largest_component_request")`, with no scripted response consumed (Review Focus 5);
  - indices 0 and 1 and the tools are byte-identical across every request in a shedding run;
  - `PlannerConversationContext` shape is unchanged when nothing is omitted (existing pins at `test_planner_conversation_history.py:54` and `test_pipeline_planner.py:10290` stay green untouched);
  - negative control: a very high cap sends the full context.
- [ ] **Step 2: Run.** Expected: fail.
- [ ] **Step 3: Implement** the contract changes, then the call site. Construct the `ContextBudgetPolicy` in `planning_application.py` beside `PlannerBudgetPolicy` and pass both.
- [ ] **Step 4: Update the skill text** at `skills/pipeline_composer.md:59` with one sentence: `prior_assistant_messages_omitted` and `additional_prior_user_requests_omitted` count earlier messages no longer shown; ask the user to restate anything the request seems to depend on. This changes `composer_skill_hash`; say so in the commit message.
- [ ] **Step 5: Update tests that pin the planner code set:** `tests/integration/web/composer/test_freeform_planner_failure_translation.py:1067` (`_ALL_PLANNER_CODES`), `tests/unit/evals/composer_battery/test_battery_planner.py:346` with `evals/lib/battery_planner.py:63` (`TRIAGE_BY_CODE`), and `src/elspeth/web/frontend/tests/e2e/helpers/tutorial-harness.ts:70-91`.
- [ ] **Step 6: Run** the new file, `test_pipeline_planner.py`, `test_planner_conversation_history.py` and `tests/integration/web/composer/test_freeform_pipeline_planner.py`. Expected: exit 0.
- [ ] **Step 7: Commit** `feat(composer): shed planner context to the budget before each call`.

---

### Task 7: Escape-hatch transcript

**Files:**
- Modify: `src/elspeth/web/composer/advisor_transcript.py` (`AdvisorVisibleTranscript` :577)
- Modify: `src/elspeth/web/composer/pipeline_planner.py` (`_engage_escape_hatch` :4201, `call_model_body`)
- Test: `tests/unit/web/composer/test_planner_context_budget.py` (extend), `tests/unit/web/composer/test_advisor_transcript.py` (extend)

**Interfaces:**
- Produces:

```python
def event_bytes(self) -> tuple[int, ...]                 # canonical JSON bytes of each event in provider_json, plus one
def sheddable_event_indices(self) -> tuple[int, ...]     # indices of events with no "content_ref" key
def without_events(self, indices: frozenset[int]) -> AdvisorVisibleTranscript
```

- `without_events` removes those events from the payload, appends the string `"oldest_events_omitted_for_context_budget"` to `availability_gaps` (once), and returns a transcript whose `binding` is `stable_hash({"transcript": self.binding, "omitted_events": sorted(indices)})`. `request` and `semantic_binding` are unchanged. It raises `ValueError` if any index is not in `sheddable_event_indices()`.
- In `call_model_body`, when the hatch transcript message is present, each sheddable event is a `HATCH_EVENT` segment with key `hatch:<index>` and saving `event_bytes()[index]`. Applying rebuilds that message from `without_events(...)` with the same header and `untrusted_content_parts()`.

`_engage_escape_hatch` must keep the transcript object (not only its JSON) so `call_model_body` can label and rebuild it.

- [ ] **Step 1: Write failing tests:** `without_events` drops exactly the named events, keeps the event carrying `content_ref`, records the gap once, changes `binding`, keeps `semantic_binding`; an index for a `content_ref` event raises; a hatch run over the high mark sends a transcript missing its oldest events while later ones remain, `shed_hatch_event_indices` matches, and assistant conversation-context entries were shed before any hatch event.
- [ ] **Step 2: Run.** Expected: fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** both test files and `tests/unit/web/composer/ -k "advisor or hatch"`. Expected: exit 0.
- [ ] **Step 5: Commit** `feat(composer): bound the escape-hatch transcript by the context budget`.

---

### Task 8: What the user sees

**Files:**
- Modify: `src/elspeth/web/sessions/routes/_helpers.py` (:3014-3147)
- Modify: `src/elspeth/web/sessions/composer_turn.py` (convergence handling :1578)
- Modify: `src/elspeth/contracts/composer_progress.py` (`ComposerProgressReason` :94)
- Modify: `src/elspeth/web/composer/service.py`, `pipeline_planner.py` (emit the progress event)
- Modify frontend: `src/types/index.ts` (:379), `src/api/composerOperationDecoder.ts` (:281), `src/components/chat/ComposerOperationOutcomeNotice.tsx` (`OUTCOME_LABELS`), `src/config/composer.ts` (`isComposePermanentRefusal` :16)
- Test: `tests/integration/web/composer/test_freeform_planner_failure_translation.py`, `tests/unit/web/composer/test_progress.py`, the frontend component tests beside each changed file

**Interfaces:**
- Operation `failure_code`: `context_budget_exceeded`, for both the planner code `CONTEXT_BUDGET_EXHAUSTED` and the compose-loop reason `context_budget_exceeded`. HTTP status: the same as the existing budget-exhaustion failures in `_FREEFORM_PLANNER_FAILURE_HTTP`.
- `REQUEST_BYTES_EXHAUSTED` maps to the same `context_budget_exceeded` failure code instead of falling through to `operation_failed`.
- User-facing detail, exact: `This conversation is too large for the composer to continue. Start a new session, or shorten your request.`
- `context_budget_exceeded` is a permanent refusal (no Retry) in `isComposePermanentRefusal`.
- New `ComposerProgressReason` member `context_reduced`, mirrored in the two frontend files. It is emitted once per turn, before the first model call whose decision sheds any `history:` key, with headline exactly `Earlier conversation is no longer visible to the composer.`

- [ ] **Step 1: Write failing tests:** both refusal sources translate to `failure_code == "context_budget_exceeded"` with the exact detail; `REQUEST_BYTES_EXHAUSTED` now translates the same way; the progress reason set includes `context_reduced` and the frontend mirror matches (`test_progress.py`); a shedding turn emits exactly one `context_reduced` event and a non-shedding turn emits none; the outcome notice renders the detail and shows no Retry.
- [ ] **Step 2: Run** the Python tests and `npm test -- --run` for the touched frontend files. Expected: fail.
- [ ] **Step 3: Implement** the mappings, the progress event and the frontend label.
- [ ] **Step 4: Run** again, plus `tests/unit/web/sessions/`. Expected: exit 0.
- [ ] **Step 5: Commit** `feat(web): name the context budget refusal and announce shed history`.

---

### Task 9: Changelog and whole-tree verification

**Files:**
- Modify: `CHANGELOG.md` (confirm the target release with the maintainer first; do not infer it from the checkout)
- Modify: `docs/development/composer-context-budget-brief.md` (add a "Design, as built" section linking the spec and listing the settings)

- [ ] **Step 1: Ask the maintainer which release this lands in.** Then write the entry, stating both breaking changes: `ELSPETH_WEB__COMPOSER_CONTEXT_MAX_PROMPT_TOKENS` is required and the server will not start without it; the default of `composer_planner_max_request_bytes` is now 960 KiB (was 2 MiB), and it also applies to the compose loop. State the `composer_skill_hash` change.
- [ ] **Step 2: Run ruff and mypy** through `scripts/full-suite-gate.sh --execute --detach --stages ruff,mypy,contracts`; read `summary.txt`.
- [ ] **Step 3: Compare the keyless lint corpus** before (at `c2386ece`) and after, per the brief's Phase 4 command, with line numbers normalised. Account for every added or removed finding. Stage any allowlist work for the operator; sign nothing.
- [ ] **Step 4: Run the architecture suite** `tests/unit/architecture/ -n 3` to a log; read the exit code.
- [ ] **Step 5: Run the full gate** `scripts/full-suite-gate.sh --execute --detach` after checking for other suites on the host; read `summary.txt` and confirm `frozen=YES`. The change touches shared composer runtime behaviour, so the full suite is required. No schema changed (`chat_messages.tool_calls` is a JSON column), so the `testcontainer` stage is included only because the gate runs it by default where Docker exists; say explicitly if it did not run.
- [ ] **Step 6: Commit** `docs(changelog): composer context budget` and push with `git push -u origin claude/lucid-lamport-wank6h`.

## Not covered by this plan

- Replaying the affected users' failing sessions through the new policy (the brief's acceptance evidence). It needs their session records, which this plan does not have.
- A tutorial smoke run showing the two-turn case still hits the prefix cache. It needs a live provider.
