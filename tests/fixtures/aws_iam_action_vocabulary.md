# AWS IAM action-vocabulary fixture

`aws_iam_action_vocabulary_2026-08-09.jsonl` is a bounded snapshot of the
official AWS Service Authorization Reference. It contains the complete action
name list for each service prefix used by the policy templates in
`deploy/aws-ecs/terraform/iam` on the capture date. It is test data, not live
acceptance evidence and not a claim about action/resource/condition semantics.

Source provenance:

- catalogue documentation:
  <https://docs.aws.amazon.com/service-authorization/latest/reference/service-reference.html>
- catalogue index:
  <https://servicereference.us-east-1.amazonaws.com/>
- per-service source: the `source_url` in each JSONL service record
- captured: 2026-08-09

## Deterministic refresh

Refresh only when an AWS action newly used by a template is absent from the
snapshot or as an explicit maintenance change. Do not fetch this data in the
test suite.

1. Set `capture_date` to the UTC date of the refresh.
2. Derive the closed service-prefix set from every quoted IAM action in all
   `deploy/aws-ecs/terraform/iam/*.json.tftpl` files.
3. Retrieve the AWS catalogue index and require exactly one source URL for
   every derived prefix.
4. For each prefix in lexical order, retrieve that URL and emit one compact
   JSON object with keys `service`, `source_url`, `version`, and `actions`.
   Sort and deduplicate `actions` lexically. Preserve the header as the first
   line, changing `captured_on`, `catalogue_schema_version`, and any
   deliberate dated escapes to match the refreshed records.
5. Run the focused oracle test. It checks that the fixture and policy service
   sets are identical, source URLs have the documented canonical shape,
   actions are sorted and unique, and every template literal/wildcard resolves.

The canonical service-record transformation is:

```bash
service_name=s3
source_url="https://servicereference.us-east-1.amazonaws.com/v1/${service_name}/${service_name}.json"
curl --fail --silent --show-error "${source_url}" |
  jq -c --arg service "${service_name}" --arg source_url "${source_url}" \
    '{service:$service,source_url:$source_url,version:.Version,actions:[.Actions[].Name] | sort | unique}'
```

Repeat that transformation in lexical prefix order. A refresh is incomplete
unless the focused test passes with no escape for an already-published action.

## Dated escapes

AWS can publish a new action before a scheduled snapshot refresh. The header's
`escapes` array is the only temporary bridge. Every entry must be an exact
literal action and contain:

- `action`
- `issue` (`elspeth-` plus the ten-character issue suffix)
- `added_on`
- `review_by`
- `rationale`

Wildcards cannot be escaped. Expired, unused, untracked, or rationale-free
escapes fail the test. Prefer refreshing the official snapshot; never use an
escape for a typo or an action absent from the live policy templates.
