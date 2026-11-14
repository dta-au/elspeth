# Astra combined review of the remediation range (summary only)

Scope: `git diff 5e192342c^ dd3b3524c` (17 commits), reviewed read-only by Astra on
10 November 2026. No tests were run. The reviewer's full draft was not written: its
sandbox allowed writes only in a sibling worktree. This file records the findings it
returned in its final message, verbatim in substance. Each finding still needs to be
reproduced before it is fixed.

## Findings

- **BLOCKER for 0.8.12 (reviewer's rating; pre-existing omission, not introduced by the
  range).** A pending `patch_node_options` proposal does not retain its inline blob
  dependencies. An admitted edit on an incomplete graph can await approval while its
  uploaded prompt blob is deleted or replaced. The tool is missing from both reference
  extraction and the retention query's allowlist. Reviewer confidence: high, callers
  traced. Location: `src/elspeth/web/sessions/proposal_blob_refs.py:26,115,231`.
- **SHOULD-FIX (regression from the path-admission change).** The new path checks reject
  valid Chroma client-mode configurations that carry `persist_directory: null`.
  Admission and saved-state preview fail although the plugin requires no persistence
  directory in client mode. The added tests pin the blanket rejection without a real
  Chroma positive control. Reviewer confidence: high, plugin contract and callers
  checked. Location: `src/elspeth/web/composer/tools/_common.py:2402` and the matching
  validation and materialization checks.
- **NOTE.** The second recovery registration added by `dc67bdd2f` is semantically safe
  on the shipped paths: standalone startup owns a fresh interpreter and refuses an
  existing owner; embedded MCP retains host ownership.

## Coverage limit

All changed production files were covered, but unchanged portions of several large
modules were not exhaustively reread.
