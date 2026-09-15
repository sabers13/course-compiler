"""Local SQLite persistence for one exact T003 workflow-state snapshot."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Literal, TypeAlias

from .rendering import DocumentReference
from .workflow import (
    ApprovalRecord,
    HistoricalCandidateRecord,
    LectureMapRecord,
    LectureProgress,
    MapReopenContext,
    OperationReceipt,
    PendingSourceRecord,
    PolicyReference,
    PriorityBasisRecord,
    SourceAssessmentRecord,
    SourceEvidenceReference,
    ValidationRecord,
    WorkflowArtifactReference,
    WorkflowBlocker,
    WorkflowDiagnostic,
    WorkflowFailure,
    WorkflowPolicySet,
    WorkflowState,
    validate_workflow_state,
)


WORKFLOW_PERSISTENCE_SCHEMA_VERSION = "local-workflow-state-sqlite/v1"

__all__ = [
    "WORKFLOW_PERSISTENCE_SCHEMA_VERSION",
    "LocalWorkflowStateStore",
    "WorkflowPersistenceDiagnostic",
    "WorkflowPersistenceFailure",
    "WorkflowPersistenceResult",
    "open_workflow_state_store",
]


_DIAGNOSTICS = {
    "invalid_persistence_input": ("input", "The workflow-state snapshot is invalid."),
    "persistence_unavailable": ("storage", "The local workflow-state store is unavailable."),
    "workflow_not_found": ("storage", "No stored workflow-state snapshot was found."),
    "stored_state_invalid": ("decode", "The stored workflow-state snapshot is invalid."),
    "unsupported_storage_schema": ("decode", "The stored workflow-state schema is unsupported."),
    "initial_revision_required": ("revision", "The first stored snapshot must have revision zero."),
    "stale_revision": ("revision", "The workflow-state snapshot revision is stale."),
    "revision_gap": ("revision", "The workflow-state snapshot revision has a gap."),
    "revision_conflict": ("revision", "The workflow-state snapshot conflicts at its revision."),
    "workflow_identity_mismatch": ("revision", "The stored workflow identity is inconsistent."),
    "persistence_exception": ("adapter", "The local workflow-state adapter failed."),
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workflow_state_snapshots (
    workflow_id TEXT PRIMARY KEY NOT NULL,
    revision INTEGER NOT NULL,
    storage_schema_version TEXT NOT NULL,
    payload TEXT NOT NULL
)
"""

_RECORD_TYPES = (
    ApprovalRecord,
    DocumentReference,
    HistoricalCandidateRecord,
    LectureMapRecord,
    LectureProgress,
    MapReopenContext,
    OperationReceipt,
    PendingSourceRecord,
    PolicyReference,
    PriorityBasisRecord,
    SourceAssessmentRecord,
    SourceEvidenceReference,
    ValidationRecord,
    WorkflowArtifactReference,
    WorkflowBlocker,
    WorkflowDiagnostic,
    WorkflowFailure,
    WorkflowPolicySet,
    WorkflowState,
)
_RECORD_BY_NAME = {record_type.__name__: record_type for record_type in _RECORD_TYPES}
_STORE_CONSTRUCTION_TOKEN = object()


@dataclass(frozen=True, slots=True)
class WorkflowPersistenceDiagnostic:
    """One fixed, content-safe local-persistence diagnostic."""

    code: str
    classification: Literal["input", "storage", "decode", "revision", "adapter"]
    message: str

    def __post_init__(self) -> None:
        if self.code not in _DIAGNOSTICS:
            raise ValueError("persistence diagnostic code is not registered")
        classification, message = _DIAGNOSTICS[self.code]
        if self.classification != classification or self.message != message:
            raise ValueError("persistence diagnostic fields are not registered")


@dataclass(frozen=True, slots=True)
class WorkflowPersistenceFailure:
    """A content-safe persistence failure with exactly one fixed diagnostic."""

    status: Literal["persistence_failed"]
    diagnostics: tuple[WorkflowPersistenceDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "persistence_failed":
            raise ValueError("persistence failure status is invalid")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) != 1
            or not _is_valid_diagnostic(self.diagnostics[0])
        ):
            raise ValueError("persistence failure diagnostics are invalid")


WorkflowPersistenceResult: TypeAlias = WorkflowState | WorkflowPersistenceFailure


class LocalWorkflowStateStore:
    """A local store with no workflow-transition or course-content behavior."""

    __slots__ = ("_connection",)

    def __init__(self, connection: sqlite3.Connection, token: object) -> None:
        if token is not _STORE_CONSTRUCTION_TOKEN:
            raise TypeError("workflow state stores must be opened by the adapter")
        self._connection = connection

    def save(self, state: WorkflowState) -> WorkflowPersistenceResult:
        """Atomically store one exact valid current snapshot."""

        if type(state) is not WorkflowState:
            raise TypeError("state must be exactly WorkflowState")
        try:
            if not _is_valid_workflow_state(state):
                return _failure("invalid_persistence_input")
            payload = _serialize_payload(state)
        except Exception:
            return _failure("persistence_exception")

        try:
            existing = self._connection.execute(
                "SELECT workflow_id, revision, storage_schema_version, payload "
                "FROM workflow_state_snapshots WHERE workflow_id = ?",
                (state.workflow_id,),
            ).fetchone()
            if existing is None:
                if state.revision != 0:
                    return _failure("initial_revision_required")
                self._insert_snapshot(state, payload)
                return state
            return self._replace_if_valid_advancement(state, payload, existing)
        except sqlite3.Error:
            return _failure("persistence_unavailable")
        except Exception:
            return _failure("persistence_exception")

    def load(self, workflow_id: str) -> WorkflowPersistenceResult:
        """Reopen one validated current snapshot by its content-free identity."""

        if type(workflow_id) is not str:
            raise TypeError("workflow_id must be exactly str")
        try:
            row = self._connection.execute(
                "SELECT workflow_id, revision, storage_schema_version, payload "
                "FROM workflow_state_snapshots WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
        except sqlite3.Error:
            return _failure("persistence_unavailable")
        except Exception:
            return _failure("persistence_exception")
        if row is None:
            return _failure("workflow_not_found")
        return _decode_row(row, expected_workflow_id=workflow_id)

    def close(self) -> None:
        """Close this adapter's connection; it has no persisted close state."""

        self._connection.close()

    def _insert_snapshot(self, state: WorkflowState, payload: str) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO workflow_state_snapshots "
                "(workflow_id, revision, storage_schema_version, payload) VALUES (?, ?, ?, ?)",
                (state.workflow_id, state.revision, WORKFLOW_PERSISTENCE_SCHEMA_VERSION, payload),
            )

    def _replace_if_valid_advancement(
        self, state: WorkflowState, payload: str, row: tuple[object, ...]
    ) -> WorkflowPersistenceResult:
        previous = _decode_row(row, expected_workflow_id=state.workflow_id)
        if type(previous) is WorkflowPersistenceFailure:
            return previous
        previous_payload = row[3]
        if state.revision == previous.revision:
            if type(previous_payload) is not str or payload != previous_payload:
                return _failure("revision_conflict")
            return self._confirm_idempotent_snapshot(state, payload)
        if state.revision < previous.revision:
            return _failure("stale_revision")
        if state.revision != previous.revision + 1:
            return _failure("revision_gap")
        if not _is_next_t003_snapshot(previous, state):
            return _failure("revision_conflict")
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE workflow_state_snapshots "
                "SET revision = ?, storage_schema_version = ?, payload = ? "
                "WHERE workflow_id = ? AND revision = ?",
                (
                    state.revision,
                    WORKFLOW_PERSISTENCE_SCHEMA_VERSION,
                    payload,
                    state.workflow_id,
                    previous.revision,
                ),
            )
            if cursor.rowcount != 1:
                return _failure("stale_revision")
        return state

    def _confirm_idempotent_snapshot(
        self, state: WorkflowState, payload: str
    ) -> WorkflowPersistenceResult:
        """Linearize an idempotent success against concurrent SQLite writers."""

        with self._connection:
            cursor = self._connection.execute(
                "UPDATE workflow_state_snapshots SET revision = revision "
                "WHERE workflow_id = ? AND revision = ? "
                "AND storage_schema_version = ? AND payload = ?",
                (
                    state.workflow_id,
                    state.revision,
                    WORKFLOW_PERSISTENCE_SCHEMA_VERSION,
                    payload,
                ),
            )
            if cursor.rowcount == 1:
                return state
        return _failure("stale_revision")


def open_workflow_state_store(database_path: str | Path) -> LocalWorkflowStateStore | WorkflowPersistenceFailure:
    """Open or deterministically initialize one trusted local SQLite database."""

    if type(database_path) is not str and not isinstance(database_path, Path):
        raise TypeError("database_path must be exactly str or Path")
    try:
        connection = sqlite3.connect(database_path)
        connection.execute(_SCHEMA_SQL)
        if not _schema_is_recognized(connection):
            connection.close()
            return _failure("unsupported_storage_schema")
        return LocalWorkflowStateStore(connection, _STORE_CONSTRUCTION_TOKEN)
    except sqlite3.Error:
        return _failure("persistence_unavailable")
    except Exception:
        return _failure("persistence_exception")


def _schema_is_recognized(connection: sqlite3.Connection) -> bool:
    try:
        rows = connection.execute("PRAGMA table_info(workflow_state_snapshots)").fetchall()
    except sqlite3.Error:
        return False
    expected = (
        ("workflow_id", "TEXT", 1),
        ("revision", "INTEGER", 0),
        ("storage_schema_version", "TEXT", 0),
        ("payload", "TEXT", 0),
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


def _is_valid_workflow_state(value: object) -> bool:
    if type(value) is not WorkflowState:
        return False
    try:
        return validate_workflow_state(value) == ()
    except Exception:
        return False


def _is_next_t003_snapshot(previous: WorkflowState, candidate: WorkflowState) -> bool:
    """Require the T003 receipt sequence to extend exactly one accepted revision."""

    try:
        return (
            candidate.workflow_id == previous.workflow_id
            and candidate.revision == previous.revision + 1
            and type(candidate.operation_receipts) is tuple
            and candidate.operation_receipts[:-1] == previous.operation_receipts
            and len(candidate.operation_receipts) == len(previous.operation_receipts) + 1
            and candidate.operation_receipts[-1].from_revision == previous.revision
            and candidate.operation_receipts[-1].to_revision == candidate.revision
        )
    except Exception:
        return False


def _serialize_payload(state: WorkflowState) -> str:
    value = {
        "storage_schema_version": WORKFLOW_PERSISTENCE_SCHEMA_VERSION,
        "state": _encode_value(state),
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _encode_value(value: object) -> object:
    if value is None or type(value) is str or type(value) is int:
        return value
    if type(value) is tuple:
        return {"$type": "tuple", "items": [_encode_value(item) for item in value]}
    if type(value) in _RECORD_TYPES and is_dataclass(value):
        return {
            "$type": type(value).__name__,
            "fields": {item.name: _encode_value(getattr(value, item.name)) for item in fields(value)},
        }
    raise ValueError("workflow-state storage value is unsupported")


def _decode_row(row: tuple[object, ...], *, expected_workflow_id: str) -> WorkflowPersistenceResult:
    if type(row) is not tuple or len(row) != 4:
        return _failure("stored_state_invalid")
    workflow_id, revision, schema_version, payload = row
    if type(schema_version) is not str or schema_version != WORKFLOW_PERSISTENCE_SCHEMA_VERSION:
        return _failure("unsupported_storage_schema")
    if type(workflow_id) is not str or workflow_id != expected_workflow_id or type(revision) is not int or type(payload) is not str:
        return _failure("stored_state_invalid")
    try:
        decoded = json.loads(payload)
        if type(decoded) is not dict or set(decoded) != {"storage_schema_version", "state"}:
            return _failure("stored_state_invalid")
        if decoded["storage_schema_version"] != WORKFLOW_PERSISTENCE_SCHEMA_VERSION:
            return _failure("unsupported_storage_schema")
        state = _decode_value(decoded["state"])
    except (TypeError, ValueError, json.JSONDecodeError, KeyError):
        return _failure("stored_state_invalid")
    except Exception:
        return _failure("persistence_exception")
    if type(state) is not WorkflowState or not _is_valid_workflow_state(state):
        return _failure("stored_state_invalid")
    if state.workflow_id != workflow_id:
        return _failure("workflow_identity_mismatch")
    if state.revision != revision:
        return _failure("stored_state_invalid")
    return state


def _decode_value(value: object) -> object:
    if value is None or type(value) is str or type(value) is int:
        return value
    if type(value) is not dict or type(value.get("$type")) is not str:
        raise ValueError("workflow-state storage value is invalid")
    type_name = value["$type"]
    if type_name == "tuple":
        if set(value) != {"$type", "items"} or type(value["items"]) is not list:
            raise ValueError("workflow-state tuple is invalid")
        return tuple(_decode_value(item) for item in value["items"])
    record_type = _RECORD_BY_NAME.get(type_name)
    if record_type is None or set(value) != {"$type", "fields"} or type(value["fields"]) is not dict:
        raise ValueError("workflow-state record is invalid")
    encoded_fields = value["fields"]
    expected_names = {item.name for item in fields(record_type)}
    if set(encoded_fields) != expected_names:
        raise ValueError("workflow-state record fields are invalid")
    return record_type(**{name: _decode_value(encoded_fields[name]) for name in expected_names})


def _is_valid_diagnostic(value: object) -> bool:
    if type(value) is not WorkflowPersistenceDiagnostic:
        return False
    try:
        WorkflowPersistenceDiagnostic(value.code, value.classification, value.message)
    except (AttributeError, TypeError, ValueError):
        return False
    return True


def _failure(code: str) -> WorkflowPersistenceFailure:
    classification, message = _DIAGNOSTICS[code]
    return WorkflowPersistenceFailure(
        status="persistence_failed",
        diagnostics=(WorkflowPersistenceDiagnostic(code, classification, message),),
    )
