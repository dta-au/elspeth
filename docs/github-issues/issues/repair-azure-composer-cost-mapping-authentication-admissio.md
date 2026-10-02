---
title: Repair Azure Composer cost mapping, authentication admission and LLM response handling
labels: [area/composer, area/deployment, type/task, needs-triage]
---

A deployment on Azure reported invalid LLM cost metadata from the Composer. This covers
reproducing and repairing the cost mapping, and reviewing the Azure Composer authentication
admission and LLM response handling alongside it.

## Background

The Composer records, per LLM call, what the call cost, and the planner enforces a cumulative
spend cap from those figures. A cost that is wrong is not only a reporting error: it moves the
cap. ELSPETH accepts a cost from one of three sources, named in
`src/elspeth/contracts/composer_llm_audit.py`: `response_usage.cost` and
`_hidden_params.response_cost`, both reported by the provider, and `litellm.cost_per_token`, a
figure ELSPETH calculates from a pricing catalogue when neither is present.

## What happens

Composer runs on an Azure deployment recorded invalid cost metadata. LiteLLM 1.85.0 — the
release pinned when this was reported — ships a bundled pricing catalogue that does not contain
the model in use, so the calculated fallback for that model depends on a successful remote
catalogue fetch. Note that a catalogue only changes when the package update is in the deployed
image: copying source files into an older image leaves the catalogue it already carries.

## Where to start

Cost admission is in `src/elspeth/core/llm_pricing.py`, whose own docstring states the contract
this work must preserve — "Unknown prices and malformed metadata remain unavailable, never
fabricated zero" — with the accepted cost sources and their validation in
`src/elspeth/contracts/composer_llm_audit.py`. The other items below sit in different places:
run settlement in the Composer's run lifecycle, the deadline and proxy values in the tracked
Compose, systemd and nginx deployment examples. `docs/diagnostics/azure-081-deployment-handoff-review.md`
records the boundaries in more detail than this issue does, and
`docs/runbooks/azure-llm-composer-troubleshooting.md` covers cost admission and deployment
guidance.

**Size.** Too large as one ticket, and it should be split before anyone picks it up — the five
items below share only the deployment that surfaced them. Cost admission is the one with a
reported symptom and a named entry point, and is where to start. Verifying anything on the
transport or deployment items needs access to an Azure deployment; the cost, temperature and
settlement work does not.

## Scope

- **Cost admission.** A missing response cost should be recovered only when both provider-
  reported fields are absent; explicitly malformed costs and unsupported models stay
  fail-closed; any calculated cost counts towards the planner's cumulative cap. Cache-write
  recovery needs an explicit valid catalogue rate for each reported cache duration — a missing
  one-hour rate must remain unavailable, because the pricing library's zero-cost default would
  otherwise understate the cap.
- **Temperature and profile ownership.** Azure source and transform configurations omit
  temperature by default, with explicit numeric values still supported. For nodes bound to an
  operator profile, the profile owns temperature and authored node options must not override
  it; lowering a profile must distinguish an omitted value from an explicit null while
  retaining an explicit zero.
- **Deadlines and transport.** The tracked Compose and systemd examples carry a 180-second
  Composer deadline and a 240-second transport ceiling, and the nginx template must forward
  HTTP/1.1 WebSocket upgrade headers with a matching proxy timeout. Turn limits remain upper
  bounds, not a guarantee that every allowed turn fits inside the wall-clock deadline.
- **Run settlement.** Normal execution recorded a terminal run status and emitted terminal
  progress without closing the run saga. Settlement should require successful output
  finalisation, terminal durable status and terminal-event persistence under the live owner's
  fence, so a failed finalisation or event persistence stays recoverable instead of being
  marked complete.
- **Database failure diagnostics.** HTTP database failure diagnostics should carry a validated
  SQLSTATE, a bounded driver class, a connection-invalidated flag and explicitly labelled
  session-pool occupancy, and omit raw exception messages, SQL and parameters.

Configured model aliases stay in place while retained pipelines reference them; introducing a
new default alias does not rewrite append-only session history. No global alias substitution,
historical-state migration, credential reset or account-state change is in scope.

## Verification

For cost admission: a response carrying neither provider-reported cost field yields a
calculated cost from the catalogue without a network fetch, one carrying a malformed cost is
refused rather than coerced, an unsupported model stays unavailable rather than becoming zero,
and a cache-write duration with no catalogue rate stays unavailable. The catalogue assertions
must run against the catalogue bundled in the image, not against a fetched one, or they prove
nothing about a deployment. For run settlement: a run whose output finalisation fails is left
recoverable and is not reported complete.

## Limits

The reported progress-polling `OperationalError` has no driver code in the report, so its cause
is not established. Source review found scoped database transactions, not a demonstrated
connection leak. Better diagnostics enable the remaining investigation; they do not establish
the cause and do not show the original database incident is understood.

HTTP readiness alone does not prove the proxy can upgrade a connection: that needs the tracked
template installed on the host and the WebSocket ingress probe run against the deployment.
Local checks do not constitute live Azure acceptance — publishing an image, installing host
configuration, changing deployed profiles and validating live execution are all separate from
any source-level repair.

## Note

This issue records a work programme rather than a diagnosed mechanism. The
authentication-admission half is named in the title but nowhere characterised: no symptom, no
mechanism, no location. It should be characterised — symptom, mechanism and location — or
removed from the title before anyone picks this up.
