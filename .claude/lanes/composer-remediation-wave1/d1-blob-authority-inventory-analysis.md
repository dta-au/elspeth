# D1 blob authority inventory attribution

Read-only diagnosis at base `ff755b12b5c01bcdc3e060a8f2d93b0219518de6`; production patch remained frozen. No production files, tests, policies, reviewed identities, signatures, or keys were changed/acquired.

## Result

All seven reported failures are exact-identity line drift. The canonical producer detects no arrival, departure, duplication, changed writer fingerprint, changed authority, changed escaping-connection status, or changed path/symbol/table/operation/ordinal in the compared old/new source multisets. This is source-attribution proof, not admission or a passing gate.

Raw comparison log:

```
repository old 80 new 80 equal_except_line True seconds 1.91
service_and_composer old 25 new 25 equal_except_line True seconds 2.46
service old 22 new 22 equal_except_line True seconds 2.15
proposal_receipts old 90 new 90 equal_except_line True seconds 2.61
other_changed_production old 0 new 0 equal_except_line True seconds 6.79
observed_sources_frozen 13 True
```

Complete old/new multisets, exact unexpected/stale identities, line pairings, and observed-source hashes are in `d1-blob-authority-identity-delta.json`. The scripts and log are lane-private evidence, never-stage.

## Method and controls

* Imported the unchanged gate module from the worktree and asserted both production package roots resolve to this worktree.
* Called the actual `scan_production_writers` with the four distinct file scopes the failing tests use (repository alone; service+composer; service alone; repository+proposal effects+mutation capabilities). Also scanned the four remaining changed production files, which produced no writer identities in either source view.
* Old source is `git show BASE:path` for each changed production file. Temporary in-process `Path.read_bytes` and `Path.read_text` overlays cover both the scanner input and imported-helper reads. Unchanged files are shared between views. No alternate regex/counting producer was used.
* Compared complete `Counter` multisets using the real `_identity_key`; attribution removes only the `line` field using `dataclasses.replace(site, line=0)`. Identical-site negative control passes; line-only mutation fails the real identity comparison but passes attribution. Changed fingerprint, authority, ordinal, escape, and duplicate-count positive controls all fail attribution.
* Canonical producer synthetic controls: exact `blobs/delete` is found; the same connection executing `select(blobs_table)` produces no writer. Controls patch only synthetic input reads in memory and leave files untouched.
* All 13 source files observed by the producer retained identical SHA-256 values before/after measurement. Recheck retained source hashes before using this evidence on a later tree.

## Failure-level evidence

| Gate scope | Identities old/new | Exact unexpected/stale now | Only line differs |
|---|---:|---:|---|
| blob replacement facet | 7/7 | 7/7 | yes |
| output storage facets | 6/6 | 6/6 | yes |
| blob service metadata writes | 7/7 | 7/7 | yes |
| blob service contained acquisitions (assertion after first failure) | 9/9 | 9/9 | yes |
| atomic deletion retirement | 1/1 | 1/1 | yes |
| custody read admission (two failing tests share this baseline) | 1/1 | 1/1 | yes |
| proposal effect receipts | 3/3 | 1/1 | yes |

The custody injection test fails on its unchanged-source baseline subtraction for its final case, before testing the final case’s injected mutation/escape. The earlier run is therefore not evidence that every injection case executed. It requires a fresh completed run after reviewed line re-derivation.

## Exact refresh candidates

There are 101 distinct reviewed identities with moved source locations: 79 in `coordination/repository.py` and 22 in `blobs/service.py`. The service set contains 16 mutation/contained-acquisition entries and 6 reviewed read entries. All 80 repository producer sites move; only 79 are reviewed. The table below derives candidates by exact no-line identity join, not by editing signatures or guessing a line offset.

| Manifest | Symbol | Table / operation | Fingerprint / ordinal | Old → new line |
|---|---|---|---|---:|
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.commit_blob_replacement` | output_storage_reservations / update | `eeb24b28f3f2ab1c` / 1 | 2559 → 2628 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations._settle_purged_blob_output_storage` | output_storage_reservations / update | `3d723c4f6c791eec` / 1 | 3559 → 3678 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations._settle_purged_blob_output_storage` | output_storage_reservations / delete | `3d723c4f6c791eec` / 1 | 3561 → 3680 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations._set_run_output_storage` | output_storage_reservations / insert | `8c40ff41aedf8438` / 1 | 3887 → 4006 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations._set_run_output_storage` | output_storage_reservations / update | `8c40ff41aedf8438` / 1 | 3890 → 4009 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations._set_run_output_storage` | blobs / update | `4cfeafb2df7dcb9d` / 1 | 3895 → 4014 |
| _REVIEWED_WRITERS | `BlobServiceImpl._cleanup_blobs_for_fork_sync` | <sessions-write-connection> / write_connection | `ceea1c242cbfdb83` / 1 | 4291 → 4372 |
| _REVIEWED_WRITERS | `BlobServiceImpl._cleanup_blobs_for_fork_sync` | <sessions-write-connection> / write_connection | `ceea1c242cbfdb83` / 2 | 4334 → 4415 |
| _REVIEWED_WRITERS | `BlobServiceImpl._cleanup_blobs_for_fork_sync` | <sessions-write-connection> / write_connection | `ceea1c242cbfdb83` / 3 | 4447 → 4528 |
| _REVIEWED_WRITERS | `BlobServiceImpl._cleanup_blobs_for_fork_sync` | <sessions-write-connection> / write_connection | `ceea1c242cbfdb83` / 4 | 4371 → 4452 |
| _REVIEWED_WRITERS | `BlobServiceImpl._delete_fork_blob_row_locked` | blob_deletion_cleanups / insert | `16da07b191192600` / 1 | 4234 → 4315 |
| _REVIEWED_WRITERS | `BlobServiceImpl._delete_fork_blob_row_locked` | blobs / delete | `16da07b191192600` / 1 | 4244 → 4325 |
| _REVIEWED_WRITERS | `BlobServiceImpl._finalize_registered_fork_blob_deletion` | <sessions-write-connection> / write_connection | `1039c549bf5d962c` / 1 | 4263 → 4344 |
| _REVIEWED_WRITERS | `BlobServiceImpl._finalize_registered_fork_blob_deletion` | blob_deletion_cleanups / delete | `9adc75e8d72e71ad` / 1 | 4266 → 4347 |
| _REVIEWED_WRITERS | `_discard_nonidempotent_reservation` | <sessions-write-connection> / write_connection | `726caad0e48b0bcc` / 1 | 1458 → 1467 |
| _REVIEWED_WRITERS | `_discard_nonidempotent_reservation` | blobs / delete | `dfb9e7995e5fc483` / 1 | 1460 → 1469 |
| _REVIEWED_WRITERS | `_finalize_reserved_blob` | <sessions-write-connection> / write_connection | `9f94f34df90428d1` / 1 | 1431 → 1440 |
| _REVIEWED_WRITERS | `_finalize_reserved_blob` | blobs / update | `69ac6eeb2d6a3bb4` / 1 | 1441 → 1450 |
| _REVIEWED_WRITERS | `_insert_pending_blob_row` | blobs / insert | `bb9d60d5e991d99d` / 1 | 1317 → 1326 |
| _REVIEWED_WRITERS | `_reserve_pending_blob` | <sessions-write-connection> / write_connection | `58557289c6621c6c` / 1 | 1353 → 1362 |
| _REVIEWED_WRITERS | `persist_inline_custody_blob_on_connection` | blobs / update | `8269e201d89d7e63` / 1 | 2057 → 2066 |
| _REVIEWED_WRITERS | `reconcile_inline_custody_publications` | <sessions-write-connection> / write_connection | `c29cebfe705db1ba` / 1 | 1893 → 1902 |
| _REVIEWED_WRITERS | `_ForkChildSessionMutations.append_child_messages` | chat_messages / insert | `b3fcd50e04854888` / 1 | 4275 → 4394 |
| _REVIEWED_WRITERS | `_ForkChildSessionMutations.insert_child_state` | composition_states / insert | `a20e7856361bd56b` / 1 | 4222 → 4341 |
| _REVIEWED_WRITERS | `_ForkParentReceiptMutations.bind_fork_receipt` | session_operation_receipts / update | `93116a46895f86ed` / 1 | 4404 → 4523 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations._adopt_pending_run_outputs` | blobs / update | `d8e2ea36cf4bd35c` / 1 | 3699 → 3818 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations._record_applied_blob_proposal_effect` | proposal_blob_effect_receipts / insert | `8c66fbb679cf7fa3` / 1 | 3243 → 3312 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.abort_blob_deletion` | blob_deletion_cleanups / delete | `4f9e61b1cf7247b0` / 1 | 3598 → 3717 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.abort_blob_replacement` | blob_replacement_cleanups / delete | `75415f0d12b7a661` / 1 | 2618 → 2687 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.commit_blob_deletion` | blob_deletion_cleanups / update | `32bd4fca1428b085` / 1 | 3511 → 3630 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.commit_blob_deletion` | blobs / delete | `87f41b9adb5c8772` / 1 | 3518 → 3637 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.commit_blob_replacement` | blob_replacement_cleanups / update | `2ecd759927d60392` / 1 | 2524 → 2593 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.commit_blob_replacement` | blobs / update | `d2ea73ee0e8472e4` / 1 | 2532 → 2601 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.discard_pending_blob` | blobs / delete | `0e357be387ddc260` / 1 | 2945 → 3014 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.finalize_pending_output_blob` | blobs / update | `d2eb84d546dbcdd2` / 1 | 2721 → 2790 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.insert_blob_inline_resolutions` | blob_inline_resolutions / insert | `4c42110eaa7c5994` / 1 | 4067 → 4186 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.insert_blob_run_link` | blob_run_links / insert | `05fd90838bf9a030` / 1 | 3623 → 3742 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.mark_blob_deletion_staged` | blob_deletion_cleanups / update | `afe03ed89b5a16c7` / 1 | 3470 → 3589 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.mark_blob_ready` | blobs / update | `3fd8ace829dbb08b` / 1 | 2916 → 2985 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.mark_blob_replacement_staged` | blob_replacement_cleanups / update | `6128417bd9a69f02` / 1 | 2465 → 2547 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.mark_run_output_blob_error` | blobs / update | `f7b3307ef1e90c89` / 1 | 3984 → 4103 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.mark_run_output_blob_ready` | blobs / update | `a2e525368ada9268` / 1 | 3956 → 4075 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.prepare_blob_deletion` | blob_deletion_cleanups / insert | `850298970c19565f` / 1 | 3320 → 3439 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.prepare_blob_replacement` | blob_replacement_cleanups / insert | `bca2ee74a8a93022` / 1 | 2402 → 2484 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.reserve_blob` | blobs / insert | `d112eae374c9b904` / 1 | 2838 → 2907 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.reserve_blob` | blobs / update | `d4eba14bc84728e8` / 1 | 2791 → 2860 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.reserve_pending_output_blob` | blobs / insert | `c997a216a1a51351` / 1 | 2637 → 2706 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.retire_abandoned_blob_reservation` | blobs / delete | `d22b3792b721f8ac` / 1 | 3012 → 3081 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.retire_atomic_blob_deletion` | blob_deletion_cleanups / delete | `2f1a2d64948bf4b7` / 1 | 3421 → 3540 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.retire_blob_deletion` | blob_deletion_cleanups / delete | `4f9e61b1cf7247b0` / 1 | 3580 → 3699 |
| _REVIEWED_WRITERS | `_RepositoryBlobMutations.retire_blob_replacement` | blob_replacement_cleanups / delete | `75415f0d12b7a661` / 1 | 2598 → 2667 |
| _REVIEWED_WRITERS | `_RepositoryComposerCompletionMutations.mark_ready_for_review` | composer_completion_events / insert | `b0eb63d6e0d9027b` / 1 | 6110 → 6229 |
| _REVIEWED_WRITERS | `_RepositoryComposerCompletionMutations.record_yaml_export` | composer_completion_events / insert | `2e42be4b632cb581` / 1 | 6134 → 6253 |
| _REVIEWED_WRITERS | `_RepositoryCompositionStateMutations.append_state` | composition_states / insert | `ac69541fd42b27b5` / 1 | 897 → 898 |
| _REVIEWED_WRITERS | `_RepositoryInterpretationMutations._create_pending` | composition_states / insert | `41c9e2e9e0ed6283` / 1 | 1379 → 1380 |
| _REVIEWED_WRITERS | `_RepositoryInterpretationMutations._create_pending` | interpretation_events / insert | `0a4fa2d4c69e1069` / 1 | 1302 → 1303 |
| _REVIEWED_WRITERS | `_RepositoryInterpretationMutations._create_pending` | interpretation_events / insert | `146bbcd39bedce3b` / 1 | 1330 → 1331 |
| _REVIEWED_WRITERS | `_RepositoryInterpretationMutations._create_pending` | interpretation_events / update | `b62dec99662793d2` / 1 | 1225 → 1226 |
| _REVIEWED_WRITERS | `_RepositoryInterpretationMutations.record_auto_interpreted_no_surfaces_event` | interpretation_events / insert | `9cb6f5b7f0c9310b` / 1 | 1514 → 1515 |
| _REVIEWED_WRITERS | `_RepositoryInterpretationMutations.record_session_opt_out` | interpretation_events / insert | `b12fb9db66a3bddd` / 1 | 1455 → 1456 |
| _REVIEWED_WRITERS | `_RepositoryInterpretationMutations.record_session_opt_out` | sessions / update | `3966641511f4795d` / 1 | 1483 → 1484 |
| _REVIEWED_WRITERS | `_RepositoryInterpretationMutations.resolve_pending_event` | interpretation_events / update | `569666d773a0b112` / 1 | 1572 → 1573 |
| _REVIEWED_WRITERS | `_RepositoryRunMutations.append_run_event` | run_events / insert | `5009526a773c2d16` / 1 | 1994 → 1995 |
| _REVIEWED_WRITERS | `_RepositoryRunMutations.complete_admission_refusal` | runs / update | `ab417310ef40b484` / 1 | 1773 → 1774 |
| _REVIEWED_WRITERS | `_RepositoryRunMutations.create_pending_run` | run_execution_inputs / insert | `46b7dd19a3e250fc` / 1 | 1695 → 1696 |
| _REVIEWED_WRITERS | `_RepositoryRunMutations.create_pending_run` | runs / insert | `6e3fcabf86bb6ffa` / 1 | 1678 → 1679 |
| _REVIEWED_WRITERS | `_RepositoryRunMutations.create_pending_run` | runs / update | `46b7dd19a3e250fc` / 1 | 1699 → 1700 |
| _REVIEWED_WRITERS | `_RepositoryRunMutations.mark_recovery_outputs_finalized` | runs / update | `1822dba79eb9ed3d` / 1 | 1816 → 1817 |
| _REVIEWED_WRITERS | `_RepositoryRunMutations.mark_recovery_required` | runs / update | `6613984de3a26bff` / 1 | 1835 → 1836 |
| _REVIEWED_WRITERS | `_RepositoryRunMutations.rebind_run_ownership` | runs / update | `911ff034cb757589` / 1 | 1800 → 1801 |
| _REVIEWED_WRITERS | `_RepositoryRunMutations.transition_run_status` | runs / update | `f926157e24accee8` / 1 | 1895 → 1896 |
| _REVIEWED_WRITERS | `_RepositorySessionMutations.decide_and_soft_archive` | sessions / update | `2aec308084effcf4` / 1 | 842 → 843 |
| _REVIEWED_WRITERS | `_RepositorySessionMutations.mark_session_updated` | sessions / update | `44e58336446946af` / 1 | 676 → 677 |
| _REVIEWED_WRITERS | `_RepositorySessionMutations.record_composition_rejection` | composition_rejection_events / insert | `e0386cbdb277f0b0` / 1 | 711 → 712 |
| _REVIEWED_WRITERS | `_RepositorySessionMutations.record_plugin_crash_breadcrumb` | sessions / update | `016ef4c50b5e5390` / 1 | 650 → 651 |
| _REVIEWED_WRITERS | `_RepositorySessionMutations.set_title` | sessions / update | `feed562da5394634` / 1 | 745 → 746 |
| _REVIEWED_WRITERS | `_SessionOperationAuthorityRepository._admit_blob_read` | session_read_admissions / delete | `8cf4bc5cc87457bd` / 1 | 5110 → 5229 |
| _REVIEWED_WRITERS | `_SessionOperationAuthorityRepository._admit_blob_read` | session_read_admissions / insert | `8cf4bc5cc87457bd` / 1 | 5118 → 5237 |
| _REVIEWED_WRITERS | `_SessionOperationAuthorityRepository._advance_exclusive_fence_on_connection` | session_operation_fences / update | `4ad5425d08f127f8` / 1 | 4988 → 5107 |
| _REVIEWED_WRITERS | `_SessionOperationAuthorityRepository._compare_and_swap_on_connection` | session_operation_fences / update | `ff923dcba8b3e8e7` / 1 | 5236 → 5355 |
| _REVIEWED_WRITERS | `_SessionOperationAuthorityRepository._insert_fork_child` | session_operation_fences / insert | `d5ab9aa3497d191a` / 1 | 5639 → 5758 |
| _REVIEWED_WRITERS | `_SessionOperationAuthorityRepository._insert_fork_child` | session_operation_fences / update | `7326f9c03db2a1c7` / 1 | 5653 → 5772 |
| _REVIEWED_WRITERS | `_SessionOperationAuthorityRepository._insert_fork_child` | sessions / insert | `b964b22650d92b97` / 1 | 5624 → 5743 |
| _REVIEWED_WRITERS | `_SessionOperationAuthorityRepository._resume_or_take_over_fork_child` | session_operation_fences / update | `988ac755147ef9bc` / 1 | 5714 → 5833 |
| _REVIEWED_WRITERS | `_SessionOperationAuthorityRepository.archive_delete` | sessions / delete | `66cc108182d86009` / 1 | 5890 → 6009 |
| _REVIEWED_WRITERS | `_SessionOperationAuthorityRepository.create_session_with_initial_fence` | session_operation_fences / insert | `3d19f2b10e4f6a34` / 1 | 4864 → 4983 |
| _REVIEWED_WRITERS | `_SessionOperationAuthorityRepository.create_session_with_initial_fence` | session_operation_fences / update | `3d19f2b10e4f6a34` / 1 | 4878 → 4997 |
| _REVIEWED_WRITERS | `_SessionOperationAuthorityRepository.create_session_with_initial_fence` | sessions / insert | `3d19f2b10e4f6a34` / 1 | 4854 → 4973 |
| _REVIEWED_WRITERS | `_SessionOperationAuthorityRepository.mutate_fork_creation` | <sessions-write-connection> / write_connection | `7b84539400c1c53a` / 1 | 5760 → 5879 |
| _REVIEWED_WRITERS | `_SessionOperationAuthorityRepository.release` | session_operation_fences / update | `bafc06df8fd8905b` / 1 | 5865 → 5984 |
| _REVIEWED_WRITERS | `_SessionOperationAuthorityRepository.release` | session_read_admissions / delete | `bafc06df8fd8905b` / 1 | 5855 → 5974 |
| _REVIEWED_WRITERS | `_SessionOperationAuthorityRepository.renew` | session_operation_fences / update | `d41bbaef242667cd` / 1 | 5315 → 5434 |
| _REVIEWED_WRITERS | `_SessionOperationAuthorityRepository.renew` | session_read_admissions / update | `d41bbaef242667cd` / 1 | 5302 → 5421 |
| _REVIEWED_WRITERS | `_SessionOperationAuthorityRepository.renew_fork_child_lease` | session_operation_fences / update | `b268c9591db479c5` / 1 | 5419 → 5538 |
| _REVIEWED_READ_CONNECTIONS | `BlobServiceImpl._stage_output_blob_error_and_remove_bytes` | <sessions-write-connection> / write_connection | `990aa332b8773eb8` / 1 | 3944 → 4025 |
| _REVIEWED_READ_CONNECTIONS | `BlobServiceImpl.copy_blobs_for_fork._verify_exact_target` | <sessions-write-connection> / write_connection | `4536b6d6104a3575` / 1 | 4153 → 4234 |
| _REVIEWED_READ_CONNECTIONS | `BlobServiceImpl.copy_blobs_for_fork._verify_plan_and_quota` | <sessions-write-connection> / write_connection | `3a607d21bd66351e` / 1 | 4028 → 4109 |
| _REVIEWED_READ_CONNECTIONS | `BlobServiceImpl.get_blob_run_links._sync` | <sessions-write-connection> / write_connection | `793b2531e21a4ec2` / 1 | 3530 → 3611 |
| _REVIEWED_READ_CONNECTIONS | `BlobServiceImpl.list_blobs._sync` | <sessions-write-connection> / write_connection | `4fa518a6c8ccdf63` / 1 | 2865 → 2874 |
| _REVIEWED_READ_CONNECTIONS | `publish_inline_custody_publication_locked` | <sessions-write-connection> / write_connection | `ef84064b21eb8128` / 1 | 1796 → 1805 |
| _REVIEWED_READ_CONNECTIONS | `_SessionOperationAuthorityRepository._session_exists` | <sessions-write-connection> / write_connection | `f98bc6d74193a045` / 1 | 4909 → 5028 |

## Existing debt stays excluded

The repository scope already emits one unreviewed escaping connection before D1: `_SessionOperationAuthorityRepository.__build_locked_fork_pair_controls.locked_pair_transaction`, `<sessions-write-connection>/write_connection`, fingerprint `749bb668e4cee8c8`, ordinal 1, authority `SessionOperationAuthority`, `connection_escape=True`, old line 4732 → new line 4851. Do not add it to any reviewed tuple. This unchanged baseline gap is distinct from the 101 already-reviewed line bindings.

## Canonical review/remedy

1. Keep the frozen production patch. Review its behavior separately; unchanged writer fingerprints do not prove unchanged prerequisites, quota behavior, locks, or runtime safety.
2. Follow `CONTRIBUTING.md:587` (Sessions mutation-authority gate): re-derive the literal manifest entries with this gate’s `scan_production_writers` and review their complete identity diff. The manifest comment at gate line 1480 admits only writers already routed through exact table-specific typed authorities. No dedicated Sessions regeneration command was found in `scripts/`, `.agents/skills/`, or the searched maintainer/gate documentation; do not substitute Landscape’s `scripts/fencing_inventory.py`.
3. In a separately authorized reviewed refresh, update only the 101 derived line fields for already-reviewed entries. Preserve fingerprints, ordinals, authority bindings, table policy, source shape, and `_identity_key`; do not admit the existing escape or widen a policy. Search other literal site assertions against the producer as well, since the reported selection covers only nine tests.
4. Run the selected affected authority tests to completion with current worktree package provenance and frozen source hashes. Then run every affected Sessions authority gate that consumes the changed repository/service files; the 80/22 moved sites have broader reach than the seven reported assertion failures. Report existing whole-tree expected-failure debt separately. No gate is cleared by this diagnostic.
5. HMAC trust-tier signing is a separate operator-controlled workflow. These Python writer tuples are not HMAC signatures; no signing-key request or signature manipulation is needed for their source-derived line refresh. Preserve any separate trust-tier review/signing obligations caused by the production patch.

## Risks and limits

* Complete producer equality except line is bounded to the changed production files and exact affected test scopes. It does not certify unrelated tree-wide inventories or runtime behavior.
* Every repository identity moved because code was inserted above writer sites; different blocks have different line deltas. A global offset or source padding would be incorrect and would obscure honest source changes.
* The existing 7-failed/1-passed/1-xfailed log remains red. This task did not alter expectations or rerun the heavy test selection.

## Final None-adapter bounded recheck

After the authorized adapter return change, the canonical scanner was run only on `composer/tools/blobs.py` and `composer/tool_batch.py`. It emitted the same three exact composer read identities (lines 501, 536, 561), and no tool-batch writer identities. Exact identity subtraction returned no unexpected/stale entries. Both repository and service hashes match the original retained measurement; all four recheck inputs stayed frozen. The 101 line-refresh candidates remain valid. Evidence: `d1-blob-authority-final-adapter-recheck.json`.
