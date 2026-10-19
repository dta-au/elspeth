# Composer qualification repairs

This repair candidate starts from `0b13686a647481761839310013663cdd9b8df5df`
(version 0.8.9). Commit `47860b0227f23298e275803e4dd4afd3e30cf139`
repairs the original delegated qualification failures. The subsequent test-only
follow-up repairs the 16 wider planner consumers exposed by that checkpoint.
The integrator owns the canonical full gate; these focused results do not
qualify the entire candidate.

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

## Planner consumer follow-up

The wider focused planner run completed with exit 1: 38 failed, 357 passed.
After the discovery/palette repairs, a fresh run reproduced the remaining
16 failures on `47860b022`. The follow-up changes only planner tests:

- Discovery assertions bind the completed tool result to its actual call ID
  and retain closure/decline ordering without assuming removed pressure text.
- Ordinary briefs leave literal model identifiers discoverable. The optional
  complete-catalog fixture now explicitly supplies real catalog-helper output;
  supplied/no-gain and deferred-aid coverage remains meaningful.
- The real three-plugin schema batch can exceed the retained aggregate schema
  allowance. The scripted provider follows the advertised assistance tool for
  refused contracts and retains the honest refusal; it does not falsely claim
  the complete schema was supplied. The actual proposed pipeline is validated.
- Seven Advisor hatch consumers recognize the retained senior-advisor notice.
  Provider identity, terminal-only palette, complete protocol pairs, rejected
  candidate bytes, safe feedback, and truncated-response omission remain pinned.
  The separate pressure test rejects the removed steering text.
- Repeated cancellation checks the actual secondary cancellation's settlement
  failure cause. Python 3.14 intentionally reports that same completed shielded
  task even when its exception was retrieved; only that exact diagnostic is
  accepted. Unknown diagnostics and lost causes still fail.

The measured fixed scaffold is 131,702 canonical bytes, against the previous
131,593-byte pin. John explicitly approved the 109-byte test-only adjustment
for the retained attachment declaration and control guidance. The finite
ceiling, exact-boundary and one-byte overflow controls remain; no production
request limit or prompt padding changed.

The focused follow-up completed with exit 0: 34 passed, including all 16
repaired cases and existing schema-budget, deadline, custody, cancellation and
settlement controls. Three deliberate private negative controls each produced
the expected exit 1 at the intended assertion: lost settlement cause, unknown
unobserved-task diagnostic, and restored discovery pressure.

After completing the aid fixture's declared shape, its four cases plus the
masquerade and mock-discipline gates completed with exit 0: 250 passed, one
documented PEP-649 skip on Python 3.14. Canonical contracts, Ruff, formatting,
and whitespace checks also exit 0; the production mapping census stays 2,573.

No remaining red is known in this delegated focused set. Root-owned Advisor
scoped-stop work and integrated qualification remain separate. No production
code changed after `47860b022`; no broad suite, PostgreSQL gate, live provider
acceptance, Gate1 prompt sequence, merge, push, or deployment was performed.
