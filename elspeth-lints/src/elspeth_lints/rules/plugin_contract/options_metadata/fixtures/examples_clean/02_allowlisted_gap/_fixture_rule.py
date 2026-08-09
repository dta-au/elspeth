from __future__ import annotations

from pydantic import Field

from elspeth.contracts.enums import Determinism
from elspeth.plugins.infrastructure.base import BaseSource
from elspeth.plugins.infrastructure.config_base import PluginConfig
from elspeth_lints.rules.plugin_contract.options_metadata.rule import OptionsMetadataRule


class _Options(PluginConfig):
    missing_title: str = Field(title="", description="Has description but no title")


class _Source(BaseSource):
    name = "metadata_gap"
    determinism = Determinism.IO_READ
    config_model = _Options


class _Manager:
    def get_sources(self) -> list[type]:
        return [_Source]

    def get_transforms(self) -> list[type]:
        return []

    def get_sinks(self) -> list[type]:
        return []


RULE = OptionsMetadataRule(plugin_manager_factory=_Manager)
