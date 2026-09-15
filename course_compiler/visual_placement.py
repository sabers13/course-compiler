"""Immutable, versioned, content-free visual-placement relation.

A `VisualPlacement` associates exactly one accepted T002 `DocumentReference`,
one document-relative source-text code-point offset, and one accepted T021
`AssetReference`. It carries no asset bytes, extraction provenance, or
layout/display metadata, and it performs no rendering, compiler staging,
persistence, or workflow behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .asset import AssetReference
from .rendering import DocumentReference


VISUAL_PLACEMENT_VERSION = "visual-placement/v1"


def _valid_document_reference(value: object) -> bool:
    """Revalidate the exact accepted document identity.

    The accepted T002 `DocumentReference` validates some fields with
    `isinstance`-based checks, so a forged exact-type instance can carry a
    hostile field value whose comparison raises an arbitrary ordinary
    `Exception` during constructor revalidation. Any such ordinary exception
    must be treated as an invalid nested reference rather than escaping with
    caller-controlled content; only `BaseException` itself is left
    unswallowed.
    """

    if type(value) is not DocumentReference:
        return False
    try:
        DocumentReference(
            value.contract_version,
            value.document_id,
            value.order,
            value.content_sha256,
        )
    except Exception:
        return False
    return True


def _valid_asset_reference(value: object) -> bool:
    """Revalidate the exact accepted asset identity."""

    if type(value) is not AssetReference:
        return False
    try:
        AssetReference(value.reference_version, value.content_sha256)
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        return False
    return True


def _valid_source_text_offset(value: object) -> bool:
    return type(value) is int and value >= 0


@dataclass(frozen=True, slots=True)
class VisualPlacement:
    """One exact document, source-text offset, and asset relation."""

    placement_version: Literal["visual-placement/v1"]
    document_reference: DocumentReference
    source_text_offset: int
    asset_reference: AssetReference

    def __post_init__(self) -> None:
        if (
            type(self.placement_version) is not str
            or self.placement_version != VISUAL_PLACEMENT_VERSION
        ):
            raise ValueError("visual placement version is unsupported")
        if not _valid_document_reference(self.document_reference):
            raise ValueError("visual placement document reference is invalid")
        if not _valid_source_text_offset(self.source_text_offset):
            raise ValueError("visual placement source-text offset is invalid")
        if not _valid_asset_reference(self.asset_reference):
            raise ValueError("visual placement asset reference is invalid")


__all__ = ["VISUAL_PLACEMENT_VERSION", "VisualPlacement"]
