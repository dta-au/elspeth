# Composer Redaction Policy Changes — Governance Guide

**Audience.** Engineers modifying the composer redaction manifest
(`MANIFEST` in `src/elspeth/web/composer/redaction.py`) or the policy
snapshot (`tests/unit/web/composer/redaction_policy_snapshot.json`).

**Status.** Checked against the current direction checker on 2026-10-15.
The authoritative control definition now lives in the redaction `MANIFEST` and
its policy snapshot tests. Historical spec and review context are preserved in
git history or maintainer-local archives; generated reviewer sidecars are not
required for day-to-day policy changes.

---

## When does this guide apply?

Any PR that modifies:

- `src/elspeth/web/composer/redaction.py` (manifest entries, summarizers,
  `MANIFEST` dict)
- `src/elspeth/web/composer/redaction_telemetry.py`
- `src/elspeth/web/composer/tools/` (promoted handler argument models)
- `tests/unit/web/composer/redaction_policy_snapshot.json`
- `tests/unit/web/composer/test_adequacy_guard.py`
- `tests/unit/web/composer/test_walk_model_schema.py`

...will make the `composer-redaction-gate` CI workflow at
`.github/workflows/composer-redaction-gate.yml` enforce the redaction policy
direction check. The workflow itself starts on every pull request to the
protected branch set and exits successfully as not applicable when no
redaction-sensitive files changed.

---

## Mechanical gates

### 1. Policy-hash snapshot (primary control)

`tests/unit/web/composer/redaction_policy_snapshot.json` contains a
SHA-256 hash per manifest entry. The `test_adequacy_guard.py` test
module asserts the live manifest's hashes match the committed file.

Changes to the hashed schema walk, declarative key sets, summarizer key sets
or structured reason text flip the entry's hash. Commit the corresponding
snapshot change before CI goes green. Declarative summarizer callable bodies
are not hashed: changing a callable without changing its registered keys
requires direct behavior review and tests, even if the snapshot stays equal.
The shared implementation is `tests/unit/web/composer/_adequacy_helpers.py`.

Preview the snapshot after editing the manifest:

```bash
.venv/bin/python scripts/cicd/bootstrap_redaction_snapshot.py
```

Regenerate the snapshot after reviewing the dry-run output:

```bash
.venv/bin/python scripts/cicd/bootstrap_redaction_snapshot.py --write
```

### 2. Direction-aware label-gate CI step

The `composer-redaction-gate` CI workflow performs its **direction-aware** check
when the snapshot file changes on a PR. The implementation is
`scripts/cicd/check_redaction_direction.py`:

- Compares every changed manifest entry in the PR-head snapshot with the
  pull request's base-branch snapshot. Type-driven entries count
  `Sensitive`-annotated paths from `walk_model_schema`; declarative entries
  count sensitive argument keys plus sensitive response keys.
- If **any entry's count decreases**, the direction is `weaken`. An
  unrelated entry's increase cannot offset that decrease: aggregate totals
  are diagnostic only.
- An existing entry that changes while keeping the **same nonzero count**
  also yields `weaken`. Snapshot hashes do not identify individual paths, so
  an equal-count change could replace sensitive coverage. This conservative
  routing also applies to editorial or summarizer changes on such entries.
- Otherwise the direction is `strengthen`. A changed entry with zero paths
  on both sides therefore takes this default direction.
- A `weaken` result requires `policy-weaken-justified` and the rationale
  section below; a `strengthen` result requires `policy-strengthen`.
  `scripts/cicd/assert_redaction_label.py` rejects the opposite label.

These direction names route review. A `weaken` result can reflect uncertainty
about a same-count change; a `strengthen` result does not establish that the
new semantics protect sensitive content. Review the actual diff in either case.

### 3. Adequacy guard (CI-time, five assertions)

`tests/unit/web/composer/test_adequacy_guard.py` runs in the standard
pytest gate and asserts:

1. Registry-manifest set equality (every dispatch registry tool name
   has exactly one manifest entry; no orphans).
2. Per-entry shape walk via the shared `walk_model_schema` iterator.
3. Mass-copy uniqueness of declarative `HandlesNoSensitiveDataReason`
   text (no two tools may share the same `why_arguments_safe` or
   `why_responses_safe` string).
4. Policy-hash snapshot equality (live manifest hashes == committed
   snapshot file).
5. `extra="forbid"` on all type-driven argument models (prevents
   `arguments_canonical` / walker discrepancy).

---

## Which label should I use?

| Change | Correct label |
|---|---|
| Added a `Sensitive()` annotation to a field | `policy-strengthen` |
| Added a new tool to a registry and gave it a manifest entry | `policy-strengthen` |
| Added a new `sensitive_argument_key` | `policy-strengthen` |
| Removed a `Sensitive()` annotation | `policy-weaken-justified` |
| Removed a `sensitive_argument_key` | `policy-weaken-justified` |
| Replaced sensitive keys or paths at the same nonzero count | `policy-weaken-justified` |
| Summarizer change that changes the snapshot hash at the same nonzero count | `policy-weaken-justified` (conservative routing) |
| Editorial hash change on an entry with the same nonzero count | `policy-weaken-justified` (conservative routing) |
| Editorial change to a zero-path `HandlesNoSensitiveDataReason` entry | `policy-strengthen` |

The table assumes no other changed entry requires `weaken`. Any per-entry
decrease or same-nonzero-count change determines the direction for the PR.
Callable-only edits that leave the snapshot unchanged do not trigger this
label gate; they still require the behavior review described above.

When using `policy-weaken-justified`, the PR body MUST include a section
headed exactly:

```
## Redaction policy weakening rationale
```

The CI step grep-asserts this section header exists (the workflow's
Python check uses substring match on the body string — exact phrasing is
load-bearing).

---

## What the weakening rationale section MUST contain

A label is a routing signal; the rationale is the substantive control.
The CI gate cannot inspect the *content* of the rationale section — that
is the auditor's job. A complete rationale answers all of the following:

For conservative same-count routing without an actual coverage reduction,
explain why the snapshot changed and show that the sensitive paths remain
covered. Do not describe a semantic weakening that did not occur. The
weakening-specific questions and acknowledgement below apply when coverage
actually changes; the required section heading and routing label stay the same.

1. **What changed, and why was it routed as weakening?** Name the manifest
   entries and any paths removed or replaced. Quote the
   `sensitive_path_count` before and after, including equal counts.

2. **Why is this weakening *safe*?** From an auditor's perspective, not
   the implementer's. The default presumption is that redaction
   coverage exists for a reason; removing it requires showing the
   reason no longer applies. Acceptable arguments include:

   - The path was redundant because an upstream component already
     redacts the same field before the data reaches this tool.
   - The path covered a field that has been removed from the tool's
     input schema entirely — there is no data flowing through it any
     more.
   - The path was a copy-paste artefact that never matched real data
     (verifiable from run history showing zero redactions on that
     path).

   Arguments that are NOT sufficient:

   - "The path was annoying in tests" — annoyance is not an audit
     argument.
   - "I want to log this value for debugging" — see the
     `logging-telemetry-policy` skill §Logging Policy; this needs an
     audit or telemetry channel, not a removed redaction.
   - "Nothing sensitive ever appears here" — claims about absence of
     sensitive data require evidence, not assertion.

3. **What new mitigation replaces the prior coverage?** If the
   weakened path is being replaced (e.g., redaction moved upstream),
   name the replacement and cite its tests. If no replacement exists,
   explain why none is needed.

4. **Bulk-editorial-PR saturation acknowledgement.** This is the most
   important auditor-facing assertion. A PR that touches many files
   with a single weakening tucked in among them is exactly the failure
   mode this gate is designed to detect. The rationale section MUST
   include a sentence of the form:

   > This PR includes a weakening of the redaction policy. The
   > weakening is the substantive change, not an incidental
   > side-effect of refactoring. I am not relying on reviewer fatigue
   > to land it.

   Do not make that assertion for an editorial change routed conservatively
   at the same nonzero count. Explain the unchanged coverage instead. If an
   actual weakening is obscured by unrelated refactoring, split the changes
   for review; apply the checker-derived label to each resulting PR, since
   a refactoring-only hash change can still route as `weaken`.

---

## Escalation: when a weakening rationale is insufficient

If a reviewer (or, in a single-owner repo, the future-self auditor
reading this in CI logs) believes a weakening rationale is insufficient,
the correct response is to **re-open the PR with a stricter posture**,
not to merge with a warning attached.

Options for "stricter posture":

- Decline the weakening and keep the existing path.
- Replace the weakening with an explicit, narrower mitigation (e.g.,
  remove one specific sensitive path rather than the entire manifest
  entry).
- Split the PR so the weakening lands separately with its own
  reviewable diff.

The bar to clear is "would this hold up under formal inquiry?" — see
`ARCHITECTURE.md` § *Design Principles* (auditability: "I don't know
what happened" is never acceptable). If the answer is "probably, but
I'd rather not have to defend it," the PR isn't ready.

---

## Single-owner governance note

The label-gate (`policy-strengthen` / `policy-weaken-justified`) is a
**mechanical CI control**, not a code-review requirement enforced by a
human gatekeeper.

On a multi-person repository, CODEOWNERS would gate label application
via review approval from a designated security-review team. **On a
single-owner or personal-account repository, the label gate is
self-bypassing** — the repository owner can apply any label without
external review, and there is no second human in the loop.

This is an acknowledged governance gap documented in spec rev-5 §4.4.5
and the rev-2 review JSON under `M_governance_single_owner`.

**Consequence for single-owner repos:** snapshot-hash flips must be
treated as warranting a manual sanity-check of the diff direction before
merging, even when CI is green. Specifically:

1. Open the snapshot diff and verify the `sensitive_path_count` deltas
   match the intent of the change.
2. If any entry's count decreased or an existing entry changed at the same
   nonzero count, verify the `policy-weaken-justified` label and explain the
   actual coverage change or conservative same-count routing.
3. If the PR description includes a "Redaction policy weakening
   rationale" section, verify it accurately describes the change.

When the project migrates to an organisation-owned repo, CODEOWNERS team
routing becomes available and can be added as defense-in-depth on top of
the label gate.

---

## References

- `scripts/cicd/check_redaction_direction.py` — authoritative direction logic
- `scripts/cicd/assert_redaction_label.py` — label and rationale enforcement
- `tests/unit/web/composer/test_label_gate_direction.py` — executable controls
- `.github/workflows/composer-redaction-gate.yml` — paths and base-branch wiring
- Spec §4.4.5 — label-gate CI step definition and governance note
- Spec §4.4.3 — policy-hash snapshot (hash semantics; known false-negative
  class for closure-state mutations)
- Spec §4.2.3 — summarizer purity requirement
- Spec §4.2.8 — `arguments_canonical` posture (a) intentional raw
- Spec §12.3 — rev-2 reviewer finding traceability, including the label-gate
  direction and single-owner governance finding
