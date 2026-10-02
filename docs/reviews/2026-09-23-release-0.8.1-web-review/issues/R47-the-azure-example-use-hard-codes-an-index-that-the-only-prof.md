# R47. The Azure `example_use` hard-codes an index that the only profile's closed pin may reject

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Plugin policy (`azure_ai_search`) |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-19#3 |

## Finding

- **Location:** `src/elspeth/web/plugin_policy/profiles.py:1611-1621` and `:1253-1260`.
- **Wrong:** The example always says `index: approved-documents`, even when a single closed-pin profile turns `index` into an enum. The `list_transforms` response carries `example_use` (`inventory_response_contracts.py:294`).
- **Fix:** When the pin is closed, use the first pinned index. With several profiles, `available_aliases[0]` can itself be a closed pin, so handle that case too.
- **Sources:** be-19#3.
- **Verifier notes:** A sibling finding on the same line, seam-04-plugin-policy#4, was *refuted* (§4) on the argument that the enum and profile description already teach the admitted index. Both refuters agree the enum is present. They differ only on whether one wasted repair turn counts as a defect.


## Source findings and verification

### be-19#3: Azure example_use hard-codes an index the only profile's pin may reject

- **Reported at:** `src/elspeth/web/plugin_policy/profiles.py:1617`; reviewer severity low; category teaching; diff-anchored True.
- **Summary:** public_summary's new Azure arm teaches 'index: approved-documents' even when exactly one closed-pin profile is available, which is the case where public_schema turns index into a closed enum of that pin.
- **Failure scenario:** The only profile is 'hr' with indexes=('hr-policies',). The planner copies the example and the Draft202012 check rejects it with "option(s) failing the public profile schema: ['index']", costing a repair turn.
- **Evidence:** profiles.py:1256-1260 sets index enum for a sole closed pin; profiles.py:1611-1621 example is fixed text; example_alias already derives from available_aliases.
- **Suggested fix:** When one profile is available and its pin is closed, use its first pinned index in example_use; otherwise keep the placeholder.
- **Verifier (trace):** upheld, confidence high, severity low. Confirmed at 74c0ce0db. The Azure arm of public_summary derives the profile alias from available_aliases, but the index is the fixed literal 'approved-documents' (profiles.py:1611-1621, added in the window by d3dba8a9b). When exactly one profile is available and its pin is closed, public_schema turns index into a closed enum of that pin (profiles.py:1253-1257). validation.py:475-503 runs Draft202012Validator over the authored options and reports "option(s) failing the public profile schema: ['index']". The example does reach the planner: the planner discovery digest leaves example_use out (recent-code-hints 2026-08-26 and planner_authoring_aids.py:1746), but the list_transforms inventory response includes it (inventory_response_contracts.py:294, used by composer/tools/transforms.py). No recorded ruling covers this. The only nearby test checks the S3 example (test_profiles.py:986-993). Mitigations: the profile description and the schema enum both list the admitted indexes, so the planner has the correct answer in front of it. The cost is at most a repair turn, which keeps this at low severity. The problem is actually a little wider than reported: with several profiles, available_aliases[0] may itself be a closed pin that does not admit 'approved-documents'.
