"""Build the 'current deployment capabilities' record with the real code path.

Reproduces the audited instance configuration (evals/composer-live-audit/instance.sh,
broad allowlist + 'standard' openrouter profile), builds the PluginAvailabilitySnapshot via
build_plugin_snapshot, then calls advisor_abandonment.build_abandonment_review and extracts
the 'current deployment capabilities' record verbatim. Writes record_<mode>.json.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from pydantic import SecretBytes

import elspeth
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.composer.advisor_abandonment import build_abandonment_review
from elspeth.web.composer.state import CompositionState
from elspeth.web.config import WebSettings
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.availability import build_plugin_snapshot
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig

OUT = Path(__file__).resolve().parent
BROAD = [
    "source:blob_rows",
    "source:null",
    "source:llm",
    "transform:batch_classifier_metrics",
    "transform:batch_data_quality_report",
    "transform:batch_distribution_profile",
    "transform:batch_drift_compare",
    "transform:batch_duplicate_annotator",
    "transform:batch_effect_size",
    "transform:batch_experiment_compare",
    "transform:batch_outlier_annotator",
    "transform:batch_paired_preference",
    "transform:batch_rank",
    "transform:batch_replicate",
    "transform:batch_stats",
    "transform:batch_threshold_summary",
    "transform:batch_top_k",
    "transform:blob_csv_expand",
    "transform:blob_fetch",
    "transform:blob_json_expand",
    "transform:blob_text_expand",
    "transform:json_explode",
    "transform:keyword_filter",
    "transform:line_explode",
    "transform:passthrough",
    "transform:reference_join",
    "transform:report_assemble",
    "transform:truncate",
    "transform:type_coerce",
    "transform:value_transform",
    "sink:document",
]


class _Inventory:
    def has_server_ref(self, name: str) -> bool:
        return name == "OPENROUTER_API_KEY"

    def has_user_ref(self, principal: str, name: str) -> bool:
        return False

    def has_ref(self, principal: str, name: str) -> bool:
        return self.has_server_ref(name)

    def server_generation(self, name: str) -> str | None:
        return "gen" if self.has_server_ref(name) else None

    def user_generation(self, principal: str, name: str) -> str | None:
        return None


def main(mode: str) -> None:
    print("elspeth from", elspeth.__file__, file=sys.stderr)
    kwargs = {
        "data_dir": Path(tempfile.mkdtemp(dir=OUT)),
        "composer_model": "openrouter/deepseek/deepseek-v4.1-flash",
        "composer_max_composition_turns": 10,
        "composer_max_discovery_turns": 24,
        "composer_timeout_seconds": 240.0,
        "composer_rate_limit_per_minute": 10,
        "secret_key": "s1-spike-throwaway-secret-key-0123456789abcdef",
        "shareable_link_signing_key": SecretBytes(__import__("os").urandom(32)),
        "llm_profiles": {
            "standard": {
                "provider": "openrouter",
                "model": "deepseek/deepseek-v4.1-flash",
                "credential_scope": "server",
                "credential_ref": "OPENROUTER_API_KEY",
            }
        },
        "default_llm_profile": "standard",
    }
    if mode == "broad":
        kwargs["plugin_allowlist"] = tuple(BROAD)
    settings = WebSettings(**kwargs)
    runtime = RuntimeWebPluginConfig.from_settings(settings)
    policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime)
    profiles = OperatorProfileRegistry(policy=policy, settings=runtime)
    catalog = create_catalog_service()
    snapshot = build_plugin_snapshot(
        policy=policy,
        catalog=catalog,
        profiles=profiles,
        principal_scope="local:spike-s1",
        secret_inventory=_Inventory(),
        generation_key=b"s1-spike-generation-key-000000000",
    )
    review = build_abandonment_review(
        request="(placeholder)",
        state=CompositionState.from_dict(
            json.loads(
                '{"edges":[],"metadata":{"description":"","name":"Untitled Pipeline"},"nodes":[],"outputs":[],"sources":{},"version":1}'
            )
        ),
        plugin_snapshot=snapshot,
        messages=[],
        request_identity="spike",
        request_scope=(None, None, "spike"),
        registration_revision=0,
    )
    rec = next(r for r in review.records if r.label == "current deployment capabilities")
    payload = {
        "mode": mode,
        "snapshot_hash": snapshot.snapshot_hash,
        "policy_hash": snapshot.policy_hash,
        "unavailable": [(str(u.plugin_id), u.reason.value) for u in snapshot.unavailable],
        "record_label": rec.label,
        "record_content": rec.content,
        "provider_message": rec.provider_message()["content"],
    }
    (OUT / f"record_{mode}.json").write_text(json.dumps(payload, indent=1))
    print(json.dumps(payload, indent=1))


if __name__ == "__main__":
    main(sys.argv[1])
