---
title: Untrusted-content admission does not propagate taint from source roots
labels: [area/engine, area/plugins, type/task]
---

ELSPETH can recognise a transform that produces externally controlled content and require a prompt shield downstream of it. A source cannot make that declaration at all, so data that is externally controlled from the moment it enters the graph is treated as trusted.

## Why

Two terms. A **prompt shield** is a control that inspects content before it reaches a model, defending against instructions smuggled in through data. **Taint** is the property of being externally controlled: once content is tainted, everything derived from it should stay tainted until something clears it.

The entry point is `untrusted_content_transform_names()` in `src/elspeth/plugins/infrastructure/manager.py`:

```python
def untrusted_content_transform_names() -> frozenset[str]:
    """Return transforms whose produced content requires untrusted-input controls."""
    return frozenset(
        plugin_cls.name for plugin_cls in _registered_transform_classes() if plugin_cls.content_trust is ContentTrust.UNTRUSTED
    )
```

It builds its vocabulary from `_registered_transform_classes()` — transforms, and nothing else. Its consumers then ask whether a *producer* is in that set: three checks in `src/elspeth/web/interpretation_state.py`, including the prompt-shield admission check, and one in `src/elspeth/web/composer/planner_authoring_aids.py`. A source can never be a member, so no source ever triggers those checks.

`ContentTrust` is defined in `src/elspeth/contracts/plugin_capabilities.py` with two members, `TRUSTED_INTERNAL` and `UNTRUSTED`, and the plugin base class defaults to `TRUSTED_INTERNAL`. No plugin under `src/elspeth/plugins/sources/` declares `content_trust` at all — verified by grep over that directory, with a positive control on a transform that does declare it and a negative control on a token appearing nowhere.

## Impact

Sources such as Dataverse, S3, Azure Blob and the blob-backed readers can carry content an outsider controls, while presenting to the graph as trusted internal data. A pipeline that reads externally controlled rows and feeds them straight to a model gets no admission check, where the same content arriving via `web_scrape` would.

## Fix

**A design decision has to come first, and no code can start until it is answered.** That is the honest size of this ticket: the implementation is moderate, the semantics are the hard part. At minimum:

- Is trust a property of a whole source, of a row, or of individual fields?
- How does it propagate through nodes that combine rows — aggregations, forks, joins? Reachability lives in `src/elspeth/core/dag/`.
- Does a prompt shield downstream *clear* the taint for everything after it, or only satisfy the requirement at that point?

Once settled, the work is in two parts: a source-side trust contract so a source can declare that the rows it emits are externally controlled, and propagation through graph reachability so that declaration reaches every downstream consumer.

Done looks like: a pipeline whose source declares untrusted content and which feeds a model with no shield in between is refused at graph build, with the same diagnostic a `web_scrape`-fed pipeline in the same shape produces today — and a test that mutates the declaration to trusted and confirms the refusal disappears.

This was deferred rather than folded into the earlier change that closed the transform side, because it needs the contract design and taint semantics above rather than a small edit.
