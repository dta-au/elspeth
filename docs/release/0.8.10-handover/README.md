# 0.8.10 pause handover snapshot (2026-10-25)

0.8.10 assembly paused for several days on the maintainer's order. The live coordination files live under the gitignored `.claude/lanes/release-0.8.10/`, so this is a tracked copy. Home-directory paths have been rewritten to repo-relative form.

**Start here:** `HANDOVER-LEDGER.md` (the cold-start status report), then `RESUME-FIRST.md` (Task 0: make every test finish; done ONCE by named owners, first).

- `AGENT-BRIEF.md`, `PAUSE-PROTOCOL.md`: the lane rules at the pause.
- `HANDOVER-LEDGER-review.md`, `COLD-START-REVIEW.md`: the independent Fable reviews.
- `lanes/`: each lane's report with its `COLD START (paused 2026-10-25)` section.
- `u46/`: U-46 evidence index and the Mythos rulings the (a) design must satisfy. These include the evidence-producing instrument versions (`corpus_compare.py`, `u46oracle.py`), which differ from the later tracked copies on `codex/0.8.10-u46-diagnosis`.
- `instruments/`: small harnesses and captures that lanes' next steps depend on, copied out of gitignored worktree lane dirs and a session scratchpad.

This is pause scaffolding, not release content. Delete this directory before the 0.8.10 merge to `main`, or keep the parts worth keeping. The large evidence (54 MB corpora, run logs) stays in the lane dirs.
- Harness `.py` files are stored as `.py.txt` so the repo-wide ruff gate does not lint archived scratch tools. Rename them back to run them.
