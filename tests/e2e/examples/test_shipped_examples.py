# tests/e2e/examples/test_shipped_examples.py
"""E2E tests verifying all shipped example pipelines are valid configurations.

Every example directory under examples/ must contain at least one YAML
settings file, and each file must:
  1. Be parseable as YAML
  2. Contain a dict with the required top-level keys (source or sources, sinks)
  3. Where possible, pass full ElspethSettings validation via load_settings()

Examples that require external services (Azure, OpenRouter) or
environment variables cannot be fully validated without those vars set,
so they are tested for structural validity only.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import sqlite3
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import select
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.contracts import RunStatus
from elspeth.core.config import ElspethSettings, load_settings
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import rows_table, run_sources_table
from elspeth.core.payload_store import FilesystemPayloadStore

# Examples that contain ${VAR} env var references that would fail
# load_settings without the env vars being set.
_EXAMPLES_WITH_ENV_VARS: frozenset[str] = frozenset(
    {
        "azure_blob_sentiment",
        "azure_keyvault_secrets",
        "azure_openai_sentiment",
        "chroma_rag_qa",
        "multi_query_assessment",
        "openrouter_multi_query_assessment",
        "openrouter_sentiment",
        "schema_contracts_llm_assessment",
        "template_lookups",
    }
)

# Examples that reference external template/lookup files via
# template_file or lookup_file keys. These require those files to
# exist relative to the settings path, which they do, but they also
# tend to have env var references so they overlap with the above set.
_EXAMPLES_WITH_FILE_REFS: frozenset[str] = frozenset(
    {
        "multi_query_assessment",
        "openrouter_multi_query_assessment",
        "schema_contracts_llm_assessment",
        "template_lookups",
    }
)

# Examples that have no YAML settings file at all (e.g. data-only directories).
_EXAMPLES_WITHOUT_SETTINGS: frozenset[str] = frozenset(
    {
        "chaosllm",  # Contains only responses.jsonl (replay data)
    }
)

# Required top-level keys for any Elspeth settings file. Source roots may use
# either the legacy singular ``source`` form or the canonical plural ``sources``
# form for named multi-source examples.
_REQUIRED_KEYS: frozenset[str] = frozenset({"sinks"})


class TestShippedExamples:
    """Verify all shipped examples are valid configurations."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_example_settings(examples_dir: Path) -> list[tuple[str, Path]]:
        """Find all settings YAML files in examples.

        Returns a list of (example_name, yaml_path) tuples. Only files
        whose name contains "settings" are included; auxiliary YAML files
        (chaos_config.yaml, criteria_lookup.yaml) are excluded.
        """
        results: list[tuple[str, Path]] = []
        for example_dir in sorted(examples_dir.iterdir()):
            if not example_dir.is_dir() or example_dir.name.startswith("."):
                continue
            if example_dir.name in _EXAMPLES_WITHOUT_SETTINGS:
                continue
            for yaml_file in sorted(example_dir.glob("*.yaml")):
                if "settings" in yaml_file.name:
                    results.append((example_dir.name, yaml_file))
        return results

    @staticmethod
    def _needs_env_vars(example_name: str) -> bool:
        """Return True if the example requires env vars we cannot set in CI."""
        return example_name in _EXAMPLES_WITH_ENV_VARS

    @staticmethod
    def _assert_source_roots_valid(name: str, path: Path, data: dict[str, Any]) -> None:
        has_source = "source" in data
        has_sources = "sources" in data
        assert has_source != has_sources, f"{name}/{path.name}: define exactly one of source or sources"
        if has_source:
            source = data["source"]
            assert isinstance(source, dict), f"{name}/{path.name}: source must be a dict"
            assert "plugin" in source, f"{name}/{path.name}: source missing 'plugin' key"
            return

        sources = data["sources"]
        assert isinstance(sources, dict), f"{name}/{path.name}: sources must be a dict"
        assert sources, f"{name}/{path.name}: sources must not be empty"
        for source_name, source in sources.items():
            assert isinstance(source_name, str), f"{name}/{path.name}: source name must be a string"
            assert isinstance(source, dict), f"{name}/{path.name}: source '{source_name}' must be a dict"
            assert "plugin" in source, f"{name}/{path.name}: source '{source_name}' missing 'plugin' key"

    @staticmethod
    def _copy_example_to_tmp(example_pipeline_dir: Path, tmp_path: Path, example_name: str) -> Path:
        scratch_examples_dir = tmp_path / "examples"
        scratch_examples_dir.mkdir()
        copied_example_dir = scratch_examples_dir / example_name
        shutil.copytree(
            example_pipeline_dir / example_name,
            copied_example_dir,
            ignore=shutil.ignore_patterns("*.db", "*.db-shm", "*.db-wal", "*.jsonl", "payloads"),
        )
        return copied_example_dir

    @staticmethod
    def _run_example(settings_path: Path) -> tuple[dict[str, Any], LandscapeDB]:
        settings = load_settings(settings_path)
        runner = CliRunner()
        result = runner.invoke(
            app,
            # The CLI logging handler writes to stdout; JSON logs keep this
            # captured channel parseable without skipping malformed lines.
            ["--json-logs", "run", "--settings", str(settings_path), "--execute", "--format", "json"],
        )
        assert result.exit_code == 0, result.output

        events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        execution_events = [event for event in events if event["event"] == "execution_result"]
        assert len(execution_events) == 1, result.output
        db = LandscapeDB(settings.landscape.url)
        return execution_events[0], db

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    @staticmethod
    def _audit_source_rows(db: LandscapeDB, run_id: str) -> tuple[list[tuple[str, str, str]], list[tuple[str, int, int]]]:
        with db.engine.connect() as conn:
            run_sources = conn.execute(
                select(
                    run_sources_table.c.source_name,
                    run_sources_table.c.source_node_id,
                    run_sources_table.c.lifecycle_state,
                )
                .where(run_sources_table.c.run_id == run_id)
                .order_by(run_sources_table.c.source_name)
            ).all()
            rows = conn.execute(
                select(
                    rows_table.c.source_node_id,
                    rows_table.c.source_row_index,
                    rows_table.c.ingest_sequence,
                )
                .where(rows_table.c.run_id == run_id)
                .order_by(rows_table.c.ingest_sequence)
            ).all()
        return (
            [(source_name, source_node_id, lifecycle_state) for source_name, source_node_id, lifecycle_state in run_sources],
            [(source_node_id, source_row_index, ingest_sequence) for source_node_id, source_row_index, ingest_sequence in rows],
        )

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_examples_directory_exists(self, example_pipeline_dir: Path) -> None:
        """The examples/ directory exists at the repo root."""
        assert example_pipeline_dir.is_dir(), f"examples/ not found at {example_pipeline_dir}"

    def test_all_examples_have_settings(self, example_pipeline_dir: Path) -> None:
        """Every example directory has at least one settings file (or is excused)."""
        example_dirs = [d for d in sorted(example_pipeline_dir.iterdir()) if d.is_dir() and not d.name.startswith(".")]
        assert len(example_dirs) > 0, "No example directories found"

        for d in example_dirs:
            if d.name in _EXAMPLES_WITHOUT_SETTINGS:
                continue
            yamls = list(d.glob("*.yaml")) + list(d.glob("*.yml"))
            assert len(yamls) > 0, f"Example {d.name} has no YAML config files"

    def test_discover_settings_files(self, example_pipeline_dir: Path) -> None:
        """Sanity check: discovery finds a reasonable number of settings files."""
        settings = self._find_example_settings(example_pipeline_dir)
        # We know there are 20+ example directories with settings
        assert len(settings) >= 20, f"Expected at least 20 settings files, found {len(settings)}"

    def test_all_settings_are_valid_yaml(self, example_pipeline_dir: Path) -> None:
        """All example settings files are parseable YAML producing dicts."""
        settings = self._find_example_settings(example_pipeline_dir)
        assert len(settings) > 0, "No settings files found"

        for name, path in settings:
            with open(path) as f:
                data = yaml.safe_load(f)
            assert isinstance(data, dict), f"{name}/{path.name}: settings is not a dict, got {type(data).__name__}"

    def test_all_settings_have_required_keys(self, example_pipeline_dir: Path) -> None:
        """All settings files contain the required top-level keys."""
        settings = self._find_example_settings(example_pipeline_dir)

        for name, path in settings:
            with open(path) as f:
                data: dict[str, Any] = yaml.safe_load(f)

            missing = _REQUIRED_KEYS - set(data.keys())
            assert not missing, f"{name}/{path.name}: missing required keys {missing}"
            self._assert_source_roots_valid(name, path, data)

    def test_all_settings_have_valid_source_structure(self, example_pipeline_dir: Path) -> None:
        """All settings files have a properly structured source section."""
        settings = self._find_example_settings(example_pipeline_dir)

        for name, path in settings:
            with open(path) as f:
                data: dict[str, Any] = yaml.safe_load(f)

            self._assert_source_roots_valid(name, path, data)

    def test_all_settings_have_valid_sinks_structure(self, example_pipeline_dir: Path) -> None:
        """All settings files have a properly structured sinks section."""
        settings = self._find_example_settings(example_pipeline_dir)

        for name, path in settings:
            with open(path) as f:
                data: dict[str, Any] = yaml.safe_load(f)

            sinks = data.get("sinks")
            assert isinstance(sinks, dict), f"{name}/{path.name}: sinks must be a dict"
            assert len(sinks) > 0, f"{name}/{path.name}: sinks must not be empty"

            # Each sink must have a plugin key
            for sink_name, sink_config in sinks.items():
                assert isinstance(sink_config, dict), f"{name}/{path.name}: sink '{sink_name}' must be a dict"
                assert "plugin" in sink_config, f"{name}/{path.name}: sink '{sink_name}' missing 'plugin' key"

    def test_local_examples_load_via_config_system(self, example_pipeline_dir: Path) -> None:
        """Examples without env var requirements load through ElspethSettings.

        These examples have no ${VAR} references and no external template
        files, so load_settings() should succeed and produce a valid
        ElspethSettings instance.
        """
        settings = self._find_example_settings(example_pipeline_dir)
        local_settings = [(name, path) for name, path in settings if not self._needs_env_vars(name)]

        assert len(local_settings) > 0, "No local (no-env-var) examples found"

        for name, path in local_settings:
            loaded = load_settings(path)
            assert isinstance(loaded, ElspethSettings), f"{name}/{path.name}: load_settings did not return ElspethSettings"
            assert loaded.sources, f"{name}/{path.name}: no sources defined"
            for source_name, source in loaded.sources.items():
                assert source.plugin, f"{name}/{path.name}: source '{source_name}' plugin is empty"
            # Verify at least one sink exists
            assert len(loaded.sinks) > 0, f"{name}/{path.name}: no sinks defined"

    def test_multi_flow_example_executes_end_to_end(
        self,
        example_pipeline_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """multi_flow ships as a runnable two-source, two-flow example."""
        example_dir = self._copy_example_to_tmp(example_pipeline_dir, tmp_path, "multi_flow")
        monkeypatch.chdir(tmp_path)
        db: LandscapeDB | None = None
        try:
            result, db = self._run_example(example_dir / "settings.yaml")

            assert result["status"] == RunStatus.COMPLETED.value
            assert result["rows_processed"] == 4
            run_sources, rows = self._audit_source_rows(db, str(result["run_id"]))
            assert [(source_name, state) for source_name, _node_id, state in run_sources] == [
                ("signups", "exhausted"),
                ("tickets", "exhausted"),
            ]
            node_to_source = {node_id: source_name for source_name, node_id, _state in run_sources}
            assert [
                (node_to_source[node_id], source_row_index, ingest_sequence) for node_id, source_row_index, ingest_sequence in rows
            ] == [
                ("signups", 0, 0),
                ("signups", 1, 1),
                ("tickets", 0, 2),
                ("tickets", 1, 3),
            ]
            signups = self._read_jsonl(example_dir / "output" / "signups.jsonl")
            tickets = self._read_jsonl(example_dir / "output" / "tickets.jsonl")
            assert [row["signup_id"] for row in signups] == ["S-100", "S-101"]
            assert [row["ticket_id"] for row in tickets] == ["T-900", "T-901"]
        finally:
            if db is not None:
                db.close()

    def test_multi_source_queue_example_executes_end_to_end(
        self,
        example_pipeline_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """multi_source_queue ships as a runnable fan-in queue example."""
        example_dir = self._copy_example_to_tmp(example_pipeline_dir, tmp_path, "multi_source_queue")
        monkeypatch.chdir(tmp_path)
        db: LandscapeDB | None = None
        try:
            result, db = self._run_example(example_dir / "settings.yaml")

            assert result["status"] == RunStatus.COMPLETED.value
            assert result["rows_processed"] == 3
            run_sources, rows = self._audit_source_rows(db, str(result["run_id"]))
            assert [(source_name, state) for source_name, _node_id, state in run_sources] == [
                ("orders", "exhausted"),
                ("refunds", "exhausted"),
            ]
            node_to_source = {node_id: source_name for source_name, node_id, _state in run_sources}
            assert [
                (node_to_source[node_id], source_row_index, ingest_sequence) for node_id, source_row_index, ingest_sequence in rows
            ] == [
                ("orders", 0, 0),
                ("orders", 1, 1),
                ("refunds", 0, 2),
            ]
            combined = self._read_jsonl(example_dir / "output" / "combined.jsonl")
            assert len(combined) == 3
            assert Counter(row["kind"] for row in combined) == Counter({"order": 2, "refund": 1})
        finally:
            if db is not None:
                db.close()

    def test_env_var_examples_are_structurally_valid(self, example_pipeline_dir: Path) -> None:
        """Examples with env vars are valid YAML with correct structure.

        We cannot call load_settings() because env var expansion would
        fail, but we can verify the raw YAML structure matches what
        ElspethSettings expects.
        """
        settings = self._find_example_settings(example_pipeline_dir)
        env_settings = [(name, path) for name, path in settings if self._needs_env_vars(name)]

        assert len(env_settings) > 0, "No env-var examples found"

        for name, path in env_settings:
            with open(path) as f:
                data: dict[str, Any] = yaml.safe_load(f)

            # These must have the required keys
            assert "sinks" in data, f"{name}/{path.name}: missing sinks"
            self._assert_source_roots_valid(name, path, data)

            # Verify transforms structure if present
            if "transforms" in data:
                assert isinstance(data["transforms"], list), f"{name}/{path.name}: transforms must be a list"
                for i, t in enumerate(data["transforms"]):
                    assert isinstance(t, dict), f"{name}/{path.name}: transform[{i}] must be a dict"
                    assert "plugin" in t, f"{name}/{path.name}: transform[{i}] missing 'plugin'"

    @pytest.mark.parametrize(
        ("example_name", "settings_name", "required_env_var"),
        [
            pytest.param("landscape_journal", "settings.yaml", None, id="landscape-journal"),
            pytest.param(
                "openrouter_multi_query_assessment",
                "settings_journal.yaml",
                "OPENROUTER_API_KEY",
                id="openrouter-multi-query-assessment",
            ),
        ],
    )
    def test_shipped_journal_paths_resolve_next_to_audit_db_with_hostile_env(
        self,
        example_pipeline_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        example_name: str,
        settings_name: str,
        required_env_var: str | None,
    ) -> None:
        """Shipped journal examples keep their SQLite journal beside the audit DB."""
        example_dir = self._copy_example_to_tmp(
            example_pipeline_dir,
            tmp_path,
            example_name,
        )
        monkeypatch.chdir(tmp_path)
        # A process-wide override must not redirect or disable this copied fixture.
        monkeypatch.setenv("ELSPETH_LANDSCAPE__DUMP_TO_JSONL", "false")
        for variable_name in tuple(os.environ):
            if variable_name.startswith("ELSPETH_"):
                monkeypatch.delenv(variable_name)
        if required_env_var is not None:
            monkeypatch.setenv(required_env_var, "test-openrouter-key")
        settings = load_settings(example_dir / settings_name)

        db = LandscapeDB.from_url(
            settings.landscape.url,
            dump_to_jsonl=settings.landscape.dump_to_jsonl,
            dump_to_jsonl_path=settings.landscape.dump_to_jsonl_path,
            dump_to_jsonl_include_payloads=settings.landscape.dump_to_jsonl_include_payloads,
            dump_to_jsonl_payload_base_path=(
                str(settings.payload_store.base_path)
                if settings.landscape.dump_to_jsonl_payload_base_path is None
                else settings.landscape.dump_to_jsonl_payload_base_path
            ),
        )
        try:
            assert db._journal is not None
            assert db._journal._path == example_dir / "runs" / "audit.journal.jsonl"
        finally:
            db.close()

    def test_no_duplicate_sink_names(self, example_pipeline_dir: Path) -> None:
        """Sink names are unique within each settings file (YAML keys are unique by spec)."""
        settings = self._find_example_settings(example_pipeline_dir)

        for name, path in settings:
            with open(path) as f:
                data: dict[str, Any] = yaml.safe_load(f)

            sinks = data.get("sinks", {})
            # YAML dict keys are inherently unique, but verify they are all lowercase
            for sink_name in sinks:
                assert sink_name == sink_name.lower(), f"{name}/{path.name}: sink name '{sink_name}' is not lowercase"

    def test_gate_conditions_are_strings(self, example_pipeline_dir: Path) -> None:
        """Gate conditions must be strings (expression syntax)."""
        settings = self._find_example_settings(example_pipeline_dir)

        for name, path in settings:
            with open(path) as f:
                data: dict[str, Any] = yaml.safe_load(f)

            gates = data.get("gates", [])
            for i, gate in enumerate(gates):
                assert "condition" in gate, f"{name}/{path.name}: gate[{i}] missing 'condition'"
                assert isinstance(gate["condition"], str), f"{name}/{path.name}: gate[{i}] condition must be a string"
                assert "routes" in gate, f"{name}/{path.name}: gate[{i}] missing 'routes'"

    def test_large_scale_readme_describes_durable_scheduler_performance(self, example_pipeline_dir: Path) -> None:
        """large_scale_test must not ship pre-durable-scheduler throughput claims."""
        readme = (example_pipeline_dir / "large_scale_test" / "README.md").read_text()

        assert "durable scheduler" in readme
        assert "5,000-10,000 rows/sec" not in readme

    def test_chaosllm_endurance_documents_smoke_row_override(self, example_pipeline_dir: Path) -> None:
        """chaosllm_endurance must expose a bounded dogfood mode."""
        run_sh = (example_pipeline_dir / "chaosllm_endurance" / "run.sh").read_text()
        readme = (example_pipeline_dir / "chaosllm_endurance" / "README.md").read_text()
        agent_guide = (example_pipeline_dir / "AGENTS.md").read_text()

        assert "CHAOSLLM_ENDURANCE_ROWS" in run_sh
        assert "CHAOSLLM_ENDURANCE_INPUT_PATH" in run_sh
        assert "input.${ROWS}.csv" in run_sh
        assert "EXISTING_ROWS" in run_sh
        assert '--rows "$ROWS"' in run_sh
        assert "input.<rows>.csv" in readme
        assert "CHAOSLLM_ENDURANCE_ROWS=20" in readme
        assert "not gate dogfood" in agent_guide

    def test_multi_worker_showcase_stats_use_completed_terminal_outcomes(self, example_pipeline_dir: Path, tmp_path: Path) -> None:
        """Showcase stats distinguish successful and failed terminal outcomes."""
        run_id = "run-under-test"
        db_path = tmp_path / "audit.db"
        with sqlite3.connect(db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE token_work_items (run_id TEXT NOT NULL, status TEXT NOT NULL);
                CREATE TABLE token_outcomes (
                    run_id TEXT NOT NULL,
                    outcome TEXT,
                    completed INTEGER NOT NULL
                );
                """
            )
            conn.executemany(
                "INSERT INTO token_work_items (run_id, status) VALUES (?, 'terminal')",
                [(run_id,)] * 200,
            )
            conn.executemany(
                "INSERT INTO token_outcomes (run_id, outcome, completed) VALUES (?, ?, 1)",
                [(run_id, "success")] * 192 + [(run_id, "failure")] * 8,
            )
            conn.execute(
                "INSERT INTO token_outcomes (run_id, outcome, completed) VALUES (?, 'failure', 0)",
                (run_id,),
            )
            conn.execute("INSERT INTO token_outcomes (run_id, outcome, completed) VALUES ('other-run', 'failure', 1)")

        stats_helper = example_pipeline_dir / "multi_worker_showcase" / "outcome_stats.sh"
        assert stats_helper.is_file(), "multi_worker_showcase must ship its outcome stats helper"
        result = subprocess.run(
            ["bash", stats_helper, db_path, run_id],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "200|192|8"

    def test_multi_worker_showcase_stats_reject_invalid_inputs(self, example_pipeline_dir: Path, tmp_path: Path) -> None:
        """Outcome stats fail closed for invalid identity, storage, and output."""
        stats_helper = example_pipeline_dir / "multi_worker_showcase" / "outcome_stats.sh"

        empty_run_id = subprocess.run(
            ["bash", stats_helper, tmp_path / "unused.db", ""],
            capture_output=True,
            text=True,
            check=False,
        )
        assert empty_run_id.returncode == 2
        assert "invalid run id" in empty_run_id.stderr

        missing_db = subprocess.run(
            ["bash", stats_helper, tmp_path / "missing.db", "run-under-test"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert missing_db.returncode != 0
        assert missing_db.stderr

        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        fake_sqlite = fake_bin / "sqlite3"
        fake_sqlite.write_text("#!/usr/bin/env bash\necho 'not|valid'\n")
        fake_sqlite.chmod(0o755)
        invalid_result = subprocess.run(
            ["bash", stats_helper, tmp_path / "unused.db", "run-under-test"],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        )
        assert invalid_result.returncode != 0
        assert "invalid outcome counts" in invalid_result.stderr

    def test_multi_worker_showcase_launcher_fails_closed_on_invalid_stats(self, example_pipeline_dir: Path) -> None:
        """The showcase launcher propagates and validates outcome helper output."""
        run_sh = (example_pipeline_dir / "multi_worker_showcase" / "run.sh").read_text()

        assert "set -euo pipefail" in run_sh
        assert 'OUTCOME_COUNTS="$(bash "$SCRIPT_DIR/outcome_stats.sh" "$DB" "$RUN_ID")"' in run_sh
        assert "IFS='|' read -r TOTAL_ROWS SUCCEEDED FAILED EXTRA_COUNT" in run_sh
        assert '[ -n "${EXTRA_COUNT:-}" ]' in run_sh
        for field in ("TOTAL_ROWS", "SUCCEEDED", "FAILED"):
            assert f'[[ ! "${field}" =~ ^[0-9]+$ ]]' in run_sh
        assert "invalid outcome counts" in run_sh
        assert '2>/dev/null || echo "0|0|0"' not in run_sh
        assert "Failed outcomes:" in run_sh
        assert "Quarantined:" not in run_sh

    def test_blob_transform_offline_launcher_runs_from_clean_copy(self, example_pipeline_dir: Path, tmp_path: Path) -> None:
        """blob_transforms ships a self-contained offline launcher."""
        repository_root = example_pipeline_dir.parent
        tracked_result = subprocess.run(
            ["git", "ls-files", "--", "examples/blob_transforms"],
            cwd=repository_root,
            capture_output=True,
            text=True,
            check=True,
        )
        tracked_paths = {Path(line) for line in tracked_result.stdout.splitlines() if line}
        required_paths = {
            Path("examples/blob_transforms/run.sh"),
            Path("examples/blob_transforms/input/feed_a.csv"),
            Path("examples/blob_transforms/input/feed_b.csv"),
        }
        assert required_paths <= tracked_paths

        for tracked_path in sorted(tracked_paths):
            source_path = repository_root / tracked_path
            copied_path = tmp_path / tracked_path
            copied_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, copied_path)

        copied_example_dir = tmp_path / "examples" / "blob_transforms"
        (tmp_path / ".venv").symlink_to(repository_root / ".venv")

        hosted_state = {
            copied_example_dir / "payloads" / "hosted-sentinel": b"hosted payload sentinel\n",
            copied_example_dir / "runs" / "audit.db": b"hosted audit sentinel\n",
            copied_example_dir / "output" / "tutorial_html_blobs.jsonl": b'{"blob_ref":"hosted-sentinel"}\n',
        }
        for path, content in hosted_state.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        result = subprocess.run(
            ["bash", copied_example_dir / "run.sh"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

        observed_hosted_state = {path: path.read_bytes() if path.is_file() else None for path in hosted_state}
        assert observed_hosted_state == hosted_state

        with (copied_example_dir / "input" / "csv_blob_manifest.csv").open(newline="", encoding="utf-8") as f:
            manifest = list(csv.DictReader(f))
        assert [row["source_name"] for row in manifest] == ["feed_a", "feed_b"]

        blob_refs = [row["blob_ref"] for row in manifest]
        assert len(blob_refs) == 2
        assert len(set(blob_refs)) == 2
        payload_store = FilesystemPayloadStore(copied_example_dir / "payloads" / "offline")
        for source_name, blob_ref in zip(("feed_a", "feed_b"), blob_refs, strict=True):
            assert payload_store.retrieve(blob_ref) == (copied_example_dir / "input" / f"{source_name}.csv").read_bytes()
        assert (copied_example_dir / "runs" / "offline_audit.db").is_file()

        with (copied_example_dir / "output" / "expanded_csv_rows.csv").open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        manifest_by_source = {row["source_name"]: row for row in manifest}
        expected_rows: list[tuple[str, str, str, str, str]] = []
        for source_name in ("feed_a", "feed_b"):
            with (copied_example_dir / "input" / f"{source_name}.csv").open(newline="", encoding="utf-8") as f:
                fixture_rows = list(csv.DictReader(f))
            expected_rows.extend(
                (
                    source_name,
                    manifest_by_source[source_name]["blob_ref"],
                    fixture_row["id"],
                    fixture_row["text"],
                    str(row_index),
                )
                for row_index, fixture_row in enumerate(fixture_rows)
            )

        assert [(row["source_name"], row["blob_ref"], row["id"], row["text"], row["csv_row_index"]) for row in rows] == expected_rows

    def test_blob_transform_documents_canonical_launcher(self, example_pipeline_dir: Path) -> None:
        """blob_transforms documents its clean-checkout launcher everywhere."""
        command = "./examples/blob_transforms/run.sh"
        assert command in (example_pipeline_dir / "blob_transforms" / "README.md").read_text()
        assert command in (example_pipeline_dir / "README.md").read_text()
        assert command in (example_pipeline_dir / "AGENTS.md").read_text()
