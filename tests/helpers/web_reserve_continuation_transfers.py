"""Finite continuation transfers on caller-owned source ASTs.

These receipts supply local ownership facts to the existing common origin and
effect proof. They do not grant runtime SQL/provider authority. Exact selected
body and declaration bindings supplement the concrete constructor, caller,
checkpoint, registration and completion checks below. No receipt survives a
failed premise, and every returned site belongs to the supplied source trees.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import textwrap

from elspeth_lints.core.ast_dump import stable_ast_dump
from tests.helpers import web_reserve_composer_receiver as receiver

CARRIERS = "src/elspeth/web/composer/_compose_loop_carriers.py"
RUNTIME = "src/elspeth/web/composer/continuation_runtime.py"
INITIALIZATION = "src/elspeth/web/composer/continuation_composition_initialization.py"
INITIAL_DATA = "src/elspeth/web/composer/continuation_composition_initialization_data.py"
INITIAL_PUBLICATION = "src/elspeth/web/sessions/continuation_composition_initialization.py"
FIRST_RUNTIME = "src/elspeth/web/composer/continuation_first_runtime.py"
FREEZE = "src/elspeth/contracts/freeze.py"
CACHES = "src/elspeth/web/composer/continuation_caches.py"
FINISHER = "src/elspeth/web/composer/continuation_attachment_finisher.py"
ATTACHMENT_DRIVER = "src/elspeth/web/composer/continuation_attachment_application_driver.py"
ATTACHMENT_RESULT = "src/elspeth/web/composer/continuation_attachment_result.py"
ATTACHMENT_APPLICATION = "src/elspeth/web/composer/continuation_attachment_application.py"
CHECKPOINT = "src/elspeth/web/sessions/continuation_attachment_result.py"
PRETITLE = "src/elspeth/web/composer/continuation_initialization.py"
ROUTE_HELPERS = "src/elspeth/web/sessions/routes/_helpers.py"
SERVICE = "src/elspeth/web/composer/service.py"
SESSIONS = "src/elspeth/web/sessions/service.py"
BATCH = "src/elspeth/web/composer/tool_batch.py"
MESSAGE_BUILDERS = "src/elspeth/web/composer/continuation_message_builders.py"
FAMILY_BUILDERS = "src/elspeth/web/composer/continuation_message_family_builders.py"
PRODUCERS = "src/elspeth/web/composer/continuation_prompt_producers.py"
ARGUMENT_MESSAGES = "src/elspeth/web/composer/continuation_tool_argument_messages.py"
DISCOVERY_CACHE = "src/elspeth/web/composer/discovery_cache.py"
PROVIDER_THINKING = "src/elspeth/web/composer/provider_thinking.py"

# Each binding is a specifically reviewed current declaration/body, not a
# renewable corpus baseline. Mutation controls exercise these with real owners.
_CONTRACTS = {
    CARRIERS: {
        "_ComposeIterationState": "5b7f18b7a861edda90eac46eaaa86423df0b88e0fe3de755007920af02f8a46f",
        "_ComposeIterationBindings": "8f27ec143b0913cff07f20d11b72062e9a9e3da6a055ee2ae9ed484efcf5de73",
    },
    RUNTIME: {
        "RestoredContinuationRuntime": "195cdd725c38b3b1562a41e8fd33c303873926545e7dbc00e3e49a40cf37c098",
        "_work_owners": "1fada800b13cd96d92dbb9de7f7e597e41d48b7edf8b6d24a86910bc0d7acf50",
        "_registered_read": "30eaa77dd59549652234696c9fd7fbbef8cd5da0e342b6285f4b0f8c82225ef9",
        "_restore_live": "14f718fc149331eacbf75823064ef405120601181554bb71c34c9defa482e729",
        "restore_continuation_runtime": "9a7c0e445f8e6d6a0f2330c51e78cd93db44cb96d1b2db954ef7f49589b730b5",
        "_capture_owners": "9024aaa16c0dd21e397cc98436ed5eadd779c169729dfdfa2a82a64fb0e66322",
        "capture_continuation_runtime_owners": "7634f7a7760374ba74ab52aa7c97a55466ddc4d0e438c8c442a605a8a2eb6f83",
    },
    INITIALIZATION: {
        "_read_source": "930122c8723ffeaf34e06aac5c2fd8a74e51e7ae552780becda04cfcc8a74d0a",
        "_build_owners": "0071d008384de02c98745f37ffb9382c33b4846f65d4f0e44f04d1a6ece53e9e",
        "_finish_preparation": "2c8069d9869e015f7b30b9a27a76966a8899e66d14bb69d788adcf90797cc50b",
        "_prepare": "54b7068a220964720820b4c51c8524dd3f899e4c3fb43c67cb9f0d79bbe0ccd6",
        "_bind_first_state_on_connection": "eef4fec80a8af93d7c194409a597bbe372d4e9ab3bcbe0413cf36df3e96833cd",
        "_consume": "efada4ceb3273dcc661de85e69786485bf476fb8b5a91de24fb405246e4ee4b9",
        "initialize_continuation_composition": "cddf6d272b52c42bd758452461f7d3da934bca02e5e10d4890d8727a82fbc3ee",
    },
    INITIAL_DATA: {
        "InitialCompositionOwners": "a3cdda92d287d3f9abeb1d74f49b3adccfcbb1931e966cc86080847a1d203490",
        "capture_initial_composition": "742e921fc44ccb4f3eb9bcf6e7070de73c4f893397d63f0891d89f6160033c5d",
    },
    INITIAL_PUBLICATION: {
        "publish_composition_initialization_on_connection": "bb4c0285311eea1caf7ca4cb4a04e691fb853afbdd392db9785bdc2bd6d8c534",
    },
    FIRST_RUNTIME: {
        "_require_same_sources": "34e3815e1d152e012fa83bdc34f1472635e1196619904faa12854c96c8d42157",
        "_read_first_sources": "34b9028427bea9a8f3f38f100819d3f2cdbfa5b7556eb3b94bafb2e1a3264a00",
        "_restore_first_graph": "f8e4e03046e02fef5575d0f4a7d0783f2d8b754ddec8a423700657f910e95f8b",
        "_registered_graph": "97cdeae4d6d65e0d93c9242f6917b8b906bda56d56ce4c94a20c358fc6ab75aa",
        "restore_first_continuation_runtime": "dfeb0ea55d502e567b2db815c33278674677a12b88e3099072772f80e46e56d3",
    },
    FREEZE: {
        "FrozenJsonArray": "ab2304eccace21563ba875ffebc8a93cd4433cac74860dbb3d34215dba1e119e",
        "deep_freeze": "30f7bd0766b066de645f366f30d9ade4e9c62260000923a2f5bfcea856a6db17",
        "freeze_fields": "85eb41c203c39cf4ca4287f9469b4ba9c536ecb429a9f2c6308632ec923b1bdc",
    },
    CACHES: {
        "ContinuationCachesSnapshot": "b4bfd4c19249defd5a264d799e336726b50ef75483b3f31299d01b017e88e642",
        "ColdComposerCaches": "5e4285a17a5d02e00bcd159334f3903be53756b731945652e5340a5e227b71f7",
        "restore_continuation_caches": "8bd58ab7c938ea0e9db0a3437594c5795e21ca1696ec49a87d5df3adbc7562bf",
    },
    FINISHER: {
        "CompletedAttachmentProjection": "cb78978f9a57a4fca5f2f965f87f6736f2155dacd5c1585f0faefa3054ae459a",
        "finish_current_attachment_result": "7b04ae5633ef0bf7490b52adf7516030069b1f142c51a5356fc7b2d9cdd1a7f7",
    },
    ATTACHMENT_DRIVER: {
        "read_current_applied_attachment": "a963a789b0a292a7cd50da87cba13ef8827c22335ed31da59e36f293bdc32539",
        "apply_current_attachment_runtime": "c4c80d0c8c31a087b12276302795d4f2a2eea17de04ba6188dbfec6988e53294",
    },
    ATTACHMENT_APPLICATION: {
        "_append_message": "4f7326633989a70bc181468ba39819ff3baa893a6a0ee055b6dea137638c88af",
        "_register_page": "30ee6cc2d1c3331ffcfa36a613976411bddd36dc734635d2938e5b8e0232278d",
        "require_attachment_projection": "ce75a6b5fb4ffe0a74cdab10d883e48d177998f0269e45380b9c6df522f3a39a",
        "admit_attachment_application": "9afe362ca0e8a10720ed23d185f403966c1435052aed8d1a617edcde2b39d803",
    },
    ATTACHMENT_RESULT: {
        "_owners": "58dd9cfd7480f34526f12c922dbc73fb98c112fe5bfba91f7f05e6015ae7f951",
        "_project": "22e12f820063154280508a8ef13e2546951a6d7e4fe064704bd76751a9f34ecd",
        "read_attachment_result": "7b142092b8c9518fa1ad65b225fcc3770a64b7ede2ed648ac4858167aa8b527c",
        "run_attachment_result": "f974b33e474c89cf2110e39905e206d3c9fb795b91797c2e7871a933553ba3f4",
    },
    CHECKPOINT: {
        "AttachmentResultCheckpoint": "7f1b631b894339da19f59b2aef39fe04e037eff4df3ca65eeac465594dd1dd37",
        "_selected": "47648159c619e836e4c7f786459a2b1292df91ea04096d4874b293cb2cb7a858",
        "_chunk": "a43b2ad39cc3c5bd1b3db38cc474443f6258839d02aebeac34da8db7cdb7f195",
        "_input": "b8e92a5f469056a59c5893f073e56af4c29c68f219599d6a32f20b2f8a5cf459",
        "_history": "e09ddd3a1d7eca509392173e975bd906ba175fddc986a871a4f83dee19292049",
        "_effect": "99ca18775d5de44c1ca3b2136f25e6e161fc7f5f53ee2acafd0ec7d663e9171c",
        "_result": "f4701f3e0dfb45cfe624c4437622169c6de34407bf50a5faf4b67e2fa410c1cf",
        "prepare_attachment_result_on_connection": "92000096232b140d8c4256cb11c8dbb62d00070a9a1cf9fc60b2428b0d429d96",
        "require_attachment_read_audit": "a6a50db8e7b37b2faa82589c20311a26fa1f3253a74ad9de33e8fdbe66f24d7f",
        "_retained_history": "3e28175b788978059d1ba8a23a61d76d7d240e06ba9023e950c01f724a251f1c",
        "read_attachment_result_on_connection": "a18c21fcfede284f6c62546e2617791adacc496af548260d70fcb83b38507d5c",
        "verify_attachment_result_source_on_connection": "98d7ed35401fe6276af381104029415113863c812ede3fd34ba75ee4a7ef739c",
    },
    PRETITLE: {
        "_sessions": "a9799d22a147d402b4d2a3b61d97f9d709a9d4ab4c05cc3b28f44c09a6cc515d",
        "_read_inserted": "7f5f70dd676f3934e78ac7225e84b8ef594599a39fa9892af9633f2a66091403",
        "_read_published": "6f965d380eda01f70d6544089bb4e4f5bc2464cf4830c4ef0a4dce6990176e08",
        "_publish": "ff2a6e28089267caba143a909d40e77fb91d0ae44fbfd857e96209d51a030afc",
        "_registered": "61b1a31c77cde09183075cd54aa77d9b68674d6c612101b62ae7c012f16a0f7f",
        "_prepare_skill_history": "71aa571807dafb1a7c9b58a5e37f11c6105719dc4fdfbd7deefd51ccf5d88ddf",
        "_initial_disclosure": "b260fc931ff3234c3ed480f26be6a691a2ce5d6a8698f1f58e0699be193fc116",
        "prepare_pretitle_bootstrap": "520cabb77a49b019b0458443887682f42104ab5d28ed817c38cd61dfad8b5731",
    },
    ROUTE_HELPERS: {"_join_freeform_owned_task": "35c99dc6b72f109f671dc0207dc4bc2d1b1f4dfde134930f8e5b7a70b9c8ede1"},
    SERVICE: {
        "ComposerServiceImpl._maybe_upsert_skill_markdown_history": "9dc17a2a47bdd1a78378fdd684b18e48500f2c233554ab299f0a58e16bef945e",
        "_chat_ingress_inputs_for_compose": "2660aa0e6cbb24b1bfb0993766735d57923925a1c7a3caf11f4d88d95fe533a2",
    },
    SESSIONS: {"SessionServiceImpl.upsert_skill_markdown_history": "5631aefb606b3047065e415c746ae15795df19bb9722010ec67d0c15b5273e7d"},
    BATCH: {
        "_retain_llm_tool_argument_parts": "b8c590ad654f78bed89339fb89bb3244cc806ddf3347365706af731833a17a3e",
        "_replace_llm_tool_call_arguments": "2b0434e066dc1f90322edf2e8e3f630652062a0514fc22756c109552e9428889",
        "_register_continuation_assistant_message": "56ed58e18e85cff61e9817715d5ec2629802d0ad464b6a1536c1f7602712993d",
    },
    MESSAGE_BUILDERS: {"tool_result_message": "5105eba42b9d5a9a12bed680a2107a52fbf91f538b7d6cb04b4515ff0b7f0e27"},
    FAMILY_BUILDERS: {
        "native_assistant_message": "4e75e518acd1f6108596471feff571c156ca399e171a53c0d7b76e0b256e2eb1",
        "family_message_wire": "5a1d272ba95d7b4e49e7944090570c18fe9b5dcbb314703766a413d133e708dd",
        "retained_family_wire": "6af50f3fe99d236b6115f7c10a2e6a4e95cba49bc62537f1a7049357e0c2e41d",
        "_require_native_source": "73086c22b45b43e3fe802620470a1df1dc18b3d00c3c77fcc545b4f081b71abc",
        "_native_message_from_wire": "9a015a00150943abb4b2792ec87ce79050bd5bcbf157eeffefc8a2041337157f",
        "capture_native_message_recipe": "1840a2f526cc7227bd1f94db4f547cb0ba4ed12808d004906b20a1e821ada1fb",
        "require_native_message_refresh": "5deff7c3abe95da6219dfd6452dbc755c19a6a8731f37e6ab880ec17f19aed20",
        "attachment_result_message": "3544b405e98561990dc9c2f0878a170fb03b9ac02639cf92a30cef78040d76ae",
        "capture_attachment_result_recipe": "c5b6c6d919a69e973dac62caa6596a0a0e6cc4f8a42f68e6696ff91490676496",
    },
    PRODUCERS: {
        "_wire": "e0cc70d5d613cabac671e496048bdd46aea8a661710f0376b6f25e7f589933bc",
        "CurrentContinuationMessageProducers.__post_init__": "6045878e11e611a9682d7901b805bfb53a56ff18a36960c5f66d409e4f840a00",
        "CurrentContinuationMessageProducers._require_compose_family": "0cdad0b6282fee521e0df6a9973af0dddd73973b252df43a92dfff99c5b27dbd",
        "CurrentContinuationMessageProducers.require_context": "9ac16b32a1984ed5e362b33070e0b04ce626b5d1ba47a1c94bf53e3b44958842",
        "CurrentContinuationMessageProducers._register": "3691495de97489278e7827d53a1fc835ad4b82b2b5877a7fb24f816e372609cf",
        "CurrentContinuationMessageProducers.register_tool_call_message": "fa3c99e717b2603c25535c9587c1f5c21b6a0cdcdb21b6baa14144959d0ef190",
        "CurrentContinuationMessageProducers.register_schema_message": "edd6bc2b11c4c6b8fc47afd041a13f1d9d51a51c859bb5ff38338a6c6bbb7822",
        "CurrentContinuationMessageProducers.register_attachment_message": "0aa6ce8a62e4efd2810d90ecf1a3f0ea9a532d0b77ac184f976d7e79098a0d20",
    },
    ARGUMENT_MESSAGES: {
        "_indexes": "8e0271d8ec4957830f3f6313310714d319b112521b22d218465d4f6e4cc73bc2",
        "_ordinary_message": "dd03a28f49ff14be29251edc11044c60abe03986152480a2b7fe471543f197d1",
        "require_ordinary_argument_parts": "edc2e25ee903affa41bf3887b22cba5d5cb1deab3cbffd285ede6ff4ded9f665",
        "_require_ordinary_source": "f6c6da9251f912dadaef25d69cfa881b4db81d46a7f103aba8e5ec5f8f283250",
        "capture_tool_call_message_recipe": "d15f8fa174a98dd523304dea62e9eabdc459a5333929ddde8ea0cb15d0ccab9f",
        "require_tool_call_message_refresh": "0c85be9ff584c61a6e12e03337878077e02db390d095405dcf1a806701efa196",
    },
    DISCOVERY_CACHE: {"tool_result_content_parts": "b38c21d3b4fb654edef99dc3e73dd0d7a7f9683e4ecce91d9b0824eaaff499ff"},
    PROVIDER_THINKING: {
        "parse_native_thinking_blocks": "fcc0dc258bd5fe2e7bf5f7e8b810505e050c88682276e89f89e7ccd99a794a9a",
        "native_thinking_blocks": "435bdaa14eff7160f0bcc1681df76733127ee4170b75aa85bfdf38189c066a67",
        "attach_provider_thinking": "bbb1a2535e4310a73fdb20d1ca349463ce734f4c3d21f2a4df5a57f1868071a1",
    },
}

_NAMESPACES = {
    CARRIERS: "8d2c2569e860710d040a2399a5e4167881920bb9565a95407a7d4564cfe25f9c",
    RUNTIME: "6eb6f2a8e743f484ce6010469c3ee7ddb355a5bedfdfca8c638a4d6d05e447f0",
    INITIALIZATION: "6536dc9ab162bc576c9fba059b0a80a85a2f84f58108ed871260b8cd4e32a95e",
    INITIAL_DATA: "5dd11778d1b9d5405c6ce58383cd02b53a4bc4f97a8b6befc9aab01882b669e9",
    INITIAL_PUBLICATION: "25f499509f5972d36111fd648ba5abae25a3b0c00d0063cd7e4c19e88006f49c",
    FIRST_RUNTIME: "b5b4599f3dafc011662286fb0131a69ebaeed31b536677099ce655902a8900e2",
    FREEZE: "327e64409e2caca0c9ddf73fb3122c346fef89a74e8bc843c659c72c9a1b1a0e",
    CACHES: "625b97608140682adbc8a83fd0ba44c4f08f6e9facde4eb943b6a439fe87517b",
    FINISHER: "cff6d017e95e37012ef491899a4c4482c9bc5f4a42c6a8c711d738a1d040dbf3",
    ATTACHMENT_DRIVER: "4a9d370ed7a41b4692144b1fae783e7b3ef8d9781040b18d44a1ef69864a2af9",
    ATTACHMENT_RESULT: "90541fcf5407eb03b9555ca08b34a677b300f5a2d832a84c93d6cf6ddbf8e6e2",
    ATTACHMENT_APPLICATION: "0c4ec5dbf6333aa9797ea87126a03f183d63d4c2e79858bd63932347b0e7a19e",
    CHECKPOINT: "ff768b7049488416c47a3b9161593e1f93db41c408077531097c1a5e405e5af6",
    PRETITLE: "fc7537a2a015475b5359d96cee3a1f5f4d2164c9adbd8586e6402ba9e448e37f",
    ROUTE_HELPERS: "25cb1d5770cc13b85004814b543d4293a83bcbdc8a2215d1aab06b89a8f36bde",
    SERVICE: "9d3ac3c160e995a18871b24de83cf77b4874db8a57ac7c3cbcfe6bc8bf7c4ca6",
    SESSIONS: "9dc7ccce3b847a9f2d06baed91cb975614511ca6afa04126ec98b3700119e60f",
    BATCH: "fafcefeb6965fcaa27789a408ae1d1a59e55a730e82d2fc16a32baae4343dda5",
    MESSAGE_BUILDERS: "b14fcdde6706a5f9715d88c51c9fccbbd5951d5ce69aff8018f719e34db7b030",
    FAMILY_BUILDERS: "a4edfe2e92b9ef87a61c17b87a2e89495f5014593b9280f4e0342eb285705d53",
    PRODUCERS: "d368ae8d09030c79af74858915a27b19145d8b5a7ebdca171bdc30206bae8a7a",
    ARGUMENT_MESSAGES: "1ebabde9cfbef7c13468cdcdc3db2b9aa8bd440ead25da4e38859324e7977e4d",
    DISCOVERY_CACHE: "bdea5fd438a967c112c6bdb9fac33d2392d1f1ec6d146280ef1b96bd5daeb24c",
    PROVIDER_THINKING: "3eb59f67cefeb6178703c5631cee8b530d29d0382639dd3ea505d73558264d78",
}
REQUIRED_PATHS = tuple(_NAMESPACES)
SITE_KEYS = (
    "selected_definitions",
    "class_uses",
    "constructor_calls",
    "typecheck_uses",
    "batch_typecheck_uses",
    "data_constructor_calls",
    "data_constructor_uses",
    "state_writes",
    "reserve_calls",
    "calls",
    "uses",
    "native_calls",
    "native_uses",
    "retained_comparison_uses",
    "retained_defaults",
    "import_nodes",
)
LIVE_FIELDS = (
    "state",
    "initial_version",
    "llm_messages",
    "tools",
    "recorder",
    "composition_turns_used",
    "discovery_turns_used",
    "mutation_success_seen",
    "discovery_cache",
    "last_validation",
    "runtime_preflight_cache",
    "last_runtime_preflight",
    "anti_anchor",
    "advisor_calls_used",
    "repair_turns_used",
    "advisor_checkpoint_passes_used",
    "advisor_review_state",
    "persisted_assistant_message_id",
    "persisted_assistant_content",
    "persisted_tool_call_turn",
    "failed_turn",
    "current_state_id",
    "advisor_repair_context_introduced",
    "pending_advisor_elision_indices",
    "turn_evidence",
)


class _Namespace(ast.NodeTransformer):
    def visit_FunctionDef(self, node):
        clone = copy.copy(node)
        clone.body = []
        return clone

    visit_AsyncFunctionDef = visit_FunctionDef


def _digest(node):
    return hashlib.sha256(stable_ast_dump(node).encode("utf-8")).hexdigest()


def _module(path):
    return path.removeprefix("src/").removesuffix(".py").replace("/", ".")


def _find(tree, name):
    return receiver.find(tree, name)


def _same(node, source):
    expected = ast.parse(textwrap.dedent(source)).body
    return len(expected) == 1 and ast.dump(node, include_attributes=False) == ast.dump(expected[0], include_attributes=False)


def _empty():
    return {key: set() for key in SITE_KEYS} | {
        "origins": set(),
        "function_origins": set(),
        "class_origins": set(),
        "supplier_bindings": set(),
        "receiver_members": {},
    }


def _require(failures, condition, detail):
    if not condition:
        failures.append(detail)


def _select(units, paths, phase):
    index = {unit.path: unit for unit in units}
    if len(index) != len(units) or any(path not in index for path in paths):
        return [phase + " source set missing or duplicated"], {}, {}, {}
    failures, selected, receipt = [], {}, _empty()
    for path in paths:
        unit = index[path]
        # Work on a detached AST; namespace calculation must never mutate the
        # caller's source identities while deciding a same-AST receipt.
        namespace = _Namespace().visit(copy.deepcopy(unit.tree))
        _require(failures, _digest(namespace) == _NAMESPACES[path], phase + " declaration namespace changed " + path)
        receipt["import_nodes"].update((path, id(node)) for node in ast.walk(unit.tree) if isinstance(node, (ast.Import, ast.ImportFrom)))
        for name, digest in _CONTRACTS[path].items():
            node = _find(unit.tree, name)
            detail = "selected body changed " + name
            if name == "_ComposeIterationState":
                detail = "state declaration changed"
            elif name == "_ComposeIterationBindings":
                detail = "bindings declaration changed"
            _require(failures, node is not None and _digest(node) == digest, phase + " " + detail)
            if node is None:
                continue
            selected[path, name] = node
            receipt["selected_definitions"].add((path, id(node)))
            origin = _module(path) + "." + name
            receipt["origins"].add(origin)
            if isinstance(node, ast.ClassDef):
                receipt["class_origins"].add(origin)
            else:
                receipt["function_origins"].add(origin)
            receipt["supplier_bindings"].add(_module(path) + "." + name.split(".")[0])
            if "." not in name:
                _require(failures, receiver.owned_binding(unit.tree, name, node), phase + " selected binding changed " + name)
            for child in ast.walk(node):
                if isinstance(child, (ast.Name, ast.Attribute)) and isinstance(child.ctx, ast.Load):
                    receipt["uses"].add((path, id(child)))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = name.rsplit(".", 1)[0] if "." in name else None
                if owner is not None:
                    receipt["receiver_members"].setdefault(_module(path) + "." + owner, set()).add(node.name)
                _require(
                    failures,
                    not node.type_params or name in {"_registered_read", "_registered", "_project", "_join_freeform_owned_task"},
                    phase + " generic owner changed " + name,
                )
    return failures, index, selected, receipt


def _calls(node, expression):
    expected = ast.dump(ast.parse(expression, mode="eval").body, include_attributes=False)
    return [child for child in ast.walk(node) if isinstance(child, ast.Call) and ast.dump(child, include_attributes=False) == expected]


def _one_call(failures, receipt, path, node, expression, detail, *, key="calls"):
    matches = _calls(node, expression)
    _require(failures, len(matches) == 1, detail)
    if len(matches) != 1:
        return None
    receipt[key].add((path, id(matches[0])))
    return matches[0]


def _references(units, names):
    return {
        (unit.path, id(node))
        for unit in units
        for node in ast.walk(unit.tree)
        if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in names)
        or (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and node.attr in names)
    }


def _finish(failures, receipt):
    if failures:
        return failures, {}
    return [], {
        **{key: frozenset(receipt[key]) for key in (*SITE_KEYS, "origins", "function_origins", "class_origins", "supplier_bindings")},
        "receiver_members": {owner: frozenset(members) for owner, members in receipt["receiver_members"].items()},
        "live_transfer_fields": LIVE_FIELDS,
    }


_PUBLICATION_NOMINAL_GUARD = """if (
    type(owners) is not InitialCompositionOwners
    or type(captured) is not CapturedInitialComposition
    or type(owners.live) is not _ComposeIterationState
):
    raise AuditIntegrityError("first Compose publication requires its actual complete capture owners")
"""


def iteration_local_transfers(units):
    """Exact extra state/bindings construction and the first-state SQL join."""
    paths = (CARRIERS, RUNTIME, INITIALIZATION, INITIAL_DATA, INITIAL_PUBLICATION, FIRST_RUNTIME, FREEZE, CACHES, SERVICE)
    failures, index, selected, receipt = _select(units, paths, "continuation iteration")
    if failures:
        return failures, {}
    state = selected[CARRIERS, "_ComposeIterationState"]
    fields = [node for node in state.body if isinstance(node, ast.AnnAssign)]
    _require(
        failures,
        len(state.body) == len(fields) and tuple(node.target.id for node in fields) == (*LIVE_FIELDS, "continuation_cache_history"),
        "continuation iteration exact field inventory changed",
    )
    _require(
        failures,
        _same(fields[-1], "continuation_cache_history: ContinuationCachesSnapshot | None = None"),
        "continuation iteration retained-history default changed",
    )
    binding = selected[CARRIERS, "_ComposeIterationBindings"]
    post = _find(index[CARRIERS].tree, "_ComposeIterationBindings.__post_init__")
    _require(
        failures,
        _same(post, 'def __post_init__(self) -> None:\n    freeze_fields(self, "chat_ingress_inputs")'),
        "continuation iteration exact value freeze changed",
    )
    _require(
        failures,
        receiver.imported(index[CARRIERS].tree, "freeze_fields", "elspeth.contracts.freeze", "freeze_fields"),
        "continuation iteration freeze supplier changed",
    )
    receipt["origins"].update({"dataclasses.dataclass", "elspeth.contracts.freeze.freeze_fields", "elspeth.contracts.freeze.deep_freeze"})
    receipt["supplier_bindings"].add(_module(CARRIERS) + ".freeze_fields")
    _one_call(failures, receipt, CARRIERS, post, 'freeze_fields(self, "chat_ingress_inputs")', "continuation iteration freeze call changed")
    extra_owners = (
        (INITIALIZATION, selected[INITIALIZATION, "_build_owners"]),
        (INITIAL_DATA, selected[INITIAL_DATA, "InitialCompositionOwners"]),
        (INITIAL_DATA, selected[INITIAL_DATA, "capture_initial_composition"]),
        (RUNTIME, selected[RUNTIME, "RestoredContinuationRuntime"]),
        (RUNTIME, selected[RUNTIME, "_restore_live"]),
    )
    names = {"_ComposeIterationState", "_ComposeIterationBindings"}
    receipt["class_uses"].update(
        (path, id(node))
        for path, owner in extra_owners
        for node in ast.walk(owner)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in names
    )
    for path, owner in extra_owners:
        receipt["constructor_calls"].update(
            (path, id(node))
            for node in ast.walk(owner)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in names
        )
    check = selected[INITIAL_DATA, "capture_initial_composition"]
    receipt["typecheck_uses"].update(
        (INITIAL_DATA, id(node)) for node in ast.walk(check) if isinstance(node, ast.Name) and node.id == "_ComposeIterationState"
    )
    publisher = selected[INITIAL_PUBLICATION, "publish_composition_initialization_on_connection"]
    publisher_tree = index[INITIAL_PUBLICATION].tree
    guard_names = {"type", "_ComposeIterationState", "InitialCompositionOwners", "CapturedInitialComposition", "AuditIntegrityError"}
    publisher_bindings = receiver.lexical_binding_events(publisher)
    _require(
        failures,
        receiver.builtin_binding(publisher_tree, "type")
        and all(
            receiver.imported(publisher_tree, binding, module, member)
            for binding, module, member in (
                ("_ComposeIterationState", _module(CARRIERS), "_ComposeIterationState"),
                ("InitialCompositionOwners", _module(INITIAL_DATA), "InitialCompositionOwners"),
                ("CapturedInitialComposition", _module(INITIAL_DATA), "CapturedInitialComposition"),
                ("AuditIntegrityError", "elspeth.contracts.errors", "AuditIntegrityError"),
            )
        )
        and not any(name in guard_names for name, _kind, _node in publisher_bindings)
        and tuple(parameter.arg for parameter in publisher.args.kwonlyargs) == ("owners", "captured")
        and all(
            [(kind, node) for name, kind, node in publisher_bindings if name == parameter.arg] == [("PARAMETER", parameter)]
            for parameter in publisher.args.kwonlyargs
        ),
        "continuation iteration publisher nominal suppliers changed",
    )
    # This is one nominal comparison operand, not every reference in the SQL
    # publisher. Complete source bindings protect its other effects; the common
    # pass still proves imports/origins and the final complete class-use set.
    publication_guard = publisher.body[0] if publisher.body else None
    guard_matches = publication_guard is not None and _same(publication_guard, _PUBLICATION_NOMINAL_GUARD)
    _require(failures, guard_matches, "continuation iteration publisher nominal guard changed")
    if guard_matches:
        nominal_use = (INITIAL_PUBLICATION, id(publication_guard.test.values[2].comparators[0]))
        receipt["class_uses"].add(nominal_use)
        receipt["typecheck_uses"].add(nominal_use)
    loop = _find(index[SERVICE].tree, "ComposerServiceImpl._compose_loop")
    advance = _find(index[SERVICE].tree, "ComposerServiceImpl._advance_live_compose_iteration")
    ordinary = {
        (SERVICE, id(node))
        for owner in (loop, advance)
        for node in ast.walk(owner)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in names
    }
    _require(
        failures, _references(units, names) == ordinary | receipt["class_uses"], "continuation iteration class reference inventory changed"
    )
    restore = selected[RUNTIME, "_restore_live"]
    history_arguments = [
        keyword.value
        for node in ast.walk(restore)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_ComposeIterationState"
        for keyword in node.keywords
        if keyword.arg == "continuation_cache_history"
    ]
    _require(
        failures,
        len(history_arguments) == 1 and ast.unparse(history_arguments[0]) == "caches.history",
        "continuation iteration restored-history argument changed",
    )
    join = selected[INITIALIZATION, "_bind_first_state_on_connection"]
    stores = [node for node in ast.walk(join) if isinstance(node, ast.Assign) and _same(node, "runtime.live.state = effects.initial_state")]
    _require(failures, len(stores) == 1, "continuation iteration first-state store changed")
    if len(stores) == 1:
        receipt["state_writes"].add((INITIALIZATION, id(stores[0].targets[0])))
    consume = selected[INITIALIZATION, "_consume"]
    joined = _one_call(
        failures,
        receipt,
        INITIALIZATION,
        consume,
        "_bind_first_state_on_connection(conn, service, running, runtime)",
        "continuation iteration first-state caller changed",
    )
    allowed_join = {(INITIALIZATION, id(joined.func))} if joined is not None else set()
    _require(
        failures,
        _references(units, {"_bind_first_state_on_connection"}) == allowed_join,
        "continuation iteration first-state reference inventory changed",
    )
    receipt["receiver_members"].setdefault(_module(CARRIERS) + "._ComposeIterationState", set()).update(
        (*LIVE_FIELDS, "continuation_cache_history")
    )
    receipt["receiver_members"].setdefault(_module(CARRIERS) + "._ComposeIterationBindings", set()).update(
        node.target.id for node in binding.body if isinstance(node, ast.AnnAssign)
    )
    return _finish(failures, receipt)


_ATTACHMENT_ADMISSION = """if retained_attachment is not None:
    from elspeth.web.sessions.continuation_attachment_result import AttachmentResultCheckpoint
    if (
        type(retained_attachment) is not AttachmentResultCheckpoint
        or not retained_attachment.history_applied
        or retained_attachment.prepared.batch is not batch
        or retained_attachment.prepared.accumulator is not acc
        or batch.current_call is not call
        or call.tool_name not in ("read_attachment", "get_blob_content")
        or retained_attachment.result.updated_state is not call.pre_state
        or call_model.completion.tool_batch.calls != retained_attachment.prepared.calls
    ):
        raise AuditIntegrityError("retained attachment execution changed its actual selected read graph")
"""
_ATTACHMENT_REGISTRATION = """if retained_attachment is not None:
    if ctx.message_producers is None or admitted_result is None:
        raise AuditIntegrityError("retained attachment result lost its actual current message producer")
    from elspeth.web.composer.continuation_codec import encode_continuation_response
    response_source = encode_continuation_response(call_model.completion, audit_call=call_model.audit_call)
    ctx.message_producers.register_attachment_message(
        response=response_source, call=tool_call, result=admitted_result.to_tool_result(), message=result_message
    )
"""


def attachment_local_transfers(units):
    """The one retained attachment B2 caller and its real message suppliers."""
    paths = (
        BATCH,
        FINISHER,
        ATTACHMENT_DRIVER,
        ATTACHMENT_RESULT,
        ATTACHMENT_APPLICATION,
        CHECKPOINT,
        MESSAGE_BUILDERS,
        FAMILY_BUILDERS,
        PRODUCERS,
        ARGUMENT_MESSAGES,
        DISCOVERY_CACHE,
        PROVIDER_THINKING,
    )
    failures, index, selected, receipt = _select(units, paths, "continuation attachment")
    if failures:
        return failures, {}
    execute = _find(index[BATCH].tree, "_execute_continuation_call_live")
    _require(failures, isinstance(execute, ast.AsyncFunctionDef), "continuation attachment execute owner changed")
    if not isinstance(execute, ast.AsyncFunctionDef):
        return failures, {}
    args = execute.args
    _require(
        failures,
        not args.posonlyargs
        and not args.args
        and args.vararg is None
        and args.kwarg is None
        and not args.defaults
        and tuple(arg.arg for arg in args.kwonlyargs)
        == ("call_model", "ctx", "acc", "llm_messages", "batch", "call", "retained_attachment")
        and args.kw_defaults[:-1] == [None] * 6
        and isinstance(args.kw_defaults[-1], ast.Constant)
        and args.kw_defaults[-1].value is None,
        "continuation attachment exact optional formal/default changed",
    )
    if args.kw_defaults and args.kw_defaults[-1] is not None:
        receipt["retained_defaults"].add((BATCH, id(args.kw_defaults[-1])))
    admission = [node for node in execute.body if isinstance(node, ast.If) and _same(node, _ATTACHMENT_ADMISSION)]
    registration = [node for node in ast.walk(execute) if isinstance(node, ast.If) and _same(node, _ATTACHMENT_REGISTRATION)]
    _require(failures, len(admission) == 1 and len(registration) == 1, "continuation attachment selected admission/registration changed")
    if len(admission) == 1:
        receipt["retained_comparison_uses"].update(
            (BATCH, id(value))
            for node in ast.walk(admission[0])
            if isinstance(node, ast.Compare)
            for value in node.comparators
            if isinstance(value, ast.Name) and value.id in {"batch", "call"}
        )
    dispatches = [node for node in ast.walk(execute) if isinstance(node, ast.AsyncFunctionDef) and node.name == "_do_dispatch"]
    _require(failures, len(dispatches) == 1, "continuation attachment actual dispatch closure changed")
    if len(dispatches) == 1:
        dispatch = dispatches[0]
        capture = [
            (arg, value) for arg, value in zip(dispatch.args.args, dispatch.args.defaults, strict=True) if arg.arg == "_retained_attachment"
        ]
        _require(
            failures,
            len(capture) == 1 and isinstance(capture[0][1], ast.Name) and capture[0][1].id == "retained_attachment",
            "continuation attachment retained closure capture changed",
        )
        branches = [
            node
            for node in dispatch.body
            if isinstance(node, ast.If)
            and _same(
                node, "if _retained_attachment is not None:\n    return admit_discovery_result(_tool_name, _retained_attachment.result)"
            )
        ]
        _require(
            failures,
            len(branches) == 1 and len(dispatch.body) >= 3 and dispatch.body[2] is branches[0],
            "continuation attachment retained-only dispatch branch changed",
        )
    finisher = selected[FINISHER, "finish_current_attachment_result"]
    # These concrete nominal checks occur outside the old batch/data receipt.
    # They are not constructors or permission to use the class elsewhere.
    for path, name in (
        (ATTACHMENT_RESULT, "run_attachment_result"),
        (CHECKPOINT, "_input"),
        (CHECKPOINT, "prepare_attachment_result_on_connection"),
        (CHECKPOINT, "_result"),
    ):
        owner = selected[path, name]
        nominal = [
            node
            for node in ast.walk(owner)
            if isinstance(node, ast.Compare)
            and len(node.comparators) == 1
            and isinstance(node.comparators[0], ast.Name)
            and node.comparators[0].id == "ContinuationCallState"
            and isinstance(node.left, ast.Call)
            and ast.unparse(node.left) == "type(call)"
        ]
        _require(failures, len(nominal) == 1, "continuation attachment exact nominal call check changed " + name)
        if len(nominal) == 1:
            receipt["batch_typecheck_uses"].add((path, id(nominal[0].comparators[0])))
    prepared_calls = [
        node
        for node in ast.walk(finisher)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "RestoredPreparedBatchData"
    ]
    _require(
        failures,
        len(prepared_calls) == 2
        and {ast.unparse(node) for node in prepared_calls}
        == {
            "RestoredPreparedBatchData(data.batch, data.accumulator, data.calls, restored.selected_audit, restored.selected_dialect)",
            "RestoredPreparedBatchData(before_graph.selected.batch, before_graph.selected.accumulator, before_graph.selected.calls, before_graph.selected_audit, before_graph.selected_dialect)",
        },
        "continuation attachment complete prepared constructor transfers changed",
    )
    receipt["data_constructor_calls"].update((FINISHER, id(node)) for node in prepared_calls)
    receipt["data_constructor_uses"].update((FINISHER, id(node.func)) for node in prepared_calls)
    native = _one_call(
        failures,
        receipt,
        FINISHER,
        finisher,
        "_execute_continuation_call_live(call_model=call_model, ctx=ctx, acc=data.accumulator, llm_messages=restored.messages, batch=data.batch, call=call, retained_attachment=bound_source)",
        "continuation attachment exact native caller changed",
        key="native_calls",
    )
    adapter = _find(index[BATCH].tree, "run_tool_batch")
    ordinary = _calls(
        adapter,
        "_execute_continuation_call_live(call_model=call_model, ctx=ctx, acc=acc, llm_messages=llm_messages, batch=batch, call=call)",
    )
    expected_native = {(FINISHER, id(native.func))} if native is not None else set()
    expected_native.update((BATCH, id(node.func)) for node in ordinary)
    _require(
        failures,
        len(ordinary) == 1 and _references(units, {"_execute_continuation_call_live"}) == expected_native,
        "continuation attachment native reference inventory changed",
    )
    if native is not None:
        receipt["native_uses"].add((FINISHER, id(native.func)))
    caller = selected[ATTACHMENT_DRIVER, "apply_current_attachment_runtime"]
    finishing = _one_call(
        failures,
        receipt,
        ATTACHMENT_DRIVER,
        caller,
        "finish_current_attachment_result(service=service, authority=authority, selected=selected, inventory=inventory, required_work=work, ctx=ctx, call_model=call_model, restored=runtime.graph)",
        "continuation attachment exact finisher caller changed",
    )
    _require(
        failures,
        _references(units, {"finish_current_attachment_result"})
        == ({(ATTACHMENT_DRIVER, id(finishing.func))} if finishing is not None else set()),
        "continuation attachment finisher reference inventory changed",
    )
    _one_call(
        failures,
        receipt,
        FINISHER,
        finisher,
        "_result(current.retained, prepared, inventory.names)",
        "continuation attachment actual shared pre-state readmission changed",
    )
    _one_call(
        failures,
        receipt,
        FINISHER,
        finisher,
        "replace(current, prepared=prepared, result=result)",
        "continuation attachment actual checkpoint readmission changed",
    )
    _one_call(
        failures,
        receipt,
        FINISHER,
        finisher,
        "read_attachment_result(authority=authority, selected=selected, inventory=inventory, required_work=required_work)",
        "continuation attachment current checkpoint read changed",
    )
    _one_call(
        failures,
        receipt,
        FINISHER,
        finisher,
        "_registered_read(required_work, ContinuationRuntimeSource, lambda: authority.read_continuation_runtime(inventory.running, selected=selected, inventory=inventory))",
        "continuation attachment registered current SQL read changed",
    )
    for path, binding, module, member in (
        (BATCH, "tool_result_message", _module(MESSAGE_BUILDERS), "tool_result_message"),
        (FINISHER, "_execute_continuation_call_live", _module(BATCH), "_execute_continuation_call_live"),
        (FINISHER, "replace", "dataclasses", "replace"),
    ):
        _require(
            failures, receiver.imported(index[path].tree, binding, module, member), "continuation attachment supplier changed " + binding
        )
        receipt["supplier_bindings"].add(_module(path) + "." + binding)
        receipt["origins"].add(module + "." + member)
    producer = _module(PRODUCERS) + ".CurrentContinuationMessageProducers"
    receipt["origins"].add(producer)
    receipt["class_origins"].add(producer)
    receipt["receiver_members"].setdefault(producer, set()).update(
        {
            "service",
            "authority",
            "selected",
            "inventory",
            "require_context",
            "_require_compose_family",
            "_register",
            "register_tool_call_message",
            "register_schema_message",
            "register_attachment_message",
        }
    )
    receipt["origins"].update(
        {
            _module(CHECKPOINT) + ".AttachmentResultCheckpoint",
            "dataclasses.replace",
            "elspeth.web.coordination.composer_operation_authority.ComposerAsyncOperationAuthority.read_attachment_result",
            "elspeth.web.coordination.composer_operation_authority.ComposerAsyncOperationAuthority.read_continuation_runtime",
        }
    )
    _attachment_projection_transfers(units, failures, index, selected, receipt)
    return _finish(failures, receipt)


def _attachment_projection_transfers(units, failures, index, selected, receipt):
    """An original-DATA checker mutates only its detached batch projection.

    This closes the external native append edge. It supplies no current SQL
    admission, audit-finisher or provider authority. Imports and complete bodies
    remain exact trusted contracts; the independent checks below preserve the
    actual copy, original receipt joins and complete successor comparisons.
    """
    path = ATTACHMENT_APPLICATION
    checker = selected[path, "require_attachment_projection"]
    historical = selected[path, "admit_attachment_application"]
    finisher = selected[FINISHER, "finish_current_attachment_result"]

    def statement(source, detail):
        matches = [node for node in checker.body if _same(node, source)]
        _require(failures, len(matches) == 1, "continuation attachment projection " + detail)
        return matches[0] if len(matches) == 1 else None

    # The receiver is a fresh dataclass replacement. These two mutable members
    # must be copied before the real native outcome writer sees that receiver.
    copied = statement(
        """
        batch = replace(
            prepared.batch,
            tool_outcomes=list(prepared.batch.tool_outcomes),
            refused_resources=set(prepared.batch.refused_resources),
            state=nominal.updated_state,
            last_validation=nominal.validation,
            last_runtime_preflight=nominal.runtime_preflight or prepared.batch.last_runtime_preflight,
            all_cache_hits=False,
            turn_has_discovery=True,
        )
        """,
        "detached state/copy transfer changed",
    )
    if copied is not None:
        _require(
            failures,
            [(kind, node) for name, kind, node in receiver.lexical_binding_events(checker) if name == "batch"]
            == [("STORE_OR_DELETE", copied.targets[0])],
            "continuation attachment projection detached batch rebound",
        )
    for name, source in (
        ("old", "old = require_evidence_application_links(before, env_ref_names=env_ref_names)"),
        ("new", "new = require_evidence_application_links(after, env_ref_names=env_ref_names)"),
        (
            "prepared",
            "prepared = RestoredPreparedBatchData(old.selected.batch, old.selected.accumulator, old.selected.calls, old.selected_audit, old.selected_dialect)",
        ),
        ("call", "call = prepared.batch.current_call"),
        ("result", "result = _result(retained_result, prepared, env_ref_names)"),
        ("admitted", "admitted = admit_discovery_result(call.tool_name, result)"),
        ("nominal", "nominal = admitted.to_tool_result()"),
        ("audit", "audit = read_audit"),
        ("authoring", "authoring = old.selected.batch.private_authorings_by_call[call.tool_call.id]"),
        ("recorder", "recorder = replace(old.recorder, invocations=(*old.recorder.invocations, invocation))"),
        (
            "expected_graph",
            "expected_graph = replace(old, selected=RestoredBatchData(batch, prepared.accumulator, prepared.calls), recorder=recorder, selected_audit=None, selected_dialect=None)",
        ),
        ("expected_json", "expected_json = encode_application_graph(expected_graph, env_ref_names=env_ref_names)"),
    ):
        transferred = statement(source, "actual selected identity assignment changed " + name)
        if transferred is not None:
            _require(
                failures,
                [(kind, node) for bound, kind, node in receiver.lexical_binding_events(checker) if bound == name]
                == [("STORE_OR_DELETE", transferred.targets[0])],
                "continuation attachment projection selected identity rebound " + name,
            )
    for expression, detail in (
        (
            "RestoredPreparedBatchData(old.selected.batch, old.selected.accumulator, old.selected.calls, old.selected_audit, old.selected_dialect)",
            "complete original prepared constructor changed",
        ),
        ("require_attachment_read_audit(prepared, read_audit, names=env_ref_names)", "original prepared/read audit changed"),
        ("_result(retained_result, prepared, env_ref_names)", "original retained result/pre-state changed"),
        ("admit_discovery_result(call.tool_name, result)", "original discovery result changed"),
        (
            "require_receipt_result(receipt, selected_call=call.tool_call, pre_state=call.pre_state, result=nominal, env_ref_names=env_ref_names)",
            "original receipt/call/pre-state changed",
        ),
        (
            "replace(old, selected=RestoredBatchData(batch, prepared.accumulator, prepared.calls), recorder=recorder, selected_audit=None, selected_dialect=None)",
            "complete successor identity graph changed",
        ),
        ("replace(invocation, private_authoring=authoring)", "original private authoring projection changed"),
        ("replace(old.recorder, invocations=(*old.recorder.invocations, invocation))", "original recorder projection changed"),
    ):
        call = _one_call(failures, receipt, path, checker, expression, "continuation attachment projection " + detail)
        if expression.startswith("RestoredPreparedBatchData(") and call is not None:
            receipt["data_constructor_calls"].add((path, id(call)))
            receipt["data_constructor_uses"].add((path, id(call.func)))
    wrapper = _one_call(
        failures,
        receipt,
        path,
        checker,
        "RestoredBatchData(batch, prepared.accumulator, prepared.calls)",
        "continuation attachment projection original successor constructor changed",
        key="data_constructor_calls",
    )
    if wrapper is not None:
        receipt["data_constructor_uses"].add((path, id(wrapper.func)))
    native = _one_call(
        failures,
        receipt,
        path,
        checker,
        "_append_continuation_tool_outcome(batch, call, response=nominal, error_class=None, error_category=None, error_message=None, post_version=nominal.updated_state.version)",
        "continuation attachment projection actual native append changed",
        key="native_calls",
    )
    if native is not None:
        receipt["native_uses"].add((path, id(native.func)))
    forwarding = _find(index[BATCH].tree, "_execute_continuation_call_live._append_tool_outcome")
    ordinary = (
        _calls(
            forwarding,
            "_append_continuation_tool_outcome(_batch, _call, response=response, error_class=error_class, error_category=error_category, error_message=error_message, post_version=post_version)",
        )
        if forwarding is not None
        else []
    )
    expected = {(BATCH, id(node.func)) for node in ordinary}
    if native is not None:
        expected.add((path, id(native.func)))
    _require(
        failures,
        len(ordinary) == 1 and _references(units, {"_append_continuation_tool_outcome"}) == expected,
        "continuation attachment projection native append reference inventory changed",
    )
    successor_statements = []
    for source in (
        "stopped = batch.stop_issued_batch or prepared.accumulator.authority_revocation is not None",
        "batch.next_tool_index += 1",
        "batch.current_call = None",
        "batch.stop_issued_batch = stopped",
        'batch.phase = "dispatch_ready" if stopped or batch.next_tool_index == len(prepared.calls) else "call_ready"',
    ):
        transferred = statement(source, "actual successor state transfer changed")
        if transferred is not None:
            successor_statements.append(transferred)
    expected_stores = {"next_tool_index", "current_call", "stop_issued_batch", "phase"}
    stores = [
        node
        for node in ast.walk(checker)
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, (ast.Store, ast.Del))
        and isinstance(node.value, ast.Name)
        and node.value.id == "batch"
    ]
    _require(
        failures,
        len(stores) == len(expected_stores) and {node.attr for node in stores} == expected_stores,
        "continuation attachment projection actual successor store inventory changed",
    )
    allowed_batch_scopes = successor_statements + [node for node in (native, wrapper) if node is not None]
    expected_batch_uses = {
        id(node)
        for scope in allowed_batch_scopes
        for node in ast.walk(scope)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == "batch"
    }
    _require(
        failures,
        {id(node) for node in ast.walk(checker) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == "batch"}
        == expected_batch_uses,
        "continuation attachment projection detached batch receiver escaped",
    )
    for source, detail in (
        (
            """if encode_restored_prepared_batch_data(prepared, env_ref_names=env_ref_names) != encode_restored_prepared_batch_data(prepared_source, env_ref_names=env_ref_names):
                raise AuditIntegrityError("attachment projection changed its actual selected prepared/read source")""",
            "complete original prepared/source comparison changed",
        ),
        (
            """if decode_continuation_response(response).tool_batch.calls != prepared.calls:
                raise AuditIntegrityError("attachment projection changed its actual complete provider call cohort")""",
            "original complete provider call cohort changed",
        ),
        (
            """if (receipt.effect_key != effect_key or invocation.status is not ComposerToolStatus.SUCCESS or invocation.cache_hit
                or invocation.tool_call_id != audit.tool_call_id or invocation.tool_name != audit.tool_name
                or invocation.arguments_canonical != audit.arguments_canonical or invocation.arguments_hash != audit.arguments_hash
                or invocation.authority_arguments_canonical != audit.authority_arguments_canonical
                or invocation.authority_arguments_hash != audit.authority_arguments_hash or invocation.actor != audit.actor
                or invocation.strict_sent is not audit.strict_sent or invocation.wire_conformant is not audit.wire_conformant
                or invocation.started_at != audit.started_at or invocation.latency_ms < audit.elapsed_active_ns // 1_000_000):
                raise AuditIntegrityError("attachment projection lost its original completed-read audit/timing")""",
            "original audit cohort/timing changed",
        ),
        (
            """if any(row.tool_call_id == call.tool_call.id for row in old.recorder.invocations):
                raise AuditIntegrityError("attachment projection cannot finish an already recorded call")""",
            "already recorded original call guard changed",
        ),
        (
            """if invocation.private_authoring != authoring:
                raise AuditIntegrityError("attachment projection changed its original private authoring")""",
            "original private authoring guard changed",
        ),
        (
            """if encode_application_graph(new, env_ref_names=env_ref_names) != expected_json:
                raise AuditIntegrityError("attachment projection changed its complete batch/recorder/history identity graph")""",
            "complete successor graph comparison changed",
        ),
        (
            """if encode_turn_evidence(after, env_ref_names=env_ref_names) != encode_turn_evidence(expected, env_ref_names=env_ref_names):
                raise AuditIntegrityError("attachment projection changed original evidence/transcript or actual page registration")""",
            "complete evidence/transcript comparison changed",
        ),
    ):
        statement(source, detail)
    actual_callers = set()
    for owner_path, owner, expression in (
        (
            FINISHER,
            finisher,
            "require_attachment_projection(before=before, after=after, prepared_source=current.prepared, retained_result=current.retained, read_audit=current.read_audit, effect_key=current.effect_key, receipt=receipt, response=response, env_ref_names=inventory.names)",
        ),
        (
            path,
            historical,
            "require_attachment_projection(before=before, after=after, prepared_source=prepared, retained_result=retained, read_audit=read_audit, effect_key=value.effect_key, receipt=receipt, response=response, env_ref_names=env_ref_names)",
        ),
    ):
        call = _one_call(
            failures, receipt, owner_path, owner, expression, "continuation attachment projection actual checker caller changed"
        )
        if call is not None:
            actual_callers.add((owner_path, id(call.func)))
    _require(
        failures,
        _references(units, {"require_attachment_projection"}) == actual_callers,
        "continuation attachment projection checker reference inventory changed",
    )
    local = [
        node
        for node in finisher.body
        if _same(node, "from elspeth.web.composer.continuation_attachment_application import require_attachment_projection")
    ]
    _require(
        failures,
        len(local) == 1
        and [(kind, node) for name, kind, node in receiver.lexical_binding_events(finisher) if name == "require_attachment_projection"]
        == [("IMPORT", local[0])],
        "continuation attachment projection exact local checker supplier changed",
    )
    for binding, module, member in (
        ("replace", "dataclasses", "replace"),
        ("_append_continuation_tool_outcome", _module(BATCH), "_append_continuation_tool_outcome"),
        ("RestoredPreparedBatchData", "elspeth.web.composer.continuation_batch_data", "RestoredPreparedBatchData"),
        ("RestoredBatchData", "elspeth.web.composer.continuation_batch_data", "RestoredBatchData"),
        ("require_receipt_result", "elspeth.web.composer.continuation_tool_receipts", "require_receipt_result"),
        ("_result", "elspeth.web.sessions.continuation_attachment_result", "_result"),
        ("require_attachment_read_audit", "elspeth.web.sessions.continuation_attachment_result", "require_attachment_read_audit"),
        ("encode_application_graph", "elspeth.web.composer.continuation_application_graph", "encode_application_graph"),
        ("encode_turn_evidence", "elspeth.web.composer.continuation_evidence", "encode_turn_evidence"),
    ):
        _require(
            failures,
            receiver.imported(index[path].tree, binding, module, member),
            "continuation attachment projection canonical supplier changed " + binding,
        )
    # This metadata protects the reviewed suppliers from replacement. It is not
    # a permission for arbitrary reads/calls, constructors or current SQL effects.
    for node in ast.walk(index[path].tree):
        if isinstance(node, ast.ImportFrom) and node.module != "__future__":
            for alias in node.names:
                receipt["origins"].add(node.module + "." + alias.name)
                receipt["supplier_bindings"].add(_module(path) + "." + (alias.asname or alias.name))
    receipt["supplier_bindings"].add(_module(FINISHER) + ".require_attachment_projection")


def initialization_local_transfers(units):
    """Exact nested pre-title skill producer, completion, task and caller."""
    paths = (PRETITLE, ROUTE_HELPERS, SERVICE, SESSIONS)
    failures, index, selected, receipt = _select(units, paths, "continuation initialization")
    if failures:
        return failures, {}
    preparation = selected[PRETITLE, "_prepare_skill_history"]
    owned = _find(index[PRETITLE].tree, "_prepare_skill_history.owned")
    _require(failures, isinstance(owned, ast.AsyncFunctionDef), "continuation initialization native producer changed")
    if not isinstance(owned, ast.AsyncFunctionDef):
        return failures, {}
    reserve = _one_call(
        failures,
        receipt,
        PRETITLE,
        owned,
        "work.coordinator.reserve(RequiredWorkSource.OWNED_TURN_SETUP_PRODUCER, transition_ordinal=work.transition_ordinal, semantic_ordinal=work.semantic_ordinal)",
        "continuation initialization actual coordinator/source/ordinal transfer changed",
        key="reserve_calls",
    )
    _require(
        failures,
        sum(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "reserve"
            for node in ast.walk(preparation)
        )
        == 1,
        "continuation initialization reserve site inventory changed",
    )
    for expression in (
        "owned()",
        'asyncio.create_task(coroutine, name="continuation-pretitle-skill-history")',
        "coroutine.close()",
        "_join_freeform_owned_task(task, cancellation_observations=cancellations)",
        "service._maybe_upsert_skill_markdown_history()",
        "producer.complete_owned(error)",
        "producer.complete_owned()",
    ):
        _one_call(
            failures,
            receipt,
            PRETITLE,
            preparation,
            expression,
            "continuation initialization task/completion transfer changed " + expression,
        )
    outer = selected[PRETITLE, "prepare_pretitle_bootstrap"]
    call = _one_call(
        failures,
        receipt,
        PRETITLE,
        outer,
        "_prepare_skill_history(service, required_work)",
        "continuation initialization actual checked caller changed",
    )
    _require(
        failures,
        _references(units, {"_prepare_skill_history"}) == ({(PRETITLE, id(call.func))} if call is not None else set()),
        "continuation initialization producer reference inventory changed",
    )
    _require(
        failures,
        receiver.imported(
            index[PRETITLE].tree, "_join_freeform_owned_task", "elspeth.web.sessions.routes._helpers", "_join_freeform_owned_task"
        ),
        "continuation initialization actual join supplier changed",
    )
    if reserve is not None:
        receipt["uses"].add((PRETITLE, id(reserve.func)))
    receipt["selected_definitions"].add((PRETITLE, id(owned)))
    receipt["origins"].update(
        {
            "asyncio.create_task",
            "asyncio.current_task",
            "asyncio.shield",
            "elspeth.web.required_work.RequiredWorkSource",
            "elspeth.web.required_work.RequiredWorkSource.OWNED_TURN_SETUP_PRODUCER",
            "elspeth.web.required_work.RequiredWorkCoordinator.reserve",
            "elspeth.web.required_work.RequiredWorkTicket.complete_owned",
            "elspeth.web.required_work.RequiredWorkTicket.complete_without_submission",
            "elspeth.web.required_work.make_required_work_key",
        }
    )
    receipt["supplier_bindings"].update(
        _module(PRETITLE) + "." + name
        for name in ("asyncio", "_join_freeform_owned_task", "RequiredWorkSource", "RequiredWorkBinding", "ComposerServiceImpl")
    )
    receipt["receiver_members"].update(
        {
            "elspeth.web.required_work.RequiredWorkBinding": {
                "coordinator",
                "running",
                "transition_ordinal",
                "semantic_ordinal",
                "validate_context",
            },
            "elspeth.web.required_work.RequiredWorkCoordinator": {"authority", "reserve"},
            "elspeth.web.required_work.RequiredWorkTicket": {"complete_owned", "complete_without_submission"},
            "elspeth.web.composer.service.ComposerServiceImpl": {"_maybe_upsert_skill_markdown_history"},
            "elspeth.web.sessions.service.SessionServiceImpl": {"upsert_skill_markdown_history"},
        }
    )
    return _finish(failures, receipt)


def continuation_local_transfers(units):
    """Join the finite receipts without authorizing any new evaluator rule."""
    failures, joined = [], _empty()
    for check in (iteration_local_transfers, attachment_local_transfers, initialization_local_transfers):
        current_failures, receipt = check(units)
        failures.extend(current_failures)
        if current_failures:
            continue
        for key in (*SITE_KEYS, "origins", "function_origins", "class_origins", "supplier_bindings"):
            joined[key].update(receipt[key])
        for owner, members in receipt["receiver_members"].items():
            joined["receiver_members"].setdefault(owner, set()).update(members)
    return _finish(failures, joined)
