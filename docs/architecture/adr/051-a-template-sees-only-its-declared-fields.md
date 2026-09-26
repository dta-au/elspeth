# ADR-051: A Template Sees Only Its Declared Fields

**Date:** 2026-09-26
**Status:** Accepted
**Deciders:** ELSPETH maintainer (lane-owner decision Q5 on elspeth-5887fb7928 S0, 2026-09-26, under the operator's standing delegation of calls that follow from existing rulings; amended by the two-seat specialist review the same day)
**Review evidence:** `.claude/lanes/5887-batch-row/specialist-q5-architecture.md`, `.claude/lanes/5887-batch-row/specialist-q5-template-safety.md`, and the three S0 red-team rounds `review-S0-sandbox-r1..r3.md` (1, then 7, then 19 new escape shapes against the static gate)
**Tags:** template, llm, rag, confidentiality, declaration-contract, audit-integrity, composer-runtime-parity
**Depends on:** [ADR-013](013-declared-required-fields-contract.md), [ADR-032](032-validate-by-trust-domain.md), [ADR-040](040-composer-runtime-validation-posture.md)
**Amends:** [ADR-013](013-declared-required-fields-contract.md) (for a template transform the declaration is also what the template can see), [ADR-040](040-composer-runtime-validation-posture.md) (the confidentiality of undeclared fields is enforced on the executor surface, by construction)

## Context

An LLM prompt template is operator-authored code that runs against row data,
and what it renders is sent to an external provider. A node declares the row
fields it reads in `required_input_fields`. Until this decision the template's
`row` was the whole row, and the only thing standing between an undeclared
column and the provider was a static analysis of the Jinja source
(`core/templates.extract_jinja2_field_usage`): configuration refused a
template whose reads the analysis could not name.

That analysis cannot be complete. Jinja can carry a row through lists, dicts,
tuples, loop targets, `loop.nextitem`, method calls on carriers, macro
arguments, `varargs`, `kwargs` and `caller()`, and any Mapping consumer
(`dictsort`, `items`, `dict(row)`, `'%(x)s' % row`, `.format`) then reads every
column. S0 made the template row a `Mapping` of plain field values (so no
template can reach `PipelineRow` or `SchemaContract` API) and added a
"whole-row" static gate. Three red-team rounds found 1, 7 and then 19 new
escape shapes, each admitted by configuration and each sending undeclared
columns — sentinel values included — to the provider in a real
`elspeth run --execute`. Some escapes are plain field reads, not Mapping
artefacts (`{% for v in [[row]] %}{{ v[0].secret }}`), so dropping the
Mapping base would not have closed the class either. Three further measured
facts:

- An **omitted** declaration (`None`) was admitted whenever the analysis found
  no row read, and three admitted forms then rendered the whole row.
- The web **required-control coverage** (`web/plugin_policy/coverage.py`)
  computed the fields a guardrail must scan from the same analysis, so it
  credited a control scoped to `[note]` for a template that sent `secret`
  too — a security control certifying something false.
- The same analysis hung on self-referencing carriers (fixed in `eef47097b`
  with a path cap, reported as `carrier-limit`); that fix is independent of
  confidentiality and stays.

## Decision

**What a template can see of a row is its node's declaration, enforced when
the render context is built.** The row a template renders against is
*projected* in the parent process, before the context is pickled for the
render worker. A field the node did not declare never reaches the worker, so
no Jinja shape — known or not yet found — can render it.

(a) **`required_input_fields` is a template transform's visibility boundary as
well as its precondition.** This formalises what the LLM validators and the
composer teaching already said ("a `prompt_template` may not read a
`row.<field>` its `required_input_fields` omits"). One consequence: a field
that is genuinely optional has no visibility channel except the `[]` opt-out;
an `optional_input_fields` declaration would be the way to add one later.

(b) **The declaration is read one way** (`declared_row_projection`):

| declaration | the template's `row` holds |
|---|---|
| a list | exactly those fields the row carries, readable by canonical name and by original header spelling |
| `[]` (the documented opt-out) | the whole row, as before |
| omitted (`None`) | nothing — never the whole row |

A multi-query node's `row.source_row` is the same projection of the same
declaration. A query's `input_fields` values are read in the parent from the
full row (they are themselves the operator's per-query declaration, and may
name `image_inputs` columns). Declarations are canonical names (the 2026-09-25
field-name spelling rule), and the projection filters `PipelineRow.name_index`
to the declared targets so both spellings of a declared field resolve. The
projection is a required, typed argument (`DeclaredFields` | `AllFields`) of
the one constructor that turns a `PipelineRow` into a `TemplateRow`
(`TemplateRow.project`); an unprojected `PipelineRow` reaching the context
packer is a `FrameworkBugError`.

(c) **The static analysis is configuration's early error, not a
confidentiality control.** It stays a hard refusal: an undeclared literal read
(`row.secret` with `[note]`), a computed key (`row[k]`, `row.get(k)`,
`row|attr(k)`) — through `row.source_row` too — the reserved row-API names and
`carrier-limit` still fail at `elspeth validate`, in the composer and at run
start, because each would otherwise fail every row. The "whole-row" kind is
retired: a whole row used as a value holds only the declared fields, which is
exactly what the node declared. Relaxing the remaining dynamic-key refusal is
a later UX decision, not part of this one. A multi-query `row.source_row.<col>`
read must be covered by `required_input_fields` itself (not by `image_inputs`
columns), on both the plugin and the composer surface, because that is what
the projected `source_row` holds.

(d) **Required-control coverage reads the declaration.** The protected set of
an LLM node is its `required_input_fields` plus every query's `input_fields`
values. `[]` (the whole row) and an omitted or malformed declaration are not a
provable set: only a control scanning `fields: all` covers them.

(e) **`_variables_hash` is the hash of what the template could see**: the
projected field values (for a query, its variables with `source_row`
projected). Together with the recorded node configuration it states exactly
which fields each rendered prompt could draw on. A non-canonical value (NaN,
Infinity) in an undeclared column no longer fails the row, and under `[]` the
hash is unchanged.

(f) **RAG `query_template` is in scope, as the next unit (RAG-projection).**
It renders `row.to_dict()` today and has no declared read set, so it gets the
LLM-shaped validators first (template row reads ⊆ `required_input_fields` ∪
`{query_field}`; omitted plus row reads refused; the declared-but-unreferenced
dual) with composer agreement, then this same projection. It is not an
exception to this decision.

(g) **Reversibility: moderate.** Widening the view back is a one-line change in
`declared_row_projection`, but the documented template semantics, the meaning
of `_variables_hash` and the coverage rule would all move again, and every
leak shape this decision closes by construction would reopen.

### Reading an undeclared field

A template that reads a field outside the declaration fails the row with its
own value-free reason, distinct from a declared field the row does not carry:

```
Undeclared field: the template reads 'secret', a field this node does not declare in required_input_fields
```

The key is printed only when the template spells it out; a key computed from
the row prints as `<a key the template does not spell out>`. The failure is a
render-phase row failure (routed through `on_error`, `template_rendering_failed`),
never an abort.

**A deliberate deviation from the `Mapping` contract.** `'x' in row` and
`row.get('x', default)` on an *undeclared* name raise that error instead of
answering `False` or the default; so do `row.x`, `row['x']`, `row|attr('x')`
and `row.x is defined`. A template can learn nothing about an undeclared field,
not even that it is absent — `for k in row` over the whole row used to list
undeclared column *names* to the provider. On a *declared* field the row does
not carry, `in` is `False`, `get` returns its default and a lookup is the
ordinary undefined, which is the documented guard idiom. `len`, iteration,
`dict(row)`, `**row`, `items` and `dictsort` see the declared fields the row
carries and work as a `Mapping`.

## Consequences

- The confidentiality guarantee holds by construction for every Jinja shape.
  The S0 leak corpus (268 single-row forms and 13 multi-query forms from the
  r1–r3 red-team rounds and the fix rounds) is a runtime test with zero
  sentinel hits (`tests/unit/plugins/infrastructure/test_template_projection.py`),
  independent of the static analysis. The transport bytes are tested too.
- A template form the analysis cannot see and that reads an undeclared field
  now fails every row at render (routed, value-free) instead of leaking. That
  is the "configuration green, rows fail" shape the tree already accepts for
  guarded optional reads.
- `required_input_fields` and `_variables_hash` together are an auditable
  data-flow record: which columns could reach each provider call.
- Indirect prompt injection through an undeclared free-text column, and data
  minimisation, stop depending on template hygiene.
- No shipped example, composer skill or scenario fixture uses a form whose
  output changes (measured, `specialist-q5-architecture.md` §1); the `[]`
  opt-outs render byte-identically.

## Related

- [ADR-013](013-declared-required-fields-contract.md): the declaration as a
  per-row precondition; this ADR adds visibility.
- [ADR-040](040-composer-runtime-validation-posture.md): the executor surface
  now carries the confidentiality guarantee; Stages 1/2 carry the early error,
  and the composer's multi-query column check mirrors the plugin's.
- `src/elspeth/plugins/infrastructure/templates.py` (`TemplateRow.project`,
  `declared_row_projection`), `src/elspeth/plugins/transforms/llm/templates.py`
  (`_variables_hash`), `src/elspeth/web/plugin_policy/coverage.py`
  (`_llm_input_fields`).
