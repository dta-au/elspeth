"""Strict ECS task-metadata admission for container entrypoints."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from elspeth.contracts.trust_boundary import trust_boundary

_MAX_METADATA_URI_CHARS = 1_024
_MAX_METADATA_DOCUMENT_BYTES = 64 * 1_024
_METADATA_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~-]{1,512}\Z")
_ACCOUNT_ID_PATTERN = re.compile(r"[0-9]{12}\Z")
_REGION_PATTERN = re.compile(r"[a-z]{2}(?:-gov)?-[a-z]+-[1-9][0-9]*\Z")
_ECS_NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,255}\Z")
_REVISION_PATTERN = re.compile(r"[1-9][0-9]{0,9}\Z")
_TASK_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")


class ECSMetadataError(RuntimeError):
    """Raised with a static message when ECS metadata cannot be admitted."""


@dataclass(frozen=True, slots=True)
class ECSTaskIdentity:
    """Owned projection of one validated ECS task metadata document."""

    family: str
    revision: str
    task_arn: str


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def _request_metadata(url: str) -> tuple[object, object]:
    try:
        opener = build_opener(_NoRedirects())
        request = Request(url, headers={"Accept": "application/json"}, method="GET")
        with opener.open(request, timeout=5) as response:
            status = response.status
            body = response.read(_MAX_METADATA_DOCUMENT_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        raise ECSMetadataError("ecs_task_metadata_invalid") from None
    return status, body


@trust_boundary(
    tier=3,
    source="ECS_CONTAINER_METADATA_URI_V4 and its task metadata HTTP response",
    source_param="raw_uri",
    suppresses=("R1", "R5"),
    invariant=(
        "raises ECSMetadataError before use on a non-link-local URI, non-200 or oversized response, malformed JSON, "
        "or metadata not bound to the expected partition, account, region, cluster, and task family"
    ),
    test_ref=("tests/unit/web/aws_ecs_acceptance/test_ecs_metadata.py::test_fetch_task_identity_rejects_non_link_local_uri_before_request"),
    test_fingerprint="af4c1e9353b5bc9b2113269267ca43b1ed0982452372fea0b782d9dd3b8ae0b6",
)
def fetch_task_identity(
    raw_uri: object,
    *,
    expected_partition: str,
    expected_account_id: str,
    expected_region: str,
    expected_cluster: str,
    expected_family: str,
    request_metadata: Callable[[str], tuple[object, object]] = _request_metadata,
) -> ECSTaskIdentity:
    """Fetch, validate, and own the minimum ECS task identity projection."""

    try:
        if (
            type(expected_partition) is not str
            or expected_partition not in {"aws", "aws-us-gov", "aws-cn"}
            or _ACCOUNT_ID_PATTERN.fullmatch(expected_account_id) is None
            or _REGION_PATTERN.fullmatch(expected_region) is None
            or _ECS_NAME_PATTERN.fullmatch(expected_cluster) is None
            or _ECS_NAME_PATTERN.fullmatch(expected_family) is None
            or type(raw_uri) is not str
            or raw_uri == ""
            or len(raw_uri) > _MAX_METADATA_URI_CHARS
        ):
            raise ValueError
        parsed_uri = urlsplit(raw_uri)
        try:
            port = parsed_uri.port
        except ValueError:
            raise ValueError from None
        path_parts = parsed_uri.path.split("/")
        if (
            parsed_uri.scheme != "http"
            or parsed_uri.hostname != "169.254.170.2"
            or parsed_uri.username is not None
            or parsed_uri.password is not None
            or port is not None
            or parsed_uri.query != ""
            or parsed_uri.fragment != ""
            or len(path_parts) != 3
            or path_parts[:2] != ["", "v4"]
            or _METADATA_TOKEN_PATTERN.fullmatch(path_parts[2]) is None
            or raw_uri != f"http://169.254.170.2/v4/{path_parts[2]}"
        ):
            raise ValueError

        response = request_metadata(f"{raw_uri}/task")
        if type(response) is not tuple or len(response) != 2:
            raise ValueError
        status, body = response
        if type(status) is not int or status != 200 or type(body) is not bytes or body == b"" or len(body) > _MAX_METADATA_DOCUMENT_BYTES:
            raise ValueError

        def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        def reject_constant(_value: str) -> object:
            raise ValueError

        document = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
        if not isinstance(document, dict) or not {"Cluster", "TaskARN", "Family", "Revision"} <= set(document):
            raise ValueError
        cluster = document["Cluster"]
        task_arn = document["TaskARN"]
        family = document["Family"]
        revision = document["Revision"]
        expected_cluster_arn = f"arn:{expected_partition}:ecs:{expected_region}:{expected_account_id}:cluster/{expected_cluster}"
        if (
            type(cluster) is not str
            or cluster not in {expected_cluster, expected_cluster_arn}
            or type(task_arn) is not str
            or type(family) is not str
            or family != expected_family
            or type(revision) is not str
            or _REVISION_PATTERN.fullmatch(revision) is None
        ):
            raise ValueError
        task_match = re.fullmatch(
            rf"arn:{re.escape(expected_partition)}:ecs:{re.escape(expected_region)}:"
            rf"{re.escape(expected_account_id)}:task/{re.escape(expected_cluster)}/(?P<task_id>[0-9a-f]{{32}})",
            task_arn,
        )
        if task_match is None or _TASK_ID_PATTERN.fullmatch(task_match.group("task_id")) is None:
            raise ValueError
        return ECSTaskIdentity(family=family, revision=revision, task_arn=task_arn)
    except (json.JSONDecodeError, RecursionError, TypeError, UnicodeError, ValueError):
        raise ECSMetadataError("ecs_task_metadata_invalid") from None


def main() -> int:
    try:
        identity = fetch_task_identity(
            os.environ["ECS_CONTAINER_METADATA_URI_V4"],
            expected_partition="aws",
            expected_account_id=os.environ["ELSPETH_ACCEPTANCE_AWS_ACCOUNT_ID"],
            expected_region=os.environ["AWS_REGION"],
            expected_cluster=os.environ["ELSPETH_WEB__OPERATOR_TELEMETRY_ECS_CLUSTER"],
            expected_family=os.environ["ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_FAMILY"],
        )
    except (ECSMetadataError, KeyError):
        sys.stderr.write("ecs_task_metadata_invalid\n")
        return 1
    sys.stdout.write(f"{identity.family} {identity.revision}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
