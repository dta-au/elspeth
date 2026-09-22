---
title: Untyped app.state service access lets required-keyword contract breaks reach runtime
labels: [area/web, type/bug]
---

Adding a required keyword-only parameter to a service method is a hard contract change at the callee, but routes reach those services through `request.app.state.<service>`, which is typed `Any`. mypy never checks the call, so the omission surfaces only as a 500 at runtime.

## What happens

A staging trial of `release/0.8.0` at `77fe3b2fb` produced two 500s on user-facing paths, both of the same shape:

1. `get_messages` in `src/elspeth/web/sessions/routes/messages.py` called `record_audit_grade_view_async()` without the newly required `auth_provider_type` — a `TypeError` 500 on every audit-grade messages view.
2. The wiring-confirm branch of `post_guided_respond` in `src/elspeth/web/sessions/routes/composer/guided.py` called `validate_state()` without the newly required `session_operation_context` — a `guided.operation_terminal_failure` 500 on every step-4 confirm.

Both were inherited from the shared platform parent `a2176dfe2`.

## Why

Two layers have to fail together, and both did.

**Production.** The contract is enforced at the callee, but the route's path to it is untyped. `request.app.state.execution_service.validate_state(...)` in `guided.py` is still an unannotated attribute read, so no static check sees the call. The correct shape already exists a few files away: `_verify_session_ownership` in `src/elspeth/web/sessions/routes/_helpers.py` annotates the local first — `service: SessionServiceProtocol = request.app.state.session_service` — which restores checking.

**Tests.** The suite relied on doubles whose signatures matched the *old* call. `DualFencedSessionServiceHarness` supplied a default `auth_provider_type="local"` — an explicit fail-open default — and the execution-service fakes on the guided path declared `validate_state` with the pre-change signature. A guard whose only enforcement is the shape of a test double is not a guard.

In the current tree both call sites pass the required keyword, the harness no longer declares `auth_provider_type`, and the guided fakes require the context. Nothing structural prevents the next omission.

## Impact

Every route that reaches a service through an untyped `app.state` read, which is most of them. The failure is a 500 on a user-visible path and is invisible to the type checker and to a suite whose doubles are permissive.

## Fix

Two rules worth formalising as `elspeth-lints` rules:

- **Typed boundary.** Flag any `request.app.state.<name>` read in a route that is subsequently called without an annotation on the local binding.
- **Doubles may not be narrower than what they stand in for.** For classes installed on `app.state` in conftests, or that subclass a production service, compare the keyword-only-without-default set per method against the production protocol's. Any default added on the double is a finding.

An ad-hoc AST sweep for call sites omitting a watched keyword-only parameter (`session_operation_context`, `auth_provider_type`, `guided_fence`) where every definition requires it found no further occurrences beyond those two, correctly excluding one `functools.partial` binding. Keying a rule on "a required keyword-only parameter was added to a protocol" rather than on a fixed watch-list is what would make it durable.
