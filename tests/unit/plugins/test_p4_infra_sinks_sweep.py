"""Plugin review 2026-11-11, P4 sweep: plugin infrastructure, S3, audit export and Dataverse sink.

Findings #1, #7, #8, #12, #15, #16, #17, #31 and #89. Each test fails at an
assertion on the code the finding describes and passes once it is fixed.
"""

from __future__ import annotations

import base64
import hashlib
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest

from elspeth.contracts.plugin_capabilities import ControlRole, PluginCapability
from elspeth.contracts.sink_effects import (
    SinkEffectInspectionRequest,
    SinkEffectPipelineMembersInput,
    SinkEffectPrepareRequest,
    SinkEffectReconcileKind,
)

# ---------------------------------------------------------------------------
# #1: the base control-credit default must not interpret a phantom option.
# ---------------------------------------------------------------------------


def test_base_control_credit_ignores_the_phantom_detect_only_key() -> None:
    """No policy-capable config admits ``detect_only``; the base default must not read it.

    A detect-only configuration is expressed by each plugin's real knobs
    (azure_content_safety thresholds, bedrock ``source``, prompt shield
    ``analysis_type``) and decided by that plugin's override. The base
    default decides from the declared capability alone.
    """
    from elspeth.plugins.transforms.aws.bedrock_prompt_shield import AWSBedrockPromptShield, AWSBedrockPromptShieldConfig

    assert "detect_only" not in AWSBedrockPromptShieldConfig.model_fields
    assert AWSBedrockPromptShieldConfig.model_config.get("extra") == "forbid"
    assert "is_effective_blocking_control" not in vars(AWSBedrockPromptShield)

    credited = AWSBedrockPromptShield.is_effective_blocking_control(
        capability=PluginCapability.PROMPT_SHIELD,
        role=ControlRole.INPUT,
        options={"detect_only": True},
    )
    assert credited is True


def test_real_non_blocking_configs_still_get_no_credit() -> None:
    """The real non-blocking knobs keep refusing credit (the coverage the phantom key stood in for)."""
    from elspeth.plugins.transforms.aws.bedrock_content_safety import AWSBedrockContentSafety
    from elspeth.plugins.transforms.azure.content_safety import AzureContentSafety
    from elspeth.plugins.transforms.azure.prompt_shield import AzurePromptShield

    assert not AzureContentSafety.is_effective_blocking_control(
        capability=PluginCapability.CONTENT_SAFETY,
        role=ControlRole.OUTPUT,
        options={"thresholds": {"hate": 6, "violence": 6, "sexual": 6, "self_harm": 6}},
    )
    assert not AzurePromptShield.is_effective_blocking_control(
        capability=PluginCapability.PROMPT_SHIELD,
        role=ControlRole.INPUT,
        options={"analysis_type": "user_prompt"},
    )
    assert not AWSBedrockContentSafety.is_effective_blocking_control(
        capability=PluginCapability.CONTENT_SAFETY,
        role=ControlRole.OUTPUT,
        options={"source": "INPUT"},
    )


# ---------------------------------------------------------------------------
# #7: a render whose context carries a row never returns raw exception text.
# ---------------------------------------------------------------------------

_ROW_SENTINEL = "ROWVALUESENTINEL"


def _render_failure_text(source: str, **context: object) -> str:
    from elspeth.plugins.infrastructure.templates import create_sandboxed_environment

    template = create_sandboxed_environment().from_string(source)
    try:
        template.render(**context)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    raise AssertionError("the render was expected to fail")


def test_default_environment_withholds_row_values_from_render_failures() -> None:
    from elspeth.plugins.infrastructure.templates import ALL_FIELDS, TemplateRow
    from elspeth.testing import make_pipeline_row

    row = TemplateRow.project(make_pipeline_row({"q": _ROW_SENTINEL}), ALL_FIELDS)
    text = _render_failure_text("{{ {}[row.q] }}", row=row)
    assert _ROW_SENTINEL not in text
    assert text.startswith("TemplateError: ")


def test_default_environment_keeps_operator_only_render_diagnostics() -> None:
    """A context with no row (e.g. a blob path over run_id) keeps the detailed message."""
    text = _render_failure_text("{{ {}[run_id] }}", run_id="OPERATORVALUE")
    assert "OPERATORVALUE" in text


# ---------------------------------------------------------------------------
# #8: sink pre-validation threads config through get_config_model.
# ---------------------------------------------------------------------------


class _DispatchingSink:
    """A sink whose config model is chosen from its config, like a provider-dispatched plugin."""

    seen: ClassVar[list[dict[str, Any] | None]] = []

    @classmethod
    def get_config_model(cls, config: dict[str, Any] | None = None) -> Any:
        cls.seen.append(config)
        if config is None:
            return None
        raise ValueError("Unknown provider 'nope'")


def test_sink_validation_passes_config_and_reports_dispatch_rejection() -> None:
    from elspeth.plugins.infrastructure.validation import ValidationError, validate_sink_config

    _DispatchingSink.seen.clear()
    errors = validate_sink_config("dispatching", {"provider": "nope"}, plugin_cls=cast("Any", _DispatchingSink))
    assert _DispatchingSink.seen == [{"provider": "nope"}]
    assert errors == [ValidationError(field="provider", message="Unknown provider 'nope'", value=None)]


def test_sink_validation_through_the_shared_manager_passes_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``plugin_cls is None`` path resolves through the shared manager and still threads config."""
    from elspeth.plugins.infrastructure import manager as manager_module
    from elspeth.plugins.infrastructure.validation import ValidationError, validate_sink_config

    class _Manager:
        def get_sink_by_name(self, name: str) -> Any:
            assert name == "dispatching"
            return _DispatchingSink

    monkeypatch.setattr(manager_module, "get_shared_plugin_manager", _Manager)
    _DispatchingSink.seen.clear()
    errors = validate_sink_config("dispatching", {"provider": "nope"})
    assert _DispatchingSink.seen == [{"provider": "nope"}]
    assert errors == [ValidationError(field="provider", message="Unknown provider 'nope'", value=None)]


# ---------------------------------------------------------------------------
# #12: the S3 source parses the SDK Body (ADR-032), not a runtime_checkable Protocol.
# ---------------------------------------------------------------------------


class _NonCallableReadBody:
    read: Any = 1

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _ProxiedBody:
    """A body whose methods come through ``__getattr__`` (a wrapper/proxy shape)."""

    def __init__(self, data: bytes) -> None:
        self._chunks = [data]
        self.closed = False

    def _read(self, size: int) -> bytes:
        del size
        return self._chunks.pop(0) if self._chunks else b""

    def _close(self) -> None:
        self.closed = True

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name == "read":
            return self._read
        if name == "close":
            return self._close
        raise AttributeError(name)


class _S3Client:
    def __init__(self, data: bytes, body: object) -> None:
        self._data = data
        self._body = body

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {"ContentLength": len(self._data), "ETag": '"etag"'}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {"ContentLength": len(self._data), "Body": self._body}

    def close(self) -> None:
        return None


def test_s3_body_with_non_callable_read_is_invalid_body_not_a_crash() -> None:
    from elspeth.plugins.sources.aws_s3_source import S3SourceReadError, _download_s3_object

    body = _NonCallableReadBody()
    caught: BaseException | None = None
    try:
        _download_s3_object(_S3Client(b"", body), bucket="bucket", key="key", max_object_bytes=1)
    except Exception as exc:
        caught = exc
    assert type(caught) is S3SourceReadError
    assert caught.provider_error_type == "InvalidS3Body"
    assert body.closed


def test_s3_body_read_and_close_are_parsed_by_attribute_not_static_protocol() -> None:
    from elspeth.plugins.sources.aws_s3_source import S3SourceReadError, _download_s3_object

    data = b"a,b\n1,2\n"
    body = _ProxiedBody(data)
    caught: BaseException | None = None
    try:
        downloaded = _download_s3_object(_S3Client(data, body), bucket="bucket", key="key", max_object_bytes=len(data))
    except S3SourceReadError as exc:
        caught = exc
    assert caught is None
    assert downloaded.content_hash == hashlib.sha256(data).hexdigest()
    assert body.closed
    downloaded.close()


# ---------------------------------------------------------------------------
# #15 / #16 / #17: audit-export bundle preflight and reconcile.
# ---------------------------------------------------------------------------


def test_preflight_surfaces_a_probe_that_could_not_be_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.plugins.sinks import _audit_export_bundle_effects as bundle_effects

    monkeypatch.setattr(bundle_effects, "_remove_scratch_path_at", lambda _parent_fd, _name: False)
    caught: BaseException | None = None
    try:
        bundle_effects.preflight_audit_export_bundle(tmp_path / "audit")
    except bundle_effects.AuditExportBundlePreflightError as exc:
        caught = exc
    assert caught is not None
    assert "probe cleanup failed" in str(caught)


def test_preflight_compares_the_real_staging_parent_device(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.plugins.sinks import _audit_export_bundle_effects as bundle_effects

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(bundle_effects, "_staging_path", lambda target, effect_id: elsewhere / f".{target.name}.{effect_id}")
    monkeypatch.setattr(bundle_effects, "_device_id", lambda path: 2 if path == elsewhere else 1)
    caught: BaseException | None = None
    try:
        bundle_effects.preflight_audit_export_bundle(tmp_path / "audit")
    except bundle_effects.AuditExportBundlePreflightError as exc:
        caught = exc
    assert caught is not None
    assert "same device" in str(caught)


def test_reconcile_records_the_measured_staging_state(tmp_path: Path) -> None:
    from elspeth.plugins.sinks import _audit_export_bundle_effects as bundle_effects
    from tests.unit.plugins.sinks.test_audit_export_bundle_effects import _prepare, _stage

    plan = _prepare(tmp_path / "audit")
    shutil.rmtree(_stage(plan))

    result = bundle_effects.reconcile_audit_export_bundle(plan)

    assert result.kind is SinkEffectReconcileKind.UNKNOWN
    assert dict(result.evidence) == {"staging": "absent", "target": "absent"}


def test_reconcile_still_reports_a_compared_divergent_staging_tree(tmp_path: Path) -> None:
    from elspeth.plugins.sinks import _audit_export_bundle_effects as bundle_effects
    from tests.unit.plugins.sinks.test_audit_export_bundle_effects import _prepare, _stage

    plan = _prepare(tmp_path / "audit")
    (_stage(plan) / "extra.csv").write_text("unexpected", encoding="utf-8")

    result = bundle_effects.reconcile_audit_export_bundle(plan)

    assert result.kind is SinkEffectReconcileKind.UNKNOWN
    assert dict(result.evidence) == {"staging": "divergent", "target": "absent"}


# ---------------------------------------------------------------------------
# #31: an S3 composite (multipart) SHA-256 checksum is not malformed.
# ---------------------------------------------------------------------------


def _s3_sink() -> Any:
    from elspeth.plugins.sinks.aws_s3_sink import AWSS3Sink
    from tests.unit.plugins.sinks.test_aws_s3_sink import _base_config

    return AWSS3Sink(_base_config())


def test_s3_head_composite_checksum_records_no_full_object_checksum() -> None:
    from elspeth.plugins.sinks._remote_object_effects import RemoteObjectPreconditionError

    composite = base64.b64encode(b"\x02" * 32).decode("ascii") + "-3"
    observation: Any = None
    caught: BaseException | None = None
    try:
        observation = _s3_sink()._observation_from_head({"ETag": '"etag-3"', "ChecksumSHA256": composite})
    except RemoteObjectPreconditionError as exc:
        caught = exc
    assert caught is None
    assert observation.checksum_b64 is None
    assert observation.checksum_algorithm is None
    assert observation.etag == '"etag-3"'


@pytest.mark.parametrize(
    "checksum",
    [
        pytest.param(base64.b64encode(b"\x02" * 32).decode("ascii") + "-0", id="zero-parts"),
        pytest.param(base64.b64encode(b"\x02" * 32).decode("ascii") + "-x", id="non-numeric-parts"),
        pytest.param(base64.b64encode(b"\x02" * 32).decode("ascii") + "-03", id="leading-zero-parts"),
        pytest.param(base64.b64encode(b"\x02" * 32).decode("ascii") + "-10001", id="too-many-parts"),
        pytest.param(base64.b64encode(b"\x02" * 31).decode("ascii") + "-3", id="short-digest"),
        pytest.param("not-base64-3", id="not-base64"),
    ],
)
def test_s3_head_malformed_composite_checksum_is_still_rejected(checksum: str) -> None:
    from elspeth.plugins.sinks._remote_object_effects import RemoteObjectPreconditionError

    with pytest.raises(RemoteObjectPreconditionError, match="malformed checksum"):
        _s3_sink()._observation_from_head({"ChecksumSHA256": checksum})


# ---------------------------------------------------------------------------
# #89: Dataverse member-effect validation computes plan material once per batch.
# ---------------------------------------------------------------------------


def _dataverse_batch(monkeypatch: pytest.MonkeyPatch, size: int) -> tuple[Any, Any, Any, Any, tuple[Any, ...]]:
    from elspeth.plugins.sinks import dataverse as dataverse_module
    from elspeth.plugins.sinks.dataverse import DataverseSink
    from tests.fixtures.base_classes import inject_write_failure
    from tests.unit.plugins.sinks.test_dataverse_sink import (
        FakeDataverseRowSchema,
        _config,
        _effect_member,
        _RecoverableDataverseClient,
        _restricted_effect_context,
    )

    monkeypatch.setattr(dataverse_module, "create_schema_from_config", lambda *_args, **_kwargs: FakeDataverseRowSchema)
    client = _RecoverableDataverseClient()
    sink = inject_write_failure(DataverseSink(_config()))
    monkeypatch.setattr(sink, "_client", client)
    members = tuple(_effect_member(index, {"email": f"user{index}", "name": f"Name {index}"}) for index in range(size))
    effect_input = SinkEffectPipelineMembersInput(members, members, len(members))
    ctx = _restricted_effect_context()
    inspection = sink.inspect_effect(SinkEffectInspectionRequest(effect_id="a" * 64, target="{}", predecessor_descriptor=None), ctx)
    plan = sink.prepare_effect(SinkEffectPrepareRequest(effect_id="a" * 64, effect_input=effect_input, inspection=inspection), ctx)
    return sink, plan, effect_input, ctx, members


def test_dataverse_member_validation_computes_batch_material_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.plugins.sinks.dataverse import DataverseSink

    sink, plan, effect_input, ctx, members = _dataverse_batch(monkeypatch, 4)
    original = DataverseSink._member_effect_material
    calls: list[str] = []

    def counting(self: Any, effect_id: str, batch: Any) -> Any:
        calls.append(effect_id)
        return original(self, effect_id, batch)

    monkeypatch.setattr(DataverseSink, "_member_effect_material", counting)
    for member in members:
        sink.commit_member_effect(plan, member, effect_input, ctx)
    for member in members:
        sink.reconcile_member_effect(plan, member, effect_input, ctx)

    assert len(calls) == 1


def test_dataverse_cached_material_still_rejects_a_divergent_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    sink, plan, effect_input, ctx, members = _dataverse_batch(monkeypatch, 3)
    sink.commit_member_effect(plan, members[0], effect_input, ctx)

    tampered = replace(plan, plan_hash=hashlib.sha256(b"other").hexdigest())
    with pytest.raises(ValueError, match="divergent"):
        sink.commit_member_effect(tampered, members[1], effect_input, ctx)

    # A different input object (equal content) is recomputed and still validates.
    rebuilt = SinkEffectPipelineMembersInput(tuple(members), tuple(members), len(members))
    assert rebuilt is not effect_input
    sink.commit_member_effect(plan, members[1], rebuilt, ctx)

    # A different batch bound to the same plan is divergent even after the cache holds material.
    shorter = SinkEffectPipelineMembersInput(members[:2], members[:2], 2)
    with pytest.raises(ValueError, match="divergent"):
        sink.commit_member_effect(plan, members[1], shorter, ctx)
