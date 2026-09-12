# Hidden response label disposition

`_redacted_response_field_N` labels retain associations between hidden keys
and their projected child shapes within one mapping. They are not source
identifiers. Numbering starts at 1 in each mapping and follows the order in
which hidden keys reach the response redactor. A recognized structural key
keeps its name without consuming a number. Swapping two hidden entries swaps
the child shapes attached to their labels. Nested mappings restart numbering.

The retained information is container shape, local ordering, recognized
structural vocabulary, and permitted public scalar facts. Arbitrary source
keys and scalar text must not survive in message content or its audit envelope.
Labels do not encode key text or provide a reversible lookup, and consumers
must not correlate the same label across mappings, responses, or persistence
paths as though it identified the same source field.

The legacy audit path canonicalizes the original invocation result before
`audit_storage` decodes and redacts it. Canonicalization sorts mapping keys;
its local order can therefore differ from the producer's insertion order.
Direct response redaction follows producer order, while this persistence path
follows the canonical input order. Both preserve associations at their own
boundary. Exact label identities across those paths are not promised.

`tests/unit/web/composer/test_hidden_response_label_persistence.py` exercises
a real `upsert_node` plugin-options rejection with a nominal catalog schema
fixture. The producer's `plugin_schemas` payload contains source-key and
value canaries plus distinct valid schema child shapes. Four combinations
cover insertion order, swapped order, and a recognized structural key inserted
between hidden entries. Assertions cover nested numbering restart, complete
child-shape association, and absence of every canary after redaction.

The same cases execute `dispatch_with_audit`, project through
`redacted_tool_invocation_content_and_envelope`, and call the actual
`_persist_tool_invocations` adapter with the existing capturing session-service
double. They check persisted content and envelopes and the redacted result
hash. This measures the persistence projection and writer inputs; it does not
claim a database round trip or coverage of the separate `persist_turn_audit`
cohort path.
