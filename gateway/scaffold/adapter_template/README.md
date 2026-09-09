# `yourorg_adapter` — new adapter scaffold

This directory is a **template**, not a package the gateway ships or
imports. It exists to be copied out of this repository into your own
project and renamed. Nothing under `gateway/scaffold/` is on the gateway's
import path, is packaged into the gateway wheel, or runs in the gateway's
own test suite.

## Using this scaffold

1. Copy this whole `adapter_template` directory to a new location outside
   the ELSPETH repository — this is your agency's own adapter project.
2. Rename every occurrence of `yourorg_adapter` (the directory
   `src/yourorg_adapter`, its imports, the `pyproject.toml`
   `[project.entry-points."elspeth_llm_gateway.adapters"]` table, and the
   `ADAPTER_NAME` constant in `descriptor.py`) to your own package name.
   `ADAPTER_NAME` must match `^[a-z][a-z0-9_]{2,63}$`.
3. Rename `tests/test_adapter.py.template` to `tests/test_adapter.py` (the
   `.template` suffix exists only so this skeleton is inert inside the
   gateway's own pytest run — see the module docstring in that file).
4. Pin a real `elspeth-llm-gateway` version in `pyproject.toml`.
5. Work through the modules in this order, replacing every
   `# TRANSLATION POINT` and every `NotImplementedError`:
   - `descriptor.py` — your adapter's name, version, and declared
     `Capability` set (only what your upstream genuinely supports).
   - `config.py` — any deployment-specific configuration options, **and**
     `validate_model_target`, which `/readyz` calls once per configured
     model mapping. This one is not optional in practice: leave it
     unimplemented and a deployment mapped to a target your adapter cannot
     read passes readiness and then fails every completion. The conformance
     kit checks for it, so a derived image without it does not qualify.
   - `request.py` — `CanonicalRequest` → your upstream's real invoke shape.
   - `response.py` — your upstream's real success body → `CanonicalResponse`.
   - `errors.py` — your upstream's real failure body →
     `ErrorClassification` (from the closed `CLASSIFIABLE_CODES` vocabulary).
   - `adapter.py` — wires the five modules above together; this is what the
     `elspeth_llm_gateway.adapters` entry point resolves.
6. Read `elspeth_llm_gateway.reference.adapter` (the fictional
   `reference_v1_invoke` adapter shipped with the gateway) alongside each
   module here — it is the worked example every `# TRANSLATION POINT` in
   this scaffold is deliberately structured to mirror.

See `gateway/README.md`'s "Building your own adapter" section for the full
six-step onboarding path (reference stack → scaffold → sanitized fixtures →
derived image → conformance-against-image → publish by digest), including
how to pin the built derived image FROM this gateway's digest and run the
conformance kit against it once it exists.
