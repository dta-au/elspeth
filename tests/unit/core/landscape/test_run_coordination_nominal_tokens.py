"""Structural token impostors cannot enter coordination transactions."""

from dataclasses import dataclass
from typing import Any

import pytest

from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction, fenced_member_transaction
from tests.fixtures.landscape import make_landscape_db


@dataclass(frozen=True)
class _TokenImpostor:
    run_id: str = "unregistered"
    worker_id: str = "unregistered"
    leader_epoch: int = 1


@pytest.mark.parametrize("kind", ["member", "leader"])
def test_fence_rejects_structural_token_before_database_access(kind: str) -> None:
    db = make_landscape_db()
    token: Any = _TokenImpostor()
    try:
        with pytest.raises(TypeError, match=r"requires a .*Token"):
            if kind == "member":
                with fenced_member_transaction(db.engine, member_token=token, verb="test"):
                    pytest.fail("structural member token entered payload")
            else:
                with fenced_leader_transaction(db.engine, token=token, window_seconds=80, verb="test"):
                    pytest.fail("structural leader token entered payload")
    finally:
        db.close()
