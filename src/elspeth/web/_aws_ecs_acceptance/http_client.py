"""Bounded HTTP transport for AWS ECS acceptance.

The client is provider-neutral and lives in
``elspeth.web._acceptance_common.http_client``; it is re-imported here by
identity so the ECS facade, ``capture`` and the ECS tests keep reaching it
through this module unchanged.
"""

from __future__ import annotations

from elspeth.web._acceptance_common.http_client import AcceptanceHttpClient as AcceptanceHttpClient
