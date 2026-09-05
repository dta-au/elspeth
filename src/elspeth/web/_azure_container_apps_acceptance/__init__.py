"""Azure Container Apps acceptance: the thin provider binding of ``_acceptance_common``.

Everything provider-neutral (the receipt envelope validator, the forbidden-key
visitor, the ``schema_facts`` derivation, the compatibility gate predicate, the
replica-probe decision tables, the ``testcontainer-run`` gate) lives in
``elspeth.web._acceptance_common`` and is imported here unchanged. This package
binds it to the platform: the ``replica_binding_sha256`` subject, the twelve
Container Apps check kinds with their closed detail sets and ``mechanism``
enum, the ``ReplicaController`` port over role revocation and revision
deactivation, and the projections of ``az`` / KQL / Resource Graph JSON onto
those detail sets. It never imports ``_aws_ecs_acceptance`` or either facade.

Modules (layered, lowest first):

- ``receipt_contracts``: replica binding, detail types, kind validators, the
  provider descriptor, the compatibility record, exec-receipt encode/extract.
- ``evidence``: Tier-3 projections of platform JSON onto the detail types, the
  receipt store and the bundle check.
- ``controller``: the platform ``ReplicaController`` (partition by role
  revocation, grace-0 deactivate, label-URL addressing).

The two-methodology situation and the v3 consolidation trigger are recorded
in ``README.md`` beside this file.
"""
