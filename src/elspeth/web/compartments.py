"""Compartment identifiers, public marking, and exact input provenance."""

from __future__ import annotations

import hashlib
import re
from typing import Final, TypedDict

COMPARTMENT_ID_PATTERN: Final = r"[a-z0-9][a-z0-9-]{0,62}"
COMPARTMENT_ID_RE: Final = re.compile(rf"{COMPARTMENT_ID_PATTERN}\Z")
COMPARTMENT_MARKING_PREFIX: Final = "# compartment_id: "
_MARKING_LINE_RE: Final = re.compile(rf"(?:# )?compartment_id: ({COMPARTMENT_ID_PATTERN})\Z")


class CompositionIngressRecord(TypedDict):
    text_sha256: str
    foreign_compartment_ids: list[str]


class ChatIngressInput(CompositionIngressRecord):
    message_id: str


def is_compartment_id(value: str) -> bool:
    """Accept only the bounded identifier syntax used in public markings."""
    return COMPARTMENT_ID_RE.fullmatch(value) is not None


def compartment_marking_header(compartment_id: str | None) -> str:
    if compartment_id is None:
        return ""
    if not is_compartment_id(compartment_id):
        raise ValueError(f"compartment_id {compartment_id!r} does not match {COMPARTMENT_ID_PATTERN}")
    return f"{COMPARTMENT_MARKING_PREFIX}{compartment_id}\n"


def compartment_ingress_record(text: str, *, own_compartment_id: str | None) -> CompositionIngressRecord:
    """Record exact input bytes and whole-line foreign markings, without parsing a graph."""
    foreign: set[str] = set()
    for line in text.splitlines():
        match = _MARKING_LINE_RE.fullmatch(line.strip())
        if match is not None and (marking := match.group(1)) != own_compartment_id:
            foreign.add(marking)
    return {
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "foreign_compartment_ids": sorted(foreign),
    }


def chat_ingress_input(message_id: str, text: str, *, own_compartment_id: str | None) -> ChatIngressInput:
    """Bind a durable user chat row to its content-free ingress evidence."""
    return {"message_id": message_id, **compartment_ingress_record(text, own_compartment_id=own_compartment_id)}
