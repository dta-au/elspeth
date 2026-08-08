"""Contracts for strict ECS task-metadata admission."""

from __future__ import annotations

import json

import pytest

from elspeth.web._aws_ecs_acceptance import ecs_metadata

_BASE_URI = "http://169.254.170.2/v4/0123456789abcdef"
_ACCOUNT_ID = "123456789012"
_REGION = "ap-southeast-2"
_CLUSTER = "acceptance-a-cluster"
_FAMILY = "a-web"
_TASK_ARN = f"arn:aws:ecs:{_REGION}:{_ACCOUNT_ID}:task/{_CLUSTER}/" + "a" * 32


def _document(**overrides: object) -> bytes:
    document: dict[str, object] = {
        "Cluster": _CLUSTER,
        "TaskARN": _TASK_ARN,
        "Family": _FAMILY,
        "Revision": "17",
        "ForwardCompatibleField": {"retained": True},
    }
    document.update(overrides)
    return json.dumps(document).encode()


def _fetch(
    *,
    raw_uri: object = _BASE_URI,
    response: tuple[object, object] | None = None,
) -> ecs_metadata.ECSTaskIdentity:
    return ecs_metadata.fetch_task_identity(
        raw_uri,
        expected_partition="aws",
        expected_account_id=_ACCOUNT_ID,
        expected_region=_REGION,
        expected_cluster=_CLUSTER,
        expected_family=_FAMILY,
        request_metadata=lambda _url: response if response is not None else (200, _document()),
    )


def test_fetch_task_identity_admits_short_or_arn_cluster_and_returns_owned_projection() -> None:
    requested: list[str] = []

    def request(url: str) -> tuple[object, object]:
        requested.append(url)
        return 200, _document(Cluster=f"arn:aws:ecs:{_REGION}:{_ACCOUNT_ID}:cluster/{_CLUSTER}")

    identity = ecs_metadata.fetch_task_identity(
        _BASE_URI,
        expected_partition="aws",
        expected_account_id=_ACCOUNT_ID,
        expected_region=_REGION,
        expected_cluster=_CLUSTER,
        expected_family=_FAMILY,
        request_metadata=request,
    )

    assert identity == ecs_metadata.ECSTaskIdentity(family=_FAMILY, revision="17", task_arn=_TASK_ARN)
    assert requested == [f"{_BASE_URI}/task"]


@pytest.mark.parametrize(
    "raw_uri",
    (
        "https://169.254.170.2/v4/token",
        "http://169.254.169.254/v4/token",
        "http://user@169.254.170.2/v4/token",
        "http://169.254.170.2:8080/v4/token",
        "http://169.254.170.2/v3/token",
        "http://169.254.170.2/v4/token/task",
        "http://169.254.170.2/v4/token?next=foreign",
        "http://169.254.170.2/v4/token%2Fforeign",
    ),
)
def test_fetch_task_identity_rejects_non_link_local_uri_before_request(raw_uri: str) -> None:
    with pytest.raises(ecs_metadata.ECSMetadataError, match="ecs_task_metadata_invalid"):
        ecs_metadata.fetch_task_identity(
            raw_uri=raw_uri,
            expected_partition="aws",
            expected_account_id=_ACCOUNT_ID,
            expected_region=_REGION,
            expected_cluster=_CLUSTER,
            expected_family=_FAMILY,
            request_metadata=lambda _url: pytest.fail("invalid URI reached the request boundary"),
        )


@pytest.mark.parametrize(
    "response",
    (
        (302, _document()),
        (200, b""),
        (200, b"x" * (64 * 1_024 + 1)),
        (200, b"[]"),
        (200, b'{"Cluster":"a","Cluster":"b"}'),
        (200, b'{"Cluster":NaN}'),
    ),
)
def test_fetch_task_identity_rejects_invalid_http_or_json_response(response: tuple[object, object]) -> None:
    with pytest.raises(ecs_metadata.ECSMetadataError, match="ecs_task_metadata_invalid"):
        _fetch(response=response)


@pytest.mark.parametrize(
    ("overrides", "account_id", "region", "cluster", "family"),
    (
        ({"Cluster": "foreign"}, _ACCOUNT_ID, _REGION, _CLUSTER, _FAMILY),
        ({"TaskARN": _TASK_ARN.replace(_ACCOUNT_ID, "999999999999")}, _ACCOUNT_ID, _REGION, _CLUSTER, _FAMILY),
        ({"TaskARN": _TASK_ARN.replace(_REGION, "us-east-1")}, _ACCOUNT_ID, _REGION, _CLUSTER, _FAMILY),
        ({"TaskARN": _TASK_ARN.replace(_CLUSTER, "foreign")}, _ACCOUNT_ID, _REGION, _CLUSTER, _FAMILY),
        ({"Family": "foreign"}, _ACCOUNT_ID, _REGION, _CLUSTER, _FAMILY),
        ({"Revision": 17}, _ACCOUNT_ID, _REGION, _CLUSTER, _FAMILY),
        ({"Revision": "0"}, _ACCOUNT_ID, _REGION, _CLUSTER, _FAMILY),
        ({"TaskARN": None}, _ACCOUNT_ID, _REGION, _CLUSTER, _FAMILY),
    ),
)
def test_fetch_task_identity_rejects_unbound_or_malformed_fields(
    overrides: dict[str, object],
    account_id: str,
    region: str,
    cluster: str,
    family: str,
) -> None:
    with pytest.raises(ecs_metadata.ECSMetadataError, match="ecs_task_metadata_invalid"):
        ecs_metadata.fetch_task_identity(
            _BASE_URI,
            expected_partition="aws",
            expected_account_id=account_id,
            expected_region=region,
            expected_cluster=cluster,
            expected_family=family,
            request_metadata=lambda _url: (200, _document(**overrides)),
        )


def test_no_redirect_handler_refuses_redirect_requests() -> None:
    handler = ecs_metadata._NoRedirects()
    assert handler.redirect_request(object(), object(), 302, "Found", object(), "http://example.invalid") is None  # type: ignore[arg-type]
