"""Complete owned text receives the same detector, over its actual full extent."""

import pytest

from elspeth.contracts.credential_material import DEFAULT_CREDENTIAL_TRAVERSAL_POLICY
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.advisor_content import PreparedAdvisorContent, scan_prepared_advisor_content
from elspeth.web.credential_guard import CredentialMaterialRefused, require_no_credential_material


def test_large_bound_utf8_body_is_scanned_whole_without_modifying_the_wire():
    text = "é" * 180_000 + "VISIBLE_TAIL_CANARY"
    messages = [{"role": "user", "content": text}]
    with pytest.raises(CredentialMaterialRefused):
        require_no_credential_material(messages, surface="unowned_control")
    residual = scan_prepared_advisor_content(messages, (PreparedAdvisorContent(0, text),), role="advisor")
    require_no_credential_material(residual, surface="residual_control")
    assert messages[0]["content"] == text


@pytest.mark.parametrize("position", [7_990, 262_135, 300_000])
def test_complete_scan_refuses_secrets_at_part_seams_and_beyond_default_boundary(position):
    secret = "sk-" + "A1b2C3d4" * 6
    text = "x" * position + " " + secret + " tail"
    with pytest.raises(CredentialMaterialRefused):
        scan_prepared_advisor_content([{"role": "user", "content": text}], (PreparedAdvisorContent(0, text),), role="advisor")


@pytest.mark.parametrize("case", ["forged", "overlap", "changed", "position", "wrong_role"])
def test_witness_cannot_be_forged_rebound_or_repurposed(case):
    messages = [{"role": "user", "content": "Actual selected content"}]
    witness = PreparedAdvisorContent(0, messages[0]["content"])
    witnesses = (witness,)
    if case == "forged":
        witnesses = ({"message_index": 0, "content": messages[0]["content"]},)
    elif case == "overlap":
        witnesses = (witness, witness)
    elif case == "changed":
        messages[0]["content"] = "Changed after capture"
    elif case == "position":
        witnesses = (PreparedAdvisorContent(1, messages[0]["content"]),)
    with pytest.raises(AuditIntegrityError):
        scan_prepared_advisor_content(messages, witnesses, role="composer" if case == "wrong_role" else "advisor")


@pytest.mark.parametrize("case", ["other_body", "nested_credential", "cycle"])
def test_only_the_bound_scalar_changes_traversal_extent(case):
    messages = [{"role": "user", "content": "Bound body"}]
    if case == "other_body":
        messages.append({"role": "user", "content": "x" * 300_000})
    elif case == "nested_credential":
        messages[0]["other"] = {"api_key": "secret-value"}
    else:
        cycle = []
        cycle.append(cycle)
        messages[0]["other"] = cycle
    residual = scan_prepared_advisor_content(messages, (PreparedAdvisorContent(0, "Bound body"),), role="advisor")
    with pytest.raises(CredentialMaterialRefused):
        require_no_credential_material(residual, surface="ordinary_remaining_tree")


@pytest.mark.parametrize("case", ["depth", "width", "nodes", "nested_tool_arguments"])
def test_full_body_witness_preserves_each_residual_resource_limit(case):
    policy = DEFAULT_CREDENTIAL_TRAVERSAL_POLICY
    messages = [{"role": "user", "content": "Bound body"}]
    if case == "depth":
        value = "leaf"
        for _ in range(policy.max_depth + 1):
            value = {"child": value}
    elif case == "width":
        value = ["leaf"] * (policy.max_width + 1)
    elif case == "nodes":
        breadth = min(64, policy.max_width)
        value = [["leaf" for _ in range(breadth)] for _ in range(policy.max_nodes // breadth + 1)]
        assert len(value) < policy.max_width
    else:
        value = {
            "tool_calls": [
                {
                    "id": "unowned",
                    "type": "function",
                    "function": {
                        "name": "some_tool",
                        "arguments": "x" * (policy.max_string_bytes + 1),
                    },
                }
            ]
        }
    messages[0]["other"] = value
    residual = scan_prepared_advisor_content(messages, (PreparedAdvisorContent(0, "Bound body"),), role="advisor")
    with pytest.raises(CredentialMaterialRefused):
        require_no_credential_material(residual, surface="residual_resource_negative")


def test_actual_message_role_mutation_and_invalid_utf8_fail_before_dispatch():
    content = "Selected content"
    with pytest.raises(AuditIntegrityError):
        scan_prepared_advisor_content([{"role": "assistant", "content": content}], (PreparedAdvisorContent(0, content),), role="advisor")
    invalid = "invalid Unicode scalar: \ud800"
    with pytest.raises(UnicodeEncodeError):
        scan_prepared_advisor_content([{"role": "user", "content": invalid}], (PreparedAdvisorContent(0, invalid),), role="advisor")
