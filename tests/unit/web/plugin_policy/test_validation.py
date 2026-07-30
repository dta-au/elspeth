def test_profile_unavailable_finding_enumerates_available_aliases() -> None:
    """When operator profiles EXIST and the options simply failed to select
    one, the finding must say so and name the aliases — telling the model
    'an operator must enable a profile' when 'sonnet' is sitting right there
    caused a false honest decline and blind repairs (live sessions 6b9da203 +
    e47fd8df)."""
    from elspeth.web.plugin_policy.validation import _profile_unavailable_finding

    class _Component:
        component_id = "node:llm_1"
        component_type = "transform"

    from elspeth.web.plugin_policy.models import PluginId

    finding = _profile_unavailable_finding(_Component(), PluginId("transform", "llm"), available_aliases=("sonnet",))
    assert "sonnet" in finding.message
    assert "operator must enable" not in finding.message

    unconfigured = _profile_unavailable_finding(_Component(), PluginId("transform", "llm"))
    assert "sonnet" not in unconfigured.message


# ── required_control_coverage messages ───────────────────────────────────────


def _coverage_message(reason: str, *, uncovered_stream: str | None, input_role: bool = False) -> str:
    from elspeth.contracts.plugin_capabilities import ControlRole, PluginCapability
    from elspeth.web.plugin_policy.coverage import ControlCoverageFinding
    from elspeth.web.plugin_policy.validation import _control_coverage_finding

    finding = _control_coverage_finding(
        ControlCoverageFinding(
            component_id="judge",
            capability=PluginCapability.PROMPT_SHIELD if input_role else PluginCapability.CONTENT_SAFETY,
            role=ControlRole.INPUT if input_role else ControlRole.OUTPUT,
            reason=reason,  # type: ignore[arg-type]
            uncovered_stream=uncovered_stream,
        )
    )
    assert finding.stage == "required_control_coverage"
    assert finding.error_code == "required_control_coverage"
    assert finding.component_id == "judge"
    return finding.message


def test_error_route_coverage_message_names_the_conflict_and_the_one_repair() -> None:
    """The on_error rejection reads as a contradiction unless it explains itself.

    The planner is taught to quarantine failed rows to a sink; a required
    output control then rejects the pipeline for doing exactly that. The
    message must name the node, the offending error route, the control, why
    that route is an independent write path, and the single authorable
    repair — on_error='discard'.
    """
    message = _coverage_message("output_error_route_not_post_dominated", uncovered_stream="quarantine")

    assert "judge" in message
    assert "quarantine" in message
    assert "on_error" in message
    assert "content_safety" in message
    assert "independent output path" in message
    assert "'discard'" in message


def test_error_route_coverage_message_never_offers_the_unauthorable_repair() -> None:
    """Interposing the control on the error branch is rejected by the graph.

    ``on_error`` may only name a sink or 'discard'
    (``core/dag/builder.py:1108``), so advising a control transform on the
    error branch would swap this rejection for
    ``transform_on_error_unknown_sink``. The message must say the interposition
    is impossible, never offer it as an option.
    """
    message = _coverage_message("output_error_route_not_post_dominated", uncovered_stream="quarantine")

    assert "no control can be interposed on an error branch" in message
    assert "may only name a sink or 'discard'" in message


def test_non_error_route_coverage_keeps_the_general_message() -> None:
    message = _coverage_message("output_not_post_dominated", uncovered_stream=None)

    assert message == "Node 'judge' is not covered by the required 'content_safety' output control."


def test_input_role_coverage_keeps_the_general_message() -> None:
    message = _coverage_message("input_not_dominated", uncovered_stream=None, input_role=True)

    assert message == "Node 'judge' is not covered by the required 'prompt_shield' input control."
    assert "on_error" not in message


def test_error_route_coverage_message_routes_retention_to_the_operator() -> None:
    """Retaining failed rows is real, but it is not an authoring change.

    ``on_error: <quarantine sink>`` builds and runs — only the *required*
    control mode rejects it. Without naming the operator-side escape hatch the
    author reads the requirement as a bug and re-attempts quarantine shapes.
    """
    message = _coverage_message("output_error_route_not_post_dominated", uncovered_stream="quarantine")

    assert "operator decision" in message
    assert "'recommend'" in message
    assert "content hash" in message
