"""set_pipeline surfaces the source-boundary field-reachability rejection
as an actionable validation envelope, not a 500 (elspeth-3664e213c4).

The rejection is raised by the source config model inside ``from_dict``;
``_prevalidate_plugin_options`` is the composer's existing plugin-config-error
path, so the message must arrive under the standard ``Invalid options for
source '<name>'`` prefix with the remedy intact.
"""

from elspeth.web.composer.tools import _prevalidate_plugin_options


def test_unreachable_declared_field_surfaces_as_invalid_options_envelope() -> None:
    error = _prevalidate_plugin_options(
        "source",
        "csv",
        {
            "path": "/data/blobs/test-session/tickets.csv",
            "schema": {
                "mode": "flexible",
                "fields": [{"name": "TicketID", "field_type": "str"}],
            },
        },
        injected_fields={"on_validation_failure": "discard"},
    )

    assert error is not None
    assert error.startswith("Invalid options for source 'csv':")
    assert "can never appear" in error
    assert "ticketid" in error, "envelope must carry the reachable normalized form"
    assert "field_mapping" in error, "envelope must carry the preserve-original remedy"


def test_normalized_declared_field_passes_prevalidation() -> None:
    error = _prevalidate_plugin_options(
        "source",
        "csv",
        {
            "path": "/data/blobs/test-session/tickets.csv",
            "schema": {
                "mode": "flexible",
                "fields": [{"name": "ticketid", "field_type": "str"}],
            },
        },
        injected_fields={"on_validation_failure": "discard"},
    )

    assert error is None
