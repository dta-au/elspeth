import inspect

from elspeth_llm_gateway.core.auth import check_bearer

EXPECTED = "sekrit-token-123"


# --- accepted shape ----------------------------------------------------------


def test_correct_bearer_token_passes():
    assert check_bearer(f"Bearer {EXPECTED}", EXPECTED) is True


# --- rejected shapes -----------------------------------------------------------


def test_wrong_token_fails():
    assert check_bearer("Bearer wrong-token", EXPECTED) is False


def test_lowercase_scheme_fails():
    assert check_bearer(f"bearer {EXPECTED}", EXPECTED) is False


def test_double_space_after_scheme_fails():
    assert check_bearer(f"Bearer  {EXPECTED}", EXPECTED) is False


def test_basic_scheme_fails():
    assert check_bearer(f"Basic {EXPECTED}", EXPECTED) is False


def test_empty_header_fails():
    assert check_bearer("", EXPECTED) is False


def test_none_header_fails():
    assert check_bearer(None, EXPECTED) is False


def test_token_with_trailing_space_fails():
    assert check_bearer(f"Bearer {EXPECTED} ", EXPECTED) is False


def test_header_missing_scheme_prefix_fails():
    assert check_bearer(EXPECTED, EXPECTED) is False


def test_header_with_no_token_fails():
    assert check_bearer("Bearer ", EXPECTED) is False


def test_header_with_no_space_fails():
    assert check_bearer(f"Bearer{EXPECTED}", EXPECTED) is False


# --- timing-safety tripwire ----------------------------------------------------


def test_check_bearer_source_uses_compare_digest():
    source = inspect.getsource(check_bearer)
    assert "hmac.compare_digest" in source
