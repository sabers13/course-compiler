"""Immutable canonical identity for one physical PDF page position."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .workflow import SourceEvidenceReference


PDF_PAGE_REFERENCE_VERSION = "pdf-page-reference/v1"


def _valid_source_reference(value: object) -> bool:
    """Revalidate the exact accepted source-evidence identity."""

    if type(value) is not SourceEvidenceReference:
        return False
    try:
        SourceEvidenceReference(
            value.reference_version,
            value.source_id,
            value.content_sha256,
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        return False
    return True


@dataclass(frozen=True, slots=True)
class PdfPageReference:
    """One-based physical page position within exact source evidence."""

    reference_version: Literal["pdf-page-reference/v1"]
    source_reference: SourceEvidenceReference
    physical_page_ordinal: int

    def __post_init__(self) -> None:
        if (
            type(self.reference_version) is not str
            or self.reference_version != PDF_PAGE_REFERENCE_VERSION
        ):
            raise ValueError("PDF page reference version is unsupported")
        if not _valid_source_reference(self.source_reference):
            raise ValueError("PDF page source reference is invalid")
        if (
            type(self.physical_page_ordinal) is not int
            or self.physical_page_ordinal < 1
        ):
            raise ValueError("PDF physical page ordinal is invalid")


__all__ = ["PDF_PAGE_REFERENCE_VERSION", "PdfPageReference"]
