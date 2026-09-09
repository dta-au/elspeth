from __future__ import annotations

from pydantic import Field

from elspeth.contracts.enums import Determinism
from elspeth.plugins.infrastructure.base import BaseSink
from elspeth.plugins.infrastructure.config_base import PluginConfig
from elspeth_lints.rules.plugin_contract.options_metadata.rule import OptionsMetadataRule


class _Options(PluginConfig):
    missing_description: str = Field(title="Missing description", description="")


class _Sink(BaseSink):
    name = "metadata_gap"
    determinism = Determinism.IO_WRITE
    config_model = _Options


class _Manager:
    def get_sources(self) -> list[type]:
        return []

    def get_transforms(self) -> list[type]:
        return []

    def get_sinks(self) -> list[type]:
        return [_Sink]


RULE = OptionsMetadataRule(plugin_manager_factory=_Manager)
