"""Plugin review 2026-11-11, P5: sandboxed template environments are value-free by default.

Finding #7, maintainer ruling 2026-11-14 (option b). ``create_sandboxed_environment``
defaults to value-free render failures; a caller whose render context can never
carry row data opts in to raw diagnostics explicitly with ``value_free=False``.
A context that carries a ``TemplateRow`` stays value-free whatever the flag says.
The default tests fail at an assertion on the code the finding describes; the
opt-in and row-guard tests hold the behaviour that must survive the flip.
"""

from __future__ import annotations

import inspect
from typing import Any

_SENTINEL = "CONTEXTVALUESENTINEL"


def _failure_text(template: Any, **context: object) -> str:
    try:
        template.render(**context)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    raise AssertionError("the render was expected to fail")


def test_create_sandboxed_environment_defaults_to_value_free() -> None:
    from elspeth.plugins.infrastructure.templates import create_sandboxed_environment

    parameter = inspect.signature(create_sandboxed_environment).parameters["value_free"]
    assert parameter.default is True


def test_default_environment_withholds_context_values_without_a_row() -> None:
    """A new caller that forgets to choose gets value-free failures, row or not."""
    from elspeth.plugins.infrastructure.templates import create_sandboxed_environment

    template = create_sandboxed_environment().from_string("{{ {}[key] }}")
    text = _failure_text(template, key=_SENTINEL)
    assert _SENTINEL not in text
    assert text.startswith("TemplateError: ")


def test_operator_only_opt_in_keeps_raw_diagnostics() -> None:
    from elspeth.plugins.infrastructure.templates import create_sandboxed_environment

    template = create_sandboxed_environment(value_free=False).from_string("{{ {}[run_id] }}")
    text = _failure_text(template, run_id=_SENTINEL)
    assert _SENTINEL in text


def test_opt_in_is_still_value_free_for_a_row_carrying_context() -> None:
    """The row-carrying guard survives the explicit opt-in."""
    from elspeth.plugins.infrastructure.templates import ALL_FIELDS, TemplateRow, create_sandboxed_environment
    from elspeth.testing import make_pipeline_row

    row = TemplateRow.project(make_pipeline_row({"q": _SENTINEL}), ALL_FIELDS)
    template = create_sandboxed_environment(value_free=False).from_string("{{ {}[row.q] }}")
    text = _failure_text(template, row=row)
    assert _SENTINEL not in text
    assert text.startswith("TemplateError: ")


def test_azure_blob_path_render_opts_in_to_raw_diagnostics() -> None:
    """The blob path renders only run_id and timestamp, so it is an operator-only context."""
    from elspeth.plugins.sinks.azure_blob_sink import AzureBlobSink

    sink = AzureBlobSink(
        {
            "connection_string": "DefaultEndpointsProtocol=https;AccountName=fake;AccountKey=ZmFrZQ==;EndpointSuffix=core.windows.net",
            "container": "test-container",
            "blob_path": "{{ {}[run_id] }}",
            "schema": {"mode": "observed"},
        }
    )
    text = _failure_text(sink._blob_path_compiled, run_id=_SENTINEL, timestamp="2026-11-14T00:00:00+00:00")
    assert _SENTINEL in text


def test_value_free_render_reports_the_output_bound_in_full() -> None:
    """ELSPETH's own output bound names no value, so value-free rendering keeps its text."""
    from elspeth.plugins.infrastructure.templates import ALL_FIELDS, SandboxedTemplate, TemplateRow
    from elspeth.testing import make_pipeline_row

    row = TemplateRow.project(make_pipeline_row({"text": "x"}), ALL_FIELDS)
    text = _failure_text(SandboxedTemplate("{{ row.text * 5000000 }}"), row=row)
    assert "Rendered template exceeds" in text
