"""The guided option-summary allowlist's tiers derive from the catalog lowering.

``protocol._NODE_OPTION_SUMMARY_ALLOWLIST`` carries a tier per allowlisted
key because the wire-stage and proposal projections — and the audit
verifier's re-derivation — run without a catalog handle. This pin keeps that
table honest: annotate an allowlisted knob with a ``composer_tier`` and the
table must follow (elspeth-ca456d9d8d).
"""

from __future__ import annotations

from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.composer.guided.protocol import (
    _NODE_OPTION_DISPLAY_ONLY_ALLOWLIST,
    _NODE_OPTION_SUMMARY_ALLOWLIST,
)


def test_allowlist_tiers_match_the_lowered_knob_schema() -> None:
    svc = CatalogServiceImpl(get_shared_plugin_manager())
    # Both tables are rendered on the cards with a tier; both must follow
    # the catalog lowering.
    entries = list(_NODE_OPTION_SUMMARY_ALLOWLIST.items()) + list(_NODE_OPTION_DISPLAY_ONLY_ALLOWLIST.items())
    for plugin, tiers in entries:
        # Only transforms are allowlisted today; the allowlist is keyed by plugin
        # name without a kind, so allowlisting a source/sink means extending this.
        fields = {field["name"]: field for field in svc.get_schema("transform", plugin).knob_schema["fields"]}
        for key, tier in tiers.items():
            assert key in fields, f"{plugin}.{key} is allowlisted but not a lowered knob"
            assert fields[key]["tier"] == tier, f"{plugin}.{key}: allowlist says {tier}, catalog lowers {fields[key]['tier']}"
