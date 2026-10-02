# R34. The new `prompt_template_parts_required` code is not registered in the validation-guidance catalogue

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Composer, advisor and planner |
| Review line | backend |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | be-11#4 |

## Finding

- **Location:** `src/elspeth/web/composer/tools/transforms.py:1669`, `generation.py` and `_dispatch.py:585`.
- **Wrong:** The envelope advertises `explain_validation_error`, but the lookup returns nothing for this code. The planner wastes a turn.
- **Fix:** Add a `DirectValidationGuidance` entry and a closed-and-actionable test.
- **Sources:** be-11#4.
- **Verifier notes:** The claim that "every sibling code is catalogued" is false. Four other tool codes are also missing (`edge_not_lowerable`, `edge_route_conflict`, `round_trip_unavailable`, `runtime_preflight_not_run`). The rejection text itself is actionable.


## Source findings and verification

### be-11#4: New error_code prompt_template_parts_required not registered in the validation-guidance catalogue

- **Reported at:** `src/elspeth/web/composer/tools/transforms.py:1669`; reviewer severity low; category half-wired; diff-anchored True.
- **Summary:** Every sibling tool-emitted code is closed and actionable in `generation.py`, and each is pinned in `test_validation_error_codes.py`. The new code is not registered, so the freeform `validation_guidance` advertises `explain_validation_error` for it, and that call explains nothing.
- **Failure scenario:** Planner receives the rejection, follows the envelope's explain_tool advert, and calls explain_validation_error('prompt_template_parts_required'). The code has no catalogue entry, so the call explains nothing and the turn is wasted.
- **Evidence:** Probe be-11-probe/probe2.py and probe3.py: 'catalogued: False True'; build_validation_guidance returns codes {} plus explain_tool advert; explain_validation_code('prompt_template_parts_required') -> None.
- **Suggested fix:** Add a DirectValidationGuidance entry, or a _LEGACY_VALIDATION_ERROR_CODES plus pattern entry, with the explanation and fix (edit prompt_template_parts, preserve interpretation_ref entries, read the parts via get_pipeline_state), plus a closed-and-actionable test.
- **Verifier (trace):** upheld, confidence high, severity low. Confirmed at the pinned commit. patch_node_options can reach the rejection with ordinary input: an llm node whose options already hold prompt_template_parts, and a patch that changes prompt_template without including prompt_template_parts. That rejection carries error_code 'prompt_template_parts_required', which the catalogue has no entry for. _dispatch.py:585 runs build_validation_guidance over the envelope's codes. It returns codes={} and advertises explain_tool, telling the model the call can expand the entry. When the model calls explain_validation_error with the code, the exact lookup, the regex patterns and the fuzzy closed-code scan all miss. The call then gets the generic 'does not match any known validation message' reply plus a list of all closed codes. So the advertised repair turn adds nothing.

The finding overstates one point. The claim that 'every sibling tool-emitted code is closed' is false: I found four other uncatalogued tool codes (edge_not_lowerable, edge_route_conflict, round_trip_unavailable, runtime_preflight_not_run), so the convention is not fully enforced. The impact is also small. The rejection message is itself actionable ('edit prompt_template_parts ... preserving its interpretation_ref entries'), so at worst the model spends one wasted turn. That keeps the severity low, but the half-wired gap in the new code is real.
