"""Trusted-local source-evidence ingestion application operation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal, TypeAlias

from .source_persistence import (
    LocalSourceEvidenceStore,
    SourceEvidencePayload,
    SourcePersistenceFailure,
)
from .workflow import SOURCE_EVIDENCE_REFERENCE_VERSION, SourceEvidenceReference


__all__ = [
    "SourceEvidenceIngestionDiagnostic",
    "SourceEvidenceIngestionFailure",
    "SourceEvidenceIngestionResult",
    "ingest_source_evidence",
]


_DIAGNOSTICS = {
    "invalid_source_ingestion_input": (
        "input",
        "The source-evidence ingestion input is invalid.",
    ),
    "source_identity_conflict": (
        "identity",
        "The source-evidence ID is already bound to different content.",
    ),
    "source_store_failed": (
        "storage",
        "The local source-evidence store failed.",
    ),
    "source_ingestion_exception": (
        "application",
        "The source-evidence ingestion operation failed.",
    ),
}


@dataclass(frozen=True, slots=True)
class SourceEvidenceIngestionDiagnostic:
    """One fixed, content-safe source-evidence ingestion diagnostic."""

    code: str
    classification: Literal["input", "identity", "storage", "application"]
    message: str

    def __post_init__(self) -> None:
        if self.code not in _DIAGNOSTICS:
            raise ValueError("source ingestion diagnostic code is not registered")
        classification, message = _DIAGNOSTICS[self.code]
        if self.classification != classification or self.message != message:
            raise ValueError("source ingestion diagnostic fields are not registered")


@dataclass(frozen=True, slots=True)
class SourceEvidenceIngestionFailure:
    """A fail-closed ingestion failure with one fixed redacted diagnostic."""

    status: Literal["source_ingestion_failed"]
    diagnostics: tuple[SourceEvidenceIngestionDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "source_ingestion_failed":
            raise ValueError("source ingestion failure status is invalid")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) != 1
            or not _is_valid_diagnostic(self.diagnostics[0])
        ):
            raise ValueError("source ingestion failure diagnostics are invalid")


SourceEvidenceIngestionResult: TypeAlias = (
    SourceEvidencePayload | SourceEvidenceIngestionFailure
)


def ingest_source_evidence(
    source_id: str,
    payload: bytes,
    *,
    source_store: LocalSourceEvidenceStore,
) -> SourceEvidenceIngestionResult:
    """Hash exact caller bytes and save them once through the T008 store."""

    if type(source_id) is not str:
        raise TypeError("source_id must be exactly str")
    if type(payload) is not bytes:
        raise TypeError("payload must be exactly bytes")
    if type(source_store) is not LocalSourceEvidenceStore:
        raise TypeError("source_store must be exactly LocalSourceEvidenceStore")

    try:
        try:
            reference = SourceEvidenceReference(
                SOURCE_EVIDENCE_REFERENCE_VERSION,
                source_id,
                hashlib.sha256(payload).hexdigest(),
            )
        except (TypeError, ValueError):
            return _failure("invalid_source_ingestion_input")

        result = source_store.save(reference, payload)
        if type(result) is SourceEvidencePayload:
            return result
        if type(result) is SourcePersistenceFailure:
            if _failure_code(result) == "immutable_identity_conflict":
                return _failure("source_identity_conflict")
            return _failure("source_store_failed")
        return _failure("source_ingestion_exception")
    except Exception:
        return _failure("source_ingestion_exception")


def _failure_code(value: SourcePersistenceFailure) -> str | None:
    try:
        diagnostic = value.diagnostics[0]
        return diagnostic.code if type(diagnostic.code) is str else None
    except (AttributeError, IndexError, KeyError, RecursionError, TypeError, ValueError):
        return None


def _is_valid_diagnostic(value: object) -> bool:
    if type(value) is not SourceEvidenceIngestionDiagnostic:
        return False
    try:
        SourceEvidenceIngestionDiagnostic(
            value.code,
            value.classification,
            value.message,
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        return False
    return True


def _failure(code: str) -> SourceEvidenceIngestionFailure:
    classification, message = _DIAGNOSTICS[code]
    return SourceEvidenceIngestionFailure(
        status="source_ingestion_failed",
        diagnostics=(SourceEvidenceIngestionDiagnostic(code, classification, message),),
    )
