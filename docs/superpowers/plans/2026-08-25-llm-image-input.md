# LLM Image Input Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** LLM transforms (`llm`, `llm_multi_query`) accept images alongside text — blob-ref columns resolved through the payload store, sent to all four providers as typed content parts, audited as hash+ref only.

**Architecture:** Owned frozen dataclasses (`TextPart`/`ImagePart`/`ChatMessage`) replace `list[dict[str, str]]` across the whole provider seam in one parity sweep. Two projections derive from one message list: a wire form (OpenAI content-parts dialect, which litellm translates for Bedrock) and an audit form (bytes-free `{format, sha256, byte_count, blob_ref}`). Image binding happens in shared strategy code so both transforms gain it; structured output reuses `llm_multi_query`'s existing `output_fields`.

**Tech Stack:** Python 3.12, pydantic v2, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-25-llm-image-input-design.md` (committed in this worktree). Read it first.

## Global Constraints

- Work in the worktree `/home/john/elspeth/.claude/worktrees/llm-vision-input` (branch `worktree-llm-vision-input`). NEVER bare `python`/`pytest`: use `PYTHONPATH=/home/john/elspeth/.claude/worktrees/llm-vision-input/src /home/john/elspeth/.venv/bin/python -m pytest ...` and verify `elspeth.__file__` points into the worktree once at session start.
- Read `docs/agents/recent-code-hints.md` BEFORE writing code (whole-tree AST gates pin dynamic-attribute sites; a stray `getattr` breaks sibling branches).
- ADR-032: nominal `isinstance` against owned classes; sentinel-`getattr` parsing only at Tier-3 boundaries. No `runtime_checkable` Protocol as a security control.
- Image formats in this change: `jpeg`, `png` ONLY. `pdf` is out of scope (additive later).
- Image bytes must NEVER reach audit records, tracer payloads, logs, or exception text.
- `max_image_bytes` default 5_242_880, hard upper bound 20_971_520. `max_images_per_call` default 20.
- All new row-level failures return `TransformResult.error({...}, retryable=False)` — nothing new aborts a run.
- Text-only pipelines must produce byte-identical audit records to the pre-change tree (`content` stays `str` end to end when no images are configured).
- Commit after every task; pre-commit hooks run (never `--no-verify`).

---

### Task 1: Content-part contracts (`chat_parts.py`)

**Files:**
- Create: `src/elspeth/contracts/chat_parts.py`
- Test: `tests/unit/contracts/test_chat_parts.py`

**Interfaces:**
- Consumes: `binary_document_signature_matches`, `BINARY_DOCUMENT_FORMAT_BY_MIME` from `elspeth.contracts.binary_documents`.
- Produces (later tasks rely on these exact names):
  - `ImageFormat = Literal["jpeg", "png"]`
  - `TextPart(text: str)` (frozen, slots)
  - `ImagePart(format: ImageFormat, data: bytes, sha256: str, byte_count: int, blob_ref: str | None)` (frozen, slots); classmethod `from_bytes(*, format: ImageFormat, data: bytes, blob_ref: str | None) -> ImagePart`; method `audit_view() -> dict[str, str | int | None]`
  - `ContentPart = TextPart | ImagePart`
  - `ChatMessage(role: Literal["system", "user", "assistant"], content: str | tuple[ContentPart, ...])` (frozen, slots)
  - `wire_messages(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]` — OpenAI dialect
  - `audit_messages(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]` — bytes-free
  - `parts_hash(content: tuple[ContentPart, ...]) -> str`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/contracts/test_chat_parts.py
"""ImagePart invariants, ChatMessage projections. Guards are mutation-tested:
every tampered field must raise, not just the happy path pass."""
import base64
import hashlib

import pytest

from elspeth.contracts.chat_parts import (
    ChatMessage,
    ImagePart,
    TextPart,
    audit_messages,
    parts_hash,
    wire_messages,
)

# Smallest valid 1x1 PNG (signature-correct real image).
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 16  # signature-correct prefix


def _part(data: bytes = PNG_BYTES, fmt: str = "png") -> ImagePart:
    return ImagePart.from_bytes(format=fmt, data=data, blob_ref="a" * 64)


class TestImagePartFromBytes:
    def test_computes_hash_and_count(self) -> None:
        part = _part()
        assert part.sha256 == hashlib.sha256(PNG_BYTES).hexdigest()
        assert part.byte_count == len(PNG_BYTES)
        assert part.format == "png"
        assert part.blob_ref == "a" * 64

    def test_jpeg_signature_accepted(self) -> None:
        assert _part(JPEG_BYTES, "jpeg").format == "jpeg"

    def test_signature_mismatch_rejected(self) -> None:
        with pytest.raises(ValueError, match="signature"):
            ImagePart.from_bytes(format="jpeg", data=PNG_BYTES, blob_ref=None)

    def test_empty_data_rejected(self) -> None:
        with pytest.raises(ValueError):
            ImagePart.from_bytes(format="png", data=b"", blob_ref=None)


class TestImagePartInvariants:
    """A hand-built ImagePart cannot lie — __post_init__ re-asserts everything."""

    def test_tampered_sha256_rejected(self) -> None:
        with pytest.raises(ValueError, match="sha256"):
            ImagePart(format="png", data=PNG_BYTES, sha256="0" * 64, byte_count=len(PNG_BYTES), blob_ref=None)

    def test_tampered_byte_count_rejected(self) -> None:
        good = hashlib.sha256(PNG_BYTES).hexdigest()
        with pytest.raises(ValueError, match="byte_count"):
            ImagePart(format="png", data=PNG_BYTES, sha256=good, byte_count=1, blob_ref=None)

    def test_tampered_format_rejected(self) -> None:
        good = hashlib.sha256(PNG_BYTES).hexdigest()
        with pytest.raises(ValueError, match="signature"):
            ImagePart(format="jpeg", data=PNG_BYTES, sha256=good, byte_count=len(PNG_BYTES), blob_ref=None)

    def test_audit_view_has_no_bytes(self) -> None:
        view = _part().audit_view()
        assert view == {
            "type": "image",
            "format": "png",
            "sha256": hashlib.sha256(PNG_BYTES).hexdigest(),
            "byte_count": len(PNG_BYTES),
            "blob_ref": "a" * 64,
        }
        assert not any(isinstance(v, bytes) for v in view.values())


class TestProjections:
    def test_str_content_passes_through_both(self) -> None:
        msgs = [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="hi")]
        assert wire_messages(msgs) == [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
        assert audit_messages(msgs) == wire_messages(msgs)

    def test_wire_parts_are_openai_dialect(self) -> None:
        part = _part()
        msgs = [ChatMessage(role="user", content=(TextPart(text="describe"), part))]
        wire = wire_messages(msgs)
        b64 = base64.b64encode(PNG_BYTES).decode("ascii")
        assert wire == [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }
        ]

    def test_audit_parts_carry_no_bytes(self) -> None:
        part = _part()
        msgs = [ChatMessage(role="user", content=(TextPart(text="describe"), part))]
        audit = audit_messages(msgs)
        assert audit == [
            {"role": "user", "content": [{"type": "text", "text": "describe"}, part.audit_view()]}
        ]

    def test_parts_hash_is_order_sensitive_and_bytes_free(self) -> None:
        a, b = _part(), _part(JPEG_BYTES, "jpeg")
        t = TextPart(text="x")
        h1 = parts_hash((t, a, b))
        h2 = parts_hash((t, b, a))
        assert h1 != h2
        assert len(h1) == 64
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src /home/john/elspeth/.venv/bin/python -m pytest tests/unit/contracts/test_chat_parts.py -v`
Expected: FAIL — `ModuleNotFoundError: elspeth.contracts.chat_parts`

- [ ] **Step 3: Implement `chat_parts.py`**

```python
# src/elspeth/contracts/chat_parts.py
"""Owned chat-message content parts for multimodal LLM calls.

Layer: L0. No upward imports.

One authority for what an LLM message IS inside ELSPETH: a role plus either a
plain string (text-only — byte-identical audit behavior to the pre-image tree)
or an ordered tuple of typed parts. Two projections derive from it and are the
ONLY sanctioned exits: ``wire_messages`` (OpenAI content-parts dialect — the
single wire dialect; litellm translates it for Bedrock/Converse) and
``audit_messages`` (bytes-free — the only image representation permitted in
audit, tracing, hashing, and logs). Image bytes never leave this module any
other way.

Signature validation follows binary_documents doctrine: the byte signature
proves agreement with the declared format; it never chooses one.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, get_args

from elspeth.contracts.binary_documents import binary_document_signature_matches
from elspeth.contracts.hashing import canonical_json

ImageFormat = Literal["jpeg", "png"]
"""Closed set of LLM-input image formats. Subset of BinaryDocumentFormat;
widening to pdf is a deliberate later change, not a config knob."""

IMAGE_FORMATS: frozenset[str] = frozenset(get_args(ImageFormat))

_IMAGE_MIME_BY_FORMAT = {"jpeg": "image/jpeg", "png": "image/png"}


@dataclass(frozen=True, slots=True)
class TextPart:
    """One text segment of a message's content."""

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError(f"TextPart.text must be str, got {type(self.text).__name__}")


@dataclass(frozen=True, slots=True)
class ImagePart:
    """One image segment. Construct via from_bytes(); __post_init__ re-asserts
    every invariant so a hand-built instance cannot lie."""

    format: ImageFormat
    data: bytes
    sha256: str
    byte_count: int
    blob_ref: str | None

    def __post_init__(self) -> None:
        if self.format not in IMAGE_FORMATS:
            raise ValueError(f"ImagePart.format must be one of {sorted(IMAGE_FORMATS)}, got {self.format!r}")
        if not isinstance(self.data, bytes) or not self.data:
            raise ValueError("ImagePart.data must be non-empty bytes")
        if not binary_document_signature_matches(self.format, self.data):
            raise ValueError(f"ImagePart data does not carry the {self.format} byte signature")
        actual_hash = hashlib.sha256(self.data).hexdigest()
        if self.sha256 != actual_hash:
            raise ValueError("ImagePart.sha256 does not match data")
        if self.byte_count != len(self.data):
            raise ValueError("ImagePart.byte_count does not match data")
        if self.blob_ref is not None and not isinstance(self.blob_ref, str):
            raise ValueError("ImagePart.blob_ref must be str or None")

    @classmethod
    def from_bytes(cls, *, format: ImageFormat, data: bytes, blob_ref: str | None) -> ImagePart:
        if not isinstance(data, bytes) or not data:
            raise ValueError("ImagePart.from_bytes requires non-empty bytes")
        return cls(
            format=format,
            data=data,
            sha256=hashlib.sha256(data).hexdigest(),
            byte_count=len(data),
            blob_ref=blob_ref,
        )

    def audit_view(self) -> dict[str, str | int | None]:
        """The bytes-free projection — the ONLY image shape audit may hold."""
        return {
            "type": "image",
            "format": self.format,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
            "blob_ref": self.blob_ref,
        }


ContentPart = TextPart | ImagePart


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One chat message. content is str for text-only (audit byte-identical to
    the pre-image tree) or an ordered non-empty tuple of parts."""

    role: Literal["system", "user", "assistant"]
    content: str | tuple[ContentPart, ...]

    def __post_init__(self) -> None:
        if self.role not in ("system", "user", "assistant"):
            raise ValueError(f"ChatMessage.role invalid: {self.role!r}")
        if isinstance(self.content, str):
            return
        if not isinstance(self.content, tuple) or not self.content:
            raise ValueError("ChatMessage.content must be str or a non-empty tuple of parts")
        for part in self.content:
            if not isinstance(part, (TextPart, ImagePart)):
                raise ValueError(f"ChatMessage part must be TextPart or ImagePart, got {type(part).__name__}")


def _wire_content(content: str | tuple[ContentPart, ...]) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    out: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, TextPart):
            out.append({"type": "text", "text": part.text})
        else:
            b64 = base64.b64encode(part.data).decode("ascii")
            mime = _IMAGE_MIME_BY_FORMAT[part.format]
            out.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    return out


def _audit_content(content: str | tuple[ContentPart, ...]) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    out: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, TextPart):
            out.append({"type": "text", "text": part.text})
        else:
            out.append(part.audit_view())
    return out


def wire_messages(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    """OpenAI-dialect wire form. The only projection that may contain bytes
    (base64-encoded), and it goes to the provider ONLY — never to audit."""
    return [{"role": m.role, "content": _wire_content(m.content)} for m in messages]


def audit_messages(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    """Bytes-free audit form for LLMCallRequest recording."""
    return [{"role": m.role, "content": _audit_content(m.content)} for m in messages]


def parts_hash(content: tuple[ContentPart, ...]) -> str:
    """Order-sensitive SHA-256 over the audit views of a parts tuple."""
    views: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, TextPart):
            views.append({"type": "text", "sha256": hashlib.sha256(part.text.encode("utf-8")).hexdigest()})
        else:
            views.append(part.audit_view())
    return hashlib.sha256(canonical_json(views).encode("utf-8")).hexdigest()
```

Check `elspeth.contracts.hashing.canonical_json` exists with that signature (`contracts/hashing.py:64`) before writing the import.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src /home/john/elspeth/.venv/bin/python -m pytest tests/unit/contracts/test_chat_parts.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/contracts/chat_parts.py tests/unit/contracts/test_chat_parts.py
git commit -m "feat(contracts): owned chat content-part model — TextPart/ImagePart/ChatMessage with wire and audit projections"
```

---

### Task 2: Widen the provider seam to `Sequence[ChatMessage]` (parity sweep, text-only behavior preserved)

**Files:**
- Modify: `src/elspeth/plugins/transforms/llm/provider.py` (protocol, line ~293)
- Modify: `src/elspeth/plugins/infrastructure/clients/llm.py` (`chat_completion`, line ~356)
- Modify: `src/elspeth/plugins/transforms/llm/providers/azure.py` (execute_query ~139, preflight ~222)
- Modify: `src/elspeth/plugins/transforms/llm/providers/bedrock.py` (execute_query ~136, preflight ~195)
- Modify: `src/elspeth/plugins/transforms/llm/providers/openrouter.py` (execute_query ~307, `_build_llm_request_payload` ~441, preflight)
- Modify: `src/elspeth/plugins/transforms/llm/providers/gateway.py` (execute_query ~435 and ~566, preflight ~732)
- Modify: `src/elspeth/plugins/transforms/llm/transform.py` (message build sites ~287 and ~607)
- Test: existing suites under `tests/unit/plugins/transforms/llm/` and `tests/unit/plugins/infrastructure/` (update message fixtures), plus new assertions below

**Interfaces:**
- Consumes: Task 1's `ChatMessage`, `wire_messages`, `audit_messages`.
- Produces: `LLMProvider.execute_query(self, messages: Sequence[ChatMessage], *, model, temperature, max_tokens, audit_parent, response_format=None)`; `AuditedLLMClient.chat_completion(self, model: str, messages: Sequence[ChatMessage], *, ...)`. ALL providers and call sites use `ChatMessage` after this task — no dict-message call site survives.

This is one mechanical sweep. The conversion rules, applied uniformly:

1. **Protocol** (`provider.py`): change the annotation to `messages: Sequence[ChatMessage]` (import `ChatMessage` from `elspeth.contracts.chat_parts`, `Sequence` from `collections.abc`).
2. **AuditedLLMClient.chat_completion** (`clients/llm.py`): annotation to `Sequence[ChatMessage]`; then two projections replace the single `messages` use:

```python
from elspeth.contracts.chat_parts import ChatMessage, audit_messages, wire_messages

    def chat_completion(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        resolved_prompt_template_hash: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        ...
        request_dto = LLMCallRequest(
            model=model,
            messages=audit_messages(messages),   # bytes-free audit form
            temperature=temperature,
            provider=self._provider,
            max_tokens=max_tokens,
            extra_kwargs=kwargs,
        )
        ...
        sdk_kwargs: dict[str, Any] = {
            "model": model,
            "messages": wire_messages(messages),  # wire form to the SDK only
            "temperature": temperature,
            **kwargs,
        }
```

3. **azure.py / bedrock.py**: annotation change only in `execute_query` (they pass `messages` straight to `chat_completion`, which now projects). Preflight sites become `messages=[ChatMessage(role="user", content="This is a pre-flight smoke test. Please reply with ok.")]`.
4. **openrouter.py / gateway.py** (HTTP providers build body AND audit payload themselves):

```python
        wire = wire_messages(messages)
        ...
        request_body: dict[str, Any] = {
            "model": model,
            "messages": wire,
            "temperature": temperature,
        }
```

and `_build_llm_request_payload` gains the audit form: annotation `messages: Sequence[ChatMessage]`, body uses `messages=audit_messages(messages)` in the `LLMCallRequest`. Preflight sites: same `ChatMessage` literal as azure. Apply identically at BOTH gateway execute paths (~435 and ~566).

5. **transform.py** message-build sites (single ~287, multi ~607):

```python
        messages: list[ChatMessage] = []
        if self.system_prompt:
            messages.append(ChatMessage(role="system", content=self.system_prompt))
        messages.append(ChatMessage(role="user", content=rendered.prompt))
```

(multi-query uses `provider_prompt` instead of `rendered.prompt`.)

- [ ] **Step 1: Grep for every construction/annotation site**

Run: `grep -rn 'list\[dict\[str, str\]\]' src/elspeth/ | grep -iv test` and `grep -rn '"role":' src/elspeth/plugins | grep -v chat_parts`
Expected: the files listed above. Any EXTRA hit (e.g. langfuse/tracing reconstructing messages) is in-scope for the same sweep — convert it with the same rules. Zero hits may remain when done.

- [ ] **Step 2: Add regression tests for the projection split (before converting)**

Append to the AuditedLLMClient test module (find it via `grep -rln 'chat_completion' tests/unit/plugins/infrastructure/`):

```python
def test_chat_completion_audits_bytes_free_and_wires_openai_dialect(audited_llm_client, fake_sdk):
    """The SDK sees wire form; the recorded LLMCallRequest sees audit form; bytes appear in neither audit row nor logs."""
    from elspeth.contracts.chat_parts import ChatMessage, ImagePart, TextPart
    part = ImagePart.from_bytes(format="png", data=PNG_BYTES, blob_ref="a" * 64)
    msgs = [ChatMessage(role="user", content=(TextPart(text="t"), part))]

    audited_llm_client.chat_completion(model="m", messages=msgs)

    sdk_messages = fake_sdk.last_kwargs["messages"]
    assert sdk_messages[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    recorded = recorder.last_request_data  # adapt to the module's existing recorder fixture
    audit_content = recorded["messages"][0]["content"]
    assert audit_content[1] == part.audit_view()
    assert "base64" not in str(recorded)
```

Adapt fixture names to the module's existing fakes — do not invent new fixture machinery; the module already fakes the SDK client and recorder. Run it: FAIL (chat_completion rejects/mishandles ChatMessage).

- [ ] **Step 3: Apply the sweep (all files in the conversion rules above)**

- [ ] **Step 4: Fix the existing test fixtures**

Run: `PYTHONPATH=$PWD/src /home/john/elspeth/.venv/bin/python -m pytest tests/unit/plugins/transforms/llm/ tests/unit/plugins/infrastructure/ -x -q -p no:cacheprovider -n 8`
Every failure is a fixture still passing `[{"role": ..., "content": ...}]` — convert to `ChatMessage(...)` literals. Do NOT loosen any assertion: assertions on recorded audit `messages` keep their dict shape (audit form is still dicts).

- [ ] **Step 5: mypy + targeted suites green**

Run: `PYTHONPATH=$PWD/src /home/john/elspeth/.venv/bin/python -m mypy src/elspeth/plugins/transforms/llm src/elspeth/plugins/infrastructure/clients` then re-run Step 4's pytest command.
Expected: clean / all PASS. Confirm the Step 2 test passes.

- [ ] **Step 6: Commit**

```bash
git add -A src/elspeth tests
git commit -m "refactor(llm): widen the provider seam to Sequence[ChatMessage] — one parity sweep, wire/audit projection split, text-only behavior preserved"
```

---

### Task 3: Image input resolution (`image_inputs.py`)

**Files:**
- Create: `src/elspeth/plugins/transforms/llm/image_inputs.py`
- Test: `tests/unit/plugins/transforms/llm/test_image_inputs.py`

**Interfaces:**
- Consumes: `ImagePart`, `ImageFormat`, `IMAGE_FORMATS` (Task 1); `PayloadStore`, `PayloadNotFoundError` (`elspeth.contracts.payload_store`); `BINARY_DOCUMENT_FORMAT_BY_MIME` (`elspeth.contracts.binary_documents`); `TransformResult` (import path: match what `transform.py` uses).
- Produces:
  - `class ImageInputConfig(BaseModel)` — fields `field: str`, `format: ImageFormat | None = None`, `format_field: str | None = None`, `required: bool = True`; model validator enforcing exactly one of `format`/`format_field` and identifier-valid names.
  - `resolve_image_parts(row: PipelineRow, *, payload_store: PayloadStore | None, specs: Sequence[ImageInputConfig], max_image_bytes: int, max_images_per_call: int) -> tuple[ImagePart, ...] | TransformResult` — parts in spec order then per-row list order; on any failure returns `TransformResult.error({...}, retryable=False)` with the reasons below.

Error reasons (exact strings; mirror `textract_inline_analysis._read_document_bytes` vocabulary, `aws/textract_inline_analysis.py:453-508`): `missing_field`, `invalid_input` (with `error_type` one of `invalid_payload_ref`, `empty_document`, `image_signature_mismatch`, `unmapped_image_mime`, `non_string_ref`), `blob_not_found`, `blob_too_large`, `too_many_images`. A `required: false` spec whose column is absent or `None` contributes zero parts; any other failure on it still errors (a present-but-broken ref is data corruption, not absence). `IntegrityError` from the store propagates (Tier-1, same doctrine as Textract). Blob refs validate against `re.compile(r"[0-9a-f]{64}")` before hitting the store. List-valued columns: each element resolved in order; element failures name the index in the error dict (`"list_index": i`).

- [ ] **Step 1: Write the failing tests** — cover, with a `FakePayloadStore` (dict-backed, `retrieve` raising `PayloadNotFoundError` on miss):
  - happy path scalar `format="png"`; happy path `format_field` mapping `image/png`→png via `BINARY_DOCUMENT_FORMAT_BY_MIME`
  - list-valued column preserves order; two specs concatenate in spec order
  - `required=False` + absent column → `()`; `required=False` + present bad ref → error
  - each error reason above, asserting the exact `reason`/`error_type` strings and `retryable is False`
  - `max_image_bytes` boundary (exactly at limit passes, +1 fails); `max_images_per_call` boundary
  - config validator: both `format` and `format_field` → ValueError; neither → ValueError

Use `PNG_BYTES`/`JPEG_BYTES` from Task 1's test module (import them). Build rows the way the module's neighbors do (see `tests/unit/plugins/transforms/llm/` fixtures for `PipelineRow` construction).

- [ ] **Step 2: Run to verify failure** (`ModuleNotFoundError`)

- [ ] **Step 3: Implement**

```python
# src/elspeth/plugins/transforms/llm/image_inputs.py
"""Config-declared image inputs for LLM transforms: blob refs -> ImageParts.

Resolution mirrors textract_inline_analysis._read_document_bytes: every
row-data failure is a typed row-level TransformResult.error; payload-store
IntegrityError propagates (Tier-1). Bytes exist only in the returned
ImageParts — never in error dicts or logs.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, model_validator

from elspeth.contracts.binary_documents import BINARY_DOCUMENT_FORMAT_BY_MIME
from elspeth.contracts.chat_parts import IMAGE_FORMATS, ImageFormat, ImagePart
from elspeth.contracts.payload_store import PayloadNotFoundError, PayloadStore
from elspeth.contracts.schema_contract import PipelineRow

_PAYLOAD_REF_PATTERN = re.compile(r"[0-9a-f]{64}")


class ImageInputConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    format: ImageFormat | None = None
    format_field: str | None = None
    required: bool = True

    @model_validator(mode="after")
    def _exactly_one_format_source(self) -> "ImageInputConfig":
        if (self.format is None) == (self.format_field is None):
            raise ValueError("image input requires exactly one of 'format' or 'format_field'")
        for name in (self.field, self.format_field):
            if name is not None and not name.isidentifier():
                raise ValueError(f"image input field names must be identifiers, got {name!r}")
        return self
```

`resolve_image_parts` follows directly from the test matrix; import `TransformResult` from the same module path `transform.py` uses (check its imports). Resolve format per-image: literal `format`, else read `row[spec.format_field]`, map through `BINARY_DOCUMENT_FORMAT_BY_MIME`, reject results not in `IMAGE_FORMATS` (`unmapped_image_mime` — this is what keeps `pdf` out until the vocabulary widens). `ImagePart.from_bytes` raising `ValueError` on signature mismatch is caught and converted to the `image_signature_mismatch` error dict (never re-raise with byte content in the message).

- [ ] **Step 4: Run to verify pass**; **Step 5: Commit** (`feat(llm): image_inputs — blob-ref resolution to typed ImageParts with row-level error vocabulary`)

---

### Task 4: `LLMConfig` fields, declared inputs, knob-schema golden

**Files:**
- Modify: `src/elspeth/plugins/transforms/llm/base.py` (LLMConfig, after line ~114)
- Modify: `src/elspeth/plugins/transforms/llm/transform.py` (`__init__` ~1351, declared-input-fields wiring ~1396)
- Modify: `tests/golden/knob_schema/transform__llm*.json` (regenerate via that suite's blessed regen path — find it in the golden test module's header, never hand-edit)
- Test: `tests/unit/plugins/transforms/llm/test_llm_config_image_inputs.py`

**Interfaces:**
- Consumes: `ImageInputConfig` (Task 3).
- Produces on `LLMConfig`: `image_inputs: list[ImageInputConfig] | None = None`, `max_image_bytes: int` (default `5_242_880`, `gt=0`, `le=20_971_520`), `max_images_per_call: int` (default `20`, `gt=0`). Duplicate `field` names across entries rejected by a model validator. `LLMTransform.declared_input_fields` includes every `image_inputs[].field` and `format_field`.

- [ ] **Step 1: Failing tests** — config accepts the YAML shape from the spec §4; rejects duplicate fields, `max_image_bytes` over cap, unknown keys inside entries; `LLMTransform({...with image_inputs...}).declared_input_fields` contains `page_blob_ref` and `page_mime_type`. Follow how existing `LLMTransform` unit tests build a minimal config dict.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement.** Field declarations on LLMConfig with descriptions (composer schema exposure follows automatically from the catalog path — no composer-special code). For declared inputs, follow the textract pattern (`aws/textract_inline_analysis.py:242`): override the `declared_input_fields` property on `LLMTransform` as `super().declared_input_fields | frozenset(image fields + format fields)`, and register the names with `_reject_input_options_naming_created_fields` if LLMTransform uses that guard (grep it; textract does at `:342`).
- [ ] **Step 4: Run new tests + knob-schema golden suite; regenerate golden through its blessed path; diff the golden to confirm only additive keys.**
- [ ] **Step 5: Commit** (`feat(llm): image_inputs config on LLMConfig — declared inputs, caps, knob-schema golden`)

---

### Task 5: Strategy binding — parts into the call, parts_hash into audit

**Files:**
- Modify: `src/elspeth/plugins/transforms/llm/transform.py` (SingleQueryStrategy ~243-419, MultiQueryStrategy message build ~558-630, strategy construction ~1463/~1512)
- Modify: `src/elspeth/plugins/transforms/llm/__init__.py` (`build_llm_audit_metadata` ~272)
- Test: `tests/unit/plugins/transforms/llm/test_image_binding_strategies.py`

**Interfaces:**
- Consumes: Tasks 1-4. Strategies gain fields `image_specs: tuple[ImageInputConfig, ...]`, `max_image_bytes: int`, `max_images_per_call: int` (populated from config at construction; empty tuple = text-only).
- Produces: user message content becomes `(TextPart(prompt), *image_parts)` when parts resolve non-empty, else stays `str`. `build_llm_audit_metadata` gains keyword `parts_hash: str | None = None` emitting key `f"{field_prefix}_parts_hash"` only when not None.

- [ ] **Step 1: Failing tests** with a `FakeProvider` capturing `execute_query` args (mirror existing strategy-test fakes in `tests/unit/plugins/transforms/llm/`):
  - text-only config → captured user message content is a plain `str` (regression pin for audit byte-identity)
  - with images → content is `(TextPart, ImagePart, ...)` in spec order then list order; text part first
  - resolve failure (missing blob) → the provider is NEVER called and the strategy returns that error result unchanged
  - success_reason metadata contains `llm_response_parts_hash` matching `parts_hash(content_tuple)`; absent for text-only
  - tracer `record_success` receives `extra_metadata={"image_parts": [audit views]}` when images present (assert no `bytes` anywhere in it)
  - multi-query: images bind identically on each query message; `response_format` json_schema + images coexist on the same captured call
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement.** In each strategy's execute, immediately after template render:

```python
        image_parts: tuple[ImagePart, ...] = ()
        if self.image_specs:
            resolved = resolve_image_parts(
                row,
                payload_store=ctx.payload_store,
                specs=self.image_specs,
                max_image_bytes=self.max_image_bytes,
                max_images_per_call=self.max_images_per_call,
            )
            if isinstance(resolved, TransformResult):
                return resolved
            image_parts = resolved

        user_content: str | tuple[ContentPart, ...] = rendered.prompt
        content_hash: str | None = None
        if image_parts:
            user_content = (TextPart(text=rendered.prompt), *image_parts)
            content_hash = parts_hash(user_content)
        messages.append(ChatMessage(role="user", content=user_content))
```

Thread `content_hash` into `build_llm_audit_metadata(..., parts_hash=content_hash)` and, when images are present, pass `extra_metadata={"image_parts": [p.audit_view() for p in image_parts]}` to the tracer's `record_success`/`record_error` calls (currently `extra_metadata=None` in single-query; multi-query already passes a dict — merge the key). Multi-query note: images append to `provider_prompt` content AFTER the standard-mode schema suffix is applied, so the schema text and the images travel in the same user message.
- [ ] **Step 4: Run new tests + the full llm transform suite; all PASS.**
- [ ] **Step 5: Commit** (`feat(llm): bind image parts into single- and multi-query calls — parts_hash audited, bytes-free tracing`)

---

### Task 6: E2E — rasterized-page shape through a stub provider; audit is bytes-free

**Files:**
- Create: `tests/integration/pipeline/test_llm_image_input_pipeline.py`
- Fixture: reuse `PNG_BYTES`; stage blobs through the payload store the way `tests/integration/pipeline/` neighbors do (grep `payload_store` there for the blessed fixture)

**Interfaces:** consumes everything prior; produces no new API.

- [ ] **Step 1: Write the test** — build a pipeline whose rows carry `page_blob_ref` + `page_mime_type` (the `pdf_rasterize` output shape, but authored directly — that branch is NOT a dependency), through an `llm` transform configured with `image_inputs: [{field: page_blob_ref, format_field: page_mime_type}]` and a stub provider. Assert: (a) output rows carry the response; (b) recorded call request `messages` contain the audit view and — the load-bearing assertion — `"base64" not in canonical_json(recorded_request)` and no `bytes` values anywhere in it; (c) a row whose blob ref is absent from the store produces an `on_error`-routed row with `reason == "blob_not_found"` while sibling rows succeed; (d) a text-only sibling pipeline's recorded request equals the pre-change dict shape `[{"role": "user", "content": "<prompt>"}]` exactly.
- [ ] **Step 2: Run — FAIL, then wire until PASS.** Follow the integration suite's existing engine-run harness; do not invent a new one.
- [ ] **Step 3: Commit** (`test(pipeline): llm image-input e2e — rasterized-page shape, bytes-free audit, row-level blob errors`)

---

### Task 7: Whole-tree gates and hand-off

**Files:**
- Modify: `docs/agents/recent-code-hints.md` (new entry: the ChatMessage seam — messages are `Sequence[ChatMessage]`; wire/audit projections are the only exits; never put image bytes in audit/logs/errors)

- [ ] **Step 1:** Full suite: `PYTHONPATH=$PWD/src /home/john/elspeth/.venv/bin/python -m pytest tests/ -n 24 -q` — confirm a real `N passed` line (silent zero-collection is VOID). Record N.
- [ ] **Step 2:** Trust-tier gate, corpus COMPARE (count, never tail): run `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth` on the pre-branch base and on HEAD; diff finding COUNTS. New findings from this work = fix them; the pre-existing corpus (elspeth-13f0cc04fb) is not yours to clear.
- [ ] **Step 3:** Wardline gate of record: `wardline scan . --fail-on ERROR --fail-on-inert --trust-pack scripts.wardline_pack --allow-custom-packs --local-only` — exit 0 required; fix findings at the boundary, not the sink.
- [ ] **Step 4:** mypy over the touched packages (Task 2 Step 5 command plus `src/elspeth/contracts`).
- [ ] **Step 5:** Write the `recent-code-hints.md` entry and commit it with any gate fixes: `git commit -m "docs(agents): recent-code-hints — ChatMessage seam and bytes-free audit rule"`.
- [ ] **Step 6:** STOP. Do not merge, do not touch the trust-tier allowlist, do not run branch-finish. Report: test count, lint-count delta, wardline exit, and the branch name for John's review.

---

## Self-review record

- Spec coverage: §3→Task 1, §4→Tasks 3-4, §5→Task 2, §6→Tasks 1/2/5/6, §7→Tasks 3/5/6, §8→Task 5 (json_schema+images pin), §9→every task's tests + Task 7, §10 respected (no pdf, no single-query output_fields, no composer/tutorial paths, no model_catalog flag).
- Type consistency: `ChatMessage`/`wire_messages`/`audit_messages`/`parts_hash`/`ImageInputConfig`/`resolve_image_parts` names and signatures match across Tasks 1-6.
- Known intentional deltas from spec prose: the serializer lives in `contracts/chat_parts.py` (not `providers/_content_parts.py`) because `AuditedLLMClient` sits in `plugins/infrastructure`, which must not import from `plugins/transforms` — the spec's "one shared serializer" requirement is met, one layer lower.
