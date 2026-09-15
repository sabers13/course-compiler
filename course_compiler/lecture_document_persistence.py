"""Local SQLite persistence for exact immutable T002 lecture documents."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Literal, TypeAlias

from .contracts import (
    LectureDocument,
    SourceProvenance,
    has_validation_errors,
    validate_document,
)
from .rendering import DocumentReference, document_reference


LECTURE_DOCUMENT_PERSISTENCE_SCHEMA_VERSION = "local-lecture-document-sqlite/v1"

__all__ = [
    "LECTURE_DOCUMENT_PERSISTENCE_SCHEMA_VERSION",
    "LectureDocumentLoadResult",
    "LectureDocumentPersistenceDiagnostic",
    "LectureDocumentPersistenceFailure",
    "LectureDocumentPersistenceResult",
    "LectureDocumentSaveResult",
    "LocalLectureDocumentStore",
    "open_lecture_document_store",
]


_DIAGNOSTICS = {
    "invalid_persistence_input": (
        "input",
        "The lecture-document persistence input is invalid.",
    ),
    "persistence_unavailable": (
        "storage",
        "The local lecture-document store is unavailable.",
    ),
    "document_not_found": ("storage", "No stored lecture document was found."),
    "document_reference_mismatch": (
        "storage",
        "The stored lecture document does not match its reference.",
    ),
    "immutable_identity_conflict": (
        "identity",
        "The document reference is already bound to different immutable content.",
    ),
    "stored_document_invalid": ("decode", "The stored lecture document is invalid."),
    "unsupported_storage_schema": (
        "decode",
        "The stored lecture-document schema is unsupported.",
    ),
    "persistence_exception": ("adapter", "The local lecture-document adapter failed."),
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS lecture_documents (
    contract_version TEXT NOT NULL,
    document_id TEXT NOT NULL,
    document_order INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    storage_schema_version TEXT NOT NULL,
    source_text BLOB NOT NULL,
    PRIMARY KEY (contract_version, document_id, document_order, content_sha256)
)
"""
_STORE_CONSTRUCTION_TOKEN = object()


@dataclass(frozen=True, slots=True)
class LectureDocumentPersistenceDiagnostic:
    """One fixed, content-safe lecture-document persistence diagnostic."""

    code: str
    classification: Literal["input", "storage", "identity", "decode", "adapter"]
    message: str

    def __post_init__(self) -> None:
        if self.code not in _DIAGNOSTICS:
            raise ValueError("lecture document persistence diagnostic code is not registered")
        classification, message = _DIAGNOSTICS[self.code]
        if self.classification != classification or self.message != message:
            raise ValueError("lecture document persistence diagnostic fields are not registered")


@dataclass(frozen=True, slots=True)
class LectureDocumentPersistenceFailure:
    """A content-safe lecture-document persistence failure."""

    status: Literal["persistence_failed"]
    diagnostics: tuple[LectureDocumentPersistenceDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "persistence_failed":
            raise ValueError("lecture document persistence failure status is invalid")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) != 1
            or not _is_valid_diagnostic(self.diagnostics[0])
        ):
            raise ValueError("lecture document persistence failure diagnostics are invalid")


LectureDocumentSaveResult: TypeAlias = DocumentReference | LectureDocumentPersistenceFailure
LectureDocumentLoadResult: TypeAlias = LectureDocument | LectureDocumentPersistenceFailure
LectureDocumentPersistenceResult: TypeAlias = (
    DocumentReference | LectureDocument | LectureDocumentPersistenceFailure
)


class LocalLectureDocumentStore:
    """A trusted-local immutable lecture-document store with no lifecycle behavior."""

    __slots__ = ("_connection",)

    def __init__(self, connection: sqlite3.Connection, token: object) -> None:
        if token is not _STORE_CONSTRUCTION_TOKEN:
            raise TypeError("lecture document stores must be opened by the adapter")
        self._connection = connection

    def save(self, document: LectureDocument) -> LectureDocumentSaveResult:
        """Persist one exact document, or confirm its immutable stored value."""

        if type(document) is not LectureDocument:
            raise TypeError("document must be exactly LectureDocument")
        try:
            reference, source_bytes = _validated_document(document)
        except Exception:
            return _failure("invalid_persistence_input")

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            if not _schema_is_recognized(self._connection):
                self._connection.rollback()
                return _failure("unsupported_storage_schema")
            row = self._connection.execute(
                "SELECT contract_version, document_id, document_order, content_sha256, "
                "storage_schema_version, source_text FROM lecture_documents "
                "WHERE contract_version = ? AND document_id = ? AND document_order = ? "
                "AND content_sha256 = ?",
                _reference_key(reference),
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO lecture_documents "
                    "(contract_version, document_id, document_order, content_sha256, "
                    "storage_schema_version, source_text) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        *_reference_key(reference),
                        LECTURE_DOCUMENT_PERSISTENCE_SCHEMA_VERSION,
                        source_bytes,
                    ),
                )
                self._connection.commit()
                return reference
            stored = _decode_row(row, expected_reference=reference)
            if type(stored) is LectureDocumentPersistenceFailure:
                self._connection.rollback()
                return stored
            self._connection.rollback()
            if stored == document:
                return reference
            return _failure("immutable_identity_conflict")
        except sqlite3.Error:
            _rollback_quietly(self._connection)
            return _failure("persistence_unavailable")
        except Exception:
            _rollback_quietly(self._connection)
            return _failure("persistence_exception")

    def load(self, reference: DocumentReference) -> LectureDocumentLoadResult:
        """Reopen one exact document only through its complete reference."""

        if type(reference) is not DocumentReference:
            raise TypeError("reference must be exactly DocumentReference")
        try:
            _validated_reference(reference)
        except Exception:
            return _failure("invalid_persistence_input")
        try:
            self._connection.execute("BEGIN")
            if not _schema_is_recognized(self._connection):
                self._connection.rollback()
                return _failure("unsupported_storage_schema")
            row = self._connection.execute(
                "SELECT contract_version, document_id, document_order, content_sha256, "
                "storage_schema_version, source_text FROM lecture_documents "
                "WHERE contract_version = ? AND document_id = ? AND document_order = ? "
                "AND content_sha256 = ?",
                _reference_key(reference),
            ).fetchone()
        except sqlite3.Error:
            _rollback_quietly(self._connection)
            return _failure("persistence_unavailable")
        except Exception:
            _rollback_quietly(self._connection)
            return _failure("persistence_exception")
        if row is None:
            self._connection.rollback()
            return _failure("document_not_found")
        try:
            result = _decode_row(row, expected_reference=reference)
            self._connection.rollback()
            return result
        except Exception:
            _rollback_quietly(self._connection)
            return _failure("persistence_exception")

    def close(self) -> None:
        """Close this adapter's connection; it has no persisted close state."""

        self._connection.close()


def open_lecture_document_store(
    database_path: str | Path,
) -> LocalLectureDocumentStore | LectureDocumentPersistenceFailure:
    """Open or deterministically initialize one trusted local document store."""

    if type(database_path) is not str and not isinstance(database_path, Path):
        raise TypeError("database_path must be exactly str or Path")
    try:
        connection = sqlite3.connect(database_path)
        connection.execute(_SCHEMA_SQL)
        if not _schema_is_recognized(connection):
            connection.close()
            return _failure("unsupported_storage_schema")
        return LocalLectureDocumentStore(connection, _STORE_CONSTRUCTION_TOKEN)
    except sqlite3.Error:
        return _failure("persistence_unavailable")
    except Exception:
        return _failure("persistence_exception")


def _validated_document(document: LectureDocument) -> tuple[DocumentReference, bytes]:
    if type(document) is not LectureDocument:
        raise ValueError("lecture document is invalid")
    try:
        if tuple(item.name for item in fields(LectureDocument)) != (
            "contract_version",
            "document_id",
            "order",
            "source_text",
            "provenance",
        ):
            raise ValueError("lecture document is invalid")
        if (
            type(document.contract_version) is not str
            or type(document.document_id) is not str
            or type(document.order) is not int
            or type(document.source_text) is not str
            or type(document.provenance) is not SourceProvenance
            or type(document.provenance.content_sha256) is not str
        ):
            raise ValueError("lecture document is invalid")
    except (AttributeError, TypeError):
        raise ValueError("lecture document is invalid") from None
    diagnostics = validate_document(document)
    if has_validation_errors(diagnostics):
        raise ValueError("lecture document is invalid")
    reference = document_reference(document)
    if type(reference) is not DocumentReference:
        raise ValueError("lecture document is invalid")
    _validated_reference(reference)
    source_bytes = document.source_text.encode("utf-8", "strict")
    if _sha256(source_bytes) != reference.content_sha256:
        raise ValueError("lecture document is invalid")
    return reference, source_bytes


def _validated_reference(reference: DocumentReference) -> None:
    if type(reference) is not DocumentReference:
        raise ValueError("document reference is invalid")
    try:
        if (
            type(reference.contract_version) is not str
            or type(reference.document_id) is not str
            or type(reference.order) is not int
            or type(reference.content_sha256) is not str
        ):
            raise ValueError("document reference is invalid")
        DocumentReference(
            reference.contract_version,
            reference.document_id,
            reference.order,
            reference.content_sha256,
        )
    except (AttributeError, TypeError, ValueError):
        raise ValueError("document reference is invalid") from None


def _decode_row(
    row: tuple[object, ...], *, expected_reference: DocumentReference
) -> LectureDocumentLoadResult:
    if type(row) is not tuple or len(row) != 6:
        return _failure("stored_document_invalid")
    (
        contract_version,
        document_id,
        order,
        content_sha256,
        schema_version,
        source_bytes,
    ) = row
    if (
        type(schema_version) is not str
        or schema_version != LECTURE_DOCUMENT_PERSISTENCE_SCHEMA_VERSION
    ):
        return _failure("unsupported_storage_schema")
    if (
        type(contract_version) is not str
        or type(document_id) is not str
        or type(order) is not int
        or type(content_sha256) is not str
        or type(source_bytes) is not bytes
    ):
        return _failure("stored_document_invalid")
    try:
        reference = DocumentReference(contract_version, document_id, order, content_sha256)
        _validated_reference(reference)
        if reference != expected_reference:
            return _failure("document_reference_mismatch")
        if _sha256(source_bytes) != reference.content_sha256:
            return _failure("stored_document_invalid")
        source_text = source_bytes.decode("utf-8", "strict")
        document = LectureDocument(
            reference.contract_version,
            reference.document_id,
            reference.order,
            source_text,
            SourceProvenance(reference.content_sha256),
        )
        derived_reference, derived_bytes = _validated_document(document)
        if derived_reference != expected_reference or derived_bytes != source_bytes:
            return _failure("stored_document_invalid")
        return document
    except (AttributeError, TypeError, UnicodeError, ValueError):
        return _failure("stored_document_invalid")
    except Exception:
        return _failure("persistence_exception")


def _reference_key(reference: DocumentReference) -> tuple[str, str, int, str]:
    return (
        reference.contract_version,
        reference.document_id,
        reference.order,
        reference.content_sha256,
    )


def _schema_is_recognized(connection: sqlite3.Connection) -> bool:
    try:
        rows = connection.execute("PRAGMA table_info(lecture_documents)").fetchall()
    except sqlite3.Error:
        return False
    expected = (
        ("contract_version", "TEXT", 1, 1),
        ("document_id", "TEXT", 1, 2),
        ("document_order", "INTEGER", 1, 3),
        ("content_sha256", "TEXT", 1, 4),
        ("storage_schema_version", "TEXT", 1, 0),
        ("source_text", "BLOB", 1, 0),
    )
    return (
        type(rows) is list
        and len(rows) == len(expected)
        and all(
            type(row) is tuple
            and len(row) == 6
            and row[1] == name
            and row[2] == column_type
            and row[3] == not_null
            and row[4] is None
            and row[5] == primary_key
            for row, (name, column_type, not_null, primary_key) in zip(rows, expected)
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
    if type(value) is not LectureDocumentPersistenceDiagnostic:
        return False
    try:
        LectureDocumentPersistenceDiagnostic(
            value.code, value.classification, value.message
        )
    except (AttributeError, TypeError, ValueError):
        return False
    return True


def _failure(code: str) -> LectureDocumentPersistenceFailure:
    classification, message = _DIAGNOSTICS[code]
    return LectureDocumentPersistenceFailure(
        status="persistence_failed",
        diagnostics=(LectureDocumentPersistenceDiagnostic(code, classification, message),),
    )
