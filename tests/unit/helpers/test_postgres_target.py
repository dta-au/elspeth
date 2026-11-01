"""Pure controls for the canonical PostgreSQL target and run resource assignment."""

from __future__ import annotations

from types import TracebackType
from typing import Any

import pytest
from docker.errors import DockerException
from docker.types import HostConfig
from sqlalchemy.engine import URL, make_url
from tests.helpers import postgres_target as target


class _DockerStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.entered = 0
        self.exits: list[BaseException | None] = []
        self.construct_error: BaseException | None = None
        self.enter_error: BaseException | None = None

    def __call__(self, image: str, **kwargs: Any) -> _DockerStub:
        self.calls.append((image, kwargs))
        if self.construct_error is not None:
            raise self.construct_error
        return self

    def __enter__(self) -> _DockerStub:
        self.entered += 1
        if self.enter_error is not None:
            raise self.enter_error
        return self

    def __exit__(self, _exc_type: type[BaseException] | None, error: BaseException | None, _traceback: TracebackType | None) -> None:
        self.exits.append(error)

    def get_connection_url(self) -> str:
        return "postgresql://docker-target.invalid/fixture"


class _EngineStub:
    def __init__(self) -> None:
        self.calls: list[tuple[URL, dict[str, Any]]] = []
        self.statements: list[str] = []
        self.connections = 0
        self.disposals = 0

    def __call__(self, url: URL, **kwargs: Any) -> _EngineStub:
        self.calls.append((url, kwargs))
        return self

    def connect(self) -> _EngineStub:
        self.connections += 1
        return self

    def __enter__(self) -> _EngineStub:
        return self

    def __exit__(self, _exc_type: type[BaseException] | None, _error: BaseException | None, _traceback: TracebackType | None) -> None:
        return None

    def exec_driver_sql(self, statement: str) -> None:
        self.statements.append(statement)

    def dispose(self) -> None:
        self.disposals += 1


def _unexpected_engine(*_args: Any, **_kwargs: Any) -> None:
    raise AssertionError("Docker target must not create a provisioned database engine")


@pytest.fixture(autouse=True)
def isolated_postgres_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (target.PROVISIONED_POSTGRES_URL_ENV, target.POSTGRES_NANO_CPUS_ENV, target.POSTGRES_MEMORY_BYTES_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def docker_stub(monkeypatch: pytest.MonkeyPatch) -> _DockerStub:
    stub = _DockerStub()
    monkeypatch.setattr(target, "PostgresContainer", stub)
    monkeypatch.setattr(target, "create_engine", _unexpected_engine)
    return stub


def _assign(monkeypatch: pytest.MonkeyPatch, *, cpu: str = "2000000000", memory: str = "1073741824") -> None:
    monkeypatch.setenv(target.POSTGRES_NANO_CPUS_ENV, cpu)
    monkeypatch.setenv(target.POSTGRES_MEMORY_BYTES_ENV, memory)


@pytest.mark.parametrize(
    "limits",
    [{}, {"nano_cpus": 500_000_000, "mem_limit": "256m"}, {"nano_cpus": "invalid", "mem_limit": "invalid"}],
)
def test_docker_without_assignment_preserves_caller_kwargs(docker_stub: _DockerStub, limits: dict[str, Any]) -> None:
    labels = {"owner": "resource-control"}
    kwargs = {**limits, "labels": labels, "shm_size": "64m", "memswap_limit": -1}
    with target.postgres_test_target(driver="psycopg", **kwargs) as url:
        assert url == "postgresql://docker-target.invalid/fixture"
    assert docker_stub.calls == [(target.POSTGRES_IMAGE, {"driver": "psycopg", **kwargs})]
    assert docker_stub.calls[0][1]["labels"] is labels
    assert docker_stub.entered == 1
    assert docker_stub.exits == [None]


@pytest.mark.parametrize(
    ("limits", "expected"),
    [
        ({}, {}),
        ({"nano_cpus": 500_000_000}, {"nano_cpus": 2_000_000_000}),
        ({"mem_limit": "256m"}, {"mem_limit": 1_073_741_824}),
        (
            {"nano_cpus": 500_000_000, "mem_limit": "256m"},
            {"nano_cpus": 2_000_000_000, "mem_limit": 1_073_741_824},
        ),
    ],
)
def test_docker_assignment_replaces_only_each_existing_cap(
    monkeypatch: pytest.MonkeyPatch, docker_stub: _DockerStub, limits: dict[str, Any], expected: dict[str, int]
) -> None:
    _assign(monkeypatch)
    with target.postgres_test_target(**limits):
        pass
    assert docker_stub.calls == [(target.POSTGRES_IMAGE, {"driver": "psycopg2", **expected})]


@pytest.mark.parametrize("cpu", [None, 0, False, ""])
def test_docker_assignment_preserves_uncapped_cpu(monkeypatch: pytest.MonkeyPatch, docker_stub: _DockerStub, cpu: Any) -> None:
    _assign(monkeypatch)
    with target.postgres_test_target(nano_cpus=cpu, mem_limit="256m"):
        pass
    kwargs = docker_stub.calls[0][1]
    assert kwargs["nano_cpus"] is cpu
    assert kwargs["mem_limit"] == 1_073_741_824


@pytest.mark.parametrize("memory", [None, 0, 0.0, False, "0", "0m", "0.0m", "0kb", ""])
def test_docker_assignment_preserves_uncapped_memory(monkeypatch: pytest.MonkeyPatch, docker_stub: _DockerStub, memory: Any) -> None:
    _assign(monkeypatch)
    with target.postgres_test_target(nano_cpus=500_000_000, mem_limit=memory):
        pass
    kwargs = docker_stub.calls[0][1]
    assert kwargs["nano_cpus"] == 2_000_000_000
    assert kwargs["mem_limit"] is memory


@pytest.mark.parametrize("cpu", [-1, 500_000_000.0, "500000000", True])
def test_docker_assignment_preserves_invalid_caller_cpu(monkeypatch: pytest.MonkeyPatch, docker_stub: _DockerStub, cpu: Any) -> None:
    _assign(monkeypatch)
    with target.postgres_test_target(nano_cpus=cpu, mem_limit="256m"):
        pass
    assert docker_stub.calls[0][1]["nano_cpus"] is cpu
    assert docker_stub.calls[0][1]["mem_limit"] == 1_073_741_824


@pytest.mark.parametrize("cpu", [500_000_000.0, "500000000"])
def test_docker_assignment_retains_actual_sdk_cpu_type_refusal(monkeypatch: pytest.MonkeyPatch, docker_stub: _DockerStub, cpu: Any) -> None:
    _assign(monkeypatch)
    observed: list[dict[str, Any]] = []

    def refusing_factory(_image: str, **kwargs: Any) -> None:
        observed.append(kwargs)
        HostConfig(version="1.47", nano_cpus=kwargs["nano_cpus"])
        pytest.fail("the SDK must reject the caller's invalid CPU type")

    monkeypatch.setattr(target, "PostgresContainer", refusing_factory)
    with pytest.raises(TypeError, match="nano_cpus"), target.postgres_test_target(nano_cpus=cpu, mem_limit="256m"):
        pytest.fail("an invalid caller must not yield a target")
    assert observed == [{"driver": "psycopg2", "nano_cpus": cpu, "mem_limit": 1_073_741_824}]
    assert docker_stub.calls == []


@pytest.mark.parametrize("memory", [-1, 268_435_456.0, True, float("inf"), float("nan")])
def test_docker_assignment_preserves_invalid_numeric_memory(monkeypatch: pytest.MonkeyPatch, docker_stub: _DockerStub, memory: Any) -> None:
    _assign(monkeypatch)
    with target.postgres_test_target(nano_cpus=500_000_000, mem_limit=memory):
        pass
    assert docker_stub.calls[0][1]["mem_limit"] is memory
    assert docker_stub.calls[0][1]["nano_cpus"] == 2_000_000_000


@pytest.mark.parametrize("swap", [None, -1, 0, "2g"])
def test_docker_assignment_preserves_explicit_swap_and_other_kwargs(
    monkeypatch: pytest.MonkeyPatch, docker_stub: _DockerStub, swap: Any
) -> None:
    _assign(monkeypatch)
    labels = {"owner": "resource-control"}
    with target.postgres_test_target(
        nano_cpus=500_000_000, mem_limit="256m", memswap_limit=swap, labels=labels, shm_size="64m", cpu_quota=50_000
    ):
        pass
    assert docker_stub.calls == [
        (
            target.POSTGRES_IMAGE,
            {
                "driver": "psycopg2",
                "nano_cpus": 2_000_000_000,
                "mem_limit": 1_073_741_824,
                "memswap_limit": swap,
                "labels": labels,
                "shm_size": "64m",
                "cpu_quota": 50_000,
            },
        )
    ]
    assert docker_stub.calls[0][1]["memswap_limit"] is swap
    assert docker_stub.calls[0][1]["labels"] is labels


@pytest.mark.parametrize("present", [target.POSTGRES_NANO_CPUS_ENV, target.POSTGRES_MEMORY_BYTES_ENV])
@pytest.mark.parametrize("limits", [{}, {"nano_cpus": 500_000_000, "mem_limit": "256m"}])
def test_docker_assignment_requires_both_variables(
    monkeypatch: pytest.MonkeyPatch, docker_stub: _DockerStub, present: str, limits: dict[str, Any]
) -> None:
    monkeypatch.setenv(present, "1")
    with pytest.raises(pytest.UsageError, match="must be set together"), target.postgres_test_target(**limits):
        pytest.fail("a one-sided assignment must not yield a target")
    assert docker_stub.calls == []


@pytest.mark.parametrize("name", [target.POSTGRES_NANO_CPUS_ENV, target.POSTGRES_MEMORY_BYTES_ENV])
@pytest.mark.parametrize(
    "raw",
    ["", " 1", "1 ", "+1", "-1", "0", "00", "0.5", "1e3", "1_000", "1m", "\uff11\uff12", "\u0661\u0662"],
)
def test_docker_assignment_rejects_non_positive_ascii_integer(
    monkeypatch: pytest.MonkeyPatch, docker_stub: _DockerStub, name: str, raw: str
) -> None:
    _assign(monkeypatch)
    monkeypatch.setenv(name, raw)
    with (
        pytest.raises(pytest.UsageError, match=f"{name} must be a positive ASCII integer"),
        target.postgres_test_target(nano_cpus=500_000_000, mem_limit="256m"),
    ):
        pytest.fail("an invalid assignment must not yield a target")
    assert docker_stub.calls == []


@pytest.mark.parametrize(
    ("cpu", "memory", "expected_cpu", "expected_memory"),
    [("9007199254740993", "9007199254740995", 9_007_199_254_740_993, 9_007_199_254_740_995), ("0001", "0002", 1, 2)],
)
def test_docker_assignment_keeps_integer_precision_without_clamping(
    monkeypatch: pytest.MonkeyPatch, docker_stub: _DockerStub, cpu: str, memory: str, expected_cpu: int, expected_memory: int
) -> None:
    _assign(monkeypatch, cpu=cpu, memory=memory)
    with target.postgres_test_target(nano_cpus=500_000_000, mem_limit="256m"):
        pass
    kwargs = docker_stub.calls[0][1]
    assert kwargs["nano_cpus"] == expected_cpu and type(kwargs["nano_cpus"]) is int
    assert kwargs["mem_limit"] == expected_memory and type(kwargs["mem_limit"]) is int


def test_docker_assignment_does_not_mask_invalid_original_memory(monkeypatch: pytest.MonkeyPatch, docker_stub: _DockerStub) -> None:
    _assign(monkeypatch)
    with (
        pytest.raises(DockerException, match="should specify the units"),
        target.postgres_test_target(nano_cpus=500_000_000, mem_limit="invalid"),
    ):
        pytest.fail("invalid caller memory must retain the SDK refusal")
    assert docker_stub.calls == []


def test_docker_assignment_preserves_original_memory_parser_error(monkeypatch: pytest.MonkeyPatch, docker_stub: _DockerStub) -> None:
    _assign(monkeypatch)
    failure = DockerException("original SDK memory refusal")

    def refuse_memory(_value: Any) -> None:
        raise failure

    monkeypatch.setattr(target, "parse_bytes", refuse_memory)
    with pytest.raises(DockerException) as caught, target.postgres_test_target(nano_cpus=500_000_000, mem_limit="256m"):
        pytest.fail("the original memory refusal must escape")
    assert caught.value is failure
    assert docker_stub.calls == []


@pytest.mark.parametrize("where", ["construct", "enter", "body"])
def test_docker_refusal_and_body_exception_propagate(monkeypatch: pytest.MonkeyPatch, docker_stub: _DockerStub, where: str) -> None:
    _assign(monkeypatch)
    failure = RuntimeError("original container failure")
    if where == "construct":
        docker_stub.construct_error = failure
    elif where == "enter":
        docker_stub.enter_error = failure
    with pytest.raises(RuntimeError) as caught, target.postgres_test_target(nano_cpus=500_000_000, mem_limit="256m"):
        raise failure
    assert caught.value is failure
    assert len(docker_stub.calls) == 1
    assert docker_stub.entered == (0 if where == "construct" else 1)
    assert docker_stub.exits == ([failure] if where == "body" else [])


@pytest.mark.parametrize("assignment", [(None, None), ("invalid", None), (None, "0"), ("bad", "bad"), ("2", "1024")])
@pytest.mark.parametrize("body_fails", [False, True])
def test_provisioned_target_ignores_resource_assignment_and_keeps_cleanup(
    monkeypatch: pytest.MonkeyPatch, assignment: tuple[str | None, str | None], body_fails: bool
) -> None:
    admin_url = URL.create(
        "postgresql",
        username="fixture-role",
        password="fixture-placeholder",
        host="postgres.invalid",
        port=5432,
        database="admin",
        query={"sslmode": "verify-full", "sslrootcert": "fixture-ca.pem"},
    )
    monkeypatch.setenv(target.PROVISIONED_POSTGRES_URL_ENV, admin_url.render_as_string(hide_password=False))
    for name, value in zip((target.POSTGRES_NANO_CPUS_ENV, target.POSTGRES_MEMORY_BYTES_ENV), assignment, strict=True):
        if value is not None:
            monkeypatch.setenv(name, value)

    def no_resource_parser() -> None:
        raise AssertionError("provisioned targets must not parse container assignments")

    def no_docker(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("provisioned targets must not construct Docker targets")

    engine = _EngineStub()
    monkeypatch.setattr(target, "postgres_container_resource_assignment", no_resource_parser)
    monkeypatch.setattr(target, "PostgresContainer", no_docker)
    monkeypatch.setattr(target, "create_engine", engine)
    yielded: list[URL] = []
    failure = RuntimeError("original provisioned body failure")

    def use_target() -> None:
        with target.postgres_test_target(driver="psycopg", require_tls=True, nano_cpus="invalid", mem_limit="invalid") as url:
            yielded.append(make_url(url))
            if body_fails:
                raise failure

    if body_fails:
        with pytest.raises(RuntimeError) as caught:
            use_target()
        assert caught.value is failure
    else:
        use_target()
    assert engine.calls == [(admin_url.set(drivername="postgresql+psycopg"), {"isolation_level": "AUTOCOMMIT"})]
    assert len(yielded) == 1
    database = yielded[0].database
    assert database is not None and database.startswith("elspeth_tc_")
    assert len(database.removeprefix("elspeth_tc_")) == 32
    assert all(character in "0123456789abcdef" for character in database.removeprefix("elspeth_tc_"))
    assert yielded[0] == admin_url.set(drivername="postgresql+psycopg", database=database)
    assert engine.statements == [f'CREATE DATABASE "{database}"', f'DROP DATABASE "{database}" WITH (FORCE)']
    assert engine.connections == 2
    assert engine.disposals == 1
