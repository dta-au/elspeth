"""Canonical public disclosure for categorically prohibited plugins."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, TypedDict

from elspeth.web.catalog.schemas import PluginSummary
from elspeth.web.plugin_policy.models import PluginUnavailableReason

WEB_PROHIBITED_PLUGIN_EXPLANATION: Final[str] = (
    "the plugin is installed and authorized for this deployment's runtime but prohibited on the web authoring surface by security policy"
)


class ProhibitedPluginDisclosure(TypedDict):
    name: str
    reason: str
    explanation: str


def prohibited_plugin_section(items: Sequence[PluginSummary]) -> tuple[ProhibitedPluginDisclosure, ...]:
    """Project policy-admitted prohibited summaries to one canonical wire shape."""
    reason = PluginUnavailableReason.WEB_SURFACE_PROHIBITED
    return tuple(
        {
            "name": item.name,
            "reason": reason.value,
            "explanation": WEB_PROHIBITED_PLUGIN_EXPLANATION,
        }
        for item in items
    )


__all__ = ["WEB_PROHIBITED_PLUGIN_EXPLANATION", "ProhibitedPluginDisclosure", "prohibited_plugin_section"]
