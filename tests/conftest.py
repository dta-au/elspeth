# tests/conftest.py
"""Root conftest for test suite v2.

Responsibilities:
- Register ALL pytest markers
- Register Hypothesis profiles (ci, nightly, debug)
- Auto-mark tests by directory location
- Autouse secrets fixture for CI parity
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import Phase, Verbosity, settings

# Belt-and-suspenders fence: the Tier-1 guards in production code are
# explicit ``raise AuditIntegrityError`` (survives ``python -O``), but a
# handful of existing tests still use plain ``assert`` statements in
# arrange/act lines.  Running the suite under ``-O`` would silently erase
# those assertions, turning coverage-theatre into green-on-broken.  We
# refuse to import the suite under an optimised interpreter so the
# failure is loud and unmistakable.  Expressed as an ``if / raise`` (not
# ``assert``) because ``-O`` strips asserts at import time.
if sys.flags.optimize != 0:
    raise RuntimeError(
        "ELSPETH tests must not run under `python -O` — assert statements are stripped, "
        "which silently disables assertion-based test contracts.  Re-run without -O."
    )

# ---------------------------------------------------------------------------
# DeclarationContract registry population
# ---------------------------------------------------------------------------
#
# ADR-010 Phase 2A introduced a set-equality bootstrap check against
# EXPECTED_CONTRACTS (issue elspeth-b03c6112c0 / C2). Every contract in
# the manifest MUST be registered before ``prepare_for_run()`` is called,
# or bootstrap raises. Registration is a module-import side effect — see
# ``src/elspeth/contracts/declaration_contracts.py`` CLOSED-SET comment.
#
# Test files that invoke the orchestrator (directly or via ``elspeth
# run``) but do not transitively import the contract-defining executors
# hit an empty-registry bootstrap failure. In xdist-distributed runs the
# failure manifests non-deterministically depending on which worker
# receives which test file first. Importing the authoritative production
# bootstrap surface at root-conftest level populates the registry once
# per pytest process (xdist workers included) so every test starts from
# the same contract set production uses. Individual tests that need to
# clear or mutate the registry use the
# ``_snapshot_registry_for_tests`` / ``_restore_registry_snapshot_for_tests``
# helpers, which are pytest-gated (issue elspeth-cc511e7234 / C3).
import elspeth.engine.executors.declaration_contract_bootstrap  # noqa: F401

pytest_plugins = ["tests.fixtures.azurite"]

# ---------------------------------------------------------------------------
# Marker Registration
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers for test tiers."""
    config.addinivalue_line("markers", "integration: multi-component tests with real DB")
    config.addinivalue_line("markers", "e2e: full pipeline, real I/O, file-based DB")
    config.addinivalue_line("markers", "performance: benchmarks and regression detection")
    config.addinivalue_line("markers", "stress: load tests requiring ChaosLLM HTTP server")
    config.addinivalue_line("markers", "slow: long-running tests (>10s)")
    config.addinivalue_line(
        "markers",
        "live_provider: protected tests that cross a real remote-provider boundary",
    )
    config.addinivalue_line(
        "markers",
        "composer_llm_eval: characterization/replay tests for the 2026-04-28 composer LLM evaluation scenarios",
    )
    config.addinivalue_line(
        "markers",
        "chaosllm(preset=None, **kwargs): Configure ChaosLLM server for the test. "
        "Use preset='name' to load a preset, and keyword args to override specific settings.",
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the operator-only live-provider execution gate."""
    group = parser.getgroup("live-provider")
    group.addoption(
        "--run-live-provider",
        action="store_true",
        default=False,
        help="Authorize execution of tests marked live_provider (credentials alone never authorize execution).",
    )


# ---------------------------------------------------------------------------
# Auto-Marking by Directory
# ---------------------------------------------------------------------------


def _is_declared_remote_service_node(item: pytest.Item) -> bool:
    """Return whether repository placement or a legacy marker declares remote I/O."""
    path = item.path.as_posix()
    return (
        item.get_closest_marker("live_aws") is not None
        or item.get_closest_marker("live_azure") is not None
        or item.get_closest_marker("live_dataverse") is not None
        or item.get_closest_marker("live_chroma") is not None
        or ("/tests/integration/plugins/" in path and path.endswith("_live.py"))
        or path.endswith("/tests/integration/web/composer/test_bedrock_live_smoke.py")
        or path.endswith("/tests/e2e/recovery/test_run_lifecycle_aws_live.py")
    )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-mark test tiers and reject uncontained remote-service tests."""
    uncontained_remote_nodes: list[str] = []
    for item in items:
        path = item.path.as_posix()
        if "/e2e/" in path:
            item.add_marker(pytest.mark.e2e)
        elif "/performance/" in path and "/stress/" in path:
            item.add_marker(pytest.mark.stress)
            item.add_marker(pytest.mark.performance)
        elif "/performance/" in path:
            item.add_marker(pytest.mark.performance)
        # integration/ tests get marker from their conftest
        if _is_declared_remote_service_node(item) and item.get_closest_marker("live_provider") is None:
            uncontained_remote_nodes.append(item.nodeid)
    if uncontained_remote_nodes:
        joined = "\n".join(f"- {node_id}" for node_id in sorted(uncontained_remote_nodes))
        raise pytest.UsageError("remote-service test nodes must carry @pytest.mark.live_provider:\n" + joined)
    selected_live_nodes = [item.nodeid for item in items if item.get_closest_marker("live_provider") is not None]
    if selected_live_nodes and not config.getoption("run_live_provider") and not config.getoption("collectonly"):
        # Collection-only inventory (the selector-manifest validator's exact
        # node accounting) is authority-free; execution stays double-gated by
        # this check and pytest_runtest_setup below.
        raise pytest.UsageError("--run-live-provider is required to select @pytest.mark.live_provider nodes")


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Require explicit CLI authority independently of dotenv credentials."""
    if item.get_closest_marker("live_provider") is not None and not item.config.getoption("run_live_provider"):
        raise pytest.UsageError("--run-live-provider is required to execute @pytest.mark.live_provider nodes")


# ---------------------------------------------------------------------------
# Hypothesis Profiles
# ---------------------------------------------------------------------------

settings.register_profile(
    "ci",
    max_examples=100,
    deadline=None,
    phases=[Phase.explicit, Phase.reuse, Phase.generate, Phase.shrink],
)
settings.register_profile(
    "nightly",
    max_examples=1000,
    deadline=None,
    phases=[Phase.explicit, Phase.reuse, Phase.generate, Phase.shrink],
)
settings.register_profile(
    "debug",
    max_examples=10,
    deadline=None,
    verbosity=Verbosity.verbose,
    phases=[Phase.explicit, Phase.reuse, Phase.generate, Phase.shrink],
)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "ci"))


@pytest.fixture(autouse=True)
def _allow_raw_secrets_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow raw secrets in all tests — CI has no .env file.

    Locally, .env sets ELSPETH_ALLOW_RAW_SECRETS=true which bypasses
    the fingerprint key requirement for test API keys.  CI doesn't load
    .env, so tests that create AuditedHTTPClient with auth headers fail
    with FrameworkBugError.  This fixture ensures consistent behaviour.
    """
    monkeypatch.setenv("ELSPETH_ALLOW_RAW_SECRETS", "true")


@pytest.fixture(autouse=True)
def _isolate_planner_authoring_aids_memo() -> Iterator[None]:
    """Clear the process-global authoring-aids memo between tests.

    ``build_planner_authoring_aids`` memoizes on ``snapshot_hash`` alone
    (``planner_authoring_aids._AIDS_MEMO``). That key is sound in production —
    one plugin registry per process, so equal availability implies equal
    catalog CONTENT — but tests violate the premise. ``StubCatalog`` in
    ``test_prompts.py`` and ``_mock_catalog`` in
    ``tests/unit/web/composer/conftest.py`` expose the identical plugin id set
    (csv / passthrough / csv) and therefore hash identically, while carrying
    different ``composer_hints``. Whichever built first won, and the other
    silently received its aids.

    That surfaced as ``test_context_includes_discovery_time_composer_hints``
    failing only under the full suite, at whatever xdist distribution put both
    on one worker — invisible to any scoped run. Isolate the memo rather than
    weaken the production key, which is a measured cost optimisation
    (elspeth-a79f1b2e6b).
    """
    from elspeth.web.composer.planner_authoring_aids import _AIDS_MEMO

    _AIDS_MEMO.clear()
    yield
    _AIDS_MEMO.clear()


@pytest.fixture(autouse=True, scope="session")
def _sink_effect_spool_outside_repo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Keep the default sink-effect spool out of the repository tree.

    The remote-object spool defaults to the project-local
    ``.elspeth/sink-effect-spool`` (CWD-relative), so tests that prepare
    remote effects without an explicit override would otherwise write into
    the checkout. Tests asserting spool behaviour still win: function-scoped
    monkeypatch.setenv/delenv overrides this session-level default.
    """
    patch = pytest.MonkeyPatch()
    patch.setenv("ELSPETH_EFFECT_SPOOL_DIR", str(tmp_path_factory.mktemp("sink-effect-spool")))
    yield
    patch.undo()


_REPO_ROOT = Path(__file__).resolve().parent.parent
_REPO_ELSPETH = _REPO_ROOT / ".elspeth"
_WRITE_OPEN_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


def _in_elspeth_root(candidate: object, root: Path) -> Path | None:
    """Return the resolved path when ``candidate`` targets ``root``, else None."""
    if not isinstance(candidate, (str, bytes, os.PathLike)):
        return None
    raw_candidate = os.fspath(candidate)
    if isinstance(raw_candidate, bytes):
        raw_candidate = raw_candidate.decode(errors="replace")

    resolved = Path(raw_candidate).expanduser().resolve()
    if resolved == root or root in resolved.parents:
        return resolved
    return None


def _in_repo_elspeth(candidate: object) -> Path | None:
    """Return the resolved path when it targets ``<repo>/.elspeth``, else None."""
    return _in_elspeth_root(candidate, _REPO_ELSPETH)


def _write_open_requested(mode: object, flags: object) -> bool:
    """Return whether an ``open`` audit event requests write authority."""
    if isinstance(mode, str) and any(marker in mode for marker in ("w", "a", "x", "+")):
        return True
    return isinstance(flags, int) and bool(flags & _WRITE_OPEN_FLAGS)


def _elspeth_tree_snapshot(root: Path) -> bytes:
    """Return a compact fingerprint of path and inode metadata beneath ``root``."""
    digest = hashlib.sha256()

    def add_entry(relative_path: Path, entry_stat: os.stat_result) -> None:
        path_bytes = os.fsencode(relative_path.as_posix())
        metadata = (
            entry_stat.st_dev,
            entry_stat.st_ino,
            entry_stat.st_mode,
            entry_stat.st_nlink,
            entry_stat.st_uid,
            entry_stat.st_gid,
            entry_stat.st_size,
            entry_stat.st_mtime_ns,
            entry_stat.st_ctime_ns,
        )
        record = path_bytes + b"\0" + b"\0".join(str(value).encode("ascii") for value in metadata)
        digest.update(len(record).to_bytes(8, byteorder="big"))
        digest.update(record)

    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return digest.digest()

    add_entry(Path("."), root_stat)
    if not stat.S_ISDIR(root_stat.st_mode):
        return digest.digest()

    def raise_walk_error(error: OSError) -> None:
        raise error

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=raise_walk_error):
        dirnames.sort(key=os.fsencode)
        filenames.sort(key=os.fsencode)
        directory = Path(dirpath)
        for name in (*dirnames, *filenames):
            entry = directory / name
            add_entry(entry.relative_to(root), entry.lstat())

    return digest.digest()


@pytest.fixture(autouse=True, scope="session")
def _refuse_in_repo_elspeth_writes() -> Iterator[None]:
    """Fail closed when the suite writes into the checkout's ``.elspeth/``.

    Several settings default to CWD-relative ``.elspeth/`` paths and pytest
    runs with the checkout as CWD, so a test that drives a real run without
    redirecting them silently accumulates state in the developer's tree. It is
    gitignored, so ``git status`` never surfaces it. Two defaults have already
    done this: ``payload_store.base_path`` (~800K per full run) and the
    sign-bundle rotation log.

    Three layers, because no single one covers every write:

    * Python's ``open`` audit event covers built-in, ``pathlib``, and
      ``os.open`` write modes. Refusing there catches writes beneath an
      existing directory as well as overwrites of an existing file.
    * ``os.mkdir`` is the choke point for ``Path.mkdir`` and ``os.makedirs``.
      Refusing there names the offending test and, by raising before the call,
      keeps the directory from being created at all.
    * The fd-relative walk in ``core.audit_export_content_store`` passes bare
      component names against a parent descriptor. The ``os.mkdir`` patch
      resolves ``dir_fd`` through ``/proc/self/fd`` (Linux), so directory
      creation there is attributed to the running test; fd-relative file
      writes beneath pre-existing directories, subprocess writes, and
      native-extension writes fall to the recursive session-end fingerprint.

    Fix a firing guard at the test, never by relaxing this fixture:

    * ``payload_store.base_path`` is user-configurable — declare it under
      ``tmp_path`` in the test's settings.
    * ``landscape.export`` ``spool_root`` / ``content_store.root`` are
      code-owned and reject absolute paths, so CWD is the only lever —
      use ``monkeypatch.chdir(tmp_path)``.
    """
    before = _elspeth_tree_snapshot(_REPO_ELSPETH)
    guarded_root = _REPO_ELSPETH
    audit_active = True

    def guard_write_open(event: str, args: tuple[object, ...]) -> None:
        if not audit_active or event != "open" or len(args) < 3:
            return
        candidate, mode, flags = args[:3]
        if not _write_open_requested(mode, flags):
            return
        resolved = _in_elspeth_root(candidate, guarded_root)
        if resolved is not None:
            pytest.fail(
                f"Test wrote into the repository checkout: {resolved}\n"
                f"A CWD-relative `.elspeth/` default was left unredirected. Redirect it in "
                f"the test — see tests/conftest.py::_refuse_in_repo_elspeth_writes.",
                pytrace=True,
            )

    sys.addaudithook(guard_write_open)

    original_mkdir = os.mkdir

    def guarded_mkdir(path: object, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        candidate = os.fspath(path) if isinstance(path, (str, bytes, os.PathLike)) else None
        if isinstance(candidate, bytes):
            candidate = candidate.decode(errors="replace")
        if dir_fd is not None and candidate is not None:
            # dir_fd calls carry a bare component name; on Linux the parent
            # directory is recoverable from the descriptor, which lets this
            # layer name the offending test instead of deferring to the
            # anonymous session-end sweep.
            try:
                parent = os.readlink(f"/proc/self/fd/{dir_fd}")
            except OSError:
                parent = None
            candidate = os.path.join(parent, candidate) if parent is not None else None
        # Cheap pre-filter: almost no mkdir in the suite mentions .elspeth,
        # and resolve() is a syscall we should not pay on every call.
        if candidate is not None and ".elspeth" in candidate:
            resolved = _in_repo_elspeth(candidate)
            if resolved is not None:
                # BaseException-derived (pytest.fail), so the `except OSError`
                # and `suppress(FileExistsError)` guards around production
                # mkdir calls cannot swallow it.
                pytest.fail(
                    f"Test wrote into the repository checkout: {resolved}\n"
                    f"A CWD-relative `.elspeth/` default was left unredirected. Redirect it in "
                    f"the test — see tests/conftest.py::_refuse_in_repo_elspeth_writes.",
                    pytrace=True,
                )
        return original_mkdir(path, mode, dir_fd=dir_fd)  # type: ignore[arg-type]

    patch = pytest.MonkeyPatch()
    patch.setattr(os, "mkdir", guarded_mkdir)
    try:
        yield
    finally:
        audit_active = False
        patch.undo()

        after = _elspeth_tree_snapshot(_REPO_ELSPETH)
        if after != before:
            pytest.fail(
                f"Contents under {_REPO_ELSPETH} changed while this worker ran.\n"
                f"This is the anonymous end-of-session sweep: the writer was NOT the test "
                f"named above (the per-write hooks would have failed it directly). Either a "
                f"test wrote through a channel the hooks cannot see (subprocess, fd-relative "
                f"file write), or a concurrent process outside pytest wrote into the checkout "
                f"— e.g. an `elspeth run` of an example, or a signing/staging tool. Compare "
                f"mtimes under {_REPO_ELSPETH} against the suite window before hunting in the "
                f"suite — see tests/conftest.py::_refuse_in_repo_elspeth_writes.",
                pytrace=False,
            )


@pytest.fixture(autouse=True)
def _freeze_runtime_val_registries_before_begin_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirror the runtime-VAL manifest precondition for direct repository tests.

    Production reaches ``RunLifecycleRepository.begin_run()`` only after
    orchestrator bootstrap has sealed the declaration and Tier-1 registries.
    Many tests exercise the repository layer directly or register test-only
    declaration contracts that are intentionally outside
    ``EXPECTED_CONTRACT_SITES``. For those paths we freeze the current test
    registry state immediately before ``begin_run()`` rather than forcing the
    full production bootstrap equality check.

    Empty declaration registries are left unfrozen so ``begin_run()`` still
    fails closed when a test genuinely models the unsafe path.
    """
    import functools

    from elspeth.contracts.declaration_contracts import (
        freeze_declaration_registry,
        registered_declaration_contracts,
    )
    from elspeth.contracts.tier_registry import _TIER_1_ERRORS_VIEW, freeze_tier_registry
    from elspeth.core.landscape.run_lifecycle_repository import RunLifecycleRepository

    original_begin_run = RunLifecycleRepository.begin_run

    @functools.wraps(original_begin_run)
    def wrapped_begin_run(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if registered_declaration_contracts():
            freeze_declaration_registry()
        if len(_TIER_1_ERRORS_VIEW) > 0:
            freeze_tier_registry()
        # OpenRouter catalog snapshot is mandatory at the runs row.  In
        # production, the L3 entry point (web lifespan / CLI bootstrap)
        # resolves these via ``read_openrouter_catalog_snapshot_id``;
        # the resulting tuple is non-None by construction (the bundled
        # fallback is always available).  Unit tests construct repos
        # directly and bypass that resolution — supply a deterministic
        # synthetic snapshot so tests don't need to thread the value
        # through every call site.  Tests that exercise the snapshot
        # contract supply their own values (or call the real reader).
        kwargs.setdefault("openrouter_catalog_sha256", "0" * 64)
        kwargs.setdefault("openrouter_catalog_source", "bundled")
        return original_begin_run(self, *args, **kwargs)

    monkeypatch.setattr(RunLifecycleRepository, "begin_run", wrapped_begin_run)

    # Mirror the snapshot defaulting at ``orchestrator.run`` so tests
    # invoking the orchestrator directly (without the L3 entry-point
    # wiring that resolves the snapshot in production) don't need to
    # thread the value through every call site. Production callers
    # (web/execution/service.py, cli.py, cli_helpers.py) always supply
    # both fields explicitly.  ``functools.wraps`` preserves
    # ``inspect.signature(Orchestrator.run)`` so signature-introspection
    # tests still see the real parameter names.
    from elspeth.engine.orchestrator.core import Orchestrator

    original_orch_run = Orchestrator.run

    def with_test_sink_effect_modes(config):  # type: ignore[no-untyped-def]
        """Mirror RuntimePluginFactory mode resolution for test-only sinks."""
        from dataclasses import replace
        from unittest.mock import Mock

        from elspeth.contracts import (
            ResolvedSinkEffectMode,
            SinkEffectContract,
            SinkEffectExecutionPurpose,
            SinkEffectInputKind,
        )
        from elspeth.engine.orchestrator import PipelineConfig
        from elspeth.engine.orchestrator.preflight import validate_pipeline_sink_effect_capabilities

        if isinstance(config, Mock):
            # Resume unit tests use a spec-bound PipelineConfig mock for fields
            # unrelated to sink execution. Give the new admission boundary an
            # exact empty surface instead of letting auto-created Mock fields
            # masquerade as a forged admission receipt.
            config.sinks = {}
            config.sink_effect_modes = {}
            config.sink_effect_admission = None
            return config
        if type(config) is not PipelineConfig:
            return config
        modes: dict[str, str] = dict(config.sink_effect_modes)
        if not modes:
            for sink_name, sink in config.sinks.items():
                # Nominal, not structural: production admission
                # (``preflight._resolved_modes_for_sinks``) gates on this exact
                # marker class before calling the adapter-owned resolver, and
                # ``BaseSink`` declares it, so every sink that owns a mode
                # resolver is a ``SinkEffectContract``. A legacy sink that
                # declares nothing simply has no mode to contribute.
                if not isinstance(sink, SinkEffectContract):
                    continue
                resolved = type(sink)._resolve_sink_effect_mode(dict(sink.config), purpose=SinkEffectExecutionPurpose.FRESH)
                if resolved is None:
                    continue
                if not isinstance(resolved, ResolvedSinkEffectMode):
                    raise TypeError("test sink effect resolver must return ResolvedSinkEffectMode")
                modes[sink_name] = resolved.value
        admission = validate_pipeline_sink_effect_capabilities(
            config.sinks,
            configured_modes=modes,
            required_input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
        )
        return replace(config, sink_effect_modes=modes, sink_effect_admission=admission)

    @functools.wraps(original_orch_run)
    def wrapped_orch_run(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        # Direct orchestrator tests bypass RuntimePluginFactory, which is the
        # production authority that resolves and binds sink-effect modes. Keep
        # test-only sink fixtures honest under the same effect-only runtime by
        # resolving their declared mode before the call. Production sink types
        # and tests that provide an explicit mode map are left untouched.
        if "config" in kwargs:
            kwargs["config"] = with_test_sink_effect_modes(kwargs["config"])
        elif args:
            args = (with_test_sink_effect_modes(args[0]), *args[1:])
        kwargs.setdefault("openrouter_catalog_sha256", "0" * 64)
        kwargs.setdefault("openrouter_catalog_source", "bundled")
        return original_orch_run(self, *args, **kwargs)

    monkeypatch.setattr(Orchestrator, "run", wrapped_orch_run)

    original_orch_resume = Orchestrator.resume

    @functools.wraps(original_orch_resume)
    def wrapped_orch_resume(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if "config" in kwargs:
            kwargs["config"] = with_test_sink_effect_modes(kwargs["config"])
        elif len(args) >= 2:
            args = (args[0], with_test_sink_effect_modes(args[1]), *args[2:])
        return original_orch_resume(self, *args, **kwargs)

    monkeypatch.setattr(Orchestrator, "resume", wrapped_orch_resume)

    # ExecutionServiceImpl: in production the lifespan calls
    # ``set_openrouter_catalog_snapshot()`` after construction. Tests
    # construct the service directly and skip that step; auto-prime the
    # synthetic snapshot in ``__init__`` so ``_run_pipeline`` doesn't
    # raise on the wiring check. Production ``app.py`` still calls
    # ``set_openrouter_catalog_snapshot`` explicitly — and the setter
    # validates inputs — so this wrapper doesn't mask production bugs.
    from elspeth.web.execution.service import ExecutionServiceImpl

    original_exec_init = ExecutionServiceImpl.__init__

    @functools.wraps(original_exec_init)
    def wrapped_exec_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        original_exec_init(self, *args, **kwargs)
        self._openrouter_catalog_sha256 = "0" * 64
        self._openrouter_catalog_source = "bundled"

    monkeypatch.setattr(ExecutionServiceImpl, "__init__", wrapped_exec_init)


@pytest.fixture(autouse=True)
def _restore_runtime_val_registries_after_each_test() -> None:
    """Restore runtime-VAL registries and fail on leaked registry membership.

    Tests may freeze registries through production bootstrap paths; that flag
    is restored without failing. Membership and reason/site-map mutations must
    be restored by the test that made them, because leaking synthetic contracts
    or Tier-1 classes changes every later runtime-VAL manifest.
    """
    import elspeth.contracts.declaration_contracts as dc
    import elspeth.contracts.tier_registry as tr

    with dc._REGISTRY_LOCK:
        saved_dc_registry = list(dc._REGISTRY)
        saved_dc_per_site = {site: list(lst) for site, lst in dc._REGISTRY_BY_SITE.items()}
        saved_dc_frozen = dc._FROZEN

    with tr._REGISTRY_LOCK:
        saved_tr_registry = list(tr._REGISTRY)
        saved_tr_reasons = dict(tr._REASONS)
        saved_tr_frozen = tr._FROZEN

    yield

    leaked: list[str] = []
    with dc._REGISTRY_LOCK:
        if saved_dc_registry != dc._REGISTRY or any(saved_dc_per_site[site] != dc._REGISTRY_BY_SITE[site] for site in dc.DispatchSite):
            leaked.append("declaration-contract registry")
        dc._REGISTRY[:] = saved_dc_registry
        for site in dc.DispatchSite:
            dc._REGISTRY_BY_SITE[site][:] = saved_dc_per_site[site]
        dc._FROZEN = saved_dc_frozen

    with tr._REGISTRY_LOCK:
        if saved_tr_registry != tr._REGISTRY or saved_tr_reasons != tr._REASONS:
            leaked.append("Tier-1 error registry")
        tr._REGISTRY[:] = saved_tr_registry
        tr._REASONS.clear()
        tr._REASONS.update(saved_tr_reasons)
        tr._FROZEN = saved_tr_frozen

    if leaked:
        pytest.fail(f"Runtime-VAL registry state leaked from test: {', '.join(leaked)}")
