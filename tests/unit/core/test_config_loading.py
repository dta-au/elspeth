"""Configuration loading, normalization, resolution, and expansion tests."""

from pathlib import Path

import pytest
from pydantic import ValidationError


class TestLoadSettings:
    """Test Dynaconf-based settings loading."""

    def test_load_from_yaml_file(self, tmp_path: Path) -> None:
        from elspeth.config_loading import load_settings

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: "csv"
    on_success: output
    options:
      path: "input.csv"
sinks:
  output:
    plugin: "csv"
    on_write_failure: discard
    options:
      path: "output.csv"

retry:
  max_attempts: 5
""")
        settings = load_settings(config_file)
        assert settings.sources["primary"].plugin == "csv"
        assert settings.sources["primary"].options == {"path": "input.csv"}
        assert settings.retry.max_attempts == 5

    def test_load_with_env_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from elspeth.config_loading import load_settings

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: "csv"
    on_success: output
sinks:
  output:
    plugin: "csv"
    on_write_failure: discard

""")
        # Environment variable should override YAML
        # Multi-source config nests sources under named roots, so the env-override
        # segment must name the source: ELSPETH_SOURCES__<NAME>__PLUGIN. The bare
        # ELSPETH_SOURCE__ form is a single-source relic that no longer maps to any
        # config key (proven: the plural+named form overrides; the singular does not).
        monkeypatch.setenv("ELSPETH_SOURCES__PRIMARY__PLUGIN", "json")

        settings = load_settings(config_file)
        assert settings.sources["primary"].plugin == "json"

    def test_load_sink_env_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variable overrides for sink settings should work.

        Regression test for P2-2026-02-02: When _lowercase_schema_keys was updated
        to preserve sink names, it incorrectly preserved ALL keys inside sinks,
        including schema keys like 'plugin'. Dynaconf uppercases env var segments,
        so ELSPETH_SINKS__OUTPUT__PLUGIN becomes {sinks: {OUTPUT: {PLUGIN: csv}}}.
        If _lowercase_schema_keys doesn't lowercase PLUGIN, Pydantic sees an unknown
        field and reports 'plugin' as missing.
        """
        from elspeth.config_loading import load_settings

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv
    on_success: output
sinks:
  output:
    plugin: csv
    on_write_failure: discard

""")
        # Environment variable should override sink plugin
        monkeypatch.setenv("ELSPETH_SINKS__OUTPUT__PLUGIN", "json")

        settings = load_settings(config_file)
        assert settings.sinks["output"].plugin == "json"

    def test_load_sink_defined_only_via_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sinks defined entirely via environment variables should work.

        Regression test for P2-2026-02-02: When a sink is defined purely from
        env vars (not merged with YAML), Dynaconf produces uppercase keys
        that must be lowercased for Pydantic validation.
        """
        from elspeth.config_loading import load_settings

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv
    on_success: output

""")
        # Define entire sink via env vars
        monkeypatch.setenv("ELSPETH_SINKS__OUTPUT__PLUGIN", "csv")
        monkeypatch.setenv("ELSPETH_SINKS__OUTPUT__ON_WRITE_FAILURE", "discard")

        settings = load_settings(config_file)
        assert "output" in settings.sinks
        assert settings.sinks["output"].plugin == "csv"

    def test_load_settings_rejects_report_assemble_title_placeholder_before_env_expansion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from elspeth.config_loading import load_settings

        monkeypatch.setenv("REPORT_SECRET", "expanded-host-secret")
        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv
    on_success: text_rows
    options:
      path: input.csv
transforms:
  - name: report
    plugin: report_assemble
    input: text_rows
    on_success: output
    on_error: discard
    options:
      schema:
        mode: observed
      text_field: line
      title: "${REPORT_SECRET}"
sinks:
  output:
    plugin: csv
    on_write_failure: discard
    options:
      path: output.csv
""")

        with pytest.raises(ValueError, match=r"report_assemble.*title.*environment-variable placeholders"):
            load_settings(config_file)

    def test_load_settings_from_yaml_string_rejects_report_assemble_join_with_placeholder_before_env_expansion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from elspeth.config_loading import load_settings_from_yaml_string

        monkeypatch.setenv("REPORT_JOINER", "expanded-host-secret")

        with pytest.raises(ValueError, match=r"report_assemble.*join_with.*environment-variable placeholders"):
            load_settings_from_yaml_string(
                """
sources:
  primary:
    plugin: csv
    on_success: text_rows
    options:
      path: input.csv
transforms:
  - name: report
    plugin: report_assemble
    input: text_rows
    on_success: output
    on_error: discard
    options:
      schema:
        mode: observed
      text_field: line
      join_with: "${REPORT_JOINER}"
sinks:
  output:
    plugin: csv
    on_write_failure: discard
    options:
      path: output.csv
"""
            )

    def test_load_env_override_lowercases_structural_schema_option_keys(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Environment overrides under options.schema must reach runtime schema config."""
        from elspeth.config_loading import load_settings

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv
    on_success: output
sinks:
  output:
    plugin: csv
    on_write_failure: discard

""")
        monkeypatch.setenv("ELSPETH_SOURCES__PRIMARY__OPTIONS__SCHEMA__MODE", "observed")

        settings = load_settings(config_file)

        assert settings.sources["primary"].options == {"schema": {"mode": "observed"}}
        assert "SCHEMA" not in settings.sources["primary"].options

    def test_load_env_override_merges_with_existing_schema_option(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Environment overrides for options.schema fields must override YAML schema fields."""
        from elspeth.config_loading import load_settings

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv
    on_success: output
    options:
      schema:
        mode: fixed
        fields:
          - name: customer_id
            type: str
sinks:
  output:
    plugin: csv
    on_write_failure: discard

""")
        monkeypatch.setenv("ELSPETH_SOURCES__PRIMARY__OPTIONS__SCHEMA__MODE", "flexible")

        settings = load_settings(config_file)

        assert settings.sources["primary"].options["schema"] == {
            "mode": "flexible",
            "fields": [{"name": "customer_id", "type": "str"}],
        }
        assert "SCHEMA" not in settings.sources["primary"].options

    def test_load_validates_schema(self, tmp_path: Path) -> None:
        from elspeth.config_loading import load_settings

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: "csv"
    on_success: output
sinks:
  output:
    plugin: "csv"
    on_write_failure: discard

concurrency:
  max_workers: -1
""")
        with pytest.raises(ValidationError):
            load_settings(config_file)

    def test_load_missing_required_field(self, tmp_path: Path) -> None:
        from elspeth.config_loading import load_settings

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
retry:
  max_attempts: 5
""")
        # datasource, sinks, output_sink are required
        with pytest.raises(ValidationError):
            load_settings(config_file)

    def test_load_missing_file_raises_file_not_found(self, tmp_path: Path) -> None:
        from elspeth.config_loading import load_settings

        missing_file = tmp_path / "nonexistent.yaml"
        with pytest.raises(FileNotFoundError, match="Config file not found"):
            load_settings(missing_file)


class TestLoadSettingsArchitecture:
    """load_settings() parses architecture-compliant YAML."""

    def test_load_readme_example(self, tmp_path: Path) -> None:
        """Load config with config-driven gates."""
        from elspeth.config_loading import load_settings

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv_local
    on_success: results
    options:
      path: data/submissions.csv

sinks:
  results:
    plugin: csv
    on_write_failure: discard
    options:
      path: output/results.csv
  flagged:
    plugin: csv
    on_write_failure: discard
    options:
      path: output/flagged_for_review.csv

gates:
  - name: safety_check
    input: source_out
    condition: "row['suspicious'] == True"
    routes:
      "true": flagged
      "false": results



landscape:
  enabled: true
  backend: sqlite
  url: sqlite:///./state/audit.db
""")

        settings = load_settings(config_file)

        assert settings.sources["primary"].plugin == "csv_local"
        assert settings.sources["primary"].options["path"] == "data/submissions.csv"
        assert len(settings.sinks) == 2
        assert len(settings.gates) == 1
        assert settings.gates[0].name == "safety_check"
        assert settings.landscape.backend == "sqlite"

    def test_load_minimal_config(self, tmp_path: Path) -> None:
        """Minimal valid configuration."""
        from elspeth.config_loading import load_settings

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv
    on_success: output

sinks:
  output:
    plugin: csv
    on_write_failure: discard


""")

        settings = load_settings(config_file)

        assert settings.sources["primary"].plugin == "csv"
        assert settings.landscape.enabled is True  # Default
        assert settings.concurrency.max_workers == 4  # Default

    def test_load_default_sink_in_yaml_rejected(self, tmp_path: Path) -> None:
        """default_sink in YAML is rejected as unknown key."""
        from elspeth.config_loading import load_settings

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv
    on_success: results

sinks:
  results:
    plugin: csv
    on_write_failure: discard

default_sink: results
""")

        with pytest.raises(ValueError) as exc_info:
            load_settings(config_file)

        error_msg = str(exc_info.value)
        assert "default_sink" in error_msg


class TestResolveConfig:
    """Tests for resolve_config function."""

    def test_resolve_config_returns_dict(self) -> None:
        """resolve_config converts ElspethSettings to dict."""
        from elspeth.core.config import ElspethSettings, resolve_config

        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output", "options": {"path": "input.csv"}}},
            sinks={"output": {"plugin": "csv", "on_write_failure": "discard", "options": {"path": "output.csv"}}},
        )

        resolved = resolve_config(settings)

        assert isinstance(resolved, dict)
        assert "sources" in resolved
        assert resolved["sources"]["primary"]["plugin"] == "csv"

    def test_resolve_config_includes_defaults(self) -> None:
        """resolve_config includes default values for audit completeness."""
        from elspeth.core.config import ElspethSettings, resolve_config

        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output"}},
            sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
        )

        resolved = resolve_config(settings)

        # Should include defaults
        assert "landscape" in resolved
        assert resolved["landscape"]["enabled"] is True
        assert "concurrency" in resolved
        assert resolved["concurrency"]["max_workers"] == 4
        assert "retry" in resolved
        assert resolved["retry"]["max_attempts"] == 3

    def test_resolve_config_json_serializable(self) -> None:
        """resolve_config output is JSON-serializable for Landscape storage."""
        import json

        from elspeth.core.config import ElspethSettings, resolve_config

        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output", "options": {"path": "input.csv"}}},
            sinks={"output": {"plugin": "csv", "on_write_failure": "discard", "options": {"path": "output.csv"}}},
        )

        resolved = resolve_config(settings)

        # Should not raise - must be JSON serializable
        json_str = json.dumps(resolved)
        assert isinstance(json_str, str)
        assert len(json_str) > 0

    def test_resolve_config_preserves_transforms(self) -> None:
        """resolve_config includes row_plugins configuration."""
        from elspeth.core.config import ElspethSettings, resolve_config

        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output"}},
            sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
            transforms=[
                {
                    "name": "t1",
                    "plugin": "field_mapper",
                    "input": "source_out",
                    "on_success": "output",
                    "on_error": "discard",
                    "options": {"mapping": {"a": "b"}},
                },
            ],
        )

        resolved = resolve_config(settings)

        assert "transforms" in resolved
        assert len(resolved["transforms"]) == 1
        assert resolved["transforms"][0]["plugin"] == "field_mapper"


class TestLoadSettingsWithGates:
    """Tests for loading YAML with gates configuration."""

    def test_load_settings_with_gates(self, tmp_path: Path) -> None:
        """Load YAML with gates section."""
        from elspeth.config_loading import load_settings

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv
    on_success: output

sinks:
  output:
    plugin: csv
    on_write_failure: discard
  review:
    plugin: csv
    on_write_failure: discard



gates:
  - name: quality_check
    input: source_out
    condition: "row['confidence'] >= 0.85"
    routes:
      "true": quality_ok
      "false": review
""")
        settings = load_settings(config_file)

        assert len(settings.gates) == 1
        assert settings.gates[0].name == "quality_check"
        assert settings.gates[0].condition == "row['confidence'] >= 0.85"
        assert settings.gates[0].routes == {"true": "quality_ok", "false": "review"}

    def test_load_settings_with_fork_gate(self, tmp_path: Path) -> None:
        """Load YAML with fork gate."""
        from elspeth.config_loading import load_settings

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv
    on_success: output

sinks:
  output:
    plugin: csv
    on_write_failure: discard



gates:
  - name: parallel_analysis
    input: source_out
    condition: "True"
    routes:
      "true": fork
      "false": output
    fork_to:
      - path_a
      - path_b
""")
        settings = load_settings(config_file)

        assert len(settings.gates) == 1
        assert settings.gates[0].name == "parallel_analysis"
        assert settings.gates[0].routes == {"true": "fork", "false": "output"}
        assert settings.gates[0].fork_to == ["path_a", "path_b"]

    def test_load_settings_preserves_mixed_case_route_labels(self, tmp_path: Path) -> None:
        """Route labels preserve case through config loading.

        Regression test for P1-2026-02-05: _lowercase_schema_keys() was lowercasing
        gate route labels, causing routing failures when gate conditions returned
        mixed-case strings like "High" but routes had been lowercased to "high".
        """
        from elspeth.config_loading import load_settings

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv
    on_success: output

sinks:
  output:
    plugin: csv
    on_write_failure: discard
  urgent:
    plugin: csv
    on_write_failure: discard
  archive:
    plugin: csv
    on_write_failure: discard



gates:
  - name: priority_router
    input: source_out
    condition: "row['priority']"
    routes:
      High: urgent
      Medium: standard
      Low: archive
""")
        settings = load_settings(config_file)

        assert len(settings.gates) == 1
        gate = settings.gates[0]
        assert gate.name == "priority_router"
        # Route labels must preserve exact case - "High" not "high"
        assert gate.routes == {"High": "urgent", "Medium": "standard", "Low": "archive"}
        assert "High" in gate.routes
        assert "high" not in gate.routes

    def test_load_settings_preserves_schema_route_label(self, tmp_path: Path) -> None:
        """Route labels named SCHEMA are user data, not plugin schema options."""
        from elspeth.config_loading import load_settings

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv
    on_success: priority_router
sinks:
  output:
    plugin: csv
    on_write_failure: discard
  schema_sink:
    plugin: csv
    on_write_failure: discard
gates:
  - name: priority_router
    input: source_out
    condition: "row['priority']"
    routes:
      SCHEMA: schema_sink
      fallback: output
""")

        settings = load_settings(config_file)

        assert settings.gates[0].routes == {"SCHEMA": "schema_sink", "fallback": "output"}
        assert "schema" not in settings.gates[0].routes


class TestExpandTemplateFiles:
    """Tests for _expand_template_files function."""

    def test_template_option_materializer_registry_drives_plugin_collections(self, tmp_path: Path) -> None:
        from elspeth.core.template_materialization import FILE_BACKED_TEMPLATE_OPTION_KEYS, TemplateOptionMaterializer

        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "template.j2").write_text("Hello {{ row.name }}")
        (prompts_dir / "lookup.yaml").write_text("categories:\n  - Clothing\n")
        (prompts_dir / "system.txt").write_text("Use concise labels.")
        (prompts_dir / "products.csv").write_text("sku,description\nhats,A fine hat\n")

        materializer = TemplateOptionMaterializer(tmp_path / "settings.yaml")
        config = materializer.materialize_config(
            {
                "transforms": [
                    {
                        "name": "classify",
                        "options": {
                            "template_file": "prompts/template.j2",
                            "lookup_file": "prompts/lookup.yaml",
                            "reference_file": "prompts/products.csv",
                        },
                    }
                ],
                "aggregations": [
                    {
                        "name": "summarize",
                        "options": {
                            "system_prompt_file": "prompts/system.txt",
                        },
                    }
                ],
            }
        )

        assert frozenset({"template_file", "lookup_file", "reference_file", "system_prompt_file"}) == FILE_BACKED_TEMPLATE_OPTION_KEYS
        assert config["transforms"][0]["options"]["prompt_template"] == "Hello {{ row.name }}"
        assert config["transforms"][0]["options"]["lookup"] == {"categories": ["Clothing"]}
        # reference_file materializes as TEXT, unlike lookup_file's parsed YAML —
        # reference_join parses the table itself so one field can serve CSV and JSON.
        assert config["transforms"][0]["options"]["reference_content"] == "sku,description\nhats,A fine hat\n"
        assert config["aggregations"][0]["options"]["system_prompt"] == "Use concise labels."

    def test_expand_template_file_not_found(self, tmp_path: Path) -> None:
        """Missing template file raises TemplateFileError."""
        from elspeth.core.config import TemplateFileError, _expand_template_files

        settings_path = tmp_path / "settings.yaml"
        config = {"template_file": "prompts/missing.j2"}

        with pytest.raises(TemplateFileError, match="not found"):
            _expand_template_files(config, settings_path)

    def test_expand_template_file_with_inline_raises(self, tmp_path: Path) -> None:
        """Cannot specify both prompt_template and template_file."""
        from elspeth.core.config import TemplateFileError, _expand_template_files

        settings_path = tmp_path / "settings.yaml"
        config = {
            "prompt_template": "inline template",
            "template_file": "prompts/test.j2",
        }

        with pytest.raises(TemplateFileError, match="Cannot specify both"):
            _expand_template_files(config, settings_path)

    def test_expand_lookup_file_invalid_yaml(self, tmp_path: Path) -> None:
        """Invalid YAML in lookup file raises TemplateFileError."""
        from elspeth.core.config import TemplateFileError, _expand_template_files

        lookup_file = tmp_path / "bad.yaml"
        lookup_file.write_text("invalid: yaml: content: [")

        settings_path = tmp_path / "settings.yaml"
        config = {
            "prompt_template": "test",
            "lookup_file": "bad.yaml",
        }

        with pytest.raises(TemplateFileError, match="Invalid YAML"):
            _expand_template_files(config, settings_path)

    def test_expand_template_file(self, tmp_path: Path) -> None:
        """template_file is expanded to template content at config time."""
        from elspeth.core.config import _expand_template_files

        # Create template file
        template_file = tmp_path / "prompts" / "test.j2"
        template_file.parent.mkdir(parents=True)
        template_file.write_text("Hello, {{ row.name }}!")

        # Create settings file path (for relative resolution)
        settings_path = tmp_path / "settings.yaml"

        config = {
            "template_file": "prompts/test.j2",
        }

        expanded = _expand_template_files(config, settings_path)

        assert "prompt_template" in expanded
        assert expanded["prompt_template"] == "Hello, {{ row.name }}!"
        assert expanded["prompt_template_source"] == "prompts/test.j2"
        assert "template_file" not in expanded  # Original key removed

    def test_expand_lookup_file_with_inline_raises(self, tmp_path: Path) -> None:
        """Cannot specify both lookup and lookup_file."""
        from elspeth.core.config import TemplateFileError, _expand_template_files

        settings_path = tmp_path / "settings.yaml"
        config = {
            "prompt_template": "test",
            "lookup": {"existing": "data"},
            "lookup_file": "prompts/lookups.yaml",
        }

        with pytest.raises(TemplateFileError, match="Cannot specify both"):
            _expand_template_files(config, settings_path)

    def test_expand_lookup_file(self, tmp_path: Path) -> None:
        """lookup_file is expanded to parsed YAML at config time."""
        from elspeth.core.config import _expand_template_files

        # Create lookup file
        lookup_file = tmp_path / "prompts" / "lookups.yaml"
        lookup_file.parent.mkdir(parents=True, exist_ok=True)
        lookup_file.write_text("categories:\n  - Electronics\n  - Clothing\n")

        settings_path = tmp_path / "settings.yaml"

        config = {
            "prompt_template": "{{ lookup.categories }}",
            "lookup_file": "prompts/lookups.yaml",
        }

        expanded = _expand_template_files(config, settings_path)

        assert expanded["lookup"] == {"categories": ["Electronics", "Clothing"]}
        assert expanded["lookup_source"] == "prompts/lookups.yaml"
        assert "lookup_file" not in expanded

    def test_expand_template_and_lookup_files(self, tmp_path: Path) -> None:
        """Both template_file and lookup_file expand together."""
        from elspeth.core.config import _expand_template_files

        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()

        (prompts_dir / "classify.j2").write_text("Category: {{ lookup.cats[row.id] }}")
        (prompts_dir / "lookups.yaml").write_text("cats:\n  0: A\n  1: B\n")

        settings_path = tmp_path / "settings.yaml"

        config = {
            "template_file": "prompts/classify.j2",
            "lookup_file": "prompts/lookups.yaml",
        }

        expanded = _expand_template_files(config, settings_path)

        assert expanded["prompt_template"] == "Category: {{ lookup.cats[row.id] }}"
        assert expanded["prompt_template_source"] == "prompts/classify.j2"
        assert expanded["lookup"] == {"cats": {0: "A", 1: "B"}}
        assert expanded["lookup_source"] == "prompts/lookups.yaml"

    def test_expand_system_prompt_file_not_found(self, tmp_path: Path) -> None:
        """Missing system_prompt file raises TemplateFileError."""
        from elspeth.core.config import TemplateFileError, _expand_template_files

        settings_path = tmp_path / "settings.yaml"
        config = {
            "prompt_template": "test",
            "system_prompt_file": "prompts/missing.txt",
        }

        with pytest.raises(TemplateFileError, match="not found"):
            _expand_template_files(config, settings_path)

    def test_expand_system_prompt_file_with_inline_raises(self, tmp_path: Path) -> None:
        """Cannot specify both system_prompt and system_prompt_file."""
        from elspeth.core.config import TemplateFileError, _expand_template_files

        settings_path = tmp_path / "settings.yaml"
        config = {
            "prompt_template": "test",
            "system_prompt": "You are a helpful assistant.",
            "system_prompt_file": "prompts/system.txt",
        }

        with pytest.raises(TemplateFileError, match="Cannot specify both"):
            _expand_template_files(config, settings_path)

    def test_expand_system_prompt_file(self, tmp_path: Path) -> None:
        """system_prompt_file is expanded to system_prompt content at config time."""
        from elspeth.core.config import _expand_template_files

        # Create system prompt file
        system_file = tmp_path / "prompts" / "system.txt"
        system_file.parent.mkdir(parents=True, exist_ok=True)
        system_file.write_text("You are an expert classifier. Be precise and consistent.")

        settings_path = tmp_path / "settings.yaml"

        config = {
            "prompt_template": "Classify: {{ row.text }}",
            "system_prompt_file": "prompts/system.txt",
        }

        expanded = _expand_template_files(config, settings_path)

        assert "system_prompt" in expanded
        assert expanded["system_prompt"] == "You are an expert classifier. Be precise and consistent."
        assert expanded["system_prompt_source"] == "prompts/system.txt"
        assert "system_prompt_file" not in expanded  # Original key removed

    def test_expand_all_file_types(self, tmp_path: Path) -> None:
        """All file types (template, lookup, system_prompt) expand together."""
        from elspeth.core.config import _expand_template_files

        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()

        (prompts_dir / "classify.j2").write_text("Classify: {{ row.text }}")
        (prompts_dir / "lookups.yaml").write_text("categories:\n  - A\n  - B\n")
        (prompts_dir / "system.txt").write_text("You are an expert.")

        settings_path = tmp_path / "settings.yaml"

        config = {
            "template_file": "prompts/classify.j2",
            "lookup_file": "prompts/lookups.yaml",
            "system_prompt_file": "prompts/system.txt",
        }

        expanded = _expand_template_files(config, settings_path)

        # Template expanded
        assert expanded["prompt_template"] == "Classify: {{ row.text }}"
        assert expanded["prompt_template_source"] == "prompts/classify.j2"
        assert "template_file" not in expanded

        # Lookup expanded
        assert expanded["lookup"] == {"categories": ["A", "B"]}
        assert expanded["lookup_source"] == "prompts/lookups.yaml"
        assert "lookup_file" not in expanded

        # System prompt expanded
        assert expanded["system_prompt"] == "You are an expert."
        assert expanded["system_prompt_source"] == "prompts/system.txt"
        assert "system_prompt_file" not in expanded


class TestLoadSettingsWithRunMode:
    """Tests for loading YAML with run_mode configuration."""

    def test_load_settings_with_live_mode(self, tmp_path: Path) -> None:
        """Load YAML with live mode (default)."""
        from elspeth.config_loading import load_settings
        from elspeth.contracts.enums import RunMode

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv
    on_success: output

sinks:
  output:
    plugin: csv
    on_write_failure: discard


run_mode: live
""")
        settings = load_settings(config_file)

        assert settings.run_mode == RunMode.LIVE
        assert settings.replay_from is None

    def test_load_settings_with_replay_mode(self, tmp_path: Path) -> None:
        """Load YAML with replay mode."""
        from elspeth.config_loading import load_settings
        from elspeth.contracts.enums import RunMode

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv
    on_success: output

sinks:
  output:
    plugin: csv
    on_write_failure: discard


run_mode: replay
replay_from: run-abc123
""")
        settings = load_settings(config_file)

        assert settings.run_mode == RunMode.REPLAY
        assert settings.replay_from == "run-abc123"

    def test_load_settings_with_verify_mode(self, tmp_path: Path) -> None:
        """Load YAML with verify mode."""
        from elspeth.config_loading import load_settings
        from elspeth.contracts.enums import RunMode

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv
    on_success: output

sinks:
  output:
    plugin: csv
    on_write_failure: discard


run_mode: verify
replay_from: run-xyz789
""")
        settings = load_settings(config_file)

        assert settings.run_mode == RunMode.VERIFY
        assert settings.replay_from == "run-xyz789"

    def test_load_settings_replay_without_source_run_id_fails(self, tmp_path: Path) -> None:
        """Loading replay mode without source_run_id should fail."""
        from elspeth.config_loading import load_settings

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv
    on_success: output

sinks:
  output:
    plugin: csv
    on_write_failure: discard


run_mode: replay
""")
        with pytest.raises(ValidationError) as exc_info:
            load_settings(config_file)

        assert "replay_from is required" in str(exc_info.value)


class TestLoadSettingsTemplateFileExpansion:
    """Tests for template file expansion during load_settings."""

    def test_load_settings_expands_template_files(self, tmp_path: Path) -> None:
        """load_settings expands template_file in row_plugins."""
        from elspeth.config_loading import load_settings

        # Create directory structure
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.j2").write_text("Hello {{ row.name }}")
        (prompts_dir / "lookups.yaml").write_text("greetings:\n  - Hello\n")

        # Create settings file
        settings_file = tmp_path / "settings.yaml"
        settings_file.write_text("""
sources:
  primary:
    plugin: csv_local
    on_success: output
    options:
      path: test.csv

sinks:
  output:
    plugin: csv_local
    on_write_failure: discard
    options:
      path: out.csv



transforms:
  - name: t1
    plugin: llm
    input: source_out
    on_success: output
    on_error: discard
    options:
      provider: openrouter
      model: test
      template_file: prompts/test.j2
      lookup_file: prompts/lookups.yaml
      schema:
        mode: observed
""")

        settings = load_settings(settings_file)

        # Check that files were expanded
        plugin_opts = settings.transforms[0].options
        assert plugin_opts["prompt_template"] == "Hello {{ row.name }}"
        assert plugin_opts["prompt_template_source"] == "prompts/test.j2"
        assert plugin_opts["lookup"] == {"greetings": ["Hello"]}
        assert plugin_opts["lookup_source"] == "prompts/lookups.yaml"


class TestEnvVarExpansion:
    """Tests for ${VAR} and ${VAR:-default} environment variable expansion."""

    def test_expand_env_var_with_value_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variables are expanded when set."""
        from elspeth.core.config import _expand_env_vars

        monkeypatch.setenv("TEST_API_KEY", "secret-key-123")

        config = {"api_key": "${TEST_API_KEY}"}
        result = _expand_env_vars(config)

        assert result["api_key"] == "secret-key-123"

    def test_expand_env_var_with_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default value is used when env var is not set."""
        from elspeth.core.config import _expand_env_vars

        monkeypatch.delenv("UNSET_VAR", raising=False)

        config = {"endpoint": "${UNSET_VAR:-https://default.example.com}"}
        result = _expand_env_vars(config)

        assert result["endpoint"] == "https://default.example.com"

    def test_expand_env_var_prefers_env_over_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variable value takes precedence over default."""
        from elspeth.core.config import _expand_env_vars

        monkeypatch.setenv("MY_ENDPOINT", "https://actual.example.com")

        config = {"endpoint": "${MY_ENDPOINT:-https://default.example.com}"}
        result = _expand_env_vars(config)

        assert result["endpoint"] == "https://actual.example.com"

    def test_expand_env_var_required_raises_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing required env var (no default) raises clear error."""
        from elspeth.core.config import _expand_env_vars

        monkeypatch.delenv("REQUIRED_API_KEY", raising=False)

        config = {"api_key": "${REQUIRED_API_KEY}"}

        with pytest.raises(ValueError) as exc_info:
            _expand_env_vars(config)

        error_msg = str(exc_info.value)
        assert "REQUIRED_API_KEY" in error_msg
        assert "not set" in error_msg
        assert ":-default" in error_msg  # Suggests the fix

    def test_expand_env_var_in_nested_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variables are expanded in nested structures."""
        from elspeth.core.config import _expand_env_vars

        monkeypatch.setenv("NESTED_VALUE", "expanded")

        config = {"outer": {"inner": {"value": "${NESTED_VALUE}"}}}
        result = _expand_env_vars(config)

        assert result["outer"]["inner"]["value"] == "expanded"

    def test_expand_env_var_in_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variables are expanded in lists."""
        from elspeth.core.config import _expand_env_vars

        monkeypatch.setenv("LIST_ITEM", "item-value")

        config = {"items": ["static", "${LIST_ITEM}", "another"]}
        result = _expand_env_vars(config)

        assert result["items"] == ["static", "item-value", "another"]

    def test_expand_multiple_env_vars_in_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Multiple env vars in a single string are all expanded."""
        from elspeth.core.config import _expand_env_vars

        monkeypatch.setenv("PROTO", "https")
        monkeypatch.setenv("HOST", "api.example.com")

        config = {"url": "${PROTO}://${HOST}/v1"}
        result = _expand_env_vars(config)

        assert result["url"] == "https://api.example.com/v1"

    def test_expand_empty_default_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty string default is valid (${VAR:-} pattern)."""
        from elspeth.core.config import _expand_env_vars

        monkeypatch.delenv("OPTIONAL_PREFIX", raising=False)

        config = {"prefix": "${OPTIONAL_PREFIX:-}"}
        result = _expand_env_vars(config)

        assert result["prefix"] == ""

    def test_load_settings_from_yaml_string_preserves_env_placeholders_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The in-memory loader treats ${VAR} as literal user data by default."""
        from elspeth.config_loading import load_settings_from_yaml_string

        monkeypatch.setenv("INLINE_PROMPT_SECRET", "server-secret-value")

        settings = load_settings_from_yaml_string(
            """
sources:
  primary:
    plugin: csv_local
    on_success: output
    options:
      prompt_template: prefix-${INLINE_PROMPT_SECRET}-suffix
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options: {}
"""
        )

        assert settings.sources["primary"].options["prompt_template"] == "prefix-${INLINE_PROMPT_SECRET}-suffix"

    def test_load_settings_from_yaml_string_can_opt_into_env_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Trusted in-process callers can explicitly request host environment expansion."""
        from elspeth.config_loading import load_settings_from_yaml_string

        monkeypatch.setenv("INLINE_PROMPT_SECRET", "server-secret-value")

        settings = load_settings_from_yaml_string(
            """
sources:
  primary:
    plugin: csv_local
    on_success: output
    options:
      prompt_template: prefix-${INLINE_PROMPT_SECRET}-suffix
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options: {}
""",
            expand_env_vars=True,
        )

        assert settings.sources["primary"].options["prompt_template"] == "prefix-server-secret-value-suffix"

    def test_load_settings_from_yaml_string_can_skip_env_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Opt-out keeps runtime-substituted ${VAR} literal so host secrets are not resolved.

        Web execution uses this after splicing in inline-blob bytes / resolved
        secrets: the operator tree was already expanded, and attacker-supplied
        blob text like ${INLINE_PROMPT_SECRET} must stay data, not a host lookup.
        """
        from elspeth.config_loading import load_settings_from_yaml_string

        monkeypatch.setenv("INLINE_PROMPT_SECRET", "server-secret-value")

        settings = load_settings_from_yaml_string(
            """
sources:
  primary:
    plugin: csv_local
    on_success: output
    options:
      prompt_template: prefix-${INLINE_PROMPT_SECRET}-suffix
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options: {}
""",
            expand_env_vars=False,
        )

        assert settings.sources["primary"].options["prompt_template"] == "prefix-${INLINE_PROMPT_SECRET}-suffix"

    def test_load_settings_from_yaml_string_rejects_aggregation_file_backed_options(self) -> None:
        from elspeth.config_loading import load_settings_from_yaml_string

        with pytest.raises(ValueError) as exc_info:
            load_settings_from_yaml_string(
                """
sources:
  primary:
    plugin: csv_local
    on_success: batch_input
    options: {}
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options: {}
aggregations:
  - name: summarize
    plugin: llm
    input: batch_input
    on_success: output
    on_error: discard
    trigger:
      count: 10
    options:
      system_prompt_file: prompts/system.txt
"""
            )

        message = str(exc_info.value)
        assert "system_prompt_file" in message
        assert "aggregations['summarize']" in message
        assert "load_settings()" in message


class TestUnknownKeyRejection:
    """Regression tests for jkmk: load_settings must reject unknown top-level keys.

    Previously, load_settings() silently dropped unknown keys via a positive
    allowlist before Pydantic's extra='forbid' could reject them. This meant
    typos like 'trnasforms' were silently ignored and the pipeline ran with
    no transforms configured.
    """

    def test_typo_key_raises_value_error(self, tmp_path: Path) -> None:
        """A typo like 'trnasforms' instead of 'transforms' must be rejected."""
        from elspeth.config_loading import load_settings

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv
    on_success: output
sinks:
  output:
    plugin: csv
    on_write_failure: discard
trnasforms:
  - plugin: passthrough
    name: test
    input: source_out
    on_success: output
    on_error: discard
""")
        with pytest.raises(ValueError, match="trnasforms") as exc_info:
            load_settings(config_file)
        # Error message should also include valid keys for diagnosis
        assert "Valid top-level keys" in str(exc_info.value)

    def test_multiple_typo_keys_all_reported(self, tmp_path: Path) -> None:
        """Multiple unknown keys should all appear in the error message."""
        from elspeth.config_loading import load_settings

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv
    on_success: output
sinks:
  output:
    plugin: csv
    on_write_failure: discard
trnasforms:
  - plugin: passthrough
retrry:
  max_attempts: 5
""")
        with pytest.raises(ValueError, match="trnasforms") as exc_info:
            load_settings(config_file)
        assert "retrry" in str(exc_info.value)

    def test_valid_config_still_loads(self, tmp_path: Path) -> None:
        """A config with only valid keys must load without error (no regression)."""
        from elspeth.config_loading import load_settings

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv
    on_success: output
    options:
      path: input.csv
sinks:
  output:
    plugin: csv
    on_write_failure: discard
    options:
      path: output.csv
transforms:
  - plugin: passthrough
    name: test
    input: source_out
    on_success: output
    on_error: discard
retry:
  max_attempts: 5
""")
        settings = load_settings(config_file)
        assert settings.sources["primary"].plugin == "csv"
        assert settings.retry.max_attempts == 5
        assert len(settings.transforms) == 1

    def test_dynaconf_internal_keys_not_rejected(self, tmp_path: Path) -> None:
        """Dynaconf internal keys (e.g., load_dotenv) must not trigger an error.

        Dynaconf injects keys like LOAD_DOTENV into the settings dict.
        These are filtered by the allowlist but must not be treated as user errors.
        """
        from elspeth.config_loading import load_settings

        # A minimal valid config — Dynaconf will inject LOAD_DOTENV automatically
        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv
    on_success: output
sinks:
  output:
    plugin: csv
    on_write_failure: discard
""")
        # Should load without error (Dynaconf injects LOAD_DOTENV internally)
        settings = load_settings(config_file)
        assert settings.sources["primary"].plugin == "csv"

    def test_env_var_injected_keys_not_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """ELSPETH_* env vars injected by Dynaconf must not trigger unknown key error.

        Regression test: Dynaconf captures ALL ELSPETH_* process env vars (e.g.,
        ELSPETH_LOG_LEVEL set in docker-compose) and injects them into raw_config.
        The unknown-key check must only flag keys from the YAML file, not env vars.
        """
        from elspeth.config_loading import load_settings

        # Set an ELSPETH_* env var that is NOT an ElspethSettings field
        monkeypatch.setenv("ELSPETH_LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("ELSPETH_CUSTOM_THING", "42")

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv
    on_success: output
sinks:
  output:
    plugin: csv
    on_write_failure: discard
""")
        # Must NOT raise ValueError about "log_level" or "custom_thing"
        settings = load_settings(config_file)
        assert settings.sources["primary"].plugin == "csv"

    def test_yaml_typo_still_rejected_with_env_vars_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """YAML typos must still be rejected even when env vars are present.

        Regression test: env var filtering must not accidentally disable
        the typo check for actual YAML key typos.
        """
        from elspeth.config_loading import load_settings

        # Set a legitimate env var
        monkeypatch.setenv("ELSPETH_LOG_LEVEL", "DEBUG")

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv
    on_success: output
sinks:
  output:
    plugin: csv
    on_write_failure: discard
trnasforms:
  - plugin: passthrough
""")
        with pytest.raises(ValueError, match="trnasforms"):
            load_settings(config_file)

    def test_secrets_key_not_rejected(self, tmp_path: Path) -> None:
        """The 'secrets' YAML key is handled by SecretsConfig, not ElspethSettings.

        It must not be rejected as an unknown key even though it's not in
        ElspethSettings.model_fields.
        """
        from elspeth.config_loading import load_settings

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv
    on_success: output
sinks:
  output:
    plugin: csv
    on_write_failure: discard
secrets:
  source: env
""")
        settings = load_settings(config_file)
        assert settings.sources["primary"].plugin == "csv"


class TestLowercaseSchemaKeysBranchPreservation:
    """Bug 7.8: _lowercase_schema_keys must preserve coalesce branch names.

    Coalesce branch names are user-defined identifiers that may contain
    mixed-case. Lowercasing them would cause lookup failures during
    coalesce execution.
    """

    def test_dynaconf_key_normalizer_preserves_declarative_user_data_paths(self) -> None:
        from elspeth.core.dynaconf_normalization import USER_DATA_KEYS, DynaconfKeyNormalizer

        normalizer = DynaconfKeyNormalizer()
        result = normalizer.normalize(
            {
                "TRANSFORMS": [
                    {
                        "NAME": "classify",
                        "OPTIONS": {
                            "PromptKey": "preserved",
                            "SCHEMA": {
                                "MODE": "fixed",
                                "FIELDS": ["Label: str"],
                            },
                        },
                    }
                ],
                "GATES": [
                    {
                        "NAME": "router",
                        "ROUTES": {
                            "High": "urgent",
                            "SCHEMA": "schema_sink",
                        },
                    }
                ],
                "COALESCE": [
                    {
                        "NAME": "merge",
                        "BRANCHES": {
                            "SentimentPath": "sentiment_out",
                        },
                    }
                ],
            }
        )

        assert frozenset({"options", "routes", "branches"}) == USER_DATA_KEYS
        assert result["transforms"][0]["name"] == "classify"
        assert result["transforms"][0]["options"]["PromptKey"] == "preserved"
        assert result["transforms"][0]["options"]["schema"] == {"mode": "fixed", "fields": ["Label: str"]}
        assert result["gates"][0]["routes"] == {"High": "urgent", "SCHEMA": "schema_sink"}
        assert result["coalesce"][0]["branches"] == {"SentimentPath": "sentiment_out"}

    def test_branches_keys_are_preserved(self) -> None:
        from elspeth.core.config import _lowercase_schema_keys

        config = {
            "COALESCE": [
                {
                    "NAME": "merge_results",
                    "BRANCHES": {
                        "SentimentPath": "sentiment_out",
                        "EntityPath": "entity_out",
                    },
                    "POLICY": "require_all",
                    "MERGE": "union",
                }
            ]
        }
        result = _lowercase_schema_keys(config)

        # Schema keys should be lowercased
        assert "coalesce" in result
        coalesce = result["coalesce"][0]
        assert "name" in coalesce
        assert "branches" in coalesce

        # Branch names (keys inside branches) must be PRESERVED
        branches = coalesce["branches"]
        assert "SentimentPath" in branches
        assert "EntityPath" in branches
        # Values inside branches must also be preserved
        assert branches["SentimentPath"] == "sentiment_out"
        assert branches["EntityPath"] == "entity_out"

    def test_schema_branch_key_is_preserved(self) -> None:
        """A branch named SCHEMA is user data, not plugin schema config."""
        from elspeth.core.config import _lowercase_schema_keys

        config = {
            "COALESCE": [
                {
                    "NAME": "merge_results",
                    "BRANCHES": {
                        "SCHEMA": "schema_out",
                        "fallback": "fallback_out",
                    },
                    "POLICY": "require_all",
                    "MERGE": "select",
                    "SELECT_BRANCH": "SCHEMA",
                    "ON_SUCCESS": "output",
                }
            ]
        }
        result = _lowercase_schema_keys(config)

        coalesce = result["coalesce"][0]
        assert coalesce["branches"] == {"SCHEMA": "schema_out", "fallback": "fallback_out"}
        assert coalesce["select_branch"] == "SCHEMA"
        assert "schema" not in coalesce["branches"]

    def test_load_settings_preserves_coalesce_branch_names(self, tmp_path: Path) -> None:
        """Full config loading preserves mixed-case coalesce branch names."""
        from elspeth.config_loading import load_settings

        config_file = tmp_path / "settings.yaml"
        config_file.write_text("""
sources:
  primary:
    plugin: csv
    on_success: quality_gate
sinks:
  output:
    plugin: csv
    on_write_failure: discard
gates:
  - name: quality_gate
    input: quality_gate
    condition: "True"
    routes:
      "true": fork
      "false": output
    fork_to:
      - sentiment_path
      - entity_path
transforms:
  - name: sentiment_transform
    plugin: passthrough
    input: sentiment_path
    on_success: sentiment_out
    on_error: discard
  - name: entity_transform
    plugin: passthrough
    input: entity_path
    on_success: entity_out
    on_error: discard
coalesce:
  - name: merge_results
    branches:
      sentiment_path: sentiment_out
      entity_path: entity_out
    policy: require_all
    merge: union
    on_success: output
""")
        settings = load_settings(config_file)
        assert len(settings.coalesce) == 1
        branches = settings.coalesce[0].branches
        assert "sentiment_path" in branches
        assert "entity_path" in branches
