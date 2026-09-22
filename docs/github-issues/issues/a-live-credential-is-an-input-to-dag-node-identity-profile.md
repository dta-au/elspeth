---
title: A resolved credential appears to be an input to DAG node identity, breaking resume after rotation
labels: [area/engine, area/web, type/bug]
---

Every node in a pipeline gets an id derived by hashing its whole configuration. On the web execution path that configuration appears to contain the resolved API key by the time the hash is taken. If so, rotating a credential re-identifies every LLM node and makes any checkpoint taken before the rotation unresumable.

## Where this lives

Two subsystems meet here:

- **The graph builder**, `src/elspeth/core/dag/builder.py` — turns validated settings into an execution graph, assigning each node a deterministic id via its local `node_id` function. Ids must be stable across runs, because checkpoint and resume match on them.
- **The web execution path**, `src/elspeth/web/execution/` — loads a pipeline authored in the Composer, resolves any secret references, and hands the settings to the builder.

Background on secrets: a Composer-authored pipeline stores a *reference marker* rather than the credential itself — `{"secret_ref": NAME, "secret_scope": SCOPE}` — and whichever surface is about to run the pipeline materialises that marker through its own secret store.

## What happens

After an operator rotates an LLM credential, a checkpoint taken before the rotation would no longer be resumable. It fails as a topology mismatch — an error saying the graph no longer matches the one the checkpoint was taken from — which names no cause. Nothing in the message points at the credential, because nothing in the model says a credential is part of the topology.

**This consequence has not been reproduced.** It follows from the chain below. Each link was verified individually in the current tree; the end-to-end claim that the resolved configuration is the one reaching the hash was not.

## Why

Three places, each verified individually:

1. `lower_llm_profile_options` (`src/elspeth/core/llm_profiles.py:245`) injects `provider`, `model` and an `api_key` entry into the executable plugin options. What it injects is the reference marker, not a credential — the function performs no I/O and its docstring says so explicitly.
2. `load_runtime_settings` (`src/elspeth/web/execution/_validation_runtime.py:195-204`) materialises those markers through the secret store when a resolver is present, so the settings handed onward carry the real value. When no resolver is present it substitutes `SECRET_REF_VALIDATION_PLACEHOLDER` (`src/elspeth/core/secrets.py`) instead, which is why the export preflight — the path that generates YAML without running it — is unaffected.
3. `node_id` (`src/elspeth/core/dag/builder.py:223`) hashes the entire configuration with canonical JSON and truncates the digest to 12 hex characters, 48 bits. It is called for each transform at `src/elspeth/core/dag/builder.py:384` with `transform.config` verbatim — there is no filtering of which keys participate.

Resume compounds it. `compute_full_topology_hash` (`src/elspeth/core/canonical.py`) embeds each `node_id` verbatim alongside a separate `config_hash` of the same configuration, and the resume gate fails closed on any difference — `src/elspeth/core/checkpoint/compatibility.py:60-72`, reached from `src/elspeth/engine/orchestrator/resume.py`.

Separately, that 48-bit digest is persisted (in the audit trail's nodes table and in composition-state diagnostics) and shown to operators. At 48 bits over a large configuration this is not a practical secret-recovery vector and is not claimed as one. The point is narrower: a credential should not be an input to a durable identifier at all.

## Impact

If the chain holds: any deployment that rotates LLM credentials and relies on resume. Every LLM node is re-identified by a rotation, so any checkpoint spanning one becomes unresumable. Deployments that never rotate mid-run, or never resume, would see nothing.

## Fix

**Size: not small, and it starts with an investigation rather than a change.** Confirm or exclude the end-to-end behaviour first — build the same graph twice with different resolved key values and compare the node ids. If they differ, the rest follows; if they do not, this issue should be closed with the finding recorded.

Assuming they differ, the fix is to narrow the set of configuration keys fed to `node_id` so credential material and other runtime-resolved, non-structural values are excluded.

One design question must be answered explicitly rather than fallen into: whether the narrowing applies to the node-id suffix only, or also to the `config_hash` inside `compute_full_topology_hash`. These have different consumers, and changing one by accident leaves the other inconsistent.

Two constraints on any change:

- **The id's shape must not change** — only its inputs. A large corpus of expected ids is pinned in tests, and `_MAX_NODE_NAME_LENGTH = 38` (`src/elspeth/core/config.py:78`) participates in length arithmetic at many call sites. Changing the digest length or the id format turns a contained change into a wide one.
- `src/elspeth/web/_aws_ecs_acceptance/capture.py` re-derives the sink-id formula independently, in `_fixed_output_sink_node_id`, with no import linking it to the builder. Nothing will flag it if it is left behind, so it has to move in the same commit.

Done looks like: rotating a credential leaves every node id and the full topology hash unchanged. You would know by the same two-build comparison used to confirm the defect — it should show differing ids before the change and identical ids after.
