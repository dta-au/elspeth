"""Opt-in live acceptance proof for asynchronous Amazon Textract analysis.

One node per production auth-discriminator variant (PB-09:
``transform:aws_textract_document_analysis@default_chain`` /
``@secret_refs``).  Both nodes cross the plugin boundary the original
proof crossed: real config validation, ``on_start`` with a live audit
recorder, and ``_process_single_with_state`` against the operator-seeded
S3 fixture document.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, get_args

import pytest
from scripts.state_engine_assessment_lib.selectors import AWS_RESOURCES, COMMON_LIVE_RESOURCES

from elspeth.core.config import _expand_env_vars
from elspeth.plugins.transforms.aws.textract_config_shared import AuthMode
from elspeth.plugins.transforms.aws.textract_document_analysis import AWSTextractDocumentAnalysis
from elspeth.testing import make_pipeline_row

_RUN_GATE = "ELSPETH_RUN_LIVE_TEXTRACT"
_REQUIRED_INPUTS = (
    "ELSPETH_TEST_TEXTRACT_REGION",
    "ELSPETH_TEST_TEXTRACT_BUCKET",
    "ELSPETH_TEST_TEXTRACT_KEY",
    "ELSPETH_TEST_TEXTRACT_EXPECTED_TEXT",
)

# Resource names come from the closed live-lane vocabulary
# (scripts.state_engine_assessment_lib.selectors is the single source of truth).
_CLOSED_RESOURCE_VOCABULARY = frozenset(AWS_RESOURCES) | frozenset(COMMON_LIVE_RESOURCES)
for _resource_name in _REQUIRED_INPUTS:
    if _resource_name not in _CLOSED_RESOURCE_VOCABULARY:
        raise AssertionError(f"{_resource_name} is not in the closed live-resource vocabulary")

# The two PB-09 auth variants, pinned against the production Pydantic
# discriminator (the golden matrix mirrors this set; the Literal is the
# authority).
TEXTRACT_AUTH_MODES: tuple[str, ...] = get_args(AuthMode)
if TEXTRACT_AUTH_MODES != ("default_chain", "secret_refs"):
    raise AssertionError(f"Textract AuthMode discriminator drifted: {TEXTRACT_AUTH_MODES}")

# boto3's environment credential provider owns these names; they are the
# ambient credential channel for the secret_refs variant, not an
# ELSPETH-invented vocabulary (same posture as the Azure lanes'
# azure-identity EnvironmentCredential trio).
_SDK_ACCESS_KEY_ID_ENV = "AWS_ACCESS_KEY_ID"
_SDK_SECRET_ACCESS_KEY_ENV = "AWS_SECRET_ACCESS_KEY"
_SDK_SESSION_TOKEN_ENV = "AWS_SESSION_TOKEN"


@pytest.fixture(params=TEXTRACT_AUTH_MODES)
def textract_auth_options(request: pytest.FixtureRequest) -> dict[str, Any]:
    """Real config-discriminator fields for one Textract auth variant (or skip).

    The ``secret_refs`` arm builds genuine ``${VAR}`` credential references
    and resolves them through ``_expand_env_vars`` — the production loader
    seam that resolves secret references for YAML-authored pipelines — so
    the config validator receives credentials exactly as the runtime would
    deliver them.  Values never appear in the test source.
    """
    mode = request.param
    if mode == "default_chain":
        return {"auth_mode": mode}
    if mode == "secret_refs":
        if not os.environ.get(_SDK_ACCESS_KEY_ID_ENV) or not os.environ.get(_SDK_SECRET_ACCESS_KEY_ENV):
            pytest.skip(
                "ambient AWS credential pair is not configured for the secret_refs variant "
                f"({_SDK_ACCESS_KEY_ID_ENV}/{_SDK_SECRET_ACCESS_KEY_ENV})"
            )
        references: dict[str, Any] = {
            "auth_mode": mode,
            "aws_access_key_id": f"${{{_SDK_ACCESS_KEY_ID_ENV}}}",
            "aws_secret_access_key": f"${{{_SDK_SECRET_ACCESS_KEY_ENV}}}",
        }
        if os.environ.get(_SDK_SESSION_TOKEN_ENV):
            references["aws_session_token"] = f"${{{_SDK_SESSION_TOKEN_ENV}}}"
        return _expand_env_vars(references)
    raise AssertionError(f"unsupported Textract auth mode {mode!r}")


@dataclass
class _LiveLandscapeRecorder:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def allocate_call_index(self, state_id: str) -> int:
        del state_id
        return len(self.calls)

    def record_call(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(call_id=f"live-call-{len(self.calls)}")


@pytest.mark.live_aws
@pytest.mark.live_provider
def test_async_textract_document_analysis_live(textract_auth_options: dict[str, Any]) -> None:
    gate = os.getenv(_RUN_GATE)
    if gate is None:
        pytest.fail("live Amazon Textract proof gate is required", pytrace=False)
    if gate != "1":
        pytest.fail("live Amazon Textract proof gate is invalid", pytrace=False)

    values = {name: os.getenv(name) for name in _REQUIRED_INPUTS}
    if any(not value for value in values.values()):
        pytest.fail("live Amazon Textract proof inputs are incomplete", pytrace=False)

    region = values["ELSPETH_TEST_TEXTRACT_REGION"]
    bucket = values["ELSPETH_TEST_TEXTRACT_BUCKET"]
    key = values["ELSPETH_TEST_TEXTRACT_KEY"]
    expected_text = values["ELSPETH_TEST_TEXTRACT_EXPECTED_TEXT"]
    assert region is not None and bucket is not None and key is not None and expected_text is not None

    recorder = _LiveLandscapeRecorder()
    transform: AWSTextractDocumentAnalysis | None = None
    try:
        transform = AWSTextractDocumentAnalysis(
            {
                "region": region,
                **textract_auth_options,
                "bucket_field": "document_bucket",
                "key_field": "document_key",
                "feature_types": ["LAYOUT"],
                "text_field": "textract_text",
                "metadata_field": "textract_metadata",
                "poll_timeout_seconds": 3600.0,
                "batch_wait_timeout_seconds": 3690.0,
                "schema": {"mode": "observed"},
            }
        )
        transform.on_start(
            SimpleNamespace(
                landscape=recorder,
                node_id="live-textract-node",
                run_id="live-textract-run",
                telemetry_emit=lambda _event: None,
                rate_limit_registry=None,
                shutdown_event=None,
            )
        )
        result = transform._process_single_with_state(
            make_pipeline_row({"document_bucket": bucket, "document_key": key}),
            "live-textract-state",
            token_id="live-textract-token",
        )
    except Exception:
        pytest.fail("live Amazon Textract analysis failed", pytrace=False)
    finally:
        if transform is not None:
            try:
                transform.close()
            except Exception:
                pytest.fail("live Amazon Textract cleanup failed", pytrace=False)

    if result.status != "success" or result.row is None:
        pytest.fail("live Amazon Textract analysis did not succeed", pytrace=False)
    text = result.row["textract_text"]
    metadata = result.row["textract_metadata"]
    if type(text) is not str or expected_text not in text:
        pytest.fail("live Amazon Textract expected text was not found", pytrace=False)
    if not isinstance(metadata, Mapping):
        pytest.fail("live Amazon Textract metadata was unavailable", pytrace=False)
    page_count = metadata.get("page_count")
    block_count = metadata.get("block_count")
    if type(page_count) is not int or page_count <= 0 or type(block_count) is not int or block_count <= 0:
        pytest.fail("live Amazon Textract result counts were invalid", pytrace=False)
    operations = [call["request_data"].to_dict()["operation"] for call in recorder.calls]
    if operations.count("start_document_analysis") != 1 or operations.count("get_document_analysis") < 1:
        pytest.fail("live Amazon Textract audit calls were incomplete", pytrace=False)
