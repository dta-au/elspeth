"""Finite current batch DATA imports, nominal members and transfer receipts.

The existing fence still proves the original mutable carriers and common
origin/effect closure. This supplement grants only the exact source AST sites
returned below. Complete reviewed bodies protect other ordering/codec effects;
they are not replaced by a general permission for DATA or application code.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import textwrap

from elspeth_lints.core.ast_dump import stable_ast_dump
from tests.helpers import web_reserve_composer_receiver as receiver

DATA = "src/elspeth/web/composer/continuation_batch_data.py"
SCHEMA = "src/elspeth/web/composer/_schema_response_grammar.py"
GENERATION = "src/elspeth/web/composer/tools/_generation_schema_response.py"
DISCOVERY = "src/elspeth/web/composer/discovery_response.py"
RECEIPTS = "src/elspeth/web/composer/continuation_tool_receipts.py"
APPLICATION = "src/elspeth/web/composer/continuation_application_graph.py"
FAMILIES = "src/elspeth/web/composer/continuation_message_families.py"
REQUIRED_PATHS = (DATA, SCHEMA, GENERATION, DISCOVERY, RECEIPTS, APPLICATION, FAMILIES)

# Complete executable namespaces: imports, constants, registries, class fields,
# bases, decorators, defaults and annotations. Function bodies have independent
# finite receipts below; this does not classify their effects as pure.
NAMESPACE_BINDINGS = {
    SCHEMA: "2ef81bea48031e7ca66fb316f396d64387f4ec14d60d27c8f502ec193571ba4c",
    GENERATION: "751ab26e95174f401516201af1d272fbfcb43f3be392396a4d245f8293c80c2d",
    DISCOVERY: "7cfe3766949f87ed815e0e2664920858ed642439a47aca74fbd812de9d3db33a",
    RECEIPTS: "cf0bedc32c03f9afe9affd14b327206ebadeb16b745bd64cb7c8820fde5d6e8a",
    APPLICATION: "2f0209d195a69dba129d4e8059f8884c7cc6ccc1da5bd9da306c4c6c9761c669",
    FAMILIES: "6c7a933a70a7b2e35213a3434a746415ea4424b2260d9afd097afcf718cda51a",
}

TOP_LEVEL_IMPORT_SOURCE = "from __future__ import annotations\nfrom dataclasses import dataclass\nfrom types import MappingProxyType\nfrom typing import TYPE_CHECKING, Any, NoReturn\nfrom pydantic import TypeAdapter\nfrom pydantic import ValidationError as ModelValidationError\nfrom elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus, PrivateFailureFacts, PrivateToolAuthoring, ToolArgumentErrorCategory\nfrom elspeth.contracts.composer_llm_audit import ToolContractDialect\nfrom elspeth.contracts.composer_mutation import ComposerMutationCeiling, ComposerMutationRevocation\nfrom elspeth.contracts.enums import AuditCharacteristic\nfrom elspeth.contracts.errors import AuditIntegrityError\nfrom elspeth.contracts.freeze import FrozenJsonArray\nfrom elspeth.contracts.hashing import canonical_json\nfrom elspeth.contracts.plugin_capabilities import CapabilityDeclaration, ControlRole, PluginCapability, WebConfigAuthority\nfrom elspeth.contracts.provider_rejection import SafeRequestRejection\nfrom elspeth.web.catalog.schemas import ConfigFieldSummary, PluginSchemaInfo, PluginSecretRequirement, PluginSummary\nfrom elspeth.web.composer._compose_loop_carriers import _AdmittedToolCall, _AdmittedToolFunction, _ToolOutcome\nfrom elspeth.web.composer._schema_response_grammar import JSONSchemaSnapshot, KnobSchemaSnapshot\nfrom elspeth.web.composer.audit import DispatchAudit\nfrom elspeth.web.composer.authority_hashing import composer_authority_canonical_json\nfrom elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES, bounded_json_loads\nfrom elspeth.web.composer.continuation import PrivateChunkRef\nfrom elspeth.web.composer.continuation_codec import PrivateChunk, encode_private_chunk, owned_json, verify_private_chunk\nfrom elspeth.web.composer.continuation_dispatch_audit import PortableDispatchAudit, capture_dispatch_audit, decode_dispatch_audit, encode_dispatch_audit, re_admit_dispatch_audit\nfrom elspeth.web.composer.continuation_result_credentials import require_retained_argument_credentials, require_retained_result_credentials\nfrom elspeth.web.composer.continuation_tool_batch import RetainedAdvisorRefusalSource, RetainedBatchFailure, _decode_validation, _read_json_value, _response_contract, _state_from_tool_json, _validation_json, encode_tool_result_value\nfrom elspeth.web.composer.continuation_tool_receipts import decode_retained_tool_receipt, require_receipt_result\nfrom elspeth.web.composer.discovery_response import AdmittedDiscoveryResult, admit_discovery_result\nfrom elspeth.web.composer.manual_advisor_allowance import ManualAdvisorAllowance\nfrom elspeth.web.composer.pipeline_custody import inline_custody_audit_projection\nfrom elspeth.web.composer.state import CompositionState, ValidationSummary\nfrom elspeth.web.composer.tool_batch import BatchAccumulator, ContinuationBatchState, ContinuationCallState, _CallWireFacts\nfrom elspeth.web.composer.tools._common import ToolResult\nfrom elspeth.web.composer.tools._generation_schema_response import PLUGIN_SCHEMA_RESPONSE_CONTRACT, AdmittedPluginSchemaResponse, PluginSchemaSnapshot, SchemaSecretRequirement\nfrom elspeth.web.composer.tools._registry import response_contract_for\nfrom elspeth.web.composer.tools.wire_projection import _WIRE_TOOL_DEFS, decode_wire_arguments\nfrom elspeth.web.credential_guard import require_no_credential_material\nfrom elspeth.web.execution.schemas import SemanticEdgeContractResponse, ValidationCheck, ValidationError, ValidationReadiness, ValidationReadinessBlocker, ValidationResult, ValidationWarning"

TYPE_ONLY_IMPORT_SOURCE = """if TYPE_CHECKING:
    from elspeth.web.composer.continuation_application_graph import ApplicationGraph, ApplicationRecorderSource
"""
# Owner, exact import statement, exact containing phase, protected origins.
# The original two local suppliers remain independently checked by the fence.
LOCAL_SUPPLIER_SPECS = (
    (
        "_argument_text",
        "from elspeth.contracts.json_parser import parse_json_strict",
        None,
        ("elspeth.contracts.json_parser.parse_json_strict",),
    ),
    (
        "_check_prepared",
        "from elspeth.web.composer.continuation_dispatch_audit import _binding",
        None,
        ("elspeth.web.composer.continuation_dispatch_audit._binding",),
    ),
    (
        "_encode",
        "from elspeth.web.composer.continuation_application_graph import application_result_tools",
        "application is not None",
        ("elspeth.web.composer.continuation_application_graph.application_result_tools",),
    ),
    (
        "_encode_values",
        "from elspeth.web.composer.continuation_application_graph import application_recorder_slots, require_application_recorder_source",
        "application is not None",
        (
            "elspeth.web.composer.continuation_application_graph.application_recorder_slots",
            "elspeth.web.composer.continuation_application_graph.require_application_recorder_source",
        ),
    ),
    (
        "_encode_values.visit",
        "from elspeth.web.composer.continuation_application_graph import application_members",
        "application is not None",
        ("elspeth.web.composer.continuation_application_graph.application_members",),
    ),
    (
        "_encode_values.visit",
        "from elspeth.web.composer.continuation_application_graph import application_members",
        "type(value) is ComposerToolInvocation",
        ("elspeth.web.composer.continuation_application_graph.application_members",),
    ),
    (
        "_decode_graph_values",
        "from elspeth.web.composer.continuation_application_graph import require_application_recorder_source",
        "application",
        ("elspeth.web.composer.continuation_application_graph.require_application_recorder_source",),
    ),
    (
        "_decode_graph_values.visit",
        "from elspeth.web.composer.continuation_application_graph import construct_application_llm",
        "kind == 'application_llm'",
        ("elspeth.web.composer.continuation_application_graph.construct_application_llm",),
    ),
    (
        "_decode_graph_values.visit",
        "from elspeth.web.composer.continuation_application_graph import construct_application_member",
        "(application and kind.startswith('application_')) or kind == 'application_invocation'",
        ("elspeth.web.composer.continuation_application_graph.construct_application_member",),
    ),
    (
        "_decode_graph_values",
        "from elspeth.web.composer.continuation_application_graph import ApplicationGraph, application_recorder_slots, application_result_tools",
        "application",
        (
            "elspeth.web.composer.continuation_application_graph.ApplicationGraph",
            "elspeth.web.composer.continuation_application_graph.application_recorder_slots",
            "elspeth.web.composer.continuation_application_graph.application_result_tools",
        ),
    ),
)

# Raw source-segment bindings for the parent-owned existing receipt only.
# New local contracts below use the portable canonical AST serializer.
CHANGED_BODY_SOURCE_SHA256 = {
    "_joint": "92bad4904e369227e1fabc39e737ad61ffe02435d12b13d50255bd47b861af8c",
    "_members": "62b565b238d533c6135eb777eb883e35ae5da638eb632e9c10f3207ec7de65fe",
    "_construct": "9531f31f1f7414c2143974c855b00f2e6292b2212f20c47c6e17e58fe8b46888",
    "_encode": "5ab65db4cecdbf7d4c92c9e37383f654b6ba5dd1addeb3c883f48a41e3c13f4b",
    "_decode_graph": "2c6d5266a828ed6233231a25d5205562945e5e63a2f009fb72ec55220c8a4031",
    "_check_prepared": "60da77a6668297af64a8020020f6b04abed39bc906667c233a3a0b679bfceb16",
}
NEW_BODY_SOURCE_SHA256 = {
    "RestoredMemoryCompletionBatchData": "01dc03518187f6a5712e33c12df44e0eaa31383d3d220829d2357e83ed7e41e7",
    "_check_memory_completion": "cd49546d331d4142f49089d9d4205b50c1e217155333066bb83ccc064eff58f4",
    "encode_memory_completion_batch_data": "aa5c855e28a09cff112c713f9c605334c657e129cbb80a53df49f6315ae5adfe",
    "encode_restored_memory_completion_batch_data": "769c23680df32481b754f99ea96a93ee455f731b53aa60899eba09544db10c35",
    "decode_memory_completion_batch_data": "f51b9ae5d435a72da5e8cd5b0ca2ca23f47db97520a9d0a7290f05c6b1e08560",
    "require_selected_memory_completion": "76004736f9ddf9608e0867330ff332ec11dfc8c9ec1eab6cfdea98dd2c55029c",
}
ADDITIONAL_BATCH_ROLES = frozenset(
    {
        ("RestoredMemoryCompletionBatchData", "annotation", "ContinuationBatchState"),
        ("encode_memory_completion_batch_data", "annotation", "ContinuationBatchState"),
        ("_check_memory_completion", "compare", "type(call) is not ContinuationCallState"),
        ("encode_memory_completion_batch_data", "compare", "type(batch) is not ContinuationBatchState"),
        ("encode_memory_completion_batch_data", "compare", "type(batch.current_call) is not ContinuationCallState"),
        ("encode_restored_memory_completion_batch_data", "compare", "type(value.batch.current_call) is not ContinuationCallState"),
        ("require_selected_memory_completion", "compare", "type(call) is not ContinuationCallState"),
    }
)
SCHEMA_FIELDS = {
    "AdmittedDiscoveryResult": ("result", "response", "tool_name"),
    "AdmittedPluginSchemaResponse": ("snapshot",),
    "PluginSchemaSnapshot": (
        "name",
        "plugin_type",
        "description",
        "json_schema",
        "knob_schema",
        "composer_hints",
        "secret_requirements",
        "web_config_authority",
        "policy_capabilities",
        "aggregation_output_modes",
    ),
    "SchemaSecretRequirement": ("field", "candidates"),
    "JSONSchemaSnapshot": ("keywords",),
    "KnobSchemaSnapshot": ("fields",),
}
BODY_BINDINGS = {
    (DATA, "_joint"): "94b29607e677c56c8b7d7336ddebfafa5e4e906589c2848d9843bd7c558aaa8e",
    (DATA, "_members"): "20ee877a13ff20360c371547c3293e307fe9e4c13d64f6d72ceb2fbde5622851",
    (DATA, "_construct"): "29e25e9a6fb0b8a3f89cdf92659df05eed43b7d213037a2a217940d62b26ba3d",
    (DATA, "_encode"): "b9232e264c1c5b65c260db030488895a732d0d2f274e10e80854b9af4218833b",
    (DATA, "_decode_graph"): "b42e0e16e1250c4a393f8a1a9c48acc2fb6e049917c35a51de1bb0aab6bcd489",
    (DATA, "_encode_values"): "c0136bcc24072d9083576e89cb55700116fcef5c33f90d824a2334088827cb0f",
    (DATA, "_decode_graph_values"): "ed2a64e77c4bae7f1ab81a9616672045e6bbd25e2b79615480ff6a33ff873254",
    (DATA, "_check_prepared"): "b6e6d4170dbb040db8b6e15f6f33fe47e45e41eec1d22538f0c2ac7356e2d423",
    (DATA, "RestoredMemoryCompletionBatchData"): "e0a778360a43a417f9d1bf8733c772f323e6167d99d18e818b486268d9cb9a13",
    (DATA, "_check_memory_completion"): "3e05ed91305f48c6ad280a38cb95b93b6b3e50b36103b225f6ec5f3cb3c16f48",
    (DATA, "encode_memory_completion_batch_data"): "a6680ff41480a239b5f837ed4b2af036328d48641f045ea3c51e201704fa4661",
    (DATA, "encode_restored_memory_completion_batch_data"): "1b5ee6c8dc2227b44ce92d2e235235b3ab702d17b956e329fcfb1cffefa003fe",
    (DATA, "decode_memory_completion_batch_data"): "9f14e8de45095fb3abb5fcef3e7203c471a4d4c7a4edb2ad4e00ce7e9aab0036",
    (DATA, "require_selected_memory_completion"): "bf164aa5d6d4f03e95e13b9b93ad9db73cee56ef09f65439a368a18d578b9cd1",
    (SCHEMA, "JSONSchemaSnapshot"): "6159c2c49d2090c8f3da3218649532d301cd99e39eb2f39b1888a48392167dcc",
    (SCHEMA, "KnobSchemaSnapshot"): "32244fbfcf8fd7167fc53c28e7c1421f6c0e1e10bbaddb5203a1214575380e44",
    (SCHEMA, "readmit_json_schema"): "4926eec56477bc90ed1a7548788ae8a691685561ee11130cbaf12225f5be73a8",
    (SCHEMA, "readmit_knob_schema"): "43f82ec0c1bfb39327225be62169c28e6f29f4c1aabe6359ca0a49b5ad3050aa",
    (SCHEMA, "parse_json_schema"): "ffbc5973ae8585afde2dbfd5bb37d040a6196dc630f31c81c03d67befb8f548b",
    (SCHEMA, "parse_knob_schema"): "c6e3e69723366de3f0c391bbe7950753ed24389dfd43281f32a2e5a14370a2ec",
    (SCHEMA, "validate_json_schema_scalar"): "e20a2434cc5074bff657c696850187eab6cf22f8d848c4788401c7d507eb14c1",
    (SCHEMA, "_mapping"): "5d5389580b10ab54dd6cb0df28d0dcd8da7fe5305543d4c4d3481518be6d1675",
    (SCHEMA, "_strings"): "194d404d93c88d4264d3d07c2234a311a8d2c375d2fecdb009a7eaeae21b881f",
    (SCHEMA, "_predicate"): "63a98afc66f630cb4d6a4b3d0bd3d1edfcdf666193e6d28b2bb6cfaed0cc5c38",
    (SCHEMA, "_schema_node"): "368eae399f326f7fa2db356c5f9f2ecb6c8c8a4d4d7af3fc59b1f7cf9d238664",
    (SCHEMA, "_knob_node"): "0e206e214f5827a514f9b6001444b6874e0b9a1ced733e7da3bbb75b73a2a7d9",
    (GENERATION, "SchemaSecretRequirement"): "c5d43bad2a799aeced1fcd501b0871b7036fee9cc9c448b0f38819c997d819be",
    (GENERATION, "PluginSchemaSnapshot"): "3eb3ebf6659d08a6300809472152fb42a029d3d92addf78c3a8bcbd6a333bce5",
    (GENERATION, "_bad"): "07bce56468db2d480d7d40d486c312f95e185bb682b86b2243a9923c54eb0208",
    (GENERATION, "_text"): "dc68d450b5994c2d9d4336c3c04b713d4a292a7f885ed5b4984be801bec356dd",
    (GENERATION, "_tuple"): "31e216b87a6bd97d71d23962f60abe61abb96456d162ff5cd586e4a794ab64bd",
    (GENERATION, "_texts"): "31da92dfcdeece9adcebf484f54bcacf2653ac33a7ba5235378237ff19cfa711",
    (GENERATION, "_secret"): "da5e110453054602170b3f698a317381f2c8d3cf22437fbca22a55dfd6aa5f3a",
    (GENERATION, "_capability"): "c45a2b6e06ee3f4552bbb2ac43acccae3e877a3c45fdf60f3b38b3e459f40cfc",
    (GENERATION, "_snapshot"): "cf270c4fc60db8edfeee14232fa7496b3a434ba2acc995fd4b729be7a5713281",
    (GENERATION, "_encode"): "0d6b4ad0bebdd3d9be5fb695c46e3d6568644df9da3b3c7838d38167867bc804",
    (GENERATION, "AdmittedPluginSchemaResponse"): "97a773f2bd789ef15d28258ceba8527cc3c0c5daf13be65b6025ae44a4c9efc0",
    (GENERATION, "PluginSchemaResponseContract"): "924c812d4c65d73499815bec088e19c5c344edf84626eb810881f2dfa5b6260b",
    (DISCOVERY, "AdmittedDiscoveryResult"): "2d220a11edf04a44db8cf8b2046433937af16d18e0625f999f619960e39426f8",
    (DISCOVERY, "require_discovery_result_data"): "e8234ed00cdd475c8dce4f1f6ba0c5364b485fb599f0ce8d25a5c434eeff4666",
    (DISCOVERY, "admit_discovery_result"): "2e1c22c63ec9728b069733088c26674a3d4626b56108fc3cf29ca3bf463e2d1b",
    (RECEIPTS, "RetainedToolReceipt"): "5790f9126fd27abb8c67ec042b96345d8416cc6d14d4149afba9bfedd3d55cf8",
    (RECEIPTS, "_invocation"): "bd4043561f86fec2bda3a86d17def3d21b4368002aac8c9c96523313fe9264f7",
    (RECEIPTS, "decode_retained_tool_receipt"): "c1e06dc3937f3ba599f5db498db3edbb5c60633b0b7598e6a64c1200585f5bda",
    (RECEIPTS, "encode_retained_tool_receipt"): "3b471e271c9eb53d5df0b393672d6e84e0fffb985a2b43df775dbbd6da9eb3fd",
    (RECEIPTS, "capture_tool_receipt"): "0be22ec2a62726ec1bf425b4dfed4a2002eb362be19b2d9c8374723e1f928d2b",
    (RECEIPTS, "require_receipt_result"): "3e207f927d73012bd0d65d6289b1081d61d3738a134d092949ae1353dd5115f0",
    (APPLICATION, "ApplicationGraphCut"): "69f4e3c832572e424c8b4c3934c210c51f61cb2c5923b33b5e028fc7bbebf8e9",
    (APPLICATION, "ApplicationGraph"): "c04a53fba0584d86e7380bdd690819677a5c20f72a470dd509a5b2c952e3f5d4",
    (APPLICATION, "ApplicationRecorderSource"): "18f04ae52a559c0a700e0e174eee11499edeee64005ea9451bfd1382d0881547",
    (APPLICATION, "_require_source_environment"): "4250c1045487039b3734a80304e3ca551c6b449356536ceda11eb7c3473c9e21",
    (APPLICATION, "_require_source_chunk"): "a0fd3cabbade96edf38d9ef740d6c1ddcf6333256cf7d6df8898cf1e997bc61f",
    (APPLICATION, "_require_source_credentials"): "e230d7605324b4992061e82c46da1f91432bff3d0a247df087aa6b410e88c26b",
    (APPLICATION, "capture_application_recorder_source"): "2c30870331665726c24c01616128cd33dfa54685ad74cdc830579bb67ae1fe41",
    (APPLICATION, "admit_application_recorder_source"): "4bc7de23a334690724e751448b65f252630ba5cca6251b672ef6611405843db5",
    (APPLICATION, "require_application_recorder_source"): "2d8a36b0b5110058d8cada81ddac0423b2e962016bf1199fe13d762f60acb888",
    (APPLICATION, "application_recorder_slots"): "59b9e19bfde75a5964b54223a8cb3333c1c37c041510d8ab9025244ef11be92d",
    (APPLICATION, "construct_application_llm"): "4fa9b476730c315e5f10ecefaa46b2a5361757d2a87d0ed5cc42a34d92330f8d",
    (APPLICATION, "_atom_json"): "d8c18d01bfc09accf187f3876e6401dde588c3f3813035709c4ce2267f41f8a5",
    (APPLICATION, "_admit_effects"): "3e2128ea596b8ca5dcbd6bb52eb63c5be4a191c62de7464949b130d2d084bf63",
    (APPLICATION, "application_members"): "31c097108ada7fc208e28ab6e85d448fdc86e9c98581ef068fcdfe30e9b9bb41",
    (APPLICATION, "construct_application_member"): "a7558aa72ccd92def9b8dd9cbec7ac3f627596eb9fb24370b2f233d93b73813e",
    (APPLICATION, "application_result_tools"): "3c4ebe6b6f9abb0d2837cbe97d8685eb6ac4ea5baa18bd29d36639a5c9bbc13e",
    (APPLICATION, "encode_application_graph"): "a7ec2416bbb7bf6ffb4b4838db3d16afa510718a19a380c6a83480c05d882b8e",
    (APPLICATION, "decode_application_graph"): "a2c5c1315453a1dcd60796db8ab6aeac5f25c56c3041b847b5155d20609948b2",
    (FAMILIES, "_read_current_sources"): "f0544018d62a54f58d4466d72092078a54f694633ef9642b1d5753768a538ddf",
    (FAMILIES, "_actual_chunk"): "b1dfd2e12b4f719f73afb8767c094e92b0ee53a41ed0c4fec1463691c6a34286",
}

SITE_KEYS = (
    "selected_definitions",
    "additional_local_imports",
    "type_only_imports",
    "application_imports",
    "constructor_calls",
    "constructor_uses",
    "nominal_uses",
    "batch_uses",
    "application_calls",
    "application_uses",
    "external_data_calls",
    "external_data_uses",
    "member_uses",
)


def _module(path):
    return path.removeprefix("src/").removesuffix(".py").replace("/", ".")


def _digest(node):
    return hashlib.sha256(stable_ast_dump(node).encode("utf-8")).hexdigest()


class _Namespace(ast.NodeTransformer):
    def visit_FunctionDef(self, node):
        result = copy.copy(node)
        result.body = []
        return result

    visit_AsyncFunctionDef = visit_FunctionDef


def _namespaces(index, failures, receipt):
    for path, digest in NAMESPACE_BINDINGS.items():
        namespace = _Namespace().visit(copy.deepcopy(index[path].tree))
        _require(failures, _digest(namespace) == digest, "dependency namespace changed " + path)
        for statement in namespace.body:
            targets = (
                statement.targets
                if isinstance(statement, ast.Assign)
                else [statement.target]
                if isinstance(statement, ast.AnnAssign)
                else []
            )
            names = {target.id for target in targets if isinstance(target, ast.Name)}
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(statement.name)
            for name in names:
                receipt["protected_origins"].add(_module(path) + "." + name)
                receipt["supplier_bindings"].add(_module(path) + "." + name)
        for node in ast.walk(namespace):
            if isinstance(node, ast.ImportFrom) and node.module != "__future__":
                for alias in node.names:
                    receipt["protected_origins"].add(node.module + "." + alias.name)
                    receipt["supplier_bindings"].add(_module(path) + "." + (alias.asname or alias.name))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    receipt["protected_origins"].add(alias.name)
                    receipt["supplier_bindings"].add(_module(path) + "." + (alias.asname or alias.name.split(".")[0]))


def _same(node, source, *, expression=False):
    expected = ast.parse(textwrap.dedent(source), mode="eval" if expression else "exec")
    if not expression and len(expected.body) != 1:
        return False
    return stable_ast_dump(node) == stable_ast_dump(expected.body if expression else expected.body[0])


def _require(failures, condition, detail):
    if not condition:
        failures.append("batch DATA " + detail)


def _sites(receipt, key, path, nodes):
    receipt[key].update((path, id(node)) for node in nodes)


def _matching(owner, kind, source, *, expression=False):
    return [node for node in ast.walk(owner) if isinstance(node, kind) and _same(node, source, expression=expression)]


def _one(failures, receipt, key, path, owner, kind, source, detail, *, expression=False):
    matches = _matching(owner, kind, source, expression=expression)
    _require(failures, len(matches) == 1, detail)
    if len(matches) == 1:
        _sites(receipt, key, path, matches)
        return matches[0]
    return None


def _owner(node, parents):
    parts = []
    current = parents.get(id(node))
    while current is not None:
        if isinstance(current, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            parts.append(current.name)
        current = parents.get(id(current))
    return ".".join(reversed(parts))


def _phase(node, parents):
    current = parents.get(id(node))
    while current is not None:
        if isinstance(current, ast.If):
            return current.test
        current = parents.get(id(current))
    return None


def _empty():
    return {key: set() for key in SITE_KEYS} | {
        "protected_origins": set(),
        "function_origins": set(),
        "class_origins": set(),
        "supplier_bindings": set(),
        "receiver_members": {},
    }


def _bind(units, failures, receipt):
    index = {unit.path: unit for unit in units}
    if len(index) != len(units) or any(path not in index for path in REQUIRED_PATHS):
        failures.append("batch DATA source set missing or duplicated")
        return {}, {}
    selected = {}
    for (path, name), digest in BODY_BINDINGS.items():
        declaration = receiver.find(index[path].tree, name)
        expected_kind = (
            ast.ClassDef
            if name
            in {
                "RestoredMemoryCompletionBatchData",
                "JSONSchemaSnapshot",
                "KnobSchemaSnapshot",
                "SchemaSecretRequirement",
                "PluginSchemaSnapshot",
                "AdmittedPluginSchemaResponse",
                "PluginSchemaResponseContract",
                "AdmittedDiscoveryResult",
                "RetainedToolReceipt",
                "ApplicationGraphCut",
                "ApplicationGraph",
                "ApplicationRecorderSource",
            }
            else ast.FunctionDef
        )
        _require(failures, isinstance(declaration, expected_kind) and _digest(declaration) == digest, "selected body changed " + name)
        if declaration is None:
            continue
        selected[path, name] = declaration
        _sites(receipt, "selected_definitions", path, [declaration])
        _require(failures, receiver.owned_binding(index[path].tree, name, declaration), "selected definition binding changed " + name)
        origin = _module(path) + "." + name
        receipt["protected_origins"].add(origin)
        receipt["supplier_bindings"].add(origin)
        receipt["class_origins" if isinstance(declaration, ast.ClassDef) else "function_origins"].add(origin)
    return index, selected


def _imports(unit, failures, receipt):
    imports = [node for node in unit.tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    expected = ast.parse(TOP_LEVEL_IMPORT_SOURCE).body
    _require(
        failures,
        [stable_ast_dump(node) for node in imports] == [stable_ast_dump(node) for node in expected],
        "complete top-level imports changed",
    )
    _require(failures, receiver.imported(unit.tree, "TYPE_CHECKING", "typing", "TYPE_CHECKING"), "canonical TYPE_CHECKING binding changed")
    receipt["protected_origins"].add("typing.TYPE_CHECKING")
    receipt["supplier_bindings"].add(_module(DATA) + ".TYPE_CHECKING")
    type_only = [node for node in unit.tree.body if isinstance(node, ast.If) and _same(node, TYPE_ONLY_IMPORT_SOURCE)]
    _require(failures, len(type_only) == 1, "type-only application import changed")
    matched = set()
    if len(type_only) == 1:
        node = type_only[0].body[0]
        matched.add(id(node))
        _sites(receipt, "type_only_imports", DATA, [node])
    parents = {id(child): parent for parent in ast.walk(unit.tree) for child in ast.iter_child_nodes(parent)}
    locals_ = [node for node in ast.walk(unit.tree) if isinstance(node, (ast.Import, ast.ImportFrom)) and node not in imports]
    for owner, source, phase, origins in LOCAL_SUPPLIER_SPECS:
        candidates = [node for node in locals_ if _owner(node, parents) == owner and _same(node, source)]
        if phase is not None:
            candidates = [
                node for node in candidates if _phase(node, parents) is not None and _same(_phase(node, parents), phase, expression=True)
            ]
        _require(failures, len(candidates) == 1, "local import owner/phase changed " + owner)
        if len(candidates) == 1:
            matched.add(id(candidates[0]))
            if phase is not None:
                _sites(receipt, "application_imports", DATA, candidates)
        receipt["protected_origins"].update(origins)
        for origin in origins:
            receipt["supplier_bindings"].add(_module(DATA) + "." + origin.rsplit(".", 1)[1])
    _require(failures, {id(node) for node in locals_} == matched, "local import role inventory changed")
    receipt["additional_local_imports"].update(receipt["type_only_imports"] | receipt["application_imports"])


def _schema_members(unit, selected, failures, receipt):
    candidates = [
        node
        for node in unit.tree.body
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "_FIELDS" for target in node.targets)
    ]
    registry = candidates[0] if len(candidates) == 1 else None
    _require(
        failures,
        isinstance(registry, ast.Assign)
        and len(registry.targets) == 1
        and isinstance(registry.targets[0], ast.Name)
        and isinstance(registry.value, ast.Dict),
        "schema registry declaration changed",
    )
    if not isinstance(registry, ast.Assign) or not isinstance(registry.value, ast.Dict):
        return
    stores = [
        node
        for node in receiver.immediate(unit.tree)
        if isinstance(node, ast.Name) and node.id == "_FIELDS" and isinstance(node.ctx, (ast.Store, ast.Del))
    ]
    _require(failures, stores == registry.targets, "schema registry binding ownership changed")
    members, constructor = selected[DATA, "_members"], selected[DATA, "_construct"]
    for name, fields in SCHEMA_FIELDS.items():
        path = (
            DISCOVERY
            if name == "AdmittedDiscoveryResult"
            else SCHEMA
            if name in {"JSONSchemaSnapshot", "KnobSchemaSnapshot"}
            else GENERATION
        )
        declaration = selected[path, name]
        owned_fields = tuple(
            node.target.id for node in declaration.body if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        )
        expected_fields = (
            ("result", "response", "contract")
            if name == "AdmittedDiscoveryResult"
            else ("snapshot", "contract")
            if name == "AdmittedPluginSchemaResponse"
            else fields
        )
        _require(failures, owned_fields == expected_fields, "schema nominal field inventory changed " + name)
        receipt["receiver_members"].setdefault(_module(path) + "." + name, set()).update(expected_fields)
        entries = [
            value
            for key, value in zip(registry.value.keys, registry.value.values, strict=True)
            if isinstance(key, ast.Constant) and key.value == name
        ]
        _require(failures, len(entries) == 1 and _same(entries[0], repr(fields), expression=True), "schema registry fields changed " + name)
        projection = [
            node for node in members.body if isinstance(node, ast.If) and _same(node.test, "type(value) is " + name, expression=True)
        ]
        pairs = ", ".join(repr(field) + ": " + ("selected_tool" if field == "tool_name" else "value." + field) for field in fields)
        expected = "if type(value) is " + name + ":\n"
        if name == "AdmittedDiscoveryResult":
            expected += '    if selected_tool is None:\n        _fail("admitted discovery graph lost its selected tool contract")\n'
        expected += "    return " + repr(name) + ", {" + pairs + "}\n"
        _require(failures, len(projection) == 1 and _same(projection[0], expected), "schema field projection changed " + name)
        if len(projection) == 1:
            _sites(receipt, "nominal_uses", DATA, [projection[0].test.comparators[0]])
            _sites(
                receipt,
                "member_uses",
                DATA,
                [
                    node
                    for node in ast.walk(projection[0])
                    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "value"
                ],
            )
        branches = [
            node for node in constructor.body if isinstance(node, ast.If) and _same(node.test, "kind == " + repr(name), expression=True)
        ]
        if name == "AdmittedDiscoveryResult":
            body = """selected_contract = response_contract_for(members["tool_name"])
if selected_contract is None:
    _fail("retained discovery has no actual selected response contract")
return AdmittedDiscoveryResult(members["result"], members["response"], selected_contract)"""
        elif name == "AdmittedPluginSchemaResponse":
            body = 'return AdmittedPluginSchemaResponse(members["snapshot"], PLUGIN_SCHEMA_RESPONSE_CONTRACT)'
        else:
            body = "return " + name + "(**members)"
        expected = "if kind == " + repr(name) + ":\n" + textwrap.indent(body, "    ")
        _require(failures, len(branches) == 1 and _same(branches[0], expected), "schema constructor changed " + name)
        if len(branches) == 1:
            calls = [
                node
                for node in ast.walk(branches[0])
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
            ]
            _require(failures, len(calls) == 1, "schema constructor site changed " + name)
            _sites(receipt, "constructor_calls", DATA, calls)
            _sites(receipt, "constructor_uses", DATA, [node.func for node in calls])
    gate = constructor.body[0]
    _require(
        failures,
        _same(
            gate,
            'if kind not in _FIELDS or tuple(members) != _FIELDS[kind]:\n    _fail("batch graph lost complete ordered nominal members")',
        ),
        "complete nominal member constructor gate changed",
    )


def _signatures(selected, failures):
    # None means a required formal; the source string "None" is an actual
    # optional default. An extra default/callback never inherits DATA status.
    specifications = {
        "_joint": (("data", "env_ref_names", "prepared"), ("memory_completion",), ("None",), ("False",)),
        "_members": (("value", "selected_tool"), (), ("None",), ()),
        "_construct": (("kind", "members"), (), (), ()),
        "_encode": (
            ("data", "env_ref_names", "prepared"),
            ("memory_completion", "application", "recorder_source"),
            ("None",),
            ("False", "None", "None"),
        ),
        "_encode_values": (
            ("data", "env_ref_names", "result_tools", "prepared"),
            ("memory_completion", "application", "recorder_source"),
            ("None",),
            ("False", "None", "None"),
        ),
        "_decode_graph": (
            ("encoded", "env_ref_names"),
            ("prepared", "memory_completion", "application", "recorder_source"),
            (),
            ("False", "False", "False", "None"),
        ),
        "_decode_graph_values": (
            ("encoded", "env_ref_names"),
            ("prepared", "memory_completion", "application", "recorder_source"),
            (),
            ("False", "False", "False", "None"),
        ),
        "_check_prepared": (("data", "retained", "dialect", "env_ref_names"), ("memory_completion",), (), ("False",)),
        "_check_memory_completion": (("data", "audit", "env_ref_names", "result_tools"), (), (), ()),
        "encode_memory_completion_batch_data": (
            (),
            ("batch", "accumulator", "calls", "dialect", "env_ref_names"),
            (),
            (None, None, None, None, "frozenset()"),
        ),
        "encode_restored_memory_completion_batch_data": (("value",), ("env_ref_names",), (), ("frozenset()",)),
        "decode_memory_completion_batch_data": (("encoded",), ("env_ref_names",), (), ("frozenset()",)),
        "require_selected_memory_completion": (
            ("value",),
            ("selected_call", "selected_pre_state", "current_dialect", "env_ref_names"),
            (),
            (None, None, None, None),
        ),
    }
    for name, (positional, keywords, defaults, kw_defaults) in specifications.items():
        declaration = selected[DATA, name]
        args = declaration.args
        _require(
            failures,
            not declaration.decorator_list
            and not declaration.type_params
            and not args.posonlyargs
            and tuple(arg.arg for arg in args.args) == positional
            and tuple(arg.arg for arg in args.kwonlyargs) == keywords
            and args.vararg is None
            and args.kwarg is None
            and len(args.defaults) == len(defaults)
            and all(_same(value, source, expression=True) for value, source in zip(args.defaults, defaults, strict=True))
            and len(args.kw_defaults) == len(kw_defaults)
            and all(
                value is None if source is None else value is not None and _same(value, source, expression=True)
                for value, source in zip(args.kw_defaults, kw_defaults, strict=True)
            ),
            "exact formal/default transfer changed " + name,
        )


def _memory_and_application(unit, selected, failures, receipt):
    joint, encode, decode = (selected[DATA, name] for name in ("_joint", "_encode", "_decode_graph_values"))
    _one(
        failures,
        receipt,
        "member_uses",
        DATA,
        joint,
        ast.Assign,
        "result_tools[id(response)] = outcome.call.function.name",
        "result contract identity transfer changed",
    )
    _one(
        failures,
        receipt,
        "member_uses",
        DATA,
        encode,
        ast.If,
        'if prepared is not None or memory_completion or application.selected is not data:\n    _fail("application graph mixed another cut or selected root")',
        "application selected cut changed",
    )
    _one(failures, receipt, "member_uses", DATA, decode, ast.Assign, 'members["audit"] = None', "portable audit separation changed")
    prepared = selected[DATA, "_check_prepared"]
    _one(
        failures,
        receipt,
        "member_uses",
        DATA,
        prepared,
        ast.Assign,
        'expected_phase = "application_pending" if memory_completion else "prepared"',
        "prepared memory phase changed",
    )
    selected_memory = selected[DATA, "require_selected_memory_completion"]
    _one(
        failures,
        receipt,
        "member_uses",
        DATA,
        selected_memory,
        ast.If,
        """if (
    type(call) is not ContinuationCallState
    or selected_call is not call.tool_call
    or selected_pre_state is not call.pre_state
    or current_dialect is not value.dialect
):
    _fail("memory application differs from its exact selected DATA roots/dialect")""",
        "selected memory root identity changed",
    )
    outer = selected[DATA, "RestoredMemoryCompletionBatchData"]
    _require(
        failures,
        _same(
            outer,
            '''@dataclass(frozen=True, slots=True)
class RestoredMemoryCompletionBatchData:
    """Completed memory result DATA before application, never a current grant.

    The completed receipt preserves original invocation timing. The portable
    audit remains historical context; application must not finish it again.
    """
    batch: ContinuationBatchState
    accumulator: BatchAccumulator
    calls: tuple[_AdmittedToolCall, ...]
    audit: PortableDispatchAudit
    dialect: ToolContractDialect
''',
        ),
        "memory outer DATA declaration changed",
    )
    wrappers = (
        (
            "encode_memory_completion_batch_data",
            "_encode(RestoredBatchData(batch, accumulator, calls), env_ref_names, (retained, dialect), memory_completion=True)",
        ),
        (
            "encode_restored_memory_completion_batch_data",
            "_encode(RestoredBatchData(value.batch, value.accumulator, value.calls), env_ref_names, (value.audit, value.dialect), memory_completion=True)",
        ),
        ("decode_memory_completion_batch_data", "_decode_graph(encoded, env_ref_names, prepared=True, memory_completion=True)"),
    )
    for owner, expression in wrappers:
        call = _one(
            failures,
            receipt,
            "external_data_calls",
            DATA,
            selected[DATA, owner],
            ast.Call,
            expression,
            "memory codec phase/caller changed " + owner,
            expression=True,
        )
        if call is not None:
            _sites(receipt, "external_data_uses", DATA, [call.func])
    for owner, expression in (
        ("_encode", "application_result_tools(application, env_ref_names)"),
        ("_encode_values", "application_members(value, recorder_slots=recorder_slots)"),
        ("_encode_values", "application_members(value)"),
        ("_encode_values", "require_application_recorder_source(recorder_source, env_ref_names)"),
        ("_encode_values", "application_recorder_slots(application, recorder_source)"),
        ("_decode_graph_values", "require_application_recorder_source(recorder_source, env_ref_names)"),
        ("_decode_graph_values", "construct_application_member(kind, members)"),
        ("_decode_graph_values", "construct_application_llm(recorder_source, slot)"),
        ("_decode_graph_values", "application_result_tools(application_value, env_ref_names)"),
        ("_decode_graph_values", "application_recorder_slots(application_value, recorder_source)"),
    ):
        call = _one(
            failures,
            receipt,
            "application_calls",
            DATA,
            selected[DATA, owner],
            ast.Call,
            expression,
            "application supplier call changed " + owner,
            expression=True,
        )
        if call is not None:
            _sites(receipt, "application_uses", DATA, [call.func])
    for owner, expression in (
        ("_decode_graph_values", "RestoredMemoryCompletionBatchData(batch, accumulator, calls, audit_values[0], dialect)"),
        ("_decode_graph_values", "ApplicationGraph(data, effects, recorder, cuts, audit_values[0], dialect)"),
        ("_decode_graph_values", "ApplicationGraph(data, effects, recorder, cuts)"),
        ("encode_memory_completion_batch_data", "RestoredBatchData(batch, accumulator, calls)"),
        ("encode_restored_memory_completion_batch_data", "RestoredBatchData(value.batch, value.accumulator, value.calls)"),
        ("require_selected_memory_completion", "RestoredBatchData(value.batch, value.accumulator, value.calls)"),
    ):
        call = _one(
            failures,
            receipt,
            "constructor_calls",
            DATA,
            selected[DATA, owner],
            ast.Call,
            expression,
            "exact outer constructor transfer changed " + owner,
            expression=True,
        )
        if call is not None:
            _sites(receipt, "constructor_uses", DATA, [call.func])
    for owner, expression in (
        ("_check_memory_completion", "type(result) is AdmittedDiscoveryResult"),
        ("_check_memory_completion", "type(result.response) is not AdmittedPluginSchemaResponse"),
        ("_check_memory_completion", "type(result.response.snapshot) is not PluginSchemaSnapshot"),
        ("encode_restored_memory_completion_batch_data", "type(value) is not RestoredMemoryCompletionBatchData"),
        ("decode_memory_completion_batch_data", "type(restored) is not RestoredMemoryCompletionBatchData"),
    ):
        checks = _matching(selected[DATA, owner], ast.Compare, expression, expression=True)
        _require(failures, len(checks) == 1, "memory nominal owner observation changed " + owner)
        _sites(receipt, "nominal_uses", DATA, [node.comparators[0] for node in checks])
    for name, role, expression in ADDITIONAL_BATCH_ROLES:
        owner = selected[DATA, name]
        if role == "annotation":
            if isinstance(owner, ast.ClassDef):
                uses = [
                    node.annotation
                    for node in owner.body
                    if isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and node.target.id == "batch"
                    and node.value is None
                ]
            else:
                uses = [arg.annotation for arg in owner.args.kwonlyargs if arg.arg == "batch"]
            uses = [node for node in uses if isinstance(node, ast.Name) and node.id == expression]
        else:
            checks = _matching(owner, ast.Compare, expression, expression=True)
            uses = [node.comparators[0] for node in checks]
        _require(failures, len(uses) == 1, "new native batch reference role changed " + name)
        _sites(receipt, "batch_uses", DATA, uses)


def _constructor_entry_references(units):
    """Resolve the selected entry binding, not unrelated `_construct` names."""
    found = set()
    for unit in units:
        direct = {"_construct"} if unit.path == DATA else set()
        modules = set()
        for node in ast.walk(unit.tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == _module(DATA):
                direct.update(alias.asname or alias.name for alias in node.names if alias.name == "_construct")
            elif isinstance(node, ast.Import):
                modules.update(alias.asname or alias.name for alias in node.names if alias.name == _module(DATA))
        for node in ast.walk(unit.tree):
            if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in direct) or (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Load)
                and node.attr == "_construct"
                and ast.unparse(node.value) in modules
            ):
                found.add((unit.path, id(node)))
    return found


def _directional_core_transfers(selected, failures, receipt):
    """The original caller owns result contracts; the cores return DATA only."""
    for symbol, source, diagnostic in (
        (
            "_encode",
            "return _encode_values(data, env_ref_names, result_tools, prepared, memory_completion=memory_completion, application=application, recorder_source=recorder_source)",
            "directional encode caller changed",
        ),
        (
            "_decode_graph",
            "return _decode_graph_values(encoded, env_ref_names, prepared=prepared, memory_completion=memory_completion, application=application, recorder_source=recorder_source)[0]",
            "directional decode caller changed",
        ),
    ):
        statement = _one(failures, receipt, "member_uses", DATA, selected[DATA, symbol], ast.Return, source, diagnostic)
        if statement is not None:
            call = statement.value.value if isinstance(statement.value, ast.Subscript) else statement.value
            _require(failures, isinstance(call, ast.Call), diagnostic)
            if isinstance(call, ast.Call):
                _sites(receipt, "external_data_calls", DATA, [call])
                _sites(receipt, "external_data_uses", DATA, [call.func])
    decode = selected[DATA, "_decode_graph_values"]
    _one(
        failures,
        receipt,
        "member_uses",
        DATA,
        decode,
        ast.If,
        'if type(slot) is not int or not 0 <= slot < len(recorder_source.snapshot.llm_calls):\n    _fail("application LLM has an invalid original recorder slot")',
        "exact recorder-slot guard changed",
    )
    visitors = [node for node in decode.body if isinstance(node, ast.FunctionDef) and node.name == "visit"]
    claims = (
        'if slot in claimed_llm_slots:\n    _fail("distinct application LLM nodes claim the same recorder slot")',
        "claimed_llm_slots[slot] = index",
        "from elspeth.web.composer.continuation_application_graph import construct_application_llm",
        "result = construct_application_llm(recorder_source, slot)",
    )
    windows = []
    if len(visitors) == 1:
        for branch in ast.walk(visitors[0]):
            if not isinstance(branch, ast.If) or not _same(branch.test, "kind == 'application_llm'", expression=True):
                continue
            for index in range(len(branch.body) - len(claims) + 1):
                if all(_same(node, expected) for node, expected in zip(branch.body[index : index + len(claims)], claims, strict=True)):
                    windows.append(branch.body[index : index + len(claims)])
    _require(failures, len(windows) == 1, "recorder-slot claim order changed")
    _one(
        failures,
        receipt,
        "member_uses",
        DATA,
        decode,
        ast.If,
        'if any(actual_slots.get(id(restored[index])) != slot for slot, index in claimed_llm_slots.items()):\n    _fail("application LLM changed its canonical first recorder identity slot")',
        "canonical recorder-slot identity changed",
    )
    for symbol, source in (
        ("application_recorder_slots", "slots.setdefault(id(call), index)"),
        ("construct_application_llm", 'restored = TypeAdapter(ComposerLLMCall).validate_json(encoded, strict=True, extra="forbid")'),
        (
            "require_application_recorder_source",
            'if type(source.snapshot) is not RecorderSnapshot or encode_recorder_snapshot(source.snapshot) != source.chunk:\n    raise AuditIntegrityError("application recorder changed its complete original fields")',
        ),
    ):
        kind = ast.Expr if symbol == "application_recorder_slots" else ast.Assign if symbol == "construct_application_llm" else ast.If
        _one(
            failures,
            receipt,
            "member_uses",
            APPLICATION,
            selected[APPLICATION, symbol],
            kind,
            source,
            "complete recorder source transfer changed " + symbol,
        )


def _external_calls(selected, failures, receipt):
    for name in ("encode_application_graph", "decode_application_graph"):
        owner = selected[APPLICATION, name]
        expected = {
            "_decode_graph_values(encoded, env_ref_names, application=True, recorder_source=recorder_source)",
            "_encode_values(restored.selected, env_ref_names, result_tools, application=restored, recorder_source=recorder_source)",
        }
        if name == "encode_application_graph":
            expected.add("_encode(graph.selected, env_ref_names, application=graph, recorder_source=recorder_source)")
        actual = []
        for node in ast.walk(owner):
            if (
                not isinstance(node, ast.Call)
                or not isinstance(node.func, ast.Name)
                or node.func.id not in {"_encode", "_encode_values", "_decode_graph_values"}
            ):
                continue
            expression = ast.unparse(node)
            actual.append(expression)
            _require(failures, expression in expected, "application external DATA caller changed " + name)
            _sites(receipt, "external_data_calls", APPLICATION, [node])
            _sites(receipt, "external_data_uses", APPLICATION, [node.func])
        _require(failures, len(actual) == len(expected) and set(actual) == expected, "application external DATA caller changed " + name)
    memory = _one(
        failures,
        receipt,
        "external_data_calls",
        FAMILIES,
        selected[FAMILIES, "_read_current_sources"],
        ast.Call,
        "decode_memory_completion_batch_data(accumulator.canonical, env_ref_names=inventory.names)",
        "current-source memory decoder call changed",
        expression=True,
    )
    if memory is not None:
        _sites(receipt, "external_data_uses", FAMILIES, [memory.func])


def batch_data_local_contracts(units):
    """Supplement the unchanged native three-constructor/common-effect proof."""
    failures, receipt = [], _empty()
    index, selected = _bind(units, failures, receipt)
    if failures:
        return failures, {}
    unit = index[DATA]
    _namespaces(index, failures, receipt)
    _imports(unit, failures, receipt)
    _schema_members(unit, selected, failures, receipt)
    _signatures(selected, failures)
    _memory_and_application(unit, selected, failures, receipt)
    _directional_core_transfers(selected, failures, receipt)
    _external_calls(selected, failures, receipt)
    native = _one(
        failures,
        receipt,
        "external_data_calls",
        DATA,
        selected[DATA, "_decode_graph_values"],
        ast.Call,
        "_construct(kind, members)",
        "actual constructor entry call changed",
        expression=True,
    )
    expected = {(DATA, id(native.func))} if native is not None else set()
    _require(failures, _constructor_entry_references(units) == expected, "constructor entry reference inventory changed")
    receipt["protected_origins"].update(
        {
            "elspeth.web.composer.tools._registry.response_contract_for",
            _module(GENERATION) + ".PLUGIN_SCHEMA_RESPONSE_CONTRACT",
            "elspeth.contracts.composer_audit.ComposerToolInvocation",
            "elspeth.contracts.composer_audit.ComposerToolStatus",
            "elspeth.contracts.freeze.freeze_fields",
            "dataclasses.dataclass",
        }
    )
    for name in SCHEMA_FIELDS:
        receipt["supplier_bindings"].add(_module(DATA) + "." + name)
    receipt["receiver_members"].setdefault(_module(GENERATION) + ".AdmittedPluginSchemaResponse", set()).update(
        {"contract", "readmit", "to_wire"}
    )
    receipt["receiver_members"].setdefault(_module(DISCOVERY) + ".AdmittedDiscoveryResult", set()).update(
        {"contract", "to_dict", "to_tool_result"}
    )
    receipt["receiver_members"].setdefault(_module(GENERATION) + ".PluginSchemaResponseContract", set()).add("admit")
    receipt["supplier_bindings"].update(
        _module(DATA) + "." + name
        for name in (
            "ComposerToolInvocation",
            "ComposerToolStatus",
            "decode_retained_tool_receipt",
            "require_receipt_result",
            "PLUGIN_SCHEMA_RESPONSE_CONTRACT",
            "response_contract_for",
        )
    )
    if failures:
        return failures, {}
    return [], {
        **{
            key: frozenset(receipt[key])
            for key in (*SITE_KEYS, "protected_origins", "function_origins", "class_origins", "supplier_bindings")
        },
        "receiver_members": {owner: frozenset(members) for owner, members in receipt["receiver_members"].items()},
    }
