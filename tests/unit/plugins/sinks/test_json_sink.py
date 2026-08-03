"""Tests for JSON sink plugin."""

import json
from pathlib import Path

import pytest

from elspeth.contracts import Determinism
from elspeth.contracts.plugin_context import PluginContext
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from tests.fixtures.base_classes import inject_write_failure
from tests.fixtures.factories import make_context
from tests.fixtures.landscape import make_factory, make_landscape_db

# Dynamic schema config for tests - PathConfig now requires schema
DYNAMIC_SCHEMA = {"mode": "observed"}


class TestJSONSink:
    """Tests for JSONSink plugin."""

    @pytest.fixture
    def ctx(self) -> PluginContext:
        """Create a minimal plugin context."""
        db = make_landscape_db()
        factory = make_factory(db)
        return make_context(landscape=factory.plugin_audit_writer())

    def test_json_array_fail_if_exists_collision_policy_refuses_existing_target(
        self,
        tmp_path: Path,
    ) -> None:
        """Explicit fail-if-exists mode must not replace an existing JSON output."""
        from elspeth.plugins.sinks.json_sink import JSONSink

        output_file = tmp_path / "output.json"
        output_file.write_text('[{"id": 0}]')
        with pytest.raises(FileExistsError, match="already exists"):
            JSONSink(
                {
                    "path": str(output_file),
                    "format": "json",
                    "schema": DYNAMIC_SCHEMA,
                    "collision_policy": "fail_if_exists",
                }
            )

        assert json.loads(output_file.read_text()) == [{"id": 0}]

    def test_json_array_append_mode_is_rejected(self, tmp_path: Path) -> None:
        """JSON array format must reject append mode to prevent silent overwrite."""
        from elspeth.plugins.sinks.json_sink import JSONSink

        output_file = tmp_path / "output.json"
        with pytest.raises(PluginConfigError, match="does not support mode='append'"):
            JSONSink(
                {
                    "path": str(output_file),
                    "format": "json",
                    "mode": "append",
                    "schema": DYNAMIC_SCHEMA,
                }
            )

    def test_json_extension_append_mode_is_rejected(self, tmp_path: Path) -> None:
        """Auto-detected JSON array format must also reject append mode."""
        from elspeth.plugins.sinks.json_sink import JSONSink

        output_file = tmp_path / "output.json"
        with pytest.raises(PluginConfigError, match="does not support mode='append'"):
            JSONSink(
                {
                    "path": str(output_file),
                    "mode": "append",
                    "schema": DYNAMIC_SCHEMA,
                }
            )

    def test_unknown_encoding_is_rejected_before_sink_construction(self, tmp_path: Path) -> None:
        """An unavailable codec must fail owned config validation, before publication."""
        from elspeth.plugins.sinks.json_sink import JSONSink, JSONSinkConfig

        config = {
            "path": str(tmp_path / "sensitive-output-name.json"),
            "schema": DYNAMIC_SCHEMA,
            "encoding": "definitely-not-a-real-codec",
        }

        with pytest.raises(PluginConfigError, match=r"encoding: Value error, unknown encoding") as config_error:
            JSONSinkConfig.from_dict(config, plugin_name="json")
        with pytest.raises(PluginConfigError, match=r"encoding: Value error, unknown encoding") as sink_error:
            JSONSink(config)

        assert "sensitive-output-name" not in str(config_error.value)
        assert str(sink_error.value) == str(config_error.value)

    @pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf8"])
    def test_available_text_encoding_is_accepted(self, tmp_path: Path, encoding: str) -> None:
        """Standard, non-default, and normalized alias spellings remain valid."""
        from elspeth.plugins.sinks.json_sink import JSONSink, JSONSinkConfig

        config = {
            "path": str(tmp_path / "output.json"),
            "schema": DYNAMIC_SCHEMA,
            "encoding": encoding,
        }

        validated = JSONSinkConfig.from_dict(config, plugin_name="json")
        sink = JSONSink(config)

        assert validated.encoding == encoding
        assert sink._encoding == encoding

    @pytest.mark.parametrize("encoding", [None, True, 65001, b"utf-8"])
    def test_encoding_requires_an_exact_string(self, tmp_path: Path, encoding: object) -> None:
        """Codec validation must not coerce null, booleans, numbers, or bytes."""
        from elspeth.plugins.sinks.json_sink import JSONSinkConfig

        config = {
            "path": str(tmp_path / "sensitive-output-name.json"),
            "schema": DYNAMIC_SCHEMA,
            "encoding": encoding,
        }

        with pytest.raises(PluginConfigError, match=r"encoding: Input should be a valid string") as error:
            JSONSinkConfig.from_dict(config, plugin_name="json")

        assert "sensitive-output-name" not in str(error.value)

    def test_has_plugin_version(self) -> None:
        """JSONSink has plugin_version attribute."""
        from elspeth.plugins.sinks.json_sink import JSONSink

        sink = inject_write_failure(JSONSink({"path": "/tmp/test.json", "schema": DYNAMIC_SCHEMA}))
        assert sink.plugin_version == "1.0.0"

    def test_has_determinism(self) -> None:
        """JSONSink has determinism attribute."""
        from elspeth.plugins.sinks.json_sink import JSONSink

        sink = inject_write_failure(JSONSink({"path": "/tmp/test.json", "schema": DYNAMIC_SCHEMA}))
        assert sink.determinism == Determinism.IO_WRITE


class TestJSONSinkNonFiniteRejection:
    """Non-finite/non-serializable values are per-row Tier-2 faults.

    A single row's value that cannot be encoded as standard JSON (NaN/Infinity, or
    a non-serializable object) must NOT be emitted as non-standard JSON, and must NOT
    abort the whole batch. With on_write_failure configured the offending row is
    diverted (recorded + routed) and the remaining rows are written; with no
    on_write_failure configured the sink fails closed (the framework refuses to guess
    the operator's discard-vs-preserve intent).
    """

    @pytest.fixture
    def ctx(self) -> PluginContext:
        db = make_landscape_db()
        factory = make_factory(db)
        return make_context(landscape=factory.plugin_audit_writer())
