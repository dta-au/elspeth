import json

import pytest
from elspeth_llm_gateway.core.config import ENV_PREFIX, KNOWN_ENV, ConfigError, GatewayConfig, load_config
from pydantic import ValidationError

_SECRET = "sekret" * 8  # 48 chars — long enough to pass the 32-char bearer minimum

BASE_ENV = {
    "ELSPETH_LLM_GATEWAY_INBOUND_BEARER": "b" * 40,
    "ELSPETH_LLM_GATEWAY_ADAPTER": "example_adapter",
    "ELSPETH_LLM_GATEWAY_UPSTREAM_ORIGIN": "https://upstream.example.com",
    "ELSPETH_LLM_GATEWAY_OAUTH_TOKEN_URL": "https://auth.example.com/token",
    "ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_ID": "client-id-value",
    "ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_SECRET": "c" * 40,
    "ELSPETH_LLM_GATEWAY_OAUTH_AUTH_METHOD": "client_secret_basic",
    "ELSPETH_LLM_GATEWAY_OAUTH_SCOPES": "read write",
    "ELSPETH_LLM_GATEWAY_MAX_MESSAGES": "50",
    "ELSPETH_LLM_GATEWAY_MAX_TOOLS": "10",
    "ELSPETH_LLM_GATEWAY_MAX_STRING_CHARS": "10000",
    "ELSPETH_LLM_GATEWAY_MAX_SCHEMA_BYTES": "65536",
    "ELSPETH_LLM_GATEWAY_MAX_SCHEMA_DEPTH": "10",
    "ELSPETH_LLM_GATEWAY_MODEL_MAPPINGS": '{"gpt-4o": {"target": "backend-a"}}',
}


def _env(**overrides):
    env = dict(BASE_ENV)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


# --- happy path --------------------------------------------------------------


def test_load_config_happy_path_uses_defaults_for_optional_vars():
    config = load_config(_env())

    assert isinstance(config, GatewayConfig)
    assert config.adapter_name == "example_adapter"
    assert config.upstream_origin == "https://upstream.example.com"
    assert config.oauth_auth_method == "client_secret_basic"
    assert config.oauth_scopes == ("read", "write")
    assert config.oauth_fixed_lifetime_seconds is None
    assert config.refresh_skew_seconds == 60
    assert config.request_timeout_seconds == 60.0
    assert config.max_body_bytes == 1_048_576
    assert config.max_response_bytes == 4_194_304
    assert config.bounds.max_messages == 50
    assert config.bounds.max_tools == 10
    assert config.bounds.temperature_min == 0.0
    assert config.bounds.temperature_max == 2.0
    assert config.bounds.max_max_tokens == 32768
    assert config.model_mappings == {"gpt-4o": {"target": "backend-a"}}
    assert config.inbound_bearer.get_secret_value() == "b" * 40


def test_load_config_ignores_unrelated_env():
    env = _env()
    env["PATH"] = "/usr/bin:/bin"
    env["ELSPETH_OTHER_X"] = "whatever"
    config = load_config(env)
    assert isinstance(config, GatewayConfig)


def test_known_env_matches_declared_variable_names():
    expected_suffixes = {
        "INBOUND_BEARER",
        "ADAPTER",
        "UPSTREAM_ORIGIN",
        "OAUTH_TOKEN_URL",
        "OAUTH_CLIENT_ID",
        "OAUTH_CLIENT_SECRET",
        "OAUTH_AUTH_METHOD",
        "OAUTH_SCOPES",
        "OAUTH_FIXED_LIFETIME_SECONDS",
        "REFRESH_SKEW_SECONDS",
        "REQUEST_TIMEOUT_SECONDS",
        "MAX_BODY_BYTES",
        "MAX_RESPONSE_BYTES",
        "MAX_MESSAGES",
        "MAX_TOOLS",
        "MAX_STRING_CHARS",
        "MAX_SCHEMA_BYTES",
        "MAX_SCHEMA_DEPTH",
        "TEMPERATURE_MIN",
        "TEMPERATURE_MAX",
        "MAX_MAX_TOKENS",
        "MODEL_MAPPINGS",
    }
    assert {ENV_PREFIX + suffix for suffix in expected_suffixes} == KNOWN_ENV
    assert BASE_ENV.keys() <= KNOWN_ENV | {"PATH", "ELSPETH_OTHER_X"}


# --- unknown / missing env ----------------------------------------------------


def test_unknown_prefixed_env_var_raises_with_code():
    env = _env()
    env["ELSPETH_LLM_GATEWAY_TYPO"] = "x"
    with pytest.raises(ConfigError) as exc_info:
        load_config(env)
    assert "unknown_env:ELSPETH_LLM_GATEWAY_TYPO" in exc_info.value.errors


def test_multiple_unknown_env_vars_all_collected():
    env = _env()
    env["ELSPETH_LLM_GATEWAY_TYPO_ONE"] = "x"
    env["ELSPETH_LLM_GATEWAY_TYPO_TWO"] = "y"
    with pytest.raises(ConfigError) as exc_info:
        load_config(env)
    assert "unknown_env:ELSPETH_LLM_GATEWAY_TYPO_ONE" in exc_info.value.errors
    assert "unknown_env:ELSPETH_LLM_GATEWAY_TYPO_TWO" in exc_info.value.errors


def test_missing_required_env_var_raises_with_code():
    env = _env(ELSPETH_LLM_GATEWAY_INBOUND_BEARER=None)
    with pytest.raises(ConfigError) as exc_info:
        load_config(env)
    assert "missing_env:ELSPETH_LLM_GATEWAY_INBOUND_BEARER" in exc_info.value.errors


def test_collects_all_errors_at_once_not_just_the_first():
    env = _env(
        ELSPETH_LLM_GATEWAY_INBOUND_BEARER=None,
        ELSPETH_LLM_GATEWAY_ADAPTER=None,
        ELSPETH_LLM_GATEWAY_UPSTREAM_ORIGIN="http://localhost:9",
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(env)
    errors = exc_info.value.errors
    assert "missing_env:ELSPETH_LLM_GATEWAY_INBOUND_BEARER" in errors
    assert "missing_env:ELSPETH_LLM_GATEWAY_ADAPTER" in errors
    assert "invalid_origin" in errors
    assert len(errors) >= 3


# --- origin rule ---------------------------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [
        "https://upstream.example.com",
        "https://upstream.example.com:8443",
        "http://127.0.0.1:9",
    ],
)
def test_origin_accepted(origin):
    config = load_config(_env(ELSPETH_LLM_GATEWAY_UPSTREAM_ORIGIN=origin))
    assert config.upstream_origin == origin


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:9",
        "https://h/path",
        "https://user@h",
        "not-a-url",
        "https://h?query=1",
        "ftp://h",
    ],
)
def test_origin_rejected(origin):
    with pytest.raises(ConfigError) as exc_info:
        load_config(_env(ELSPETH_LLM_GATEWAY_UPSTREAM_ORIGIN=origin))
    assert "invalid_origin" in exc_info.value.errors


# --- bearer --------------------------------------------------------------------


def test_bearer_too_short_rejected():
    with pytest.raises(ConfigError) as exc_info:
        load_config(_env(ELSPETH_LLM_GATEWAY_INBOUND_BEARER="short"))
    assert "bearer_too_short" in exc_info.value.errors


def test_bearer_exactly_32_chars_accepted():
    config = load_config(_env(ELSPETH_LLM_GATEWAY_INBOUND_BEARER="b" * 32))
    assert config.inbound_bearer.get_secret_value() == "b" * 32


def test_bearer_all_whitespace_rejected():
    """A bearer of 32 spaces passes the length check but is empty in every
    way that matters; it must fail closed the same way the other four
    required strings do (see the ``empty_env`` sweep below)."""
    with pytest.raises(ConfigError) as exc_info:
        load_config(_env(ELSPETH_LLM_GATEWAY_INBOUND_BEARER=" " * 32))
    assert f"empty_env:{ENV_PREFIX}INBOUND_BEARER" in exc_info.value.errors


# --- model mappings --------------------------------------------------------------


def test_empty_model_mappings_rejected():
    with pytest.raises(ConfigError) as exc_info:
        load_config(_env(ELSPETH_LLM_GATEWAY_MODEL_MAPPINGS="{}"))
    assert "empty_model_mappings" in exc_info.value.errors


def test_model_mappings_not_json_object_rejected():
    with pytest.raises(ConfigError) as exc_info:
        load_config(_env(ELSPETH_LLM_GATEWAY_MODEL_MAPPINGS="[1, 2]"))
    assert "model_mappings_not_object" in exc_info.value.errors


def test_model_mappings_invalid_json_rejected():
    with pytest.raises(ConfigError) as exc_info:
        load_config(_env(ELSPETH_LLM_GATEWAY_MODEL_MAPPINGS="not json"))
    assert "invalid_model_mappings_json" in exc_info.value.errors


def test_deeply_nested_model_mappings_rejected_as_config_error_not_recursion_error():
    """A MODEL_MAPPINGS value that is deeply nested but individually tiny
    (well under the model-mappings byte cap) must raise a clean ConfigError,
    not a raw RecursionError escaping load_config -- see
    parse_strict_json's own depth guard in core/parsing.py."""
    deeply_nested = "[" * 2000 + "]" * 2000
    with pytest.raises(ConfigError) as exc_info:
        load_config(_env(ELSPETH_LLM_GATEWAY_MODEL_MAPPINGS=deeply_nested))
    assert "invalid_model_mappings_json" in exc_info.value.errors


def test_model_mappings_invalid_alias_rejected():
    with pytest.raises(ConfigError) as exc_info:
        load_config(_env(ELSPETH_LLM_GATEWAY_MODEL_MAPPINGS='{"Bad Alias!": {"target": "x"}}'))
    assert any(err.startswith("invalid_model_alias:") for err in exc_info.value.errors)


def test_model_mappings_non_dict_value_rejected():
    with pytest.raises(ConfigError) as exc_info:
        load_config(_env(ELSPETH_LLM_GATEWAY_MODEL_MAPPINGS='{"gpt-4o": "not-a-dict"}'))
    assert any(err.startswith("invalid_model_mapping_value:") for err in exc_info.value.errors)


def test_mapping_generation_stable_across_key_order():
    mappings_a = {"gpt-4o": {"target": "a"}, "claude": {"target": "b"}}
    mappings_b = {"claude": {"target": "b"}, "gpt-4o": {"target": "a"}}

    config_a = load_config(_env(ELSPETH_LLM_GATEWAY_MODEL_MAPPINGS=json.dumps(mappings_a)))
    config_b = load_config(_env(ELSPETH_LLM_GATEWAY_MODEL_MAPPINGS=json.dumps(mappings_b)))

    assert config_a.mapping_generation == config_b.mapping_generation
    assert len(config_a.mapping_generation) == 16


def test_mapping_generation_differs_for_different_mappings():
    config_a = load_config(_env(ELSPETH_LLM_GATEWAY_MODEL_MAPPINGS=json.dumps({"gpt-4o": {"target": "a"}})))
    config_b = load_config(_env(ELSPETH_LLM_GATEWAY_MODEL_MAPPINGS=json.dumps({"gpt-4o": {"target": "b"}})))
    assert config_a.mapping_generation != config_b.mapping_generation


# --- oauth auth method / scopes -------------------------------------------------


def test_invalid_oauth_auth_method_rejected():
    with pytest.raises(ConfigError) as exc_info:
        load_config(_env(ELSPETH_LLM_GATEWAY_OAUTH_AUTH_METHOD="bogus_method"))
    assert "invalid_oauth_auth_method" in exc_info.value.errors


def test_oauth_scopes_default_empty_tuple_when_absent():
    config = load_config(_env(ELSPETH_LLM_GATEWAY_OAUTH_SCOPES=None))
    assert config.oauth_scopes == ()


def test_oauth_scopes_space_separated():
    config = load_config(_env(ELSPETH_LLM_GATEWAY_OAUTH_SCOPES="alpha  beta   gamma"))
    assert config.oauth_scopes == ("alpha", "beta", "gamma")


def test_oauth_scopes_with_embedded_tab_rejected():
    with pytest.raises(ConfigError) as exc_info:
        load_config(_env(ELSPETH_LLM_GATEWAY_OAUTH_SCOPES="read\twrite"))
    assert "invalid_oauth_scopes:ELSPETH_LLM_GATEWAY_OAUTH_SCOPES" in exc_info.value.errors


def test_oauth_scopes_with_disallowed_char_rejected():
    with pytest.raises(ConfigError) as exc_info:
        load_config(_env(ELSPETH_LLM_GATEWAY_OAUTH_SCOPES='read"write'))
    assert "invalid_oauth_scopes:ELSPETH_LLM_GATEWAY_OAUTH_SCOPES" in exc_info.value.errors


# --- numeric overrides -----------------------------------------------------------


def test_numeric_override_applies():
    config = load_config(_env(ELSPETH_LLM_GATEWAY_REFRESH_SKEW_SECONDS="120"))
    assert config.refresh_skew_seconds == 120


def test_invalid_int_env_var_rejected():
    with pytest.raises(ConfigError) as exc_info:
        load_config(_env(ELSPETH_LLM_GATEWAY_REFRESH_SKEW_SECONDS="not-an-int"))
    assert "invalid_int:ELSPETH_LLM_GATEWAY_REFRESH_SKEW_SECONDS" in exc_info.value.errors


def test_invalid_float_env_var_rejected():
    with pytest.raises(ConfigError) as exc_info:
        load_config(_env(ELSPETH_LLM_GATEWAY_REQUEST_TIMEOUT_SECONDS="not-a-float"))
    assert "invalid_float:ELSPETH_LLM_GATEWAY_REQUEST_TIMEOUT_SECONDS" in exc_info.value.errors


def test_bounds_temperature_and_max_tokens_overridable():
    config = load_config(
        _env(
            ELSPETH_LLM_GATEWAY_TEMPERATURE_MIN="0.1",
            ELSPETH_LLM_GATEWAY_TEMPERATURE_MAX="1.5",
            ELSPETH_LLM_GATEWAY_MAX_MAX_TOKENS="4096",
        )
    )
    assert config.bounds.temperature_min == 0.1
    assert config.bounds.temperature_max == 1.5
    assert config.bounds.max_max_tokens == 4096


@pytest.mark.parametrize("non_finite", ["inf", "-inf", "Infinity", "nan"])
def test_non_finite_float_env_var_rejected(non_finite):
    with pytest.raises(ConfigError) as exc_info:
        load_config(_env(ELSPETH_LLM_GATEWAY_REQUEST_TIMEOUT_SECONDS=non_finite))
    assert "non_finite_float:ELSPETH_LLM_GATEWAY_REQUEST_TIMEOUT_SECONDS" in exc_info.value.errors


def test_non_finite_temperature_max_rejected():
    with pytest.raises(ConfigError) as exc_info:
        load_config(_env(ELSPETH_LLM_GATEWAY_TEMPERATURE_MAX="inf"))
    assert "non_finite_float:ELSPETH_LLM_GATEWAY_TEMPERATURE_MAX" in exc_info.value.errors


@pytest.mark.parametrize(
    "env_key,bad_value",
    [
        ("ELSPETH_LLM_GATEWAY_MAX_MESSAGES", "0"),
        ("ELSPETH_LLM_GATEWAY_MAX_MESSAGES", "-1"),
        ("ELSPETH_LLM_GATEWAY_MAX_TOOLS", "0"),
        ("ELSPETH_LLM_GATEWAY_MAX_STRING_CHARS", "0"),
        ("ELSPETH_LLM_GATEWAY_MAX_SCHEMA_BYTES", "0"),
        ("ELSPETH_LLM_GATEWAY_MAX_SCHEMA_DEPTH", "0"),
        ("ELSPETH_LLM_GATEWAY_MAX_MAX_TOKENS", "0"),
        ("ELSPETH_LLM_GATEWAY_MAX_BODY_BYTES", "0"),
        ("ELSPETH_LLM_GATEWAY_MAX_RESPONSE_BYTES", "0"),
        ("ELSPETH_LLM_GATEWAY_OAUTH_FIXED_LIFETIME_SECONDS", "0"),
    ],
)
def test_non_positive_bound_rejected(env_key, bad_value):
    with pytest.raises(ConfigError) as exc_info:
        load_config(_env(**{env_key: bad_value}))
    assert f"out_of_range:{env_key}" in exc_info.value.errors


def test_request_timeout_seconds_zero_rejected():
    with pytest.raises(ConfigError) as exc_info:
        load_config(_env(ELSPETH_LLM_GATEWAY_REQUEST_TIMEOUT_SECONDS="0"))
    assert "out_of_range:ELSPETH_LLM_GATEWAY_REQUEST_TIMEOUT_SECONDS" in exc_info.value.errors


def test_refresh_skew_seconds_zero_accepted():
    config = load_config(_env(ELSPETH_LLM_GATEWAY_REFRESH_SKEW_SECONDS="0"))
    assert config.refresh_skew_seconds == 0


def test_refresh_skew_seconds_negative_rejected():
    with pytest.raises(ConfigError) as exc_info:
        load_config(_env(ELSPETH_LLM_GATEWAY_REFRESH_SKEW_SECONDS="-1"))
    assert "out_of_range:ELSPETH_LLM_GATEWAY_REFRESH_SKEW_SECONDS" in exc_info.value.errors


# --- empty required strings -------------------------------------------------------


@pytest.mark.parametrize(
    "env_key",
    [
        "ELSPETH_LLM_GATEWAY_ADAPTER",
        "ELSPETH_LLM_GATEWAY_OAUTH_TOKEN_URL",
        "ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_ID",
        "ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_SECRET",
    ],
)
def test_empty_required_string_env_var_rejected(env_key):
    with pytest.raises(ConfigError) as exc_info:
        load_config(_env(**{env_key: ""}))
    assert f"empty_env:{env_key}" in exc_info.value.errors


def test_whitespace_only_required_string_env_var_rejected():
    with pytest.raises(ConfigError) as exc_info:
        load_config(_env(ELSPETH_LLM_GATEWAY_ADAPTER="   "))
    assert "empty_env:ELSPETH_LLM_GATEWAY_ADAPTER" in exc_info.value.errors


# --- origin ambiguous-syntax hardening ---------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [
        "https://upstream.example.com\r\n",
        "  https://upstream.example.com",
        "https://upstream.example.com  ",
        "HTTPS://upstream.example.com",
        "https://upstream.example.com/",
    ],
)
def test_origin_non_canonical_form_rejected(origin):
    with pytest.raises(ConfigError) as exc_info:
        load_config(_env(ELSPETH_LLM_GATEWAY_UPSTREAM_ORIGIN=origin))
    assert "invalid_origin" in exc_info.value.errors


def test_origin_with_embedded_control_char_rejected():
    with pytest.raises(ConfigError) as exc_info:
        load_config(_env(ELSPETH_LLM_GATEWAY_UPSTREAM_ORIGIN="https://ho\x00st.example.com"))
    assert "invalid_origin" in exc_info.value.errors


# --- frozen / secrecy ------------------------------------------------------------


def test_gateway_config_is_frozen():
    config = load_config(_env())
    with pytest.raises(ValidationError):
        config.adapter_name = "other"


def test_repr_does_not_leak_secrets():
    env = _env(
        ELSPETH_LLM_GATEWAY_INBOUND_BEARER=_SECRET,
        ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_SECRET=_SECRET,
    )
    config = load_config(env)
    assert _SECRET not in repr(config)
    assert _SECRET not in str(config)


def test_configerror_does_not_leak_secrets_when_other_errors_present():
    env = _env(
        ELSPETH_LLM_GATEWAY_INBOUND_BEARER=_SECRET,
        ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_SECRET=_SECRET,
        ELSPETH_LLM_GATEWAY_UPSTREAM_ORIGIN="http://localhost:9",
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(env)
    assert _SECRET not in repr(exc_info.value)
    assert _SECRET not in str(exc_info.value)
    assert all(_SECRET not in err for err in exc_info.value.errors)
