"""Wire facts travel from ``DispatchAudit`` onto every recorded invocation.

S1 T5 (``docs/plans/2026-09-23-composer-strict-tool-contracts-s1.md``): the
compose loop knows, before dispatch, whether a ``strict`` key was sent for
the tool (``strict_sent``, D16) and whether the raw arguments conformed to the
wire schema W they were sent under (``wire_conformant``, C23). It opens the
audit envelope with those facts, and every finaliser — the four ``finish_*``
builders and ``dispatch_with_audit`` on each of its paths — must copy them onto
the :class:`ComposerToolInvocation`, so an invocation stored by the route drain
carries the same facts as the P4 row for the same call.

The opening helpers default both facts to ``None`` for the callers that
honestly do not know them (``pipeline_commit``, guided discovery, tests).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from elspeth.contracts.composer_audit import (
    ComposerToolInvocation,
    ComposerToolStatus,
    ToolArgumentErrorCategory,
)
from elspeth.web.composer.audit import (
    BufferingRecorder,
    DispatchAudit,
    begin_dispatch,
    begin_dispatch_or_arg_error,
    dispatch_with_audit,
    finish_arg_error,
    finish_cancelled,
    finish_plugin_crash,
    finish_success,
    rebind_dispatch_arguments,
)
from elspeth.web.composer.protocol import ToolArgumentError

# Pairs chosen so that a builder that drops the copy (and so records the
# ``None`` defaults) cannot pass by accident: every pair has at least one
# boolean, and the pairs cover both orders and the D16 "no key sent" case.
_FACT_PAIRS = (
    pytest.param(True, True, id="strict-true-conformant"),
    pytest.param(False, False, id="strict-false-nonconformant"),
    pytest.param(None, False, id="no-key-nonconformant"),
    pytest.param(True, None, id="strict-true-undecoded"),
)

_ARGUMENTS = {"patch": {"name": "wire-facts"}}


def _open(strict_sent: bool | None, wire_conformant: bool | None) -> DispatchAudit:
    return begin_dispatch(
        "tc-wire-facts",
        "set_metadata",
        _ARGUMENTS,
        version_before=1,
        actor="assistant",
        strict_sent=strict_sent,
        wire_conformant=wire_conformant,
    )


def _facts(record: DispatchAudit | ComposerToolInvocation) -> tuple[bool | None, bool | None]:
    return record.strict_sent, record.wire_conformant


def _arg_error() -> ToolArgumentError:
    return ToolArgumentError(
        argument="patch",
        expected="an object",
        actual_type="int",
        category=ToolArgumentErrorCategory.MODEL_VALIDATION,
    )


class TestOpeningHelpers:
    def test_begin_dispatch_defaults_to_unknown(self) -> None:
        audit = begin_dispatch("tc", "set_metadata", _ARGUMENTS, version_before=1, actor="assistant")

        assert _facts(audit) == (None, None)

    @pytest.mark.parametrize(("strict_sent", "wire_conformant"), _FACT_PAIRS)
    def test_begin_dispatch_carries_the_facts(self, strict_sent: bool | None, wire_conformant: bool | None) -> None:
        assert _facts(_open(strict_sent, wire_conformant)) == (strict_sent, wire_conformant)

    @pytest.mark.parametrize(("strict_sent", "wire_conformant"), _FACT_PAIRS)
    def test_begin_dispatch_carries_the_facts_on_the_raw_string_path(self, strict_sent: bool | None, wire_conformant: bool | None) -> None:
        audit = begin_dispatch(
            "tc",
            "set_metadata",
            "{not json",
            version_before=1,
            actor="assistant",
            strict_sent=strict_sent,
            wire_conformant=wire_conformant,
        )

        assert _facts(audit) == (strict_sent, wire_conformant)

    @pytest.mark.parametrize(("strict_sent", "wire_conformant"), _FACT_PAIRS)
    def test_begin_dispatch_or_arg_error_carries_the_facts(self, strict_sent: bool | None, wire_conformant: bool | None) -> None:
        audit, failure = begin_dispatch_or_arg_error(
            "tc",
            "set_metadata",
            _ARGUMENTS,
            version_before=1,
            actor="assistant",
            strict_sent=strict_sent,
            wire_conformant=wire_conformant,
        )

        assert failure is None
        assert _facts(audit) == (strict_sent, wire_conformant)

    @pytest.mark.parametrize(("strict_sent", "wire_conformant"), _FACT_PAIRS)
    def test_begin_dispatch_or_arg_error_carries_the_facts_on_the_canonicalization_fallback(
        self, strict_sent: bool | None, wire_conformant: bool | None
    ) -> None:
        """NaN cannot be canonicalised, so the helper builds its sentinel ``DispatchAudit``."""
        audit, failure = begin_dispatch_or_arg_error(
            "tc",
            "set_metadata",
            {"patch": {"name": float("nan")}},
            version_before=1,
            actor="assistant",
            strict_sent=strict_sent,
            wire_conformant=wire_conformant,
        )

        assert failure is not None
        assert _facts(audit) == (strict_sent, wire_conformant)

    def test_begin_dispatch_or_arg_error_defaults_to_unknown(self) -> None:
        audit, _ = begin_dispatch_or_arg_error("tc", "set_metadata", _ARGUMENTS, version_before=1, actor="assistant")

        assert _facts(audit) == (None, None)

    @pytest.mark.parametrize(("strict_sent", "wire_conformant"), _FACT_PAIRS)
    def test_rebind_keeps_the_facts(self, strict_sent: bool | None, wire_conformant: bool | None) -> None:
        rebound = rebind_dispatch_arguments(_open(strict_sent, wire_conformant), {"patch": {"name": "rebound"}})

        assert _facts(rebound) == (strict_sent, wire_conformant)


def _finish_success(audit: DispatchAudit) -> ComposerToolInvocation:
    return finish_success(audit, result_payload={"success": True}, version_after=2)


def _finish_arg_error(audit: DispatchAudit) -> ComposerToolInvocation:
    return finish_arg_error(
        audit,
        error_class="ToolArgumentError",
        error_category=ToolArgumentErrorCategory.SCHEMA_SHAPE,
        error_message="patch must be an object",
    )


def _finish_cancelled(audit: DispatchAudit) -> ComposerToolInvocation:
    return finish_cancelled(audit, exc=asyncio.CancelledError())


def _finish_plugin_crash(audit: DispatchAudit) -> ComposerToolInvocation:
    return finish_plugin_crash(audit, exc=RuntimeError("boom"))


_BUILDERS: tuple[Any, ...] = (
    pytest.param(_finish_success, ComposerToolStatus.SUCCESS, id="finish_success"),
    pytest.param(_finish_arg_error, ComposerToolStatus.ARG_ERROR, id="finish_arg_error"),
    pytest.param(_finish_cancelled, ComposerToolStatus.CANCELLED, id="finish_cancelled"),
    pytest.param(_finish_plugin_crash, ComposerToolStatus.PLUGIN_CRASH, id="finish_plugin_crash"),
)


class TestFinishBuilders:
    @pytest.mark.parametrize(("build", "status"), _BUILDERS)
    @pytest.mark.parametrize(("strict_sent", "wire_conformant"), _FACT_PAIRS)
    def test_builder_copies_the_facts(
        self,
        build: Callable[[DispatchAudit], ComposerToolInvocation],
        status: ComposerToolStatus,
        strict_sent: bool | None,
        wire_conformant: bool | None,
    ) -> None:
        invocation = build(_open(strict_sent, wire_conformant))

        assert invocation.status is status
        assert _facts(invocation) == (strict_sent, wire_conformant)
        assert invocation.to_dict()["strict_sent"] is strict_sent
        assert invocation.to_dict()["wire_conformant"] is wire_conformant


@dataclass(frozen=True, slots=True)
class _UpdatedState:
    version: int


@dataclass(frozen=True, slots=True)
class _Result:
    updated_state: _UpdatedState

    def to_dict(self) -> dict[str, Any]:
        return {"success": True}


async def _succeed() -> _Result:
    return _Result(updated_state=_UpdatedState(version=2))


async def _raise_arg_error() -> _Result:
    raise _arg_error()


async def _crash() -> _Result:
    raise RuntimeError("boom")


async def _cancel() -> _Result:
    raise asyncio.CancelledError


_DISPATCH_PATHS: tuple[Any, ...] = (
    pytest.param(_succeed, None, ComposerToolStatus.SUCCESS, id="success"),
    pytest.param(_raise_arg_error, ToolArgumentError, ComposerToolStatus.ARG_ERROR, id="arg-error"),
    pytest.param(_crash, RuntimeError, ComposerToolStatus.PLUGIN_CRASH, id="plugin-crash"),
    pytest.param(_cancel, asyncio.CancelledError, ComposerToolStatus.CANCELLED, id="cancelled"),
)


class TestDispatchWithAudit:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("do_dispatch", "raises", "status"), _DISPATCH_PATHS)
    @pytest.mark.parametrize(("strict_sent", "wire_conformant"), _FACT_PAIRS)
    async def test_every_path_records_the_facts(
        self,
        do_dispatch: Callable[[], Any],
        raises: type[BaseException] | None,
        status: ComposerToolStatus,
        strict_sent: bool | None,
        wire_conformant: bool | None,
    ) -> None:
        recorder = BufferingRecorder()

        async def _run() -> None:
            await dispatch_with_audit(
                recorder=recorder,
                audit=_open(strict_sent, wire_conformant),
                do_dispatch=do_dispatch,
                version_after_provider=lambda result: result.updated_state.version,
                arg_error_payload_factory=lambda _exc: {"error": "patch must be an object"},
            )

        if raises is None:
            await _run()
        else:
            with pytest.raises(raises):
                await _run()

        (invocation,) = recorder.invocations
        assert invocation.status is status
        assert _facts(invocation) == (strict_sent, wire_conformant)
