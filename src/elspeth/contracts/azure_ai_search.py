"""Option names of the azure_ai_search transform that only an operator may bind."""

# A web-authored node never carries these: an operator profile supplies them at
# lowering. Shared by the plugin and the web policy layer so the two cannot drift.
AZURE_AI_SEARCH_PRIVATE_BINDING_OPTION_NAMES: frozenset[str] = frozenset(
    {
        "endpoint",
        "api_key",
        "use_managed_identity",
        "client_id",
        "api_version",
    }
)
