# Session service decomposition

## Target

Make `src/elspeth/web/sessions/service.py` a smaller, clearer composition root while preserving session, proposal, fork, audit, and persistence behavior. This work starts from `fix/composer-system-boundaries-20260927` and merges into `release/0.8.1` after that branch lands. `src/elspeth/web/composer/service.py` is owned by another agent and is outside this change.

## Steps

1. Extract proposal authority helpers, guided proposal replay helpers, and fork custody helpers into cohesive modules. Move their callers and tests to the implementation modules; leave no compatibility reexports.
2. Extract the existing composer and guided mutation capability classes into one module. Keep their exact lifetime and fence checks, with transaction ownership in `SessionServiceImpl`.
3. Retain the service constructor, public API, process wide lock instance, and post commit projection in `service.py`. Do not replace the service with mixins or attach methods dynamically.
4. Reconcile the release tip, review the final integrated diff, and merge only after the required gates finish.

## Proof

- Compare executable ASTs of moved definitions before and after extraction; investigate every difference.
- Run Ruff, mypy, contracts and whole tree authority, attribute, masquerade, and custody gates affected by the moves. Control any inventory scanner with a known positive and an injected unauthorized write.
- Run focused session, guided, composer, and fork tests. Run one frozen full suite and the serial PostgreSQL testcontainer selection for the integrated candidate, subject to host test capacity.
- Capture the trust tier finding set before and after integration. Do not stage signatures.
- Ask Astra to review the plan, each extraction checkpoint, and the integrated result; fix all verified findings before merge.
