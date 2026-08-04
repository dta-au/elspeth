"""Textract transform acceptance for the AWS ECS deployment lane.

Proves the shipped ``aws_textract_document_analysis`` transform is packaged
in the running image, that its SDK client can be constructed, and that the
task role can invoke ``textract:StartDocumentAnalysis`` and
``textract:GetDocumentAnalysis``.  The live probes are deliberately
negative-space: they target a guaranteed-absent object under the granted
acceptance S3 prefix and a syntactically valid but unknown job id, so an
authorized principal receives Textract's static "invalid input" responses
(proving the action is invocable) while an unauthorized principal is
rejected before any document is processed.  No document content is ever
uploaded, read, or emitted.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from functools import partial
from typing import Any

from botocore.exceptions import ClientError

from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.plugins.transforms.aws.textract_client import build_textract_sdk_client
from elspeth.plugins.transforms.aws.textract_document_analysis import AWSTextractDocumentAnalysisConfig
from elspeth.web.plugin_policy.profiles import AWSTextractProfileSettings

from .contracts import (
    FORBIDDEN_AWS_OVERRIDE_ENV,
    AcceptanceCheckError,
    _resolve_acceptance_s3_location,
)

_TEXTRACT_TRANSFORM_NAME = "aws_textract_document_analysis"

# Static Textract error codes that can only be returned once the caller is
# authorized for the probed action: the request was accepted, authorized,
# and rejected on its deliberately-invalid input.
_START_INVOCABLE_CODES = frozenset({"InvalidS3ObjectException"})
_GET_INVOCABLE_CODES = frozenset({"InvalidJobIdException"})


def _client_error_code(error: ClientError) -> str | None:
    error_payload = error.response.get("Error")
    code = error_payload.get("Code") if isinstance(error_payload, Mapping) else None
    return code if type(code) is str and code else None


def _probe_invocable(invoke: Callable[[], object], *, invocable_codes: frozenset[str], check: str) -> None:
    """Require one probe call to prove the action is authorized and reachable."""

    try:
        invoke()
    except ClientError as error:
        if _client_error_code(error) in invocable_codes:
            return
        raise AcceptanceCheckError(check) from None
    except AcceptanceCheckError:
        raise
    except Exception:
        raise AcceptanceCheckError(check) from None
    # An outright success also proves the action is invocable (it can only
    # happen if something already exists at the probe identity).


def _verify_textract_profiles(
    env: Mapping[str, str],
    *,
    client: Any,
    region: str,
    probe_suffix_factory: Callable[[], str],
) -> dict[str, object]:
    """Exercise each operator Textract document profile without processing a document.

    Two proofs per profile: the binding satisfies the engine's own bucket-mode
    configuration rules (so web lowering cannot produce an unbuildable node),
    and the granted document location is invocable by the task role via the
    same negative-space StartDocumentAnalysis probe as the raw-SDK lane.
    """
    raw = env.get("ELSPETH_WEB__AWS_TEXTRACT_PROFILES")
    if raw is None or not raw.strip():
        return {"profiles_configured": 0, "profile_locations_invocable": True}
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise AcceptanceCheckError("textract_profile_settings") from None
    if type(parsed) is not list or not parsed:
        raise AcceptanceCheckError("textract_profile_settings")
    try:
        profiles = tuple(AWSTextractProfileSettings.model_validate(entry) for entry in parsed)
    except Exception:
        raise AcceptanceCheckError("textract_profile_settings") from None

    for profile in profiles:
        binding_options: dict[str, object] = {
            "region": region,
            "bucket": profile.bucket,
            "key_field": "document_key",
            "feature_types": ["TABLES"],
            "text_field": "textract_text",
            "schema": {"mode": "observed"},
        }
        if profile.key_prefix is not None:
            binding_options["key_prefix"] = profile.key_prefix
        try:
            AWSTextractDocumentAnalysisConfig.from_dict(binding_options, plugin_name=_TEXTRACT_TRANSFORM_NAME)
        except Exception:
            raise AcceptanceCheckError("textract_profile_binding") from None

        probe_name = f"verify-textract-{probe_suffix_factory()}.probe"
        probe_key = probe_name if profile.key_prefix is None else f"{profile.key_prefix}/{probe_name}"
        if len(probe_key.encode("utf-8")) > 1024:
            raise AcceptanceCheckError("textract_profile_binding")
        probe_location = {"S3Object": {"Bucket": profile.bucket, "Name": probe_key}}
        _probe_invocable(
            partial(client.start_document_analysis, DocumentLocation=probe_location, FeatureTypes=["TABLES"]),
            invocable_codes=_START_INVOCABLE_CODES,
            check="textract_profile_start_document_analysis",
        )
    return {"profiles_configured": len(profiles), "profile_locations_invocable": True}


def verify_textract(
    env: Mapping[str, str],
    *,
    plugin_manager_factory: Callable[[], Any] = get_shared_plugin_manager,
    sdk_client_factory: Callable[..., Any] = build_textract_sdk_client,
    probe_suffix_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
) -> dict[str, object]:
    """Prove the packaged Textract path end-to-end without processing a document."""

    if any(name in env for name in FORBIDDEN_AWS_OVERRIDE_ENV):
        raise AcceptanceCheckError("textract_aws_override")
    bucket, prefix, region = _resolve_acceptance_s3_location(env, check="textract_input")

    try:
        transforms = {plugin.name for plugin in plugin_manager_factory().get_transforms()}
    except Exception:
        raise AcceptanceCheckError("textract_plugin") from None
    if _TEXTRACT_TRANSFORM_NAME not in transforms:
        raise AcceptanceCheckError("textract_plugin")

    try:
        client = sdk_client_factory(
            region=region,
            aws_access_key_id=None,
            aws_secret_access_key=None,
            aws_session_token=None,
        )
    except Exception:
        raise AcceptanceCheckError("textract_client") from None

    close_failed = False
    try:
        probe_key = f"{prefix}/verify-textract-{probe_suffix_factory()}.probe"
        if len(probe_key.encode("utf-8")) > 1024:
            raise AcceptanceCheckError("textract_input")
        _probe_invocable(
            lambda: client.start_document_analysis(
                DocumentLocation={"S3Object": {"Bucket": bucket, "Name": probe_key}},
                FeatureTypes=["TABLES"],
            ),
            invocable_codes=_START_INVOCABLE_CODES,
            check="textract_start_document_analysis",
        )
        probe_job_id = f"{uuid.uuid4().hex}{uuid.uuid4().hex}"
        _probe_invocable(
            lambda: client.get_document_analysis(JobId=probe_job_id, MaxResults=1),
            invocable_codes=_GET_INVOCABLE_CODES,
            check="textract_get_document_analysis",
        )
        profile_details = _verify_textract_profiles(
            env,
            client=client,
            region=region,
            probe_suffix_factory=probe_suffix_factory,
        )
    finally:
        try:
            client.close()
        except Exception:
            close_failed = True
    if close_failed:
        raise AcceptanceCheckError("textract_resource_close")

    return {
        "transform_registered": True,
        "client_constructed": True,
        "start_document_analysis_invocable": True,
        "get_document_analysis_invocable": True,
        **profile_details,
    }
