# Composer and Advisor: semantic heuristic evaluation

Evaluation date: 2026-10-20. Scope: Composer, Advisor, and their interpretation
review dependencies under `src/elspeth/web`.

The strongest findings are hard authoring refusals based on component names,
field names, and descriptive prose. Composer also uses English grammar and
plugin-name mentions to control authoring routes and conversational exits.
Advisor receives an incorrect absence signal for supported whole-row prompts,
coupled to an instruction to flag them. These mechanisms should be removed or
replaced with factual evidence that leaves task interpretation to the model.

This is an evaluation, with removal recommendations; production code was not
changed. Deterministic validation of actual graph effects, authorization, and
review evidence should remain.

## Evidence boundary

The shared checkout started on `codex/composer-fixes` at
`58b25d524d9c5a2550b0f895ea8f70e1dea9ea51` and advanced during evaluation to
`d970e79be4f83edf2670dea65f55f27040978b2f`. Other work was preserved. The command
below produced no output: the reviewed production tree did not change across
that movement.

```sh
git diff --stat 58b25d524d9c5a2550b0f895ea8f70e1dea9ea51 d970e79be4f83edf2670dea65f55f27040978b2f -- src/elspeth/web
```

Findings combine source tracing with in-memory calls of real production
helpers. Probes used the existing environment and verified local source
imports; no live provider requests, database changes, or pipeline executions
were performed. The Advisor finding establishes misleading evidence and
instructions, not an observed live-model rejection. The routing finding does
not establish that one authoring route has better model success rates.

## 1. Cleanup review requires magic phrases in descriptive prose

**High priority: remove the prose predicate.**

`interpretation_state.py:994-1001` recognizes the structured
`drop_raw_html_fields` decision only if its draft contains both `raw html`
and `fingerprint`. Lines 967-974 reject paraphrases and instruct the planner
to copy the canonical draft verbatim.

Controlled calls to `raw_html_cleanup_review_contract_error` with an otherwise
identical scraper and selection mapper produced:

```text
'Drop the scraped raw HTML and fingerprint fields before saving the output.'
  recognized=True; contract=None
'Drop the scraped HTML content and fingerprint columns before the sink.'
  recognized=False; contract=Raw-html cleanup review draft is malformed ...
'Delete the source markup and content digest before saving.'
  recognized=False; contract=Raw-html cleanup review draft is malformed ...
'Keep raw HTML and fingerprint fields before saving.'
  recognized=True; contract=None
```

Thus the words neither recognize equivalent meaning nor distinguish an
opposite instruction. The structured decision identifier already exists.
Use that identifier and the actual mapping/artifact to bind the review;
leave descriptive wording to the model. Preserve accepted-review provenance,
artifact binding, and real checks that reviewed effects match the graph.

This is a hard authoring gate: `composition_review_contract_error`
(`interpretation_state.py:628-643`) feeds `set_pipeline` rejection
(`composer/tools/sessions.py:2020-2039`) and transform upsert/splice/patch
rejection (`composer/tools/transforms.py:908-912,1423-1427,1939-1943`).

## 2. Field spelling is treated as proof of raw content

**High priority: remove name vocabulary from the blocking predicate.**

`interpretation_state.py:1037-1039` treats exact `content`, any `html`
substring, and any `fingerprint` substring as raw data.
`validate_pipeline_decision_semantics` rejects cleanup mappings preserving
either a source or target name matching this vocabulary (`:531-542`).

With actual scraper raw fields declared as `page_body` and `page_hash`, real
validator calls produced:

```text
summary                       OK
page_body                     rejected: preserves raw field
content                       rejected: preserves raw field
html_word_count               rejected: preserves raw field
fingerprint_similarity_score  rejected: preserves raw field
```

`summary` is the negative control; `page_body` verifies the actual declared-field
membership check. Derived features and a clean value named `content` are
rejected on spelling alone. Keep checks against actual producer fields and
their provenance; remove `_looks_like_raw_html_field` as an authority.

## 3. A mapper's identifier can forbid its actual behavior

**High priority: remove identifier-based rejection.**

`interpretation_state.py:1042-1044` interprets substrings `drop`, `cleanup`,
`clean`, `raw`, and `html` as cleanup intent. Lines 603-608 reject a
selection mapper preserving a known raw field when its name matches.

Only changing the identifier on the same mapper produced:

```text
select_fields         contract=None
clean_customer_names  rejected: named like raw HTML cleanup
draw_summary          rejected: named like raw HTML cleanup
drop_duplicates       rejected: named like raw HTML cleanup
```

`draw_summary` matches because `draw` contains `raw`. Identifiers cannot
establish intended data retention. Delete this rejection arm; retain checks
of explicit review decisions and actual mapping effects.

## 4. Closed English grammar chooses the authoring surface

**Medium priority: remove intent classification from entry routing and repair.**

`composer/no_tool_policy.py:514-696,1384-1421` implements a bounded English
grammar with a verb list, quote restrictions, question restrictions, a
revocation regex, and exemplar-specific phrases.
`composer/service.py:1241-1284` uses it to select the initial planner.

Real classifier output, including positive and negative controls:

```text
EXPLICIT_MUTATION  Build a pipeline.
CONVERSATIONAL     Explain the pipeline.
EXPLICIT_MUTATION  Build a pipeline that reads input.csv and writes rows to output.csv.
AMBIGUOUS          Build a pipeline that reads "input.csv" and writes rows to "output.csv".
AMBIGUOUS          Build a pipeline that reads input.csv and writes rows to output.csv. Can you do that?
CONVERSATIONAL     Construct a workflow that reads input.csv and writes rows to output.csv.
CONVERSATIONAL     Please turn the uploaded CSV into a JSON file.
AMBIGUOUS          Build a pipeline that reads patient.csv and aborts rows with status abort.
EXPLICIT_MUTATION  Build a pipeline that reads patient.csv and rejects rows with status failed.
```

Important limit: unrecognized requests enter the ordinary Composer loop;
they are not denied all authoring. The real loop palette includes
`set_pipeline`, `request_interpretation_review`, and `request_advisor_hint`
(`service.py:2494`, `provider_gateway.py:391-411`). The restriction changes
surface/tool/budget semantics based on spelling.

The same classifier conditions a repair opportunity after failed pre-state
interpretation review (`composition_completion.py:1603-1619`), giving
paraphrases different recovery behavior. That failure is itself recognized
through error-message substrings (`no_tool_policy.py:1551-1562`).

Let the provider choose explanation, clarification, discovery, and authoring,
using a model-selected action if a separate planner remains necessary. Return
typed prerequisite errors to the model rather than guessing intent or ordering
a repair from user text. Keep actual stop actions, budgets, approvals, and
graph validation. Do not replace this grammar with a larger synonym list.

## 5. Plugin mentions create mandatory discovery before a text decline

**Medium priority: remove text-derived discovery debt as an exit condition.**

`composer/pipeline_planner.py:539-542` extracts kind-qualified plugin tokens
from anywhere in request text and adds them to pending information (`:3476`).
The planner allows ordinary `DECLINE:` text only after all such information
is covered (`:4294-4314`). Real gate probes produced:

```text
Use source:csv.                                      pending=source/csv; decline=False
Do not use source:csv.                               pending=source/csv; decline=False
Explain why the string "source:csv" appears in the docs. pending=source/csv; decline=False
Do not use CSV.                                      pending=empty;      decline=True
```

A valid refusal or question can be withheld and sent through forced-tool
nudges because a quoted or negated plugin mention was interpreted as a
selection. These examples probe the planner gate directly; ordinary chat
entry can choose a different route.

Keep discovery information as evidence for the model, and validate plugins
when it actually submits a candidate. A conversational exit should not need
to prove discovery completion. Counterevidence: typed `handoff_to_user`
remains available (`:4400-4413`), so the model can still ask or stop through
that route.

The exact case-sensitive `DECLINE:` marker also rejects otherwise valid
questions (`:1723-1740,1827-1850`). This is an explicit protocol restriction,
distinct from guessing user intent. Prefer an existing typed conversational
action or ordinary model-authored termination; adding prefix variants would
retain the fragility.

## 6. Advisor gets false absence evidence plus a mandatory verdict rule

**High priority: correct the evidence and remove the unconditional rubric.**

`composer/advisor_context.py:1415-1453` uses a named-field extractor and
labels an empty field set `interpolates row fields: NONE`. The runtime supports
whole-row templates, which need not reference an individually named field.

The actual config validator and sandbox renderer produced:

```text
template: Summarize all fields: {{ row | dictsort }}
config_valid: True
annotation: interpolates row fields: NONE
render: Summarize all fields: [('body', 'ALPHA')]
render: Summarize all fields: [('body', 'BETA')]

template: Answer the same fixed question for each row: what is 2+2?
config_valid: True
annotation: interpolates row fields: NONE
render: Answer the same fixed question for each row: what is 2+2?

template: Summarize this body: {{ row.body }}
config_valid: True
annotation: interpolates row fields: [body]
render: Summarize this body: ALPHA
render: Summarize this body: BETA
```

The static and named-field controls show that `NONE` conflates a supported
varying prompt with a truly constant prompt.

The END review prompt orders Advisor to FLAG no-varying-content or
URL/identifier-only prompts and asserts they will fabricate or repeat answers
(`advisor_checkpoint.py:786-797`). This also overrules legitimate constant
tasks and judgments about identifiers themselves. An admitted FLAGGED verdict
is blocking (`:212-215`; `advisor_policy.py:550-564`). No live false FLAG was
measured, but the wrong evidence and mandatory instruction are directly present.

Describe named-field extraction limits honestly. Let Advisor judge whether the
actual prompt, other inputs, and requested task are adequate. Absence of named
field accesses must not be reported as absence of varying input. Preserve actual
template validation and honest evidence coverage.

## Adjacent findings requiring separate treatment

- **Prompt-label guard:** `sessions/pending_interpretation.py:613-626,768-775,977-986`
  rejects accepted interpretation substitution after `system:`, `role:`, or
  `instructions:`. A real resolver probe accepts `Describe the operating
  system - ` but rejects `Describe the operating system: `; `Evaluate these
  guidelines: ` passes while `Evaluate these instructions: ` fails. This is
  a security-origin substring heuristic. Runtime roles are assigned structurally
  (`plugins/transforms/llm/transform.py:350-358`), so these labels do not themselves
  create a provider system message. Replace only after tracing all applicable
  render/trust paths; preserve accepted-value authority and prompt binding.
- **Global cleanup field union:** `interpretation_state.py:1610-1621` collects
  raw fields from all scraper nodes, then checks every selection mapper
  (`:590-624,1638-1670`) without ancestry. A helper probe flags a mapper on an
  independent input as dropping scrape fields. This needs provenance-aware
  scoping, separate from removing word lists. A complete valid multi-source
  integration reproduction was not executed.
- **Advisor note rewriting:** `advisor_output.py:191-249` strips scheme URLs,
  `www.` references, and email-shaped values from displayed notes. A legitimate example option URL
  becomes `[link removed]`. Internal findings remain available to Composer,
  so this is an egress/presentation policy issue, not a tool-choice veto.
- **Progress guessing:** `composer/progress.py:499-509` selects an HTML/JSON
  headline from keywords. This changes progress copy, not model actions.

## Keep the factual boundaries

The inspected code also contains legitimate constraints: authenticated plugin
policy, explicit approval mode, secret wiring permissions, quotas/deadlines,
tool schema admission, resource dependency checks, graph/runtime validation,
review artifact hashes, publication bindings, and instruction/data provenance.
These do not decide semantic intent from user wording and should remain.

Prompt Guard is a separate pre-provider security verdict, not a local keyword
classifier (`prompt_guard_clients.py:75-92`, `prompt_guard.py:496-511`). No
external-service false positive was measured. Historical Advisor pre-scan
comments/DTO branches are not evidence of an active local keyword veto.
The live planner also retains its discovery tools after reads; an obsolete
palette-shrinking mechanism should not be reported as current behavior.

## Validation and removal sequence

Focused existing tests completed with explicit exit code 0:

```sh
python -m pytest \
  tests/unit/web/composer/test_request_intent_policy.py::test_complete_multi_clause_pipeline_request_is_explicit \
  tests/unit/web/composer/test_advisor_checkpoint.py::test_end_checkpoint_problem_summary_carries_degeneracy_rubric \
  -n 0 -q
```

```text
..............                                                           [100%]
14 passed in 8.35s
pytest_exit=0
```

These characterize current behavior; they do not prove a fix or real-model
quality. No whole-tree, PostgreSQL, or live-provider acceptance claim is made.

Recommended implementation order:

1. Remove cleanup prose/name vetoes; retain explicit review identity and actual
   effect checks. Test paraphrase and rename invariance plus real raw-field
   preservation controls.
2. Correct Advisor input evidence and replace the unconditional degeneracy
   rubric with task-conditioned review. Test whole-row, named-field, constant,
   and identifier-analysis tasks.
3. Remove English entry routing and plugin-mention stopping prerequisites.
   Use scripted provider decisions to prove semantic choice reaches the model
   while invalid proposals still fail real contract/policy validation.
4. Review prompt-label security assumptions separately and replace lexical
   guesses with actual structured boundaries where warranted.

Production changes would affect shared authoring/review behavior and need
the applicable integration and whole-tree gates before merging. Live provider
evaluation would then measure whether model decisions improve, independently
of deterministic gate removal.
