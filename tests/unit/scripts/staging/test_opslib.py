"""The shared staging-tool boundaries in scripts/staging/opslib.py."""

from __future__ import annotations

import stat
from collections.abc import Mapping
from pathlib import Path

import pytest
from scripts.staging import opslib
from scripts.staging.opslib import ApiError, CheckpointStore, HttpResponse, UsageError, WebClient


def test_api_error_text_never_carries_the_bearer_token() -> None:
    token = "synthetic.bearer.token-0001"

    def echo(method: str, url: str, *, headers: Mapping[str, str], body: bytes | None, timeout: float) -> HttpResponse:
        return HttpResponse(status=500, body=("server echoed " + headers["Authorization"]).encode())

    client = WebClient("https://deployment.invalid", transport=echo)
    client.use_token(token)
    with pytest.raises(ApiError) as caught:
        client.get_json("/api/sessions")
    assert token not in str(caught.value)
    assert "<redacted>" in str(caught.value)


def test_supplied_token_is_not_revoked_but_a_login_token_is() -> None:
    seen: list[str] = []

    def transport(method: str, url: str, *, headers: Mapping[str, str], body: bytes | None, timeout: float) -> HttpResponse:
        seen.append(url.rsplit("/", 1)[-1])
        return HttpResponse(status=200, body=b'{"access_token": "t"}')

    client = WebClient("https://deployment.invalid", transport=transport)
    client.use_token("supplied")
    client.close()
    assert seen == []
    client.login("user", "pw")
    client.close()
    assert seen == ["login", "logout"]


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://deployment.invalid", True),
        ("http://127.0.0.1:8000", True),
        ("http://deployment.invalid", False),
        ("https://deployment.invalid/api", False),
        ("https://u:p@deployment.invalid", False),
        ("ftp://deployment.invalid", False),
    ],
)
def test_base_url_rules(url: str, ok: bool) -> None:
    if ok:
        assert opslib.validate_base_url(url) == url
    else:
        with pytest.raises(UsageError):
            opslib.validate_base_url(url)


def test_setting_falls_back_to_the_environment_and_has_no_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELSPETH_OPS_EXAMPLE", raising=False)
    with pytest.raises(UsageError, match="--example is required"):
        opslib.setting(None, flag="--example", env="ELSPETH_OPS_EXAMPLE")
    monkeypatch.setenv("ELSPETH_OPS_EXAMPLE", "from-env")
    assert opslib.setting(None, flag="--example", env="ELSPETH_OPS_EXAMPLE") == "from-env"
    assert opslib.setting("from-flag", flag="--example", env="ELSPETH_OPS_EXAMPLE") == "from-flag"


def test_checkpoints_are_private_and_names_are_constrained(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "state", repo_root=tmp_path / "repo")
    store.write("receipt-1", {"a": 1})
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "state" / "receipt-1.json").stat().st_mode) == 0o600
    assert store.read("receipt-1") == {"a": 1}
    with pytest.raises(UsageError):
        store.write("../escape", {})
    (tmp_path / "state" / "broken.json").write_text("[1, 2]")
    with pytest.raises(opslib.OperationFailed, match="not a JSON object"):
        store.read("broken")


def test_differences_compare_types_exactly_and_report_paths() -> None:
    assert opslib.differences({"a": [1, {"b": True}]}, {"a": [1, {"b": True}]}) == []
    assert opslib.differences({"a": 1}, {"a": True}) == ["/a"]
    assert opslib.differences({"a": [1]}, {"a": [1, 2]}) == ["/a/1"]
    assert opslib.differences({"x/y": 1}, {}) == ["/x~1y"]


def test_token_expiry_reads_the_exp_claim() -> None:
    from tests.unit.scripts.staging.fake_deployment import fake_jwt

    assert opslib.token_expiry(fake_jwt("user", 123.0)) == 123.0
    with pytest.raises(opslib.OperationFailed):
        opslib.token_expiry("not-a-jwt")
