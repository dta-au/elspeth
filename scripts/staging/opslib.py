"""Shared support for the staging operator tools.

This module replaces the git-ignored lane helpers the delivered staging
scripts imported. It owns four boundaries, each injectable so the tools can be
tested without a network, Docker, sudo or cloud credentials:

* :class:`WebClient` -- authenticated calls to the ELSPETH web API through an
  :class:`HttpTransport` (``urllib`` by default).
* :class:`CommandRunner` -- local subprocesses (``docker``, ``sudo -n``).
* :class:`CheckpointStore` -- private JSON receipts and resume checkpoints in
  an operator-chosen directory outside the repository.
* graph comparison -- :func:`graph_content`, :func:`api_projection`,
  :func:`clean_node` and :func:`differences`.

Bearer tokens come from ``POST /api/auth/login`` with a password read from an
environment variable or a hidden prompt, or from a pre-issued token in an
environment variable. This module never mints a token for an account.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import copy
import getpass
import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Protocol
from uuid import UUID, uuid4

type JsonObject = dict[str, Any]

REPO_ROOT = Path(__file__).resolve().parents[2]
CHUNK_BYTES = 4 * 1024 * 1024
_ERROR_DETAIL_LIMIT = 500
_RECEIPT_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,200}$")


class UsageError(Exception):
    """A required value is missing or malformed. Tools exit 2 on it."""


class OperationFailed(Exception):
    """A safety check or remote call failed. Tools exit 1 on it."""


# ── arguments ──────────────────────────────────────────────────────────────


def setting(value: str | None, *, flag: str, env: str) -> str:
    """Return the flag value, else the environment value; refuse when neither is set.

    There is deliberately no fallback default: every deployment value must be
    named by the operator for this run.
    """
    if value is not None and value != "":
        return value
    from_env = os.environ.get(env)
    if from_env is not None and from_env != "":
        return from_env
    raise UsageError(f"{flag} is required (or set {env})")


def require_uuid(value: str, *, flag: str) -> str:
    """Return ``value`` when it is a canonical UUID; refuse otherwise."""
    try:
        parsed = UUID(value)
    except ValueError:
        raise UsageError(f"{flag} must be a UUID") from None
    if str(parsed) != value:
        raise UsageError(f"{flag} must be a canonical lowercase UUID")
    return value


def add_execute_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--execute",
        action="store_true",
        help="act; without it the tool only prints what it would do (dry run)",
    )


def run_tool(main: Callable[[Sequence[str] | None], int], argv: Sequence[str] | None = None) -> int:
    """Run a tool's ``main`` and map its failures to exit codes 2 (usage) and 1 (failure)."""
    try:
        return main(argv)
    except UsageError as exc:
        print(f"usage error: {exc}", file=sys.stderr)
        return 2
    except OperationFailed as exc:
        emit("operation_failed", reason=str(exc))
        return 1


def emit(event: str, **values: object) -> None:
    """Print one JSON event line. Callers pass only non-secret values."""
    print(json.dumps({"event": event, **values}, default=str, sort_keys=True), flush=True)


# ── secrets ────────────────────────────────────────────────────────────────


def redact(text: str, secrets: Sequence[str]) -> str:
    """Replace every non-empty secret value in ``text`` with ``<redacted>``."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def read_secret(*, env: str | None, prompt: str, prompt_secret: Callable[[str], str] = getpass.getpass) -> str:
    """Read a secret from the named environment variable, else a hidden prompt."""
    if env is not None:
        value = os.environ.get(env)
        if value is None or value == "":
            raise UsageError(f"environment variable {env} is not set")
        return value
    value = prompt_secret(prompt)
    if value == "":
        raise UsageError("no secret entered")
    return value


# ── HTTP ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


class HttpTransport(Protocol):
    def __call__(self, method: str, url: str, *, headers: Mapping[str, str], body: bytes | None, timeout: float) -> HttpResponse: ...


def urllib_transport(method: str, url: str, *, headers: Mapping[str, str], body: bytes | None, timeout: float) -> HttpResponse:
    """Send one request with ``urllib``; HTTP error statuses are returned, not raised."""
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        # The scheme and host are checked by validate_base_url before any call.
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(status=response.status, body=response.read(), headers=dict(response.headers.items()))
    except urllib.error.HTTPError as exc:
        return HttpResponse(status=exc.code, body=exc.read(), headers=dict(exc.headers.items()))


class ApiError(OperationFailed):
    def __init__(self, *, method: str, path: str, status: int, detail: str) -> None:
        super().__init__(f"{method} {path} returned HTTP {status}: {detail}")
        self.status = status


def validate_base_url(base_url: str) -> str:
    """Require ``https://host``, or ``http://`` to a loopback host, with no path, query or credentials."""
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.username is not None or parsed.password is not None:
        raise UsageError("--base-url must not carry credentials")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment or not parsed.hostname:
        raise UsageError("--base-url must be a bare origin such as https://host.example")
    if parsed.scheme == "https":
        return base_url.rstrip("/")
    if parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1"):
        return base_url.rstrip("/")
    raise UsageError("--base-url must use https (plain http is accepted only for a loopback host)")


class WebClient:
    """A bearer-token client for the ELSPETH web API.

    The token never appears in an error message: every error text is passed
    through :func:`redact` with the current token before it is raised.
    """

    def __init__(self, base_url: str, *, transport: HttpTransport = urllib_transport, timeout: float = 120.0) -> None:
        self.base_url = validate_base_url(base_url)
        self._transport = transport
        self._timeout = timeout
        self._token: str | None = None
        self._issued_by_login = False

    @property
    def authenticated(self) -> bool:
        return self._token is not None

    @property
    def token(self) -> str:
        if self._token is None:
            raise OperationFailed("not authenticated")
        return self._token

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: object = None,
        raw: bytes | None = None,
        content_type: str | None = None,
        authenticated: bool = True,
    ) -> HttpResponse:
        if not path.startswith("/api/"):
            raise UsageError(f"API path must start with /api/: {path}")
        headers = {"Accept": "application/json"}
        body: bytes | None = None
        if json_body is not None:
            body = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        elif raw is not None:
            body = raw
            if content_type is None:
                raise UsageError("raw request body needs a content type")
            headers["Content-Type"] = content_type
        if authenticated:
            headers["Authorization"] = "Bearer " + self.token
        response = self._transport(method, self.base_url + path, headers=headers, body=body, timeout=self._timeout)
        if response.status >= 400:
            secrets = [self._token] if self._token is not None else []
            detail = redact(response.body[:_ERROR_DETAIL_LIMIT].decode(errors="replace"), secrets)
            raise ApiError(method=method, path=path, status=response.status, detail=detail)
        return response

    def get_json(self, path: str) -> Any:
        return _json_body(self.request("GET", path), path)

    def post_json(self, path: str, body: object) -> Any:
        response = self.request("POST", path, json_body=body)
        if response.status == 204 or response.body == b"":
            return None
        return _json_body(response, path)

    def download(self, path: str) -> bytes:
        return self.request("GET", path).body

    def upload_file(self, path: str, *, filename: str, mime_type: str, data: bytes) -> Any:
        """POST one file as ``multipart/form-data`` under the field name ``file``."""
        if any(character in filename for character in "\r\n\x00"):
            raise OperationFailed("file name contains a line break or NUL")
        quoted = filename.replace("\\", "\\\\").replace('"', '\\"')
        boundary = "elspeth-ops-" + uuid4().hex
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{quoted}"\r\nContent-Type: {mime_type}\r\n\r\n'
        ).encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        response = self.request("POST", path, raw=head + data + tail, content_type="multipart/form-data; boundary=" + boundary)
        return _json_body(response, path)

    # -- authentication --------------------------------------------------

    def login(self, username: str, password: str) -> None:
        """Obtain a token with ``POST /api/auth/login`` (local authentication)."""
        response = self.request("POST", "/api/auth/login", json_body={"username": username, "password": password}, authenticated=False)
        self._token = _token_from(response)
        self._issued_by_login = True

    def use_token(self, token: str) -> None:
        """Use a pre-issued bearer token. It is not revoked by :meth:`close`."""
        self._token = token
        self._issued_by_login = False

    def refresh(self) -> None:
        """Replace the token through ``POST /api/auth/token``."""
        self._token = _token_from(self.request("POST", "/api/auth/token"))

    def close(self) -> None:
        """Revoke a token this client obtained by login; leave a supplied token alone."""
        if self._token is not None and self._issued_by_login:
            self.request("POST", "/api/auth/logout")
        self._token = None


def list_pages(client: WebClient, path: str, *, limit: int = 500) -> list[Any]:
    """Read every page of a ``limit``/``offset`` list endpoint."""
    separator = "&" if "?" in path else "?"
    items: list[Any] = []
    offset = 0
    while True:
        page = require_list(client.get_json(f"{path}{separator}limit={limit}&offset={offset}"), path)
        items.extend(page)
        if len(page) < limit:
            return items
        offset += limit


def _json_body(response: HttpResponse, path: str) -> Any:
    try:
        return json.loads(response.body)
    except ValueError:
        raise OperationFailed(f"{path} did not return JSON") from None


def _token_from(response: HttpResponse) -> str:
    value = _json_body(response, "/api/auth")
    if type(value) is not dict or "access_token" not in value or type(value["access_token"]) is not str:
        raise OperationFailed("token response has no access_token")
    token: str = value["access_token"]
    return token


def token_expiry(token: str) -> float:
    """Return a JWT's ``exp`` claim without verifying it (used only to schedule a refresh)."""
    parts = token.split(".")
    if len(parts) != 3:
        raise OperationFailed("bearer token is not a JWT")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, binascii.Error):
        raise OperationFailed("bearer token payload is not JSON") from None
    if type(claims) is not dict or "exp" not in claims or type(claims["exp"]) not in (int, float):
        raise OperationFailed("bearer token has no numeric exp claim")
    expiry: float = float(claims["exp"])
    return expiry


@dataclass(frozen=True, slots=True)
class AccountAuth:
    """How to authenticate one account: a token variable, a password variable, or a hidden prompt."""

    username: str
    token_env: str | None = None
    password_env: str | None = None

    def __post_init__(self) -> None:
        if self.token_env is not None and self.password_env is not None:
            raise UsageError(f"give a token variable or a password variable for {self.username}, not both")


def sign_in(client: WebClient, auth: AccountAuth, *, prompt_secret: Callable[[str], str] = getpass.getpass) -> JsonObject:
    """Authenticate ``client`` as ``auth.username`` and prove it with ``/api/auth/me``."""
    if auth.token_env is not None:
        client.use_token(read_secret(env=auth.token_env, prompt="", prompt_secret=prompt_secret))
    else:
        password = read_secret(env=auth.password_env, prompt=f"Password for {auth.username} (hidden input): ", prompt_secret=prompt_secret)
        client.login(auth.username, password)
    me = client.get_json("/api/auth/me")
    if type(me) is not dict or "username" not in me or me["username"] != auth.username:
        client.close()
        raise OperationFailed(f"authenticated identity is not {auth.username}")
    result: JsonObject = me
    return result


def add_account_arguments(parser: argparse.ArgumentParser, *, prefix: str, role: str) -> None:
    """Add ``--<prefix>account``, ``--<prefix>token-env`` and ``--<prefix>password-env``."""
    dest = prefix.replace("-", "_")
    parser.add_argument(f"--{prefix}account", dest=f"{dest}account", help=f"{role} account (username); no default")
    parser.add_argument(
        f"--{prefix}token-env",
        dest=f"{dest}token_env",
        metavar="VAR",
        help=f"environment variable holding a bearer token for the {role} account",
    )
    parser.add_argument(
        f"--{prefix}password-env",
        dest=f"{dest}password_env",
        metavar="VAR",
        help=f"environment variable holding the {role} account's password; without either variable the password is read from a hidden prompt",
    )


def account_from(args: argparse.Namespace, *, prefix: str, env: str) -> AccountAuth:
    dest = prefix.replace("-", "_")
    namespace = vars(args)
    username = setting(namespace[f"{dest}account"], flag=f"--{prefix}account", env=env)
    return AccountAuth(username=username, token_env=namespace[f"{dest}token_env"], password_env=namespace[f"{dest}password_env"])


# ── private receipts and checkpoints ──────────────────────────────────────


class CheckpointStore:
    """Private JSON receipts in a directory outside the repository.

    Receipts carry graph state, file bytes and identities, so the directory is
    created with mode 0700, files with 0600, and a directory inside this
    repository is refused. Writes are atomic (temporary file and rename).
    """

    def __init__(self, root: Path, *, repo_root: Path = REPO_ROOT) -> None:
        resolved = root.expanduser().resolve()
        if resolved == repo_root or resolved.is_relative_to(repo_root):
            raise UsageError("--state-dir must be outside the repository: receipts hold private deployment data")
        self.root = resolved

    def _path(self, name: str) -> Path:
        if not _RECEIPT_NAME.fullmatch(name):
            raise UsageError(f"receipt name {name!r} is not a lowercase file name")
        return self.root / (name + ".json")

    def read(self, name: str) -> JsonObject | None:
        path = self._path(name)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            raise OperationFailed(f"receipt {name} is not valid JSON; inspect it before resuming") from None
        if type(value) is not dict:
            raise OperationFailed(f"receipt {name} is not a JSON object; inspect it before resuming")
        result: JsonObject = value
        return result

    def write(self, name: str, value: Mapping[str, Any]) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._path(name)
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)


# ── subprocesses ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class CommandFailed(OperationFailed):
    def __init__(self, argv: Sequence[str], returncode: int, stderr: bytes) -> None:
        super().__init__(f"{argv[0]} exited {returncode}")
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stderr = stderr


class CommandRunner(Protocol):
    """The subprocess boundary. A fake replaces it in tests."""

    def run(self, argv: Sequence[str], *, stdin: bytes | None = None, timeout: float | None = None) -> CommandResult: ...

    def stream(self, argv: Sequence[str], *, gzip: bool = False) -> Iterator[bytes]:
        """Yield stdout chunks; raise :class:`CommandFailed` after the last chunk if the command failed."""
        ...

    def feed(self, argv: Sequence[str], chunks: Iterator[bytes], *, timeout: float = 300.0) -> bytes:
        """Write ``chunks`` to the command's stdin and return its stdout; raise on failure."""
        ...


def capture(runner: CommandRunner, argv: Sequence[str], *, stdin: bytes | None = None) -> bytes:
    result = runner.run(argv, stdin=stdin)
    if result.returncode != 0:
        raise CommandFailed(argv, result.returncode, result.stderr)
    return result.stdout


class SubprocessRunner:
    """The real :class:`CommandRunner`."""

    def run(self, argv: Sequence[str], *, stdin: bytes | None = None, timeout: float | None = None) -> CommandResult:
        completed = subprocess.run(list(argv), input=stdin, capture_output=True, timeout=timeout, check=False)
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)

    def stream(self, argv: Sequence[str], *, gzip: bool = False) -> Iterator[bytes]:
        source = subprocess.Popen(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        errors: list[bytes] = []
        reader = _drain(source.stderr, errors)
        compressor: subprocess.Popen[bytes] | None = None
        output: IO[bytes] | None = source.stdout
        if gzip:
            compressor = subprocess.Popen(["gzip", "-1"], stdin=source.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if source.stdout is not None:
                source.stdout.close()
            output = compressor.stdout
        if output is None:
            raise OperationFailed("command has no stdout pipe")
        try:
            while chunk := output.read(CHUNK_BYTES):
                yield chunk
            if compressor is not None and compressor.wait(timeout=60) != 0:
                raise CommandFailed(["gzip"], compressor.returncode, b"")
            code = source.wait(timeout=60)
            reader.join(timeout=60)
            if code != 0:
                raise CommandFailed(argv, code, b"".join(errors))
        finally:
            for process in (source, compressor):
                if process is not None and process.poll() is None:
                    process.terminate()

    def feed(self, argv: Sequence[str], chunks: Iterator[bytes], *, timeout: float = 300.0) -> bytes:
        process = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output: list[bytes] = []
        errors: list[bytes] = []
        readers = [_drain(process.stdout, output), _drain(process.stderr, errors)]
        try:
            if process.stdin is None:
                raise OperationFailed("command has no stdin pipe")
            for chunk in chunks:
                try:
                    process.stdin.write(chunk)
                except BrokenPipeError:
                    break
            with contextlib.suppress(BrokenPipeError):
                process.stdin.close()
            process.wait(timeout=timeout)
            for reader in readers:
                reader.join(timeout=60)
            if process.returncode != 0:
                raise CommandFailed(argv, process.returncode, b"".join(errors))
            return b"".join(output)
        finally:
            if process.poll() is None:
                process.terminate()


def _drain(stream: IO[bytes] | None, into: list[bytes]) -> threading.Thread:
    def read() -> None:
        if stream is not None:
            into.append(stream.read())

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    return thread


# ── graph comparison ──────────────────────────────────────────────────────

GRAPH_KEYS = ("sources", "nodes", "edges", "outputs", "metadata")
REDACTED_SOURCE_PATH = "<redacted-blob-source-path>"


def graph_content(state: Mapping[str, Any]) -> JsonObject:
    """The authored graph of a state response: sources, nodes, edges, outputs and metadata."""
    return {key: copy.deepcopy(state[key]) for key in GRAPH_KEYS}


def api_projection(state: Mapping[str, Any]) -> JsonObject:
    """:func:`graph_content` with a blob-bound source path replaced as the state API shows it."""
    result = graph_content(state)
    for spec in result["sources"].values():
        options = spec["options"]
        if "blob_ref" in options and "path" in options:
            options["path"] = REDACTED_SOURCE_PATH
    return result


def clean_node(node: Mapping[str, Any]) -> JsonObject:
    """A detached copy of a node for comparison.

    Nothing is dropped: a field the server derives shows up as a difference
    rather than being silently ignored, so a comparison fails toward reporting
    a change, never toward passing one.
    """
    return copy.deepcopy(dict(node))


def differences(expected: object, actual: object, path: str = "") -> list[str]:
    """JSON-pointer paths at which two JSON values differ (types compared exactly)."""
    if type(expected) is not type(actual):
        return [path or "/"]
    if isinstance(expected, dict) and isinstance(actual, dict):
        found: list[str] = []
        for key in sorted(set(expected) | set(actual), key=str):
            child = path + "/" + str(key).replace("~", "~0").replace("/", "~1")
            if key not in expected or key not in actual:
                found.append(child)
            else:
                found.extend(differences(expected[key], actual[key], child))
        return found
    if isinstance(expected, list) and isinstance(actual, list):
        found = []
        for index in range(max(len(expected), len(actual))):
            child = f"{path}/{index}"
            if index >= len(expected) or index >= len(actual):
                found.append(child)
            else:
                found.extend(differences(expected[index], actual[index], child))
        return found
    return [] if expected == actual else [path or "/"]


def blob_markers(value: object) -> list[JsonObject]:
    """Every ``{"blob_ref": ...}`` marker inside a JSON value, in document order."""
    if isinstance(value, dict):
        if "blob_ref" in value:
            return [value]
        return [marker for child in value.values() for marker in blob_markers(child)]
    if isinstance(value, list):
        return [marker for child in value for marker in blob_markers(child)]
    return []


def require_dict(value: object, what: str) -> JsonObject:
    if type(value) is not dict:
        raise OperationFailed(f"{what} is not a JSON object")
    result: JsonObject = value
    return result


def require_list(value: object, what: str) -> list[Any]:
    if type(value) is not list:
        raise OperationFailed(f"{what} is not a JSON list")
    result: list[Any] = value
    return result
