# Phase 1: elspeth-llm-gateway Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `elspeth-llm-gateway` runtime product: contract-major-1 HTTP surface, adapter SDK, fictional reference adapter, deterministic mock stack, conformance kit, and container image — per `docs/superpowers/specs/2026-07-30-llm-compatibility-gateway-runtime-design.md`.

**Architecture:** A FastAPI app (`core/`) validates a strict OpenAI Chat Completions subset, checks capabilities, and orchestrates a pinned adapter (`sdk/` protocol) that maps canonical requests to a custom `POST v1/invoke` body. The core alone owns OAuth2 client-credentials lifecycle, origin-pinned HTTP transport with exactly one 401-refresh replay, and metadata-only logging. Everything is sync-free of ELSPETH internals: the package lives in `gateway/` with its own `pyproject.toml`.

**Tech Stack:** Python 3.12, FastAPI, pydantic v2 (`extra='forbid'`, frozen models), httpx (+ respx and `httpx.ASGITransport` in tests), pytest.

## Global Constraints

- Contract major is `1`; header `X-ELSPETH-LLM-Gateway-Contract: 1` required on requests and echoed on ALL responses including errors.
- Closed capability vocabulary: `text tools json_object json_schema seed usage`; `text` mandatory.
- Closed error codes exactly: `invalid_request inbound_authentication_failed contract_mismatch model_not_allowed capability_unsupported context_length_exceeded content_policy_rejected oauth_token_unavailable upstream_unauthorized upstream_rate_limited upstream_timeout upstream_unavailable upstream_response_invalid internal_error`.
- Allowed finish reasons: `stop length tool_calls content_filter`. Unmapped upstream reason → `upstream_response_invalid`, never silently `stop`.
- Usage is never estimated or invented; absent usage is omitted, not zeroed.
- No prompt, response, schema, tool definition/argument, credential, token, client ID/secret, raw header, or raw upstream/OAuth body in any log line or error message. Error messages are fixed per-code strings.
- Adapter may not: perform network I/O, read env/credentials, supply an `Authorization`/`Host` header, supply an absolute URL or `..` path segment.
- Upstream origin must be `https://…` (exact exception: `http://127.0.0.1:<port>` for local/mock use). Redirects never followed. TLS verification always on.
- Env namespace `ELSPETH_LLM_GATEWAY_`; any unknown variable in that namespace fails startup.
- No ELSPETH-repo imports inside `gateway/`; no venv mutation (root-test wiring is `sys.path` via conftest, NOT editable install).
- Every commit: stage by pathspec, message style `feat(gateway): …` / `test(gateway): …`; run the task's tests before committing. Run `ruff check --fix <files> && ruff format <files>` (repo venv) BEFORE committing — the pre-commit hook enforces both and brief code blocks are not pre-formatted.
- All gateway unit/conformance tests must also pass under the root gate later; keep them importable with `gateway/src` on `sys.path` and zero ELSPETH imports.

## File Structure (locked)

```
gateway/
  pyproject.toml
  README.md
  Dockerfile
  src/elspeth_llm_gateway/
    __init__.py            # __version__, CONTRACT_MAJOR, ADAPTER_API_MAJOR
    sdk/__init__.py        # re-exports: types, protocol, Capability
    sdk/types.py           # canonical frozen request/response models
    sdk/protocol.py        # AdapterDescriptor, InvokePlan, UpstreamFailure, AdapterProtocol
    core/errors.py         # GatewayErrorCode, GatewayError, envelope
    core/parsing.py        # strict JSON (dup keys, UTF-8, non-finite, size)
    core/contract.py       # inbound ChatRequest subset + response builder
    core/config.py         # GatewayConfig.from_env, model mappings + generation hash
    core/auth.py           # constant-time bearer check
    core/oauth.py          # TokenManager (client_secret_basic/post, single-flight)
    core/transport.py      # UpstreamClient (origin pinning, one 401 replay)
    core/service.py        # CompletionService orchestration
    core/events.py         # metadata-only event logging (field allowlist)
    core/app.py            # create_app(): routes /v1/chat/completions /healthz /readyz
    reference/adapter.py   # fictional reference_v1_invoke adapter
  mock/oauth.py            # deterministic mock token endpoint (FastAPI)
  mock/upstream.py         # deterministic mock fictional /v1/invoke (FastAPI)
  mock/stack.py            # `python -m mock.stack` local runner
  conformance/             # URL-or-app parameterized pytest kit
    conftest.py  test_contract.py  test_capabilities.py  test_tools.py
    test_structured.py  test_errors.py  test_identity.py
  tests/                   # unit tests, one file per core/sdk module
tests/gateway_runtime/     # ROOT repo: conftest sys.path shim + in-process conformance run
```

---

### Task 1: Package scaffold, identity, strict JSON parsing

**Files:**
- Create: `gateway/pyproject.toml`, `gateway/src/elspeth_llm_gateway/__init__.py`, `gateway/src/elspeth_llm_gateway/core/parsing.py`, `gateway/tests/conftest.py`, `gateway/tests/test_parsing.py`

**Interfaces:**
- Produces: `CONTRACT_MAJOR: int = 1`, `ADAPTER_API_MAJOR: int = 1`, `__version__: str` in `elspeth_llm_gateway`; `parse_strict_json(raw: bytes, *, max_bytes: int) -> Any` raising `StrictJsonError(reason: str)` (reasons: `too_large invalid_utf8 invalid_json duplicate_key non_finite`).

- [ ] **Step 1: Write failing tests**

```python
# gateway/tests/conftest.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# gateway/tests/test_parsing.py
import pytest
from elspeth_llm_gateway.core.parsing import StrictJsonError, parse_strict_json

def test_parses_plain_object():
    assert parse_strict_json(b'{"a": 1}', max_bytes=100) == {"a": 1}

@pytest.mark.parametrize("raw,reason", [
    (b'{"a":1,"a":2}', "duplicate_key"),
    (b'{"a": Infinity}', "non_finite"),
    (b'{"a": NaN}', "non_finite"),
    (b'\xff\xfe', "invalid_utf8"),
    (b'{"a"', "invalid_json"),
    (b'[1,2]' + b" " * 200, "too_large"),
])
def test_rejections(raw, reason):
    with pytest.raises(StrictJsonError) as exc:
        parse_strict_json(raw, max_bytes=100)
    assert exc.value.reason == reason

def test_nested_duplicate_key_rejected():
    with pytest.raises(StrictJsonError):
        parse_strict_json(b'{"x": {"b":1,"b":2}}', max_bytes=100)
```

- [ ] **Step 2: Run to verify failure** — `cd gateway && python -m pytest tests/test_parsing.py -q` → import error.

- [ ] **Step 3: Implement**

```toml
# gateway/pyproject.toml
[project]
name = "elspeth-llm-gateway"
version = "0.1.0"
description = "ELSPETH LLM compatibility gateway: strict OpenAI Chat subset over custom agency invoke APIs"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115,<1",
    "httpx>=0.28,<1",
    "pydantic>=2.12,<3",
    "uvicorn>=0.30,<1",
]
[project.optional-dependencies]
test = ["pytest>=8", "pytest-asyncio>=0.24", "respx>=0.22"]
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
[tool.hatch.build.targets.wheel]
packages = ["src/elspeth_llm_gateway"]
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

(pydantic floor is `2.12` — the venv actually runs 2.12.4 and the image must not ship an untested newer floor. The `[tool.pytest.ini_options]` block pins pytest's rootdir to `gateway/` so root-repo `addopts`/`pythonpath` — which include ELSPETH `src` — never leak into gateway test runs, and makes the conformance kit standalone-runnable for derived-image qualification.)

Additional Task 1 requirement (finiteness): `parse_strict_json` must also reject overflow literals — `b'[1e400]'` parses to `inf` without `parse_constant` firing. After `json.loads`, walk the result and raise `StrictJsonError("non_finite")` if any float fails `math.isfinite`. Add test: `parse_strict_json(b'{"t": 1e400}', max_bytes=100)` and `b'[-1e400]'` both raise with reason `non_finite`.

```python
# gateway/src/elspeth_llm_gateway/__init__.py
CONTRACT_MAJOR = 1
ADAPTER_API_MAJOR = 1
__version__ = "0.1.0"  # kept in lockstep with gateway/pyproject.toml
```

```python
# gateway/src/elspeth_llm_gateway/core/parsing.py
import json, math
from typing import Any

class StrictJsonError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"strict json rejection: {reason}")

def _no_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise StrictJsonError("duplicate_key")
        obj[key] = value
    return obj

def _reject_constant(_value: str) -> Any:
    raise StrictJsonError("non_finite")

def parse_strict_json(raw: bytes, *, max_bytes: int) -> Any:
    if len(raw) > max_bytes:
        raise StrictJsonError("too_large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StrictJsonError("invalid_utf8") from exc
    try:
        return json.loads(text, object_pairs_hook=_no_dupes, parse_constant=_reject_constant)
    except StrictJsonError:
        raise
    except json.JSONDecodeError as exc:
        raise StrictJsonError("invalid_json") from exc
```

- [ ] **Step 4: Run to verify pass** — same command, all green.
- [ ] **Step 5: Commit** — `git add gateway/pyproject.toml gateway/src gateway/tests && git commit -m "feat(gateway): package scaffold, identity constants, strict JSON parsing"`

---

### Task 2: SDK canonical types and capabilities

**Files:**
- Create: `gateway/src/elspeth_llm_gateway/sdk/__init__.py`, `gateway/src/elspeth_llm_gateway/sdk/types.py`
- Test: `gateway/tests/test_sdk_types.py`

**Interfaces:**
- Produces (all pydantic v2, `model_config = ConfigDict(frozen=True, extra="forbid")`):
  - `Capability(StrEnum)`: `TEXT="text" TOOLS="tools" JSON_OBJECT="json_object" JSON_SCHEMA="json_schema" SEED="seed" USAGE="usage"`
  - `FinishReason(StrEnum)`: `STOP="stop" LENGTH="length" TOOL_CALLS="tool_calls" CONTENT_FILTER="content_filter"`
  - `CanonicalToolDef(name: str, description: str | None, parameters_schema: dict)`
  - `CanonicalToolCall(call_id: str, name: str, arguments_json: str)`
  - `CanonicalMessage(role: Literal["system","user","assistant","tool"], content: str | None, tool_calls: tuple[CanonicalToolCall, ...] = (), tool_call_id: str | None = None)`
  - `ResponseFormatSpec(kind: Literal["json_object","json_schema"], schema_name: str | None = None, schema: dict | None = None)`
  - `CanonicalRequest(model_target: dict, model_alias: str, messages: tuple[CanonicalMessage, ...], temperature: float | None, seed: int | None, max_tokens: int | None, tools: tuple[CanonicalToolDef, ...] = (), tool_choice: str | None = None, tool_choice_function: str | None = None, response_format: ResponseFormatSpec | None = None)`
  - `CanonicalUsage(prompt_tokens: int, completion_tokens: int, total_tokens: int)` — validator: all `>= 0` and `total_tokens == prompt_tokens + completion_tokens`
  - `CanonicalResponse(text: str | None, tool_calls: tuple[CanonicalToolCall, ...] = (), finish_reason: FinishReason, usage: CanonicalUsage | None = None)` — validator: exactly one of (`text` is not None) XOR (`tool_calls` non-empty)

- [ ] **Step 1: Write failing tests** — construct each model happy-path; assert frozen (`ValidationError`/`TypeError` on attribute set); assert `extra="forbid"` (unknown kwarg raises); assert `CanonicalResponse(text="x", tool_calls=(tc,), ...)` raises; assert usage arithmetic validator raises on `total_tokens=5, prompt=1, completion=1`; assert `CanonicalResponse` requires one content form (both `None`/empty raises).
- [ ] **Step 2: Run to verify fail** — `cd gateway && python -m pytest tests/test_sdk_types.py -q`
- [ ] **Step 3: Implement** exactly the models above; `sdk/__init__.py` re-exports every name.
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** — `git add gateway/src/elspeth_llm_gateway/sdk gateway/tests/test_sdk_types.py && git commit -m "feat(gateway): canonical SDK types and capability vocabulary"`

---

### Task 3: SDK adapter protocol and InvokePlan safety

**Files:**
- Create: `gateway/src/elspeth_llm_gateway/sdk/protocol.py`
- Test: `gateway/tests/test_sdk_protocol.py`

**Interfaces:**
- Produces:
  - `AdapterDescriptor(name: str, version: str, adapter_api_major: int, capabilities: frozenset[Capability])` — validator: `Capability.TEXT` must be present; `name` matches `^[a-z][a-z0-9_]{2,63}$`.
  - `InvokePlan(path: str, headers: dict[str, str] = {}, body: dict)` — validators: `path` must not start with `/` or contain `://`, `..`, `?`, `#`, whitespace; header names lower-cased on validation and must not be in `{"authorization", "host", "cookie", "x-forwarded-for"}`; header values `str` ≤ 1024 chars.
  - `UpstreamFailure(status: int, body: dict | None)` (frozen; body is the already-bounded parsed body or None).
  - `ErrorClassification(code: str, retryable: bool)` — `code` must be one of `CLASSIFIABLE_CODES`, defined as a LITERAL `frozenset[str]` in `sdk/protocol.py` itself = `{"context_length_exceeded","content_policy_rejected","upstream_rate_limited","upstream_timeout","upstream_unavailable","upstream_response_invalid"}`. The `sdk/` package must import NOTHING from `core/` (SDK ships standalone to adapter authors); `core` asserts at import time that `CLASSIFIABLE_CODES <= {c.value for c in GatewayErrorCode}`.
  - `class AdapterProtocol(Protocol)`: `def descriptor(self) -> AdapterDescriptor`; `def validate_configuration(self, options: dict) -> None`; `def build_invoke(self, request: CanonicalRequest) -> InvokePlan`; `def parse_success(self, body: dict) -> CanonicalResponse`; `def classify_error(self, failure: UpstreamFailure) -> ErrorClassification`.

- [ ] **Step 1: Write failing tests** — `InvokePlan(path="/abs")`, `path="a/../b"`, `path="https://evil"`, header `{"Authorization": "x"}`, header `{"AUTHORIZATION": "x"}` all raise; valid plan normalizes header names to lowercase; `AdapterDescriptor` without `text` raises; `ErrorClassification(code="internal_error")` raises (not adapter-classifiable); a minimal in-test class satisfying `AdapterProtocol` passes `isinstance` under `runtime_checkable`.
- [ ] **Step 2: Run to verify fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(gateway): adapter protocol with fail-closed InvokePlan validation"` (pathspec: `gateway/src/elspeth_llm_gateway/sdk/protocol.py gateway/tests/test_sdk_protocol.py`)

---

### Task 4: Gateway error model

**Files:**
- Create: `gateway/src/elspeth_llm_gateway/core/errors.py`
- Test: `gateway/tests/test_errors.py`

**Interfaces:**
- Produces:
  - `GatewayErrorCode(StrEnum)` — exactly the 14 codes from Global Constraints.
  - `HTTP_STATUS: dict[GatewayErrorCode, int]` — `invalid_request:400 inbound_authentication_failed:401 contract_mismatch:400 model_not_allowed:404 capability_unsupported:422 context_length_exceeded:400 content_policy_rejected:400 oauth_token_unavailable:503 upstream_unauthorized:502 upstream_rate_limited:429 upstream_timeout:504 upstream_unavailable:503 upstream_response_invalid:502 internal_error:500`.
  - `SAFE_MESSAGE: dict[GatewayErrorCode, str]` — fixed operator-safe string per code; NO formatting placeholders.
  - `RETRYABLE: dict[GatewayErrorCode, bool]` — true only for `oauth_token_unavailable upstream_rate_limited upstream_timeout upstream_unavailable`.
  - `class GatewayError(Exception)`: `__init__(self, code: GatewayErrorCode)`; `.code`, `.status`, `.retryable`, `.safe_message` derived from the tables. Constructor takes NO free-text message (leak-proof by construction).
  - `def error_envelope(error: GatewayError, request_id: str) -> dict` → `{"error": {"message": ..., "type": "gateway_error", "code": ..., "retryable": ..., "request_id": ...}}`.

- [ ] **Step 1: Write failing tests** — all 14 codes present and no extras (`set(GatewayErrorCode) == {...}` literal); every code has status/message/retryable entries; envelope shape matches design JSON exactly; `GatewayError` accepts no message argument (`pytest.raises(TypeError)` on `GatewayError(code, "text")`); `str(GatewayError(code))` contains only the code and safe message.
- [ ] **Step 2: Run to verify fail.** — [ ] **Step 3: Implement.** — [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(gateway): closed error vocabulary with leak-proof envelope"`

---

### Task 5: Inbound contract models and response builder

**Files:**
- Create: `gateway/src/elspeth_llm_gateway/core/contract.py`
- Test: `gateway/tests/test_contract.py`

**Interfaces:**
- Consumes: `sdk.types` (Task 2), `core.errors` (Task 4).
- Produces (pydantic, `extra="forbid"` everywhere):
  - `ChatFunctionDef(name: str, description: str | None = None, parameters: dict | None = None)`; `ChatTool(type: Literal["function"], function: ChatFunctionDef)`
  - `ChatToolCallFunction(name: str, arguments: str)`; `ChatToolCall(id: str, type: Literal["function"], function: ChatToolCallFunction)`
  - `ChatMessage(role: Literal["system","user","assistant","tool"], content: str | None = None, tool_calls: list[ChatToolCall] | None = None, tool_call_id: str | None = None)` — validators: `tool_calls` only on `assistant`; `tool_call_id` required iff role `tool`; `content` must be `str` for non-assistant roles (no content-parts lists).
  - `NamedToolChoiceFunction(name: str)`; `NamedToolChoice(type: Literal["function"], function: NamedToolChoiceFunction)`
  - `JsonSchemaFormat(name: str, schema: dict, strict: bool | None = None)`; `ResponseFormat(type: Literal["json_object","json_schema"], json_schema: JsonSchemaFormat | None = None)` — `json_schema` required iff type is `json_schema`.
  - `ChatRequest(model: str, messages: list[ChatMessage], temperature: float | None = None, seed: int | None = None, max_tokens: int | None = None, tools: list[ChatTool] | None = None, tool_choice: str | NamedToolChoice | None = None, response_format: ResponseFormat | None = None)` — validators: messages non-empty; string `tool_choice` in `{"auto","none","required"}`; temperature finite.
  - `def bounds_check(request: ChatRequest, bounds: Bounds) -> None` raising `GatewayError(invalid_request)`; `Bounds` model defined in Task 6 — for this task declare `Bounds(BaseModel)` HERE with fields `max_messages: int, max_tools: int, max_string_chars: int, max_schema_bytes: int, max_schema_depth: int, temperature_min: float = 0.0, temperature_max: float = 2.0, max_max_tokens: int = 32768` (Task 6 imports it). `bounds_check` also enforces: `temperature` within `[temperature_min, temperature_max]`; `max_tokens` positive and `<= max_max_tokens` (spec: "positive integer within gateway bounds", temperature "within the configured supported range").
  - `ChatRequest` cross-field validator: a named `tool_choice` (`NamedToolChoice`) must reference a function name present in `tools` — else validation error (spec: "one explicitly named declared function").
  - `def build_completion_response(*, response_id: str, created: int, model_alias: str, canonical: CanonicalResponse) -> dict` — OpenAI shape: one choice, index 0, assistant message with `content` or `tool_calls`, `finish_reason`, `usage` present only when `canonical.usage` is not None.

- [ ] **Step 1: Write failing tests** — unknown top-level field rejected (`{"model":"m","messages":[...],"stream":true}` raises, same for `n`, `logprobs`, nested unknown inside a message); tool message without `tool_call_id` rejected; `tool_choice={"type":"function","function":{"name":"f"}}` accepted; response builder golden test for text, tool-call, usage-present, and usage-absent cases; `bounds_check` failures for message count / string length / schema depth.
- [ ] **Step 2: Run to verify fail.** — [ ] **Step 3: Implement.** — [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(gateway): strict inbound Chat Completions subset and response builder"`

---

### Task 6: Configuration from environment

**Files:**
- Create: `gateway/src/elspeth_llm_gateway/core/config.py`
- Test: `gateway/tests/test_config.py`

**Interfaces:**
- Consumes: `Bounds` (Task 5), `Capability` (Task 2).
- Produces:
  - `class GatewayConfig(BaseModel)` (frozen): `inbound_bearer: SecretStr` (min 32 chars), `adapter_name: str`, `upstream_origin: str`, `oauth_token_url: str`, `oauth_client_id: SecretStr`, `oauth_client_secret: SecretStr`, `oauth_auth_method: Literal["client_secret_basic","client_secret_post"]`, `oauth_scopes: tuple[str, ...] = ()`, `oauth_fixed_lifetime_seconds: int | None = None`, `refresh_skew_seconds: int = 60`, `request_timeout_seconds: float = 60.0`, `max_body_bytes: int = 1_048_576`, `max_response_bytes: int = 4_194_304`, `bounds: Bounds`, `model_mappings: dict[str, dict]` (non-empty; alias `^[a-z0-9][a-z0-9._-]{0,63}$`).
  - `GatewayConfig.mapping_generation` property → `sha256(json.dumps(model_mappings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]`.
  - `ENV_PREFIX = "ELSPETH_LLM_GATEWAY_"`; `KNOWN_ENV: frozenset[str]` of full variable names: `..._INBOUND_BEARER _ADAPTER _UPSTREAM_ORIGIN _OAUTH_TOKEN_URL _OAUTH_CLIENT_ID _OAUTH_CLIENT_SECRET _OAUTH_AUTH_METHOD _OAUTH_SCOPES _OAUTH_FIXED_LIFETIME_SECONDS _REFRESH_SKEW_SECONDS _REQUEST_TIMEOUT_SECONDS _MAX_BODY_BYTES _MAX_RESPONSE_BYTES _MAX_MESSAGES _MAX_TOOLS _MAX_STRING_CHARS _MAX_SCHEMA_BYTES _MAX_SCHEMA_DEPTH _TEMPERATURE_MIN _TEMPERATURE_MAX _MAX_MAX_TOKENS _MODEL_MAPPINGS`. `_OAUTH_SCOPES` is SPACE-separated (RFC 6749 scope syntax).
  - `class ConfigError(Exception)` carrying `errors: list[str]` of safe code-like strings (e.g. `"unknown_env:ELSPETH_LLM_GATEWAY_TYPO"`, `"invalid_origin"`, `"bearer_too_short"`) — never secret values.
  - `def load_config(environ: Mapping[str, str]) -> GatewayConfig` — collects ALL errors then raises `ConfigError`; origin rule: `https://host[:port]` with no path/query/userinfo, or exactly `http://127.0.0.1:<port>`; `MODEL_MAPPINGS` is a JSON object parsed with `parse_strict_json`.

- [ ] **Step 1: Write failing tests** — happy path from a full env dict; unknown `ELSPETH_LLM_GATEWAY_TYPO=x` raises with `unknown_env:` entry; unrelated env (`PATH`, `ELSPETH_OTHER_X`) ignored; `http://127.0.0.1:9` accepted, `http://localhost:9` and `https://h/path` and `https://user@h` rejected; short bearer rejected; empty mappings rejected; `mapping_generation` is stable across key order; `repr(config)` and `ConfigError` args contain no secret material (assert `"sekret"*8 not in repr(...)`).
- [ ] **Step 2: Run to verify fail.** — [ ] **Step 3: Implement.** — [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(gateway): fail-closed environment configuration with mapping generation"`

---

### Task 7: Static bearer authentication

**Files:**
- Create: `gateway/src/elspeth_llm_gateway/core/auth.py`
- Test: `gateway/tests/test_auth.py`

**Interfaces:**
- Produces: `def check_bearer(authorization_header: str | None, expected: str) -> bool` — strict parse: exactly `Bearer ` + token (single space, case-sensitive scheme), then `hmac.compare_digest(token.encode(), expected.encode())`. No other header shapes, no query/cookie path anywhere else in the app.

- [ ] **Step 1: Write failing tests** — correct token passes; wrong token, `bearer x` (lowercase), `Bearer  x` (double space), `Basic x`, empty, `None`, token-with-trailing-space all fail; timing-safety smoke: function uses `hmac.compare_digest` (assert via `inspect.getsource`).
- [ ] **Step 2–4: fail → implement → pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(gateway): constant-time static bearer authentication"`

---

### Task 8: OAuth2 client-credentials TokenManager

**Files:**
- Create: `gateway/src/elspeth_llm_gateway/core/oauth.py`
- Test: `gateway/tests/test_oauth.py` (respx against `httpx.AsyncClient`)

**Interfaces:**
- Consumes: `GatewayConfig` (Task 6), `GatewayError`/codes (Task 4).
- Produces:
  - `class TokenManager: def __init__(self, config: GatewayConfig, client: httpx.AsyncClient, *, clock: Callable[[], float] = time.monotonic)`
  - `async def get_token(self) -> str` — returns cached token while `now < expires_at - refresh_skew`; otherwise single-flight (one `asyncio.Lock`) fetch. POST to `oauth_token_url` with `grant_type=client_credentials` (+ `scope` if configured); `client_secret_basic` → HTTP Basic header; `client_secret_post` → `client_id`/`client_secret` form fields. Response must be 200 JSON with non-empty `access_token: str`, `token_type` case-insensitively `bearer`, `expires_in` positive int (or use `oauth_fixed_lifetime_seconds` when configured AND `expires_in` absent). Any deviation → `GatewayError(OAUTH_TOKEN_UNAVAILABLE)`. `follow_redirects=False`, timeout from config.
  - `def invalidate(self) -> None` — drops the cached token.

- [ ] **Step 1: Write failing tests** —
  - basic method sends `Authorization: Basic base64(id:secret)` and NO secrets in form body; post method sends form fields and no Basic header;
  - second `get_token()` call does not hit the network (respx call_count == 1);
  - expiry + skew forces refresh (inject fake clock);
  - 10 concurrent `get_token()` on cold cache → exactly 1 token request (`asyncio.gather`);
  - missing `access_token`, `token_type: "mac"`, `expires_in: 0`, non-JSON body → `GatewayError` with code `oauth_token_unavailable`, and `str(exc)` does NOT contain the client id, secret, or response body text;
  - `expires_in` absent + `oauth_fixed_lifetime_seconds=300` → token cached 300s−skew.
- [ ] **Step 2–4: fail → implement → pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(gateway): single-flight OAuth2 client-credentials token lifecycle"`

---

### Task 9: Upstream transport with one 401 replay

**Files:**
- Create: `gateway/src/elspeth_llm_gateway/core/transport.py`
- Test: `gateway/tests/test_transport.py` (respx)

**Interfaces:**
- Consumes: `InvokePlan` (Task 3), `TokenManager` (Task 8), errors (Task 4), config (Task 6).
- Produces:
  - `@dataclass(frozen=True) class UpstreamResult: status: int; body: dict | None` (body parsed with `parse_strict_json` under `max_response_bytes`; parse failure of a 2xx body → `GatewayError(UPSTREAM_RESPONSE_INVALID)`; parse failure of an error body → `body=None`).
  - `class UpstreamClient: def __init__(self, config, token_manager, client: httpx.AsyncClient)`
  - `async def invoke(self, plan: InvokePlan) -> UpstreamResult`:
    1. URL = `f"{config.upstream_origin}/{plan.path}"`; assert resulting URL still starts with `upstream_origin + "/"`.
    2. Headers = plan.headers + `Authorization: Bearer <token>` + `Content-Type: application/json`. (InvokePlan already forbids adapter Authorization; transport asserts again defensively.)
    3. `follow_redirects=False`; 3xx → `GatewayError(UPSTREAM_RESPONSE_INVALID)`.
    4. On 401: `token_manager.invalidate()`, fetch fresh token, replay ONCE; second 401 → `GatewayError(UPSTREAM_UNAUTHORIZED)`.
    5. `httpx.TimeoutException` → `UPSTREAM_TIMEOUT`; `httpx.TransportError` → `UPSTREAM_UNAVAILABLE`; 429 → `UPSTREAM_RATE_LIMITED`. NO retry/replay for any of these (respx call_count == 1). **CAUTION: `httpx.TimeoutException` IS a subclass of `httpx.TransportError` — catch it FIRST or every timeout becomes `upstream_unavailable`.**
    6. Other statuses (2xx, 4xx, 5xx) → returned as `UpstreamResult` for adapter classification. (429 never reaches the adapter — it is core-owned by rule 5; adapters classify only what the transport passes through.)
    7. The 401 replay must fetch the fresh token via `token_manager.get_token()` AFTER `invalidate()`; test asserts the second request's `Authorization` header value differs from the first. The response-size-cap test must use streaming (`client.send(..., stream=True)` semantics inside `invoke`): read at most `max_response_bytes + 1` bytes before deciding, never `.content` on an unbounded body.

- [ ] **Step 1: Write failing tests** — 401-then-200 sequence: exactly 2 upstream calls + 2 token requests, second call carries the new token; 401-then-401 → `upstream_unauthorized` and exactly 2 calls; 429 → `upstream_rate_limited`, 1 call; timeout → `upstream_timeout`, 1 call; connect error → `upstream_unavailable`; 500 returns `UpstreamResult(status=500)` (no exception, no replay); 302 → `upstream_response_invalid`; oversized 2xx body → `upstream_response_invalid`; response body exceeding `max_response_bytes` never fully buffered beyond the cap (use a 2×cap body).
- [ ] **Step 2–4: fail → implement → pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(gateway): origin-pinned upstream transport with single 401 replay"`

---

### Task 10: Fictional reference adapter

**Files:**
- Create: `gateway/src/elspeth_llm_gateway/reference/adapter.py`, `gateway/src/elspeth_llm_gateway/reference/__init__.py`
- Test: `gateway/tests/test_reference_adapter.py`

**Interfaces:**
- Consumes: entire SDK (Tasks 2–3).
- Produces: `class ReferenceV1InvokeAdapter` satisfying `AdapterProtocol`, `descriptor()` → name `reference_v1_invoke`, version `0.1.0`, api major 1, ALL six capabilities. The fictional upstream schema (deliberately non-agency, documented in the module docstring):
  - Request body: `{"conversation": [{"speaker": "<role>", "text": "<content>", "operations": [...], "operation_ref": "..."}], "generation": {"target": "<model_target['target']>", "temperature": t?, "seed": s?, "max_output": m?}, "directives": [{"operation": name, "about": desc, "payload_schema": {...}}]?, "format": {"kind": "object"|"schema", "schema": {...}}?}`
  - Success body: `{"result": {"text": "..."} | {"invocations": [{"ref": id, "operation": name, "payload": {...}}]}, "halt": "complete"|"truncated"|"operations"|"screened", "accounting": {"input_units": n, "output_units": n}?}`
  - `parse_success` maps halt → finish reason (`complete→stop truncated→length operations→tool_calls screened→content_filter`); any other halt raises `ValueError` (service converts to `upstream_response_invalid`); `accounting` → `CanonicalUsage` (total computed as sum — reference upstream supplies only the two parts); absent accounting → usage None; invocation payloads re-serialized with `json.dumps` into `arguments_json`.
  - `classify_error` reads `body["fault"]["kind"]`: `throttle→upstream_rate_limited(retryable) overloaded→upstream_unavailable(retryable) screening→content_policy_rejected too_long→context_length_exceeded`; anything else / missing body → `upstream_response_invalid` non-retryable.

- [ ] **Step 1: Write failing tests** — golden request-mapping test (full CanonicalRequest with tools+schema → exact expected invoke body); tool round-trip mapping (assistant tool_calls + tool result message → `operations`/`operation_ref` fields); each halt value; unknown halt raises; each fault kind; usage absent/present.
- [ ] **Step 2–4: fail → implement → pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(gateway): fictional reference_v1_invoke adapter"`

---

### Task 11: CompletionService, event logging, app factory, routes

**Files:**
- Create: `gateway/src/elspeth_llm_gateway/core/events.py`, `gateway/src/elspeth_llm_gateway/core/service.py`, `gateway/src/elspeth_llm_gateway/core/app.py`
- Test: `gateway/tests/test_events.py`, `gateway/tests/test_service.py`, `gateway/tests/test_app.py` (httpx `ASGITransport` + respx for upstream/oauth)

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `events.py`: `SAFE_FIELDS: frozenset[str]` = `{request_id, request_hash, response_hash, contract_major, adapter_name, adapter_version, adapter_api_major, adapter_fingerprint, model_alias, mapping_generation, status, latency_ms, response_bytes, upstream_status_class, oauth_cache_hit, oauth_refresh, oauth_refresh_outcome, error_code, event}`; `def log_event(logger: logging.Logger, event: str, **fields) -> None` raising `ValueError` on any field not in `SAFE_FIELDS`; `def canonical_hash(obj: Any) -> str` (sha256 of sorted-keys compact JSON, first 32 hex chars).
  - `service.py`: `class CompletionService: def __init__(self, config, adapter: AdapterProtocol, upstream: UpstreamClient, logger)`; `async def complete(self, request: ChatRequest, request_id: str) -> dict`:
    1. capability check: request uses tools → `TOOLS` declared; `response_format json_object/json_schema` → matching cap; `seed` → `SEED`; else `GatewayError(CAPABILITY_UNSUPPORTED)`.
    2. `request.model` in `config.model_mappings` else `GatewayError(MODEL_NOT_ALLOWED)`.
    3. `bounds_check`; canonicalize `ChatRequest` → `CanonicalRequest`.
    4. `adapter.build_invoke` (adapter exception → `INTERNAL_ERROR`); `upstream.invoke`.
    5. 2xx → `adapter.parse_success` (exception → `UPSTREAM_RESPONSE_INVALID`); validate: `finish_reason` is a `FinishReason`; tool-call responses only when tools requested.
    6. non-2xx → `adapter.classify_error` → `GatewayError(classification.code)`; adapter exception → `INTERNAL_ERROR`.
    7. Build response via `build_completion_response` with `response_id = "gwcmpl-" + request_id`, `created = int(time.time())`; log one `completion` event (safe fields only).
  - `app.py`: `def create_app(config: GatewayConfig, *, adapter: AdapterProtocol | None = None, upstream_client: httpx.AsyncClient | None = None) -> FastAPI`:
    - default adapter resolution: `reference_v1_invoke` → `ReferenceV1InvokeAdapter`, else importlib entry point group `elspeth_llm_gateway.adapters`; unknown → `ConfigError`.
    - adapter fingerprint: `sha256` of the adapter module's source file bytes (first 16 hex chars), computed at startup.
    - middleware order: request-ID (accept inbound `X-Request-ID` if `^[A-Za-z0-9._-]{1,128}$` else generate `uuid4().hex`; echo header) → contract header check (`X-ELSPETH-LLM-Gateway-Contract` must be `"1"` on `/v1/*` routes; else 400 `contract_mismatch`) → auth (constant-time bearer on `/v1/*`; else 401 `inbound_authentication_failed`).
    - `POST /v1/chat/completions`: read body with `max_body_bytes` cap → `parse_strict_json` → `ChatRequest` (`ValidationError`/`StrictJsonError` → `invalid_request`) → `service.complete`. Every response carries `X-ELSPETH-LLM-Gateway-Contract: 1` and `X-Request-ID`.
    - `GET /healthz` → `200 {"status": "ok"}` (no auth, no other content).
    - `GET /readyz` → `200/503` JSON exactly: `{"ready": bool, "contract_major": 1, "adapter": {"name","version","adapter_api_major","fingerprint"}, "capabilities": [...], "model_aliases": [...], "mapping_generation": "...", "oauth_fixed_lifetime": bool, "errors": ["safe_code", ...]}` — validates config presence, adapter import + `validate_configuration({})`, non-empty mappings, AND contract compatibility: `descriptor().adapter_api_major == ADAPTER_API_MAJOR` else not-ready with error code `adapter_api_incompatible`; NO OAuth call, NO upstream call. `oauth_fixed_lifetime` is `true` iff `oauth_fixed_lifetime_seconds` is configured (spec: fixed-lifetime fallback "is visible in safe readiness metadata").
    - **Middleware ordering caution:** Starlette's `add_middleware` makes the LAST-added middleware OUTERMOST. To execute request-ID → contract → auth, ADD them in reverse order (auth first, request-ID last). The request-ID middleware, being outermost, must stamp `X-Request-ID` and the contract header on EVERY response, including error responses from inner middleware and exception handlers — test: a 401 auth failure still carries both headers.
    - Exception handler: `GatewayError` → envelope with its status; any other exception → `internal_error` envelope, stack trace logged WITHOUT request content.

- [ ] **Step 1: Write failing tests** —
  - events: unknown field raises; `canonical_hash({"b":1,"a":2}) == canonical_hash({"a":2,"b":1})`.
  - service (fake adapter + respx upstream): happy text path returns OpenAI shape; capability mismatch (tools w/o cap) → 422 code `capability_unsupported` BEFORE any upstream call; unknown alias → `model_not_allowed`; adapter `parse_success` raising → `upstream_response_invalid`; usage-absent → no `usage` key.
  - app end-to-end over `ASGITransport`: missing/wrong bearer → 401 envelope; missing contract header → 400 `contract_mismatch`; contract header `"2"` → `contract_mismatch`; unknown JSON field → 400 `invalid_request`; happy path echoes request id + contract header; `/healthz` 200 without auth; `/readyz` 200 with exact key set and no secret material (assert bearer/client-secret strings absent from body text); caplog sweep: run happy path + error path, assert no log record contains the prompt text, bearer, client secret, or upstream body markers.
- [ ] **Step 2–4: fail → implement → pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(gateway): completion service, hardened app surface, metadata-only events"`

---

### Task 12: Deterministic mock stack

**Files:**
- Create: `gateway/mock/__init__.py`, `gateway/mock/oauth.py`, `gateway/mock/upstream.py`, `gateway/mock/stack.py`
- Test: `gateway/tests/test_mock_stack.py`

**Interfaces:**
- Produces:
  - `mock/oauth.py`: `def create_mock_oauth_app(*, client_id: str = "mock-client", client_secret: str = "mock-secret-0123456789abcdef0123456789abcdef", expires_in: int = 3600, fail_next: dict | None = None) -> FastAPI` — `POST /token` validating grant type + both auth methods; issues `mock-token-<counter>` (deterministic counter, no randomness); `fail_next={"status": 500}` makes exactly the next call fail (for refresh tests).
  - `mock/upstream.py`: `def create_mock_upstream_app(*, require_bearer_prefix: str = "mock-token-") -> FastAPI` — `POST /v1/invoke` implementing the fictional schema deterministically: last `conversation` entry's text `"USE_TOOL <op> <json>"` → invocation response with halt `operations`; request `format.kind == "schema"` → body echoing `{"echo": "<last user text>"}` serialized per schema's first property name; request `format.kind == "object"` → text response that IS valid JSON: `json.dumps({"echo": "<last user text>"})` (so `json_object` conformance can assert `json.loads(content)` succeeds); otherwise text response `"MOCK:" + <last user text>`; accounting = `{"input_units": total chars // 4, "output_units": reply chars // 4}`; missing/foreign bearer → 401; text `"TRIGGER_FAULT <kind>"` → `{"fault": {"kind": "<kind>"}}` with status 429 for throttle, 503 overloaded, 400 screening/too_long.
  - `mock/stack.py`: `async def start_local_stack(gateway_port=8787, oauth_port=8788, upstream_port=8789)` and `python -m mock.stack` entry — runs all three uvicorn servers with a fully wired env (documented mock secrets), prints the curl example from the README. First lines of `mock/stack.py`: insert `<gateway>/src` into `sys.path` (the package is not installed during development; compute the path relative to `__file__`).

- [ ] **Step 1: Write failing tests** — mock oauth issues deterministic sequential tokens and honors both auth methods; `fail_next` fails exactly once; mock upstream: text echo, tool invocation trigger, schema format echo, each fault kind, 401 on missing bearer.
- [ ] **Step 2–4: fail → implement → pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(gateway): deterministic mock OAuth and fictional upstream stack"`

---

### Task 13: Conformance kit + root-repo test wiring

**Files:**
- Create: `gateway/conformance/__init__.py`, `gateway/conformance/conftest.py`, `gateway/conformance/test_contract.py`, `gateway/conformance/test_capabilities.py`, `gateway/conformance/test_tools.py`, `gateway/conformance/test_structured.py`, `gateway/conformance/test_errors.py`, `gateway/conformance/test_identity.py`
- Create (ROOT repo): `tests/gateway_runtime/__init__.py`, `tests/gateway_runtime/conftest.py`, `tests/gateway_runtime/test_inprocess_conformance.py`

**Interfaces:**
- Consumes: everything; conformance talks ONLY HTTP (no gateway-internals imports beyond the conftest app fixture) so it can qualify derived images.
- Produces:
  - `gateway/conformance/conftest.py`: FIRST lines: a self-contained `sys.path` shim inserting `<gateway>/src` (computed relative to `__file__`), skipped when `elspeth_llm_gateway` is already importable — the conformance kit must run as a bare subprocess AND standalone against a derived image, so it can NEVER rely on an outer conftest for imports. Fixture `gateway_client` — if env `GATEWAY_CONFORMANCE_URL` is set, a real `httpx.AsyncClient(base_url=...)` (image-qualification mode; the mock stack must already be reachable per the README); otherwise in-process: build mock oauth + mock upstream apps and dispatch to them with ONE custom `httpx.AsyncBaseTransport` that routes by request host (e.g. `oauth.mock` vs `upstream.mock`) to two `ASGITransport` instances — `create_app`'s single `upstream_client` parameter serves BOTH the TokenManager and the UpstreamClient, so the router transport is what makes in-process mode work. Also fixtures `bearer` and `declared_capabilities` (parsed from `/readyz`).
  - Conformance tests keyed by declared capabilities: every test first reads `/readyz`; tests for undeclared capabilities assert the gateway REJECTS the feature with `capability_unsupported` (claiming-and-failing is the only failure). Coverage: contract header both ways incl. error responses; auth strictness INCLUDING credential-channel rejection (`?access_token=<bearer>` query param and `Cookie: token=<bearer>` header, each with NO Authorization header → 401 `inbound_authentication_failed`); unknown-field rejection; text completion happy path; `json_object` (assert `json.loads(content)` succeeds) and `json_schema` modes; tool call + tool result round trip; error envelope shape for `model_not_allowed`, `capability_unsupported`, `invalid_request`; `/readyz` identity fields present and stable across two calls.
  - Root `tests/gateway_runtime/conftest.py`:

```python
import sys
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parents[2]
for entry in (_REPO_ROOT / "gateway" / "src", _REPO_ROOT / "gateway"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))
```

  - Root `tests/gateway_runtime/test_inprocess_conformance.py`: runs the full conformance suite as a SUBPROCESS — `subprocess.run([sys.executable, "-m", "pytest", str(_REPO_ROOT / "gateway" / "conformance"), "-q"], cwd=str(_REPO_ROOT / "gateway"), ...)` — and asserts returncode 0, printing captured output on failure. The subprocess inherits NOTHING from the root conftest: imports work because the conformance conftest carries its own sys.path shim (above), and `cwd=gateway/` plus gateway's own `[tool.pytest.ini_options]` pin the rootdir so root addopts/pythonpath never leak. A second root test does the same for `gateway/tests`. This is how the CI-equivalent `pytest tests/` gate covers the gateway without importing it into the ELSPETH test session.

- [ ] **Step 1: Write failing tests** — the conformance files ARE the tests; write them, run `cd gateway && python -m pytest conformance -q` and watch the specific gaps fail (any failure at this point is a Task 1–12 defect: fix the defect, never the conformance assertion).
- [ ] **Step 2: Run root suite subset** — `pytest tests/gateway_runtime/ -q` from repo root: green.
- [ ] **Step 3: Full regression** — `pytest tests/ -q` from repo root: green (no ELSPETH behavior touched; failures here are wiring mistakes).
- [ ] **Step 4: Commit** — `git add gateway/conformance tests/gateway_runtime && git commit -m "test(gateway): conformance kit and root CI-gate wiring"`

---

### Task 14: Container image, adapter scaffold, README

**Files:**
- Create: `gateway/Dockerfile`, `gateway/README.md`, `gateway/scaffold/adapter_template/` (descriptor/config/request/response/errors/test module templates with `yourorg_adapter` placeholders)
- Test: `gateway/tests/test_dockerfile_policy.py` (static assertions on the Dockerfile text — no docker daemon dependency in unit tests)

**Interfaces:**
- Produces: multi-stage Dockerfile — builder installs `gateway/` wheel into a venv at `/venv`; final stage `python:3.12-slim` (digest-pinned tag), copies `/venv` only, `USER 65532:65532`, `ENV PYTHONDONTWRITEBYTECODE=1 PATH="/venv/bin:$PATH"`, no shell entrypoint: `ENTRYPOINT ["/venv/bin/python", "-m", "uvicorn", "elspeth_llm_gateway.core.app:build", "--host", "0.0.0.0", "--port", "8787", "--factory", "--timeout-graceful-shutdown", "30"]` with `build()` = `create_app(load_config(os.environ))` — the ABSOLUTE `/venv/bin/python` is mandatory (bare `python` resolves to the system interpreter without the venv). `EXPOSE 8787`, read-only-rootfs compatible (no writes outside `/tmp`). README: quick-start (mock stack), onboarding path 1–6 from the design, derived-image instructions (`FROM elspeth-llm-gateway:<digest>` + `pip install yourorg-adapter==X.Y.Z` at BUILD time only), conformance-against-image instructions (`GATEWAY_CONFORMANCE_URL`).
- Also add: `app.py` gains `def build() -> FastAPI` factory reading `os.environ` (tested via env-injected unit test).

- [ ] **Step 1: Write failing tests** — Dockerfile policy: contains `USER 65532`, `ENTRYPOINT` uses `/venv/bin/python` and includes `--timeout-graceful-shutdown`, does NOT contain `pip install` in the FINAL stage, no `ADD`, pinned base image tag; `build()` factory returns an app when a valid env is set and raises `ConfigError` (safe messages) when not.
- [ ] **Step 2–4: fail → implement → pass.** **REQUIRED container verification** (docker daemon is present in this environment): `docker build gateway/ -t elspeth-llm-gateway:dev`, start the mock stack + the container wired to it (host networking or host.docker.internal), run the conformance kit with `GATEWAY_CONFORMANCE_URL` against the container, and record the run output in the task report — this is the master plan's "conformance green against the built container" phase gate. The behavioral graceful-shutdown drain test is deferred to this container step only (record outcome); if the daemon is genuinely unavailable at execution time, report DONE_WITH_CONCERNS naming it.
- [ ] **Step 5: Commit** — `git commit -m "feat(gateway): container image, adapter scaffold, onboarding README"`

---

## Self-review checklist (run after Task 14)

1. Every runtime-design "Verification and acceptance" bullet maps to a test: schemas/unknown-field/bounds (T1,T5,T13), capabilities (T11,T13), normalization forms (T10,T13), alias allowlist + generation (T6,T11), both OAuth methods (T8,T12), expiry/skew/single-flight/concurrency (T8), one 401 replay (T9), no replay otherwise (T9), non-leakage (T4,T6,T8,T11), adapter Authorization/origin-escape/bounds attempts (T3,T9), shutdown/in-flight bounds (T14 uvicorn defaults — NOTE: bounded in-flight drain is uvicorn `--timeout-graceful-shutdown`; add flag to ENTRYPOINT), healthz/readyz/contract mismatch (T11,T13), derived-image conformance (T13 URL mode, exercised for real in Phase 5).
2. Grep the plan for TBD/TODO/"appropriate" — none.
3. Type names consistent across tasks (CanonicalRequest fields, Bounds home in contract.py, error-code strings).
