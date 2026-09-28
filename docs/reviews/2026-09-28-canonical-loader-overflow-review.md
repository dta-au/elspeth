# Canonical loader overflow fix: independent review

**Verdict: GO for the code fix.** I found no remaining release blocker in the two-file change against `b2da1e6f50b64055928bb0a1013f60e45970282c`. The final frozen static gate and a clean commit are still required before the local fast-forward; they are delivery checks, not unresolved code findings.

## Scope and reasoning

I read the complete changed files, `src/elspeth/contracts/hashing.py` and `tests/unit/contracts/test_hashing.py`, and compared their diff with the base. The canonical loader now checks that both integer-form and exponent/decimal-form numeric tokens produce finite floats (`hashing.py:95-120,148-153`). It raises `json.JSONDecodeError` for overflow rather than returning infinity. Finite values, including `1e308`, retain their prior decoding behavior (`test_hashing.py:412-422`). The encoder and hash functions are unchanged.

My initial review found that a 5001-digit JSON integer raised a raw Python `ValueError` at `int(literal)`, bypassing callers that classify `JSONDecodeError` as corrupt persisted JSON. The candidate now converts that failure to `JSONDecodeError` (`hashing.py:95-102`) and tests the case (`test_hashing.py:417-419`). This finding is resolved.

The new checks preserve the loader's existing inverse rule: safe integer literals remain `int`; larger integral literals that the canonical encoder can produce from a double become `float`. No new accepted non-finite state was apparent in either numeric parser.

## Verification and delivery boundary

- `/tmp/canonical-loader-focused-final-20260928.log` records **210 passed, 2 warnings**, exit 0, for hashing tests and downstream sink-effect/row-routing tests on the final candidate. `git diff --check` is clean.
- The broad candidate suite was reported as exit 1 with 5 failures and 61,316 passed. The same five node IDs fail serially on unchanged base in `/tmp/canonical-loader-five-baseline-20260928.log`; I checked that its failure list matches `/tmp/canonical-loader-five-serial-20260928.log`. They concern plugin assistance, protocol fields, plugin dispatch, MCP summary truthiness, and session DB authority inventory, outside the two changed files. This is baseline red, not a detected regression from the loader fix.
- At review time the candidate remains uncommitted; both `HEAD` and clean side branch `fix/5887-codex-final` point to `b2da1e6f50b64055928bb0a1013f60e45970282c`. The code is suitable to commit and fast-forward after the final frozen static gate passes and ancestry/status are rechecked. This review does not claim that integration has happened.
