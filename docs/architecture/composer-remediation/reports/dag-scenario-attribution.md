# DAG scenario production-path failures: attribution

Measured 2026-11-10. Read-only. Test file: `tests/integration/core/dag/test_dag_scenario_production_path.py`.
Run form: cwd=tree, `PYTHONPATH=$W/src:$W/elspeth-lints/src /home/john/elspeth/.venv/bin/python -m pytest <file> -n 4 -o "pythonpath=$W/src $W/elspeth-lints/src"`, logged to `.claude/lanes/dag-attrib/*.log` (the logs and extracts were deleted at the end of the run).
`elspeth.__file__` was confirmed to resolve inside each tree under test.

## Measured

| Tree | Commit | pyproject version | exit | result |
|---|---|---|---|---|
| HEAD (worktree) | 586a91d68 | 0.8.10 | 1 | 17 failed, 157 passed |
| candidate | a67628c29 | 0.8.10 | 1 | 17 failed, 157 passed |
| remediation tip | 25301aa93 | 0.8.9 | 0 | 174 passed |
| merge-base | 6825994b4 | 0.8.9 | 0 | 174 passed |

- Deterministic on HEAD: `test_linear_happy_path_has_exact_production_evidence` run alone with `-n 0` fails (exit 1) with the same projection mismatch (assert at test file line 1430, `evidence.runtime.durable_projection == case.expected.projection`).
- The failing side is the candidate (0.8.10); the remediation side is clean.

### Field-level difference (representative: the linear happy path case, from `-vv` full diff)
The two `StableRunProjection` reprs, tokenised, differ in exactly ONE token of 260. Only a node's hash `material` string differs, in the `plugin_version` of the engine-owned queue node `queue:inbound`:
- golden side: `..."plugin_name":"queue:inbound","plugin_version":"engine:0.8.10",...`
- runtime side: `..."plugin_name":"queue:inbound","plugin_version":"engine:0.8.9",...`
(The hashes derived from that material differ with it.) No ordering, count, or column differences. I did not tokenise-diff the other 16 cases; they share the same assertion and the same count, so same cause is inferred, not measured.

### Where the values come from
- Expected: stored golden, `docs/architecture/dag/scenario-corpus/v1/manifest.yaml`. Count of `engine:0.8.10` / `engine:0.8.9` in it: 6825994b4 = 0/7, 25301aa93 = 0/7, a67628c29 = 7/0.
- Actual: `src/elspeth/engine/orchestrator/landscape_registration.py:64` builds `plugin_version=f"engine:{ENGINE_VERSION}"` from `elspeth.__version__`, which `src/elspeth/__init__.py` reads via `importlib.metadata.version("elspeth")`, NOT from pyproject.toml.
- The shared venv's installed `elspeth` metadata (editable install of /home/john/elspeth) reports `0.8.9` (`importlib.metadata.version('elspeth')` = 0.8.9). It is the same for every tree I ran, since all use that interpreter.

## Candidate culprit commits (measured via git log)
- `9aacf8439 test(dag): pin the 0.8.10 engine version in the scenario-corpus oracle`: the only commit in 6825994b4..a67628c29 touching the manifest. It moved the golden to engine:0.8.10.
- `65f51f3a3 release: version metadata and changelog for the 0.8.10 candidate`: bumps pyproject to 0.8.10 (the only commit in 25301aa93..a67628c29 touching pyproject.toml).
- No commits touched the test file or `landscape_registration.py` / `__init__.py` on either side.
- Remediation side (6825994b4..25301aa93): no changes to the manifest or the test file.

## Environment attribution
Environment is the cause of the mismatch, as measured: the golden (0.8.10) was bumped with pyproject, but the runtime version comes from installed package metadata, which in the shared venv is still 0.8.9. The extracts at 0.8.9 pass only because their golden also says 0.8.9, which coincides with the stale metadata. Evidence that would settle it fully but was NOT run: re-installing metadata (forbidden here) or running with a venv whose metadata is 0.8.10. `uv.lock`/`pyproject.toml` diff between a67628c29 and 25301aa93 is 20 lines each; the version/dependency lines were checked below only for relevance to hashing.
- uv.lock differences between a67628c29 and 25301aa93 include the elspeth version (0.8.10 vs 0.8.9) and bumps of litellm (1.102.2 vs 1.102.0) and two other packages (4.2.1 vs 4.0.7; 4.0.1 vs 4.0.0). I did not identify the latter packages or check them against the installed venv. They cannot be tied to a one-token plugin_version difference. (inferred)

## Inferred
- On a venv whose elspeth metadata is 0.8.10 (e.g. CI after `uv sync`), these 17 tests would be expected to pass at a67628c29/HEAD. Not measured.
- The 0.8.9 trees pass in this venv only by coincidence of version, not because of a code difference.
