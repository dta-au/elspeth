"""Protected-document reads shared by every acceptance provider.

Moved verbatim from ``_aws_ecs_acceptance/secure_documents.py``: a receipt,
record or manifest is only read when it is a regular, owner-only file whose
identity does not change between the ``lstat`` and the open descriptor, and
whose size is within the control-document bound. Writes and the serialized
mutation lock stay with the ECS package until a second provider needs them.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from .errors import AcceptanceCheckError

MAX_CONTROL_DOCUMENT_BYTES = 256 * 1024


def _read_protected_document(path: Path, *, check: str) -> dict[str, object]:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o077:
            raise AcceptanceCheckError(check)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except AcceptanceCheckError:
        raise
    except OSError:
        raise AcceptanceCheckError(check) from None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_mode & 0o077
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_size > MAX_CONTROL_DOCUMENT_BYTES
        ):
            raise AcceptanceCheckError(check)
        content = os.read(descriptor, MAX_CONTROL_DOCUMENT_BYTES + 1)
        if len(content) > MAX_CONTROL_DOCUMENT_BYTES or os.read(descriptor, 1):
            raise AcceptanceCheckError(check)
    except AcceptanceCheckError:
        raise
    except OSError:
        raise AcceptanceCheckError(check) from None
    finally:
        os.close(descriptor)
    try:
        decoded = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise AcceptanceCheckError(check) from None
    if type(decoded) is not dict:
        raise AcceptanceCheckError(check)
    return decoded
