---
title: Untrusted-content admission does not propagate taint from source roots
labels: [area/engine, area/plugins, type/task]
---

Registry-derived `ContentTrust` closes the producer vocabulary for transforms, but source-root content is never declared untrusted, so prompt-shield admission cannot see externally controlled data that enters the graph at a source.

## Why

`ContentTrust` (`src/elspeth/contracts/plugin_capabilities.py`) has two members, `TRUSTED_INTERNAL` and `UNTRUSTED`, and the plugin base defaults to `TRUSTED_INTERNAL`. No plugin under `src/elspeth/plugins/sources/` declares `content_trust` at all — verified by grep over that directory, with a positive control on a transform that does declare it and a negative control on a token that appears nowhere.

That leaves sources such as Dataverse, S3, Azure Blob and the blob-backed readers contributing content that can be externally controlled while presenting as trusted internal data. Admission decisions that depend on untrusted provenance therefore only ever see transform producers.

## Fix

Two pieces, and the second is the substantial one:

1. A source-side trust contract, so a source can declare that the rows it emits are externally controlled.
2. Taint propagation through graph reachability, so a declaration at a source root reaches every downstream consumer and prompt-shield admission applies to source-originating content on the same footing as transform-produced content.

This was deferred rather than folded into the related fix that closed the transform side: it needs source contract design and taint semantics, not a small change.
