# tests/plugins/llm/test_tracing_config.py
"""Tests for Tier 2 tracing configuration models."""

import pytest

from elspeth.plugins.transforms.llm.tracing import (
    AzureAITracingConfig,
    LangfuseTracingConfig,
    TracingConfig,
    parse_tracing_config,
    validate_tracing_config,
)


class TestTracingConfigParsing:
    """Tests for parse_tracing_config function."""

    def test_none_config_returns_none(self) -> None:
        """None input returns None."""
        result = parse_tracing_config(None)
        assert result is None

    def test_empty_dict_returns_base_config(self) -> None:
        """Empty dict returns base TracingConfig with provider='none'."""
        result = parse_tracing_config({})
        assert isinstance(result, TracingConfig)
        assert result.provider == "none"

    def test_azure_ai_provider_returns_azure_config(self) -> None:
        """Provider 'azure_ai' returns AzureAITracingConfig."""
        config = {
            "provider": "azure_ai",
            "connection_string": "InstrumentationKey=xxx",
            "enable_content_recording": True,
            "enable_live_metrics": False,
        }
        result = parse_tracing_config(config)

        assert isinstance(result, AzureAITracingConfig)
        assert result.provider == "azure_ai"
        assert result.connection_string == "InstrumentationKey=xxx"
        assert result.enable_content_recording is True
        assert result.enable_live_metrics is False

    def test_azure_ai_rejects_missing_connection_string(self) -> None:
        """AzureAITracingConfig crashes without connection_string."""
        config = {"provider": "azure_ai"}
        with pytest.raises(ValueError, match="connection_string"):
            parse_tracing_config(config)

    def test_azure_ai_defaults(self) -> None:
        """AzureAITracingConfig has sensible defaults for optional fields."""
        config = {"provider": "azure_ai", "connection_string": "InstrumentationKey=test"}
        result = parse_tracing_config(config)

        assert isinstance(result, AzureAITracingConfig)
        assert result.connection_string == "InstrumentationKey=test"
        assert result.enable_content_recording is True  # Default: capture prompts
        assert result.enable_live_metrics is False  # Default: off

    def test_langfuse_provider_returns_langfuse_config(self) -> None:
        """Provider 'langfuse' returns LangfuseTracingConfig."""
        config = {
            "provider": "langfuse",
            "public_key": "pk-xxx",
            "secret_key": "sk-xxx",
            "host": "https://self-hosted.example.com",
        }
        result = parse_tracing_config(config)

        assert isinstance(result, LangfuseTracingConfig)
        assert result.provider == "langfuse"
        assert result.public_key == "pk-xxx"
        assert result.secret_key == "sk-xxx"
        assert result.host == "https://self-hosted.example.com"

    def test_langfuse_rejects_missing_host(self) -> None:
        """LangfuseTracingConfig crashes without host — infrastructure addressing."""
        with pytest.raises(ValueError, match="host"):
            parse_tracing_config(
                {
                    "provider": "langfuse",
                    "public_key": "pk-xxx",
                    "secret_key": "sk-xxx",
                }
            )

    def test_langfuse_rejects_missing_keys(self) -> None:
        """LangfuseTracingConfig crashes without public_key/secret_key."""
        base = {"provider": "langfuse", "host": "https://langfuse.example.com"}
        with pytest.raises(ValueError, match="public_key"):
            parse_tracing_config(base)
        with pytest.raises(ValueError, match="secret_key"):
            parse_tracing_config({**base, "public_key": "pk-xxx"})

    def test_langfuse_defaults(self) -> None:
        """LangfuseTracingConfig defaults for genuinely optional fields."""
        config = {
            "provider": "langfuse",
            "public_key": "pk-xxx",
            "secret_key": "sk-xxx",
            "host": "https://langfuse.example.com",
        }
        result = parse_tracing_config(config)

        assert isinstance(result, LangfuseTracingConfig)
        assert result.host == "https://langfuse.example.com"
        assert result.tracing_enabled is True  # v3: default enabled

    def test_langfuse_tracing_enabled_field(self) -> None:
        """LangfuseTracingConfig supports tracing_enabled field (v3)."""
        config = {
            "provider": "langfuse",
            "public_key": "pk-xxx",
            "secret_key": "sk-xxx",
            "host": "https://langfuse.example.com",
            "tracing_enabled": False,  # Explicitly disabled
        }
        result = parse_tracing_config(config)

        assert isinstance(result, LangfuseTracingConfig)
        assert result.tracing_enabled is False

    def test_unknown_provider_raises_value_error(self) -> None:
        """Unknown provider raises ValueError instead of silently accepting."""
        import pytest

        config = {"provider": "unknown_provider"}
        with pytest.raises(ValueError, match="Unknown tracing provider"):
            parse_tracing_config(config)

    def test_none_provider_returns_base_config(self) -> None:
        """Provider 'none' returns base TracingConfig."""
        config = {"provider": "none"}
        result = parse_tracing_config(config)

        assert isinstance(result, TracingConfig)
        assert result.provider == "none"


class TestTracingConfigValidation:
    """Tests for construction-time and post-construction validation."""

    def test_azure_ai_rejects_none_connection_string(self) -> None:
        """Azure AI crashes at construction without connection_string."""
        with pytest.raises(ValueError, match="connection_string"):
            AzureAITracingConfig(connection_string=None)

    def test_azure_ai_rejects_wrong_provider(self) -> None:
        """Azure AI crashes if provider discriminator is overridden."""
        with pytest.raises(ValueError, match="provider='azure_ai'"):
            AzureAITracingConfig(provider="langfuse", connection_string="x")

    def test_azure_ai_valid_construction(self) -> None:
        """Azure AI with connection_string passes validation."""
        config = AzureAITracingConfig(connection_string="InstrumentationKey=xxx")
        errors = validate_tracing_config(config)
        assert len(errors) == 0

    def test_langfuse_rejects_none_keys(self) -> None:
        """Langfuse crashes at construction without required keys."""
        with pytest.raises(ValueError, match="public_key"):
            LangfuseTracingConfig(public_key=None, secret_key="sk-xxx", host="https://x.com")
        with pytest.raises(ValueError, match="secret_key"):
            LangfuseTracingConfig(public_key="pk-xxx", secret_key=None, host="https://x.com")
        with pytest.raises(ValueError, match="host"):
            LangfuseTracingConfig(public_key="pk-xxx", secret_key="sk-xxx", host=None)

    def test_langfuse_rejects_wrong_provider(self) -> None:
        """Langfuse crashes if provider discriminator is overridden."""
        with pytest.raises(ValueError, match="provider='langfuse'"):
            LangfuseTracingConfig(provider="azure_ai", public_key="pk", secret_key="sk", host="https://x.com")

    def test_langfuse_valid_construction(self) -> None:
        """Langfuse with all required fields passes validation."""
        config = LangfuseTracingConfig(public_key="pk-xxx", secret_key="sk-xxx", host="https://langfuse.example.com")
        errors = validate_tracing_config(config)
        assert len(errors) == 0

    def test_none_provider_returns_no_errors(self) -> None:
        """Provider 'none' always valid."""
        config = TracingConfig(provider="none")
        errors = validate_tracing_config(config)
        assert len(errors) == 0

    def test_unknown_provider_returns_validation_error(self) -> None:
        """Unknown provider returns explicit validation error."""
        config = TracingConfig(provider="unknown_provider")
        errors = validate_tracing_config(config)
        assert len(errors) == 1
        assert "Unknown tracing provider" in errors[0]
        assert "unknown_provider" in errors[0]


def _langfuse(host: object) -> LangfuseTracingConfig:
    """Construct a Langfuse tracing config that varies only in ``host``."""
    return LangfuseTracingConfig(public_key="pk-xxx", secret_key="sk-xxx", host=host)  # type: ignore[arg-type]


class TestLangfuseHostEgressGuard:
    """The Langfuse host carries public_key/secret_key to a network peer.

    ``create_langfuse_tracer`` hands ``host`` to ``Langfuse(...)`` alongside
    both API keys, so a plaintext or credential-bearing host is a secret-egress
    channel. The settings-load boundary must fail closed (elspeth-f54b6a6f55).
    """

    @pytest.mark.parametrize(
        "host",
        [
            "https://cloud.langfuse.com",
            "https://langfuse.example.com/",
            "https://langfuse.example.com:3000",
            "https://langfuse.example.com/observability",
            "https://langfuse.example.com:3000/observability/",
        ],
        ids=["bare", "trailing-slash", "port", "path-prefix", "port-and-path"],
    )
    def test_accepts_https_hosts(self, host: str) -> None:
        """HTTPS hosts — including port and path-prefix forms — stay accepted."""
        assert _langfuse(host).host == host

    @pytest.mark.parametrize(
        "host",
        ["http://localhost", "http://localhost:3000", "http://127.0.0.1:3000", "http://[::1]:3000"],
        ids=["localhost", "localhost-port", "ipv4-loopback", "ipv6-loopback"],
    )
    def test_accepts_http_loopback_hosts(self, host: str) -> None:
        """Self-hosted local Langfuse over plaintext loopback is legitimate."""
        assert _langfuse(host).host == host

    @pytest.mark.parametrize(
        "host",
        [
            "http://langfuse.example.invalid",
            "http://127.0.0.1.evil.invalid",
            "http://localhost.evil.invalid",
            "http://10.0.0.5",
        ],
        ids=["public-host", "loopback-prefixed-host", "localhost-prefixed-host", "private-non-loopback"],
    )
    def test_rejects_plaintext_non_loopback_hosts(self, host: str) -> None:
        """Plaintext HTTP off the loopback interface would egress both API keys."""
        with pytest.raises(ValueError, match="HTTPS"):
            _langfuse(host)

    @pytest.mark.parametrize(
        "host",
        [
            "https://user:s3cr3t-p4ssw0rd@langfuse.example.com",
            "http://user:s3cr3t-p4ssw0rd@localhost:3000",
            "https://s3cr3t-p4ssw0rd@langfuse.example.com",
        ],
        ids=["https-userinfo", "http-loopback-userinfo", "username-only"],
    )
    def test_rejects_embedded_credentials_without_echoing_them(self, host: str) -> None:
        """Userinfo is rejected, and the rejection never echoes the credential."""
        with pytest.raises(ValueError, match="embedded credentials") as excinfo:
            _langfuse(host)
        assert "s3cr3t-p4ssw0rd" not in str(excinfo.value)
        assert "user" not in str(excinfo.value)

    @pytest.mark.parametrize(
        "host",
        ["https://", "langfuse.example.com", "/observability", "not a url"],
        ids=["scheme-only", "bare-authority", "bare-path", "unparseable"],
    )
    def test_rejects_hosts_without_a_hostname(self, host: str) -> None:
        """A value urlsplit cannot resolve to a hostname is not addressable."""
        with pytest.raises(ValueError, match="hostname"):
            _langfuse(host)

    def test_rejects_empty_host(self) -> None:
        """An empty host is a configuration mistake, not an implicit default."""
        with pytest.raises(ValueError, match="host"):
            _langfuse("")

    @pytest.mark.parametrize("host", [123, ["https://langfuse.example.com"]], ids=["int", "list"])
    def test_rejects_non_string_host_as_value_error(self, host: object) -> None:
        """parse_tracing_config splats untrusted YAML, so a non-str host must
        raise ValueError at the boundary rather than AttributeError inside it."""
        with pytest.raises(ValueError, match="host"):
            _langfuse(host)

    def test_stores_the_stripped_host(self) -> None:
        """The validated string is the stored string — surrounding whitespace
        must not survive validation and reach the Langfuse client unstripped."""
        assert _langfuse("  https://langfuse.example.com  ").host == "https://langfuse.example.com"

    def test_parse_tracing_config_enforces_the_same_rule(self) -> None:
        """The guard is reachable through the YAML parse entry point."""
        with pytest.raises(ValueError, match="HTTPS"):
            parse_tracing_config(
                {
                    "provider": "langfuse",
                    "public_key": "pk-xxx",
                    "secret_key": "sk-xxx",
                    "host": "http://langfuse.example.invalid",
                }
            )
