"""Helper for scripts/examples-gate.sh: inventory, per-case preflight, and verdicts.

The driver (examples-gate.sh) owns process control: copying trees, starting
fixture servers, running the documented command and recording its exit code.
This module never runs an example. It reads the case data file
(examples-gate-cases.yaml), the exported tree, and -- after a case ran -- the
case's audit databases (opened read-only) and its log.

Subcommands
  validate   --cases F --tree T            data file covers every example, config and launcher
  inventory  --cases F --tree T [--out F]  render the inventory (classification + deciding line)
  list       --cases F [--only IDS]        one line per case for the driver (\x1f-separated)
  count      --cases F [--only IDS]        selected case count for list integrity
  digest     --tree T --manifest F         frozen-tree digest, including metadata
  excluded   --cases F                     one SKIP line per config the gate does not run
  preflight  --cases F --tree T --case ID  offline guard + prerequisites for one case
  check      --cases F --root R --case ID --exit N --log L --evidence E
                                           verdict line for one finished case

Exit codes: 0 = ok/PASS, 1 = FAIL/refused, 2 = usage or data-file error.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import yaml

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
URL_RE = re.compile(r"[a-z][a-z0-9+.-]*://[^/\s'\"<>?#]+", re.IGNORECASE)
# Plugins that talk to a remote service by construction, whatever their URL.
REMOTE_PLUGINS = frozenset(
    {
        "azure_blob",
        "azure_content_safety",
        "azure_prompt_shield",
        "azure_ai_search",
        "power_automate",
        "aws_textract_inline_analysis",
        "aws_s3",
        "aws_bedrock_content_safety",
        "aws_bedrock_prompt_shield",
        "aws_textract_document_analysis",
        "azure_document_intelligence",
        "azure_content_understanding",
        "dataverse",
    }
)
URL_OPTION_KEYS = frozenset({"url", "base_url", "endpoint", "account_url", "vault_url", "trigger_url"})
CLASSES = frozenset({"NON-NETWORK", "NETWORK", "CONTAINER", "NOT-A-PIPELINE", "UNSURE"})


class DataError(Exception):
    """The case data file is malformed or does not cover the tree."""


# --------------------------------------------------------------------------- data


def _mapping(node: Any, allowed: set[str], label: str) -> dict[str, Any]:
    if not isinstance(node, dict) or any(not isinstance(key, str) for key in node):
        raise DataError(f"{label}: expected a mapping")
    unknown = set(node) - allowed
    if unknown:
        raise DataError(f"{label}: unknown keys {sorted(unknown)}")
    return cast("dict[str, Any]", node)


def _typed(node: Any, expected: type, label: str) -> None:
    if type(node) is not expected:
        raise DataError(f"{label}: expected {expected.__name__}")


def _list_of(node: Any, expected: type, label: str) -> None:
    if not isinstance(node, list) or any(type(value) is not expected for value in node):
        raise DataError(f"{label}: expected a list of {expected.__name__}")


GENERATED_INPUT_SUFFIXES = frozenset({".csv", ".jsonl", ".json"})
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.S)
_PATH_VARIABLE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _command_words(command: str, label: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError as error:
        raise DataError(f"{label}.command: invalid command: {error}") from error


def case_environment(case: dict[str, Any]) -> dict[str, str]:
    """The variables a case sets for its own command: its env mapping, overridden by its leading VAR=value words."""
    environment: dict[str, str] = dict(case.get("env") or {})
    for word in _command_words(case["command"], f"case {case['id']!r}"):
        if not _ASSIGNMENT_RE.fullmatch(word):
            break
        name, value = word.split("=", 1)
        environment[name] = value
    return environment


def resolve_source_path(raw: str, environment: dict[str, str]) -> str:
    """A config's source path as the case's command sees it.

    ${NAME:-default} takes the case's own value of NAME, else the default; a variable the case
    does not set is never looked up anywhere else.
    """

    def substitute(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in environment:
            return environment[name]
        return default if default is not None else match.group(0)

    return _PATH_VARIABLE_RE.sub(substitute, raw)


def _validate_generated_inputs(case: dict[str, Any], label: str) -> None:
    block = _mapping(case["generated_inputs"], {"generator", "paths"}, f"{label}.generated_inputs")
    _typed(block.get("generator"), str, f"{label}.generated_inputs.generator")
    _list_of(block.get("paths"), str, f"{label}.generated_inputs.paths")
    if not block["paths"] or len(set(block["paths"])) != len(block["paths"]):
        raise DataError(f"{label}.generated_inputs.paths: expected distinct paths")
    example_dir = Path("examples") / str(case.get("example"))
    for raw in [block["generator"], *block["paths"]]:
        path = Path(raw)
        if raw != os.path.normpath(raw) or path.is_absolute() or re.search(r"[*?\[\]]", raw) or not path.is_relative_to(example_dir):
            raise DataError(f"{label}.generated_inputs: {raw!r} is not an exact path inside {example_dir}")
    for raw in block["paths"]:
        if Path(raw).suffix not in GENERATED_INPUT_SUFFIXES:
            raise DataError(f"{label}.generated_inputs: {raw!r} is not one of {sorted(GENERATED_INPUT_SUFFIXES)}")
    words = [word for word in _command_words(case["command"], label) if not _ASSIGNMENT_RE.fullmatch(word)]
    if not words or os.path.normpath(words[0]) != block["generator"]:
        raise DataError(f"{label}.generated_inputs.generator: {block['generator']!r} is not the case's own launcher")


def generated_inputs_of(case: dict[str, Any]) -> list[str]:
    return list((case.get("generated_inputs") or {}).get("paths") or [])


def sandbox_in_force(proof: str | None, nonce: str | None) -> bool:
    """True only when this run's sandbox self-test output carries this run's nonce.

    The gate refuses every case when the self-test fails; checking its output here as well keeps
    the deferral of the offline guard tied to the sandbox, so a call without it never defers.
    """
    if not proof or not nonce or not re.fullmatch(r"[0-9a-f]{32}", nonce):
        return False
    try:
        lines = Path(proof).read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return f"self-test-ok {nonce}" in lines


def gitignored(tree: Path, rel: str, scratch: Path) -> bool:
    """Whether the tree's own .gitignore files ignore ``rel``. The tree is an export with no .git:
    an empty repository lends git its machinery, and no user or system ignore file is read."""
    git_dir = scratch / "ignore-check.git"
    if not git_dir.is_dir():
        subprocess.run(["git", "init", "-q", "--bare", "--template=", str(git_dir)], check=True, env=_GIT_ENV)
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.excludesFile=/dev/null",
            f"--git-dir={git_dir}",
            f"--work-tree={tree}",
            "check-ignore",
            "-q",
            "--no-index",
            "--",
            rel,
        ],
        env=_GIT_ENV,
        cwd=tree,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise DataError(f"git check-ignore {rel}: exit {result.returncode}")
    return result.returncode == 0


_GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def generated_input_refusals(tree: Path, case: dict[str, Any], scratch: Path) -> list[str]:
    """Before the run: the generator is in the export, and each declared input is gitignored and absent."""
    block = case["generated_inputs"]
    refusals = []
    generator = block["generator"]
    if not (tree / generator).is_file() or gitignored(tree, generator, scratch):
        refusals.append(f"generated inputs: generator {generator} is not a tracked file of the export")
    for raw in block["paths"]:
        if os.path.lexists(tree / raw):
            refusals.append(f"generated inputs: {raw} exists before the run (tracked: stale declaration)")
        elif not gitignored(tree, raw, scratch):
            refusals.append(f"generated inputs: {raw} is not gitignored")
    return refusals


def _strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(key)
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _input_values(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".csv":
        return [cell for row in csv.reader(io.StringIO(text)) for cell in row]
    if path.suffix == ".jsonl":
        return [value for line in text.splitlines() if line.strip() for value in _strings(json.loads(line))]
    return list(_strings(json.loads(text)))


def generated_input_failures(root: Path, case: dict[str, Any]) -> tuple[list[str], list[str]]:
    """After the run: each declared input exists inside the copy and names nothing off the copy.

    A remote URL fails the case; so does an absolute (or file:) path, or a relative one, that
    resolves outside the case copy.
    """
    failures: list[str] = []
    evidence: list[str] = []
    real_root = os.path.realpath(root)
    for raw in generated_inputs_of(case):
        path = root / raw
        if not _within(os.path.realpath(path), real_root) or not path.is_file():
            failures.append(f"generated input {raw}: not a file inside the case copy after the run")
            continue
        if path.stat().st_size >= 50_000_000:
            failures.append(f"generated input {raw}: exceeds size bound")
            continue
        try:
            values = _input_values(path)
        except (OSError, UnicodeDecodeError, ValueError, csv.Error) as error:
            failures.append(f"generated input {raw}: unreadable ({type(error).__name__})")
            continue
        bad = []
        for value in values:
            for match in URL_RE.finditer(value):
                host = _url_host(match.group(0))
                if host not in LOOPBACK_HOSTS:
                    bad.append(f"remote URL host {host}")
            target = value[len("file://") :] if value.startswith("file://") else value
            if "\n" in target or not target:
                continue
            names_a_path = os.path.isabs(target) or os.path.normpath(target).split(os.sep)[0] == ".."
            if names_a_path and not _within(os.path.realpath(root / target), real_root):
                bad.append(f"path {target} outside the case copy")
        if bad:
            failures.append(f"generated input {raw}: {'; '.join(dict.fromkeys(bad))}")
        else:
            evidence.append(f"generated input {raw}: {len(values)} values, none remote or outside the copy")
    return failures, evidence


def _relative_home_path(raw: str, label: str) -> Path:
    path = Path(raw)
    if path.is_absolute() or path == Path(".") or "," in raw or any(part in {"..", ""} for part in path.parts):
        raise DataError(f"{label}: expected a HOME-relative path")
    return path


def load_cases(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise DataError(f"{path}: expected a mapping with version: 1")
    _mapping(data, {"version", "examples", "cases"}, str(path))
    for key in ("examples", "cases"):
        if key not in data:
            raise DataError(f"{path}: missing top-level key {key!r}")
    if not isinstance(data["cases"], list) or not isinstance(data["examples"], dict):
        raise DataError(f"{path}: cases must be a list and examples a mapping")
    case_keys = {
        "id",
        "example",
        "command",
        "expect_exit",
        "configs",
        "expectation_source",
        "audit",
        "log_markers",
        "skip",
        "ports",
        "server",
        "fingerprint_key",
        "requires",
        "env",
        "files",
        "generated_inputs",
    }
    audit_keys = {
        "db",
        "runs",
        "status",
        "source_rows",
        "sinks",
        "outcomes",
        "sql",
        "label",
        "conservation",
        "mode",
        "run_index",
        "allow_zero_rows",
    }
    for case in data["cases"]:
        label = f"case {case.get('id')!r}" if isinstance(case, dict) else "case"
        _mapping(case, case_keys, label)
        for key in ("id", "example", "command", "expectation_source"):
            if key in case:
                _typed(case[key], str, f"{label}.{key}")
        for key in ("expect_exit", "configs", "ports"):
            if key in case:
                _list_of(case[key], int if key in {"expect_exit", "ports"} else str, f"{label}.{key}")
        if "log_markers" in case:
            if not isinstance(case["log_markers"], list):
                raise DataError(f"{label}.log_markers: expected a list")
            for marker in case["log_markers"]:
                if isinstance(marker, str):
                    continue
                block = _mapping(marker, {"text", "count"}, f"{label}.log_markers")
                _typed(block.get("text"), str, f"{label}.log_markers.text")
                _typed(block.get("count"), int, f"{label}.log_markers.count")
        for key in ("skip",):
            if key in case:
                _typed(case[key], str, f"{label}.{key}")
        if "fingerprint_key" in case:
            _typed(case["fingerprint_key"], bool, f"{label}.fingerprint_key")
        if "env" in case:
            env = _mapping(case["env"], set(case["env"]) if isinstance(case["env"], dict) else set(), f"{label}.env")
            for name, value in env.items():
                _typed(value, str, f"{label}.env.{name}")
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or re.search(r"[\s*?\[\]]", value):
                    raise DataError(f"{label}.env.{name}: unsafe name or value")
        if "files" in case:
            files = _mapping(case["files"], set(case["files"]) if isinstance(case["files"], dict) else set(), f"{label}.files")
            if any(value != "absent_or_empty" for value in files.values()):
                raise DataError(f"{label}.files: expected absent_or_empty")
        if "server" in case:
            server = _mapping(case["server"], {"command", "port"}, f"{label}.server")
            _typed(server.get("command"), str, f"{label}.server.command")
            _typed(server.get("port"), int, f"{label}.server.port")
            server_command = server["command"]
            try:
                server_words = shlex.split(server_command)
            except ValueError as error:
                raise DataError(f"{label}.server: invalid command: {error}") from error
            if re.search(r"[;&|$<>()`\n\r]", server_command) or server_words[:2] not in (["chaosllm", "serve"], ["chaosweb", "serve"]):
                raise DataError(f"{label}.server: unsupported fixture server")
        if "requires" in case:
            requires = _mapping(case["requires"], {"python_modules", "files", "home_ro"}, f"{label}.requires")
            for key in ("python_modules", "files", "home_ro"):
                if key in requires:
                    _list_of(requires[key], str, f"{label}.requires.{key}")
            home_ro = [_relative_home_path(raw, f"{label}.requires.home_ro") for raw in requires.get("home_ro", [])]
            for raw in requires.get("files", []):
                if not raw.startswith("~/"):
                    raise DataError(f"{label}.requires.files: expected ~/ path")
                file_path = _relative_home_path(raw[2:], f"{label}.requires.files")
                if not any(file_path.is_relative_to(directory) for directory in home_ro):
                    raise DataError(f"{label}.requires.files: {raw} is outside home_ro")
        if "audit" in case:
            if not isinstance(case["audit"], list):
                raise DataError(f"{label}.audit: expected a list")
            for spec in case["audit"]:
                audit = _mapping(spec, audit_keys, f"{label}.audit")
                _typed(audit.get("db"), str, f"{label}.audit.db")
                if Path(audit["db"]).is_absolute() or ".." in Path(audit["db"]).parts:
                    raise DataError(f"{label}.audit.db: expected a tree-relative path")
                for key in ("runs", "source_rows", "run_index"):
                    if key in audit:
                        _typed(audit[key], int, f"{label}.audit.{key}")
                if "status" in audit:
                    _list_of(audit["status"], str, f"{label}.audit.status")
                for key in ("conservation", "allow_zero_rows"):
                    if key in audit:
                        _typed(audit[key], bool, f"{label}.audit.{key}")
                for key in ("label", "mode"):
                    if key in audit:
                        _typed(audit[key], str, f"{label}.audit.{key}")
                for key in ("sinks", "outcomes"):
                    if key in audit:
                        values = audit[key]
                        if not isinstance(values, dict) or any(type(k) is not str or type(v) is not int for k, v in values.items()):
                            raise DataError(f"{label}.audit.{key}: expected string-to-int mapping")
                if "sql" in audit:
                    if not isinstance(audit["sql"], list):
                        raise DataError(f"{label}.audit.sql: expected a list")
                    for query in audit["sql"]:
                        block = _mapping(query, {"name", "query", "expect"}, f"{label}.audit.sql")
                        _typed(block.get("name"), str, f"{label}.audit.sql.name")
                        _typed(block.get("query"), str, f"{label}.audit.sql.query")
                        if not isinstance(block.get("expect"), list):
                            raise DataError(f"{label}.audit.sql.expect: expected a list")
    ids = [case["id"] for case in data["cases"]]
    if len(ids) != len(set(ids)):
        raise DataError(f"{path}: duplicate case ids")
    for case in data["cases"]:
        for key in ("id", "example", "command", "expect_exit", "configs", "expectation_source"):
            if key not in case:
                raise DataError(f"case {case.get('id')!r}: missing {key!r}")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", case["id"]):
            raise DataError(f"case id {case['id']!r} is not filename-safe")
        if "generated_inputs" in case:
            _validate_generated_inputs(case, f"case {case['id']!r}")
    for name, entry in data["examples"].items():
        if entry.get("class") not in CLASSES:
            raise DataError(f"example {name!r}: class {entry.get('class')!r} not in {sorted(CLASSES)}")
        for cfg, cfg_entry in (entry.get("configs") or {}).items():
            if cfg_entry.get("class") not in CLASSES:
                raise DataError(f"example {name}/{cfg}: class {cfg_entry.get('class')!r} not in {sorted(CLASSES)}")
    return cast("dict[str, Any]", data)


def case_by_id(data: dict[str, Any], case_id: str) -> dict[str, Any]:
    for case in data["cases"]:
        if case["id"] == case_id:
            return cast("dict[str, Any]", case)
    raise DataError(f"no case {case_id!r}")


def is_pipeline(doc: Any) -> bool:
    return isinstance(doc, dict) and "sinks" in doc and ("source" in doc or "sources" in doc)


def discover(tree: Path) -> dict[str, dict[str, list[str]]]:
    """Every example directory, its top-level pipeline configs and its launchers."""
    found: dict[str, dict[str, list[str]]] = {}
    for directory in sorted((tree / "examples").iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        configs = []
        for path in sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")]):
            if is_pipeline(yaml.safe_load(path.read_text(encoding="utf-8"))):
                configs.append(path.name)
        launchers = sorted(p.name for p in directory.glob("*.sh"))
        found[directory.name] = {"configs": configs, "launchers": launchers}
    return found


def digest_tree(tree: Path, manifest: Path) -> str:
    """Hash every entry's name, type, mode, link target, and regular-file bytes."""
    digest = hashlib.sha256()
    with manifest.open("wb") as output:
        for base, dirs, files in os.walk(tree, followlinks=False, onerror=_raise_walk_error):
            dirs.sort()
            for name in sorted([*dirs, *files]):
                path = Path(base) / name
                info = path.lstat()
                entry_type = stat.S_IFMT(info.st_mode)
                record = f"{path.relative_to(tree)}\0{entry_type:o}\0{stat.S_IMODE(info.st_mode):o}\0".encode()
                if stat.S_ISLNK(info.st_mode):
                    record += os.fsencode(os.readlink(path))
                elif stat.S_ISREG(info.st_mode):
                    with path.open("rb") as source:
                        record += hashlib.file_digest(source, "sha256").hexdigest().encode()
                record += b"\0"
                output.write(record)
                digest.update(record)
    return digest.hexdigest()


def _raise_walk_error(error: OSError) -> None:
    raise error


def cmd_digest(args: argparse.Namespace) -> int:
    print(digest_tree(Path(args.tree), Path(args.manifest)))
    return 0


# ----------------------------------------------------------------- sandbox binds

# Every bwrap option the gate passes, with its argument count. An option missing here is
# refused, so a new mount can never be added without its source being walked.
BWRAP_OPTION_ARITY = {
    "--bind": 2,
    "--ro-bind": 2,
    "--ro-bind-try": 2,
    "--symlink": 2,
    "--dev": 1,
    "--proc": 1,
    "--tmpfs": 1,
    "--remount-ro": 1,
    "--unshare-net": 0,
    "--unshare-pid": 0,
    "--die-with-parent": 0,
}
BIND_OPTIONS = frozenset({"--bind", "--ro-bind", "--ro-bind-try"})


def bind_sources(bwrap_args: list[str]) -> list[tuple[str, bool]]:
    """The host source of every bind in a bwrap option list, and whether the bind is optional."""
    sources: list[tuple[str, bool]] = []
    index = 0
    while index < len(bwrap_args):
        option = bwrap_args[index]
        if option not in BWRAP_OPTION_ARITY:
            raise DataError(f"unclassified bwrap option {option!r}")
        arity = BWRAP_OPTION_ARITY[option]
        if index + arity >= len(bwrap_args):
            raise DataError(f"bwrap option {option} is missing its arguments")
        if option in BIND_OPTIONS:
            sources.append((bwrap_args[index + 1], option.endswith("-try")))
        index += 1 + arity
    return sources


# Bind sources always allowed (system trees); the gate adds the venv, the case base and the
# declared home caches with --allow. Nothing is ever bound from these, at any depth.
SYSTEM_BIND_ROOTS = ("/usr", "/etc", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
NEVER_BOUND = ("/proc", "/sys", "/dev")


def _within(path: str, root: str) -> bool:
    root = root.rstrip("/")
    return path == root or path.startswith(root + "/")


def bind_source_refusal(source: str, allowed_roots: list[str]) -> str | None:
    """Why a bind source may not be bound at all, decided before anything is walked.

    The source is judged as written with dot segments removed, and a directory also by where it
    resolves: bwrap binds the resolved directory, so a link out of an allowed root is refused.
    A file source may resolve elsewhere (the /etc links into /usr and /run): only that file is bound.
    """
    lexical = os.path.normpath(source)
    real = os.path.realpath(source)
    for path in (lexical, real):
        if path == "/":
            return "the host root"
        if any(_within(path, never) for never in NEVER_BOUND):
            return f"{path} is a kernel or device tree"
    roots = [root for root in (*SYSTEM_BIND_ROOTS, *allowed_roots) if root.rstrip("/")]
    if not any(_within(lexical, root) for root in roots):
        return "outside the allowed bind roots"
    if os.path.isdir(real) and not any(_within(real, os.path.realpath(root)) for root in roots):
        return f"resolves to {real}, outside the allowed bind roots"
    return None


def _decode_mount_path(field: str) -> str:
    # mountinfo escapes space, tab, newline and backslash as \ooo octal.
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), field)


def mount_points(mountinfo: str) -> list[str]:
    """Every mount point in /proc/self/mountinfo text, decoded."""
    return [_decode_mount_path(line.split()[4]) for line in mountinfo.splitlines() if line.strip()]


def mounts_beneath(source: str, points: list[str]) -> list[str]:
    """Mount points strictly beneath a bound source: bwrap binds recursively, so each would be inside.

    Read from the mount table, not from st_dev, so a bind mount on the same filesystem and a
    mount over a single file are both seen.
    """
    real = os.path.realpath(source)
    if real == "/":
        return [point for point in points if point != "/"]
    return sorted({point for point in points if point.startswith(real.rstrip("/") + "/")})


def sockets_under(source: Path) -> tuple[list[str], int]:
    """Socket files under a bound source, and how many directories this user cannot search.

    A directory that cannot be listed is a finding unless this user also cannot search it:
    nothing beneath an unsearchable directory is reachable by name. A mount point beneath the
    source is a finding and is not entered: bwrap binds recursively, so it would be inside too.
    """
    real = Path(os.path.realpath(source))
    info = real.lstat()
    if stat.S_ISSOCK(info.st_mode):
        return [f"socket {real}"], 0
    if not stat.S_ISDIR(info.st_mode):
        return [], 0
    device = info.st_dev
    findings: list[str] = []
    unsearchable = 0
    pending = [real]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.stat(follow_symlinks=False).st_dev != device:
                            findings.append(f"mount point {entry.path}")
                            continue
                        pending.append(Path(entry.path))
                    elif not entry.is_file(follow_symlinks=False) and not entry.is_symlink():
                        try:
                            mode = entry.stat(follow_symlinks=False).st_mode
                        except FileNotFoundError:
                            continue
                        if stat.S_ISSOCK(mode):
                            findings.append(f"socket {entry.path}")
        except OSError as error:
            if os.access(directory, os.X_OK):
                findings.append(f"unlistable directory {directory}: {error.strerror}")
            else:
                unsearchable += 1
    return findings, unsearchable


def cmd_socket_walk(args: argparse.Namespace) -> int:
    bwrap_args = list(args.bwrap_args)
    if bwrap_args[:1] == ["--"]:
        bwrap_args = bwrap_args[1:]
    sources = bind_sources(bwrap_args)
    points = mount_points(Path("/proc/self/mountinfo").read_text())
    refusals: list[str] = []
    for source, _optional in sources:
        reason = bind_source_refusal(source, args.allow)
        if reason:
            refusals.append(f"REFUSE bind {source}: {reason}")
        elif os.path.lexists(source):
            refusals.extend(f"REFUSE bind {source}: mount point {point}" for point in mounts_beneath(source, points))
    if refusals:
        print("\n".join(refusals))
        return 1
    # The first --shared-count options were walked for sockets earlier in this run.
    shared = len(bind_sources(bwrap_args[: args.shared_count]))
    status = 0
    for source, optional in sources[shared:]:
        if not os.path.lexists(source):
            if optional:
                print(f"bind {source}: absent, optional")
                continue
            print(f"REFUSE bind {source}: missing")
            status = 1
            continue
        findings, unsearchable = sockets_under(Path(source))
        for finding in findings:
            print(f"REFUSE bind {source}: {finding}")
            status = 1
        if unsearchable:
            print(f"bind {source}: {unsearchable} directories this user cannot search")
    if status == 0:
        # The gate accepts exit 0 only with this line: an exit before the walk ends is no verdict.
        print(f"socket-walk complete: {len(sources)} binds checked, {len(sources) - shared} walked")
    return status


# ----------------------------------------------------------------- offline guard


def _walk(node: Any, path: str = "") -> Iterator[tuple[str, str, Any]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield path, key, value
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")


def _line_of(text: str, needle: str) -> tuple[int, str]:
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return number, line.strip()
    return 0, ""


def _url_host(value: str) -> str | None:
    try:
        return urlsplit(value).hostname
    except ValueError:
        return None


def _source_paths(doc: dict[str, Any]) -> list[str]:
    sources = doc.get("sources") or ({"primary": doc["source"]} if "source" in doc else {})
    paths = []
    for source in sources.values():
        if isinstance(source, dict):
            value = (source.get("options") or {}).get("path")
            if isinstance(value, str):
                paths.append(value)
    return paths


def network_findings(
    tree: Path, config: Path, *, environment: dict[str, str] | None = None, deferred: frozenset[str] = frozenset()
) -> list[str]:
    """Reasons a config would reach beyond loopback. Empty list = offline by construction.

    Each finding is 'file:line: text -- reason', the deciding line. Source paths are resolved with
    the case's own ``environment``. A path in ``deferred`` is a declared generated input that does
    not exist yet; only cmd_preflight passes any, and only when this run's sandbox self-test passed.
    """
    text = config.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    rel = config.relative_to(tree)
    findings: list[str] = []

    def add(needle: str, reason: str) -> None:
        number, line = _line_of(text, needle)
        findings.append(f"{rel}:{number}: {line} -- {reason}")

    for match in URL_RE.finditer(text):
        host = _url_host(match.group(0))
        if host not in LOOPBACK_HOSTS:
            add(match.group(0), f"non-loopback URL host {host}")
    for _path, key, value in _walk(doc):
        if key == "plugin" and value in REMOTE_PLUGINS:
            add(f"plugin: {value}", f"remote-service plugin {value}")
        if key in URL_OPTION_KEYS and isinstance(value, str) and "${" in value:
            add(value, f"{key} taken from the environment")
        if key == "provider" and value in {"azure", "bedrock", "aws_bedrock"}:
            add(f"provider: {value}", f"cloud LLM provider {value}")
    # An LLM node (transform or source) with no loopback base_url goes to the provider's public API.
    for container in _llm_nodes(doc):
        options = container.get("options") or {}
        base_url = options.get("base_url")
        provider = options.get("provider")
        if provider in {"openrouter", "openai", "anthropic"}:
            if not isinstance(base_url, str):
                add(f"provider: {provider}", f"LLM provider {provider} with no base_url (public API)")
            else:
                base_match = URL_RE.match(base_url)
                if base_match is None or _url_host(base_match.group(0)) not in LOOPBACK_HOSTS:
                    add(base_url, "LLM base_url is not loopback")
    for _path, key, _value in _walk(doc):
        if key == "vault_url":
            add("vault_url", "secret store URL")
    for raw in _source_paths(doc):
        raw = resolve_source_path(raw, environment or {})
        if raw in deferred:
            # Inspected after the run instead (generated_input_findings). Deferring the text guard is
            # acceptable only because the case runs in the sandbox with no network to reach.
            continue
        candidate = tree / raw
        if not candidate.is_file():
            add(raw, f"input fixture {raw} missing or unreadable")
            continue
        try:
            if candidate.stat().st_size >= 50_000_000:
                add(raw, f"input fixture {raw} exceeds size bound")
                continue
            data = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            add(raw, f"input fixture {raw} unreadable")
            continue
        for match in URL_RE.finditer(data):
            host = _url_host(match.group(0))
            if host not in LOOPBACK_HOSTS:
                number, line = _line_of(data, match.group(0))
                findings.append(f"{candidate.relative_to(tree)}:{number}: {line[:120]} -- source row URL host {host}")
                break
    # De-duplicate while keeping order.
    return list(dict.fromkeys(findings))


def _llm_nodes(doc: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = []
    for _path, _key, value in _walk(doc):
        if isinstance(value, dict) and value.get("plugin") == "llm":
            nodes.append(value)
        if isinstance(value, list):
            nodes.extend(v for v in value if isinstance(v, dict) and v.get("plugin") == "llm")
    unique: list[dict[str, Any]] = []
    for node in nodes:
        if all(node is not other for other in unique):
            unique.append(node)
    return unique


def offline_evidence(tree: Path, config: Path) -> str:
    """For an offline config: the deciding line (a loopback base_url/URL, else the source plugin)."""
    text = config.read_text(encoding="utf-8")
    rel = config.relative_to(tree)
    for match in URL_RE.finditer(text):
        number, line = _line_of(text, match.group(0))
        return f"{rel}:{number}: {line} -- loopback only"
    doc = yaml.safe_load(text)
    plugins = sorted({value for _p, key, value in _walk(doc) if key == "plugin" and isinstance(value, str)})
    number, line = _line_of(text, "plugin:")
    return f"{rel}:{number}: {line} -- plugins {', '.join(plugins)}; no URL, no remote-service plugin"


# --------------------------------------------------------------------- validate


def cmd_validate(args: argparse.Namespace) -> int:
    data = load_cases(Path(args.cases))
    tree = Path(args.tree)
    found = discover(tree)
    problems = []
    declared = data["examples"]
    for name in sorted(set(found) - set(declared)):
        problems.append(f"example directory {name!r} is not classified in the data file")
    for name in sorted(set(declared) - set(found)):
        problems.append(f"data file classifies {name!r}, which is not an example directory")
    case_ids = {case["id"] for case in data["cases"]}
    cases_by_id = {case["id"]: case for case in data["cases"]}
    for name, entry in declared.items():
        if name not in found:
            continue
        configs = entry.get("configs") or {}
        for cfg in found[name]["configs"]:
            if cfg not in configs:
                problems.append(f"{name}/{cfg}: pipeline config not classified")
        for cfg in configs:
            if cfg not in found[name]["configs"]:
                problems.append(f"{name}/{cfg}: classified but not a pipeline config in the tree")
        launchers = entry.get("launchers") or {}
        for launcher in found[name]["launchers"]:
            if launcher not in launchers:
                problems.append(f"{name}/{launcher}: launcher not classified")
        for item in [*configs.values(), *launchers.values()]:
            if item.get("class") == "NON-NETWORK" and not item.get("case") and not item.get("covered_by"):
                problems.append(f"{name}: a NON-NETWORK item names no case")
            for ref in [item.get("case"), item.get("covered_by")]:
                if ref and ref not in case_ids:
                    problems.append(f"{name}: references unknown case {ref!r}")
        for cfg, item in configs.items():
            if item["class"] != "NON-NETWORK":
                continue
            ref = item.get("case") or item.get("covered_by")
            if ref in cases_by_id:
                case = cases_by_id[ref]
                if f"examples/{name}/{cfg}" not in case["configs"]:
                    problems.append(f"{name}/{cfg}: NON-NETWORK config absent from case {ref}")
                if case.get("skip"):
                    problems.append(f"{name}/{cfg}: NON-NETWORK config belongs to skipped case {ref}")
    for case in data["cases"]:
        if case["example"] not in declared:
            problems.append(f"case {case['id']}: unknown example {case['example']!r}")
        if not case.get("skip") and not case["configs"]:
            problems.append(f"case {case['id']}: non-skip case has no configs")
        for command in [case["command"], (case.get("server") or {}).get("command", "")]:
            if not isinstance(command, str) or any(char in command for char in ("\n", "\r", "\x00")):
                problems.append(f"case {case['id']}: command contains control characters")
        for cfg in case["configs"]:
            if not (tree / cfg).is_file():
                problems.append(f"case {case['id']}: config {cfg} missing from tree")
                continue
            parts = Path(cfg).parts
            if len(parts) != 3 or parts[0] != "examples":
                problems.append(f"case {case['id']}: config {cfg} is outside an example")
                continue
            item = (declared.get(parts[1], {}).get("configs") or {}).get(parts[2])
            if not item or item.get("class") != "NON-NETWORK" or (item.get("case") or item.get("covered_by")) != case["id"]:
                problems.append(f"case {case['id']}: config {cfg} has no matching NON-NETWORK classification")
    for line in problems:
        print(f"DATA: {line}")
    print(
        f"validate: {len(found)} example dirs, "
        f"{sum(len(v['configs']) for v in found.values())} pipeline configs, "
        f"{sum(len(v['launchers']) for v in found.values())} launchers, "
        f"{len(data['cases'])} cases, {len(problems)} problems"
    )
    return 2 if problems else 0


# -------------------------------------------------------------------- inventory


def cmd_inventory(args: argparse.Namespace) -> int:
    data = load_cases(Path(args.cases))
    tree = Path(args.tree)
    found = discover(tree)
    lines = [
        "| Example | Config / launcher | Declared | Computed by the offline guard | Deciding line | Case | Note |",
        "|---|---|---|---|---|---|---|",
    ]
    disagreements = 0
    counts: dict[str, int] = {}
    for name, entry in data["examples"].items():
        configs = entry.get("configs") or {}
        launchers = entry.get("launchers") or {}
        if not configs and not launchers:
            counts[entry["class"]] = counts.get(entry["class"], 0) + 1
            lines.append(f"| {name} | (none) | {entry['class']} | - | - | - | {entry.get('note', '')} |")
            continue
        for cfg, item in configs.items():
            path = tree / "examples" / name / cfg
            findings = network_findings(tree, path) if path.is_file() else ["missing"]
            computed = "NETWORK" if findings else "NON-NETWORK"
            declared_class = item["class"]
            counts[declared_class] = counts.get(declared_class, 0) + 1
            comparable = declared_class in {"NON-NETWORK", "NETWORK"}
            agree = (not comparable) or computed == declared_class
            if not agree:
                disagreements += 1
            deciding = "; ".join(findings[:2]) if findings else offline_evidence(tree, path)
            flag = "" if agree else " **DISAGREES**"
            lines.append(
                f"| {name} | {cfg} | {declared_class} | {computed}{flag} | `{deciding}` | "
                f"{item.get('case') or item.get('covered_by') or '-'} | {item.get('note', '')} |"
            )
        for launcher, item in launchers.items():
            declared_class = item["class"]
            lines.append(
                f"| {name} | {launcher} (launcher) | {declared_class} | - | - | "
                f"{item.get('case') or item.get('covered_by') or '-'} | {item.get('note', '')} |"
            )
    summary = (
        f"Configs+empty dirs by declared class: {json.dumps(counts, sort_keys=True)}; "
        f"guard disagreements: {disagreements}; example dirs: {len(found)}"
    )
    text = "\n".join([summary, "", *lines]) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(summary)
    return 1 if disagreements else 0


# ------------------------------------------------------------------------- list


def _selected_cases(data: dict[str, Any], only_arg: str | None) -> list[dict[str, Any]]:
    """The cases --only selects, in case-file order; an id that names no case is refused, never dropped."""
    if not only_arg:
        return list(data["cases"])
    only = set(only_arg.split(","))
    unknown = sorted(only - {case["id"] for case in data["cases"]})
    if unknown:
        raise DataError(f"--only names no case: {', '.join(unknown)}")
    return [case for case in data["cases"] if case["id"] in only]


def cmd_list(args: argparse.Namespace) -> int:
    data = load_cases(Path(args.cases))
    for case in _selected_cases(data, args.only):
        server = case.get("server") or {}
        env = " ".join(f"{k}={v}" for k, v in (case.get("env") or {}).items())
        fields = [
            case["id"],
            case["command"],
            server.get("command", ""),
            str(server.get("port", "")),
            ",".join(str(p) for p in case.get("ports", [])),
            env,
            "1" if case.get("fingerprint_key") else "0",
            ",".join((case.get("requires") or {}).get("home_ro", [])),
            case.get("skip", ""),
        ]
        # \x1f (unit separator): bash `read` collapses runs of whitespace IFS characters,
        # so a tab separator would silently shift every field after an empty one.
        if any("\x1f" in f or "\n" in f for f in fields):
            raise DataError(f"case {case['id']}: separator or newline in a driver field")
        print("\x1f".join(fields))
    return 0


def cmd_count(args: argparse.Namespace) -> int:
    data = load_cases(Path(args.cases))
    print(len(_selected_cases(data, args.only)))
    return 0


def cmd_excluded(args: argparse.Namespace) -> int:
    """One SKIP line per config/dir the gate does not run because it is not NON-NETWORK."""
    data = load_cases(Path(args.cases))
    for name, entry in data["examples"].items():
        configs = entry.get("configs") or {}
        if not configs and entry["class"] != "NON-NETWORK":
            print(f"SKIP({entry['class'].lower()}) {name} -- {entry.get('note') or entry.get('table', '')}")
        for cfg, item in configs.items():
            if item["class"] in {"NETWORK", "NOT-A-PIPELINE", "UNSURE"}:
                print(f"SKIP({item['class'].lower()}) {name}/{cfg} -- {item.get('note') or entry.get('table', '')}")
    return 0


# -------------------------------------------------------------------- preflight


def cmd_preflight(args: argparse.Namespace) -> int:
    data = load_cases(Path(args.cases))
    case = case_by_id(data, args.case)
    tree = Path(args.tree)
    refusals = []
    deferred: frozenset[str] = frozenset()
    if "generated_inputs" in case:
        # A declared generated input does not exist until the case's own launcher writes it, so the
        # text guard cannot read it now. Deferring that read to after the run is acceptable only
        # because the case runs in the sandbox, which has no network: without this run's self-test
        # proof nothing is deferred and the missing input is refused as before.
        if not sandbox_in_force(args.sandbox_proof, args.nonce):
            refusals.append("generated inputs: no sandbox self-test proof for this run; not deferring the offline guard")
        elif not args.scratch:
            refusals.append("generated inputs: no scratch directory for the ignore check")
        else:
            gen_refusals = generated_input_refusals(tree, case, Path(args.scratch))
            refusals.extend(gen_refusals)
            if not gen_refusals:
                deferred = frozenset(generated_inputs_of(case))
    environment = case_environment(case)
    for cfg in case["configs"]:
        for finding in network_findings(tree, tree / cfg, environment=environment, deferred=deferred):
            refusals.append(f"offline guard: {finding}")
    requires = case.get("requires") or {}
    for module in requires.get("python_modules", []):
        if importlib.util.find_spec(module) is None:
            refusals.append(f"prerequisite: python module {module} not importable")
    for raw in requires.get("files", []):
        if not (Path.home() / raw[2:]).is_file():
            refusals.append(f"prerequisite: file {raw} absent (would be downloaded; refusing)")
    for line in refusals:
        print(line)
    if not refusals:
        note = f"; {len(deferred)} generated input(s) inspected after the run" if deferred else ""
        print(f"preflight ok: {len(case['configs'])} config(s) offline by construction{note}")
    return 1 if refusals else 0


def cmd_reset_audit(args: argparse.Namespace) -> int:
    case = case_by_id(load_cases(Path(args.cases)), args.case)
    root = Path(args.root)
    for spec in case.get("audit", []):
        path = root / spec["db"]
        for stale in (path, Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal")):
            if stale.is_file():
                stale.unlink()
                print(f"removed stale audit file {stale.relative_to(root)}")
    return 0


def cmd_classify(args: argparse.Namespace) -> int:
    if Path(args.server_marker).exists():
        print("fixture server did not listen")
        return 1
    exit_file = Path(args.exit_file)
    if not exit_file.is_file():
        print(f"sandbox error: command exit file missing (bwrap exit {args.sandbox_rc})")
        return 1
    value = exit_file.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"0|[1-9][0-9]{0,2}", value) or int(value) > 255 or int(value) != args.sandbox_rc:
        print(f"sandbox error: invalid command exit file or bwrap exit mismatch ({args.sandbox_rc})")
        return 1
    print(value)
    return 0


def cmd_totals(args: argparse.Namespace) -> int:
    print(
        f"totals: PASS={args.pass_count} FAIL={args.fail_count} NOTRUN={args.notrun_count} "
        f"SKIP={args.skip_count} selected={args.selected} total={args.total} "
        f"scope={'PARTIAL' if args.selected < args.total else 'FULL'} frozen={args.frozen} wall={args.wall}s"
    )
    return 0


def cmd_overall(args: argparse.Namespace) -> int:
    print(int(not (args.pass_count > 0 and args.fail_count == 0 and args.notrun_count == 0 and args.frozen == "YES")))
    return 0


# ------------------------------------------------------------------------ check


def _ro(db: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _run_facts(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    q = connection.execute
    facts: dict[str, Any] = {}
    facts["source_rows"] = q("SELECT COUNT(*) FROM rows WHERE run_id=?", (run_id,)).fetchone()[0]
    facts["sinks"] = dict(
        q(
            "SELECT sink_name, COUNT(*) FROM token_outcomes WHERE run_id=? AND completed=1 AND sink_name IS NOT NULL GROUP BY sink_name",
            (run_id,),
        ).fetchall()
    )
    facts["outcomes"] = {
        f"{o}/{p}": n
        for o, p, n in q(
            "SELECT outcome, path, COUNT(*) FROM token_outcomes WHERE run_id=? AND completed=1 GROUP BY outcome, path", (run_id,)
        ).fetchall()
    }
    facts["transform_errors"] = q("SELECT COUNT(*) FROM transform_errors WHERE run_id=?", (run_id,)).fetchone()[0]
    # Rows whose tokens do not carry exactly one completed terminal outcome (1:1 pipelines only).
    facts["rows_without_exactly_one_terminal"] = q(
        "SELECT COUNT(*) FROM rows r WHERE r.run_id=? AND (SELECT COUNT(*) FROM token_outcomes o "
        "JOIN tokens t ON t.token_id=o.token_id AND t.run_id=o.run_id "
        "WHERE t.row_id=r.row_id AND o.run_id=r.run_id AND o.completed=1) != 1",
        (run_id,),
    ).fetchone()[0]
    return facts


def _check_run_spec(root: Path, spec: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return (failures, evidence) for one audit-DB expectation block."""
    failures: list[str] = []
    evidence: list[str] = []
    db = root / spec["db"]
    label = spec.get("label", Path(spec["db"]).name)
    if not db.is_file():
        return [f"{label}: audit DB {spec['db']} absent"], evidence
    with _ro(db) as connection:
        mode = spec.get("mode", "live")
        runs = connection.execute("SELECT run_id, status FROM runs WHERE run_mode=? ORDER BY started_at", (mode,)).fetchall()
        want_runs = spec.get("runs")
        if want_runs is not None and len(runs) != want_runs:
            failures.append(f"{label}: {len(runs)} {mode} run(s) != {want_runs}")
        index = spec.get("run_index", -1)
        if not runs or not (-len(runs) <= index < len(runs)):
            failures.append(f"{label}: no {mode} run at index {index}")
            return failures, evidence
        run_id, status = runs[index]
        facts = _run_facts(connection, run_id)
        evidence.append(f"{label}: status={status} rows={facts['source_rows']} sinks={facts['sinks']}")
        statuses = spec.get("status")
        if statuses is not None and status not in statuses:
            failures.append(f"{label}: status {status} not in {statuses}")
        if "source_rows" in spec and facts["source_rows"] != spec["source_rows"]:
            failures.append(f"{label}: source rows {facts['source_rows']} != {spec['source_rows']}")
        if facts["source_rows"] == 0 and not spec.get("allow_zero_rows"):
            failures.append(f"{label}: zero source rows")
        for sink, want in (spec.get("sinks") or {}).items():
            got = facts["sinks"].get(sink, 0)
            if got != want:
                failures.append(f"{label}: sink {sink} {got} != {want}")
        for key, want in (spec.get("outcomes") or {}).items():
            got = facts["outcomes"].get(key, 0)
            if got != want:
                failures.append(f"{label}: outcome {key} {got} != {want}")
        if spec.get("conservation"):
            bad = facts["rows_without_exactly_one_terminal"]
            evidence.append(
                f"{label}: conservation rows_without_exactly_one_terminal={bad} "
                f"transform_errors={facts['transform_errors']} outcomes={facts['outcomes']}"
            )
            if bad:
                failures.append(f"{label}: {bad} row(s) lack exactly one terminal outcome")
            failed = sum(n for k, n in facts["outcomes"].items() if k.startswith("failure/"))
            if failed and facts["transform_errors"] == 0:
                failures.append(f"{label}: {failed} failure outcome(s) but no transform_errors reason")
        for sql_check in spec.get("sql", []):
            rows = connection.execute(sql_check["query"], {"run_id": run_id}).fetchall()
            got = [list(r) for r in rows]
            evidence.append(f"{label}: {sql_check['name']}={got}")
            if got != sql_check["expect"]:
                failures.append(f"{label}: {sql_check['name']} {got} != {sql_check['expect']}")
    return failures, evidence


def cmd_check(args: argparse.Namespace) -> int:
    data = load_cases(Path(args.cases))
    case = case_by_id(data, args.case)
    root = Path(args.root)
    failures: list[str] = []
    evidence: list[str] = [f"exit={args.exit}"]
    expect_exit = case["expect_exit"]
    if args.exit not in expect_exit:
        failures.append(f"exit {args.exit} not in {expect_exit}")
    log_text = Path(args.log).read_text(encoding="utf-8", errors="replace") if Path(args.log).is_file() else ""
    for marker in case.get("log_markers", []):
        text, count = (marker, 1) if isinstance(marker, str) else (marker["text"], marker.get("count", 1))
        got = log_text.count(text)
        if got < count:
            failures.append(f"log marker {text!r} x{got} < {count}")
    for spec in case.get("audit", []):
        try:
            more_failures, more_evidence = _check_run_spec(root, spec)
        except (sqlite3.Error, OSError, KeyError) as error:  # evidence capture failed: that is a FAIL, not a skip
            more_failures, more_evidence = [f"{spec.get('db')}: capture error {type(error).__name__}: {error}"], []
        failures.extend(more_failures)
        evidence.extend(more_evidence)
    more_failures, more_evidence = generated_input_failures(root, case)
    failures.extend(more_failures)
    evidence.extend(more_evidence)
    for rel, want in (case.get("files") or {}).items():
        path = root / rel
        if want == "absent_or_empty":
            ok = (not path.exists()) or path.stat().st_size == 0
            evidence.append(f"{rel}: {'absent' if not path.exists() else path.stat().st_size}")
            if not ok:
                failures.append(f"{rel} should be absent or empty")
    verdict = "PASS" if not failures else "FAIL"
    record = {
        "case": case["id"],
        "verdict": verdict,
        "failures": failures,
        "evidence": evidence,
        "expectation_source": case["expectation_source"],
    }
    Path(args.evidence).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    detail = "; ".join(failures) if failures else "; ".join(evidence)
    print(f"{verdict} {case['id']} -- {detail}")
    return 0 if verdict == "PASS" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("validate")
    p.add_argument("--cases", required=True)
    p.add_argument("--tree", required=True)
    p = sub.add_parser("inventory")
    p.add_argument("--cases", required=True)
    p.add_argument("--tree", required=True)
    p.add_argument("--out")
    p = sub.add_parser("list")
    p.add_argument("--cases", required=True)
    p.add_argument("--only")
    p = sub.add_parser("count")
    p.add_argument("--cases", required=True)
    p.add_argument("--only")
    p = sub.add_parser("digest")
    p.add_argument("--tree", required=True)
    p.add_argument("--manifest", required=True)
    p = sub.add_parser("excluded")
    p.add_argument("--cases", required=True)
    p = sub.add_parser("preflight")
    p.add_argument("--cases", required=True)
    p.add_argument("--tree", required=True)
    p.add_argument("--case", required=True)
    p.add_argument("--sandbox-proof", help="this run's self-test output (generated inputs only)")
    p.add_argument("--nonce", help="this run's self-test nonce (generated inputs only)")
    p.add_argument("--scratch", help="a gate-owned directory for the ignore check (generated inputs only)")
    p = sub.add_parser("check")
    p.add_argument("--cases", required=True)
    p.add_argument("--root", required=True)
    p.add_argument("--case", required=True)
    p.add_argument("--exit", type=int, required=True)
    p.add_argument("--log", required=True)
    p.add_argument("--evidence", required=True)
    p = sub.add_parser("reset-audit")
    p.add_argument("--cases", required=True)
    p.add_argument("--root", required=True)
    p.add_argument("--case", required=True)
    p = sub.add_parser("classify")
    p.add_argument("--server-marker", required=True)
    p.add_argument("--exit-file", required=True)
    p.add_argument("--sandbox-rc", type=int, required=True)
    p = sub.add_parser("totals")
    p.add_argument("--pass-count", type=int, required=True)
    p.add_argument("--fail-count", type=int, required=True)
    p.add_argument("--notrun-count", type=int, required=True)
    p.add_argument("--skip-count", type=int, required=True)
    p.add_argument("--selected", type=int, required=True)
    p.add_argument("--total", type=int, required=True)
    p.add_argument("--frozen", choices=["YES", "NO"], required=True)
    p.add_argument("--wall", type=int, required=True)
    p = sub.add_parser("overall")
    p.add_argument("--pass-count", type=int, required=True)
    p.add_argument("--fail-count", type=int, required=True)
    p.add_argument("--notrun-count", type=int, required=True)
    p.add_argument("--frozen", choices=["YES", "NO"], required=True)
    p = sub.add_parser("socket-walk")
    p.add_argument("--allow", action="append", default=[], help="an allowed bind root besides the system trees")
    p.add_argument("--shared-count", type=int, default=0, help="leading bwrap options already walked for sockets this run")
    p.add_argument("bwrap_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    handlers = {
        "validate": cmd_validate,
        "inventory": cmd_inventory,
        "list": cmd_list,
        "count": cmd_count,
        "digest": cmd_digest,
        "excluded": cmd_excluded,
        "preflight": cmd_preflight,
        "check": cmd_check,
        "reset-audit": cmd_reset_audit,
        "classify": cmd_classify,
        "totals": cmd_totals,
        "overall": cmd_overall,
        "socket-walk": cmd_socket_walk,
    }
    try:
        return handlers[args.command](args)
    except DataError as error:
        print(f"DATA ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
