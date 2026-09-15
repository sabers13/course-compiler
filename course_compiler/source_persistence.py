"""Local SQLite persistence for immutable T003 source-evidence bytes."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeAlias

from .workflow import SourceEvidenceReference


SOURCE_PERSISTENCE_SCHEMA_VERSION = "local-source-evidence-sqlite/v1"

__all__ = [
    "SOURCE_PERSISTENCE_SCHEMA_VERSION",
    "LocalSourceEvidenceStore",
    "SourceEvidencePayload",
    "SourcePersistenceDiagnostic",
    "SourcePersistenceFailure",
    "SourcePersistenceResult",
    "open_source_evidence_store",
]


_DIAGNOSTICS = {
    "invalid_persistence_input": ("input", "The source-evidence input is invalid."),
    "persistence_unavailable": ("storage", "The local source-evidence store is unavailable."),
    "source_not_found": ("storage", "No stored source evidence was found."),
    "source_reference_mismatch": ("storage", "The stored source evidence does not match its reference."),
    "immutable_identity_conflict": ("identity", "The source ID is already bound to different immutable content."),
    "stored_source_invalid": ("decode", "The stored source evidence is invalid."),
    "unsupported_storage_schema": ("decode", "The stored source-evidence schema is unsupported."),
    "persistence_exception": ("adapter", "The local source-evidence adapter failed."),
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS source_evidence_blobs (
    source_id TEXT PRIMARY KEY NOT NULL,
    reference_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    storage_schema_version TEXT NOT NULL,
    payload BLOB NOT NULL
)
"""
_STORE_CONSTRUCTION_TOKEN = object()


@dataclass(frozen=True, slots=True)
class SourcePersistenceDiagnostic:
    """One fixed, content-safe local-source-persistence diagnostic."""

    code: str
    classification: Literal["input", "storage", "identity", "decode", "adapter"]
    message: str

    def __post_init__(self) -> None:
        if self.code not in _DIAGNOSTICS:
            raise ValueError("source persistence diagnostic code is not registered")
        classification, message = _DIAGNOSTICS[self.code]
        if self.classification != classification or self.message != message:
            raise ValueError("source persistence diagnostic fields are not registered")


@dataclass(frozen=True, slots=True)
class SourcePersistenceFailure:
    """A content-safe source-persistence failure with one fixed diagnostic."""

    status: Literal["persistence_failed"]
    diagnostics: tuple[SourcePersistenceDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "persistence_failed":
            raise ValueError("source persistence failure status is invalid")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) != 1
            or not _is_valid_diagnostic(self.diagnostics[0])
        ):
            raise ValueError("source persistence failure diagnostics are invalid")


@dataclass(frozen=True, slots=True)
class SourceEvidencePayload:
    """One validated source reference and its exact, representation-redacted bytes."""

    reference: SourceEvidenceReference
    payload: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _validated_reference(self.reference)
        if type(self.payload) is not bytes:
            raise ValueError("source evidence payload is invalid")
        if _sha256(self.payload) != self.reference.content_sha256:
            raise ValueError("source evidence payload digest is invalid")


SourcePersistenceResult: TypeAlias = SourceEvidencePayload | SourcePersistenceFailure


class LocalSourceEvidenceStore:
    """A trusted-local immutable source-content store with no workflow behavior."""

    __slots__ = ("_connection",)

    def __init__(self, connection: sqlite3.Connection, token: object) -> None:
        if token is not _STORE_CONSTRUCTION_TOKEN:
            raise TypeError("source evidence stores must be opened by the adapter")
        self._connection = connection

    def save(
        self, reference: SourceEvidenceReference, payload: bytes
    ) -> SourcePersistenceResult:
        """Persist exact bytes once, or confirm the same immutable identity."""

        if type(reference) is not SourceEvidenceReference:
            raise TypeError("reference must be exactly SourceEvidenceReference")
        if type(payload) is not bytes:
            raise TypeError("payload must be exactly bytes")
        try:
            _validated_reference(reference)
            if _sha256(payload) != reference.content_sha256:
                return _failure("invalid_persistence_input")
            candidate = SourceEvidencePayload(reference, payload)
        except Exception:
            return _failure("invalid_persistence_input")

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT source_id, reference_version, content_sha256, "
                "storage_schema_version, payload FROM source_evidence_blobs WHERE source_id = ?",
                (reference.source_id,),
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO source_evidence_blobs "
                    "(source_id, reference_version, content_sha256, storage_schema_version, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        reference.source_id,
                        reference.reference_version,
                        reference.content_sha256,
                        SOURCE_PERSISTENCE_SCHEMA_VERSION,
                        payload,
                    ),
                )
                self._connection.commit()
                return candidate
            stored = _decode_row(row, expected_source_id=reference.source_id)
            if type(stored) is SourcePersistenceFailure:
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

    def load(self, reference: SourceEvidenceReference) -> SourcePersistenceResult:
        """Reopen exact immutable bytes only through their complete reference."""

        if type(reference) is not SourceEvidenceReference:
            raise TypeError("reference must be exactly SourceEvidenceReference")
        try:
            _validated_reference(reference)
        except Exception:
            return _failure("invalid_persistence_input")
        try:
            row = self._connection.execute(
                "SELECT source_id, reference_version, content_sha256, "
                "storage_schema_version, payload FROM source_evidence_blobs WHERE source_id = ?",
                (reference.source_id,),
            ).fetchone()
        except sqlite3.Error:
            return _failure("persistence_unavailable")
        except Exception:
            return _failure("persistence_exception")
        if row is None:
            return _failure("source_not_found")
        stored = _decode_row(row, expected_source_id=reference.source_id)
        if type(stored) is SourcePersistenceFailure:
            return stored
        if stored.reference != reference:
            return _failure("source_reference_mismatch")
        return stored

    def close(self) -> None:
        """Close this adapter's connection; it has no persisted close state."""

        self._connection.close()


def open_source_evidence_store(
    database_path: str | Path,
) -> LocalSourceEvidenceStore | SourcePersistenceFailure:
    """Open or deterministically initialize one trusted local source store."""

    if type(database_path) is not str and not isinstance(database_path, Path):
        raise TypeError("database_path must be exactly str or Path")
    try:
        connection = sqlite3.connect(database_path)
        connection.execute(_SCHEMA_SQL)
        if not _schema_is_recognized(connection):
            connection.close()
            return _failure("unsupported_storage_schema")
        return LocalSourceEvidenceStore(connection, _STORE_CONSTRUCTION_TOKEN)
    except sqlite3.Error:
        return _failure("persistence_unavailable")
    except Exception:
        return _failure("persistence_exception")


def _validated_reference(reference: SourceEvidenceReference) -> None:
    if type(reference) is not SourceEvidenceReference:
        raise ValueError("source evidence reference is invalid")
    SourceEvidenceReference(
        reference.reference_version,
        reference.source_id,
        reference.content_sha256,
    )


def _decode_row(
    row: tuple[object, ...], *, expected_source_id: str
) -> SourcePersistenceResult:
    if type(row) is not tuple or len(row) != 5:
        return _failure("stored_source_invalid")
    source_id, reference_version, content_sha256, schema_version, payload = row
    if type(schema_version) is not str or schema_version != SOURCE_PERSISTENCE_SCHEMA_VERSION:
        return _failure("unsupported_storage_schema")
    if (
        type(source_id) is not str
        or source_id != expected_source_id
        or type(reference_version) is not str
        or type(content_sha256) is not str
        or type(payload) is not bytes
    ):
        return _failure("stored_source_invalid")
    try:
        reference = SourceEvidenceReference(reference_version, source_id, content_sha256)
        if _sha256(payload) != reference.content_sha256:
            return _failure("stored_source_invalid")
        return SourceEvidencePayload(reference, payload)
    except (TypeError, ValueError):
        return _failure("stored_source_invalid")
    except Exception:
        return _failure("persistence_exception")


def _schema_is_recognized(connection: sqlite3.Connection) -> bool:
    try:
        rows = connection.execute("PRAGMA table_info(source_evidence_blobs)").fetchall()
    except sqlite3.Error:
        return False
    expected = (
        ("source_id", "TEXT", 1),
        ("reference_version", "TEXT", 0),
        ("content_sha256", "TEXT", 0),
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
    if type(value) is not SourcePersistenceDiagnostic:
        return False
    try:
        SourcePersistenceDiagnostic(value.code, value.classification, value.message)
    except (AttributeError, TypeError, ValueError):
        return False
    return True


def _failure(code: str) -> SourcePersistenceFailure:
    classification, message = _DIAGNOSTICS[code]
    return SourcePersistenceFailure(
        status="persistence_failed",
        diagnostics=(SourcePersistenceDiagnostic(code, classification, message),),
    )
