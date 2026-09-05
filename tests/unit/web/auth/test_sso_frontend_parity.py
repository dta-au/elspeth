"""The SPA's failure vocabulary is the backend's, key for key.

``web/auth/sso.py`` sends a refused login back to the browser as
``#/auth/callback?error=<category>`` where the category is one of
``SSO_FAILURE_CATEGORIES``. The SPA's ``ssoCallback.ts`` refuses any category
it does not know as malformed, so a category added on one side and not the
other is a login failure the person is told nothing useful about. This pins
the two tables to each other by reading the TypeScript source, the same way
the provider discriminator and the semantic-edge contract are pinned.
"""

from __future__ import annotations

import re
from pathlib import Path

import elspeth
from elspeth.web.auth.sso import SSO_FAILURE_CATEGORIES

_TS_CALLBACK_PATH = Path(elspeth.__file__).parent / "web" / "frontend" / "src" / "components" / "auth" / "ssoCallback.ts"
_TS_TABLE_RE = re.compile(r"^export const SSO_FAILURE_MESSAGES = \{\n(?P<body>.*?)^\} as const;", re.MULTILINE | re.DOTALL)
_TS_ENTRY_RE = re.compile(r'^\s*(?P<key>[a-z_]+):\s*"(?P<message>[^"]*)",\s*$', re.MULTILINE)


def _frontend_table() -> dict[str, str]:
    source = _TS_CALLBACK_PATH.read_text(encoding="utf-8")
    match = _TS_TABLE_RE.search(source)
    assert match is not None, f"SSO_FAILURE_MESSAGES not found in {_TS_CALLBACK_PATH}"
    entries = {m.group("key"): m.group("message") for m in _TS_ENTRY_RE.finditer(match.group("body"))}
    assert entries, "the table parsed to nothing — the regex no longer matches the file"
    return entries


def test_the_frontend_table_is_actually_parsed() -> None:
    assert _TS_CALLBACK_PATH.is_file(), f"expected the SPA callback module at {_TS_CALLBACK_PATH}"
    assert len(_frontend_table()) == len(SSO_FAILURE_CATEGORIES)


def test_frontend_categories_are_the_backend_categories_key_for_key() -> None:
    assert set(_frontend_table()) == set(SSO_FAILURE_CATEGORIES)


def test_every_frontend_message_is_a_sentence_that_does_not_echo_its_category() -> None:
    for category, message in _frontend_table().items():
        assert message.endswith("."), f"{category}: {message!r} is not a sentence"
        assert category not in message, f"{category}: the person is shown the audit category"
