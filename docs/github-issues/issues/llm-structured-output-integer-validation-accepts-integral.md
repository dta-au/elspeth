---
title: Integer output validation accepts integral floats but keeps the float value
labels: [area/plugins, type/bug]
---

Structured LLM output validated against an `integer` output field accepts a float such as
`1.0` and then passes that float through unchanged. The validated result claims the declared
integer contract while carrying a Python `float`.

## Why

`validate_field_value` in `src/elspeth/plugins/transforms/llm/validation.py:63-72` treats the
integer case as satisfied when the value is an `int` **or** a `float` whose `is_integer()` is
true, and returns no error in either case. The extraction path then copies the original
parsed value straight into the result — `extracted[field.suffix] = parsed[field.suffix]` at
`validation.py:275`, inside `extract_structured_fields` — with no conversion. Nothing between
validation and the consumer narrows the type.

## Repro

Validate the structured output `{"count": 1.0}` against an output field declared as
`integer`. Validation succeeds and the extracted value is still `1.0`. A downstream consumer
holding a fixed integer schema can then reject a value this boundary has already certified.

## Impact

The validation boundary certifies a value whose runtime type contradicts the declared output
schema, so a type error that should have been caught at the boundary surfaces later — at a
sink, a schema check, or a consumer that cannot explain where the float came from. The blast
radius is limited to LLM transforms with an `integer` output field whose provider returns a
JSON number rendered as a float.

## Where the work starts

One file: `src/elspeth/plugins/transforms/llm/validation.py`. The two functions above are the
whole mechanism. Its tests are `tests/unit/plugins/llm/test_validation.py`, with property-based
coverage in `tests/property/plugins/llm/test_response_validation_properties.py`.

## Fix — one decision, then a small change

Integral floats must either be rejected at validation, or normalised to a real `int` before
the value leaves validation. The source does not decide between them, and they are not
equivalent: rejecting is stricter and surfaces a provider that is not honouring the schema;
normalising is more forgiving and keeps working pipelines working. That choice belongs to the
maintainer and should be settled before the patch.

## Done looks like

A test that validates `{"count": 1.0}` against an `integer` output field and asserts either a
validation error, or that `type(extracted["count"]) is int` — matching whichever behaviour is
chosen. The general invariant worth pinning is that the runtime type of every extracted value
matches its declared output field type, not merely its numeric value.

## Size

Small — a few lines in one file plus a test, once the reject-or-normalise question is
answered. Suitable as a first issue.

Reviewed against the tree at commit `3dc67fb1d`.
