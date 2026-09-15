"""Trusted-local T027 workflow-artifact ingestion application operation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal, TypeAlias

from .workflow_artifact_persistence import (
    LocalWorkflowArtifactStore,
    WorkflowArtifactPayload,
    WorkflowArtifactPersistenceFailure,
)
from .workflow import (
    ARTIFACT_PRODUCERS,
    WORKFLOW_ARTIFACT_REFERENCE_VERSION,
    WorkflowArtifactReference,
)


__all__ = [
    "WorkflowArtifactIngestionDiagnostic",
    "WorkflowArtifactIngestionFailure",
    "WorkflowArtifactIngestionResult",
    "ingest_workflow_artifact",
]


_DIAGNOSTICS = {
    "invalid_workflow_artifact_ingestion_input": (
        "input",
        "The workflow-artifact ingestion input is invalid.",
    ),
    "workflow_artifact_identity_conflict": (
        "identity",
        "The workflow-artifact ID is already bound to different content.",
    ),
    "workflow_artifact_store_failed": (
        "storage",
        "The local workflow-artifact store failed.",
    ),
    "workflow_artifact_ingestion_exception": (
        "application",
        "The workflow-artifact ingestion operation failed.",
    ),
}


@dataclass(frozen=True, slots=True)
class WorkflowArtifactIngestionDiagnostic:
    """One fixed, content-safe workflow-artifact ingestion diagnostic."""

    code: str
    classification: Literal["input", "identity", "storage", "application"]
    message: str

    def __post_init__(self) -> None:
        if self.code not in _DIAGNOSTICS:
            raise ValueError("workflow artifact ingestion diagnostic code is not registered")
        classification, message = _DIAGNOSTICS[self.code]
        if self.classification != classification or self.message != message:
            raise ValueError("workflow artifact ingestion diagnostic fields are not registered")


@dataclass(frozen=True, slots=True)
class WorkflowArtifactIngestionFailure:
    """A fail-closed ingestion failure with one fixed redacted diagnostic."""

    status: Literal["workflow_artifact_ingestion_failed"]
    diagnostics: tuple[WorkflowArtifactIngestionDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "workflow_artifact_ingestion_failed":
            raise ValueError("workflow artifact ingestion failure status is invalid")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) != 1
            or not _is_valid_diagnostic(self.diagnostics[0])
        ):
            raise ValueError("workflow artifact ingestion failure diagnostics are invalid")


WorkflowArtifactIngestionResult: TypeAlias = (
    WorkflowArtifactPayload | WorkflowArtifactIngestionFailure
)


def ingest_workflow_artifact(
    artifact_id: str,
    artifact_kind: str,
    payload: bytes,
    *,
    artifact_store: LocalWorkflowArtifactStore,
) -> WorkflowArtifactIngestionResult:
    """Hash exact caller bytes and save them once through the T009 store.

    The caller supplies only the artifact's identity, its declared kind, and
    its exact opaque bytes; this operation derives the immutable digest and
    the accepted producer version rather than trusting caller-computed values,
    then persists the result through the unchanged T009 store.
    """

    if type(artifact_id) is not str:
        raise TypeError("artifact_id must be exactly str")
    if type(artifact_kind) is not str:
        raise TypeError("artifact_kind must be exactly str")
    if type(payload) is not bytes:
        raise TypeError("payload must be exactly bytes")
    if type(artifact_store) is not LocalWorkflowArtifactStore:
        raise TypeError("artifact_store must be exactly LocalWorkflowArtifactStore")

    try:
        try:
            producer_version = ARTIFACT_PRODUCERS[artifact_kind]
        except (KeyError, TypeError):
            return _failure("invalid_workflow_artifact_ingestion_input")
        try:
            reference = WorkflowArtifactReference(
                WORKFLOW_ARTIFACT_REFERENCE_VERSION,
                artifact_id,
                artifact_kind,  # type: ignore[arg-type]
                hashlib.sha256(payload).hexdigest(),
                producer_version,  # type: ignore[arg-type]
            )
        except (TypeError, ValueError):
            return _failure("invalid_workflow_artifact_ingestion_input")

        result = artifact_store.save(reference, payload)
        if type(result) is WorkflowArtifactPayload:
            return result
        if type(result) is WorkflowArtifactPersistenceFailure:
            if _failure_code(result) == "immutable_identity_conflict":
                return _failure("workflow_artifact_identity_conflict")
            return _failure("workflow_artifact_store_failed")
        return _failure("workflow_artifact_ingestion_exception")
    except Exception:
        return _failure("workflow_artifact_ingestion_exception")


def _failure_code(value: WorkflowArtifactPersistenceFailure) -> str | None:
    try:
        diagnostic = value.diagnostics[0]
        return diagnostic.code if type(diagnostic.code) is str else None
    except (AttributeError, IndexError, KeyError, RecursionError, TypeError, ValueError):
        return None


def _is_valid_diagnostic(value: object) -> bool:
    if type(value) is not WorkflowArtifactIngestionDiagnostic:
        return False
    try:
        WorkflowArtifactIngestionDiagnostic(
            value.code,
            value.classification,
            value.message,
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        return False
    return True


def _failure(code: str) -> WorkflowArtifactIngestionFailure:
    classification, message = _DIAGNOSTICS[code]
    return WorkflowArtifactIngestionFailure(
        status="workflow_artifact_ingestion_failed",
        diagnostics=(WorkflowArtifactIngestionDiagnostic(code, classification, message),),
    )
