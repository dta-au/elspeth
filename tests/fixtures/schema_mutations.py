"""Copy declared schemas without losing dialect guards or audit triggers."""

from sqlalchemy import CheckConstraint, MetaData, event
from sqlalchemy.dialects import sqlite


def copy_metadata_for_mutation(metadata: MetaData) -> MetaData:
    copied = MetaData()
    for table in metadata.tables.values():
        duplicate = table.to_metadata(copied)
        for listener in table.dispatch.after_create:
            event.listen(duplicate, "after_create", listener)
        # SQLAlchemy's table copy does not retain conditional DDL guards.
        # Preserve the actual dialect choice before mutating one contract.
        for original in table.constraints:
            if isinstance(original, CheckConstraint):
                matches = [
                    candidate
                    for candidate in duplicate.constraints
                    if isinstance(candidate, CheckConstraint)
                    and candidate.name == original.name
                    and str(candidate.sqltext.compile(dialect=sqlite.dialect())) == str(original.sqltext.compile(dialect=sqlite.dialect()))
                ]
                assert len(matches) == 1
                matches[0]._ddl_if = original._ddl_if
    return copied
