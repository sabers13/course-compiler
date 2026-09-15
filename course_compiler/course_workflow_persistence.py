"""Local SQLite persistence for exact immutable course-workflow associations."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

from .course import CourseReference
from .course_workflow import CourseWorkflowAssociation


COURSE_WORKFLOW_PERSISTENCE_SCHEMA_VERSION = (
    "local-course-workflow-association-sqlite/v1"
)

__all__ = [
    "COURSE_WORKFLOW_PERSISTENCE_SCHEMA_VERSION",
    "CourseWorkflowPersistenceDiagnostic",
    "CourseWorkflowPersistenceFailure",
    "CourseWorkflowPersistenceResult",
    "LocalCourseWorkflowAssociationStore",
    "open_course_workflow_association_store",
]


_DIAGNOSTICS = {
    "invalid_persistence_input": (
        "input",
        "The course-workflow association persistence input is invalid.",
    ),
    "persistence_unavailable": (
        "storage",
        "The local course-workflow association store is unavailable.",
    ),
    "unsupported_storage_schema": (
        "decode",
        "The stored course-workflow association schema is unsupported.",
    ),
    "association_not_found": (
        "storage",
        "No stored course-workflow association was found.",
    ),
    "stored_association_invalid": (
        "decode",
        "The stored course-workflow association is invalid.",
    ),
    "association_identity_mismatch": (
        "identity",
        "The stored course-workflow association does not match its identity.",
    ),
    "persistence_exception": (
        "adapter",
        "The local course-workflow association adapter failed.",
    ),
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS course_workflow_associations (
    association_version TEXT NOT NULL,
    course_reference_version TEXT NOT NULL,
    course_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    storage_schema_version TEXT NOT NULL,
    PRIMARY KEY (
        association_version,
        course_reference_version,
        course_id,
        workflow_id
    )
)
"""
_EXPECTED_TABLE_SQL = _SCHEMA_SQL.replace("IF NOT EXISTS ", "", 1).strip()
_PRIMARY_KEY_INDEX_NAME = "sqlite_autoindex_course_workflow_associations_1"
_STORE_CONSTRUCTION_TOKEN = object()


@dataclass(frozen=True, slots=True)
class CourseWorkflowPersistenceDiagnostic:
    """One fixed, content-safe association-persistence diagnostic."""

    code: str
    classification: Literal["input", "storage", "decode", "identity", "adapter"]
    message: str

    def __post_init__(self) -> None:
        if self.code not in _DIAGNOSTICS:
            raise ValueError(
                "course-workflow persistence diagnostic code is not registered"
            )
        classification, message = _DIAGNOSTICS[self.code]
        if self.classification != classification or self.message != message:
            raise ValueError(
                "course-workflow persistence diagnostic fields are not registered"
            )


@dataclass(frozen=True, slots=True)
class CourseWorkflowPersistenceFailure:
    """A content-safe association-persistence failure with one diagnostic."""

    status: Literal["persistence_failed"]
    diagnostics: tuple[CourseWorkflowPersistenceDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "persistence_failed":
            raise ValueError("course-workflow persistence failure status is invalid")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) != 1
            or not _is_valid_diagnostic(self.diagnostics[0])
        ):
            raise ValueError(
                "course-workflow persistence failure diagnostics are invalid"
            )


CourseWorkflowPersistenceResult: TypeAlias = (
    CourseWorkflowAssociation | CourseWorkflowPersistenceFailure
)


class LocalCourseWorkflowAssociationStore:
    """A trusted-local immutable association store with neutral cardinality."""

    __slots__ = ("_connection",)

    def __init__(self, connection: sqlite3.Connection, token: object) -> None:
        if token is not _STORE_CONSTRUCTION_TOKEN:
            raise TypeError("course-workflow stores must be opened by the adapter")
        self._connection = connection

    def save(
        self, association: CourseWorkflowAssociation
    ) -> CourseWorkflowPersistenceResult:
        """Persist one complete association identity once, or confirm it."""

        if type(association) is not CourseWorkflowAssociation:
            raise TypeError("association must be exactly CourseWorkflowAssociation")
        try:
            _validated_association(association)
        except Exception:
            return _failure("invalid_persistence_input")

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            if not _storage_is_recognized(self._connection):
                self._connection.rollback()
                return _failure("unsupported_storage_schema")
            row = self._connection.execute(
                "SELECT association_version, course_reference_version, course_id, "
                "workflow_id, storage_schema_version "
                "FROM course_workflow_associations "
                "WHERE association_version = ? AND course_reference_version = ? "
                "AND course_id = ? AND workflow_id = ?",
                _association_key(association),
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO course_workflow_associations "
                    "(association_version, course_reference_version, course_id, "
                    "workflow_id, storage_schema_version) VALUES (?, ?, ?, ?, ?)",
                    (
                        *_association_key(association),
                        COURSE_WORKFLOW_PERSISTENCE_SCHEMA_VERSION,
                    ),
                )
                self._connection.commit()
                return association
            stored = _decode_row(row, expected_association=association)
            self._connection.rollback()
            return stored
        except sqlite3.Error:
            _rollback_quietly(self._connection)
            return _failure("persistence_unavailable")
        except Exception:
            _rollback_quietly(self._connection)
            return _failure("persistence_exception")

    def load(
        self, association: CourseWorkflowAssociation
    ) -> CourseWorkflowPersistenceResult:
        """Reopen one association only through its complete exact identity."""

        if type(association) is not CourseWorkflowAssociation:
            raise TypeError("association must be exactly CourseWorkflowAssociation")
        try:
            _validated_association(association)
        except Exception:
            return _failure("invalid_persistence_input")

        try:
            self._connection.execute("BEGIN")
            if not _storage_is_recognized(self._connection):
                self._connection.rollback()
                return _failure("unsupported_storage_schema")
            row = self._connection.execute(
                "SELECT association_version, course_reference_version, course_id, "
                "workflow_id, storage_schema_version "
                "FROM course_workflow_associations "
                "WHERE association_version = ? AND course_reference_version = ? "
                "AND course_id = ? AND workflow_id = ?",
                _association_key(association),
            ).fetchone()
        except sqlite3.Error:
            _rollback_quietly(self._connection)
            return _failure("persistence_unavailable")
        except Exception:
            _rollback_quietly(self._connection)
            return _failure("persistence_exception")
        if row is None:
            self._connection.rollback()
            return _failure("association_not_found")
        try:
            result = _decode_row(row, expected_association=association)
            self._connection.rollback()
            return result
        except Exception:
            _rollback_quietly(self._connection)
            return _failure("persistence_exception")

    def close(self) -> None:
        """Close this adapter's connection; there is no persisted close state."""

        self._connection.close()


def open_course_workflow_association_store(
    database_path: str | Path,
) -> LocalCourseWorkflowAssociationStore | CourseWorkflowPersistenceFailure:
    """Open or initialize one trusted local association store."""

    if type(database_path) is not str and not isinstance(database_path, Path):
        raise TypeError("database_path must be exactly str or Path")
    try:
        connection = sqlite3.connect(database_path)
        connection.execute(_SCHEMA_SQL)
        if not _storage_is_recognized(connection):
            connection.close()
            return _failure("unsupported_storage_schema")
        return LocalCourseWorkflowAssociationStore(
            connection, _STORE_CONSTRUCTION_TOKEN
        )
    except sqlite3.Error:
        return _failure("persistence_unavailable")
    except Exception:
        return _failure("persistence_exception")


def _validated_association(association: CourseWorkflowAssociation) -> None:
    if type(association) is not CourseWorkflowAssociation:
        raise ValueError("course-workflow association is invalid")
    try:
        course_reference = association.course_reference
        if (
            type(association.association_version) is not str
            or type(course_reference) is not CourseReference
            or type(course_reference.reference_version) is not str
            or type(course_reference.course_id) is not str
            or type(association.workflow_id) is not str
        ):
            raise ValueError("course-workflow association is invalid")
        validated_reference = CourseReference(
            course_reference.reference_version,
            course_reference.course_id,
        )
        CourseWorkflowAssociation(
            association.association_version,
            validated_reference,
            association.workflow_id,
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ValueError("course-workflow association is invalid") from None


def _decode_row(
    row: tuple[object, ...],
    *,
    expected_association: CourseWorkflowAssociation,
) -> CourseWorkflowPersistenceResult:
    if type(row) is not tuple or len(row) != 5:
        return _failure("stored_association_invalid")
    (
        association_version,
        course_reference_version,
        course_id,
        workflow_id,
        schema_version,
    ) = row
    if (
        type(schema_version) is not str
        or schema_version != COURSE_WORKFLOW_PERSISTENCE_SCHEMA_VERSION
    ):
        return _failure("unsupported_storage_schema")
    if (
        type(association_version) is not str
        or type(course_reference_version) is not str
        or type(course_id) is not str
        or type(workflow_id) is not str
    ):
        return _failure("stored_association_invalid")
    try:
        association = CourseWorkflowAssociation(
            association_version,
            CourseReference(course_reference_version, course_id),
            workflow_id,
        )
        _validated_association(association)
        if association != expected_association:
            return _failure("association_identity_mismatch")
        return association
    except (AttributeError, KeyError, TypeError, ValueError):
        return _failure("stored_association_invalid")
    except Exception:
        return _failure("persistence_exception")


def _association_key(
    association: CourseWorkflowAssociation,
) -> tuple[str, str, str, str]:
    return (
        association.association_version,
        association.course_reference.reference_version,
        association.course_reference.course_id,
        association.workflow_id,
    )


def _storage_is_recognized(connection: sqlite3.Connection) -> bool:
    if not _schema_is_recognized(connection):
        return False
    try:
        row = connection.execute(
            "SELECT 1 FROM course_workflow_associations "
            "WHERE typeof(storage_schema_version) != 'text' "
            "OR storage_schema_version != ? LIMIT 1",
            (COURSE_WORKFLOW_PERSISTENCE_SCHEMA_VERSION,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is None


def _schema_is_recognized(connection: sqlite3.Connection) -> bool:
    try:
        table_rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM main.sqlite_schema "
            "WHERE name = ?",
            ("course_workflow_associations",),
        ).fetchall()
        columns = connection.execute(
            "PRAGMA main.table_xinfo(course_workflow_associations)"
        ).fetchall()
        foreign_keys = connection.execute(
            "PRAGMA main.foreign_key_list(course_workflow_associations)"
        ).fetchall()
        indexes = connection.execute(
            "PRAGMA main.index_list(course_workflow_associations)"
        ).fetchall()
        primary_key_columns = connection.execute(
            f"PRAGMA main.index_xinfo({_PRIMARY_KEY_INDEX_NAME})"
        ).fetchall()
        triggers = connection.execute(
            "SELECT type, name, tbl_name, sql FROM main.sqlite_schema "
            "WHERE type = 'trigger' AND tbl_name = ?",
            ("course_workflow_associations",),
        ).fetchall()
        temporary_objects = connection.execute(
            "SELECT type, name, tbl_name, sql FROM temp.sqlite_schema "
            "WHERE name = ? OR tbl_name = ?",
            (
                "course_workflow_associations",
                "course_workflow_associations",
            ),
        ).fetchall()
    except sqlite3.Error:
        return False
    expected_columns = (
        (0, "association_version", "TEXT", 1, None, 1, 0),
        (1, "course_reference_version", "TEXT", 1, None, 2, 0),
        (2, "course_id", "TEXT", 1, None, 3, 0),
        (3, "workflow_id", "TEXT", 1, None, 4, 0),
        (4, "storage_schema_version", "TEXT", 1, None, 0, 0),
    )
    expected_primary_key_columns = (
        (0, 0, "association_version", 0, "BINARY", 1),
        (1, 1, "course_reference_version", 0, "BINARY", 1),
        (2, 2, "course_id", 0, "BINARY", 1),
        (3, 3, "workflow_id", 0, "BINARY", 1),
        (4, -1, None, 0, "BINARY", 0),
    )
    return (
        type(table_rows) is list
        and len(table_rows) == 1
        and type(table_rows[0]) is tuple
        and len(table_rows[0]) == 4
        and table_rows[0][:3]
        == (
            "table",
            "course_workflow_associations",
            "course_workflow_associations",
        )
        and _schema_sql_key(table_rows[0][3])
        == _schema_sql_key(_EXPECTED_TABLE_SQL)
        and type(columns) is list
        and tuple(columns) == expected_columns
        and type(foreign_keys) is list
        and foreign_keys == []
        and type(indexes) is list
        and indexes
        == [(0, _PRIMARY_KEY_INDEX_NAME, 1, "pk", 0)]
        and type(primary_key_columns) is list
        and tuple(primary_key_columns) == expected_primary_key_columns
        and type(triggers) is list
        and triggers == []
        and type(temporary_objects) is list
        and temporary_objects == []
    )


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
    if type(value) is not CourseWorkflowPersistenceDiagnostic:
        return False
    try:
        CourseWorkflowPersistenceDiagnostic(
            value.code,
            value.classification,
            value.message,
        )
    except (AttributeError, TypeError, ValueError):
        return False
    return True


def _failure(code: str) -> CourseWorkflowPersistenceFailure:
    classification, message = _DIAGNOSTICS[code]
    return CourseWorkflowPersistenceFailure(
        status="persistence_failed",
        diagnostics=(
            CourseWorkflowPersistenceDiagnostic(code, classification, message),
        ),
    )
