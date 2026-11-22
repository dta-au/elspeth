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
which are allowed by name and reported as server-written. One more change
is allowed and reported (``own_description_corrected``): the turn rewriting
the existing description of a component an expectation's pointer targets,
so a description that names a fact the edit changed can stay true.

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
  values, only the listed option keys, and every other field unset except a
  ``description``, which is allowed and reported (``new_component_notes``)
* ``{"new_output": NAME, "fields": {...}, "one_of": {...}, "options": {...}, "option_keys": [...]}``
  -- the same for an output; ``options`` maps a key to ``{"equals": V}``,
  ``{"ends_with": S}`` or ``{"one_of": [...]}``
* ``{"renamed_node": {"from": A, "to": B}}`` -- A is gone and B carries every one of A's
  fields (plugin, options, description, error routing...) except its id and its place
  in the chain
* ``{"bound": PTR, "file": NAME}`` -- the binding at PTR (a source's ``options/blob_ref``,
  or an ``inline_content`` file marker) points at the session file NAME: the file the
  battery attached under that name, or, for a name the plan does not attach, a file the
  Composer created (created by the assistant); an optional ``"content"`` is the exact
  text that file must hold (trailing whitespace ignored)
* ``{"files": [NAME, ...]}`` -- the session files the whole graph points at, by file
  name and counted once per binding, are exactly these

File-binding plans (``composer_file_bindings.json``) add, all optional and unused by the
other plans: a plan ``start`` of ``"empty"`` (the attempt must start on an empty
pipeline), a plan ``pass_rule`` of ``"all_passed_as_found"`` (the process passes when
every edit passed and the graph is left as found, however few edits the plan has), and
per edit ``attach`` (files under ``edit_plans/files/`` the battery uploads, as the user
would, before sending the message), ``new_errors`` (validation error codes the edit is
expected to introduce, such as ``no_source_configured`` when it empties the pipeline)
and an ``approve`` ``card`` of ``"user_files"`` or ``"agent_written"`` (the card must be
there, and be of that kind: every prompt slot bound to a file the user uploaded, or
not).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts.staging.opslib import JsonObject, UsageError, differences

PLAN_FORMAT = "elspeth-composer-edit-plan/2"
PLAN_DIR = Path(__file__).resolve().parent / "edit_plans"
DEFAULT_PLAN = PLAN_DIR / "composer_gold_50.json"
MIXED_PLAN = PLAN_DIR / "composer_mixed_50.json"
FILE_BINDINGS_PLAN = PLAN_DIR / "composer_file_bindings.json"

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
# The two prompt review card kinds the Composer shows: every prompt slot bound to a file the
# user uploaded (``promptReviewIsUserSuppliedFiles`` in the frontend's promptTemplateDisplay.ts),
# or anything else (inline text, a file the Composer wrote).
CARD_KINDS = ("user_files", "agent_written")
OPERATORS = (
    "equals",
    "ends_with",
    "present",
    "absent",
    "restored",
    "chain",
    "text",
    "new_node",
    "new_output",
    "renamed_node",
    "bound",
    "files",
)
FILE_OPERATORS = ("bound", "files")
PLAN_STARTS = ("empty",)
PASS_RULES = ("gold", "all_passed_as_found")
ATTACHMENT_DIR = "files"
# The upload MIME type for each attachment extension (all in the server's ALLOWED_MIME_TYPES).
ATTACHMENT_MIME_TYPES: Mapping[str, str] = {
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".json": "application/json",
    ".jsonl": "application/x-jsonlines",
}
_ATTACHMENT_NAME = re.compile(r"[a-z0-9][a-z0-9_]*\.(?:csv|txt|json|jsonl)")
# What a session with no graph yet stands for. The server answers GET .../state with null
# until the first edit, and the Composer starts that edit from an empty state with the default
# pipeline metadata (``PipelineMetadata()``; test_edit_plan.py pins the two to each other).
EMPTY_STATE: Mapping[str, Any] = {
    "id": None,
    "sources": {},
    "nodes": [],
    "edges": [],
    "outputs": [],
    "metadata": {"name": "Untitled Pipeline", "description": ""},
    "is_valid": False,
    "validation_errors": None,
}
TEXT_CHANGES = ("append", "replace", "remove")
TEMPORARY_PREFIX = "battery_"
MARKER_TEXTS = ("Battery marker", "Battery file marker")

# Option keys the server writes itself when it reconciles review evidence on a
# model edit or resolves a prompt review card (``interpretation_state``,
# ``sessions/pending_interpretation``), and the authoring mark the source bind
# tools stamp. Allowed by name only, and reported as server-written.
SERVER_NODE_OPTION_KEYS = ("interpretation_requirements", "approved_prompt_artifact_hash")
SERVER_SOURCE_OPTION_KEYS = ("interpretation_requirements", "source_authoring")
# A file-held prompt's stored approval hash is cleared by the server whenever it
# reconciles that node (the run binds the hash from the admitted snapshot), so a
# non-null to null change of this key on a file-held step is server-written on
# any component. Any other change of it counts only where the edit names the node.
CLEARED_FILE_PROMPT_HASH_KEY = "approved_prompt_artifact_hash"
# LLM options an operator profile owns: a node that binds a ``profile`` can never
# author them (a subset of ``elspeth.core.llm_profiles.LLM_PROFILE_PRIVATE_FIELDS``).
# An edit whose every expectation sets one of them on a profile-bound node is not
# applicable to that graph, and is skipped like an edit missing a role.
PROFILE_OWNED_OPTION_KEYS = ("max_tokens", "temperature")
AUTO_WIRED_CONTROL_TERM = "required_control_auto_wired"
_ROUTING_FIELDS = ("id", "input", "on_success")

_NAME = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_MESSAGE_FIELD = re.compile(r"\{was:((?:[^{}]|\{[a-z][a-z0-9_]*\})+)\}|\{([a-z][a-z0-9_]*)\}")
_PLAN_KEYS = frozenset({"format", "name", "description", "forbidden_tools", "targets", "edits"})
_OPTIONAL_PLAN_KEYS = frozenset({"start", "pass_rule"})
_EDIT_KEYS = frozenset(
    {"id", "message", "expect", "may_change", "edges_touching", "tools_any", "tools_all", "approve", "attach", "new_errors"}
)
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
    """A loaded plan. ``verifier`` names a workload verifier that judges each edit in place of its expectations.

    A plan file has no verifier. A workload built in code (see
    :mod:`scripts.staging.structural_cycle`) carries one, with its arguments,
    and each of its edits carries what that verifier checks.
    """

    name: str
    sha256: str
    forbidden_tools: tuple[str, ...]
    targets: Mapping[str, JsonObject]
    edits: tuple[JsonObject, ...]
    verifier: str | None = None
    verifier_args: Mapping[str, Any] = field(default_factory=dict)
    # File-binding plans only (see the module docstring): the start the plan needs, how the
    # process decides a pass, and the bytes of every file its edits attach, by file name.
    start: str | None = None
    pass_rule: str = "gold"
    attachments: Mapping[str, bytes] = field(default_factory=dict)

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
    if type(raw) is not dict or not _PLAN_KEYS <= set(raw) <= _PLAN_KEYS | _OPTIONAL_PLAN_KEYS or raw["format"] != PLAN_FORMAT:
        raise UsageError(
            f"plan {path} must be an object with exactly {sorted(_PLAN_KEYS)} (and optionally {sorted(_OPTIONAL_PLAN_KEYS)}) "
            f"and format {PLAN_FORMAT}"
        )
    if "start" in raw and raw["start"] not in PLAN_STARTS:
        raise UsageError(f"plan start must be one of {list(PLAN_STARTS)}")
    if "pass_rule" in raw and raw["pass_rule"] not in PASS_RULES:
        raise UsageError(f"plan pass_rule must be one of {list(PASS_RULES)}")
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
    attachments = _load_attachments(path, edits)
    return Plan(
        name=raw["name"],
        sha256=_plan_sha256(data, attachments),
        forbidden_tools=tuple(forbidden),
        targets=targets,
        edits=tuple(edits),
        start=raw["start"] if "start" in raw else None,
        pass_rule=raw["pass_rule"] if "pass_rule" in raw else "gold",
        attachments=attachments,
    )


def _plan_sha256(data: bytes, attachments: Mapping[str, bytes]) -> str:
    """The plan file's SHA-256; with attachments, over the plan and every attached file, so a changed file is a different plan."""
    if not attachments:
        return hashlib.sha256(data).hexdigest()
    digest = hashlib.sha256(data)
    for name in sorted(attachments):
        digest.update(b"\0" + name.encode() + b"\0" + hashlib.sha256(attachments[name]).digest())
    return digest.hexdigest()


def _load_attachments(path: Path, edits: Sequence[Mapping[str, Any]]) -> dict[str, bytes]:
    """Read every file the plan's edits attach, from ``files/`` beside the plan; each name is attached by one edit only."""
    attachments: dict[str, bytes] = {}
    for edit in edits:
        for name in edit["attach"] if "attach" in edit else []:
            if name in attachments:
                raise UsageError(f"edit {edit['id']}: {name} is attached by an earlier edit; each file is uploaded once")
            try:
                attachments[name] = (path.parent / ATTACHMENT_DIR / name).read_bytes()
            except OSError as exc:
                raise UsageError(f"edit {edit['id']}: cannot read attachment {ATTACHMENT_DIR}/{name}: {exc.strerror}") from None
    return attachments


def attachment_mime_type(name: str) -> str:
    return ATTACHMENT_MIME_TYPES[Path(name).suffix]


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
        if (
            type(approve) is not dict
            or not {"kind", "node"} <= set(approve) <= {"kind", "node", "card"}
            or approve["kind"] not in REVIEW_KINDS
            or ("card" in approve and approve["card"] not in CARD_KINDS)
        ):
            raise UsageError(
                f"edit {label}: approve must be {{kind, node}} with kind in {sorted(REVIEW_KINDS)}, and an optional card in {list(CARD_KINDS)}"
            )
    if "attach" in edit:
        names = edit["attach"]
        if type(names) is not list or not names or not all(type(name) is str and _ATTACHMENT_NAME.fullmatch(name) for name in names):
            raise UsageError(f"edit {label}: attach must list file names such as orders_a.csv (lowercase; .csv, .txt, .json or .jsonl)")
        if len(set(names)) != len(names):
            raise UsageError(f"edit {label}: attach names a file twice")
    if "new_errors" in edit and (
        type(edit["new_errors"]) is not list or not edit["new_errors"] or not all(type(code) is str and code for code in edit["new_errors"])
    ):
        raise UsageError(f"edit {label}: new_errors must be a non-empty list of validation error codes")
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
    elif operator == "bound":
        valid = (
            {"bound", "file"} <= keys <= {"bound", "file", "content"}
            and type(expectation["bound"]) is str
            and type(expectation["file"]) is str
            and bool(_ATTACHMENT_NAME.fullmatch(expectation["file"]))
            and ("content" not in expectation or type(expectation["content"]) is str)
        )
    elif operator == "files":
        valid = keys == {"files"} and type(expectation["files"]) is list and all(type(name) is str for name in expectation["files"])
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


def _is_file_held_prompt(value: Any) -> bool:
    return (
        type(value) is dict
        and "blob_ref" in value
        and type(value["blob_ref"]) is str
        and "mode" in value
        and value["mode"] == "inline_content"
    )


def _approval_hash_deferred(node: Mapping[str, Any]) -> bool:
    """True when the server stores no approved prompt hash for this step because a prompt is held in a file.

    Mirrors ``approved_prompt_artifact_hash_from_options`` in ``elspeth.web.interpretation_state``
    (which returns ``None`` for these options); ``test_edit_plan.py`` pins the two to each other. A
    step's prompts are its ``system_prompt``, its ``queries`` (the mapping or list, each entry and each
    entry's ``template``) and its ``prompt_template`` when a query falls back to it (no queries, or a
    query without a template).
    """
    options = node["options"]
    if node["node_type"] != "transform":
        return False
    values = [options[key] for key in ("system_prompt", "queries") if key in options]
    queries = options["queries"] if "queries" in options else None
    listed = queries.values() if type(queries) is dict else queries if type(queries) is list else ()
    entries = [entry for entry in listed if type(entry) is dict]
    if (not entries or any("template" not in entry or entry["template"] is None for entry in entries)) and "prompt_template" in options:
        values.append(options["prompt_template"])
    for entry in entries:
        values.append(entry)
        if "template" in entry:
            values.append(entry["template"])
    return any(_is_file_held_prompt(value) for value in values)


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


_POINTER_KEYS = frozenset({"equals", "ends_with", "present", "absent", "restored", "text", "bound"})


def profile_owned_pointers(expect: Sequence[Mapping[str, Any]], baseline: Mapping[str, Any]) -> list[str]:
    """The pointers of an edit whose EVERY expectation sets a profile-owned option on a profile-bound node, else ``[]``.

    Such an edit (and the edit that restores the same option) can never pass on
    that graph: the server refuses the option on a node that binds a profile.
    """
    pointers: list[str] = []
    for expectation in expect:
        operator = next(key for key in OPERATORS if key in expectation)
        if operator not in ("equals", "restored"):
            return []
        parts = _segments(expectation[operator])
        if len(parts) != 4 or parts[0] != "nodes" or parts[2] != "options" or parts[3] not in PROFILE_OWNED_OPTION_KEYS:
            return []
        if parts[1] not in baseline["nodes"]:
            return []
        options = baseline["nodes"][parts[1]]["options"]
        if "profile" not in options or type(options["profile"]) is not str or not options["profile"]:
            return []
        pointers.append(expectation[operator])
    return pointers


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

        def fill_field(match: re.Match[str]) -> str:
            if match.group(2) is not None:
                return targets[match.group(2)]
            return render_was(baseline, _fill(match.group(1), targets, pointer=True))

        expect: list[JsonObject] = []
        for expectation in edit["expect"]:
            expect.append({key: _fill(value, targets, pointer=key in _POINTER_KEYS) for key, value in expectation.items()})
        owned = profile_owned_pointers(expect, baseline)
        if owned:
            resolved.append({"number": number, "id": edit["id"], "applicable": False, "missing_roles": [], "profile_owned": owned})
            continue
        resolved.append(
            {
                "number": number,
                "id": edit["id"],
                "applicable": True,
                "message": _MESSAGE_FIELD.sub(fill_field, edit["message"]),
                "expect": expect,
                "may_change": _fill(edit["may_change"], targets, pointer=True) if "may_change" in edit else [],
                "edges_touching": _fill(edit["edges_touching"], targets, pointer=False) if "edges_touching" in edit else [],
                "tools_any": list(edit["tools_any"]) if "tools_any" in edit else [],
                "tools_all": list(edit["tools_all"]) if "tools_all" in edit else [],
                "approve": _fill(edit["approve"], targets, pointer=False) if "approve" in edit else None,
            }
        )
        for key in ("attach", "new_errors"):
            if key in edit:
                resolved[-1][key] = list(edit[key])
        if "order" in edit:
            resolved[-1]["order"] = _fill(edit["order"], targets, pointer=False)
        if "position" in edit:
            resolved[-1]["position"] = dict(edit["position"])
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
        elif "files" in expectation:
            continue
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
    The one exception: a file-held prompt step's approval hash going from a
    value to null is server-written on any component (the server clears it
    when it reconciles the node). A change to another value, or on an inline
    prompt step, still counts only where the edit names the node.
    """
    auto = auto_wired_controls(before, after)
    auto_routes = {route for node_id in auto for route in (after["nodes"][node_id]["input"], after["nodes"][node_id]["on_success"])}

    def _file_prompt_hash_cleared(path: str) -> bool:
        # Non-null before, null or absent after, on a step with a prompt held in a file (before and after):
        # a prompt template, a system prompt, or a multi-query step's query templates.
        node_id = _segments(path)[1]
        if node_id not in before["nodes"] or node_id not in after["nodes"]:
            return False
        if not _approval_hash_deferred(before["nodes"][node_id]) or not _approval_hash_deferred(after["nodes"][node_id]):
            return False
        was_found, was = value_at(before, path)
        now_found, now = value_at(after, path)
        return was_found and was is not None and (not now_found or now is None)

    def server_written(path: str) -> bool:
        parts = _segments(path)
        collection = parts[0]
        if collection == "nodes" and len(parts) >= 2 and parts[1] in auto:
            return True
        if len(parts) >= 4 and parts[2] == "options":
            if collection == "nodes" and len(parts) == 4 and parts[3] == CLEARED_FILE_PROMPT_HASH_KEY and _file_prompt_hash_cleared(path):
                return True
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
    # A description on the component the edit creates is allowed and reported (new_component_notes).
    exempt = {*fields, *one_of, "options", "name", "description", *_ROUTING_FIELDS}
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


def new_component_notes(edit: Mapping[str, Any], before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """What a passing new step or output carries beyond what the edit asked for, reported (never failed).

    ``new_component_described:PTR`` -- the model gave the component it created a description;
    ``new_output_path_placed:PTR`` -- the output's path ends with the asked file name but is
    not that bare name (the server writes sink paths into the session's output folder).
    """
    notes: list[str] = []
    for expectation in edit["expect"]:
        operator = next(key for key in OPERATORS if key in expectation)
        if operator not in ("new_node", "new_output"):
            continue
        collection = "nodes" if operator == "new_node" else "outputs"
        name = expectation[operator]
        if name in before[collection] or name not in after[collection]:
            continue
        item = after[collection][name]
        pointer = f"/{collection}/{escape(name)}"
        if "description" not in expectation["fields"] and "description" in item and not _empty(item["description"]):
            notes.append(f"new_component_described:{pointer}/description")
        rules: Mapping[str, Mapping[str, Any]] = expectation["options"] if "options" in expectation else {}
        placed = "path" in rules and "ends_with" in rules["path"] and "path" in item["options"]
        if placed and item["options"]["path"] != rules["path"]["ends_with"]:
            notes.append(f"new_output_path_placed:{pointer}/options/path")
    return notes


_POINTER_OPERATORS = ("equals", "ends_with", "present", "absent", "restored", "text", "bound")


def targeted_components(edit: Mapping[str, Any]) -> set[tuple[str, str]]:
    """The ``(collection, id)`` of each existing component an expectation's own pointer points inside.

    Only an expectation pointer below a component counts (``/outputs/x/options/path`` targets output
    ``x``). A chain, a new or renamed step, ``may_change``, ``edges_touching`` and the node whose card the
    edit approves do not make a component a target.
    """
    found: set[tuple[str, str]] = set()
    for expectation in edit["expect"]:
        operator = next(key for key in OPERATORS if key in expectation)
        if operator not in _POINTER_OPERATORS:
            continue
        parts = _segments(expectation[operator])
        if len(parts) >= 3 and parts[0] in ("sources", "nodes", "outputs"):
            found.add((parts[0], parts[1]))
    return found


def own_description_corrections(edit: Mapping[str, Any], before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """``own_description_corrected:PTR`` for each target whose existing description the turn rewrote.

    A target is a component the edit changes (:func:`targeted_components`). When the turn rewrites that
    component's own description, from one text to another, the change is allowed and reported here, never
    hidden: changing a fact a description states (an output's file name) and keeping the description true
    is the right result. A description added where there was none, or removed, is not a correction and
    stays an unrelated change; so does any other component's description, the pipeline metadata, and
    every option and route.
    """
    notes: list[str] = []
    for collection, name in sorted(targeted_components(edit)):
        if name not in before[collection] or name not in after[collection]:
            continue
        old, new = before[collection][name]["description"], after[collection][name]["description"]
        if not _empty(old) and not _empty(new) and old != new:
            notes.append(f"own_description_corrected:/{collection}/{escape(name)}/description")
    return notes


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


@dataclass(frozen=True, slots=True)
class FileContext:
    """What a file expectation is judged against: the files the battery attached, and the session's files.

    ``uploads`` maps each attached file name to the id the upload returned; ``listing``
    maps each session file id to its listing row (``filename``, ``created_by``...).
    """

    uploads: Mapping[str, str]
    listing: Mapping[str, Mapping[str, Any]]


def uses_files(edit: Mapping[str, Any]) -> bool:
    """Whether an edit has an expectation judged against the session's files."""
    return any(operator in expectation for expectation in edit["expect"] for operator in FILE_OPERATORS)


def bound_blob(value: object) -> str | None:
    """The file id a binding holds: a source's ``blob_ref`` string, or an ``inline_content`` marker's ``blob_ref``."""
    if type(value) is str and value:
        return value
    if type(value) is dict and _is_file_held_prompt(value):
        blob: str = value["blob_ref"]
        return blob
    return None


def referenced_blob_ids(view: Mapping[str, Any]) -> list[str]:
    """Every file id the graph's sources, nodes and outputs point at, once per binding (``blob_ref`` and ``blob_id`` keys)."""
    found: list[str] = []

    def walk(value: object) -> None:
        if type(value) is dict:
            for key, child in value.items():
                if key in ("blob_ref", "blob_id") and type(child) is str:
                    found.append(child)
                else:
                    walk(child)
        elif type(value) is list:
            for child in value:
                walk(child)

    walk([view["sources"], view["nodes"], view["outputs"]])
    return found


def referenced_file_names(view: Mapping[str, Any], files: FileContext) -> list[str]:
    """The names of the files the graph points at, sorted; a file missing from the session reads ``missing:<id>``."""
    return sorted(files.listing[blob]["filename"] if blob in files.listing else f"missing:{blob}" for blob in referenced_blob_ids(view))


def _bound_ok(expectation: Mapping[str, Any], view: Mapping[str, Any], files: FileContext, read_text: ReadText) -> bool:
    found, value = value_at(view, expectation["bound"])
    blob = bound_blob(value) if found else None
    if blob is None:
        return False
    name = expectation["file"]
    if name in files.uploads:
        ok = blob == files.uploads[name]
    else:
        # A file the Composer created: the name asked for, written by the assistant, not one the battery uploaded.
        row = files.listing[blob] if blob in files.listing else None
        ok = row is not None and row["filename"] == name and row["created_by"] == "assistant" and blob not in set(files.uploads.values())
    if ok and "content" in expectation:
        form, text = text_at(view, expectation["bound"], read_text)
        ok = form == "file" and text is not None and text.rstrip() == expectation["content"].rstrip()
    return ok


def _edge_paths(view: Mapping[str, Any], components: Sequence[str]) -> list[str]:
    wanted = set(components)
    return ["/edges/" + escape(key) for key in view["edges"] if set(key.split("|")[:2]) & wanted]


def check_expectations(
    edit: Mapping[str, Any],
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    baseline: Mapping[str, Any],
    read_text: ReadText,
    *,
    files: FileContext | None = None,
) -> tuple[list[str], list[str]]:
    """``(failures, covered pointers)``: each failure names the expectation that did not hold.

    ``files`` is needed only by the file expectations (``bound``, ``files``); without it they fail.
    """
    failures: list[str] = []
    covered: list[str] = []
    transparent = auto_wired_controls(before, after)
    touching = list(edit["edges_touching"])
    for index, expectation in enumerate(edit["expect"]):
        label = f"expect[{index}]"
        operator = next(key for key in OPERATORS if key in expectation)
        if operator == "files":
            pointer = "files"
            ok = files is not None and referenced_file_names(after, files) == sorted(expectation["files"])
        elif operator == "bound":
            pointer = expectation["bound"]
            covered.append(pointer)
            parts = _segments(pointer)
            if len(parts) == 4 and parts[0] == "sources" and parts[2] == "options" and parts[3] == "blob_ref":
                # The bind tool writes the file's storage path beside its id (the state view shows it redacted).
                covered.append(f"/sources/{escape(parts[1])}/options/path")
            ok = files is not None and _bound_ok(expectation, after, files, read_text)
        elif operator == "chain":
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


def preconditions(
    edit: Mapping[str, Any],
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
    read_text: ReadText,
    *,
    files: FileContext | None = None,
) -> list[str]:
    """Expectations that already hold on the current graph: the edit would pass with nothing changed."""
    failures, _ = check_expectations(edit, current, current, baseline, read_text, files=files)
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


_COMPONENT_KIND_PREFIXES = ("node:", "output:", "sink:", "source:")
_REVIEW_PENDING_CODE = "interpretation_review_pending"


def new_validation_errors(before_state: Mapping[str, Any], after_state: Mapping[str, Any]) -> list[str]:
    """Validation errors present after the edit that were not present before it (by code, component and message).

    The server names the same component either bare (``cs1_scoring``) or with its kind (``node:cs1_scoring``,
    ``output:results``) depending on which validation path produced the entry, so components are compared
    without that prefix: one error re-spelt between two reads is not a new error.

    A pending interpretation review is stored with either a sentence ("llm_prompt_template review pending for
    transform 'x': ...") or the bare review kind ("llm_prompt_template"), again by write path, so those errors are
    compared by the review kind (the message's first word) instead of the message. A pending review of another
    kind, or on another component, is still new.
    """

    def component(raw: str | None) -> str | None:
        if raw is None:
            return None
        for prefix in _COMPONENT_KIND_PREFIXES:
            if raw.startswith(prefix):
                return raw[len(prefix) :]
        return raw

    def detail(error: Mapping[str, Any]) -> Any:
        message = error["message"]
        if error["error_code"] == _REVIEW_PENDING_CODE and type(message) is str and message.split():
            return message.split()[0]
        return message

    def identity(error: Mapping[str, Any]) -> str:
        return json.dumps([error["error_code"], component(error["component"]), detail(error)])

    remaining = [identity(error) for error in before_state["validation_errors"] or []]
    added: list[str] = []
    for error in after_state["validation_errors"] or []:
        key = identity(error)
        if key in remaining:
            remaining.remove(key)
        else:
            # Reported as the server stored it, so a finding still shows the full message.
            added.append(json.dumps([error["error_code"], component(error["component"]), error["message"]]))
    return added


def expected_new_error(edit: Mapping[str, Any], key: str) -> bool:
    """Whether a ``new_validation_errors`` key carries a code the edit lists in ``new_errors``."""
    return "new_errors" in edit and json.loads(key)[0] in edit["new_errors"]


def empty_state() -> JsonObject:
    """A fresh copy of :data:`EMPTY_STATE`, for a session whose state read answers null."""
    return copy.deepcopy(dict(EMPTY_STATE))


def is_empty_graph(state: Mapping[str, Any]) -> bool:
    """No source, no step and no output."""
    return not state["sources"] and not state["nodes"] and not state["outputs"]


def _prompt_slots(surface: Mapping[str, Any]) -> dict[str, object]:
    """The filled prompt slots of a review's authored surface, by the field path a card binding carries."""
    slots: dict[str, object] = {}
    for key in ("prompt_template", "system_prompt"):
        if key in surface and surface[key] is not None:
            slots[key] = surface[key]
    queries = surface["queries"] if "queries" in surface else None
    entries = list(queries.items()) if type(queries) is dict else list(enumerate(queries)) if type(queries) is list else []
    for name, entry in entries:
        if type(entry) is dict and "template" in entry and entry["template"] is not None:
            slots[f"queries.{name}.template"] = entry["template"]
    return slots


def card_kind(snapshot: Mapping[str, Any]) -> str:
    """``user_files`` when every filled prompt slot is bound to a file the user uploaded, else ``agent_written``.

    The frontend's ``promptReviewIsUserSuppliedFiles`` rule: a live review with
    bindings, one binding per filled slot and no unbound slot, every binding
    created by the user with the ``verbatim`` modality.
    """
    bindings = snapshot["bindings"]
    if snapshot["is_live_review"] is not True or type(bindings) is not list or not bindings:
        return "agent_written"
    paths = [binding["field_path"] for binding in bindings]
    slots = _prompt_slots(snapshot["authored_surface"])
    user = all(binding["created_by"] == "user" and binding["creation_modality"] == "verbatim" for binding in bindings)
    exact = len(set(paths)) == len(paths) and set(paths) == set(slots) and all(type(value) is dict for value in slots.values())
    return "user_files" if user and exact else "agent_written"


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
