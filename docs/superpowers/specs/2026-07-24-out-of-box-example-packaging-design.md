# Out-of-Box Example Packaging Design

**Date:** 2026-07-24
**Release:** 0.7.2

## Objective

Every runnable example must expose one documented command that works from a
clean repository checkout after the documented project installation. Examples
may require real credentials or external services when those are intrinsic to
what they demonstrate, but they must not require an undocumented preparation
step or runtime artifact that is absent from Git.

The release validation excludes Azure examples, PostgreSQL variants, the
ChaosLLM endurance workload, and the OpenRouter stress/endurance configuration.
Those exclusions limit this release run; they do not weaken the packaging
contract for ordinary example entry points.

## Entry-Point Contract

- A standalone example whose tracked inputs are sufficient may use the direct
  `elspeth run --settings ... --execute` command as its canonical entry point.
- An example that must seed data, provision a local service, or coordinate
  multiple processes must provide a `run.sh` launcher that owns those steps.
- Each README must identify the canonical command and any intrinsic external
  prerequisite, such as `OPENROUTER_API_KEY`, ChromaDB, Docker, or a local
  fault-injection server.
- Launchers may clear only the example's own generated and ignored artifacts.
  They must fail fast when preparation or execution fails.

## Blob Transform Repair

The offline blob-transform settings currently reference a generated manifest,
and the manifest generator borrows its source CSVs from
`multi_worker_showcase`. A clean invocation of the settings therefore fails,
and the example is not independently packaged.

The repair will:

1. package the two source CSV fixtures under `examples/blob_transforms/input/`;
2. update the manifest generator to read those local fixtures;
3. add `examples/blob_transforms/run.sh` as the canonical offline entry point;
4. make the launcher clear only the offline example's prior manifest, payloads,
   audit database, and outputs before regenerating and executing; and
5. update the blob example documentation and catalog entry to show the
   one-command path.

The hosted HTML-fetch configuration remains a direct opt-in command because it
needs no generated fixture and deliberately performs public network access.

## Failure Handling

Preparation and pipeline failures propagate as a non-zero launcher exit. The
launcher will not substitute sample output, continue with stale blobs, or hide
a missing dependency. Cleanup is scoped to known ignored files within
`examples/blob_transforms`; tracked fixtures are never removed.

If the wider example run exposes another reproducible packaging defect, the
same rule applies: automate local preparation at that example's boundary and
add a focused regression. Runtime or product defects remain ordinary defects
and are fixed at their owning layer rather than concealed in a launcher.

## Verification

A new end-to-end regression will copy the blob example without generated
artifacts, run its canonical launcher in that clean copy, and assert that:

- preparation and execution exit successfully;
- the manifest and payload blobs are generated locally;
- the audit database and expected output are created; and
- the output contains the rows derived from both packaged source fixtures.

After the focused regression passes, the release checkout will run every
non-excluded example entry point and every ordinary OpenRouter configuration.
Expected-failure demonstrations pass only when they fail in their documented
way. Results will distinguish successful executions, expected failures, and
explicitly excluded Azure, PostgreSQL, and endurance cases.

## Non-Goals

- Committing generated payload-store blobs, manifests, databases, or outputs.
- Hiding credential or external-service requirements.
- Building a central dispatcher that replaces independently usable examples.
- Changing Azure, PostgreSQL, or endurance behavior during this release pass.
