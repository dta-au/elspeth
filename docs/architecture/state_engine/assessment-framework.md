# State Engine Assessment Framework

This framework explains how to interpret evidence. Follow the executable
[assessment program](assessment-program.md) for capture and rerun commands.

## Assessment modes

### Full

A full assessment inventories all 73 catalog legs, every required case, and all
hard gates. It may change the global verdict.

### Delta

A delta assessment names the changed legs/cases, establishes change impact,
reruns their evidence and adjacent invariants, and changes only those cells. Its
manifest still materializes all 73 legs, names the parent digest, and lists the
changed tuples. It inherits unresolved cells from the preceding full assessment
and cannot declare the whole engine complete.

### Historical rerun

A historical rerun reconstructs a named baseline and executes recorded command
vectors in a fresh worktree. It writes a new rerun directory and divergence
report; it never overwrites the original assessment.

## Assessment sequence

1. Fix the code baseline and capture the complete worktree identity.
2. Verify Loomweave freshness and record structural-query limitations.
3. Load the exact catalog version and verify all 73 legs are present.
4. Discover production writers, callers, read models, external-effect
   boundaries, and cross-transaction seams.
5. Reconcile live Filigree issues without treating issue closure as proof.
6. Execute evidence from production boundary inward.
7. Attach evidence only to the cases its assertions establish.
8. Classify remaining cases and create or update Filigree issues for coherent
   actionable gap themes, not one issue per unknown cell.
9. Run independent architecture, evidence, and future-agent reviews.
10. Resolve material findings, rerun affected evidence, and publish the new
    dated package and hub pointer.

## Evidence record

Every execution record contains:

- a stable evidence and command ID;
- an argument vector, never an ellipsis or ambiguous prose command;
- repository-relative working directory;
- safe environment additions/removals and required resources;
- start/end times, timeout, exit code, and duration;
- one machine-produced runtime profile report with its state store, deployment,
  semantic backend version, probe kind, and exact probe node;
- positive, unique collected node IDs and exact
  pass/fail/error/skip/xfail/xpass/warning counts;
- stdout, stderr, JUnit, and retained artifact paths with SHA-256 when stored;
- catalog coverage tuples: leg, dimension, semantic case, profile case, and the
  exact node IDs that establish that tuple;
- `establishes` and `does_not_establish` statements;
- reproducibility class: `deterministic`, `semantic_comparison`, or
  `external_observation`.

Warnings, skips, xfails, missing credentials, and partial platform coverage are
evidence facts. Do not omit them to make a run appear cleaner.

The `collect-evidence` compatibility command statically revalidates retained
artifacts; it never replays pytest or imports a recorded test module. It and
`validate-package` share one exact command/environment schema. The recorded
vector must load the trusted profile reporter, select explicit `tests/` nodes,
and bind its JUnit and profile outputs to retained artifacts. The reporter gets
backend facts from the live database connection at a trusted test boundary;
deployment is a trusted test assertion bound to that probe node, not a manifest
label. The closed safe-environment allowlist records only approved reproduction
inputs.

Only a successful `pytest` record with a positive pass count, no
fail/error/skip/xfail/xpass result, matching JUnit testcase/aggregate totals,
exact equality among JUnit properties, node index, and profile node IDs, and one
proof subject per cited node can promote a behavioral cell. A node may cover
several dimensions of that one leg/case/profile subject. Documentation records
are support-only and cannot promote behavioral cells.

## Classification rules

- Use `pass` only when executable evidence covers the whole required case at
  the current code baseline.
- Use `partial` when evidence is real but narrower than the case. State exactly
  what remains.
- Use `fail` only when executable evidence demonstrates contrary behavior.
- Use `unknown` for absent, stale, unexecuted, mock-only, or unrepresentative
  evidence.
- Use `not_applicable` only when the catalog already marks it so.
- Record an explicit `owner_issue` key for every active gap. `null` means
  visibly unowned; it does not make the gap disappear.
- State an observable exit gate. “Add more tests” is not an exit gate.

Hard-gate status, affected legs, and support are derived from the catalog's
gate-to-dimension mappings and the validated cell evidence. Assessors explain
the result; they do not independently close a gate.

## Proof boundaries

The following substitutions are invalid:

- a closed issue for an executed regression;
- a test name for a passing run;
- source inspection for runtime composition;
- a mock plugin for a real external-effect boundary;
- one connection for a multi-connection race;
- one process for a process-death claim;
- caught exceptions for abrupt process loss;
- state-only assertions for state-plus-event atomicity;
- a nearby passing leg for the leg under assessment;
- an older commit's result for the current baseline.

## Review protocol

Every full assessment receives three independent lenses:

1. **Architecture:** Is the state/transaction/boundary model complete and
   faithful to current source?
2. **Evidence:** Does each result prove exactly its attached case and preserve
   limitations?
3. **Future agent:** Can a new agent reproduce the assessment from the package
   alone?

Record every material finding, its severity, disposition, rationale, changed
files, and re-review outcome. Preserve unresolved dissent. Review is technical
challenge, not an approval chain or signed receipt.

## Snapshot policy

Dated assessments are baseline-bound historical records. Correct a factual
error with a clearly named erratum; do not silently rewrite the original claim.
When code, catalog, or evidence changes, create a new assessment.

Git supplies normal document history. Do not add signature chains, sealed
plans, reviewer receipts, or hash manifests for the documents themselves.
Hash only retained evidence or overlay artifacts when the digest is needed to
reproduce the run.
