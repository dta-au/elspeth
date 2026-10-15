# Release 0.8.4–0.8.6 documentation review

Reviewed 15 October 2026. The bounded documentation inconsistencies identified
below are corrected and verified against the candidate source. Root integration
review remains separate. This is not release, CI, signature, image or deployment
approval.

## Release and store boundaries

| Source | Package / lock | Sessions / Landscape | Verified publication fact |
| --- | --- | --- | --- |
| 0.8.4, PR #287 merge `327fba03318b008a83c7c82d24cce8b5e1e05847` | `0.8.4` / `0.8.4` | `75` / `49` | Merged source; no corresponding version tag appeared in the queried tag collections. |
| 0.8.5, PR #311 merge `9d7735c094eab212549868afeaf6019272cf5c57` | `0.8.5` / `0.8.5` | `75` / `49` | Annotated `v0.8.5` peels to this merge. |
| 0.8.6 candidate, published HEAD `11d1d59e876dda0476aff89cf512dbaef0da5f6b` plus current repairs | `0.8.6` / `0.8.6` | `76` / `49` | PR #313 remained unmerged at the review observation; no 0.8.6 version tag appeared. |

Package and lock values were parsed from TOML; prior values came from immutable
Git revisions. Read-only GitHub API queries independently confirmed both merge
commits and the annotated tag. The release collection returned no GitHub Releases.
These observations do not establish container-registry state.

Epoch 76 makes committed graph-review facts authoritative without rewriting
immutable graph snapshots. Epoch-75 session stores remain preserved with their
compatible prior application; the candidate requires a separate epoch-76 target
and isolated old/new workers. Landscape stays at 49 and still requires exact
schema admission. No migration, reset, restamp or downgrade is authorized.
Historical release facts, policy dates and actual receipts were retained.

## Corrections

- [Shareable-review deployment](../guides/sharing-pipelines.md#first-deploy-operator-action)
  now identifies session epoch 76 and explains preserved epoch-75 stores,
  separate targets and worker isolation.
- [Chroma example prerequisites](../../examples/chroma_rag/README.md#prerequisite)
  now use the locked base installation, which already includes ChromaDB,
  and retain the shared-worktree environment warning.
- [LLM configuration](../reference/configuration.md#llm-transform-llm) now
  lists `gateway`, the actual `llm` plugin, conditional `prompt_template`, and
  `query_input_mode`. Ordered-mode guidance covers list/pool requirements,
  earlier-output bindings, original-row views, refusal and recovery limits.
  The complete multi-query example now reads `row.review`.
- [Blob inspection](../reference/composer-tools.md#get_blob_content) now
  documents continuation arguments, verified content identity, actual character
  ranges, page/response caps and the difference between a prefix and a complete
  inspection.

## Verification and scope

The corrected multi-query example admits through the actual owned config model;
the old top-level variable form is refused. All-query template overrides admit
without a node prompt, while a missing effective query template is refused.
The ordered guide admits; pool size two and forward references are refused.
Blob continuation admits with the required hash and refuses its omission.
The new relative guide link resolves and a deliberately nonexistent link does
not. Whitespace checks passed. Inspected correction/source files remained
unchanged during these checks.

The live registry measured 10 sources, 39 transforms and 10 sinks. All named
plugin families in the new [YAML guide](../guides/yaml-authoring.md) and plugin
references in 104 tracked example configurations with a `sources` section
resolve. Known-name lookups succeed and nonexistent controls are refused. This
checks names, not full configuration or execution. The YAML guide is discoverable
from the [documentation index](../README.md).

Current prospective AWS and ACA receipt examples match the source-derived
session-76/Landscape-49 facts; an epoch-75 mutation fails the comparison.
Current Docker guidance requires confirmed published image identity and does
not derive availability from the package version. Automatic image publication
and deployment remain separate operator decisions.

## Confidence Assessment

High for the corrected passages: direct source reads, parsed metadata, live
registry/API evidence and positive/negative model controls support the findings.

## Risk Assessment

Low implementation risk; corrections are reversible documentation changes.
Preserving historical facts avoids falsely implying prior stores or releases
used the current format.

## Information Gaps

Final integrated commit, exact-head CI, registry publication, operator signing
and deployment are outside this review and remain with the integrator.

## Caveats and Required Follow-ups

No tests, providers, runtime examples, installs or environment sync were run by
this reviewer. The checks used metadata, Markdown/YAML/config parsers and plugin
registration only. Prior walkthrough execution belongs to its original evidence.
The integrator must independently review these documentation changes and qualify
the final candidate; this report does not claim a frozen whole-candidate result.
