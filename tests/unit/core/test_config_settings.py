"""Configuration settings validation and audit-export configuration tests."""

from pathlib import Path

import pytest
from pydantic import ValidationError


def _complete_audit_export_config(**overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "enabled": True,
        "compartment_id": "test-compartment",
        "sink": "audit_archive",
        "format": "csv",
        "signing_mode": "unsigned",
        "signer_key_id": "UNSIGNED",
        "total_record_limit": 10_000,
        "total_byte_limit": 10_000_000,
        "chunk_limit": 100,
        "per_chunk_record_limit": 1_000,
        "per_chunk_byte_limit": 1_000_000,
        "spool_root": ".elspeth/audit-export-spool",
        "content_store": {
            "content_store_id": "archive-primary-v1",
            "namespace": "audit-export",
            "root": ".elspeth/audit-export-content-store/primary",
            "policy_version": "audit-store-policy-v1",
            "retention_days": 365,
            "durability": "fsync",
        },
    }
    config.update(overrides)
    return config


class TestDatabaseSettings:
    """Database configuration validation."""

    def test_valid_sqlite_url(self) -> None:
        from elspeth.core.config import DatabaseSettings

        settings = DatabaseSettings(url="sqlite:///audit.db")
        assert settings.url == "sqlite:///audit.db"
        assert settings.pool_size == 5  # default

    def test_valid_postgres_url(self) -> None:
        from elspeth.core.config import DatabaseSettings

        settings = DatabaseSettings(
            url="postgresql://user:pass@localhost/db",  # secret-scan: allow-this-line
            pool_size=10,
        )
        assert settings.pool_size == 10

    def test_pool_size_must_be_positive(self) -> None:
        from elspeth.core.config import DatabaseSettings

        with pytest.raises(ValidationError):
            DatabaseSettings(url="sqlite:///test.db", pool_size=0)


class TestRetrySettings:
    """Retry configuration validation."""

    def test_defaults(self) -> None:
        from elspeth.core.config import RetrySettings

        settings = RetrySettings()
        assert settings.max_attempts == 3
        assert settings.initial_delay_seconds == 1.0
        assert settings.max_delay_seconds == 60.0
        assert settings.exponential_base == 2.0

    def test_max_attempts_must_be_positive(self) -> None:
        from elspeth.core.config import RetrySettings

        with pytest.raises(ValidationError):
            RetrySettings(max_attempts=0)

    def test_delays_must_be_positive(self) -> None:
        from elspeth.core.config import RetrySettings

        with pytest.raises(ValidationError):
            RetrySettings(initial_delay_seconds=-1.0)

    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("initial_delay_seconds", 0.005),
            ("max_delay_seconds", 0.05),
        ],
    )
    def test_delay_floors_match_runtime_contract(self, field_name: str, value: float) -> None:
        from elspeth.core.config import RetrySettings

        with pytest.raises(ValidationError) as exc_info:
            RetrySettings(**{field_name: value})

        assert exc_info.value.errors()[0]["loc"] == (field_name,)


class TestElspethSettings:
    """Top-level settings validation."""

    def test_minimal_valid_config(self) -> None:
        from elspeth.core.config import ElspethSettings

        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output"}},
            sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
        )
        assert settings.sources["primary"].plugin == "csv"
        assert settings.retry.max_attempts == 3  # default

    def test_nested_config(self) -> None:
        from elspeth.core.config import ElspethSettings

        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output"}},
            sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
            retry={"max_attempts": 5},
        )
        assert settings.retry.max_attempts == 5

    def test_rejects_duplicate_collection_probes(self) -> None:
        """Duplicate probe collections are rejected at config time, before any probe runs.

        Regression for elspeth-b657daab02: probe results are keyed by collection
        in the commencement gate context, so two probes for the same collection
        collide. The old guard fired only in resolve_preflight, AFTER each
        probe.probe() had already run, and raised FrameworkBugError.
        """
        from pydantic import ValidationError

        from elspeth.core.config import ElspethSettings

        with pytest.raises(ValidationError, match="duplicate collection_probes"):
            ElspethSettings(
                sources={"primary": {"plugin": "csv", "on_success": "output"}},
                sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
                collection_probes=[
                    {"collection": "docs", "provider": "chroma"},
                    {"collection": "docs", "provider": "chroma"},
                ],
            )

    def test_accepts_distinct_collection_probes(self) -> None:
        from elspeth.core.config import ElspethSettings

        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output"}},
            sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
            collection_probes=[
                {"collection": "docs", "provider": "chroma"},
                {"collection": "images", "provider": "chroma"},
            ],
        )
        assert len(settings.collection_probes) == 2


class TestSourceSettings:
    """SourceSettings matches architecture specification."""

    def test_source_settings_structure(self) -> None:
        """SourceSettings has plugin and options."""
        from elspeth.core.config import SourceSettings

        ds = SourceSettings(plugin="csv_local", on_success="output", options={"path": "data/input.csv"})
        assert ds.plugin == "csv_local"
        assert ds.options == {"path": "data/input.csv"}

    def test_source_settings_options_default_empty(self) -> None:
        """Options defaults to empty dict."""
        from elspeth.core.config import SourceSettings

        ds = SourceSettings(plugin="csv", on_success="output")
        assert ds.options == {}


class TestTransformSettings:
    """TransformSettings matches architecture specification."""

    def test_transform_settings_structure(self) -> None:
        """TransformSettings has plugin and options (transforms only)."""
        from elspeth.core.config import TransformSettings

        # TransformSettings is now transform-only (gates are config-driven)
        rp = TransformSettings(
            name="test_transform",
            plugin="field_mapper",
            input="source_out",
            on_success="output",
            on_error="discard",
            options={"field": "confidence", "min": 0.8},
        )
        assert rp.plugin == "field_mapper"
        assert rp.options == {"field": "confidence", "min": 0.8}

    def test_transform_settings_defaults(self) -> None:
        """TransformSettings defaults: options is empty dict."""
        from elspeth.core.config import TransformSettings

        rp = TransformSettings(name="test_transform", plugin="passthrough", input="source_out", on_success="output", on_error="discard")
        assert rp.plugin == "passthrough"
        assert rp.options == {}

    def test_transform_settings_requires_plugin(self) -> None:
        """Plugin name is required."""
        from elspeth.core.config import TransformSettings

        with pytest.raises(ValidationError):
            TransformSettings(options={})  # type: ignore[call-arg]


class TestSinkSettings:
    """SinkSettings matches architecture specification."""

    def test_sink_settings_structure(self) -> None:
        """SinkSettings has plugin and options."""
        from elspeth.core.config import SinkSettings

        sink = SinkSettings(plugin="csv", on_write_failure="discard", options={"path": "output/results.csv"})
        assert sink.plugin == "csv"
        assert sink.options == {"path": "output/results.csv"}

    def test_sink_settings_options_default_empty(self) -> None:
        """Options defaults to empty dict."""
        from elspeth.core.config import SinkSettings

        sink = SinkSettings(plugin="database", on_write_failure="discard")
        assert sink.options == {}


class TestLandscapeExportSettings:
    """LandscapeExportSettings for audit trail export configuration."""

    def test_landscape_export_config_defaults(self) -> None:
        """Export config should have sensible defaults."""
        from elspeth.core.config import LandscapeSettings

        settings = LandscapeSettings()
        assert settings.export is not None
        assert settings.export.enabled is False
        assert settings.export.format == "csv"
        assert settings.export.sign is False
        assert settings.export.serialization_version == "audit-export-v3"

    @pytest.mark.parametrize("version", ["audit-export-v2", "audit-export-v4"])
    def test_landscape_export_rejects_unsupported_serialization(self, version: str) -> None:
        from elspeth.core.config import LandscapeExportSettings

        with pytest.raises(ValidationError, match="serialization_version"):
            LandscapeExportSettings(serialization_version=version)

    def test_landscape_export_accepts_current_serialization(self) -> None:
        from elspeth.core.config import LandscapeExportSettings

        settings = LandscapeExportSettings(serialization_version="audit-export-v3")
        assert settings.serialization_version == "audit-export-v3"

    def test_landscape_export_config_with_sink(self) -> None:
        """Export config should accept sink reference."""
        from elspeth.core.config import LandscapeSettings

        settings = LandscapeSettings(
            export=_complete_audit_export_config(
                signing_mode="hmac_sha256",
                signer_key_id="audit-key-v1",
                signing_secret_ref="ELSPETH_SIGNING_KEY",
            )
        )
        assert settings.export.enabled is True
        assert settings.export.sink == "audit_archive"
        assert settings.export.sign is True

    def test_landscape_export_format_validation(self) -> None:
        """Format must be 'csv' or 'json'."""
        from elspeth.core.config import LandscapeExportSettings

        with pytest.raises(ValidationError):
            LandscapeExportSettings(format="xml")


class TestLandscapeSettings:
    """LandscapeSettings matches architecture specification."""

    def test_landscape_settings_structure(self) -> None:
        """LandscapeSettings has enabled, backend, url."""
        from elspeth.core.config import LandscapeSettings

        ls = LandscapeSettings(enabled=True, backend="sqlite", url="sqlite:///./state/audit.db")
        assert ls.enabled is True
        assert ls.backend == "sqlite"
        assert ls.url == "sqlite:///./state/audit.db"

    def test_landscape_settings_defaults(self) -> None:
        """LandscapeSettings has sensible defaults."""
        from elspeth.core.config import LandscapeSettings

        ls = LandscapeSettings()
        assert ls.enabled is True
        assert ls.backend == "sqlite"
        # Default audit DB lives under data/ alongside the other system DBs
        # (sessions.db, auth.db, runs/audit.db) — a separate file from the web's
        # data/runs/audit.db to avoid SQLite write-contention with the running
        # web service. Examples override landscape.url in their own settings.yaml.
        assert ls.url == "sqlite:///./data/audit.db"

    def test_landscape_settings_postgresql_url(self) -> None:
        """LandscapeSettings accepts PostgreSQL DSNs without mangling."""
        from elspeth.core.config import LandscapeSettings

        # This would fail with pathlib.Path which mangles // as UNC paths
        pg_url = "postgresql://user:pass@localhost:5432/elspeth_audit"  # secret-scan: allow-this-line
        ls = LandscapeSettings(enabled=True, backend="postgresql", url=pg_url)
        assert ls.url == pg_url  # Preserved exactly

    def test_landscape_settings_backend_validation(self) -> None:
        """Backend must be sqlite, sqlcipher, or postgresql."""
        from elspeth.core.config import LandscapeSettings

        with pytest.raises(ValidationError):
            LandscapeSettings(backend="mysql")

    def test_landscape_settings_url_validation_valid_sqlite(self) -> None:
        """Valid SQLite URLs are accepted."""
        from elspeth.core.config import LandscapeSettings

        # Memory database
        ls = LandscapeSettings(url="sqlite:///:memory:")
        assert ls.url == "sqlite:///:memory:"

        # File-based with relative path
        ls = LandscapeSettings(url="sqlite:///./data/audit.db")
        assert ls.url == "sqlite:///./data/audit.db"

        # File-based with absolute path
        ls = LandscapeSettings(url="sqlite:////var/lib/elspeth/audit.db")
        assert ls.url == "sqlite:////var/lib/elspeth/audit.db"

    def test_landscape_settings_url_validation_valid_postgresql(self) -> None:
        """Valid PostgreSQL URLs are accepted."""
        from elspeth.core.config import LandscapeSettings

        # Full DSN with credentials
        pg_url = "postgresql://user:pass@localhost:5432/elspeth_audit"  # secret-scan: allow-this-line
        ls = LandscapeSettings(backend="postgresql", url=pg_url)
        assert ls.url == pg_url

        # With SSL mode parameter
        pg_url_ssl = "postgresql://user:pass@db.example.com/audit?sslmode=require"  # secret-scan: allow-this-line
        ls = LandscapeSettings(backend="postgresql", url=pg_url_ssl)
        assert ls.url == pg_url_ssl

    def test_landscape_settings_url_validation_rejects_invalid(self) -> None:
        """Invalid database URLs are rejected at config time."""
        from elspeth.core.config import LandscapeSettings

        # Not a valid URL format at all
        with pytest.raises(ValidationError, match="invalid database URL format"):
            LandscapeSettings(url="not-a-valid-url")

        # Missing scheme/driver
        with pytest.raises(ValidationError, match="invalid database URL format"):
            LandscapeSettings(url="://localhost/db")

    def test_landscape_settings_url_validation_rejects_empty(self) -> None:
        """Empty URL is rejected."""
        from elspeth.core.config import LandscapeSettings

        with pytest.raises(ValidationError, match="must not be blank"):
            LandscapeSettings(url="")

    def test_sqlcipher_backend_requires_sqlite_url(self) -> None:
        """backend='sqlcipher' with a PostgreSQL URL is rejected."""
        from elspeth.core.config import LandscapeSettings

        with pytest.raises(ValidationError, match="requires URL scheme"):
            LandscapeSettings(backend="sqlcipher", url="postgresql://user:pass@host/db")  # secret-scan: allow-this-line

    def test_sqlcipher_backend_valid(self) -> None:
        """backend='sqlcipher' with a SQLite URL is accepted."""
        from elspeth.core.config import LandscapeSettings

        ls = LandscapeSettings(backend="sqlcipher", url="sqlite:///./state/audit.db")
        assert ls.backend == "sqlcipher"
        assert ls.url == "sqlite:///./state/audit.db"

    def test_encryption_key_env_default(self) -> None:
        """Default encryption_key_env is ELSPETH_AUDIT_KEY."""
        from elspeth.core.config import LandscapeSettings

        ls = LandscapeSettings(backend="sqlcipher", url="sqlite:///./state/audit.db")
        assert ls.encryption_key_env == "ELSPETH_AUDIT_KEY"

    def test_encryption_key_env_custom(self) -> None:
        """Custom encryption_key_env is accepted."""
        from elspeth.core.config import LandscapeSettings

        ls = LandscapeSettings(backend="sqlcipher", url="sqlite:///./state/audit.db", encryption_key_env="MY_CUSTOM_KEY")
        assert ls.encryption_key_env == "MY_CUSTOM_KEY"


class TestConcurrencySettings:
    """ConcurrencySettings matches architecture specification."""

    def test_concurrency_settings_structure(self) -> None:
        """ConcurrencySettings has max_workers."""
        from elspeth.core.config import ConcurrencySettings

        cs = ConcurrencySettings(max_workers=16)
        assert cs.max_workers == 16

    def test_concurrency_settings_default(self) -> None:
        """Default max_workers is 4 per architecture."""
        from elspeth.core.config import ConcurrencySettings

        cs = ConcurrencySettings()
        assert cs.max_workers == 4

    def test_concurrency_settings_validation(self) -> None:
        """max_workers must be positive."""
        from elspeth.core.config import ConcurrencySettings

        with pytest.raises(ValidationError):
            ConcurrencySettings(max_workers=0)
        with pytest.raises(ValidationError):
            ConcurrencySettings(max_workers=-1)


class TestExportSinkValidation:
    """Validation that export.sink references a defined sink."""

    def test_export_sink_must_exist_when_enabled(self) -> None:
        """If export.enabled=True, export.sink must reference a defined sink."""
        from elspeth.core.config import ElspethSettings

        with pytest.raises(ValidationError) as exc_info:
            ElspethSettings(
                sources={"primary": {"plugin": "csv", "on_success": "output", "options": {"path": "input.csv"}}},
                sinks={"output": {"plugin": "csv", "on_write_failure": "discard", "options": {"path": "out.csv"}}},
                landscape={
                    "export": {
                        **_complete_audit_export_config(sink="nonexistent_sink"),
                    }
                },
            )

        assert "export.sink 'nonexistent_sink' not found in sinks" in str(exc_info.value)

    def test_export_sink_not_required_when_disabled(self) -> None:
        """If export.enabled=False, sink can be None."""
        from elspeth.core.config import ElspethSettings

        # Should not raise
        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output", "options": {"path": "input.csv"}}},
            sinks={"output": {"plugin": "csv", "on_write_failure": "discard", "options": {"path": "out.csv"}}},
            landscape={
                "export": {"enabled": False}  # No sink required
            },
        )
        assert settings.landscape.export.sink is None

    def test_export_sink_required_when_enabled(self) -> None:
        """If export.enabled=True, sink cannot be None."""
        from elspeth.core.config import ElspethSettings

        with pytest.raises(ValidationError) as exc_info:
            ElspethSettings(
                sources={"primary": {"plugin": "csv", "on_success": "output", "options": {"path": "input.csv"}}},
                sinks={"output": {"plugin": "csv", "on_write_failure": "discard", "options": {"path": "out.csv"}}},
                landscape={
                    "export": {
                        **_complete_audit_export_config(sink=None),
                    }
                },
            )

        assert "landscape.export.sink is required when export is enabled" in str(exc_info.value)

    def test_export_sink_valid_reference(self) -> None:
        """If export.sink references a valid sink, no error."""
        from elspeth.core.config import ElspethSettings

        # Should not raise
        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output", "options": {"path": "input.csv"}}},
            sinks={
                "output": {"plugin": "csv", "on_write_failure": "discard", "options": {"path": "out.csv"}},
                "audit_archive": {"plugin": "csv", "on_write_failure": "discard", "options": {"path": "audit.csv"}},
            },
            landscape={
                "export": {
                    **_complete_audit_export_config(sink="audit_archive"),
                }
            },
        )
        assert settings.landscape.export.sink == "audit_archive"


class TestCheckpointSettings:
    """Tests for checkpoint configuration."""

    def test_checkpoint_settings_defaults(self) -> None:
        from elspeth.core.config import CheckpointSettings

        settings = CheckpointSettings()

        assert settings.enabled is True
        assert settings.frequency == "every_row"

    def test_checkpoint_frequency_options(self) -> None:
        from elspeth.core.config import CheckpointSettings

        # Every row (safest, slowest)
        s1 = CheckpointSettings(frequency="every_row")
        assert s1.frequency == "every_row"

        # Every N rows (balanced)
        s2 = CheckpointSettings(frequency="every_n", checkpoint_interval=100)
        assert s2.frequency == "every_n"
        assert s2.checkpoint_interval == 100

        # Aggregation boundaries only (fastest, less safe)
        s3 = CheckpointSettings(frequency="aggregation_only")
        assert s3.frequency == "aggregation_only"

    def test_checkpoint_settings_validation(self) -> None:
        from pydantic import ValidationError

        from elspeth.core.config import CheckpointSettings

        # every_n requires checkpoint_interval
        with pytest.raises(ValidationError):
            CheckpointSettings(frequency="every_n", checkpoint_interval=None)

    def test_checkpoint_interval_must_be_positive(self) -> None:
        """checkpoint_interval must be > 0 when provided."""
        from pydantic import ValidationError

        from elspeth.core.config import CheckpointSettings

        # Zero is invalid
        with pytest.raises(ValidationError):
            CheckpointSettings(frequency="every_n", checkpoint_interval=0)

        # Negative is invalid
        with pytest.raises(ValidationError):
            CheckpointSettings(frequency="every_n", checkpoint_interval=-1)

    def test_checkpoint_settings_invalid_frequency(self) -> None:
        """Frequency must be a valid option."""
        from pydantic import ValidationError

        from elspeth.core.config import CheckpointSettings

        with pytest.raises(ValidationError):
            CheckpointSettings(frequency="invalid")


class TestElspethSettingsArchitecture:
    """Top-level settings matches architecture specification."""

    def test_elspeth_settings_required_fields(self) -> None:
        """ElspethSettings requires datasource, sinks, output_sink."""
        from elspeth.core.config import ElspethSettings

        # Missing required fields
        with pytest.raises(ValidationError) as exc_info:
            ElspethSettings()  # type: ignore[call-arg]

        errors = exc_info.value.errors()
        missing_fields = {e["loc"][0] for e in errors if e["type"] == "missing"}
        assert "sources" in missing_fields
        assert "sinks" in missing_fields

    def test_elspeth_settings_minimal_valid(self) -> None:
        """Minimal valid configuration."""
        from elspeth.core.config import (
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )

        settings = ElspethSettings(
            sources={"primary": SourceSettings(plugin="csv", on_success="results", options={"path": "in.csv"})},
            sinks={"results": SinkSettings(plugin="csv", on_write_failure="discard", options={"path": "out.csv"})},
        )

        assert settings.sources["primary"].plugin == "csv"
        assert "results" in settings.sinks
        # Defaults applied
        assert settings.transforms == []
        assert settings.landscape.enabled is True
        assert settings.concurrency.max_workers == 4

    def test_elspeth_settings_rejects_default_sink(self) -> None:
        """default_sink is rejected by extra='forbid' on ElspethSettings.

        The helpful migration message mentioning on_success is generated in
        load_settings() (YAML path), not at the Pydantic model level.
        """
        from elspeth.core.config import (
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )

        with pytest.raises(ValidationError) as exc_info:
            ElspethSettings(
                sources={"primary": SourceSettings(plugin="csv", on_success="results")},
                sinks={"results": SinkSettings(plugin="csv", on_write_failure="discard")},
                **{"default_sink": "results"},  # testing rejected field
            )

        assert "default_sink" in str(exc_info.value)

    def test_elspeth_settings_at_least_one_sink(self) -> None:
        """At least one sink is required."""
        from elspeth.core.config import ElspethSettings, SourceSettings

        with pytest.raises(ValidationError) as exc_info:
            ElspethSettings(
                sources={"primary": SourceSettings(plugin="csv", on_success="output")},
                sinks={},  # Empty!
            )

        assert "sink" in str(exc_info.value).lower()


class TestRateLimitSettings:
    """Tests for rate limit configuration."""

    def test_rate_limit_settings_defaults(self) -> None:
        from elspeth.core.config import RateLimitSettings

        settings = RateLimitSettings()

        assert settings.enabled is True
        assert settings.default_requests_per_minute == 60
        assert settings.persistence_path is None

    def test_rate_limit_per_service(self) -> None:
        from elspeth.core.config import RateLimitSettings, ServiceRateLimit

        settings = RateLimitSettings(
            services={
                "openai": ServiceRateLimit(
                    requests_per_minute=100,
                ),
                "weather_api": ServiceRateLimit(
                    requests_per_minute=120,
                ),
            }
        )

        assert settings.services["openai"].requests_per_minute == 100
        assert settings.services["weather_api"].requests_per_minute == 120

    def test_rate_limit_get_service_config(self) -> None:
        from elspeth.core.config import RateLimitSettings, ServiceRateLimit

        settings = RateLimitSettings(
            default_requests_per_minute=60,
            services={
                "openai": ServiceRateLimit(requests_per_minute=100),
            },
        )

        # Configured service
        openai_config = settings.get_service_config("openai")
        assert openai_config.requests_per_minute == 100

        # Unconfigured service falls back to default
        other_config = settings.get_service_config("other_api")
        assert other_config.requests_per_minute == 60

    def test_service_rate_limit_requests_per_minute_must_be_positive(self) -> None:
        """requests_per_minute must be > 0."""
        from elspeth.core.config import ServiceRateLimit

        with pytest.raises(ValidationError):
            ServiceRateLimit(requests_per_minute=0)

        with pytest.raises(ValidationError):
            ServiceRateLimit(requests_per_minute=-1)

    def test_rate_limit_settings_default_requests_per_minute_must_be_positive(
        self,
    ) -> None:
        """default_requests_per_minute must be > 0."""
        from elspeth.core.config import RateLimitSettings

        with pytest.raises(ValidationError):
            RateLimitSettings(default_requests_per_minute=0)

        with pytest.raises(ValidationError):
            RateLimitSettings(default_requests_per_minute=-1)

        with pytest.raises(ValidationError):
            RateLimitSettings(default_requests_per_minute=0)

        with pytest.raises(ValidationError):
            RateLimitSettings(default_requests_per_minute=-1)


class TestGateSettings:
    """Tests for engine-level gate configuration (WP-09)."""

    def test_gate_settings_minimal_valid(self) -> None:
        """GateSettings with required fields only."""
        from elspeth.core.config import GateSettings

        gate = GateSettings(
            name="quality_check",
            input="source_out",
            condition="row['confidence'] >= 0.85",
            routes={"true": "quality_ok", "false": "review_sink"},
        )
        assert gate.name == "quality_check"
        assert gate.condition == "row['confidence'] >= 0.85"
        assert gate.routes == {"true": "quality_ok", "false": "review_sink"}
        assert gate.on_error is None
        assert "on_error" not in gate.model_dump(mode="json")
        assert gate.fork_to is None

    @pytest.mark.parametrize("on_error", ["discard", "gate_errors", "gate-errors"])
    def test_gate_settings_accepts_row_error_policy(self, on_error: str) -> None:
        """A gate may divert row-level expression failures or discard them."""
        from elspeth.core.config import GateSettings

        gate = GateSettings(
            name="quality_check",
            input="source_out",
            condition="row['confidence'] >= 0.85",
            routes={"true": "quality_ok", "false": "review_sink"},
            on_error=f" {on_error} ",
        )

        assert gate.on_error == on_error
        assert gate.model_dump(mode="json")["on_error"] == on_error

    def test_gate_settings_rejects_blank_row_error_policy(self) -> None:
        """A present policy must identify a sink or the discard sentinel."""
        from elspeth.core.config import GateSettings

        with pytest.raises(ValidationError, match="on_error must be"):
            GateSettings(
                name="quality_check",
                input="source_out",
                condition="row['confidence'] >= 0.85",
                routes={"true": "quality_ok", "false": "review_sink"},
                on_error="  ",
            )

    def test_gate_settings_with_fork(self) -> None:
        """GateSettings with fork_to for parallel paths."""
        from elspeth.core.config import GateSettings

        gate = GateSettings(
            name="parallel_analysis",
            input="source_out",
            condition="True",
            routes={"true": "fork", "false": "fallback_path"},
            fork_to=["path_a", "path_b"],
        )
        assert gate.name == "parallel_analysis"
        assert gate.routes == {"true": "fork", "false": "fallback_path"}
        assert gate.fork_to == ["path_a", "path_b"]

    @pytest.mark.parametrize(
        "bad_condition",
        [
            "5",
            "row['amount'] + 1",
            "len(row['items'])",
            "abs(row['x'])",
            "row['x'] / 2",
            "row['x'] // 2",
            "-row['x']",
        ],
    )
    def test_gate_settings_rejects_provably_numeric_condition(self, bad_condition: str) -> None:
        """Conditions that statically return a numeric value are rejected at config time.

        Regression for elspeth-f78e84f509: GateExecutor accepts only bool (-> 'true'/'false')
        or str (-> route label). A provably-numeric expression can never produce a valid
        route label and would TypeError at row execution — reject it at settings validation.
        """
        from pydantic import ValidationError

        from elspeth.core.config import GateSettings

        with pytest.raises(ValidationError, match="numeric value"):
            GateSettings(
                name="numeric_gate",
                input="source_out",
                condition=bad_condition,
                routes={"high": "a", "low": "b"},
            )

    @pytest.mark.parametrize(
        "ok_condition",
        [
            "row['tier']",  # unknown type — may be a route-label string
            "row.get('label')",
            "'gold' if row['vip'] else 'silver'",  # ternary of strings
            "row['a'] + row['b']",  # Add of two unknowns — could be str concat
            "row['name'] * 2",  # Mult — could be str repetition
            "row['s'] % row['t']",  # Mod — could be str formatting
        ],
    )
    def test_gate_settings_accepts_ambiguous_or_string_condition(self, ok_condition: str) -> None:
        """Conservative: only PROVABLY-numeric conditions are rejected; ambiguous/string ones pass."""
        from elspeth.core.config import GateSettings

        gate = GateSettings(
            name="route_gate",
            input="source_out",
            condition=ok_condition,
            routes={"gold": "a", "silver": "b"},
        )
        assert gate.condition == ok_condition

    def test_gate_settings_invalid_condition_syntax(self) -> None:
        """Condition must be valid Python syntax."""
        from elspeth.core.config import GateSettings

        with pytest.raises(ValidationError) as exc_info:
            GateSettings(
                name="bad_gate",
                input="source_out",
                condition="row['x' >=",  # Invalid syntax
                routes={"yes": "next_step"},
            )
        assert "Invalid condition syntax" in str(exc_info.value)

    def test_gate_settings_forbidden_condition_construct(self) -> None:
        """Condition must not contain forbidden constructs."""
        from elspeth.core.config import GateSettings

        # Lambda is forbidden
        with pytest.raises(ValidationError) as exc_info:
            GateSettings(
                name="bad_gate",
                input="source_out",
                condition="(lambda x: x)(row['field'])",
                routes={"yes": "next_step"},
            )
        assert "Forbidden construct" in str(exc_info.value)

    def test_gate_settings_forbidden_name_in_condition(self) -> None:
        """Condition must only use 'row' as a name."""
        from elspeth.core.config import GateSettings

        with pytest.raises(ValidationError) as exc_info:
            GateSettings(
                name="bad_gate",
                input="source_out",
                condition="os.system('rm -rf /')",  # Forbidden name
                routes={"yes": "next_step"},
            )
        assert "Forbidden" in str(exc_info.value)

    def test_gate_settings_empty_routes(self) -> None:
        """Routes must have at least one entry."""
        from elspeth.core.config import GateSettings

        with pytest.raises(ValidationError) as exc_info:
            GateSettings(
                name="bad_gate",
                input="source_out",
                condition="row['x'] > 0",
                routes={},
            )
        assert "at least one entry" in str(exc_info.value)

    def test_gate_settings_routes_max_length_enforced(self) -> None:
        """Routes map is capped to prevent unbounded fan-out configuration."""
        from elspeth.core.config import GateSettings

        oversized_routes = {f"route_{i}": f"dest_{i}" for i in range(33)}
        with pytest.raises(ValidationError, match="at most 32"):
            GateSettings(
                name="oversized_routes_gate",
                input="source_out",
                condition="row['category']",
                routes=oversized_routes,
            )

    def test_gate_settings_hyphenated_sink_destination_accepted(self) -> None:
        """Route destination can be any sink name, including hyphenated.

        Regression test for gate-route-destination-name-validation-mismatch bug.
        Sink names don't need to match identifier pattern - they're just dict keys.
        The DAG builder validates that destinations are actual sink keys.
        """
        from elspeth.core.config import GateSettings

        # Hyphenated sink names should be accepted at GateSettings level
        gate = GateSettings(
            name="hyphen_gate",
            input="source_out",
            condition="row['x'] > 0",
            routes={"true": "output-sink", "false": "next_step"},
        )
        assert gate.routes["true"] == "output-sink"

    def test_gate_settings_numeric_prefix_sink_destination_accepted(self) -> None:
        """Route destination can start with numbers (not identifier-constrained).

        Sink names are dict keys, not Python identifiers. Names like "123_sink"
        are valid sink names and should be accepted in gate routes.
        """
        from elspeth.core.config import GateSettings

        gate = GateSettings(
            name="numeric_gate",
            input="source_out",
            condition="row['x'] > 0",
            routes={"true": "123_sink", "false": "next_step"},
        )
        assert gate.routes["true"] == "123_sink"

    def test_gate_settings_fork_requires_fork_to(self) -> None:
        """fork_to is required when routes use 'fork' destination."""
        from elspeth.core.config import GateSettings

        with pytest.raises(ValidationError) as exc_info:
            GateSettings(
                name="bad_gate",
                input="source_out",
                condition="True",
                routes={"all": "fork"},
                # Missing fork_to
            )
        assert "fork_to is required" in str(exc_info.value)

    def test_gate_settings_fork_to_requires_fork_route(self) -> None:
        """fork_to is only valid when a route destination is 'fork'."""
        from elspeth.core.config import GateSettings

        with pytest.raises(ValidationError) as exc_info:
            GateSettings(
                name="bad_gate",
                input="source_out",
                condition="row['x'] > 0",
                routes={"yes": "next_step"},
                fork_to=["path_a", "path_b"],  # No fork route
            )
        assert "fork_to is only valid" in str(exc_info.value)

    def test_gate_settings_empty_fork_to_list_is_rejected_at_settings(self) -> None:
        """``fork_to: []`` must be a settings rejection, not a builder crash.

        elspeth-2ed41f0a4a census (2026-08-17). ``validate_fork_to_labels``
        looped zero times over an empty list and ``validate_fork_consistency``
        only fires when a ``fork`` route is present, so ``fork_to: []`` with
        ordinary routes reached ``build_execution_graph``, where
        ``_GateEntry.__post_init__`` raised a bare ``ValueError`` — which the
        composer's graph phase does not catch, so the composer preflight
        surfaced a 500 instead of a structured verdict.
        """
        from elspeth.core.config import GateSettings

        with pytest.raises(ValidationError) as exc_info:
            GateSettings(
                name="bad_gate",
                input="source_out",
                condition="row['x'] > 0",
                routes={"true": "next_step", "false": "discard"},
                fork_to=[],
            )
        assert "fork_to must not be an empty list" in str(exc_info.value)

    def test_gate_settings_fork_to_max_length_enforced(self) -> None:
        """fork_to list is capped to prevent pathological branch explosions."""
        from elspeth.core.config import GateSettings

        oversized_branches = [f"path_{i}" for i in range(33)]
        with pytest.raises(ValidationError, match="at most 32"):
            GateSettings(
                name="oversized_fork_gate",
                input="source_out",
                condition="True",
                routes={"all": "fork"},
                fork_to=oversized_branches,
            )

    def test_gate_settings_valid_identifiers(self) -> None:
        """Valid identifier sink names are accepted."""
        from elspeth.core.config import GateSettings

        # Non-boolean condition allows custom route labels
        gate = GateSettings(
            name="multi_route",
            input="source_out",
            condition="row['category']",  # Returns string, not boolean
            routes={
                "a": "sink_a",
                "b": "Sink_B",
                "c": "_private_sink",
                "d": "next_stage",
            },
        )
        assert len(gate.routes) == 4

    def test_gate_settings_complex_condition(self) -> None:
        """Complex expressions are validated correctly."""
        from elspeth.core.config import GateSettings

        gate = GateSettings(
            name="complex_gate",
            input="source_out",
            condition="row['confidence'] >= 0.85 and row['category'] != 'spam'",
            routes={"true": "quality_ok", "false": "quarantine"},
        )
        assert "and" in gate.condition

    def test_gate_settings_reserved_route_label_rejected(self) -> None:
        """Route label 'continue' is reserved and must be rejected.

        Using 'continue' as a route label would cause edge_map collisions
        in the orchestrator (the DAG builder already uses 'continue' for
        edges between sequential nodes), leading to routing events being
        recorded against the wrong edge - corrupting the audit trail.
        """
        from elspeth.core.config import GateSettings

        with pytest.raises(ValidationError) as exc_info:
            GateSettings(
                name="bad_gate",
                input="source_out",
                condition="row['score'] >= 0.5",
                routes={"continue": "some_sink"},  # 'continue' as label is forbidden
            )
        assert "reserved" in str(exc_info.value).lower()
        assert "continue" in str(exc_info.value)

    def test_gate_settings_reserved_fork_branch_rejected(self) -> None:
        """Fork branch 'continue' is reserved and must be rejected.

        Fork branches become edge labels, so using 'continue' would cause
        the same edge_map collision issue as reserved route labels.
        """
        from elspeth.core.config import GateSettings

        with pytest.raises(ValidationError) as exc_info:
            GateSettings(
                name="bad_fork_gate",
                input="source_out",
                condition="True",
                routes={"all": "fork"},
                fork_to=["path_a", "continue"],  # 'continue' as branch is forbidden
            )
        assert "reserved" in str(exc_info.value).lower()
        assert "continue" in str(exc_info.value)

    def test_gate_settings_dunder_route_label_rejected(self) -> None:
        """Route labels starting with __ are reserved for system edges."""
        from elspeth.core.config import GateSettings

        with pytest.raises(ValidationError) as exc_info:
            GateSettings(
                name="bad_gate",
                input="source_out",
                condition="row['x'] > 0",
                routes={"__quarantine__": "some_sink", "true": "next_step"},
            )
        assert "__" in str(exc_info.value)
        assert "reserved" in str(exc_info.value).lower()

    def test_gate_settings_dunder_fork_branch_rejected(self) -> None:
        """Fork branches starting with __ are reserved for system edges."""
        from elspeth.core.config import GateSettings

        with pytest.raises(ValidationError) as exc_info:
            GateSettings(
                name="bad_gate",
                input="source_out",
                condition="True",
                routes={"all": "fork"},
                fork_to=["path_a", "__error_0__"],
            )
        assert "__" in str(exc_info.value)
        assert "reserved" in str(exc_info.value).lower()

    def test_gate_settings_continue_as_destination_rejected(self) -> None:
        """'continue' as a route destination has been removed."""
        from elspeth.core.config import GateSettings

        with pytest.raises(ValidationError, match="has been removed"):
            GateSettings(
                name="invalid_gate",
                input="source_out",
                condition="row['valid']",
                routes={"pass": "continue", "fail": "error_sink"},
            )

    def test_gate_settings_boolean_condition_requires_true_false_routes(self) -> None:
        """Boolean conditions must use 'true'/'false' route labels.

        A condition like `row['amount'] > 1000` evaluates to True or False,
        not 'above' or 'below'. Using arbitrary labels is a config error.
        """
        from elspeth.core.config import GateSettings

        # This should fail - using 'above'/'below' for a boolean condition
        with pytest.raises(ValidationError) as exc_info:
            GateSettings(
                name="threshold_gate",
                input="source_out",
                condition="row['amount'] > 1000",
                routes={"above": "high_sink", "below": "standard_sink"},
            )
        error_msg = str(exc_info.value)
        assert "boolean condition" in error_msg
        assert "true" in error_msg.lower()
        assert "false" in error_msg.lower()

    def test_gate_settings_non_boolean_allows_custom_routes(self) -> None:
        """Non-boolean conditions (field access, ternary) allow custom labels.

        A condition like `row['category']` returns the field value directly,
        so routes can be labeled with expected values like 'high'/'low'.
        """
        from elspeth.core.config import GateSettings

        # Field access returns string, not boolean
        gate = GateSettings(
            name="category_router",
            input="source_out",
            condition="row['priority']",
            routes={"high": "urgent_sink", "medium": "standard_sink", "low": "archive_sink"},
        )
        assert len(gate.routes) == 3

        # Ternary returns the branch value, not boolean
        gate2 = GateSettings(
            name="ternary_router",
            input="source_out",
            condition="'high' if row['score'] > 0.8 else 'low'",
            routes={"high": "priority_sink", "low": "default_sink"},
        )
        assert gate2.condition.startswith("'high'")


class TestElspethSettingsWithGates:
    """Tests for ElspethSettings with engine-level gates."""

    def test_elspeth_settings_gates_default_empty(self) -> None:
        """Gates defaults to empty list."""
        from elspeth.core.config import ElspethSettings

        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output"}},
            sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
        )
        assert settings.gates == []

    def test_elspeth_settings_with_gates(self) -> None:
        """ElspethSettings accepts gates configuration."""
        from elspeth.core.config import ElspethSettings

        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output"}},
            sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}, "review": {"plugin": "csv", "on_write_failure": "discard"}},
            gates=[
                {
                    "name": "quality_check",
                    "input": "source_out",
                    "condition": "row['confidence'] >= 0.85",
                    "routes": {"true": "quality_ok", "false": "review"},
                },
            ],
        )
        assert len(settings.gates) == 1
        assert settings.gates[0].name == "quality_check"

    def test_elspeth_settings_multiple_gates(self) -> None:
        """ElspethSettings accepts multiple gates."""
        from elspeth.core.config import ElspethSettings

        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output"}},
            sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
            gates=[
                {
                    "name": "gate_1",
                    "input": "source_out",
                    "condition": "row['x'] > 0",
                    "routes": {"true": "next_a", "false": "next_b"},
                },
                {
                    "name": "gate_2",
                    "input": "source_out",
                    "condition": "row['y'] < 100",
                    "routes": {"true": "next_c", "false": "next_d"},
                },
            ],
        )
        assert len(settings.gates) == 2
        assert settings.gates[0].name == "gate_1"
        assert settings.gates[1].name == "gate_2"

    def test_elspeth_settings_rejects_duplicate_gate_names(self) -> None:
        """Gate names must be unique - duplicates rejected at config time."""
        from pydantic import ValidationError

        from elspeth.core.config import ElspethSettings

        with pytest.raises(ValidationError, match=r"Node name 'my_gate' is used by both"):
            ElspethSettings(
                sources={"primary": {"plugin": "csv", "on_success": "output"}},
                sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
                gates=[
                    {
                        "name": "my_gate",
                        "input": "source_out",
                        "condition": "row['x'] > 0",
                        "routes": {"true": "next_stage", "false": "output"},
                    },
                    {
                        "name": "my_gate",  # Duplicate!
                        "input": "source_out",
                        "condition": "row['y'] > 0",
                        "routes": {"true": "next_stage", "false": "output"},
                    },
                ],
            )

    def test_elspeth_settings_rejects_multiple_duplicate_gate_pairs(self) -> None:
        """First duplicate gate name triggers rejection."""
        from pydantic import ValidationError

        from elspeth.core.config import ElspethSettings

        with pytest.raises(ValidationError, match=r"Node name 'gate_a' is used by both"):
            ElspethSettings(
                sources={"primary": {"plugin": "csv", "on_success": "output"}},
                sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
                gates=[
                    {"name": "gate_a", "input": "source_out", "condition": "True", "routes": {"true": "next_a", "false": "output"}},
                    {"name": "gate_a", "input": "source_out", "condition": "True", "routes": {"true": "next_a", "false": "output"}},
                    {"name": "gate_b", "input": "source_out", "condition": "True", "routes": {"true": "next_b", "false": "output"}},
                    {"name": "gate_b", "input": "source_out", "condition": "True", "routes": {"true": "next_b", "false": "output"}},
                    {"name": "gate_c", "input": "source_out", "condition": "True", "routes": {"true": "next_c", "false": "output"}},
                ],
            )

    def test_resolve_config_includes_gates(self) -> None:
        """resolve_config preserves gates configuration."""
        from elspeth.core.config import ElspethSettings, resolve_config

        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output"}},
            sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
            gates=[
                {
                    "name": "quality_check",
                    "input": "source_out",
                    "condition": "row['confidence'] >= 0.85",
                    "routes": {"true": "quality_ok", "false": "output"},
                },
            ],
        )

        resolved = resolve_config(settings)

        assert "gates" in resolved
        assert len(resolved["gates"]) == 1
        assert resolved["gates"][0]["name"] == "quality_check"
        assert resolved["gates"][0]["condition"] == "row['confidence'] >= 0.85"


class TestCoalesceSettings:
    """Test CoalesceSettings configuration model."""

    def test_coalesce_settings_basic(self) -> None:
        """Basic coalesce configuration should validate."""
        from elspeth.core.config import CoalesceSettings

        settings = CoalesceSettings(
            name="merge_results",
            branches=["path_a", "path_b"],
            policy="require_all",
            merge="union",
        )

        assert settings.name == "merge_results"
        assert settings.branches == {"path_a": "path_a", "path_b": "path_b"}
        assert settings.policy == "require_all"
        assert settings.merge == "union"
        assert settings.timeout_seconds is None
        assert settings.quorum_count is None

    def test_coalesce_settings_quorum_requires_count(self) -> None:
        """Quorum policy requires quorum_count."""
        from elspeth.core.config import CoalesceSettings

        with pytest.raises(ValidationError, match="quorum_count"):
            CoalesceSettings(
                name="quorum_merge",
                branches=["a", "b", "c"],
                policy="quorum",
                merge="union",
                # Missing quorum_count
            )

    def test_coalesce_settings_quorum_with_count(self) -> None:
        """Quorum policy with count should validate."""
        from elspeth.core.config import CoalesceSettings

        settings = CoalesceSettings(
            name="quorum_merge",
            branches=["a", "b", "c"],
            policy="quorum",
            merge="union",
            quorum_count=2,
        )

        assert settings.quorum_count == 2

    def test_coalesce_settings_best_effort_requires_timeout(self) -> None:
        """Best effort policy requires timeout."""
        from elspeth.core.config import CoalesceSettings

        with pytest.raises(ValidationError, match="timeout"):
            CoalesceSettings(
                name="best_effort_merge",
                branches=["a", "b"],
                policy="best_effort",
                merge="union",
                # Missing timeout_seconds
            )

    def test_coalesce_settings_nested_merge_strategy(self) -> None:
        """Nested merge strategy should validate."""
        from elspeth.core.config import CoalesceSettings

        settings = CoalesceSettings(
            name="nested_merge",
            branches=["sentiment", "entities"],
            policy="require_all",
            merge="nested",
        )

        assert settings.merge == "nested"

    def test_coalesce_settings_select_merge_strategy(self) -> None:
        """Select merge requires select_branch."""
        from elspeth.core.config import CoalesceSettings

        with pytest.raises(ValidationError, match="select_branch"):
            CoalesceSettings(
                name="select_merge",
                branches=["a", "b"],
                policy="require_all",
                merge="select",
                # Missing select_branch
            )

    def test_coalesce_settings_select_with_branch(self) -> None:
        """Select merge with branch should validate."""
        from elspeth.core.config import CoalesceSettings

        settings = CoalesceSettings(
            name="select_merge",
            branches=["primary", "fallback"],
            policy="require_all",
            merge="select",
            select_branch="primary",
        )

        assert settings.select_branch == "primary"

    def test_coalesce_settings_quorum_count_cannot_exceed_branches(self) -> None:
        """Quorum count cannot exceed number of branches."""
        from elspeth.core.config import CoalesceSettings

        with pytest.raises(ValidationError, match="cannot exceed"):
            CoalesceSettings(
                name="quorum_merge",
                branches=["a", "b"],
                policy="quorum",
                merge="union",
                quorum_count=3,  # More than 2 branches
            )

    def test_coalesce_settings_select_branch_must_be_in_branches(self) -> None:
        """Select branch must be one of the defined branches."""
        from elspeth.core.config import CoalesceSettings

        with pytest.raises(ValidationError, match="must be one of"):
            CoalesceSettings(
                name="select_merge",
                branches=["a", "b"],
                policy="require_all",
                merge="select",
                select_branch="c",  # Not in branches
            )

    def test_coalesce_settings_branches_minimum_two(self) -> None:
        """Branches must have at least 2 entries."""
        from elspeth.core.config import CoalesceSettings

        with pytest.raises(ValidationError):
            CoalesceSettings(
                name="single_branch",
                branches=["only_one"],
                policy="require_all",
                merge="union",
            )

    def test_coalesce_settings_timeout_must_be_positive(self) -> None:
        """Timeout must be positive when provided."""
        from elspeth.core.config import CoalesceSettings

        with pytest.raises(ValidationError):
            CoalesceSettings(
                name="bad_timeout",
                branches=["a", "b"],
                policy="best_effort",
                merge="union",
                timeout_seconds=0,
            )

    def test_coalesce_settings_quorum_count_must_be_positive(self) -> None:
        """Quorum count must be positive when provided."""
        from elspeth.core.config import CoalesceSettings

        with pytest.raises(ValidationError):
            CoalesceSettings(
                name="bad_quorum",
                branches=["a", "b", "c"],
                policy="quorum",
                merge="union",
                quorum_count=0,
            )

    def test_coalesce_settings_first_policy(self) -> None:
        """First policy should validate without additional requirements."""
        from elspeth.core.config import CoalesceSettings

        settings = CoalesceSettings(
            name="first_wins",
            branches=["fast_model", "slow_model"],
            policy="first",
            merge="union",
        )

        assert settings.policy == "first"

    def test_coalesce_settings_best_effort_with_timeout(self) -> None:
        """Best effort policy with timeout should validate."""
        from elspeth.core.config import CoalesceSettings

        settings = CoalesceSettings(
            name="best_effort_merge",
            branches=["a", "b"],
            policy="best_effort",
            merge="union",
            timeout_seconds=30.0,
        )

        assert settings.timeout_seconds == 30.0

    def test_coalesce_settings_timeout_negative_rejected(self) -> None:
        """Negative timeout values should be rejected."""
        from elspeth.core.config import CoalesceSettings

        with pytest.raises(ValidationError):
            CoalesceSettings(
                name="test",
                branches=["branch_a", "branch_b"],
                policy="best_effort",
                merge="union",
                timeout_seconds=-1.0,
            )

    def test_coalesce_settings_timeout_infinite_rejected(self) -> None:
        """Infinite timeout values should be rejected.

        `gt=0` lets `inf` through, and CoalesceExecutor.check_timeouts compares
        `elapsed > timeout_seconds` — so an infinite timeout silently disables
        the whole timeout sweep instead of bounding the wait.
        """
        from elspeth.core.config import CoalesceSettings

        with pytest.raises(ValidationError, match="finite"):
            CoalesceSettings(
                name="test",
                branches=["branch_a", "branch_b"],
                policy="best_effort",
                merge="union",
                timeout_seconds=float("inf"),
            )

    def test_coalesce_settings_nan_timeout_rejected(self) -> None:
        """NaN timeout values should be rejected."""
        from elspeth.core.config import CoalesceSettings

        with pytest.raises(ValidationError):
            CoalesceSettings(
                name="test",
                branches=["branch_a", "branch_b"],
                policy="best_effort",
                merge="union",
                timeout_seconds=float("nan"),
            )

    def test_coalesce_settings_quorum_count_negative_rejected(self) -> None:
        """Negative quorum count should be rejected."""
        from elspeth.core.config import CoalesceSettings

        with pytest.raises(ValidationError):
            CoalesceSettings(
                name="test",
                branches=["branch_a", "branch_b"],
                policy="quorum",
                merge="union",
                quorum_count=-1,
            )

    # --- ARCH-15: dict-format branches (per-branch transforms) ---

    def test_coalesce_branches_list_normalizes_to_dict(self) -> None:
        """List format [a, b] normalizes to identity dict {a: a, b: b}."""
        from elspeth.core.config import CoalesceSettings

        settings = CoalesceSettings(
            name="merge",
            branches=["path_a", "path_b"],
            policy="require_all",
            merge="union",
        )

        assert settings.branches == {"path_a": "path_a", "path_b": "path_b"}
        assert isinstance(settings.branches, dict)

    def test_coalesce_branches_dict_accepted(self) -> None:
        """Dict format {a: x, b: y} passes validation directly."""
        from elspeth.core.config import CoalesceSettings

        settings = CoalesceSettings(
            name="merge",
            branches={"path_a": "sentiment_done", "path_b": "entities_done"},
            policy="require_all",
            merge="nested",
        )

        assert settings.branches == {"path_a": "sentiment_done", "path_b": "entities_done"}

    def test_coalesce_branches_list_dict_equivalence(self) -> None:
        """List and identity dict produce the same result.

        branches: [a, b] and branches: {a: a, b: b} should be functionally
        identical after normalization.
        """
        from elspeth.core.config import CoalesceSettings

        from_list = CoalesceSettings(
            name="merge",
            branches=["path_a", "path_b"],
            policy="require_all",
            merge="union",
        )

        from_dict = CoalesceSettings(
            name="merge",
            branches={"path_a": "path_a", "path_b": "path_b"},
            policy="require_all",
            merge="union",
        )

        assert from_list.branches == from_dict.branches

    def test_coalesce_branches_list_rejects_duplicates(self) -> None:
        """Duplicate branch names in list form must be rejected.

        Without this check, dict comprehension {b: b for b in v} silently
        discards duplicates, hiding a config error.
        """
        from elspeth.core.config import CoalesceSettings

        with pytest.raises(ValidationError, match="Duplicate branch names"):
            CoalesceSettings(
                name="merge",
                branches=["path_a", "path_b", "path_a"],
                policy="require_all",
                merge="union",
            )

    def test_coalesce_branches_list_no_duplicates_ok(self) -> None:
        """Unique list branches should normalize correctly."""
        from elspeth.core.config import CoalesceSettings

        settings = CoalesceSettings(
            name="merge",
            branches=["path_a", "path_b"],
            policy="require_all",
            merge="union",
        )
        assert settings.branches == {"path_a": "path_a", "path_b": "path_b"}

    def test_coalesce_branches_dict_unaffected_by_dedup_check(self) -> None:
        """Dict-form branches bypass the duplicate check (keys inherently unique)."""
        from elspeth.core.config import CoalesceSettings

        settings = CoalesceSettings(
            name="merge",
            branches={"path_a": "path_a", "path_b": "path_b"},
            policy="require_all",
            merge="union",
        )
        assert settings.branches == {"path_a": "path_a", "path_b": "path_b"}

    def test_coalesce_branches_dict_rejects_keys_that_collide_after_trim(self) -> None:
        """Branch keys that collapse into one after trimming must be rejected.

        min_length=2 is checked on the raw dict, so untrimmed keys could
        silently shrink a 2-branch coalesce to 1 — the join contract changes
        meaning (require_all over one branch) with no diagnostic.
        """
        from elspeth.core.config import CoalesceSettings

        with pytest.raises(ValidationError, match="collide"):
            CoalesceSettings(
                name="merge",
                branches={"path_a": "x", " path_a": "y"},
                policy="require_all",
                merge="union",
            )

    def test_coalesce_branches_list_rejects_entries_that_collide_after_trim(self) -> None:
        """List entries differing only by whitespace must be rejected too.

        The raw strings differ, so normalize_branches' duplicate check passes
        and min_length=2 is satisfied — the collapse only happens at trim time.
        """
        from elspeth.core.config import CoalesceSettings

        with pytest.raises(ValidationError, match="collide"):
            CoalesceSettings(
                name="merge",
                branches=["path_a", " path_a"],
                policy="require_all",
                merge="union",
            )

    def test_coalesce_branches_dict_validates_values(self) -> None:
        """Invalid connection names in dict values should be rejected.

        Values are input connection names and must pass
        _validate_connection_or_sink_name validation.
        """
        from elspeth.core.config import CoalesceSettings

        with pytest.raises(ValidationError, match="invalid characters"):
            CoalesceSettings(
                name="merge",
                branches={"path_a": "valid_name", "path_b": "inv@lid!"},
                policy="require_all",
                merge="union",
            )

    def test_coalesce_select_branch_with_dict(self) -> None:
        """select_branch must be validated against dict keys, not values.

        With branches: {a: x, b: y}, select_branch must be "a" or "b"
        (branch identities), not "x" or "y" (input connections).
        """
        from elspeth.core.config import CoalesceSettings

        # Valid: select_branch is a key
        settings = CoalesceSettings(
            name="merge",
            branches={"primary": "enriched_primary", "fallback": "enriched_fallback"},
            policy="require_all",
            merge="select",
            select_branch="primary",
        )
        assert settings.select_branch == "primary"

        # Invalid: select_branch is a value, not a key
        with pytest.raises(ValidationError, match="must be one of"):
            CoalesceSettings(
                name="merge",
                branches={"primary": "enriched_primary", "fallback": "enriched_fallback"},
                policy="require_all",
                merge="select",
                select_branch="enriched_primary",
            )

    # --- union_collision_policy (Task 2 of coalesce field provenance fix) ---

    def test_coalesce_settings_union_collision_policy_default(self) -> None:
        """Omitting union_collision_policy defaults to 'last_wins' (preserves existing behavior)."""
        from elspeth.core.config import CoalesceSettings

        settings = CoalesceSettings(
            name="merge_results",
            branches=["path_a", "path_b"],
            policy="require_all",
            merge="union",
        )

        assert settings.union_collision_policy == "last_wins"

    def test_coalesce_settings_union_collision_policy_explicit_last_wins(self) -> None:
        """Explicitly setting 'last_wins' validates cleanly."""
        from elspeth.core.config import CoalesceSettings

        settings = CoalesceSettings(
            name="merge_results",
            branches=["path_a", "path_b"],
            policy="require_all",
            merge="union",
            union_collision_policy="last_wins",
        )

        assert settings.union_collision_policy == "last_wins"

    def test_coalesce_settings_union_collision_policy_first_wins(self) -> None:
        """Setting 'first_wins' validates cleanly."""
        from elspeth.core.config import CoalesceSettings

        settings = CoalesceSettings(
            name="merge_results",
            branches=["path_a", "path_b"],
            policy="require_all",
            merge="union",
            union_collision_policy="first_wins",
        )

        assert settings.union_collision_policy == "first_wins"

    def test_coalesce_settings_union_collision_policy_fail(self) -> None:
        """Setting 'fail' validates cleanly."""
        from elspeth.core.config import CoalesceSettings

        settings = CoalesceSettings(
            name="merge_results",
            branches=["path_a", "path_b"],
            policy="require_all",
            merge="union",
            union_collision_policy="fail",
        )

        assert settings.union_collision_policy == "fail"

    def test_coalesce_settings_union_collision_policy_invalid_raises(self) -> None:
        """Invalid union_collision_policy values are rejected by the Literal type at parse time."""
        from elspeth.core.config import CoalesceSettings

        with pytest.raises(ValidationError):
            CoalesceSettings(
                name="merge_results",
                branches=["path_a", "path_b"],
                policy="require_all",
                merge="union",
                union_collision_policy="crash",
            )


class TestElspethSettingsWithCoalesce:
    """Tests for ElspethSettings with coalesce configuration."""

    def test_elspeth_settings_with_coalesce(self) -> None:
        """ElspethSettings should accept coalesce configuration."""
        from elspeth.core.config import (
            CoalesceSettings,
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )

        settings = ElspethSettings(
            sources={"primary": SourceSettings(plugin="csv_local", on_success="default", options={"path": "test.csv"})},
            sinks={"default": SinkSettings(plugin="csv", on_write_failure="discard", options={"path": "out.csv"})},
            coalesce=[
                CoalesceSettings(
                    name="merge_results",
                    branches=["path_a", "path_b"],
                    policy="require_all",
                    merge="union",
                ),
            ],
        )

        assert len(settings.coalesce) == 1
        assert settings.coalesce[0].name == "merge_results"

    def test_elspeth_settings_coalesce_default_empty(self) -> None:
        """Coalesce defaults to empty list."""
        from elspeth.core.config import ElspethSettings

        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output"}},
            sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
        )
        assert settings.coalesce == []

    def test_resolve_config_includes_coalesce(self) -> None:
        """resolve_config preserves coalesce configuration."""
        from elspeth.core.config import ElspethSettings, resolve_config

        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output"}},
            sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
            coalesce=[
                {
                    "name": "merge_results",
                    "branches": ["path_a", "path_b"],
                    "policy": "require_all",
                    "merge": "union",
                },
            ],
        )

        resolved = resolve_config(settings)

        assert "coalesce" in resolved
        assert len(resolved["coalesce"]) == 1
        assert resolved["coalesce"][0]["name"] == "merge_results"

    def test_elspeth_settings_rejects_duplicate_coalesce_names(self) -> None:
        """Coalesce names must be unique - duplicates rejected at config time."""
        from pydantic import ValidationError

        from elspeth.core.config import ElspethSettings

        with pytest.raises(ValidationError, match=r"Node name 'my_coalesce' is used by both"):
            ElspethSettings(
                sources={"primary": {"plugin": "csv", "on_success": "output"}},
                sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
                coalesce=[
                    {
                        "name": "my_coalesce",
                        "branches": ["path_a", "path_b"],
                        "policy": "require_all",
                        "merge": "union",
                    },
                    {
                        "name": "my_coalesce",  # Duplicate!
                        "branches": ["path_c", "path_d"],
                        "policy": "require_all",
                        "merge": "union",
                    },
                ],
            )

    def test_elspeth_settings_rejects_multiple_duplicate_coalesce_pairs(self) -> None:
        """First duplicate coalesce name triggers rejection."""
        from pydantic import ValidationError

        from elspeth.core.config import ElspethSettings

        with pytest.raises(ValidationError, match=r"Node name 'coal_a' is used by both"):
            ElspethSettings(
                sources={"primary": {"plugin": "csv", "on_success": "output"}},
                sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
                coalesce=[
                    {"name": "coal_a", "branches": ["x", "y"], "policy": "require_all", "merge": "union"},
                    {"name": "coal_a", "branches": ["x", "y"], "policy": "require_all", "merge": "union"},
                    {"name": "coal_b", "branches": ["x", "y"], "policy": "require_all", "merge": "union"},
                    {"name": "coal_b", "branches": ["x", "y"], "policy": "require_all", "merge": "union"},
                    {"name": "coal_c", "branches": ["x", "y"], "policy": "require_all", "merge": "union"},
                ],
            )


class TestRunModeSettings:
    """Tests for run_mode configuration."""

    def test_run_mode_defaults_to_live(self) -> None:
        """Default run_mode should be 'live'."""
        from elspeth.contracts.enums import RunMode
        from elspeth.core.config import ElspethSettings

        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output"}},
            sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
        )

        assert settings.run_mode == RunMode.LIVE

    def test_run_mode_accepts_all_valid_modes(self) -> None:
        """All valid run_mode values should be accepted."""
        from elspeth.contracts.enums import RunMode
        from elspeth.core.config import ElspethSettings

        # Test live mode
        settings_live = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output"}},
            sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
            run_mode=RunMode.LIVE,
        )
        assert settings_live.run_mode == RunMode.LIVE

        # Test replay mode (with required source run ID)
        settings_replay = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output"}},
            sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
            run_mode=RunMode.REPLAY,
            replay_from="run-abc123",
        )
        assert settings_replay.run_mode == RunMode.REPLAY

        # Test verify mode (with required source run ID)
        settings_verify = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output"}},
            sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
            run_mode=RunMode.VERIFY,
            replay_from="run-abc123",
        )
        assert settings_verify.run_mode == RunMode.VERIFY

    def test_replay_mode_requires_source_run_id(self) -> None:
        """Replay mode requires replay_from."""
        from elspeth.contracts.enums import RunMode
        from elspeth.core.config import ElspethSettings

        with pytest.raises(ValidationError) as exc_info:
            ElspethSettings(
                sources={"primary": {"plugin": "csv", "on_success": "output"}},
                sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
                run_mode=RunMode.REPLAY,
                # Missing replay_from
            )

        assert "replay_from is required" in str(exc_info.value)
        assert "replay" in str(exc_info.value)

    def test_verify_mode_requires_source_run_id(self) -> None:
        """Verify mode requires replay_from."""
        from elspeth.contracts.enums import RunMode
        from elspeth.core.config import ElspethSettings

        with pytest.raises(ValidationError) as exc_info:
            ElspethSettings(
                sources={"primary": {"plugin": "csv", "on_success": "output"}},
                sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
                run_mode=RunMode.VERIFY,
                # Missing replay_from
            )

        assert "replay_from is required" in str(exc_info.value)
        assert "verify" in str(exc_info.value)

    def test_live_mode_ignores_source_run_id(self) -> None:
        """Live mode doesn't require replay_from."""
        from elspeth.contracts.enums import RunMode
        from elspeth.core.config import ElspethSettings

        # Should not raise - live mode doesn't require source run ID
        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output"}},
            sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
            run_mode=RunMode.LIVE,
            # No replay_from
        )

        assert settings.run_mode == RunMode.LIVE
        assert settings.replay_from is None

    def test_live_mode_accepts_source_run_id(self) -> None:
        """Live mode accepts but ignores replay_from if provided."""
        from elspeth.contracts.enums import RunMode
        from elspeth.core.config import ElspethSettings

        # Should not raise - live mode ignores source run ID
        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output"}},
            sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
            run_mode=RunMode.LIVE,
            replay_from="run-abc123",  # Provided but not required
        )

        assert settings.run_mode == RunMode.LIVE
        assert settings.replay_from == "run-abc123"

    def test_run_mode_invalid_value_rejected(self) -> None:
        """Invalid run_mode values should be rejected."""
        from elspeth.core.config import ElspethSettings

        with pytest.raises(ValidationError):
            ElspethSettings(
                sources={"primary": {"plugin": "csv", "on_success": "output"}},
                sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
                run_mode="invalid_mode",
            )

    def test_resolve_config_includes_run_mode(self) -> None:
        """resolve_config includes run_mode settings."""
        from elspeth.contracts.enums import RunMode
        from elspeth.core.config import ElspethSettings, resolve_config

        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output"}},
            sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
            run_mode=RunMode.REPLAY,
            replay_from="run-abc123",
        )

        resolved = resolve_config(settings)

        assert "run_mode" in resolved
        assert resolved["run_mode"] == "replay"
        assert "replay_from" in resolved
        assert resolved["replay_from"] == "run-abc123"


class TestSinkNameCasing:
    """Sink names must be lowercase - explicit validation, no silent transformation."""

    def test_lowercase_sink_names_accepted(self) -> None:
        """Lowercase sink names work as expected."""
        from elspeth.core.config import ElspethSettings

        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output"}},
            sinks={
                "output": {"plugin": "csv", "on_write_failure": "discard"},
                "error_sink": {"plugin": "csv", "on_write_failure": "discard"},
            },
        )
        assert "output" in settings.sinks
        assert "error_sink" in settings.sinks

    def test_mixed_case_sink_name_rejected(self) -> None:
        """Mixed-case sink names are rejected with a clear error."""
        from elspeth.core.config import ElspethSettings

        with pytest.raises(ValidationError) as exc_info:
            ElspethSettings(
                sources={"primary": {"plugin": "csv", "on_success": "output"}},
                sinks={"MyOutput": {"plugin": "csv", "on_write_failure": "discard"}},
            )
        # Should get a clear error about lowercase requirement
        assert "lowercase" in str(exc_info.value).lower()
        assert "MyOutput" in str(exc_info.value)

    def test_uppercase_sink_name_rejected(self) -> None:
        """Uppercase sink names are rejected with a clear error."""
        from elspeth.core.config import ElspethSettings

        with pytest.raises(ValidationError) as exc_info:
            ElspethSettings(
                sources={"primary": {"plugin": "csv", "on_success": "output"}},
                sinks={"OUTPUT": {"plugin": "csv", "on_write_failure": "discard"}},
            )
        assert "lowercase" in str(exc_info.value).lower()

    def test_sink_name_with_underscores_and_numbers_accepted(self) -> None:
        """Sink names with underscores and numbers are fine if lowercase."""
        from elspeth.core.config import ElspethSettings

        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output_v2"}},
            sinks={
                "output_v2": {"plugin": "csv", "on_write_failure": "discard"},
                "sink_123": {"plugin": "csv", "on_write_failure": "discard"},
            },
        )
        assert "output_v2" in settings.sinks
        assert "sink_123" in settings.sinks

    def test_default_sink_rejected(self) -> None:
        """default_sink is rejected by extra='forbid'."""
        from elspeth.core.config import ElspethSettings

        with pytest.raises(ValidationError) as exc_info:
            ElspethSettings(
                sources={"primary": {"plugin": "csv"}},
                sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
                **{"default_sink": "nonexistent"},  # testing rejected field
            )
        assert "default_sink" in str(exc_info.value)

    def test_load_settings_rejects_mixed_case_sink_names(self, tmp_path: Path) -> None:
        """Mixed-case sink names from YAML are rejected with helpful error."""
        from elspeth.config_loading import load_settings

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv
sinks:
  MyOutput:
    plugin: csv
    on_write_failure: discard
""")
        # Mixed-case sink names are preserved so validator catches them
        with pytest.raises(ValidationError) as exc_info:
            load_settings(config_file)
        error_msg = str(exc_info.value)
        assert "lowercase" in error_msg.lower()
        assert "MyOutput" in error_msg


class TestPluginConfigSchemaValidation:
    """PluginConfig.from_dict handles malformed schema gracefully."""

    def test_schema_null_gives_clear_error(self) -> None:
        """schema: null should give a clear error, not TypeError."""
        from elspeth.plugins.infrastructure.config_base import PluginConfig, PluginConfigError

        with pytest.raises(PluginConfigError) as exc_info:
            PluginConfig.from_dict({"schema": None})
        # Should mention schema must be a dict/mapping, not TypeError
        error_msg = str(exc_info.value).lower()
        assert "schema" in error_msg
        assert "dict" in error_msg or "mapping" in error_msg

    def test_schema_string_gives_clear_error(self) -> None:
        """schema: 'invalid' should give a clear error."""
        from elspeth.plugins.infrastructure.config_base import PluginConfig, PluginConfigError

        with pytest.raises(PluginConfigError) as exc_info:
            PluginConfig.from_dict({"schema": "invalid"})
        error_msg = str(exc_info.value).lower()
        assert "schema" in error_msg

    def test_schema_list_gives_clear_error(self) -> None:
        """schema: [fields] should give a clear error."""
        from elspeth.plugins.infrastructure.config_base import PluginConfig, PluginConfigError

        with pytest.raises(PluginConfigError) as exc_info:
            PluginConfig.from_dict({"schema": ["fields"]})
        error_msg = str(exc_info.value).lower()
        assert "schema" in error_msg

    def test_schema_int_gives_clear_error(self) -> None:
        """schema: 123 should give a clear error."""
        from elspeth.plugins.infrastructure.config_base import PluginConfig, PluginConfigError

        with pytest.raises(PluginConfigError) as exc_info:
            PluginConfig.from_dict({"schema": 123})
        error_msg = str(exc_info.value).lower()
        assert "schema" in error_msg

    def test_valid_schema_dict_works(self) -> None:
        """Valid schema dict should work fine."""
        from elspeth.plugins.infrastructure.config_base import PluginConfig

        config = PluginConfig.from_dict({"schema": {"mode": "observed"}})
        assert config.schema_config is not None
        assert config.schema_config.is_observed
