# Composer standard battery

A fixed, hand-transcribed set of operator scenarios for exercising the **web
composer end-to-end** — author a pipeline through the chat surface, then run
it and check the output.

- `battery.md` — the cases (3 cases, 6 turns). Format and per-case pass
  criteria are documented in the file itself.
- `fixtures/<case>/` — supplied input data for cases that are not driven from
  invented data.

## How to use it

Drive it by hand against a live composer (this is what it was transcribed
from), or wire it into a driver. There is no scorer here: each case states
what it is testing in prose, and the criteria are checkable by eye against the
run output.

Fire the turns of a case **in order, in one session** — `manufacturing_leads`
in particular is testing incremental amendment, so restarting between turns
does not exercise the case.

## Relationship to the sibling batteries

| Harness | Question it answers |
| --- | --- |
| `evals/composer-battery/` | Given a task in operator voice that never names a mechanism, does the composer pick the right shape? Compose only — it never executes a pipeline, and it scores offline against a pre-registered floor per case. |
| **`evals/composer-standard-battery/`** (this one) | Given a task that *does* name a mechanism, does the composer honour it, and does the pipeline then run and produce the right rows? Compose **and** run. |
| `evals/composer-parity/` | Do the MCP and web surfaces agree? |
| `evals/composer-rgr/` | Targeted red/green regression scenarios. |

The two corpora are deliberately kept apart. `composer-battery/corpus.md` is
pinned: `tests/unit/evals/composer_battery/test_corpus.py` asserts an exact
20-case set and every case needs a registered `scenarios/<case>/scenario.json`
floor, so rounds stay comparable by binding identity. Adding these multi-turn,
fixture-bearing, run-to-completion cases there would break that comparability
and red the gate.

## Provenance

Transcribed from `images/test_examples.md` on the previous host (`nyx`,
2026-09-02). Typos corrected (`al ookup`→`a lookup`, `jsut`→`just`,
`assignement`→`assignment`); an unrelated issue-tracker note that had been
pasted into the same file was dropped. The prompt text is otherwise unchanged
— it is operator voice on purpose, warts included, because that is what the
composer has to cope with.

`vendor_risk` has a completed reference run: the operator's screenshot
`vendor-risk-run-complete.png` (repo root, untracked, 2026-09-01).
