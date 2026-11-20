"""Generate a large, wired-up Composer test graph for the edit battery.

The fixture is synthetic: every name, field and word is made up here from a
seed. It is built to resemble the large graphs operators edit -- a CSV source
with a fixed schema bound to an uploaded file, a ``file_inject`` step with
file-held content, two assessment chains (an assessment LLM step feeding a
multi-query scoring step), further LLM review and scoring steps, a gate to a
moderation queue, a final ``field_mapper`` and a main sink that declares its
fields, plus an error sink. Later prompts read fields that earlier steps
produce, and every LLM step declares ``required_input_fields``.

Prompt text lives in FILES bound to the steps: each single-prompt LLM step has
a system and a user prompt file, each multi-query step a system prompt file
(the rubric), and the ``file_inject`` step one file per entry. Query templates
cannot be file-held, so they stay short and inline.

Two presets:

``acceptance``
    The realistic graph (defaults: 27 steps, 12 LLM steps, about 200 KB of
    prompt text in files). The manifest lists three anchor positions -- after
    the source, mid-chain between two LLM steps, and at the final mapper --
    for the structural-cycle workload to rotate through.
``single-node``
    A like-for-like copy of the shape measured on a customer deployment: one
    source ``source``, one LLM node ``capacity_probe`` with 32 queries of
    15,600 bytes each held inline (about 500 KB, as that fixture's were), a
    small system prompt file, a main sink ``results`` and an error sink.

``--queries-per-llm-step`` (and, for ``single-node``, ``--query-bytes``)
builds graphs of several hundred queries on purpose. The manifest's ``state_scan_values`` is the number of values the
server's whole-state credential scan visits for the imported graph, measured
with that scan; the server refuses a state over 4,096 of them.

The output directory holds ``pipeline.yaml`` (the import document),
``files/`` (each file to upload) and ``manifest.json`` (what to upload, where
each file binds, the positions and the measured sizes). The battery's
``setup`` action creates the session's graph from it through the normal
authenticated API: it uploads the files, imports the YAML with the bindings
(``POST /api/sessions/{id}/state/yaml``) and resolves the prompt reviews after
checking each against the files. Nothing is written to a server here.

The ``--file-inject`` switch chooses how the ``file_inject`` content is held:
``file`` (default) binds uploaded files; ``omit`` leaves the step out and
gives its share of the prompt bytes to the other prompt files. The composer
refuses ``file_inject`` content that is not an uploaded file, so there is no
inline form. ``omit`` exists for servers where a file-held ``file_inject``
makes authoring validation lose every upstream field ("Producer guarantees:
(none)"); see the runbook.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml

from scripts.staging.opslib import REPO_ROOT, JsonObject, OperationFailed, UsageError, emit, run_tool

if TYPE_CHECKING:
    from elspeth.web.composer.state import CompositionState
from scripts.staging.structural_cycle import Position

MANIFEST_VERSION = 1
# Under the credential scan's width limit (512 entries in one mapping), so only its value count is in play.
MAX_QUERIES = 500
SCAN_CEILING = 1_000_000
type InjectHolding = Literal["file", "omit"]
type Preset = Literal["acceptance", "single-node"]

# 14 source fields, all text: the CSV source declares a fixed schema of exactly these.
SOURCE_FIELDS = (
    "submission_id",
    "applicant_region",
    "programme",
    "submitted_on",
    "cs1_background",
    "cs1_findings",
    "cs1_history",
    "cs1_plan",
    "cs2_background",
    "cs2_findings",
    "cs2_history",
    "cs2_plan",
    "reviewer_notes",
    "word_count",
)
CRITERIA = (
    "accuracy",
    "reasoning",
    "safety",
    "communication",
    "evidence",
    "completeness",
    "ethics",
    "feasibility",
    "clarity",
    "impact",
    "consistency",
    "follow_up",
)
# Neutral synthetic vocabulary for filler text; no real names or data.
_WORDS = (
    "assess",
    "criterion",
    "evidence",
    "outcome",
    "review",
    "panel",
    "rubric",
    "score",
    "finding",
    "context",
    "consider",
    "weight",
    "record",
    "justify",
    "summary",
    "observation",
    "standard",
    "threshold",
    "case",
    "plan",
    "history",
    "risk",
    "benefit",
    "follow",
    "clear",
    "concise",
    "relevant",
    "complete",
    "consistent",
    "reason",
    "explain",
    "note",
    "band",
    "level",
    "guidance",
    "example",
    "describe",
    "compare",
    "measure",
    "the",
    "and",
    "of",
    "for",
    "each",
    "with",
    "to",
    "a",
    "is",
    "in",
)


@dataclass(frozen=True, slots=True)
class FixtureFile:
    """One file to upload; ``slot`` is the inline-binding path it fills, ``None`` for the source data."""

    name: str
    slot: str | None
    mime_type: str
    content: bytes


class _BlockDumper(yaml.SafeDumper):
    """Writes multi-line text as literal blocks (``|``), as a person writes prompts in YAML, not as wrapped quoted strings."""


def _represent_text(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


_BlockDumper.add_representer(str, _represent_text)


@dataclass(frozen=True, slots=True)
class FixtureSpec:
    preset: Preset = "acceptance"
    seed: int = 1
    steps: int = 27
    llm_steps: int = 12
    queries_per_llm_step: int = 3
    large_queries: int = 8
    query_bytes: int = 15_600
    prompt_bytes: int = 200_000
    inject_files: int = 2
    file_inject: InjectHolding = "file"
    profile: str = "task-role"
    rows: int = 3


@dataclass(frozen=True, slots=True)
class Fixture:
    spec: FixtureSpec
    pipeline: JsonObject
    source_name: str
    source_file: FixtureFile
    files: tuple[FixtureFile, ...]
    sink_paths: Mapping[str, str]
    positions: tuple[Position, ...]
    inline_bytes: int = 0
    llm_steps: tuple[str, ...] = field(default_factory=tuple)

    @property
    def yaml_text(self) -> str:
        text: str = yaml.dump(self.pipeline, Dumper=_BlockDumper, sort_keys=False, allow_unicode=False, width=120)
        return text

    def import_body(self, blob_ids: Mapping[str, str]) -> JsonObject:
        """The ``POST /state/yaml`` body once each file named in the manifest is uploaded as ``blob_ids[name]``."""
        return {
            "yaml": self.yaml_text,
            "source_blob_ids": {self.source_name: blob_ids[self.source_file.name]},
            "inline_blob_bindings": {file.slot: {"blob_id": blob_ids[file.name]} for file in self.files if file.slot is not None},
            "sink_path_bindings": {name: {"path": {"folder": "outputs", "relative_path": path}} for name, path in self.sink_paths.items()},
        }

    def measures(self) -> JsonObject:
        transforms = self.pipeline["transforms"] if "transforms" in self.pipeline else []
        gates = self.pipeline["gates"] if "gates" in self.pipeline else []
        multi = [step for step in transforms if step["plugin"] == "llm" and "queries" in step["options"]]
        return {
            "steps": len(transforms) + len(gates),
            "llm_steps": len(self.llm_steps),
            "multi_query_steps": len(multi),
            "queries": sum(len(step["options"]["queries"]) for step in multi),
            "sources": len(self.pipeline["sources"]),
            "sinks": len(self.pipeline["sinks"]),
            "files": len(self.files),
            "prompt_bytes_in_files": sum(len(file.content) for file in self.files),
            "inline_prompt_bytes": self.inline_bytes,
            "source_file_bytes": len(self.source_file.content),
            "yaml_bytes": len(self.yaml_text.encode()),
        }


# ── text ───────────────────────────────────────────────────────────────────


def _text(rng: random.Random, size: int, head: str, *, sentences_per_line: int = 1) -> str:
    """``head`` then synthetic sentences, ``sentences_per_line`` to a line; exactly ``size`` bytes of ASCII ending in a newline."""
    parts = [head.rstrip("\n"), ""]
    length = len(parts[0]) + 1
    while length < size:
        sentences = [" ".join(rng.choice(_WORDS) for _ in range(rng.randint(8, 18))).capitalize() + "." for _ in range(sentences_per_line)]
        line = " ".join(sentences)
        parts.append(line)
        length += len(line) + 1
    text = "\n".join(parts)
    if len(head) + 1 > size:
        raise UsageError(f"a prompt of {size} bytes cannot hold its {len(head)}-byte header; raise --prompt-bytes")
    body = text[: size - 1]
    # Never end a line on a space: YAML cannot hold such text as a literal block.
    if body.endswith(" "):
        body = body[:-1] + "."
    return body + "\n"


def _refs(fields: Sequence[str]) -> str:
    return "\n".join(f"{name.replace('_', ' ').title()}: {{{{ row['{name}'] }}}}" for name in fields)


def _sizes(rng: random.Random, weights: Sequence[float], total: int) -> list[int]:
    """Split ``total`` bytes by ``weights`` with a seeded +-15% spread; the parts sum to exactly ``total``."""
    jittered = [weight * rng.uniform(0.85, 1.15) for weight in weights]
    scale = total / sum(jittered)
    sizes = [max(400, int(weight * scale)) for weight in jittered]
    sizes[-1] += total - sum(sizes)
    if sizes[-1] < 400:
        raise UsageError("--prompt-bytes is too small for this many prompt files")
    return sizes


# ── the acceptance graph ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Role:
    name: str
    reads: tuple[str, ...]
    kind: Literal["single", "multi", "large"]
    prefix: str = ""


def _roles(count: int) -> list[_Role]:
    """The LLM steps in dependency order: each reads only source fields and what earlier roles wrote."""
    base = [
        _Role("cs1_assessment", ("cs1_background", "cs1_findings", "cs1_history", "cs1_plan", "framework"), "single"),
        _Role("cs1_scoring", ("cs1_assessment", "cs1_plan"), "multi", "cs1"),
        _Role("cs2_assessment", ("cs2_background", "cs2_findings", "cs2_history", "cs2_plan", "framework"), "single"),
        _Role("cs2_scoring", ("cs2_assessment", "cs2_plan"), "multi", "cs2"),
        _Role("cs1_evidence_check", ("cs1_assessment", "cs1_findings"), "single"),
        _Role("cs2_evidence_check", ("cs2_assessment", "cs2_findings"), "single"),
        _Role("cross_case_comparison", ("cs1_assessment", "cs2_assessment", "cs1_evidence_check", "cs2_evidence_check"), "single"),
        _Role("risk_review", ("cross_case_comparison", "reviewer_notes"), "multi", "risk"),
        _Role("equity_review", ("applicant_region", "programme", "cross_case_comparison"), "single"),
        _Role("overall_rubric", ("cs1_assessment", "cs2_assessment", "cross_case_comparison", "equity_review"), "large", "overall"),
        _Role("moderation_summary", ("cross_case_comparison", "equity_review", f"risk_{CRITERIA[0]}_score"), "single"),
        _Role("feedback_letter", ("submission_id", "moderation_summary"), "single"),
    ]
    roles = base[:count]
    for extra in range(count - len(base)):
        roles.insert(len(roles) - 1, _Role(f"additional_review_{extra + 1}", ("moderation_summary",), "single"))
    return roles


def _query_names(prefix: str, count: int) -> list[str]:
    """``count`` query names: the criteria in order, then numbered rounds of them."""
    return [
        f"{prefix}_{CRITERIA[index % len(CRITERIA)]}" + ("" if index < len(CRITERIA) else f"_{index // len(CRITERIA) + 1}")
        for index in range(count)
    ]


def _multi_query_options(role: _Role, queries: int) -> tuple[JsonObject, list[str], int]:
    names = _query_names(role.prefix, queries)
    inputs = {f"input_{index + 1}": name for index, name in enumerate(role.reads)}
    placeholders = " ".join(f"{{{{ row.input_{index + 1} }}}}" for index in range(len(role.reads)))
    definitions: JsonObject = {}
    inline = 0
    for name in names:
        criterion = name.removeprefix(role.prefix + "_").rstrip("_0123456789").replace("_", " ")
        template = f"Score the {criterion} criterion from 0 to 100 using the rubric. Material: {placeholders}\n"
        inline += len(template.encode())
        definitions[name] = {
            "input_fields": dict(inputs),
            "template": template,
            "output_fields": [{"suffix": "score", "type": "integer"}, {"suffix": "rationale", "type": "string"}],
        }
    options: JsonObject = {"required_input_fields": list(role.reads), "queries": definitions}
    return options, [f"{name}_score" for name in names] + [f"{name}_rationale" for name in names], inline


def _acceptance(spec: FixtureSpec) -> Fixture:
    if not 1 <= spec.llm_steps <= 40:
        raise UsageError("--llm-steps must be between 1 and 40")
    rng = random.Random(spec.seed)
    roles = _roles(spec.llm_steps)
    inject = spec.file_inject == "file"
    if not inject:
        roles = [_Role(role.name, tuple(name for name in role.reads if name != "framework"), role.kind, role.prefix) for role in roles]
    gate = any(role.name == "overall_rubric" for role in roles)
    fixed = 1 + (1 if inject else 0) + (1 if gate else 0)  # output mapper, file_inject, gate
    fillers = spec.steps - len(roles) - fixed
    if fillers < 0:
        raise UsageError(f"--steps must be at least {len(roles) + fixed} for {len(roles)} LLM steps")

    # Prompt files: system + user per single step, a rubric per multi-query step, and the injected files.
    plan: list[tuple[str, str, float]] = []  # (file name, slot, weight)
    for role in roles:
        if role.kind == "single":
            plan.append((f"{role.name}.system.md", f"node:{role.name}.options.system_prompt", 0.7))
            plan.append((f"{role.name}.user.md", f"node:{role.name}.options.prompt_template", 0.7))
        else:
            plan.append((f"{role.name}.rubric.md", f"node:{role.name}.options.system_prompt", 4.2 if role.kind == "large" else 3.5))
    inject_entries = [("framework", "framework")] + [(f"annex_{index}", f"annex_{index}") for index in range(1, spec.inject_files)]
    if not inject:
        inject_entries = []
    for entry, _ in inject_entries:
        plan.append(
            (f"attach_framework.{entry}.md", f"node:attach_framework.options.files.{entry}.content", 4.0 if entry == "framework" else 3.0)
        )
    sizes = _sizes(rng, [weight for _, _, weight in plan], spec.prompt_bytes)

    produced: dict[str, str] = {}  # field -> producing step
    for name in SOURCE_FIELDS:
        produced[name] = "submissions"
    files: list[FixtureFile] = []
    contents: dict[str, str] = {}
    for (name, slot, _), size in zip(plan, sizes, strict=True):
        step = slot.split(":", 1)[1].split(".", 1)[0]
        owner = next((item for item in roles if item.name == step), None)
        if owner is not None and slot.endswith("prompt_template"):
            head = f"# {owner.name.replace('_', ' ').title()}\n\nUse the material below.\n\n{_refs(owner.reads)}\n\nGuidance follows.\n"
        elif owner is not None:
            head = f"# {owner.name.replace('_', ' ').title()} instructions\n\nYou are one step of an assessment panel.\n"
        else:
            head = f"# Assessment framework: {name.split('.')[1].replace('_', ' ')}\n"
        contents[name] = _text(rng, size, head)
        files.append(FixtureFile(name=name, slot=slot, mime_type="text/markdown", content=contents[name].encode()))

    transforms: list[JsonObject] = []
    gates: list[JsonObject] = []
    route = "intake"
    names_in_order: list[str] = []

    def add(step: JsonObject) -> None:
        nonlocal route
        step["input"] = route
        route = f"{step['name']}_out"
        step["on_success"] = route
        transforms.append(step)
        names_in_order.append(step["name"])

    inject_files: JsonObject = {}
    for entry, output_field in inject_entries:
        # The content itself is bound from the uploaded file at import (the inline-binding slot).
        inject_files[entry] = {"format": "text", "output_field": output_field}
        produced[output_field] = "attach_framework"
    if inject:
        add(
            {
                "name": "attach_framework",
                "plugin": "file_inject",
                "on_error": "rejected_rows",
                "options": {"schema": {"mode": "observed"}, "mode": "all", "files": inject_files},
            }
        )
    # Truncate steps cap raw case text before the steps that read it, so LLM steps that read only what earlier
    # steps wrote follow each other directly (the mid-chain position needs an LLM step between two LLM steps).
    takers = [index for index, role in enumerate(roles) if set(role.reads) & set(SOURCE_FIELDS[4:13])] or [0]
    filler_slots = [0] * len(roles)
    for count in range(fillers):
        filler_slots[takers[count % len(takers)]] += 1
    inline = 0
    llm_names: list[str] = []
    for role, filler_count in zip(roles, filler_slots, strict=True):
        text_field = next((name for name in role.reads if name in produced and name != "framework"), role.reads[0])
        for index in range(filler_count):
            add(
                {
                    "name": f"prepare_{role.name}" + (f"_{index + 1}" if index else ""),
                    "plugin": "truncate",
                    "on_error": "rejected_rows",
                    "options": {"schema": {"mode": "observed"}, "fields": {text_field: 12_000 - 1000 * index}, "suffix": "..."},
                }
            )
        options: JsonObject = {"profile": spec.profile, "schema": {"mode": "observed"}}
        if role.kind == "single":
            options.update({"required_input_fields": list(role.reads), "response_field": role.name})
            produced[role.name] = role.name
        else:
            queries = spec.large_queries if role.kind == "large" else spec.queries_per_llm_step
            query_options, outputs, query_bytes = _multi_query_options(role, queries)
            options.update(query_options)
            options["response_field"] = f"{role.name}_summary"
            inline += query_bytes
            for output in [*outputs, f"{role.name}_summary"]:
                produced[output] = role.name
        missing = [name for name in role.reads if name not in produced]
        if missing:
            raise UsageError(f"LLM step {role.name} reads {missing} that no earlier step writes; raise --llm-steps")
        add({"name": role.name, "plugin": "llm", "on_error": "rejected_rows", "options": options})
        llm_names.append(role.name)
        if role.name == "overall_rubric":
            # A gate after the overall rubric: low first-criterion scores go to the moderation queue.
            first = f"{role.prefix}_{CRITERIA[0]}_score"
            gate_route = route
            route = "moderation_passed"
            gate_step: JsonObject = {
                "name": "moderation_gate",
                "input": gate_route,
                "condition": f"row['{first}'] >= 50",
                "routes": {"true": route, "false": "moderation_queue"},
            }
            names_in_order.append("moderation_gate")
            gates.append(gate_step)
    final_fields = [
        "submission_id",
        *(name for name in ("feedback_letter", "moderation_summary", "cross_case_comparison") if name in produced),
    ]
    if len(final_fields) == 1:
        final_fields.append(roles[-1].name)
    add(
        {
            "name": "output_mapper",
            "plugin": "field_mapper",
            "on_error": "rejected_rows",
            "options": {
                "schema": {"mode": "observed"},
                "required_input_fields": final_fields,
                "mapping": {name: name for name in final_fields},
                "select_only": True,
            },
        }
    )
    transforms[-1]["on_success"] = "assessment_results"
    pipeline: JsonObject = {
        "sources": {
            "submissions": {
                "plugin": "csv",
                "on_success": "intake",
                "on_validation_failure": "rejected_rows",
                "options": {"schema": {"mode": "fixed", "fields": [f"{name}: str" for name in SOURCE_FIELDS]}},
            }
        },
        "transforms": transforms,
    }
    if gates:
        pipeline["gates"] = gates
    pipeline["sinks"] = {
        "assessment_results": _sink("json", [f"{name}: str" for name in final_fields]),
        "rejected_rows": _sink("json", None),
    }
    sink_paths = {"assessment_results": "assessment_results.json", "rejected_rows": "rejected_rows.json"}
    if gate:
        pipeline["sinks"]["moderation_queue"] = _sink("json", None)
        sink_paths["moderation_queue"] = "moderation_queue.json"
    positions = _acceptance_positions(names_in_order, llm_names)
    return Fixture(
        spec=spec,
        pipeline=pipeline,
        source_name="submissions",
        source_file=_source_csv(rng, spec.rows, "submissions.csv"),
        files=tuple(files),
        sink_paths=sink_paths,
        positions=positions,
        inline_bytes=inline,
        llm_steps=tuple(llm_names),
    )


def _acceptance_positions(order: Sequence[str], llm_names: Sequence[str]) -> tuple[Position, ...]:
    """After the source; mid-chain at an LLM step between two LLM steps where one exists; at the final mapper."""
    chain = [name for name in order if name != "moderation_gate"]
    gate_neighbours = set()
    if "moderation_gate" in order:
        index = order.index("moderation_gate")
        gate_neighbours = {order[index - 1], order[index + 1]} if index + 1 < len(order) else {order[index - 1]}
    first = Position("submissions", order[0], order[1], False)
    candidates = [
        index
        for index, name in enumerate(order)
        if name in llm_names
        and 0 < index < len(order) - 1
        and name not in gate_neighbours
        and order[index] not in (order[0], "output_mapper")
    ]
    llm_between = [index for index in candidates if order[index - 1] in llm_names and order[index + 1] in llm_names]
    pool = llm_between or candidates
    positions = [first]
    if pool:
        middle = pool[len(pool) // 2]
        positions.append(Position(order[middle - 1], order[middle], order[middle + 1], False))
    mapper = chain.index("output_mapper")
    before = order[order.index("output_mapper") - 1]
    if before != "moderation_gate" and mapper > 0:
        positions.append(Position(before, "output_mapper", "assessment_results", True))
    return tuple(positions)


def _sink(plugin: str, fields: Sequence[str] | None) -> JsonObject:
    schema: JsonObject = {"mode": "observed"} if fields is None else {"mode": "flexible", "fields": list(fields)}
    return {
        "plugin": plugin,
        "on_write_failure": "discard",
        "options": {"format": "json", "mode": "write", "collision_policy": "auto_increment", "schema": schema},
    }


def _source_csv(rng: random.Random, rows: int, name: str) -> FixtureFile:
    lines = [",".join(SOURCE_FIELDS)]
    for index in range(rows):
        values = []
        for field_name in SOURCE_FIELDS:
            if field_name == "submission_id":
                values.append(f"S{index + 1:04d}")
            elif field_name == "word_count":
                values.append(str(rng.randint(300, 2400)))
            elif field_name == "submitted_on":
                values.append(f"2026-0{rng.randint(1, 9)}-1{rng.randint(0, 9)}")
            else:
                values.append(" ".join(rng.choice(_WORDS) for _ in range(rng.randint(6, 14))))
        lines.append(",".join(values))
    return FixtureFile(name=name, slot=None, mime_type="text/csv", content=("\n".join(lines) + "\n").encode())


# ── the single-node preset ─────────────────────────────────────────────────


def _single_node(spec: FixtureSpec) -> Fixture:
    rng = random.Random(spec.seed)
    queries = spec.queries_per_llm_step
    fields = ("case_text", "case_history", "reviewer_notes")
    system_size = spec.prompt_bytes
    files = [
        FixtureFile(
            name="capacity_probe.system.md",
            slot="node:capacity_probe.options.system_prompt",
            mime_type="text/markdown",
            content=_text(rng, system_size, "# Capacity probe instructions\n").encode(),
        )
    ]
    definitions: JsonObject = {}
    inline = 0
    for index in range(queries):
        name = f"q{index + 1:02d}_{CRITERIA[index % len(CRITERIA)]}"
        head = "Score this criterion from 0 to 100. Case: {{ row.input_1 }} History: {{ row.input_2 }} Notes: {{ row.input_3 }}\n"
        # Paragraphs, as real prompts are; each YAML line carries indentation, so line count is import size.
        template = _text(rng, spec.query_bytes, head, sentences_per_line=6)
        inline += len(template.encode())
        definitions[name] = {
            "input_fields": {"input_1": fields[0], "input_2": fields[1], "input_3": fields[2]},
            "template": template,
            "output_fields": [{"suffix": "score", "type": "integer"}, {"suffix": "rationale", "type": "string"}],
        }
    pipeline: JsonObject = {
        "sources": {
            "source": {
                "plugin": "csv",
                "on_success": "probe_in",
                "on_validation_failure": "errors",
                "options": {"schema": {"mode": "fixed", "fields": [f"{name}: str" for name in fields]}},
            }
        },
        "transforms": [
            {
                "name": "capacity_probe",
                "plugin": "llm",
                "input": "probe_in",
                "on_success": "results",
                "on_error": "errors",
                "options": {
                    "profile": spec.profile,
                    "schema": {"mode": "observed"},
                    "required_input_fields": list(fields),
                    "response_field": "capacity_probe_summary",
                    "queries": definitions,
                },
            }
        ],
        "sinks": {"results": _sink("json", None), "errors": _sink("json", None)},
    }
    lines = [",".join(fields)] + [",".join(" ".join(rng.choice(_WORDS) for _ in range(10)) for _ in fields) for _ in range(spec.rows)]
    return Fixture(
        spec=spec,
        pipeline=pipeline,
        source_name="source",
        source_file=FixtureFile(name="source.csv", slot=None, mime_type="text/csv", content=("\n".join(lines) + "\n").encode()),
        files=tuple(files),
        sink_paths={"results": "results.json", "errors": "errors.json"},
        positions=(Position("source", "capacity_probe", "results", True),),
        inline_bytes=inline,
        llm_steps=("capacity_probe",),
    )


# ── building, writing and reading ──────────────────────────────────────────


def preset_spec(preset: Preset, **overrides: Any) -> FixtureSpec:
    base = (
        FixtureSpec(preset=preset)
        if preset == "acceptance"
        else FixtureSpec(preset=preset, steps=1, llm_steps=1, queries_per_llm_step=32, prompt_bytes=4_000)
    )
    return replace(base, **overrides)


def state_scan_values(yaml_text: str) -> int:
    """Values the server's whole-state credential scan visits for this graph as imported (the YAML importer's state)."""
    from elspeth.web.composer.yaml_importer import composition_state_from_runtime_yaml

    return scan_values_of_state(composition_state_from_runtime_yaml(yaml_text))


def scan_values_of_state(state: CompositionState) -> int:
    """Values the server's whole-state credential scan visits for ``state``.

    Measured with the server's own code: the ``set_pipeline`` projection the
    state guard scans, and the detector run with ever smaller node limits
    until it reports the traversal limit. The server's limit is
    ``CredentialTraversalPolicy.max_nodes`` (4,096).
    """
    from elspeth.contracts.credential_material import CredentialTraversalPolicy, find_credential_material
    from elspeth.web.credential_guard import credential_material_tool_arguments_projection

    tree = credential_material_tool_arguments_projection("set_pipeline", state.to_dict())

    def fits(limit: int) -> bool:
        finding = find_credential_material(tree, CredentialTraversalPolicy(max_nodes=limit))
        if finding is None:
            return True
        if finding.category == "traversal_limit" and limit < SCAN_CEILING:
            return False
        raise OperationFailed(f"the generated graph fails the credential scan for another reason: {finding.category}")

    low, high = 1, SCAN_CEILING
    fits(high)
    while low < high:
        middle = (low + high) // 2
        if fits(middle):
            high = middle
        else:
            low = middle + 1
    return low


def build_fixture(spec: FixtureSpec) -> Fixture:
    if spec.prompt_bytes < 4000 or spec.prompt_bytes > 4_000_000:
        raise UsageError("--prompt-bytes must be between 4000 and 4000000")
    if not 1 <= spec.queries_per_llm_step <= MAX_QUERIES or not 1 <= spec.large_queries <= MAX_QUERIES:
        raise UsageError(f"--queries-per-llm-step and --large-queries must be 1-{MAX_QUERIES}")
    if not 200 <= spec.query_bytes <= 262_144:
        raise UsageError("--query-bytes must be between 200 and 262144")
    if not 1 <= spec.inject_files <= 8:
        raise UsageError("--inject-files must be between 1 and 8")
    if spec.preset == "single-node":
        return _single_node(spec)
    return _acceptance(spec)


def manifest(fixture: Fixture) -> JsonObject:
    spec = fixture.spec
    return {
        "version": MANIFEST_VERSION,
        "spec": asdict(spec),
        "source": {"name": fixture.source_name, "file": _file_entry(fixture.source_file)},
        "files": [_file_entry(file) for file in fixture.files],
        "sink_paths": dict(fixture.sink_paths),
        "positions": [position.as_json() for position in fixture.positions],
        "llm_steps": list(fixture.llm_steps),
        "measures": {**fixture.measures(), "state_scan_values": state_scan_values(fixture.yaml_text), "state_scan_limit": _scan_limit()},
    }


def _scan_limit() -> int:
    from elspeth.contracts.credential_material import CredentialTraversalPolicy

    return CredentialTraversalPolicy().max_nodes


def _file_entry(file: FixtureFile) -> JsonObject:
    return {
        "name": file.name,
        "slot": file.slot,
        "mime_type": file.mime_type,
        "bytes": len(file.content),
        "sha256": hashlib.sha256(file.content).hexdigest(),
    }


def write_fixture(fixture: Fixture, out_dir: Path, *, repo_root: Path = REPO_ROOT) -> Path:
    """Write ``pipeline.yaml``, ``files/`` and ``manifest.json``; refuse a directory inside the repository."""
    target = out_dir.expanduser().resolve()
    if target == repo_root or target.is_relative_to(repo_root):
        raise UsageError("--out must be outside the repository")
    if target.exists() and any(target.iterdir()):
        raise UsageError(f"--out {target} is not empty")
    (target / "files").mkdir(parents=True, exist_ok=True)
    (target / "pipeline.yaml").write_text(fixture.yaml_text, encoding="utf-8")
    for file in (fixture.source_file, *fixture.files):
        (target / "files" / file.name).write_bytes(file.content)
    (target / "manifest.json").write_text(json.dumps(manifest(fixture), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


@dataclass(frozen=True, slots=True)
class LoadedFixture:
    """A written fixture read back, every file checked against the manifest's digest."""

    root: Path
    manifest: JsonObject
    yaml_text: str
    contents: Mapping[str, bytes]

    @property
    def positions(self) -> tuple[Position, ...]:
        return tuple(Position.from_json(entry) for entry in self.manifest["positions"])

    def import_body(self, blob_ids: Mapping[str, str]) -> JsonObject:
        source = self.manifest["source"]
        return {
            "yaml": self.yaml_text,
            "source_blob_ids": {source["name"]: blob_ids[source["file"]["name"]]},
            "inline_blob_bindings": {entry["slot"]: {"blob_id": blob_ids[entry["name"]]} for entry in self.manifest["files"]},
            "sink_path_bindings": {
                name: {"path": {"folder": "outputs", "relative_path": path}} for name, path in self.manifest["sink_paths"].items()
            },
        }


def load_fixture(root: Path) -> LoadedFixture:
    resolved = root.expanduser().resolve()
    try:
        data = json.loads((resolved / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UsageError(f"--fixture {resolved}: no readable manifest.json ({type(exc).__name__})") from None
    if type(data) is not dict or data["version"] != MANIFEST_VERSION:
        raise UsageError(f"--fixture {resolved}: manifest version is not {MANIFEST_VERSION}")
    contents: dict[str, bytes] = {}
    for entry in [data["source"]["file"], *data["files"]]:
        path = resolved / "files" / entry["name"]
        if path.parent != resolved / "files":
            raise UsageError(f"--fixture: file name {entry['name']!r} leaves the files directory")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise UsageError(f"--fixture: files/{entry['name']} does not match the manifest; regenerate the fixture")
        contents[entry["name"]] = content
    return LoadedFixture(
        root=resolved, manifest=data, yaml_text=(resolved / "pipeline.yaml").read_text(encoding="utf-8"), contents=contents
    )


# ── command line ───────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.staging.large_graph_fixture", description=__doc__.split("\n\n")[0])
    parser.add_argument("--preset", choices=["acceptance", "single-node"], default="acceptance")
    parser.add_argument("--out", type=Path, required=True, help="empty directory outside the repository for the fixture")
    parser.add_argument("--seed", type=int, help="seed for every generated name-independent choice (default 1)")
    parser.add_argument("--steps", type=int, help="acceptance: transforms and gates in the chain (default 27)")
    parser.add_argument("--llm-steps", type=int, help="acceptance: LLM steps (default 12)")
    parser.add_argument(
        "--queries-per-llm-step",
        type=int,
        help=f"queries in each multi-query LLM step: acceptance's scoring steps (default 3), single-node's one step (default 32); up to {MAX_QUERIES}",
    )
    parser.add_argument("--large-queries", type=int, help="acceptance: queries in the larger multi-query step (default 8)")
    parser.add_argument("--query-bytes", type=int, help="single-node: bytes of each inline query template (default 15600)")
    parser.add_argument(
        "--prompt-bytes",
        type=int,
        help="acceptance: prompt text bytes held in files (default 200000); single-node: its system prompt file (default 4000)",
    )
    parser.add_argument("--inject-files", type=int, help="acceptance: file_inject entries, one file each (default 2)")
    parser.add_argument(
        "--file-inject",
        choices=["file", "omit"],
        help="acceptance: a file_inject step with file-held content (default), or omit the step (see the runbook)",
    )
    parser.add_argument("--profile", help="the LLM profile name the steps use on the target server (default task-role)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    given = {
        "seed": args.seed,
        "steps": args.steps,
        "llm_steps": args.llm_steps,
        "queries_per_llm_step": args.queries_per_llm_step,
        "large_queries": args.large_queries,
        "query_bytes": args.query_bytes,
        "prompt_bytes": args.prompt_bytes,
        "inject_files": args.inject_files,
        "file_inject": args.file_inject,
        "profile": args.profile,
    }
    overrides = {name: value for name, value in given.items() if value is not None}
    fixture = build_fixture(preset_spec(args.preset, **overrides))
    root = write_fixture(fixture, args.out)
    emit(
        "fixture_written", out=str(root), **manifest(fixture)["measures"], positions=[position.as_json() for position in fixture.positions]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tool(main))
