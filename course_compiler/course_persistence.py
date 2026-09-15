"""Local SQLite persistence for CourseRecord (T048).

Durable user-facing container: immutable identity + mutable metadata (optimistic
revision) + reference-only source set + workflow association + optional job pointer.
No source/artifact BLOB duplication.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Literal, TypeAlias

from .course import COURSE_REFERENCE_VERSION, CourseReference
from .workflow import SourceEvidenceReference

COURSE_PERSISTENCE_SCHEMA_VERSION = "local-course-sqlite/v2"
COURSE_RECORD_VERSION = "course-record/v1"

__all__ = [
    "COURSE_PERSISTENCE_SCHEMA_VERSION",
    "COURSE_RECORD_VERSION",
    "CourseRecord",
    "CoursePersistenceDiagnostic",
    "CoursePersistenceFailure",
    "CoursePersistenceResult",
    "LocalCourseStore",
    "open_course_store",
]

_DIAGNOSTICS = {
    "invalid_persistence_input": ("input", "The course persistence input is invalid."),
    "persistence_unavailable": ("storage", "The local course store is unavailable."),
    "course_not_found": ("storage", "No stored course was found."),
    "stored_course_invalid": ("decode", "The stored course is invalid."),
    "unsupported_storage_schema": ("decode", "The stored course schema is unsupported."),
    "stale_revision": ("revision", "The course revision is stale."),
    "revision_conflict": ("revision", "The course revision conflicts."),
    "immutable_identity_conflict": ("identity", "The course immutable identity conflicts."),
    "course_identity_mismatch": ("identity", "The stored course identity does not match."),
    "persistence_exception": ("adapter", "The local course adapter failed."),
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS courses (
    course_id TEXT PRIMARY KEY NOT NULL,
    reference_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    title TEXT NOT NULL,
    ai_mode TEXT NOT NULL,
    quality_mode TEXT NOT NULL,
    owner_scope TEXT NOT NULL,
    metadata_revision INTEGER NOT NULL,
    source_refs_json TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    current_job_id TEXT,
    storage_schema_version TEXT NOT NULL,
    course_guidance TEXT NOT NULL DEFAULT ''
)
"""
_EXPECTED_TABLE_SQL = _SCHEMA_SQL.replace("IF NOT EXISTS ", "", 1).strip()
_LEGACY_TABLE_SQL = _EXPECTED_TABLE_SQL.replace(",\n    course_guidance TEXT NOT NULL DEFAULT ''", "")
_PRIMARY_KEY_INDEX_NAME = "sqlite_autoindex_courses_1"

_STORE_CONSTRUCTION_TOKEN = object()

_COURSE_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_WORKFLOW_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_SAFE_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_CREATED_AT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_TITLE_RE = re.compile(r"[^\x00-\x1f\x7f]*\Z")
_MAX_REVISION = 9_223_372_036_854_775_807

_ALLOWED_AI_MODES = frozenset({"gpt", "byok"})
_ALLOWED_QUALITY_MODES = frozenset({"fast", "review"})
_ALLOWED_OWNER_SCOPES = frozenset({"single_user_local"})


def _valid_safe_id(value: object) -> bool:
    return (
        type(value) is str
        and value not in (".", "..")
        and _SAFE_ID_RE.fullmatch(value) is not None
    )


def _valid_course_id(value: object) -> bool:
    return (
        type(value) is str
        and value not in (".", "..")
        and _COURSE_ID_RE.fullmatch(value) is not None
    )


def _valid_title(value: object) -> bool:
    if type(value) is not str:
        return False
    stripped = value.strip()
    if not (1 <= len(stripped) <= 200):
        return False
    if stripped != value:
        # Title is stored stripped; reject surrounding whitespace to keep deterministic
        return False
    if _TITLE_RE.fullmatch(value) is None:
        return False
    # Reject control chars
    for ch in value:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            return False
    return True


def valid_course_guidance(value: object) -> bool:
    return (type(value) is str and len(value.encode("utf-8")) <= 32768
            and not any(ord(c) < 32 and c not in "\n\r\t" for c in value))


def _valid_created_at(value: object) -> bool:
    return type(value) is str and _CREATED_AT_RE.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class CoursePersistenceDiagnostic:
    """One fixed, content-safe course-persistence diagnostic."""

    code: str
    classification: Literal["input", "storage", "decode", "identity", "revision", "adapter"]
    message: str

    def __post_init__(self) -> None:
        if self.code not in _DIAGNOSTICS:
            raise ValueError("course persistence diagnostic code is not registered")
        classification, message = _DIAGNOSTICS[self.code]
        if self.classification != classification or self.message != message:
            raise ValueError("course persistence diagnostic fields are not registered")


@dataclass(frozen=True, slots=True)
class CoursePersistenceFailure:
    """A content-safe course-persistence failure with one fixed diagnostic."""

    status: Literal["persistence_failed"]
    diagnostics: tuple[CoursePersistenceDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "persistence_failed":
            raise ValueError("course persistence failure status is invalid")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) != 1
            or not _is_valid_diagnostic(self.diagnostics[0])
        ):
            raise ValueError("course persistence failure diagnostics are invalid")


@dataclass(frozen=True, slots=True)
class CourseRecord:
    """Immutable + mutable Course container (representation-safe)."""

    reference_version: Literal["course-reference/v1"]
    course_id: str
    created_at: str
    title: str
    ai_mode: Literal["gpt", "byok"]
    quality_mode: Literal["fast", "review"]
    owner_scope: Literal["single_user_local"]
    metadata_revision: int
    source_refs: tuple[SourceEvidenceReference, ...]
    workflow_id: str
    current_job_id: str | None
    course_guidance: str = ""

    def __post_init__(self) -> None:
        if type(self.reference_version) is not str or self.reference_version != COURSE_REFERENCE_VERSION:
            raise ValueError("course reference version is unsupported")
        if not _valid_course_id(self.course_id):
            raise ValueError("course ID is invalid")
        if not _valid_created_at(self.created_at):
            raise ValueError("course created_at is invalid")
        if not _valid_title(self.title):
            raise ValueError("course title is invalid")
        if type(self.ai_mode) is not str or self.ai_mode not in _ALLOWED_AI_MODES:
            raise ValueError("course ai_mode is invalid")
        if type(self.quality_mode) is not str or self.quality_mode not in _ALLOWED_QUALITY_MODES:
            raise ValueError("course quality_mode is invalid")
        if type(self.owner_scope) is not str or self.owner_scope not in _ALLOWED_OWNER_SCOPES:
            raise ValueError("course owner_scope is invalid")
        if type(self.metadata_revision) is not int or not (0 <= self.metadata_revision <= _MAX_REVISION):
            raise ValueError("course metadata revision is invalid")
        # source_refs validation: tuple of exact SourceEvidenceReference, canonical, unique
        if type(self.source_refs) is not tuple:
            raise ValueError("course source_refs is invalid")
        seen_ids: set[str] = set()
        seen_digests: set[str] = set()
        for ref in self.source_refs:
            if type(ref) is not SourceEvidenceReference:
                raise ValueError("course source_refs is invalid")
            # Revalidate exact reference
            try:
                SourceEvidenceReference(ref.reference_version, ref.source_id, ref.content_sha256)
            except Exception:
                raise ValueError("course source_refs is invalid") from None
            if ref.source_id in seen_ids or ref.content_sha256 in seen_digests:
                raise ValueError("course source_refs duplicate is invalid")
            seen_ids.add(ref.source_id)
            seen_digests.add(ref.content_sha256)
        # Canonical ordering: sorted by (source_id, content_sha256)
        canonical = tuple(sorted(self.source_refs, key=lambda r: (r.source_id, r.content_sha256)))
        if self.source_refs != canonical:
            raise ValueError("course source_refs is not canonical")
        if not _valid_safe_id(self.workflow_id):
            raise ValueError("course workflow_id is invalid")
        if self.current_job_id is not None:
            if not _valid_safe_id(self.current_job_id):
                raise ValueError("course current_job_id is invalid")
        if not valid_course_guidance(self.course_guidance):
            raise ValueError("course guidance is invalid")
        # Validate via CourseReference construction
        CourseReference(self.reference_version, self.course_id)

    def __repr__(self) -> str:
        return (
            f"CourseRecord(course_id={self.course_id!r}, "
            f"workflow_id={self.workflow_id!r}, metadata_revision={self.metadata_revision!r})"
        )


CoursePersistenceResult: TypeAlias = CourseRecord | CoursePersistenceFailure


class LocalCourseStore:
    """Local SQLite store for CourseRecord with exact schema validation."""

    __slots__ = ("_connection",)

    def __init__(self, connection: sqlite3.Connection, token: object) -> None:
        if token is not _STORE_CONSTRUCTION_TOKEN:
            raise TypeError("course stores must be opened by the adapter")
        self._connection = connection

    def save(self, record: CourseRecord) -> CoursePersistenceResult:
        """Persist one CourseRecord idempotently or as conditional revision advancement."""

        if type(record) is not CourseRecord:
            raise TypeError("record must be exactly CourseRecord")
        try:
            # Revalidate via constructor
            CourseRecord(
                record.reference_version,
                record.course_id,
                record.created_at,
                record.title,
                record.ai_mode,  # type: ignore[arg-type]
                record.quality_mode,  # type: ignore[arg-type]
                record.owner_scope,  # type: ignore[arg-type]
                record.metadata_revision,
                record.source_refs,
                record.workflow_id,
                record.current_job_id,
                record.course_guidance,
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
                "SELECT course_id, reference_version, created_at, title, ai_mode, quality_mode, "
                "owner_scope, metadata_revision, source_refs_json, workflow_id, current_job_id, "
                "storage_schema_version, course_guidance FROM courses WHERE course_id = ?",
                (record.course_id,),
            ).fetchone()
            if row is None:
                if record.metadata_revision != 0:
                    self._connection.rollback()
                    return _failure("stale_revision")
                payload = _serialize_source_refs(record.source_refs)
                self._connection.execute(
                    "INSERT INTO courses (course_id, reference_version, created_at, title, ai_mode, "
                    "quality_mode, owner_scope, metadata_revision, source_refs_json, workflow_id, "
                    "current_job_id, storage_schema_version, course_guidance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.course_id,
                        record.reference_version,
                        record.created_at,
                        record.title,
                        record.ai_mode,
                        record.quality_mode,
                        record.owner_scope,
                        record.metadata_revision,
                        payload,
                        record.workflow_id,
                        record.current_job_id,
                        COURSE_PERSISTENCE_SCHEMA_VERSION,
                        record.course_guidance,
                    ),
                )
                self._connection.commit()
                return record

            previous = _decode_row(row, expected_course_id=record.course_id)
            if type(previous) is CoursePersistenceFailure:
                self._connection.rollback()
                return previous

            # Immutable identity must match exactly for any same course_id
            if (
                previous.reference_version != record.reference_version
                or previous.created_at != record.created_at
                or previous.workflow_id != record.workflow_id
            ):
                self._connection.rollback()
                return _failure("immutable_identity_conflict")

            # Exact repeat idempotent
            if previous == record:
                # Confirm still exists with same payload via conditional update
                self._connection.rollback()
                # Use same idempotent confirmation path as workflow persistence
                # but course has deterministic json; do lightweight check
                serialized = _serialize_source_refs(record.source_refs)
                with self._connection:
                    cur = self._connection.execute(
                        "UPDATE courses SET metadata_revision = metadata_revision "
                        "WHERE course_id = ? AND metadata_revision = ? "
                        "AND reference_version = ? AND created_at = ? AND title = ? "
                        "AND ai_mode = ? AND quality_mode = ? AND owner_scope = ? "
                        "AND source_refs_json = ? AND workflow_id = ? "
                        "AND ((current_job_id IS NULL AND ? IS NULL) OR current_job_id = ?) "
                        "AND storage_schema_version = ? AND course_guidance = ?",
                        (
                            record.course_id,
                            record.metadata_revision,
                            record.reference_version,
                            record.created_at,
                            record.title,
                            record.ai_mode,
                            record.quality_mode,
                            record.owner_scope,
                            serialized,
                            record.workflow_id,
                            record.current_job_id,
                            record.current_job_id,
                            COURSE_PERSISTENCE_SCHEMA_VERSION,
                            record.course_guidance,
                        ),
                    )
                    if cur.rowcount == 1:
                        return record
                return _failure("stale_revision")

            # Revision check: must be previous + 1 for advancement
            if record.metadata_revision == previous.metadata_revision:
                self._connection.rollback()
                return _failure("revision_conflict")
            if record.metadata_revision != previous.metadata_revision + 1:
                self._connection.rollback()
                return _failure("stale_revision")

            # Allowed mutation: title, ai_mode, quality_mode, owner_scope, source_refs, current_job_id may change
            # Immutable already checked. Proceed to conditional update
            serialized = _serialize_source_refs(record.source_refs)
            with self._connection:
                cur = self._connection.execute(
                    "UPDATE courses SET reference_version = ?, created_at = ?, title = ?, ai_mode = ?, "
                    "quality_mode = ?, owner_scope = ?, metadata_revision = ?, source_refs_json = ?, "
                    "workflow_id = ?, current_job_id = ?, storage_schema_version = ?, course_guidance = ? "
                    "WHERE course_id = ? AND metadata_revision = ?",
                    (
                        record.reference_version,
                        record.created_at,
                        record.title,
                        record.ai_mode,
                        record.quality_mode,
                        record.owner_scope,
                        record.metadata_revision,
                        serialized,
                        record.workflow_id,
                        record.current_job_id,
                        COURSE_PERSISTENCE_SCHEMA_VERSION,
                        record.course_guidance,
                        record.course_id,
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

    def load(self, course_id: str) -> CoursePersistenceResult:
        """Load one CourseRecord by exact course_id."""

        if type(course_id) is not str:
            raise TypeError("course_id must be exactly str")
        if not _valid_course_id(course_id):
            return _failure("invalid_persistence_input")
        try:
            if not _storage_is_recognized(self._connection):
                return _failure("unsupported_storage_schema")
        except Exception:
            return _failure("persistence_exception")
        try:
            row = self._connection.execute(
                "SELECT course_id, reference_version, created_at, title, ai_mode, quality_mode, "
                "owner_scope, metadata_revision, source_refs_json, workflow_id, current_job_id, "
                "storage_schema_version, course_guidance FROM courses WHERE course_id = ?",
                (course_id,),
            ).fetchone()
        except sqlite3.Error:
            return _failure("persistence_unavailable")
        except Exception:
            return _failure("persistence_exception")
        if row is None:
            return _failure("course_not_found")
        return _decode_row(row, expected_course_id=course_id)

    def list_courses(self) -> tuple[CourseRecord, ...] | CoursePersistenceFailure:
        """Return deterministic ordered tuple of all courses."""

        try:
            if not _storage_is_recognized(self._connection):
                return _failure("unsupported_storage_schema")  # type: ignore[return-value]
        except Exception:
            return _failure("persistence_exception")  # type: ignore[return-value]
        try:
            rows = self._connection.execute(
                "SELECT course_id, reference_version, created_at, title, ai_mode, quality_mode, "
                "owner_scope, metadata_revision, source_refs_json, workflow_id, current_job_id, "
                "storage_schema_version, course_guidance FROM courses ORDER BY course_id ASC"
            ).fetchall()
        except sqlite3.Error:
            return _failure("persistence_unavailable")  # type: ignore[return-value]
        except Exception:
            return _failure("persistence_exception")  # type: ignore[return-value]

        result: list[CourseRecord] = []
        try:
            for row in rows:
                if type(row) is not tuple or len(row) != 13:
                    return _failure("stored_course_invalid")  # type: ignore[return-value]
                course_id = row[0]
                if type(course_id) is not str:
                    return _failure("stored_course_invalid")  # type: ignore[return-value]
                decoded = _decode_row(row, expected_course_id=course_id)
                if type(decoded) is CoursePersistenceFailure:
                    return decoded  # type: ignore[return-value]
                result.append(decoded)
        except Exception:
            return _failure("persistence_exception")  # type: ignore[return-value]
        return tuple(result)

    def close(self) -> None:
        """Close adapter connection."""

        self._connection.close()


def open_course_store(database_path: str | Path) -> LocalCourseStore | CoursePersistenceFailure:
    """Open or initialize one trusted local course store."""

    if type(database_path) is not str and not isinstance(database_path, Path):
        raise TypeError("database_path must be exactly str or Path")
    try:
        connection = sqlite3.connect(database_path)
        connection.execute(_SCHEMA_SQL)
        if _schema_is_recognized(connection, legacy=True):
            # Additive, transactional migration: no original field/content is removed.
            if connection.execute("SELECT 1 FROM courses WHERE storage_schema_version != 'local-course-sqlite/v1' LIMIT 1").fetchone():
                connection.close()
                return _failure("unsupported_storage_schema")
            with connection:
                connection.execute("ALTER TABLE courses ADD COLUMN course_guidance TEXT NOT NULL DEFAULT ''")
                connection.execute("UPDATE courses SET storage_schema_version = ?", (COURSE_PERSISTENCE_SCHEMA_VERSION,))
        if not _storage_is_recognized(connection):
            connection.close()
            return _failure("unsupported_storage_schema")
        return LocalCourseStore(connection, _STORE_CONSTRUCTION_TOKEN)
    except sqlite3.Error:
        return _failure("persistence_unavailable")
    except Exception:
        return _failure("persistence_exception")


def _serialize_source_refs(refs: tuple[SourceEvidenceReference, ...]) -> str:
    items = [
        {
            "reference_version": r.reference_version,
            "source_id": r.source_id,
            "content_sha256": r.content_sha256,
        }
        for r in refs
    ]
    # Canonical already sorted, but ensure deterministic json
    return json.dumps(items, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _deserialize_source_refs(payload: str) -> tuple[SourceEvidenceReference, ...]:
    try:
        decoded = json.loads(payload)
        if type(decoded) is not list:
            raise ValueError("source refs not list")
        refs = []
        for item in decoded:
            if type(item) is not dict or set(item) != {"reference_version", "source_id", "content_sha256"}:
                raise ValueError("source ref item invalid")
            refs.append(
                SourceEvidenceReference(
                    item["reference_version"], item["source_id"], item["content_sha256"]
                )
            )
        # Validate canonical ordering
        canonical = tuple(sorted(refs, key=lambda r: (r.source_id, r.content_sha256)))
        if tuple(refs) != canonical:
            raise ValueError("source refs not canonical")
        # Uniqueness already via CourseRecord
        return tuple(refs)
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("source refs json invalid") from None


def _decode_row(row: tuple[object, ...], *, expected_course_id: str) -> CoursePersistenceResult:
    if type(row) is not tuple or len(row) != 13:
        return _failure("stored_course_invalid")
    (
        course_id,
        reference_version,
        created_at,
        title,
        ai_mode,
        quality_mode,
        owner_scope,
        metadata_revision,
        source_refs_json,
        workflow_id,
        current_job_id,
        schema_version,
        course_guidance,
    ) = row
    if type(schema_version) is not str or schema_version != COURSE_PERSISTENCE_SCHEMA_VERSION:
        return _failure("unsupported_storage_schema")
    if (
        type(course_id) is not str
        or course_id != expected_course_id
        or type(reference_version) is not str
        or type(created_at) is not str
        or type(title) is not str
        or type(ai_mode) is not str
        or type(quality_mode) is not str
        or type(owner_scope) is not str
        or type(metadata_revision) is not int
        or type(source_refs_json) is not str
        or type(workflow_id) is not str
        or (current_job_id is not None and type(current_job_id) is not str)
    ):
        return _failure("stored_course_invalid")
    try:
        source_refs = _deserialize_source_refs(source_refs_json)
        record = CourseRecord(
            reference_version,  # type: ignore[arg-type]
            course_id,
            created_at,
            title,
            ai_mode,  # type: ignore[arg-type]
            quality_mode,  # type: ignore[arg-type]
            owner_scope,  # type: ignore[arg-type]
            metadata_revision,
            source_refs,
            workflow_id,
            current_job_id,
            course_guidance,
        )
        return record
    except (TypeError, ValueError):
        return _failure("stored_course_invalid")
    except Exception:
        return _failure("persistence_exception")


def _schema_is_recognized(connection: sqlite3.Connection, *, legacy: bool = False) -> bool:
    try:
        table_rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM main.sqlite_schema WHERE name = ?",
            ("courses",),
        ).fetchall()
        columns = connection.execute("PRAGMA main.table_xinfo(courses)").fetchall()
        foreign_keys = connection.execute("PRAGMA main.foreign_key_list(courses)").fetchall()
        indexes = connection.execute("PRAGMA main.index_list(courses)").fetchall()
        primary_key_columns = connection.execute(
            f"PRAGMA main.index_xinfo({_PRIMARY_KEY_INDEX_NAME})"
        ).fetchall()
        triggers = connection.execute(
            "SELECT type, name, tbl_name, sql FROM main.sqlite_schema WHERE type = 'trigger' AND tbl_name = ?",
            ("courses",),
        ).fetchall()
        temporary_objects = connection.execute(
            "SELECT type, name, tbl_name, sql FROM temp.sqlite_schema WHERE name = ? OR tbl_name = ?",
            ("courses", "courses"),
        ).fetchall()
    except sqlite3.Error:
        return False
    expected_columns = (
        (0, "course_id", "TEXT", 1, None, 1, 0),
        (1, "reference_version", "TEXT", 1, None, 0, 0),
        (2, "created_at", "TEXT", 1, None, 0, 0),
        (3, "title", "TEXT", 1, None, 0, 0),
        (4, "ai_mode", "TEXT", 1, None, 0, 0),
        (5, "quality_mode", "TEXT", 1, None, 0, 0),
        (6, "owner_scope", "TEXT", 1, None, 0, 0),
        (7, "metadata_revision", "INTEGER", 1, None, 0, 0),
        (8, "source_refs_json", "TEXT", 1, None, 0, 0),
        (9, "workflow_id", "TEXT", 1, None, 0, 0),
        (10, "current_job_id", "TEXT", 0, None, 0, 0),
        (11, "storage_schema_version", "TEXT", 1, None, 0, 0),
    )
    if not legacy:
        expected_columns += ((12, "course_guidance", "TEXT", 1, "''", 0, 0),)
    expected_primary_key_columns = (
        (0, 0, "course_id", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0),
    )
    return (
        type(table_rows) is list
        and len(table_rows) == 1
        and type(table_rows[0]) is tuple
        and len(table_rows[0]) == 4
        and table_rows[0][:3] == ("table", "courses", "courses")
        and _schema_sql_key(table_rows[0][3]) == _schema_sql_key(_LEGACY_TABLE_SQL if legacy else _EXPECTED_TABLE_SQL)
        and type(columns) is list
        and tuple(columns) == expected_columns
        and type(foreign_keys) is list
        and foreign_keys == []
        and type(indexes) is list
        and indexes == [(0, _PRIMARY_KEY_INDEX_NAME, 1, "pk", 0)]
        and type(primary_key_columns) is list
        and tuple(primary_key_columns) == expected_primary_key_columns
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
            "SELECT 1 FROM courses WHERE typeof(storage_schema_version) != 'text' OR storage_schema_version != ? LIMIT 1",
            (COURSE_PERSISTENCE_SCHEMA_VERSION,),
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
    if type(value) is not CoursePersistenceDiagnostic:
        return False
    try:
        CoursePersistenceDiagnostic(value.code, value.classification, value.message)
    except (AttributeError, TypeError, ValueError):
        return False
    return True


def _failure(code: str) -> CoursePersistenceFailure:
    classification, message = _DIAGNOSTICS[code]
    return CoursePersistenceFailure(
        status="persistence_failed",
        diagnostics=(CoursePersistenceDiagnostic(code, classification, message),),
    )
