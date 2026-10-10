# CLI continuation: extract lint argument grammar

**Choose `elspeth-lints/src/elspeth_lints/core/cli.py`, extracting its complete argparse grammar into `core/cli_parser.py`.** Keep main dispatch, finding computation, allowlist writes, signing, rekeying and command execution in the existing module. This separates an actual responsibility—CLI syntax, defaults, help and argument validation—without a forwarding file or a new command framework.

Analysis base is clean `d38890ddcc5c6e3d570af645d5e3a25efddfea2a`, atop published streaming `9f26833c8f6a7c56abd2c5b35c80484ebe0c82bc`. Both CLI files have identical blobs on original main `9e481bc85a32ee15f808c5e2b16cab7634e57b8e`, published streaming, integrated repair `703cdd1674d01d422baa4d54f210105edccf498c` and this HEAD:

```text
src/elspeth/cli.py: cc55e64847ec76446e639ec068026f9ecf927a27
elspeth-lints/src/elspeth_lints/core/cli.py: bb929eb4c8508d949446c6a0963cc77768146a69
```

This lane ran no pytest and made no repository changes. Counts below are static measurements; planned validation is not a passing result.

## Ownership and capacity

Neither CLI path is a literal member of the canonical 538 streaming paths (list digest `3149d9cf6e2b97e92a3e5002da4d0dc7a3c7430df0c5535eae7f8ad8366afc38`). Omission alone was not treated as release. Root subsequently obtained the streaming owner's explicit statement: “I release the narrow lint CLI parser extraction and its existing focused test coverage from streaming ownership. Keep dispatch, finding computation, signing/rekey behavior and policy unchanged.” The proposed new `elspeth-lints/src/elspeth_lints/core/cli_parser.py` does not exist and is absent from the list.

That release applies to this bounded grammar extraction, not the main application CLI or other runtime/harness files. Streaming lands first. Root has the initial serial, low-priority baseline slot and must coordinate again before later tests/gates because streaming browser acceptance is next. Full premerge verification has its own capacity requirement; no new broad suite should be started by an implementation subagent.

## Exact implementation map

Move these eight definitions intact, preserving names, signatures, bodies, error text and help strings:

| Definition | Body lines | Responsibility |
|---|---:|---|
| `_build_parser` | 1,030 | All command/option declarations; no handler dispatch |
| `_add_judge_transport_arg` | 22 | Shared transport flag and help |
| `_add_judge_tools_arg` | 27 | Shared investigation-tools flag and help |
| `_non_empty_string` | 36 | Audit-identity refusal rules |
| `_positive_int` | 9 | Positive integer converter |
| `_timezone_aware_datetime_arg` | 9 | Timezone-aware timestamp converter |
| `_bounded_rationale_string` | 15 | Nonempty UTF-8 byte-bounded rationale converter |
| `_sign_bundle_lanes` | 9 | Nonempty, recognized signing-lane selection |

Move the existing assignments for `JUSTIFY_RATIONALE_MAX_BYTES`, `OPERATOR_OVERRIDE_TOKEN_ENV`, `OPERATOR_OVERRIDE_TOKEN_SHA256_ENV`, `_MAX_AUDIT_IDENTITY_LENGTH`, `_CLI_TRANSPORT_CHOICES`, `_SIGN_BUNDLE_LANES`, and `_SIGN_BUNDLE_MAX_JUDGE_CONCURRENCY` into the same authority. Preserve order within the transport mapping and lane tuple. Place all definitions before they are needed when `_build_parser` is called; no new import-time parser construction.

The new module imports `argparse`, `datetime`, `Path`, `AuditReviewVerdict`, `is_substantive_audit_anchor`, and `TRANSPORT_AGENT`, `TRANSPORT_CODEX_CLI`, `TRANSPORT_OPENROUTER` from their existing owners. Keep postponed annotations. It must not import `cli`, any handler, or any signing transaction executor. The dependency is one-way: `cli -> cli_parser -> existing allowlist/judge definitions`.

Measured original: 6,110 lines. The selected function/assignment AST spans occupy 1,168 lines, leaving **4,942 before new imports and ordinary removal of vacated blank lines/comments**. A normal explicit import block fits below 5,000; final length must be measured after formatting. Do not manufacture the threshold by dense formatting. The new module will be roughly 1,200 lines. The exact grammar is substantial even though the original has limited spare room below the threshold.

Keep `main`, its command branch order and every `_run_*` function unchanged. `_build_parser` supplies argument objects only. This lint CLI is **argparse**, not Click/Typer; no command registration decorator moves. The main application's Typer tree is outside this slice.

### Existing bindings and patch semantics

Root explicitly approved preserving **all fifteen moved names** in `cli` through direct imports from their canonical owner, `cli_parser`: the eight functions and seven constants listed above. This includes the seven names without current consumers. Preserve existing imports and private bindings as requested, rather than deleting names merely because the current repository has no caller. Use explicit same-name re-export syntax where needed for ruff; do not wrap functions or copy values.

For every moved name, the binding in `cli` must be the identical object exposed by `cli_parser`. In particular, `cli._build_parser is cli_parser._build_parser` and `cli._CLI_TRANSPORT_CHOICES is cli_parser._CLI_TRANSPORT_CHOICES` must hold. The transport mapping is mutable: preserving its shared object preserves in-place mutation visibility through either import path. Creating a second dictionary with equal entries is insufficient.

The measured direct-import consumers of `_build_parser`, `JUSTIFY_RATIONALE_MAX_BYTES` and `_SIGN_BUNDLE_LANES` are `test_core_smoke.py`, `test_cli_sign_bundle.py`, `test_justify.py`, and `test_judge_mcp_server.py`. Their presence explains part of the compatibility requirement; it does not limit the approved fifteen-name import set.

The retained implementations also need these ordinary imports from the new owner:

| Original consumer | Imported dependency |
|---|---|
| `_operator_override_authorization_error`; `_JUDGE_SIGNING_ENV_FILE_KEYS` assignment | Both operator-override environment variable name constants |
| `main` | `_build_parser` |
| `_run_justify`, `_run_reaudit`, `_run_sign_judge_signatures`, `_run_check_judge_quality` | `_CLI_TRANSPORT_CHOICES` |
| `_run_sign_bundle` | `_CLI_TRANSPORT_CHOICES`, `_SIGN_BUNDLE_MAX_JUDGE_CONCURRENCY` |
| `_load_judge_signing_manifest`, `_execute_drift_repair_action` | `_bounded_rationale_string` |

The parser and converters intentionally change global ownership to `cli_parser`. Preserving all fifteen direct bindings is distinct from preserving old-module global lookup. Re-exporting a function does not make a subsequent assignment to `cli._non_empty_string`, `cli.JUSTIFY_RATIONALE_MAX_BYTES`, or `cli._CLI_TRANSPORT_CHOICES` replace the binding in that function's new globals. In-place dictionary mutation and rebinding a module attribute are different operations. The unobserved old-module rebinding behavior is not promised. Controlled scans of all Git-tracked Python found no moved-symbol literal patch target or `setattr`/`patch.object` target. A second controlled module-alias scan found 31 actual CLI patch calls across three test modules, all targeting these retained names:

`_append_judge_decision_event_after_judge`, `_emit_justify_output`, `_execute_new_judgment_action`, `_execute_rotation_action`, `_execute_stale_delete_action`, `_load_judge_signing_env_file`, `_pop_allow_hits_entry`, `_run_justify`, `_signing_specs_from_diagnosis`, `atomic_update_text`.

All their attribute-name arguments are literal; their intersection with moved names is empty. The same scan found no module-alias attribute read of moved names. These are current static consumer findings, not a promise about hypothetical external private monkeypatches. Preserve the observed seams and all fifteen canonical import bindings, and document ownership transfer. Those explicit imports are the approved compatibility surface; do not add a dynamic module proxy, `globals()` synchronization, reverse import, or wrappers that emulate old-module global lookup.

## Parser and output characterization

Before editing, capture the actual argparse command registry, ordered actions and help. Capture the same after extraction. Keep these fields exact: command names/order, argument spellings/order, destination, action class, `nargs`, required flags, choices, defaults, converter behavior, help and metavar. Normalize only the known moved converter identities in the diagnostic capture; do not normalize values, types, command names, whitespace or error messages to make a difference disappear.

Exercise root help and every registered subcommand help with fixed locale/terminal width; compare stdout, stderr and exit status byte-for-byte. The 17 existing main branches cover `check`, `dump-edges`, `rotate`, `justify`, `audit-verdict`, `reaudit`, `diagnose-judge-signatures`, `sign-judge-signatures`, `sign-bundle`, `rekey`, `migrate-judge-scope`, `check-judge-coverage`, `check-per-file-blanket-ratchet`, `check-judge-quality`, `check-trust-boundary-diff`, `check-rotation-audit`, and `check-override-rate`.

Use **parser-only** valid/invalid vectors for defaults, missing required arguments, unknown command/option, transport/tools selections, all verdict choices, lane normalization/refusal, integer bounds, timezone absence, multiline/insubstantial/overlength identity, and blank/UTF-8-overlimit rationale. Capture parsed namespace values and exact `SystemExit`/error output. Do not dispatch operational commands merely to test parsing. No real allowlist, signing bundle, provider, secret environment or judge process is needed. Mutate a captured option/default, an error/help byte and a converter result to prove the comparator rejects each change.

Compare moved function source spans and location-free ASTs exactly. Compare all retained function/class definitions exactly and top-level executable assignments modulo the explicit moved/imported declarations. Confirm all fifteen old import bindings are identical to the canonical objects in the new module, including the shared mutable transport dictionary, and that current private import consumers retain their bindings. Confirm all observed old patch targets still affect the unchanged handlers; do not treat this as a claim about unobserved rebinding of parser globals through the old module.

## Focused execution and gates

Root owns execution. Baseline and repeat the following existing modules, serially, with a complete private log and recorded exit code. All names are under `tests/unit/elspeth_lints/`:

```text
test_core_smoke.py
test_trust_tier_dump_edges.py
test_rotate_tier_model.py
test_rotate_exclude_judge_gated.py
test_justify.py
test_reaudit.py
test_reaudit_multi_rule.py
test_reaudit_sidecar.py
test_judge_tools.py
test_judge_signature_diagnosis.py
test_cli_sign_bundle.py
test_cli_rekey.py
test_cli_migrate_judge_scope.py
test_judge_coverage.py
test_per_file_blanket_ratchet.py
test_judge_quality.py
test_trust_boundary_diff.py
test_override_rate.py
test_allowlist_yaml_roundtrip_contract.py
test_judge_mcp_server.py
test_source_excerpt_security.py
test_pre_commit_triggers.py
```

Invoke the chosen path list through `.venv/bin/python -m pytest -n 0` with both worktree source roots exported in `PYTHONPATH` and passed through pytest's `-o pythonpath`. The signing/judge tests use their existing fixtures/doubles; they are not authorization to perform live signing, call an external judge or acquire an operator key. Recheck collected selection and source provenance on the actual baseline commit rather than reuse results from a prior slice.

Run ruff check/format on both source files and `git diff --check`. Freeze scanned Python inputs before/after the applicable whole-tree gates: mock discipline, canonical Python walker authority, and the masquerade live-scan and negative-control selections used by earlier slices. The controlled actual `iter_masquerade_sites` scan found no sites in the current lint CLI, and its parsed baseline has no row for this path; no baseline reseed is expected. New import resolution still warrants running the live gate.

No moved function has a boundary decorator, and no policy binding is proposed for relocation. Keep all signed YAML and its signatures unchanged. This is not an operator-signature clearance claim. Because `check --rules ...` parses through the changed grammar, run the keyless before/after lint corpus comparison and the explicit boundary rules through the normal entry point; require unchanged diagnostic sets and output/exit behavior, rather than assume a zero corpus. Do not execute signing/rekey commands against real state.

This is a production change to the entry infrastructure used by enforcement tooling, so the local focused proof is not the final premerge gate. Root should schedule the canonical full premerge verification after streaming's priority work and when capacity is released. The grammar move does not itself touch SQL, persistence, locks or schema and introduces no separate PostgreSQL-contention requirement. If dispatch, registry loading, finding computation or persistence behavior changes, the narrow release and this validation scope no longer apply.

## Why the other targets are not this slice

| Target | Measured dependency evidence | Exact remaining condition |
|---|---|---|
| `src/elspeth/cli.py` — 5,278 lines | One Typer root registers Composer/users, doctor and audit-export groups. The 287-line `health` command touches web probes, plugin discovery and optional DB connectivity; moving it alone leaves negligible room under 5,000 after registration/imports. Doctor commands call web deployment checks; Composer commands share identity retirement/session/audit helpers. Existing tests patch `elspeth.cli._load_settings_with_secrets`, `_load_dotenv`, `_composer_auth_provider`, and `_composer_auth_audit_recorder`. | Absent from literal 538, but the parser release does not authorize this Composer/admin/deployment integration surface. A later coherent doctor/health or audit-export module needs explicit registration ordering, callback/default/output characterization and owner coordination. Shared bootstrap extraction also requires broad runtime verification. Do not describe the entire file as a proven literal reservation. |
| `src/elspeth/engine/processor.py` — 5,523 lines | `RowProcessor` occupies 5,140 lines and already composes `DAGNavigator`, `SchedulerDrainCoordinator`, `BarrierIntakeCoordinator`, `BarrierRecoveryCoordinator`, `TokenTraversalEngine`, and `SchedulerWorkCodec`. Aggregation flush, committed output and group-loss settlement methods directly depend on audit repositories, scheduler authority, member/leader tokens and side-effect order. The new test decomposition leaves private production patch targets intact. | Not a literal 538 member; no blanket ownership claim. A meaningful production extraction needs an explicit owned collaborator/context (not a mixin or forwarding every method), runtime/engine integration coordination and full-suite capacity. Persistence/lock-related movement also requires PostgreSQL tests. It is technically broader than the released grammar slice. |
| `tests/fixtures/dag_scenario_corpus/harness.py` — 5,408 lines | Stable/semantic projections, 377-line audit-record projection, 348-line stable projection, 572-line public durable export conversion, and recovery runners share graph/runtime/audit state. `docs/architecture/dag/scenario-corpus/v1/manifest.yaml` and `tests/unit/architecture/test_dag_scenario_corpus_contract.py` are actual 538-list members; they pin the producer's projection bytes, evidence locators and hashes. | Harness itself is not a literal member. A projection/recovery extraction requires coordination with the reserved manifest/gate owner and real producer/oracle equality across affected scenarios, plus broad shared-harness validation capacity. Unchanged oracle bytes are required; a changed frozen oracle is semantic drift, not a routine re-pin. No manifest/gate edit is authorized by the lint-parser release. |

These are different conditions: only the narrow lint-parser ownership release is established; the other rows have specific integration/validation dependencies, not an invented blanket reservation. No source change is proposed for them now.

Supporting private static files are `cli-continuation-static.py/.json` and `cli-parser-bindings-static.py/.json`. The dependency instrument uses AST and `symtable` over Git-tracked sources, with positive/negative import-alias and global-reference controls; all tracked Python parsed successfully. Keep those exploratory instruments outside the repository's gate input tree. This note is the reviewable implementation plan; it does not create an approval manifest or claim completed validation.
