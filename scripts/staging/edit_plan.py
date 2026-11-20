"""Edit plans for the Composer edit battery: loading, target resolution and per-edit verdicts.

An edit plan is a JSON file of small edits, each one ordinary chat text plus
the change it must make. A plan is data with no deployment details: it names
graph roles (``{source}``, ``{llm_file}``) that are resolved against the
session's own graph when an attempt starts, and the resolved plan is saved
with the attempt so a resumed run judges exactly what was submitted. A role
may be optional; an edit that needs an optional role the graph lacks is
skipped as not applicable.

The graph is compared through a *view* of the state response: sources,
nodes keyed by id, outputs keyed by name, metadata, and edges keyed by their
content (edge ids are server-assigned). Inside it, any list whose items all
carry a distinct ``id`` (review requirement rows, for example) is keyed by
that id, because its order carries no meaning. Pointers in a plan are JSON
pointers into that view, for example ``/nodes/{llm_file}/options/max_tokens``.

Verdicts work both ways. Before an edit is sent, every expectation must be
FALSE on the current graph (an edit whose target already holds is a plan or
precondition error, never a pass). After the turn every expectation must be
true, and every difference from the previous graph must be accounted for: by
an expectation, by the edit's ``may_change`` prefixes or ``edges_touching``
components, or as a field the server itself writes (review requirement rows,
approval hashes, source authoring marks, auto-wired required controls),
which are allowed by name and reported as server-written.

Expectation forms (exactly one operator key each):

* ``{"equals": PTR, "value": V}``      -- the value at PTR is V (types exact)
* ``{"ends_with": PTR, "value": S}``   -- the string at PTR ends with S
* ``{"present": PTR}`` / ``{"absent": PTR}``
* ``{"restored": PTR}``                -- PTR is as it was when the attempt started;
  absent, null and empty text count as the same "unset"
* ``{"chain": {"from": A, "to": B, "via": [...]}}`` -- rows from A reach B through exactly
  the listed nodes, in order, each route having a single consumer (an auto-wired
  required control the server adds is stepped through)
* ``{"text": PTR, "append": S}`` / ``{"text": PTR, "replace": [OLD, NEW]}`` /
  ``{"text": PTR, "remove": S}`` -- a prompt's text, inline or in a bound file,
  changed exactly so; an optional ``"form": "inline" | "file"`` pins where the
  text must live afterwards
* ``{"new_node": ID, "fields": {...}, "one_of": {...}, "option_keys": [...]}`` -- a node
  that did not exist, with the listed fields, each ``one_of`` field one of the listed
  values, only the listed option keys, and every other field unset
* ``{"new_output": NAME, "fields": {...}, "one_of": {...}, "options": {...}, "option_keys": [...]}``
  -- the same for an output; ``options`` maps a key to ``{"equals": V}``,
  ``{"ends_with": S}`` or ``{"one_of": [...]}``
* ``{"renamed_node": {"from": A, "to": B}}`` -- A is gone and B carries every one of A's
  fields (plugin, options, description, error routing...) except its id and its place
  in the chain
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.staging.opslib import JsonObject, UsageError, differences

PLAN_FORMAT = "elspeth-composer-edit-plan/2"
PLAN_DIR = Path(__file__).resolve().parent / "edit_plans"
DEFAULT_PLAN = PLAN_DIR / "composer_gold_50.json"
MIXED_PLAN = PLAN_DIR / "composer_mixed_50.json"

TARGET_KINDS = frozenset(
    {
        "first_source",
        "consumer_of",
        "llm_node",
        "first_output",
        "source_without_option",
        "secret_holder",
        "secret_option_of",
        "secret_name_of",
        "operator",
    }
)
REVIEW_KINDS = frozenset({"llm_prompt_template"})
OPERATORS = ("equals", "ends_with", "present", "absent", "restored", "chain", "text", "new_node", "new_output", "renamed_node")
TEXT_CHANGES = ("append", "replace", "remove")
TEMPORARY_PREFIX = "battery_"
MARKER_TEXTS = ("Battery marker", "Battery file marker")

# Option keys the server writes itself when it reconciles review evidence on a
# model edit or resolves a prompt review card (``interpretation_state``,
# ``sessions/pending_interpretation``), and the authoring mark the source bind
# tools stamp. Allowed by name only, and reported as server-written.
SERVER_NODE_OPTION_KEYS = ("interpretation_requirements", "approved_prompt_artifact_hash")
SERVER_SOURCE_OPTION_KEYS = ("interpretation_requirements", "source_authoring")
AUTO_WIRED_CONTROL_TERM = "required_control_auto_wired"
_ROUTING_FIELDS = ("id", "input", "on_success")

_NAME = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_MESSAGE_FIELD = re.compile(r"\{was:((?:[^{}]|\{[a-z][a-z0-9_]*\})+)\}|\{([a-z][a-z0-9_]*)\}")
_PLAN_KEYS = frozenset({"format", "name", "description", "forbidden_tools", "targets", "edits"})
_EDIT_KEYS = frozenset({"id", "message", "expect", "may_change", "edges_touching", "tools_any", "tools_all", "approve"})
_TARGET_ARGS: Mapping[str, frozenset[str]] = {
    "first_source": frozenset(),
    "first_output": frozenset(),
    "consumer_of": frozenset({"target"}),
    "llm_node": frozenset({"prompt"}),
    "source_without_option": frozenset({"plugin", "option"}),
    "secret_holder": frozenset(),
    "secret_option_of": frozenset({"target"}),
    "secret_name_of": frozenset({"target"}),
    "operator": frozenset(),
}

type ReadText = Callable[[Mapping[str, Any]], str]


# ── graph view and pointers ────────────────────────────────────────────────


def edge_key(edge: Mapping[str, Any]) -> str:
    return f"{edge['from_node']}|{edge['to_node']}|{edge['edge_type']}|{edge['label']}"


def _keyed(value: object) -> Any:
    """Key every list of distinct-``id`` objects by id, recursively; other lists keep their order."""
    if type(value) is dict:
        return {key: _keyed(item) for key, item in value.items()}
    if type(value) is list:
        ids = [item["id"] for item in value if type(item) is dict and "id" in item and type(item["id"]) is str]
        if value and len(ids) == len(value) and len(set(ids)) == len(ids):
            return {"id=" + item["id"]: _keyed(item) for item in value}
        return [_keyed(item) for item in value]
    return copy.deepcopy(value)


def _component(item: Mapping[str, Any]) -> JsonObject:
    """One source, node or output: keyed, with an empty description read as no description.

    A description that is absent, null or empty text is the same "no
    description"; a step re-sent whole may carry any of the three.
    """
    keyed: JsonObject = _keyed(item)
    if "description" not in keyed or keyed["description"] == "":
        keyed["description"] = None
    return keyed


def graph_view(state: Mapping[str, Any]) -> JsonObject:
    """The authored graph of a state response, keyed for comparison."""
    return {
        "sources": {name: _component(source) for name, source in state["sources"].items()},
        "nodes": {node["id"]: _component(node) for node in state["nodes"]},
        "outputs": {output["name"]: _component(output) for output in state["outputs"]},
        "metadata": _keyed(state["metadata"]),
        "edges": {edge_key(edge): True for edge in state["edges"]},
    }


def escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _segments(pointer: str) -> list[str]:
    if not pointer.startswith("/"):
        raise UsageError(f"plan pointer {pointer!r} must start with /")
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def value_at(view: Mapping[str, Any], pointer: str) -> tuple[bool, Any]:
    """``(True, value)`` when the pointer resolves in ``view``, else ``(False, None)``."""
    current: Any = view
    for part in _segments(pointer):
        if type(current) is dict and part in current:
            current = current[part]
        elif type(current) is list and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False, None
    return True, current


def covers(prefix: str, path: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _blank(found: bool, value: object) -> bool:
    return not found or value is None or value == ""


# ── loading and resolving ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Plan:
    name: str
    sha256: str
    forbidden_tools: tuple[str, ...]
    targets: Mapping[str, JsonObject]
    edits: tuple[JsonObject, ...]

    def roles_of(self, edit: Mapping[str, Any]) -> set[str]:
        return set(_NAME.findall(json.dumps(edit)))

    def expected_tools(self) -> list[str]:
        return sorted({tool for edit in self.edits for key in ("tools_any", "tools_all") if key in edit for tool in edit[key]})


def load_plan(path: Path) -> Plan:
    """Read and validate a plan file; every fault is a usage error naming the edit."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise UsageError(f"cannot read plan {path}: {exc.strerror}") from None
    try:
        raw = json.loads(data)
    except ValueError:
        raise UsageError(f"plan {path} is not JSON") from None
    if type(raw) is not dict or set(raw) != _PLAN_KEYS or raw["format"] != PLAN_FORMAT:
        raise UsageError(f"plan {path} must be an object with exactly {sorted(_PLAN_KEYS)} and format {PLAN_FORMAT}")
    if type(raw["name"]) is not str or not raw["name"]:
        raise UsageError("plan name must be a non-empty string")
    forbidden = raw["forbidden_tools"]
    if type(forbidden) is not list or not all(type(tool) is str for tool in forbidden):
        raise UsageError("forbidden_tools must be a list of tool names")
    targets = raw["targets"]
    if type(targets) is not dict:
        raise UsageError("targets must be an object")
    for name, spec in targets.items():
        _check_target(name, spec, targets)
    edits = raw["edits"]
    if type(edits) is not list or not edits:
        raise UsageError("a plan needs at least one edit")
    seen: set[str] = set()
    for edit in edits:
        _check_edit(edit, targets, seen, forbidden)
    return Plan(
        name=raw["name"],
        sha256=hashlib.sha256(data).hexdigest(),
        forbidden_tools=tuple(forbidden),
        targets=targets,
        edits=tuple(edits),
    )


def _check_target(name: str, spec: object, targets: Mapping[str, Any]) -> None:
    if not _NAME.fullmatch("{" + name + "}") or type(spec) is not dict or "kind" not in spec or spec["kind"] not in TARGET_KINDS:
        raise UsageError(f"target {name!r} needs a lowercase name and a kind from {sorted(TARGET_KINDS)}")
    kind = spec["kind"]
    allowed = {"kind", "optional", *_TARGET_ARGS[kind]}
    if not {"kind", *_TARGET_ARGS[kind]} <= set(spec) <= allowed:
        raise UsageError(f"target {name!r} of kind {kind} takes {sorted(allowed)}")
    if "optional" in spec and type(spec["optional"]) is not bool:
        raise UsageError(f"target {name!r} optional must be true or false")
    if "target" in spec and (spec["target"] not in targets or list(targets).index(spec["target"]) >= list(targets).index(name)):
        raise UsageError(f"target {name!r} must name an earlier target")
    if kind == "llm_node" and spec["prompt"] not in ("inline", "file"):
        raise UsageError(f"target {name!r} prompt must be inline or file")
    if kind == "operator" and not ("optional" in spec and spec["optional"] is True):
        raise UsageError(f"target {name!r} is named by the operator only, so it must be optional")


def _check_edit(edit: object, targets: Mapping[str, Any], seen: set[str], forbidden: Sequence[str]) -> None:
    if type(edit) is not dict or not {"id", "message", "expect"} <= set(edit) <= _EDIT_KEYS:
        raise UsageError(f"each edit needs id, message and expect, and may add {sorted(_EDIT_KEYS - {'id', 'message', 'expect'})}")
    label = edit["id"]
    if type(label) is not str or not label or label in seen:
        raise UsageError(f"edit ids must be unique non-empty strings ({label!r})")
    seen.add(label)
    if type(edit["message"]) is not str or not edit["message"].strip():
        raise UsageError(f"edit {label}: message must be text")
    if type(edit["expect"]) is not list or not edit["expect"]:
        raise UsageError(f"edit {label}: expect must list at least one expectation")
    for expectation in edit["expect"]:
        _check_expectation(label, expectation)
    for key in ("may_change", "edges_touching", "tools_any", "tools_all"):
        if key in edit and (type(edit[key]) is not list or not edit[key] or not all(type(item) is str for item in edit[key])):
            raise UsageError(f"edit {label}: {key} must be a non-empty list of strings")
    if "may_change" in edit and any(prefix in ("/edges", "/nodes", "/outputs", "/sources", "/") for prefix in edit["may_change"]):
        raise UsageError(f"edit {label}: may_change must name specific paths, not a whole collection; use edges_touching for edges")
    if "tools_any" not in edit and "tools_all" not in edit:
        raise UsageError(f"edit {label}: every edit names its acceptable tools (tools_any and/or tools_all)")
    named = [tool for key in ("tools_any", "tools_all") if key in edit for tool in edit[key]]
    if set(named) & set(forbidden):
        raise UsageError(f"edit {label}: expects a forbidden tool")
    if any(tool in edit["message"] for tool in named):
        raise UsageError(f"edit {label}: the message names a tool; describe the change, not the tool")
    if "approve" in edit:
        approve = edit["approve"]
        if type(approve) is not dict or set(approve) != {"kind", "node"} or approve["kind"] not in REVIEW_KINDS:
            raise UsageError(f"edit {label}: approve must be {{kind, node}} with kind in {sorted(REVIEW_KINDS)}")
    for name in _NAME.findall(json.dumps(edit)):
        if name not in targets:
            raise UsageError(f"edit {label}: placeholder {{{name}}} is not a plan target")


def _str_map(value: object) -> bool:
    return type(value) is dict and all(type(key) is str for key in value)


def _check_expectation(label: str, expectation: object) -> None:
    if type(expectation) is not dict:
        raise UsageError(f"edit {label}: an expectation must be an object")
    operators = [key for key in OPERATORS if key in expectation]
    if len(operators) != 1:
        raise UsageError(f"edit {label}: each expectation takes exactly one of {list(OPERATORS)}")
    operator = operators[0]
    keys = set(expectation)
    if operator in ("equals", "ends_with"):
        valid = keys == {operator, "value"} and (operator == "equals" or type(expectation["value"]) is str)
    elif operator in ("present", "absent", "restored"):
        valid = keys == {operator}
    elif operator == "chain":
        chain = expectation["chain"]
        valid = (
            keys == {"chain"}
            and type(chain) is dict
            and set(chain) == {"from", "to", "via"}
            and type(chain["via"]) is list
            and all(type(item) is str for item in [chain["from"], chain["to"], *chain["via"]])
        )
    elif operator == "renamed_node":
        rename = expectation["renamed_node"]
        valid = (
            keys == {"renamed_node"}
            and type(rename) is dict
            and set(rename) == {"from", "to"}
            and all(type(v) is str for v in rename.values())
        )
    elif operator in ("new_node", "new_output"):
        extra = {"options"} if operator == "new_output" else set()
        valid = (
            type(expectation[operator]) is str
            and {operator, "fields", "option_keys"} <= keys <= {operator, "fields", "one_of", "option_keys", *extra}
            and _str_map(expectation["fields"])
            and (
                "one_of" not in expectation
                or (_str_map(expectation["one_of"]) and all(type(v) is list for v in expectation["one_of"].values()))
            )
            and type(expectation["option_keys"]) is list
            and all(type(key) is str for key in expectation["option_keys"])
            and (
                "options" not in expectation
                or (
                    _str_map(expectation["options"])
                    and all(
                        type(rule) is dict and len(rule) == 1 and next(iter(rule)) in ("equals", "ends_with", "one_of")
                        for rule in expectation["options"].values()
                    )
                )
            )
        )
    else:
        changes = [key for key in TEXT_CHANGES if key in expectation]
        valid = (
            len(changes) == 1
            and keys <= {"text", "form", *TEXT_CHANGES}
            and ("form" not in expectation or expectation["form"] in ("inline", "file"))
            and (
                type(expectation[changes[0]]) is str
                if changes[0] != "replace"
                else type(expectation["replace"]) is list
                and len(expectation["replace"]) == 2
                and all(type(item) is str for item in expectation["replace"])
            )
        )
    if not valid:
        raise UsageError(f"edit {label}: malformed {operator} expectation")


def resolve_targets(plan: Plan, state: Mapping[str, Any], overrides: Mapping[str, str]) -> dict[str, str]:
    """Resolve each plan target on the graph; an override must name a fitting component.

    A required target the graph lacks is a usage error; an optional one is left
    unresolved, and the edits that need it are skipped as not applicable.
    """
    view = graph_view(state)
    unknown = sorted(set(overrides) - set(plan.targets))
    if unknown:
        raise UsageError(f"--target names {unknown} are not targets of plan {plan.name}")
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for name, spec in plan.targets.items():
        if "target" in spec and spec["target"] not in resolved:
            continue
        value = overrides[name] if name in overrides else _find_target(spec, view, resolved)
        if value is None:
            if not ("optional" in spec and spec["optional"] is True):
                missing.append(f"{name} ({_describe(spec)})")
            continue
        if not _fits(spec, view, resolved, value):
            raise UsageError(f"--target {name}={value} is not {_describe(spec)} in this graph")
        resolved[name] = value
    if missing:
        raise UsageError(
            "this graph has no component for plan targets: "
            + "; ".join(missing)
            + ". Name one with --target NAME=ID, or use a plan that fits the graph."
        )
    return resolved


def _describe(spec: Mapping[str, Any]) -> str:
    kind = spec["kind"]
    if kind == "consumer_of":
        return f"the single node fed by {{{spec['target']}}}"
    if kind == "llm_node":
        return f"a transform with a prompt_template {'string' if spec['prompt'] == 'inline' else 'bound to a file'}"
    if kind == "source_without_option":
        return f"a {spec['plugin']} source with no explicit {spec['option']} option"
    if kind == "secret_option_of":
        return f"the option holding a secret reference on {{{spec['target']}}}"
    if kind == "secret_name_of":
        return f"the secret named on {{{spec['target']}}}"
    return {
        "first_source": "a source",
        "first_output": "an output",
        "secret_holder": "a node with a secret reference option",
        "operator": "a value only the operator can name",
    }[kind]


def _route_of(view: Mapping[str, Any], name: str) -> str | None:
    if name in view["sources"]:
        route: str | None = view["sources"][name]["on_success"]
        return route
    if name in view["nodes"]:
        route = view["nodes"][name]["on_success"]
        return route
    return None


def _prompt_form(node: Mapping[str, Any]) -> str | None:
    options = node["options"]
    if node["node_type"] != "transform" or "prompt_template" not in options:
        return None
    template = options["prompt_template"]
    if type(template) is str:
        return "inline"
    if type(template) is dict and "blob_ref" in template:
        return "file"
    return None


def _secret_slots(node: Mapping[str, Any]) -> list[tuple[str, str]]:
    return [
        (key, value["secret_ref"])
        for key, value in node["options"].items()
        if type(value) is dict and set(value) == {"secret_ref"} and type(value["secret_ref"]) is str
    ]


def _find_target(spec: Mapping[str, Any], view: Mapping[str, Any], resolved: Mapping[str, str]) -> str | None:
    kind = spec["kind"]
    if kind == "first_source":
        return next(iter(view["sources"]), None)
    if kind == "first_output":
        return next(iter(view["outputs"]), None)
    if kind == "operator":
        return None
    if kind == "source_without_option":
        return next((name for name in view["sources"] if _fits(spec, view, resolved, name)), None)
    if kind == "consumer_of":
        route = _route_of(view, resolved[spec["target"]])
        consumers = [node_id for node_id, node in view["nodes"].items() if route is not None and node["input"] == route]
        return consumers[0] if len(consumers) == 1 else None
    if kind == "secret_holder":
        return next((node_id for node_id, node in view["nodes"].items() if _secret_slots(node)), None)
    if kind in ("secret_option_of", "secret_name_of"):
        holder = view["nodes"][resolved[spec["target"]]]
        slots = _secret_slots(holder)
        return (slots[0][0] if kind == "secret_option_of" else slots[0][1]) if slots else None
    return next((node_id for node_id, node in view["nodes"].items() if _prompt_form(node) == spec["prompt"]), None)


def _fits(spec: Mapping[str, Any], view: Mapping[str, Any], resolved: Mapping[str, str], value: str) -> bool:
    kind = spec["kind"]
    if kind == "first_source":
        return value in view["sources"]
    if kind == "first_output":
        return value in view["outputs"]
    if kind == "operator":
        return bool(value)
    if kind == "source_without_option":
        return (
            value in view["sources"]
            and view["sources"][value]["plugin"] == spec["plugin"]
            and spec["option"] not in view["sources"][value]["options"]
        )
    if kind == "consumer_of":
        return value in view["nodes"] and view["nodes"][value]["input"] == _route_of(view, resolved[spec["target"]])
    if kind == "secret_holder":
        return value in view["nodes"] and bool(_secret_slots(view["nodes"][value]))
    if kind == "secret_option_of":
        return value in [key for key, _name in _secret_slots(view["nodes"][resolved[spec["target"]]])]
    if kind == "secret_name_of":
        return value in [name for _key, name in _secret_slots(view["nodes"][resolved[spec["target"]]])]
    return value in view["nodes"] and _prompt_form(view["nodes"][value]) == spec["prompt"]


def _fill(value: object, targets: Mapping[str, str], *, pointer: bool) -> Any:
    if isinstance(value, str):
        return _NAME.sub(lambda match: escape(targets[match.group(1)]) if pointer else targets[match.group(1)], value)
    if isinstance(value, list):
        return [_fill(item, targets, pointer=pointer) for item in value]
    if isinstance(value, dict):
        return {key: _fill(item, targets, pointer=pointer) for key, item in value.items()}
    return value


def render_was(baseline: Mapping[str, Any], pointer: str) -> str:
    """A baseline value as message text; an unset value never renders as the word null."""
    found, value = value_at(baseline, pointer)
    if _blank(found, value):
        return "no value (remove the setting)"
    return json.dumps(value)


_POINTER_KEYS = frozenset({"equals", "ends_with", "present", "absent", "restored", "text"})


def resolve_edits(plan: Plan, targets: Mapping[str, str], baseline_state: Mapping[str, Any]) -> list[JsonObject]:
    """The plan's edits with targets and ``{was:...}`` values filled in, numbered from 1.

    An edit that needs a role the graph lacks is kept with ``applicable: false``.
    """
    baseline = graph_view(baseline_state)
    resolved: list[JsonObject] = []
    for number, edit in enumerate(plan.edits, start=1):
        missing = sorted(plan.roles_of(edit) - set(targets))
        if missing:
            resolved.append({"number": number, "id": edit["id"], "applicable": False, "missing_roles": missing})
            continue

        def field(match: re.Match[str]) -> str:
            if match.group(2) is not None:
                return targets[match.group(2)]
            return render_was(baseline, _fill(match.group(1), targets, pointer=True))

        expect: list[JsonObject] = []
        for expectation in edit["expect"]:
            expect.append({key: _fill(value, targets, pointer=key in _POINTER_KEYS) for key, value in expectation.items()})
        resolved.append(
            {
                "number": number,
                "id": edit["id"],
                "applicable": True,
                "message": _MESSAGE_FIELD.sub(field, edit["message"]),
                "expect": expect,
                "may_change": _fill(edit["may_change"], targets, pointer=True) if "may_change" in edit else [],
                "edges_touching": _fill(edit["edges_touching"], targets, pointer=False) if "edges_touching" in edit else [],
                "tools_any": list(edit["tools_any"]) if "tools_any" in edit else [],
                "tools_all": list(edit["tools_all"]) if "tools_all" in edit else [],
                "approve": _fill(edit["approve"], targets, pointer=False) if "approve" in edit else None,
            }
        )
    return resolved


# ── server-written changes ─────────────────────────────────────────────────


def auto_wired_controls(before: Mapping[str, Any], after: Mapping[str, Any]) -> set[str]:
    """Nodes the server added as deployment-required controls in this turn."""
    found: set[str] = set()
    for node_id, node in after["nodes"].items():
        if node_id in before["nodes"] or "interpretation_requirements" not in node["options"]:
            continue
        rows = node["options"]["interpretation_requirements"]
        items = rows.values() if type(rows) is dict else rows
        if any(type(row) is dict and "user_term" in row and row["user_term"] == AUTO_WIRED_CONTROL_TERM for row in items):
            found.add(node_id)
    return found


def named_components(edit: Mapping[str, Any]) -> set[str]:
    """The sources, nodes and outputs an edit names: in its pointers, chains, new or renamed components and edge list."""
    named: set[str] = set(edit["edges_touching"])
    pointers: list[str] = list(edit["may_change"])
    for expectation in edit["expect"]:
        if "chain" in expectation:
            named.update(_members(expectation["chain"]))
        elif "renamed_node" in expectation:
            named.update(expectation["renamed_node"].values())
        elif "new_node" in expectation or "new_output" in expectation:
            named.add(expectation["new_node"] if "new_node" in expectation else expectation["new_output"])
        else:
            pointers.append(expectation[next(key for key in OPERATORS if key in expectation)])
    for pointer in pointers:
        parts = _segments(pointer)
        if len(parts) >= 2 and parts[0] in ("sources", "nodes", "outputs"):
            named.add(parts[1])
    return named


def server_written_paths(
    before: Mapping[str, Any], after: Mapping[str, Any], paths: Sequence[str], *, named: set[str] | None = None
) -> list[str]:
    """The difference paths that are fields the server writes itself, allowed by name.

    Review requirement rows, approval hashes and source authoring marks count
    only on the components in ``named`` (the ones the edit names, and the node
    whose card was approved); on any other component they are unrelated
    changes. ``named=None`` allows them anywhere (the as-found comparison).
    """
    auto = auto_wired_controls(before, after)
    auto_routes = {route for node_id in auto for route in (after["nodes"][node_id]["input"], after["nodes"][node_id]["on_success"])}

    def server_written(path: str) -> bool:
        parts = _segments(path)
        collection = parts[0]
        if collection == "nodes" and len(parts) >= 2 and parts[1] in auto:
            return True
        if len(parts) >= 4 and parts[2] == "options":
            if named is not None and parts[1] not in named:
                return False
            if collection == "nodes":
                return parts[3] in SERVER_NODE_OPTION_KEYS
            return collection == "sources" and parts[3] in SERVER_SOURCE_OPTION_KEYS
        if collection == "edges" and len(parts) == 2:
            return bool(set(parts[1].split("|")[:2]) & auto)
        if len(parts) == 3 and collection in ("nodes", "sources") and parts[2] in ("input", "on_success"):
            found, value = value_at(after, path)
            return found and value in auto_routes
        return False

    return [path for path in paths if server_written(path)]


# ── verdicts ───────────────────────────────────────────────────────────────


def text_at(view: Mapping[str, Any], pointer: str, read_text: ReadText) -> tuple[str | None, str | None]:
    """``(form, text)`` of a prompt at ``pointer``: inline string or bound file; ``(None, None)`` otherwise."""
    found, value = value_at(view, pointer)
    if not found:
        return None, None
    if type(value) is str:
        return "inline", value
    if type(value) is dict and "blob_ref" in value:
        return "file", read_text(value)
    return None, None


def _text_ok(expectation: Mapping[str, Any], before: str | None, after: str | None) -> bool:
    if before is None or after is None:
        return False
    if "append" in expectation:
        stem = before.rstrip()
        return after.startswith(stem) and after[len(stem) :].strip() == expectation["append"]
    if "replace" in expectation:
        old, new = expectation["replace"]
        return before.count(old) == 1 and after == before.replace(old, new)
    removed: str = expectation["remove"]
    return before.count(removed) == 1 and after.rstrip() == before.replace(removed, "").rstrip()


def _members(chain: Mapping[str, Any]) -> list[str]:
    return [chain["from"], *chain["via"], chain["to"]]


def chain_paths(chain: Mapping[str, Any], view: Mapping[str, Any]) -> list[str]:
    """The routing pointers a chain expectation accounts for."""
    start = chain["from"]
    paths = [f"/sources/{escape(start)}/on_success" if start in view["sources"] else f"/nodes/{escape(start)}/on_success"]
    for node in chain["via"]:
        paths.extend([f"/nodes/{escape(node)}/input", f"/nodes/{escape(node)}/on_success"])
    if chain["to"] in view["nodes"]:
        paths.append(f"/nodes/{escape(chain['to'])}/input")
    return paths


def _chain_ok(chain: Mapping[str, Any], view: Mapping[str, Any], transparent: set[str]) -> bool:
    nodes = view["nodes"]

    def consumers(route: str) -> list[str]:
        return [node_id for node_id, node in nodes.items() if node["input"] == route]

    def step_through(route: str | None) -> str | None:
        # An auto-wired required control the server spliced in is part of the path, not a stray consumer.
        while route is not None:
            found = consumers(route)
            if len(found) == 1 and found[0] in transparent:
                route = nodes[found[0]]["on_success"]
            else:
                return route
        return route

    route = step_through(_route_of(view, chain["from"]))
    for node in chain["via"]:
        if route is None or node not in nodes or consumers(route) != [node]:
            return False
        route = step_through(nodes[node]["on_success"])
    if chain["to"] in nodes:
        return route is not None and consumers(route) == [chain["to"]]
    return route is not None and route == chain["to"]


def _empty(value: object) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _new_component_ok(expectation: Mapping[str, Any], operator: str, before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    collection = "nodes" if operator == "new_node" else "outputs"
    name = expectation[operator]
    if name in before[collection] or name not in after[collection]:
        return False
    item = after[collection][name]
    fields: Mapping[str, Any] = expectation["fields"]
    one_of: Mapping[str, list[Any]] = expectation["one_of"] if "one_of" in expectation else {}
    for key, value in fields.items():
        if key not in item or type(item[key]) is not type(value) or item[key] != value:
            return False
    for key, values in one_of.items():
        current = item[key] if key in item else None
        if not any(type(current) is type(value) and current == value for value in values):
            return False
    exempt = {*fields, *one_of, "options", "name", *_ROUTING_FIELDS}
    if any(not _empty(value) for key, value in item.items() if key not in exempt):
        return False
    server_keys = SERVER_NODE_OPTION_KEYS if collection == "nodes" else ("interpretation_requirements",)
    if not set(item["options"]) <= {*expectation["option_keys"], *server_keys}:
        return False
    rules: Mapping[str, Mapping[str, Any]] = expectation["options"] if "options" in expectation else {}
    for key, rule in rules.items():
        if key not in item["options"]:
            return False
        current = item["options"][key]
        if "equals" in rule and not (type(current) is type(rule["equals"]) and current == rule["equals"]):
            return False
        if "ends_with" in rule and not (type(current) is str and current.endswith(rule["ends_with"])):
            return False
        if "one_of" in rule and current not in rule["one_of"]:
            return False
    return True


def _renamed_ok(rename: Mapping[str, str], before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    old, new = rename["from"], rename["to"]
    if old not in before["nodes"] or old in after["nodes"] or new in before["nodes"] or new not in after["nodes"]:
        return False
    was, now = before["nodes"][old], after["nodes"][new]
    for key in set(was) | set(now):
        if key in _ROUTING_FIELDS:
            continue
        if key == "options":
            kept = {k: v for k, v in was["options"].items() if k not in SERVER_NODE_OPTION_KEYS}
            moved = {k: v for k, v in now["options"].items() if k not in SERVER_NODE_OPTION_KEYS}
            if differences(kept, moved):
                return False
        elif _blank(key in was, was[key] if key in was else None) and _blank(key in now, now[key] if key in now else None):
            continue
        elif key not in was or key not in now or differences(was[key], now[key]):
            return False
    return True


def _edge_paths(view: Mapping[str, Any], components: Sequence[str]) -> list[str]:
    wanted = set(components)
    return ["/edges/" + escape(key) for key in view["edges"] if set(key.split("|")[:2]) & wanted]


def check_expectations(
    edit: Mapping[str, Any],
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    baseline: Mapping[str, Any],
    read_text: ReadText,
) -> tuple[list[str], list[str]]:
    """``(failures, covered pointers)``: each failure names the expectation that did not hold."""
    failures: list[str] = []
    covered: list[str] = []
    transparent = auto_wired_controls(before, after)
    touching = list(edit["edges_touching"])
    for index, expectation in enumerate(edit["expect"]):
        label = f"expect[{index}]"
        operator = next(key for key in OPERATORS if key in expectation)
        if operator == "chain":
            chain = expectation["chain"]
            covered.extend(chain_paths(chain, before) + chain_paths(chain, after))
            touching.extend(_members(chain))
            ok = _chain_ok(chain, after, transparent)
            pointer = chain["from"] + ">" + chain["to"]
        elif operator == "renamed_node":
            rename = expectation["renamed_node"]
            covered.extend([f"/nodes/{escape(rename['from'])}", f"/nodes/{escape(rename['to'])}"])
            touching.extend([rename["from"], rename["to"]])
            ok = _renamed_ok(rename, before, after)
            pointer = rename["from"] + ">" + rename["to"]
        elif operator in ("new_node", "new_output"):
            pointer = f"/{'nodes' if operator == 'new_node' else 'outputs'}/{escape(expectation[operator])}"
            covered.append(pointer)
            touching.append(expectation[operator])
            ok = _new_component_ok(expectation, operator, before, after)
        else:
            pointer = expectation[operator]
            covered.append(pointer)
            found, value = value_at(after, pointer)
            if operator == "equals":
                ok = found and type(value) is type(expectation["value"]) and value == expectation["value"]
            elif operator == "ends_with":
                ok = found and type(value) is str and value.endswith(expectation["value"])
            elif operator == "present":
                ok = found
            elif operator == "absent":
                ok = not found
                parts = _segments(pointer)
                if len(parts) == 2 and parts[0] in ("nodes", "outputs"):
                    touching.append(parts[1])
            elif operator == "restored":
                was_found, was = value_at(baseline, pointer)
                ok = _blank(found, value) if _blank(was_found, was) else (found and not differences(was, value))
            else:
                _, before_text = text_at(before, pointer, read_text)
                form, after_text = text_at(after, pointer, read_text)
                ok = _text_ok(expectation, before_text, after_text) and ("form" not in expectation or form == expectation["form"])
        if not ok:
            failures.append(f"{label}:{operator}:{pointer}")
    covered.extend(_edge_paths(before, touching) + _edge_paths(after, touching))
    return failures, covered


def preconditions(edit: Mapping[str, Any], current: Mapping[str, Any], baseline: Mapping[str, Any], read_text: ReadText) -> list[str]:
    """Expectations that already hold on the current graph: the edit would pass with nothing changed."""
    failures, _ = check_expectations(edit, current, current, baseline, read_text)
    failed = {item.split(":", 1)[0] for item in failures}
    return [f"precondition:expect[{index}]_already_holds" for index in range(len(edit["expect"])) if f"expect[{index}]" not in failed]


@dataclass(frozen=True, slots=True)
class Changes:
    unrelated: list[str]
    server_written: list[str]


def classify_changes(
    before: Mapping[str, Any], after: Mapping[str, Any], allowed: Sequence[str], *, named: set[str] | None = None
) -> Changes:
    """Split the difference paths no expectation covers into server-written ones and unrelated ones."""
    rest = [path for path in differences(dict(before), dict(after)) if not any(covers(prefix, path) for prefix in allowed)]
    server = server_written_paths(before, after, rest, named=named)
    return Changes(unrelated=[path for path in rest if path not in server], server_written=server)


def unrelated_changes(
    before: Mapping[str, Any], after: Mapping[str, Any], allowed: Sequence[str], *, named: set[str] | None = None
) -> list[str]:
    return classify_changes(before, after, allowed, named=named).unrelated


def new_validation_errors(before_state: Mapping[str, Any], after_state: Mapping[str, Any]) -> list[str]:
    """Validation errors present after the edit that were not present before it (by code, component and message)."""

    def keys(state: Mapping[str, Any]) -> list[str]:
        errors = state["validation_errors"] or []
        return [json.dumps([error["error_code"], error["component"], error["message"]]) for error in errors]

    remaining = keys(before_state)
    added: list[str] = []
    for key in keys(after_state):
        if key in remaining:
            remaining.remove(key)
        else:
            added.append(key)
    return added


_USED = ("applied", "completed")
_REFUSED = ("rejected", "failed", "cancelled")


def tool_findings(edit: Mapping[str, Any], evidence: Mapping[str, Any], forbidden: Sequence[str]) -> list[str]:
    """A forbidden tool called and not rejected; none of ``tools_any`` used; any of ``tools_all`` unused."""
    outcomes: Mapping[str, Mapping[str, int]] = evidence["tool_outcomes"]
    findings: list[str] = []
    for tool in forbidden:
        if tool in outcomes and any(outcome != "rejected" for outcome in outcomes[tool]):
            findings.append(f"forbidden_tool_called:{tool}")

    def used(tool: str) -> bool:
        return tool in outcomes and any(outcome in _USED for outcome in outcomes[tool])

    if edit["tools_any"] and not any(used(tool) for tool in edit["tools_any"]):
        findings.append("expected_tool_not_used:" + "|".join(edit["tools_any"]))
    findings.extend(f"required_tool_not_used:{tool}" for tool in edit["tools_all"] if not used(tool))
    return findings


def tool_warnings(evidence: Mapping[str, Any]) -> list[str]:
    """Tool calls that were refused inside the turn; on a passing edit they were corrected."""
    outcomes: Mapping[str, Mapping[str, int]] = evidence["tool_outcomes"]
    return [
        f"tool_call_{outcome}:{tool}:{count}"
        for tool, counts in outcomes.items()
        for outcome, count in counts.items()
        if outcome in _REFUSED
    ]


def leftover_findings(state: Mapping[str, Any], read_text: ReadText) -> list[str]:
    """Traces of an earlier battery attempt: temporary steps or outputs, marker text in prompts, the battery's pipeline name."""
    view = graph_view(state)
    found = [f"/nodes/{node_id}" for node_id in view["nodes"] if node_id.startswith(TEMPORARY_PREFIX)]
    found += [f"/outputs/{name}" for name in view["outputs"] if name.startswith(TEMPORARY_PREFIX)]
    if "name" in view["metadata"] and type(view["metadata"]["name"]) is str and view["metadata"]["name"].startswith("Battery check"):
        found.append("/metadata/name")
    for node_id in view["nodes"]:
        for key in ("prompt_template", "system_prompt"):
            _, text = text_at(view, f"/nodes/{escape(node_id)}/options/{key}", read_text)
            if text is not None and any(marker in text for marker in MARKER_TEXTS):
                found.append(f"/nodes/{node_id}/options/{key}")
    return found


def as_found_differences(baseline_state: Mapping[str, Any], final_state: Mapping[str, Any], read_text: ReadText) -> list[str]:
    """Differences between the final graph and the attempt's starting graph that a plan should have undone.

    Prompts compare by their text (a re-bound file with the same text is the same
    prompt) with trailing whitespace ignored; server-written fields are ignored.
    """
    before, after = graph_view(baseline_state), graph_view(final_state)
    for view in (before, after):
        for node_id, node in view["nodes"].items():
            for key in ("prompt_template", "system_prompt"):
                form, text = text_at(view, f"/nodes/{escape(node_id)}/options/{key}", read_text)
                if text is not None:
                    node["options"][key] = {"form": form, "text": text.rstrip()}
    changes = classify_changes(before, after, [])
    return changes.unrelated
