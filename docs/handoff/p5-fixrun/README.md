# p5 plugin fix run: paused handoff (2026-11-15)

Temporary transfer branch for a hardware move. Do not merge it; delete it once the run has resumed.

- `RESUME.md`: run state, rulings, lane table and landing procedure (a copy of `.claude/lanes/fixrun/p5/RESUME.md`).
- `lanes.tar.gz`: the gitignored `.claude/lanes/` tree (lane-manager status.json, report.md, briefs, tickets, the drive/suite scripts, logs).
  Base-tree snapshots were excluded because `git archive <sha>` regenerates them; files over 5 MB (final-gate logs) were excluded too. The `*.exclude` files list both.
- `scratchpad.tar.gz`: the session scratchpad, packed the same way.

Restore from the repository root:

```bash
mkdir -p .claude/lanes && tar -xzf docs/handoff/p5-fixrun/lanes.tar.gz -C .claude/lanes
```

The unlanded code is on the pushed `lane/*` branches; see RESUME.md for the list.
