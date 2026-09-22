---
title: Retention and Azure safety properties are vacuous — no generated case reaches production parsing
labels: [area/tests, type/bug, priority/P2]
---

Two groups of property tests are the stated guard on fail-closed safety contracts. Measured
during a real run, no generated case in either group reaches the production code the contract
lives in, so every assertion is satisfied by construction.

## Background

ELSPETH uses Hypothesis property tests under `tests/property/`. Two of those files guard
contracts where failing open is the dangerous direction: retention purging, which decides
which payload references expire, and the Azure content-safety and prompt-shield transforms,
which must reject a malformed provider response rather than treat it as safe.

## What happens

Both suites pass, and they would keep passing if the code they name were broken.

**Retention.** `tests/property/core/test_retention_monotonicity.py` asserts that a longer
retention window expires no more references than a shorter one, and that a later cutoff
expires no fewer. Instrumenting `PurgeManager.find_expired_payload_refs`
(`src/elspeth/core/retention/purge.py:178`) during a real run of those two properties,
recording every call and its result count:

```
test_longer_retention_fewer_expired: 100 calls, 50 pairs, pairs where the two sides differ = 0
test_later_cutoff_more_expired:       60 calls, 30 pairs, pairs where the two sides differ = 0
```

A zero from a blind instrument looks exactly like a zero from a working one, so the same
recorded calls were re-paired across adjacent generated examples instead of within each
property, as a control: 32 of 49 and 17 of 29 pairs then differ. The detector can see a
difference; across 80 generated examples there was none to see.

**Azure.** `tests/property/plugins/transforms/azure/test_azure_safety_properties.py` says so
itself in its class docstring at `:353-357` — "Since `_analyze_content` requires HTTP
infrastructure, we test the invariants that the validation code upholds". What the cases
assert is Python semantics:

```python
assert _AZURE_CATEGORY_MAP.get(unknown_category) is None   # :373
assert not isinstance(value, bool)                         # :427
assert not None                                            # :440
assert not 0                                               # :446
assert not ""                                              # :452
```

`_analyze_content` (`src/elspeth/plugins/transforms/azure/content_safety.py:306`) and
`_analyze_prompt` (`src/elspeth/plugins/transforms/azure/prompt_shield.py:246`) are never
called.

## Why

Retention: `test_longer_retention_fewer_expired` (`:162-195`) calls
`find_expired_payload_refs(days)` with no `as_of`, so the cutoff is wall-clock now while the
fixture run completed 20 days before a fixed 2025-01-01 reference. At any retention up to 365
days the run is already expired on both sides, and the gap widens as the wall clock advances.
`test_later_cutoff_more_expired` (`:252-290`) does pass `as_of`, but with a 10-day retention
and both cutoff ranges landing after the run completed, so both sides again see the same set.

Azure: the file asserts the patterns it believes the production functions enforce instead of
driving the functions. One case notes in a comment that it "mirrors the `_analyze_prompt`
loop" (`:482`); a copy of the loop is not the loop.

## Impact

No production defect is alleged here and none was found. The cost is false assurance in the
suite. A change that broke the category lookup in `_analyze_content`, or the strict-bool check
in `_analyze_prompt`, would leave all of these green, and so would a retention cutoff that
stopped discriminating. These are the tests a reviewer would point at to argue the fail-closed
behaviour is covered.

## Fix

Make each property capable of failing.

- Retention: pass an explicit `as_of` everywhere, generate runs that straddle the cutoff rather
  than all sitting on one side of it, and assert a strict set difference rather than a length
  inequality that equality already satisfies.
- Azure: drive `_analyze_content` and `_analyze_prompt` with generated response bodies through
  a mocked HTTP layer, so the properties exercise the real parsing.

Prove it by mutation, not by a green run: break the category lookup, break the strict-bool
check, and make the retention cutoff ignore `as_of`. Each named property must then go red, and
recording those three mutations in the change is what distinguishes a repair from rewriting the
tests into a different vacuous shape.

**Size.** Moderate, and mostly generator design. The Azure half needs the HTTP boundary mocked
at the right seam; the retention half is arithmetic on the generation strategy. No product
decision is required first.
