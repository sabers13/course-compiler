"""Deterministic in-memory PDF page preview and region extraction."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from typing import Literal, TypeAlias

from .asset import ASSET_REFERENCE_VERSION, AssetReference
from .pdf_page import PdfPageReference


PDF_VISUAL_EXTRACTION_PROFILE = "pdftoppm-cropbox-region-png-144dpi/v1"
PDF_VISUAL_EXTRACTION_TIMEOUT_SECONDS = 30
PDF_PAGE_PREVIEW_PROFILE = "pdftoppm-cropbox-page-png-144dpi/v1"
PDF_PAGE_PREVIEW_COORDINATE_FRAME = (
    "pdftoppm-cropbox-pixels-top-left-144dpi/v1"
)

_MAX_COORDINATE = 2_147_483_647
_MAX_REGION_DIMENSION = 8192
_MAX_REGION_AREA = 16_777_216
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_IHDR_TOTAL_SIZE = 33

_DIAGNOSTICS = {
    "invalid_extraction_input": (
        "input",
        "The PDF visual extraction input is invalid.",
    ),
    "source_bytes_mismatch": (
        "input",
        "The source PDF bytes do not match the page reference.",
    ),
    "renderer_unavailable": (
        "renderer",
        "The fixed local PDF renderer is unavailable.",
    ),
    "renderer_timeout": (
        "renderer",
        "The fixed local PDF renderer timed out.",
    ),
    "renderer_failed": (
        "renderer",
        "The fixed local PDF renderer reported a failure.",
    ),
    "invalid_rendered_output": (
        "output",
        "The rendered PNG output is invalid.",
    ),
    "region_out_of_bounds": (
        "output",
        "The rendered region does not have the requested dimensions.",
    ),
    "extraction_exception": (
        "adapter",
        "The PDF visual extraction adapter failed.",
    ),
}

_PAGE_PREVIEW_DIAGNOSTICS = {
    "invalid_page_preview_input": (
        "input",
        "The PDF page preview input is invalid.",
    ),
    "source_bytes_mismatch": (
        "input",
        "The source PDF bytes do not match the page reference.",
    ),
    "renderer_unavailable": (
        "renderer",
        "The fixed local PDF renderer is unavailable.",
    ),
    "renderer_timeout": (
        "renderer",
        "The fixed local PDF renderer timed out.",
    ),
    "renderer_failed": (
        "renderer",
        "The fixed local PDF renderer reported a failure.",
    ),
    "invalid_rendered_output": (
        "output",
        "The rendered PNG output is invalid.",
    ),
    "page_preview_dimensions_unsupported": (
        "output",
        "The rendered PDF page dimensions are unsupported.",
    ),
    "page_preview_exception": (
        "adapter",
        "The PDF page preview adapter failed.",
    ),
}

__all__ = [
    "PDF_PAGE_PREVIEW_COORDINATE_FRAME",
    "PDF_PAGE_PREVIEW_PROFILE",
    "PDF_VISUAL_EXTRACTION_PROFILE",
    "PDF_VISUAL_EXTRACTION_TIMEOUT_SECONDS",
    "ExtractedPdfVisual",
    "RenderedPdfPagePreview",
    "PdfPagePreviewDiagnostic",
    "PdfPagePreviewFailure",
    "PdfPagePreviewResult",
    "PdfVisualExtractionDiagnostic",
    "PdfVisualExtractionFailure",
    "PdfVisualExtractionResult",
    "extract_pdf_page_region",
    "render_pdf_page_preview",
]


@dataclass(frozen=True, slots=True)
class RenderedPdfPagePreview:
    """Exact complete-page PNG in the coordinate frame used by T022."""

    status: Literal["rendered"]
    preview_profile: Literal["pdftoppm-cropbox-page-png-144dpi/v1"]
    coordinate_frame: Literal[
        "pdftoppm-cropbox-pixels-top-left-144dpi/v1"
    ]
    page_reference: PdfPageReference
    width_px: int
    height_px: int
    format: Literal["png"]
    content_bytes: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.status) is not str or self.status != "rendered":
            raise ValueError("rendered PDF page preview status is invalid")
        if (
            type(self.preview_profile) is not str
            or self.preview_profile != PDF_PAGE_PREVIEW_PROFILE
        ):
            raise ValueError("PDF page preview profile is invalid")
        if (
            type(self.coordinate_frame) is not str
            or self.coordinate_frame != PDF_PAGE_PREVIEW_COORDINATE_FRAME
        ):
            raise ValueError("PDF page preview coordinate frame is invalid")
        if not _valid_page_reference(self.page_reference):
            raise ValueError("rendered PDF page preview page reference is invalid")
        if not _valid_preview_dimensions(self.width_px, self.height_px):
            raise ValueError("rendered PDF page preview dimensions are invalid")
        if type(self.format) is not str or self.format != "png":
            raise ValueError("rendered PDF page preview format is invalid")
        if type(self.content_bytes) is not bytes or not self.content_bytes:
            raise ValueError("rendered PDF page preview content is invalid")
        dimensions = _png_dimensions(self.content_bytes)
        if dimensions is None:
            raise ValueError("rendered PDF page preview content is invalid")
        if dimensions != (self.width_px, self.height_px):
            raise ValueError("rendered PDF page preview dimensions are invalid")


@dataclass(frozen=True, slots=True)
class PdfPagePreviewDiagnostic:
    """One registered, fixed, content-safe page-preview diagnostic."""

    code: str
    classification: Literal["input", "renderer", "output", "adapter"]
    message: str

    def __post_init__(self) -> None:
        if type(self.code) is not str or self.code not in _PAGE_PREVIEW_DIAGNOSTICS:
            raise ValueError("PDF page preview diagnostic code is not registered")
        classification, message = _PAGE_PREVIEW_DIAGNOSTICS[self.code]
        if (
            type(self.classification) is not str
            or type(self.message) is not str
            or self.classification != classification
            or self.message != message
        ):
            raise ValueError("PDF page preview diagnostic fields are not registered")


@dataclass(frozen=True, slots=True)
class PdfPagePreviewFailure:
    """A fixed failure with no source, renderer output, or exception details."""

    status: Literal["pdf_page_preview_failed"]
    diagnostics: tuple[PdfPagePreviewDiagnostic, ...]

    def __post_init__(self) -> None:
        if type(self.status) is not str or self.status != "pdf_page_preview_failed":
            raise ValueError("PDF page preview failure status is invalid")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) != 1
            or not _valid_page_preview_diagnostic(self.diagnostics[0])
        ):
            raise ValueError("PDF page preview failure diagnostics are invalid")


PdfPagePreviewResult: TypeAlias = RenderedPdfPagePreview | PdfPagePreviewFailure


@dataclass(frozen=True, slots=True)
class ExtractedPdfVisual:
    """Exact PNG bytes and the minimum provenance needed to reproduce them."""

    status: Literal["extracted"]
    extraction_profile: Literal["pdftoppm-cropbox-region-png-144dpi/v1"]
    page_reference: PdfPageReference
    left_px: int
    top_px: int
    width_px: int
    height_px: int
    asset_reference: AssetReference
    format: Literal["png"]
    content_bytes: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.status) is not str or self.status != "extracted":
            raise ValueError("extracted PDF visual status is invalid")
        if (
            type(self.extraction_profile) is not str
            or self.extraction_profile != PDF_VISUAL_EXTRACTION_PROFILE
        ):
            raise ValueError("PDF visual extraction profile is invalid")
        if not _valid_page_reference(self.page_reference):
            raise ValueError("extracted PDF visual page reference is invalid")
        if not _valid_region(
            self.left_px,
            self.top_px,
            self.width_px,
            self.height_px,
        ):
            raise ValueError("extracted PDF visual region is invalid")
        if not _valid_asset_reference(self.asset_reference):
            raise ValueError("extracted PDF visual asset reference is invalid")
        if type(self.format) is not str or self.format != "png":
            raise ValueError("extracted PDF visual format is invalid")
        if type(self.content_bytes) is not bytes or not self.content_bytes:
            raise ValueError("extracted PDF visual content is invalid")
        dimensions = _png_dimensions(self.content_bytes)
        if dimensions is None:
            raise ValueError("extracted PDF visual content is invalid")
        if dimensions != (self.width_px, self.height_px):
            raise ValueError("extracted PDF visual dimensions are invalid")
        if (
            hashlib.sha256(self.content_bytes).hexdigest()
            != self.asset_reference.content_sha256
        ):
            raise ValueError("extracted PDF visual asset reference is invalid")


@dataclass(frozen=True, slots=True)
class PdfVisualExtractionDiagnostic:
    """One registered, fixed, content-safe extraction diagnostic."""

    code: str
    classification: Literal["input", "renderer", "output", "adapter"]
    message: str

    def __post_init__(self) -> None:
        if type(self.code) is not str or self.code not in _DIAGNOSTICS:
            raise ValueError("PDF visual extraction diagnostic code is not registered")
        classification, message = _DIAGNOSTICS[self.code]
        if (
            type(self.classification) is not str
            or type(self.message) is not str
            or self.classification != classification
            or self.message != message
        ):
            raise ValueError("PDF visual extraction diagnostic fields are not registered")


@dataclass(frozen=True, slots=True)
class PdfVisualExtractionFailure:
    """A fixed failure with no source, renderer output, or exception details."""

    status: Literal["pdf_visual_extraction_failed"]
    diagnostics: tuple[PdfVisualExtractionDiagnostic, ...]

    def __post_init__(self) -> None:
        if type(self.status) is not str or self.status != "pdf_visual_extraction_failed":
            raise ValueError("PDF visual extraction failure status is invalid")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) != 1
            or not _valid_diagnostic(self.diagnostics[0])
        ):
            raise ValueError("PDF visual extraction failure diagnostics are invalid")


PdfVisualExtractionResult: TypeAlias = (
    ExtractedPdfVisual | PdfVisualExtractionFailure
)


def render_pdf_page_preview(
    source_pdf_bytes: bytes,
    page_reference: PdfPageReference,
) -> PdfPagePreviewResult:
    """Render one complete CropBox page in T022's exact pixel frame.

    Crop coordinates selected from this preview can be supplied directly to
    :func:`extract_pdf_page_region`; no coordinate transform is required.
    """

    if type(source_pdf_bytes) is not bytes:
        raise TypeError("source_pdf_bytes must be exactly bytes")
    if type(page_reference) is not PdfPageReference:
        raise TypeError("page_reference must be exactly PdfPageReference")

    try:
        if not _valid_page_reference(page_reference):
            return _page_preview_failure("invalid_page_preview_input")
        if (
            hashlib.sha256(source_pdf_bytes).hexdigest()
            != page_reference.source_reference.content_sha256
        ):
            return _page_preview_failure("source_bytes_mismatch")
    except Exception:
        return _page_preview_failure("invalid_page_preview_input")

    try:
        page_ordinal = str(page_reference.physical_page_ordinal)
        argv = (
            "pdftoppm",
            "-f",
            page_ordinal,
            "-l",
            page_ordinal,
            "-singlefile",
            "-r",
            "144",
            "-cropbox",
            "-png",
            "-",
        )
    except Exception:
        return _page_preview_failure("page_preview_exception")
    try:
        completed = _run_pdftoppm(argv, source_pdf_bytes)
    except FileNotFoundError:
        return _page_preview_failure("renderer_unavailable")
    except subprocess.TimeoutExpired:
        return _page_preview_failure("renderer_timeout")
    except Exception:
        return _page_preview_failure("page_preview_exception")

    try:
        if completed.returncode != 0:
            return _page_preview_failure("renderer_failed")
        if type(completed.stdout) is not bytes:
            return _page_preview_failure("invalid_rendered_output")
        dimensions = _png_dimensions(completed.stdout)
        if dimensions is None:
            return _page_preview_failure("invalid_rendered_output")
        width_px, height_px = dimensions
        if not _valid_preview_dimensions(width_px, height_px):
            return _page_preview_failure("page_preview_dimensions_unsupported")
        return RenderedPdfPagePreview(
            status="rendered",
            preview_profile=PDF_PAGE_PREVIEW_PROFILE,
            coordinate_frame=PDF_PAGE_PREVIEW_COORDINATE_FRAME,
            page_reference=page_reference,
            width_px=width_px,
            height_px=height_px,
            format="png",
            content_bytes=completed.stdout,
        )
    except Exception:
        return _page_preview_failure("page_preview_exception")


def extract_pdf_page_region(
    source_pdf_bytes: bytes,
    page_reference: PdfPageReference,
    *,
    left_px: int,
    top_px: int,
    width_px: int,
    height_px: int,
) -> PdfVisualExtractionResult:
    """Render one caller-selected CropBox region through the fixed profile."""

    if type(source_pdf_bytes) is not bytes:
        raise TypeError("source_pdf_bytes must be exactly bytes")
    if type(page_reference) is not PdfPageReference:
        raise TypeError("page_reference must be exactly PdfPageReference")
    for name, value in (
        ("left_px", left_px),
        ("top_px", top_px),
        ("width_px", width_px),
        ("height_px", height_px),
    ):
        if type(value) is not int:
            raise TypeError(f"{name} must be exactly int")

    try:
        if not _valid_page_reference(page_reference) or not _valid_region(
            left_px,
            top_px,
            width_px,
            height_px,
        ):
            return _failure("invalid_extraction_input")
        if (
            hashlib.sha256(source_pdf_bytes).hexdigest()
            != page_reference.source_reference.content_sha256
        ):
            return _failure("source_bytes_mismatch")
    except Exception:
        return _failure("invalid_extraction_input")

    try:
        page_ordinal = str(page_reference.physical_page_ordinal)
        argv = (
            "pdftoppm",
            "-f",
            page_ordinal,
            "-l",
            page_ordinal,
            "-singlefile",
            "-r",
            "144",
            "-cropbox",
            "-x",
            str(left_px),
            "-y",
            str(top_px),
            "-W",
            str(width_px),
            "-H",
            str(height_px),
            "-png",
            "-",
        )
    except Exception:
        return _failure("extraction_exception")
    try:
        completed = _run_pdftoppm(argv, source_pdf_bytes)
    except FileNotFoundError:
        return _failure("renderer_unavailable")
    except subprocess.TimeoutExpired:
        return _failure("renderer_timeout")
    except Exception:
        return _failure("extraction_exception")

    try:
        if completed.returncode != 0:
            return _failure("renderer_failed")
        if type(completed.stdout) is not bytes:
            return _failure("invalid_rendered_output")
        dimensions = _png_dimensions(completed.stdout)
        if dimensions is None:
            return _failure("invalid_rendered_output")
        if dimensions != (width_px, height_px):
            return _failure("region_out_of_bounds")
        asset_reference = AssetReference(
            ASSET_REFERENCE_VERSION,
            hashlib.sha256(completed.stdout).hexdigest(),
        )
        return ExtractedPdfVisual(
            status="extracted",
            extraction_profile=PDF_VISUAL_EXTRACTION_PROFILE,
            page_reference=page_reference,
            left_px=left_px,
            top_px=top_px,
            width_px=width_px,
            height_px=height_px,
            asset_reference=asset_reference,
            format="png",
            content_bytes=completed.stdout,
        )
    except Exception:
        return _failure("extraction_exception")


def _valid_page_reference(value: object) -> bool:
    if type(value) is not PdfPageReference:
        return False
    try:
        PdfPageReference(
            value.reference_version,
            value.source_reference,
            value.physical_page_ordinal,
        )
    except Exception:
        return False
    return True


def _valid_asset_reference(value: object) -> bool:
    if type(value) is not AssetReference:
        return False
    try:
        AssetReference(value.reference_version, value.content_sha256)
    except Exception:
        return False
    return True


def _valid_region(left_px: int, top_px: int, width_px: int, height_px: int) -> bool:
    return (
        type(left_px) is int
        and 0 <= left_px <= _MAX_COORDINATE
        and type(top_px) is int
        and 0 <= top_px <= _MAX_COORDINATE
        and type(width_px) is int
        and 1 <= width_px <= _MAX_REGION_DIMENSION
        and type(height_px) is int
        and 1 <= height_px <= _MAX_REGION_DIMENSION
        and width_px * height_px <= _MAX_REGION_AREA
    )


def _valid_preview_dimensions(width_px: int, height_px: int) -> bool:
    return (
        type(width_px) is int
        and 1 <= width_px <= _MAX_REGION_DIMENSION
        and type(height_px) is int
        and 1 <= height_px <= _MAX_REGION_DIMENSION
        and width_px * height_px <= _MAX_REGION_AREA
    )


def _run_pdftoppm(
    argv: tuple[str, ...], source_pdf_bytes: bytes
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        input=source_pdf_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=PDF_VISUAL_EXTRACTION_TIMEOUT_SECONDS,
        shell=False,
    )


def _png_dimensions(content: bytes) -> tuple[int, int] | None:
    if type(content) is not bytes or len(content) < _IHDR_TOTAL_SIZE:
        return None
    if content[:8] != _PNG_SIGNATURE:
        return None
    if int.from_bytes(content[8:12], "big") != 13 or content[12:16] != b"IHDR":
        return None
    width = int.from_bytes(content[16:20], "big")
    height = int.from_bytes(content[20:24], "big")
    if width < 1 or height < 1:
        return None
    return width, height


def _valid_diagnostic(value: object) -> bool:
    if type(value) is not PdfVisualExtractionDiagnostic:
        return False
    try:
        PdfVisualExtractionDiagnostic(value.code, value.classification, value.message)
    except Exception:
        return False
    return True


def _valid_page_preview_diagnostic(value: object) -> bool:
    if type(value) is not PdfPagePreviewDiagnostic:
        return False
    try:
        PdfPagePreviewDiagnostic(value.code, value.classification, value.message)
    except Exception:
        return False
    return True


def _failure(code: str) -> PdfVisualExtractionFailure:
    classification, message = _DIAGNOSTICS[code]
    return PdfVisualExtractionFailure(
        status="pdf_visual_extraction_failed",
        diagnostics=(
            PdfVisualExtractionDiagnostic(
                code=code,
                classification=classification,
                message=message,
            ),
        ),
    )


def _page_preview_failure(code: str) -> PdfPagePreviewFailure:
    classification, message = _PAGE_PREVIEW_DIAGNOSTICS[code]
    return PdfPagePreviewFailure(
        status="pdf_page_preview_failed",
        diagnostics=(
            PdfPagePreviewDiagnostic(
                code=code,
                classification=classification,
                message=message,
            ),
        ),
    )
