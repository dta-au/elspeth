"""Tests for shared path resolution helper."""

from __future__ import annotations

from pathlib import Path

import pytest

from elspeth.web.paths import (
    MAX_SESSION_RELATIVE_PATH_CHARS,
    allowed_sink_directories,
    allowed_source_directories,
    locate_in_session_folder,
    normalise_session_relative_path,
    resolve_data_path,
    resolve_sink_data_path,
    session_folder_path,
)


class TestResolveDataPath:
    """Unit tests for resolve_data_path — the single resolution function
    used by both validation and execution service."""

    def test_relative_path_resolved_against_data_dir(self) -> None:
        """A relative path is joined to data_dir before resolving."""
        result = resolve_data_path("blobs/data.csv", "/tmp/data")
        assert result == Path("/tmp/data/blobs/data.csv")

    def test_absolute_path_unchanged(self) -> None:
        """An absolute path resolves to itself (no data_dir involvement)."""
        result = resolve_data_path("/etc/passwd", "/tmp/data")
        assert result == Path("/etc/passwd")

    def test_traversal_resolved(self) -> None:
        """Traversal (../) is resolved by the OS — blocking is the allowlist's job."""
        result = resolve_data_path("../etc/passwd", "/tmp/data")
        assert result == Path("/tmp/etc/passwd")


class TestAllowedSinkDirectories:
    """Session-scoped sink allowlist (elspeth-bdc17cfdb1).

    The blobs entry is confined to the caller's own session subtree —
    blob storage is laid out as ``blobs/<session_id>/<blob_id>_<name>``
    and the sink side must not be able to address another session's
    subtree. Logical paths such as ``outputs/classified.jsonl`` resolve into
    the caller's session-scoped outputs subtree.
    """

    def test_blobs_entry_is_session_scoped(self) -> None:
        result = allowed_sink_directories("/tmp/data", session_id="sess-a")
        assert result == (
            Path("/tmp/data/outputs/sess-a"),
            Path("/tmp/data/blobs/sess-a"),
        )

    def test_none_session_fails_closed_to_no_local_paths(self) -> None:
        """No session identity means neither shared-root reads nor writes."""
        result = allowed_sink_directories("/tmp/data", session_id=None)
        assert result == ()

    def test_other_sessions_subtree_is_not_contained(self) -> None:
        """The boundary this exists for: session A's allowlist must not
        contain session B's blob subtree."""
        allowed = allowed_sink_directories("/tmp/data", session_id="sess-a")
        foreign = Path("/tmp/data/blobs/sess-b/out.csv")
        assert not any(foreign.is_relative_to(d) for d in allowed)

    def test_blobs_root_is_not_contained(self) -> None:
        """A file directly under blobs/ (no session segment) is outside
        every session's allowlist."""
        allowed = allowed_sink_directories("/tmp/data", session_id="sess-a")
        assert not any(Path("/tmp/data/blobs/loose.csv").is_relative_to(d) for d in allowed)

    def test_other_sessions_output_subtree_is_not_contained(self) -> None:
        allowed = allowed_sink_directories("/tmp/data", session_id="sess-a")
        foreign = Path("/tmp/data/outputs/sess-b/out.csv")
        assert not any(foreign.is_relative_to(directory) for directory in allowed)

    @pytest.mark.parametrize(
        "bad_session_id",
        ["", "a/b", "a\\b", "..", ".", "sess/../other"],
    )
    def test_malformed_session_id_rejected(self, bad_session_id: str) -> None:
        """A session id that could alter the path shape is a contract
        violation at this boundary — raise, never silently widen."""
        with pytest.raises(ValueError, match="session_id"):
            allowed_sink_directories("/tmp/data", session_id=bad_session_id)


class TestSessionOwnedLocalPaths:
    def test_source_allowlist_is_session_scoped(self) -> None:
        assert allowed_source_directories("/tmp/data", session_id="sess-a") == (Path("/tmp/data/blobs/sess-a"),)

    def test_source_allowlist_without_session_fails_closed(self) -> None:
        assert allowed_source_directories("/tmp/data", session_id=None) == ()

    def test_logical_output_path_resolves_inside_session_subtree(self) -> None:
        resolved = resolve_sink_data_path("outputs/report.csv", "/tmp/data", session_id="sess-a")
        assert resolved == Path("/tmp/data/outputs/sess-a/report.csv")

    def test_already_scoped_output_path_is_not_double_prefixed(self) -> None:
        resolved = resolve_sink_data_path("outputs/sess-a/report.csv", "/tmp/data", session_id="sess-a")
        assert resolved == Path("/tmp/data/outputs/sess-a/report.csv")

    def test_foreign_absolute_output_is_outside_session_allowlist(self) -> None:
        resolved = resolve_sink_data_path("/tmp/data/outputs/sess-b/report.csv", "/tmp/data", session_id="sess-a")
        allowed = allowed_sink_directories("/tmp/data", session_id="sess-a")
        assert not any(resolved.is_relative_to(directory) for directory in allowed)

    def test_foreign_relative_session_output_is_not_adopted(self) -> None:
        own_session = "11111111-1111-4111-8111-111111111111"
        foreign_session = "22222222-2222-4222-8222-222222222222"
        resolved = resolve_sink_data_path(
            f"outputs/{foreign_session}/report.csv",
            "/tmp/data",
            session_id=own_session,
        )
        allowed = allowed_sink_directories("/tmp/data", session_id=own_session)

        assert resolved == Path(f"/tmp/data/outputs/{foreign_session}/report.csv")
        assert not any(resolved.is_relative_to(directory) for directory in allowed)

    def test_nested_logical_output_path_is_scoped_to_session(self) -> None:
        resolved = resolve_sink_data_path("outputs/reports/daily.csv", "/tmp/data", session_id="sess-a")
        assert resolved == Path("/tmp/data/outputs/sess-a/reports/daily.csv")

    def test_symlink_from_owned_output_to_foreign_output_is_outside_allowlist(self, tmp_path: Path) -> None:
        own_root = tmp_path / "outputs" / "sess-a"
        foreign_root = tmp_path / "outputs" / "sess-b"
        own_root.mkdir(parents=True)
        foreign_root.mkdir(parents=True)
        (own_root / "foreign-link").symlink_to(foreign_root, target_is_directory=True)

        resolved = resolve_sink_data_path("outputs/foreign-link/report.csv", str(tmp_path), session_id="sess-a")
        allowed = allowed_sink_directories(str(tmp_path), session_id="sess-a")

        assert not any(resolved.is_relative_to(directory) for directory in allowed)


TRAVERSAL_PAYLOADS = (
    "../../etc/passwd",
    "/etc/passwd",
    "outputs/../../../etc/passwd",
    "outputs/./../../etc/shadow",
    "outputs/../../blobs/sess-b/secret.csv",
    "outputs/sess-a/../../sess-b/report.csv",
)


class TestResolutionNeverAdoptsAnEscapingPath:
    """The containment invariant every caller of these resolvers depends on.

    These resolvers deliberately do NOT block traversal — they canonicalize,
    and the caller decides containment with ``is_relative_to`` against
    ``allowed_*_directories``. That split is only safe while resolution never
    *adopts* an escaping value into the allowlist, so this class pins the
    property rather than the caller's check.

    It is also the standing evidence behind the dismissal of CodeQL alerts
    1241/1242/1243 (``py/path-injection`` at ``paths.py`` lines 39/41/124),
    which flag the ``Path.resolve()`` calls below as taint sinks. The flagged
    call is the normalization step the containment check depends on: it reads
    no file content and creates nothing, and every caller checks containment
    on its result before any real filesystem access. If a change makes one of
    these assertions fail, that dismissal is no longer true and the alerts
    must be re-opened rather than re-dismissed.
    """

    @pytest.mark.parametrize("payload", TRAVERSAL_PAYLOADS)
    def test_sink_traversal_never_resolves_inside_the_allowlist(self, payload: str) -> None:
        resolved = resolve_sink_data_path(payload, "/tmp/data", session_id="sess-a")
        allowed = allowed_sink_directories("/tmp/data", session_id="sess-a")

        assert not any(resolved.is_relative_to(directory) for directory in allowed)

    @pytest.mark.parametrize("payload", TRAVERSAL_PAYLOADS)
    def test_source_traversal_never_resolves_inside_the_allowlist(self, payload: str) -> None:
        resolved = resolve_data_path(payload, "/tmp/data")
        allowed = allowed_source_directories("/tmp/data", session_id="sess-a")

        assert not any(resolved.is_relative_to(directory) for directory in allowed)

    @pytest.mark.parametrize("payload", TRAVERSAL_PAYLOADS)
    def test_traversal_without_a_session_identity_has_no_allowed_target(self, payload: str) -> None:
        """Fail-closed: with no session identity the allowlist is empty, so no
        resolved value — escaping or not — can ever be contained."""
        resolved = resolve_sink_data_path(payload, "/tmp/data", session_id=None)
        allowed = allowed_sink_directories("/tmp/data", session_id=None)

        assert not any(resolved.is_relative_to(directory) for directory in allowed)
        assert allowed == ()


class TestSessionFolderRelativePaths:
    """Locate a resolved path inside a session's folders and rebuild it in another session."""

    @pytest.mark.parametrize(
        ("resolved", "expected"),
        [
            ("/tmp/data/outputs/sess-a/report.csv", ("outputs", Path("report.csv"))),
            ("/tmp/data/outputs/sess-a/nested/deep/out.jsonl", ("outputs", Path("nested/deep/out.jsonl"))),
            ("/tmp/data/blobs/sess-a/out.jsonl", ("blobs", Path("out.jsonl"))),
            ("/tmp/data/outputs/sess-a", ("outputs", Path("."))),
        ],
    )
    def test_locates_a_path_inside_the_session_folders(self, resolved: str, expected: tuple[str, Path]) -> None:
        assert locate_in_session_folder(Path(resolved), "/tmp/data", session_id="sess-a") == expected

    @pytest.mark.parametrize(
        "resolved",
        [
            "/tmp/data/outputs/sess-b/report.csv",
            "/tmp/data/outputs/report.csv",
            "/tmp/data/blobs/report.csv",
            "/tmp/data/other/sess-a/report.csv",
            "/etc/passwd",
        ],
    )
    def test_a_path_outside_the_session_folders_is_not_located(self, resolved: str) -> None:
        assert locate_in_session_folder(Path(resolved), "/tmp/data", session_id="sess-a") is None

    def test_session_folder_path_builds_under_the_named_session(self) -> None:
        assert session_folder_path("/tmp/data", "blobs", session_id="sess-b", relative_path="nested/out.jsonl") == Path(
            "/tmp/data/blobs/sess-b/nested/out.jsonl"
        )

    @pytest.mark.parametrize("session_id", ["", ".", "..", "a/b", "a\\b"])
    def test_malformed_session_id_is_refused(self, session_id: str) -> None:
        with pytest.raises(ValueError, match="malformed session_id"):
            locate_in_session_folder(Path("/tmp/data/outputs/x"), "/tmp/data", session_id=session_id)
        with pytest.raises(ValueError, match="malformed session_id"):
            session_folder_path("/tmp/data", "outputs", session_id=session_id, relative_path="x")

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("report.csv", "report.csv"),
            ("nested/deep/out.jsonl", "nested/deep/out.jsonl"),
            ("nested/./out.jsonl", "nested/out.jsonl"),
            ("nested//out.jsonl", "nested/out.jsonl"),
            ("./report.csv", "report.csv"),
        ],
    )
    def test_normalise_accepts_and_normalises_a_relative_path(self, value: str, expected: str) -> None:
        assert normalise_session_relative_path(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "",
            ".",
            "./",
            "/etc/passwd",
            "//server/share",
            "../x",
            "nested/../../x",
            "nested/../x",
            "..\\x",
            "nested\\out.csv",
            "bad\x00name",
        ],
    )
    def test_normalise_refuses_empty_absolute_dotdot_and_backslash(self, value: str) -> None:
        with pytest.raises(ValueError):
            normalise_session_relative_path(value)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("a\nINFO fake log line.csv", id="newline"),
            pytest.param("a\rb.csv", id="carriage-return"),
            pytest.param("a\tb.csv", id="tab"),
            pytest.param("a\x1bb.csv", id="escape"),
            pytest.param("a\x7fb.csv", id="delete"),
        ],
    )
    def test_normalise_refuses_control_characters(self, value: str) -> None:
        with pytest.raises(ValueError, match="control character"):
            normalise_session_relative_path(value)

    def test_normalise_bounds_the_path_length(self) -> None:
        longest = "a" * MAX_SESSION_RELATIVE_PATH_CHARS
        assert MAX_SESSION_RELATIVE_PATH_CHARS == 1024
        assert normalise_session_relative_path(longest) == longest
        with pytest.raises(ValueError, match="longer than 1024"):
            normalise_session_relative_path(longest + "a")
