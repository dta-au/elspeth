"""Launch-time out-of-band approval guard for wired secrets (elspeth-f3c1aafd25).

The adjudicated secret-wiring policy has two halves: a server-authored
destination allowlist (enforced at wire time and in ``validate_secret_evidence``)
and an out-of-band user approval. This module is the approval half: immediately
before run creation, execution pauses with the exact secret→destination
disclosure set until the caller re-submits with the deterministic token
returned here. The token is derived from the composition snapshot plus the
disclosure set, so approval is bound to exactly what will run — any mutation
re-keys it. LLM text/tool arguments are never approval: no composer or MCP
tool can reach the execute endpoint, and the token only travels on the
authenticated execute request.

Mirrors ``fanout_guard.py`` — same evaluate → 428 → re-submit-with-token →
annotate-accepted-guard-into-the-run's-YAML-launch-record shape.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import TypedDict

from elspeth.contracts.freeze import freeze_fields
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.core.secrets import collect_secret_ref_marker_sites
from elspeth.web.composer.state import CompositionState

SECRET_GUARD_ERROR_TYPE = "execution_secret_approval_required"
SECRET_GUARD_AUDIT_COMMENT = "elspeth_execution_secret_approval"


class ExecutionSecretWiringPayload(TypedDict):
    """Transport shape for one wired secret use requiring approval."""

    secret_name: str
    component_id: str
    component_type: str
    plugin: str
    option_key: str


class ExecutionSecretApprovalGuardPayload(TypedDict):
    """Transport shape for an execution secret-approval guard."""

    ack_token: str
    summary: str
    wirings: list[ExecutionSecretWiringPayload]


@dataclass(frozen=True, slots=True)
class ExecutionSecretWiring:
    """One wired secret use disclosed for user approval. Names only, never values."""

    secret_name: str
    component_id: str
    component_type: str
    plugin: str
    option_key: str

    def to_dict(self) -> ExecutionSecretWiringPayload:
        return {
            "secret_name": self.secret_name,
            "component_id": self.component_id,
            "component_type": self.component_type,
            "plugin": self.plugin,
            "option_key": self.option_key,
        }


@dataclass(frozen=True, slots=True)
class ExecutionSecretApprovalGuard:
    """Structured precondition response for secret-using execution."""

    ack_token: str
    summary: str
    wirings: Sequence[ExecutionSecretWiring]

    def __post_init__(self) -> None:
        # ``wirings`` declared as Sequence; the elements are frozen but a
        # mutable list at the call site would leak ``append`` mutability
        # through the attribute without this guard.
        freeze_fields(self, "wirings")

    def to_dict(self) -> ExecutionSecretApprovalGuardPayload:
        return {
            "ack_token": self.ack_token,
            "summary": self.summary,
            "wirings": [wiring.to_dict() for wiring in self.wirings],
        }


class ExecutionSecretApprovalRequired(Exception):
    """Raised when a run requires explicit user approval of its wired secrets."""

    def __init__(self, guard: ExecutionSecretApprovalGuard) -> None:
        self.guard = guard
        super().__init__(guard.summary)


def evaluate_execution_secret_guard(
    state: CompositionState,
    *,
    env_ref_names: Collection[str] = frozenset(),
) -> ExecutionSecretApprovalGuard | None:
    """Return a guard when the composition uses any wired secret.

    ``env_ref_names`` extends detection to exact ``${NAME}`` env-marker
    strings for names in the caller's secret inventory, matching
    ``validate_secret_evidence``'s collection exactly — the approval set and
    the resolution set must be the same set.
    """
    wirings: list[ExecutionSecretWiring] = []
    for source_name, source in state.sources.items():
        component_id = "source" if source_name == "source" else f"source:{source_name}"
        wirings.extend(
            ExecutionSecretWiring(
                secret_name=site.secret_name,
                component_id=component_id,
                component_type="source",
                plugin=source.plugin,
                option_key=site.field_path,
            )
            for site in collect_secret_ref_marker_sites(source.options, env_ref_names)
        )
    for node in state.nodes:
        wirings.extend(
            ExecutionSecretWiring(
                secret_name=site.secret_name,
                component_id=node.id,
                component_type="transform",
                plugin=node.plugin or "<unset>",
                option_key=site.field_path,
            )
            for site in collect_secret_ref_marker_sites(node.options, env_ref_names)
        )
    for output in state.outputs:
        wirings.extend(
            ExecutionSecretWiring(
                secret_name=site.secret_name,
                component_id=output.name,
                component_type="sink",
                plugin=output.plugin,
                option_key=site.field_path,
            )
            for site in collect_secret_ref_marker_sites(output.options, env_ref_names)
        )

    if not wirings:
        return None

    wiring_dicts = [wiring.to_dict() for wiring in wirings]
    ack_token = stable_hash(
        {
            "kind": "execution_secret_guard_v1",
            "composition_state": state.to_dict(),
            "wirings": wiring_dicts,
        }
    )[:32]
    return ExecutionSecretApprovalGuard(
        ack_token=ack_token,
        summary=_guard_summary(wirings),
        wirings=tuple(wirings),
    )


def annotate_pipeline_yaml_with_secret_guard(
    pipeline_yaml: str,
    guard: ExecutionSecretApprovalGuard,
) -> str:
    """Persist an accepted secret approval in the run's YAML launch record."""
    payload = {
        "kind": "execution_secret_guard_v1",
        "accepted": True,
        "ack_token": guard.ack_token,
        "summary": guard.summary,
        "wirings": [wiring.to_dict() for wiring in guard.wirings],
    }
    return f"# {SECRET_GUARD_AUDIT_COMMENT}: {canonical_json(payload)}\n{pipeline_yaml}"


def _guard_summary(wirings: Sequence[ExecutionSecretWiring]) -> str:
    if len(wirings) == 1:
        wiring = wirings[0]
        return (
            f"Approve secret use before execution: secret '{wiring.secret_name}' is wired into "
            f"{wiring.component_type} '{wiring.component_id}' ({wiring.plugin}) option '{wiring.option_key}'."
        )
    secret_names = sorted({wiring.secret_name for wiring in wirings})
    return f"Approve secret use before execution: {len(wirings)} wired secret use(s) of {', '.join(secret_names)}."
