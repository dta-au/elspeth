"""Complete owned P4 state-payload values, with no SQL or restored authority.

SQL allocates composition-state versions. The temporary graph used solely by
credential admission never becomes a returned state or persistence input.
"""

from pydantic import TypeAdapter, ValidationError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_codec import owned_json
from elspeth.web.composer.continuation_tool_batch import _read_json_value
from elspeth.web.composer.state import CompositionState
from elspeth.web.credential_guard import (
    credential_material_tool_arguments_projection,
    require_no_credential_material,
    require_no_credential_material_in_state,
)
from elspeth.web.sessions._persist_payload import StatePayload
from elspeth.web.sessions.protocol import CompositionStateData, serialize_composition_validation_errors

_P4_PAYLOAD = TypeAdapter(StatePayload)


def _encode_p4_state_payload(payload: StatePayload) -> str:
    if type(payload) is not StatePayload or type(payload.data) is not CompositionStateData:
        raise TypeError("P4 retention requires the actual state payload")
    data = payload.data
    # Admit the frozen JSON algebra before any serializer can normalize it.
    return canonical_json(
        {
            "data": {
                "sources": owned_json(data.sources),
                "nodes": owned_json(data.nodes),
                "edges": owned_json(data.edges),
                "outputs": owned_json(data.outputs),
                "metadata_": owned_json(data.metadata_),
                "is_valid": data.is_valid,
                "validation_errors": owned_json(serialize_composition_validation_errors(data.validation_errors)),
                "composer_meta": owned_json(data.composer_meta),
            },
            "derived_from_state_id": payload.derived_from_state_id,
        }
    )


def decode_p4_state_payload(encoded: str, *, env_ref_names: frozenset[str] = frozenset()) -> StatePayload:
    _read_json_value(encoded)
    try:
        payload = _P4_PAYLOAD.validate_json(encoded, strict=True)
    except (ValidationError, TypeError, ValueError) as error:
        raise AuditIntegrityError("retained P4 state payload violates its owned contract") from error
    if _encode_p4_state_payload(payload) != encoded:
        raise AuditIntegrityError("retained P4 state payload lost or normalized fields")
    # The payload owns nullable SQL columns, not a caller-allocated state version.
    # Project only its graph for the existing component-aware credential guard.
    data = payload.data
    graph = {
        "version": 1,
        "sources": owned_json(data.sources) if data.sources is not None else {},
        "nodes": owned_json(data.nodes) if data.nodes is not None else [],
        "edges": owned_json(data.edges) if data.edges is not None else [],
        "outputs": owned_json(data.outputs) if data.outputs is not None else [],
        "metadata": {"name": "Untitled Pipeline", "description": ""},
    }
    require_no_credential_material(
        credential_material_tool_arguments_projection("set_pipeline", graph),
        surface="continuation P4 graph",
        env_ref_names=env_ref_names,
    )
    require_no_credential_material(
        {
            "metadata": owned_json(data.metadata_),
            "validation_errors": owned_json(serialize_composition_validation_errors(data.validation_errors)),
            "derived_from_state_id": payload.derived_from_state_id,
        },
        surface="continuation P4 non-graph state fields",
        env_ref_names=env_ref_names,
    )
    try:
        credential_state = CompositionState.from_dict(graph)
    except (KeyError, TypeError, ValueError) as error:
        raise AuditIntegrityError("retained P4 graph violates its composition contract") from error
    require_no_credential_material_in_state(credential_state, surface="continuation P4 state", env_ref_names=env_ref_names)
    require_no_credential_material(owned_json(data.composer_meta), surface="continuation P4 state metadata", env_ref_names=env_ref_names)
    return payload


def encode_p4_state_payload(payload: StatePayload, *, env_ref_names: frozenset[str] = frozenset()) -> str:
    encoded = _encode_p4_state_payload(payload)
    decode_p4_state_payload(encoded, env_ref_names=env_ref_names)
    return encoded
