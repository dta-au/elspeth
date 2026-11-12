# Composer live audit harness

Drives a real local `elspeth web` composer through multi-turn, demo-shaped
scenarios. It captures every provider request and response, including model
reasoning where the provider returns it, and turns them into a findings digest
and the tool-call register. Used for the 2026-11-12 desk audit
(`docs/development/composer-desk-audit-2026-11-12/`).

This is a development harness, not product code. Instances run with
`ELSPETH_WEB__COMPOSER_PROMPT_GUARD_MODE=debug-when-unconfigured`, local auth
and SQLite. Each instance grants its scratch user the `user` role directly in
its own session store.

## Run

```bash
source .venv/bin/activate
export OPENROUTER_API_KEY=...            # or whichever provider your composer models use
export ELSPETH_WEB__COMPOSER_MODEL=openrouter/deepseek/deepseek-v4.1-flash
export ELSPETH_WEB__COMPOSER_ADVISOR_MODEL=openrouter/z-ai/glm-5.3
export ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS=10 ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS=24
export ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS=240 ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE=20
export ELSPETH_WEB__SECRET_KEY=$(openssl rand -hex 32) ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY=$(openssl rand -base64 32)

evals/composer-live-audit/instance.sh 8452 lv1            # broad plugin allowlist
evals/composer-live-audit/instance.sh 8456 core1 --core   # default allowlist (required core only)
evals/composer-live-audit/run_scenarios.sh 8452 lv1 s01-orders-edits s02-feedback-classify
```

Outputs go to `evals/composer-live-audit/runs/`, which is gitignored:

| File | Contents |
|---|---|
| `runs/sessions/<scenario>/transcript.md` | Per turn: what the model was sent (new messages only), its reasoning, content and tool calls, what the user saw, and the validation result |
| `runs/sessions/<scenario>/summary.json` | Per-turn latency, provider calls, terminal status and reply |
| `runs/digest.md` | All sessions: reasoning confusion markers, failed tool results, and advisor FLAGGED/WITHHOLD bodies |
| `runs/register.md` | The tool-call register joined with live call counts and failures |

Stop an instance by killing its PID (printed at start). Keep one driver per
instance: provider calls are attributed to turns by time window.

## Scenarios

`scenarios/*.json` holds `{"name", "turns": [...], "attachments": [...]}`. A
turn is a string or `{"content", "reviews": "accept"|"leave", "proposals":
"accept"|"reject"|"leave"}`. By default the driver accepts pending
interpretation reviews and proposals after each turn, as a cooperative user
would. The `sNN` scenarios are battery-corpus and demo phrasings, `cNN` are
for the default allowlist, `pNN` are capability questions, and `rNN` repeats
the headline flow.

## Files

| File | Role |
|---|---|
| `instance.sh` | Start one isolated instance (user, role grant, capture hook) |
| `launch_web.py` | `elspeth web` with a litellm success/failure callback that writes `capture/llm_calls.jsonl` |
| `drive_session.py` | One multi-turn session over the HTTP API, plus per-turn capture slicing |
| `run_scenarios.sh` | Serial scenario queue, then mine and register |
| `mine_sessions.py` | Digest of reasoning markers, tool failures and advisor verdicts |
| `build_register.py`, `render_register.py` | Tool-call register from live registries, joined with captures |
