"""Pure single-document renderer protocol and fragment result states."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from typing import Literal, Protocol, TypeAlias, runtime_checkable

from .contracts import (
    DOCUMENT_CONTRACT_VERSION,
    LectureDocument,
    RenderDiagnostic,
    SourceProvenance,
    _is_valid_digest,
    _is_valid_document_id,
    _is_valid_order,
)


LEGACY_RENDER_PROFILE = "legacy-markdown-to-tex/v1"

RenderedStatus: TypeAlias = Literal["rendered", "rendered_with_warnings"]
_SEMVER_PRERELEASE_IDENTIFIER = (
    r"(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
)
_SEMVER_RE = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    rf"(?:-{_SEMVER_PRERELEASE_IDENTIFIER}"
    rf"(?:\.{_SEMVER_PRERELEASE_IDENTIFIER})*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
)
_RENDERER_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")


@dataclass(frozen=True, slots=True)
class RendererIdentity:
    renderer_name: str
    renderer_version: str
    contract_version: Literal["lecture-document/v1"]
    render_profile: Literal["legacy-markdown-to-tex/v1"]

    def __post_init__(self) -> None:
        if not isinstance(self.renderer_name, str) or _RENDERER_NAME_RE.fullmatch(self.renderer_name) is None:
            raise ValueError("renderer name is invalid")
        if not isinstance(self.renderer_version, str) or _SEMVER_RE.fullmatch(self.renderer_version) is None:
            raise ValueError("renderer version is not semantic-version syntax")
        if self.contract_version != DOCUMENT_CONTRACT_VERSION:
            raise ValueError("renderer contract version is unsupported")
        if self.render_profile != LEGACY_RENDER_PROFILE:
            raise ValueError("renderer profile is unsupported")


@dataclass(frozen=True, slots=True)
class DocumentReference:
    contract_version: Literal["lecture-document/v1"]
    document_id: str
    order: int
    content_sha256: str

    def __post_init__(self) -> None:
        if self.contract_version != DOCUMENT_CONTRACT_VERSION:
            raise ValueError("document reference contract version is unsupported")
        if not _is_valid_document_id(self.document_id):
            raise ValueError("document reference ID is invalid")
        if not _is_valid_order(self.order):
            raise ValueError("document reference order is invalid")
        if not _is_valid_digest(self.content_sha256):
            raise ValueError("document reference digest is invalid")


@dataclass(frozen=True, slots=True)
class StructuralMetrics:
    input_characters: int
    input_lines: int
    input_headings: int
    output_headings: int
    input_code_blocks: int
    output_code_blocks: int
    input_display_math_blocks: int
    output_display_math_blocks: int
    input_tables: int
    output_tables: int

    def __post_init__(self) -> None:
        if any(
            isinstance(getattr(self, item.name), bool)
            or not isinstance(getattr(self, item.name), int)
            or getattr(self, item.name) < 0
            for item in fields(self)
        ):
            raise ValueError("structural metrics must be non-negative integers")


@dataclass(frozen=True, slots=True)
class RenderedLecture:
    status: RenderedStatus
    document: DocumentReference
    renderer: RendererIdentity
    tex_fragment: str = field(repr=False)
    metrics: StructuralMetrics
    diagnostics: tuple[RenderDiagnostic, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.document, DocumentReference):
            raise ValueError("rendered result document must be DocumentReference")
        if not isinstance(self.renderer, RendererIdentity):
            raise ValueError("rendered result renderer must be RendererIdentity")
        if not isinstance(self.metrics, StructuralMetrics):
            raise ValueError("rendered result metrics must be StructuralMetrics")
        _validate_fragment(self.tex_fragment)
        _require_diagnostic_tuple(self.diagnostics)
        if not structural_postconditions_match(self.metrics):
            raise ValueError("rendered results require matching structural metrics")
        if self.status == "rendered":
            if self.diagnostics:
                raise ValueError("rendered results cannot contain diagnostics")
        elif self.status == "rendered_with_warnings":
            if not self.diagnostics or any(
                diagnostic.severity != "warning" for diagnostic in self.diagnostics
            ):
                raise ValueError("rendered-with-warnings results require warnings only")
        else:
            raise ValueError("rendered result status is invalid")


@dataclass(frozen=True, slots=True)
class RejectedLecture:
    status: Literal["validation_failed"]
    document: DocumentReference | None
    diagnostics: tuple[RenderDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "validation_failed":
            raise ValueError("rejected result status is invalid")
        if self.document is not None and not isinstance(
            self.document, DocumentReference
        ):
            raise ValueError(
                "rejected result document must be DocumentReference or None"
            )
        _require_diagnostic_tuple(self.diagnostics, non_empty=True)
        if any(
            diagnostic.stage != "validation" or diagnostic.severity != "error"
            for diagnostic in self.diagnostics
        ):
            raise ValueError("rejected results require validation-stage errors")


@dataclass(frozen=True, slots=True)
class RendererFailure:
    status: Literal["render_failed"]
    document: DocumentReference
    renderer: RendererIdentity
    diagnostics: tuple[RenderDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "render_failed":
            raise ValueError("renderer-failure status is invalid")
        if not isinstance(self.document, DocumentReference):
            raise ValueError("renderer failure document must be DocumentReference")
        if not isinstance(self.renderer, RendererIdentity):
            raise ValueError("renderer failure renderer must be RendererIdentity")
        _require_diagnostic_tuple(self.diagnostics, non_empty=True)
        if any(
            diagnostic.stage != "render" or diagnostic.severity != "error"
            for diagnostic in self.diagnostics
        ):
            raise ValueError("renderer failures require render-stage errors")


LectureRenderResult: TypeAlias = RenderedLecture | RejectedLecture | RendererFailure


@runtime_checkable
class LectureRenderer(Protocol):
    """A pure boundary for rendering exactly one document to one fragment."""

    identity: RendererIdentity

    def render(self, document: LectureDocument) -> LectureRenderResult:
        ...


def document_reference(document: LectureDocument) -> DocumentReference | None:
    """Build a safe reference, or return None when reference fields are invalid."""

    digest = (
        document.provenance.content_sha256
        if isinstance(document.provenance, SourceProvenance)
        else None
    )
    if (
        document.contract_version != DOCUMENT_CONTRACT_VERSION
        or not _is_valid_document_id(document.document_id)
        or not _is_valid_order(document.order)
        or not _is_valid_digest(digest)
    ):
        return None
    return DocumentReference(
        contract_version=document.contract_version,
        document_id=document.document_id,
        order=document.order,
        content_sha256=digest,
    )


def structural_postconditions_match(metrics: StructuralMetrics) -> bool:
    """Return whether fail-closed renderer structure postconditions hold."""

    return (
        metrics.input_headings == metrics.output_headings
        and metrics.input_code_blocks == metrics.output_code_blocks
        and metrics.input_display_math_blocks == metrics.output_display_math_blocks
        and metrics.input_tables == metrics.output_tables
    )


def _validate_fragment(tex_fragment: object) -> None:
    if not isinstance(tex_fragment, str):
        raise ValueError("TeX fragment must be a string")
    try:
        tex_fragment.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("TeX fragment must be UTF-8 encodable") from None


def _require_diagnostic_tuple(
    diagnostics: object, *, non_empty: bool = False
) -> None:
    if not isinstance(diagnostics, tuple) or any(
        not isinstance(diagnostic, RenderDiagnostic) for diagnostic in diagnostics
    ):
        raise ValueError("diagnostics must be an immutable diagnostic tuple")
    if non_empty and not diagnostics:
        raise ValueError("diagnostics must not be empty")
