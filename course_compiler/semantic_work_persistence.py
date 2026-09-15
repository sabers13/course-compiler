"""Local SQLite persistence for SemanticWorkRequest/Result (T049).

Durable control-plane store for semantic-work coordination.

Privacy: this store persists only the durable semantic control/result
envelope needed for request/result identity, operation identity, expected
revision, kind, lease state, authoritative immutable output
references/hashes, diagnostics, and idempotency/recovery. Produced
artifact bytes and lecture source text are NEVER stored here; byte/content
authority remains with ``LocalWorkflowArtifactStore`` (artifact bytes),
``LocalLectureDocumentStore`` (lecture source text), and
``LocalSourceEvidenceStore`` (source bytes). The accepted-result row keeps
an envelope of content-addressed references (ids + sha256) only.

Lease exclusivity: one active executor owns a lease at a time via an opaque
caller-provided ``lease_holder`` token (SafeId grammar). A second holder
cannot acquire the same active lease; the same holder may renew/retry; after
expiry the next holder obtains the SAME logical request (same request_id,
operation_id, expected_revision, kind, input identity).

Exact schema validation fail-closed, no implicit migrations.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypeAlias

from .semantic_work import (
    SEMANTIC_WORK_REQUEST_VERSION,
    SEMANTIC_WORK_RESULT_VERSION,
    ProducedArtifactSpec,
    ProducedDocumentSpec,
    SemanticDiagnostic,
    SemanticWorkRequest,
    SemanticWorkResult,
)

SEMANTIC_WORK_PERSISTENCE_SCHEMA_VERSION = "local-semantic-work-sqlite/v1"

__all__ = [
    "SEMANTIC_WORK_PERSISTENCE_SCHEMA_VERSION",
    "SemanticAcceptedRecord",
    "SemanticArtifactRef",
    "SemanticDocumentRef",
    "SemanticWorkPersistenceDiagnostic",
    "SemanticWorkPersistenceFailure",
    "SemanticWorkPersistenceResult",
    "LocalSemanticWorkStore",
    "envelope_for_result",
    "open_semantic_work_store",
]

_DIAGNOSTICS = {
    "invalid_persistence_input": ("input", "The semantic-work persistence input is invalid."),
    "persistence_unavailable": ("storage", "The local semantic-work store is unavailable."),
    "request_not_found": ("storage", "No stored semantic request was found."),
    "stored_request_invalid": ("decode", "The stored semantic request is invalid."),
    "unsupported_storage_schema": ("decode", "The stored semantic-work schema is unsupported."),
    "stale_revision": ("revision", "The semantic-work revision is stale."),
    "revision_conflict": ("revision", "The semantic-work revision conflicts."),
    "immutable_identity_conflict": ("identity", "The semantic-work immutable identity conflicts."),
    "operation_identity_conflict": ("identity", "The semantic-work operation identity conflicts."),
    "request_identity_conflict": ("identity", "The semantic-work request identity conflicts."),
    "lease_conflict": ("revision", "The semantic-work lease is still active."),
    "invalid_semantic_request": ("input", "The semantic request is invalid."),
    "invalid_semantic_result": ("input", "The semantic result is invalid."),
    "persistence_exception": ("adapter", "The local semantic-work adapter failed."),
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS semantic_work_requests (
    request_id TEXT PRIMARY KEY NOT NULL,
    job_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    expected_revision INTEGER NOT NULL,
    operation_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    request_json TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL,
    lease_expires_at TEXT,
    lease_holder TEXT,
    storage_schema_version TEXT NOT NULL
)
"""
_EXPECTED_TABLE_SQL = _SCHEMA_SQL.replace("IF NOT EXISTS ", "", 1).strip()
_PRIMARY_KEY_INDEX_NAME = "sqlite_autoindex_semantic_work_requests_1"
_UNIQUE_OPERATION_INDEX_NAME = "sqlite_autoindex_semantic_work_requests_2"

_STORE_CONSTRUCTION_TOKEN = object()

_STATUS_VOCAB = frozenset({"pending", "leased", "accepted", "rejected"})

_SELECT_COLUMNS = (
    "request_id, job_id, workflow_id, expected_revision, operation_id, kind, "
    "status, request_json, result_json, created_at, lease_expires_at, "
    "lease_holder, storage_schema_version"
)


@dataclass(frozen=True, slots=True)
class SemanticWorkPersistenceDiagnostic:
    code: str
    classification: Literal["input", "storage", "decode", "identity", "revision", "adapter"]
    message: str

    def __post_init__(self) -> None:
        if self.code not in _DIAGNOSTICS:
            raise ValueError("semantic-work diagnostic code is not registered")
        classification, message = _DIAGNOSTICS[self.code]
        if self.classification != classification or self.message != message:
            raise ValueError("semantic-work diagnostic fields are not registered")


@dataclass(frozen=True, slots=True)
class SemanticWorkPersistenceFailure:
    status: Literal["persistence_failed"]
    diagnostics: tuple[SemanticWorkPersistenceDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "persistence_failed":
            raise ValueError("semantic-work persistence failure status is invalid")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) != 1
            or not _is_valid_diagnostic(self.diagnostics[0])
        ):
            raise ValueError("semantic-work persistence failure diagnostics are invalid")


@dataclass(frozen=True, slots=True)
class SemanticArtifactRef:
    """Content-addressed reference to one persisted produced artifact (no bytes)."""

    kind: str
    artifact_id: str
    content_sha256: str

    def __post_init__(self) -> None:
        if type(self.kind) is not str or not self.kind:
            raise ValueError("semantic artifact reference kind is invalid")
        if not _valid_safe_id(self.artifact_id):
            raise ValueError("semantic artifact reference ID is invalid")
        if not _valid_digest(self.content_sha256):
            raise ValueError("semantic artifact reference digest is invalid")


@dataclass(frozen=True, slots=True)
class SemanticDocumentRef:
    """Content-addressed reference to one persisted produced document (no text)."""

    lecture_id: str
    content_sha256: str

    def __post_init__(self) -> None:
        import re

        if type(self.lecture_id) is not str or not re.fullmatch(r"l[1-9][0-9]{0,3}\Z", self.lecture_id):
            raise ValueError("semantic document reference lecture ID is invalid")
        if not _valid_digest(self.content_sha256):
            raise ValueError("semantic document reference digest is invalid")


@dataclass(frozen=True, slots=True)
class SemanticAcceptedRecord:
    """Durable accepted-result envelope: identity + output references only.

    No produced artifact bytes and no lecture source text are stored here.
    Two transport results with identical ids and identical content hashes
    produce identical envelopes; differing content produces differing
    envelopes, so conflicting resubmission is still detected.
    """

    result_version: Literal["semantic-work-result/v1"]
    request_id: str
    operation_id: str
    kind: str
    artifact_refs: tuple[SemanticArtifactRef, ...]
    document_refs: tuple[SemanticDocumentRef, ...]
    diagnostics: tuple[SemanticDiagnostic, ...]
    priority_subject_sha256: str | None
    map_subject_sha256: str | None
    candidate_subject_sha256: str | None
    request_revision: int

    def __post_init__(self) -> None:
        if type(self.result_version) is not str or self.result_version != SEMANTIC_WORK_RESULT_VERSION:
            raise ValueError("semantic accepted record version is unsupported")
        if not _valid_safe_id(self.request_id):
            raise ValueError("semantic accepted record request ID is invalid")
        if not _valid_safe_id(self.operation_id):
            raise ValueError("semantic accepted record operation ID is invalid")
        if type(self.kind) is not str or not self.kind:
            raise ValueError("semantic accepted record kind is invalid")
        if type(self.artifact_refs) is not tuple or any(type(x) is not SemanticArtifactRef for x in self.artifact_refs):
            raise ValueError("semantic accepted record artifact refs are invalid")
        for ref in self.artifact_refs:
            try:
                SemanticArtifactRef(ref.kind, ref.artifact_id, ref.content_sha256)
            except Exception:
                raise ValueError("semantic accepted record artifact refs are invalid") from None
        if type(self.document_refs) is not tuple or any(type(x) is not SemanticDocumentRef for x in self.document_refs):
            raise ValueError("semantic accepted record document refs are invalid")
        for ref in self.document_refs:
            try:
                SemanticDocumentRef(ref.lecture_id, ref.content_sha256)
            except Exception:
                raise ValueError("semantic accepted record document refs are invalid") from None
        if type(self.diagnostics) is not tuple or any(type(x) is not SemanticDiagnostic for x in self.diagnostics):
            raise ValueError("semantic accepted record diagnostics are invalid")
        for diag in self.diagnostics:
            try:
                SemanticDiagnostic(diag.code, diag.message)
            except Exception:
                raise ValueError("semantic accepted record diagnostics are invalid") from None
        if self.priority_subject_sha256 is not None and not _valid_digest(self.priority_subject_sha256):
            raise ValueError("semantic accepted record priority subject hash is invalid")
        if self.map_subject_sha256 is not None and not _valid_digest(self.map_subject_sha256):
            raise ValueError("semantic accepted record map subject hash is invalid")
        if self.candidate_subject_sha256 is not None and not _valid_digest(self.candidate_subject_sha256):
            raise ValueError("semantic accepted record candidate subject hash is invalid")
        if type(self.request_revision) is not int or not (0 <= self.request_revision <= 9_223_372_036_854_775_807):
            raise ValueError("semantic accepted record request revision is invalid")

    def __repr__(self) -> str:
        return (
            f"SemanticAcceptedRecord(request_id={self.request_id!r}, kind={self.kind!r}, "
            f"operation_id={self.operation_id!r}, request_revision={self.request_revision!r}, "
            f"artifacts={len(self.artifact_refs)}, documents={len(self.document_refs)})"
        )


def envelope_for_result(result: SemanticWorkResult) -> SemanticAcceptedRecord:
    """Derive the durable envelope for one transport result (hashes only)."""

    if type(result) is not SemanticWorkResult:
        raise TypeError("result must be exactly SemanticWorkResult")
    artifact_refs = tuple(
        SemanticArtifactRef(
            spec.kind,
            spec.artifact_id,
            hashlib.sha256(spec.content_bytes).hexdigest(),
        )
        for spec in result.produced_artifacts
    )
    document_refs = tuple(
        SemanticDocumentRef(
            spec.lecture_id,
            hashlib.sha256(spec.source_text.encode("utf-8")).hexdigest(),
        )
        for spec in result.produced_documents
    )
    return SemanticAcceptedRecord(
        result.result_version,  # type: ignore[arg-type]
        result.request_id,
        result.operation_id,
        result.kind,  # type: ignore[arg-type]
        artifact_refs,
        document_refs,
        result.diagnostics,
        result.priority_subject_sha256,
        result.map_subject_sha256,
        result.candidate_subject_sha256,
        result.request_revision,
    )


SemanticWorkPersistenceResult: TypeAlias = SemanticWorkRequest | SemanticAcceptedRecord | SemanticWorkPersistenceFailure


class LocalSemanticWorkStore:
    """Local SQLite store for SemanticWorkRequest lifecycle with holder-bound leases."""

    __slots__ = ("_connection",)

    def __init__(self, connection: sqlite3.Connection, token: object) -> None:
        if token is not _STORE_CONSTRUCTION_TOKEN:
            raise TypeError("semantic-work stores must be opened by the adapter")
        self._connection = connection

    def create_request(self, request: SemanticWorkRequest) -> SemanticWorkRequest | SemanticWorkPersistenceFailure:
        """Persist one SemanticWorkRequest idempotently; fail closed on conflict."""

        if type(request) is not SemanticWorkRequest:
            raise TypeError("request must be exactly SemanticWorkRequest")
        try:
            SemanticWorkRequest(
                request.request_version,
                request.request_id,
                request.job_id,
                request.workflow_id,
                request.expected_revision,
                request.operation_id,
                request.kind,  # type: ignore[arg-type]
                request.input_refs,
                request.created_at,
                request.lease_expires_at,
            )
        except Exception:
            return _failure("invalid_semantic_request")

        try:
            if not _storage_is_recognized(self._connection):
                return _failure("unsupported_storage_schema")
        except Exception:
            return _failure("persistence_exception")

        serialized = _serialize_request(request)

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM semantic_work_requests WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()
            if row is not None:
                previous = _decode_row(row, expected_request_id=request.request_id)
                if type(previous) is SemanticWorkPersistenceFailure:
                    self._connection.rollback()
                    return previous
                if type(previous) is SemanticWorkRequest:
                    if previous == request:
                        self._connection.rollback()
                        with self._connection:
                            cur = self._connection.execute(
                                "UPDATE semantic_work_requests SET status=status WHERE request_id=? AND request_json=? AND storage_schema_version=?",
                                (request.request_id, serialized, SEMANTIC_WORK_PERSISTENCE_SCHEMA_VERSION),
                            )
                            if cur.rowcount == 1:
                                return request
                        return _failure("stale_revision")
                    if (
                        previous.job_id != request.job_id
                        or previous.workflow_id != request.workflow_id
                        or previous.expected_revision != request.expected_revision
                        or previous.operation_id != request.operation_id
                        or previous.kind != request.kind
                        or previous.input_refs != request.input_refs
                    ):
                        self._connection.rollback()
                        return _failure("immutable_identity_conflict")
                    else:
                        self._connection.rollback()
                        return _failure("immutable_identity_conflict")
                else:
                    self._connection.rollback()
                    return _failure("immutable_identity_conflict")

            op_row = self._connection.execute(
                "SELECT request_id FROM semantic_work_requests WHERE operation_id = ? AND request_id != ?",
                (request.operation_id, request.request_id),
            ).fetchone()
            if op_row is not None:
                self._connection.rollback()
                return _failure("operation_identity_conflict")

            pending_row = self._connection.execute(
                "SELECT request_id FROM semantic_work_requests WHERE job_id=? AND workflow_id=? AND expected_revision=? AND status IN ('pending','leased') AND request_id != ? LIMIT 1",
                (request.job_id, request.workflow_id, request.expected_revision, request.request_id),
            ).fetchone()
            if pending_row is not None:
                self._connection.rollback()
                return _failure("request_identity_conflict")

            with self._connection:
                self._connection.execute(
                    "INSERT INTO semantic_work_requests (request_id, job_id, workflow_id, expected_revision, operation_id, kind, status, request_json, result_json, created_at, lease_expires_at, lease_holder, storage_schema_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        request.request_id,
                        request.job_id,
                        request.workflow_id,
                        request.expected_revision,
                        request.operation_id,
                        request.kind,
                        "pending",
                        serialized,
                        None,
                        request.created_at,
                        request.lease_expires_at,
                        None,
                        SEMANTIC_WORK_PERSISTENCE_SCHEMA_VERSION,
                    ),
                )
            return request
        except sqlite3.Error:
            _rollback_quietly(self._connection)
            return _failure("persistence_unavailable")
        except Exception:
            _rollback_quietly(self._connection)
            return _failure("persistence_exception")

    def load_original_request(self, request_id: str):
        """Read validated task binding, including after acceptance (no mutation)."""
        if not _valid_safe_id(request_id):
            return _failure("invalid_persistence_input")
        try:
            if not _storage_is_recognized(self._connection):
                return _failure("unsupported_storage_schema")
            row = self._connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM semantic_work_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                return _failure("request_not_found")
            decoded = _decode_row(row, expected_request_id=request_id)
            if type(decoded) is SemanticWorkPersistenceFailure:
                return decoded
            return _deserialize_request(row[7])
        except Exception:
            return _failure("persistence_exception")

    def load(self, request_id: str) -> SemanticWorkRequest | SemanticAcceptedRecord | SemanticWorkPersistenceFailure:
        """Load one request (or accepted envelope if accepted) by exact request_id."""

        if type(request_id) is not str:
            raise TypeError("request_id must be exactly str")
        if not _valid_safe_id(request_id):
            return _failure("invalid_persistence_input")
        try:
            if not _storage_is_recognized(self._connection):
                return _failure("unsupported_storage_schema")
        except Exception:
            return _failure("persistence_exception")
        try:
            row = self._connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM semantic_work_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        except sqlite3.Error:
            return _failure("persistence_unavailable")
        except Exception:
            return _failure("persistence_exception")
        if row is None:
            return _failure("request_not_found")
        return _decode_row(row, expected_request_id=request_id)

    def load_by_operation(self, operation_id: str) -> SemanticWorkRequest | SemanticAcceptedRecord | SemanticWorkPersistenceFailure:
        if type(operation_id) is not str:
            raise TypeError("operation_id must be exactly str")
        if not _valid_safe_id(operation_id):
            return _failure("invalid_persistence_input")
        try:
            if not _storage_is_recognized(self._connection):
                return _failure("unsupported_storage_schema")
        except Exception:
            return _failure("persistence_exception")
        try:
            row = self._connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM semantic_work_requests WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        except sqlite3.Error:
            return _failure("persistence_unavailable")
        except Exception:
            return _failure("persistence_exception")
        if row is None:
            return _failure("request_not_found")
        if type(row) is tuple and len(row) >= 1 and type(row[0]) is str:
            return _decode_row(row, expected_request_id=row[0])
        return _failure("stored_request_invalid")

    def load_pending_for_job(self, job_id: str) -> SemanticWorkRequest | SemanticWorkPersistenceFailure | None:
        """Return single pending/leased request for job, else None. Fail if >1 pending."""
        if type(job_id) is not str:
            raise TypeError("job_id must be exactly str")
        if not _valid_safe_id(job_id):
            return _failure("invalid_persistence_input")
        try:
            if not _storage_is_recognized(self._connection):
                return _failure("unsupported_storage_schema")
        except Exception:
            return _failure("persistence_exception")
        try:
            rows = self._connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM semantic_work_requests WHERE job_id = ? AND status IN ('pending','leased') ORDER BY expected_revision ASC, created_at ASC",
                (job_id,),
            ).fetchall()
        except sqlite3.Error:
            return _failure("persistence_unavailable")
        except Exception:
            return _failure("persistence_exception")
        if not rows:
            return None
        if len(rows) > 1:
            return _failure("request_identity_conflict")
        row = rows[0]
        result = _decode_row(row, expected_request_id=row[0])
        if type(result) is SemanticWorkPersistenceFailure:
            return result
        if type(result) is not SemanticWorkRequest:
            return _failure("stored_request_invalid")
        return result

    def load_current_for_job(
        self, job_id: str, *, workflow_id: str, expected_revision: int
    ) -> SemanticWorkRequest | SemanticWorkPersistenceFailure | None:
        """Return the single pending/leased request bound to the authoritative workflow.

        Only a row with matching ``workflow_id`` AND ``expected_revision`` is
        current. Stale rows (older revisions, superseded work) are
        history/recovery evidence and are never returned here, so they can
        never overlay current authority. Fails closed if more than one
        current-bound row exists.
        """

        if type(job_id) is not str:
            raise TypeError("job_id must be exactly str")
        if type(workflow_id) is not str:
            raise TypeError("workflow_id must be exactly str")
        if type(expected_revision) is not int:
            raise TypeError("expected_revision must be exactly int")
        if not _valid_safe_id(job_id) or not _valid_safe_id(workflow_id):
            return _failure("invalid_persistence_input")
        try:
            if not _storage_is_recognized(self._connection):
                return _failure("unsupported_storage_schema")
        except Exception:
            return _failure("persistence_exception")
        try:
            rows = self._connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM semantic_work_requests WHERE job_id = ? AND workflow_id = ? AND expected_revision = ? AND status IN ('pending','leased') ORDER BY created_at ASC",
                (job_id, workflow_id, expected_revision),
            ).fetchall()
        except sqlite3.Error:
            return _failure("persistence_unavailable")
        except Exception:
            return _failure("persistence_exception")
        if not rows:
            return None
        if len(rows) > 1:
            return _failure("request_identity_conflict")
        row = rows[0]
        result = _decode_row(row, expected_request_id=row[0])
        if type(result) is SemanticWorkPersistenceFailure:
            return result
        if type(result) is not SemanticWorkRequest:
            return _failure("stored_request_invalid")
        return result

    def lease_info(self, request_id: str) -> tuple[str, str | None, str | None] | SemanticWorkPersistenceFailure:
        """Return ``(status, lease_expires_at, lease_holder)`` for one request."""

        if type(request_id) is not str:
            raise TypeError("request_id must be exactly str")
        if not _valid_safe_id(request_id):
            return _failure("invalid_persistence_input")
        try:
            if not _storage_is_recognized(self._connection):
                return _failure("unsupported_storage_schema")
        except Exception:
            return _failure("persistence_exception")
        try:
            row = self._connection.execute(
                "SELECT status, lease_expires_at, lease_holder FROM semantic_work_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        except sqlite3.Error:
            return _failure("persistence_unavailable")
        except Exception:
            return _failure("persistence_exception")
        if row is None:
            return _failure("request_not_found")
        if (
            type(row) is not tuple
            or len(row) != 3
            or type(row[0]) is not str
            or (row[1] is not None and type(row[1]) is not str)
            or (row[2] is not None and type(row[2]) is not str)
        ):
            return _failure("stored_request_invalid")
        return (row[0], row[1], row[2])

    def load_all_for_job(self, job_id: str) -> tuple[SemanticWorkRequest | SemanticAcceptedRecord, ...] | SemanticWorkPersistenceFailure:
        if type(job_id) is not str:
            raise TypeError("job_id must be exactly str")
        if not _valid_safe_id(job_id):
            return _failure("invalid_persistence_input")
        try:
            if not _storage_is_recognized(self._connection):
                return _failure("unsupported_storage_schema")
        except Exception:
            return _failure("persistence_exception")
        try:
            rows = self._connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM semantic_work_requests WHERE job_id = ? ORDER BY expected_revision ASC, created_at ASC",
                (job_id,),
            ).fetchall()
        except sqlite3.Error:
            return _failure("persistence_unavailable")
        except Exception:
            return _failure("persistence_exception")
        result: list[SemanticWorkRequest | SemanticAcceptedRecord] = []
        for row in rows:
            decoded = _decode_row(row, expected_request_id=row[0])
            if type(decoded) is SemanticWorkPersistenceFailure:
                return decoded
            result.append(decoded)  # type: ignore[arg-type]
        return tuple(result)

    def acquire_lease(
        self,
        request_id: str,
        *,
        lease_expires_at: str,
        now_iso: str | None = None,
        holder_id: str,
    ) -> SemanticWorkRequest | SemanticWorkPersistenceFailure:
        """Atomically acquire/renew a holder-bound lease.

        - pending row (no lease): any valid holder acquires.
        - leased + expired (now >= lease): any valid holder re-offers the SAME
          logical request with a new expiry and new holder.
        - leased + active + same holder: idempotent renewal for the same holder.
        - leased + active + different holder: ``lease_conflict``.
        - accepted/rejected: ``stale_revision``.
        """

        if type(request_id) is not str:
            raise TypeError("request_id must be exactly str")
        if type(lease_expires_at) is not str:
            raise TypeError("lease_expires_at must be exactly str")
        if type(holder_id) is not str:
            raise TypeError("holder_id must be exactly str")
        if not _valid_safe_id(request_id):
            return _failure("invalid_persistence_input")
        if not _valid_created_at(lease_expires_at):
            return _failure("invalid_persistence_input")
        if not _valid_safe_id(holder_id):
            return _failure("invalid_persistence_input")
        try:
            if not _storage_is_recognized(self._connection):
                return _failure("unsupported_storage_schema")
        except Exception:
            return _failure("persistence_exception")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM semantic_work_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                self._connection.rollback()
                return _failure("request_not_found")
            decoded = _decode_row(row, expected_request_id=request_id)
            if type(decoded) is SemanticWorkPersistenceFailure:
                self._connection.rollback()
                return decoded
            if type(decoded) is not SemanticWorkRequest:
                self._connection.rollback()
                return _failure("stored_request_invalid")
            current: SemanticWorkRequest = decoded  # type: ignore[assignment]
            status = row[6]
            existing_lease = row[10]
            existing_holder = row[11]
            if status in ("accepted", "rejected"):
                self._connection.rollback()
                return _failure("stale_revision")
            now = now_iso if now_iso is not None else _now_iso()
            if not _valid_created_at(now):
                self._connection.rollback()
                return _failure("invalid_persistence_input")
            if status == "leased" and existing_lease is not None:
                try:
                    active = _is_lease_active(existing_lease, now)
                except ValueError:
                    self._connection.rollback()
                    return _failure("stored_request_invalid")
                if active and existing_holder != holder_id:
                    self._connection.rollback()
                    return _failure("lease_conflict")
            with self._connection:
                new_request = SemanticWorkRequest(
                    current.request_version,
                    current.request_id,
                    current.job_id,
                    current.workflow_id,
                    current.expected_revision,
                    current.operation_id,
                    current.kind,  # type: ignore[arg-type]
                    current.input_refs,
                    current.created_at,
                    lease_expires_at,
                )
                serialized = _serialize_request(new_request)
                cur = self._connection.execute(
                    "UPDATE semantic_work_requests SET status='leased', lease_expires_at=?, lease_holder=?, request_json=? WHERE request_id=? AND status IN ('pending','leased')",
                    (lease_expires_at, holder_id, serialized, request_id),
                )
                if cur.rowcount != 1:
                    return _failure("lease_conflict")
            return new_request
        except sqlite3.Error:
            _rollback_quietly(self._connection)
            return _failure("persistence_unavailable")
        except Exception:
            _rollback_quietly(self._connection)
            return _failure("persistence_exception")

    def release_lease(self, request_id: str) -> SemanticWorkRequest | SemanticWorkPersistenceFailure | None:
        """Revert leased->pending, clearing both the columns and request_json.

        Both the ``lease_expires_at``/``lease_holder`` columns and the embedded
        ``request_json`` lease are cleared atomically so the row still decodes.
        """

        if type(request_id) is not str:
            raise TypeError("request_id must be exactly str")
        if not _valid_safe_id(request_id):
            return _failure("invalid_persistence_input")
        try:
            if not _storage_is_recognized(self._connection):
                return _failure("unsupported_storage_schema")
        except Exception:
            return _failure("persistence_exception")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM semantic_work_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                self._connection.rollback()
                return None
            decoded = _decode_row(row, expected_request_id=request_id)
            if type(decoded) is SemanticWorkPersistenceFailure:
                self._connection.rollback()
                return decoded
            if type(decoded) is not SemanticWorkRequest:
                self._connection.rollback()
                return _failure("stored_request_invalid")
            current: SemanticWorkRequest = decoded  # type: ignore[assignment]
            cleared = SemanticWorkRequest(
                current.request_version,
                current.request_id,
                current.job_id,
                current.workflow_id,
                current.expected_revision,
                current.operation_id,
                current.kind,  # type: ignore[arg-type]
                current.input_refs,
                current.created_at,
                None,
            )
            serialized = _serialize_request(cleared)
            with self._connection:
                self._connection.execute(
                    "UPDATE semantic_work_requests SET status='pending', lease_expires_at=NULL, lease_holder=NULL, request_json=? WHERE request_id=?",
                    (serialized, request_id),
                )
            return cleared
        except sqlite3.Error:
            _rollback_quietly(self._connection)
            return _failure("persistence_unavailable")
        except Exception:
            _rollback_quietly(self._connection)
            return _failure("persistence_exception")

    def reoffer_expired(
        self, job_id: str, *, now_iso: str, lease_duration_seconds: int = 300, holder_id: str
    ) -> SemanticWorkRequest | SemanticWorkPersistenceFailure | None:
        """Re-offer an expired pending/leased request to a new holder, else None."""
        if type(job_id) is not str:
            raise TypeError("job_id must be exactly str")
        if type(holder_id) is not str:
            raise TypeError("holder_id must be exactly str")
        if type(now_iso) is not str or not _valid_created_at(now_iso):
            return _failure("invalid_persistence_input")
        if not _valid_safe_id(holder_id):
            return _failure("invalid_persistence_input")
        try:
            if not _storage_is_recognized(self._connection):
                return _failure("unsupported_storage_schema")
        except Exception:
            return _failure("persistence_exception")
        try:
            pending = self.load_pending_for_job(job_id)
            if pending is None:
                return None
            if type(pending) is SemanticWorkPersistenceFailure:
                return pending
            row = self._connection.execute(
                "SELECT lease_expires_at, status, lease_holder FROM semantic_work_requests WHERE request_id=?",
                (pending.request_id,),
            ).fetchone()
            if row is None:
                return _failure("request_not_found")
            lease_expires_at, status, _existing_holder = row
            if status == "leased" and lease_expires_at is not None:
                try:
                    if _is_lease_active(lease_expires_at, now_iso):
                        return None
                except ValueError:
                    return _failure("stored_request_invalid")
            new_expires = _iso_plus_seconds(now_iso, lease_duration_seconds)
            res = self.acquire_lease(pending.request_id, lease_expires_at=new_expires, now_iso=now_iso, holder_id=holder_id)
            if type(res) is SemanticWorkRequest:
                return res
            if type(res) is SemanticWorkPersistenceFailure and res.diagnostics[0].code == "lease_conflict":
                return None
            return res
        except Exception:
            return _failure("persistence_exception")

    def mark_accepted(self, result: SemanticWorkResult) -> SemanticAcceptedRecord | SemanticWorkPersistenceFailure:
        """Mark a request accepted, persisting the reference-only envelope.

        Idempotent exact-envelope repeat; conflicting envelope fails closed.
        Produced bytes/text are never stored.
        """

        if type(result) is not SemanticWorkResult:
            raise TypeError("result must be exactly SemanticWorkResult")
        try:
            SemanticWorkResult(
                result.result_version,
                result.request_id,
                result.operation_id,
                result.kind,  # type: ignore[arg-type]
                result.produced_artifacts,
                result.produced_documents,
                result.diagnostics,
                result.priority_subject_sha256,
                result.map_subject_sha256,
                result.candidate_subject_sha256,
                result.request_revision,
            )
            envelope = envelope_for_result(result)
        except Exception:
            return _failure("invalid_semantic_result")
        try:
            if not _storage_is_recognized(self._connection):
                return _failure("unsupported_storage_schema")
        except Exception:
            return _failure("persistence_exception")
        serialized_envelope = _serialize_envelope(envelope)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM semantic_work_requests WHERE request_id = ?",
                (result.request_id,),
            ).fetchone()
            if row is None:
                self._connection.rollback()
                return _failure("request_not_found")
            decoded = _decode_row(row, expected_request_id=result.request_id)
            if type(decoded) is SemanticWorkPersistenceFailure:
                self._connection.rollback()
                return decoded
            status = row[6]
            stored_result_json = row[8]
            stored_kind = row[5]
            stored_op = row[4]
            stored_rev = row[3]
            if stored_kind != result.kind:
                self._connection.rollback()
                return _failure("immutable_identity_conflict")
            if stored_op != result.operation_id:
                self._connection.rollback()
                return _failure("immutable_identity_conflict")
            if stored_rev != result.request_revision:
                self._connection.rollback()
                return _failure("immutable_identity_conflict")
            if status == "accepted":
                if stored_result_json == serialized_envelope:
                    self._connection.rollback()
                    with self._connection:
                        cur = self._connection.execute(
                            "UPDATE semantic_work_requests SET status=status WHERE request_id=? AND result_json=?",
                            (result.request_id, serialized_envelope),
                        )
                        if cur.rowcount == 1:
                            return envelope
                    return _failure("stale_revision")
                else:
                    self._connection.rollback()
                    return _failure("immutable_identity_conflict")
            if status == "rejected":
                self._connection.rollback()
                return _failure("immutable_identity_conflict")
            try:
                current_req = _deserialize_request(row[7])
                cleared = SemanticWorkRequest(
                    current_req.request_version,
                    current_req.request_id,
                    current_req.job_id,
                    current_req.workflow_id,
                    current_req.expected_revision,
                    current_req.operation_id,
                    current_req.kind,  # type: ignore[arg-type]
                    current_req.input_refs,
                    current_req.created_at,
                    None,
                )
                cleared_json = _serialize_request(cleared)
            except Exception:
                self._connection.rollback()
                return _failure("stored_request_invalid")
            with self._connection:
                cur = self._connection.execute(
                    "UPDATE semantic_work_requests SET status='accepted', request_json=?, result_json=?, lease_expires_at=NULL, lease_holder=NULL WHERE request_id=? AND status IN ('pending','leased')",
                    (cleared_json, serialized_envelope, result.request_id),
                )
                if cur.rowcount != 1:
                    return _failure("stale_revision")
            return envelope
        except sqlite3.Error:
            _rollback_quietly(self._connection)
            return _failure("persistence_unavailable")
        except Exception:
            _rollback_quietly(self._connection)
            return _failure("persistence_exception")

    def mark_rejected(self, request_id: str, diagnostics: tuple[SemanticDiagnostic, ...] | None = None) -> SemanticWorkRequest | SemanticWorkPersistenceFailure:
        """Mark request as rejected (failed). Not used for accepted window but for validation failures."""

        if type(request_id) is not str or not _valid_safe_id(request_id):
            return _failure("invalid_persistence_input")
        try:
            if not _storage_is_recognized(self._connection):
                return _failure("unsupported_storage_schema")
        except Exception:
            return _failure("persistence_exception")
        try:
            with self._connection:
                cur = self._connection.execute("UPDATE semantic_work_requests SET status='rejected' WHERE request_id=? AND status IN ('pending','leased')", (request_id,))
                if cur.rowcount != 1:
                    row = self._connection.execute("SELECT status FROM semantic_work_requests WHERE request_id=?", (request_id,)).fetchone()
                    if row is None:
                        return _failure("request_not_found")
                    if row[0] in ("accepted", "rejected"):
                        return _failure("immutable_identity_conflict")
                    return _failure("stale_revision")
            row = self._connection.execute(f"SELECT {_SELECT_COLUMNS} FROM semantic_work_requests WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                return _failure("request_not_found")
            return _decode_row(row, expected_request_id=request_id)  # type: ignore[return-value]
        except sqlite3.Error:
            return _failure("persistence_unavailable")
        except Exception:
            return _failure("persistence_exception")

    def close(self) -> None:
        self._connection.close()


def open_semantic_work_store(database_path: str | Path) -> LocalSemanticWorkStore | SemanticWorkPersistenceFailure:
    if type(database_path) is not str and not isinstance(database_path, Path):
        raise TypeError("database_path must be exactly str or Path")
    try:
        connection = sqlite3.connect(database_path)
        connection.execute(_SCHEMA_SQL)
        if not _storage_is_recognized(connection):
            connection.close()
            return _failure("unsupported_storage_schema")
        return LocalSemanticWorkStore(connection, _STORE_CONSTRUCTION_TOKEN)
    except sqlite3.Error:
        return _failure("persistence_unavailable")
    except Exception:
        return _failure("persistence_exception")


def _serialize_request(request: SemanticWorkRequest) -> str:
    payload = {
        "request_version": request.request_version,
        "request_id": request.request_id,
        "job_id": request.job_id,
        "workflow_id": request.workflow_id,
        "expected_revision": request.expected_revision,
        "operation_id": request.operation_id,
        "kind": request.kind,
        "input_refs": list(request.input_refs),
        "created_at": request.created_at,
        "lease_expires_at": request.lease_expires_at,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _deserialize_request(payload: str) -> SemanticWorkRequest:
    data = json.loads(payload)
    if type(data) is not dict or set(data.keys()) != {"request_version", "request_id", "job_id", "workflow_id", "expected_revision", "operation_id", "kind", "input_refs", "created_at", "lease_expires_at"}:
        raise ValueError("request JSON invalid")
    if type(data["input_refs"]) is not list or any(type(x) is not str for x in data["input_refs"]):
        raise ValueError("input_refs invalid")
    return SemanticWorkRequest(
        data["request_version"],
        data["request_id"],
        data["job_id"],
        data["workflow_id"],
        data["expected_revision"],
        data["operation_id"],
        data["kind"],  # type: ignore[arg-type]
        tuple(data["input_refs"]),
        data["created_at"],
        data["lease_expires_at"],
    )


def _serialize_envelope(record: SemanticAcceptedRecord) -> str:
    payload = {
        "result_version": record.result_version,
        "request_id": record.request_id,
        "operation_id": record.operation_id,
        "kind": record.kind,
        "artifact_refs": [
            {"kind": ref.kind, "artifact_id": ref.artifact_id, "content_sha256": ref.content_sha256}
            for ref in record.artifact_refs
        ],
        "document_refs": [
            {"lecture_id": ref.lecture_id, "content_sha256": ref.content_sha256}
            for ref in record.document_refs
        ],
        "diagnostics": [{"code": diag.code, "message": diag.message} for diag in record.diagnostics],
        "priority_subject_sha256": record.priority_subject_sha256,
        "map_subject_sha256": record.map_subject_sha256,
        "candidate_subject_sha256": record.candidate_subject_sha256,
        "request_revision": record.request_revision,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _deserialize_envelope(payload: str) -> SemanticAcceptedRecord:
    data = json.loads(payload)
    expected_keys = {"result_version", "request_id", "operation_id", "kind", "artifact_refs", "document_refs", "diagnostics", "priority_subject_sha256", "map_subject_sha256", "candidate_subject_sha256", "request_revision"}
    if type(data) is not dict:
        raise ValueError("accepted envelope JSON invalid")
    # Reject legacy content-bearing envelopes outright: bytes/text must never
    # be reintroduced into this store.
    if "produced_artifacts" in data or "produced_documents" in data:
        raise ValueError("accepted envelope must not carry produced content")
    if set(data.keys()) != expected_keys:
        raise ValueError("accepted envelope JSON invalid")
    artifact_refs = []
    for item in data["artifact_refs"]:
        if type(item) is not dict or set(item.keys()) != {"kind", "artifact_id", "content_sha256"}:
            raise ValueError("artifact ref invalid")
        artifact_refs.append(SemanticArtifactRef(item["kind"], item["artifact_id"], item["content_sha256"]))
    document_refs = []
    for item in data["document_refs"]:
        if type(item) is not dict or set(item.keys()) != {"lecture_id", "content_sha256"}:
            raise ValueError("document ref invalid")
        document_refs.append(SemanticDocumentRef(item["lecture_id"], item["content_sha256"]))
    diagnostics = tuple(SemanticDiagnostic(item["code"], item["message"]) for item in data["diagnostics"])
    return SemanticAcceptedRecord(
        data["result_version"],  # type: ignore[arg-type]
        data["request_id"],
        data["operation_id"],
        data["kind"],
        tuple(artifact_refs),
        tuple(document_refs),
        diagnostics,
        data["priority_subject_sha256"],
        data["map_subject_sha256"],
        data["candidate_subject_sha256"],
        data["request_revision"],
    )


def _decode_row(row: tuple[object, ...], *, expected_request_id: str) -> SemanticWorkRequest | SemanticAcceptedRecord | SemanticWorkPersistenceFailure:
    if type(row) is not tuple or len(row) != 13:
        return _failure("stored_request_invalid")
    (
        request_id,
        job_id,
        workflow_id,
        expected_revision,
        operation_id,
        kind,
        status,
        request_json,
        result_json,
        created_at,
        lease_expires_at,
        lease_holder,
        schema_version,
    ) = row
    if type(schema_version) is not str or schema_version != SEMANTIC_WORK_PERSISTENCE_SCHEMA_VERSION:
        return _failure("unsupported_storage_schema")
    if (
        type(request_id) is not str
        or request_id != expected_request_id
        or type(job_id) is not str
        or type(workflow_id) is not str
        or type(expected_revision) is not int
        or type(operation_id) is not str
        or type(kind) is not str
        or type(status) is not str
        or status not in _STATUS_VOCAB
        or type(request_json) is not str
        or (result_json is not None and type(result_json) is not str)
        or type(created_at) is not str
        or (lease_expires_at is not None and type(lease_expires_at) is not str)
        or (lease_holder is not None and type(lease_holder) is not str)
    ):
        return _failure("stored_request_invalid")
    if lease_holder is not None and not _valid_safe_id(lease_holder):
        return _failure("stored_request_invalid")
    if lease_expires_at is not None and not _valid_created_at(lease_expires_at):
        return _failure("stored_request_invalid")
    # Lease columns must be consistent: a holder requires an expiry and vice versa.
    if (lease_holder is None) != (lease_expires_at is None):
        if status in ("pending", "leased"):
            return _failure("stored_request_invalid")
    try:
        request = _deserialize_request(request_json)
        if (
            request.request_id != request_id
            or request.job_id != job_id
            or request.workflow_id != workflow_id
            or request.expected_revision != expected_revision
            or request.operation_id != operation_id
            or request.kind != kind
            or request.created_at != created_at
            or request.lease_expires_at != lease_expires_at
        ):
            return _failure("stored_request_invalid")
        if status == "accepted":
            if result_json is None:
                return _failure("stored_request_invalid")
            record = _deserialize_envelope(result_json)
            if record.request_id != request_id or record.operation_id != operation_id or record.kind != kind:
                return _failure("stored_request_invalid")
            return record
        if status == "rejected":
            return request
        return request
    except (TypeError, ValueError, json.JSONDecodeError):
        return _failure("stored_request_invalid")
    except Exception:
        return _failure("persistence_exception")


def _schema_is_recognized(connection: sqlite3.Connection) -> bool:
    try:
        table_rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM main.sqlite_schema WHERE name = ?",
            ("semantic_work_requests",),
        ).fetchall()
        columns = connection.execute("PRAGMA main.table_xinfo(semantic_work_requests)").fetchall()
        foreign_keys = connection.execute("PRAGMA main.foreign_key_list(semantic_work_requests)").fetchall()
        indexes = connection.execute("PRAGMA main.index_list(semantic_work_requests)").fetchall()
        primary_key_columns = connection.execute(f"PRAGMA main.index_xinfo({_PRIMARY_KEY_INDEX_NAME})").fetchall()
        unique_operation_columns = connection.execute(f"PRAGMA main.index_xinfo({_UNIQUE_OPERATION_INDEX_NAME})").fetchall()
        triggers = connection.execute(
            "SELECT type, name, tbl_name, sql FROM main.sqlite_schema WHERE type='trigger' AND tbl_name=?",
            ("semantic_work_requests",),
        ).fetchall()
        temporary_objects = connection.execute(
            "SELECT type, name, tbl_name, sql FROM temp.sqlite_schema WHERE name=? OR tbl_name=?",
            ("semantic_work_requests", "semantic_work_requests"),
        ).fetchall()
    except sqlite3.Error:
        return False
    expected_columns = (
        (0, "request_id", "TEXT", 1, None, 1, 0),
        (1, "job_id", "TEXT", 1, None, 0, 0),
        (2, "workflow_id", "TEXT", 1, None, 0, 0),
        (3, "expected_revision", "INTEGER", 1, None, 0, 0),
        (4, "operation_id", "TEXT", 1, None, 0, 0),
        (5, "kind", "TEXT", 1, None, 0, 0),
        (6, "status", "TEXT", 1, None, 0, 0),
        (7, "request_json", "TEXT", 1, None, 0, 0),
        (8, "result_json", "TEXT", 0, None, 0, 0),
        (9, "created_at", "TEXT", 1, None, 0, 0),
        (10, "lease_expires_at", "TEXT", 0, None, 0, 0),
        (11, "lease_holder", "TEXT", 0, None, 0, 0),
        (12, "storage_schema_version", "TEXT", 1, None, 0, 0),
    )
    expected_primary_key_columns = (
        (0, 0, "request_id", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0),
    )
    expected_unique_operation_columns = (
        (0, 4, "operation_id", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0),
    )
    expected_indexes_set = {
        (0, _UNIQUE_OPERATION_INDEX_NAME, 1, "u", 0),
        (1, _PRIMARY_KEY_INDEX_NAME, 1, "pk", 0),
    }
    indexes_set = set(indexes) if isinstance(indexes, list) else set()
    return (
        type(table_rows) is list
        and len(table_rows) == 1
        and type(table_rows[0]) is tuple
        and len(table_rows[0]) == 4
        and table_rows[0][:3] == ("table", "semantic_work_requests", "semantic_work_requests")
        and _schema_sql_key(table_rows[0][3]) == _schema_sql_key(_EXPECTED_TABLE_SQL)
        and type(columns) is list
        and tuple(columns) == expected_columns
        and type(foreign_keys) is list
        and foreign_keys == []
        and type(indexes) is list
        and indexes_set == expected_indexes_set
        and type(primary_key_columns) is list
        and tuple(primary_key_columns) == expected_primary_key_columns
        and type(unique_operation_columns) is list
        and tuple(unique_operation_columns) == expected_unique_operation_columns
        and type(triggers) is list
        and triggers == []
        and type(temporary_objects) is list
        and temporary_objects == []
    )


def _storage_is_recognized(connection: sqlite3.Connection) -> bool:
    if not _schema_is_recognized(connection):
        return False
    try:
        row = connection.execute(
            "SELECT 1 FROM semantic_work_requests WHERE typeof(storage_schema_version) != 'text' OR storage_schema_version != ? LIMIT 1",
            (SEMANTIC_WORK_PERSISTENCE_SCHEMA_VERSION,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is None


def _schema_sql_key(value: object) -> str | None:
    if type(value) is not str:
        return None
    return "".join(value.split()).casefold()


def _rollback_quietly(connection: sqlite3.Connection) -> None:
    try:
        connection.rollback()
    except Exception:
        pass


def _is_valid_diagnostic(value: object) -> bool:
    if type(value) is not SemanticWorkPersistenceDiagnostic:
        return False
    try:
        SemanticWorkPersistenceDiagnostic(value.code, value.classification, value.message)
    except (AttributeError, TypeError, ValueError):
        return False
    return True


def _failure(code: str) -> SemanticWorkPersistenceFailure:
    classification, message = _DIAGNOSTICS[code]
    return SemanticWorkPersistenceFailure(status="persistence_failed", diagnostics=(SemanticWorkPersistenceDiagnostic(code, classification, message),))


def _valid_safe_id(value: object) -> bool:
    import re

    return type(value) is str and value not in (".", "..") and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}\Z", value) is not None


def _valid_created_at(value: object) -> bool:
    import re

    return type(value) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z", value) is not None


def _valid_digest(value: object) -> bool:
    import re

    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}\Z", value) is not None


def _parse_created_at(value: str) -> datetime:
    """Parse an accepted created_at timestamp into an aware UTC datetime.

    Chronological comparison must use parsed values, never lexicographic
    string comparison: the accepted grammar permits fractional seconds of
    varying width.
    """

    if type(value) is not str or not value.endswith("Z"):
        raise ValueError("created_at timestamp is invalid")
    text = value[:-1]
    parsed: datetime | None = None
    for format in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, format)
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValueError("created_at timestamp is invalid")
    return parsed.replace(tzinfo=timezone.utc)


def _is_lease_active(lease_expires_at: str, now_iso: str) -> bool:
    """Return True while ``now`` is strictly before expiry (parsed comparison)."""

    return _parse_created_at(now_iso) < _parse_created_at(lease_expires_at)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_plus_seconds(iso: str, seconds: int) -> str:
    from datetime import datetime, timezone

    try:
        parsed = _parse_created_at(iso)
    except ValueError:
        parsed = datetime.now(timezone.utc)
    from datetime import timedelta

    return (parsed + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
