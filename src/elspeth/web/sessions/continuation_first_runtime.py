"""One actual first-cut SQL read; local projections are DATA, not authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import Connection

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_evidence_restore import read_turn_evidence_on_connection
from elspeth.web.composer.continuation_prompt_producers import _CurrentProducerInputs, _inputs_from_current_evidence_on_connection
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation_composition_initialization import (
    CurrentCompositionInitializationSource,
    _read_composition_initialization_source_on_connection,
    _require_first_values_from_evidence,
)
from elspeth.web.sessions.continuation_runtime import ContinuationRuntimeSource, _runtime_source_from_inputs_on_connection

if TYPE_CHECKING:
    from elspeth.web.composer.service import ComposerServiceImpl


@dataclass(frozen=True, slots=True)
class FirstRuntimeSources:
    initial: CurrentCompositionInitializationSource
    runtime: ContinuationRuntimeSource
    inputs: _CurrentProducerInputs


def read_first_runtime_sources_on_connection(
    conn: Connection,
    composer: ComposerServiceImpl,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
) -> FirstRuntimeSources:
    """Issue no reusable SQL capability; independently run on every window.

    The actual existing source reader owns all current/event/original checks.
    The full actual evidence reader then supplies one snapshot to the shared
    pure first-value and message projections on THIS connection. The runtime
    finisher retains its later live inventory/root/chunk/segment checks.
    """
    initial = _read_composition_initialization_source_on_connection(conn, composer, running, selected=selected, inventory=inventory)
    if not initial.initialized:
        raise AuditIntegrityError("first Compose consumption requires its actual committed initialization")
    evidence = read_turn_evidence_on_connection(conn, running, selected=selected, inventory=inventory)
    _require_first_values_from_evidence(initial, evidence)
    inputs = _inputs_from_current_evidence_on_connection(conn, selected=selected, inventory=inventory, evidence=evidence)
    runtime = _runtime_source_from_inputs_on_connection(conn, running, selected=selected, inventory=inventory, inputs=inputs)
    return FirstRuntimeSources(initial, runtime, inputs)
