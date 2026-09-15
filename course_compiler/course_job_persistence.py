"""Local SQLite persistence for CourseJobRecord (T048/T050) + WorkflowState projection."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

from .course import COURSE_REFERENCE_VERSION, CourseReference
from .workflow import WorkflowState

COURSE_JOB_PERSISTENCE_SCHEMA_VERSION_V1 = "local-course-job-sqlite/v1"
COURSE_JOB_PERSISTENCE_SCHEMA_VERSION = "local-course-job-sqlite/v2"
COURSE_JOB_RECORD_VERSION_V1 = "course-job-record/v1"
COURSE_JOB_RECORD_VERSION = "course-job-record/v2"

__all__ = [
    "COURSE_JOB_PERSISTENCE_SCHEMA_VERSION",
    "COURSE_JOB_PERSISTENCE_SCHEMA_VERSION_V1",
    "COURSE_JOB_RECORD_VERSION",
    "COURSE_JOB_RECORD_VERSION_V1",
    "COURSE_JOB_STATUS_VOCABULARY",
    "CourseJobPersistenceDiagnostic",
    "CourseJobPersistenceFailure",
    "CourseJobPersistenceResult",
    "CourseJobRecord",
    "LocalCourseJobStore",
    "derive_job_status",
    "open_course_job_store",
]

_DIAGNOSTICS = {
    "invalid_persistence_input": ("input", "The course-job persistence input is invalid."),
    "persistence_unavailable": ("storage", "The local course-job store is unavailable."),
    "job_not_found": ("storage", "No stored course job was found."),
    "stored_job_invalid": ("decode", "The stored course job is invalid."),
    "unsupported_storage_schema": ("decode", "The stored course-job schema is unsupported."),
    "stale_revision": ("revision", "The course-job revision is stale."),
    "revision_conflict": ("revision", "The course-job revision conflicts."),
    "immutable_identity_conflict": ("identity", "The course-job immutable identity conflicts."),
    "job_identity_mismatch": ("identity", "The stored course-job identity does not match."),
    "workflow_identity_conflict": ("identity", "The course-job workflow identity conflicts."),
    "invalid_job_status": ("input", "The course-job status is invalid."),
    "persistence_exception": ("adapter", "The local course-job adapter failed."),
}

_SCHEMA_SQL_V1 = """
CREATE TABLE IF NOT EXISTS course_jobs (
    job_id TEXT PRIMARY KEY NOT NULL,
    course_id TEXT NOT NULL,
    course_reference_version TEXT NOT NULL,
    workflow_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    created_revision INTEGER NOT NULL,
    current_revision INTEGER NOT NULL,
    metadata_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    current_stage TEXT,
    current_disposition TEXT,
    ai_mode TEXT NOT NULL,
    quality_mode TEXT NOT NULL,
    retry_count INTEGER NOT NULL,
    failure_code TEXT,
    storage_schema_version TEXT NOT NULL
)
"""

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS course_jobs (
    job_id TEXT PRIMARY KEY NOT NULL,
    course_id TEXT NOT NULL,
    course_reference_version TEXT NOT NULL,
    workflow_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    created_revision INTEGER NOT NULL,
    current_revision INTEGER NOT NULL,
    metadata_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    current_stage TEXT,
    current_disposition TEXT,
    ai_mode TEXT NOT NULL,
    quality_mode TEXT NOT NULL,
    retry_count INTEGER NOT NULL,
    failure_code TEXT,
    storage_schema_version TEXT NOT NULL,
    completed_build_id TEXT,
    completed_build_sha256 TEXT
)
"""
_EXPECTED_TABLE_SQL_V1 = _SCHEMA_SQL_V1.replace("IF NOT EXISTS ", "", 1).strip()
_EXPECTED_TABLE_SQL = _SCHEMA_SQL.replace("IF NOT EXISTS ", "", 1).strip()
_PRIMARY_KEY_INDEX_NAME = "sqlite_autoindex_course_jobs_1"
_UNIQUE_WORKFLOW_INDEX_NAME = "sqlite_autoindex_course_jobs_2"

_STORE_CONSTRUCTION_TOKEN = object()

_SAFE_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_CREATED_AT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_REVISION = 9_223_372_036_854_775_807
_MAX_RETRY = 2_147_483_647

COURSE_JOB_STATUS_VOCABULARY = frozenset(
    {
        "created",
        "sourcing",
        "awaiting_semantic",
        "semantic_running",
        "deterministic_building",
        "needs_attention",
        "completed",
        "failed",
    }
)
_ALLOWED_AI_MODES = frozenset({"gpt", "byok"})
_ALLOWED_QUALITY_MODES = frozenset({"fast", "review"})
_ALLOWED_STAGES = frozenset(
    {
        "source_assessment",
        "priority_approval",
        "lecture_mapping",
        "map_approval",
        "lecture_production",
        "lecture_validation",
        "completed",
    }
)
_ALLOWED_DISPOSITIONS = frozenset({"ready", "awaiting_approval", "blocked", "failed", "completed"})


def _valid_safe_id(value: object) -> bool:
    return (
        type(value) is str
        and value not in (".", "..")
        and _SAFE_ID_RE.fullmatch(value) is not None
    )


def _valid_created_at(value: object) -> bool:
    return type(value) is str and _CREATED_AT_RE.fullmatch(value) is not None


def _valid_sha256_hex(value: object) -> bool:
    return type(value) is str and _SHA256_HEX_RE.fullmatch(value) is not None


def derive_job_status(
    workflow_state: WorkflowState | None,
    *,
    completed_build_id: str | None = None,
    completed_build_sha256: str | None = None,
) -> tuple[str, str | None, str | None, str | None]:
    """Derive coarse Job status projection from authoritative WorkflowState.

    Returns (status, stage, disposition, failure_code).
    """

    if workflow_state is None:
        return ("created", None, None, None)

    stage = workflow_state.stage
    disposition = workflow_state.disposition
    if stage == "completed" and disposition == "completed":
        if completed_build_id is not None and completed_build_sha256 is not None:
            return ("completed", "completed", "completed", None)
        return ("deterministic_building", "completed", "completed", None)
    if disposition == "failed":
        code = None
        if workflow_state.active_issue is not None and hasattr(workflow_state.active_issue, "code"):
            try:
                code = workflow_state.active_issue.code  # type: ignore[attr-defined]
            except Exception:
                code = None
        return ("failed", stage, "failed", code)
    if disposition == "blocked":
        code = None
        if workflow_state.active_issue is not None and hasattr(workflow_state.active_issue, "code"):
            try:
                code = workflow_state.active_issue.code  # type: ignore[attr-defined]
            except Exception:
                code = None
        return ("needs_attention", stage, "blocked", code)
    if stage == "source_assessment" and disposition == "ready":
        return ("sourcing", stage, disposition, None)
    if stage in ("priority_approval", "lecture_mapping", "map_approval"):
        return ("awaiting_semantic", stage, disposition, None)
    if stage in ("lecture_production", "lecture_validation"):
        # Without lease evidence we report awaiting_semantic; future running
        # state will be distinguished by later lease tracking.
        return ("awaiting_semantic", stage, disposition, None)
    # Fallback for ready/awaiting_approval in other stages
    return ("sourcing", stage, disposition, None)


@dataclass(frozen=True, slots=True)
class CourseJobPersistenceDiagnostic:
    """One fixed, content-safe course-job persistence diagnostic."""

    code: str
    classification: Literal["input", "storage", "decode", "identity", "revision", "adapter"]
    message: str

    def __post_init__(self) -> None:
        if self.code not in _DIAGNOSTICS:
            raise ValueError("course-job persistence diagnostic code is not registered")
        classification, message = _DIAGNOSTICS[self.code]
        if self.classification != classification or self.message != message:
            raise ValueError("course-job persistence diagnostic fields are not registered")


@dataclass(frozen=True, slots=True)
class CourseJobPersistenceFailure:
    """A content-safe course-job persistence failure with one fixed diagnostic."""

    status: Literal["persistence_failed"]
    diagnostics: tuple[CourseJobPersistenceDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "persistence_failed":
            raise ValueError("course-job persistence failure status is invalid")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) != 1
            or not _is_valid_diagnostic(self.diagnostics[0])
        ):
            raise ValueError("course-job persistence failure diagnostics are invalid")


@dataclass(frozen=True, slots=True)
class CourseJobRecord:
    """Thin projection of one Course run (Job) onto one WorkflowState."""

    job_id: str
    course_reference: CourseReference
    workflow_id: str
    created_at: str
    created_revision: int
    current_revision: int
    metadata_revision: int
    status: str
    current_stage: str | None
    current_disposition: str | None
    ai_mode: Literal["gpt", "byok"]
    quality_mode: Literal["fast", "review"]
    retry_count: int
    failure_code: str | None
    completed_build_id: str | None = None
    completed_build_sha256: str | None = None

    def __post_init__(self) -> None:
        if not _valid_safe_id(self.job_id):
            raise ValueError("job ID is invalid")
        if type(self.course_reference) is not CourseReference:
            raise ValueError("course reference is invalid")
        try:
            CourseReference(self.course_reference.reference_version, self.course_reference.course_id)
        except Exception:
            raise ValueError("course reference is invalid") from None
        if not _valid_safe_id(self.workflow_id):
            raise ValueError("workflow ID is invalid")
        if not _valid_created_at(self.created_at):
            raise ValueError("job created_at is invalid")
        if type(self.created_revision) is not int or not (0 <= self.created_revision <= _MAX_REVISION):
            raise ValueError("job created_revision is invalid")
        if type(self.current_revision) is not int or not (0 <= self.current_revision <= _MAX_REVISION):
            raise ValueError("job current_revision is invalid")
        if type(self.metadata_revision) is not int or not (0 <= self.metadata_revision <= _MAX_REVISION):
            raise ValueError("job metadata_revision is invalid")
        if type(self.status) is not str or self.status not in COURSE_JOB_STATUS_VOCABULARY:
            raise ValueError("job status is invalid")
        if self.current_stage is not None:
            if type(self.current_stage) is not str or self.current_stage not in _ALLOWED_STAGES:
                raise ValueError("job current_stage is invalid")
        if self.current_disposition is not None:
            if type(self.current_disposition) is not str or self.current_disposition not in _ALLOWED_DISPOSITIONS:
                raise ValueError("job current_disposition is invalid")
        if type(self.ai_mode) is not str or self.ai_mode not in _ALLOWED_AI_MODES:
            raise ValueError("job ai_mode is invalid")
        if type(self.quality_mode) is not str or self.quality_mode not in _ALLOWED_QUALITY_MODES:
            raise ValueError("job quality_mode is invalid")
        if type(self.retry_count) is not int or not (0 <= self.retry_count <= _MAX_RETRY):
            raise ValueError("job retry_count is invalid")
        if self.failure_code is not None:
            if type(self.failure_code) is not str or not self.failure_code:
                raise ValueError("job failure_code is invalid")
        if (self.completed_build_id is None) != (self.completed_build_sha256 is None):
            raise ValueError("completed_build pointer pair invariant violated: both must be None or both non-None")
        if self.completed_build_id is not None and not _valid_safe_id(self.completed_build_id):
            raise ValueError("completed_build_id is invalid")
        if self.completed_build_sha256 is not None and not _valid_sha256_hex(self.completed_build_sha256):
            raise ValueError("completed_build_sha256 is invalid")
        # Consistency: completed must have matching stage/disposition and build pointers
        if self.status == "completed":
            if self.current_stage != "completed" or self.current_disposition != "completed":
                raise ValueError("job completed state is invalid")
            if self.completed_build_id is None or self.completed_build_sha256 is None:
                raise ValueError("completed job must have completed_build_id and completed_build_sha256")
        if self.status == "failed":
            if self.current_disposition != "failed" or self.failure_code is None:
                raise ValueError("job failed state is invalid")
        if self.status == "needs_attention":
            if self.current_disposition != "blocked" or self.failure_code is None:
                raise ValueError("job needs_attention state is invalid")
        if self.status == "created":
            if self.current_stage is not None or self.current_disposition is not None:
                # created means no workflow yet; stage/disposition must be None
                raise ValueError("job created state is invalid")

    def __repr__(self) -> str:
        return (
            f"CourseJobRecord(job_id={self.job_id!r}, "
            f"workflow_id={self.workflow_id!r}, status={self.status!r}, "
            f"current_revision={self.current_revision!r}, "
            f"completed_build_id={self.completed_build_id!r})"
        )


CourseJobPersistenceResult: TypeAlias = CourseJobRecord | CourseJobPersistenceFailure


class LocalCourseJobStore:
    """Local SQLite store for CourseJobRecord with WorkflowState projection support."""

    __slots__ = ("_connection",)

    def __init__(self, connection: sqlite3.Connection, token: object) -> None:
        if token is not _STORE_CONSTRUCTION_TOKEN:
            raise TypeError("course-job stores must be opened by the adapter")
        self._connection = connection

    def save(self, record: CourseJobRecord) -> CourseJobPersistenceResult:
        """Persist one Job record idempotently or as conditional metadata advancement."""

        if type(record) is not CourseJobRecord:
            raise TypeError("record must be exactly CourseJobRecord")
        try:
            CourseJobRecord(
                job_id=record.job_id,
                course_reference=record.course_reference,
                workflow_id=record.workflow_id,
                created_at=record.created_at,
                created_revision=record.created_revision,
                current_revision=record.current_revision,
                metadata_revision=record.metadata_revision,
                status=record.status,
                current_stage=record.current_stage,
                current_disposition=record.current_disposition,
                ai_mode=record.ai_mode,
                quality_mode=record.quality_mode,
                retry_count=record.retry_count,
                failure_code=record.failure_code,
                completed_build_id=record.completed_build_id,
                completed_build_sha256=record.completed_build_sha256,
            )
        except Exception:
            return _failure("invalid_persistence_input")

        try:
            if not _storage_is_recognized(self._connection):
                return _failure("unsupported_storage_schema")
        except Exception:
            return _failure("persistence_exception")

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT job_id, course_id, course_reference_version, workflow_id, created_at, "
                "created_revision, current_revision, metadata_revision, status, current_stage, "
                "current_disposition, ai_mode, quality_mode, retry_count, failure_code, "
                "storage_schema_version, completed_build_id, completed_build_sha256 FROM course_jobs WHERE job_id = ?",
                (record.job_id,),
            ).fetchone()

            # Check workflow_id uniqueness for new job_id
            workflow_row = self._connection.execute(
                "SELECT job_id FROM course_jobs WHERE workflow_id = ? AND job_id != ?",
                (record.workflow_id, record.job_id),
            ).fetchone()
            if workflow_row is not None:
                self._connection.rollback()
                return _failure("workflow_identity_conflict")

            if row is None:
                if record.metadata_revision != 0:
                    self._connection.rollback()
                    return _failure("stale_revision")
                self._connection.execute(
                    "INSERT INTO course_jobs (job_id, course_id, course_reference_version, workflow_id, "
                    "created_at, created_revision, current_revision, metadata_revision, status, "
                    "current_stage, current_disposition, ai_mode, quality_mode, retry_count, "
                    "failure_code, storage_schema_version, completed_build_id, completed_build_sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _record_to_tuple(record),
                )
                self._connection.commit()
                return record

            previous = _decode_row(row, expected_job_id=record.job_id)
            if type(previous) is CourseJobPersistenceFailure:
                self._connection.rollback()
                return previous

            # Immutable identity check
            if (
                previous.course_reference != record.course_reference
                or previous.workflow_id != record.workflow_id
                or previous.created_at != record.created_at
                or previous.created_revision != record.created_revision
                or previous.ai_mode != record.ai_mode
                or previous.quality_mode != record.quality_mode
            ):
                self._connection.rollback()
                return _failure("immutable_identity_conflict")

            if previous == record:
                self._connection.rollback()
                # Idempotent confirm
                with self._connection:
                    cur = self._connection.execute(
                        "UPDATE course_jobs SET metadata_revision = metadata_revision "
                        "WHERE job_id = ? AND metadata_revision = ? AND status = ? "
                        "AND current_revision = ? AND storage_schema_version = ?",
                        (
                            record.job_id,
                            record.metadata_revision,
                            record.status,
                            record.current_revision,
                            COURSE_JOB_PERSISTENCE_SCHEMA_VERSION,
                        ),
                    )
                    if cur.rowcount == 1:
                        return record
                return _failure("stale_revision")

            if record.metadata_revision == previous.metadata_revision:
                self._connection.rollback()
                return _failure("revision_conflict")
            if record.metadata_revision != previous.metadata_revision + 1:
                self._connection.rollback()
                return _failure("stale_revision")

            # Conditional update on metadata_revision
            with self._connection:
                cur = self._connection.execute(
                    "UPDATE course_jobs SET course_id = ?, course_reference_version = ?, workflow_id = ?, "
                    "created_at = ?, created_revision = ?, current_revision = ?, metadata_revision = ?, "
                    "status = ?, current_stage = ?, current_disposition = ?, ai_mode = ?, quality_mode = ?, "
                    "retry_count = ?, failure_code = ?, storage_schema_version = ?, completed_build_id = ?, "
                    "completed_build_sha256 = ? "
                    "WHERE job_id = ? AND metadata_revision = ?",
                    (
                        record.course_reference.course_id,
                        record.course_reference.reference_version,
                        record.workflow_id,
                        record.created_at,
                        record.created_revision,
                        record.current_revision,
                        record.metadata_revision,
                        record.status,
                        record.current_stage,
                        record.current_disposition,
                        record.ai_mode,
                        record.quality_mode,
                        record.retry_count,
                        record.failure_code,
                        COURSE_JOB_PERSISTENCE_SCHEMA_VERSION,
                        record.completed_build_id,
                        record.completed_build_sha256,
                        record.job_id,
                        previous.metadata_revision,
                    ),
                )
                if cur.rowcount != 1:
                    return _failure("stale_revision")
            return record

        except sqlite3.Error:
            _rollback_quietly(self._connection)
            return _failure("persistence_unavailable")
        except Exception:
            _rollback_quietly(self._connection)
            return _failure("persistence_exception")

    def load(self, job_id: str) -> CourseJobPersistenceResult:
        """Load one Job by exact job_id."""

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
            row = self._connection.execute(
                "SELECT job_id, course_id, course_reference_version, workflow_id, created_at, "
                "created_revision, current_revision, metadata_revision, status, current_stage, "
                "current_disposition, ai_mode, quality_mode, retry_count, failure_code, "
                "storage_schema_version, completed_build_id, completed_build_sha256 FROM course_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        except sqlite3.Error:
            return _failure("persistence_unavailable")
        except Exception:
            return _failure("persistence_exception")
        if row is None:
            return _failure("job_not_found")
        return _decode_row(row, expected_job_id=job_id)

    def load_by_workflow_id(self, workflow_id: str) -> CourseJobPersistenceResult:
        """Load one Job by its unique workflow_id."""

        if type(workflow_id) is not str:
            raise TypeError("workflow_id must be exactly str")
        if not _valid_safe_id(workflow_id):
            return _failure("invalid_persistence_input")
        try:
            if not _storage_is_recognized(self._connection):
                return _failure("unsupported_storage_schema")
        except Exception:
            return _failure("persistence_exception")
        try:
            row = self._connection.execute(
                "SELECT job_id, course_id, course_reference_version, workflow_id, created_at, "
                "created_revision, current_revision, metadata_revision, status, current_stage, "
                "current_disposition, ai_mode, quality_mode, retry_count, failure_code, "
                "storage_schema_version, completed_build_id, completed_build_sha256 FROM course_jobs WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
        except sqlite3.Error:
            return _failure("persistence_unavailable")
        except Exception:
            return _failure("persistence_exception")
        if row is None:
            return _failure("job_not_found")
        if type(row) is tuple and len(row) >= 1 and type(row[0]) is str:
            return _decode_row(row, expected_job_id=row[0])
        return _failure("stored_job_invalid")

    load_by_workflow = load_by_workflow_id

    def list_jobs_for_course(self, course_id: str) -> tuple[CourseJobRecord, ...] | CourseJobPersistenceFailure:
        """Return all Jobs for a given course_id, ordered by job_id."""

        if type(course_id) is not str:
            raise TypeError("course_id must be exactly str")
        if not _valid_safe_id(course_id):
            return _failure("invalid_persistence_input")
        try:
            if not _storage_is_recognized(self._connection):
                return _failure("unsupported_storage_schema")
        except Exception:
            return _failure("persistence_exception")
        try:
            rows = self._connection.execute(
                "SELECT job_id, course_id, course_reference_version, workflow_id, created_at, "
                "created_revision, current_revision, metadata_revision, status, current_stage, "
                "current_disposition, ai_mode, quality_mode, retry_count, failure_code, "
                "storage_schema_version, completed_build_id, completed_build_sha256 FROM course_jobs WHERE course_id = ? ORDER BY job_id ASC",
                (course_id,),
            ).fetchall()
        except sqlite3.Error:
            return _failure("persistence_unavailable")
        except Exception:
            return _failure("persistence_exception")
        result: list[CourseJobRecord] = []
        for row in rows:
            if type(row) is not tuple or len(row) != 18:
                return _failure("stored_job_invalid")
            job_id = row[0]
            if type(job_id) is not str:
                return _failure("stored_job_invalid")
            decoded = _decode_row(row, expected_job_id=job_id)
            if type(decoded) is CourseJobPersistenceFailure:
                return decoded
            result.append(decoded)
        return tuple(result)

    def close(self) -> None:
        self._connection.close()

    def reconcile_from_workflow(
        self, job_id: str, workflow_state: WorkflowState | None
    ) -> CourseJobPersistenceResult:
        """Recompute projection from WorkflowState and persist if stale (idempotent)."""

        if type(job_id) is not str:
            raise TypeError("job_id must be exactly str")
        existing = self.load(job_id)
        if type(existing) is CourseJobPersistenceFailure:
            return existing
        job: CourseJobRecord = existing  # type: ignore[assignment]
        status, stage, disposition, failure_code = derive_job_status(
            workflow_state,
            completed_build_id=job.completed_build_id,
            completed_build_sha256=job.completed_build_sha256,
        )
        current_revision = workflow_state.revision if workflow_state is not None else job.created_revision
        retry_count = job.retry_count
        if (
            job.status == status
            and job.current_stage == stage
            and job.current_disposition == disposition
            and job.current_revision == current_revision
            and job.failure_code == failure_code
        ):
            return job

        updated = CourseJobRecord(
            job_id=job.job_id,
            course_reference=job.course_reference,
            workflow_id=job.workflow_id,
            created_at=job.created_at,
            created_revision=job.created_revision,
            current_revision=current_revision,
            metadata_revision=job.metadata_revision + 1,
            status=status,
            current_stage=stage,
            current_disposition=disposition,
            ai_mode=job.ai_mode,
            quality_mode=job.quality_mode,
            retry_count=retry_count,
            failure_code=failure_code,
            completed_build_id=job.completed_build_id,
            completed_build_sha256=job.completed_build_sha256,
        )
        return self.save(updated)


def open_course_job_store(database_path: str | Path) -> LocalCourseJobStore | CourseJobPersistenceFailure:
    """Open or initialize one exact-schema SQLite store for CourseJob."""

    if type(database_path) is not str and not isinstance(database_path, Path):
        raise TypeError("database_path must be exactly str or Path")
    try:
        connection = sqlite3.connect(database_path)
        has_table = connection.execute(
            "SELECT 1 FROM main.sqlite_schema WHERE type = 'table' AND name = 'course_jobs'"
        ).fetchone() is not None

        if not has_table:
            with connection:
                connection.execute(_SCHEMA_SQL)
        else:
            if _is_v1_storage(connection):
                # Migration is gated by a read-only preflight that has already
                # proven this database is exactly accepted v1. Anything else --
                # including v1-like drift -- reaches the v2 recognizer below
                # with zero mutation applied and fails closed.
                #
                # One explicit BEGIN IMMEDIATE: Python's sqlite3 does not open a
                # transaction for DDL on its own, so `with connection` alone
                # would leave the two ALTERs in autocommit and a crash between
                # them would half-migrate the store. Taking the write lock up
                # front makes the whole v1 -> v2 migration atomic.
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("ALTER TABLE course_jobs ADD COLUMN completed_build_id TEXT")
                    connection.execute("ALTER TABLE course_jobs ADD COLUMN completed_build_sha256 TEXT")
                    connection.execute(
                        "UPDATE course_jobs SET status = 'deterministic_building' WHERE status = 'completed' AND completed_build_id IS NULL"
                    )
                    connection.execute(
                        "UPDATE course_jobs SET storage_schema_version = ?",
                        (COURSE_JOB_PERSISTENCE_SCHEMA_VERSION,),
                    )
                    connection.commit()
                except Exception:
                    _rollback_quietly(connection)
                    connection.close()
                    return _failure("unsupported_storage_schema")

        if not _storage_is_recognized(connection):
            connection.close()
            return _failure("unsupported_storage_schema")
        return LocalCourseJobStore(connection, _STORE_CONSTRUCTION_TOKEN)
    except sqlite3.Error:
        return _failure("persistence_unavailable")
    except Exception:
        return _failure("persistence_exception")


def _record_to_tuple(record: CourseJobRecord) -> tuple:
    return (
        record.job_id,
        record.course_reference.course_id,
        record.course_reference.reference_version,
        record.workflow_id,
        record.created_at,
        record.created_revision,
        record.current_revision,
        record.metadata_revision,
        record.status,
        record.current_stage,
        record.current_disposition,
        record.ai_mode,
        record.quality_mode,
        record.retry_count,
        record.failure_code,
        COURSE_JOB_PERSISTENCE_SCHEMA_VERSION,
        record.completed_build_id,
        record.completed_build_sha256,
    )


def _decode_row(row: tuple[object, ...], *, expected_job_id: str) -> CourseJobPersistenceResult:
    if type(row) is not tuple or len(row) != 18:
        return _failure("stored_job_invalid")
    (
        job_id,
        course_id,
        course_reference_version,
        workflow_id,
        created_at,
        created_revision,
        current_revision,
        metadata_revision,
        status,
        current_stage,
        current_disposition,
        ai_mode,
        quality_mode,
        retry_count,
        failure_code,
        schema_version,
        completed_build_id,
        completed_build_sha256,
    ) = row
    if type(schema_version) is not str or schema_version != COURSE_JOB_PERSISTENCE_SCHEMA_VERSION:
        return _failure("unsupported_storage_schema")
    if (
        type(job_id) is not str
        or job_id != expected_job_id
        or type(course_id) is not str
        or type(course_reference_version) is not str
        or type(workflow_id) is not str
        or type(created_at) is not str
        or type(created_revision) is not int
        or type(current_revision) is not int
        or type(metadata_revision) is not int
        or type(status) is not str
        or (current_stage is not None and type(current_stage) is not str)
        or (current_disposition is not None and type(current_disposition) is not str)
        or type(ai_mode) is not str
        or type(quality_mode) is not str
        or type(retry_count) is not int
        or (failure_code is not None and type(failure_code) is not str)
        or (completed_build_id is not None and type(completed_build_id) is not str)
        or (completed_build_sha256 is not None and type(completed_build_sha256) is not str)
    ):
        return _failure("stored_job_invalid")
    try:
        course_ref = CourseReference(course_reference_version, course_id)  # type: ignore[arg-type]
        record = CourseJobRecord(
            job_id=job_id,
            course_reference=course_ref,
            workflow_id=workflow_id,
            created_at=created_at,
            created_revision=created_revision,
            current_revision=current_revision,
            metadata_revision=metadata_revision,
            status=status,  # type: ignore[arg-type]
            current_stage=current_stage,
            current_disposition=current_disposition,
            ai_mode=ai_mode,  # type: ignore[arg-type]
            quality_mode=quality_mode,  # type: ignore[arg-type]
            retry_count=retry_count,
            failure_code=failure_code,
            completed_build_id=completed_build_id,
            completed_build_sha256=completed_build_sha256,
        )
        return record
    except (TypeError, ValueError):
        return _failure("stored_job_invalid")
    except Exception:
        return _failure("persistence_exception")


_EXPECTED_COLUMNS_V1 = (
    (0, "job_id", "TEXT", 1, None, 1, 0),
    (1, "course_id", "TEXT", 1, None, 0, 0),
    (2, "course_reference_version", "TEXT", 1, None, 0, 0),
    (3, "workflow_id", "TEXT", 1, None, 0, 0),
    (4, "created_at", "TEXT", 1, None, 0, 0),
    (5, "created_revision", "INTEGER", 1, None, 0, 0),
    (6, "current_revision", "INTEGER", 1, None, 0, 0),
    (7, "metadata_revision", "INTEGER", 1, None, 0, 0),
    (8, "status", "TEXT", 1, None, 0, 0),
    (9, "current_stage", "TEXT", 0, None, 0, 0),
    (10, "current_disposition", "TEXT", 0, None, 0, 0),
    (11, "ai_mode", "TEXT", 1, None, 0, 0),
    (12, "quality_mode", "TEXT", 1, None, 0, 0),
    (13, "retry_count", "INTEGER", 1, None, 0, 0),
    (14, "failure_code", "TEXT", 0, None, 0, 0),
    (15, "storage_schema_version", "TEXT", 1, None, 0, 0),
)


def _v1_schema_is_exact(connection: sqlite3.Connection) -> bool:
    """Prove the database is exactly accepted ``local-course-job-sqlite/v1``.

    Migration eligibility is decided at the same strength as the accepted T048
    exact-schema validator: exact CREATE TABLE SQL, exact column shape, the
    exact primary-key and UNIQUE(workflow_id) indexes and no others, no
    triggers, no foreign keys, and no temp shadow objects. Anything that merely
    looks like v1 is unknown drift and must never be mutated.
    """

    try:
        table_rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM main.sqlite_schema WHERE name = ?",
            ("course_jobs",),
        ).fetchall()
        columns = connection.execute("PRAGMA main.table_xinfo(course_jobs)").fetchall()
        foreign_keys = connection.execute("PRAGMA main.foreign_key_list(course_jobs)").fetchall()
        indexes = connection.execute("PRAGMA main.index_list(course_jobs)").fetchall()
        primary_key_columns = connection.execute(
            f"PRAGMA main.index_xinfo({_PRIMARY_KEY_INDEX_NAME})"
        ).fetchall()
        unique_workflow_columns = connection.execute(
            f"PRAGMA main.index_xinfo({_UNIQUE_WORKFLOW_INDEX_NAME})"
        ).fetchall()
        triggers = connection.execute(
            "SELECT type, name, tbl_name, sql FROM main.sqlite_schema WHERE type = 'trigger' AND tbl_name = ?",
            ("course_jobs",),
        ).fetchall()
        temporary_objects = connection.execute(
            "SELECT type, name, tbl_name, sql FROM temp.sqlite_schema WHERE name = ? OR tbl_name = ?",
            ("course_jobs", "course_jobs"),
        ).fetchall()
    except sqlite3.Error:
        return False

    expected_primary_key_columns = (
        (0, 0, "job_id", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0),
    )
    expected_unique_workflow_columns = (
        (0, 3, "workflow_id", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0),
    )
    expected_indexes_set = {
        (0, _UNIQUE_WORKFLOW_INDEX_NAME, 1, "u", 0),
        (1, _PRIMARY_KEY_INDEX_NAME, 1, "pk", 0),
    }
    indexes_set = set(indexes) if isinstance(indexes, list) else set()
    return (
        type(table_rows) is list
        and len(table_rows) == 1
        and type(table_rows[0]) is tuple
        and len(table_rows[0]) == 4
        and table_rows[0][:3] == ("table", "course_jobs", "course_jobs")
        and _schema_sql_key(table_rows[0][3]) == _schema_sql_key(_EXPECTED_TABLE_SQL_V1)
        and type(columns) is list
        and tuple(columns) == _EXPECTED_COLUMNS_V1
        and type(foreign_keys) is list
        and foreign_keys == []
        and type(indexes) is list
        and indexes_set == expected_indexes_set
        and type(primary_key_columns) is list
        and tuple(primary_key_columns) == expected_primary_key_columns
        and type(unique_workflow_columns) is list
        and tuple(unique_workflow_columns) == expected_unique_workflow_columns
        and type(triggers) is list
        and triggers == []
        and type(temporary_objects) is list
        and temporary_objects == []
    )


def _is_v1_storage(connection: sqlite3.Connection) -> bool:
    """Report migration eligibility: exactly v1 schema AND exactly v1 rows."""

    if not _v1_schema_is_exact(connection):
        return False
    try:
        row = connection.execute(
            "SELECT 1 FROM course_jobs WHERE typeof(storage_schema_version) != 'text' OR storage_schema_version != ? LIMIT 1",
            (COURSE_JOB_PERSISTENCE_SCHEMA_VERSION_V1,),
        ).fetchone()
        return row is None
    except sqlite3.Error:
        return False


def _schema_is_recognized(connection: sqlite3.Connection) -> bool:
    try:
        table_rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM main.sqlite_schema WHERE name = ?",
            ("course_jobs",),
        ).fetchall()
        columns = connection.execute("PRAGMA main.table_xinfo(course_jobs)").fetchall()
        foreign_keys = connection.execute("PRAGMA main.foreign_key_list(course_jobs)").fetchall()
        indexes = connection.execute("PRAGMA main.index_list(course_jobs)").fetchall()
        primary_key_columns = connection.execute(
            f"PRAGMA main.index_xinfo({_PRIMARY_KEY_INDEX_NAME})"
        ).fetchall()
        unique_workflow_columns = connection.execute(
            f"PRAGMA main.index_xinfo({_UNIQUE_WORKFLOW_INDEX_NAME})"
        ).fetchall()
        triggers = connection.execute(
            "SELECT type, name, tbl_name, sql FROM main.sqlite_schema WHERE type = 'trigger' AND tbl_name = ?",
            ("course_jobs",),
        ).fetchall()
        temporary_objects = connection.execute(
            "SELECT type, name, tbl_name, sql FROM temp.sqlite_schema WHERE name = ? OR tbl_name = ?",
            ("course_jobs", "course_jobs"),
        ).fetchall()
    except sqlite3.Error:
        return False

    expected_columns = (
        (0, "job_id", "TEXT", 1, None, 1, 0),
        (1, "course_id", "TEXT", 1, None, 0, 0),
        (2, "course_reference_version", "TEXT", 1, None, 0, 0),
        (3, "workflow_id", "TEXT", 1, None, 0, 0),
        (4, "created_at", "TEXT", 1, None, 0, 0),
        (5, "created_revision", "INTEGER", 1, None, 0, 0),
        (6, "current_revision", "INTEGER", 1, None, 0, 0),
        (7, "metadata_revision", "INTEGER", 1, None, 0, 0),
        (8, "status", "TEXT", 1, None, 0, 0),
        (9, "current_stage", "TEXT", 0, None, 0, 0),
        (10, "current_disposition", "TEXT", 0, None, 0, 0),
        (11, "ai_mode", "TEXT", 1, None, 0, 0),
        (12, "quality_mode", "TEXT", 1, None, 0, 0),
        (13, "retry_count", "INTEGER", 1, None, 0, 0),
        (14, "failure_code", "TEXT", 0, None, 0, 0),
        (15, "storage_schema_version", "TEXT", 1, None, 0, 0),
        (16, "completed_build_id", "TEXT", 0, None, 0, 0),
        (17, "completed_build_sha256", "TEXT", 0, None, 0, 0),
    )
    expected_primary_key_columns = (
        (0, 0, "job_id", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0),
    )
    expected_unique_workflow_columns = (
        (0, 3, "workflow_id", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0),
    )
    expected_indexes_set = {
        (0, _UNIQUE_WORKFLOW_INDEX_NAME, 1, "u", 0),
        (1, _PRIMARY_KEY_INDEX_NAME, 1, "pk", 0),
    }
    indexes_set = set(indexes) if isinstance(indexes, list) else set()
    return (
        type(table_rows) is list
        and len(table_rows) == 1
        and type(table_rows[0]) is tuple
        and len(table_rows[0]) == 4
        and table_rows[0][:3] == ("table", "course_jobs", "course_jobs")
        and _schema_sql_key(table_rows[0][3]) == _schema_sql_key(_EXPECTED_TABLE_SQL)
        and type(columns) is list
        and tuple(columns) == expected_columns
        and type(foreign_keys) is list
        and foreign_keys == []
        and type(indexes) is list
        and indexes_set == expected_indexes_set
        and type(primary_key_columns) is list
        and tuple(primary_key_columns) == expected_primary_key_columns
        and type(unique_workflow_columns) is list
        and tuple(unique_workflow_columns) == expected_unique_workflow_columns
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
            "SELECT 1 FROM course_jobs WHERE typeof(storage_schema_version) != 'text' OR storage_schema_version != ? LIMIT 1",
            (COURSE_JOB_PERSISTENCE_SCHEMA_VERSION,),
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
    if type(value) is not CourseJobPersistenceDiagnostic:
        return False
    try:
        CourseJobPersistenceDiagnostic(value.code, value.classification, value.message)
    except (AttributeError, TypeError, ValueError):
        return False
    return True


def _failure(code: str) -> CourseJobPersistenceFailure:
    classification, message = _DIAGNOSTICS[code]
    return CourseJobPersistenceFailure(
        status="persistence_failed",
        diagnostics=(CourseJobPersistenceDiagnostic(code, classification, message),),
    )
