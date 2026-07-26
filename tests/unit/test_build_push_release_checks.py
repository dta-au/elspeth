"""Build-push workflow release proof invariants."""

from __future__ import annotations

import csv
import json
import posixpath
import re
import shlex
import tomllib
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_PUSH_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-push.yaml"
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
PYPROJECT = REPO_ROOT / "pyproject.toml"
PINNED_NODE_BASE = "node:24.18.0-bookworm-slim@sha256:6f7b03f7c2c8e2e784dcf9295400527b9b1270fd37b7e9a7285cf83b6951452d"
OLD_NODE_BASE = "node:24.13.0-bookworm-slim@sha256:4660b1ca8b28d6d1906fd644abe34b2ed81d15434d26d845ef0aced307cf4b6f"
UNSUPPORTED_DOCKER_SOURCE = "<unsupported-or-dynamic-docker-source>"
IMMUTABLE_EXTERNAL_IMAGE_RE = re.compile(r"^[^@$\s]+@sha256:[0-9a-f]{64}$")


def _workflow() -> dict[str, Any]:
    raw = yaml.safe_load(BUILD_PUSH_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _build_push_job() -> dict[str, Any]:
    workflow = _workflow()
    job = workflow["jobs"]["build-push"]
    assert isinstance(job, dict)
    return job


def _job(name: str) -> dict[str, Any]:
    workflow = _workflow()
    job = workflow["jobs"][name]
    assert isinstance(job, dict)
    return job


def _step(job: dict[str, Any], step_name: str) -> dict[str, Any]:
    for step in job["steps"]:
        if step.get("name") == step_name:
            assert isinstance(step, dict)
            return step
    raise AssertionError(f"Missing step {step_name!r}")


def _step_run(job: dict[str, Any], step_name: str) -> str:
    run = _step(job, step_name).get("run")
    assert isinstance(run, str)
    return run


def _dockerfile_instructions() -> list[str]:
    instructions: list[str] = []
    current = ""
    for raw_line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        current = f"{current} {line}".strip()
        if current.endswith("\\"):
            current = current[:-1].rstrip()
            continue
        instructions.append(current)
        current = ""
    assert not current, "Dockerfile must not end with a continued instruction"
    return instructions


def _instruction_parts(instruction: str) -> tuple[str, str] | None:
    parts = instruction.strip().split(maxsplit=1)
    return (parts[0], parts[1]) if len(parts) == 2 else None


def _leading_instruction_options(remainder: str) -> tuple[list[str], str] | None:
    options: list[str] = []
    remainder = remainder.lstrip()
    while remainder.startswith("--"):
        match = re.match(r"^(--\S+)\s+(.+)$", remainder, flags=re.DOTALL)
        if match is None:
            return None
        option, remainder = match.groups()
        options.append(option)
        remainder = remainder.lstrip()
    return options, remainder


def _trusted_noncontext_source(reference: str, prior_stages: set[str]) -> bool:
    return reference.casefold() in prior_stages or IMMUTABLE_EXTERNAL_IMAGE_RE.fullmatch(reference) is not None


def _unsupported_docker_pattern(value: str) -> bool:
    return any(character in value for character in "[]\\")


def _docker_context_sources(instruction: str, prior_stages: set[str] | None = None) -> list[str]:
    parts = _instruction_parts(instruction)
    if parts is None or parts[0].upper() not in {"COPY", "ADD"}:
        return []
    _opcode, remainder = parts

    parsed_options = _leading_instruction_options(remainder)
    if parsed_options is None:
        return [UNSUPPORTED_DOCKER_SOURCE]
    options, remainder = parsed_options

    from_values: list[str] = []
    exclusions: list[str] = []
    for option in options:
        if option == "--from" or option == "--exclude":
            return [UNSUPPORTED_DOCKER_SOURCE]
        if option.startswith("--from="):
            from_values.append(option.removeprefix("--from="))
        elif option.startswith("--exclude="):
            exclusions.append(option.removeprefix("--exclude="))

    if len(from_values) > 1:
        return [UNSUPPORTED_DOCKER_SOURCE]
    if from_values:
        reference = from_values[0]
        return [] if _trusted_noncontext_source(reference, prior_stages or set()) else [UNSUPPORTED_DOCKER_SOURCE]

    if any(
        not exclusion or "$" in exclusion or exclusion.startswith("!") or _unsupported_docker_pattern(exclusion) for exclusion in exclusions
    ):
        return [UNSUPPORTED_DOCKER_SOURCE]

    if remainder.startswith("["):
        try:
            arguments = json.loads(remainder)
        except (json.JSONDecodeError, TypeError):
            return [UNSUPPORTED_DOCKER_SOURCE]
        if not isinstance(arguments, list) or len(arguments) < 2 or not all(isinstance(value, str) for value in arguments):
            return [UNSUPPORTED_DOCKER_SOURCE]
    else:
        if "\\" in remainder:
            return [UNSUPPORTED_DOCKER_SOURCE]
        try:
            arguments = shlex.split(remainder)
        except ValueError:
            return [UNSUPPORTED_DOCKER_SOURCE]
        if len(arguments) < 2:
            return [UNSUPPORTED_DOCKER_SOURCE]

    sources = arguments[:-1]
    if any("$" in source or source.startswith("<<") or _unsupported_docker_pattern(source) for source in sources):
        return [UNSUPPORTED_DOCKER_SOURCE]

    readme_is_excluded = any(_source_covers_root_readme(exclusion) for exclusion in exclusions)
    return [source for source in sources if not (readme_is_excluded and _source_covers_root_readme(source))]


def _run_bind_context_sources(instruction: str, prior_stages: set[str]) -> list[str]:
    parts = _instruction_parts(instruction)
    if parts is None or parts[0].upper() != "RUN":
        return []
    _opcode, remainder = parts

    parsed_options = _leading_instruction_options(remainder)
    if parsed_options is None:
        return [UNSUPPORTED_DOCKER_SOURCE]
    options, _command = parsed_options
    mount_options = [option.removeprefix("--mount=") for option in options if option.startswith("--mount=")]
    if any(option == "--mount" for option in options):
        return [UNSUPPORTED_DOCKER_SOURCE]

    sources: list[str] = []
    for mount_option in mount_options:
        try:
            fields = next(csv.reader([mount_option], skipinitialspace=True, strict=True))
        except csv.Error:
            return [UNSUPPORTED_DOCKER_SOURCE]

        values: dict[str, str] = {}
        for field in fields:
            if "=" not in field:
                if field not in {"ro", "readonly", "rw", "readwrite"}:
                    return [UNSUPPORTED_DOCKER_SOURCE]
                continue
            key, value = field.split("=", 1)
            if key in values or not key or not value or "$" in value:
                return [UNSUPPORTED_DOCKER_SOURCE]
            values[key] = value

        mount_type = values.get("type", "bind")
        if mount_type in {"cache", "tmpfs", "secret", "ssh"}:
            continue
        if mount_type != "bind":
            return [UNSUPPORTED_DOCKER_SOURCE]
        if not any(key in values for key in {"target", "dst", "destination"}):
            return [UNSUPPORTED_DOCKER_SOURCE]

        unknown_keys = set(values) - {
            "type",
            "from",
            "source",
            "src",
            "target",
            "dst",
            "destination",
        }
        if unknown_keys or ("source" in values and "src" in values):
            return [UNSUPPORTED_DOCKER_SOURCE]

        reference = values.get("from")
        if reference is not None:
            if not _trusted_noncontext_source(reference, prior_stages):
                return [UNSUPPORTED_DOCKER_SOURCE]
            continue
        sources.append(values.get("source", values.get("src", ".")))
    return sources


def _stage_alias(instruction: str) -> str | None:
    tokens = shlex.split(instruction)
    if not tokens or tokens[0].upper() != "FROM":
        return None
    aliases = [index for index, token in enumerate(tokens) if token.upper() == "AS"]
    return tokens[aliases[-1] + 1].casefold() if aliases and aliases[-1] + 1 < len(tokens) else None


def _host_context_sources(instructions: list[str] | None = None) -> list[str]:
    sources: list[str] = []
    prior_stages: set[str] = set()
    current_stage: str | None = None
    for instruction in instructions if instructions is not None else _dockerfile_instructions():
        if instruction.split(maxsplit=1)[0].upper() == "FROM":
            if current_stage is not None:
                prior_stages.add(current_stage)
            current_stage = _stage_alias(instruction)
            continue
        sources.extend(_docker_context_sources(instruction, prior_stages))
        sources.extend(_run_bind_context_sources(instruction, prior_stages))
    return sources


def _source_covers_root_readme(source: str) -> bool:
    if source == UNSUPPORTED_DOCKER_SOURCE or "$" in source or source.startswith("<<") or _unsupported_docker_pattern(source):
        return True

    normalized = posixpath.normpath(source.replace("\\", "/").lstrip("/"))
    while normalized.startswith("../"):
        normalized = normalized.removeprefix("../")
    if normalized in {"", "."}:
        return True

    patterns = [normalized]
    while patterns[-1].startswith("**/"):
        patterns.append(patterns[-1].removeprefix("**/"))
    return any(fnmatchcase("README.md", pattern) or PurePosixPath("README.md").match(pattern) for pattern in patterns)


def _stage_instructions(stage_name: str, instructions: list[str] | None = None) -> list[str]:
    selected: list[str] = []
    current_stage: str | None = None
    found = False
    target = stage_name.casefold()

    for instruction in instructions if instructions is not None else _dockerfile_instructions():
        tokens = shlex.split(instruction)
        if tokens and tokens[0].upper() == "FROM":
            aliases = [index for index, token in enumerate(tokens) if token.upper() == "AS"]
            current_stage = tokens[aliases[-1] + 1].casefold() if aliases and aliases[-1] + 1 < len(tokens) else None
            if current_stage == target:
                assert not found, f"Dockerfile defines stage {stage_name!r} more than once"
                found = True
            continue
        if current_stage == target:
            selected.append(instruction)

    assert found, f"Dockerfile does not define stage {stage_name!r}"
    return selected


def test_build_push_verifies_ruleset_required_checks_for_image_sha() -> None:
    """Release image publication must mirror live branch-protection contexts."""
    job = _build_push_job()

    run = _step_run(job, "Verify required checks for image commit")

    assert "scripts/cicd/check_release_required_checks.py" in run
    assert '--sha "$IMAGE_SHA"' in run
    assert '--repo "$GITHUB_REPOSITORY"' in run
    assert "check_name=CI%20Success" not in run
    assert "Workflow run trigger already supplied successful CI conclusion" not in run


def test_build_push_grants_read_permissions_for_required_check_verifier() -> None:
    """The verifier must be able to read rulesets, check runs, and statuses."""
    job = _build_push_job()

    assert job["permissions"]["actions"] == "read"
    assert job["permissions"]["checks"] == "read"
    assert job["permissions"]["statuses"] == "read"


# ---------------------------------------------------------------------------
# elspeth-118bf5ea8c / elspeth-8cb798c3fd:
# An ACR-only manual dispatch sets push_ghcr=false, so no GHCR image is pushed.
# The smoke-test job must NOT unconditionally pull the GHCR image — it must test
# whichever registry was actually pushed.
# ---------------------------------------------------------------------------


def test_build_push_exposes_registry_push_decisions_as_outputs() -> None:
    """Downstream jobs need to know which registries were actually pushed."""
    job = _build_push_job()
    outputs = job["outputs"]

    assert "steps.registries.outputs.push_ghcr" in outputs["push_ghcr"]
    assert "steps.registries.outputs.push_acr" in outputs["push_acr"]
    assert outputs["ghcr_digest"] == "${{ steps.ghcr-push.outputs.digest }}"
    assert outputs["acr_digest"] == "${{ steps.acr-push.outputs.digest }}"
    assert "secrets." not in str(outputs)


def test_smoke_test_skipped_when_no_image_was_pushed() -> None:
    """If neither registry was pushed there is nothing to smoke-test."""
    job = _job("smoke-test")
    condition = job["if"]

    assert "needs.build-push.outputs.push_ghcr" in condition
    assert "needs.build-push.outputs.push_acr" in condition


def test_smoke_test_image_selection_is_registry_aware() -> None:
    """Smoke must consume the immutable digest from every pushed registry."""
    job = _job("smoke-test")
    step = _step(job, "Determine smoke-test images")
    run = step["run"]

    assert step["env"]["GHCR_DIGEST"] == "${{ needs.build-push.outputs.ghcr_digest }}"
    assert step["env"]["ACR_DIGEST"] == "${{ needs.build-push.outputs.acr_digest }}"
    assert step["env"]["ACR_REGISTRY"] == "${{ secrets.ACR_REGISTRY }}"
    assert 'GHCR_IMAGE="ghcr.io/${REPO_OWNER}/${IMAGE_NAME}@${GHCR_DIGEST}"' in run
    assert 'ACR_IMAGE="${ACR_REGISTRY}/${IMAGE_NAME}@${ACR_DIGEST}"' in run
    assert 'SMOKE_IMAGE="$GHCR_IMAGE"' in run
    assert 'SMOKE_IMAGE="$ACR_IMAGE"' in run
    assert ":sha-" not in run
    assert "GITHUB_ENV" in run


def test_smoke_test_run_steps_use_the_selected_image() -> None:
    """After digest selection, smoke run steps must use the selected image."""
    job = _job("smoke-test")
    for step in job["steps"]:
        run = step.get("run")
        if not isinstance(run, str) or step.get("name") == "Determine smoke-test images":
            continue
        assert 'IMAGE="ghcr.io/${REPO_OWNER}' not in run, f"hardcoded GHCR image in step {step.get('name')!r}"


def test_smoke_test_logs_into_the_pushed_registry() -> None:
    """GHCR login fires only for a GHCR smoke; an ACR login path exists too."""
    job = _job("smoke-test")
    ghcr_login = _step(job, "Login to GHCR (to pull image)")
    acr_login = _step(job, "Login to ACR (to pull image)")

    assert "ghcr" in ghcr_login["if"]
    assert "acr" in acr_login["if"]
    assert acr_login["with"]["registry"] == "${{ secrets.ACR_REGISTRY }}"


def test_release_dockerfile_builds_frontend_dist_before_python_install() -> None:
    """Release image must build the SPA instead of relying on ignored host dist."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "AS frontend-builder" in dockerfile
    assert "npm ci" in dockerfile
    assert "npm run build" in dockerfile
    assert "COPY --from=frontend-builder /frontend/dist /tmp/frontend-dist/" in dockerfile
    assert dockerfile.index("npm run build") < dockerfile.index('uv sync --frozen "$@" --no-editable --active')


def test_build_workflow_uses_the_pinned_frontend_runtime_for_both_architectures() -> None:
    """Every registry build consumes the exact Node/npm Docker contract."""
    workflow = _workflow()
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert workflow["env"]["PLATFORMS"] == "linux/amd64,linux/arm64"
    assert f"FROM {PINNED_NODE_BASE} AS frontend-builder" in dockerfile
    assert "npm install --global npm@11.6.2" in dockerfile
    assert 'test "$(node --version)" = "v24.18.0"' in dockerfile
    assert 'test "$(npm --version)" = "11.6.2"' in dockerfile
    assert OLD_NODE_BASE not in dockerfile

    for name in ("Build and push to GHCR", "Build and push to ACR"):
        step = _step(_build_push_job(), name)
        assert step["with"]["context"] == "."
        assert step["with"]["platforms"] == "${{ env.PLATFORMS }}"
        assert "build-contexts" not in step["with"]

    assert "--build-context" not in BUILD_PUSH_WORKFLOW.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "instruction",
    [
        "COPY README.md ./",
        "COPY /README.md ./",
        "COPY\t/README.md\t./",
        "COPY *.md ./",
        "COPY /*.md ./",
        "COPY . ./",
        'COPY ["README.md", "/build/"]',
        'COPY ["src/", "/README.md", "/build/"]',
        "ADD README.md ./",
        "ADD /README.md ./",
        "ADD *.md ./",
        "ADD /*.md ./",
        "ADD . ./",
        'ADD ["README.md", "/build/"]',
        'ADD ["src/", "/README.md", "/build/"]',
        "COPY ${BUILD_INPUT} ./",
    ],
)
def test_host_input_detector_rejects_any_source_that_can_cover_root_readme(instruction: str) -> None:
    sources = _host_context_sources([instruction])

    assert sources
    assert any(_source_covers_root_readme(source) for source in sources), sources


@pytest.mark.parametrize(
    ("instruction", "expected"),
    [
        ('COPY ["README.md", "/build/"]', ["README.md"]),
        ('COPY ["src/", "/README.md", "/build/"]', ["src/", "/README.md"]),
        ('ADD ["README.md", "/build/"]', ["README.md"]),
        ('ADD ["src/", "/README.md", "/build/"]', ["src/", "/README.md"]),
    ],
)
def test_host_input_detector_parses_legal_json_array_forms(instruction: str, expected: list[str]) -> None:
    assert _host_context_sources([instruction]) == expected


@pytest.mark.parametrize(
    "instruction",
    [
        "COPY ${BUILD_INPUT} ./",
        'COPY ["README.md", "/build/"',
        "ADD 'README.md ./",
        "COPY [^X]EADME.md /build/",
        "COPY [README.md /build/",
        "COPY README\\?.md /build/",
    ],
)
def test_host_input_detector_conservatively_rejects_dynamic_or_unsupported_sources(instruction: str) -> None:
    assert _host_context_sources([instruction]) == [UNSUPPORTED_DOCKER_SOURCE]
    assert _source_covers_root_readme(UNSUPPORTED_DOCKER_SOURCE)


@pytest.mark.parametrize("pattern", ["[^X]EADME.md", "[README.md", "README\\?.md"])
def test_source_matcher_treats_go_specific_or_escaped_patterns_as_readme_covered(pattern: str) -> None:
    assert _source_covers_root_readme(pattern)


@pytest.mark.parametrize(
    ("instruction", "expected"),
    [
        ("RUN --mount=type=bind,source=README.md,target=/mnt true", ["README.md"]),
        ("RUN --mount=type=bind,src=/README.md,target=/mnt true", ["/README.md"]),
        ("RUN --mount=type=bind,target=/mnt true", ["."]),
        ("RUN\t--mount=type=bind,source=README.md,target=/mnt\ttrue", ["README.md"]),
    ],
)
def test_host_input_detector_includes_context_backed_run_bind_mounts(instruction: str, expected: list[str]) -> None:
    assert _host_context_sources([instruction]) == expected
    assert any(_source_covers_root_readme(source) for source in expected)


@pytest.mark.parametrize(
    "instruction",
    [
        "RUN --mount=type=bind,source=${BUILD_INPUT},target=/mnt true",
        "RUN --mount=type=${MOUNT_TYPE},source=README.md,target=/mnt true",
        "RUN --mount=type=bind,from=assets,source=README.md,target=/mnt true",
        "RUN --mount=type=volume,source=README.md,target=/mnt true",
        "RUN --mount=${MOUNT_SPEC} true",
        "RUN --mount=type=bind,source=README.md true",
    ],
)
def test_host_input_detector_rejects_dynamic_or_named_run_bind_mounts(instruction: str) -> None:
    assert _host_context_sources([instruction]) == [UNSUPPORTED_DOCKER_SOURCE]


@pytest.mark.parametrize("external", [False, True])
def test_host_input_detector_allows_run_bind_from_trusted_noncontext_sources(external: bool) -> None:
    immutable_image = "registry.example/elspeth/assets@sha256:" + "a" * 64
    from_value = immutable_image if external else "assets"
    instructions = [
        "FROM node:24 AS assets",
        "FROM python:3.13 AS builder",
        f"RUN --mount=type=bind,from={from_value},source=/README.md,target=/mnt true",
    ]

    assert _host_context_sources(instructions) == []


@pytest.mark.parametrize(
    "from_value",
    ["${BUILD_STAGE}", "assets", "python:3.13-slim", "local-source"],
)
def test_host_input_detector_rejects_dynamic_mutable_or_named_copy_from(from_value: str) -> None:
    instructions = [
        "FROM python:3.13 AS builder",
        f"COPY --from={from_value} /README.md /build/",
    ]

    assert _host_context_sources(instructions) == [UNSUPPORTED_DOCKER_SOURCE]


@pytest.mark.parametrize("opcode", ["COPY", "ADD"])
def test_host_input_detector_allows_copy_from_an_earlier_stage_alias(opcode: str) -> None:
    instructions = [
        "FROM node:24 AS assets",
        "FROM python:3.13 AS builder",
        f"{opcode} --from=assets /README.md /build/",
    ]

    assert _host_context_sources(instructions) == []


@pytest.mark.parametrize("opcode", ["COPY", "ADD"])
def test_host_input_detector_allows_copy_from_an_immutable_external_image(opcode: str) -> None:
    immutable_image = "registry.example/elspeth/assets@sha256:" + "a" * 64
    instructions = [
        "FROM python:3.13 AS builder",
        f"{opcode} --from={immutable_image} /README.md /build/",
    ]

    assert _host_context_sources(instructions) == []


@pytest.mark.parametrize(
    "instruction",
    [
        "COPY --exclude=README.md . /build/",
        "COPY --exclude=*.md . /build/",
        "ADD --exclude=/README.md / /build/",
        "COPY --exclude=docs/** --exclude=README.* . /build/",
    ],
)
def test_host_input_detector_accepts_broad_sources_only_when_exclusions_cover_readme(instruction: str) -> None:
    sources = _host_context_sources([instruction])

    assert not any(_source_covers_root_readme(source) for source in sources), sources


@pytest.mark.parametrize(
    "instruction",
    [
        "COPY --exclude=docs/*.md . /build/",
        "ADD --exclude=${EXCLUDE_PATTERN} . /build/",
    ],
)
def test_host_input_detector_rejects_noncovering_or_dynamic_exclusions(instruction: str) -> None:
    sources = _host_context_sources([instruction])

    assert sources
    assert any(_source_covers_root_readme(source) for source in sources), sources


@pytest.mark.parametrize("pattern", ["[!X]EADME.md", "[README.md", "README\\?.md"])
def test_host_input_detector_fails_closed_on_go_specific_or_escaped_exclusions(pattern: str) -> None:
    instruction = f"COPY --exclude={pattern} . /build/"

    assert _host_context_sources([instruction]) == [UNSUPPORTED_DOCKER_SOURCE]


def test_contract_tests_never_execute_dockerfile_text_in_a_host_shell() -> None:
    module_source = Path(__file__).read_text(encoding="utf-8")

    for forbidden in ('["/bin' + '/sh", "-c"', "subprocess" + ".run("):
        assert forbidden not in module_source


@pytest.mark.parametrize(
    "instructions",
    [
        [
            "FROM node:24 AS frontend-builder",
            "RUN printf '%s\\n' '# ELSPETH package metadata' > README.md",
            "FROM python:3.13 AS builder",
            'RUN uv sync --frozen "$@" --no-editable --active',
        ],
        [
            "FROM python:3.13 AS builder",
            "RUN printf '%s\\n' '# ELSPETH package metadata' > README.md",
            "FROM python:3.13 AS runtime",
            'RUN uv sync --frozen "$@" --no-editable --active',
        ],
    ],
)
def test_builder_stage_detector_does_not_join_instructions_across_stages(instructions: list[str]) -> None:
    builder = _stage_instructions("builder", instructions)

    assert not (
        any("# ELSPETH package metadata" in instruction for instruction in builder)
        and any('uv sync --frozen "$@" --no-editable --active' in instruction for instruction in builder)
    )


def test_release_dockerfile_uses_a_deterministic_build_only_readme() -> None:
    """Hatch metadata receives fixed README bytes without repository prose."""
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    instructions = _stage_instructions("builder")
    stub_instruction = next(
        instruction for instruction in instructions if instruction.startswith("RUN ") and "# ELSPETH package metadata" in instruction
    )
    project_install = next(instruction for instruction in instructions if 'uv sync --frozen "$@" --no-editable --active' in instruction)

    assert project["readme"] == "README.md"
    expected_stub = "RUN printf '%s\\n' '# ELSPETH package metadata' > README.md && touch --date=@0 README.md"
    assert stub_instruction == expected_stub
    assert "printf '%s\\n' '# ELSPETH package metadata' > README.md" in stub_instruction
    assert stub_instruction.endswith("touch --date=@0 README.md")
    assert instructions.index(stub_instruction) < instructions.index(project_install)


def test_host_readme_is_disjoint_from_docker_image_inputs() -> None:
    """Public README edits must not invalidate or alter release image layers."""
    sources = _host_context_sources()

    assert sources
    assert not any(_source_covers_root_readme(source) for source in sources), sources


def test_release_dockerfile_prepares_the_standalone_web_runtime_contract() -> None:
    """The published image must carry a clash-resistant identity and web roots."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "groupadd --gid 1654 elspeth" in dockerfile
    assert "useradd --uid 1654 --gid elspeth" in dockerfile
    assert "/app/data/blobs" in dockerfile
    assert "/app/data/outputs" in dockerfile
    assert dockerfile.index("/app/data/blobs") < dockerfile.index("USER elspeth")


def test_release_dockerfile_keeps_builder_os_packages_out_of_the_runtime() -> None:
    """The final image must use the pinned minimal runtime proven by registry scan."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    runtime = dockerfile.split("# Stage 3: Runtime", maxsplit=1)[1]

    assert (
        "FROM gcr.io/distroless/python3-debian13:debug-nonroot"
        "@sha256:6418f576f2011f5d265d03f53aee812b4efcba5c6646a3f4d855b9fb51cd2d72 AS runtime"
    ) in runtime
    assert "COPY --from=builder /opt/venv /opt/venv" in runtime
    assert "COPY --from=builder /runtime-root/ /" in runtime
    assert "RUN " not in runtime
    assert 'ENTRYPOINT ["/opt/venv/bin/elspeth"]' in runtime
    assert "ln -s /busybox/sh /runtime-root/usr/bin/sh" in dockerfile


def test_registry_smoke_checks_runtime_identity_and_web_directories() -> None:
    """Each independently published registry image must prove the same runtime contract."""
    workflow = BUILD_PUSH_WORKFLOW.read_text(encoding="utf-8")

    assert 'test "$(docker run --rm --entrypoint id "$image" -u)" = "1654"' in workflow
    assert 'test "$(docker run --rm --entrypoint id "$image" -g)" = "1654"' in workflow
    assert "test -d /app/data/blobs" in workflow
    assert "test -d /app/data/outputs" in workflow


def test_release_build_context_excludes_host_node_modules() -> None:
    """Host-installed frontend dependencies must not enter the Docker context."""
    raw_lines = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    dockerignore_patterns = {line.strip() for line in raw_lines if line.strip() and not line.lstrip().startswith("#")}

    assert "**/node_modules/" in dockerignore_patterns


def test_release_build_context_excludes_frontend_unit_tests() -> None:
    """Production SPA compilation must not depend on test-only source fixtures."""
    raw_lines = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    dockerignore_patterns = {line.strip() for line in raw_lines if line.strip() and not line.lstrip().startswith("#")}

    assert "src/elspeth/web/frontend/src/**/*.test.ts" in dockerignore_patterns
    assert "src/elspeth/web/frontend/src/**/*.test.tsx" in dockerignore_patterns


def test_release_dockerfile_copies_local_uv_sources_before_dependency_sync() -> None:
    """Root pyproject local uv sources must exist before Docker runs uv sync."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY elspeth-lints/ ./elspeth-lints/" in dockerfile
    assert dockerfile.index("COPY elspeth-lints/ ./elspeth-lints/") < dockerfile.index(
        'uv sync --frozen "$@" --no-install-project --active'
    )


def test_release_dockerfile_copies_frontend_dist_into_installed_package() -> None:
    """The non-editable Docker install must carry generated SPA assets into site-packages."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY --from=frontend-builder /frontend/dist /tmp/frontend-dist/" in dockerfile
    assert "import elspeth.web" in dockerfile
    assert 'shutil.copytree("/tmp/frontend-dist", target)' in dockerfile
    assert dockerfile.index('uv sync --frozen "$@" --no-editable --active') < dockerfile.index(
        'shutil.copytree("/tmp/frontend-dist", target)'
    )


def test_release_dockerfile_makes_frontend_assets_readable_by_runtime_identity() -> None:
    """Generated SPA files must remain readable after the image switches away from root."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    directory_mode = "find /tmp/frontend-dist -type d -exec chmod 0755 {} +"
    file_mode = "find /tmp/frontend-dist -type f -exec chmod 0644 {} +"
    copy_assets = 'shutil.copytree("/tmp/frontend-dist", target)'

    assert directory_mode in dockerfile
    assert file_mode in dockerfile
    assert dockerfile.index(directory_mode) < dockerfile.index(copy_assets)
    assert dockerfile.index(file_mode) < dockerfile.index(copy_assets)


def _extras_validation_blocks(dockerfile: str | None = None) -> list[str]:
    dockerfile = dockerfile if dockerfile is not None else DOCKERFILE.read_text(encoding="utf-8")
    lines = dockerfile.splitlines()
    marker = '    test -n "$INSTALL_EXTRAS" && \\'
    blocks: list[str] = []
    for start, line in enumerate(lines):
        if line != marker:
            continue
        end = start
        while lines[end].endswith("\\"):
            end += 1
            assert end < len(lines), "extras validator may not end with an unterminated continuation"
        blocks.append("\n".join([lines[start].removeprefix("    "), *lines[start + 1 : end + 1]]))
    assert len(blocks) == 2, "both dependency sync layers must use the same extras validator"
    return blocks


def _canonical_extras_validation_block(install_mode: str) -> str:
    assert install_mode in {"--no-install-project", "--no-editable"}
    return f"""test -n "$INSTALL_EXTRAS" && \\
    set -f && \\
    set -- && \\
    for e in $INSTALL_EXTRAS; do \\
        case "$e" in [a-z0-9]*) ;; *) exit 2 ;; esac; \\
        case "$e" in *[!a-z0-9-]*) exit 2 ;; esac; \\
        set -- "$@" --extra "$e"; \\
    done && \\
    test "$#" -gt 0 && \\
    uv sync --frozen "$@" {install_mode} --active"""


def _assert_canonical_extras_validation_blocks(blocks: list[str]) -> None:
    assert blocks == [
        _canonical_extras_validation_block("--no-install-project"),
        _canonical_extras_validation_block("--no-editable"),
    ]


def test_release_dockerfile_defaults_to_all_extras_and_validates_both_sync_layers() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    blocks = _extras_validation_blocks()

    assert dockerfile.count('ARG INSTALL_EXTRAS="all"') == 1
    _assert_canonical_extras_validation_blocks(blocks)


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ('test -n "$INSTALL_EXTRAS"', 'test -z "$INSTALL_EXTRAS"'),
        ("set -f", "set +f"),
        ('case "$e" in [a-z0-9]*) ;; *) exit 2 ;; esac;', 'case "$e" in [a-z0-9]*) ;; *) exit 0 ;; esac;'),
        ('set -- "$@" --extra "$e"', 'set -- "$@" --group "$e"'),
        ('test "$#" -gt 0', 'test "$#" -ge 0'),
        ('uv sync --frozen "$@" --no-install-project --active', 'uv sync --frozen "$@" --active'),
    ],
)
def test_extras_validator_canonical_comparison_rejects_behavior_mutations(original: str, replacement: str) -> None:
    blocks = [
        _canonical_extras_validation_block("--no-install-project"),
        _canonical_extras_validation_block("--no-editable"),
    ]
    blocks[0] = blocks[0].replace(original, replacement, 1)

    with pytest.raises(AssertionError):
        _assert_canonical_extras_validation_blocks(blocks)


def test_extras_validator_canonical_comparison_rejects_guard_reordering() -> None:
    first_guard = '        case "$e" in [a-z0-9]*) ;; *) exit 2 ;; esac; \\\n'
    second_guard = '        case "$e" in *[!a-z0-9-]*) exit 2 ;; esac; \\\n'
    canonical = _canonical_extras_validation_block("--no-install-project")
    reordered = canonical.replace(first_guard + second_guard, second_guard + first_guard, 1)

    with pytest.raises(AssertionError):
        _assert_canonical_extras_validation_blocks([reordered, _canonical_extras_validation_block("--no-editable")])


def test_extras_validator_extraction_preserves_trailing_behavior_for_canonical_rejection() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    original = '    uv sync --frozen "$@" --no-install-project --active'
    mutated = original + " && \\\n    false"

    blocks = _extras_validation_blocks(dockerfile.replace(original, mutated, 1))

    assert blocks[0].endswith("false")
    assert blocks[0] != _canonical_extras_validation_block("--no-install-project")


def test_postgres_extra_supports_both_accepted_sqlalchemy_url_forms() -> None:
    """The production postgres extra must install both SQLAlchemy defaults."""
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    dependencies = project["optional-dependencies"]["postgres"]

    assert any(dependency.startswith("psycopg[") for dependency in dependencies)
    assert any(dependency.startswith("psycopg2-binary") for dependency in dependencies)


def test_release_dockerfile_records_selected_extras_on_the_runtime_image() -> None:
    """Operators must be able to prove which extras a built artifact contains."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert 'LABEL io.elspeth.install-extras="$INSTALL_EXTRAS"' in dockerfile


def test_release_workflow_proves_lean_postgres_image_before_registry_push() -> None:
    """The ECS lean extras set must pass its runtime contract before publication."""
    job = _build_push_job()
    run = _step_run(job, "Verify lean PostgreSQL image contract")

    assert 'INSTALL_EXTRAS="webui llm aws postgres"' in run
    assert "import psycopg" in run
    assert "import psycopg2" in run
    assert "postgresql://" in run
    assert "postgresql+psycopg://" in run
    step_names = [step.get("name") for step in job["steps"]]
    assert step_names.index("Verify lean PostgreSQL image contract") < step_names.index("Build and push to GHCR")


def test_generic_registry_builds_explicitly_select_all_extras() -> None:
    """Generic GHCR/ACR artifacts may never inherit a lean build selection."""
    job = _build_push_job()

    for name in ("Build and push to GHCR", "Build and push to ACR"):
        build_args = _step(job, name)["with"]["build-args"]
        assert build_args == "INSTALL_EXTRAS=all"


def test_version_tags_are_promoted_only_after_smoke_verifies_the_digest() -> None:
    """A version tag must point only at the already-smoked immutable digest."""
    build_job = _build_push_job()
    for name in ("Build and push to GHCR", "Build and push to ACR"):
        tags = _step(build_job, name)["with"]["tags"]
        assert "github.ref_name" not in tags

    smoke_job = _job("smoke-test")
    verify = _step_run(smoke_job, "Verify generic image runtime contract")
    assert 'for image in "$GHCR_IMAGE" "$ACR_IMAGE"' in verify
    assert 'docker pull "$image"' in verify
    assert "docker inspect --format" in verify
    assert 'docker run --rm --interactive --entrypoint python "$image"' in verify

    release_job = _job("release")
    assert "smoke-test" in release_job["needs"]
    promote = _step_run(release_job, "Promote verified image digest to release tag")
    assert "docker buildx imagetools create" in promote
    assert "needs.build-push.outputs.ghcr_digest" in promote
    assert "needs.build-push.outputs.acr_digest" in promote


def test_release_dockerfile_documents_orchestrator_owned_probe_wiring() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "Web task definitions: loopback GET /api/health" in dockerfile
    assert "ALB target groups:     GET /api/ready" in dockerfile
    assert "Batch tasks:           process exit code" in dockerfile
    assert "elspeth health --port 8451" not in dockerfile
