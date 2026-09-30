# Power Automate source and sink

This example reads a bounded snapshot through an HTTP-triggered flow and
publishes `record_id` and `result` through a second flow. Each sink member uses
an engine delivery ID and a durable status/write protocol. Source schema
failures and proven target rejections route to the JSON quarantine sink.
Transport uncertainty stops publication pending reconciliation.

The shared quarantine sink uses `mode: append` so source validation failures
and sink business rejections retain their separate delivery groups in the
same JSONL file. Use a fresh output location for each independent run.

The [reference guide](../../docs/reference/power-automate.md) describes flow
construction, authentication, destination policy and recovery. The schemas in
[contracts/](contracts/) describe the integration protocol; they are not an
importable flow package.

## Run without credentials

From the repository root, with the development environment installed:

```bash
PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" .venv/bin/python -m pytest \
  tests/integration/plugins/test_power_automate_pipeline.py \
  tests/integration/pipeline/test_power_automate_effect_recovery.py -n 0
```

These checks use the durable flow emulator in
[tests/fixtures/power_automate.py](../../tests/fixtures/power_automate.py).
Controlled DNS and HTTP interception prevent external requests. Synthetic
URLs, credentials, target records and Landscape databases stay in temporary
test storage. The pipeline checks exercise real configuration, plugins,
graph construction and execution; the recovery checks inspect durable target
effects and original delivery identities after interrupted publication.

## Configuration

[settings.yaml](settings.yaml) uses the placeholder origin
`https://flows.example.test`. Its secret references are
`POWER_AUTOMATE_READ_TRIGGER_URL` and `POWER_AUTOMATE_WRITE_TRIGGER_URL`.
Each reference must resolve to the entire current saved designer URL,
including its signature. Configure `ELSPETH_FINGERPRINT_KEY` through the
existing secret facility before constructing a secret-bearing live plugin.
Keep these values outside version control.

Replace the origin and secret references for your deployment. Configure a
retained `snapshot_id` when repeatable source verification is required; its
omission asks the flow to choose a fresh snapshot. `snapshot_for_resume: true`
stores the finite source before the first sink effect, within the engine's
single-source 64-MiB limit. The remote target must retain complete immutable
delivery outcomes throughout the supported recovery window.

The Web Composer requires operator approval of this exact destination origin
and separate authorization for each secret's plugin option path. The
placeholder configuration grants neither permission.
