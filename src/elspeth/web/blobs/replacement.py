"""Durable blob replacement under session authority and BLOB_CUSTODY locks."""

from __future__ import annotations

import hmac
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import Engine

from elspeth.contracts.blobs import BlobRecord, BlobReplacementPlan
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.blobs.protocol import BlobIntegrityError
from elspeth.web.blobs.service import (
    _atomic_write_blob,
    _blob_custody_session_lock,
    _blob_operation_path_token,
    _inline_custody_directory_fds,
    _require_blob_operation_context,
    _require_stable_custody_root,
    _require_stable_custody_session,
    content_hash,
)
from elspeth.web.sessions.protocol import SessionOperationAuthority

_REPLACE_KINDS = frozenset({SessionOperationKind.COMPOSE, SessionOperationKind.PROPOSAL})
_RECOVERY_KINDS = frozenset(
    {
        SessionOperationKind.CREATE,
        SessionOperationKind.COMPOSE,
        SessionOperationKind.PROPOSAL,
        SessionOperationKind.EXECUTE,
        SessionOperationKind.ARCHIVE,
        SessionOperationKind.BLOB_READ,
        SessionOperationKind.SESSION_FORK,
    }
)
_Evidence = Literal["absent", "old", "new", "both"]


@dataclass(frozen=True, slots=True)
class _CustodyDirectory:
    root_fd: int
    session_fd: int
    session_path: Path

    def validate(self) -> None:
        _require_stable_custody_root(self.session_path.parent, self.root_fd)
        _require_stable_custody_session(
            self.root_fd, session_name=self.session_path.name, descriptor=self.session_fd, session_dir=self.session_path
        )


class BlobReplacementCoordinator:
    """Filesystem work never runs inside a session mutation transaction."""

    def __init__(self, *, engine: Engine, data_dir: Path, session_operation_authority: SessionOperationAuthority) -> None:
        self._engine = engine
        self._data_dir = data_dir.expanduser().resolve()
        self._authority = session_operation_authority

    def _validated_paths(self, plan: BlobReplacementPlan) -> tuple[Path, Path, Path, Path]:
        session_dir = self._data_dir / "blobs" / str(plan.session_id)
        storage = session_dir / f"{plan.blob_id}_{plan.old_blob.filename}"
        if storage.parent != session_dir or Path(plan.storage_path) != storage:
            raise AuditIntegrityError("blob replacement storage escaped exact session custody")
        token = _blob_operation_path_token(
            operation_id=plan.operation_id, operation_epoch=plan.operation_epoch, operation_kind=plan.operation_kind
        )
        stem = f".{plan.blob_id}.replace-{token}-{plan.replacement_id}"
        staging = storage.with_name(f"{stem}.stage")
        backup = storage.with_name(f"{stem}.backup")
        temporary = staging.with_name(f".{staging.name}.custody.tmp")
        if Path(plan.staging_path) != staging or Path(plan.backup_path) != backup:
            raise AuditIntegrityError("blob replacement paths are not exact invocation-qualified paths")
        return storage, staging, backup, temporary

    @contextmanager
    def _directory(self, session_id: UUID) -> Iterator[_CustodyDirectory]:
        session_path = self._data_dir / "blobs" / str(session_id)
        with _inline_custody_directory_fds(session_path / "replacement", session_id=str(session_id), create=False) as descriptors:
            if descriptors is None:
                raise AuditIntegrityError("blob replacement custody directory is missing")
            directory = _CustodyDirectory(descriptors[0], descriptors[1], session_path)
            directory.validate()
            yield directory
            directory.validate()

    @staticmethod
    def _regular_or_absent(path: Path, directory_fd: _CustodyDirectory) -> bool:
        directory_fd.validate()
        try:
            observed = os.stat(path.name, dir_fd=directory_fd.session_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(observed.st_mode):
            raise AuditIntegrityError("blob replacement artifact is not a regular file")
        return True

    @classmethod
    def _read_bytes(cls, path: Path, directory_fd: _CustodyDirectory) -> bytes | None:
        if not cls._regular_or_absent(path, directory_fd):
            return None
        fd = os.open(path.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd.session_fd)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise AuditIntegrityError("blob replacement artifact changed to a nonregular file")
            return source.read()

    @classmethod
    def _evidence(cls, path: Path, plan: BlobReplacementPlan, directory_fd: _CustodyDirectory) -> _Evidence:
        data = cls._read_bytes(path, directory_fd)
        if data is None:
            return "absent"
        digest = content_hash(data)
        old = len(data) == plan.old_blob.size_bytes and hmac.compare_digest(digest, plan.old_blob.content_hash or "")
        new = len(data) == plan.replacement_blob.size_bytes and hmac.compare_digest(digest, plan.replacement_blob.content_hash or "")
        if old and new:
            return "both"
        if old:
            return "old"
        if new:
            return "new"
        raise BlobIntegrityError(str(plan.blob_id), expected=plan.old_blob.content_hash or "<absent>", actual=digest)

    @staticmethod
    def _matches(evidence: _Evidence, expected: Literal["old", "new"]) -> bool:
        return evidence in {expected, "both"}

    def _read_plan(self, context: SessionOperationContext, blob_id: UUID) -> BlobReplacementPlan | None:
        return self._authority.mutate(context, lambda transaction: transaction.blobs.read_blob_replacement(blob_id=blob_id))

    def _observe(self, context: SessionOperationContext, blob_id: UUID, primary: Exception) -> BlobReplacementPlan | None:
        try:
            return self._read_plan(context, blob_id)
        except Exception as observation_error:
            primary.add_note(f"Replacement outcome remains uncertain: {type(observation_error).__name__}")
            raise primary from observation_error

    def _rename(self, source: Path, target: Path, directory_fd: _CustodyDirectory, context: SessionOperationContext) -> None:
        self._authority.compare_and_swap(context)
        self._regular_or_absent(source, directory_fd)
        self._regular_or_absent(target, directory_fd)
        os.replace(source.name, target.name, src_dir_fd=directory_fd.session_fd, dst_dir_fd=directory_fd.session_fd)
        os.fsync(directory_fd.session_fd)
        directory_fd.validate()

    def _remove(self, path: Path, directory_fd: _CustodyDirectory, context: SessionOperationContext) -> None:
        self._authority.compare_and_swap(context)
        if self._regular_or_absent(path, directory_fd):
            os.unlink(path.name, dir_fd=directory_fd.session_fd)
            os.fsync(directory_fd.session_fd)
            directory_fd.validate()

    def _recover_plan(self, context: SessionOperationContext, plan: BlobReplacementPlan) -> None:
        """Choose bytes from the durable phase, never from a failed acknowledgement."""
        if str(plan.session_id) != context.fence.session_id:
            raise AuditIntegrityError("replacement recovery escaped exact session custody")
        current = self._authority.mutate(context, lambda transaction: transaction.blobs.read_blob(blob_id=plan.blob_id))
        committed = plan.phase == "purge_pending"
        if current != (plan.replacement_blob if committed else plan.old_blob):
            raise AuditIntegrityError("replacement recovery metadata differs from the complete durable snapshot")
        storage, staging, backup, temporary = self._validated_paths(plan)
        with self._directory(plan.session_id) as directory_fd:
            # Validate every artifact before the first mutation. A malformed
            # temporary must preserve the ledger and the recoverable copies.
            for path in (storage, staging, backup, temporary):
                self._regular_or_absent(path, directory_fd)
            storage_evidence = self._evidence(storage, plan, directory_fd)
            staging_evidence = self._evidence(staging, plan, directory_fd)
            backup_evidence = self._evidence(backup, plan, directory_fd)
            if staging_evidence != "absent" and not self._matches(staging_evidence, "new"):
                raise AuditIntegrityError("replacement staging does not contain proposed bytes")
            if backup_evidence != "absent" and not self._matches(backup_evidence, "old"):
                raise AuditIntegrityError("replacement backup does not contain old bytes")
            if committed:
                if not self._matches(storage_evidence, "new"):
                    if not self._matches(staging_evidence, "new"):
                        raise AuditIntegrityError("committed replacement lost exact proposed bytes")
                    self._rename(staging, storage, directory_fd, context)
            elif not self._matches(storage_evidence, "old"):
                if not self._matches(backup_evidence, "old"):
                    raise AuditIntegrityError("uncommitted replacement lost exact old bytes")
                self._rename(backup, storage, directory_fd, context)
            for path in (staging, backup, temporary):
                self._remove(path, directory_fd, context)
        # Read leases can make canonical bytes readable again. Their facet
        # cannot settle a write ledger, which remains for the next writer.
        if context.operation_kind is SessionOperationKind.BLOB_READ:
            return
        try:
            if committed:
                settled = self._authority.mutate(context, lambda transaction: transaction.blobs.retire_blob_replacement(plan=plan))
            else:
                settled = self._authority.mutate(context, lambda transaction: transaction.blobs.abort_blob_replacement(plan=plan))
        except Exception as exc:
            if self._observe(context, plan.blob_id, exc) is None:
                return
            raise
        if not settled and self._read_plan(context, plan.blob_id) is not None:
            raise AuditIntegrityError("replacement changed before exact recovery settlement")

    def _reconcile_blob_replacements_locked(self, context: SessionOperationContext) -> None:
        _require_blob_operation_context(context, allowed_kinds=_RECOVERY_KINDS)
        plans = self._authority.mutate(context, lambda transaction: transaction.blobs.list_blob_replacements())
        for plan in plans:
            self._recover_plan(context, plan)

    def reconcile(self, *, context: SessionOperationContext) -> None:
        _require_blob_operation_context(context, allowed_kinds=_RECOVERY_KINDS)
        with _blob_custody_session_lock(self._engine, context.fence.session_id):
            self._reconcile_blob_replacements_locked(context)

    def replace_blob(
        self,
        *,
        expected: BlobRecord,
        replacement: BlobRecord,
        content: bytes,
        context: SessionOperationContext,
        max_storage_per_session: int,
        accepting_proposal_id: UUID | None,
    ) -> BlobRecord:
        _require_blob_operation_context(context, allowed_kinds=_REPLACE_KINDS)
        if type(expected) is not BlobRecord or type(replacement) is not BlobRecord or type(content) is not bytes:
            raise TypeError("replacement requires exact blob records and bytes")
        if context.operation_kind is SessionOperationKind.PROPOSAL:
            if type(accepting_proposal_id) is not UUID:
                raise AuditIntegrityError("proposal replacement requires an exact accepting proposal identity")
        elif accepting_proposal_id is not None:
            raise AuditIntegrityError("non-proposal replacement cannot exclude proposal retention")
        if (
            expected.id != replacement.id
            or expected.session_id != replacement.session_id
            or str(expected.session_id) != context.fence.session_id
        ):
            raise AuditIntegrityError("blob replacement escaped exact session custody")
        if replacement.content_hash is None or not hmac.compare_digest(content_hash(content), replacement.content_hash):
            raise AuditIntegrityError("proposed metadata does not match replacement bytes")
        if len(content) != replacement.size_bytes:
            raise AuditIntegrityError("proposed size does not match replacement bytes")
        with _blob_custody_session_lock(self._engine, context.fence.session_id):
            self._reconcile_blob_replacements_locked(context)
            current = self._authority.mutate(context, lambda transaction: transaction.blobs.read_blob(blob_id=expected.id))
            if current != expected:
                raise AuditIntegrityError("blob metadata changed before durable replacement")
            session_dir = self._data_dir / "blobs" / str(expected.session_id)
            storage = session_dir / f"{expected.id}_{expected.filename}"
            if storage.parent != session_dir or Path(expected.storage_path) != storage or replacement.storage_path != expected.storage_path:
                raise AuditIntegrityError("blob replacement storage escaped exact custody")
            replacement_id = uuid4()
            token = _blob_operation_path_token(
                operation_id=context.fence.operation_id,
                operation_epoch=context.fence.operation_epoch,
                operation_kind=context.operation_kind,
            )
            stem = f".{expected.id}.replace-{token}-{replacement_id}"
            staging = storage.with_name(f"{stem}.stage")
            backup = storage.with_name(f"{stem}.backup")
            with self._directory(expected.session_id) as directory_fd:
                old_data = self._read_bytes(storage, directory_fd)
                if old_data is None or expected.content_hash is None:
                    raise AuditIntegrityError("replacement requires existing canonical content with a hash")
                digest = content_hash(old_data)
                if len(old_data) != expected.size_bytes or not hmac.compare_digest(digest, expected.content_hash):
                    raise BlobIntegrityError(str(expected.id), expected=expected.content_hash, actual=digest)
                try:
                    plan = self._authority.mutate(
                        context,
                        lambda transaction: transaction.blobs.prepare_blob_replacement(
                            replacement_id=replacement_id,
                            expected=expected,
                            replacement=replacement,
                            staging_path=str(staging),
                            backup_path=str(backup),
                            max_storage_per_session=max_storage_per_session,
                            accepting_proposal_id=accepting_proposal_id,
                        ),
                    )
                except Exception as exc:
                    observed = self._observe(context, expected.id, exc)
                    if observed is None or observed.replacement_id != replacement_id or observed.phase != "intent":
                        raise
                    plan = observed
                try:
                    for path in self._validated_paths(plan):
                        self._regular_or_absent(path, directory_fd)

                    def stage_guard() -> None:
                        directory_fd.validate()
                        self._authority.compare_and_swap(context)

                    _atomic_write_blob(
                        staging,
                        content,
                        write_guard=stage_guard,
                        directory_fd=directory_fd.session_fd,
                    )
                    plan = self._authority.mutate(context, lambda transaction: transaction.blobs.mark_blob_replacement_staged(plan=plan))
                    self._rename(storage, backup, directory_fd, context)
                    self._rename(staging, storage, directory_fd, context)
                    self._authority.compare_and_swap(context)
                    plan = self._authority.mutate(
                        context,
                        lambda transaction: transaction.blobs.commit_blob_replacement(
                            plan=plan,
                            max_storage_per_session=max_storage_per_session,
                            accepting_proposal_id=accepting_proposal_id,
                        ),
                    )
                except Exception as exc:
                    observed = self._observe(context, expected.id, exc)
                    if observed is None or observed.replacement_id != replacement_id:
                        raise
                    if observed.phase == "purge_pending":
                        plan = observed
                    else:
                        try:
                            self._recover_plan(context, observed)
                        except Exception as recovery_error:
                            exc.add_note(f"Durable replacement recovery remains pending: {type(recovery_error).__name__}")
                            raise exc from recovery_error
                        raise
            self._recover_plan(context, plan)
            return replacement
