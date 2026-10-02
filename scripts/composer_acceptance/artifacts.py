"""Select current output identities without discarding the historical manifest."""

from typing import Any


def current_artifacts(artifacts: list[dict[str, Any]], effects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep a repeated target's proven final publication, never guess by time.

    The caller supplies effects from the manifest run's read-only audit
    snapshot. A manifest retains every cumulative publication; an older
    publication's descriptor cannot describe the final bytes at its URI.
    Distinct URIs remain distinct even when their basenames coincide.
    """
    by_target: dict[str, list[dict[str, Any]]] = {}
    by_effect = {effect["effect_id"]: effect for effect in effects}
    if len(by_effect) != len(effects):
        raise ValueError("Duplicate sink effect identities")
    if len({artifact["artifact_id"] for artifact in artifacts}) != len(artifacts):
        raise ValueError("Duplicate artifact identities")
    for artifact in artifacts:
        by_target.setdefault(artifact["path_or_uri"], []).append(artifact)
    selected = []
    for versions in by_target.values():
        if len(versions) == 1 and versions[0].get("sink_effect_id") is None:
            selected.append(versions[0])
            continue
        members = {}
        for artifact in versions:
            effect_id = artifact["sink_effect_id"]
            if effect_id is None or effect_id not in by_effect:
                raise ValueError("Artifact lacks sink effect evidence")
            effect = by_effect[effect_id]
            if (
                effect["state"] != "finalized"
                or effect["artifact_id"] != artifact["artifact_id"]
                or effect["sink_node_id"] != artifact["sink_node_id"]
            ):
                raise ValueError("Artifact does not match its finalized sink effect")
            members[effect_id] = artifact
        chain = [by_effect[effect_id] for effect_id in members]
        if len({(effect["stream_id"], effect["sink_node_id"]) for effect in chain}) != 1 or chain[0]["stream_id"] is None:
            raise ValueError("Repeated target spans ambiguous sink streams")
        stream_key = (chain[0]["stream_id"], chain[0]["sink_node_id"])
        finalized_stream = {
            effect["effect_id"]
            for effect in effects
            if (effect["stream_id"], effect["sink_node_id"]) == stream_key and effect["state"] == "finalized"
        }
        if finalized_stream != members.keys():
            raise ValueError("Artifact manifest is incomplete for its finalized sink stream")
        successors = {}
        roots = []
        for effect in chain:
            predecessor = effect["predecessor_effect_id"]
            if predecessor is None:
                roots.append(effect)
            else:
                if predecessor not in members:
                    raise ValueError("Sink effect predecessor is absent from target history")
                if predecessor in successors:
                    raise ValueError("Sink effect history contains a branch")
                successors[predecessor] = effect
        if len(roots) != 1 or roots[0]["stream_sequence"] != 0:
            raise ValueError("Sink effect history lacks one complete root")
        tip = roots[0]
        visited = {tip["effect_id"]}
        while tip["effect_id"] in successors:
            successor = successors[tip["effect_id"]]
            if successor["effect_id"] in visited or successor["stream_sequence"] != tip["stream_sequence"] + 1:
                raise ValueError("Sink effect history is cyclic or has a sequence gap")
            tip = successor
            visited.add(tip["effect_id"])
        if visited != members.keys():
            raise ValueError("Sink effect history is disconnected")
        selected.append(members[tip["effect_id"]])
    return selected
