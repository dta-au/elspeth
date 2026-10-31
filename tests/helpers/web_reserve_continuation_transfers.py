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
from collections import Counter

from scripts.cicd.composer_advisor_binding_templates import CLOSED_SOURCE_RECORDS, OWNER_TEMPLATES

from elspeth_lints.core.ast_dump import stable_ast_dump
from tests.helpers import web_reserve_composer_receiver as receiver

CARRIERS = "src/elspeth/web/composer/_compose_loop_carriers.py"
RUNTIME = "src/elspeth/web/composer/continuation_runtime.py"
INITIALIZATION = "src/elspeth/web/composer/continuation_composition_initialization.py"
INITIAL_DATA = "src/elspeth/web/composer/continuation_composition_initialization_data.py"
INITIAL_PUBLICATION = "src/elspeth/web/sessions/continuation_composition_initialization.py"
FIRST_RUNTIME = "src/elspeth/web/composer/continuation_first_runtime.py"
EVIDENCE_GRAPH = "src/elspeth/web/composer/continuation_evidence_graph.py"
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
        "_restore_live": "5343756b69963cc0e66e6bf3b7888f111e88f92051a28f0019329267334e9b5f",
        "restore_continuation_runtime": "c5681885fa715619243432c44514c4cfe616a9ff30b736f9193085861b9be49d",
        "_capture_owners": "d24595a9e9de77168990ac926e6be7ea47b8642fc2d1590336c08eeaeadcec9a",
        "capture_continuation_runtime_owners": "d3db499cd6f768b6e6051f54f52c0b8f9ffea3f124886ec0cfd7aa77236646fd",
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
        "capture_initial_composition": "152896f03612495c2e93d2f3bf0ff82089c33ce70959ff5d3ad3437e8160b324",
    },
    INITIAL_PUBLICATION: {
        "publish_composition_initialization_on_connection": "bb4c0285311eea1caf7ca4cb4a04e691fb853afbdd392db9785bdc2bd6d8c534",
    },
    FIRST_RUNTIME: {
        "_require_same_sources": "34e3815e1d152e012fa83bdc34f1472635e1196619904faa12854c96c8d42157",
        "_read_first_sources": "34b9028427bea9a8f3f38f100819d3f2cdbfa5b7556eb3b94bafb2e1a3264a00",
        "_restore_first_graph": "d36f7daded16ea8fd40e77357ea0ca0f3c249647e1b3e5102114c072eb2360c9",
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
        "CompletedAttachmentProjection": "d5c40d584461ec5ff291f603511eca19dd28deff66440c4bb3f2e6eda0aab9e7",
        "finish_current_attachment_result": "8ae30f4fb7eb38a47ef2d672ad61227683a1a33841ce5675072ba06fcb3883af",
    },
    ATTACHMENT_DRIVER: {
        "read_current_applied_attachment": "a963a789b0a292a7cd50da87cba13ef8827c22335ed31da59e36f293bdc32539",
        "apply_current_attachment_runtime": "8dc2225149366e70724e6b4836a62d72364d2e0156502a9bded40d3689d9c109",
    },
    ATTACHMENT_APPLICATION: {
        "_append_message": "4f7326633989a70bc181468ba39819ff3baa893a6a0ee055b6dea137638c88af",
        "_register_page": "30ee6cc2d1c3331ffcfa36a613976411bddd36dc734635d2938e5b8e0232278d",
        "require_attachment_projection": "a7711a821bfa8ffafe06dcdd0e7cb1215a6f5f0c1eb189381c80b302daf305b6",
        "admit_attachment_application": "21342f44a3bd6d8ba2c5de5ac5b9c8e1a5ded701901b2d166d1aac5fdf342df9",
    },
    ATTACHMENT_RESULT: {
        "_owners": "58dd9cfd7480f34526f12c922dbc73fb98c112fe5bfba91f7f05e6015ae7f951",
        "_project": "22e12f820063154280508a8ef13e2546951a6d7e4fe064704bd76751a9f34ecd",
        "read_attachment_result": "7b142092b8c9518fa1ad65b225fcc3770a64b7ede2ed648ac4858167aa8b527c",
        "run_attachment_result": "f974b33e474c89cf2110e39905e206d3c9fb795b91797c2e7871a933553ba3f4",
    },
    CHECKPOINT: {
        "_history_data": "fad2b7191efa624f094eacf59435b8d6e8525afd1870a7eb55e13d2e45b1f1f6",
        "_effect_data": "93bd3df893c8e4c9324dd78d87bcf6861afaba6bcfc6cd7f554fa26e3d3e5156",
        "verify_attachment_result_data_on_connection": "2e3ead03f36f7b9b3da11e0a136cd4c872760a1240ec5ba8c08c7317a44739a7",
        "AttachmentResultCheckpoint": "7f1b631b894339da19f59b2aef39fe04e037eff4df3ca65eeac465594dd1dd37",
        "_selected": "47648159c619e836e4c7f786459a2b1292df91ea04096d4874b293cb2cb7a858",
        "_chunk": "a43b2ad39cc3c5bd1b3db38cc474443f6258839d02aebeac34da8db7cdb7f195",
        "_input": "b8e92a5f469056a59c5893f073e56af4c29c68f219599d6a32f20b2f8a5cf459",
        "_history": "7677c6cc44f86c8a77724aedacd291195fe37bfd92c2a8a5c7fee9047d39f251",
        "_effect": "0e3387c232320f817f926338334dbb122e19cbbeb58166b68446e66b72e0fb67",
        "_result": "f4701f3e0dfb45cfe624c4437622169c6de34407bf50a5faf4b67e2fa410c1cf",
        "prepare_attachment_result_on_connection": "63e6ee5c9dcf5455a19c8cf43af7e54a3f0be65ea1055d6a3f37e2599b1a9d64",
        "require_attachment_read_audit": "a6a50db8e7b37b2faa82589c20311a26fa1f3253a74ad9de33e8fdbe66f24d7f",
        "_retained_history": "3e28175b788978059d1ba8a23a61d76d7d240e06ba9023e950c01f724a251f1c",
        "read_attachment_result_on_connection": "a18c21fcfede284f6c62546e2617791adacc496af548260d70fcb83b38507d5c",
        "verify_attachment_result_source_on_connection": "b4a9c88ac3bf9b4689574223cb09f940557dbf6f8ae21d35ce363a4ffc8e4a06",
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
        "CurrentContinuationMessageProducers.__post_init__": "18dc66996d9e8fa4e7b3eb0dc174967f1b3d42706dacf96a9fb1cc4f1f5ea9cc",
        "CurrentContinuationMessageProducers._require_compose_family": "0cdad0b6282fee521e0df6a9973af0dddd73973b252df43a92dfff99c5b27dbd",
        "CurrentContinuationMessageProducers.require_context": "9ac16b32a1984ed5e362b33070e0b04ce626b5d1ba47a1c94bf53e3b44958842",
        "CurrentContinuationMessageProducers._register": "5438610b07c13b4d1e9dfffab66f3dc2fcf0a9579baa26680fb2a6fe93a7ae53",
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
        "validate_native_thinking_blocks": "f7ffafb5bdffcafb56366af65aaba0d0bd4c1df5c461d53a09dc0693032094b4",
        "scan_native_thinking_blocks": "bd0c4b7a8dc6b36c95477bdc84fc6d70088bae5853e5bfd0419fbaf0cc1e57af",
        "parse_native_thinking_blocks": "9e6e1539fa657b3492b0b938421945b1b2661ae7c0ab780c7902b4c301937461",
        "native_thinking_blocks": "51c3a0f2abbdfecd907fef1e8a7ceda71f21c2ac48e7edcd7aee521a71606df9",
        "attach_provider_thinking": "bbb1a2535e4310a73fdb20d1ca349463ce734f4c3d21f2a4df5a57f1868071a1",
    },
}

_NAMESPACES = {
    CARRIERS: "972983b4c8f258cecbb3c8270caf8def215bdce58ab396a5c454187e47d34619",
    RUNTIME: "efc197e39b4c22e03d884a0c75393d1ff5ef278e1906e787c0e47997ad13488a",
    INITIALIZATION: "6536dc9ab162bc576c9fba059b0a80a85a2f84f58108ed871260b8cd4e32a95e",
    INITIAL_DATA: "1136b7ef4694be56e5ebb899195daf9ca27ff58d751f3e30043a3410f51bba53",
    INITIAL_PUBLICATION: "25f499509f5972d36111fd648ba5abae25a3b0c00d0063cd7e4c19e88006f49c",
    FIRST_RUNTIME: "b5b4599f3dafc011662286fb0131a69ebaeed31b536677099ce655902a8900e2",
    FREEZE: "327e64409e2caca0c9ddf73fb3122c346fef89a74e8bc843c659c72c9a1b1a0e",
    CACHES: "625b97608140682adbc8a83fd0ba44c4f08f6e9facde4eb943b6a439fe87517b",
    FINISHER: "8f6740a0589e7e47c587306ce8328be09e3a4c005924d2655f43f17aba1fa9f2",
    ATTACHMENT_DRIVER: "66a998d678c9d73799456a4078819c0b68b0e358b1b534bb221892efe0ffa872",
    ATTACHMENT_RESULT: "90541fcf5407eb03b9555ca08b34a677b300f5a2d832a84c93d6cf6ddbf8e6e2",
    ATTACHMENT_APPLICATION: "835a0b0ad96d624bd472a5c3a3aca0a4284d3ecb85b8aa8cce477fb5b5a6b37e",
    CHECKPOINT: "359ceb9f7617a7dc2609a1c2860bfa98c0b2b3f012eeea27c541f74f5a66675f",
    PRETITLE: "fc7537a2a015475b5359d96cee3a1f5f4d2164c9adbd8586e6402ba9e448e37f",
    ROUTE_HELPERS: "25cb1d5770cc13b85004814b543d4293a83bcbdc8a2215d1aab06b89a8f36bde",
    SERVICE: "fb85da32a435a7df3fa0c85b73d0dd6e66c4d99e83be2388e851da19754b18ae",
    SESSIONS: "b8270e8e4772ff337ac45cffec06d56a0fb8e4893ee8eb8f26f1f4a36d9e05db",
    BATCH: "4dd922ec5fa17ffff0b10240110da8ebfd827845d7247b453baebeb57d2924f5",
    MESSAGE_BUILDERS: "b14fcdde6706a5f9715d88c51c9fccbbd5951d5ce69aff8018f719e34db7b030",
    FAMILY_BUILDERS: "eb94935044679344dd45c6b89ed9ac00fcdafed4085eddc273dcff4dd107aaba",
    PRODUCERS: "b81b5a21eb06dc2822eb997fb7ef41d1c5b6f8a2011ef94ef46f737612aebe27",
    ARGUMENT_MESSAGES: "1ebabde9cfbef7c13468cdcdc3db2b9aa8bd440ead25da4e38859324e7977e4d",
    DISCOVERY_CACHE: "bdea5fd438a967c112c6bdb9fac33d2392d1f1ec6d146280ef1b96bd5daeb24c",
    PROVIDER_THINKING: "ac56041030c04740391bc555cda5d78ef1990d84cbf08913d2590f3c945893cd",
}
RECORD_PATHS = {
    "graph": EVIDENCE_GRAPH,
    "families": "src/elspeth/web/composer/continuation_message_families.py",
    "producers": PRODUCERS,
}
RECORD_CALLERS = {
    "src/elspeth/web/composer/continuation_live_p4.py": {"ContinuationLiveP4.prepare": "p4.ContinuationLiveP4.prepare"},
    "src/elspeth/web/composer/continuation_live_p4_application.py": {
        "finish_live_p4_application": "p4_application.finish_live_p4_application"
    },
    "src/elspeth/web/composer/continuation_live_phases.py": {
        "ContinuationLivePhases._initialize_batch": "phases.ContinuationLivePhases._initialize_batch",
        "ContinuationLivePhases._execute_one_owned": "phases.ContinuationLivePhases._execute_one_owned",
    },
    PRODUCERS: {
        "CurrentContinuationMessageProducers._bind_feedback_outcome": "producers.CurrentContinuationMessageProducers._bind_feedback_outcome"
    },
}
SOURCE_RECORD_FIELDS = {
    "graph.RestoredTurnEvidence": (
        "evidence",
        "messages",
        "recorder",
        "drained",
        "batch",
        "applications",
        "selected_audit",
        "selected_dialect",
    ),
    "families._ToolResultFamilySource": ("recipe", "call", "result", "message"),
    "families._ToolFeedbackFamilySource": ("recipe", "call", "outcome", "invocation", "message"),
    "families._RestoredFamilyCohort": ("messages", "feedback", "sources"),
    "producers._FeedbackMessageProvenance": (
        "message",
        "recipe",
        "keys",
        "role",
        "call_id",
        "body",
        "selector",
        "call",
        "invocation",
        "authoring",
        "outcome",
    ),
    "producers.ContinuationToolMessagePublication": ("producers", "completion", "batch", "call", "recorder", "messages", "catalog"),
    "producers.CurrentMessageProduction": ("producers", "messages"),
}
REQUIRED_PATHS = tuple(dict.fromkeys((*_NAMESPACES, *RECORD_PATHS.values(), *RECORD_CALLERS)))
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


# These six caller bodies were already selected by the retained contract.
# Exact AST grammar preserves their complete ownership/order checks; this map
# grants no new selected definition, callback, native call or SQL permission.
_RUNTIME_INITIALIZATION_BODIES = {
    (RUNTIME, "_restore_live"): """def _restore_live(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner,
    source: ContinuationRuntimeSource,
    graph: RestoredTurnEvidence,
    materialization: CurrentContinuationMessageProducers,
    monotonic_before_read: float,
    progress: ComposerProgressSink | None,
    finish_read: ContinuationFinishRead | None = None,
) -> RestoredContinuationRuntime:
    from elspeth.web.composer.service import _chat_ingress_inputs_for_compose

    _index, frame = _compose(selected)
    names = inventory.names
    original = source.original
    initial = graph.evidence.effects.initial_state
    if frame.state.codec != "composer.state.v1":
        raise AuditIntegrityError("runtime current state changed its actual closed codec")
    selected_state = runtime_chunk(selected, frame.state).canonical
    if graph.batch is not None:
        state = graph.batch.batch.state
    else:
        from elspeth.web.composer.continuation_evidence import state_from_evidence_json

        state = state_from_evidence_json(selected_state)
    if (
        canonical_json(state.to_dict()) != selected_state
        or runtime_chunk(selected, frame.initial_state).canonical != original.snapshot.initial_state_json
        or canonical_json(initial.to_dict()) != original.snapshot.initial_state_json
        or initial.version != frame.initial_version
        or frame.current_state_id != graph.evidence.effects.latest_committed_state_id
    ):
        raise AuditIntegrityError("runtime changed original base or actual shared state/frontier")
    require_no_credential_material_in_state(state, surface="current continuation runtime state", env_ref_names=names)
    if runtime_chunk(selected, frame.audit_buffer) != encode_recorder_snapshot(
        graph.recorder.snapshot_for_continuation(drained=graph.drained)
    ):
        raise AuditIntegrityError("runtime recorder differs from its one shared evidence graph")
    if runtime_chunk(selected, frame.transcript) != encode_message_shelf(source.evidence.transcript):
        raise AuditIntegrityError("runtime transcript differs from its complete selected shelf")
    expected_messages = canonical_json(
        {"messages_json": source.evidence.transcript.messages_json, "current_indexes": source.evidence.transcript.current_indexes}
    )
    if runtime_chunk(selected, frame.messages).canonical != expected_messages:
        raise AuditIntegrityError("runtime messages differ from the shared selected transcript")
    snapshot, catalog = service._policy_context.build(original.snapshot.actor_user_id)
    current = catalog.validate_composition_state(state)
    if canonical_json(current.authored_state.to_dict()) != selected_state:
        raise AuditIntegrityError("current policy requires authored-state reinspection before runtime restore")
    composition_scope_for_operation(
        state,
        session_id=selected.stack.logical.session_id,
        session_operation_context=inventory.running.session_operation_context,
        session_operation_authority=service._require_sessions_service().session_operation_authority,
    )
    tools = composer_loop_tool_definitions(service._planner_dialect)
    if (
        frame.tools is None
        or frame.tools.codec != "composer.tools.v1"
        or runtime_chunk(selected, frame.tools).canonical != canonical_json(tools)
    ):
        raise AuditIntegrityError("runtime tool contract changed and requires current reacquisition")
    if graph.selected_dialect is not None and graph.selected_dialect is not service._planner_dialect:
        raise AuditIntegrityError("runtime prepared wire dialect changed and requires current reacquisition")
    values = iteration_values(source.iteration, env_ref_names=names)
    cache_history = decode_continuation_caches(runtime_chunk(selected, frame.caches), env_ref_names=names)
    if cache_history.session_id != selected.stack.logical.session_id:
        raise AuditIntegrityError("runtime cache history belongs to another session")
    caches = restore_continuation_caches(cache_history, env_ref_names=names)
    attachments = source.attachments
    if (frame.persisted_assistant_message_id is None) != (frame.persisted_assistant_content is None):
        raise AuditIntegrityError("runtime lost its paired persisted assistant identity/content")
    if any(type(index) is not int or not 0 <= index < len(graph.messages) for index in frame.pending_advisor_elision_indices):
        raise AuditIntegrityError("runtime pending Advisor elision lost its exact message positions")
    ceiling = selected.stack.mutation_ceiling.materialize()
    ceiling.validate_context(inventory.running.session_operation_context)
    live = _ComposeIterationState(
        state=state,
        initial_version=frame.initial_version,
        llm_messages=graph.messages,
        tools=tools,
        recorder=graph.recorder,
        composition_turns_used=frame.composition_slice,
        discovery_turns_used=frame.discovery_slice,
        mutation_success_seen=frame.mutation_success_seen,
        discovery_cache=caches.discovery,
        last_validation=current.validation,
        runtime_preflight_cache=caches.preflight,
        last_runtime_preflight=caches.last_preflight,
        anti_anchor=AntiAnchorTracker.for_continuation(source.iteration.anti_anchor),
        advisor_calls_used=frame.advisor_calls,
        repair_turns_used=frame.repair_turns,
        advisor_checkpoint_passes_used=frame.advisor_checkpoint_passes,
        advisor_review_state=values.advisor_review,
        persisted_assistant_message_id=frame.persisted_assistant_message_id,
        persisted_assistant_content=frame.persisted_assistant_content,
        persisted_tool_call_turn=frame.persisted_tool_call_turn,
        failed_turn=values.failed_turn,
        current_state_id=frame.current_state_id,
        advisor_repair_context_introduced=frame.advisor_repair_context_introduced,
        pending_advisor_elision_indices=list(frame.pending_advisor_elision_indices),
        turn_evidence=graph.evidence,
        continuation_cache_history=caches.history,
    )
    bindings = _ComposeIterationBindings(
        message=original.message,
        session_id=selected.stack.logical.session_id,
        user_id=original.snapshot.actor_user_id,
        user_message_id=selected.stack.logical.original_user_message_id,
        deadline=monotonic_before_read + source.timing.hard_remaining_seconds,
        progress=progress,
        plugin_snapshot=snapshot,
        policy_catalog=catalog,
        mutation_ceiling=ceiling,
        session_operation_context=inventory.running.session_operation_context,
        completion_gates=original.completion_gates,
        diagnostics=None,
        required_work=work,
        provider_owner=provider_owner,
        ingress=compartment_ingress_record(original.message, own_compartment_id=service._settings.compartment_id),
        chat_ingress_inputs=_chat_ingress_inputs_for_compose(
            original.message,
            list(original.history),
            user_message_id=selected.stack.logical.original_user_message_id,
            own_compartment_id=service._settings.compartment_id,
        ),
        actor=f"composer-web:user-{original.snapshot.actor_user_id}",
        session_scope=f"session:{selected.stack.logical.session_id}",
    )
    # A later read can only shorten admission; never use its newer remaining
    # observation to renew the monotonic anchor or original logical budgets.
    if finish_read is None:
        authority.read_continuation_runtime(inventory.running, selected=selected, inventory=inventory)
    else:
        finish_read.require_selection(selected, inventory)
        finish_read.read_runtime(authority)
    service._schema_disclosure.restore_session_for_continuation(
        source.iteration.disclosure,
        session_id=selected.stack.logical.session_id,
        catalog_hash=snapshot.snapshot_hash,
        policy_hash=snapshot.policy_hash,
    )
    return RestoredContinuationRuntime(live, bindings, graph, materialization, source, graph.drained, attachments, names)""",
    (RUNTIME, "restore_continuation_runtime"): """async def restore_continuation_runtime(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    required_work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner,
    progress: ComposerProgressSink | None = None,
    finish_read: ContinuationFinishRead | None = None,
) -> RestoredContinuationRuntime:
    from elspeth.web.composer.service import ComposerServiceImpl

    running = _work_owners(authority, inventory, required_work)
    if (
        type(service) is not ComposerServiceImpl
        or type(provider_owner) is not ProviderInvocationOwner
        or provider_owner.required_work is not required_work
        or provider_owner.service is not service._require_sessions_service()
    ):
        raise AuditIntegrityError("runtime lost actual current service/provider-work custody")
    monotonic_before_read = time.monotonic()
    if finish_read is not None:
        from elspeth.web.composer.continuation_finish_reads import ContinuationFinishRead

        if type(finish_read) is not ContinuationFinishRead:
            raise AuditIntegrityError("runtime restore received a foreign owed-finish facet")
        finish_read.require_selection(selected, inventory)
    source = await _registered_read(
        required_work,
        ContinuationRuntimeSource,
        lambda: (
            authority.read_continuation_runtime(running, selected=selected, inventory=inventory)
            if finish_read is None
            else finish_read.read_runtime(authority)
        ),
    )
    if type(source) is not ContinuationRuntimeSource:
        raise AuditIntegrityError("runtime source returned another nominal owner")
    production = await load_current_message_producers(
        service=service, authority=authority, selected=selected, inventory=inventory, required_work=required_work, finish_read=finish_read
    )
    graph = await restore_current_turn_evidence(
        service=service,
        authority=authority,
        selected=selected,
        inventory=inventory,
        required_work=required_work,
        materialization=production.producers,
    )
    result = await _registered_read(
        required_work,
        RestoredContinuationRuntime,
        lambda: _restore_live(
            service=service,
            authority=authority,
            selected=selected,
            inventory=inventory,
            work=required_work,
            provider_owner=provider_owner,
            source=source,
            graph=graph,
            materialization=production.producers,
            monotonic_before_read=monotonic_before_read,
            progress=progress,
            finish_read=finish_read,
        ),
    )
    if type(result) is not RestoredContinuationRuntime:
        raise AuditIntegrityError("runtime restore returned another nominal owner")
    return result""",
    (RUNTIME, "_capture_owners"): """def _capture_owners(
    runtime: RestoredContinuationRuntime, *, source: ContinuationRuntimeSource, inventory: CurrentContinuationEnvInventory
) -> CapturedContinuationRuntime:
    live, bindings = runtime.live, runtime.bindings
    selected = source.selected
    root = selected.stack
    index, frame = _compose(selected)
    if (
        bindings.session_operation_context is not inventory.running.session_operation_context
        or source.bootstrap != runtime.source.bootstrap
        or source.policy != runtime.source.policy
        or root.mutation_ceiling != runtime.source.selected.stack.mutation_ceiling
        or bindings.mutation_ceiling != root.mutation_ceiling.materialize()
        or bindings.session_id != root.logical.session_id
        or bindings.user_message_id != root.logical.original_user_message_id
        or bindings.user_id != source.bootstrap.actor_user_id
        or runtime.env_ref_names != inventory.names
    ):
        raise AuditIntegrityError("runtime capture changed original request/ceiling/current source")
    composition_delta = live.composition_turns_used - frame.composition_slice
    discovery_delta = live.discovery_turns_used - frame.discovery_slice
    if (
        composition_delta < 0
        or discovery_delta < 0
        or live.advisor_calls_used < frame.advisor_calls
        or live.repair_turns_used < frame.repair_turns
        or live.advisor_checkpoint_passes_used < frame.advisor_checkpoint_passes
        or (frame.mutation_success_seen and not live.mutation_success_seen)
    ):
        raise AuditIntegrityError("runtime capture cannot reset actual logical counters or mutation history")
    if live.initial_version != frame.initial_version or live.current_state_id != live.turn_evidence.effects.latest_committed_state_id:
        raise AuditIntegrityError("runtime capture changed its original base/current SQL frontier")
    if live.continuation_cache_history is None:
        raise AuditIntegrityError("runtime capture cannot replace complete retained cache history")
    names = inventory.names
    if source.admitted_evidence.snapshot is not source.evidence:
        raise AuditIntegrityError("runtime source lost its resolved original evidence cohort")
    current_recorder = decode_recorder_snapshot(source.admitted_evidence.recorder_chunk)
    actual_recorder = live.recorder.snapshot_for_continuation(drained=runtime.drained)
    if (
        len(actual_recorder.invocations) < len(current_recorder.invocations)
        or len(actual_recorder.llm_calls) < len(current_recorder.llm_calls)
        or len(actual_recorder.planner_attempts) < len(current_recorder.planner_attempts)
        or len(actual_recorder.withheld_replies) < len(current_recorder.withheld_replies)
        or len(actual_recorder.visible_tool_events) < len(current_recorder.visible_tool_events)
        or len(actual_recorder.private_candidates) < len(current_recorder.private_candidates)
        or len(actual_recorder.private_authorings) < len(current_recorder.private_authorings)
        or any(
            actual < retained
            for actual, retained in (
                (actual_recorder.drained.invocations, current_recorder.drained.invocations),
                (actual_recorder.drained.llm_calls, current_recorder.drained.llm_calls),
                (actual_recorder.drained.planner_attempts, current_recorder.drained.planner_attempts),
                (actual_recorder.drained.withheld_replies, current_recorder.drained.withheld_replies),
                (actual_recorder.drained.visible_tool_events, current_recorder.drained.visible_tool_events),
                (actual_recorder.drained.private_candidates, current_recorder.drained.private_candidates),
                (actual_recorder.drained.private_authorings, current_recorder.drained.private_authorings),
            )
        )
    ):
        raise AuditIntegrityError("runtime capture lost its complete current SQL recorder prefix/drains")
    current_prefix = replace(
        actual_recorder,
        invocations=actual_recorder.invocations[: len(current_recorder.invocations)],
        llm_calls=actual_recorder.llm_calls[: len(current_recorder.llm_calls)],
        planner_attempts=actual_recorder.planner_attempts[: len(current_recorder.planner_attempts)],
        withheld_replies=actual_recorder.withheld_replies[: len(current_recorder.withheld_replies)],
        visible_tool_events=actual_recorder.visible_tool_events[: len(current_recorder.visible_tool_events)],
        private_candidates=actual_recorder.private_candidates[: len(current_recorder.private_candidates)],
        private_authorings=actual_recorder.private_authorings[: len(current_recorder.private_authorings)],
        drained=current_recorder.drained,
    )
    if encode_recorder_snapshot(current_prefix) != encode_recorder_snapshot(current_recorder):
        raise AuditIntegrityError("runtime capture changed its complete current SQL recorder prefix")
    service = runtime.materialization.service
    current_snapshot, current_catalog = service._policy_context.build(source.bootstrap.actor_user_id)
    if (
        current_snapshot.snapshot_hash != bindings.plugin_snapshot.snapshot_hash
        or current_snapshot.policy_hash != bindings.plugin_snapshot.policy_hash
        or canonical_json(live.tools) != canonical_json(composer_loop_tool_definitions(service._planner_dialect))
        or canonical_json(current_catalog.validate_composition_state(live.state).authored_state.to_dict())
        != canonical_json(live.state.to_dict())
    ):
        raise AuditIntegrityError("runtime capture requires current policy/tools/state reacquisition")
    composition_scope_for_operation(
        live.state,
        session_id=root.logical.session_id,
        session_operation_context=inventory.running.session_operation_context,
        session_operation_authority=service._require_sessions_service().session_operation_authority,
    )
    require_no_credential_material_in_state(live.state, surface="captured continuation runtime state", env_ref_names=names)
    prior_caches = decode_continuation_caches(runtime_chunk(selected, frame.caches), env_ref_names=names)
    retained_caches = live.continuation_cache_history
    if (
        prior_caches.capture_count < retained_caches.capture_count
        or prior_caches.capture_creators[: retained_caches.capture_count] != retained_caches.capture_creators
        or prior_caches.discovery[: len(retained_caches.discovery)] != retained_caches.discovery
        or prior_caches.preflight[: len(retained_caches.preflight)] != retained_caches.preflight
        or (*prior_caches.previous_last_preflight_json, prior_caches.last_preflight_json)[: retained_caches.capture_count]
        != (*retained_caches.previous_last_preflight_json, retained_caches.last_preflight_json)
        or (*prior_caches.previous_last_preflight_identities, prior_caches.last_preflight_identity)[: retained_caches.capture_count]
        != (*retained_caches.previous_last_preflight_identities, retained_caches.last_preflight_identity)
    ):
        raise AuditIntegrityError("runtime capture replaced its retained complete cache prefix")
    data = runtime.graph.batch
    captured_data: RestoredBatchData | RestoredPreparedBatchData | None = data
    if data is not None and data.batch.phase == "call_active":
        if runtime.graph.selected_audit is None or runtime.graph.selected_dialect is None:
            raise AuditIntegrityError("runtime capture lost its prepared audit DATA pair")
        call = data.batch.current_call
        if call is None:
            raise AuditIntegrityError("runtime capture lost its actual prepared call")
        portable = runtime.graph.selected_audit if call.audit is None else capture_dispatch_audit(call.audit, env_ref_names=names)
        captured_data = RestoredPreparedBatchData(data.batch, data.accumulator, data.calls, portable, runtime.graph.selected_dialect)
    admitted = capture_turn_evidence(
        live.turn_evidence,
        live.llm_messages,
        logical=root.logical,
        recorder=live.recorder,
        drained=runtime.drained,
        env_ref_names=names,
        data=captured_data,
        materialization=runtime.materialization,
        mandatory_retention=True,
        prior_application_entry=source.evidence.application_entry,
    )
    shelf = admitted.snapshot
    evidence = encode_turn_evidence(admitted, env_ref_names=names, mandatory_retention=True)
    transcript = encode_message_shelf(shelf.transcript, mandatory_retention=True, env_ref_names=names)
    messages = encode_mandatory_private_chunk(
        codec="composer.messages.v1",
        value={"messages_json": shelf.transcript.messages_json, "current_indexes": shelf.transcript.current_indexes},
        env_ref_names=names,
    )
    tools = encode_mandatory_private_chunk(codec="composer.tools.v1", value=live.tools, env_ref_names=names)
    state = encode_mandatory_private_chunk(codec="composer.state.v1", value=live.state.to_dict(), env_ref_names=names)
    audit = admitted.recorder_chunk
    caches = encode_continuation_caches(
        capture_continuation_caches(
            discovery=live.discovery_cache,
            preflight=live.runtime_preflight_cache,
            last_preflight=live.last_runtime_preflight,
            creator=inventory.running.session_operation_context,
            prior=prior_caches,
            env_ref_names=names,
        ),
        env_ref_names=names,
    )
    iteration = encode_continuation_iteration(
        capture_continuation_iteration(
            producer_inputs=runtime.materialization.snapshot_inputs(),
            last_validation=live.last_validation,
            advisor_review=live.advisor_review_state,
            failed_turn=live.failed_turn,
            anti_anchor=live.anti_anchor.snapshot_for_continuation(),
            disclosure=runtime.materialization.service._schema_disclosure.snapshot_for_continuation(
                session_id=root.logical.session_id,
                catalog_hash=bindings.plugin_snapshot.snapshot_hash,
                policy_hash=bindings.plugin_snapshot.policy_hash,
            ),
            env_ref_names=names,
        ),
        env_ref_names=names,
    )
    changed = frame.model_copy(
        update={
            "messages": messages.reference,
            "tools": tools.reference,
            "state": state.reference,
            "evidence": evidence.reference,
            "transcript": transcript.reference,
            "audit_buffer": audit.reference,
            "caches": caches.reference,
            "iteration": iteration.reference,
            "current_state_id": live.current_state_id,
            "composition_slice": live.composition_turns_used,
            "discovery_slice": live.discovery_turns_used,
            "composition_total": frame.composition_total + composition_delta,
            "discovery_total": frame.discovery_total + discovery_delta,
            "advisor_calls": live.advisor_calls_used,
            "repair_turns": live.repair_turns_used,
            "advisor_checkpoint_passes": live.advisor_checkpoint_passes_used,
            "mutation_success_seen": live.mutation_success_seen,
            "persisted_assistant_message_id": live.persisted_assistant_message_id,
            "persisted_assistant_content": live.persisted_assistant_content,
            "persisted_tool_call_turn": live.persisted_tool_call_turn,
            "advisor_repair_context_introduced": live.advisor_repair_context_introduced,
            "pending_advisor_elision_indices": tuple(live.pending_advisor_elision_indices),
        }
    )
    frames = list(root.frames)
    frames[index] = changed
    added: tuple[PrivateChunk, ...] = (messages, tools, state, evidence, transcript, audit, caches, iteration)
    batch_indexes = tuple(i for i, value in enumerate(frames) if type(value) is ToolBatchFrame)
    if batch_indexes and data is not None and data.batch.phase not in ("p4_pending", "complete"):
        captured_graph = admitted.graph
        if captured_graph is None:
            raise AuditIntegrityError("runtime capture lost its actual encoded shared batch graph")
        projected = captured_graph.selected
        if len(batch_indexes) != 1:
            raise AuditIntegrityError("runtime capture lost its one actual selected batch")
        batch_index = batch_indexes[0]
        batch_frame = frames[batch_index]
        if type(batch_frame) is not ToolBatchFrame:
            raise AuditIntegrityError("runtime capture changed its nominal batch frame")
        if projected.batch.phase == "call_active":
            if captured_graph.selected_audit is None or captured_graph.selected_dialect is None:
                raise AuditIntegrityError("runtime capture lost its encoded portable audit DATA pair")
            encoded = encode_restored_prepared_batch_data(
                RestoredPreparedBatchData(
                    projected.batch, projected.accumulator, projected.calls, captured_graph.selected_audit, captured_graph.selected_dialect
                ),
                env_ref_names=names,
            )
        else:
            encoded = encode_coupled_batch_data(
                batch=projected.batch, accumulator=projected.accumulator, calls=projected.calls, env_ref_names=names
            )
        # Keep a consumed attachment wrapper while the same call remains owed.
        # Its actual finisher may replace it only after advancing that call.
        old_chunk = runtime_chunk(selected, batch_frame.accumulator)
        if batch_frame.phase == "result_retained" and data.batch.next_tool_index == batch_frame.next_tool_index:
            from elspeth.web.composer.continuation_attachment_result_data import decode_attachment_history_batch

            retained = decode_attachment_history_batch(old_chunk, env_ref_names=names)
            if retained.prepared_json != encoded:
                raise AuditIntegrityError("runtime changed an unconsumed attachment prepared source")
        else:
            batch = encode_private_chunk(
                codec=("composer.tool-batch.v1" if projected.batch.phase == "call_active" else "composer.coupled-batch-data.v1"),
                canonical=encoded,
                env_ref_names=names,
            )
            frames[batch_index] = batch_frame.model_copy(
                update={"accumulator": batch.reference, "next_tool_index": data.batch.next_tool_index}
            )
            added = (*added, batch)
    changed_root = root.model_copy(update={"frames": tuple(frames)})
    chunks = _selected_chunks(changed_root, selected.chunks, added)
    SelectedContinuationState(selected.checkpoint_id, changed_root, chunks)
    return CapturedContinuationRuntime(selected, changed_root, chunks)""",
    (RUNTIME, "capture_continuation_runtime_owners"): """async def capture_continuation_runtime_owners(
    runtime: RestoredContinuationRuntime, *, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
) -> CapturedContinuationRuntime:
    if type(runtime) is not RestoredContinuationRuntime:
        raise TypeError("runtime capture requires its actual restored live owners")
    work = runtime.bindings.required_work
    if type(work) is not RequiredWorkBinding:
        raise AuditIntegrityError("runtime capture lost its actual required work")
    authority = runtime.materialization.authority
    running = _work_owners(authority, inventory, work)

    def capture() -> CapturedContinuationRuntime:
        from elspeth.web.composer.continuation_evidence import state_from_evidence_json
        from elspeth.web.composer.protocol import ComposerContextUnsupported

        source = authority.read_continuation_runtime(running, selected=selected, inventory=inventory)
        try:
            captured = _capture_owners(runtime, source=source, inventory=inventory)
        except ComposerContextUnsupported as refused:
            try:
                authority.read_continuation_runtime(running, selected=selected, inventory=inventory)
            except BaseException as failure:
                raise BaseExceptionGroup("Retention refusal and final current-source admission", [refused, failure]) from None
            # Carry only work the actual recorder has not drained. Prior
            # committed effects and the last valid checkpoint stay in place.
            live = runtime.live
            raise refused.with_evidence(
                state=live.state,
                initial_version=state_from_evidence_json(source.evidence.effects.latest_verified_state_json).version,
                tool_invocations=live.recorder.invocations[runtime.drained.invocations :],
                llm_calls=live.recorder.llm_calls[runtime.drained.llm_calls :],
                failed_turn=live.failed_turn,
            ) from refused
        authority.read_continuation_runtime(running, selected=selected, inventory=inventory)
        return captured

    result = await _registered_read(work, CapturedContinuationRuntime, capture)
    if type(result) is not CapturedContinuationRuntime:
        raise AuditIntegrityError("runtime capture returned another nominal owner")
    return result""",
    (
        INITIAL_DATA,
        "capture_initial_composition",
    ): '''def capture_initial_composition(owners: InitialCompositionOwners) -> CapturedInitialComposition:
    """Pure complete capture, also used inside the publisher's existing transaction.

    No current producer callbacks or engine transactions run from this function.
    The publisher separately re-admits source/current authority on its connection.
    """
    if type(owners) is not InitialCompositionOwners:
        raise AuditIntegrityError("initial composition capture requires its actual owner bundle")
    source, materialization, live = owners.source, owners.materialization, owners.live
    if type(live) is not _ComposeIterationState:
        raise AuditIntegrityError("initial composition capture requires its actual complete iteration-state owner")
    if (
        source.initialized
        or type(materialization) is not CurrentContinuationMessageProducers
        or type(materialization.inputs) is not _ReadyProducerInputs
        or materialization.inputs.source is not source
        or materialization.selected is not source.selected
        or materialization.inventory is not source.inventory
        or type(live.turn_evidence) is not TurnEvidence
        or type(live.recorder) is not BufferingRecorder
        or type(owners.drained) is not RecorderDrainCursors
        or type(live.anti_anchor) is not AntiAnchorTracker
        or type(live.advisor_review_state) is not _AdvisorReviewState
        or type(owners.disclosure) is not SchemaDisclosureSnapshot
        or type(live.llm_messages) is not list
        or type(live.tools) is not list
    ):
        raise AuditIntegrityError("initial composition capture changed its actual live/source owners")
    original, root = source.original, source.selected.stack
    context = source.inventory.running.session_operation_context
    logical = root.logical
    if (
        live.state is not original.initial_state
        or live.initial_version != original.initial_state.version
        or live.current_state_id != logical.base_state_id
        or live.composition_turns_used != 0
        or live.discovery_turns_used != 0
        or live.mutation_success_seen
        or live.last_validation is not None
        or live.last_runtime_preflight is not None
        or live.advisor_calls_used != 0
        or live.repair_turns_used != 0
        or live.advisor_checkpoint_passes_used != 0
        or live.persisted_assistant_message_id is not None
        or live.persisted_assistant_content is not None
        or live.persisted_tool_call_turn
        or live.failed_turn is not None
        or live.advisor_repair_context_introduced
        or live.pending_advisor_elision_indices
        or live.continuation_cache_history is not None
    ):
        raise AuditIntegrityError("initial iteration owner is not the actual original first composition")
    if (
        live.turn_evidence.effects.initial_state is not original.initial_state
        or live.turn_evidence.effects.latest_verified_state is not original.initial_state
        or live.turn_evidence.effects.initial_state_id != logical.base_state_id
        or live.turn_evidence.effects.latest_committed_state_id != logical.base_state_id
        or live.turn_evidence.session_id != logical.session_id
        or live.turn_evidence.operation_id != context.fence.operation_id
        or live.turn_evidence._continuation_current_epoch != context.fence.operation_epoch
        or live.turn_evidence._continuation_actor_user_id != original.snapshot.actor_user_id
        or live.turn_evidence.request_id != logical.original_user_message_id
        or live.turn_evidence.request_hash != stable_hash(original.message)
    ):
        raise AuditIntegrityError("initial composition capture changed its original/current evidence binding")
    reader = live.turn_evidence.transcript._blob_reader
    if type(reader) is not CurrentTranscriptBlobReader or reader.service is not materialization.service or reader.context is not context:
        raise AuditIntegrityError("initial composition lost its actual current transcript blob reader")
    reader.require_for_evidence(live.turn_evidence)
    if live.discovery_cache or live.runtime_preflight_cache:
        raise AuditIntegrityError("first composition cannot hide preexisting cache observations")
    empty_recorder = encode_recorder_snapshot(BufferingRecorder().snapshot_for_continuation(drained=RecorderDrainCursors()))
    producer_inputs = capture_continuation_producer_inputs((), env_ref_names=source.inventory.names)
    if materialization.snapshot_inputs() != producer_inputs:
        raise AuditIntegrityError("first composition has no schema-call producer history")
    produced = materialization._produced
    if not produced or any(
        type(recipe) is not InitialMessageRecipe or recipe.index != index for index, (_, recipe, _) in enumerate(produced)
    ):
        raise AuditIntegrityError("first composition lost its exact initial-message producer positions")
    if any(index >= len(live.llm_messages) or live.llm_messages[index] is not message for index, (message, _, _) in enumerate(produced)):
        raise AuditIntegrityError("first composition substituted a copy of an actual registered initial message")
    expected_tail = []
    if original.recovery_context is not None:
        expected_tail.append({"role": "system", "content": recovery_context_message(original.recovery_context)})
    expected_tail.append({"role": "system", "content": retained_operation_context_message(original.retained_context)})
    if live.llm_messages[len(produced) :] != expected_tail:
        raise AuditIntegrityError("first composition changed original recovery/retained-context message order or bytes")
    admitted = capture_turn_evidence(
        live.turn_evidence,
        live.llm_messages,
        logical=logical,
        recorder=live.recorder,
        drained=owners.drained,
        env_ref_names=source.inventory.names,
        materialization=materialization,
        mandatory_retention=True,
        expected_recorder=empty_recorder,
    )
    audit = admitted.recorder_chunk
    evidence = encode_turn_evidence(admitted, env_ref_names=source.inventory.names, mandatory_retention=True)
    snapshot = admitted.snapshot
    transcript = encode_message_shelf(snapshot.transcript, mandatory_retention=True, env_ref_names=source.inventory.names)
    messages = encode_mandatory_private_chunk(
        codec="composer.messages.v1",
        value={"messages_json": snapshot.transcript.messages_json, "current_indexes": snapshot.transcript.current_indexes},
        env_ref_names=source.inventory.names,
    )
    tools = encode_mandatory_private_chunk(codec="composer.tools.v1", value=live.tools, env_ref_names=source.inventory.names)
    state = encode_private_chunk(
        codec="composer.state.v1", canonical=original.snapshot.initial_state_json, env_ref_names=source.inventory.names
    )
    caches = encode_continuation_caches(
        capture_continuation_caches(
            discovery=live.discovery_cache,
            preflight=live.runtime_preflight_cache,
            last_preflight=None,
            creator=context,
            prior=None,
            env_ref_names=source.inventory.names,
        ),
        env_ref_names=source.inventory.names,
    )
    iteration = encode_continuation_iteration(
        capture_continuation_iteration(
            producer_inputs=producer_inputs,
            last_validation=None,
            advisor_review=live.advisor_review_state,
            failed_turn=None,
            anti_anchor=live.anti_anchor.snapshot_for_continuation(),
            disclosure=owners.disclosure,
            env_ref_names=source.inventory.names,
        ),
        env_ref_names=source.inventory.names,
    )
    attachments = encode_attachment_history(empty_attachment_history(session_id=logical.session_id, operation_id=logical.operation_id))
    frontier = encode_frontier_chunk(
        initial_continuation_frontier(
            request=original.message,
            original_message_id=logical.original_user_message_id,
            graph_hash=authored_state_hash(original.initial_state),
        )
    )
    sequence = source.inventory.running.claim.segment_sequence
    if type(sequence) is not int or sequence < 1:
        raise AuditIntegrityError("first composition lost its actual positive segment sequence")
    compose = ComposeFrame(
        phase="action_ready",
        bootstrap=source.bootstrap.reference,
        iteration=iteration.reference,
        messages=messages.reference,
        tools=tools.reference,
        initial_state=state.reference,
        state=state.reference,
        evidence=evidence.reference,
        audit_buffer=audit.reference,
        transcript=transcript.reference,
        caches=caches.reference,
        attachment_admissions=attachments.reference,
        initial_version=original.initial_state.version,
        current_state_id=logical.base_state_id,
        slice_sequence=sequence,
        composition_slice=0,
        discovery_slice=0,
        composition_total=0,
        discovery_total=0,
        advisor_calls=0,
        repair_turns=0,
        advisor_checkpoint_passes=0,
        mutation_success_seen=False,
        persisted_assistant_message_id=None,
        persisted_assistant_content=None,
        persisted_tool_call_turn=False,
        advisor_repair_context_introduced=False,
        pending_advisor_elision_indices=(),
        pending_response=None,
    )
    following = root.model_copy(update={"frames": (compose,), "semantic_frontier": frontier.reference})
    added = (source.bootstrap, iteration, messages, tools, state, evidence, audit, transcript, caches, attachments, frontier)
    available = {chunk.reference: chunk for chunk in added}
    chunks = tuple(available[ref] for ref in required_private_chunks(following))
    require_private_chunk_closure(following, chunks)
    return CapturedInitialComposition(following, chunks)''',
    (FIRST_RUNTIME, "_restore_first_graph"): """def _restore_first_graph(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    work: RequiredWorkBinding,
    expected: FirstRuntimeSources,
) -> FirstRuntimeGraph:
    _require_current_reader_service(service, authority)
    running = _work_owners(authority, inventory, work)
    with locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn:
        before = read_first_runtime_sources_on_connection(conn, service, running, selected=selected, inventory=inventory)
        _require_same_sources(expected, before)
    # Nothing from the ended SQL window is a live admission capability.
    # These are provisional values; all DATA are independently reread below.
    inputs, snapshot = before.inputs, before.runtime.evidence
    producer = CurrentContinuationMessageProducers(service, authority, selected, inventory, inputs)
    producer.require_context(
        session_id=selected.stack.logical.session_id,
        context=running.session_operation_context,
        actor_user_id=inputs.bootstrap.actor_user_id,
    )
    evidence, initial, latest = _begin_current_evidence(snapshot, context=running.session_operation_context)
    history = AdvisorTranscriptOwner._continuation_history(
        snapshot.transcript,
        session_id=snapshot.session_id,
        actor_user_id=snapshot.actor_user_id,
        request_id=snapshot.request_id,
        request=snapshot.transcript.request,
    )
    # Actual initial/policy/schema/content-scope callbacks remain outside SQL.
    shelf = producer._rebuild_shelf(snapshot.transcript, inputs=inputs)
    evidence.transcript, messages = AdvisorTranscriptOwner._from_continuation_shelf(
        snapshot.transcript, request=snapshot.transcript.request, history=history, shelf=shelf
    )
    reader = CurrentTranscriptBlobReader.from_service(
        service, session_id=str(running.claim.session_id), context=running.session_operation_context
    )
    graph = _finish_current_evidence(
        inputs.admitted_evidence,
        context=running.session_operation_context,
        owner=evidence,
        initial=initial,
        latest=latest,
        messages=messages,
        reader=reader,
        env_ref_names=inventory.names,
    )
    # This fresh complete SQL window is AFTER shelf/graph callbacks and BEFORE
    # any live callback. No earlier currentness/decode survives as authority.
    with locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn:
        after = read_first_runtime_sources_on_connection(conn, service, running, selected=selected, inventory=inventory)
        _require_same_sources(before, after)
    return FirstRuntimeGraph(expected, graph, producer)""",
}


_CAPTURE_EVIDENCE_SIGNATURE = """def capture_turn_evidence(
    owner: TurnEvidence, messages: Sequence[Mapping[str, Any]], *,
    logical: LogicalRequestBinding, recorder: BufferingRecorder,
    drained: RecorderDrainCursors, env_ref_names: frozenset[str],
    data: RestoredBatchData | RestoredPreparedBatchData | None = None,
    materialization: CurrentContinuationMessageProducers | None = None,
    mandatory_retention: bool = False,
    prior_application_entry: PortableP4ApplicationEntry | None = None,
    expected_recorder: PrivateChunk | None = None,
) -> ce.AdmittedTurnEvidence:
    pass
"""

_CAPTURE_RECORDER_GUARD = """
recorder_snapshot = recorder.snapshot_for_continuation(drained=drained)
recorder_source = capture_application_recorder_source(recorder_snapshot, env_ref_names=env_ref_names)
recorder_chunk = recorder_source.chunk
if expected_recorder is not None and recorder_chunk != expected_recorder:
    raise AuditIntegrityError("evidence capture changed its expected original recorder")
joint_graph_json, application_entry = capture_joint_evidence_graph(
    owner=owner, recorder=recorder, drained=drained, data=data,
    env_ref_names=env_ref_names, recorder_snapshot=recorder_snapshot, recorder_source=recorder_source,
)
sources = P4ApplicationSources(application_entry, owner._portable_p4_sources)
"""

_CAPTURE_HISTORY_GUARD_AND_RETURN = """
fields = capture_fields() if mandatory_retention or prior_application_entry is not None else None
if fields is not None:
    ce.require_evidence_fields_admission(fields, env_ref_names=env_ref_names, sources=sources, recorder_chunk=recorder_chunk)
    if prior_application_entry is not None and (
        fields.application_entry is None
        or fields.application_entry.applications[:len(prior_application_entry.applications)] != prior_application_entry.applications
    ):
        raise AuditIntegrityError("runtime capture lost selected actual application history")
transcript_snapshot = owner.transcript.snapshot_for_continuation(
    messages, materialization=materialization, mandatory_retention=mandatory_retention
)
if fields is None:
    fields = capture_fields()
snapshot = ce.TurnEvidenceSnapshot(**fields.__dict__, transcript=transcript_snapshot)
return ce.admit_turn_evidence(
    snapshot, env_ref_names=env_ref_names, mandatory_retention=mandatory_retention,
    source_chunks=(recorder_chunk, *sources.chunks),
)
"""


def _capture_evidence_moved_guards(unit, failures):
    """Bind only the two guards moved out of the retained caller bodies."""
    owner = _custody_node_at(unit, "capture_turn_evidence")
    if not isinstance(owner, ast.FunctionDef):
        failures.append("runtime initialization moved capture supplier definition changed")
        return set()
    signature = ast.parse(_CAPTURE_EVIDENCE_SIGNATURE).body[0]
    _require(
        failures,
        not owner.decorator_list
        and not owner.type_params
        and ast.dump(owner.args) == ast.dump(signature.args)
        and isinstance(owner.returns, ast.AST)
        and ast.dump(owner.returns) == ast.dump(signature.returns),
        "runtime initialization moved capture signature changed",
    )
    prefix = ast.parse(_CAPTURE_RECORDER_GUARD).body
    prefixes = [
        owner.body[start : start + len(prefix)]
        for start in range(len(owner.body) - len(prefix) + 1)
        if all(
            ast.dump(actual) == ast.dump(expected) for actual, expected in zip(owner.body[start : start + len(prefix)], prefix, strict=True)
        )
    ]
    _require(failures, len(prefixes) == 1, "runtime initialization moved empty-recorder guard changed")
    tail = ast.parse(_CAPTURE_HISTORY_GUARD_AND_RETURN).body
    actual_tail = owner.body[-len(tail) :]
    tail_matches = len(actual_tail) == len(tail) and all(
        ast.dump(actual) == ast.dump(expected) for actual, expected in zip(actual_tail, tail, strict=True)
    )
    _require(failures, tail_matches, "runtime initialization moved history/admitted return changed")
    constructors = [node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == "capture_fields"]
    _require(failures, len(constructors) == 1, "runtime initialization moved fields producer changed")
    if len(prefixes) != 1 or not tail_matches or len(constructors) != 1:
        return set()
    prefix, fields = prefixes[0], constructors[0]
    # Exact slices bind the original calls, but cannot by themselves rule out
    # an earlier duplicate expression. Inventory these direct callees across
    # enclosing execution and this known actually called local producer.
    # Other deferred helper bodies stay outside the finite proof. Compare
    # original AST nodes rather than just matching call arguments.
    direct_calls = [node for scope in (owner, fields) for node in receiver.executed_body(scope) if isinstance(node, ast.Call)]
    for label, expected_calls in (
        ("recorder snapshot", [prefix[0].value]),
        ("recorder source capture", [prefix[1].value]),
        ("joint-graph capture", [prefix[4].value]),
        ("application sources", [prefix[5].value]),
        ("fields production", [actual_tail[0].value.body, actual_tail[3].body[0].value]),
        ("fields admission", [actual_tail[1].body[0].value]),
        ("transcript materialization", [actual_tail[2].value]),
        ("snapshot construction", [actual_tail[4].value]),
        ("evidence admission", [actual_tail[5].value]),
    ):
        callee = ast.dump(expected_calls[0].func)
        _require(
            failures,
            [node for node in direct_calls if ast.dump(node.func) == callee] == expected_calls,
            "runtime initialization moved " + label + " site/order changed",
        )
    _require(
        failures,
        owner.body.index(prefix[-1]) < owner.body.index(fields) < owner.body.index(actual_tail[0]),
        "runtime initialization moved recorder/fields/history order changed",
    )
    field_return = fields.body[0] if len(fields.body) == 1 else None
    field_call = field_return.value if isinstance(field_return, ast.Return) else None
    field_shape = (
        not fields.decorator_list
        and not fields.type_params
        and ast.dump(fields.args) == ast.dump(ast.parse("def capture_fields(): pass").body[0].args)
        and isinstance(fields.returns, ast.AST)
        and ast.dump(fields.returns) == ast.dump(ast.parse("ce.TurnEvidenceFields", mode="eval").body)
        and isinstance(field_call, ast.Call)
        and ast.unparse(field_call.func) == "ce.TurnEvidenceFields"
        and not field_call.args
        and all(keyword.arg is not None for keyword in field_call.keywords)
        and len({keyword.arg for keyword in field_call.keywords}) == len(field_call.keywords)
    )
    _require(failures, field_shape, "runtime initialization moved fields constructor changed")
    if field_shape:
        for name, expression in (
            ("recorder", "recorder_chunk.reference"),
            ("historical_application_count", "0 if application_entry is None else len(application_entry.applications)"),
            ("joint_graph_json", "joint_graph_json"),
            ("application_entry", "application_entry"),
        ):
            values = [keyword.value for keyword in field_call.keywords if keyword.arg == name]
            _require(
                failures,
                len(values) == 1 and ast.dump(values[0]) == ast.dump(ast.parse(expression, mode="eval").body),
                "runtime initialization moved fields forwarding changed " + name,
            )
    field_events = receiver.lexical_binding_events(fields)
    for name in ("ce", "len", "recorder_chunk", "application_entry", "joint_graph_json", "sources"):
        _require(
            failures,
            not any(bound == name for bound, _kind, _node in field_events),
            "runtime initialization moved fields supplier/local binding changed " + name,
        )
    # Match binding events against original nodes, including the two exact
    # fields stores. No arbitrary rebind, extra return, or deferred substitute
    # may detach the checks from the snapshot returned by this supplier.
    expected_bindings = {
        "recorder_snapshot": [("STORE_OR_DELETE", prefix[0].targets[0])],
        "recorder_source": [("STORE_OR_DELETE", prefix[1].targets[0])],
        "recorder_chunk": [("STORE_OR_DELETE", prefix[2].targets[0])],
        "joint_graph_json": [("STORE_OR_DELETE", prefix[4].targets[0].elts[0])],
        "application_entry": [("STORE_OR_DELETE", prefix[4].targets[0].elts[1])],
        "sources": [("STORE_OR_DELETE", prefix[5].targets[0])],
        "capture_fields": [("DEFINITION", fields)],
        "fields": [("STORE_OR_DELETE", actual_tail[0].targets[0]), ("STORE_OR_DELETE", actual_tail[3].body[0].targets[0])],
        "transcript_snapshot": [("STORE_OR_DELETE", actual_tail[2].targets[0])],
        "snapshot": [("STORE_OR_DELETE", actual_tail[4].targets[0])],
    }
    for parameter in owner.args.args + owner.args.kwonlyargs:
        expected_bindings[parameter.arg] = [("PARAMETER", parameter)]
    events = receiver.lexical_binding_events(owner)
    for name, expected in expected_bindings.items():
        _require(
            failures,
            [(kind, node) for bound, kind, node in events if bound == name] == expected,
            "runtime initialization moved capture binding changed " + name,
        )
    for name in ("ce", "capture_application_recorder_source", "P4ApplicationSources", "AuditIntegrityError", "len"):
        _require(
            failures,
            not any(bound == name for bound, _kind, _node in events),
            "runtime initialization moved capture supplier shadowed " + name,
        )
    imported = [
        node
        for node in owner.body
        if _same(
            node,
            "from elspeth.web.composer.continuation_evidence_application_data import capture_joint_evidence_graph, journal_effect_locators",
        )
    ]
    _require(
        failures,
        len(imported) == 1
        and [(kind, node) for name, kind, node in events if name == "capture_joint_evidence_graph"] == [("IMPORT", imported[0])],
        "runtime initialization moved joint-graph supplier changed",
    )
    _require(
        failures,
        [node for node in receiver.executed_body(owner) if isinstance(node, (ast.Return, ast.Yield, ast.YieldFrom, ast.Await))]
        == [actual_tail[-1]],
        "runtime initialization moved capture early/deferred return changed",
    )
    for node in receiver.executed_body(owner):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
            root = node.value
            while isinstance(root, (ast.Attribute, ast.Subscript)):
                root = root.value
            _require(
                failures,
                not isinstance(root, ast.Name) or root.id not in expected_bindings,
                "runtime initialization moved capture owner member changed",
            )
    return {(unit.path, id(node)) for node in (*prefix, fields, *actual_tail)}


def runtime_initialization_prerequisite_transfers(units):
    """Check retained callers and moved guards without granting effect sites."""
    index = {unit.path: unit for unit in units}
    paths = {path for path, _symbol in _RUNTIME_INITIALIZATION_BODIES} | {EVIDENCE_GRAPH}
    if len(index) != len(units) or not paths <= set(index):
        return ["runtime initialization prerequisite source set missing or duplicated"], {}
    failures, checked = [], set()
    for (path, symbol), source in _RUNTIME_INITIALIZATION_BODIES.items():
        owner = _custody_node_at(index[path], symbol)
        if owner is None or not _same(owner, source):
            failures.append("runtime initialization exact caller changed " + path + ":" + symbol)
        else:
            checked.add((path, id(owner)))
    checked.update(_capture_evidence_moved_guards(index[EVIDENCE_GRAPH], failures))
    imports = (
        *((path, "capture_turn_evidence", _module(EVIDENCE_GRAPH), "capture_turn_evidence") for path in (RUNTIME, INITIAL_DATA)),
        *(
            (path, "encode_mandatory_private_chunk", "elspeth.web.composer.continuation_codec", "encode_mandatory_private_chunk")
            for path in (RUNTIME, INITIAL_DATA)
        ),
        *(
            (path, "encode_turn_evidence", "elspeth.web.composer.continuation_evidence", "encode_turn_evidence")
            for path in (RUNTIME, INITIAL_DATA)
        ),
        *(
            (path, "encode_message_shelf", "elspeth.web.composer.continuation_message_shelf", "encode_message_shelf")
            for path in (RUNTIME, INITIAL_DATA)
        ),
        (RUNTIME, "decode_recorder_snapshot", "elspeth.web.composer.continuation_audit_buffer", "decode_recorder_snapshot"),
        (FIRST_RUNTIME, "_finish_current_evidence", _module(EVIDENCE_GRAPH), "_finish_current_evidence"),
        (EVIDENCE_GRAPH, "ce", "elspeth.web.composer", "continuation_evidence"),
        (
            EVIDENCE_GRAPH,
            "capture_application_recorder_source",
            "elspeth.web.composer.continuation_application_graph",
            "capture_application_recorder_source",
        ),
        (EVIDENCE_GRAPH, "P4ApplicationSources", "elspeth.web.composer.continuation_evidence_application_values", "P4ApplicationSources"),
        (EVIDENCE_GRAPH, "AuditIntegrityError", "elspeth.contracts.errors", "AuditIntegrityError"),
    )
    for path, name, module, member in imports:
        _require(
            failures,
            receiver.imported(index[path].tree, name, module, member),
            "runtime initialization canonical supplier changed " + path + ":" + name,
        )
    # Existing external primitives retain their definition/import boundary;
    # this does not recursively seal their implementation bodies.
    suppliers = {(module, member) for _path, name, module, member in imports if name != "ce"}
    suppliers.update(
        {
            ("elspeth.web.composer.continuation_finish_reads", "ContinuationFinishRead"),
            ("elspeth.web.composer.continuation_evidence_application_data", "capture_joint_evidence_graph"),
            *(
                ("elspeth.web.composer.continuation_evidence", name)
                for name in ("TurnEvidenceFields", "TurnEvidenceSnapshot", "require_evidence_fields_admission", "admit_turn_evidence")
            ),
        }
    )
    for module, member in sorted(suppliers):
        path = "src/" + module.replace(".", "/") + ".py"
        _require(
            failures,
            path in index and _custody_node_at(index[path], member) is not None,
            "runtime initialization supplier definition changed " + path + ":" + member,
        )
    for path, names in ((RUNTIME, ("type", "BaseException", "BaseExceptionGroup")), (EVIDENCE_GRAPH, ("len",))):
        for name in names:
            _require(
                failures, receiver.builtin_binding(index[path].tree, name), "runtime initialization builtin changed " + path + ":" + name
            )
    read = _custody_node_at(index[RUNTIME], "_registered_read")
    _require(
        failures,
        read is not None and _digest(read) == _CONTRACTS[RUNTIME]["_registered_read"],
        "runtime initialization registered SQL reader changed",
    )
    if failures:
        return failures, {}
    return [], {"checked_nodes": frozenset(checked)}


def iteration_local_transfers(units):
    """Exact extra state/bindings construction and the first-state SQL join."""
    paths = (CARRIERS, RUNTIME, INITIALIZATION, INITIAL_DATA, INITIAL_PUBLICATION, FIRST_RUNTIME, FREEZE, CACHES, SERVICE)
    failures, index, selected, receipt = _select(units, paths, "continuation iteration")
    if failures:
        return failures, {}
    failures, _ = runtime_initialization_prerequisite_transfers(units)
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
elif ctx.message_producers is not None and not (tool_name == "get_plugin_schema" and result.success):
    ctx.message_producers.require_context(session_id=session_id, context=ctx.session_operation_context, actor_user_id=user_id)
    ctx.message_producers.register_tool_result_message(
        completion=call_model.completion,
        tool_index=call.index,
        call=tool_call,
        result=admitted_result if admitted_result is not None else result,
        serialized=result_json,
        message=result_message,
        catalog=ctx.policy_catalog,
    )
"""


def _custody_node_at(unit, symbol):
    parts = symbol.split(".")
    qualified = tuple(parts)
    # A real enclosing execution can replace/delete an already defined class
    # member without rebinding its Name. Inspect the original source nodes,
    # including tuple/control-flow targets, but not deferred function bodies.
    # Alias writes and deferred/global effects remain separate common premises.
    for effect in receiver.immediate(unit.tree):
        if isinstance(effect, ast.Attribute) and isinstance(effect.ctx, (ast.Store, ast.Del)):
            target = receiver.chain(effect)
            if len(target) > 1 and qualified[: len(target)] == target:
                return None
    nodes = unit.tree.body
    for part in parts:
        matches = [
            n
            for n in nodes
            if (isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == part)
            or (isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and (n.target.id == part))
            or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == part for t in n.targets))
        ]
        if len(matches) != 1:
            return None
        n = matches[0]
        scope = ast.Module(body=nodes, type_ignores=[])
        if not receiver.owned_binding(scope, part, n):
            return None
        # Enumerate this actual enclosing lexical body without executing or
        # copying it. This includes destructuring, control-flow targets,
        # imports and named expressions while keeping nested scopes separate.
        lexical = ast.parse("def _binding_scope():\n    pass\n").body[0]
        lexical.body = nodes
        events = receiver.lexical_binding_events(lexical)
        if [(kind, node) for bound, kind, node in events if bound in {part, "*"}] != [("DEFINITION", n)]:
            return None
        nodes = n.body if isinstance(n, ast.ClassDef) else []
    return n


def p4_batch_custody_receipt(units):
    """Bind P4's concrete custody transfers; never grant an arbitrary callback."""
    batch_path = "src/elspeth/web/composer/tool_batch.py"
    phase_path = "src/elspeth/web/composer/continuation_live_phases.py"
    custody_path = "src/elspeth/web/composer/continuation_tool_failure.py"
    publication_path = "src/elspeth/web/composer/continuation_prompt_producers.py"
    indexed = {unit.path: unit for unit in units}
    if len(indexed) != len(units) or any(path not in indexed for path in (batch_path, phase_path, custody_path, publication_path)):
        return ["P4 batch custody source is missing or duplicated"], {}
    batch, phases, custody = (indexed[path] for path in (batch_path, phase_path, custody_path))
    helper = receiver
    failures = []
    receiver_uses = set()
    native_calls = set()
    native_uses = set()

    def require(condition, detail):
        if not condition:
            failures.append("P4 batch custody " + detail)

    def one(owner, spelling, detail):
        expected = ast.parse(spelling).body[0]
        found = [node for node in owner.body if stable_ast_dump(node) == stable_ast_dump(expected)]
        require(len(found) == 1, detail)
        return found[0] if len(found) == 1 else None

    initialize = _custody_node_at(batch, "_initialize_continuation_batch_live")
    primary = _custody_node_at(phases, "ContinuationLivePhases._primary_for_phase")
    execute = _custody_node_at(batch, "_execute_continuation_call_live")
    finish = _custody_node_at(batch, "_finish_continuation_batch_live")
    issue = _custody_node_at(phases, "ContinuationLivePhases._execute_one_owned")
    persist = _custody_node_at(phases, "ContinuationLivePhases._persist_batch")
    if not all(isinstance(node, ast.AsyncFunctionDef) for node in (initialize, primary, execute, issue, persist)) or not isinstance(
        finish, ast.FunctionDef
    ):
        return ["P4 batch custody native definition changed"], {}
    phase_contract = ast.parse(OWNER_TEMPLATES["phases.ContinuationLivePhases"])
    for symbol in ("_primary_for_phase", "_capture_issued_finish", "_execute_one_owned", "_persist_batch"):
        qualified = "ContinuationLivePhases." + symbol
        actual = _custody_node_at(phases, qualified)
        expected = _find(phase_contract, qualified)
        require(
            actual is not None and expected is not None and _digest(actual) == _digest(expected),
            "complete actual native source changed " + qualified,
        )
    for symbol, key in (
        (
            "ContinuationToolFailureCustody.promote_completed_service_failure",
            "failure.ContinuationToolFailureCustody.promote_completed_service_failure",
        ),
        ("TerminalServiceCompletion", "failure.TerminalServiceCompletion"),
        ("_require_service_failure_output", "failure._require_service_failure_output"),
    ):
        actual = _custody_node_at(custody, symbol)
        expected = ast.parse(OWNER_TEMPLATES[key]).body[0]
        require(actual is not None and _digest(actual) == _digest(expected), "complete service owner source changed " + symbol)
    for unit, name, declaration in ((batch, initialize.name, initialize), (batch, execute.name, execute), (batch, finish.name, finish)):
        require(helper.owned_binding(unit.tree, name, declaration), "native binding changed " + name)
    require(
        helper.imported(phases.tree, initialize.name, "elspeth.web.composer.tool_batch", initialize.name),
        "actual initializer import changed",
    )
    initialize_call = one(
        primary,
        "await _initialize_continuation_batch_live("
        "call_model=result.outcome, ctx=context, acc=accumulator, llm_messages=live.llm_messages, batch=batch)",
        "actual initializer owner arguments changed",
    )
    for declaration, parameters in (
        (execute, ("call_model", "ctx", "acc", "llm_messages", "batch", "call", "retained_attachment", "failure_custody")),
        (finish, ("call_model", "ctx", "acc", "batch", "failure_custody")),
    ):
        args = declaration.args
        require(
            not declaration.decorator_list
            and not declaration.type_params
            and not args.args
            and not args.posonlyargs
            and not args.defaults
            and args.vararg is None
            and args.kwarg is None
            and tuple(arg.arg for arg in args.kwonlyargs) == parameters
            and all(
                isinstance(default, ast.Constant) and default.value is None
                if parameter.arg in {"retained_attachment", "failure_custody"}
                else default is None
                for parameter, default in zip(args.kwonlyargs, args.kw_defaults, strict=True)
            ),
            "native signature changed " + declaration.name,
        )
        formal = [arg for arg in args.kwonlyargs if arg.arg == "failure_custody"]
        require(
            len(formal) == 1
            and [(kind, node) for name, kind, node in helper.lexical_binding_events(declaration) if name == "failure_custody"]
            == [("PARAMETER", formal[0])],
            "formal owner rebound " + declaration.name,
        )
    observation = one(
        execute, "observation = None if failure_custody is None else failure_custody.observation", "observation origin changed"
    )
    guard = one(
        execute,
        """if failure_custody is not None and observation is not None:
    if call.audit is None or failure_custody.batch is not batch or failure_custody.call is not call:
        raise AuditIntegrityError("issued failure custody changed its actual batch/call")
    observation.require(recorder, call.audit)
""",
        "actual batch/call/audit identity changed",
    )
    if observation is not None and guard is not None:
        require(execute.body.index(guard) == execute.body.index(observation) + 1, "observation guard order changed")
        require(
            [(kind, node) for name, kind, node in helper.lexical_binding_events(execute) if name == "observation"]
            == [("STORE_OR_DELETE", observation.targets[0])],
            "observation rebound",
        )
    publication = one(
        execute,
        """if ctx.message_producers is not None:
    ctx.message_producers.require_context(session_id=session_id, context=ctx.session_operation_context, actor_user_id=user_id)
    publication = ContinuationToolMessagePublication(ctx.message_producers, call_model.completion, batch, call, recorder, llm_messages, ctx.policy_catalog)
""",
        "publication owner/context changed",
    )
    empty_publication = one(execute, "publication = None", "publication initial binding changed")
    if publication is not None and empty_publication is not None:
        require(execute.body.index(publication) == execute.body.index(empty_publication) + 1, "publication guard order changed")
        require(
            [(kind, node) for name, kind, node in helper.lexical_binding_events(execute) if name == "publication"]
            == [("STORE_OR_DELETE", empty_publication.targets[0]), ("STORE_OR_DELETE", publication.body[1].targets[0])],
            "publication rebound",
        )
    one(
        execute,
        """def _append_issued_feedback(serialized: str) -> None:
    message = {'role': 'tool', 'tool_call_id': call.tool_call.id, 'content': serialized,
               PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(serialized)}
    if publication is not None:
        publication.register_feedback(serialized=serialized, message=message)
    llm_messages.append(message)
""",
        "issued feedback publication changed",
    )
    finish_guard = one(
        finish,
        """if failure_custody is not None and failure_custody.completed:
    if failure_custody.batch is not batch:
        raise AuditIntegrityError("batch finish changed its original native failure custody")
    plugin_crash = failure_custody.wrapper
    plugin_crash_cause = failure_custody.original if plugin_crash is not None else None
    advisor_origin = failure_custody.original
    if batch.service_failure is not None:
        if advisor_origin is not batch.service_failure:
            raise AuditIntegrityError("service finish replaced its actual native original")
        service_failure = batch.service_failure
    if batch.retained_failure is not None and batch.retained_failure.kind == 'advisor_failure':
        if not isinstance(advisor_origin, Exception):
            raise AuditIntegrityError("Advisor finish lost its actual handled exception")
        advisor_failure = advisor_origin
""",
        "completed failure origin changed",
    )
    outer = [node for node in execute.body if isinstance(node, ast.Try)]
    require(len(outer) == 1, "outer exception/copyback scope changed")
    if len(outer) == 1:
        expected_handler = (
            ast.parse(
                """try:
    pass
except BaseException as error:
    if failure_custody is not None:
        if failure_custody.original is None:
            failure_custody.observe_primary(error)
        combined = failure_custody.failure(error)
        if combined is error:
            raise
        raise combined from combined.__cause__
    raise
"""
            )
            .body[0]
            .handlers
        )
        require(
            [stable_ast_dump(node) for node in outer[0].handlers] == [stable_ast_dump(node) for node in expected_handler],
            "original/grouped exception transfer changed",
        )
        expected_copyback = ast.parse(
            """if failure_custody is None:
    batch.plugin_crash = plugin_crash
    batch.plugin_crash_cause = plugin_crash_cause
    batch.advisor_failure = advisor_failure
"""
        ).body[0]
        require(
            sum(stable_ast_dump(node) == stable_ast_dump(expected_copyback) for node in outer[0].finalbody) == 1,
            "failure-owned copyback changed",
        )
    calls = [node for node in ast.walk(execute) if isinstance(node, ast.Call)]
    require(
        Counter(
            ast.unparse(node) for node in calls if isinstance(node.func, ast.Attribute) and ast.unparse(node.func.value) == "publication"
        )
        == {
            "publication.register_feedback(serialized=serialized, message=message)": 1,
            "publication.register_feedback(serialized=tool_feedback_json, message=feedback_message)": 1,
            "publication.bind_completed_outcome(tool_outcomes[-1])": 1,
        },
        "exact feedback/outcome publication changed",
    )
    expected_result = ast.parse(
        """if ctx.message_producers is not None and not (tool_name == 'get_plugin_schema' and result.success):
    ctx.message_producers.require_context(session_id=session_id, context=ctx.session_operation_context, actor_user_id=user_id)
    ctx.message_producers.register_tool_result_message(
        completion=call_model.completion, tool_index=call.index, call=tool_call,
        result=admitted_result if admitted_result is not None else result,
        serialized=result_json, message=result_message, catalog=ctx.policy_catalog)
"""
    ).body[0]
    require(
        sum(stable_ast_dump(node) == stable_ast_dump(expected_result) for node in ast.walk(execute) if isinstance(node, ast.If)) == 1,
        "exact result publication changed",
    )
    forwarding = [node for node in calls if any(keyword.arg == "failure_observation" for keyword in node.keywords)]
    require(
        Counter(ast.unparse(node.func) for node in forwarding)
        == {
            "_await_started_tool_stage": 8,
            "dispatch_with_audit": 1,
            "rebind_dispatch_arguments": 3,
            "ctx.session_tools._dispatch_session_aware_tool": 1,
            "ctx.advisor_checkpoint._call_advisor_for_tool": 1,
        }
        and all(
            [ast.unparse(keyword.value) for keyword in node.keywords if keyword.arg == "failure_observation"] == ["observation"]
            for node in forwarding
        ),
        "exact observation forwarding changed",
    )
    callbacks = [keyword for node in calls for keyword in node.keywords if keyword.arg == "on_provider_dispatch"]
    require(len(callbacks) == 1 and ast.unparse(callbacks[0].value) == "reservation.dispatch", "physical dispatch callback changed")
    finalizers = [
        node
        for node in ast.walk(execute)
        if isinstance(node, ast.Try)
        and [ast.unparse(item) for item in node.finalbody] == ["reservation.close()", "advisor_calls_used = allowance.used"]
    ]
    require(len(finalizers) == 1, "manual reservation finally close changed")
    require(
        Counter(
            ast.unparse(node)
            for node in calls
            if isinstance(node.func, ast.Attribute) and ast.unparse(node.func.value) == "failure_custody"
        )
        == {
            "failure_custody.failure(error)": 1,
            "failure_custody.observe_primary(original)": 1,
            "failure_custody.failure(original)": 1,
            "failure_custody.promote_completed_service_failure(original)": 1,
            "failure_custody.failure(secondary)": 1,
            "failure_custody.observe_primary(error)": 1,
            "failure_custody.consume_nonprimary()": 3,
            "failure_custody.observe_primary(first_party_exc)": 1,
            "failure_custody.promote_completed_failure(kind='advisor_failure', original=first_party_exc)": 1,
            "failure_custody.failure(tool_exc)": 2,
            "failure_custody.promote_completed_failure(kind='plugin_crash', original=tool_exc, wrapper=plugin_crash)": 2,
            "failure_custody.failure(first_party_exc)": 1,
            "failure_custody.promote_completed_failure(kind='advisor_failure', original=first_party_exc, refusal='context_unsupported', allowance_limit=budget)": 1,
            "failure_custody.promote_completed_failure(kind='advisor_failure', original=first_party_exc, refusal='provider_cost_refused', provider_call_id=first_party_exc.call_id)": 1,
            "failure_custody.promote_completed_failure(kind='advisor_failure', original=first_party_exc, refusal='prompt_guard_refused')": 1,
            "failure_custody.observe_primary(tool_exc)": 1,
        },
        "original failure/promotion transfer changed",
    )
    # These are the actual P4 callers. They supplement, rather than replace,
    # the existing ordinary adapter and retained-attachment caller receipts.
    expected_issue_prefix = ast.parse(
        """runtime, inventory = await self.current(selected)
data = runtime.graph.batch
if data is None:
    raise AuditIntegrityError("live dispatch lost its actual batch/provider owners")
result = await self._primary_for_phase(runtime, inventory, selected)
batch, accumulator, live = data.batch, data.accumulator, runtime.live
if batch.phase != 'call_ready' or batch.current_call is not None or batch.next_tool_index >= len(data.calls):
    raise AuditIntegrityError("live dispatch lost its actual unissued next call")
context = await self._tool_context(runtime)
call = ContinuationCallState(batch.next_tool_index, data.calls[batch.next_tool_index], batch.state, batch.state.version)
batch.current_call, batch.phase = call, 'call_active'
preparation = await _prepare_continuation_call_live(call_model=result.outcome, ctx=context, llm_messages=live.llm_messages, batch=batch, call=call)
"""
    ).body
    require(
        [stable_ast_dump(node) for node in issue.body[: len(expected_issue_prefix)]]
        == [stable_ast_dump(node) for node in expected_issue_prefix],
        "actual issued source graph changed",
    )
    for name in ("data", "result", "batch", "accumulator", "live", "context", "call", "preparation"):
        original_stores = [
            node
            for statement in issue.body[: len(expected_issue_prefix)]
            for node in ast.walk(statement)
            if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Store)
        ]
        require(
            len(original_stores) == 1
            and [(kind, node) for bound, kind, node in helper.lexical_binding_events(issue) if bound == name]
            == [("STORE_OR_DELETE", original_stores[0])],
            "actual issued source rebound " + name,
        )
    expected_persist_prefix = ast.parse(
        """terminal = self.terminal_completion
if terminal is None:
    phase_read = await self._current_phase_read(selected)
    runtime, inventory = phase_read.runtime, phase_read.inventory
    result = await self._primary_for_phase(runtime, inventory, selected)
    context = await build_runtime_tool_batch_context(runtime, cancellation_requested=self.cancellation_requested, phase_read=phase_read)
    call_model = result.outcome
else:
    terminal.require_native()
    if self.runtime is not terminal.runtime or selected is not terminal.runtime.source.selected or terminal.p4 is not None:
        raise terminal.custody.failure(AuditIntegrityError("terminal P4 cannot replace or replay its actual native completion"))
    runtime = terminal.runtime
    inventory = runtime.materialization.inventory
    context, call_model = terminal.context, terminal.call_model
data = runtime.graph.batch
if data is None or data.batch.phase != 'dispatch_ready':
    raise AuditIntegrityError("live P4 lost its actual completed batch/provider owners")
"""
    ).body
    require(
        [stable_ast_dump(node) for node in persist.body[: len(expected_persist_prefix)]]
        == [stable_ast_dump(node) for node in expected_persist_prefix],
        "actual finish source graph changed",
    )
    prepared = [node for node in issue.body if isinstance(node, ast.If) and ast.unparse(node.test) == "preparation == 'prepared'"]
    expected_completed = ast.parse(
        """if preparation == 'prepared':
    if issued_effect_key is None:
        raise AuditIntegrityError("completed tool lost its actual issued effect locator")
    receipt = f"tool-result:{top.accumulator.digest}"
    root = root.model_copy(update={
        "effect_frontier": tuple(
            EffectFrontierEntry(effect_id=entry.effect_id, disposition="committed", receipt_id=receipt)
            if entry.effect_id == issued_effect_key else entry for entry in root.effect_frontier
        ),
        "frames": (*root.frames[:-1], root.frames[-1].model_copy(update={
            "settled_effect_ids": (*top.settled_effect_ids, issued_effect_key)
        })),
    })
"""
    ).body[0]
    expected_retention = ast.parse(
        "await self._retain(CapturedContinuationRuntime(selected, root, captured.chunks), inventory, "
        "'tool_completed' if preparation == 'prepared' else 'tool_preparation_completed')"
    ).body[0]
    require(
        len(prepared) == 2
        and len(issue.body) > len(expected_issue_prefix) + 1
        and _same(issue.body[len(expected_issue_prefix)], "issued_effect_key: str | None = None")
        and issue.body[len(expected_issue_prefix) + 1] is prepared[0]
        and issue.body[-2] is prepared[1]
        and stable_ast_dump(prepared[1]) == stable_ast_dump(expected_completed)
        and stable_ast_dump(issue.body[-1]) == stable_ast_dump(expected_retention),
        "actual issued/completed phase layout changed",
    )
    expected_issue = ast.parse(
        """await self._retain(CapturedContinuationRuntime(selected, root, captured.chunks), inventory, 'tool_issued')
self.failure_custody = ContinuationToolFailureCustody(inventory.running, self.work, result.response, batch, data.calls, live.recorder, call)
await _execute_continuation_call_live(call_model=result.outcome, ctx=context, acc=accumulator, llm_messages=live.llm_messages, batch=batch, call=call, failure_custody=self.failure_custody)
"""
    ).body
    require(
        len(prepared) == 2
        and [stable_ast_dump(node) for node in prepared[0].body[-3:]] == [stable_ast_dump(node) for node in expected_issue],
        "actual issued custody constructor/forwarding changed",
    )
    finish_call = one(
        persist,
        "dispatch, advisor_calls = _finish_continuation_batch_live(call_model=call_model, ctx=context, acc=data.accumulator, batch=data.batch, failure_custody=self.failure_custody)",
        "actual finish custody forwarding changed",
    )
    if finish_call is not None:
        require(persist.body.index(finish_call) == len(expected_persist_prefix), "actual finish dispatch order changed")
    prepare_call = one(
        issue,
        "preparation = await _prepare_continuation_call_live(call_model=result.outcome, ctx=context, llm_messages=live.llm_messages, batch=batch, call=call)",
        "actual issued preparation forwarding changed",
    )
    for name in ("_prepare_continuation_call_live", "_execute_continuation_call_live", "_finish_continuation_batch_live"):
        require(helper.imported(phases.tree, name, "elspeth.web.composer.tool_batch", name), "native caller import changed " + name)
    require(
        helper.imported(
            phases.tree,
            "ContinuationToolFailureCustody",
            "elspeth.web.composer.continuation_tool_failure",
            "ContinuationToolFailureCustody",
        ),
        "custody constructor import changed",
    )
    require(
        helper.imported(
            ast.Module(body=execute.body, type_ignores=[]),
            "ContinuationToolMessagePublication",
            "elspeth.web.composer.continuation_prompt_producers",
            "ContinuationToolMessagePublication",
        ),
        "publication constructor import changed",
    )
    ctor = _custody_node_at(custody, "ContinuationToolFailureCustody.__post_init__")
    observation_check = _custody_node_at(custody, "ContinuationDispatchFailureObservation.require")
    if not isinstance(ctor, ast.FunctionDef) or not isinstance(observation_check, ast.FunctionDef):
        return [*failures, "P4 batch custody nominal supplier is missing"], {}
    expected_ctor = ast.parse(
        """def __post_init__(self) -> None:
    if (self.work.running is not self.running or self.call.tool_call is not self.calls[self.call.index]
        or self.batch.current_call is not self.call or self.call.audit is None):
        raise AuditIntegrityError("tool failure custody changed its actual issued owners")
    self.observation = ContinuationDispatchFailureObservation(self, self.call.audit)
"""
    ).body[0]
    expected_check = ast.parse(
        """def require(self, recorder: BufferingRecorder, audit: DispatchAudit) -> None:
    if recorder is not self.owner.recorder or audit is not self.audit:
        raise AuditIntegrityError("dispatch failure observation belongs to another actual audit")
"""
    ).body[0]
    require(
        stable_ast_dump(ctor) == stable_ast_dump(expected_ctor) and stable_ast_dump(observation_check) == stable_ast_dump(expected_check),
        "nominal supplier identity changed",
    )
    for symbol, source in {
        "ContinuationToolFailureCustody.observe_primary": """def observe_primary(self, error: BaseException) -> None:
    if self.original is None:
        self.original = error
    elif self.original is not error:
        self.observe_secondary(error)
""",
        "ContinuationToolFailureCustody.observe_secondary": """def observe_secondary(self, error: BaseException) -> None:
    if error is not self.original and not any(error is item for item in self.errors):
        self.errors.append(error)
""",
        "ContinuationToolFailureCustody.failure": """def failure(self, error: BaseException) -> BaseException:
    self.observe_secondary(error)
    original = error if self.original is None else self.original
    if self.observation.feedback is not None:
        return combine_failure_objects(self.observation.feedback, original, *self.errors)
    return combine_failure_objects(original, *self.errors)
""",
    }.items():
        actual = _custody_node_at(custody, symbol)
        require(
            actual is not None and stable_ast_dump(actual) == stable_ast_dump(ast.parse(source).body[0]),
            "exception supplier changed " + symbol,
        )
    publication_guard = _custody_node_at(indexed[publication_path], "ContinuationToolMessagePublication.require_call")
    expected_publication_guard = ast.parse(
        """def require_call(self, *, recorder: BufferingRecorder, audit: DispatchAudit | None, tool_name: str, tool_call_id: str, messages: list[dict[str, Any]]) -> None:
    from elspeth.web.composer.audit import BufferingRecorder, DispatchAudit
    from elspeth.web.composer.tool_batch import ContinuationBatchState, ContinuationCallState
    if (type(self.producers) is not CurrentContinuationMessageProducers
        or type(self.completion) is not _AdmittedLLMCompletion
        or type(self.batch) is not ContinuationBatchState
        or type(self.call) is not ContinuationCallState
        or type(self.recorder) is not BufferingRecorder
        or self.recorder is not recorder or self.messages is not messages
        or self.batch.current_call is not self.call or self.call.audit is not audit
        or type(audit) is not DispatchAudit
        or (self.call.tool_name, self.call.tool_call.id) != (tool_name, tool_call_id)
        or not 0 <= self.call.index < len(self.completion.tool_batch.calls)
        or self.completion.tool_batch.calls[self.call.index] is not self.call.tool_call):
        raise AuditIntegrityError("tool publication changed its actual issued call/recorder/message owners")
"""
    ).body[0]
    require(
        publication_guard is not None and stable_ast_dump(publication_guard) == stable_ast_dump(expected_publication_guard),
        "publication supplier identity changed",
    )
    if failures:
        return failures, {}
    for node in (guard, publication, finish_guard):
        receiver_uses.update(
            (batch_path, id(part)) for part in ast.walk(node) if isinstance(part, ast.Name) and part.id in {"batch", "call"}
        )
    for statement in (initialize_call, prepared[0].body[-1], finish_call, prepare_call):
        call = next(node for node in ast.walk(statement) if isinstance(node, ast.Call))
        native_calls.add((phase_path, id(call)))
        native_uses.add((phase_path, id(call.func)))
    execute_call = prepared[0].body[-1].value.value
    return [], {
        "receiver_uses": frozenset(receiver_uses),
        "native_calls": frozenset(native_calls),
        "native_uses": frozenset(native_uses),
        "execute_reference": (phase_path, id(execute_call.func)),
    }


TERMINAL_RESERVE_BODY_BINDINGS = {
    (
        "src/elspeth/web/sessions/continuation_driver.py",
        "_SegmentDriver",
    ): "bc847e8d3c44d4de57522a920c91ad8536a07741ed78286822f66ad097026e02",
    (
        "src/elspeth/web/sessions/continuation_tool_failure.py",
        "fail_continuation_tool_operation",
    ): "3e8a8761c26186465d4efb57a1a42e5bf6d410063deb3f178d276a36ba66688c",
}


def terminal_failure_reserve_transfers(units):
    """Join only the existing driver projection and two bounded SQL slots."""
    indexed = {unit.path: unit for unit in units}
    failures, receipt = [], _empty()
    selected = {}
    if len(indexed) != len(units):
        return ["terminal reserve source set duplicated"], {}
    for (path, symbol), digest in TERMINAL_RESERVE_BODY_BINDINGS.items():
        unit = indexed.get(path)
        node = _custody_node_at(unit, symbol) if unit is not None else None
        if node is None or _digest(node) != digest:
            failures.append("terminal reserve complete source changed " + symbol)
            continue
        selected[symbol] = (path, node)
        receipt["selected_definitions"].add((path, id(node)))
        origin = _module(path) + "." + symbol
        receipt["origins"].add(origin)
        receipt["supplier_bindings"].add(origin)
        receipt["class_origins" if isinstance(node, ast.ClassDef) else "function_origins"].add(origin)
    if failures:
        return failures, {}
    driver_path, driver = selected["_SegmentDriver"]
    failure_path, failure = selected["fail_continuation_tool_operation"]
    if (
        not isinstance(driver, ast.ClassDef)
        or driver.bases
        or driver.keywords
        or driver.decorator_list
        or driver.type_params
        or not isinstance(failure, ast.AsyncFunctionDef)
        or failure.decorator_list
        or failure.type_params
    ):
        return ["terminal reserve original declaration/header changed"], {}
    settle = _find(indexed[driver_path].tree, "_SegmentDriver.settle")
    if not isinstance(settle, ast.AsyncFunctionDef):
        return ["terminal reserve actual settlement method missing"], {}
    projection = _one_call(
        failures,
        receipt,
        driver_path,
        settle,
        "self.work.coordinator.reserve(RequiredWorkSource.TERMINAL_FAILURE_PROJECTION)",
        "terminal reserve original projection ticket changed",
        key="reserve_calls",
    )
    joins = [
        node
        for node in ast.walk(settle)
        if isinstance(node, ast.Try)
        and len(node.body) >= 2
        and _same(node.body[0], "projection = self.work.coordinator.reserve(RequiredWorkSource.TERMINAL_FAILURE_PROJECTION)")
        and _same(
            node.body[1],
            "committed = await self.services.session_service.fail_continuation_tool_operation("
            "self.running, required_work=self.work.coordinator, failure_projection_work=projection, "
            "env_ref_names=names, original=original, custody=custody)",
        )
    ]
    _require(failures, projection is not None and len(joins) == 1, "terminal reserve original projection/caller join changed")
    failure_branches = [node for node in settle.body if isinstance(node, ast.If) and ast.unparse(node.test) == "owns_failure"]
    _require(
        failures,
        sum(
            _same(
                statement,
                "owns_failure = source is not None or (custody is not None and (custody.completed or original is not None))",
            )
            for statement in settle.body
        )
        == 1
        and len(failure_branches) == 1
        and len(joins) == 1
        and joins[0] in failure_branches[0].body,
        "terminal reserve original native/retained failure branch changed",
    )
    guard_positions = []
    for spelling in (
        "if type(required_work) is not RequiredWorkCoordinator or type(failure_projection_work) is not RequiredWorkTicket:\n"
        '    raise AuditIntegrityError("V2 failed terminal requires its actual registered coordinator/projection")',
        "authority = required_work.authority",
        "if (authority.context is not running.session_operation_context\n"
        "    or authority.durable_operation_id != running.claim.operation_id\n"
        "    or authority.claim_attempt != running.claim.attempt or authority.proposal_id is not None):\n"
        '    raise AuditIntegrityError("V2 failed terminal changed its actual original physical owner")',
        "required_work.validate_terminal_failure_work(projection_ticket=failure_projection_work)",
        "timing = required_work.sql_deadline_owner",
        "if timing is None or timing.running is not running:\n"
        '    raise AuditIntegrityError("V2 failed terminal lacks its actual original SQL deadline owner")',
        "timing.validate_context(running.session_operation_context, operation_id=running.claim.operation_id, attempt=running.claim.attempt)",
        "timing.begin_settlement()",
    ):
        positions = [index for index, statement in enumerate(failure.body) if _same(statement, spelling)]
        _require(failures, len(positions) == 1, "terminal reserve original coordinator/context/deadline guard changed")
        if len(positions) == 1:
            guard_positions.append(positions[0])
    loops = [node for node in failure.body if isinstance(node, ast.For) and ast.unparse(node.target) == "slot"]
    _require(
        failures,
        len(loops) == 1 and ast.dump(loops[0].iter) == ast.dump(ast.parse("range(2)", mode="eval").body) and not loops[0].orelse,
        "terminal reserve exact two physical slots changed",
    )
    if len(loops) == 1:
        loop = loops[0]
        _require(
            failures,
            len(guard_positions) == 8
            and guard_positions == list(range(guard_positions[0], guard_positions[0] + 8))
            and guard_positions[-1] < failure.body.index(loop),
            "terminal reserve original owner guards no longer dominate the bounded invocation",
        )
        reserve = _one_call(
            failures,
            receipt,
            failure_path,
            loop,
            "required_work.reserve(RequiredWorkSource.TERMINAL_FAILURE_SQL, sql_attempt_ordinal=slot)",
            "terminal reserve original SQL source/ordinal changed",
            key="reserve_calls",
        )
        _require(
            failures,
            len(loop.body) >= 3
            and isinstance(loop.body[0], ast.Try)
            and len(loop.body[0].body) == 2
            and _same(loop.body[0].body[0], "timing.remaining_statement(session_id=sid)")
            and _same(
                loop.body[0].body[1], "sql = required_work.reserve(RequiredWorkSource.TERMINAL_FAILURE_SQL, sql_attempt_ordinal=slot)"
            )
            and _same(loop.body[1], "physical_ticket = sql")
            and isinstance(loop.body[2], ast.Try)
            and len(loop.body[2].body) == 2
            and _same(loop.body[2].body[0], "actual = await run_required_sql_finish_once(sql, write)")
            and _same(
                loop.body[2].body[1],
                "no_submission = required_work.validate_terminal_failure_sql_outcome("
                "ticket=sql, actual_outcome=actual, expected_sql_attempt_ordinal=slot)",
            )
            and reserve is not None,
            "terminal reserve original deadline/ticket/physical-result join changed",
        )
    actual = {
        (path, id(node))
        for path, owner in ((driver_path, settle), (failure_path, failure))
        for node in ast.walk(owner)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "reserve"
    }
    _require(failures, actual == receipt["reserve_calls"] and len(actual) == 2, "terminal reserve complete original call set changed")
    return _finish(failures, receipt)


# Finite reviewed transfer grammar, independent of the retained digest registry.
# This does not refresh a seal or grant dispatch/SQL permissions.
_ATTACHMENT_PREREQUISITE_BODIES = {
    (CHECKPOINT, "_history"): """def _history(
    conn: Connection,
    running: ComposerOperationRunning,
    selected: SelectedContinuationState,
    compose: ComposeFrame,
    names: frozenset[str],
) -> ContinuationAttachmentHistory:
    return _history_data(conn, running.claim.session_id, running.claim.operation_id, selected, compose, names)""",
    (CHECKPOINT, "_history_data"): """def _history_data(
    conn: Connection,
    session_id: UUID,
    operation_id: str,
    selected: SelectedContinuationState,
    compose: ComposeFrame,
    names: frozenset[str],
) -> ContinuationAttachmentHistory:
    from elspeth.web.sessions.continuation_attachment_application import load_attachment_history_data_on_connection

    if selected.stack.frames[0] is not compose:
        raise AuditIntegrityError("attachment history changed its actual selected Compose owner")
    return load_attachment_history_data_on_connection(
        conn, session_id=session_id, operation_id=operation_id, selected=selected, names=names
    ).history""",
    (
        CHECKPOINT,
        "_effect",
    ): """def _effect(conn: Connection, running: ComposerOperationRunning, selected: SelectedContinuationState, key: str, digest: str) -> Row[Any]:
    return _effect_data(conn, running.claim.session_id, running.claim.operation_id, selected, key, digest)""",
    (CHECKPOINT, "_effect_data"): """def _effect_data(
    conn: Connection, session_id: UUID, operation_id: str, selected: SelectedContinuationState, key: str, digest: str
) -> Row[Any]:
    row = conn.execute(
        select(effects).where(
            effects.c.session_id == str(session_id),
            effects.c.operation_id == operation_id,
            effects.c.effect_key == key,
        )
    ).one_or_none()
    frontier = tuple(item for item in selected.stack.effect_frontier if item.effect_id == key)
    if (
        row is None
        or row.owner != "read"
        or row.input_sha256 != digest
        or row.action_json is not None
        or row.provider_attempt_id is not None
        or row.physical_sequence is not None
        or row.physical_generation is not None
        or row.requested_model is not None
        or row.response_finished_at is not None
        or row.committed_state_ids_json is not None
        or row.graph_sha256 is not None
        or len(frontier) != 1
        or (frontier[0].disposition, frontier[0].receipt_id) != (row.disposition, row.receipt_key)
    ):
        raise AuditIntegrityError("attachment effect lost its actual selected read owner/frontier")
    if any(item.effect_id != key and item.disposition not in ("committed", "refused") for item in selected.stack.effect_frontier):
        raise AuditIntegrityError("attachment owner cannot consume unrelated unresolved work")
    return row""",
    (CHECKPOINT, "verify_attachment_result_source_on_connection"): '''def verify_attachment_result_source_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    snapshot: StoredContinuationSnapshot,
    env_ref_names: frozenset[str],
) -> AttachmentResultCheckpoint:
    """Immutable read proof after the caller's actual current SQL admission.

    This does not authorize filesystem/current use. Historical application
    verification supplies an actual old SQL checkpoint, never a current grant.
    """
    return verify_attachment_result_data_on_connection(
        conn,
        session_id=running.claim.session_id,
        operation_id=running.claim.operation_id,
        selected=selected,
        snapshot=snapshot,
        env_ref_names=env_ref_names,
    )''',
    (CHECKPOINT, "verify_attachment_result_data_on_connection"): '''def verify_attachment_result_data_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    selected: SelectedContinuationState,
    snapshot: StoredContinuationSnapshot,
    env_ref_names: frozenset[str],
) -> AttachmentResultCheckpoint:
    """Original immutable read/result joins after the caller's SQL proof."""
    names = env_ref_names
    if (
        snapshot.checkpoint_id != selected.checkpoint_id
        or snapshot.cursor != selected.stack.cursor
        or snapshot.payload_json != encode_continuation_snapshot(selected.stack)
        or selected.stack.logical.session_id != str(session_id)
        or selected.stack.logical.operation_id != operation_id
    ):
        raise AuditIntegrityError("attachment immutable source changed its complete stored checkpoint")
    compose, frame, prepared, key, digest = _input(selected, names)
    row = _effect_data(conn, session_id, operation_id, selected, key, digest)
    if row.disposition != "committed" or row.result_chunk_key is None:
        raise AuditIntegrityError("attachment result is unresolved and cannot redispatch")
    pending = selected.stack.pending_owner_results
    applied = key in frame.settled_effect_ids
    if applied:
        if frame.phase != "result_retained" or pending or frame.settled_effect_ids.count(key) != 1:
            raise AuditIntegrityError("attachment application lost its exact consumed result cut")
        retained_batch = decode_attachment_history_batch(_chunk(selected, frame.accumulator), env_ref_names=names)
        matches = (encode_private_chunk(codec="composer.owner-result.v1", canonical=retained_batch.result_json, env_ref_names=names),)
    else:
        if frame.phase != "effect_reconciling" or len(pending) != 1 or pending[0].kind != "read" or pending[0].effect_key != key:
            raise AuditIntegrityError("attachment result lost its exact pending owner")
        matches = (_chunk(selected, pending[0].result),)
    if (
        len(matches) != 1
        or matches[0].reference.codec != "composer.owner-result.v1"
        or private_chunk_key(matches[0]) != row.result_chunk_key
    ):
        raise AuditIntegrityError("attachment result changed its selected immutable source")
    chunk = matches[0]
    stored_codec = conn.execute(
        select(chunks.c.codec).where(
            chunks.c.session_id == str(session_id),
            chunks.c.operation_id == operation_id,
            chunks.c.chunk_key == row.result_chunk_key,
        )
    ).scalar_one_or_none()
    if stored_codec != chunk.reference.codec or read_chunk(
        conn, session_id=str(session_id), operation_id=operation_id, key=row.result_chunk_key
    ) != chunk.canonical.encode("utf-8"):
        raise AuditIntegrityError("attachment retained bytes differ from their actual SQL source")
    if row.receipt_key != f"attachment-result:{chunk.reference.digest}":
        raise AuditIntegrityError("attachment result changed its actual retained receipt identity")
    publication = decode_attachment_published_result(chunk, env_ref_names=names)
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == str(session_id),
            attempts.c.operation_id == operation_id,
            attempts.c.sequence == row.creator_sequence,
            attempts.c.generation == row.creator_generation,
        )
    ).one_or_none()
    if creator is None or (
        publication.effect_key,
        publication.input_sha256,
        publication.creator_sequence,
        publication.creator_generation,
        publication.creator_operation_id,
        publication.creator_operation_epoch,
    ) != (key, digest, row.creator_sequence, row.creator_generation, creator.session_operation_id, creator.session_operation_epoch):
        raise AuditIntegrityError("attachment result changed its original creator/selected input")
    read_audit = None if publication.read_audit_json is None else decode_dispatch_audit(publication.read_audit_json, env_ref_names=names)
    if read_audit is not None:
        require_attachment_read_audit(prepared, read_audit, names=names)
    retained = decode_read_owner_result(publication.result_json, env_ref_names=names)
    result = _result(retained, prepared, names)
    history = _history_data(conn, session_id, operation_id, selected, compose, names)
    if applied and _retained_history(history, key, retained) != history:
        raise AuditIntegrityError("consumed attachment result lacks its complete history")
    return AttachmentResultCheckpoint(snapshot, key, prepared, result, retained, history, applied, read_audit)''',
    (PROVIDER_THINKING, "validate_native_thinking_blocks"): '''def validate_native_thinking_blocks(value: object) -> None:
    """Validate the complete closed native grammar without copying its blocks.

    Provider thinking has no documented per-field character ceiling. Its
    grammar has fixed depth, so it needs no generic recursive artifact walk.
    """
    if type(value) not in (list, tuple):
        raise ComposerAdmissionRefused("Provider thinking state is not a complete native block list.")
    for block in cast(list[object] | tuple[object, ...], value):
        if not isinstance(block, Mapping):
            raise ComposerAdmissionRefused("Provider thinking state contains an invalid block.")
        if block.get("type") == "thinking":
            fields = {"type", "thinking", "signature"}
        elif block.get("type") == "redacted_thinking":
            fields = {"type", "data"}
        else:
            raise ComposerAdmissionRefused("Provider thinking state contains an unsupported native block.")
        if set(block) != fields or any(type(block[key]) is not str for key in fields):
            raise ComposerAdmissionRefused("Provider thinking state has incomplete native fields.")
        try:
            for key in fields:
                utf8_size(block[key])
        except UnicodeEncodeError:
            raise ComposerAdmissionRefused("Provider thinking state contains invalid UTF-8 text.") from None
        opaque_key = "signature" if block["type"] == "thinking" else "data"
        if not block[opaque_key]:
            raise ComposerAdmissionRefused("Provider thinking state is missing its opaque replay data.")''',
    (PROVIDER_THINKING, "scan_native_thinking_blocks"): '''def scan_native_thinking_blocks(value: object) -> None:
    """Preserve complete grammar/credential admission before request copying."""
    validate_native_thinking_blocks(value)
    for block in cast(Sequence[Mapping[str, str]], value):
        # Grammar admission fixes each block to two or three string fields.
        # Preserve the detector's exact-dict boundary for accepted Mapping
        # implementations without cloning the complete native block list.
        current = {key: block[key] for key in block}
        # The schema is already closed. Scan complete strings, including token
        # and URL seams, with the same detector rather than a local size cap.
        require_no_credential_material(
            current,
            surface="composer_provider_thinking",
            max_string_bytes=max(utf8_size(text) for text in current.values()),
        )''',
    (PROVIDER_THINKING, "parse_native_thinking_blocks"): '''def parse_native_thinking_blocks(value: object) -> list[dict[str, str]]:
    """Materialize the existing detached native wire after grammar admission."""
    validate_native_thinking_blocks(value)
    return [{key: block[key] for key in block} for block in cast(Sequence[Mapping[str, str]], value)]''',
    (PROVIDER_THINKING, "native_thinking_blocks"): '''def native_thinking_blocks(value: object) -> list[dict[str, str]]:
    """Admit and detach complete native blocks under the existing policy."""
    scan_native_thinking_blocks(value)
    return parse_native_thinking_blocks(value)''',
    (ATTACHMENT_APPLICATION, "require_attachment_projection"): """def require_attachment_projection(
    *,
    before: AdmittedTurnEvidence,
    after: AdmittedTurnEvidence,
    prepared_source: RestoredPreparedBatchData,
    retained_result: ContinuationReadOwnerResult,
    read_audit: PortableDispatchAudit,
    effect_key: str,
    receipt: RetainedToolReceipt,
    response: PrivateChunk,
    env_ref_names: frozenset[str],
) -> ApplicationGraph:
    before_admitted = require_admitted_turn_evidence(before, env_ref_names=env_ref_names)
    after_admitted = require_admitted_turn_evidence(after, env_ref_names=env_ref_names)
    before_snapshot = before_admitted.snapshot
    old = before_admitted.graph
    new = after_admitted.graph
    if old is None or new is None or old.selected_audit is None or old.selected_dialect is None:
        raise AuditIntegrityError("attachment projection lacks its complete original prepared identity graph")
    prepared = RestoredPreparedBatchData(
        old.selected.batch, old.selected.accumulator, old.selected.calls, old.selected_audit, old.selected_dialect
    )
    if encode_restored_prepared_batch_data(prepared, env_ref_names=env_ref_names) != encode_restored_prepared_batch_data(
        prepared_source, env_ref_names=env_ref_names
    ):
        raise AuditIntegrityError("attachment projection changed its actual selected prepared/read source")
    require_attachment_read_audit(prepared, read_audit, names=env_ref_names)
    call = prepared.batch.current_call
    if call is None or call.tool_name not in ("read_attachment", "get_blob_content"):
        raise AuditIntegrityError("attachment projection lost its actual selected read call")
    if decode_continuation_response(response).tool_batch.calls != prepared.calls:
        raise AuditIntegrityError("attachment projection changed its actual complete provider call cohort")
    result = _result(retained_result, prepared, env_ref_names)
    admitted = admit_discovery_result(call.tool_name, result)
    nominal = admitted.to_tool_result()
    invocation = require_receipt_result(
        receipt, selected_call=call.tool_call, pre_state=call.pre_state, result=nominal, env_ref_names=env_ref_names
    )
    audit = read_audit
    if (
        receipt.effect_key != effect_key
        or invocation.status is not ComposerToolStatus.SUCCESS
        or invocation.cache_hit
        or invocation.tool_call_id != audit.tool_call_id
        or invocation.tool_name != audit.tool_name
        or invocation.arguments_canonical != audit.arguments_canonical
        or invocation.arguments_hash != audit.arguments_hash
        or invocation.authority_arguments_canonical != audit.authority_arguments_canonical
        or invocation.authority_arguments_hash != audit.authority_arguments_hash
        or invocation.actor != audit.actor
        or invocation.strict_sent is not audit.strict_sent
        or invocation.wire_conformant is not audit.wire_conformant
        or invocation.started_at != audit.started_at
        or invocation.latency_ms < audit.elapsed_active_ns // 1_000_000
    ):
        raise AuditIntegrityError("attachment projection lost its original completed-read audit/timing")
    if any(row.tool_call_id == call.tool_call.id for row in old.recorder.invocations):
        raise AuditIntegrityError("attachment projection cannot finish an already recorded call")
    authoring = old.selected.batch.private_authorings_by_call[call.tool_call.id]
    if invocation.private_authoring != authoring:
        raise AuditIntegrityError("attachment projection changed its original private authoring")
    invocation = replace(invocation, private_authoring=authoring)
    recorder = replace(old.recorder, invocations=(*old.recorder.invocations, invocation))
    # Reuse the actual outcome/refused-resource producer. This checks the
    # transition; it does not call finish_success or mutate a live runtime.
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
    _append_continuation_tool_outcome(
        batch, call, response=nominal, error_class=None, error_category=None, error_message=None, post_version=nominal.updated_state.version
    )
    stopped = batch.stop_issued_batch or prepared.accumulator.authority_revocation is not None
    batch.next_tool_index += 1
    batch.current_call = None
    batch.stop_issued_batch = stopped
    batch.phase = "dispatch_ready" if stopped or batch.next_tool_index == len(prepared.calls) else "call_ready"
    expected_graph = replace(
        old,
        selected=RestoredBatchData(batch, prepared.accumulator, prepared.calls),
        recorder=recorder,
        selected_audit=None,
        selected_dialect=None,
    )
    recorder_source = admit_application_recorder_source(after.recorder_chunk, env_ref_names=env_ref_names)
    expected_json = encode_application_graph(expected_graph, env_ref_names=env_ref_names, recorder_source=recorder_source)
    if encode_application_graph(new, env_ref_names=env_ref_names, recorder_source=recorder_source) != expected_json:
        raise AuditIntegrityError("attachment projection changed its complete batch/recorder/history identity graph")
    expected = _register_page(before_snapshot, call_id=call.tool_call.id, admitted=admitted)
    message = attachment_result_message(call=call.tool_call, result=nominal)
    shelf = _append_message(
        before_snapshot.transcript,
        wire=family_message_wire(message),
        recipe=AttachmentResultMessageRecipe(response=response.reference, tool_call_id=call.tool_call.id),
    )
    entry = (
        None
        if before_snapshot.application_entry is None
        else before_snapshot.application_entry.model_copy(update={"graph_json": expected_json})
    )
    recorder_chunk = encode_recorder_snapshot(recorder)
    expected = expected.model_copy(
        update={
            "transcript": shelf,
            "recorder": recorder_chunk.reference,
            "joint_graph_json": expected_json if entry is None else None,
            "application_entry": entry,
        }
    )
    expected_admitted = admit_turn_evidence(expected, env_ref_names=env_ref_names, source_chunks=(recorder_chunk, *before.sources.chunks))
    if encode_turn_evidence(after_admitted, env_ref_names=env_ref_names) != encode_turn_evidence(
        expected_admitted, env_ref_names=env_ref_names
    ):
        raise AuditIntegrityError("attachment projection changed original evidence/transcript or actual page registration")
    return new""",
    (ATTACHMENT_APPLICATION, "admit_attachment_application"): '''def admit_attachment_application(
    value: AttachmentApplicationData, *, env_ref_names: frozenset[str], source_chunks: tuple[PrivateChunk, ...] = ()
) -> tuple[ContinuationFrameStack, ContinuationReadOwnerResult]:
    """Check complete historical DATA; current SQL source proof is still owed."""
    from elspeth.contracts.hashing import canonical_json
    from elspeth.web.composer.anti_anchor import AntiAnchorTracker
    from elspeth.web.composer.continuation import ComposeFrame, ToolBatchFrame
    from elspeth.web.composer.continuation_attachment_result_data import (
        AttachmentHistoryBatch,
        decode_attachment_published_result,
        encode_attachment_history_batch,
    )
    from elspeth.web.composer.continuation_batch_data import decode_prepared_batch_data, encode_coupled_batch_data
    from elspeth.web.composer.continuation_caches import decode_continuation_caches
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_private_chunk
    from elspeth.web.composer.continuation_dispatch_audit import decode_dispatch_audit
    from elspeth.web.composer.continuation_evidence import decode_turn_evidence
    from elspeth.web.composer.continuation_iteration import decode_continuation_iteration, encode_continuation_iteration
    from elspeth.web.composer.continuation_tool_receipts import decode_retained_tool_receipt
    from elspeth.web.credential_guard import require_no_credential_material
    from elspeth.web.sessions.continuation_owner_results import decode_read_owner_result

    if type(value) is not AttachmentApplicationData:
        raise TypeError("attachment application requires its exact complete DATA owner")
    encoded = canonical_json(value.model_dump(mode="json"))
    checked = AttachmentApplicationData.model_validate_json(encoded, strict=True)
    if canonical_json(checked.model_dump(mode="json")) != encoded:
        raise AuditIntegrityError("attachment application lost or normalized a field")
    require_no_credential_material(
        (value.effect_key, value.source_checkpoint_id, value.application_operation_id),
        surface="attachment historical application identifiers",
        env_ref_names=env_ref_names,
    )
    root = decode_continuation_snapshot(value.source_snapshot_json)
    if len(root.frames) != 2 or root.cursor != value.source_cursor:
        raise AuditIntegrityError("attachment application lost its exact original root cursor")
    compose, frame = root.frames
    if type(compose) is not ComposeFrame or type(frame) is not ToolBatchFrame or frame.phase != "result_retained":
        raise AuditIntegrityError("attachment application lost its original history-consumed cut")
    if value.effect_key not in frame.settled_effect_ids or root.pending_owner_results:
        raise AuditIntegrityError("attachment application lost its exact consumed read effect")
    response = encode_private_chunk(codec="composer.response.v1", canonical=value.response_json, env_ref_names=env_ref_names)
    if response.reference != frame.response or compose.pending_response != frame.response:
        raise AuditIntegrityError("attachment application changed its exact original response")
    publication = decode_attachment_published_result(
        encode_private_chunk(codec="composer.owner-result.v1", canonical=value.published_result_json, env_ref_names=env_ref_names),
        env_ref_names=env_ref_names,
    )
    if publication.effect_key != value.effect_key or publication.read_audit_json is None:
        raise AuditIntegrityError("attachment application lacks the actual original timed read")
    wrapper = encode_attachment_history_batch(
        AttachmentHistoryBatch(prepared_json=value.prepared_json, result_json=value.published_result_json), env_ref_names=env_ref_names
    )
    if wrapper.reference != frame.accumulator:
        raise AuditIntegrityError("attachment application changed its complete original prepared/read bytes")
    prepared = decode_prepared_batch_data(value.prepared_json, env_ref_names=env_ref_names)
    read_audit = decode_dispatch_audit(publication.read_audit_json, env_ref_names=env_ref_names)
    retained = decode_read_owner_result(publication.result_json, env_ref_names=env_ref_names)
    before = decode_turn_evidence(
        encode_private_chunk(codec="composer.evidence.v2", canonical=value.before_evidence_json, env_ref_names=env_ref_names),
        env_ref_names=env_ref_names,
        source_chunks=source_chunks,
    )
    after = decode_turn_evidence(
        encode_private_chunk(codec="composer.evidence.v2", canonical=value.after_evidence_json, env_ref_names=env_ref_names),
        env_ref_names=env_ref_names,
        source_chunks=source_chunks,
    )
    if before.snapshot.logical != root.logical or (before.snapshot.physical_operation_id, before.snapshot.physical_operation_epoch) != (
        value.application_operation_id,
        value.application_operation_epoch,
    ):
        raise AuditIntegrityError("attachment application changed its logical/current producer attribution")
    receipt = decode_retained_tool_receipt(value.receipt_json, env_ref_names=env_ref_names)
    graph = require_attachment_projection(
        before=before,
        after=after,
        prepared_source=prepared,
        retained_result=retained,
        read_audit=read_audit,
        effect_key=value.effect_key,
        receipt=receipt,
        response=response,
        env_ref_names=env_ref_names,
    )
    if (
        encode_coupled_batch_data(
            batch=graph.selected.batch, accumulator=graph.selected.accumulator, calls=graph.selected.calls, env_ref_names=env_ref_names
        )
        != value.after_batch_json
        or frame.next_tool_index != prepared.batch.next_tool_index
    ):
        raise AuditIntegrityError("attachment application changed its actual complete selected successor")
    old_iteration = decode_continuation_iteration(
        encode_private_chunk(codec="composer.iteration.v1", canonical=value.before_iteration_json, env_ref_names=env_ref_names),
        env_ref_names=env_ref_names,
    )
    new_iteration = decode_continuation_iteration(
        encode_private_chunk(codec="composer.iteration.v1", canonical=value.after_iteration_json, env_ref_names=env_ref_names),
        env_ref_names=env_ref_names,
    )
    tracker = AntiAnchorTracker.for_continuation(old_iteration.anti_anchor)
    completed_result = graph.selected.batch.tool_outcomes[-1].response
    if type(completed_result) is not ToolResult:
        raise AuditIntegrityError("attachment application lost its actual completed nominal ToolResult")
    if not completed_result.success:
        tracker.record_failure(read_audit.tool_name, read_audit.arguments_hash)
    expected_iteration = old_iteration.model_copy(update={"anti_anchor": tracker.snapshot_for_continuation()})
    if encode_continuation_iteration(new_iteration, env_ref_names=env_ref_names) != encode_continuation_iteration(
        expected_iteration, env_ref_names=env_ref_names
    ):
        raise AuditIntegrityError("attachment application changed unrelated iteration/prompt/budget history")
    before_caches = decode_continuation_caches(
        encode_private_chunk(codec="composer.caches.v1", canonical=value.before_caches_json, env_ref_names=env_ref_names),
        env_ref_names=env_ref_names,
    )
    after_caches = decode_continuation_caches(
        encode_private_chunk(codec="composer.caches.v1", canonical=value.after_caches_json, env_ref_names=env_ref_names),
        env_ref_names=env_ref_names,
    )
    if (
        after_caches.session_id != before_caches.session_id
        or after_caches.session_id != root.logical.session_id
        or after_caches.capture_count != before_caches.capture_count + 1
        or after_caches.capture_creators[:-1] != before_caches.capture_creators
        or after_caches.capture_creators[-1].operation_id != value.application_operation_id
        or after_caches.capture_creators[-1].operation_epoch != value.application_operation_epoch
        or after_caches.discovery[: len(before_caches.discovery)] != before_caches.discovery
        or after_caches.preflight[: len(before_caches.preflight)] != before_caches.preflight
        or after_caches.previous_last_preflight_json != (*before_caches.previous_last_preflight_json, before_caches.last_preflight_json)
        or after_caches.previous_last_preflight_identities
        != (*before_caches.previous_last_preflight_identities, before_caches.last_preflight_identity)
    ):
        raise AuditIntegrityError("attachment application discarded actual historical cache values or creator observations")
    return root, retained''',
    (ATTACHMENT_DRIVER, "apply_current_attachment_runtime"): '''async def apply_current_attachment_runtime(
    *,
    runtime: RestoredContinuationRuntime,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    ctx: ToolBatchContext,
    call_model: _CallModelOutcome,
    effect_key: str,
) -> StoredAttachmentApplication:
    """Finish once in the live graph; commit once under the actual selected CAS.

    An uncertain/cancelled commit is reconciled by a new current selected read.
    The mutated old runtime is never an implicit retry cache. Warm callers keep
    their original context/registry/audit; cold callers explicitly readmit only
    the separately retained post-read audit at the original B2 seam.
    """
    if type(runtime) is not RestoredContinuationRuntime or type(ctx) is not ToolBatchContext:
        raise AuditIntegrityError("attachment application requires its actual complete runtime/context")
    work = runtime.bindings.required_work
    if type(work) is not RequiredWorkBinding:
        raise AuditIntegrityError("attachment application lost its actual required work")
    authority, service = runtime.materialization.authority, runtime.materialization.service
    running = _owners(authority, inventory, work)
    compose = tuple(frame for frame in selected.stack.frames if type(frame) is ComposeFrame)
    if len(compose) != 1:
        raise AuditIntegrityError("attachment application lost its actual Compose owner")
    # Warm publication advances the selected root while retaining the original
    # live B2 objects. Read the supplied current history through its SQL owner;
    # an old runtime's historical DATA is not a current/already-applied grant.
    current_source = await _registered_read(
        work,
        ContinuationRuntimeSource,
        lambda: authority.read_continuation_runtime(running, selected=selected, inventory=inventory),
    )
    if current_source.selected is not selected:
        raise AuditIntegrityError("attachment history read changed its actual selected source")
    history = current_source.attachments
    if any(value.effect_key == effect_key for value in history.applications):
        # Only the actual current SQL verifier, not the decoded history branch,
        # can return the already-applied disposition.
        return await read_current_applied_attachment(runtime=runtime, selected=selected, inventory=inventory, effect_key=effect_key)
    frame = selected.stack.frames[-1]
    if type(frame) is not ToolBatchFrame or effect_key != f"attachment:{frame.response.digest}:{frame.next_tool_index}":
        raise AuditIntegrityError("attachment application selected another exact result identity")
    completed = await finish_current_attachment_result(
        service=service,
        authority=authority,
        selected=selected,
        inventory=inventory,
        required_work=work,
        ctx=ctx,
        call_model=call_model,
        restored=runtime.graph,
    )
    if completed.source.effect_key != effect_key:
        raise AuditIntegrityError("attachment application changed the exact requested result identity")
    captured = await capture_continuation_runtime_owners(runtime, selected=selected, inventory=inventory)
    sql, projection = work.reserve_pair(RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(
            sql,
            authority.apply_attachment_application,
            running,
            selected=selected,
            inventory=inventory,
            projection=completed,
            captured=captured,
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    result = _project(work, sql, projection, RequiredWorkSource.CONTINUATION_STATE_SQL, outcome)
    if type(result) is not StoredAttachmentApplication:
        raise AuditIntegrityError("attachment application lost its actual atomic returned owner")
    return result''',
    (FINISHER, "finish_current_attachment_result"): """async def finish_current_attachment_result(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    required_work: RequiredWorkBinding,
    ctx: ToolBatchContext,
    call_model: _CallModelOutcome,
    restored: RestoredTurnEvidence,
) -> CompletedAttachmentProjection:
    if type(ctx) is not ToolBatchContext or type(restored) is not RestoredTurnEvidence:
        raise AuditIntegrityError("attachment finishing requires its actual current runtime owners")
    _require_current_reader_service(service, authority)
    if (
        required_work.running is not inventory.running
        or ctx.session_operation_context is not inventory.running.session_operation_context
        or ctx.custody.session_engine is not authority._engine
        or ctx.turn_sessions_service is not service._require_sessions_service()
        or ctx.turn_evidence is not restored.evidence
        or ctx.recorder is not restored.recorder
        or ctx.message_producers is None
        or ctx.message_producers.service is not service
        or ctx.message_producers.authority is not authority
        or ctx.message_producers.inventory.running is not inventory.running
        or ctx.message_producers.inventory.names != inventory.names
        or ctx.message_producers.selected.stack.logical != selected.stack.logical
        or ctx.message_producers.selected.stack.mutation_ceiling != selected.stack.mutation_ceiling
        or ctx.message_producers.selected.stack.policy_hash != selected.stack.policy_hash
        or ctx.user_id != restored.evidence._continuation_actor_user_id
        or ctx.session_id != restored.evidence.session_id
        or ctx.session_id != str(inventory.running.claim.session_id)
    ):
        raise AuditIntegrityError("attachment finishing crossed current runtime/custody/evidence owners")
    data = restored.batch
    if data is None or restored.selected_audit is None or restored.selected_dialect is None:
        raise AuditIntegrityError("attachment finishing lost its complete shared prepared audit graph")
    prepared = RestoredPreparedBatchData(data.batch, data.accumulator, data.calls, restored.selected_audit, restored.selected_dialect)
    if ctx.actor != prepared.audit.actor or ctx.tool_contract_dialect is not prepared.dialect:
        raise AuditIntegrityError("attachment finishing changed its actual current actor/dialect")
    call = data.batch.current_call
    if call is None or call.phase != "prepared" or call.tool_name not in ("read_attachment", "get_blob_content"):
        raise AuditIntegrityError("attachment finishing has no exact unconsumed prepared call")
    frame = selected.stack.frames[-1]
    if type(frame) is not ToolBatchFrame or frame.phase != "result_retained" or frame.next_tool_index != call.index:
        raise AuditIntegrityError("attachment finishing selected another actual call/cursor")
    response = encode_continuation_response(call_model.completion, audit_call=call_model.audit_call)
    if response.reference != frame.response or call_model.completion.tool_batch.calls != data.calls:
        raise AuditIntegrityError("attachment finishing changed its complete original provider response")
    ctx.message_producers.require_context(session_id=ctx.session_id, context=ctx.session_operation_context, actor_user_id=ctx.user_id)
    from elspeth.web.composer.continuation_runtime import _registered_read
    from elspeth.web.sessions.continuation_runtime import ContinuationRuntimeSource

    # The old warm registry retains original message identities; current scope
    # comes from this complete latest SQL source read, never registry rebinding.
    await _registered_read(
        required_work,
        ContinuationRuntimeSource,
        lambda: authority.read_continuation_runtime(inventory.running, selected=selected, inventory=inventory),
    )
    before = capture_turn_evidence(
        restored.evidence,
        restored.messages,
        logical=selected.stack.logical,
        recorder=restored.recorder,
        drained=restored.drained,
        env_ref_names=inventory.names,
        data=prepared,
        materialization=ctx.message_producers,
    )
    before_graph = require_admitted_turn_evidence(before, env_ref_names=inventory.names).graph
    if before_graph is None or before_graph.selected_audit is None or before_graph.selected_dialect is None:
        raise AuditIntegrityError("attachment finishing lost its complete original prepared capture")
    selected_json = encode_restored_prepared_batch_data(
        RestoredPreparedBatchData(
            before_graph.selected.batch,
            before_graph.selected.accumulator,
            before_graph.selected.calls,
            before_graph.selected_audit,
            before_graph.selected_dialect,
        ),
        env_ref_names=inventory.names,
    )
    current = await read_attachment_result(authority=authority, selected=selected, inventory=inventory, required_work=required_work)
    if not current.history_applied or encode_restored_prepared_batch_data(current.prepared, env_ref_names=inventory.names) != selected_json:
        raise AuditIntegrityError("attachment finishing differs from its complete current SQL source graph")
    if current.read_audit is None:
        raise AuditIntegrityError("historical attachment result lacks its actual completed-read timing observation")
    require_attachment_read_audit(prepared, current.read_audit, names=inventory.names)
    # Re-admit the exact nonmutating result against this real shared pre-state;
    # no equality-based merging of the separately loaded graph is performed.
    result = _result(current.retained, prepared, inventory.names)
    bound_source = replace(current, prepared=prepared, result=result)
    if call.audit is None:
        if call.audit_arguments is None:
            raise AuditIntegrityError("attachment finishing lost its actual audit argument owner")
        call.audit = re_admit_dispatch_audit(
            current.read_audit,
            selected_call=call.tool_call,
            pre_state=call.pre_state,
            audit_arguments=call.audit_arguments,
            actor=ctx.actor,
            strict_sent=call.wire.strict_sent,
            wire_conformant=call.wire.wire_conformant,
            env_ref_names=inventory.names,
        )
    else:
        observed = capture_dispatch_audit(call.audit, env_ref_names=inventory.names)
        require_attachment_read_audit(prepared, observed, names=inventory.names)
        if observed.elapsed_active_ns < current.read_audit.elapsed_active_ns:
            raise AuditIntegrityError("live attachment timer predates its actual retained read completion")
    # Existing live audits retain their original object/timer. Complete prepared
    # admission above checked their actor/arguments/start binding independently.
    original_invocations = restored.recorder.invocations
    original_count = len(data.batch.tool_outcomes)
    await _execute_continuation_call_live(
        call_model=call_model,
        ctx=ctx,
        acc=data.accumulator,
        llm_messages=restored.messages,
        batch=data.batch,
        call=call,
        retained_attachment=bound_source,
    )
    invocations = restored.recorder.invocations
    if (
        len(invocations) != len(original_invocations) + 1
        or any(old is not new for old, new in zip(original_invocations, invocations, strict=False))
        or len(data.batch.tool_outcomes) != original_count + 1
    ):
        raise AuditIntegrityError("attachment finisher lost its exact one original invocation/outcome")
    outcome = data.batch.tool_outcomes[-1]
    completed_result = outcome.response
    if type(completed_result) is not ToolResult:
        raise AuditIntegrityError("attachment finisher lost its actual completed nominal ToolResult")
    receipt = capture_tool_receipt(
        effect_key=current.effect_key,
        invocation=invocations[-1],
        result=completed_result,
        pre_state=call.pre_state,
        env_ref_names=inventory.names,
    )
    require_receipt_result(
        receipt, selected_call=call.tool_call, pre_state=call.pre_state, result=completed_result, env_ref_names=inventory.names
    )
    data.batch.next_tool_index += 1
    data.batch.current_call = None
    data.batch.phase = "dispatch_ready" if data.batch.stop_issued_batch or data.batch.next_tool_index == len(data.calls) else "call_ready"
    after = capture_turn_evidence(
        restored.evidence,
        restored.messages,
        logical=selected.stack.logical,
        recorder=restored.recorder,
        drained=restored.drained,
        env_ref_names=inventory.names,
        data=data,
        materialization=ctx.message_producers,
    )
    from elspeth.web.composer.continuation_attachment_application import require_attachment_projection

    require_attachment_projection(
        before=before,
        after=after,
        prepared_source=current.prepared,
        retained_result=current.retained,
        read_audit=current.read_audit,
        effect_key=current.effect_key,
        receipt=receipt,
        response=response,
        env_ref_names=inventory.names,
    )
    return CompletedAttachmentProjection(current, before, after, data, receipt)""",
    (
        "src/elspeth/web/sessions/continuation_attachment_application.py",
        "load_attachment_history_data_on_connection",
    ): '''def load_attachment_history_data_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    selected: SelectedContinuationState,
    names: frozenset[str],
) -> AttachmentHistorySources:
    """Closed immutable dependencies; this identity grants no filesystem use."""
    from elspeth.web.sessions.continuation_p4_application_store import _private_chunk_for_operation_on_connection

    compose = tuple(frame for frame in selected.stack.frames if type(frame) is ComposeFrame)
    if len(compose) != 1:
        raise AuditIntegrityError("attachment history lost its exact selected Compose owner")
    actual = _chunk(selected, compose[0].attachment_admissions)
    if _text_data(conn, session_id, operation_id, private_chunk_key(actual)) != actual.canonical:
        raise AuditIntegrityError("attachment history differs from its original SQL bytes")
    sid, operation = str(session_id), operation_id
    fields = decode_attachment_history_fields(actual, session_id=sid, operation_id=operation, env_ref_names=names)
    # Preserve every occurrence and its physical work. There is no new ticket,
    # budget reset or reference deduplication before a conflicting length check.
    chunks = tuple(
        _private_chunk_for_operation_on_connection(conn, session_id, operation_id, ref) for ref in attachment_history_dependencies(fields)
    )
    history = decode_attachment_history(actual, session_id=sid, operation_id=operation, env_ref_names=names, source_chunks=chunks)
    return AttachmentHistorySources(history, chunks)''',
}


def attachment_prerequisite_transfers(units):
    """Check reviewed moved obligations without admitting retained source seals.

    This returns checked definition identities only. It cannot grant a native
    call, SQL effect, callback or arbitrary additional caller permission.
    """
    index = {unit.path: unit for unit in units}
    required = {path for path, _ in _ATTACHMENT_PREREQUISITE_BODIES}
    if len(index) != len(units) or not required <= index.keys():
        return ["attachment prerequisite source set missing or duplicated"], {}
    checked = set()
    failures = []
    for (path, name), spelling in _ATTACHMENT_PREREQUISITE_BODIES.items():
        owner = _custody_node_at(index[path], name)
        expected = ast.parse(spelling).body[0]
        if (
            not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef))
            or stable_ast_dump(owner) != stable_ast_dump(expected)
            or not receiver.owned_binding(index[path].tree, name, owner)
        ):
            failures.append("attachment prerequisite exact transfer changed " + path + ":" + name)
            continue
        checked.add((path, id(owner)))
    if failures:
        return failures, {}

    # Names are concrete suppliers of the reviewed transfers, not a caller
    # whitelist. Local imports are checked in their actual function body.
    imports = (
        (PROVIDER_THINKING, None, "utf8_size", "elspeth.contracts.bounded_serialization"),
        (PROVIDER_THINKING, None, "require_no_credential_material", "elspeth.web.credential_guard"),
        (PROVIDER_THINKING, None, "ComposerAdmissionRefused", "elspeth.web.composer.protocol"),
        (PROVIDER_THINKING, None, "Mapping", "collections.abc"),
        (PROVIDER_THINKING, None, "Sequence", "collections.abc"),
        (PROVIDER_THINKING, None, "cast", "typing"),
        (
            CHECKPOINT,
            "_history_data",
            "load_attachment_history_data_on_connection",
            "elspeth.web.sessions.continuation_attachment_application",
        ),
        (CHECKPOINT, None, "read_chunk", "elspeth.web.sessions.continuation"),
        (CHECKPOINT, None, "private_chunk_key", "elspeth.web.sessions.continuation"),
        (CHECKPOINT, None, "decode_attachment_published_result", "elspeth.web.composer.continuation_attachment_result_data"),
        (CHECKPOINT, None, "decode_read_owner_result", "elspeth.web.sessions.continuation_owner_results"),
        (CHECKPOINT, None, "decode_dispatch_audit", "elspeth.web.composer.continuation_dispatch_audit"),
        (ATTACHMENT_DRIVER, None, "_registered_read", "elspeth.web.composer.continuation_runtime"),
        (ATTACHMENT_DRIVER, None, "ContinuationRuntimeSource", "elspeth.web.sessions.continuation_runtime"),
        (ATTACHMENT_APPLICATION, None, "require_admitted_turn_evidence", "elspeth.web.composer.continuation_evidence"),
        (ATTACHMENT_APPLICATION, None, "admit_turn_evidence", "elspeth.web.composer.continuation_evidence"),
        (ATTACHMENT_APPLICATION, None, "encode_turn_evidence", "elspeth.web.composer.continuation_evidence"),
        (ATTACHMENT_APPLICATION, "admit_attachment_application", "decode_turn_evidence", "elspeth.web.composer.continuation_evidence"),
        (ATTACHMENT_APPLICATION, None, "encode_recorder_snapshot", "elspeth.web.composer.continuation_audit_buffer"),
        (FINISHER, None, "require_admitted_turn_evidence", "elspeth.web.composer.continuation_evidence"),
        (FINISHER, None, "capture_turn_evidence", "elspeth.web.composer.continuation_evidence_graph"),
        (FINISHER, "finish_current_attachment_result", "_registered_read", "elspeth.web.composer.continuation_runtime"),
        (FINISHER, "finish_current_attachment_result", "ContinuationRuntimeSource", "elspeth.web.sessions.continuation_runtime"),
    )
    for path, symbol, name, module in imports:
        scope = index[path].tree
        if symbol is not None:
            owner = _custody_node_at(index[path], symbol)
            scope = ast.Module(body=owner.body, type_ignores=[])
        if not receiver.imported(scope, name, module, name):
            failures.append("attachment prerequisite supplier binding changed " + path + ":" + name)
            continue
        if module.startswith("elspeth."):
            supplier_path = "src/" + module.replace(".", "/") + ".py"
            if supplier_path not in index:
                failures.append("attachment prerequisite supplier source missing " + supplier_path)
                continue
            supplier = _custody_node_at(index[supplier_path], name)
            if not isinstance(supplier, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) or not receiver.owned_binding(
                index[supplier_path].tree, name, supplier
            ):
                failures.append("attachment prerequisite supplier definition changed " + supplier_path + ":" + name)
    # The physical SQL read contract was already retained; preserve that exact
    # existing binding rather than inventing an unchecked adapter permission.
    runtime = index[RUNTIME] if RUNTIME in index else None
    read = _custody_node_at(runtime, "_registered_read") if runtime is not None else None
    if read is None or _digest(read) != _CONTRACTS[RUNTIME]["_registered_read"]:
        failures.append("attachment prerequisite registered SQL reader changed")
    if failures:
        return failures, {}
    return [], {"checked_definitions": frozenset(checked)}


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
    failures, custody = p4_batch_custody_receipt(units)
    if failures:
        return failures, {}
    failures, _ = attachment_prerequisite_transfers(units)
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
        == ("call_model", "ctx", "acc", "llm_messages", "batch", "call", "retained_attachment", "failure_custody")
        and args.kw_defaults[:6] == [None] * 6
        and all(isinstance(default, ast.Constant) and default.value is None for default in args.kw_defaults[6:]),
        "continuation attachment exact optional formal/default changed",
    )
    retained_defaults = [
        default
        for parameter, default in zip(args.kwonlyargs, args.kw_defaults, strict=True)
        if parameter.arg == "retained_attachment" and default is not None
    ]
    receipt["retained_defaults"].update((BATCH, id(default)) for default in retained_defaults)
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
    expected_native.add(custody["execute_reference"])
    _require(
        failures,
        len(ordinary) == 1 and _references(units, {"_execute_continuation_call_live"}) == expected_native,
        "continuation attachment native reference inventory changed",
    )
    if native is not None:
        receipt["native_uses"].add((FINISHER, id(native.func)))
    receipt["native_uses"].add(custody["execute_reference"])
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

    admitted_prefix = (
        "before_admitted = require_admitted_turn_evidence(before, env_ref_names=env_ref_names)",
        "after_admitted = require_admitted_turn_evidence(after, env_ref_names=env_ref_names)",
        "before_snapshot = before_admitted.snapshot",
        "old = before_admitted.graph",
        "new = after_admitted.graph",
    )
    _require(
        failures,
        len(checker.body) >= len(admitted_prefix)
        and all(_same(node, source) for node, source in zip(checker.body[: len(admitted_prefix)], admitted_prefix, strict=True)),
        "continuation attachment projection actual admitted owner prefix changed",
    )
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
        ("before_admitted", admitted_prefix[0]),
        ("after_admitted", admitted_prefix[1]),
        ("before_snapshot", admitted_prefix[2]),
        ("old", admitted_prefix[3]),
        ("new", admitted_prefix[4]),
        (
            "expected_admitted",
            "expected_admitted = admit_turn_evidence(expected, env_ref_names=env_ref_names, source_chunks=(recorder_chunk, *before.sources.chunks))",
        ),
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
        ("recorder_source", "recorder_source = admit_application_recorder_source(after.recorder_chunk, env_ref_names=env_ref_names)"),
        (
            "expected_json",
            "expected_json = encode_application_graph(expected_graph, env_ref_names=env_ref_names, recorder_source=recorder_source)",
        ),
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
            """if encode_application_graph(new, env_ref_names=env_ref_names, recorder_source=recorder_source) != expected_json:
                raise AuditIntegrityError("attachment projection changed its complete batch/recorder/history identity graph")""",
            "complete successor graph comparison changed",
        ),
        (
            """if encode_turn_evidence(after_admitted, env_ref_names=env_ref_names) != encode_turn_evidence(expected_admitted, env_ref_names=env_ref_names):
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
        ("admit_application_recorder_source", "elspeth.web.composer.continuation_application_graph", "admit_application_recorder_source"),
        ("encode_turn_evidence", "elspeth.web.composer.continuation_evidence", "encode_turn_evidence"),
        ("require_admitted_turn_evidence", "elspeth.web.composer.continuation_evidence", "require_admitted_turn_evidence"),
        ("admit_turn_evidence", "elspeth.web.composer.continuation_evidence", "admit_turn_evidence"),
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


def closed_source_record_transfers(units):
    """Only the seven reviewed local allocations and shallow record successors."""
    index = {unit.path: unit for unit in units}
    if len(index) != len(units) or any(path not in index for path in (*RECORD_PATHS.values(), *RECORD_CALLERS, BATCH)):
        return ["source record source set missing or duplicated"], {}
    failures, receipt = [], _empty()
    for path, callers in RECORD_CALLERS.items():
        for symbol, key in callers.items():
            actual = _custody_node_at(index[path], symbol)
            expected = ast.parse(OWNER_TEMPLATES[key]).body[0]
            _require(
                failures, actual is not None and _digest(actual) == _digest(expected), "source record original caller changed " + symbol
            )
    successors = {
        "graph.RestoredTurnEvidence": {
            "with_batch": {"batch": "batch"},
            "with_drained": {"drained": "drained"},
            "with_batch_and_drained": {"batch": "batch", "drained": "drained"},
            "without_selected_dispatch": {"selected_audit": "None", "selected_dialect": "None"},
            "with_selected_dispatch": {"selected_audit": "audit", "selected_dialect": "dialect"},
        },
        "producers._FeedbackMessageProvenance": {"with_outcome": {"outcome": "outcome"}},
    }
    for key in CLOSED_SOURCE_RECORDS:
        group, name = key.split(".", 1)
        path = RECORD_PATHS[group]
        unit = index[path]
        cls = _find(unit.tree, name)
        expected = ast.parse(OWNER_TEMPLATES[key]).body[0]
        prefix = "source record " + name + " "
        if not isinstance(cls, ast.ClassDef) or _digest(cls) != _digest(expected) or not receiver.owned_binding(unit.tree, name, cls):
            failures.append(prefix + "complete declaration/binding changed")
            continue
        _require(
            failures, not cls.bases and not cls.keywords and not cls.decorator_list and not cls.type_params, prefix + "plain header changed"
        )
        _require(
            failures,
            receiver.imported(unit.tree, "FrozenInstanceError", "dataclasses", "FrozenInstanceError"),
            prefix + "write-refusal supplier changed",
        )
        for builtin in ("object", "type", "NotImplemented"):
            _require(failures, receiver.builtin_binding(unit.tree, builtin), prefix + "builtin supplier changed " + builtin)
        methods = {node.name: node for node in cls.body if isinstance(node, ast.FunctionDef)}
        if not {"__new__", "__init__", "__eq__", "__setattr__", "__delattr__"} <= methods.keys():
            failures.append(prefix + "own methods missing")
            continue
        init, new = methods["__init__"], methods["__new__"]
        fields = SOURCE_RECORD_FIELDS[key]
        for method, receiver_name in ((init, "self"), (new, "cls")):
            args = method.args
            _require(
                failures,
                not args.posonlyargs
                and tuple(arg.arg for arg in args.args) == (receiver_name, *fields)
                and not args.kwonlyargs
                and args.vararg is None
                and args.kwarg is None
                and len(args.defaults) == (4 if group == "graph" else 0)
                and all(isinstance(value, ast.Constant) and value.value is None for value in args.defaults),
                prefix + "exact constructor arguments/defaults changed",
            )
        slots = [
            node
            for node in cls.body
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__slots__" for target in node.targets)
        ]
        slot_node = slots[0].value if len(slots) == 1 else None
        slot_values = (
            tuple(node.value for node in slot_node.elts)
            if isinstance(slot_node, ast.Tuple)
            and all(isinstance(node, ast.Constant) and type(node.value) is str for node in slot_node.elts)
            else ()
        )
        _require(
            failures,
            type(slot_values) is tuple and len(slot_values) == len(fields) + 1 and set(slot_values) == {*fields, "_sealed"},
            prefix + "exact direct slots changed",
        )
        allocation = ast.parse('owner = object.__new__(cls)\nobject.__setattr__(owner, "_sealed", False)\nreturn owner').body
        _require(
            failures, [_digest(node) for node in new.body] == [_digest(node) for node in allocation], prefix + "fresh allocation changed"
        )
        guard = init.body[0] if init.body else None
        sealed = (
            isinstance(guard, ast.If)
            and _digest(guard.test) == _digest(ast.parse("self._sealed", mode="eval").body)
            and not guard.orelse
            and len(guard.body) == 1
            and isinstance(guard.body[0], ast.Raise)
            and isinstance(guard.body[0].exc, ast.Call)
            and isinstance(guard.body[0].exc.func, ast.Name)
            and guard.body[0].exc.func.id == "FrozenInstanceError"
            and len(guard.body[0].exc.args) == 1
            and isinstance(guard.body[0].exc.args[0], ast.Constant)
            and type(guard.body[0].exc.args[0].value) is str
            and not guard.body[0].exc.keywords
        )
        _require(failures, sealed, prefix + "reinitialization guard changed")
        writes = ast.parse(
            "\n".join(f"object.__setattr__(self, {field!r}, {field})" for field in fields) + '\nobject.__setattr__(self, "_sealed", True)'
        ).body
        _require(
            failures,
            [_digest(node) for node in init.body[1:]] == [_digest(node) for node in writes],
            prefix + "exact reference initialization changed",
        )
        for method_name in ("__setattr__", "__delattr__"):
            body = methods[method_name].body
            _require(
                failures,
                len(body) == 1
                and isinstance(body[0], ast.Raise)
                and isinstance(body[0].exc, ast.Call)
                and isinstance(body[0].exc.func, ast.Name)
                and body[0].exc.func.id == "FrozenInstanceError"
                and len(body[0].exc.args) == 1
                and isinstance(body[0].exc.args[0], ast.Constant)
                and type(body[0].exc.args[0].value) is str
                and not body[0].exc.keywords,
                prefix + "ordinary write refusal changed",
            )
        left = "(" + ", ".join("self." + field for field in fields) + ",)"
        right = "(" + ", ".join("other." + field for field in fields) + ",)"
        equality = ast.parse(
            f"if type(self) is not {name} or type(other) is not {name}:\n    return NotImplemented\nreturn {left} == {right}"
        ).body
        _require(
            failures,
            [_digest(node) for node in methods["__eq__"].body] == [_digest(node) for node in equality],
            prefix + "exact nominal field equality changed",
        )
        for method_name, changes in successors.get(key, {}).items():
            method = methods.get(method_name)
            arguments = ", ".join(changes.get(field, "self." + field) for field in fields)
            _require(
                failures,
                method is not None and len(method.body) == 1 and _same(method.body[0], f"return {name}({arguments})"),
                prefix + "shallow successor changed " + method_name,
            )
            receipt["receiver_members"].setdefault(_module(path) + "." + name, set()).add(method_name)
        receipt["selected_definitions"].add((path, id(cls)))
        origin = _module(path) + "." + name
        receipt["origins"].add(origin)
        receipt["class_origins"].add(origin)
        receipt["supplier_bindings"].add(origin)
        receipt["origins"].update({"builtins.object", "builtins.type", "builtins.NotImplemented", "dataclasses.FrozenInstanceError"})
        for method_name in ("__new__", "__init__", *successors.get(key, {})):
            for node in ast.walk(methods[method_name]):
                if isinstance(node, ast.Call) and (
                    ast.unparse(node.func) in {"object.__new__", "object.__setattr__"}
                    or (isinstance(node.func, ast.Name) and node.func.id == name)
                ):
                    receipt["constructor_calls"].add((path, id(node)))
                    receipt["data_constructor_uses"].add((path, id(node.func)))
        for node in ast.walk(cls):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == name:
                receipt["class_uses"].add((path, id(node)))
    ordinary = (
        (
            BATCH,
            "_execute_continuation_call_live",
            "ContinuationToolMessagePublication(ctx.message_producers, call_model.completion, batch, call, recorder, llm_messages, ctx.policy_catalog)",
        ),
        (
            EVIDENCE_GRAPH,
            "_finish_current_evidence",
            "RestoredTurnEvidence(owner, messages, recorder, drained, graph.selected, snapshot.application_entry, graph.selected_audit, graph.selected_dialect)",
        ),
        (EVIDENCE_GRAPH, "_finish_current_evidence", "RestoredTurnEvidence(owner, messages, recorder, drained)"),
        (
            RECORD_PATHS["families"],
            "_require_tool_result_source_on_connection",
            "_ToolResultFamilySource(recipe, call, outcome.response, message)",
        ),
        (
            RECORD_PATHS["families"],
            "_require_tool_feedback_source_on_connection",
            "_ToolFeedbackFamilySource(recipe, call, outcome, invocation, message)",
        ),
        (RECORD_PATHS["families"], "CurrentContinuationMessageFamilies._restore_cohort", "_RestoredFamilyCohort((), (), ())"),
        (
            RECORD_PATHS["families"],
            "CurrentContinuationMessageFamilies._restore_cohort",
            "_RestoredFamilyCohort(tuple(messages), tuple(feedback), before.tool_feedback)",
        ),
        (PRODUCERS, "_produce_current_messages", "CurrentMessageProduction(owner, tuple(owner.initial_messages()) if initial else None)"),
        (
            PRODUCERS,
            "_produce_current_messages",
            "CurrentMessageProduction(owner, tuple(messages[index] for index in retained_shelf.current_indexes))",
        ),
        (
            PRODUCERS,
            "CurrentContinuationMessageProducers._remember_feedback_provenance",
            "_FeedbackMessageProvenance(message, recipe, tuple(message), message['role'], message['tool_call_id'], message['content'], (recipe.response.codec, recipe.response.digest, recipe.response.byte_length, recipe.tool_index, recipe.tool_call_id, recipe.source), call, invocation, authoring, outcome)",
        ),
    )
    for path, symbol, expression in ordinary:
        owner = _custody_node_at(index[path], symbol)
        expected = ast.parse(expression, mode="eval").body
        found = (
            [node for node in ast.walk(owner) if isinstance(node, ast.Call) and _digest(node) == _digest(expected)]
            if owner is not None
            else []
        )
        _require(failures, len(found) == 1, "source record original construction changed " + symbol)
        if len(found) != 1:
            continue
        name = expected.func.id
        events = [(kind, node) for bound, kind, node in receiver.lexical_binding_events(owner) if bound in {name, "*"}]
        if path == BATCH:
            _require(
                failures,
                receiver.imported(ast.Module(body=owner.body, type_ignores=[]), name, _module(PRODUCERS), name),
                "source record original publication import changed",
            )
            _require(failures, len(events) == 1 and isinstance(events[0][1], ast.ImportFrom), "source record publication binding changed")
        else:
            _require(failures, not events, "source record original constructor shadowed " + symbol)
        receipt["constructor_calls"].add((path, id(found[0])))
        receipt["data_constructor_uses"].add((path, id(found[0].func)))
    return _finish(failures, receipt)


def continuation_local_transfers(units):
    """Join the finite receipts without authorizing any new evaluator rule."""
    failures, joined = [], _empty()
    for check in (iteration_local_transfers, attachment_local_transfers, initialization_local_transfers, closed_source_record_transfers):
        current_failures, receipt = check(units)
        failures.extend(current_failures)
        if current_failures:
            continue
        for key in (*SITE_KEYS, "origins", "function_origins", "class_origins", "supplier_bindings"):
            joined[key].update(receipt[key])
        for owner, members in receipt["receiver_members"].items():
            joined["receiver_members"].setdefault(owner, set()).update(members)
    return _finish(failures, joined)
