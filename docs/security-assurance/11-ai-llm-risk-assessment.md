# 11 — AI / LLM risk assessment

**Status:** reusable product assessment complete; deployment record open ·
**Product source reviewed against:** `release/0.8.1` @
`487ac85a377f135e012bb206e3769cd65f6fadb8` (2026-10-01) ·
**Owner:** DTA Cloud Engineering

ELSPETH uses language models in the Web Composer and in pipeline plugins. It
also integrates with managed prompt-shield, content-safety and retrieval
services. This assessment records the product's supported uses, technical
controls and review method. It does not decide whether a particular agency,
dataset, provider, jurisdiction or use case is acceptable.

The assessment builds on [02 — Data flows and trust boundaries](02-data-flows-and-trust-boundaries.md),
[04 § 4.3 — Composer and the LLM boundary](04-threat-model.md#43-composer-and-the-llm-boundary),
and [12 — Privacy impact assessment](12-privacy-impact-assessment.md). Evidence
IDs refer to [16 — Evidence index](16-evidence-index.md).

> **Sensitive when populated:** complete each **Deployment record** in the
> deployment's controlled copy. Provider accounts, endpoints, regions,
> contracts, datasets, people, ratings, acceptance and approval do not belong
> in this product baseline.

## 1. Assessment method and scope

For each deployment:

1. define every intended use, decision, affected group and prohibited use;
2. inventory every model, managed AI service, prompt source, retrieval source,
   tool, output sink and person who reviews or acts on output;
3. assess security against the OWASP mapping in § 5 and privacy in [12](12-privacy-impact-assessment.md);
4. assess fairness, accessibility, explainability, contestability, human
   oversight and reviewer competence using §§ 6–8;
5. establish evaluation datasets, acceptance thresholds, monitoring signals,
   change triggers, incident escalation and withdrawal criteria using § 9;
6. record risks, treatments, owners and acceptance in the controlled
   [15 — Risk register](15-risk-register.md); and
7. re-assess after any material change listed in § 9.3.

This method covers the Composer planner, Composer advisor, session-title call,
LLM source and transform, prompt shields, content-safety services, retrieval
services and LLM tracing. Document-analysis services are machine-learning
services but are outside the LLM-specific inventory; their outbound flows are
still assessed in [02](02-data-flows-and-trust-boundaries.md) and [12](12-privacy-impact-assessment.md).

## 2. Product use and authority inventory

The inventory was checked against the plugin registry, web settings and
Composer tool registry [EV-501] [EV-504] [EV-511].

| Use | Execution surface and authority | Data sent | Output and effect | Product-level human gate |
|---|---|---|---|---|
| Composer planner | Web only. The operator sets the model and endpoint; the signed-in author supplies requests and invokes discovery | User messages, redacted pipeline state, plugin catalogue and authoring aids, discovery results, and up to 50,000 characters per uploaded-file read | Tool calls and prose used to author the working composition | `auto_commit` applies eligible mutations immediately. `explicit_approve` holds composition mutations and destructive blob update/delete operations; `create_blob` remains immediate so later proposals can reference its allocated blob ID [EV-510] |
| Composer advisor | Web only. The operator sets a separate model and endpoint; the same model is refused unless explicitly allowed | Bounded, redacted and fenced request and pipeline summary | Advice and a sign-off verdict; it does not mutate the pipeline | A withheld sign-off prevents the composition being marked ready; the person still decides whether to proceed [EV-504] [EV-506] |
| Session title | Web only. Uses the planner destination | Redacted, fenced excerpt of at most 800 characters from the first user message | Display metadata admitted through a character allowlist | No approval; the title is not pipeline state or audit evidence [EV-512] |
| LLM transform and source | Web-authored run: operator profile fixes provider, model, endpoint, region and credential. CLI/YAML run: the operator supplies configuration | Declared prompt fields, authored prompt and optional images | New row data; structured output can be parsed into named fields | Web: authenticated user start, validation and any configured governance. CLI/YAML: explicit `--execute` gate under the invoking operator or automation identity; no Web identity or Web secret-acknowledgement token is required [EV-518] |
| Prompt shield and content safety | Web or CLI/YAML, according to plugin configuration and policy | Configured row fields | Automatic classification can block a row | Policy can make coverage required for Web-authored pipelines; any override or exception is a deployment governance decision [EV-502] [EV-507] |
| Retrieval | Web or CLI/YAML. Azure AI Search uses operator profiles; Chroma is bounded by plugin and address policy | Query rendered only from declared fields | Retrieved passages become untrusted row content, often used by a later LLM | No product approval per retrieval; source authority and result review are deployment responsibilities [EV-503] [EV-508] [EV-509] |
| LLM tracing | Operator YAML only; refused in web-authored LLM nodes | Depending on configured granularity, prompts and responses | Observability copy at Langfuse or Azure AI | Operator enables it outside the Composer [EV-516] |

### 2.1 Supported and unsupported uses

ELSPETH supports assisted pipeline authoring, advisory review, bounded
generation or transformation of row data, retrieval augmentation, and
operator-selected safety classification. The product preserves the distinction
between model output and ELSPETH-owned validation: model text and tool calls
remain untrusted until the applicable parser, graph validator, policy check or
human gate accepts them.

The following uses are unsupported unless a deployment adds independent,
documented controls outside ELSPETH:

- treating model output as a deterministic fact, legal conclusion, security
  decision or sole basis for a decision that materially affects a person;
- allowing model output to execute code, issue arbitrary database statements
  or invoke pipeline tools; pipeline LLM plugins do not expose tool/function
  calling [EV-513];
- relying on a prompt to enforce authorisation, confidentiality or separation
  of duties;
- sending data to an unapproved provider, region, tracing destination,
  retrieval index or sink;
- using ELSPETH to train or fine-tune a model; the reviewed product has no such
  path; or
- claiming fairness, accuracy, accessibility or suitability from product
  controls alone. Those properties depend on the actual purpose, data,
  affected people, model and evaluation evidence.

### 2.2 Deployment record — use cases and affected people

| Record | Required value |
|---|---|
| Intended use cases, outputs and decisions supported | DEPLOYMENT-TODO: describe each use case and the point at which model output influences a person or operational outcome |
| Unsupported or prohibited uses communicated to users | DEPLOYMENT-TODO: record prohibitions and where they are enforced or taught |
| Affected people and groups, including non-users | DEPLOYMENT-TODO: identify groups and any children, vulnerable people or communities requiring specific consideration |
| Use-case owner and accountable official | DEPLOYMENT-TODO: record names or accountable roles |
| User and reviewer roles | DEPLOYMENT-TODO: identify authors, operators, reviewers, approvers, data custodians and incident contacts |
| Consequences of error, delay, refusal and automation bias | DEPLOYMENT-TODO: describe plausible effects without assigning a rating here |
| Alternative non-AI path or service withdrawal plan | DEPLOYMENT-TODO: record the fallback and how affected people are supported |

## 3. Provider and data suitability

The product supplies these boundaries:

- operators choose Composer destinations and define the LLM profiles offered
  to Web authors; profile-private provider, endpoint, region, credential,
  tracing and output-limit fields cannot be authored through the Composer
  [EV-503];
- gateway and OpenRouter endpoints require HTTPS except for loopback use, and
  web-authored OpenRouter nodes cannot override `base_url`;
- Azure content-safety endpoints require HTTPS on port 443, an approved Azure
  AI Services hostname suffix, and no query or fragment [EV-514];
- secret values are not part of Composer state. The model sees approved secret
  names, scopes and availability, while the redaction manifest removes
  internal paths and sensitive arguments [EV-014];
- declared-field projection restricts prompt and retrieval templates to the
  fields declared by the node [EV-508]; and
- full telemetry and operator tracing can create additional copies of prompts
  and responses, so they require their own disclosure and retention decisions.

These controls do not determine whether provider terms, model provenance,
training/reuse terms, abuse monitoring, location, subprocessors or retention
are acceptable for a deployment.

### 3.1 Deployment record — providers and managed AI services

Complete one row for every enabled provider, gateway upstream, safety service,
retrieval service and tracing destination. Cross-reference the disclosure
register in [12 § 8](12-privacy-impact-assessment.md#8-disclosure-and-cross-border-record).

| Service and role | Model / version / endpoint | Region and countries | Data categories and classification | Training, reuse, abuse monitoring and retention terms | Contract and subprocessors | Approval |
|---|---|---|---|---|---|---|
| DEPLOYMENT-TODO: service and role | DEPLOYMENT-TODO: exact model, version and endpoint | DEPLOYMENT-TODO: hosting and support locations | DEPLOYMENT-TODO: approved inputs and outputs | DEPLOYMENT-TODO: provider terms and deletion behaviour | DEPLOYMENT-TODO: contract, privacy and subprocessor references | DEPLOYMENT-TODO: approver and date |

## 4. Security risks and controls

| Risk | Applies to | Product controls | Evidence | Deployment decision |
|---|---|---|---|---|
| Direct or indirect prompt injection and unsafe authorised egress | Chat, uploads, pipeline state, rows, fetched pages, retrieved passages and destinations that policy permits | Untrusted framing; closed-schema tool validation; proposal binding; operator profiles; declared-field templates; optional required prompt-shield and content-safety coverage | EV-402, EV-403, EV-502, EV-506–EV-508 | Record deployment exposure, treatment and acceptance under [R-005](15-risk-register.md) |
| Sensitive information disclosure | All provider calls, tracing, telemetry, retrieval and model output | Operator-chosen destinations; redaction manifest; secret names rather than values; declared-field projection; value-free public failures | EV-014, EV-503, EV-508, EV-516 | Approve the exact data, destination, terms and readers in §§ 3.1 and 11 |
| Unvalidated output taking effect | Planner tools and pipeline output | Server-side closed-schema tool validation; graph validation; proposal authority; output treated as Tier 3; pipeline LLM plugins accept explicit `stop` or an absent finish reason and fail a row for every other present value; sanitised Web rendering | EV-402, EV-403, EV-406, EV-513 | Define human review and downstream validation for the use case |
| Excessive agency or model-authored change without review | Composer tool loop | Fixed tool registry; no run tool; operator policy bounds plugins, paths, connectors and secret wiring; trust mode; separate run gate | EV-510, EV-511 | Choose trust mode, governance and reviewer competence; record residual exposure under [R-006](15-risk-register.md) |
| Misinformation and automation bias | Advice, explanations and generated row content | Advisor is labelled as advice; interpretation reviews expose model-made choices; output is untrusted; calls and state transitions are recorded | EV-505, EV-513, EV-517 | Define accuracy thresholds, review sampling, override and escalation |
| Unbounded provider cost or execution consumption | Planner, advisor and pipeline calls | Per-composition bounds; advisor bounds; required Composer rate setting; optional token quotas; run rate limits and bounded retry budgets | EV-503, EV-504 | Set limits, budgets and alerts; record residual exposure under [R-007](15-risk-register.md) |
| Provider outage or model change | All AI calls | Planner boot probe; requested and returned models recorded; catalogue snapshot for OpenRouter; classified errors; failed Composer operations leave committed state unchanged | EV-505, EV-509 | Define continuity, withdrawal and re-evaluation triggers |
| Data or retrieval poisoning | Retrieval indexes and external content | External content remains Tier 3; declared-field projection; provider and index selected within policy; call evidence recorded | EV-503, EV-508, EV-509 | Govern index writers, provenance, test corpus and poisoning response |

### 4.1 Composer controls

- Every provider tool call is validated against a closed schema before its
  handler runs. Provider strict-schema support is an additional reliability
  measure, not the security boundary [EV-402].
- A proposal is tied to the current session head and operation fence;
  whole-pipeline proposals also carry the draft hash of the material reviewed
  by the user [EV-403].
- The provider authors pipeline structure. ELSPETH validates, rejects or
  redacts it and may insert an operator-required control with disclosure; it
  does not replace the planner with server-authored structure.
- Planner state and advisor context identify embedded material as untrusted;
  advisor fence markers inside user text are neutralised before framing
  [EV-506].
- Raw provider errors are hidden by default. User-visible and audit rejection
  shapes avoid including offending row values.

### 4.2 Pipeline controls

- Web-authored LLM source and transform nodes select operator profiles. The
  author cannot supply private provider fields [EV-503].
- The Web plugin policy supplies a fixed always-authorised set; other plugins
  need the operator allowlist. Unavailable profiles and unconfigured required
  controls fail closed [EV-502].
- `required` prompt-shield controls must dominate LLM inputs and `required`
  content-safety controls must post-dominate LLM outputs. Execution refuses
  uncovered graphs [EV-507].
- Prompt and retrieval templates render in bounded workers against declared
  fields only [EV-404] [EV-508].
- LLM output is Tier 3 and untrusted. Pipeline LLM plugins do not expose tool
  calling [EV-513].

### 4.3 Consumption controls

Defaults measured from the Web settings model at the review baseline
[EV-504]:

| Limit | Setting | Product default |
|---|---|---|
| Planner calls per composition | `composer_planner_max_provider_calls` | 75 |
| Planner request size | `composer_planner_max_request_bytes` | 2 MiB |
| Planner completion tokens per call | `composer_planner_max_completion_tokens` | 16,384 |
| Planner cumulative provider cost | `composer_planner_max_cumulative_provider_cost` | USD 5.00 |
| Planner repair attempts | `composer_planner_repair_budget` | 2 |
| Tool calls per turn | `composer_max_tool_calls_per_turn` | 16 |
| Composition turns, discovery turns and compose timeout | corresponding Composer settings | Required operator settings; no product default |
| Composer messages per user per minute | `composer_rate_limit_per_minute` | Required operator setting; no product default |
| Advisor calls per compose and checkpoint passes | advisor settings | 4 and 2 |
| Advisor prompt / completion tokens and timeout | advisor settings | 4,000 / 8,192; 60 seconds |
| Identical rejected planner calls | fixed | 6 |
| Daily tokens | user/container quota settings | Off unless configured |
| External calls from Web runs | `execution_rate_limit` | 60 per minute per service |
| LLM profile timeout and output tokens | profile settings | 60 seconds; provider default unless set |

Web runs admit a chargeable LLM attempt before sending it and settle usage from
the Landscape record afterwards. Configured token quotas can refuse a later
call. CLI/YAML execution uses its own pipeline rate-limit and operator controls;
the Web per-user quota model must not be assumed for that surface.

## 5. OWASP Top 10 for LLM Applications (2025)

The reusable baseline is the [OWASP Top 10 for Large Language Model Applications](https://owasp.org/www-project-top-10-for-large-language-model-applications/),
2025 edition.

| Item | ELSPETH exposure | Product treatment | Remaining deployment work |
|---|---|---|---|
| LLM01 Prompt injection | Direct chat and indirect uploads, rows, pages and retrieved passages | § 4 framing, schema validation, proposal binding, profiles, field projection and optional required safety controls | Test the actual prompts, data sources, models and consequence paths; link residual risk to R-005 |
| LLM02 Sensitive information disclosure | Provider, retrieval, tracing, telemetry, sink and user-visible output | Redaction, secret-value exclusion, field projection, destination authority and value-free errors | Complete provider and privacy disclosure review |
| LLM03 Supply chain | Models, providers, gateway and inference dependencies | Operator selection; requested/returned model and catalogue evidence; dependency controls in [10](10-vulnerability-and-supply-chain.md) | Approve model provenance, provider and contract |
| LLM04 Data and model poisoning | Retrieval indexes and reference data | No product training/fine-tuning path; retrieved content remains untrusted | Govern source writers, provenance, evaluation and response |
| LLM05 Improper output handling | Tool calls, browser rendering and downstream pipeline steps | Closed-schema tools, validation, Tier-3 outputs, sanitised rendering and bounded templates | Validate each downstream sink and decision use |
| LLM06 Excessive agency | Composer tools and later pipeline execution | No run tool; operator policy; trust mode; separate Web/CLI execution gates | Choose approval and separation-of-duty controls; link residual risk to R-006 |
| LLM07 System prompt leakage | Composer/advisor system instructions and authored prompts | Prompts are not an authorisation boundary; secret values are excluded | Treat prompts as disclosable and review any deployment additions |
| LLM08 Vector and embedding weaknesses | Azure AI Search, RAG retrieval and Chroma | Profile/index policy, declared-field queries, address policy and vector-call evidence | Assess tenant isolation, embedding sensitivity and index control |
| LLM09 Misinformation | Advice, explanations and generated data | Advice labelling, interpretation review, untrusted output and call traceability | Establish quality thresholds, review and contestability |
| LLM10 Unbounded consumption | Composer, advisor, transforms and retrieval | § 4.3 limits and quota/rate-control options | Configure budgets, alerting and abuse response; link residual risk to R-007 |

## 6. Responsible-AI impact analysis

### 6.1 Fairness and affected groups

ELSPETH does not determine whether a model or dataset is fair. It can preserve
the model/version, prompts, inputs, outputs and review evidence needed to
evaluate disparities. A deployment must define the relevant groups and lawful
attributes, choose representative evaluation data, compare error and refusal
rates, examine proxy effects, and set an escalation path for disparate impact.
Sensitive attributes must not be collected solely to measure fairness without
the privacy and legal assessment in [12](12-privacy-impact-assessment.md).

### 6.2 Accessibility and inclusion

The assessment must cover the authoring interface, review material, generated
content and any service delivered from model output. Evaluate keyboard and
assistive-technology use, language and literacy demands, alternative formats,
time limits, error recovery, and a non-AI or assisted channel. Product audit
evidence can explain what happened; it does not by itself make an output or
workflow accessible.

### 6.3 Explainability and contestability

ELSPETH records requested and returned models, finish reason, token counts,
latency, hashes of the messages and tool specification, tool calls, proposal
events, interpretation decisions and pipeline call payload references
[EV-505] [EV-509] [EV-517]. This supports reconstruction of what the system did.
It is not proof that a model's internal reasoning is faithful or that an
affected person received a meaningful explanation.

For each use case, the deployment must define what explanation is given, what
source evidence a reviewer can inspect, how a person challenges an input or
outcome, who can override or correct it, response time expectations, and how a
contested outcome is prevented from propagating while under review.

### 6.4 Transparency

Users and affected people need context appropriate to the use: that AI is
used, the purpose, meaningful limitations, data destinations, human-review
arrangements, how to ask for review, and how to exercise privacy rights. The
public product documentation does not substitute for a deployment-specific
notice or government AI transparency statement.

### 6.5 Deployment record — impact analysis

| Topic | Required value |
|---|---|
| Fairness questions, groups and metrics | DEPLOYMENT-TODO: define the groups, evidence and comparison method |
| Accessibility standard and test participants | DEPLOYMENT-TODO: record the applicable standard, testing and alternatives |
| Explanation supplied to users and affected people | DEPLOYMENT-TODO: record content, channel and responsible role |
| Contest, correction, override and escalation path | DEPLOYMENT-TODO: record process, service level and authority |
| Transparency statement and AI use-case register entry | DEPLOYMENT-TODO: record applicability, publication location and accountable role |
| Impact-assessment outcome | DEPLOYMENT-TODO: attach the completed assessment without inserting ratings or approval into this baseline |

## 7. Traceability, retention and privacy

### 7.1 Model-call traceability

| Call | Product record | Contents and access path |
|---|---|---|
| Pipeline LLM source/transform and retrieval | Landscape `calls` plus payload store | Request/response hashes and payload references, status, latency, tokens, returned model and approved prompt artefact link. Read with `elspeth explain`, authorised Web inspection or the read-only Landscape MCP tools [EV-509] |
| Composer planner/advisor | Sessions `chat_messages` audit rows | Requested/returned model, finish reason, tokens, latency, provider request ID, message/tool-spec hashes, model settings, cost and redacted tool activity [EV-505] |
| Composition result | Sessions composition, proposal and interpretation tables | Committed versions, proposal decisions, trust-mode events and interpretation reviews [EV-510] [EV-517] |
| Session title | Token usage ledger | Usage only; the generated title is display metadata rather than pipeline audit evidence [EV-512] |

Transcript rows are immutable while their session exists. Session lifetime is
separate from payload retention: a session with durable history is
soft-archived and retains its history, while a session without durable history
can be physically deleted [EV-519]. See
[08 § 1.4](08-logging-audit-and-monitoring.md#14-session-composer-and-workflow-audit).

### 7.2 Retention boundaries

| Store or copy | Product behaviour | Deployment decision |
|---|---|---|
| Payload-store prompts, responses and retrieved content | Default purge eligibility is 90 days. `elspeth purge` is explicit, removes expired blobs and preserves Landscape hashes and metadata; missing payloads are reported as unavailable | Schedule, operator, exceptions and records authority |
| Composer session and transcript | Rows are immutable while the session exists. Durable-history sessions are soft-archived; sessions without durable history can be physically deleted | Session/archive schedule, authorised readers and request handling |
| Landscape metadata and hashes | Audit metadata and hashes remain after payload purge | Retention, export and disposal authority |
| Application logs and telemetry | Content depends on configured logging and telemetry granularity | Retention, access and redaction |
| Provider, search, safety and tracing copies | Governed by the selected external service and contract | Provider retention, deletion, training/reuse and legal decision |
| Backups and exports | Deployment-managed copies can outlive the live store | Backup/export retention and deletion propagation |

The complete privacy analysis and access/correction method is in
[12](12-privacy-impact-assessment.md).

## 8. Human oversight by execution surface

### 8.1 Web Composer and Web execution

The authenticated start and exact secret-wiring acknowledgement are Web-path
controls implemented by the [execution route](../../src/elspeth/web/execution/routes.py)
and [execution service](../../src/elspeth/web/execution/service.py) [EV-518].

| Decision point | Product gate | Default or limitation |
|---|---|---|
| Planner mutation | `explicit_approve` holds composition mutations and destructive blob updates/deletions. `create_blob` is immediate because a later proposal needs its ID | `auto_commit` is the session default [EV-510] |
| Model-made interpretation | A staged pending interpretation blocks execution until resolved | Applies when an interpretation is staged; server stages required-control and invented-source cases [EV-517] |
| Advisor verdict | A withheld sign-off withholds ready status | Advisory; the user remains responsible for the decision |
| Start run | Authenticated user starts the run after execution validation | Always on the Web path |
| Secret wiring | User acknowledges the exact Web wiring set immediately before launch | Applies when Web execution wires secrets |
| Second-person governance | An approver other than the author approves the exact state | Off unless `workflow_governance` is enabled |
| LLM row output | No product approval before it reaches a downstream step or sink | Deployment must add review or sink controls where consequence requires it |

The immediate `create_blob` path creates custody and storage before a later
composition proposal is approved. Deployments using `explicit_approve` must
include that fact in data-minimisation, retention and author training.

### 8.2 CLI/YAML execution

The [`elspeth run` command](../../src/elspeth/cli.py) requires `--execute` but
does not construct or require a Web `UserIdentity`.

| Decision point | Product gate | Limitation |
|---|---|---|
| Start run | `elspeth run --execute` is required after configuration and preflight validation | The invoking operator or automation process supplies authority; ELSPETH does not require a signed-in Web user |
| Secret wiring | CLI configuration and process/secret-store authority govern resolution | The Web acknowledgement token is not required on this path |
| Model output | Schema, row and sink handling apply according to the pipeline | ELSPETH does not require a person to approve each output or each automated invocation |
| Separation of duties | External scheduler, host and operator controls | Web workflow governance does not automatically apply |

### 8.3 Reviewer competence and escalation

The deployment must train authors and reviewers on model limitations, prompt
injection, data handling, source verification, automation bias, the meaning of
advisor and validation results, and when to stop or escalate. Reviewers need
authority to pause execution, quarantine outputs, revoke access to a provider
or profile, and initiate incident response.

## 9. Evaluation, monitoring and lifecycle control

### 9.1 Pre-use evaluation

Evaluate each use case with versioned and access-controlled test material that
represents ordinary cases, edge cases, affected groups, adversarial prompts,
unsafe content, provider failures and downstream schema failures. Define
metrics before testing: task quality, false acceptance/refusal, harmful
content, fairness measures, accessibility outcomes, latency and cost. Keep the
evaluation separate from tuning material and record model, prompt, retrieval
corpus, settings, date and reviewer.

### 9.2 Runtime monitoring

Use the existing call and audit records to monitor provider/model changes,
finish reasons, failures, token and cost trends, validation or safety-control
refusals, override rates, complaints, contested outcomes, incidents and sampled
quality. Monitoring must avoid creating an unassessed content-rich telemetry
copy. Alert thresholds, sampling and response ownership are deployment facts.

### 9.3 Re-assessment triggers

Re-run the affected parts of this assessment before or promptly after:

- changing model, provider, endpoint, region, terms or subprocessor;
- changing system prompts, tool schemas, profiles, plugin policy, trust mode,
  required controls, retrieval index or downstream sink;
- adding a use case, dataset, affected group, automated decision or material
  consequence;
- changing retention, tracing, telemetry, sharing or access arrangements;
- observing drift, a threshold breach, disparate impact, recurring override,
  significant complaint, privacy/security incident or provider deprecation; or
- changing applicable policy, law, records authority or agency guidance.

### 9.4 Deployment record — evaluation and monitoring plan

| Record | Required value |
|---|---|
| Evaluation datasets and provenance | DEPLOYMENT-TODO: identify controlled datasets, representativeness and lawful access |
| Metrics and acceptance thresholds | DEPLOYMENT-TODO: define metrics and thresholds without copying ratings into this baseline |
| Baseline result and independent review | DEPLOYMENT-TODO: link the result, reviewer and date |
| Runtime signals, sampling and alerts | DEPLOYMENT-TODO: define signals, thresholds and response |
| Drift and regression cadence | DEPLOYMENT-TODO: define cadence and comparison baseline |
| Stop, rollback and provider-withdrawal criteria | DEPLOYMENT-TODO: define authority and procedure |
| Incident and complaint linkage | DEPLOYMENT-TODO: link response, privacy and service-management procedures |

## 10. Australian Government AI policy mapping

The reusable reference baseline is:

- [Policy for responsible use of AI in government v2.0](https://www.digital.gov.au/ai/ai-in-government-policy),
  effective 15 December 2025;
- [AI Impact Assessment Tool](https://www.digital.gov.au/ai/impact-assessment-tool),
  updated 1 December 2025;
- [AI Technical Standard](https://www.digital.gov.au/policy/ai/AI-technical-standard),
  updated 22 August 2025;
- [Agentic AI addendum to the AI Technical Standard](https://www.digital.gov.au/policy/ai/agentic-ai-addendum),
  updated 4 June 2026; and
- the OWASP 2025 reference in § 5.

The Composer planner is an LLM tool loop: it maintains session state, selects
and invokes bounded authoring tools, and uses their results in later turns.
Government deployments that enable Composer must therefore assess it against
the Agentic AI Addendum as well as the underlying AI Technical Standard.

| Policy theme | Reusable coverage | Deployment completion |
|---|---|---|
| Accountability and governance | Product/deployment boundary, separate authority surfaces and evidence model | Name the accountable official, use-case owner, register and approvals |
| Transparency | §§ 6.3–6.4 and call traceability | Publish or supply the applicable statement and notices |
| Impact assessment | §§ 1, 2.2, 6 and 9 | Complete the current tool for each applicable use case |
| Human oversight and competence | § 8 | Assign trained reviewers, escalation and override authority |
| Fairness and accessibility | §§ 6.1–6.2 | Evaluate actual groups, data and service channel |
| Testing, monitoring and change control | § 9 | Approve metrics, thresholds, cadence and withdrawal criteria |
| Privacy, security and records | §§ 3–5, 7 and [12](12-privacy-impact-assessment.md) | Determine legal applicability, disclosure, retention and records obligations |
| Agentic governance, workflow, tools and lifecycle | Composer inventory in § 2; authority and tool controls in §§ 4.1, 4.3 and 8; session memory and retention in § 7; evaluation, monitoring and withdrawal in § 9 | Record applicability of Agentic AI Addendum statements AGT.1–AGT.8; define human oversight and intervention, memory controls, tool permissions, agent-level evaluation and monitoring, fallback, rollback and secure decommissioning |

Policy applicability, the relevant ISM release and any agency-specific AI
requirements are deployment decisions; record them in the controlled copy and
[05 — Statement of applicability](05-statement-of-applicability.md).

## 11. Deployment record — final decisions

| Decision | Required value |
|---|---|
| Applicable policies, laws, standards and agency instructions | DEPLOYMENT-TODO: record applicability and versions |
| Planner/advisor models and endpoints; same-model exception | DEPLOYMENT-TODO: record configuration and approved exception, if any |
| LLM profiles, safety controls, retrieval and plugin allowlist | DEPLOYMENT-TODO: record enabled set and control modes |
| Web trust mode and workflow governance | DEPLOYMENT-TODO: record required user posture |
| CLI/YAML operator and automation authority | DEPLOYMENT-TODO: record who or what may invoke `--execute` |
| Composer, pipeline, quota and cost limits | DEPLOYMENT-TODO: record configured values and alerting |
| Telemetry and tracing | DEPLOYMENT-TODO: record granularity, destinations and approval |
| Retention and authorised readers by store | DEPLOYMENT-TODO: complete § 7.2 decisions |
| Training, notices and user guidance | DEPLOYMENT-TODO: record material and completion evidence |
| Residual risks, treatment and acceptance | DEPLOYMENT-TODO: link controlled risk-register entries, owners and approval dates |
