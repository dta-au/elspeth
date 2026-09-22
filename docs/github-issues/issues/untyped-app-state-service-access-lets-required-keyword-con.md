---
title: Untyped app.state service access lets required-keyword contract breaks reach runtime
labels: [area/web, type/bug]
---

Adding a required keyword-only parameter to a service method is a hard contract change at the callee, but web routes reach those services through `request.app.state.<service>`, which is typed `Any`. mypy never checks the call, so an omission surfaces only as a 500 at runtime.

## What happens

A staging trial of `release/0.8.0` at `77fe3b2fb` produced two 500s on user-facing paths, both of the same shape:

1. `get_messages` in `src/elspeth/web/sessions/routes/messages.py` called `record_audit_grade_view_async()` — which records that someone viewed a session's audit-grade message history — without the newly required `auth_provider_type`. A `TypeError` 500 on every audit-grade messages view.
2. The wiring-confirmation branch of `post_guided_respond` in `src/elspeth/web/sessions/routes/composer/guided.py` called `validate_state()` without the newly required `session_operation_context`. A `guided.operation_terminal_failure` 500 on every confirmation in that step of the guided authoring flow.

## Why

The work starts in the route modules under `src/elspeth/web/sessions/routes/`. `app.state` there is FastAPI's per-application attribute bag: services are attached to it at startup and read back inside request handlers. Because the attribute read is untyped, two layers have to fail together — and both did.

**Production.** The contract is enforced at the callee, but the route's path to it is invisible to static checking. `request.app.state.execution_service.validate_state(...)` in `guided.py` is still an unannotated attribute read. The correct shape already exists a few files away: `_verify_session_ownership` in `src/elspeth/web/sessions/routes/_helpers.py` annotates the local binding first — `service: SessionServiceProtocol = request.app.state.session_service` — which restores checking for every call made through it.

**Tests.** The suite relied on test doubles whose signatures matched the *old* call. `DualFencedSessionServiceHarness` supplied a default `auth_provider_type="local"`, and the execution-service fakes on the guided path declared `validate_state` with the pre-change signature. A guard whose only enforcement is the shape of a test double is not a guard.

In the current tree both call sites pass the required keyword, the harness no longer declares `auth_provider_type`, and the guided fakes require the context. Nothing structural prevents the next omission.

## Impact

Every route that reaches a service through an untyped `app.state` read, which is most of them. The failure is a 500 on a user-visible path, invisible both to the type checker and to a suite whose doubles are permissive.

## Fix

Two self-contained static-analysis rules, medium-sized and independent of each other. The call sites themselves need no further work; the point is to stop the next one. Rules live under `elspeth-lints/src/elspeth_lints/rules/`, each a module directory — `trust_boundary/` is a worked example of the layout.

- **Typed boundary.** Flag any `request.app.state.<name>` read in a route that is then called, where the local binding carries no annotation.
- **Doubles may not be narrower than what they stand in for.** For classes installed on `app.state` in test fixtures, or that subclass a production service, compare the set of keyword-only parameters without defaults, per method, against the production protocol's. Any default added on the double is a finding.

Done looks like: deleting a required keyword from one of the two call sites makes the first rule fail, and adding a default for that keyword to one of the test doubles makes the second rule fail. Both should pass on the tree as it stands.

An ad-hoc AST sweep for call sites omitting a watched keyword-only parameter (`session_operation_context`, `auth_provider_type`, `guided_fence`) where every definition requires it found no further occurrences beyond those two, correctly excluding one `functools.partial` binding. Keying the rule on "a required keyword-only parameter was added to a protocol" rather than on a fixed list of names is what would make it durable, and is the one design question to settle before writing it.
