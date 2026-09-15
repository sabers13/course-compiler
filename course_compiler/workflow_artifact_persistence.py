"""Local SQLite persistence for immutable opaque T003 workflow-artifact bytes."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeAlias

from .workflow import WorkflowArtifactReference


WORKFLOW_ARTIFACT_PERSISTENCE_SCHEMA_VERSION = "local-workflow-artifact-sqlite/v1"

__all__ = [
    "WORKFLOW_ARTIFACT_PERSISTENCE_SCHEMA_VERSION",
    "LocalWorkflowArtifactStore",
    "WorkflowArtifactPayload",
    "WorkflowArtifactPersistenceDiagnostic",
    "WorkflowArtifactPersistenceFailure",
    "WorkflowArtifactPersistenceResult",
    "open_workflow_artifact_store",
]


_DIAGNOSTICS = {
    "invalid_persistence_input": ("input", "The workflow-artifact input is invalid."),
    "persistence_unavailable": ("storage", "The local workflow-artifact store is unavailable."),
    "artifact_not_found": ("storage", "No stored workflow artifact was found."),
    "artifact_reference_mismatch": ("storage", "The stored workflow artifact does not match its reference."),
    "immutable_identity_conflict": ("identity", "The artifact ID is already bound to different immutable content."),
    "stored_artifact_invalid": ("decode", "The stored workflow artifact is invalid."),
    "unsupported_storage_schema": ("decode", "The stored workflow-artifact schema is unsupported."),
    "persistence_exception": ("adapter", "The local workflow-artifact adapter failed."),
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workflow_artifact_blobs (
    artifact_id TEXT PRIMARY KEY NOT NULL,
    reference_version TEXT NOT NULL,
    artifact_kind TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    producer_version TEXT NOT NULL,
    storage_schema_version TEXT NOT NULL,
    payload BLOB NOT NULL
)
"""
_STORE_CONSTRUCTION_TOKEN = object()


@dataclass(frozen=True, slots=True)
class WorkflowArtifactPersistenceDiagnostic:
    """One fixed, content-safe workflow-artifact persistence diagnostic."""

    code: str
    classification: Literal["input", "storage", "identity", "decode", "adapter"]
    message: str

    def __post_init__(self) -> None:
        if self.code not in _DIAGNOSTICS:
            raise ValueError("workflow artifact persistence diagnostic code is not registered")
        classification, message = _DIAGNOSTICS[self.code]
        if self.classification != classification or self.message != message:
            raise ValueError("workflow artifact persistence diagnostic fields are not registered")


@dataclass(frozen=True, slots=True)
class WorkflowArtifactPersistenceFailure:
    """A content-safe workflow-artifact persistence failure with one fixed diagnostic."""

    status: Literal["persistence_failed"]
    diagnostics: tuple[WorkflowArtifactPersistenceDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "persistence_failed":
            raise ValueError("workflow artifact persistence failure status is invalid")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) != 1
            or not _is_valid_diagnostic(self.diagnostics[0])
        ):
            raise ValueError("workflow artifact persistence failure diagnostics are invalid")


@dataclass(frozen=True, slots=True)
class WorkflowArtifactPayload:
    """One validated artifact reference and its exact, representation-redacted bytes."""

    reference: WorkflowArtifactReference
    payload: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _validated_reference(self.reference)
        if type(self.payload) is not bytes:
            raise ValueError("workflow artifact payload is invalid")
        if _sha256(self.payload) != self.reference.content_sha256:
            raise ValueError("workflow artifact payload digest is invalid")


WorkflowArtifactPersistenceResult: TypeAlias = (
    WorkflowArtifactPayload | WorkflowArtifactPersistenceFailure
)


class LocalWorkflowArtifactStore:
    """A trusted-local immutable opaque-artifact store with no workflow behavior."""

    __slots__ = ("_connection",)

    def __init__(self, connection: sqlite3.Connection, token: object) -> None:
        if token is not _STORE_CONSTRUCTION_TOKEN:
            raise TypeError("workflow artifact stores must be opened by the adapter")
        self._connection = connection

    def save(
        self, reference: WorkflowArtifactReference, payload: bytes
    ) -> WorkflowArtifactPersistenceResult:
        """Persist exact opaque bytes once, or confirm the same immutable binding."""

        if type(reference) is not WorkflowArtifactReference:
            raise TypeError("reference must be exactly WorkflowArtifactReference")
        if type(payload) is not bytes:
            raise TypeError("payload must be exactly bytes")
        try:
            _validated_reference(reference)
            if _sha256(payload) != reference.content_sha256:
                return _failure("invalid_persistence_input")
            candidate = WorkflowArtifactPayload(reference, payload)
        except Exception:
            return _failure("invalid_persistence_input")

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT artifact_id, reference_version, artifact_kind, content_sha256, "
                "producer_version, storage_schema_version, payload "
                "FROM workflow_artifact_blobs WHERE artifact_id = ?",
                (reference.artifact_id,),
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO workflow_artifact_blobs "
                    "(artifact_id, reference_version, artifact_kind, content_sha256, "
                    "producer_version, storage_schema_version, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        reference.artifact_id,
                        reference.reference_version,
                        reference.artifact_kind,
                        reference.content_sha256,
                        reference.producer_version,
                        WORKFLOW_ARTIFACT_PERSISTENCE_SCHEMA_VERSION,
                        payload,
                    ),
                )
                self._connection.commit()
                return candidate
            stored = _decode_row(row, expected_artifact_id=reference.artifact_id)
            if type(stored) is WorkflowArtifactPersistenceFailure:
                self._connection.rollback()
                return stored
            self._connection.rollback()
            if stored.reference == reference and stored.payload == payload:
                return candidate
            return _failure("immutable_identity_conflict")
        except sqlite3.Error:
            _rollback_quietly(self._connection)
            return _failure("persistence_unavailable")
        except Exception:
            _rollback_quietly(self._connection)
            return _failure("persistence_exception")

    def load(self, reference: WorkflowArtifactReference) -> WorkflowArtifactPersistenceResult:
        """Reopen exact immutable opaque bytes only through their complete reference."""

        if type(reference) is not WorkflowArtifactReference:
            raise TypeError("reference must be exactly WorkflowArtifactReference")
        try:
            _validated_reference(reference)
        except Exception:
            return _failure("invalid_persistence_input")
        try:
            row = self._connection.execute(
                "SELECT artifact_id, reference_version, artifact_kind, content_sha256, "
                "producer_version, storage_schema_version, payload "
                "FROM workflow_artifact_blobs WHERE artifact_id = ?",
                (reference.artifact_id,),
            ).fetchone()
        except sqlite3.Error:
            return _failure("persistence_unavailable")
        except Exception:
            return _failure("persistence_exception")
        if row is None:
            return _failure("artifact_not_found")
        stored = _decode_row(row, expected_artifact_id=reference.artifact_id)
        if type(stored) is WorkflowArtifactPersistenceFailure:
            return stored
        if stored.reference != reference:
            return _failure("artifact_reference_mismatch")
        return stored

    def close(self) -> None:
        """Close this adapter's connection; it has no persisted close state."""

        self._connection.close()


def open_workflow_artifact_store(
    database_path: str | Path,
) -> LocalWorkflowArtifactStore | WorkflowArtifactPersistenceFailure:
    """Open or deterministically initialize one trusted local workflow-artifact store."""

    if type(database_path) is not str and not isinstance(database_path, Path):
        raise TypeError("database_path must be exactly str or Path")
    try:
        connection = sqlite3.connect(database_path)
        connection.execute(_SCHEMA_SQL)
        if not _schema_is_recognized(connection):
            connection.close()
            return _failure("unsupported_storage_schema")
        return LocalWorkflowArtifactStore(connection, _STORE_CONSTRUCTION_TOKEN)
    except sqlite3.Error:
        return _failure("persistence_unavailable")
    except Exception:
        return _failure("persistence_exception")


def _validated_reference(reference: WorkflowArtifactReference) -> None:
    if type(reference) is not WorkflowArtifactReference:
        raise ValueError("workflow artifact reference is invalid")
    WorkflowArtifactReference(
        reference.reference_version,
        reference.artifact_id,
        reference.artifact_kind,
        reference.content_sha256,
        reference.producer_version,
    )


def _decode_row(
    row: tuple[object, ...], *, expected_artifact_id: str
) -> WorkflowArtifactPersistenceResult:
    if type(row) is not tuple or len(row) != 7:
        return _failure("stored_artifact_invalid")
    (
        artifact_id,
        reference_version,
        artifact_kind,
        content_sha256,
        producer_version,
        schema_version,
        payload,
    ) = row
    if type(schema_version) is not str or schema_version != WORKFLOW_ARTIFACT_PERSISTENCE_SCHEMA_VERSION:
        return _failure("unsupported_storage_schema")
    if (
        type(artifact_id) is not str
        or artifact_id != expected_artifact_id
        or type(reference_version) is not str
        or type(artifact_kind) is not str
        or type(content_sha256) is not str
        or type(producer_version) is not str
        or type(payload) is not bytes
    ):
        return _failure("stored_artifact_invalid")
    try:
        reference = WorkflowArtifactReference(
            reference_version,
            artifact_id,
            artifact_kind,
            content_sha256,
            producer_version,
        )
        if _sha256(payload) != reference.content_sha256:
            return _failure("stored_artifact_invalid")
        return WorkflowArtifactPayload(reference, payload)
    except (TypeError, ValueError):
        return _failure("stored_artifact_invalid")
    except Exception:
        return _failure("persistence_exception")


def _schema_is_recognized(connection: sqlite3.Connection) -> bool:
    try:
        rows = connection.execute("PRAGMA table_info(workflow_artifact_blobs)").fetchall()
    except sqlite3.Error:
        return False
    expected = (
        ("artifact_id", "TEXT", 1),
        ("reference_version", "TEXT", 0),
        ("artifact_kind", "TEXT", 0),
        ("content_sha256", "TEXT", 0),
        ("producer_version", "TEXT", 0),
        ("storage_schema_version", "TEXT", 0),
        ("payload", "BLOB", 0),
    )
    return (
        type(rows) is list
        and len(rows) == len(expected)
        and all(
            type(row) is tuple
            and len(row) == 6
            and row[1] == name
            and row[2] == column_type
            and row[5] == primary_key
            for row, (name, column_type, primary_key) in zip(rows, expected)
        )
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _rollback_quietly(connection: sqlite3.Connection) -> None:
    try:
        connection.rollback()
    except Exception:
        pass


def _is_valid_diagnostic(value: object) -> bool:
    if type(value) is not WorkflowArtifactPersistenceDiagnostic:
        return False
    try:
        WorkflowArtifactPersistenceDiagnostic(value.code, value.classification, value.message)
    except (AttributeError, TypeError, ValueError):
        return False
    return True


def _failure(code: str) -> WorkflowArtifactPersistenceFailure:
    classification, message = _DIAGNOSTICS[code]
    return WorkflowArtifactPersistenceFailure(
        status="persistence_failed",
        diagnostics=(WorkflowArtifactPersistenceDiagnostic(code, classification, message),),
    )
