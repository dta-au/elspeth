# U-22 section 7 universal cap: parked 2026-10-25 06:10 for U-30 (lead's ruled order)

Branch codex/0.8.10-u22, commits 4916ff479 and d0dcfc321 on top of 451eec473.
Done: the cap at the physical seam for every role, at-cap and +1 tests for each role,
the e2e primary settlement test, settings tests, the composer_turn digest re-pin,
and the title-prelude source contract re-pin.
Open when resuming:
- The caller and gate run was stopped at about 40%, with many F/E results whose cause is not yet known.
  Re-run cap-callers2 (the file list is in the stopped command) and attribute each
  failure against a base export.
- The esbuild FileNotFoundError in test_composer_https_acceptance: confirm at the base.
- Check the masquerade, envelope and session-authority gates.
- The keyless set-diff.
- The u22 report: describe the cap, and note that retained attempts use the live setting
  and that the risk-assessment table predates the cap.
