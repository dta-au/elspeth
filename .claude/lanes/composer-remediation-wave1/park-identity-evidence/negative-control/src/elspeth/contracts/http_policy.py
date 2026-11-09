"""Closed, composition-selected HTTP evidence and diagnostic policies."""

from enum import Enum
from typing import Literal, get_args


class HTTPAuditPolicy(Enum):
    GENERIC = "generic"
    POWER_AUTOMATE_V1 = "power_automate_v1"


HTTPFailureCode = Literal[
    "transport_failed",
    "deadline_exceeded",
    "response_too_large",
    "invalid_encoding",
    "invalid_json",
    "depth_exceeded",
    "authority_refused",
]
HTTP_FAILURE_CODES: frozenset[str] = frozenset(get_args(HTTPFailureCode))
