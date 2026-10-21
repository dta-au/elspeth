"""One-call worker entrypoint, launched only by the independent supervisor.

The root-owned service profile is mounted at a fixed path by the supervisor.
There is no environment flag or caller-supplied import/executable selection.
Provider credentials belong to this worker's independently provisioned home.
"""

from __future__ import annotations

import os
import stat
import struct
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from elspeth_lints.core.judge import (
    _TRANSPORTS,
    AgentToolScope,
    JudgeConfigurationError,
    JudgeContractError,
    JudgeTransportError,
)
from elspeth_lints.core.judge_rpc import (
    MAX_FRAME_BYTES,
    SUPPORTED_PROFILE,
    decode_json,
    digest,
    frame_bytes,
    integer,
    object_fields,
    parse_observation,
    request_type,
    response_envelope,
    text,
    validate_request,
)

PROFILE_PATH = Path("/run/elspeth-worker.json")
PROVIDER_HOME = Path("/provider")
EVIDENCE_ROOT = Path("/evidence")


@dataclass(frozen=True, slots=True)
class _WorkerExecution:
    transport: str
    request_id: str
    request_sha256: str
    evidence_sha256: str


_execution: _WorkerExecution | None = None


def require_worker_execution() -> None:
    """Prevent accidental direct provider startup in the signer process."""
    if _execution is None:
        raise JudgeConfigurationError("provider startup requires an admitted independently supervised worker")


def _admit_profile(request: dict[str, Any]) -> None:
    from elspeth_lints.core.operator_runtime import validate_protected_path

    validate_protected_path(PROFILE_PATH)
    profile = object_fields(
        decode_json(PROFILE_PATH.read_bytes()),
        frozenset(
            {
                "version",
                "profile",
                "worker_uid",
                "worker_gid",
                "transport",
                "request_id",
                "request_sha256",
                "evidence_sha256",
                "authentication",
            }
        ),
    )
    if type(profile["version"]) is not int or profile["version"] != 1 or profile["profile"] != SUPPORTED_PROFILE:
        raise JudgeConfigurationError("the worker service profile is unavailable")
    uid = integer(profile["worker_uid"], minimum=1, maximum=2**31 - 1)
    gid = integer(profile["worker_gid"], minimum=1, maximum=2**31 - 1)
    if (os.getuid(), os.geteuid(), os.getgid(), os.getegid()) != (uid, uid, gid, gid):
        raise JudgeConfigurationError("the worker principal does not match its independently mounted profile")
    for key in ("transport", "request_id", "request_sha256", "evidence_sha256"):
        if type(profile[key]) is not type(request[key]) or profile[key] != request[key]:
            raise JudgeContractError("the worker request does not match its independently mounted profile")
    if profile["authentication"] not in {"subscription", "api", "bedrock", "vertex", "azure"}:
        raise JudgeConfigurationError("the worker authentication profile is unsupported")
    metadata = PROVIDER_HOME.stat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != uid or metadata.st_mode & 0o077:
        raise JudgeConfigurationError("the independent provider home is not private")
    # This profile is independently provisioned under the worker principal.
    # It is never derived from, opened by, or forwarded by the signer.
    credentials_path = PROVIDER_HOME / "provider-environment.json"
    fd = os.open(credentials_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid not in {0, uid} or metadata.st_mode & 0o077 or metadata.st_size > 65536:
            raise JudgeConfigurationError("the provider environment profile is not private")
        raw = os.read(fd, 65537)
    finally:
        os.close(fd)
    if len(raw) > 65536:
        raise JudgeConfigurationError("the provider environment profile exceeds its bound")
    environment = decode_json(raw)
    if len(environment) > 64:
        raise JudgeConfigurationError("the provider environment profile exceeds its field bound")
    for name, value in environment.items():
        # Positive provider namespaces retain supported cloud routes. Names
        # can coincide with arbitrary rekey references, but values originate
        # solely in this independent private home, never in signer custody.
        if not name.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "AWS_", "GOOGLE_", "AZURE_", "CLOUD_ML_")) and name not in {
            "OPENROUTER_API_KEY",
            "VERTEX_REGION",
        }:
            raise JudgeConfigurationError("the provider profile contains an unsupported environment name")
        if not name.replace("_", "").isalnum() or name.upper() != name:
            raise JudgeConfigurationError("the provider profile environment name is invalid")
        os.environ[name] = text(value, limit=16384)
    os.environ["HOME"] = str(PROVIDER_HOME)
    os.environ["CODEX_HOME"] = str(PROVIDER_HOME / "codex")
    os.environ["CLAUDE_CONFIG_DIR"] = str(PROVIDER_HOME / "claude")
    os.environ["TMPDIR"] = "/tmp"


def dispatch_request(request: dict[str, Any]) -> dict[str, Any]:
    """Reduce provider observations, without policy/time/signing authority."""
    require_worker_execution()
    if _execution is None or (_execution.request_id, _execution.request_sha256, _execution.transport) != (
        request["request_id"],
        request["request_sha256"],
        request["transport"],
    ):
        raise JudgeContractError("worker execution does not match the request")
    tools = request["tools"]
    scope = None
    if tools is not None:
        roots = tuple(EVIDENCE_ROOT / f"root{index}" for index in range(tools["roots"]))
        scope = AgentToolScope(allowed_roots=roots, cwd=roots[0], max_turns=tools["max_turns"])
    try:
        implementation = _TRANSPORTS[request["transport"]]
        result = implementation(request_type(request), request["model_id"], request["max_tokens"], tool_scope=scope)
        reply = response_envelope(request, observation=asdict(result))
        parse_observation(reply, request)
        return reply
    except JudgeConfigurationError:
        return response_envelope(request, error="configuration")
    except JudgeContractError:
        return response_envelope(request, error="contract")
    except (JudgeTransportError, Exception):
        # Never include exception messages, provider logs, environment values
        # or traceback frames in a reply to the signer.
        return response_envelope(request, error="transport")


def _read_request() -> dict[str, Any]:
    prefix = sys.stdin.buffer.read(4)
    if len(prefix) != 4:
        raise JudgeContractError("worker request header is incomplete")
    size = struct.unpack("!I", prefix)[0]
    if not 0 < size <= MAX_FRAME_BYTES:
        raise JudgeContractError("worker request exceeds its frame bound")
    raw = sys.stdin.buffer.read(size)
    if len(raw) != size or sys.stdin.buffer.read(1):
        raise JudgeContractError("worker request is incomplete or contains trailing data")
    return validate_request(decode_json(raw))


def main() -> int:
    global _execution
    try:
        request = _read_request()
        _admit_profile(request)
        _execution = _WorkerExecution(
            request["transport"], request["request_id"], digest(request["request_sha256"]), digest(request["evidence_sha256"])
        )
        reply = dispatch_request(request)
        sys.stdout.buffer.write(frame_bytes(reply))
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        # Admission errors are deliberately silent: no request has been
        # admitted and there is no safe correlated provider observation.
        return 2
    finally:
        _execution = None


if __name__ == "__main__":
    # Provider guards import the canonical name even when -m executes this
    # module as __main__; both names must refer to the same admitted context.
    sys.modules["elspeth_lints.core.judge_worker"] = sys.modules[__name__]
    raise SystemExit(main())
