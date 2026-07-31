"""Opaque lifetime registry for private session-operation connections."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from threading import RLock, get_ident

from sqlalchemy import Connection


@dataclass(frozen=True, slots=True)
class _MutationConnectionEntry:
    connection: Connection
    owner_thread_id: int


_MUTATION_CONNECTION_REGISTRY: dict[str, _MutationConnectionEntry] = {}
_MUTATION_CONNECTION_REGISTRY_LOCK = RLock()


def _register_mutation_connection(connection: Connection) -> str:
    with _MUTATION_CONNECTION_REGISTRY_LOCK:
        while True:
            token = secrets.token_urlsafe(32)
            if token not in _MUTATION_CONNECTION_REGISTRY:
                _MUTATION_CONNECTION_REGISTRY[token] = _MutationConnectionEntry(
                    connection=connection,
                    owner_thread_id=get_ident(),
                )
                return token


def _resolve_mutation_connection(token: str) -> Connection:
    with _MUTATION_CONNECTION_REGISTRY_LOCK:
        entry = _MUTATION_CONNECTION_REGISTRY.get(token)
    if entry is None:
        raise RuntimeError("session operation mutation transaction is closed")
    if entry.owner_thread_id != get_ident():
        raise RuntimeError("session operation mutation used outside its owning callback thread")
    return entry.connection


def _unregister_mutation_connection(token: str) -> None:
    with _MUTATION_CONNECTION_REGISTRY_LOCK:
        _MUTATION_CONNECTION_REGISTRY.pop(token, None)
