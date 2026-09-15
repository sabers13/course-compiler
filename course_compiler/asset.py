"""Immutable content-addressed identity for one exact reusable asset."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


ASSET_REFERENCE_VERSION = "asset-reference/v1"

_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}\Z")


def _valid_digest(value: object) -> bool:
    return type(value) is str and _SHA256_HEX_RE.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class AssetReference:
    """Exact-byte identity, independent from provenance and media metadata."""

    reference_version: Literal["asset-reference/v1"]
    content_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.reference_version) is not str
            or self.reference_version != ASSET_REFERENCE_VERSION
        ):
            raise ValueError("asset reference version is unsupported")
        if not _valid_digest(self.content_sha256):
            raise ValueError("asset content digest is invalid")


__all__ = ["ASSET_REFERENCE_VERSION", "AssetReference"]
