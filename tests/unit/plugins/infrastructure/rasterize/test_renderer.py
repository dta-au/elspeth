from __future__ import annotations

import os
import time

import pytest

from elspeth.plugins.infrastructure.rasterize.protocol import DocumentRefusalKind, RasterizeResponse
from elspeth.plugins.infrastructure.rasterize.renderer import PoolRenderer, RenderLimits, RenderTimedOut
from elspeth.testing.rasterize_fakes import crashing_worker, raising_worker, sleeping_worker
from tests.fixtures.pdf_documents import ENCRYPTED_PDF_PATH, minimal_pdf


def _limits(**overrides: int | bool) -> RenderLimits:
    values: dict[str, int | bool] = {
        "dpi": 72,
        "max_pages": 10,
        "max_page_pixels": 1_000_000,
        "max_page_bytes": 5 * 1024 * 1024,
        "render_timeout_seconds": 20,
        "worker_memory_limit_bytes": 2 * 1024**3,
        "extract_text": True,
        "max_page_text_bytes": 1024 * 1024,
    }
    values.update(overrides)
    return RenderLimits(**values)


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_renders_in_a_subprocess_and_hands_back_an_owned_temp_dir() -> None:
    renderer = PoolRenderer(_limits())
    try:
        result, output_dir = renderer.render(minimal_pdf(2))
        try:
            assert type(result) is RasterizeResponse
            assert output_dir is not None and output_dir.is_dir()
            assert sorted(path.name for path in output_dir.iterdir()) == ["page-1.png", "page-2.png"]
            assert all(page.png_path.parent == output_dir for page in result.rendered)
        finally:
            renderer.discard(output_dir)
        assert output_dir is not None and not output_dir.exists()
    finally:
        renderer.close()


def test_document_refusal_passes_through() -> None:
    renderer = PoolRenderer(_limits())
    try:
        result, output_dir = renderer.render(ENCRYPTED_PDF_PATH.read_bytes())
        renderer.discard(output_dir)
        assert result.kind is DocumentRefusalKind.ENCRYPTED  # type: ignore[union-attr]
    finally:
        renderer.close()


def test_timeout_kills_the_orphan_and_the_renderer_stays_usable() -> None:
    renderer = PoolRenderer(_limits(render_timeout_seconds=1), worker=sleeping_worker)
    try:
        stuck_pids: list[tuple[int, ...]] = []
        result, output_dir = renderer.render(b"%PDF-", on_submitted=stuck_pids.append)
        renderer.discard(output_dir)
        assert result == RenderTimedOut(timeout_seconds=1)
        # the pool the caller observed at submit time really was running the stuck worker
        assert len(stuck_pids) == 1
        assert len(stuck_pids[0]) == 1
        stuck_pid = stuck_pids[0][0]

        # the stuck process must be dead, not orphaned
        assert renderer.live_worker_pids() == ()
        deadline = time.monotonic() + 2.0
        while _pid_is_alive(stuck_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _pid_is_alive(stuck_pid)

        # and a fresh pool serves the next document
        result2, output_dir2 = renderer.render(b"%PDF-")
        renderer.discard(output_dir2)
        assert result2 == RenderTimedOut(timeout_seconds=1)
    finally:
        renderer.close()


def test_worker_death_is_a_tier1_code_bug() -> None:
    renderer = PoolRenderer(_limits(), worker=crashing_worker)
    try:
        with pytest.raises(RuntimeError, match="code bug"):
            renderer.render(b"%PDF-")
    finally:
        renderer.close()


def test_worker_exception_outside_the_protocol_is_a_tier1_code_bug() -> None:
    renderer = PoolRenderer(_limits(), worker=raising_worker)
    try:
        with pytest.raises(RuntimeError, match="code bug"):
            renderer.render(b"%PDF-")
    finally:
        renderer.close()


@pytest.mark.skipif(not os.path.exists("/proc"), reason="fd census needs /proc")
def test_close_leaks_no_file_descriptors() -> None:
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(3):
        renderer = PoolRenderer(_limits())
        _result, output_dir = renderer.render(minimal_pdf(1))
        renderer.discard(output_dir)
        renderer.close()
    assert len(os.listdir("/proc/self/fd")) - before < 10


def test_close_is_idempotent_and_safe_with_no_pool_created() -> None:
    renderer = PoolRenderer(_limits())
    renderer.close()
    renderer.close()


def test_live_worker_pids_is_empty_before_any_render() -> None:
    renderer = PoolRenderer(_limits())
    try:
        assert renderer.live_worker_pids() == ()
    finally:
        renderer.close()
