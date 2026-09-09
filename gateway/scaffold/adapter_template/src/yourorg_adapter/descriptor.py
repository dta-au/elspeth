"""TEMPLATE: your adapter's static identity and declared capabilities.

Work through this module first. Copy this whole ``adapter_template``
directory to a new project and rename ``yourorg_adapter`` throughout (the
directory, every import in this scaffold, and the
``project.entry-points."elspeth_llm_gateway.adapters"`` table in
``pyproject.toml``) before changing anything below.
"""

from elspeth_llm_gateway.sdk.protocol import AdapterDescriptor
from elspeth_llm_gateway.sdk.types import Capability

# TEMPLATE: must match ^[a-z][a-z0-9_]{2,63}$ and must equal the key in
# pyproject.toml's [project.entry-points."elspeth_llm_gateway.adapters"]
# table and the value ELSPETH_LLM_GATEWAY_ADAPTER is set to at deploy time.
ADAPTER_NAME = "yourorg_adapter"

# TEMPLATE: this adapter package's own semantic version -- independent of
# the gateway runtime version pinned in pyproject.toml's dependencies.
ADAPTER_VERSION = "0.1.0"

# TEMPLATE: the adapter-SDK major this adapter targets. Must equal
# elspeth_llm_gateway.ADAPTER_API_MAJOR in the gateway release you build
# against; the gateway's /readyz reports "adapter_api_incompatible" and
# fails ready if it does not.
ADAPTER_API_MAJOR = 1

# TEMPLATE: declare only the capabilities your upstream genuinely supports.
# Capability.TEXT is mandatory -- AdapterDescriptor rejects a capability set
# without it. Claiming a capability your adapter cannot actually deliver is
# a conformance failure, not a harmless overstatement: the conformance kit
# runs every mandatory test for every capability you declare, and "declare
# fewer, pass all of them" is the correct default for a first adapter.
DECLARED_CAPABILITIES = frozenset({Capability.TEXT})


def build_descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        name=ADAPTER_NAME,
        version=ADAPTER_VERSION,
        adapter_api_major=ADAPTER_API_MAJOR,
        capabilities=DECLARED_CAPABILITIES,
    )
