"""Provider-neutral core shared by the deployment acceptance harnesses.

Extracted from ``elspeth.web._aws_ecs_acceptance`` (Phase 6b, ticket
elspeth-cb993235e4) by move-and-re-import: the AWS ECS package re-imports every
moved name by identity, its receipts are byte-identical before and after the
move, and its tests are unedited. A second provider (Azure Container Apps)
binds the same derivation, validator and gate predicate to its own receipt
ids; it never imports the ECS package, and the ECS package never imports it.

Modules:

- ``errors``: the closed error-code and step vocabularies, the step contextvar,
  and the four ``Acceptance*Error`` classes every facade projects.
- ``identity``: bounded non-content identity (``SanitizedResourceIdentity``)
  with a closed cloud-provider set.
- ``schema_facts``: the ONE derivation of the release/schema facts a
  compatibility record attests (``_expected_schema_facts``).
- ``receipt_validation``: the bounded receipt-document admission, the shared
  connection-budget validator, and the exec-receipt envelope generalised by a
  provider descriptor.
- ``secure_documents``: the protected-document read every receipt and record
  passes through.
- ``http_client``: the bounded same-origin acceptance HTTP client and its
  credentials.
- ``compatibility_gate``: the runbook's rollback-refusal jq predicate as code.
- ``replica_probes``: the replicas > 1 probe driver, its ``ReplicaController``
  port, and the closed ``mechanism`` vocabulary a probe result may claim.
- ``testcontainer_run``: the ``testcontainer-run`` receipt — the pinned CI
  selection, the junit reader, one validator and the two provider schema ids.
"""
