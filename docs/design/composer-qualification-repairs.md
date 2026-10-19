# Composer qualification repairs

This repair candidate starts from `0b13686a647481761839310013663cdd9b8df5df`
(version 0.8.9). It repairs the delegated qualification failures. The entire
candidate is not qualified: the completed wider planner check exposed the
remaining cases listed below, and the integrator owns the canonical full gate.

## Repairs and current-source counterevidence

| Finding | Resolution |
| --- | --- |
| Prompt-guard collection failures | Consumers use the physical-call contract. Tests retain all 129 large documents beyond the former logical count/byte ceilings, enforce five documents and 10,000 document characters per call, and retain indivisible-unit, invalid UTF-8, timeout, cancellation, and fail-closed controls. |
| pyrate-limiter constructor | Current source and frozen lock already retain 3.x; the isolated install resolves 3.9.0. Existing enabled limiter checks pass. No dependency change was needed. |
| React nullable refs | Current manifest/lock already retain React 18. A fresh locked install resolves React/DOM 18.3.1 and their type packages 18.3.31/18.3.7; the build passes. No React upgrade or UI change was made. |
| Mapping contracts/census | Advisor evidence messages now have a closed TypedDict. External LiteLLM replay messages and immediately parsed LLM attachment arguments have documented exemptions. The canonical generator records 2,573 soft occurrences, replacing the stale 2,562 pin; two of the original 2,575 live occurrences became owned types. |
| Advisor summary expectations | Required graph structure, every schema field, and every query mapping remain visible. Assertions measure optional contribution above the required graph. Obsolete whole-summary refusals are replaced by complete required-graph dispatch under a one-token optional allowance. Public-hint, credential, provider, retry, quota, and publication controls remain covered. |
| Mutation workflow | The current lock already retains mutmut 2.5.1. Both jobs now use the existing runner; parser failures, missing reports, and zero mutants fail. Scores remain advisory. Directory reports include descendants and retain the documented strict Landscape threshold. No mutation campaign was run. |
| CSV retained-sink flake | The test resolves the retained sink by its actual graph identity. Both effect orders are exercised. Forcing the default effect first reproduced the original failure before repair. Production retention policy is unchanged. |

Restoring the complete discovery fixture exposed another concrete defect:
`read_attachment` emits `blob.attachment:` keys, but both manifest admission and
audit classification rejected them. Both exact-prefix seams now admit this
existing content-read identity as `BLOB_CONTENT`; unknown and typo prefixes still
refuse. No persisted enum, schema, budget, or continuation policy changed.

The removed preview-only discovery-budget helper had no supported successor.
Its obsolete unit matrix was retired. Integration coverage now retains ordinary
authoring after preview and checks real deadline refusal, original-state
immutability, preview evidence, tool-call identity, and inspection custody.

## Validation

All local Python execution used the isolated environment and both worktree
source roots, with serial pytest and CPU affinity 0–3. Python is 3.14.3; CI's
Python 3.13 run remains a separate integration check. Evidence is private under
`.claude/lanes/qualification-repairs-20261019/`.

| Check | Measured result |
| --- | --- |
| Prompt guard, limiter, mutation runner, CSV | Exit 0; 280 passed. |
| Advisor checkpoint, graph visibility, mandatory evidence | Exit 0; 305 passed. |
| Final attachment/discovery/preview and mutation-runner set | Exit 0; 88 passed. |
| Contracts, attribute contracts, masquerade, mock discipline | Exit 0; 338 passed, one PEP-649 skip on Python 3.14. |
| Final collection | Exit 0; 70,740/71,933 collected, 1,193 deselected. Three optional-dependency collection skips: two SQLCipher and one Datadog integration module. |
| Locked frontend install/build | Both exit 0. |
| Canonical contracts/census compare | Exit 0. |
| Final scoped mypy / Ruff / formatting | All exit 0; mypy checks four source files. |
| Keyless whole-rule comparison | The owned-message change's source-frozen scans both exit 1 with the standing corpus; 2,031 location-normalized finding records, zero added or removed. The subsequent two attachment-prefix entries receive focused classification/refusal checks. This is not operator signature clearance. |

Normal staged-file hooks are required by the commit dispatcher. Their result
is recorded in the final checkpoint handoff.

## Remaining planner failures

The wider focused planner run completed with exit 1: 38 failed, 357 passed.
After separating the repaired discovery/palette cases, 16 cases remain for the
integrator to diagnose or update. Their exact names and assertion categories
are in the private `remaining-planner-failures.md` handoff:

- One positional protocol assertion expects the last message to be a tool.
- Five initial-brief consumers read the removed `model_catalog` field.
- One selected-schema fixture expects a schema but receives a discovery error.
- One secondary-cancellation case expects a diagnostic that was not recorded.
- Eight pressure/hatch/prose cases expect removed aggregate boundaries or
  exhaust their scripted replies.

These remaining failures are not evidence for restoring removed caps or
implementing the future continuation design. No broad suite, PostgreSQL gate,
live provider acceptance, Gate1 prompt sequence, merge, push, or deployment was
performed in this lane.
