# ADR-032: Boundary Validation Splits by Trust Domain — Parse External Input, Nominally Type Internal Input

**Date:** 2026-07-31
**Status:** Accepted
**Deciders:** John Morrissey, Claude Fable 5
**Tags:** trust-tier, elspeth-lints, judge-policy, external-boundary,
          duck-typing, composer, testing-doctrine

## Context

ELSPETH has run a long campaign against soft, duck-typed Python. The
motivating threat is real: an object that merely *looks* like the right
shape should not be able to walk through a guard and acquire the
privileges of the thing it is imitating. The lint policy that encodes
this bans attribute presence probing absolutely — `getattr`, `hasattr`,
`inspect.getattr_static`, forwarding `__getattr__` — and directs any
genuinely unknown-type boundary to "`isinstance` discrimination against
a declared concrete type or runtime-checkable protocol".

A P0 defect (tracker `elspeth-9ea866438b`) showed that the second half
of that instruction is wrong, and wrong in a way that inverts its own
purpose.

`src/elspeth/web/composer/tool_batch.py` admitted provider tool-call
objects — LiteLLM's reply to a composer LLM turn — by checking them
against a `runtime_checkable` `Protocol` with `isinstance()`. Since
Python 3.12, a runtime-checkable Protocol resolves attributes via
`inspect.getattr_static`, which deliberately bypasses `__getattr__`.
LiteLLM's `ChatCompletionMessageToolCall` is a pydantic v2 model with
`extra="allow"`; `id`, `type`, and `function` arrive in the extra
mechanism (`__pydantic_extra__`) and resolve only through
`BaseModel.__getattr__`. The guard therefore rejected **every genuine
tool call from every provider**.

Approximately 33,000 unit tests passed. One live HTTP call found it.
The unit tests fed the guard hand-written typed fakes — plain
dataclasses with real, statically-visible attributes — which satisfy a
guard that reality fails.

### Evidence: the mechanism is inverted

Probe on Python 3.13.1 against the installed litellm, with a
`runtime_checkable` Protocol declaring `id: str` and `type: str`,
tested against (a) an arbitrary class that just declares `id` and
`type`, and (b) a real `ChatCompletionMessageToolCall`:

```
impostor passes the guard : True
REAL vendor object passes : False
```

The guard is permissive to an impostor and strict against the honest
object. That is the exact opposite of what it was installed to do.

Supporting facts, all established by probe:

- Pydantic v2 **declared** fields live in the instance `__dict__` and
  *are* visible to `getattr_static`, so `isinstance` passes for them.
  Only `extra="allow"` extras (`__pydantic_extra__`) and `__getattr__`
  forwarding are invisible. Verified: `Declared(id=...)` → True;
  `Extra(id=...)` → False; real litellm tool call → False;
  `httpx.Response` → True; botocore `StreamingBody` → True. The failure
  is not "third-party objects fail"; it is "dynamically-resolved
  attributes fail", which is invisible from the call site and varies by
  vendor and by version.
- `unittest.mock.Mock` fails **every** `runtime_checkable` `isinstance`
  check, including `Mock(spec=...)`. A test that passes a Mock into
  such a guard silently exercises the reject branch and proves nothing
  about the accept branch.
- `hasattr` is **immune** to this hazard: it calls the real `getattr`,
  which triggers `__getattr__`. The current policy wrongly lumps it in
  with `getattr_static`.

### The core insight

The policy's goal is sound. Its recommended mechanism is not:
**`runtime_checkable` Protocol `isinstance()` IS structural typing.**
It tests only "does this object have attributes with these names" —
precisely the duck-typing subversion the policy exists to prevent.
Python 3.12 changed only *how* the names are resolved. That broke the
check against legitimate objects without ever closing the hole.

### Two threat models were conflated

They are both real, and they want opposite mechanisms.

1. **Internal masquerade.** An ELSPETH plugin or extension presents the
   right shape in order to cross a trust boundary and gain privileges.
   This is a genuine adversary and the target of the active
   `elspeth-02cd60d8cd` "eliminate banned attribute masquerading" work.
   Nominal typing genuinely works here: `isinstance` against a
   **concrete class ELSPETH owns** cannot be duck-typed past, because
   the class object itself is the credential.

2. **External vendor data.** A litellm / botocore / openai object whose
   fields we need. The vendor is not an adversary; it is a shape we do
   not control and cannot pin. Nominal typing is brittle — pinning a
   concrete vendor class breaks across SDK versions and across
   providers that return different classes for the same logical reply.
   Structural typing is both broken (above) and useless (an impostor
   passes). **Neither is the control.**

### What the control actually is at an external boundary

Parse, don't validate. Do not attempt to authenticate the object's
type. Refuse to propagate anything that has not been re-derived
internally:

1. read each needed field **once**, with a sentinel-defaulted `getattr`;
2. assert the **values** — non-empty `str`, parseable JSON, within
   bounds — and reject on the value, not on the type;
3. construct an ELSPETH-owned frozen type from what survived;
4. discard the original object.

`tool_batch.py` already did all four downstream. The value assertions
plus the read-once copy into the frozen `_AdmittedToolCall` were always
the real boundary. The Protocol check sitting on top of them was
ceremony: an illusion of a type gate that provided no protection an
impostor could not trivially satisfy. Removing it removed theatre, not
defence.

## Decision

Boundary validation is chosen by **trust domain**, not by a single
global rule:

1. **Internal boundary (ELSPETH owns the type).** Discriminate with
   `isinstance` against a **declared concrete class ELSPETH defines**.
   Never a Protocol. A concrete class cannot be duck-typed past; a
   `runtime_checkable` Protocol can be, by construction.

2. **External boundary (a vendor, SDK, network, or remote payload owns
   the shape).** Parse, don't validate. Sentinel-defaulted `getattr`
   extraction, value assertions, construction of an owned type,
   original discarded. This is the correct Tier-3 pattern and is
   explicitly *not* a violation of the presence-probing ban.

3. **Never use `runtime_checkable` Protocol `isinstance()` as a
   security control, anywhere.** It admits impostors and rejects honest
   dynamic-attribute objects. It is not a type gate.

The judge policy text in `elspeth-lints/src/elspeth_lints/core/judge.py`
is amended to encode this split, and `AGENTS.md` carries the one-line
form.

## Consequences

### This reverses part of an existing policy

- The policy previously named "runtime-checkable protocol" as an
  acceptable unknown-type discriminator. That option is withdrawn.
- The policy previously banned `getattr` **absolutely**, including at
  external boundaries ("`getattr` remains banned even here"). That is
  relaxed: sentinel-defaulted `getattr` extraction at an external
  boundary, followed by value assertions and construction of an owned
  type, is now the prescribed pattern. The ban stands undiminished for
  presence probing used to satisfy an *internal* contract.
- `hasattr` is no longer described as sharing the `getattr_static`
  hazard. It remains banned as an internal-contract presence probe, for
  the original reason — it still lets an object pretend to satisfy a
  contract — but the mechanical claim is corrected.

### This does not relax the internal-masquerade rule

`elspeth-02cd60d8cd` is unaffected. Nominal `isinstance` against
ELSPETH-owned concrete classes is exactly right for that threat and is
strengthened, not weakened, by removing the Protocol option that was
sitting next to it as an apparent equivalent.

### Existing sites

Existing `runtime_checkable` `isinstance()` checks over internal
objects are **not defects** — they do not misbehave on objects whose
attributes are statically visible, which ELSPETH-constructed objects
generally are. They are also **not protection**: they should not be
cited as a control in any threat argument, and they must not be relied
on where the checked population can include dynamic-attribute objects —
and their tests must not substitute a `Mock`, which fails every such
check and puts the test silently on the reject branch.

Two such sites are recorded here as evidence and tracked separately;
neither is fixed by this ADR:

- `src/elspeth/web/composer/llm_response_parsing.py:299-308`
  (`_provider_artifact_owned_fields`) reads `__dict__` only, while
  `_provider_field_map` at ~:100-122 in the same file correctly merges
  `__pydantic_extra__`. This is a live second instance of the same
  class of defect. It fails silently: the audit records a
  `PROVIDER_ARTIFACT_UNAVAILABLE` sentinel instead of the real
  artifact, and no test pins that sentinel.
- `_exporter_delivery_metrics`
  (`src/elspeth/telemetry/manager.py:732`) is a structurally identical
  `runtime_checkable` Protocol guard over a population that can arrive
  from third-party pluggy plugins. A false negative returns `None`
  **silently** and delivery accounting vanishes.

### Testing

A hand-written typed fake satisfies a `getattr_static`-resolved guard
that a real vendor object fails, so a green unit suite is not evidence
that an external boundary admits real traffic. Boundaries over vendor
objects need at least one test over a genuine SDK object, or a live
call. `Mock(spec=...)` is worse than useless against such a guard: it
fails the check and silently exercises the reject branch.

## Alternatives considered

### Pin the concrete vendor class (`isinstance(x, ChatCompletionMessageToolCall)`)

**Rejected.** It breaks across litellm versions and across providers
that return a different class for the same logical reply, and it still
does not establish that the *values* are usable — which is the only
property the code actually needs.

### Keep the Protocol check and additionally read via `getattr`

**Rejected.** The Protocol check contributes nothing the value
assertions do not already contribute, admits impostors, and rejects
honest objects. Keeping it preserves the illusion of a type gate, which
is the specific harm: it invites a future reader to treat the boundary
as authenticated when it is not.

### Leave the policy alone and treat the P0 as a one-off site bug

**Rejected.** The policy is the generator. It was actively directing
authors toward the broken mechanism, and it produced a second live
instance (`_provider_artifact_owned_fields`) and a third structurally
identical guard (`_exporter_delivery_metrics`) before anyone looked.

## Related decisions

- ADR-021: Sources and Sinks Are Uniformly Boundary by Architecture —
  the existing boundary-classification decision this refines.
- ADR-023: Custom Python Static Analyzer (`elspeth-lints`) — the
  analyzer whose judge policy this ADR amends.

## References

- `elspeth-9ea866438b` — the P0: composer tool-call admission rejected
  every genuine provider tool call.
- `elspeth-9bdf46887d` — companion tracker item.
- `elspeth-02cd60d8cd` — "eliminate banned attribute masquerading"; the
  internal-masquerade campaign this ADR explicitly preserves.
- `src/elspeth/web/composer/tool_batch.py` — the corrected boundary;
  its module comment records why the Protocol check must not return.
