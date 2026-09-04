"""The ``sso_handoffs`` store: a browser-to-backend handoff, used once.

The SSO callback answers a top-level GET, so it cannot hand the browser a
session token — anything in that URL reaches the load balancer's logs,
uvicorn's logs and the browser's history. It returns a HANDOFF CODE in the URL
fragment instead, and ``complete`` trades that code for a token over POST.

That makes the code a bearer credential for exactly one exchange. This module
is the half of that promise the database keeps.

WHAT MAKES SINGLE USE TRUE
--------------------------
The claim is ONE statement: a conditional ``UPDATE ... RETURNING`` whose
``WHERE`` carries both the unused test and the expiry test. The row is claimed
by the same statement that decides it may be claimed, so two concurrent
``complete`` calls on one code cannot both win — the second finds
``consumed_at`` already set and returns no row. A ``SELECT`` followed by an
``UPDATE`` would lose exactly that race, and a double-submitted form or a
retried request makes the race ordinary rather than exotic.

The expiry bound in that statement comes from :func:`database_now` — the
DATABASE's clock, read in the same transaction. Reading it is a separate
statement, which is not a hole: it reads the clock, not the row being claimed,
so the claim stays a single atomic statement. Judging expiry on the REPLICA's
clock is the thing being avoided, because a single-use code would then be
single-use only as far as clock drift between replicas allows.

A DATABASE READ MUST NOT YIELD A CREDENTIAL
-------------------------------------------
Only ``sha256(code)`` is stored, never the code. A backup, a replica, or a row
accidentally logged gives a reader nothing redeemable. The code's own 256 bits
of entropy are why a bare hash suffices: there is no dictionary to attack, so
no salt or stretching would add anything.

WHY THE CALLER LEARNS NOTHING
-----------------------------
:meth:`consume` returns ``None`` for unknown, already-consumed and expired
alike. Distinguishing them would tell an attacker whether a guessed code ever
existed, which is the one bit a guessing attack most wants.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, insert, update
from sqlalchemy.engine import Engine

from elspeth.web.auth.sso import HANDOFF_TTL_SECONDS
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.models import sso_handoffs_table


class SsoHandoffRepository:
    """The ``HandoffStore`` implementation backed by the sessions database.

    Holds an ``Engine`` rather than taking a ``Connection`` per call because
    each operation is one short transaction that must commit on its own: the
    handoff is written after the identity is verified but before the browser
    is redirected, and consumed in a later request that shares nothing with
    it.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def issue(self, *, code_hash: str, identity_id: str, request_id: str) -> None:
        """Record a handoff, after the identity behind it has been verified.

        ``code_hash`` is trusted to be a lowercase SHA-256 digest only because
        the table's CHECK constraint enforces it. A caller that passes
        anything else gets an ``IntegrityError``, not a stored row — the guard
        is the database's, so it holds for every writer rather than for the
        ones that remembered to validate.
        """
        with self._engine.begin() as conn:
            now = database_now(conn)
            # Lazy purge, this identity only: a person logging in again is the
            # moment their own abandoned handoffs stop being interesting.
            # There is no background task and this needs no audit row — the
            # login attempt's own record already exists in the auth trail, and
            # deleting an expired single-use code is not an authority mutation.
            conn.execute(
                delete(sso_handoffs_table).where(
                    sso_handoffs_table.c.identity_id == identity_id,
                    sso_handoffs_table.c.expires_at <= now,
                )
            )
            conn.execute(
                insert(sso_handoffs_table).values(
                    code_hash=code_hash,
                    identity_id=identity_id,
                    issued_at=now,
                    expires_at=now + timedelta(seconds=HANDOFF_TTL_SECONDS),
                    request_id=request_id,
                )
            )

    def consume(self, *, code_hash: str) -> str | None:
        """Atomically claim a handoff, returning its ``identity_id`` or ``None``.

        ``None`` covers unknown, already-consumed and expired without
        distinction; see the module docstring for why that is deliberate.
        """
        with self._engine.begin() as conn:
            now = database_now(conn)
            claimed = conn.execute(
                update(sso_handoffs_table)
                .where(
                    sso_handoffs_table.c.code_hash == code_hash,
                    sso_handoffs_table.c.consumed_at.is_(None),
                    sso_handoffs_table.c.expires_at > now,
                )
                .values(consumed_at=now)
                .returning(sso_handoffs_table.c.identity_id)
            ).scalar_one_or_none()
            # Purge AFTER the claim, never before: a delete that ran first
            # would be doing the claim's expiry check in a second statement,
            # which is the race this class exists to avoid.
            conn.execute(delete(sso_handoffs_table).where(sso_handoffs_table.c.expires_at <= now))
            if claimed is None:
                return None
            if type(claimed) is not str:
                # The column is NOT NULL VARCHAR and the FK points at
                # identities.identity_id, so a non-str here is corruption
                # rather than an input to coerce.
                raise RuntimeError("sso_handoffs.identity_id returned a non-string value")
            return claimed
