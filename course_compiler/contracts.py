"""Versioned lecture input contract and content-safe validation."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Mapping, TypeAlias


DOCUMENT_CONTRACT_VERSION = "lecture-document/v1"

DiagnosticSeverity: TypeAlias = Literal["warning", "error"]
DiagnosticStage: TypeAlias = Literal["validation", "render"]

_DOCUMENT_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_CODE_FENCE_RE = re.compile(r"^(```+|~~~+)")
_DISPLAY_MATH_STARTS = frozenset(("[", r"\[", "# [", "$$"))
_DISPLAY_MATH_ENDS = frozenset(("]", r"\]", "$$"))


DIAGNOSTIC_MESSAGES: Mapping[str, str] = MappingProxyType(
    {
        "empty_source": "Source text is empty.",
        "invalid_document_id": "Document ID does not satisfy the v1 contract.",
        "invalid_order": "Document order does not satisfy the v1 contract.",
        "invalid_source_digest": "Source digest does not satisfy the v1 contract.",
        "invalid_source_text": "Source text does not satisfy the v1 contract.",
        "invalid_explicit_math": "Explicit math syntax is invalid; correct delimiters, braces or commands without changing meaning.",
        "renderer_exception": "The renderer failed while converting the document.",
        "short_source": "Source text is shorter than the legacy warning threshold.",
        "source_digest_mismatch": "Source digest does not match canonical source text.",
        "structural_postcondition_mismatch": "Rendered structure does not match input structure.",
        "unclosed_code_fence": "Source text contains an unclosed code fence.",
        "unclosed_display_math": "Source text contains an unclosed display-math block.",
        "unsupported_contract_version": "Document contract version is not supported.",
    }
)

_DIAGNOSTIC_CLASSIFICATIONS: Mapping[
    str, tuple[DiagnosticSeverity, DiagnosticStage]
] = MappingProxyType(
    {
        "empty_source": ("warning", "validation"),
        "short_source": ("warning", "validation"),
        "unclosed_code_fence": ("warning", "validation"),
        "unclosed_display_math": ("warning", "validation"),
        "unsupported_contract_version": ("error", "validation"),
        "invalid_document_id": ("error", "validation"),
        "invalid_order": ("error", "validation"),
        "invalid_source_text": ("error", "validation"),
        "invalid_source_digest": ("error", "validation"),
        "source_digest_mismatch": ("error", "validation"),
        "invalid_explicit_math": ("error", "render"),
        "renderer_exception": ("error", "render"),
        "structural_postcondition_mismatch": ("error", "render"),
    }
)


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    """Content-free identity for the exact canonical renderer input."""

    content_sha256: str


@dataclass(frozen=True, slots=True)
class LectureDocument:
    """The complete v1 input for one lecture renderer invocation."""

    contract_version: Literal["lecture-document/v1"]
    document_id: str
    order: int
    source_text: str = field(repr=False)
    provenance: SourceProvenance


@dataclass(frozen=True, slots=True)
class RenderDiagnostic:
    """A content-free diagnostic selected from fixed message templates."""

    code: str
    severity: DiagnosticSeverity
    stage: DiagnosticStage
    line: int | None

    def __post_init__(self) -> None:
        if self.code not in DIAGNOSTIC_MESSAGES:
            raise ValueError("diagnostic code is not registered")
        if (self.severity, self.stage) != _DIAGNOSTIC_CLASSIFICATIONS[self.code]:
            raise ValueError("diagnostic classification does not match registered code")
        if self.line is not None and (
            isinstance(self.line, bool)
            or not isinstance(self.line, int)
            or self.line < 1
        ):
            raise ValueError("diagnostic line must be a one-based integer or None")


def validate_document(document: LectureDocument) -> tuple[RenderDiagnostic, ...]:
    """Return deterministic, content-free diagnostics for one document."""

    diagnostics: list[RenderDiagnostic] = []

    if document.contract_version != DOCUMENT_CONTRACT_VERSION:
        diagnostics.append(_error("unsupported_contract_version"))

    if not _is_valid_document_id(document.document_id):
        diagnostics.append(_error("invalid_document_id"))

    if not _is_valid_order(document.order):
        diagnostics.append(_error("invalid_order"))

    source_bytes = _validated_source_bytes(document.source_text)
    if source_bytes is None:
        diagnostics.append(_error("invalid_source_text"))
    else:
        diagnostics.extend(_content_warnings(document.source_text))

    digest = (
        document.provenance.content_sha256
        if isinstance(document.provenance, SourceProvenance)
        else None
    )
    if not _is_valid_digest(digest):
        diagnostics.append(_error("invalid_source_digest"))
    elif source_bytes is not None:
        actual_digest = hashlib.sha256(source_bytes).hexdigest()
        if digest != actual_digest:
            diagnostics.append(_error("source_digest_mismatch"))

    return tuple(sorted(diagnostics, key=_diagnostic_sort_key))


def has_validation_errors(diagnostics: tuple[RenderDiagnostic, ...]) -> bool:
    """Return whether validation diagnostics contain at least one error."""

    return any(
        diagnostic.stage == "validation" and diagnostic.severity == "error"
        for diagnostic in diagnostics
    )


def _is_valid_document_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and value not in (".", "..")
        and _DOCUMENT_ID_RE.fullmatch(value) is not None
    )


def _is_valid_order(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and 1 <= value <= 9999


def _is_valid_digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _validated_source_bytes(source_text: object) -> bytes | None:
    if not isinstance(source_text, str):
        return None
    if source_text.startswith("\ufeff") or "\x00" in source_text or "\r" in source_text:
        return None
    try:
        return source_text.encode("utf-8")
    except UnicodeEncodeError:
        return None


def _content_warnings(source_text: str) -> list[RenderDiagnostic]:
    warnings: list[RenderDiagnostic] = []
    if len(source_text) == 0:
        warnings.append(_warning("empty_source"))
    elif len(source_text) < 1000:
        warnings.append(_warning("short_source"))

    unclosed_code_line, unclosed_math_line = _unclosed_block_lines(source_text)
    if unclosed_code_line is not None:
        warnings.append(_warning("unclosed_code_fence", line=unclosed_code_line))
    if unclosed_math_line is not None:
        warnings.append(_warning("unclosed_display_math", line=unclosed_math_line))
    return warnings


def _unclosed_block_lines(source_text: str) -> tuple[int | None, int | None]:
    code_fence: str | None = None
    code_line: int | None = None
    in_display_math = False
    math_line: int | None = None

    for line_number, line in enumerate(source_text.splitlines(), start=1):
        stripped = line.strip()
        if code_fence is not None:
            if stripped.startswith(code_fence):
                code_fence = None
                code_line = None
            continue
        if in_display_math:
            if stripped in _DISPLAY_MATH_ENDS:
                in_display_math = False
                math_line = None
            continue
        if match := _CODE_FENCE_RE.match(stripped):
            code_fence = match.group(1)
            code_line = line_number
            continue
        if stripped in _DISPLAY_MATH_STARTS:
            in_display_math = True
            math_line = line_number

    return code_line, math_line


def _error(code: str) -> RenderDiagnostic:
    return RenderDiagnostic(code=code, severity="error", stage="validation", line=None)


def _warning(code: str, *, line: int | None = None) -> RenderDiagnostic:
    return RenderDiagnostic(code=code, severity="warning", stage="validation", line=line)


def _diagnostic_sort_key(diagnostic: RenderDiagnostic) -> tuple[str, int]:
    return diagnostic.code, diagnostic.line or 0
