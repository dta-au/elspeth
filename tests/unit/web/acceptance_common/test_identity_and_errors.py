"""The shared identity type and error vocabulary, and their ECS re-imports by identity."""

from __future__ import annotations

import pytest

from elspeth.web._acceptance_common import errors, http_client, identity, secure_documents
from elspeth.web._aws_ecs_acceptance import contracts, receipt_contracts, state
from elspeth.web._aws_ecs_acceptance import http_client as ecs_http_client
from elspeth.web._aws_ecs_acceptance import secure_documents as ecs_secure_documents


class TestSanitizedResourceIdentity:
    @pytest.mark.parametrize("provider", sorted(identity.CLOUD_PROVIDERS))
    def test_closed_provider_set_is_accepted(self, provider: str) -> None:
        resource = identity.SanitizedResourceIdentity("elspeth-web", "0.8.0", "acceptance", provider)
        assert resource.cloud_provider == provider

    def test_provider_set_is_exactly_aws_and_azure(self) -> None:
        assert {"aws", "azure"} == identity.CLOUD_PROVIDERS

    @pytest.mark.parametrize("provider", ["gcp", "AWS", "aws ", "", "on-prem"])
    def test_other_providers_are_rejected(self, provider: str) -> None:
        with pytest.raises(ValueError, match="cloud_provider"):
            identity.SanitizedResourceIdentity("elspeth-web", "0.8.0", "acceptance", provider)

    def test_ecs_operator_receipt_still_demands_aws(self) -> None:
        """Widening the shared type did not loosen the ECS operator receipt."""
        details = {
            "phase": "outage",
            "metric_name": "operator.acceptance.sentinel",
            "trace_names": ["RunStarted", "RunFinished"],
            "observed_at": 1.0,
            "resource": {
                "service_name": "elspeth-web",
                "service_version": "0.8.0",
                "deployment_environment": "acceptance",
                "cloud_provider": "azure",
            },
            "sentinel_sha256": "e" * 64,
            "landscape_terminal": True,
            "trace_terminal_agrees": None,
            "collector_degraded": True,
            "cloud_receipt": False,
            "retained_metric_query": None,
            "retained_trace_id": None,
            "forbidden_content_absent": True,
        }
        with pytest.raises(errors.AcceptanceCheckError, match="exec_receipt_schema"):
            receipt_contracts._validate_operator_receipt_details(details)
        details["resource"]["cloud_provider"] = "aws"
        receipt_contracts._validate_operator_receipt_details(details)


class TestReImportsByIdentity:
    """Every moved name is the same object through the ECS module it used to live in."""

    def test_errors(self) -> None:
        assert contracts.AcceptanceCheckError is errors.AcceptanceCheckError
        assert contracts.AcceptanceHttpError is errors.AcceptanceHttpError
        assert contracts.AcceptanceInputError is errors.AcceptanceInputError
        assert contracts.AcceptanceStateError is errors.AcceptanceStateError
        assert contracts.ACCEPTANCE_ERROR_CODES is errors.ACCEPTANCE_ERROR_CODES
        assert contracts.ACCEPTANCE_STEPS is errors.ACCEPTANCE_STEPS
        assert contracts.acceptance_step is errors.acceptance_step
        assert contracts.current_acceptance_step is errors.current_acceptance_step
        assert contracts.reset_acceptance_step is errors.reset_acceptance_step

    def test_identity_and_transport(self) -> None:
        assert contracts.SanitizedResourceIdentity is identity.SanitizedResourceIdentity
        assert contracts.normalize_acceptance_origin is http_client.normalize_acceptance_origin
        assert ecs_http_client.AcceptanceHttpClient is http_client.AcceptanceHttpClient
        assert state.AcceptanceCredentials is http_client.AcceptanceCredentials
        assert ecs_secure_documents._read_protected_document is secure_documents._read_protected_document
        assert contracts.MAX_CONTROL_DOCUMENT_BYTES is secure_documents.MAX_CONTROL_DOCUMENT_BYTES

    def test_step_contextvar_is_shared(self) -> None:
        """The ECS envelope and a second provider's envelope read the same step."""
        errors.reset_acceptance_step()
        with errors.acceptance_step("login"):
            assert contracts.current_acceptance_step() == "login"
        assert contracts.current_acceptance_step() is None
