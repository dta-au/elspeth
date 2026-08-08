"""Clean fixture: reflection over an owned dataclass table, classified in the baseline.

Not a structural amnesty — a dynamic-name getattr driven by
``dataclasses.fields(obj)`` is not one of the gate's three permanent
recognizers, so this site fires unless adjudicated. It is fully covered by
this fixture's own ``config/cicd/masquerade_baseline.yaml`` with
``classification: reflection-owned-table``: the class definition being
serialized IS the table of allowed names, so no externally-controlled
string can reach this call.
"""

from __future__ import annotations

import dataclasses


def to_dict(obj: object) -> dict[str, object]:
    return {field.name: getattr(obj, field.name) for field in dataclasses.fields(obj)}
