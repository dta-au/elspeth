"""_CompiledIdentityDocument: frozen on construction, thawed only at its egress.

The document is the authored YAML mapping that the preflight config swap hands
to profiled plugins so node identity is minted from AUTHORED options. It is
deep-frozen in ``__post_init__`` (immutability.freeze_guards FG3); the swap's
extractors parse nominally (``type(x) is dict`` / ``is list``), so the egress
``audit_safe_settings()`` thaws into FRESH plain containers on every call. The
tests below pin both halves and that the two never alias each other.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import MappingProxyType
from typing import Any

import pytest

from elspeth.web.execution.preflight import _authored_named_components, _authored_options, _authored_sources
from elspeth.web.execution.validation import _CompiledIdentityDocument


def _authored_document() -> dict[str, Any]:
    return {
        "source": {"plugin": "csv", "options": {"path": "in.csv", "columns": ["a", "b"]}},
        "transforms": [{"name": "enrich", "plugin": "reference_join", "options": {"output": {"d": "ref['d']"}}}],
        "sinks": {"out": {"plugin": "json", "options": {"path": "out.json"}}},
    }


class TestFrozenDocument:
    def test_config_is_deep_frozen_and_detached_from_its_source(self) -> None:
        source = _authored_document()
        document = _CompiledIdentityDocument(config=source)

        assert type(document.config) is MappingProxyType
        assert type(document.config["transforms"]) is tuple
        assert type(document.config["transforms"][0]) is MappingProxyType
        assert type(document.config["source"]["options"]["columns"]) is tuple

        # Mutating the caller's dict after construction cannot reach the document.
        source["transforms"][0]["options"]["output"]["d"] = "ref['other']"
        source["sinks"]["out"]["options"]["path"] = "elsewhere.json"
        assert document.config["transforms"][0]["options"]["output"]["d"] == "ref['d']"
        assert document.config["sinks"]["out"]["options"]["path"] == "out.json"

        instance: Any = document
        with pytest.raises(FrozenInstanceError):
            instance.config = {}


class TestAuditSafeSettingsEgress:
    def test_returns_a_fresh_plain_copy_that_cannot_reach_the_document(self) -> None:
        expected = _authored_document()
        document = _CompiledIdentityDocument(config=_authored_document())

        thawed = document.audit_safe_settings()
        assert thawed == expected
        assert type(thawed) is dict
        assert type(thawed["transforms"]) is list
        assert type(thawed["transforms"][0]) is dict
        assert type(thawed["source"]["options"]["columns"]) is list
        assert thawed is not document.config

        # Deep mutation of the egress leaves the document and the next egress intact.
        thawed["transforms"][0]["options"]["output"]["d"] = "ref['mutated']"
        thawed["source"]["options"]["columns"].append("z")
        assert document.config["transforms"][0]["options"]["output"]["d"] == "ref['d']"
        assert document.config["source"]["options"]["columns"] == ("a", "b")
        again = document.audit_safe_settings()
        assert again == expected
        assert again is not thawed

    def test_egress_satisfies_the_swap_extractors_where_the_frozen_document_does_not(self) -> None:
        """Why the egress exists: the swap parses the document by exact type.

        If preflight's extractors ever accept Mapping/Sequence, the second half
        of this test flips and the thaw can be retired.
        """
        document = _CompiledIdentityDocument(config=_authored_document())

        settings = document.audit_safe_settings()
        assert _authored_sources(settings)["source"]["plugin"] == "csv"
        transforms = _authored_named_components(settings, "transforms")
        assert _authored_options(transforms["enrich"]) == {"output": {"d": "ref['d']"}}

        with pytest.raises(TypeError):
            _authored_sources(document.config)
        with pytest.raises(TypeError):
            _authored_named_components(document.config, "transforms")
