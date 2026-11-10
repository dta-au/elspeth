# D2-R1 scoped fix review

2026-11-09. Worktree:
`/workspace/elspeth/.claude/worktrees/composer-remediation-d2`.

**Spec verdict: pass for D2-R1 and the five-file fix delta.** Explicit null
source/sink paths now fail the named path phase before materialization. Missing
keys remain absent, and the documented nested-provider null value remains
nullable. The original D2-R1 finding is resolved.

**Code-quality verdict: pass for this scoped fix.** The change repairs the owning
boundary and removes downstream scalar coercion without introducing a fallback,
exception swallowing, migration, or new authority model. No outstanding finding
was identified in this delta. This supersedes the original review's blocking
D2-R1 verdict; the original report's broader acceptance limits still apply.

## Identity and scope

Read the appended Fix round 1 report and the complete incremental diff, and
checked the changed logic and new tests against the previously traced callers.
This is the requested scoped rereview, not a restarted whole-tree audit.

Measured HEAD: `f9c23c59c63bbd3099029e995a778bade594f6c8`.
Measured incremental patch SHA-256:
`1f59ef6e606bcc8ec1c7027de99faa4e25a78afc4ee69fabbf59e19f79e5cd95`.
Measured complete fixed patch SHA-256:
`9796d6dbc518b1c67deeb27b68378d2dde96f641fd8d43d45ca76613c77c0d46`.

Independently verified all 22 current changed/new file blob IDs against the
complete fixed artifact. Compared original and fixed manifests: exactly the
reported five files differ—`_validation_authoring.py`, `preflight.py`, and the
three test files. Independently compared current bytes, file kinds and modes
for every one of the **6,207** fixed manifest entries; all match. Before and
after manifests both hash to
`991db7b0c8f38cacf2694421fc8785300455cf7ca1adbe14bf0d6852d536baf1`.

## Why the fix closes the finding

`_validation_authoring.py` now checks source/sink key membership before reading
the value. A present null reaches `is_local_path_value` and the existing typed
failure builder. There is no longer an absent/null conflation. This applies to
both source aliases and all three sink aliases in both Web and MCP modes. The
provider branch retains its deliberate null exemption.

`preflight.py` separately validates every present rewritten path value before
calling `Path`. It uses the admitted string directly rather than `str(value)`.
Direct executable or compiled-identity materialization can therefore no longer
invent a `None`, boolean, numeric, or container pathname when invoked without
the earlier phase. Its provider branch preserves null and rejects other invalid
shapes; generated-transform scope and ordinary Web/MCP resolution remain
unchanged. Normal loaded preview rejects earlier through `path_allowlist`,
while direct invalid materializer input raises an intentional `ValueError`.

The new tests exercise the real loaded-session handler and real worker path,
with observers wrapping the actual materializer and plugin setup. They require
the exact path check to fail, a non-valid preview, no downstream entry, and
unchanged public/private state, saved bytes, hash, and version. Alias-only cases
cannot pass merely because CSV later rejects an unsupported option. Direct
resolver controls cover singular/plural sources, sinks, provider paths, and both
authority modes. Separate positives retain absent-key behavior and provider
null through both executable and identity materialization.

## Evidence inspected

Read the exact argv/exit receipts, import provenance, raw run summaries, and
the persisted observation records—not only the report's pass counts.

- Red new-control cohort: exit 1, **135 failed, 5 passed**. Red handler cohort:
  exit 1, **10 failed**. All ten recorded handler cases passed the old path check
  and reached materialization/plugin setup; source/sink `path` cases also had
  valid runtime preflight. The pre-existing `None` links produced outside
  executable paths in the two concrete path reproductions.
- Green new-control cohort: exit 0, **140 passed**. Independently read all ten
  green and all ten final handler records: each has a failed `path_allowlist`,
  invalid runtime preflight, and an empty observed downstream-entry list.
- Final affected cohort: exit 0, **950 passed**, 114.13 seconds. Scoped ruff,
  formatting, mypy, and whole contracts/census checks each record exit 0.
- Raw boundary scan: exit 1, **205 findings**. Independently compared complete
  baseline/fixed diagnostic multisets excluding only location/derived ID and
  verified every derived ID; no semantic delta. The supplied comparison also
  records source-line mapping and message/ID/line mutation controls, exit 0.
  The additional preflight import moves the existing diagnostic to line 80;
  its bound test/fingerprint is unchanged. This is not a zero-finding result
  or signed metadata clearance.

Observed unchanged outside bytes do not prove that no read occurred during the
old reproduction. The evidence establishes outside-path materialization and
plugin-setup reach before the fix, and earlier typed refusal afterward.

## Limits and remaining acceptance

No reviewer tests, scans, providers, installs, source/index edits, commits, or
child agents were run. Only this review was written. Recorded implementation
tests were inspected; their execution is attributed to the implementation
lane. No claim is made that all possible inputs or platforms were tested.

The fresh public stdio lifecycle acceptance without pytest recovery injection,
platform qualification, combined ordinary/long-proof/PostgreSQL gates, and
all-rule lint delta remain parent-owned. This fix changes none of the separate
adjacent02.17 lifecycle requirements or the original resolved-path, rather than
race-free OS sandbox, design boundary. These are acceptance limits, not new
findings against the five-file repair.
