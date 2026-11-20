"""Edit plans for the Composer edit battery: loading, target resolution and per-edit verdicts.

An edit plan is a JSON file of small edits, each one ordinary chat text plus
the change it must make. A plan is data with no deployment details: it names
graph roles (``{source}``, ``{llm_inline}``) that are resolved against the
session's own graph when an attempt starts, and the resolved plan is saved
with the attempt so a resumed run judges exactly what was submitted.

The graph is compared through a *view* of the state response: sources,
nodes keyed by id, outputs keyed by name, metadata, and edges keyed by their
content (edge ids are server-assigned). Pointers in a plan are JSON pointers
into that view, for example ``/nodes/{llm_inline}/options/max_tokens``.

Every difference between the previous state and the new one must be covered
by one of the edit's expectations or by its ``may_change`` prefixes; any other
difference is an unrelated change and fails the edit.

Expectation forms (exactly one operator key each):

* ``{"equals": PTR, "value": V}``      -- the value at PTR is V (types exact)
* ``{"ends_with": PTR, "value": S}``   -- the string at PTR ends with S
* ``{"present": PTR}`` / ``{"absent": PTR}``
* ``{"restored": PTR}``                -- PTR is as it was when the attempt started; a value
  that was absent may be absent or null
* ``{"chain": {"from": A, "to": B, "via": [...]}}`` -- rows from A reach B through exactly
  the listed nodes, in order, each route having a single consumer
* ``{"text": PTR, "append": S}`` / ``{"text": PTR, "replace": [OLD, NEW]}`` /
  ``{"text": PTR, "remove": S}`` -- a prompt's text, inline or in a bound file,
  changed exactly so; an optional ``"form": "inline" | "file"`` pins where the
  text must live afterwards
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

PLAN_FORMAT = "elspeth-composer-edit-plan/1"
DEFAULT_PLAN = Path(__file__).resolve().parent / "edit_plans" / "composer_gold_50.json"

TARGET_KINDS = frozenset({"first_source", "consumer_of", "llm_node", "first_output"})
REVIEW_KINDS = frozenset({"llm_prompt_template"})
OPERATORS = ("equals", "ends_with", "present", "absent", "restored", "chain", "text")
TEXT_CHANGES = ("append", "replace", "remove")
_NAME = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_MESSAGE_FIELD = re.compile(r"\{was:((?:[^{}]|\{[a-z][a-z0-9_]*\})+)\}|\{([a-z][a-z0-9_]*)\}")
_PLAN_KEYS = frozenset({"format", "name", "description", "forbidden_tools", "targets", "edits"})
_EDIT_KEYS = frozenset({"id", "message", "expect", "may_change", "tools_any", "approve"})

type ReadText = Callable[[Mapping[str, Any]], str]


# ── graph view and pointers ────────────────────────────────────────────────


def graph_view(state: Mapping[str, Any]) -> JsonObject:
    """The authored graph of a state response, keyed for comparison."""
    edges: JsonObject = {}
    for edge in state["edges"]:
        key = f"{edge['from_node']}|{edge['to_node']}|{edge['edge_type']}|{edge['label']}"
        edges[key] = True
    return {
        "sources": copy.deepcopy(state["sources"]),
        "nodes": {node["id"]: copy.deepcopy(node) for node in state["nodes"]},
        "outputs": {output["name"]: copy.deepcopy(output) for output in state["outputs"]},
        "metadata": copy.deepcopy(state["metadata"]),
        "edges": edges,
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


# ── loading and resolving ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Plan:
    name: str
    sha256: str
    forbidden_tools: tuple[str, ...]
    targets: Mapping[str, JsonObject]
    edits: tuple[JsonObject, ...]


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
        _check_edit(edit, targets, seen)
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
    allowed = {"first_source": {"kind"}, "first_output": {"kind"}, "consumer_of": {"kind", "target"}, "llm_node": {"kind", "prompt"}}[kind]
    if set(spec) != allowed:
        raise UsageError(f"target {name!r} of kind {kind} takes exactly {sorted(allowed)}")
    if kind == "consumer_of" and (spec["target"] not in targets or list(targets).index(spec["target"]) >= list(targets).index(name)):
        raise UsageError(f"target {name!r} must name an earlier target")
    if kind == "llm_node" and spec["prompt"] not in ("inline", "file"):
        raise UsageError(f"target {name!r} prompt must be inline or file")


def _check_edit(edit: object, targets: Mapping[str, Any], seen: set[str]) -> None:
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
    for key in ("may_change", "tools_any"):
        if key in edit and (type(edit[key]) is not list or not all(type(item) is str for item in edit[key])):
            raise UsageError(f"edit {label}: {key} must be a list of strings")
    if "approve" in edit:
        approve = edit["approve"]
        if type(approve) is not dict or set(approve) != {"kind", "node"} or approve["kind"] not in REVIEW_KINDS:
            raise UsageError(f"edit {label}: approve must be {{kind, node}} with kind in {sorted(REVIEW_KINDS)}")
    for name in _NAME.findall(json.dumps(edit)):
        if name not in targets:
            raise UsageError(f"edit {label}: placeholder {{{name}}} is not a plan target")


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
    """Resolve each plan target on the graph; an override must name a fitting component."""
    view = graph_view(state)
    unknown = sorted(set(overrides) - set(plan.targets))
    if unknown:
        raise UsageError(f"--target names {unknown} are not targets of plan {plan.name}")
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for name, spec in plan.targets.items():
        value = overrides[name] if name in overrides else _find_target(spec, view, resolved)
        if value is None:
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
    return {"first_source": "a source", "first_output": "an output"}[kind]


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


def _find_target(spec: Mapping[str, Any], view: Mapping[str, Any], resolved: Mapping[str, str]) -> str | None:
    kind = spec["kind"]
    if kind == "first_source":
        return next(iter(view["sources"]), None)
    if kind == "first_output":
        return next(iter(view["outputs"]), None)
    if kind == "consumer_of":
        route = _route_of(view, resolved[spec["target"]]) if spec["target"] in resolved else None
        consumers = [node_id for node_id, node in view["nodes"].items() if route is not None and node["input"] == route]
        return consumers[0] if len(consumers) == 1 else None
    return next((node_id for node_id, node in view["nodes"].items() if _prompt_form(node) == spec["prompt"]), None)


def _fits(spec: Mapping[str, Any], view: Mapping[str, Any], resolved: Mapping[str, str], value: str) -> bool:
    kind = spec["kind"]
    if kind == "first_source":
        return value in view["sources"]
    if kind == "first_output":
        return value in view["outputs"]
    if kind == "consumer_of":
        return (
            value in view["nodes"]
            and spec["target"] in resolved
            and view["nodes"][value]["input"] == _route_of(view, resolved[spec["target"]])
        )
    return value in view["nodes"] and _prompt_form(view["nodes"][value]) == spec["prompt"]


def _fill(value: object, targets: Mapping[str, str], *, pointer: bool) -> Any:
    if isinstance(value, str):
        return _NAME.sub(lambda match: escape(targets[match.group(1)]) if pointer else targets[match.group(1)], value)
    if isinstance(value, list):
        return [_fill(item, targets, pointer=pointer) for item in value]
    if isinstance(value, dict):
        return {key: _fill(item, targets, pointer=pointer) for key, item in value.items()}
    return value


def _render_was(baseline: Mapping[str, Any], pointer: str) -> str:
    found, value = value_at(baseline, pointer)
    if not found:
        return "nothing (remove the setting so its default applies)"
    return json.dumps(value)


def resolve_edits(plan: Plan, targets: Mapping[str, str], baseline_state: Mapping[str, Any]) -> list[JsonObject]:
    """The plan's edits with targets and ``{was:...}`` values filled in, numbered from 1."""
    baseline = graph_view(baseline_state)
    resolved: list[JsonObject] = []
    for number, edit in enumerate(plan.edits, start=1):

        def field(match: re.Match[str]) -> str:
            if match.group(2) is not None:
                return targets[match.group(2)]
            return _render_was(baseline, _fill(match.group(1), targets, pointer=True))

        message = _MESSAGE_FIELD.sub(field, edit["message"])
        expect: list[JsonObject] = []
        for expectation in edit["expect"]:
            item: JsonObject = {}
            for key, value in expectation.items():
                if key == "chain" or key in ("value", "append", "replace", "remove", "form"):
                    item[key] = _fill(value, targets, pointer=False)
                else:
                    item[key] = _fill(value, targets, pointer=True)
            expect.append(item)
        resolved.append(
            {
                "number": number,
                "id": edit["id"],
                "message": message,
                "expect": expect,
                "may_change": _fill(edit["may_change"], targets, pointer=True) if "may_change" in edit else [],
                "tools_any": list(edit["tools_any"]) if "tools_any" in edit else [],
                "approve": _fill(edit["approve"], targets, pointer=False) if "approve" in edit else None,
            }
        )
    return resolved


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


def chain_paths(chain: Mapping[str, Any], view: Mapping[str, Any]) -> list[str]:
    """The routing pointers a chain expectation accounts for."""
    start = chain["from"]
    paths = [f"/sources/{escape(start)}/on_success" if start in view["sources"] else f"/nodes/{escape(start)}/on_success"]
    for node in chain["via"]:
        paths.extend([f"/nodes/{escape(node)}/input", f"/nodes/{escape(node)}/on_success"])
    if chain["to"] in view["nodes"]:
        paths.append(f"/nodes/{escape(chain['to'])}/input")
    return paths


def _chain_ok(chain: Mapping[str, Any], view: Mapping[str, Any]) -> bool:
    route = _route_of(view, chain["from"])
    for node in chain["via"]:
        if route is None or node not in view["nodes"] or view["nodes"][node]["input"] != route:
            return False
        if sum(1 for other in view["nodes"].values() if other["input"] == route) != 1:
            return False
        route = view["nodes"][node]["on_success"]
    if chain["to"] in view["nodes"]:
        return (
            route is not None
            and view["nodes"][chain["to"]]["input"] == route
            and sum(1 for other in view["nodes"].values() if other["input"] == route) == 1
        )
    return route is not None and route == chain["to"]


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
    for index, expectation in enumerate(edit["expect"]):
        label = f"expect[{index}]"
        if "chain" in expectation:
            covered.extend(chain_paths(expectation["chain"], before) + chain_paths(expectation["chain"], after))
            if not _chain_ok(expectation["chain"], after):
                failures.append(f"{label}:chain")
            continue
        operator = next(key for key in OPERATORS if key in expectation)
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
        elif operator == "restored":
            # A setting that was absent may come back as an explicit null: the
            # patch tools clear a key by sending null.
            was_found, was = value_at(baseline, pointer)
            ok = (found and not differences(was, value)) if was_found else (not found or value is None)
        else:
            _, before_text = text_at(before, pointer, read_text)
            form, after_text = text_at(after, pointer, read_text)
            ok = _text_ok(expectation, before_text, after_text) and ("form" not in expectation or form == expectation["form"])
        if not ok:
            failures.append(f"{label}:{operator}:{pointer}")
    return failures, covered


def unrelated_changes(before: Mapping[str, Any], after: Mapping[str, Any], allowed: Sequence[str]) -> list[str]:
    """Difference paths between two views that no allowed pointer accounts for."""
    return [path for path in differences(dict(before), dict(after)) if not any(covers(prefix, path) for prefix in allowed)]


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


def tool_findings(edit: Mapping[str, Any], evidence: Mapping[str, Any], forbidden: Sequence[str]) -> list[str]:
    """A forbidden tool that was applied, or none of the edit's expected tools used successfully."""
    outcomes: Mapping[str, Mapping[str, int]] = evidence["tool_outcomes"]
    findings: list[str] = []
    for tool in forbidden:
        if tool in outcomes and "applied" in outcomes[tool]:
            findings.append(f"forbidden_tool_applied:{tool}")
    expected = edit["tools_any"]
    if expected and not any(
        tool in outcomes and any(outcome in ("applied", "completed", "unstamped") for outcome in outcomes[tool]) for tool in expected
    ):
        findings.append("expected_tool_not_used:" + "|".join(expected))
    return findings
