# Composer-plane reasoning effort — design

Date: 2026-08-05
Ticket: elspeth-dc459d438e
Status: approved (operator, 2026-08-05)

## Problem

All composer roles now run reasoning-capable models
(`openrouter/anthropic/claude-sonnet-5` primary, `claude-opus-4-8` advisor on
the local install; Bedrock- and Azure-hosted deployments are in scope), but
the composer call path passes no reasoning hint of any kind. An unhinted
hybrid model picks its own thinking budget per call, and that budget grows
with the accumulated transcript. Measured consequence (session `ff737fa4…`,
journal 2026-07-27 → 2026-08-05): tutorial planner attempts average 23–48s
with tails to 120s — chronic since the 2026-07-29 model switch, worst on
late-loop discovery/candidate calls. The operator direction is to keep
reasoning models everywhere on the composer plane and enable the
functionality properly, not to disable thinking.

## Decision

### 1. Configuration

Three new env-driven `ComposerSettings` fields, closed vocabulary
`none | low | medium | high` (`none` = send no hint — today's behavior and
the per-deployment opt-out):

| Field | Default | Governs |
|---|---|---|
| `composer_discovery_reasoning_effort` | `low` | discovery-phase planner calls (tool choreography) |
| `composer_candidate_reasoning_effort` | `high` | candidate/plan-producing calls |
| `composer_advisor_reasoning_effort` | `medium` | advisor calls (all passes) |

Validation at settings parse (Literal). Env names follow the existing
`ELSPETH_WEB__COMPOSER_*` convention.

### 2. Mechanism — provider-agnostic with one carve-out

A single owned helper beside `_apply_endpoint_kwargs` in
`web/composer/service.py`:

```python
def _apply_reasoning_kwargs(kwargs, *, model: str, effort: str | None) -> None
```

- `effort` in `(None, "none")` → no-op.
- `model.startswith("openrouter/")` → merge
  `kwargs["extra_body"]["reasoning"] = {"effort": effort}` (OpenRouter-native
  form). Rationale: litellm gates its standard `reasoning_effort` param on
  `litellm.supports_reasoning(model)`, whose registry is stale for
  `openrouter/anthropic/claude-sonnet-5` (verified False on litellm 1.85.0
  while the native-route `anthropic/claude-sonnet-5` is True) — the standard
  param would be silently dropped for exactly our configured model.
- OpenAI-surface models (bare aliases like `gpt-5.5` and `openai/`
  prefixes) → **no hint** (implementation deviation, 2026-08-05): litellm's
  `responses_api_bridge_check` routes GPT-5.4+ chat calls carrying
  `tools` + `reasoning_effort` to `/v1/responses`, which
  chat-completions-only gateways — including ELSPETH's own, whose
  `ChatRequest` contract is deliberately `extra="forbid"` — do not serve
  (404 reproduced by `tests/integration/web/composer/test_composer_against_gateway.py`).
  These models stay unhinted until the gateway contract carries the field
  (elspeth-9a46553771).
- otherwise → `kwargs["reasoning_effort"] = effort`. litellm translates per
  provider: Bedrock → Anthropic `thinking` budgets, Azure → native effort
  (Azure serves the Responses API, so the same bridge is harmless there),
  `anthropic/` → thinking. This is the entire Bedrock/Azure enablement —
  no provider-specific code beyond the two carve-outs above.

A transform-level unit test pins that the hint survives litellm's OpenRouter
request mapping (`OpenrouterConfig.map_openai_params` must not clobber or
drop our `extra_body.reasoning`) so a litellm upgrade that changes the
registry or the extra_body handling fails loudly.

### 3. Call-site classification

Every composer-plane completion call is classified
`{discovery, candidate, advisor}` and receives the matching knob. The
compose-loop planner already knows its phase; guided chat-solver and
tool-dispatch fanout sites are classified individually during
implementation planning (read each site; do not guess).

Excluded, deliberately:

- `sessions/_auto_title.py` — 40-token labeling call; thinking would trip its
  provider truncation and it has no reasoning need.
- `composer/boot_probe.py` — liveness ping.
- `web/_aws_ecs_acceptance/bedrock.py` — acceptance harness, not the
  composer plane.

### 4. Audit and failure posture

- No audit schema change: provider-call audit already carries
  `reasoning_content` and `finish_reason`. Effort settings are system
  config, not user-selected run config, so no Landscape rows (audit
  doctrine).
- A model that rejects reasoning params surfaces as the normal provider
  error path; `none` opts a deployment out per phase.
- Boot logs a warning (never fails) when a configured composer model fails
  `litellm.supports_reasoning` — registry gaps exist (see §2) and the
  warning names the model string form.

## Testing

- Settings: parse, defaults (`low`/`high`/`medium`), vocab rejection.
- Helper: kwargs shape per provider prefix (`openrouter/…`, `bedrock/…`,
  `azure/…`, `anthropic/…`), `none`/None no-op, extra_body merge (does not
  clobber existing extra_body keys).
- litellm transform pin: OpenRouter mapping preserves the reasoning hint
  end-to-end at the request-shaping layer.
- Per-site capture tests (sampling-config style): each classified call site
  passes the right knob; auto-title and boot-probe tests unchanged (proves
  exclusion).
- Live proof rides battery round 3: tutorial `planner_attempt` discovery
  gaps in the journal should collapse from the 30–50s band.

## Alternatives rejected

- OpenRouter-only `extra_body` everywhere — fails the Bedrock/Azure
  requirement.
- ELSPETH-owned per-provider effort→native-param mapping — duplicates
  litellm's translation layer and becomes permanent maintenance.
