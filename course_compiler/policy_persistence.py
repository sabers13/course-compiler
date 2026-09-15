"""Local SQLite persistence for exact immutable T003 policy-content bytes."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeAlias

from .workflow import PolicyReference


POLICY_CONTENT_PERSISTENCE_SCHEMA_VERSION = "local-policy-content-sqlite/v1"

__all__ = [
    "POLICY_CONTENT_PERSISTENCE_SCHEMA_VERSION",
    "LocalPolicyContentStore",
    "PolicyContentPayload",
    "PolicyPersistenceDiagnostic",
    "PolicyPersistenceFailure",
    "PolicyPersistenceResult",
    "open_policy_content_store",
]


_DIAGNOSTICS = {
    "invalid_persistence_input": ("input", "The policy-content input is invalid."),
    "persistence_unavailable": (
        "storage",
        "The local policy-content store is unavailable.",
    ),
    "policy_not_found": ("storage", "No stored policy content was found."),
    "policy_reference_mismatch": (
        "storage",
        "The stored policy content does not match its reference.",
    ),
    "immutable_identity_conflict": (
        "identity",
        "The policy reference is already bound to different immutable content.",
    ),
    "stored_policy_invalid": ("decode", "The stored policy content is invalid."),
    "unsupported_storage_schema": (
        "decode",
        "The stored policy-content schema is unsupported.",
    ),
    "persistence_exception": ("adapter", "The local policy-content adapter failed."),
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS policy_content_blobs (
    policy_kind TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    storage_schema_version TEXT NOT NULL,
    payload BLOB NOT NULL,
    PRIMARY KEY (policy_kind, policy_version, content_sha256)
)
"""
_STORE_CONSTRUCTION_TOKEN = object()


@dataclass(frozen=True, slots=True)
class PolicyPersistenceDiagnostic:
    """One fixed, content-safe policy-content persistence diagnostic."""

    code: str
    classification: Literal["input", "storage", "identity", "decode", "adapter"]
    message: str

    def __post_init__(self) -> None:
        if self.code not in _DIAGNOSTICS:
            raise ValueError("policy persistence diagnostic code is not registered")
        classification, message = _DIAGNOSTICS[self.code]
        if self.classification != classification or self.message != message:
            raise ValueError("policy persistence diagnostic fields are not registered")


@dataclass(frozen=True, slots=True)
class PolicyPersistenceFailure:
    """A content-safe policy-content failure with one fixed diagnostic."""

    status: Literal["persistence_failed"]
    diagnostics: tuple[PolicyPersistenceDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "persistence_failed":
            raise ValueError("policy persistence failure status is invalid")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) != 1
            or not _is_valid_diagnostic(self.diagnostics[0])
        ):
            raise ValueError("policy persistence failure diagnostics are invalid")


@dataclass(frozen=True, slots=True)
class PolicyContentPayload:
    """One valid policy reference and its exact, representation-redacted bytes."""

    reference: PolicyReference
    payload: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _validated_reference(self.reference)
        if type(self.payload) is not bytes:
            raise ValueError("policy content payload is invalid")
        if _sha256(self.payload) != self.reference.content_sha256:
            raise ValueError("policy content payload digest is invalid")


PolicyPersistenceResult: TypeAlias = PolicyContentPayload | PolicyPersistenceFailure


class LocalPolicyContentStore:
    """A trusted-local immutable policy-content store with no policy lifecycle."""

    __slots__ = ("_connection",)

    def __init__(self, connection: sqlite3.Connection, token: object) -> None:
        if token is not _STORE_CONSTRUCTION_TOKEN:
            raise TypeError("policy content stores must be opened by the adapter")
        self._connection = connection

    def save(
        self, reference: PolicyReference, payload: bytes
    ) -> PolicyPersistenceResult:
        """Persist exact bytes once, or confirm the same immutable value."""

        if type(reference) is not PolicyReference:
            raise TypeError("reference must be exactly PolicyReference")
        if type(payload) is not bytes:
            raise TypeError("payload must be exactly bytes")
        try:
            _validated_reference(reference)
            if _sha256(payload) != reference.content_sha256:
                return _failure("invalid_persistence_input")
            candidate = PolicyContentPayload(reference, payload)
        except Exception:
            return _failure("invalid_persistence_input")

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            if not _schema_is_recognized(self._connection):
                self._connection.rollback()
                return _failure("unsupported_storage_schema")
            row = self._connection.execute(
                "SELECT policy_kind, policy_version, content_sha256, "
                "storage_schema_version, payload FROM policy_content_blobs "
                "WHERE policy_kind = ? AND policy_version = ? AND content_sha256 = ?",
                _reference_key(reference),
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO policy_content_blobs "
                    "(policy_kind, policy_version, content_sha256, "
                    "storage_schema_version, payload) VALUES (?, ?, ?, ?, ?)",
                    (
                        *_reference_key(reference),
                        POLICY_CONTENT_PERSISTENCE_SCHEMA_VERSION,
                        payload,
                    ),
                )
                self._connection.commit()
                return candidate
            stored = _decode_row(row, expected_reference=reference)
            if type(stored) is PolicyPersistenceFailure:
                self._connection.rollback()
                return stored
            self._connection.rollback()
            if stored == candidate:
                return candidate
            return _failure("immutable_identity_conflict")
        except sqlite3.Error:
            _rollback_quietly(self._connection)
            return _failure("persistence_unavailable")
        except Exception:
            _rollback_quietly(self._connection)
            return _failure("persistence_exception")

    def load(self, reference: PolicyReference) -> PolicyPersistenceResult:
        """Reopen exact bytes only through their complete policy reference."""

        if type(reference) is not PolicyReference:
            raise TypeError("reference must be exactly PolicyReference")
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
                "SELECT policy_kind, policy_version, content_sha256, "
                "storage_schema_version, payload FROM policy_content_blobs "
                "WHERE policy_kind = ? AND policy_version = ? AND content_sha256 = ?",
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
            return _failure("policy_not_found")
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


def open_policy_content_store(
    database_path: str | Path,
) -> LocalPolicyContentStore | PolicyPersistenceFailure:
    """Open or initialize one trusted local policy-content store."""

    if type(database_path) is not str and not isinstance(database_path, Path):
        raise TypeError("database_path must be exactly str or Path")
    try:
        connection = sqlite3.connect(database_path)
        connection.execute(_SCHEMA_SQL)
        if not _schema_is_recognized(connection):
            connection.close()
            return _failure("unsupported_storage_schema")
        return LocalPolicyContentStore(connection, _STORE_CONSTRUCTION_TOKEN)
    except sqlite3.Error:
        return _failure("persistence_unavailable")
    except Exception:
        return _failure("persistence_exception")


def _validated_reference(reference: PolicyReference) -> None:
    if type(reference) is not PolicyReference:
        raise ValueError("policy reference is invalid")
    try:
        if (
            type(reference.policy_kind) is not str
            or type(reference.policy_version) is not str
            or type(reference.content_sha256) is not str
        ):
            raise ValueError("policy reference is invalid")
        PolicyReference(
            reference.policy_kind,
            reference.policy_version,
            reference.content_sha256,
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ValueError("policy reference is invalid") from None


def _decode_row(
    row: tuple[object, ...], *, expected_reference: PolicyReference
) -> PolicyPersistenceResult:
    if type(row) is not tuple or len(row) != 5:
        return _failure("stored_policy_invalid")
    policy_kind, policy_version, content_sha256, schema_version, payload = row
    if (
        type(schema_version) is not str
        or schema_version != POLICY_CONTENT_PERSISTENCE_SCHEMA_VERSION
    ):
        return _failure("unsupported_storage_schema")
    if (
        type(policy_kind) is not str
        or type(policy_version) is not str
        or type(content_sha256) is not str
        or type(payload) is not bytes
    ):
        return _failure("stored_policy_invalid")
    try:
        reference = PolicyReference(policy_kind, policy_version, content_sha256)
        _validated_reference(reference)
        if reference != expected_reference:
            return _failure("policy_reference_mismatch")
        if _sha256(payload) != reference.content_sha256:
            return _failure("stored_policy_invalid")
        return PolicyContentPayload(reference, payload)
    except (AttributeError, KeyError, TypeError, ValueError):
        return _failure("stored_policy_invalid")
    except Exception:
        return _failure("persistence_exception")


def _reference_key(reference: PolicyReference) -> tuple[str, str, str]:
    return (
        reference.policy_kind,
        reference.policy_version,
        reference.content_sha256,
    )


def _schema_is_recognized(connection: sqlite3.Connection) -> bool:
    try:
        rows = connection.execute("PRAGMA table_info(policy_content_blobs)").fetchall()
    except sqlite3.Error:
        return False
    expected = (
        ("policy_kind", "TEXT", 1, 1),
        ("policy_version", "TEXT", 1, 2),
        ("content_sha256", "TEXT", 1, 3),
        ("storage_schema_version", "TEXT", 1, 0),
        ("payload", "BLOB", 1, 0),
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
    if type(value) is not PolicyPersistenceDiagnostic:
        return False
    try:
        PolicyPersistenceDiagnostic(value.code, value.classification, value.message)
    except (AttributeError, TypeError, ValueError):
        return False
    return True


def _failure(code: str) -> PolicyPersistenceFailure:
    classification, message = _DIAGNOSTICS[code]
    return PolicyPersistenceFailure(
        status="persistence_failed",
        diagnostics=(PolicyPersistenceDiagnostic(code, classification, message),),
    )
