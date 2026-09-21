"""Parse nginx directive scopes and control websocket-header regression checks."""

import shlex
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


@dataclass
class Directive:
    words: tuple[str, ...]
    children: list["Directive"]


def _parse(config: str) -> list[Directive]:
    lexer = shlex.shlex(config, posix=True, punctuation_chars="{};")
    lexer.whitespace_split = True
    tokens = iter(lexer)

    def block(nested: bool) -> list[Directive]:
        result: list[Directive] = []
        words: list[str] = []
        for token in tokens:
            if token == "}":
                assert nested and not words
                return result
            if token == ";":
                assert words
                result.append(Directive(tuple(words), []))
                words = []
            elif token == "{":
                assert words
                result.append(Directive(tuple(words), block(True)))
                words = []
            else:
                words.append(token)
        assert not nested and not words
        return result

    return block(False)


def _assert_websocket_contract(config: str) -> None:
    document = _parse(config)
    mapping = next(d for d in document if d.words == ("map", "$http_upgrade", "$elspeth_connection_upgrade"))
    assert {d.words for d in mapping.children} == {("default", "upgrade"), ("", "close")}
    server = next(d for d in document if d.words == ("server",))
    location = next(d for d in server.children if d.words == ("location", "/"))
    directives = {d.words for d in location.children}
    assert ("proxy_http_version", "1.1") in directives
    assert ("proxy_set_header", "Upgrade", "$http_upgrade") in directives
    assert ("proxy_set_header", "Connection", "$elspeth_connection_upgrade") in directives
    assert ("proxy_pass", "http://127.0.0.1:8451") in directives
    compose = yaml.safe_load((ROOT / "deploy/compose/web-postgres.yaml").read_text())
    environment = compose["services"]["web"]["environment"]
    ceiling = int(environment["ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS"])
    assert ("proxy_read_timeout", f"{ceiling}s") in directives
    assert ("proxy_send_timeout", f"{ceiling}s") in directives
    timeout = int(environment["ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS"])
    headroom = int(environment["ELSPETH_WEB__COMPOSER_TRANSPORT_HEADROOM_SECONDS"])
    assert timeout <= ceiling - headroom
    log_format = next(d for d in document if d.words[:2] == ("log_format", "elspeth_safe"))
    assert "$uri" in " ".join(log_format.words)
    assert "$request " not in " ".join(log_format.words)
    assert "$args" not in " ".join(log_format.words)


def test_shipped_nginx_scopes_forward_websocket_upgrade_and_match_transport_budget() -> None:
    _assert_websocket_contract((ROOT / "deploy/compose/nginx.conf").read_text())


@pytest.mark.parametrize(
    "directive",
    [
        "proxy_set_header Upgrade $http_upgrade;",
        "proxy_set_header Connection $elspeth_connection_upgrade;",
        "proxy_http_version 1.1;",
    ],
)
def test_contract_rejects_missing_upgrade_forwarding(directive: str) -> None:
    config = (ROOT / "deploy/compose/nginx.conf").read_text()
    assert config.count(directive) == 1
    with pytest.raises(AssertionError):
        _assert_websocket_contract(config.replace(directive, ""))


def test_parser_rejects_unbalanced_scope() -> None:
    assert _parse("map $http_upgrade $connection { default upgrade; '' close; }")
    with pytest.raises(AssertionError):
        _parse("map $http_upgrade $connection { default upgrade;")
