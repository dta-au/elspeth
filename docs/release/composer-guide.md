# ELSPETH Composer Guide

**Document date:** 28 September 2026
**Release covered:** 0.8.1
**Audience:** Evaluators, program teams, operators, and technical reviewers
**Register:** Public-facing / lightly technical
**Status:** Current capability guide

## What Composer Is

Composer is the web authoring surface for ELSPETH pipelines. It helps a user
describe a workflow, choose data sources and destinations, add transformation
steps, validate the result, and either run the pipeline or hand it to someone
else for review.

The important point is not that Composer uses chat. The important point is that
Composer turns a conversation into an auditable pipeline artifact. The generated
pipeline is still validation-gated, exportable as YAML, and backed by the same
audit and lineage model as hand-authored ELSPETH pipelines.

Use Composer when you want to build a pipeline in conversation without starting
from a blank YAML file.

## What You Can Do

| Need | Composer capability |
|---|---|
| Start from plain-language intent | Describe the workflow in chat and refine the model's proposal through follow-up instructions. |
| Build with structure | Add or revise plural sources, sinks, transforms, queues, gates, forks, coalesces, final wiring, and plugin options through controlled UI turns. |
| Keep the operator in control | See the session's authority mode, inspect versioned changes, and accept or reject changes that require approval. |
| Check readiness | Use the audit-readiness and live verification panels to see validation, plugin trust, provenance, retention, LLM interpretation, and secret status. |
| Review the shape | Inspect the graph view and rendered YAML before running or sharing. |
| Handle credentials safely | Reference secrets by name instead of placing secret values in pipeline configuration. |
| Preserve work in progress | Resume after an interrupted authoring session with transcript, redacted tool rows, and state diffs. |
| Finish in the right way | Share an inspect link, run the pipeline, or export YAML depending on the user's workflow. |
| Choose the level of detail | Keep the standard detail level, which is the default, or switch to technical for raw plugin settings, every validation check, advanced options, and YAML import. |

## The Authoring Experience

Composer has one authoring path: freeform conversation. The first-run tutorial
uses that same Composer path with a fixed example, then takes the new user
through a real run, its audit story, and graduation to an ordinary session.

| Path | Best for | How it feels |
|---|---|---|
| First-run tutorial | New users learning the vocabulary | A fixed example composed through the ordinary chat, followed by Run, Audit, and Graduation. |
| Freeform Composer | Anyone building or revising a pipeline | Describe the intended result, inspect applied changes or pending proposals, and refine in conversation. |

The model proposes the pipeline structure. New sessions default to
**Auto-apply on**: eligible changes can become versioned, audited pipeline
state without an Accept click. A full-pipeline proposal auto-commits only when
a green runtime preflight validates the candidate and the session remains in
auto-apply mode. The chat header shows the authority mode. With
**Approval required**, changes
wait as proposals for explicit Accept or Reject; a full-pipeline proposal also
remains pending when auto-commit's conditions are not met. Inspect the graph and
plain-language impact in either case. A rejected proposal can be revised in the
next turn without silently replacing the operator's intent.

The desktop workspace keeps authoring beside the pipeline artifact. Its
resizable authoring pane can collapse and reopen while the Graph, Spec, YAML,
Checks, and Run views remain available in the artifact pane. Checks collects
readiness details; the inspector opens additional context without replacing
the artifact. Tutorial and ordinary sessions use this same workspace. On narrower
screens, the views remain reachable through the workspace tabs and controls.

## How Composer Keeps Work Auditable

Composer treats authoring as part of the evidence chain.

- The system records LLM calls made during composition, including provider,
  model, status, latency, and token counts.
- The system records tool invocations used to change the composition state.
- Tool arguments are redacted before they are shown back to the user where
  sensitive fields may be involved.
- Generated YAML is validated before execution.
- The audit-readiness panel shows whether the composition has enough evidence
  to run or share.
- If an LLM-assisted step depends on a subjective interpretation, Composer can
  surface that interpretation for review instead of silently deciding it.
- Pending interpretation cards ask for operator review before a subjective
  decision becomes part of the pipeline.
- Authoring operations retain durable request and failure evidence. Concurrent
  mutations are fenced so a stale response cannot overwrite newer work.

This does not make the language model an authority. The language model proposes
changes. ELSPETH records, validates, and gates the resulting pipeline.

## Readiness Panel

The audit-readiness panel is the operator's compact answer to "is this pipeline
safe to move forward?"

| Row | What it tells you |
|---|---|
| Validation | Whether the current YAML shape passes runtime-oriented validation. |
| Plugin trust | Whether the chosen plugins fit the expected trust and capability model. |
| Provenance | Whether source and composition evidence can be traced. |
| Retention | Whether output and audit retention expectations are visible. |
| LLM interpretations | Whether subjective LLM interpretation points have been reviewed or opted out. |
| Secrets | Whether credentials are referenced safely and resolvably. |

Warnings are not hidden. They are there so a user can fix the pipeline before
running it, or share it with an explicit caveat.

## Completion Options

Composer gives three ways to finish a composition.

| Action | What happens |
|---|---|
| Share inspect link | Composer marks the current composition as ready for another person to inspect, creates a signed share link, and shows the reviewer the same readiness and YAML evidence. |
| Run pipeline | ELSPETH starts a background run, streams progress, and records the run in the audit trail. |
| Export YAML | Composer renders the pipeline as YAML so an operator or engineer can run, review, or store it outside the web UI. |

These actions are deliberately separate. A compliance reviewer may want to save
for review without running anything. A researcher may want to run immediately.
An engineer may want YAML and the CLI.

## Recovery

If a Composer session is interrupted, the recovery panel can show:

- the assistant transcript;
- redacted tool-call rows;
- the before-and-after composition diff;
- the visible failure reason where one is available.

Cancellation waits for the active turn to settle before reloading durable
state. A stale response cannot overwrite a newer session version, and a
replayed fork or confirmation returns the stored result. Retry is reserved for
the latest retryable failure rather than acting as a second mutation path.

The goal is not simply to "try again." The goal is to show what happened and let
the user decide what to keep.

## What Composer Is Not

Composer is not a replacement for assurance review, operational ownership, or
agency approval.

It does not certify a pipeline as fit for a regulated workload. It helps a user
build a pipeline, see its validation state, preserve evidence of authoring, and
hand the result to the right next step.

## Where To Go Next

- Read [`guarantees.md`](guarantees.md) for the audit and lineage guarantees
  that apply once a pipeline runs.
- Read [`platform-architecture.md`](platform-architecture.md) for the broader
  system shape.
