"""Only the supported direct planner library remains installed."""

from pathlib import Path

from elspeth.web.composer import pipeline_planner


def test_private_facade_is_retired_but_direct_library_is_supported():
    planner_path = Path(pipeline_planner.__file__)
    assert not planner_path.with_name("planning_application.py").exists()
    assert callable(pipeline_planner.plan_pipeline)
