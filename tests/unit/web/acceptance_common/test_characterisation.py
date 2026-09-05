"""Characterisation corpus for the ``_acceptance_common`` extraction (6b-4, plan §9.2 item 1).

``corpus/ecs_receipts.json`` was generated ONCE from the pre-extraction tree
(``generated_from`` names the commit) and is never regenerated: one accepted
stored receipt per ECS ``_RECEIPT_KINDS`` member (both compatibility-record
scenarios), the exec-receipt sentinel each exec kind encodes to, the two
compatibility records the gate reads, and the schema facts, each as byte-exact
canonical JSON with its sha256 pinned. These tests replay every entry through
the validators as they stand now and demand the same bytes back.

Two of the pins depend on the live schema epochs (the compatibility receipts
and the schema facts). While the live epochs equal the recorded ones the bytes
must match exactly; once a schema bump moves them the corpus entry must be
REFUSED (``receipt_store_binding``) — the property the structural-changes label
exists to guarantee — and the epoch-independent entries must still match.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH
from elspeth.web import aws_ecs_acceptance as acceptance
from elspeth.web._acceptance_common import receipt_validation, schema_facts
from elspeth.web._acceptance_common.errors import AcceptanceCheckError
from elspeth.web._aws_ecs_acceptance import receipt_contracts
from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH

CORPUS_PATH = Path(__file__).parent / "corpus" / "ecs_receipts.json"
CORPUS = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
EPOCHS_UNCHANGED = CORPUS["live_epochs_at_generation"] == {"session_epoch": SESSION_SCHEMA_EPOCH, "landscape_epoch": SQLITE_SCHEMA_EPOCH}
EPOCH_DEPENDENT = frozenset({"compatibility-record:A", "compatibility-record:B"})


def _canonical(document: object) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_corpus_is_the_pre_extraction_tree_and_self_consistent() -> None:
    assert CORPUS["generated_from"] == "02d10e0c1530cbf8aea4d5c76f0e5b30f147efb2"
    assert set(CORPUS["receipt_kinds"]) == set(receipt_contracts._RECEIPT_KINDS)
    assert {key.split(":")[0] for key in CORPUS["stored_receipts"]} == set(receipt_contracts._RECEIPT_KINDS)
    for key, entry in CORPUS["stored_receipts"].items():
        assert _canonical(entry["document"]) == entry["canonical"], key
        assert _sha256(entry["canonical"]) == entry["sha256"], key


@pytest.mark.parametrize("key", sorted(CORPUS["stored_receipts"]))
def test_stored_receipt_bytes_are_identical_after_extraction(key: str) -> None:
    entry = CORPUS["stored_receipts"][key]
    payload = json.loads(entry["canonical"])
    if key in EPOCH_DEPENDENT and not EPOCHS_UNCHANGED:
        with pytest.raises(AcceptanceCheckError, match="receipt_store_binding"):
            receipt_contracts._validate_stored_receipt(payload, **entry["validate_kwargs"])
        return
    accepted = receipt_contracts._validate_stored_receipt(payload, **entry["validate_kwargs"])
    assert accepted is payload
    assert _canonical(accepted) == entry["canonical"]
    assert _sha256(_canonical(accepted)) == entry["sha256"]


@pytest.mark.parametrize("key", sorted(key for key, entry in CORPUS["stored_receipts"].items() if "sentinel" in entry))
def test_exec_receipt_sentinels_encode_and_extract_to_the_same_bytes(key: str) -> None:
    entry = CORPUS["stored_receipts"][key]
    env = {
        "ELSPETH_ACCEPTANCE_CANDIDATE_SHA": "c" * 40,
        "ELSPETH_ACCEPTANCE_TASK_ARN": "arn:aws:ecs:ap-southeast-2:123456789012:task/cluster/private-task-id",
        "ELSPETH_ACCEPTANCE_SCENARIO_ID": "scenario-a",
    }
    document = entry["document"]
    assert acceptance.encode_exec_receipt(document["check"], document["details"], env) == entry["sentinel"]
    extracted = acceptance.extract_exec_receipt(
        entry["sentinel"],
        expected_candidate_sha=env["ELSPETH_ACCEPTANCE_CANDIDATE_SHA"],
        expected_task_arn=env["ELSPETH_ACCEPTANCE_TASK_ARN"],
        expected_scenario_id=env["ELSPETH_ACCEPTANCE_SCENARIO_ID"],
        expected_check=document["check"],
    )
    assert _canonical(extracted) == entry["canonical"]


@pytest.mark.parametrize("scenario_id", ["A", "B"])
def test_schema_facts_have_one_source(scenario_id: str) -> None:
    """The ECS module, the shared module and the corpus agree byte-for-byte (while the epochs stand)."""
    live = _canonical(schema_facts._expected_schema_facts(scenario_id))
    assert _canonical(receipt_contracts._expected_schema_facts(scenario_id)) == live
    assert receipt_contracts._expected_schema_facts is schema_facts._expected_schema_facts
    recorded = CORPUS["expected_schema_facts"][scenario_id]
    if EPOCHS_UNCHANGED:
        assert live == recorded["canonical"]
        assert _sha256(live) == recorded["sha256"]
    else:
        assert live != recorded["canonical"]
    assert receipt_contracts._SCENARIO_B_STRUCTURAL_CHANGES is schema_facts._SCENARIO_B_STRUCTURAL_CHANGES
    if EPOCHS_UNCHANGED:
        assert CORPUS["scenario_b_structural_changes"] == schema_facts._SCENARIO_B_STRUCTURAL_CHANGES


def test_ecs_module_reaches_the_shared_validators_by_identity() -> None:
    assert receipt_contracts._visit_receipt_value is receipt_validation._visit_receipt_value
    assert receipt_contracts._validate_bounded_receipt_document is receipt_validation._validate_bounded_receipt_document
    assert receipt_contracts._receipt_number is receipt_validation._receipt_number
    assert receipt_contracts._FORBIDDEN_RECEIPT_KEYS is receipt_validation._FORBIDDEN_RECEIPT_KEYS
    assert receipt_contracts._ECS_EXEC_RECEIPT_DESCRIPTOR.subject_field == "task_arn_sha256"
    assert receipt_contracts._ECS_EXEC_RECEIPT_DESCRIPTOR.check_kinds == {
        "verify-s3",
        "verify-bedrock",
        "verify-textract",
        "verify-bedrock-guardrails",
        "verify-connection-budget",
        "verify-operator-telemetry",
    }
