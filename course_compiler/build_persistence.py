"""Local SQLite persistence for BuildRecord (T050).

The store binds one build to its deterministic INPUT identity (``bundle_hash``)
and to the DERIVED PDF byte integrity of that build (``pdf_sha256``). The two
are distinct authorities and never interchangeable: ``bundle_hash`` answers
"were these the exact compiler inputs", ``pdf_sha256`` answers "are these the
exact produced bytes". ``CourseJobRecord.completed_build_sha256`` remains the
bundle/input hash and keeps its T050 meaning unchanged.

Schema ``local-build-record-sqlite/v2`` adds ``pdf_sha256``. There is no v1 ->
v2 migration: ``local-build-record-sqlite/v1`` only ever existed inside this
unaccepted T050 candidate line, so a v1 file is not recognized and fails
closed as ``unsupported_storage_schema`` rather than being mutated.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

from .asset import AssetReference
from .rendering import DocumentReference

BUILD_PERSISTENCE_SCHEMA_VERSION = "local-build-record-sqlite/v2"
BUILD_RECORD_VERSION = "build-record/v2"

__all__ = [
    "BUILD_PERSISTENCE_SCHEMA_VERSION",
    "BUILD_RECORD_STATUS_VOCABULARY",
    "BUILD_RECORD_VERSION",
    "BuildDiagnostic",
    "BuildPersistenceFailure",
    "BuildPersistenceResult",
    "BuildRecord",
    "LocalBuildRecordStore",
    "open_build_record_store",
]

_DIAGNOSTICS = {
    "invalid_persistence_input": ("input", "The build persistence input is invalid."),
    "persistence_unavailable": ("storage", "The local build store is unavailable."),
    "build_not_found": ("storage", "No stored build record was found."),
    "stored_build_invalid": ("decode", "The stored build record is invalid."),
    "unsupported_storage_schema": ("decode", "The stored build schema is unsupported."),
    "immutable_identity_conflict": ("identity", "The build record immutable identity conflicts."),
    "build_identity_mismatch": ("identity", "The stored build identity does not match."),
    "persistence_exception": ("adapter", "The local build adapter failed."),
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS build_records (
    build_id TEXT PRIMARY KEY NOT NULL,
    job_id TEXT NOT NULL,
    workflow_revision INTEGER NOT NULL,
    document_refs_json TEXT NOT NULL,
    placement_refs_json TEXT NOT NULL,
    asset_refs_json TEXT NOT NULL,
    bundle_hash TEXT NOT NULL,
    pdf_sha256 TEXT,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    diagnostics_json TEXT NOT NULL,
    storage_schema_version TEXT NOT NULL
)
"""
_EXPECTED_TABLE_SQL = _SCHEMA_SQL.replace("IF NOT EXISTS ", "", 1).strip()
_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_build_records_job_id ON build_records (job_id)"
_EXPECTED_INDEX_SQL = _INDEX_SQL.replace("IF NOT EXISTS ", "", 1).strip()
_PRIMARY_KEY_INDEX_NAME = "sqlite_autoindex_build_records_1"
_JOB_INDEX_NAME = "idx_build_records_job_id"

_STORE_CONSTRUCTION_TOKEN = object()

_SAFE_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_CREATED_AT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_REVISION = 9_223_372_036_854_775_807

BUILD_RECORD_STATUS_VOCABULARY = frozenset({"succeeded", "failed"})


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


@dataclass(frozen=True, slots=True)
class BuildDiagnostic:
    """One fixed, content-safe build diagnostic."""

    code: str
    classification: Literal["input", "storage", "decode", "identity", "adapter"]
    message: str

    def __post_init__(self) -> None:
        if self.code not in _DIAGNOSTICS:
            raise ValueError("build diagnostic code is not registered")
        classification, message = _DIAGNOSTICS[self.code]
        if self.classification != classification or self.message != message:
            raise ValueError("build diagnostic fields are not registered")


@dataclass(frozen=True, slots=True)
class BuildPersistenceFailure:
    """A content-safe build persistence failure with one fixed diagnostic."""

    status: Literal["persistence_failed"]
    diagnostics: tuple[BuildDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "persistence_failed":
            raise ValueError("build persistence failure status is invalid")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) != 1
            or not _is_valid_diagnostic(self.diagnostics[0])
        ):
            raise ValueError("build persistence failure diagnostics are invalid")


@dataclass(frozen=True, slots=True)
class BuildRecord:
    """Durable evidence of one deterministic course PDF build."""

    build_id: str
    job_id: str
    workflow_revision: int
    document_refs: tuple[DocumentReference, ...]
    placement_refs: tuple[str, ...]
    asset_refs: tuple[AssetReference, ...]
    bundle_hash: str
    pdf_sha256: str | None
    created_at: str
    status: Literal["succeeded", "failed"]
    diagnostics: tuple[BuildDiagnostic, ...]
    record_version: str = BUILD_RECORD_VERSION

    def __post_init__(self) -> None:
        if not _valid_safe_id(self.build_id):
            raise ValueError("build_id is invalid")
        if not _valid_safe_id(self.job_id):
            raise ValueError("job_id is invalid")
        if type(self.workflow_revision) is not int or not (0 <= self.workflow_revision <= _MAX_REVISION):
            raise ValueError("workflow_revision is invalid")
        if type(self.document_refs) is not tuple:
            raise ValueError("document_refs must be a tuple")
        for doc_ref in self.document_refs:
            if type(doc_ref) is not DocumentReference:
                raise ValueError("document_ref item is invalid")
        if type(self.placement_refs) is not tuple:
            raise ValueError("placement_refs must be a tuple")
        for pref in self.placement_refs:
            if type(pref) is not str or not pref:
                raise ValueError("placement_ref item is invalid")
        if type(self.asset_refs) is not tuple:
            raise ValueError("asset_refs must be a tuple")
        for aref in self.asset_refs:
            if type(aref) is not AssetReference:
                raise ValueError("asset_ref item is invalid")
        if not _valid_sha256_hex(self.bundle_hash):
            raise ValueError("bundle_hash is invalid")
        if self.pdf_sha256 is not None and not _valid_sha256_hex(self.pdf_sha256):
            raise ValueError("pdf_sha256 is invalid")
        if not _valid_created_at(self.created_at):
            raise ValueError("created_at is invalid")
        if type(self.status) is not str or self.status not in BUILD_RECORD_STATUS_VOCABULARY:
            raise ValueError("status is invalid")
        if type(self.diagnostics) is not tuple:
            raise ValueError("diagnostics must be a tuple")
        for d in self.diagnostics:
            if not _is_valid_diagnostic(d):
                raise ValueError("diagnostic item is invalid")
        if self.record_version != BUILD_RECORD_VERSION:
            raise ValueError("record_version is invalid")
        if self.status == "succeeded":
            if not self.document_refs:
                raise ValueError("succeeded build must reference at least one document")
            if self.diagnostics:
                raise ValueError("succeeded build must not have diagnostics")
            # A successful build is durable evidence of exact derived bytes,
            # so the derived-artifact digest is mandatory and immutable.
            if self.pdf_sha256 is None:
                raise ValueError("succeeded build must carry pdf_sha256")
        if self.status == "failed":
            if not self.diagnostics:
                raise ValueError("failed build must have at least one diagnostic")
            if self.pdf_sha256 is not None:
                raise ValueError("failed build must not carry pdf_sha256")

    def __repr__(self) -> str:
        return (
            f"BuildRecord(build_id={self.build_id!r}, "
            f"job_id={self.job_id!r}, "
            f"workflow_revision={self.workflow_revision!r}, "
            f"bundle_hash={self.bundle_hash!r}, "
            f"pdf_sha256={self.pdf_sha256!r}, "
            f"status={self.status!r})"
        )


BuildPersistenceResult: TypeAlias = BuildRecord | BuildPersistenceFailure


class LocalBuildRecordStore:
    """Local SQLite store for immutable BuildRecord values."""

    __slots__ = ("_connection",)

    def __init__(self, connection: sqlite3.Connection, token: object) -> None:
        if token is not _STORE_CONSTRUCTION_TOKEN:
            raise TypeError("build stores must be opened by the adapter")
        self._connection = connection

    def save(self, record: BuildRecord) -> BuildPersistenceResult:
        """Persist one BuildRecord idempotently or reject immutable identity conflicts."""

        if type(record) is not BuildRecord:
            raise TypeError("record must be exactly BuildRecord")
        try:
            BuildRecord(
                build_id=record.build_id,
                job_id=record.job_id,
                workflow_revision=record.workflow_revision,
                document_refs=record.document_refs,
                placement_refs=record.placement_refs,
                asset_refs=record.asset_refs,
                bundle_hash=record.bundle_hash,
                pdf_sha256=record.pdf_sha256,
                created_at=record.created_at,
                status=record.status,
                diagnostics=record.diagnostics,
                record_version=record.record_version,
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
                "SELECT build_id, job_id, workflow_revision, document_refs_json, "
                "placement_refs_json, asset_refs_json, bundle_hash, pdf_sha256, created_at, "
                "status, diagnostics_json, storage_schema_version "
                "FROM build_records WHERE build_id = ?",
                (record.build_id,),
            ).fetchone()

            if row is None:
                self._connection.execute(
                    "INSERT INTO build_records (build_id, job_id, workflow_revision, "
                    "document_refs_json, placement_refs_json, asset_refs_json, "
                    "bundle_hash, pdf_sha256, created_at, status, diagnostics_json, storage_schema_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _record_to_tuple(record),
                )
                self._connection.commit()
                return record

            previous = _decode_row(row, expected_build_id=record.build_id)
            if type(previous) is BuildPersistenceFailure:
                self._connection.rollback()
                return previous

            if previous == record:
                self._connection.rollback()
                return record

            self._connection.rollback()
            return _failure("immutable_identity_conflict")

        except sqlite3.Error:
            _rollback_quietly(self._connection)
            return _failure("persistence_unavailable")
        except Exception:
            _rollback_quietly(self._connection)
            return _failure("persistence_exception")

    def load(self, build_id: str) -> BuildPersistenceResult:
        """Load one BuildRecord by exact build_id."""

        if type(build_id) is not str:
            raise TypeError("build_id must be exactly str")
        if not _valid_safe_id(build_id):
            return _failure("invalid_persistence_input")
        try:
            if not _storage_is_recognized(self._connection):
                return _failure("unsupported_storage_schema")
        except Exception:
            return _failure("persistence_exception")
        try:
            row = self._connection.execute(
                "SELECT build_id, job_id, workflow_revision, document_refs_json, "
                "placement_refs_json, asset_refs_json, bundle_hash, pdf_sha256, created_at, "
                "status, diagnostics_json, storage_schema_version "
                "FROM build_records WHERE build_id = ?",
                (build_id,),
            ).fetchone()
            if row is None:
                return _failure("build_not_found")
            return _decode_row(row, expected_build_id=build_id)
        except sqlite3.Error:
            return _failure("persistence_unavailable")
        except Exception:
            return _failure("persistence_exception")

    def list_for_job(self, job_id: str) -> tuple[BuildRecord, ...] | BuildPersistenceFailure:
        """List all build records for one job in created_at chronological order."""

        if type(job_id) is not str or not _valid_safe_id(job_id):
            return _failure("invalid_persistence_input")
        try:
            if not _storage_is_recognized(self._connection):
                return _failure("unsupported_storage_schema")
        except Exception:
            return _failure("persistence_exception")
        try:
            rows = self._connection.execute(
                "SELECT build_id, job_id, workflow_revision, document_refs_json, "
                "placement_refs_json, asset_refs_json, bundle_hash, pdf_sha256, created_at, "
                "status, diagnostics_json, storage_schema_version "
                "FROM build_records WHERE job_id = ? ORDER BY created_at ASC, rowid ASC",
                (job_id,),
            ).fetchall()
            records = []
            for row in rows:
                decoded = _decode_row(row, expected_build_id=str(row[0]))
                if type(decoded) is BuildPersistenceFailure:
                    return decoded
                records.append(decoded)
            return tuple(records)
        except sqlite3.Error:
            return _failure("persistence_unavailable")
        except Exception:
            return _failure("persistence_exception")

    def latest_successful_for_job(self, job_id: str) -> BuildRecord | None | BuildPersistenceFailure:
        """Return the latest successful build record for one job, or None."""

        if type(job_id) is not str or not _valid_safe_id(job_id):
            return _failure("invalid_persistence_input")
        try:
            if not _storage_is_recognized(self._connection):
                return _failure("unsupported_storage_schema")
        except Exception:
            return _failure("persistence_exception")
        try:
            row = self._connection.execute(
                "SELECT build_id, job_id, workflow_revision, document_refs_json, "
                "placement_refs_json, asset_refs_json, bundle_hash, pdf_sha256, created_at, "
                "status, diagnostics_json, storage_schema_version "
                "FROM build_records WHERE job_id = ? AND status = 'succeeded' "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            return _decode_row(row, expected_build_id=str(row[0]))
        except sqlite3.Error:
            return _failure("persistence_unavailable")
        except Exception:
            return _failure("persistence_exception")

    def close(self) -> None:
        try:
            self._connection.close()
        except Exception:
            pass


def open_build_record_store(database_path: str | Path) -> LocalBuildRecordStore | BuildPersistenceFailure:
    """Open or initialize one exact-schema SQLite store for BuildRecord."""

    if type(database_path) is not str and not isinstance(database_path, Path):
        raise TypeError("database_path must be exactly str or Path")
    try:
        connection = sqlite3.connect(database_path)
        with connection:
            connection.execute(_SCHEMA_SQL)
            connection.execute(_INDEX_SQL)
        if not _storage_is_recognized(connection):
            connection.close()
            return _failure("unsupported_storage_schema")
        return LocalBuildRecordStore(connection, _STORE_CONSTRUCTION_TOKEN)
    except sqlite3.Error:
        return _failure("persistence_unavailable")
    except Exception:
        return _failure("persistence_exception")


def _record_to_tuple(record: BuildRecord) -> tuple:
    doc_refs_data = [
        {
            "contract_version": d.contract_version,
            "document_id": d.document_id,
            "order": d.order,
            "content_sha256": d.content_sha256,
        }
        for d in record.document_refs
    ]
    doc_refs_json = json.dumps(doc_refs_data, sort_keys=True, separators=(",", ":"))
    placement_refs_json = json.dumps(list(record.placement_refs), sort_keys=True, separators=(",", ":"))
    asset_refs_data = [
        {"reference_version": a.reference_version, "content_sha256": a.content_sha256}
        for a in record.asset_refs
    ]
    asset_refs_json = json.dumps(asset_refs_data, sort_keys=True, separators=(",", ":"))
    diag_data = [
        {"code": d.code, "classification": d.classification, "message": d.message}
        for d in record.diagnostics
    ]
    diag_json = json.dumps(diag_data, sort_keys=True, separators=(",", ":"))

    return (
        record.build_id,
        record.job_id,
        record.workflow_revision,
        doc_refs_json,
        placement_refs_json,
        asset_refs_json,
        record.bundle_hash,
        record.pdf_sha256,
        record.created_at,
        record.status,
        diag_json,
        BUILD_PERSISTENCE_SCHEMA_VERSION,
    )


def _decode_row(row: tuple[object, ...], *, expected_build_id: str) -> BuildPersistenceResult:
    if type(row) is not tuple or len(row) != 12:
        return _failure("stored_build_invalid")
    (
        build_id,
        job_id,
        workflow_revision,
        document_refs_json,
        placement_refs_json,
        asset_refs_json,
        bundle_hash,
        pdf_sha256,
        created_at,
        status,
        diagnostics_json,
        schema_version,
    ) = row

    if type(schema_version) is not str or schema_version != BUILD_PERSISTENCE_SCHEMA_VERSION:
        return _failure("unsupported_storage_schema")
    if (
        type(build_id) is not str
        or build_id != expected_build_id
        or type(job_id) is not str
        or type(workflow_revision) is not int
        or type(document_refs_json) is not str
        or type(placement_refs_json) is not str
        or type(asset_refs_json) is not str
        or type(bundle_hash) is not str
        or (pdf_sha256 is not None and type(pdf_sha256) is not str)
        or type(created_at) is not str
        or type(status) is not str
        or type(diagnostics_json) is not str
    ):
        return _failure("stored_build_invalid")

    try:
        doc_raw = json.loads(document_refs_json)
        if not isinstance(doc_raw, list):
            return _failure("stored_build_invalid")
        doc_refs = tuple(
            DocumentReference(
                d["contract_version"],
                d["document_id"],
                d["order"],
                d["content_sha256"],
            )
            for d in doc_raw
        )

        placement_raw = json.loads(placement_refs_json)
        if not isinstance(placement_raw, list) or not all(type(p) is str for p in placement_raw):
            return _failure("stored_build_invalid")
        placement_refs = tuple(placement_raw)

        asset_raw = json.loads(asset_refs_json)
        if not isinstance(asset_raw, list):
            return _failure("stored_build_invalid")
        asset_refs = tuple(
            AssetReference(a["reference_version"], a["content_sha256"])
            for a in asset_raw
        )

        diag_raw = json.loads(diagnostics_json)
        if not isinstance(diag_raw, list):
            return _failure("stored_build_invalid")
        diagnostics = tuple(
            BuildDiagnostic(d["code"], d["classification"], d["message"])
            for d in diag_raw
        )

        record = BuildRecord(
            build_id=build_id,
            job_id=job_id,
            workflow_revision=workflow_revision,
            document_refs=doc_refs,
            placement_refs=placement_refs,
            asset_refs=asset_refs,
            bundle_hash=bundle_hash,
            pdf_sha256=pdf_sha256,
            created_at=created_at,
            status=status,  # type: ignore[arg-type]
            diagnostics=diagnostics,
        )
        return record
    except (TypeError, ValueError, KeyError):
        return _failure("stored_build_invalid")
    except Exception:
        return _failure("persistence_exception")


def _schema_is_recognized(connection: sqlite3.Connection) -> bool:
    try:
        table_rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM main.sqlite_schema WHERE name = ?",
            ("build_records",),
        ).fetchall()
        columns = connection.execute("PRAGMA main.table_xinfo(build_records)").fetchall()
        foreign_keys = connection.execute("PRAGMA main.foreign_key_list(build_records)").fetchall()
        indexes = connection.execute("PRAGMA main.index_list(build_records)").fetchall()
        primary_key_columns = connection.execute(
            f"PRAGMA main.index_xinfo({_PRIMARY_KEY_INDEX_NAME})"
        ).fetchall()
        job_index_columns = connection.execute(
            f"PRAGMA main.index_xinfo({_JOB_INDEX_NAME})"
        ).fetchall()
        triggers = connection.execute(
            "SELECT type, name, tbl_name, sql FROM main.sqlite_schema WHERE type = 'trigger' AND tbl_name = ?",
            ("build_records",),
        ).fetchall()
        temporary_objects = connection.execute(
            "SELECT type, name, tbl_name, sql FROM temp.sqlite_schema WHERE name = ? OR tbl_name = ?",
            ("build_records", "build_records"),
        ).fetchall()
    except sqlite3.Error:
        return False

    expected_columns = (
        (0, "build_id", "TEXT", 1, None, 1, 0),
        (1, "job_id", "TEXT", 1, None, 0, 0),
        (2, "workflow_revision", "INTEGER", 1, None, 0, 0),
        (3, "document_refs_json", "TEXT", 1, None, 0, 0),
        (4, "placement_refs_json", "TEXT", 1, None, 0, 0),
        (5, "asset_refs_json", "TEXT", 1, None, 0, 0),
        (6, "bundle_hash", "TEXT", 1, None, 0, 0),
        (7, "pdf_sha256", "TEXT", 0, None, 0, 0),
        (8, "created_at", "TEXT", 1, None, 0, 0),
        (9, "status", "TEXT", 1, None, 0, 0),
        (10, "diagnostics_json", "TEXT", 1, None, 0, 0),
        (11, "storage_schema_version", "TEXT", 1, None, 0, 0),
    )
    expected_primary_key_columns = (
        (0, 0, "build_id", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0),
    )
    expected_job_index_columns = (
        (0, 1, "job_id", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0),
    )
    indexes_set = {(item[1], item[2], item[3]) for item in indexes} if isinstance(indexes, list) else set()
    expected_indexes_set = {
        (_JOB_INDEX_NAME, 0, "c"),
        (_PRIMARY_KEY_INDEX_NAME, 1, "pk"),
    }

    return (
        type(table_rows) is list
        and len(table_rows) == 1
        and type(table_rows[0]) is tuple
        and len(table_rows[0]) == 4
        and table_rows[0][:3] == ("table", "build_records", "build_records")
        and _schema_sql_key(table_rows[0][3]) == _schema_sql_key(_EXPECTED_TABLE_SQL)
        and type(columns) is list
        and tuple(columns) == expected_columns
        and type(foreign_keys) is list
        and foreign_keys == []
        and indexes_set == expected_indexes_set
        and type(primary_key_columns) is list
        and tuple(primary_key_columns) == expected_primary_key_columns
        and type(job_index_columns) is list
        and tuple(job_index_columns) == expected_job_index_columns
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
            "SELECT 1 FROM build_records WHERE typeof(storage_schema_version) != 'text' OR storage_schema_version != ? LIMIT 1",
            (BUILD_PERSISTENCE_SCHEMA_VERSION,),
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
    if type(value) is not BuildDiagnostic:
        return False
    try:
        BuildDiagnostic(value.code, value.classification, value.message)
    except (AttributeError, TypeError, ValueError):
        return False
    return True


def _failure(code: str) -> BuildPersistenceFailure:
    classification, message = _DIAGNOSTICS[code]
    return BuildPersistenceFailure(
        status="persistence_failed",
        diagnostics=(BuildDiagnostic(code, classification, message),),
    )
