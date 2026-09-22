---
title: A resolved credential is an input to DAG node identity, so rotation re-ids every LLM node
labels: [area/engine, area/web, type/bug]
---

Node identity is a hash of the whole plugin config, and on the web execution path that config appears to contain the resolved API key by the time the hash is taken. If so, rotating a credential changes every LLM node's id and breaks resume of any checkpoint taken before the rotation.

## What happens

After an operator rotates an LLM credential, a checkpoint taken before the rotation would no longer be resumable, failing as a topology mismatch that names no cause — nothing in the error points at the credential, because nothing in the model says a credential is part of the topology.

This consequence follows from the chain below and has **not** been reproduced. Each link in the chain was verified individually; the end-to-end claim that the resolved config is the one reaching the hash was not.

## Why

Three places, each verified individually in the current tree:

1. `lower_llm_profile_options` (`src/elspeth/core/llm_profiles.py:245`) injects `provider`, `model` and an `api_key` entry into the executable plugin options. What it injects is a secret *reference* marker — `{"secret_ref": ..., "secret_scope": ...}` — and the function itself holds no credential.
2. `load_runtime_settings` (`src/elspeth/web/execution/_validation_runtime.py:195-204`) materialises those markers through the secret store when a resolver is present, so the settings handed onward carry the real value. The branch taken when no resolver is present substitutes `SECRET_REF_VALIDATION_PLACEHOLDER` (`src/elspeth/core/secrets.py`) instead, which is why the export preflight is unaffected.
3. `node_id` (`src/elspeth/core/dag/builder.py:223`) hashes the entire config with `canonical_json` and truncates to 12 hex characters — 48 bits. It is called for each transform at `src/elspeth/core/dag/builder.py:384` with `transform.config` verbatim.

Resume compounds it. `compute_full_topology_hash` (`src/elspeth/core/canonical.py`) embeds each `node_id` verbatim alongside a `config_hash` of the same config, and the resume gate fails closed on any difference — `src/elspeth/core/checkpoint/compatibility.py:60-72`, reached from `src/elspeth/engine/orchestrator/resume.py`.

Separately, the 48-bit digest of a config containing a credential is persisted (the Landscape nodes table, composition-state diagnostics) and shown to operators. At 48 bits over a large config this is not a practical secret-recovery vector, and it is not claimed as one — but it makes a credential an unnecessary input to durable identity.

## Impact

If the chain holds, any deployment that rotates LLM credentials and relies on resume: every LLM node is re-identified by a rotation, so any checkpoint spanning the rotation becomes unresumable. Deployments that never rotate mid-run, or never resume, would see nothing. Confirming or excluding the end-to-end behaviour is the first step, ahead of any fix.

## Fix

Narrow the identity surface fed to `node_id` so credential material and other non-structural runtime-resolved values are excluded.

One design question has to be answered explicitly rather than fallen into: whether the narrowing applies to the node-id suffix only, or also to the `config_hash` inside `compute_full_topology_hash`. They have different consumers, and picking one by accident leaves the other inconsistent.

Two constraints on any change:

- The suffix *shape* must not change. A large pinned corpus depends on it, and `_MAX_NODE_NAME_LENGTH = 38` (`src/elspeth/core/config.py:78`) participates in length arithmetic at many call sites.
- `src/elspeth/web/_aws_ecs_acceptance/capture.py` re-derives the sink-id formula independently, in `_fixed_output_sink_node_id`, with no import linking it to the builder. It has to move in the same commit or it will silently disagree.

Done looks like: rotating a credential leaves every node id and the full topology hash unchanged, demonstrated by a test that builds the same graph twice with different resolved key values and asserts identity equality.
