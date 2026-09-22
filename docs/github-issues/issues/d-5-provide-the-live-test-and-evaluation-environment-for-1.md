---
title: Provide a live test and evaluation environment for 1.0 deployment evidence
labels: [area/deployment, type/task]
---

Deployment acceptance and durable-state assurance for the 1.0 release need a live cloud
environment to run against. Without one, that evidence cannot be produced and the release
cannot claim it.

## What is needed

An Azure development environment, the subscription and network access required to reach it,
and testing against live model providers rather than local substitutes — enough to exercise
deployment acceptance end to end.

"Deployment acceptance" here means the checks the repository already defines for a deployed
container: the Azure Container Apps acceptance module is
`src/elspeth/web/_azure_container_apps_acceptance/` (start with its `README.md`), and the
database-backed deployment tests live under `tests/testcontainer/web/`. Those tests exercise
what they can locally; what they cannot cover is a real deployment's startup contract,
storage and secret wiring, and provider access.

## If access is unavailable

The fallback is an explicit maintainer decision narrowing the supported deployment profiles
and the scope of the 1.0 completeness claim, rather than leaving the claim resting on evidence
that was never collected. The supported profiles are a closed registry —
`DEPLOYMENT_STARTUP_PROFILES` in `src/elspeth/web/deployment_profiles.py`, one entry per
`DeploymentTarget` in `src/elspeth/web/config.py` — so narrowing them is a concrete,
reviewable change rather than a statement of intent.

## Done looks like

One of two outcomes, not neither: the Azure acceptance module has been run against a live
environment and its evidence recorded, or a merged change narrows `DEPLOYMENT_STARTUP_PROFILES`
and the stated scope of the 1.0 completeness claim to the profiles that were actually
exercised.

## Size and who can pick this up

This is not a code task and it is not startable by a new contributor: it needs cloud
subscription access and a maintainer decision about what 1.0 claims. Its value to everyone
else is as a blocker — issues that need live deployment evidence are waiting on this or on the
narrowing decision.
