# Prompt for the implementation agent

Use the following brief with this folder available in the repository.

---

Close the confirmed 0.8.10 release gaps described in this package. First read
README.md, CONFIRMED-GAPS.md, OPEN-QUESTIONS.md and VALIDATION.md, then the
repository's current AGENTS.md and CONTRIBUTING.md whole-tree gate guidance.

The original full audit was at `a6990ffb`; the **6 November open-item refresh is
pinned to `ae88147c6130051b1e3d497a1341a69bd56924e5`** on
`release/0.8.10-assembly`. It inspected production paths, test bodies and bounded
existing raw run receipts; it did not execute new tests. Treat findings as starting points,
not permission to skip independent examination or to assume the current branch
is unchanged. Recheck each finding on the receiving source and document any
already-fixed disposition with code and tests.

Preserve existing uncommitted work and unrelated worktrees. Use an isolated
worktree for implementation where necessary; do not switch or clean the shared
checkout. Keep source under a normal worktree, never in a lane directory or
`/tmp`. Verify worktree import provenance for both `elspeth` and
`elspeth_lints`; do not reinstall through a shared virtual-environment symlink.

Use relevant specialists and independent reviewers in proportion to the work.
Assign one owner for overlapping runtime files and proof bindings. Have agents
write bounded reports to lane notes and return their paths; durable delivery
notes belong under `docs/`. Establish host test capacity before starting suites.

Address only the remaining portions of G01–G07, subject to current source
revalidation. Later-Compose quantum/watermark successors are now implemented:
do not redo them. G01 concerns remaining eligible cuts; G02 now has two stale
source guards. G03–G07 remain concrete test/documentation inconsistencies.
Do not restore obsolete service methods or legacy database compatibility merely
to satisfy stale tests. Do not weaken proof assertions or blindly regenerate
hashes. Preserve the behavioral intent of migrated tests.

For O01–O08, first determine whether current source, meaningful tests or an
explicit superseding decision already resolves the question. An audit's failure
to locate something is not evidence that it does not exist. Implement missing
acceptance work where the existing requirement is clear. If a product tradeoff
or requirement change is needed, present the precise decision with evidence;
continue independent work while it is unresolved. Do not silently enlarge
attachment limits, remove safety rescans or treat documentation bindings as a
new enforcement mechanism.

O03 now includes an actual failed larger-history `read_end_retry` run whose
production/test/configuration bytes match the refresh commit. Inspect its
recorded evidence and current receiving before diagnosing/fixing the preparation
failure. Its whole-advance time does not isolate the preparation hotspot. O02
has additional joined cold handover coverage; O05 now credits migrated current
TITLE PostgreSQL tests. Complete only their remaining combined-scenario/matrix
work. Previously completed units are not in scope for a blanket re-audit;
reopen one only for a specific source/test change or other concrete evidence.

Keep the current single Composer lifecycle, provider-authored pipeline invariant,
ordinary/tutorial parity, paid-call nonreplay, atomic ownership transfer,
explicit unsupported-format refusal and signing suspension. Apply the 6 November
pre-1.0 no-backwards-compatibility ruling: remove obsolete compatibility paths as
current callers are consolidated, preserve records/evidence/backups, state actual
breaking changes, and do not mistake a version suffix for obsolescence. No operator-key access,
signing, rekeying, baseline regeneration, deployment, database reset or tool
cleanup is part of this brief. Publication/merge actions follow the user's
actual instructions and the repository's current delivery rules; this handoff
does not grant additional authority for them.

For each completed gap, report:

1. The receiving commit and whether the original finding reproduced.
2. The actual correction and relevant source/test paths.
3. Meaningful focused validation and sensitivity controls, with process exit
   codes; name unrun checks and their reason.
4. Any remaining scope decision or acceptance limitation, without representing
   it as an implementation defect.

Before completion, review the final integrated diff and run the gates appropriate
to its reach. Shared runtime/schema/lock changes require the repository's broad
and serial PostgreSQL gates, not just local unit tests. Re-derive source-bound
proof inputs against that final tree. Use keyless finding sets for trust-tier
tracking and keep release qualification separate from implementation completion.

Deliver a concise gap-by-gap disposition, implementation commits as applicable,
validation results and the remaining decisions. Do not claim release-ready until
the actual required final-source receipts support it.
