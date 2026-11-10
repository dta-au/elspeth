# Independent review: 0.8.12 gate repair (`6e226d6e7`, `c9e3d7b00`, `b9c84a7a7`)

Read-only review of branch `fix/composer-0812-gate-repair` (base `2f38b807b`), reviewed at `b9c84a7a7`. Findings were formed from the diffs and from `dc67bdd2f` before reading the author's report at `150873a57`.

**Verdict: nothing must be fixed before landing.** The re-pinned source is correct on every live path, the rewritten test still tests the same thing, and the deleted helper has no remaining code reference. Three SHOULD-FIX items concern the new registration contract: it is weaker than its docstring says. None is a regression of an existing check.

## How this was checked

- **Read:** the three commits, `dc67bdd2f` for the two pinned files, and at `b9c84a7a7` the full `async_workers.py` registration and shutdown regions, the custodian in `required_executor.py`, `composer_mcp/standalone.py`, `composer_mcp/runner.py`, `process_recovery.py`, the contract helper, `shareable_reviews/service.py` and `SessionServiceImpl.add_message`.
- **Probed:** the contract checker was run against mutated inputs on a `git archive` extract of `b9c84a7a7` in a scratch directory (since deleted). `elspeth.__file__` and the helper's `__file__` resolved inside the extract. Baseline on the extract: no failures on the five-file subset and on all 995 production units; both sha256 pins match the file bytes.
- **Not run:** no pytest. Every statement about a test passing is the author's.

## SHOULD-FIX

### S1. A re-exported wrapper call passes the contract; the docstring says it cannot

`tests/helpers/web_recovery_producer_contract.py:6-12` at `b9c84a7a7` says "Any other call, alias or escape of either callable fails the contract." It does not. `standalone.py:13` imports the wrapper at module level, so `elspeth.composer_mcp.standalone.configure_standalone_executor_recovery` is a second name for it. The call collector (helper lines 625-628) and the escape check (line 676) match only the `elspeth.web.async_workers.*` name.

Probe results (a new module added to the five-file set, verified):

| New module body | Contract result |
| --- | --- |
| `from elspeth.web.async_workers import configure_standalone_executor_recovery` then a call (control) | `recovery standalone registration call/escape universe changed` |
| `from elspeth.composer_mcp.standalone import configure_standalone_executor_recovery` then a call | `[]` |
| `from elspeth.composer_mcp import standalone` then `standalone.configure_standalone_executor_recovery(...)` | `[]` |
| the same lazy import inside a function in a new `composer_mcp` module | `[]` |
| `from elspeth.web.app import configure_required_executor_recovery` then a call | `[]` |

- **Introduced here:** the first three passing rows. The wrapper's runtime guard still bounds them: such a call can only be a first registration.
- **Not introduced here:** the last row, a third unguarded registration of the base function through `elspeth.web.app`. `6e226d6e7` does not touch `qualify` or the import environment, so this hole predates the commit (by inspection of the diff, not by running the base helper). It has no runtime backstop.
- **Live tree:** `git grep` over `src` at `b9c84a7a7` finds only the two reviewed call sites, so nothing exploits this today.

**Smallest correction:** canonicalise the two re-export names inside `qualify` (map `elspeth.web.app.configure_required_executor_recovery` and `elspeth.composer_mcp.standalone.configure_standalone_executor_recovery` to their `async_workers` names), and add the first and last rows above as negative controls. Tried on the scratch copy: all four passing rows turn red with the expected universe message, and both baselines stay `[]`. The rest of `test_web_landscape_mutation_fencing.py` was not run against that patch. The trial patch, since the scratch copy is gone:

```python
# beside CELLS
REEXPORTS = {
    APP + ".configure_required_executor_recovery": WORKER + ".configure_required_executor_recovery",
    STANDALONE + ".configure_standalone_executor_recovery": STANDALONE_REGISTRATION,
}

# in qualify()
if isinstance(value, ast.Name):
    return frozenset(REEXPORTS.get(q, q) for q in env.get(value.id, frozenset({module + "." + value.id})))
if isinstance(value, ast.Attribute):
    roots = qualify(value.value, env)
    if value.attr not in {"__dict__", "__globals__"}:
        return frozenset(
            REEXPORTS.get(root + "." + value.attr, root + "." + value.attr) for root in roots if root.count(".") < 8
        )
    # the existing __dict__ / __globals__ arms follow unchanged
```

### S2. The standalone call site is admitted by name only; its arguments are not pinned

Helper lines 844-852 at `b9c84a7a7` check that exactly one wrapper call exists and that it sits in `run_standalone`. The `create_app` site, by contrast, has its call expression, argument sources and dominance pinned. Mutations of `standalone.py` that pass the contract (verified):

| Mutation of `run_standalone` | Contract result |
| --- | --- |
| `instance_draining=threading.Event()` instead of `draining` | `[]` |
| `recovery_callback=print` | `[]` |
| registration wrapped in `for _ in range(2):` | `[]` |

The first row is the one that matters. `ProcessRecovery` sets its own event; admission reads `_INSTANCE_DRAINING`. If the two differ, a drain that `ProcessRecovery` starts (`begin_shutdown`, a monitor failure, `request_shutdown`) no longer closes admission. `shutdown_async_workers` still closes it later through `_SHARED_SHUTDOWN_STARTED` (`async_workers.py:548`, refused at `:533-534`). The live code is correct (`standalone.py:24` and `:33` pass the same `draining`).

The check also does not pin where in `run_standalone` the call sits, only its module and enclosing function. That is by inspection of helper lines 844-852, not probed.

**Smallest correction:** pin the call expression in `run_standalone` by AST equality, as `CALL` does for `create_app`, and add the first two rows as controls. Not tried.

### S3. `_RECOVERY_CALLBACK`, the cell the guard depends on, is not protected from an outside reset

The wrapper is admissible only because `_RECOVERY_CALLBACK` is never cleared once an owner registers. It is the one guard cell that stays set after a web shutdown: `shutdown_async_workers` resets `_SHARED_EXECUTOR` to `None` at `async_workers.py:632`. `CELLS` (helper line 23) still names only the two original cells. Probe (verified):

| New module body | Contract result |
| --- | --- |
| `async_workers._INSTANCE_DRAINING = None` (control) | `recovery producer qualified binding/member replacement` |
| `async_workers._RECOVERY_CALLBACK = None` | `[]` |

An in-module reset is covered by the whole-file hash pin; a cross-module one is not. No production module does this (`git grep _RECOVERY_CALLBACK b9c84a7a7 -- src` finds only `async_workers.py`).

**Smallest correction:** add `_RECOVERY_CALLBACK` to the names that the qualified-store and reflected-replacement checks protect, with the second row as a control. Not tried.

## NOTES

### Item 1: the lines `dc67bdd2f` added (independent review agrees with the author: correct)

- **Embedded host, web registered first:** the wrapper refuses at `async_workers.py:111-112`. `run_standalone` records the error with `owned = False`, so it never calls `shutdown_async_workers` and never issues the completion witness. The host's workers are untouched. Verified by reading.
- **Reverse order is not refused.** `configure_required_executor_recovery` (`async_workers.py:70-100`) has no existing-owner check, so a `create_app` after a standalone registration replaces every cell. An already-built custodian would keep the old events. Not reachable today: `git grep create_app b9c84a7a7 -- src/elspeth/composer_mcp` returns nothing (control: `run_server` returns four lines there), and `runner.py` demands a fresh interpreter. This is the base function's behaviour and predates the change.
- **Ordering:** registration precedes `create_catalog_service()` and `run_server`. Any earlier submission would fail closed in `_get_shared_executor` (`_assert_recovery_registered`).
- **Thread safety:** `_RECOVERING_GENERATIONS` is mutated only on the event-loop thread. All five `quarantine` sites are direct calls inside `async def` bodies (`async_workers.py:285, 333, 946`) or go through `loop.call_soon_threadsafe` (`:308, :923`). The guard is an unlocked check-then-set, which is safe with one caller at task start.
- **Registration failure:** argument validation at `async_workers.py:86-94` raises before any cell is assigned, so a failed registration leaves no partial state.
- **Shutdown settlement:** the new loop awaits each retained observer, joins the drain thread, and records drain errors (deduplicated by identity) and escalation. A hung worker makes it wait without bound, as the pre-existing `generation.joined` loop already did.
- **`assert observer is not None` (`async_workers.py:606`):** fires only if `quarantine()` fails between retain and task creation. `create_task` cannot fail there because every caller is on the loop. A `Thread.start()` failure would hang the earlier `joined` loop first. Not a defect introduced here.
- **Proof scope:** in a standalone process the two cells hold the runner's objects, not ones `create_app` built. `recovery_dependency_roles` (helper line 893) still inventories only the `create_app` and base-function regions, so the standalone site's producers are not in the role inventory. The call it makes to `_namespace_effects` without the wrapper is harmless: its failures are discarded.

### Item 1: the contract change

- **Exactly two sites:** yes for direct names. A third call, a qualified call, `getattr` with a constant name, `importlib.import_module(...)`, `sys.modules[...]`, an `as` alias, a local alias, `functools.partial`, an in-module extra call, a second unguarded wrapper, a nested or second function in `standalone.py`, and a renamed caller all fail (verified by probe).
- **Wrapper body:** pinned whole. A decorator, `or` changed to `and`, and the refusal replaced by `pass` all fail (verified).
- **New controls are real.** Each feeds mutated source through `recovery_cell_contract` and asserts the exact failure list. None is tautological.
- **No existing check weakened.** `len(calls) != 1` became `len(calls) != 2` with both node identities required; the protected-binding sets only grew.
- **Limits that predate the commit:** a relative import (`from .async_workers import ...`) and `getattr` with a computed name both pass, for the wrapper and for the base function. 38 files under `src/elspeth` use relative imports, so the first is not hypothetical.

### Item 2: `c9e3d7b00` (read, not executed)

- **Still tests the same thing.** The asserted reason string is produced only by `record.creation_modality is not CreationModality.VERBATIM` (`shareable_reviews/service.py:366-367`). The test still sets `creation_modality="llm_generated"` by the same direct blob update.
- **The chat row is only the anchor** for `fk_blobs_created_from_message_session` and the provenance CHECK. `add_message` writes the same role, content and `writer_principal`; the id becomes a UUID and `sequence_no` is allocated. Nothing asserted depends on either.
- **No wrong-reason pass:** `add_message` does not touch the blob or the composition state, and the lease is closed by `async with` before the GET (`__aexit__` awaits `close()`; the close path itself was not read, so release is inferred, and it is the same pattern as the `conftest` state helper).

### Item 3: `b9c84a7a7`

- **No code reference remains.** `git grep _blob_id_uuid_validation_error b9c84a7a7` returns only prose in eight tracked documents; the parent commit additionally returns the definition (positive control). `_BLOB_ID_UUID_ERROR` returns 12 lines in two `src` files (control). `observation_boundary` has no remaining use in `blobs.py`; `canonical_resource_uuid` and `Any` are still used.
- **Document count:** the author says nine documents; I count eight at `b9c84a7a7`. The ninth is presumably the author's own report. Not resolved.
- **Stale links:** three tool sheets under `docs/architecture/composer-tool-assessment/tools/` (`inspect_source.md`, `read_attachment.md`, `update_blob.md`) link to `blobs.py#L518` by the helper's name. They are dated assessments.
- **Line-keyed inventories:** the only one keyed by `blobs.py` line numbers is the approved-xfail Sessions writer inventory (`tests/unit/architecture/test_session_db_mutation_authority.py:5069-5086`, lines 538 and 563), as the author disclosed. `test_tool_result_envelope_gate.py:2123` carries `blobs.py:2084` only as a string prefix and does not read the source.

## Not reviewed

- The author's tier-model corpus, census, fingerprint-baseline and xfail-status numbers for item 3.
- The 33 pre-existing failures in `test_web_landscape_mutation_fencing.py`.
- `tests/unit/composer_mcp/test_standalone_lifecycle.py` and the other tests `dc67bdd2f` added.
- `portable_supervisor.py`, the watchdog classes, and PostgreSQL behaviour of the rewritten test.
