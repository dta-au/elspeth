"""Find the prompt-only bindings eligible for a user's text-file save."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from elspeth.contracts.blobs import BlobRecord, names_same_blob
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.sessions.protocol import CompositionStateRecord


@dataclass(frozen=True, slots=True)
class PromptBinding:
    node_id: str
    field_path: str


def prompt_bindings_for_blob(state: CompositionStateRecord | None, blob: BlobRecord) -> tuple[tuple[PromptBinding, ...], bool]:
    """Return all supported scalar bindings and whether any other use exists."""
    if state is None:
        return (), False
    bindings: list[PromptBinding] = []
    mixed = False

    def references(value: object) -> bool:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if (
                    isinstance(key, str)
                    and (key in {"blob_ref", "blob_id"} or key.endswith("_blob_id"))
                    and isinstance(item, str)
                    and names_same_blob(item, str(blob.id))
                ):
                    return True
                if (
                    key in {"path", "file"}
                    and isinstance(item, str)
                    and (item == blob.storage_path or (item.startswith("blob:") and names_same_blob(item[5:], str(blob.id))))
                ):
                    return True
                if references(item):
                    return True
        elif isinstance(value, (list, tuple)):
            return any(references(item) for item in value)
        return False

    def check_prompt(value: object, node_id: str, field_path: str) -> None:
        nonlocal mixed
        if not references(value):
            return
        if (
            isinstance(value, Mapping)
            and value.get("mode") == "inline_content"
            and isinstance(value.get("blob_ref"), str)
            and names_same_blob(value["blob_ref"], str(blob.id))
        ):
            bindings.append(PromptBinding(node_id, field_path))
        else:
            mixed = True

    for source in (state.source, state.sources, state.outputs):
        if references(source):
            mixed = True
    for raw_node in state.nodes or ():
        node_id = raw_node["id"]
        options: Mapping[str, Any] = raw_node["options"]
        if raw_node["plugin"] != "llm":
            if references(options):
                mixed = True
            continue
        for key, value in options.items():
            if key in {"prompt_template", "system_prompt"}:
                check_prompt(value, node_id, key)
            elif key == "queries" and isinstance(value, Mapping):
                for name, query in value.items():
                    if isinstance(query, Mapping):
                        check_prompt(query.get("template"), node_id, f"queries.{name}.template")
                        if references({k: v for k, v in query.items() if k != "template"}):
                            mixed = True
                    elif references(query):
                        mixed = True
            elif key == "queries" and isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                for index, query in enumerate(value):
                    if isinstance(query, Mapping):
                        check_prompt(query.get("template"), node_id, f"queries.{index}.template")
                        if references({k: v for k, v in query.items() if k != "template"}):
                            mixed = True
                    elif references(query):
                        mixed = True
            elif references(value):
                mixed = True
    return tuple(bindings), mixed


def repin_prompt_bindings(
    state: CompositionStateRecord, bindings: tuple[PromptBinding, ...], new_sha256: str
) -> Sequence[Mapping[str, Any]]:
    """Build the next authored nodes with only bound marker hashes changed."""
    nodes: list[dict[str, Any]] = deep_thaw(state.nodes or ())
    for binding in bindings:
        node = next(node for node in nodes if node["id"] == binding.node_id)
        options = node["options"]
        parts = binding.field_path.split(".")
        if len(parts) == 1:
            marker = options[parts[0]]
        else:
            queries = options["queries"]
            entry = queries[int(parts[1])] if isinstance(queries, list) else queries[parts[1]]
            marker = entry["template"]
        marker["sha256"] = new_sha256
    return nodes
