# ADR-017: Sink Required Fields Contract

**Status:** Accepted
**Date:** 2026-04-20
**Supersedes:** none
**Depends on:** ADR-010

## Context

Sinks already had an inline transactional backstop
(`SinkTransactionalInvariantError`) that checks required fields at the commit
boundary. That backstop is intentionally late and exists to catch state
divergence. It does not provide the Layer 1 intent attribution ELSPETH needs
when a row arrives at a sink already missing a declared required field.

## Decision

Introduce `SinkRequiredFieldsContract` as a `boundary_check` adopter under
ADR-010.

- Violation class: `SinkRequiredFieldsViolation`
- Payload schema: `SinkRequiredFieldsPayload`
- Tier: 1
- Runtime observation: row payload membership (`row_data.keys()`)
- Optional context: use `row_contract` only for richer coalesce-merge
  annotation on the primary sink path
- Call posture: run before `_validate_sink_input()` and before sink I/O on both
  primary and failsink paths

### Violation

`SinkRequiredFieldsViolation` subclasses `DeclarationContractViolation`
and is registered Tier 1 via `@tier_1_error`.

Payload schema:

```python
class SinkRequiredFieldsPayload(TypedDict):
    declared: Required[list[str]]
    runtime_observed: Required[list[str]]
    runtime_observed_count: NotRequired[int]
    runtime_observed_truncated: NotRequired[bool]
    runtime_observed_omitted_count: NotRequired[int]
    runtime_observed_omitted_hashes: NotRequired[list[str]]
    runtime_observed_omitted_hashes_truncated: NotRequired[bool]
    missing: Required[list[str]]
```

`declared` and `missing` are sorted field-name lists.

The five optional keys have no counterpart in ADR-016's payload, and the
two contracts observe different things: ADR-016 observes
`row_contract.fields ∩ row_data.keys()`, ADR-017 the row payload's own
key set (§Decision above). Measured in
`src/elspeth/engine/executors/sink_required_fields.py`, the sink payload
bounds `runtime_observed` to at most 20 names, each rendered at most 64
characters wide; `runtime_observed_count` carries the true total; the
omitted names are represented by up to 20 sixteen-character SHA-256
prefixes of the *field name*; and the two `*_truncated` flags record that
bounding occurred, so a reader never mistakes the sample for the whole
set. ADR-016 records its three lists unbounded. Neither ADR nor the
contract module records why the sink payload is bounded and the source
payload is not.

## Two-Layer Architecture

- Layer 1: `SinkRequiredFieldsViolation`
  This is the dispatcher-owned pre-write contract.
- Layer 2: `SinkTransactionalInvariantError`
  This remains the inline transactional backstop for divergence between Layer 1
  evaluation and commit.

The two signals are intentionally distinct and must not be merged.

## Consequences

- Missing sink-required fields are attributed before schema validation can mask
  them as generic plugin validation failures.
- Failsinks are first-class sink boundaries and run the same Layer 1 contract.
- The Layer 2 transactional backstop remains in place for the rarer
  post-Layer-1 divergence case.

## Scrubber-audit

Payload keys are structural only:

- `declared`
- `runtime_observed`
- `runtime_observed_count`
- `runtime_observed_truncated`
- `runtime_observed_omitted_count`
- `runtime_observed_omitted_hashes`
- `runtime_observed_omitted_hashes_truncated`
- `missing`

Every value is a field-name list, a count, a bool, or a hash of a field
name. No row values, config dicts, or free-form payloads are recorded, so
no scrubber extension is required in this ADR. Forbidden payload keys for
this contract include `raw_schema_config`, `config_dict`, `options`, and
`sample_row`.
