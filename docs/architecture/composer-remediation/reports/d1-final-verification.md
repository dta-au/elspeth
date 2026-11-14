# D1 combined verification checkpoint

This is execution coordination, not a completion claim. Parent owns broad
test capacity. Implementations and per-task reviews remain serial.

1. Finish legacy, state admission/caps, non-writing blob admission, staging
   resource races, and canonical/planner settlement. Require explicit spec and
   quality approval and meaningful red/green behavior controls for each.
2. Freeze all source and test files at the resulting local commits. Preserve
   dependency symlinks and imported audit docs; neither belongs in commits.
3. Reuse the full frontend unit run only if its recorded frontend source
   manifest still matches. Keep the completed typecheck/store evidence; rerun
   only if changed frontend inputs invalidate it.
4. Inspect active pytest processes/host capacity, then run the repository
   full-suite-gate with explicit worktree root and lane-private log parent,
   stages ruff,mypy,contracts,lints,pytest,testcontainer. PostgreSQL must be
   serial, separately selected. Docker availability was measured:29.9.2.
5. Read terminal stage exits, frozen status and JUnit evidence. Classify any
   unrelated failure against the exact original base; do not silently weaken
   pins or use an isolated retry as proof the original broad run passed.
6. Compare final keyless all-rule findings with d1-lints-before.json, retaining
   semantic changes and expected line-shift explanations separately. Never
   acquire a signing key or manually alter signed fingerprints.
7. Have Astra review the entire D1 commit range and relevant complete files,
   including cancellation/required-work custody, refusal classification,
   validation-only resource effects, truthful model/audit/progress outcomes,
   canonical versus ordinary partial-draft admission, and coverage evidence.
8. Address review findings with their implementer and repeat only justified
   verification. Integrate the verified commits onto the user's task branch
   after branch safety checks. No push is authorized or needed.
9. Update matching Page checkboxes only for fully covered routes. D2 and02.17
   remain separate subsequent implementations with their own final shared
   runtime, subprocess and platform qualification gates.

Main baseline is5e192342c; local legacy checkpoint66739807c. Exact final HEAD
and commands/results will be recorded once execution reaches this checkpoint.

Preflight rehearsal (no tests executed): canonical script resolves both source
roots and Docker correctly. It refuses the deliberate untracked frontend
node_modules directory symlink because sha256sum cannot hash a directory.
Evidence d1-gate-preflight.log/.exit (exit2). Before the final frozen backend
gate, record and temporarily unlink only that verified dependency symlink;
retain the main checkout dependency directory and recreate the identical link
when needed. Do not delete the target directory, hide source paths, or treat
this preflight refusal as a test failure. No broad suite may overlap the
current implementation's focused pytest process.

Lane comparator ready: compare-lint-corpora.py preserves a semantic Counter of
rule_id/file_path/severity/message/suggestion, normalizing only the exact
checkout prefix. It retains location/fingerprint multiset changes separately,
and controls additions, removals, duplicates, each semantic field, wrong-root
prefix and malformed schema. Replaying original first-wave before/after with
the measured respective main/worktree roots produces5424→5424 and zero semantic
changes (lint-comparison-control-replay.json). D1's baseline is the first-wave
AFTER file, so BOTH roots for D1 comparison are composer-remediation-wave1.
Capture final all-rule output as JSON with --format json in addition to the
canonical script's text evidence; do not compare only counts or drop duplicates.

Dependency preflight correction: do NOT leave node_modules absent for the
backend run. Source inspection found real React/HTTPS acceptance tests invoking
frontend/node_modules/esbuild/bin/esbuild (test_composer_react_generation_acceptance.py,
test_composer_https_acceptance.py). Root .gitignore259–260 ignores actual
node_modules directories but not this temporary directory symlink. Prepared
an independent physical copy of the existing378MB dependencies with
cp -a --reflink=auto into this lane's frontend-dependencies/node_modules;
copy exited0, copied esbuild reports0.25.12; disk had28GB free. At final freeze,
record/recheck the exact deliberate symlink, unlink ONLY it, and move this
prepared directory into the worktree's frontend/node_modules. The ordinary
ignored dependency layout supports real acceptance tests and canonical
source-freeze checking without changing the script or hiding source paths.
No source/index mutation; active implementer still sees the existing link.

Browser acceptance dependency is now prepared. The tests use Node
@playwright/test (not the Python playwright package); its expected Chromium
was absent. With current environment status matching the selected executor,
network unrestricted/enforced, installed the package-pinned Chromium via
node node_modules/playwright/cli.js install chromium (exit0;
playwright-install.log/.exit). Browsers are in this lane's playwright-browsers,
not a home cache. A real headless launch+close passed, version151.0.7922.34,
using the existing @playwright/test and lane-local runtime-tmp. For final
canonical full gate export PLAYWRIGHT_BROWSERS_PATH=/workspace/elspeth/.claude/lanes/composer-remediation-wave1/playwright-browsers
so inherited Node acceptance subprocesses use this verified installation.
This verifies environment readiness only, not Composer browser behavior.
