"""Configuration security, audit redaction, and trust-boundary refusal tests."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from types import UnionType
from typing import Any, Union, cast, get_args, get_origin

import pytest
from pydantic import BaseModel


class TestSecretFieldFingerprinting:
    """Test that secret fields are preserved at load time and fingerprinted for audit.

    IMPORTANT: Secrets are kept in ElspethSettings for runtime use (transforms
    need actual credentials). Fingerprinting happens only in resolve_config()
    when creating the audit copy for Landscape storage.
    """

    def test_api_key_preserved_at_load_time(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """API keys in config should be preserved for runtime use."""
        from elspeth.config_loading import load_settings

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: http_source
    on_success: output
    options:
      api_key: sk-secret-key-12345
      url: https://api.example.com
sinks:
  output:
    plugin: csv_sink
    on_write_failure: discard
    options:
      path: output.csv

""")

        settings = load_settings(config_file)

        # API key should be preserved for runtime (transforms need it!)
        assert "api_key" in settings.sources["primary"].options
        assert settings.sources["primary"].options["api_key"] == "sk-secret-key-12345"

    def test_api_key_is_fingerprinted_in_resolve_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """API keys should be fingerprinted when creating audit copy."""
        from elspeth.config_loading import load_settings
        from elspeth.core.config import resolve_config

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: http_source
    on_success: output
    options:
      api_key: sk-secret-key-12345
      url: https://api.example.com
sinks:
  output:
    plugin: csv_sink
    on_write_failure: discard
    options:
      path: output.csv

""")

        settings = load_settings(config_file)
        audit_config = resolve_config(settings)

        # API key should be removed in audit copy
        assert "api_key" not in audit_config["sources"]["primary"]["options"]
        # Should have a 64-char hex fingerprint
        fingerprint = audit_config["sources"]["primary"]["options"].get("api_key_fingerprint")
        assert fingerprint is not None
        assert len(fingerprint) == 64
        assert all(c in "0123456789abcdef" for c in fingerprint)

    def test_named_source_api_keys_are_fingerprinted_in_resolve_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every named source should fingerprint secret options in the audit copy."""
        from elspeth.config_loading import load_settings
        from elspeth.core.config import resolve_config

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  orders:
    plugin: http_source
    on_success: output
    options:
      api_key: sk-orders-key
      url: https://api.example.com/orders
  refunds:
    plugin: http_source
    on_success: output
    options:
      api_key: sk-refunds-key
      url: https://api.example.com/refunds
sinks:
  output:
    plugin: csv_sink
    on_write_failure: discard
    options:
      path: output.csv

""")

        settings = load_settings(config_file)
        audit_config = resolve_config(settings)

        for source_name in ("orders", "refunds"):
            source_options = audit_config["sources"][source_name]["options"]
            assert "api_key" not in source_options
            fingerprint = source_options["api_key_fingerprint"]
            assert len(fingerprint) == 64
            assert all(c in "0123456789abcdef" for c in fingerprint)

    def test_token_preserved_at_load_time(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Token fields should be preserved for runtime use."""
        from elspeth.config_loading import load_settings

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: webhook_source
    on_success: output
    options:
      token: bearer-token-xyz
sinks:
  output:
    plugin: csv_sink
    on_write_failure: discard

""")

        settings = load_settings(config_file)

        assert "token" in settings.sources["primary"].options
        assert settings.sources["primary"].options["token"] == "bearer-token-xyz"

    def test_token_is_fingerprinted_in_resolve_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Token fields should be fingerprinted in audit copy."""
        from elspeth.config_loading import load_settings
        from elspeth.core.config import resolve_config

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: webhook_source
    on_success: output
    options:
      token: bearer-token-xyz
sinks:
  output:
    plugin: csv_sink
    on_write_failure: discard

""")

        settings = load_settings(config_file)
        audit_config = resolve_config(settings)

        assert "token" not in audit_config["sources"]["primary"]["options"]
        assert "token_fingerprint" in audit_config["sources"]["primary"]["options"]

    def test_secret_suffix_preserved_at_load_time(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fields ending in _secret should be preserved for runtime."""
        from elspeth.config_loading import load_settings

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: custom_source
    on_success: output
    options:
      database_secret: my-db-password
sinks:
  output:
    plugin: csv_sink
    on_write_failure: discard

""")

        settings = load_settings(config_file)

        assert "database_secret" in settings.sources["primary"].options
        assert settings.sources["primary"].options["database_secret"] == "my-db-password"

    def test_secret_suffix_is_fingerprinted_in_resolve_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fields ending in _secret should be fingerprinted in audit copy."""
        from elspeth.config_loading import load_settings
        from elspeth.core.config import resolve_config

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: custom_source
    on_success: output
    options:
      database_secret: my-db-password
sinks:
  output:
    plugin: csv_sink
    on_write_failure: discard

""")

        settings = load_settings(config_file)
        audit_config = resolve_config(settings)

        assert "database_secret" not in audit_config["sources"]["primary"]["options"]
        assert "database_secret_fingerprint" in audit_config["sources"]["primary"]["options"]

    def test_sink_options_preserved_at_load_time(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Secret fields in sink options should be preserved for runtime."""
        from elspeth.config_loading import load_settings

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv_source
    on_success: output
sinks:
  output:
    plugin: database_sink
    on_write_failure: discard
    options:
      password: super-secret-password

""")

        settings = load_settings(config_file)

        assert "password" in settings.sinks["output"].options
        assert settings.sinks["output"].options["password"] == "super-secret-password"

    def test_sink_options_are_fingerprinted_in_resolve_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Secret fields in sink options should be fingerprinted in audit copy."""
        from elspeth.config_loading import load_settings
        from elspeth.core.config import resolve_config

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv_source
    on_success: output
sinks:
  output:
    plugin: database_sink
    on_write_failure: discard
    options:
      password: super-secret-password

""")

        settings = load_settings(config_file)
        audit_config = resolve_config(settings)

        assert "password" not in audit_config["sinks"]["output"]["options"]
        assert "password_fingerprint" in audit_config["sinks"]["output"]["options"]

    def test_non_secret_fields_preserved(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-secret fields should remain unchanged."""
        from elspeth.config_loading import load_settings

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv_source
    on_success: output
    options:
      path: input.csv
      delimiter: ","
sinks:
  output:
    plugin: csv_sink
    on_write_failure: discard
    options:
      path: output.csv

""")

        settings = load_settings(config_file)

        # Regular fields should be preserved
        assert settings.sources["primary"].options["path"] == "input.csv"
        assert settings.sources["primary"].options["delimiter"] == ","

    def test_options_with_sinks_key_preserved(self, tmp_path: Path) -> None:
        """Options containing a 'sinks' key should be preserved exactly.

        Regression test: _lowercase_schema_keys must not apply sink name
        handling to user data that happens to contain a 'sinks' key.
        """
        from elspeth.config_loading import load_settings

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv_source
    on_success: output
    options:
      sinks:
        MySink: some_value
      CaseSensitive: preserved
sinks:
  output:
    plugin: csv_sink
    on_write_failure: discard

""")

        settings = load_settings(config_file)

        # Keys inside options must be preserved exactly (case-sensitive)
        assert "sinks" in settings.sources["primary"].options
        assert "MySink" in settings.sources["primary"].options["sinks"]
        assert settings.sources["primary"].options["sinks"]["MySink"] == "some_value"
        assert "CaseSensitive" in settings.sources["primary"].options

    def test_row_plugin_options_preserved_at_load_time(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Secret fields in row_plugins options should be preserved for runtime."""
        from elspeth.config_loading import load_settings

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv_source
    on_success: output
sinks:
  output:
    plugin: csv_sink
    on_write_failure: discard

transforms:
  - name: t1
    plugin: llm_transform
    input: source_out
    on_success: output
    on_error: discard
    options:
      api_key: openai-key-123
      model: gpt-4
""")

        settings = load_settings(config_file)

        # Secrets preserved for runtime
        assert "api_key" in settings.transforms[0].options
        assert settings.transforms[0].options["api_key"] == "openai-key-123"
        # Non-secret field preserved
        assert settings.transforms[0].options["model"] == "gpt-4"

    def test_row_plugin_options_are_fingerprinted_in_resolve_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Secret fields in row_plugins options should be fingerprinted in audit copy."""
        from elspeth.config_loading import load_settings
        from elspeth.core.config import resolve_config

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv_source
    on_success: output
sinks:
  output:
    plugin: csv_sink
    on_write_failure: discard

transforms:
  - name: t1
    plugin: llm_transform
    input: source_out
    on_success: output
    on_error: discard
    options:
      api_key: openai-key-123
      model: gpt-4
""")

        settings = load_settings(config_file)
        audit_config = resolve_config(settings)

        assert "api_key" not in audit_config["transforms"][0]["options"]
        assert "api_key_fingerprint" in audit_config["transforms"][0]["options"]
        # Non-secret field preserved
        assert audit_config["transforms"][0]["options"]["model"] == "gpt-4"

    def test_telemetry_exporter_options_are_fingerprinted_in_resolve_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Telemetry exporter secrets should be fingerprinted in audit copy."""
        from elspeth.config_loading import load_settings
        from elspeth.core.config import resolve_config

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv_source
    on_success: output
sinks:
  output:
    plugin: csv_sink
    on_write_failure: discard

telemetry:
  enabled: true
  exporters:
    - name: otlp
      options:
        endpoint: http://localhost:4317
        headers:
          Authorization: "Bearer secret-token"
    - name: azure_monitor
      options:
        connection_string: "InstrumentationKey=secret-value"
    - name: datadog
      options:
        api_key: "dd-secret"
""")

        settings = load_settings(config_file)
        audit_config = resolve_config(settings)

        exporters = audit_config["telemetry"]["exporters"]
        otlp_options = exporters[0]["options"]
        azure_options = exporters[1]["options"]
        dd_options = exporters[2]["options"]

        assert otlp_options["endpoint"] == "http://localhost:4317"
        assert "Authorization" not in otlp_options["headers"]
        assert "Authorization_fingerprint" in otlp_options["headers"]

        assert "connection_string" not in azure_options
        assert "connection_string_fingerprint" in azure_options

        assert "api_key" not in dd_options
        assert "api_key_fingerprint" in dd_options

    # === Tests for recursive/nested fingerprinting ===

    def test_nested_secrets_are_fingerprinted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Secrets in nested dicts should be fingerprinted."""
        from elspeth.core.config import _fingerprint_secrets

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        options = {
            "api_key": "sk-top-level",
            "auth": {
                "api_key": "sk-nested",
                "nested": {"token": "nested-token"},
            },
        }

        result = _fingerprint_secrets(options)

        # Top-level secret fingerprinted
        assert "api_key" not in result
        assert "api_key_fingerprint" in result

        # Nested secrets fingerprinted
        assert "api_key" not in result["auth"]
        assert "api_key_fingerprint" in result["auth"]
        assert "token" not in result["auth"]["nested"]
        assert "token_fingerprint" in result["auth"]["nested"]

    def test_connection_string_and_authorization_are_fingerprinted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Connection strings and Authorization headers should be fingerprinted."""
        from elspeth.core.config import _fingerprint_secrets

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        options = {
            "connection_string": "InstrumentationKey=secret-value",
            "headers": {
                "Authorization": "Bearer secret-token",
                "User-Agent": "elspeth-tests",
            },
        }

        result = _fingerprint_secrets(options)

        assert "connection_string" not in result
        assert "connection_string_fingerprint" in result

        headers = result["headers"]
        assert "Authorization" not in headers
        assert "Authorization_fingerprint" in headers
        assert headers["User-Agent"] == "elspeth-tests"

    def test_secrets_in_lists_are_fingerprinted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Secrets inside list elements should be fingerprinted."""
        from elspeth.core.config import _fingerprint_secrets

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        options = {
            "providers": [
                {"name": "openai", "api_key": "sk-openai"},
                {"name": "anthropic", "api_key": "sk-anthropic"},
            ]
        }

        result = _fingerprint_secrets(options)

        for provider in result["providers"]:
            assert "api_key" not in provider
            assert "api_key_fingerprint" in provider

    def test_structural_key_fields_not_fingerprinted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Structural ``*_key`` fields stay plaintext in the audit config capture.

        Regression for elspeth-61f2c0732e: ``data_key`` (JSON extraction key)
        and ``alternate_key`` (Dataverse upsert column) are exempt from the
        bare ``_key`` suffix heuristic. ``secret_key`` is the leak guard — a
        real credential matched only by that suffix must keep fingerprinting.
        """
        from elspeth.core.config import _fingerprint_secrets

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        options = {
            "data_key": "results",
            "sink": {"alternate_key": "crabc_code"},
            "secret_key": "sk-lf-1234",
        }

        result = _fingerprint_secrets(options)

        assert result["data_key"] == "results"
        assert "data_key_fingerprint" not in result
        assert result["sink"]["alternate_key"] == "crabc_code"
        assert "alternate_key_fingerprint" not in result["sink"]
        assert "secret_key" not in result
        assert "secret_key_fingerprint" in result

    # === Tests for fail-closed behavior ===

    def test_missing_key_raises_error_on_fingerprint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing fingerprint key should raise SecretFingerprintError."""
        from elspeth.core.config import SecretFingerprintError, _fingerprint_secrets

        monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY", raising=False)
        monkeypatch.delenv("ELSPETH_ALLOW_RAW_SECRETS", raising=False)

        options = {"api_key": "sk-secret"}

        with pytest.raises(SecretFingerprintError) as exc_info:
            _fingerprint_secrets(options, fail_if_no_key=True)

        assert "ELSPETH_FINGERPRINT_KEY" in str(exc_info.value)
        assert "api_key" in str(exc_info.value)

    def test_load_settings_allows_secrets_without_fingerprint_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """load_settings should allow secrets without fingerprint key (for runtime).

        Secrets are preserved at load time for transforms to use. The
        fingerprint key is only required when calling resolve_config()
        to create the audit copy.
        """
        from elspeth.config_loading import load_settings

        monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY", raising=False)
        monkeypatch.delenv("ELSPETH_ALLOW_RAW_SECRETS", raising=False)

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: http_source
    on_success: output
    options:
      api_key: sk-secret-key
sinks:
  output:
    plugin: csv_sink
    on_write_failure: discard

""")

        # load_settings succeeds even without fingerprint key
        settings = load_settings(config_file)
        assert settings.sources["primary"].options["api_key"] == "sk-secret-key"

    def test_missing_key_raises_error_on_resolve_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """resolve_config should raise SecretFingerprintError when key missing."""
        from elspeth.config_loading import load_settings
        from elspeth.core.config import SecretFingerprintError, resolve_config

        monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY", raising=False)
        monkeypatch.delenv("ELSPETH_ALLOW_RAW_SECRETS", raising=False)

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: http_source
    on_success: output
    options:
      api_key: sk-secret-key
sinks:
  output:
    plugin: csv_sink
    on_write_failure: discard

""")

        settings = load_settings(config_file)

        with pytest.raises(SecretFingerprintError) as exc_info:
            resolve_config(settings)

        assert "ELSPETH_FINGERPRINT_KEY" in str(exc_info.value)

    def test_dev_mode_keeps_secrets_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ELSPETH_ALLOW_RAW_SECRETS=true should keep secrets as-is for dev use."""
        from elspeth.core.config import _fingerprint_secrets

        monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY", raising=False)
        monkeypatch.setenv("ELSPETH_ALLOW_RAW_SECRETS", "true")

        options = {"api_key": "sk-secret"}

        result = _fingerprint_secrets(options, fail_if_no_key=False)

        # In dev mode, secrets are kept unchanged so plugins can use them
        assert result.get("api_key") == "sk-secret"
        assert "api_key_redacted" not in result

    def test_dev_mode_allows_load_settings(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """ELSPETH_ALLOW_RAW_SECRETS=true should allow load and keep secrets as-is."""
        from elspeth.config_loading import load_settings

        monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY", raising=False)
        monkeypatch.setenv("ELSPETH_ALLOW_RAW_SECRETS", "true")

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: http_source
    on_success: output
    options:
      api_key: sk-secret-key
sinks:
  output:
    plugin: csv_sink
    on_write_failure: discard

""")

        settings = load_settings(config_file)

        # In dev mode, secrets are kept unchanged so plugins can use them
        assert settings.sources["primary"].options.get("api_key") == "sk-secret-key"
        assert "api_key_redacted" not in settings.sources["primary"].options

    # === Tests for fingerprint key collision ===

    def test_fingerprint_collision_rejects_preexisting_fingerprint_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config with both secret and _fingerprint fields must be rejected.

        Without this check, an attacker could inject a fake fingerprint by
        including both 'api_key' and 'api_key_fingerprint' in the YAML —
        the real HMAC would be computed then overwritten by the fake value.
        """
        from elspeth.core.config import SecretFingerprintError, _fingerprint_secrets

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        with pytest.raises(SecretFingerprintError, match=r"api_key.*api_key_fingerprint"):
            _fingerprint_secrets(
                {
                    "api_key": "real-secret",
                    "api_key_fingerprint": "attacker-controlled",
                    "model": "gpt-4",
                }
            )

    def test_fingerprint_collision_detected_in_nested_dicts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Collision detection must work in nested structures."""
        from elspeth.core.config import SecretFingerprintError, _fingerprint_secrets

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        with pytest.raises(SecretFingerprintError, match=r"token.*token_fingerprint"):
            _fingerprint_secrets(
                {
                    "auth": {
                        "token": "nested-secret",
                        "token_fingerprint": "fake",
                    }
                }
            )

    def test_fingerprint_collision_not_triggered_for_nonsecret_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fields ending in _fingerprint that aren't collisions should be preserved."""
        from elspeth.core.config import _fingerprint_secrets

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        result = _fingerprint_secrets(
            {
                "browser_fingerprint": "some-hash",
                "device_id": "abc123",
            }
        )

        assert result["browser_fingerprint"] == "some-hash"
        assert result["device_id"] == "abc123"

    def test_fingerprint_collision_not_triggered_without_secret_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A _fingerprint field alone (without the secret field) is fine."""
        from elspeth.core.config import _fingerprint_secrets

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        # api_key_fingerprint exists but api_key does not — no collision
        result = _fingerprint_secrets(
            {
                "api_key_fingerprint": "pre-computed-hash",
                "model": "gpt-4",
            }
        )

        assert result["api_key_fingerprint"] == "pre-computed-hash"

    # === Tests for DSN password handling ===

    def test_dsn_password_sanitized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DSN passwords should be removed and fingerprinted."""
        from elspeth.core.config import _sanitize_dsn

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        url = "postgresql://user:secret_password@localhost:5432/mydb"  # secret-scan: allow-this-line
        sanitized, fingerprint, had_password = _sanitize_dsn(url)

        assert "secret_password" not in sanitized
        assert "user@localhost" in sanitized
        assert "***" not in sanitized  # Should NOT have placeholder
        assert fingerprint is not None
        assert len(fingerprint) == 64  # SHA256 hex
        assert had_password is True

    def test_dsn_without_password_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DSN without password should pass through."""
        from elspeth.core.config import _sanitize_dsn

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        url = "sqlite:///path/to/db.sqlite"
        sanitized, fingerprint, had_password = _sanitize_dsn(url)

        assert sanitized == url
        assert fingerprint is None
        assert had_password is False

    def test_dsn_password_raises_without_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DSN with password should raise when no fingerprint key."""
        from elspeth.core.config import SecretFingerprintError, _sanitize_dsn

        monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY", raising=False)
        monkeypatch.delenv("ELSPETH_ALLOW_RAW_SECRETS", raising=False)

        url = "postgresql://user:secret@localhost/db"  # secret-scan: allow-this-line

        with pytest.raises(SecretFingerprintError) as exc_info:
            _sanitize_dsn(url, fail_if_no_key=True)

        assert "ELSPETH_FINGERPRINT_KEY" in str(exc_info.value)

    def test_dsn_password_redacted_in_dev_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DSN password should be removed (not fingerprinted) in dev mode."""
        from elspeth.core.config import _sanitize_dsn

        monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY", raising=False)

        url = "postgresql://user:secret@localhost/db"  # secret-scan: allow-this-line
        sanitized, fingerprint, had_password = _sanitize_dsn(url, fail_if_no_key=False)

        assert "secret" not in sanitized
        assert fingerprint is None
        assert had_password is True

    def test_landscape_url_password_fingerprinted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """landscape.url password should be fingerprinted in audit copy."""
        from elspeth.core.config import _fingerprint_config_for_audit

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        config_dict = {
            "landscape": {"url": "postgresql://user:mysecret@host/db"},  # secret-scan: allow-this-line
            "sources": {"primary": {"plugin": "csv", "options": {}}},
            "sinks": {"output": {"plugin": "csv_sink"}},
        }

        result = _fingerprint_config_for_audit(config_dict)

        assert "mysecret" not in result["landscape"]["url"]
        assert "***" not in result["landscape"]["url"]
        assert "url_password_fingerprint" in result["landscape"]
        assert len(result["landscape"]["url_password_fingerprint"]) == 64

    def test_landscape_url_password_redacted_in_dev_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """landscape.url password should be redacted (with flag) in dev mode."""
        from elspeth.core.config import _fingerprint_config_for_audit

        monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY", raising=False)
        monkeypatch.setenv("ELSPETH_ALLOW_RAW_SECRETS", "true")

        config_dict = {
            "landscape": {"url": "postgresql://user:mysecret@host/db"},  # secret-scan: allow-this-line
            "sources": {"primary": {"plugin": "csv", "options": {}}},
            "sinks": {"output": {"plugin": "csv_sink"}},
        }

        result = _fingerprint_config_for_audit(config_dict)

        assert "mysecret" not in result["landscape"]["url"]
        assert "url_password_fingerprint" not in result["landscape"]
        assert result["landscape"]["url_password_redacted"] is True

    def test_database_sink_url_password_fingerprinted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """database sink options.url password should be fingerprinted in audit copy."""
        from elspeth.core.config import _fingerprint_config_for_audit

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        config_dict = {
            "source": {"plugin": "csv", "options": {}},
            "sinks": {
                "warehouse": {
                    "plugin": "database",
                    "options": {"url": "postgresql://user:dbsecret@host/db"},  # secret-scan: allow-this-line
                }
            },
        }

        result = _fingerprint_config_for_audit(config_dict)
        options = result["sinks"]["warehouse"]["options"]

        assert "dbsecret" not in options["url"]
        assert "user@host" in options["url"]
        assert "url_password_fingerprint" in options
        assert len(options["url_password_fingerprint"]) == 64

    def test_database_sink_url_password_redacted_in_dev_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """database sink options.url password should be redacted in dev mode."""
        from elspeth.core.config import _fingerprint_config_for_audit

        monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY", raising=False)
        monkeypatch.setenv("ELSPETH_ALLOW_RAW_SECRETS", "true")

        config_dict = {
            "source": {"plugin": "csv", "options": {}},
            "sinks": {
                "warehouse": {
                    "plugin": "database",
                    "options": {"url": "postgresql://user:dbsecret@host/db"},  # secret-scan: allow-this-line
                }
            },
        }

        result = _fingerprint_config_for_audit(config_dict)
        options = result["sinks"]["warehouse"]["options"]

        assert "dbsecret" not in options["url"]
        assert "url_password_fingerprint" not in options
        assert options["url_password_redacted"] is True

    def test_resolve_config_database_sink_url_password_fingerprinted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """resolve_config must not return plaintext database sink DSN passwords."""
        from elspeth.core.config import ElspethSettings, resolve_config

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "warehouse"}},
            sinks={
                "warehouse": {
                    "plugin": "database",
                    "on_write_failure": "discard",
                    "options": {"url": "postgresql://user:dbsecret@host/db"},  # secret-scan: allow-this-line
                }
            },
        )

        resolved = resolve_config(settings)
        options = resolved["sinks"]["warehouse"]["options"]

        assert "dbsecret" not in options["url"]
        assert "url_password_fingerprint" in options
        assert len(options["url_password_fingerprint"]) == 64

    def test_dsn_unparsable_with_credentials_raises(self) -> None:
        """Unparsable URLs with credential patterns must not pass through."""
        from elspeth.core.config import SecretFingerprintError, _sanitize_dsn

        # JDBC-style URL — make_url() raises ArgumentError, but password is present
        url = "jdbc:postgresql://user:secret@host/db"  # secret-scan: allow-this-line

        with pytest.raises(SecretFingerprintError, match="credentials"):
            _sanitize_dsn(url)

    def test_dsn_unparsable_no_scheme_with_credentials_raises(self) -> None:
        """URLs without scheme but with credentials must not pass through."""
        from elspeth.core.config import SecretFingerprintError, _sanitize_dsn

        url = "://user:secret@host/db"

        with pytest.raises(SecretFingerprintError, match="credentials"):
            _sanitize_dsn(url)

    def test_dsn_unparsable_without_credentials_passes_through(self) -> None:
        """Unparsable URLs without credential patterns should pass through."""
        from elspeth.core.config import _sanitize_dsn

        # Not a SQLAlchemy URL and no credential pattern
        url = "not-a-url-at-all"
        sanitized, fingerprint, had_password = _sanitize_dsn(url)

        assert sanitized == url
        assert fingerprint is None
        assert had_password is False

    def test_dsn_query_param_password_scrubbed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Query-param ?password=secret must be scrubbed from sanitized URL.

        Regression test for elspeth-ef062449ca: _sanitize_dsn() only checked
        parsed.password (userinfo), missing credentials in query parameters.
        """
        from elspeth.core.config import _sanitize_dsn

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        url = "postgresql://user@host/db?password=secret&sslmode=require"
        sanitized, _fingerprint, had_password = _sanitize_dsn(url)

        assert "secret" not in sanitized
        assert "sslmode=require" in sanitized
        assert had_password is True

    def test_dsn_query_param_pwd_scrubbed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Query-param ?pwd=secret must also be scrubbed."""
        from elspeth.core.config import _sanitize_dsn

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        url = "postgresql://user@host/db?pwd=secret&sslmode=require"
        sanitized, _fingerprint, had_password = _sanitize_dsn(url)

        assert "secret" not in sanitized
        assert had_password is True

    def test_dsn_odbc_connect_pwd_scrubbed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ODBC connect string PWD= inside ?odbc_connect= must be scrubbed."""
        from elspeth.core.config import _sanitize_dsn

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        url = "mssql+pyodbc:///?odbc_connect=DRIVER%3D%7BSQL+Server%7D%3BPWD%3Dsecret123"
        sanitized, _fingerprint, had_password = _sanitize_dsn(url)

        assert "secret123" not in sanitized
        assert had_password is True

    def test_dsn_odbc_connect_sanitized_is_faithful_not_double_encoded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """elspeth-f9b8ed91a9: scrubbing odbc_connect must remove the password WITHOUT
        double-encoding the non-secret connection material. The sanitized audit URL
        must re-parse to the original connection string minus the password."""
        from sqlalchemy.engine import make_url

        from elspeth.core.config import _sanitize_dsn

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        url = "mssql+pyodbc:///?odbc_connect=DRIVER%3D%7BSQL+Server%7D%3BPWD%3Dsecret123%3BServer%3Dhost"
        sanitized, _fingerprint, had_password = _sanitize_dsn(url)

        assert had_password is True
        assert "secret123" not in sanitized
        # No double-encoding: the encoded form of '%3D'/'%20' (i.e. %253D/%2520) must not appear.
        assert "%253D" not in sanitized
        assert "%2520" not in sanitized
        # Faithful: re-parsing the sanitized URL yields the scrubbed connection string.
        reparsed = make_url(sanitized).query["odbc_connect"]
        assert reparsed == "DRIVER={SQL Server};Server=host"

    def test_dsn_repeated_odbc_connect_pwd_scrubbed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Repeated odbc_connect params parse as a tuple and must all be scrubbed."""
        from sqlalchemy.engine import make_url

        from elspeth.core.config import _sanitize_dsn

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        url = "mssql+pyodbc:///?odbc_connect=DRIVER%3D%7BODBC+Driver+17%7D%3BSERVER%3Ddb&odbc_connect=PWD%3DTupleSecret123%3BUID%3Dsa"
        sanitized, fingerprint, had_password = _sanitize_dsn(url)

        assert had_password is True
        assert fingerprint is not None
        assert "TupleSecret123" not in sanitized
        reparsed = make_url(sanitized).query["odbc_connect"]
        assert reparsed == ("DRIVER={ODBC Driver 17};SERVER=db", "UID=sa")

    def test_dsn_repeated_odbc_connect_password_braced_value_scrubbed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every repeated odbc_connect value uses the ODBC password parser."""
        from sqlalchemy.engine import make_url

        from elspeth.core.config import _sanitize_dsn

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        url = (
            "mssql+pyodbc:///?"
            "odbc_connect=DRIVER%3D%7BODBC+Driver+17%7D%3BSERVER%3Ddb"
            "&odbc_connect=Password%3D%7Bsemi%3Bsecret%7D%3BAPP%3Dreporting"
        )
        sanitized, _fingerprint, had_password = _sanitize_dsn(url)

        assert had_password is True
        assert "semi" not in sanitized
        assert "secret" not in sanitized
        reparsed = make_url(sanitized).query["odbc_connect"]
        assert reparsed == ("DRIVER={ODBC Driver 17};SERVER=db", "APP=reporting")

    def test_dsn_repeated_odbc_connect_scrubs_secret_from_each_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No repeated odbc_connect value may keep an embedded password."""
        from sqlalchemy.engine import make_url

        from elspeth.core.config import _sanitize_dsn

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        url = "mssql+pyodbc:///?odbc_connect=PWD%3Dfirst-secret%3BUID%3Dsa&odbc_connect=Password%3Dsecond-secret%3BAPP%3Dreporting"
        sanitized, _fingerprint, had_password = _sanitize_dsn(url)

        assert had_password is True
        assert "first-secret" not in sanitized
        assert "second-secret" not in sanitized
        reparsed = make_url(sanitized).query["odbc_connect"]
        assert reparsed == ("UID=sa", "APP=reporting")

    def test_dsn_query_param_password_no_userinfo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Query-param password without userinfo password still detected."""
        from elspeth.core.config import _sanitize_dsn

        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        # No userinfo password — only query param
        url = "postgresql://host/db?password=secret"
        sanitized, fingerprint, had_password = _sanitize_dsn(url)

        assert "secret" not in sanitized
        assert had_password is True
        assert fingerprint is not None


class TestAuditRedactionSectionCoverage:
    """Reflection pin: EVERY free-form mapping in the settings tree is redacted.

    A hand-listed test reproduces the exact defect it is meant to catch the
    next time a section is added. That is not hypothetical: ``collectors``
    was added to ``ElspethSettings`` and nothing told
    ``_fingerprint_config_for_audit`` about it, so collector plugin options
    reached the durable audit record in cleartext (elspeth-bc1b2c2959).

    So the set is DISCOVERED from ``ElspethSettings.model_fields`` rather than
    written down, and the assertion is BEHAVIOURAL: a sentinel secret is
    planted under each discovered mapping and must not survive the redactor.
    An inventory assertion — "the discovered set equals this literal set" —
    would have passed with the defect present, because it checks the section
    list rather than the redaction.

    DISCOVERY IS BY TYPE, NOT BY NAME. This pin originally keyed on
    ``"options" in element_model.model_fields``, which quietly encoded a second
    hand-maintained fact: that free-form plugin config is always *called*
    ``options``. That was true when written and already false —
    ``collection_probes[*].provider_config`` is the same arbitrary
    secret-bearing mapping under a different name, so the pin could not see the
    field it most needed to (elspeth-fb8492c07b). The predicate now tests the
    ANNOTATION (``dict[str, Any]`` / ``Mapping[str, Any]``), which no naming
    convention can drift away from.

    If a newly discovered mapping is genuinely not secret-bearing, record that
    disposition here — do NOT narrow the predicate back toward a hand-shaped
    scope, which is the defect this pin exists to prevent.

    The walk is recursive because ``telemetry.exporters[*].options`` is not a
    top-level section; a top-level-only walk would miss exactly the shape that
    is missed next time.
    """

    # Not a credential: a marker string asserted absent from redactor output.
    SENTINEL = "sentinel-value-must-not-survive-redaction"  # secret-scan: allow-this-line

    # Depth cap: ElspethSettings nests three levels at most. The cap is a
    # runaway guard for a future self-referential model, not a scoping choice.
    MAX_DEPTH = 6

    @staticmethod
    def _classify(annotation: object) -> tuple[str, tuple[type[BaseModel], ...]]:
        """Return the container shape and element models of one field annotation.

        Shapes: ``"dict"`` (name -> entry), ``"list"`` (sequence of entries),
        ``"single"`` (a nested model), ``""`` (carries no model).
        Optionals are unwrapped; ``None`` is not a container.
        """
        candidates = [annotation]
        if get_origin(annotation) in (Union, UnionType):
            candidates = [arg for arg in get_args(annotation) if arg is not type(None)]

        for candidate in candidates:
            origin = get_origin(candidate)
            args = get_args(candidate)
            if origin is dict and len(args) == 2:
                models = tuple(a for a in args[1:] if isinstance(a, type) and issubclass(a, BaseModel))
                if models:
                    return "dict", models
            elif origin in (list, tuple, Sequence) and args:
                models = tuple(a for a in args[:1] if isinstance(a, type) and issubclass(a, BaseModel))
                if models:
                    return "list", models
            elif isinstance(candidate, type) and issubclass(candidate, BaseModel):
                return "single", (candidate,)
        return "", ()

    @staticmethod
    def _is_free_form_mapping(annotation: object) -> bool:
        """Is this annotation an arbitrary-key config mapping?

        ``dict[str, Any]`` and ``Mapping[str, Any]``, Optional-unwrapped. Both
        forms are in use — plugin ``options`` is the former, ``provider_config``
        the latter — and ``get_origin(Mapping[str, Any])`` is
        ``collections.abc.Mapping``, not ``dict``. A predicate checking only
        ``dict`` discovers the six fields that were already covered and misses
        the one this pin was widened for, while every assertion still passes.

        The value type must be ``Any``: a mapping with a concrete value type is
        a declared structure, not free-form operator-supplied config.
        """
        candidates = [annotation]
        if get_origin(annotation) in (Union, UnionType):
            candidates = [arg for arg in get_args(annotation) if arg is not type(None)]

        for candidate in candidates:
            args = get_args(candidate)
            if get_origin(candidate) in (dict, Mapping) and len(args) == 2 and args[0] is str and args[1] is Any:
                return True
        return False

    @classmethod
    def _free_form_mapping_paths(cls) -> list[tuple[tuple[str, str], ...]]:
        """Discover every path in ElspethSettings reaching a free-form mapping.

        Each path is a tuple of ``(field_name, container_shape)`` segments whose
        LAST segment is the mapping field itself, shape ``"mapping"``, e.g.
        ``(("transforms", "list"), ("options", "mapping"))`` or
        ``(("collection_probes", "list"), ("provider_config", "mapping"))``.
        Carrying the field name in the path is what lets this pin cover a
        mapping that is not called ``options``.

        Deduplication is on the PATH, not on the model class: if two sections
        ever share an element model, dropping the second by class would
        silently remove it from this pin's coverage.
        """
        from elspeth.core.config import ElspethSettings

        found: list[tuple[tuple[str, str], ...]] = []
        seen_paths: set[tuple[tuple[str, str], ...]] = set()

        def walk(model: type[BaseModel], prefix: tuple[tuple[str, str], ...]) -> None:
            if len(prefix) >= cls.MAX_DEPTH:
                return
            for name in model.model_fields:
                annotation = model.model_fields[name].annotation
                if cls._is_free_form_mapping(annotation):
                    found.append((*prefix, (name, "mapping")))
                shape, element_models = cls._classify(annotation)
                if not element_models:
                    continue
                path = (*prefix, (name, shape))
                if path in seen_paths:
                    continue
                seen_paths.add(path)
                for element_model in element_models:
                    walk(element_model, path)

        walk(ElspethSettings, ())
        return found

    @classmethod
    def _plant(cls, path: tuple[tuple[str, str], ...]) -> dict[str, object]:
        """Build the minimal config dict that puts SENTINEL under ``path``.

        ``_fingerprint_config_for_audit`` takes a plain dict and reads it
        structurally, so this deliberately does NOT construct real settings
        models: doing so would drag in every required field and the
        scopes/collectors cross-validator without making the pin stronger.
        """
        mapping_name, mapping_shape = path[-1]
        assert mapping_shape == "mapping", f"path must end at the mapping field, got {path!r}"

        payload: object = {mapping_name: {"api_key": cls.SENTINEL}}
        for name, shape in reversed(path[:-1]):
            if shape == "dict":
                payload = {name: {"entry": payload}}
            elif shape == "list":
                payload = {name: [payload]}
            else:
                payload = {name: payload}
        assert isinstance(payload, dict)
        return payload

    def test_walk_finds_the_known_free_form_mappings(self) -> None:
        """Guard the probe itself: a broken walk must not report vacuous success.

        This is the positive control. If the discovery walk silently returned
        nothing — a changed annotation style, a pydantic upgrade — the
        behavioural tests below would pass by testing nothing at all.

        Asserted as EQUALITY, not containment. Containment cannot tell a
        widened predicate from a broken one, and the whole point of moving off
        the field name was to catch the mapping nobody thought to list. A new
        entry here is a real event: adjudicate whether it is secret-bearing and
        record the disposition in the class docstring.
        """
        paths = self._free_form_mapping_paths()
        rendered = {".".join(name for name, _shape in path) for path in paths}

        assert rendered == {
            "sources.options",
            "sinks.options",
            "transforms.options",
            "collectors.options",
            "aggregations.options",
            "telemetry.exporters.options",
            "collection_probes.provider_config",
        }, f"Free-form mappings in ElspethSettings changed; found {sorted(rendered)}"

    def test_discovery_is_not_keyed_on_the_field_name_options(self) -> None:
        """Mutation guard: the pin must still see a mapping that is not called ``options``.

        Without this, someone restoring the name-keyed predicate gets a green
        suite: the other tests iterate whatever the walk returns, so a walk that
        silently stops returning ``provider_config`` simply stops testing it.
        """
        rendered = {".".join(name for name, _shape in path) for path in self._free_form_mapping_paths()}
        not_named_options = {path for path in rendered if not path.endswith(".options")}

        assert not_named_options, (
            "Discovery found only fields named 'options'. The predicate has regressed to keying "
            "on the field NAME, so a free-form mapping under any other name is invisible to this pin "
            "(elspeth-fb8492c07b)."
        )

    def test_every_free_form_mapping_is_redacted_for_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No discovered mapping may carry a plaintext secret into the audit copy.

        Fingerprint mode: the secret must be replaced by a fingerprint.
        """
        from elspeth.core.config import _fingerprint_config_for_audit

        monkeypatch.delenv("ELSPETH_ALLOW_RAW_SECRETS", raising=False)
        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key")

        leaked = []
        for path in self._free_form_mapping_paths():
            result = _fingerprint_config_for_audit(self._plant(path))
            if self.SENTINEL in repr(result):
                leaked.append(".".join(name for name, _shape in path))

        assert not leaked, (
            f"Free-form mappings written to the audit record UNREDACTED: {sorted(leaked)}. "
            f"Add each to _fingerprint_config_for_audit and to its docstring enumeration."
        )

    def test_every_free_form_mapping_fails_closed_without_a_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Absent a fingerprint key, a secret must abort rather than persist.

        This is the asymmetry that made elspeth-bc1b2c2959 silent, and made
        elspeth-fb8492c07b silent again a section later: a covered field
        refuses to produce an audit copy at all, while the uncovered one
        returns successfully with the secret intact. Nothing in the run
        distinguishes "no secrets present" from "secrets present, not
        redacted" — which is why coverage is asserted here rather than trusted.
        """
        from elspeth.contracts.security import SecretFingerprintError
        from elspeth.core.config import _fingerprint_config_for_audit

        monkeypatch.delenv("ELSPETH_ALLOW_RAW_SECRETS", raising=False)
        monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY", raising=False)

        failed_open = []
        for path in self._free_form_mapping_paths():
            try:
                _fingerprint_config_for_audit(self._plant(path))
            except SecretFingerprintError:
                continue
            failed_open.append(".".join(name for name, _shape in path))

        assert not failed_open, (
            f"Mappings that FAIL OPEN on a secret with no fingerprint key: {sorted(failed_open)}. "
            f"Every free-form mapping must reach _fingerprint_secrets."
        )


class TestEnvPlaceholderGuardIsDerived:
    """The pre-expansion guard must enforce every plugin-side declaration.

    ``_reject_sensitive_plugin_env_placeholders_before_expansion`` is the only
    containment for an option whose VALUE is written into row data or artifact
    bytes. The plugin's own validator cannot be that containment on the
    CLI/YAML path: ``_expand_env_vars`` runs first, so the validator is handed a
    clean expanded host value that no longer matches ``${...}``.

    It used to hold a hand-maintained ``{plugin: {field, ...}}`` map with ONE
    entry, making it a no-op for every other plugin — ``truncate.suffix`` and
    ``csv.headers`` both reached artifact bytes through that gap, and
    ``sources``/``sinks`` were not walked at all (elspeth-8f0a6b3391). The map
    and the plugin-side validators agreed only by luck.

    Both halves now derive from existing declarations: the sections from
    ``ElspethSettings``; literal emitted values from ``EmittedToOutput``
    markers on the plugin config models; and emitted row-key names from
    ``BaseTransform.output_naming_config_keys``. These tests assert the
    DERIVATION holds end to end — every declaration in the tree is enforced by
    the loader — rather than checking a list of today's plugins, which is the
    shape that reproduced the defect each time it was added to.
    """

    PLACEHOLDER = "${ELSPETH_TEST_HOST_VALUE}"

    @staticmethod
    def _registered_plugins() -> list[tuple[str, str, Any]]:
        """Return ``(kind, name, plugin_class)`` for every registered plugin."""
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

        manager = get_shared_plugin_manager()
        return [
            (kind, plugin_class.name, plugin_class)
            for kind, plugin_classes in (
                ("source", manager.get_sources()),
                ("transform", manager.get_transforms()),
                ("sink", manager.get_sinks()),
            )
            for plugin_class in plugin_classes
        ]

    def test_bare_get_config_model_never_raises_for_any_registered_plugin(self) -> None:
        """Standing precondition of the union-across-registries lookup.

        The guard has no plugin-name-to-kind lookup — the registries are three
        disjoint maps and ``csv``, ``json``, ``aws_s3``, ``azure_blob``,
        ``dataverse``, ``text`` and ``llm`` each name a plugin in more than one.
        So it asks EVERY registry about a name, which means calling
        ``get_config_model()`` on plugins that are not the one in use.

        A plugin whose bare call raised would therefore break config load for a
        section that has nothing to do with it. Nothing raises today; this pin
        keeps that a property rather than a coincidence, because the plugin that
        breaks it will be added by someone who never read this ticket.
        """
        plugins = self._registered_plugins()

        assert len(plugins) > 20, (
            f"Positive control: registry discovery returned only {len(plugins)} plugins. "
            f"The registry is populated lazily, so an empty or partial walk would make every "
            f"assertion in this class pass while testing nothing."
        )

        raised = []
        for kind, name, plugin_class in plugins:
            try:
                plugin_class.get_config_model()
            except Exception as exc:  # reporting the failure IS the assertion
                raised.append(f"{kind}/{name}: {type(exc).__name__}: {exc}")

        assert not raised, (
            f"Plugins whose bare get_config_model() raises: {raised}. "
            f"The pre-expansion env guard calls this on every registry that knows a name, so one "
            f"raising plugin breaks config load for unrelated sections. Give it a bare-callable "
            f"config model; do not make the guard swallow the error."
        )

    def test_guard_walks_every_plugin_bearing_section(self) -> None:
        """Sections are discovered, and sources/sinks are no longer excluded.

        This assertion previously pinned a deliberate sources/sinks exclusion,
        justified on the registries being disjoint from the transform registry
        the forbidden map was keyed on. That justification was false —
        ``csv.headers`` writes its mapping values as the artifact's header row —
        and with the map gone there is no registry to be disjoint from.
        """
        from elspeth.config_loading import _plugin_bearing_sections

        sections = _plugin_bearing_sections()

        assert set(sections) == {"sources", "sinks", "transforms", "collectors", "aggregations"}, (
            f"Plugin-bearing sections changed: {sections}. The guard walks whatever this discovers, "
            f"so a new section is covered automatically — but confirm its entries really do carry "
            f"plugin options rather than merely resembling the shape."
        )
        assert sections["sources"] == "dict" and sections["sinks"] == "dict"
        assert {name for name, shape in sections.items() if shape == "list"} == {
            "transforms",
            "collectors",
            "aggregations",
        }

    def test_every_declared_emitted_option_is_rejected_by_the_loader(self) -> None:
        """The end-to-end derivation pin: declaration in, rejection out.

        Walks every registered plugin, takes whatever it declares
        ``EmittedToOutput``, plants a placeholder in that option and requires
        the loader to refuse it. Nothing here names a plugin or a field, so a
        newly marked option is covered the moment it is declared — and a
        declaration the guard cannot see fails loudly instead of silently
        protecting nothing.
        """
        from elspeth.config_loading import _reject_sensitive_plugin_env_placeholders_before_expansion
        from elspeth.contracts.emitted_option import emitted_option_fields

        declarations = [
            (kind, name, option)
            for kind, name, plugin_class in self._registered_plugins()
            for option in emitted_option_fields(plugin_class.get_config_model())
        ]

        assert declarations, (
            "Positive control: no plugin declares an EmittedToOutput option, so this test would pass without exercising the guard at all."
        )

        failed_open = []
        for kind, name, option in declarations:
            raw_config = {"transforms": [{"name": "probe", "plugin": name, "options": {option: self.PLACEHOLDER}}]}
            try:
                _reject_sensitive_plugin_env_placeholders_before_expansion(raw_config)
            except ValueError:
                continue
            failed_open.append(f"{kind}/{name}.{option}")

        assert not failed_open, (
            f"Options declared EmittedToOutput that the loader guard does NOT reject: {sorted(failed_open)}. "
            f"The declaration and the guard have come apart, which is the exact failure the derivation "
            f"exists to make impossible."
        )

    @pytest.mark.parametrize(
        ("section_name", "plugin_name", "option_name"),
        [
            ("transforms", "rag_retrieval", "output_prefix"),
            ("transforms", "rag_retrieval", "context_separator"),
            ("transforms", "batch_classifier_metrics", "actual_field"),
            ("transforms", "batch_classifier_metrics", "predicted_field"),
            ("transforms", "batch_outlier_annotator", "value_field"),
            ("transforms", "batch_outlier_annotator", "z_threshold"),
            ("transforms", "batch_outlier_annotator", "robust_z_threshold"),
            ("transforms", "pdf_rasterize", "page_blob_ref_field"),
            ("transforms", "pdf_rasterize", "page_number_field"),
            ("transforms", "pdf_rasterize", "document_id_field"),
            ("transforms", "pdf_rasterize", "page_mime_type_field"),
            ("transforms", "pdf_rasterize", "page_size_bytes_field"),
            ("transforms", "pdf_rasterize", "page_width_field"),
            ("transforms", "pdf_rasterize", "page_height_field"),
            ("transforms", "web_scrape", "content_field"),
            ("transforms", "web_scrape", "fingerprint_field"),
            ("transforms", "report_assemble", "output_field"),
            ("transforms", "blob_csv_expand", "columns"),
            ("transforms", "blob_csv_expand", "field_mapping"),
            ("transforms", "blob_json_expand", "field_mapping"),
            ("transforms", "field_mapper", "mapping"),
            ("sources", "csv", "columns"),
            ("sources", "csv", "field_mapping"),
            ("sources", "aws_s3", "columns"),
            ("sources", "aws_s3", "field_mapping"),
            ("sources", "azure_blob", "columns"),
            ("sources", "azure_blob", "field_mapping"),
            ("sources", "json", "field_mapping"),
            ("sources", "dataverse", "field_mapping"),
            ("sinks", "dataverse", "field_mapping"),
            ("sources", "text", "column"),
            ("sources", "llm", "response_field"),
            ("sources", "blob_rows", "blobs"),
        ],
    )
    def test_adjudicated_output_emitters_are_not_silently_undeclared(
        self,
        section_name: str,
        plugin_name: str,
        option_name: str,
    ) -> None:
        """Named pin for emitters the registry-derived sweep cannot discover.

        ``test_every_declared_emitted_option_is_rejected_by_the_loader`` proves
        that declarations are enforced, but deleting or forgetting a declaration
        removes the case from that derived test. Each source/transform option
        here has an execution path that writes its value into row data: directly
        as a separator or metadata value, as a numeric value after config
        coercion, or as a field name that becomes a row key and artifact column
        after env expansion.

        Dataverse deliberately pins one cross-kind consequence: its source
        ``field_mapping`` declaration also protects the same-named sink option
        because declarations are unioned by plugin name. Only sink ``lookups``
        and the broader policy for outbound OData identity remain deferred.
        """
        from elspeth.config_loading import _reject_sensitive_plugin_env_placeholders_before_expansion

        option_value: object
        if option_name == "columns":
            option_value = [self.PLACEHOLDER]
        elif option_name in {"field_mapping", "mapping"}:
            option_value = {"observed": self.PLACEHOLDER}
        elif option_name == "blobs":
            option_value = [
                {
                    "blob_id": "11111111-1111-1111-1111-111111111111",
                    "payload_ref": "a" * 64,
                    "filename": self.PLACEHOLDER,
                    "mime_type": "application/pdf",
                    "size_bytes": 1,
                }
            ]
        else:
            option_value = self.PLACEHOLDER

        entry = {
            "name": "probe",
            "plugin": plugin_name,
            "options": {option_name: option_value},
        }
        raw_config = {section_name: {"probe": entry} if section_name in {"sources", "sinks"} else [entry]}

        with pytest.raises(ValueError, match="environment-variable placeholders"):
            _reject_sensitive_plugin_env_placeholders_before_expansion(raw_config)

    def test_dataverse_sink_cross_kind_guard_accepts_a_clean_field_mapping(self) -> None:
        """The conservative cross-kind restriction targets placeholders, not mapping use.

        Dataverse sink mapping values name outbound OData fields rather than
        pipeline artifact columns. The same-named source declaration currently
        protects them through the deliberate plugin-name union. This positive
        control keeps that fail-closed consequence narrow: clean sink mappings
        remain valid, while sink ``lookups`` and broader outbound-identity policy
        are explicitly outside this bounded repair.
        """
        from elspeth.config_loading import _reject_sensitive_plugin_env_placeholders_before_expansion

        raw_config = {
            "sinks": {
                "probe": {
                    "name": "probe",
                    "plugin": "dataverse",
                    "options": {"field_mapping": {"observed": "destination"}},
                }
            }
        }

        _reject_sensitive_plugin_env_placeholders_before_expansion(raw_config)

    def test_every_output_naming_option_is_rejected_by_the_loader(self) -> None:
        """Reuse the transform registry's truth-tested output-name authority.

        ``BaseTransform.output_naming_config_keys`` already classifies options
        whose value names a field the transform writes. The transform invariant
        suite proves those declarations against the fields actually created, so
        the env guard must derive from them rather than restating the same field
        list as ``EmittedToOutput`` annotations on every config model.
        """
        from elspeth.config_loading import _reject_sensitive_plugin_env_placeholders_before_expansion
        from elspeth.plugins.infrastructure.base import BaseTransform
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

        manager = get_shared_plugin_manager()
        declarations: list[tuple[str, str, object]] = []
        for registered_class in manager.get_transforms():
            plugin_class = cast(type[BaseTransform], registered_class)
            config_model = plugin_class.get_config_model()
            assert config_model is not None, f"{plugin_class.name} declares output naming options without a config model"
            declarations.extend(
                (plugin_class.name, option_name, config_model.model_fields[option_name].annotation)
                for option_name in sorted(plugin_class.output_naming_config_keys)
            )

        assert len(declarations) > 20, (
            f"Positive control: only {len(declarations)} output-naming options were discovered; "
            "a partial registry walk would make the security sweep pass while testing little or nothing."
        )

        failed_open = []
        for plugin_name, option_name, annotation in declarations:
            value: object = [self.PLACEHOLDER] if get_origin(annotation) is list else self.PLACEHOLDER
            raw_config = {"transforms": [{"name": "probe", "plugin": plugin_name, "options": {option_name: value}}]}
            try:
                _reject_sensitive_plugin_env_placeholders_before_expansion(raw_config)
            except ValueError:
                continue
            failed_open.append(f"{plugin_name}.{option_name}")

        assert not failed_open, (
            f"Output-naming options that allow env expansion before plugin validation: {sorted(failed_open)}. "
            "Their expanded values can become valid output row keys and artifact headers."
        )

    def test_the_guard_is_not_a_one_plugin_map_in_disguise(self) -> None:
        """Mutation guard: enforcement must reach beyond the original entry.

        The retired map held ``report_assemble`` alone. A regression that
        rebuilds a narrow hand-map — or a derivation that silently resolves only
        the plugin it was first tested against — would still pass a test that
        checks ``report_assemble`` and nothing else.
        """
        from elspeth.config_loading import _declared_emitted_options

        enforced = {
            name
            for kind, name, _option in [
                (kind, name, option)
                for kind, name, plugin_class in self._registered_plugins()
                for option in _declared_emitted_options(name)
            ]
        }

        assert "report_assemble" in enforced, "Positive control: the original declarer is no longer enforced."
        assert enforced - {"report_assemble"}, (
            f"Only report_assemble is enforced ({enforced}). The guard has regressed to the single-entry "
            f"hand-map that let truncate.suffix and csv.headers reach artifact bytes."
        )

    def test_rejection_message_names_the_declaring_kind(self) -> None:
        """A union across registries can refuse a source option on a sink's declaration.

        The cost of having no name-to-kind lookup is a refusal that can look
        arbitrary. It must not also be unexplained: the message carries the
        declaring kind and the declared reason, so an operator can see why.
        """
        from elspeth.config_loading import _reject_sensitive_plugin_env_placeholders_before_expansion

        raw_config = {
            "sinks": {"out": {"plugin": "csv", "options": {"headers": {"body": self.PLACEHOLDER}}}},
        }

        with pytest.raises(ValueError) as exc_info:
            _reject_sensitive_plugin_env_placeholders_before_expansion(raw_config)

        message = str(exc_info.value)
        assert "as a sink plugin" in message, f"Message does not name the declaring kind: {message}"
        assert "header row" in message, f"Message does not carry the declared reason: {message}"
        assert "sinks['out']" in message and "'headers'" in message, message

    def test_message_explains_a_restriction_imported_from_a_sibling_kind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The union's whole cost is a refusal that can look arbitrary.

        With no plugin-name-to-kind lookup, a name registered as both a source
        and a sink is refused on either one's declaration. Today no dual-kind
        name declares the same emitted option in two kinds, so this branch has
        no natural fixture — and an untested branch inside a security control is
        exactly what "declaration tests pin existence, not truth" warns about.
        So the two-kind declaration is injected rather than left unproven.
        """
        from elspeth import config_loading as config_module

        def _two_kinds(plugin_name: str) -> dict[str, list[tuple[str, str]]]:
            return {
                "shared_option": [
                    ("source", "the source writes this into every emitted row"),
                    ("sink", "the sink writes this into the artifact header"),
                ]
            }

        monkeypatch.setattr(config_module, "_declared_emitted_options", _two_kinds)

        with pytest.raises(ValueError) as exc_info:
            config_module._reject_sensitive_plugin_env_placeholders_before_expansion(
                {"sinks": {"out": {"plugin": "csv", "options": {"shared_option": self.PLACEHOLDER}}}}
            )

        message = str(exc_info.value)
        assert "registered as more than one plugin kind" in message, (
            f"An imported restriction must say so; an unexplained refusal is a support incident: {message}"
        )
        assert "sink, source" in message, f"Message does not list the declaring kinds: {message}"
        assert "emitted row" in message and "artifact header" in message, (
            f"Message must carry BOTH declared reasons so the operator can see which one applies: {message}"
        )

    def test_mapping_valued_options_are_scanned_not_just_strings(self) -> None:
        """``csv.headers`` leaks through the MAPPING form, not the string form.

        A top-level ``isinstance(value, str)`` check — what the guard did before
        — walks straight past ``{field: display_name}``, which is exactly the
        shape whose values are written as the artifact's header row.
        """
        from elspeth.config_loading import _reject_sensitive_plugin_env_placeholders_before_expansion

        for label, headers in (
            ("mapping value", {"body": self.PLACEHOLDER}),
            ("mapping key", {self.PLACEHOLDER: "Body"}),
            ("bare string", self.PLACEHOLDER),
        ):
            try:
                _reject_sensitive_plugin_env_placeholders_before_expansion(
                    {"sinks": {"out": {"plugin": "csv", "options": {"headers": headers}}}}
                )
            except ValueError:
                continue
            raise AssertionError(
                f"The guard walked past a placeholder in the {label} of csv.headers. "
                f"A top-level isinstance(value, str) check misses the mapping form, which is "
                f"the shape whose values are written as the artifact's header row."
            )

    def test_clean_values_are_still_accepted(self) -> None:
        """Negative control: the guard must reject placeholders, not the option."""
        from elspeth.config_loading import _reject_sensitive_plugin_env_placeholders_before_expansion

        _reject_sensitive_plugin_env_placeholders_before_expansion(
            {
                "sinks": {"out": {"plugin": "csv", "options": {"headers": {"body": "Body"}}}},
                "transforms": [
                    {"name": "t", "plugin": "truncate", "options": {"suffix": "..."}},
                    {
                        "name": "report",
                        "plugin": "report_assemble",
                        "options": {"output_field": "report_body"},
                    },
                ],
            }
        )

    def test_unregistered_plugin_names_are_left_to_the_plugin_factory(self) -> None:
        """An unknown plugin is a different error, reported with better context.

        The guard must not turn "no such plugin" into its own message, and must
        not crash on the lookup either.
        """
        from elspeth.config_loading import _reject_sensitive_plugin_env_placeholders_before_expansion

        _reject_sensitive_plugin_env_placeholders_before_expansion(
            {"transforms": [{"name": "t", "plugin": "no_such_plugin", "options": {"title": self.PLACEHOLDER}}]}
        )


class TestTemplateFilePathTraversal:
    """Tests that template file loading blocks path traversal."""

    def test_template_file_traversal_blocked(self, tmp_path: Path) -> None:
        """template_file with .. escaping config directory is rejected."""
        from elspeth.core.config import TemplateFileError, _expand_template_files

        settings_path = tmp_path / "project" / "settings.yaml"
        settings_path.parent.mkdir(parents=True)
        settings_path.touch()

        # Create a file outside the config directory
        outside_file = tmp_path / "secret.txt"
        outside_file.write_text("secret data")

        config = {"template_file": "../secret.txt"}

        with pytest.raises(TemplateFileError, match="path traversal blocked"):
            _expand_template_files(config, settings_path)

    def test_lookup_file_traversal_blocked(self, tmp_path: Path) -> None:
        """lookup_file with .. escaping config directory is rejected."""
        from elspeth.core.config import TemplateFileError, _expand_template_files

        settings_path = tmp_path / "project" / "settings.yaml"
        settings_path.parent.mkdir(parents=True)
        settings_path.touch()

        outside_file = tmp_path / "secret.yaml"
        outside_file.write_text("key: value")

        config = {"lookup_file": "../secret.yaml"}

        with pytest.raises(TemplateFileError, match="path traversal blocked"):
            _expand_template_files(config, settings_path)

    def test_system_prompt_file_traversal_blocked(self, tmp_path: Path) -> None:
        """system_prompt_file with .. escaping config directory is rejected."""
        from elspeth.core.config import TemplateFileError, _expand_template_files

        settings_path = tmp_path / "project" / "settings.yaml"
        settings_path.parent.mkdir(parents=True)
        settings_path.touch()

        outside_file = tmp_path / "secret.txt"
        outside_file.write_text("stolen prompt")

        config = {"system_prompt_file": "../secret.txt"}

        with pytest.raises(TemplateFileError, match="path traversal blocked"):
            _expand_template_files(config, settings_path)

    def test_absolute_path_outside_config_dir_blocked(self, tmp_path: Path) -> None:
        """Absolute path pointing outside config directory is rejected."""
        from elspeth.core.config import TemplateFileError, _expand_template_files

        settings_path = tmp_path / "project" / "settings.yaml"
        settings_path.parent.mkdir(parents=True)
        settings_path.touch()

        outside_file = tmp_path / "secret.txt"
        outside_file.write_text("secret data")

        config = {"template_file": str(outside_file)}

        with pytest.raises(TemplateFileError, match="path traversal blocked"):
            _expand_template_files(config, settings_path)

    def test_subdirectory_paths_allowed(self, tmp_path: Path) -> None:
        """Paths within config directory (including subdirs) are allowed."""
        from elspeth.core.config import _expand_template_files

        settings_path = tmp_path / "settings.yaml"
        settings_path.touch()

        template_file = tmp_path / "prompts" / "deep" / "nested" / "test.j2"
        template_file.parent.mkdir(parents=True)
        template_file.write_text("Hello {{ row.name }}")

        config = {"template_file": "prompts/deep/nested/test.j2"}

        expanded = _expand_template_files(config, settings_path)
        assert expanded["prompt_template"] == "Hello {{ row.name }}"


class TestBoundedYamlStringLoader:
    """In-memory web-facing YAML loader hardening."""

    def test_load_settings_from_yaml_string_accepts_inline_blob_aggregate_sized_content(self) -> None:
        """Resolved inline content up to the blob aggregate cap fits under the YAML cap."""
        from elspeth.config_loading import load_settings_from_yaml_string
        from elspeth.core.blobs_inline import BLOB_INLINE_AGGREGATE_BYTE_CAP

        inline_content = "x" * BLOB_INLINE_AGGREGATE_BYTE_CAP
        settings = load_settings_from_yaml_string(
            f"""
sources:
  primary:
    plugin: csv_local
    on_success: output
    options:
      inline_content: {inline_content}
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options: {{}}
"""
        )

        assert settings.sources["primary"].options["inline_content"] == inline_content

    def test_load_settings_from_config_dict_accepts_content_that_would_dump_over_yaml_cap(self) -> None:
        """Resolved inline content is not serialized back through the YAML byte cap."""
        import yaml

        from elspeth.config_loading import load_settings_from_config_dict
        from elspeth.core.blobs_inline import BLOB_INLINE_AGGREGATE_BYTE_CAP
        from elspeth.core.config import MAX_IN_MEMORY_PIPELINE_YAML_BYTES

        inline_content = "\x00" * BLOB_INLINE_AGGREGATE_BYTE_CAP
        config_dict = {
            "sources": {
                "primary": {
                    "plugin": "csv_local",
                    "on_success": "output",
                    "options": {"inline_content": inline_content},
                }
            },
            "sinks": {
                "output": {
                    "plugin": "json",
                    "on_write_failure": "discard",
                    "options": {},
                }
            },
        }
        assert len(yaml.dump(config_dict, default_flow_style=False).encode("utf-8")) > MAX_IN_MEMORY_PIPELINE_YAML_BYTES

        settings = load_settings_from_config_dict(config_dict)

        assert settings.sources["primary"].options["inline_content"] == inline_content

    def test_load_settings_from_yaml_string_rejects_oversized_yaml_before_parse(self) -> None:
        """Web-facing YAML must be byte-capped before PyYAML parses it."""
        from elspeth.config_loading import load_settings_from_yaml_string
        from elspeth.core.config import MAX_IN_MEMORY_PIPELINE_YAML_BYTES

        oversized = (
            """
sources:
  primary:
    plugin: csv_local
    on_success: output
    options: {}
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options: {}
"""
            + "\n#"
            + ("x" * MAX_IN_MEMORY_PIPELINE_YAML_BYTES)
        )

        with pytest.raises(ValueError, match="byte YAML limit"):
            load_settings_from_yaml_string(oversized)

    def test_load_settings_from_yaml_string_rejects_aliases_before_construction(self) -> None:
        """Aliases are rejected so small YAML cannot amplify during object construction."""
        from elspeth.config_loading import load_settings_from_yaml_string

        aliased = """
sources:
  primary:
    plugin: csv_local
    on_success: output
    options:
      shared: &shared
        value: one
      copied: *shared
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options: {}
"""

        with pytest.raises(ValueError, match=r"^YAML parse failed: \w+Error$"):
            load_settings_from_yaml_string(aliased)

    def test_load_settings_from_yaml_string_rejects_excessive_depth(self) -> None:
        """Deep but textually small YAML is rejected before recursive construction."""
        from elspeth.config_loading import load_settings_from_yaml_string

        lines = [
            "sources:",
            "  primary:",
            "    plugin: csv_local",
            "    on_success: output",
            "    options:",
            "      nested:",
        ]
        for depth in range(70):
            lines.append(f"{'      ' + ('  ' * (depth + 1))}level_{depth}:")
        lines.append(f"{'      ' + ('  ' * 71)}value: done")
        lines.extend(
            [
                "sinks:",
                "  output:",
                "    plugin: json",
                "    on_write_failure: discard",
                "    options: {}",
            ]
        )

        with pytest.raises(ValueError, match=r"^YAML parse failed: \w+Error$"):
            load_settings_from_yaml_string("\n".join(lines))

    def test_load_settings_from_yaml_string_rejects_excessive_node_count(self) -> None:
        """Many small YAML nodes are rejected even when the byte cap is not reached."""
        from elspeth.config_loading import load_settings_from_yaml_string
        from elspeth.core.config import MAX_IN_MEMORY_PIPELINE_YAML_BYTES

        option_lines = [f"      key_{index}: value" for index in range(5_100)]
        yaml_content = "\n".join(
            [
                "sources:",
                "  primary:",
                "    plugin: csv_local",
                "    on_success: output",
                "    options:",
                *option_lines,
                "sinks:",
                "  output:",
                "    plugin: json",
                "    on_write_failure: discard",
                "    options: {}",
            ]
        )
        assert len(yaml_content.encode("utf-8")) < MAX_IN_MEMORY_PIPELINE_YAML_BYTES

        with pytest.raises(ValueError, match=r"^YAML parse failed: \w+Error$"):
            load_settings_from_yaml_string(yaml_content)


class TestExpandEnvValueBoundary:
    """Direct raising characterization for the ``_expand_env_value`` trust boundary."""

    def test_missing_env_var_without_default_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A ${VAR} reference to an unset variable with no default is refused."""
        from elspeth.core.config import _expand_env_value

        monkeypatch.delenv("ELSPETH_TEST_UNSET_ENV_VAR", raising=False)
        with pytest.raises(ValueError, match="Required environment variable 'ELSPETH_TEST_UNSET_ENV_VAR' is not set"):
            _expand_env_value({"api": ["${ELSPETH_TEST_UNSET_ENV_VAR}"]})

    def test_default_and_passthrough_shapes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Defaults expand; unrecognized scalar shapes pass through unchanged."""
        from elspeth.core.config import _expand_env_value

        monkeypatch.delenv("ELSPETH_TEST_UNSET_ENV_VAR", raising=False)
        assert _expand_env_value("${ELSPETH_TEST_UNSET_ENV_VAR:-fallback}") == "fallback"
        assert _expand_env_value(7) == 7
        assert _expand_env_value(None) is None


class TestFingerprintSecretsBoundary:
    """Direct raising characterization for the fingerprinting trust boundaries."""

    def test_secret_without_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A string secret field with no fingerprint key is refused (fail-closed)."""
        from elspeth.core.config import SecretFingerprintError, _fingerprint_process_value

        monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY", raising=False)
        with pytest.raises(SecretFingerprintError, match="Secret field 'api_key' found"):
            _fingerprint_process_value("api_key", value="raw-secret-value", have_key=False, fail_if_no_key=True)

    def test_secret_without_key_dev_mode_passthrough(self) -> None:
        """Dev mode (fail_if_no_key=False) keeps the original value, declared not silent."""
        from elspeth.core.config import _fingerprint_process_value

        key, value, was_secret = _fingerprint_process_value("api_key", value="raw-secret-value", have_key=False, fail_if_no_key=False)
        assert (key, value, was_secret) == ("api_key", "raw-secret-value", False)

    def test_fingerprint_collision_raises(self) -> None:
        """A secret field plus its pre-supplied _fingerprint counterpart is refused."""
        from elspeth.core.config import SecretFingerprintError, _recurse

        d = {"api_key": "raw", "api_key_fingerprint": "attacker-supplied"}
        with pytest.raises(SecretFingerprintError, match="Config contains both 'api_key' and 'api_key_fingerprint'"):
            _recurse(d, have_key=True, fail_if_no_key=True)


class TestSanitizeDsnOptionBoundary:
    """Direct raising characterization for the ``_sanitize_dsn_option_for_audit`` boundary."""

    def test_dsn_password_without_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A DSN option carrying a password with no fingerprint key is refused (fail-closed)."""
        from elspeth.core.config import SecretFingerprintError, _sanitize_dsn_option_for_audit

        monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY", raising=False)
        with pytest.raises(SecretFingerprintError, match="contains a password"):
            _sanitize_dsn_option_for_audit(
                options={"url": "postgresql://user:secretpass@host/db"},  # secret-scan: allow-this-line
                option_name="url",
                fingerprint_name="url_password_fingerprint",
                redacted_name="url_password_redacted",
                fail_if_no_key=True,
            )

    def test_non_string_dsn_option_is_left_for_plugin_validation(self) -> None:
        """Absent or non-string DSN values are skipped, never coerced."""
        from elspeth.core.config import _sanitize_dsn_option_for_audit

        options: dict[str, Any] = {"url": 42}
        _sanitize_dsn_option_for_audit(
            options=options,
            option_name="url",
            fingerprint_name="url_password_fingerprint",
            redacted_name="url_password_redacted",
            fail_if_no_key=True,
        )
        assert options == {"url": 42}


class TestLoadSettingsFromYamlStringBoundary:
    """Direct raising characterization for the ``load_settings_from_yaml_string`` boundary."""

    def test_non_mapping_yaml_document_raises(self) -> None:
        """A YAML document that is not a mapping is refused, never coerced."""
        from elspeth.config_loading import load_settings_from_yaml_string

        with pytest.raises(ValueError, match="must be a YAML mapping"):
            load_settings_from_yaml_string("- just\n- a\n- list\n")
        with pytest.raises(ValueError, match="must be a YAML mapping"):
            load_settings_from_yaml_string("just a scalar")


class TestLoadSettingsYamlDocumentShape:
    """Non-mapping YAML documents are rejected — falsy ones included.

    Pins the elspeth-ca0a7e71b1 fix: ``yaml.safe_load(...) or {}`` silently
    converted falsy non-mapping documents (false, 0, [], "") into a
    valid-looking empty mapping; only an EMPTY document (None) may mean
    "no keys".
    """

    @pytest.mark.parametrize("doc", ["false\n", "0\n", "[]\n"])
    def test_falsy_non_mapping_yaml_file_rejected(self, tmp_path: Path, doc: str) -> None:
        from elspeth.config_loading import load_settings

        config_file = tmp_path / "settings.yaml"
        config_file.write_text(doc)
        with pytest.raises(ValueError, match="must be a YAML mapping"):
            load_settings(config_file)


def test_web_scrape_auth_requires_exact_env_reference_for_file_settings(tmp_path: Path) -> None:
    from elspeth.config_loading import _reject_web_scrape_auth_literal, load_settings

    base = {"transforms": [{"plugin": "web_scrape", "options": {"auth": {"credential": "literal-token"}}}]}
    with pytest.raises(ValueError, match=r"web_scrape auth\.credential must be an exact environment secret reference"):
        _reject_web_scrape_auth_literal(base)
    config_path = tmp_path / "settings.yaml"
    config_path.write_text("transforms:\n  - plugin: web_scrape\n    options:\n      auth:\n        credential: literal-token\n")
    with pytest.raises(ValueError, match=r"web_scrape auth\.credential must be an exact environment secret reference"):
        load_settings(config_path)
    base["transforms"][0]["options"]["auth"]["credential"] = "${WEB_SEARCH_TOKEN}"
    _reject_web_scrape_auth_literal(base)
    base["transforms"][0]["options"]["auth"]["credential"] = "${WEB_SEARCH_TOKEN:-fallback}"
    with pytest.raises(ValueError, match="exact environment secret reference"):
        _reject_web_scrape_auth_literal(base)
