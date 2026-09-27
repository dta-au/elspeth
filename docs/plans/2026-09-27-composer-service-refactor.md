# Composer service refactor implementation plan

**Target:** `refactor/composer-service-20260927`, based on `d982e40fe` from the Composer system boundaries branch, which has landed on `release/0.8.1`. Integrate into that release branch after independent review and verification.

**Goal:** Reduce the responsibilities and size of `src/elspeth/web/composer/service.py` while preserving exact provider calls, advisor prompt bytes, validation results, audit order, and supported public entry points. Do not leave compatibility shims, duplicate helpers, or split ownership behind.

**Architecture:** Keep `ComposerServiceImpl` as the owner of provider dispatch, composition, checkpoint orchestration, and durable publication. Move pure advisor evidence projection into `advisor_context.py`; move verdict presentation and validation policy into `advisor_policy.py`. The new modules depend on existing contracts and state, never on `service.py`. Update internal callers and tests to import from the owning module. Keep only documented public service entry points. A separate interpretation surfacing extraction requires a later persistence-focused change.

## Step 1: Establish baseline and import custody

- Confirm the new worktree is clean, both `elspeth` and `elspeth_lints` import from this worktree, and the pending parent worktree remains untouched.
- Run the focused advisor checkpoint, structured checkpoint, terminal publication, advisor tool, and service tests serially before editing. Record exit codes and logs under an ignored lane path.
- Search all imports, direct helper calls, monkeypatches, and exact-source gates for symbols being moved. Preserve one nominal runtime identity for dataclasses and import aliases.

## Step 2: Extract advisor evidence context

**Create:** `src/elspeth/web/composer/advisor_context.py`.

**Modify:** `src/elspeth/web/composer/service.py` (the injection regexes and the contiguous scan/summary section from `_looks_like_advisor_prompt_injection` through `_render_interpolated_row_fields`).

- Move advisor prompt construction, injection scanning, and evidence rendering together, including their fences, regexes, constants, and functions, without changing bodies or order. Prompt-size admission and provider dispatch must use the same moved builder so exact bytes cannot drift. Import only their actual contract, state, Jinja, and trust-boundary dependencies.
- Update service call sites and test imports to the new owner; do not add compatibility aliases for private helpers. Preserve evidence limits, query ordering, truncation, redaction, and rendered bytes.
- Compare normalized ASTs of every moved definition to the base version. Run Ruff, mypy, advisor summary and injection tests, the attribute/masquerade gates, and contract census.
- Ask Astra to review the diff and test evidence; repair all actionable findings before proceeding.

The keyless tier-model comparison exposed one existing error-to-empty coercion
in this moved context: a malformed Jinja template was reported as having no
interpolated row fields. The final implementation reports a fixed UNKNOWN
analysis result for invalid template syntax; valid-template output is unchanged.
Regression tests cover single and mixed-query templates and unrelated errors.

## Step 3: Extract advisor verdict policy

**Create:** `src/elspeth/web/composer/advisor_policy.py`.

**Modify:** `src/elspeth/web/composer/service.py` (the advisor signoff validation and wording section after the context block).

- Move the closed validation result builders, wording, findings fence, and constants as one dependency group. Keep the provider checkpoint method, attempt accounting, and durable publication in the service.
- Preserve `AdvisorCheckpointVerdict` nominal identity; move it only if import/call-site evidence shows it can be done without changing the public contract.
- Update exact-path tests for moved first-party policy functions when the path itself is the assertion. Do not weaken value/redaction assertions.
- Compare normalized ASTs of moved definitions. Run focused advisor, no-tool, service, integration prevalidation, contract, lint, and type checks.
- Ask Astra for a second independent review, correct every actionable finding, and repeat affected checks.

## Step 4: Final integration evidence

- Inspect the complete changed-file diff and staged set. Run all whole-tree gates whose scanned inputs changed, including dynamic-attribute, masquerade, soft-mapping, wire-shape, and trust-tier baseline comparison as applicable.
- Since this is a pure policy extraction with bounded behavior and no schema, SQL, or lock changes, use focused Composer tests and shared-planner integration tests. Expand to canonical full and PostgreSQL gates only if the final diff reaches shared runtime or persistence behavior.
- Run `scripts/branch-safety-check.sh --intent commit`, commit only the refactor and plan files, verify release ancestry, and integrate locally after Astra's final review. Report local, remote, deployment, and gate state separately.

**Definition of done:** The Composer service delegates advisor context and publication policy to cohesive modules, all relocated definitions are behaviorally equivalent or reviewed deliberate changes, targeted tests and relevant whole-tree gates have explicit exit-code evidence, and Astra has no outstanding actionable findings.
