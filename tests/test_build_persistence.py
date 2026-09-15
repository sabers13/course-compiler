"""Tests for BuildRecord value contracts and LocalBuildRecordStore SQLite persistence (T050)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from course_compiler.asset import ASSET_REFERENCE_VERSION, AssetReference
from course_compiler.build_persistence import (
    BUILD_PERSISTENCE_SCHEMA_VERSION,
    BUILD_RECORD_STATUS_VOCABULARY,
    BUILD_RECORD_VERSION,
    BuildDiagnostic,
    BuildPersistenceFailure,
    BuildRecord,
    LocalBuildRecordStore,
    open_build_record_store,
)
from course_compiler.contracts import DOCUMENT_CONTRACT_VERSION
from course_compiler.rendering import DocumentReference

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_ISOLATED_BASE = REPOSITORY_ROOT / "local-data" / "test-tmp-build-persist"
_ISOLATED_BASE.mkdir(parents=True, exist_ok=True)


def _doc_ref(doc_id: str = "doc-1", order: int = 1, content_sha: str = "a" * 64) -> DocumentReference:
    return DocumentReference(DOCUMENT_CONTRACT_VERSION, doc_id, order, content_sha)


def _asset_ref(content_sha: str = "b" * 64) -> AssetReference:
    return AssetReference(ASSET_REFERENCE_VERSION, content_sha)


def _make_record(
    build_id: str = "bld-test1",
    job_id: str = "job-test1",
    workflow_revision: int = 5,
    document_refs: tuple[DocumentReference, ...] | None = None,
    placement_refs: tuple[str, ...] = ("doc-1@10:ast-1",),
    asset_refs: tuple[AssetReference, ...] | None = None,
    bundle_hash: str = "c" * 64,
    pdf_sha256: str | None = "d" * 64,
    created_at: str = "2026-09-03T12:00:00Z",
    status: str = "succeeded",
    diagnostics: tuple[BuildDiagnostic, ...] = (),
) -> BuildRecord:
    if document_refs is None:
        document_refs = (_doc_ref(),)
    if asset_refs is None:
        asset_refs = (_asset_ref(),)
    return BuildRecord(
        build_id=build_id,
        job_id=job_id,
        workflow_revision=workflow_revision,
        document_refs=document_refs,
        placement_refs=placement_refs,
        asset_refs=asset_refs,
        bundle_hash=bundle_hash,
        pdf_sha256=pdf_sha256,
        created_at=created_at,
        status=status,  # type: ignore[arg-type]
        diagnostics=diagnostics,
    )


class TestBuildRecordContract(unittest.TestCase):
    def test_valid_succeeded_build_record(self) -> None:
        rec = _make_record()
        self.assertEqual(rec.build_id, "bld-test1")
        self.assertEqual(rec.job_id, "job-test1")
        self.assertEqual(rec.workflow_revision, 5)
        self.assertEqual(len(rec.document_refs), 1)
        self.assertEqual(rec.bundle_hash, "c" * 64)
        self.assertEqual(rec.status, "succeeded")
        self.assertEqual(rec.record_version, BUILD_RECORD_VERSION)
        self.assertIn("bld-test1", repr(rec))

    def test_invalid_build_id(self) -> None:
        with self.assertRaises(ValueError):
            _make_record(build_id="")
        with self.assertRaises(ValueError):
            _make_record(build_id="..")
        with self.assertRaises(ValueError):
            _make_record(build_id="INVALID_CAPS")

    def test_invalid_job_id(self) -> None:
        with self.assertRaises(ValueError):
            _make_record(job_id="")
        with self.assertRaises(ValueError):
            _make_record(job_id="../job")

    def test_invalid_workflow_revision(self) -> None:
        with self.assertRaises(ValueError):
            _make_record(workflow_revision=-1)

    def test_invalid_bundle_hash(self) -> None:
        with self.assertRaises(ValueError):
            _make_record(bundle_hash="too-short")
        with self.assertRaises(ValueError):
            _make_record(bundle_hash="C" * 64)  # uppercase forbidden

    def test_invalid_created_at(self) -> None:
        with self.assertRaises(ValueError):
            _make_record(created_at="not-a-date")

    def test_succeeded_must_have_document_refs(self) -> None:
        with self.assertRaises(ValueError):
            _make_record(document_refs=())

    def test_succeeded_must_not_have_diagnostics(self) -> None:
        diag = BuildDiagnostic("stored_build_invalid", "decode", "The stored build record is invalid.")
        with self.assertRaises(ValueError):
            _make_record(status="succeeded", diagnostics=(diag,))

    def test_failed_must_have_diagnostics(self) -> None:
        with self.assertRaises(ValueError):
            _make_record(status="failed", diagnostics=())
        diag = BuildDiagnostic("stored_build_invalid", "decode", "The stored build record is invalid.")
        rec = _make_record(status="failed", document_refs=(), pdf_sha256=None, diagnostics=(diag,))
        self.assertEqual(rec.status, "failed")

    def test_pdf_digest_invariants(self) -> None:
        """pdf_sha256 is mandatory on succeeded and forbidden on failed."""

        rec = _make_record()
        self.assertEqual(rec.pdf_sha256, "d" * 64)
        # bundle_hash (input identity) and pdf_sha256 (derived bytes) are distinct.
        self.assertNotEqual(rec.bundle_hash, rec.pdf_sha256)

        with self.assertRaises(ValueError):
            _make_record(pdf_sha256=None)
        with self.assertRaises(ValueError):
            _make_record(pdf_sha256="too-short")
        with self.assertRaises(ValueError):
            _make_record(pdf_sha256="D" * 64)  # uppercase forbidden

        diag = BuildDiagnostic("stored_build_invalid", "decode", "The stored build record is invalid.")
        with self.assertRaises(ValueError):
            _make_record(status="failed", document_refs=(), diagnostics=(diag,))

    def test_diagnostic_registration(self) -> None:
        diag = BuildDiagnostic("build_not_found", "storage", "No stored build record was found.")
        self.assertEqual(diag.code, "build_not_found")
        with self.assertRaises(ValueError):
            BuildDiagnostic("unregistered_code", "storage", "msg")


class TestLocalBuildRecordStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=str(_ISOLATED_BASE), prefix="test-bld-")
        self.db_path = Path(self.tmp.name) / "build-records.sqlite3"
        self.store = open_build_record_store(self.db_path)
        self.assertIsInstance(self.store, LocalBuildRecordStore)

    def tearDown(self) -> None:
        if isinstance(self.store, LocalBuildRecordStore):
            self.store.close()
        self.tmp.cleanup()

    def test_exact_v2_schema_and_versions(self) -> None:
        """The store owns exactly local-build-record-sqlite/v2 including pdf_sha256."""

        self.assertEqual(BUILD_PERSISTENCE_SCHEMA_VERSION, "local-build-record-sqlite/v2")
        self.assertEqual(BUILD_RECORD_VERSION, "build-record/v2")

        rec = _make_record()
        self.assertEqual(self.store.save(rec), rec)

        conn = sqlite3.connect(self.db_path)
        try:
            columns = conn.execute("PRAGMA main.table_xinfo(build_records)").fetchall()
            stored = conn.execute(
                "SELECT pdf_sha256, storage_schema_version FROM build_records WHERE build_id = ?",
                (rec.build_id,),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(
            [(c[1], c[2], c[3]) for c in columns],
            [
                ("build_id", "TEXT", 1),
                ("job_id", "TEXT", 1),
                ("workflow_revision", "INTEGER", 1),
                ("document_refs_json", "TEXT", 1),
                ("placement_refs_json", "TEXT", 1),
                ("asset_refs_json", "TEXT", 1),
                ("bundle_hash", "TEXT", 1),
                ("pdf_sha256", "TEXT", 0),
                ("created_at", "TEXT", 1),
                ("status", "TEXT", 1),
                ("diagnostics_json", "TEXT", 1),
                ("storage_schema_version", "TEXT", 1),
            ],
        )
        self.assertEqual(stored, (rec.pdf_sha256, BUILD_PERSISTENCE_SCHEMA_VERSION))
        # The digest survives a reopen unchanged.
        self.store.close()
        reopened = open_build_record_store(self.db_path)
        self.assertIsInstance(reopened, LocalBuildRecordStore)
        assert isinstance(reopened, LocalBuildRecordStore)
        self.assertEqual(reopened.load(rec.build_id), rec)
        self.store = reopened

    def test_v1_schema_is_not_recognized(self) -> None:
        """A candidate-local v1 store fails closed instead of being migrated."""

        v1_path = Path(self.tmp.name) / "v1-build-records.sqlite3"
        conn = sqlite3.connect(v1_path)
        try:
            conn.execute(
                "CREATE TABLE build_records ("
                "build_id TEXT PRIMARY KEY NOT NULL, job_id TEXT NOT NULL, "
                "workflow_revision INTEGER NOT NULL, document_refs_json TEXT NOT NULL, "
                "placement_refs_json TEXT NOT NULL, asset_refs_json TEXT NOT NULL, "
                "bundle_hash TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL, "
                "diagnostics_json TEXT NOT NULL, storage_schema_version TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX idx_build_records_job_id ON build_records (job_id)"
            )
            conn.commit()
            before = conn.execute(
                "SELECT type, name, sql FROM main.sqlite_schema ORDER BY type, name"
            ).fetchall()
        finally:
            conn.close()

        result = open_build_record_store(v1_path)
        self.assertIsInstance(result, BuildPersistenceFailure)
        assert isinstance(result, BuildPersistenceFailure)
        self.assertEqual(result.diagnostics[0].code, "unsupported_storage_schema")

        conn = sqlite3.connect(v1_path)
        try:
            after = conn.execute(
                "SELECT type, name, sql FROM main.sqlite_schema ORDER BY type, name"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(after, before)

    def test_save_and_load_success(self) -> None:
        rec = _make_record()
        save_res = self.store.save(rec)
        self.assertEqual(save_res, rec)

        loaded = self.store.load(rec.build_id)
        self.assertEqual(loaded, rec)

    def test_save_idempotent(self) -> None:
        rec = _make_record()
        res1 = self.store.save(rec)
        res2 = self.store.save(rec)
        self.assertEqual(res1, rec)
        self.assertEqual(res2, rec)

    def test_save_conflict_on_different_record(self) -> None:
        rec1 = _make_record(build_id="bld-1", bundle_hash="a" * 64)
        rec2 = _make_record(build_id="bld-1", bundle_hash="b" * 64)
        self.assertEqual(self.store.save(rec1), rec1)
        conflict = self.store.save(rec2)
        self.assertIsInstance(conflict, BuildPersistenceFailure)
        self.assertEqual(conflict.diagnostics[0].code, "immutable_identity_conflict")

    def test_load_not_found(self) -> None:
        res = self.store.load("bld-nonexistent")
        self.assertIsInstance(res, BuildPersistenceFailure)
        self.assertEqual(res.diagnostics[0].code, "build_not_found")

    def test_list_for_job(self) -> None:
        rec1 = _make_record(build_id="bld-1", job_id="job-1", created_at="2026-09-03T12:00:01Z")
        rec2 = _make_record(build_id="bld-2", job_id="job-1", created_at="2026-09-03T12:00:02Z")
        rec_other = _make_record(build_id="bld-3", job_id="job-2", created_at="2026-09-03T12:00:03Z")

        self.store.save(rec1)
        self.store.save(rec2)
        self.store.save(rec_other)

        job1_builds = self.store.list_for_job("job-1")
        self.assertIsInstance(job1_builds, tuple)
        self.assertEqual(len(job1_builds), 2)
        self.assertEqual(job1_builds[0].build_id, "bld-1")
        self.assertEqual(job1_builds[1].build_id, "bld-2")

        job2_builds = self.store.list_for_job("job-2")
        self.assertEqual(len(job2_builds), 1)

        empty_builds = self.store.list_for_job("job-empty")
        self.assertEqual(empty_builds, ())

    def test_latest_successful_for_job(self) -> None:
        diag = BuildDiagnostic("stored_build_invalid", "decode", "The stored build record is invalid.")
        rec1 = _make_record(build_id="bld-1", job_id="job-1", created_at="2026-09-03T12:00:01Z", status="succeeded")
        rec2_failed = _make_record(build_id="bld-2", job_id="job-1", created_at="2026-09-03T12:00:05Z", status="failed", document_refs=(), pdf_sha256=None, diagnostics=(diag,))

        self.store.save(rec1)
        self.store.save(rec2_failed)

        latest = self.store.latest_successful_for_job("job-1")
        self.assertEqual(latest, rec1)

        none_latest = self.store.latest_successful_for_job("job-none")
        self.assertIsNone(none_latest)

    def test_schema_drift_fails_closed(self) -> None:
        # Extra column injected directly
        conn = sqlite3.connect(self.db_path)
        conn.execute("ALTER TABLE build_records ADD COLUMN rogue_col TEXT")
        conn.close()

        # Reopening should fail closed
        bad_store = open_build_record_store(self.db_path)
        self.assertIsInstance(bad_store, BuildPersistenceFailure)
        self.assertEqual(bad_store.diagnostics[0].code, "unsupported_storage_schema")


if __name__ == "__main__":
    unittest.main()
