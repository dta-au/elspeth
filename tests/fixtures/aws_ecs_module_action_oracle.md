# AWS ECS module-action oracle

`aws_ecs_module_action_oracle.json` maps every `aws_*` resource type declared
by `deploy/aws-ecs/terraform/modules/scenario` to the minimum IAM actions
required for its configured create, refresh, update, and delete paths to
succeed. The mapping is an offline regression oracle, not a claim that it
discovers undocumented provider behavior or AWS-side dependent permissions.

## Provider provenance

The repository's four tracked Terraform lockfiles pin
`hashicorp/aws` 6.54.0. The reviewed upstream source is:

- repository: <https://github.com/hashicorp/terraform-provider-aws>
- tag: `v6.54.0`
- commit: `8ca5b8dac50747f51e02a122d058f6eebd58ba19`

The JSON fixture records the main upstream lifecycle source file for every
resource type, plus helper files that carry material review evidence. A source
link is formed as:

```text
https://github.com/hashicorp/terraform-provider-aws/blob/<commit>/<provider_source>
```

Generic tag helpers and shared find/delete helpers must also be followed from
that file. The vendored action-vocabulary fixture remains the authority for
whether the resulting IAM action names are real.

## Deterministic review

Refresh the oracle when the pinned AWS provider, a module resource block, or a
configured lifecycle field changes.

1. Confirm all four tracked lockfiles select the version recorded in
   `provider.version`.
2. Check out `provider.tag` from `provider.repository` and require its commit
   to equal `provider.commit`.
3. Derive the exact `aws_*` resource-type census from every module `.tf` file.
   Every type must have one `provider_sources` entry and one `resources` entry.
4. For each concrete resource block, read its configured fields and follow the
   provider source through create, refresh/read, in-place update, delete,
   generic tag reconciliation, and helper calls. Include an action when the
   current configuration selects a required path or the path is an ordinary
   update/delete of a configured field. Exclude denial-tolerant probes and
   recovery calls whose errors do not block the lifecycle, and record that
   exclusion honestly. Do not import actions from a similarly named data
   source or another resource.
5. Translate SDK operation names to IAM action names using the official AWS
   Service Authorization Reference snapshot. Do not assume they are identical:
   S3 has operations whose authorization action is a different name.
6. Attribute each resource to its actual provider alias. Required actions must
   match an Allow in that alias's policy set and must not match an explicit
   Deny. This action-name check does not solve IAM resource/condition semantics;
   add an exact focused assertion for each known condition-context trap.
   An `expected_denies` entry has an exact provider alias, action list,
   rationale, and live Filigree issue. It records configured provider paths
   that the security design intentionally refuses, plus related actions that
   must remain denied even when the provider no longer needs them. The test
   requires every listed action to remain ungranted and explicitly denied.
7. Sort and deduplicate every action list, run the focused oracle tests, and
   review the resulting missing-grant report before changing a policy.

Environment-contingent provider recovery branches are excluded unless the
module configuration selects them. The known EFS-created ENI question remains
contingent on the least-privilege verification apply tracked by
`elspeth-acc2ce713b`; it is not inferred or pre-granted by this fixture.
