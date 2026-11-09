# Tracker excerpt

Read from canonical Composer Tool Remediation Tracker, 9 November 2026. This excerpt is reference only; Page remains the current checklist.

- [ ] **13.01** Render planner guidance using the planner’s actual tool palette.

- [ ] **13.02** Remove or explicitly scope instructions to call unavailable mutation tools.

- [ ] **13.03** Scope plugin hints and catalog repair sequences to the receiving route.

- [ ] **13.04** Remove planner completion instructions requiring an unavailable or meaningless preview/diff.

- [ ] **13.05** Teach the supported planner inline-blob authoring path.

- [ ] **13.06** Remove freeform instructions to inspect a planner-only information manifest.

- [ ] **13.07** Correct MCP hints recommending unavailable blob-binding or profile operations.

- [ ] **13.08** Correct the claim that answered discovery rereads are refused.

- [ ] **13.09** Align discovery-cap settings, validation warnings and skills with actual route behavior.

- [ ] **13.10** Correct full-replacement-options-object wording for merge-patch tools.

- [ ] **13.11** Correct listing/schema guidance discouraging a schema read required by admission.

- [ ] **13.12** Correct claims about sink edges and scalar routing.

- [ ] **13.13** Correct review guidance about an exact draft argument versus omission.

- [ ] **13.14** Declare planner response variants and strict required-null argument behavior accurately.

- [ ] **13.15** Add palette-parity tests covering system prompts, aids, plugin hints and repair text.

- [x] **13.16** Tokenize expression-operator parity tests so removing < or > makes them fail. **Verified — 9 November 2026, commit 5e192342c:** exact entry parsing preserves multiword operators and existing AST/constant checks. Six enduring mutation controls remove <, >, /, ==, is not and not in, then call the real parity assertion. Original oracle missed four removals; repaired oracle detects all six. Full discovery module: 109 passed; final combined check: 783 passed. Astra high approved; integrated.

- [ ] **13.17** Update declaration, wire, skill and planner hashes with each intentional contract/text change.

