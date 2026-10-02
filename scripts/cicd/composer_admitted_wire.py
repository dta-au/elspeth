"""Shared root-name authority for the ADMITTED gate and static scorecard.

These relations do not establish value redaction or downstream effects.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pydantic import BaseModel

from elspeth.web.composer.redaction import ToolRedaction, ToolRedactionPolicy, policy_closes_unknown_arguments


@dataclass(frozen=True)
class AdmittedWireRow:
    tool: str
    mode: str
    accepted: frozenset[str]
    model_class: str | None


class AdmittedCensusError(AssertionError):
    """A root admission relation cannot be established."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AdmittedCensusError(message)


def model_argument_keys(tool: str, model: type[BaseModel]) -> frozenset[str]:
    """Measure accepted root names; refuse unsupported alias semantics."""
    for name, field in model.model_fields.items():
        _require(field.alias in (None, name), f"{tool}.{name}: unsupported input alias")
        _require(field.validation_alias in (None, name), f"{tool}.{name}: unsupported validation alias")
        _require(field.serialization_alias in (None, name), f"{tool}.{name}: unsupported serialization alias")
    _require(model.model_config["extra"] == "forbid", f"{tool}: argument model must reject unknown root keys")
    schema = model.model_json_schema(mode="validation", by_alias=True)
    _require(schema["type"] == "object", f"{tool}: unsupported argument root")
    accepted = frozenset(schema["properties"])
    _require(accepted == frozenset(model.model_fields), f"{tool}: accepted schema/field names disagree")
    return accepted


def admitted_wire_rows(
    manifest: Mapping[str, ToolRedaction],
    closes: Callable[[ToolRedactionPolicy], bool] = policy_closes_unknown_arguments,
) -> dict[str, AdmittedWireRow]:
    """Keep every policy, including closed empty and unresolved open policies."""
    rows = {}
    for name, entry in manifest.items():
        model = entry.argument_model
        if model is not None:
            rows[name] = AdmittedWireRow(name, "type_driven", model_argument_keys(name, model), f"{model.__module__}.{model.__qualname__}")
        else:
            policy = entry.policy
            if policy is None:
                raise AdmittedCensusError(f"{name}: missing argument policy")
            mode = "closed_declarative" if closes(policy) else "open_declarative"
            rows[name] = AdmittedWireRow(name, mode, frozenset(policy.known_argument_keys), None)
    return rows
