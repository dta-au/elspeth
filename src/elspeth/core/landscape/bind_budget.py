"""The one bound-parameter budget for Landscape statements over a runtime collection.

A statement whose bind count grows with a collection (an ``IN`` list of token
ids, a per-row ``CASE`` map, a multi-row ``VALUES``) fails once the collection
is large enough: SQLite refuses a statement above SQLITE_MAX_VARIABLE_NUMBER
(historical default 999, 32766 since 3.32) and PostgreSQL's extended protocol
carries a 16-bit bind count (65535). The collections that reach Landscape
statements are data-sized — every token one sink write delivers, every member
of an aggregation batch (batch_stats has no row cap), every child a transform
emits — so no statement may bind one.

Three shapes keep a statement's bind count fixed, chosen per statement:

- a MUTATION over many rows is one ``executemany`` of a fixed-size statement
  (the Landscape mutation-fencing gate forbids a data-changing statement
  inside a loop, and executemany keeps it one execution on one connection);
- a READ over a set the database already holds (an aggregation batch's
  ``batch_members``) selects it with a subquery, joined once;
- a READ over a caller-supplied collection runs in ascending chunks from
  :func:`bind_budget_chunks`, all on the caller's one connection and
  transaction, so chunking changes no atomicity; ascending order keeps a
  locking read in the global primary-key lock order.
"""

from collections.abc import Iterator, Sequence
from typing import Final

# Below SQLite's historical 999 default with headroom for a statement's fixed
# predicates, so a chunk fits every supported SQLite build and PostgreSQL.
BIND_BUDGET_PER_STATEMENT: Final = 900


def bind_budget_chunks[ItemT](items: Sequence[ItemT], *, binds_per_item: int = 1) -> Iterator[Sequence[ItemT]]:
    """Split ``items`` in order so ``binds_per_item`` binds per item fit one statement's budget."""
    size = BIND_BUDGET_PER_STATEMENT // binds_per_item
    for offset in range(0, len(items), size):
        yield items[offset : offset + size]
