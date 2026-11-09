# D2 configured-root path admission review

2026-11-09. Candidate worktree:
`/workspace/elspeth/.claude/worktrees/composer-remediation-d2`.

**Spec verdict: changes required.** The intended authority and writer wiring is
present, but explicit null source/sink paths in preserved sessions still bypass
the runtime path check and are converted into filesystem paths afterward.

**Quality verdict: changes required for the same finding.** The implementation
is otherwise narrow and the new tests exercise meaningful production dispatch,
atomic refusal, identity, and cache behavior. The reported focused checks are
supported by their receipts. They do not establish the omitted null legacy
behavior or the outstanding combined lifecycle/full-suite acceptance.

## Reviewed identity and scope

Read the D2 implementation brief, design review, implementation report, repository
AGENTS.md and CONTRIBUTING.md guidance. Read the complete 2,076-line review
artifact, including both new files, then traced its owning functions and relevant
consumers: registered MCP dispatch, source/output/node mutations and merged
patches, both bulk source forms and subsequent bulk provider/output gates,
prepared-blob settlement, path helpers, validation phases, both YAML resolution
callers, public projection, plugin policy and semantic/config validation.
This is a task-focused source review, not a fresh repository-wide audit or a
claim to have reread every unrelated unchanged line in the large existing test
and tool modules.

Measured HEAD at the beginning and end:
`f9c23c59c63bbd3099029e995a778bade594f6c8`.
Measured patch SHA-256 at the beginning and end:
`f0308cf72acfb23967069c712d3e3d1bde3f567e70d4ed91251485b52e831d39`.
Read-only `git hash-object` comparisons matched the patch's new blob ID for all
20 changed/new files. Independently compared the current bytes, file kinds and
modes of all **6,207** manifest entries with `review-freeze.json`: all matched.
The before/after/review manifests each hash to
`60a4be8cb90a2b4cc89af42356f1cee054d739a132b1b9494f12dcaa784dde59`.

No tests, scans, providers, installs, subprocess acceptance, source/index edits,
commits, or child agents were run. The only written file is this review.

## Finding

### D2-R1 — P1: explicit null source/sink paths bypass the checked root during legacy preview

**Location:** `src/elspeth/web/execution/_validation_authoring.py:296` and
`:349`, immediately before the new `is_local_path_value` checks; downstream
conversion at `src/elspeth/web/execution/preflight.py:136`–`:140`.
Missing coverage: `tests/unit/composer_mcp/test_configured_root_admission.py:487`.

**Trigger:** load or inject an otherwise usable CSV composition whose source or
sink options contain `"path": null`, then preview it through the configured-root
MCP server. A concrete confinement counterexample is a configured-root entry
named `None` that is a symlink to a location outside the root. This does not
require a symlink race: the link can already exist before preview starts.

**Source-grounded chain:**

1. The source loop uses `source_options.get(key)` and the sink loop maps missing
   keys to `None`; both immediately `continue` when the value is `None`.
   Consequently an explicitly present null never reaches the new exact-string
   shape gate or any containment check. The phase publishes a passed
   `path_allowlist` check.
2. The trained-operator `validate_plugin_policy` path checks plugin availability
   and controls, not source/sink option model validity. The later semantic stage
   is not a substitute shape gate: `_semantic_validator.py:202` tolerates source
   draft-config failures, and `:248`–`:278` similarly tolerates sink draft-config
   failures. A simple source-to-CSV-sink graph need not have any blocking semantic
   requirements. Batch validation does not inspect source/sink paths.
3. Interpretation materialization and the private YAML generator retain the
   null option. `resolve_runtime_yaml_paths` then tests
   `Path(str(path_options[key]))`, converting null to the nonempty string
   `"None"`, and resolves that value relative to the configured root.
4. There is no second containment phase after this rewrite. The resulting
   absolute string reaches settings/plugin construction. With `root/None`
   symlinked outside, the resolved string is outside; without the symlink it is
   still an unintended real pathname. `PathConfig.path` now receives a valid
   nonempty string instead of the original invalid null.

**Impact:** the named runtime path verdict can certify a source/output location
that was never admitted, including a canonical location outside the configured
root. Admission correctly refuses such null values, so loading preserved state
produces different authorization behavior. This breaks the D2 exact-shape,
legacy-preview, and admission/materialization agreement requirements. This
review does **not** claim an observed outside read/write or an executed preview
reproduction; it establishes the unchecked materialization path from source.
The null-skip predates D2, but retaining it beside the newly strengthened check
leaves an explicit requirement of this remediation unsatisfied.

**Remedy:** distinguish a missing source/sink path key from a present key with
null before reading its value. Apply `is_local_path_value` to every present
source/sink local path key; keep only the documented nested-provider null
exception. Do not convert an invalid authored scalar into a path in order to
make downstream config validation pass.

**Required regression evidence:** add source and sink null cases through loaded
legacy state, assert a named failing `path_allowlist` check and unchanged saved
private state, and include the pre-existing `root/None` escape symlink control.
Cover the shared local-key aliases at the path phase. Keep controls for an absent
optional path key and for provider `persist_directory=None` surviving execution
and identity YAML unchanged. The current malformed-legacy matrix deliberately
contains empty string, integer, list and NUL string, but omits null for every
component; the live-authoring null test cannot exercise this preserved-state
route. Parent/implementer should execute the concrete reproduction and red/green
test under the authorized test owner.

## Requirement and test assessment

- The frozen nominal `contracts/composer_paths.py` value is issued from the
  configured root by the stdio builder. It is neither a tool argument nor a
  synthetic session. The contracts placement is consistent with its consumers
  and the parent's ruling; no whitelist expansion appears in the artifact.
- All eight nominated writers receive authority. Source and output patches
  validate merged options; provider patches validate reconciled merged options.
  Both bulk source forms, provider nodes, and outputs are guarded before the
  candidate can succeed. `_execute_set_pipeline` only persists a prepared blob
  after successful complete candidate construction. The real SQLite/blob
  refusal test supports the settlement ordering; MCP itself has no blob custody.
- The new registered-handler matrix uses exact path codes/component references,
  private saved bytes, version/hash, empty affected nodes, public-state equality,
  and semantic-versus-argument audit status. It covers relative/absolute
  admission, absolute/traversal/symlink escape, absent authority, missing storage,
  legacy non-path patches, and bulk late failures. These are useful positive and
  negative controls, not a synthetic alternate dispatcher.
- Runtime propagation includes `_ValidationRunImpl`, the public facade, empty
  validation's owned-context check, `validate_path_policy`, executable YAML,
  `_compiled_identity_settings`, and its profiled caller. Literal path equality
  tests cover both materializations and nullable provider preservation.
- Cache settings include authority mode and canonical root; the shared
  coordinator test distinguishes a second root and absent authority while
  deduplicating a canonical symlink alias. Coordination remains in-flight-only;
  completed previews are evicted, so the changed-symlink preview test does not
  merely reuse a completed cached verdict.
- Existing Web directory helpers retain their session interpretation, and the
  new Web controls exercise own/foreign/shared/missing-session/traversal/symlink
  cases. ToolContext rejects a root authority combined with session identity,
  deployment data-dir custody, session engine, operation context/authority, or
  reviewed-source authority. Wrong nominal types fail as owned errors.
- Public YAML projection still strips source/output/provider private paths;
  `generate_yaml` does not replace private working state. The new export test
  checks the response and durable private state. Loading legacy state remains
  preserving, rather than cleansing it. Ordinary escaped string paths are
  independently refused at preview; D2-R1 is the outstanding scalar edge.

## Verification evidence assessed

Read the recorded command arrays, explicit exit receipts and raw summary tails.
The final contracts-placement cohort records **579 passed**, exit 0. Scoped
ruff, formatting, mypy (14 production files), contracts, and generated inventory
each record exit 0. The earlier cohort records **1,576 passed, 3 skipped**, exit
0; its three PEP 696 skips require Python 3.13. That earlier cohort preceded the
type-placement correction and is not mislabeled final-tree execution evidence.

The boundary scanner is **not green**: its raw exit is 1 with 205 findings, and
the strict diagnostic-ID comparison also exits 1. Read
`compare_boundary_locations.py`, its receipt and result. Independently compared
the complete raw baseline/candidate diagnostic multisets excluding only line
and derived diagnostic ID: both contain 205 records with no semantic delta;
independently checked every recorded ID against the scanner's stated
`rule|file|line|column` derivation. The supplied comparison additionally maps
the four line moves to unchanged HEAD text and records message/ID/line mutation
controls. This supports location-only drift, not zero findings, signed metadata
clearance, or the still-outstanding all-rule delta. No signature/binding metadata
changes are in this patch.

## Considered but declined scope

- Standalone recovery/watchdog ownership and coordinator teardown: explicit
  adjacent02.17 ownership. The candidate still relies on the pytest recovery
  owner for isolated preview tests; this is disclosed and is not treated as a
  D2 claim that fresh public stdio preview works.
- Fresh public executable acceptance, platform qualification, full backend and
  PostgreSQL suites, all-rule lint comparison, and long proofs: parent-owned
  combined acceptance. No result is inferred from the scoped green receipts.
- Expanding provider rewriting to generated collectors/aggregations: the brief
  explicitly preserves the existing registry-triggered exclusion. No current
  D2 change needs a second general runtime path framework.
- Race-free operating-system confinement, filesystem locking, and symlink
  handle custody: excluded by the resolved-path design. D2-R1 is not such a
  race; it bypasses the required ordinary resolution check entirely.
- Retrofitting local-file inspection, exposing blob/secret tools, replacing
  public export redaction, or migrating/cleansing persisted sessions: separate
  behavior changes without authorization in this D2 brief.
- Rewriting unrelated existing tool/validation helpers or claiming all possible
  inputs tested: neither is necessary to fix the finding. The focused matrix
  should be extended at the actual missing null distinction, then the frozen
  updated candidate should return for review and the combined gates.
