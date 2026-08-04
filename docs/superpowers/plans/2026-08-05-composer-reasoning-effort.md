# Composer Reasoning Effort Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-phase/per-role reasoning-effort hints on every composer-plane LLM call, working across OpenRouter, Bedrock, and Azure.

**Architecture:** A new neutral module `web/composer/reasoning.py` owns the effort vocabulary and the provider-aware kwargs helper (neutral because `pipeline_planner.py` must not import `service.py` — the import runs the other way). Settings gain three effort knobs; the planner selects discovery vs candidate effort by its existing phase rule; freeform loop/advisor/chat-solver sites each apply their fixed knob. Auto-title and boot probe are untouched.

**Tech Stack:** Python 3.12+, pydantic settings, litellm 1.85.0 (OpenRouter `extra_body` carve-out), pytest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-05-composer-reasoning-effort-design.md` (ticket elspeth-dc459d438e).
- Effort vocabulary is exactly `"none" | "low" | "medium" | "high"`; `"none"` means "add nothing to kwargs".
- Defaults: discovery=`low`, candidate=`high`, advisor=`medium`.
- `openrouter/`-prefixed models MUST get `extra_body={"reasoning": {"effort": ...}}`, never `reasoning_effort` (litellm 1.85.0 `supports_reasoning` registry is stale for `openrouter/anthropic/claude-sonnet-5` and silently drops the standard param).
- All other models get `reasoning_effort` (litellm translates: Bedrock→thinking budgets, Azure→native effort).
- Never touch `sessions/_auto_title.py`, `composer/boot_probe.py`, or `web/_aws_ecs_acceptance/bedrock.py`.
- Worktree: `.claude/worktrees/composer-reasoning-effort`; run tests with `PYTHONPATH=$PWD/src` (shared venv editable path points at the main checkout).
- Planner log-phase → knob mapping: `{discovery, prose}` → discovery knob; `{candidate, repair, hatch}` → candidate knob.

---

### Task 1: `reasoning.py` — vocabulary, helper, boot warning, litellm pin

**Files:**
- Create: `src/elspeth/web/composer/reasoning.py`
- Test: `tests/unit/web/composer/test_reasoning_kwargs.py`

**Interfaces:**
- Produces: `ReasoningEffort` (type alias `Literal["none", "low", "medium", "high"]`), `apply_reasoning_kwargs(kwargs: dict[str, object], *, model: str, effort: str | None) -> None`, `warn_if_not_reasoning_capable(*, model: str, role: str, effort: str) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
"""Provider-aware reasoning-effort kwargs (elspeth-dc459d438e)."""

from __future__ import annotations

import pytest

from elspeth.web.composer.reasoning import apply_reasoning_kwargs


@pytest.mark.parametrize("effort", [None, "none"])
def test_none_effort_is_a_no_op(effort: str | None) -> None:
    kwargs: dict[str, object] = {"model": "openrouter/anthropic/claude-sonnet-5"}
    apply_reasoning_kwargs(kwargs, model="openrouter/anthropic/claude-sonnet-5", effort=effort)
    assert kwargs == {"model": "openrouter/anthropic/claude-sonnet-5"}


def test_openrouter_models_use_native_extra_body_reasoning() -> None:
    kwargs: dict[str, object] = {}
    apply_reasoning_kwargs(kwargs, model="openrouter/anthropic/claude-sonnet-5", effort="low")
    assert kwargs == {"extra_body": {"reasoning": {"effort": "low"}}}
    assert "reasoning_effort" not in kwargs


def test_openrouter_extra_body_merge_does_not_clobber_existing_keys() -> None:
    kwargs: dict[str, object] = {"extra_body": {"transforms": ["middle-out"]}}
    apply_reasoning_kwargs(kwargs, model="openrouter/anthropic/claude-opus-4-8", effort="medium")
    assert kwargs["extra_body"] == {"transforms": ["middle-out"], "reasoning": {"effort": "medium"}}


@pytest.mark.parametrize(
    "model",
    [
        "bedrock/us.anthropic.claude-sonnet-4-20250514-v1:0",
        "azure/gpt-5-mini",
        "anthropic/claude-sonnet-5",
    ],
    ids=["bedrock", "azure", "anthropic-native"],
)
def test_non_openrouter_models_use_standard_reasoning_effort(model: str) -> None:
    kwargs: dict[str, object] = {}
    apply_reasoning_kwargs(kwargs, model=model, effort="high")
    assert kwargs == {"reasoning_effort": "high"}


def test_invalid_effort_raises() -> None:
    with pytest.raises(ValueError, match="reasoning effort"):
        apply_reasoning_kwargs({}, model="azure/gpt-5-mini", effort="extreme")


def test_litellm_openrouter_transform_preserves_extra_body_reasoning() -> None:
    """Pin the carve-out's survival through litellm's request shaping.

    litellm's OpenrouterConfig rebuilds ``extra_body`` from OpenRouter-only
    params during ``map_openai_params``. This pin fails if a litellm upgrade
    starts dropping caller-supplied ``extra_body["reasoning"]`` — the exact
    silent-drop this module exists to avoid.
    """
    from litellm.llms.openrouter.chat.transformation import OpenrouterConfig

    mapped = OpenrouterConfig().map_openai_params(
        non_default_params={"extra_body": {"reasoning": {"effort": "low"}}},
        optional_params={"extra_body": {"reasoning": {"effort": "low"}}},
        model="openrouter/anthropic/claude-sonnet-5",
        drop_params=False,
    )
    body = mapped.get("extra_body", {})
    reasoning = body.get("reasoning") if isinstance(body, dict) else None
    assert reasoning == {"effort": "low"}, (
        "litellm's OpenRouter map_openai_params no longer preserves "
        f"caller extra_body reasoning; got mapped={mapped!r}"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd .claude/worktrees/composer-reasoning-effort && PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/unit/web/composer/test_reasoning_kwargs.py -q -p no:xdist`
Expected: FAIL — `ModuleNotFoundError: elspeth.web.composer.reasoning`

NOTE: if the litellm pin test (last one) fails on the real litellm because
`map_openai_params` clobbers caller extra_body, that is a DESIGN INPUT, not a
broken test: change `apply_reasoning_kwargs` so the OpenRouter branch ALSO
sets `kwargs["reasoning_effort"]` — wait, no: instead pass the reasoning
object as a top-level `kwargs["reasoning"]` entry, which litellm forwards to
OpenRouter as an unrecognized-but-forwarded provider param — and update the
pin test to assert whichever form verifiably reaches the request body. Do not
proceed past Task 1 until the pin test proves one working form.

- [ ] **Step 3: Write the module**

```python
"""Reasoning-effort hints for composer-plane LLM calls (elspeth-dc459d438e).

One provider-agnostic mechanism with one carve-out: litellm's standard
``reasoning_effort`` param is translated per provider (Bedrock -> Anthropic
thinking budgets, Azure -> native effort), but litellm gates it on its
``supports_reasoning`` model registry, which is stale for
``openrouter/anthropic/claude-sonnet-5`` (verified False on litellm 1.85.0
while ``anthropic/claude-sonnet-5`` is True). ``openrouter/`` models
therefore get OpenRouter's native ``extra_body`` reasoning object, which
bypasses the registry gate entirely.

Neutral module: ``pipeline_planner`` consumes this and must not import
``service`` (the import runs the other way).
"""

from __future__ import annotations

import logging
from typing import Literal

ReasoningEffort = Literal["none", "low", "medium", "high"]

_EFFORT_VALUES = ("none", "low", "medium", "high")

_logger = logging.getLogger(__name__)


def apply_reasoning_kwargs(kwargs: dict[str, object], *, model: str, effort: str | None) -> None:
    """Apply a reasoning-effort hint to LiteLLM call kwargs, in place.

    ``None`` and ``"none"`` add nothing — the unhinted status quo and the
    per-deployment opt-out. Unknown effort strings are a programmer error at
    the settings boundary and raise rather than silently under- or
    over-thinking.
    """
    if effort is None or effort == "none":
        return
    if effort not in _EFFORT_VALUES:
        raise ValueError(f"unknown reasoning effort {effort!r}; expected one of {_EFFORT_VALUES}")
    if model.startswith("openrouter/"):
        extra_body = kwargs.setdefault("extra_body", {})
        if not isinstance(extra_body, dict):
            raise ValueError("extra_body kwarg must be a dict to carry the reasoning hint")
        extra_body["reasoning"] = {"effort": effort}
        return
    kwargs["reasoning_effort"] = effort


def warn_if_not_reasoning_capable(*, model: str, role: str, effort: str) -> None:
    """Boot-time advisory when a hinted model fails litellm's registry check.

    Never raises: the registry has known gaps (the openrouter/ carve-out
    exists because of one), so this is a log line for operators, not a gate.
    """
    if effort == "none":
        return
    try:
        import litellm

        capable = bool(litellm.supports_reasoning(model=model))
    except Exception:  # registry lookup is advisory; never block boot
        return
    if not capable:
        _logger.warning(
            "composer %s model %r is not reasoning-capable per litellm's registry; "
            "reasoning effort %r may be dropped or rejected by the provider "
            "(registry gaps exist for openrouter/ model strings — the hint is "
            "still sent via OpenRouter's native form)",
            role,
            model,
            effort,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: same command as Step 2. Expected: all PASS (see Step 2 NOTE if the litellm pin fails — resolve the working form before continuing).

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web/composer/reasoning.py tests/unit/web/composer/test_reasoning_kwargs.py
git commit -m "feat(composer): provider-aware reasoning-effort kwargs helper (elspeth-dc459d438e)"
```

---

### Task 2: Settings — three knobs + advisor completion budget

**Files:**
- Modify: `src/elspeth/web/config.py` (WebSettings, composer block near line 195; advisor budget near line 299)
- Modify: `src/elspeth/web/composer/protocol.py` (ComposerSettings property block, near lines 1083–1131)
- Test: `tests/unit/web/test_config_reasoning_effort.py`

**Interfaces:**
- Consumes: `ReasoningEffort` from Task 1.
- Produces: `WebSettings.composer_discovery_reasoning_effort` / `composer_candidate_reasoning_effort` / `composer_advisor_reasoning_effort` (all `ReasoningEffort`), and matching `ComposerSettings` protocol properties. `composer_advisor_max_completion_tokens` default becomes `8192`.

- [ ] **Step 1: Write the failing tests**

```python
"""Reasoning-effort settings knobs (elspeth-dc459d438e)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from elspeth.web.config import WebSettings


def _settings(**overrides: object) -> WebSettings:
    # Mirror the construction pattern used by the nearest existing
    # WebSettings test module (copy its minimal required-field fixture).
    return WebSettings(**overrides)


def test_reasoning_effort_defaults_are_low_high_medium() -> None:
    settings = _settings()
    assert settings.composer_discovery_reasoning_effort == "low"
    assert settings.composer_candidate_reasoning_effort == "high"
    assert settings.composer_advisor_reasoning_effort == "medium"


def test_reasoning_effort_rejects_unknown_values() -> None:
    with pytest.raises(ValidationError):
        _settings(composer_discovery_reasoning_effort="extreme")


def test_none_is_a_valid_opt_out() -> None:
    settings = _settings(composer_candidate_reasoning_effort="none")
    assert settings.composer_candidate_reasoning_effort == "none"


def test_advisor_completion_budget_default_fits_thinking() -> None:
    # Anthropic thinking budgets have a 1024-token floor that must fit
    # INSIDE max_tokens; the old 1500 default left medium-effort advisor
    # calls with an illegal/starved budget.
    assert _settings().composer_advisor_max_completion_tokens == 8192
```

If `WebSettings()` requires fields, copy the minimal construction from the
nearest existing config test (search `tests/unit/web` for `WebSettings(`)
into `_settings` — keep the four test bodies unchanged.

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/unit/web/test_config_reasoning_effort.py -q -p no:xdist`
Expected: FAIL — unknown field / default mismatch (1500).

- [ ] **Step 3: Implement**

In `web/config.py`, next to the composer block (after `composer_seed`-adjacent fields), following house comment style:

```python
    # Reasoning-effort hints for the composer plane (elspeth-dc459d438e).
    # All composer roles run reasoning-capable models; these knobs bound the
    # thinking budget per call class instead of letting the model pick an
    # unhinted budget that grows with the transcript (measured 120s tails on
    # the tutorial planner). "none" sends no hint — the pre-feature
    # behaviour and the opt-out for non-reasoning deployments.
    composer_discovery_reasoning_effort: ReasoningEffort = "low"
    composer_candidate_reasoning_effort: ReasoningEffort = "high"
    composer_advisor_reasoning_effort: ReasoningEffort = "medium"
```

Import `ReasoningEffort` from `elspeth.web.composer.reasoning`. Change the
advisor budget default at line ~299:

```python
    # 8192 (was 1500): with advisor reasoning enabled the thinking budget
    # shares max_tokens, and Anthropic thinking has a 1024-token floor that
    # must fit inside it — 1500 left medium effort illegal or starved.
    composer_advisor_max_completion_tokens: int = Field(default=8192, ge=1)
```

In `web/composer/protocol.py`, add to the ComposerSettings property block
(same style as `composer_temperature`):

```python
    @property
    def composer_discovery_reasoning_effort(self) -> str: ...

    @property
    def composer_candidate_reasoning_effort(self) -> str: ...

    @property
    def composer_advisor_reasoning_effort(self) -> str: ...
```

- [ ] **Step 4: Run tests to verify they pass**

Same command; expected PASS. Also run the existing config suite:
`PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/unit/web/test_config*.py -q -p no:xdist`
If an existing test pins `composer_advisor_max_completion_tokens == 1500`,
update that pin to 8192 with the same rationale comment (locked-in buggy
expectation — the structural change is deliberate).

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web/config.py src/elspeth/web/composer/protocol.py tests/unit/web/test_config_reasoning_effort.py
git commit -m "feat(web): reasoning-effort settings knobs + advisor budget fit (elspeth-dc459d438e)"
```

---

### Task 3: Planner — phase-mapped effort

**Files:**
- Modify: `src/elspeth/web/composer/pipeline_planner.py` (PlannerModelConfig at ~295; kwargs build at ~2504–2530)
- Modify: `src/elspeth/web/composer/service.py` (three `PlannerModelConfig(...)` instantiations at ~2893, ~3300, ~3597; boot warning at init near `self._model = settings.composer_model` line ~1564)
- Test: `tests/unit/web/composer/test_planner_reasoning_effort.py`

**Interfaces:**
- Consumes: `apply_reasoning_kwargs`, `warn_if_not_reasoning_capable` (Task 1); settings knobs (Task 2).
- Produces: `PlannerModelConfig.discovery_reasoning_effort: str` and `PlannerModelConfig.candidate_reasoning_effort: str` (new required fields, no defaults — every constructor names them).

- [ ] **Step 1: Extend PlannerModelConfig**

Add two fields beside `temperature`/`seed`:

```python
    discovery_reasoning_effort: str
    candidate_reasoning_effort: str
```

- [ ] **Step 2: Apply at the kwargs site by phase rule**

At the kwargs build (~2504), after the temperature/seed lines, select the
knob with the SAME predicates that drive the `planner_attempt` log's
`phase` label (`is_hatch_turn`, `repair_count`, and the discovery-turn
condition — read the surrounding loop to bind them exactly; the mapping
rule is fixed by the Global Constraints: log-phase `{discovery, prose}` →
`discovery_reasoning_effort`, `{candidate, repair, hatch}` →
`candidate_reasoning_effort`):

```python
            apply_reasoning_kwargs(
                kwargs,
                model=effective_model,
                effort=(
                    model_config.candidate_reasoning_effort
                    if turn_is_candidate_class  # bind to the loop's real predicate
                    else model_config.discovery_reasoning_effort
                ),
            )
```

Import `apply_reasoning_kwargs` from `elspeth.web.composer.reasoning` at
module top (neutral module — no service import cycle).

- [ ] **Step 3: Thread settings at all three service instantiations**

Each `PlannerModelConfig(...)` in `service.py` gains:

```python
                discovery_reasoning_effort=self._settings.composer_discovery_reasoning_effort,
                candidate_reasoning_effort=self._settings.composer_candidate_reasoning_effort,
```

- [ ] **Step 4: Boot warning at service init**

Immediately after `self._model = settings.composer_model` (~1564):

```python
        warn_if_not_reasoning_capable(
            model=settings.composer_model,
            role="primary",
            effort=settings.composer_candidate_reasoning_effort,
        )
        warn_if_not_reasoning_capable(
            model=settings.composer_advisor_model,
            role="advisor",
            effort=settings.composer_advisor_reasoning_effort,
        )
```

- [ ] **Step 5: Write capture tests**

In `tests/unit/web/composer/test_planner_reasoning_effort.py`, follow the
existing planner-test fixture pattern (search the planner test module for
how `PlannerModelConfig` is built with a fake `completion`): build a config
with `discovery_reasoning_effort="low"`, `candidate_reasoning_effort="high"`,
model `openrouter/anthropic/claude-sonnet-5`, drive one discovery turn and
one candidate turn, and assert the fake completion captured
`extra_body == {"reasoning": {"effort": "low"}}` on the discovery call and
`{"effort": "high"}` on the candidate call. Also fix every existing
`PlannerModelConfig(` construction in the test tree (grep; the new fields
are required) by adding both fields explicitly — `"none"` where the test
does not exercise reasoning.

- [ ] **Step 6: Run planner + composer suites**

`PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/unit/web/composer/ -q`
Expected: PASS (constructor-fix fallout resolved).

- [ ] **Step 7: Commit**

```bash
git add src/elspeth/web/composer/pipeline_planner.py src/elspeth/web/composer/service.py tests/unit/web/composer/
git commit -m "feat(composer): phase-mapped planner reasoning effort + boot advisory (elspeth-dc459d438e)"
```

---

### Task 4: Freeform loop + advisor sites

**Files:**
- Modify: `src/elspeth/web/composer/service.py` (`_call_tool_llm` ~5518, `_call_text_llm` ~5556, advisor kwargs ~6062)
- Test: `tests/unit/web/composer/test_service_reasoning_effort.py`

**Interfaces:**
- Consumes: Task 1 helper, Task 2 knobs.
- Produces: nothing new — call-site wiring only.

- [ ] **Step 1: Wire the three sites**

In `_call_tool_llm` and `_call_text_llm`, after the seed line and BEFORE
`_apply_endpoint_kwargs`:

```python
            apply_reasoning_kwargs(kwargs, model=self._model, effort=self._settings.composer_discovery_reasoning_effort)
```

(Freeform tool-loop and prose calls are interactive tool choreography —
discovery class per the spec's classification.)

At the advisor kwargs build (~6062), same position:

```python
        apply_reasoning_kwargs(kwargs, model=advisor_model, effort=self._settings.composer_advisor_reasoning_effort)
```

- [ ] **Step 2: Capture tests**

Follow the sampling-config test pattern (fake `_litellm_acompletion` via
monkeypatch capturing kwargs; find the existing service-level test that
stubs it — grep `_litellm_acompletion` under `tests/unit/web/composer/`).
Three tests: tool-loop call carries the discovery hint; text call carries
the discovery hint; advisor call carries the advisor hint on an
`openrouter/` model via `extra_body` and on a `bedrock/` model via
`reasoning_effort` (parametrize the advisor one across both model strings).

- [ ] **Step 3: Run, verify, commit**

`PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/unit/web/composer/test_service_reasoning_effort.py tests/unit/web/composer/ -q`

```bash
git add src/elspeth/web/composer/service.py tests/unit/web/composer/test_service_reasoning_effort.py
git commit -m "feat(composer): freeform-loop and advisor reasoning hints (elspeth-dc459d438e)"
```

---

### Task 5: Guided chat solver

**Files:**
- Modify: `src/elspeth/web/composer/guided/chat_solver.py` (kwargs builders at ~1976, ~2371, ~3040, ~3409)
- Test: extend the solver's existing kwargs/capture test module (grep `chat_solver` under `tests/unit/web/composer/guided/`)

**Interfaces:**
- Consumes: Task 1 helper; discovery knob (Task 2).
- Produces: solver request/param threading — mirror EXACTLY how `temperature` reaches each builder (request-object field at ~1976; direct params at the others). Where a request dataclass carries `temperature`, add `reasoning_effort: str` beside it and populate it at the service-side constructor from `composer_discovery_reasoning_effort`; where the builder takes bare params, add a `reasoning_effort: str` parameter the same way.

- [ ] **Step 1: Thread + apply at all four builders**

After each builder's seed/temperature lines:

```python
    apply_reasoning_kwargs(kwargs, model=<the builder's model variable>, effort=<the threaded effort>)
```

(All guided-chat solving is discovery-class per the spec.)

- [ ] **Step 2: Capture tests + constructor fallout**

Extend the solver's existing fake-completion tests: one capture assertion
per builder path proving the hint lands (openrouter model → `extra_body`).
Fix any request-dataclass constructions in tests (new required field →
pass `"none"` where reasoning is not the subject).

- [ ] **Step 3: Run guided suite, commit**

`PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/unit/web/composer/guided/ -q`

```bash
git add src/elspeth/web/composer/guided/chat_solver.py src/elspeth/web/composer/service.py tests/
git commit -m "feat(composer): guided chat-solver reasoning hints (elspeth-dc459d438e)"
```

---

### Task 6: Reconciliation gates + merge

- [ ] **Step 1: Full CI-equivalent suite in the worktree**

`PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/ -q -n 12`
Expected: green (baseline had 37,570 pass / 27 skip / 1 trust-tier xfail).

- [ ] **Step 2: Gates**

`elspeth-lints check` (exit 0) and `.venv/bin/python scripts/wardline_gate.py`
(exit 0, non-inert) from the worktree.

- [ ] **Step 3: Merge --no-ff into release/0.7.2 from the MAIN checkout**

```bash
git -C /home/john/elspeth fetch origin release/0.7.2   # check for concurrent movement
git -C /home/john/elspeth merge --no-ff claude/composer-reasoning-effort \
  -m "merge: composer-plane reasoning effort (elspeth-dc459d438e)"
```

If release moved since 90d5508fd, rebase-or-merge decision: merge the moved
tip into the branch first, re-run the composer suites, then `--no-ff` back.

- [ ] **Step 4: Push (gh user johnm-dta, then switch back), filigree to verifying with fix_verification (battery round-3 journal cadence check: tutorial planner_attempt discovery gaps must drop from the 30–50s band), tracker checkpoint row, remove the worktree.**

- [ ] **Step 5: Enable on the local install (standing dev-server grant):** append the three `ELSPETH_WEB__COMPOSER_*_REASONING_EFFORT` knobs to `deploy/elspeth-web.env` as explicit values matching the defaults, `systemctl restart elspeth-web.service`, and verify boot logs show no reasoning-capability warning and the service comes up.
