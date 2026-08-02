"""Containment tests for staged review-bundle paths."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from elspeth_lints.core import review_bundle
from elspeth_lints.core.atomic_io import AtomicWriteSymlinkError
from elspeth_lints.core.review_bundle import ReviewBundle, write_bundle
from elspeth_lints.mcp import server as judge_server


class _BundleIdSubclass(str):
    """Unowned string subtype used to verify exact-string boundaries."""


def _bundle(bundle_id: str) -> ReviewBundle:
    return ReviewBundle(
        bundle_id=bundle_id,
        schema_version=1,
        created_at="2026-08-02T00:00:00+00:00",
        staged_by="containment-test",
        root="src/elspeth",
        allowlist_dir="config/cicd/enforce_tier_model",
        source_rev=None,
        source_dirty=False,
        actions=(),
    )


@pytest.mark.parametrize("bundle_id", ("../escaped", "nested/escaped"))
def test_write_bundle_rejects_bundle_id_path_traversal(tmp_path: Path, bundle_id: str) -> None:
    staged_dir = tmp_path / "staged"

    with pytest.raises(ValueError, match="bundle_id"):
        write_bundle(_bundle(bundle_id), staged_dir=staged_dir)

    assert not (tmp_path / "escaped.json").exists()
    assert not (staged_dir / "nested" / "escaped.json").exists()


def test_write_bundle_rejects_absolute_bundle_id(tmp_path: Path) -> None:
    staged_dir = tmp_path / "staged"
    escaped_path = tmp_path / "absolute-escaped.json"
    bundle = replace(_bundle("placeholder"), bundle_id=str(escaped_path.with_suffix("")))

    with pytest.raises(ValueError, match="bundle_id"):
        write_bundle(bundle, staged_dir=staged_dir)

    assert not escaped_path.exists()


def test_resolve_staged_bundle_path_returns_local_json_path(tmp_path: Path) -> None:
    staged_dir = tmp_path / "staged"
    resolver = getattr(review_bundle, "resolve_staged_bundle_path", None)

    assert resolver is not None
    assert resolver(staged_dir=staged_dir, bundle_id="release-072") == staged_dir / "release-072.json"


@pytest.mark.parametrize("invalid", (_BundleIdSubclass("subclass"), 7))
def test_bundle_path_boundaries_reject_non_exact_strings(tmp_path: Path, invalid: object) -> None:
    resolver = review_bundle.resolve_staged_bundle_path
    invalid_bundle_id = cast(Any, invalid)

    with pytest.raises(ValueError, match="bundle_id"):
        resolver(staged_dir=tmp_path / "staged", bundle_id=invalid_bundle_id)
    with pytest.raises(ValueError, match="bundle_id"):
        judge_server._require_str_arg({"bundle_id": invalid}, "bundle_id")


@pytest.mark.parametrize("tool_name", ("stage_scan", "stage_rekey"))
@pytest.mark.parametrize("invalid", ("", _BundleIdSubclass("subclass"), 7))
def test_optional_bundle_id_tools_reject_supplied_invalid_values(
    tmp_path: Path,
    tool_name: str,
    invalid: object,
) -> None:
    root = tmp_path / "root"
    allowlist_dir = tmp_path / "allowlist"
    root.mkdir()
    allowlist_dir.mkdir()
    ctx = judge_server._ServerContext(
        root=root,
        allowlist_dir=allowlist_dir,
        staged_dir=tmp_path / "staged",
    )
    arguments: dict[str, Any] = {"bundle_id": invalid}
    if tool_name == "stage_rekey":
        arguments.update(old_key_env="OLD_KEY", new_key_env="NEW_KEY")

    outcome = judge_server._run_tool(ctx, tool_name, arguments)

    assert outcome.is_error is True
    assert outcome.text.startswith(f"{tool_name}: argument 'bundle_id'")
    assert not ctx.staged_dir.exists()


@pytest.mark.parametrize("target_exists", (True, False), ids=("existing", "dangling"))
def test_bundle_path_rejects_symlink_to_outside_staged_dir(tmp_path: Path, target_exists: bool) -> None:
    staged_dir = tmp_path / "staged"
    staged_dir.mkdir()
    outside = tmp_path / "outside" / "bundle.json"
    outside.parent.mkdir()
    if target_exists:
        outside.write_text("outside sentinel\n", encoding="utf-8")
    bundle_path = staged_dir / "bundle.json"
    bundle_path.symlink_to(outside)

    with pytest.raises(ValueError, match="outside staged_dir"):
        review_bundle.resolve_staged_bundle_path(staged_dir=staged_dir, bundle_id="bundle")
    with pytest.raises(ValueError, match="outside staged_dir"):
        write_bundle(_bundle("bundle"), staged_dir=staged_dir)

    assert bundle_path.is_symlink()
    if target_exists:
        assert outside.read_text(encoding="utf-8") == "outside sentinel\n"
    else:
        assert not outside.exists()


def test_write_bundle_refuses_symlink_target_within_staged_dir(tmp_path: Path) -> None:
    staged_dir = tmp_path / "staged"
    staged_dir.mkdir()
    target = staged_dir / "target.json"
    target.write_text("inside sentinel\n", encoding="utf-8")
    bundle_path = staged_dir / "bundle.json"
    bundle_path.symlink_to(target)

    with pytest.raises(AtomicWriteSymlinkError, match="symlinked target"):
        write_bundle(_bundle("bundle"), staged_dir=staged_dir)

    assert bundle_path.is_symlink()
    assert target.read_text(encoding="utf-8") == "inside sentinel\n"
