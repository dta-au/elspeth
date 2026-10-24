import pytest

from elspeth.contracts.composer_audit import ToolArgumentErrorCategory
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.anti_anchor import AntiAnchorSnapshot, AntiAnchorTracker
from elspeth.web.composer.schema_disclosure import (
    SchemaDisclosureSnapshot,
    SchemaDisclosureTracker,
)


def test_anti_anchor_cold_snapshot_keeps_order_categories_and_consumed_state():
    owner = AntiAnchorTracker()
    for digest in ("one", "two", "three"):
        owner.record_failure("set_pipeline", digest, category=ToolArgumentErrorCategory.SEMANTIC_RULE)
    expected = owner.next_hint()
    snapshot = owner.snapshot_for_continuation()
    cold = AntiAnchorSnapshot.model_validate_json(snapshot.model_dump_json())
    restored = AntiAnchorTracker.for_continuation(cold)
    assert restored is not owner
    assert restored.snapshot_for_continuation() == snapshot
    assert restored.next_hint() == expected
    assert expected is not None and "different arguments" in expected
    restored.consume_fire()
    drained = AntiAnchorTracker.for_continuation(restored.snapshot_for_continuation())
    assert drained.next_hint() is None
    assert owner.next_hint() == expected


def test_disclosure_cold_snapshot_keeps_current_session_and_refuses_changed_catalog():
    owner = SchemaDisclosureTracker()
    owner.mark_plugin_schema_loaded("session", "source", "csv")
    owner.mark_plugin_schema_loaded("other", "sink", "json")
    snapshot = owner.snapshot_for_continuation(
        session_id="session",
        catalog_hash="catalog",
        policy_hash="policy",
    )
    cold = SchemaDisclosureSnapshot.model_validate_json(snapshot.model_dump_json())
    restored = SchemaDisclosureTracker()
    restored.restore_session_for_continuation(
        cold,
        session_id="session",
        catalog_hash="catalog",
        policy_hash="policy",
    )
    assert restored.schemas_loaded_for_session("session") == frozenset({("source", "csv")})
    assert not restored.schemas_loaded_for_session("other")
    with pytest.raises(AuditIntegrityError, match="acquire current schemas"):
        restored.restore_session_for_continuation(
            cold,
            session_id="session",
            catalog_hash="new-catalog",
            policy_hash="policy",
        )
    assert restored.schemas_loaded_for_session("session") == frozenset({("source", "csv")})
