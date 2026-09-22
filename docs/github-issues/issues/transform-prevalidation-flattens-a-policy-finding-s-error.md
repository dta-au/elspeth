---
title: Transform prevalidation discards a policy finding's error code, so repair advice contradicts itself
labels: [area/composer, type/bug]
---

A helper that pre-validates a transform returns a bare string, so a structured reason such as `profile_unavailable` survives only as prose. Every caller then re-labels the failure `plugin_options_invalid`, and the language model is handed a remedy that cannot possibly work.

## Where this lives

The Web Composer is ELSPETH's second authoring surface: a language model builds a pipeline by calling tools, and the server validates each proposal. Those tools live in `src/elspeth/web/composer/tools/`. The loop that drives the model, decides which validation details it is allowed to see, and gives it a bounded number of retries lives in `src/elspeth/web/composer/pipeline_planner.py`. Work on this issue starts in `tools/_common.py`, at `_prevalidate_transform_for_context`.

## What happens

A transform naming an operator profile the deployment has not configured produces a validation finding whose `error_code` is `profile_unavailable`. What reaches the model's retry is the unrelated code `plugin_options_invalid`. Each code carries a fixed explanation and suggested fix, injected automatically; the one for `plugin_options_invalid` opens "Apply exactly what 'detail' names … call `get_plugin_schema` … fix only the offending options, and re-emit" — beside a `detail` string that reads `profile_unavailable`. No edit to the options can create an operator profile, so the model has to out-reason its own injected instruction to behave correctly. The advice it should have received says instead: "Pick a plugin the live catalogue offers, or tell the user an operator must configure an operator profile for this one before it can be used."

## Why

`_prevalidate_transform_for_context` is typed `str | None`, so it structurally cannot carry a code; it formats the finding as `Invalid options for transform '<plugin>': <error_code> — <message>`. All four call sites — three in `tools/transforms.py`, one in `tools/sessions.py` — therefore pass a hardcoded `error_code="plugin_options_invalid"` to `_failure_result`.

Codes cross every redaction boundary in the planner; prose crosses none. `_allowlisted_candidate_feedback` in `pipeline_planner.py` withholds raw validator messages by default, because they can quote plugin names, option values or row content. It enriches each known code with its static explanation and suggested fix, and lets the original message through as `detail` for only three codes.

The more serious consequence is on the withholding path. `_entry_withholding` marks an entry as config-owned when its subject cannot be attributed, or when the subject is a component the server's finaliser wrote; `_derive_finalizer_owned_refs` works that out by structural diff. A node restored by a correction is exactly that shape, and can be the profile-bearing node in question. When it is, `detail` is stripped, the component is masked, and generic blind-mode guidance is substituted — at which point the profile reason disappears completely, because prose was its only carrier. A structured code would have survived untouched, since codes are never stripped.

The same loss shows up in the audit trail. `profile_unavailable` is already a recognised code — `tools/generation.py` derives the whole family from the `PluginUnavailableReason` enum — so the existing `_closed_planner_rejection_codes` filter would have carried it through to the recorded rejection codes intact. What is recorded instead is the generic code, attributing an operator-enablement problem to model authoring. Reconstructing the truth afterwards is hard, because stored chat messages hold the redaction placeholder defined in `src/elspeth/web/composer/redaction.py`.

## Impact

Retry budget is spent on an impossible remedy whenever a transform hits a plugin-enablement or operator-profile finding. Where the entry is withheld, the reason is lost outright rather than merely mislabelled. Because the trail records the generic code, occurrences are effectively invisible in stored evidence.

## Fix

Change `_prevalidate_transform_for_context` to return the code alongside the message — `tuple[str, str] | None` rather than `str | None` — and have each of the four call sites pass the finding's own `error_code` to `_failure_result` while keeping the explanatory prose as the message. `_failure_result` already accepts both, so the prose never had to be bought by discarding the code.

You know it holds when, for a transform whose operator profile is not configured:

1. the rejection's `error_code` is `profile_unavailable`, not `plugin_options_invalid`;
2. the feedback the model receives carries the operator-profile advice quoted above;
3. that code appears in the recorded rejection codes; and
4. all of the above still holds when the entry is withheld, which is the case a test must cover deliberately — construct a proposal whose profile-bearing node the finaliser has rewritten, and assert the code survives while the detail does not.

Existing coverage to build on is in `tests/unit/web/composer/test_tools.py` and `tests/integration/web/composer/parity/test_authoring_profile_lowering.py`.

## Size

Small and self-contained: one return type, four call sites, no design decision outstanding. Most of the effort is in the tests, particularly the withheld-entry case. A reasonable first issue for someone new to the composer, since it forces a read of the feedback-projection path without requiring a change to it.

## Status of evidence

The mechanism is verified: the fixed advice text was read from the tree and the projection path traced end to end. Occurrence is not observed — no captured session shows this pair consuming a retry budget, and per the stored-message redaction above, stored evidence could not show it.

Noticed during the removal of the pipeline recipe system (commit `e7a85bf8e`); not caused by it.
