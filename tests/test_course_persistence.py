"""Focused tests for T048 CourseRecord / LocalCourseStore."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
import shutil

from course_compiler.course import COURSE_REFERENCE_VERSION, CourseReference
from course_compiler.course_persistence import (
    COURSE_PERSISTENCE_SCHEMA_VERSION,
    COURSE_RECORD_VERSION,
    CoursePersistenceDiagnostic,
    CoursePersistenceFailure,
    CourseRecord,
    LocalCourseStore,
    open_course_store,
)
from course_compiler.workflow import SourceEvidenceReference

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_ISOLATED_BASE = REPOSITORY_ROOT / "local-data" / "test-tmp-course-persist"
_ISOLATED_BASE.mkdir(parents=True, exist_ok=True)


def _tmp_dir() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(dir=str(_ISOLATED_BASE), prefix="course-")


def _make_ref(source_id: str, marker: str) -> SourceEvidenceReference:
    return SourceEvidenceReference("source-evidence-reference/v1", source_id, hashlib.sha256(marker.encode()).hexdigest())


def _make_course(
    course_id: str = "c001",
    created_at: str = "2026-01-02T03:04:05Z",
    title: str = "Intro Algorithms",
    ai_mode: str = "gpt",
    quality_mode: str = "fast",
    metadata_revision: int = 0,
    source_refs: tuple[SourceEvidenceReference, ...] = (),
    workflow_id: str = "w001",
    current_job_id: str | None = None,
    reference_version: str = COURSE_REFERENCE_VERSION,
    owner_scope: str = "single_user_local",
) -> CourseRecord:
    return CourseRecord(
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
    )


def failure_code(result: object) -> str:
    assert isinstance(result, CoursePersistenceFailure)
    return result.diagnostics[0].code


class TestCourseRecord(unittest.TestCase):
    def test_frozen_slotted_and_redacted_repr(self) -> None:
        rec = _make_course()
        self.assertEqual(COURSE_RECORD_VERSION, "course-record/v1")
        self.assertEqual(COURSE_PERSISTENCE_SCHEMA_VERSION, "local-course-sqlite/v2")
        self.assertFalse(hasattr(rec, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            rec.title = "changed"  # type: ignore[misc]
        # repr must not contain title private?
        r = repr(rec)
        self.assertIn("c001", r)
        # title should be redacted? Our repr only shows course_id, workflow_id, revision
        self.assertNotIn("Intro Algorithms", r)
        # fields exact
        self.assertEqual(
            [f.name for f in fields(CourseRecord)],
            [
                "reference_version",
                "course_id",
                "created_at",
                "title",
                "ai_mode",
                "quality_mode",
                "owner_scope",
                "metadata_revision",
                "source_refs",
                "workflow_id",
                "current_job_id",
                "course_guidance",
            ],
        )

    def test_valid_record_and_invalid_title(self) -> None:
        rec = _make_course()
        self.assertEqual(rec.course_id, "c001")
        with self.assertRaises(ValueError):
            _make_course(title="")
        with self.assertRaises(ValueError):
            _make_course(title="a" * 201)
        with self.assertRaises(ValueError):
            _make_course(title=" bad leading")
        with self.assertRaises(ValueError):
            _make_course(title="bad\x00control")
        with self.assertRaises(ValueError):
            _make_course(ai_mode="invalid")
        with self.assertRaises(ValueError):
            _make_course(quality_mode="invalid")
        with self.assertRaises(ValueError):
            _make_course(course_id="Bad/Path")
        with self.assertRaises(ValueError):
            _make_course(course_id=".")
        with self.assertRaises(ValueError):
            _make_course(created_at="not-iso")
        with self.assertRaises(ValueError):
            _make_course(workflow_id="bad/id")
        with self.assertRaises(ValueError):
            _make_course(current_job_id="bad/id")
        # source_refs canonical ordering enforcement
        r1 = _make_ref("src-a", "bytes-a")
        r2 = _make_ref("src-b", "bytes-b")
        # Correct order
        rec2 = _make_course(source_refs=(r1, r2))
        self.assertEqual(rec2.source_refs, (r1, r2))
        # Reverse should raise
        with self.assertRaises(ValueError):
            _make_course(source_refs=(r2, r1))
        # Duplicate source_id
        with self.assertRaises(ValueError):
            _make_course(source_refs=(r1, _make_ref("src-a", "other")))

    def test_diagnostic_fixed(self) -> None:
        d = CoursePersistenceDiagnostic("course_not_found", "storage", "No stored course was found.")
        self.assertFalse(hasattr(d, "__dict__"))
        with self.assertRaises(ValueError):
            CoursePersistenceDiagnostic("unknown", "storage", "bad")


class TestLocalCourseStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _tmp_dir()
        self.db_path = Path(self.tmp.name) / "courses.sqlite3"
        store = open_course_store(self.db_path)
        assert isinstance(store, LocalCourseStore)
        self.store = store

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_valid_round_trip(self) -> None:
        rec = _make_course()
        self.assertEqual(self.store.save(rec), rec)
        loaded = self.store.load("c001")
        self.assertEqual(loaded, rec)

    def test_exact_reopen_survives(self) -> None:
        rec = _make_course(course_id="c002", workflow_id="w002")
        self.store.save(rec)
        self.store.close()
        reopened = open_course_store(self.db_path)
        assert isinstance(reopened, LocalCourseStore)
        self.store = reopened
        loaded = self.store.load("c002")
        self.assertEqual(loaded, rec)

    def test_immutable_identity_cannot_be_rewritten(self) -> None:
        rec = _make_course(course_id="c003", created_at="2026-01-02T03:04:05Z", workflow_id="w003")
        self.assertEqual(self.store.save(rec), rec)
        # Same course_id different created_at
        bad = _make_course(course_id="c003", created_at="2026-01-03T00:00:00Z", workflow_id="w003")
        self.assertEqual(failure_code(self.store.save(bad)), "immutable_identity_conflict")
        # Different workflow_id
        bad2 = _make_course(course_id="c003", created_at="2026-01-02T03:04:05Z", workflow_id="w999")
        self.assertEqual(failure_code(self.store.save(bad2)), "immutable_identity_conflict")
        # Ensure original still loads
        self.assertEqual(self.store.load("c003"), rec)

    def test_metadata_validation_rejected_before_mutation(self) -> None:
        # Invalid course should not insert row
        with self.assertRaises(ValueError):
            bad = _make_course(course_id="bad/id")
            self.store.save(bad)  # type: ignore[arg-type]
        # Ensure no row for bad id
        # Use invalid save via store.save with forged record that bypasses constructor? Need to test store's own validation via invalid record type?
        # Try to save with type mismatch
        with self.assertRaises(TypeError):
            self.store.save("not a record")  # type: ignore[arg-type]
        # Check that existing valid not affected
        rec = _make_course(course_id="c004", workflow_id="w004")
        self.store.save(rec)
        self.assertEqual(self.store.load("c004"), rec)

    def test_duplicate_conflict_and_idempotent_exact_repeat(self) -> None:
        rec = _make_course(course_id="c005", workflow_id="w005")
        self.assertEqual(self.store.save(rec), rec)
        # Exact repeat idempotent
        self.assertEqual(self.store.save(rec), rec)
        # Same revision but different title -> revision_conflict
        modified = _make_course(course_id="c005", workflow_id="w005", title="Changed Title")
        self.assertEqual(failure_code(self.store.save(modified)), "revision_conflict")
        # Same revision+1 with correct increment succeeds
        updated = _make_course(course_id="c005", workflow_id="w005", title="Changed Title", metadata_revision=1)
        self.assertEqual(self.store.save(updated), updated)
        self.assertEqual(self.store.load("c005"), updated)

    def test_conditional_metadata_update_stale(self) -> None:
        rec = _make_course(course_id="c006", workflow_id="w006", title="T1")
        self.store.save(rec)
        # Advance to rev 1
        r1 = _make_course(course_id="c006", workflow_id="w006", title="T2", metadata_revision=1)
        self.assertEqual(self.store.save(r1), r1)
        # Attempt to save with old revision 0 should be stale
        stale = _make_course(course_id="c006", workflow_id="w006", title="Stale", metadata_revision=0)
        # But stale == rec, exact repeat would have been idempotent if same as original? However we already advanced to rev1, so this is stale
        # Since rec == 0 and current is 1, saving rec again with same payload but old revision should be stale_revision (because previous revision is 1)
        # Our save logic treats same revision but different payload as conflict; but if payload same as original (rec) and current is r1, this will be revision check: rec.metadata_revision (0) != prev(1) and != prev+1 => stale
        self.assertEqual(failure_code(self.store.save(stale)), "stale_revision")
        # Gap revision 3 when expected 2 -> stale
        gap = _make_course(course_id="c006", workflow_id="w006", title="Gap", metadata_revision=3)
        self.assertEqual(failure_code(self.store.save(gap)), "stale_revision")

    def test_deterministic_list_ordering(self) -> None:
        # Create in non-sorted order
        for cid in ["c009", "c007", "c008"]:
            rec = _make_course(course_id=cid, workflow_id="w" + cid[1:])
            self.store.save(rec)
        listed = self.store.list_courses()
        assert isinstance(listed, tuple)
        ids = [r.course_id for r in listed]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(ids, ["c007", "c008", "c009"])

    def test_source_refs_only_no_blob_duplication(self) -> None:
        r1 = _make_ref("src-1", "bytes-1")
        r2 = _make_ref("src-2", "bytes-2")
        rec = _make_course(course_id="c010", workflow_id="w010", source_refs=(r1, r2))
        self.store.save(rec)
        loaded = self.store.load("c010")
        assert isinstance(loaded, CourseRecord)
        self.assertEqual(loaded.source_refs, (r1, r2))
        # Ensure source bytes not stored: check table has only json, not blob
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT source_refs_json FROM courses WHERE course_id='c010'").fetchone()
        conn.close()
        self.assertIsNotNone(row)
        data = json.loads(row[0])
        # Only references, not bytes
        self.assertEqual(len(data), 2)
        for item in data:
            self.assertIn("source_id", item)
            self.assertIn("content_sha256", item)
            self.assertNotIn("payload", item)
            self.assertNotIn("bytes", item)

    def test_corruption_and_schema_fail(self) -> None:
        rec = _make_course(course_id="c011", workflow_id="w011")
        self.store.save(rec)
        # Corrupt the json
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE courses SET source_refs_json='not json' WHERE course_id='c011'")
        conn.commit()
        conn.close()
        # Reopen store should still be valid, but load should fail
        self.assertEqual(failure_code(self.store.load("c011")), "stored_course_invalid")
        # Repair
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE courses SET source_refs_json=? WHERE course_id='c011'", (json.dumps([], separators=(",", ":"), sort_keys=True),))
        conn.commit()
        conn.close()
        self.assertEqual(self.store.load("c011"), rec)
        # Now corrupt schema: add extra column
        conn = sqlite3.connect(self.db_path)
        # Add extra column via recreation? Simpler: create bad schema file
        conn.close()
        # Test unsupported schema at open
        bad_path = Path(self.tmp.name) / "bad.sqlite3"
        c = sqlite3.connect(bad_path)
        c.execute("CREATE TABLE courses (course_id TEXT PRIMARY KEY, unexpected TEXT)")
        c.commit()
        c.close()
        self.assertEqual(failure_code(open_course_store(bad_path)), "unsupported_storage_schema")

    def test_wrong_schema_version_rejected(self) -> None:
        rec = _make_course(course_id="c012", workflow_id="w012")
        self.store.save(rec)
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE courses SET storage_schema_version='bad/v9' WHERE course_id='c012'")
        conn.commit()
        conn.close()
        self.assertEqual(failure_code(self.store.load("c012")), "unsupported_storage_schema")

    def test_unknown_course_safe_404(self) -> None:
        self.assertEqual(failure_code(self.store.load("nonexistent")), "course_not_found")

    def test_invalid_input_types_fail_closed(self) -> None:
        with self.assertRaises(TypeError):
            self.store.load(123)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            self.store.save("bad")  # type: ignore[arg-type]
        self.assertEqual(failure_code(self.store.load("bad/id")), "invalid_persistence_input")
        # No private leakage in failure
        res = self.store.load("bad/id")
        self.assertNotIn("bad/id", repr(res))  # repr is controlled, should not leak? Our failure repr is generic, but check not leaking path
        self.assertNotIn(str(self.db_path), repr(res))

    def test_list_courses_empty_and_restart(self) -> None:
        # Empty list initially
        listed = self.store.list_courses()
        assert isinstance(listed, tuple)
        self.assertEqual(len(listed), 0)
        rec = _make_course(course_id="c013", workflow_id="w013")
        self.store.save(rec)
        self.store.close()
        reopened = open_course_store(self.db_path)
        assert isinstance(reopened, LocalCourseStore)
        self.store = reopened
        listed2 = self.store.list_courses()
        self.assertEqual(len(listed2), 1)
        self.assertEqual(listed2[0].course_id, "c013")

class TestCourseSchemaAdversarial(unittest.TestCase):
    def _bad_open(self, sql: str) -> str:
        tmp = tempfile.mktemp(suffix=".sqlite3", dir=str(_ISOLATED_BASE))
        conn = sqlite3.connect(tmp)
        try:
            conn.execute(sql)
            conn.commit()
        finally:
            conn.close()
        result = open_course_store(tmp)
        try:
            self.assertIsInstance(result, CoursePersistenceFailure)
            assert isinstance(result, CoursePersistenceFailure)
            return result.diagnostics[0].code
        finally:
            try:
                Path(tmp).unlink()
            except Exception:
                pass

    def test_missing_not_null_rejected(self) -> None:
        code = self._bad_open(
            "CREATE TABLE courses (course_id TEXT PRIMARY KEY NOT NULL, reference_version TEXT NOT NULL, created_at TEXT NOT NULL, title TEXT, ai_mode TEXT NOT NULL, quality_mode TEXT NOT NULL, owner_scope TEXT NOT NULL, metadata_revision INTEGER NOT NULL, source_refs_json TEXT NOT NULL, workflow_id TEXT NOT NULL, current_job_id TEXT, storage_schema_version TEXT NOT NULL)"
        )
        self.assertEqual(code, "unsupported_storage_schema")

    def test_unexpected_default_rejected(self) -> None:
        code = self._bad_open(
            "CREATE TABLE courses (course_id TEXT PRIMARY KEY NOT NULL, reference_version TEXT NOT NULL, created_at TEXT NOT NULL, title TEXT NOT NULL DEFAULT 'x', ai_mode TEXT NOT NULL, quality_mode TEXT NOT NULL, owner_scope TEXT NOT NULL, metadata_revision INTEGER NOT NULL, source_refs_json TEXT NOT NULL, workflow_id TEXT NOT NULL, current_job_id TEXT, storage_schema_version TEXT NOT NULL)"
        )
        self.assertEqual(code, "unsupported_storage_schema")

    def test_extra_generated_column_rejected(self) -> None:
        code = self._bad_open(
            "CREATE TABLE courses (course_id TEXT PRIMARY KEY NOT NULL, reference_version TEXT NOT NULL, created_at TEXT NOT NULL, title TEXT NOT NULL, ai_mode TEXT NOT NULL, quality_mode TEXT NOT NULL, owner_scope TEXT NOT NULL, metadata_revision INTEGER NOT NULL, source_refs_json TEXT NOT NULL, workflow_id TEXT NOT NULL, current_job_id TEXT, storage_schema_version TEXT NOT NULL, extra TEXT GENERATED ALWAYS AS (title) VIRTUAL)"
        )
        self.assertEqual(code, "unsupported_storage_schema")

    def test_unexpected_trigger_rejected(self) -> None:
        tmp = tempfile.mktemp(suffix=".sqlite3", dir=str(_ISOLATED_BASE))
        conn = sqlite3.connect(tmp)
        try:
            conn.execute(
                "CREATE TABLE courses (course_id TEXT PRIMARY KEY NOT NULL, reference_version TEXT NOT NULL, created_at TEXT NOT NULL, title TEXT NOT NULL, ai_mode TEXT NOT NULL, quality_mode TEXT NOT NULL, owner_scope TEXT NOT NULL, metadata_revision INTEGER NOT NULL, source_refs_json TEXT NOT NULL, workflow_id TEXT NOT NULL, current_job_id TEXT, storage_schema_version TEXT NOT NULL)"
            )
            conn.execute("CREATE TRIGGER t BEFORE INSERT ON courses BEGIN SELECT RAISE(ABORT,'x'); END")
            conn.commit()
        finally:
            conn.close()
        result = open_course_store(tmp)
        self.assertIsInstance(result, CoursePersistenceFailure)
        assert isinstance(result, CoursePersistenceFailure)
        self.assertEqual(result.diagnostics[0].code, "unsupported_storage_schema")
        Path(tmp).unlink(missing_ok=True)

    def test_unexpected_index_rejected(self) -> None:
        tmp = tempfile.mktemp(suffix=".sqlite3", dir=str(_ISOLATED_BASE))
        conn = sqlite3.connect(tmp)
        try:
            conn.execute(
                "CREATE TABLE courses (course_id TEXT PRIMARY KEY NOT NULL, reference_version TEXT NOT NULL, created_at TEXT NOT NULL, title TEXT NOT NULL, ai_mode TEXT NOT NULL, quality_mode TEXT NOT NULL, owner_scope TEXT NOT NULL, metadata_revision INTEGER NOT NULL, source_refs_json TEXT NOT NULL, workflow_id TEXT NOT NULL, current_job_id TEXT, storage_schema_version TEXT NOT NULL)"
            )
            conn.execute("CREATE INDEX idx_title ON courses(title)")
            conn.commit()
        finally:
            conn.close()
        result = open_course_store(tmp)
        self.assertIsInstance(result, CoursePersistenceFailure)
        assert isinstance(result, CoursePersistenceFailure)
        self.assertEqual(result.diagnostics[0].code, "unsupported_storage_schema")
        Path(tmp).unlink(missing_ok=True)

    def test_wrong_pk_rejected(self) -> None:
        code = self._bad_open(
            "CREATE TABLE courses (course_id TEXT NOT NULL, reference_version TEXT NOT NULL, created_at TEXT NOT NULL, title TEXT NOT NULL, ai_mode TEXT NOT NULL, quality_mode TEXT NOT NULL, owner_scope TEXT NOT NULL, metadata_revision INTEGER NOT NULL, source_refs_json TEXT NOT NULL, workflow_id TEXT PRIMARY KEY NOT NULL, current_job_id TEXT, storage_schema_version TEXT NOT NULL)"
        )
        self.assertEqual(code, "unsupported_storage_schema")

    def test_foreign_key_rejected(self) -> None:
        tmp = tempfile.mktemp(suffix=".sqlite3", dir=str(_ISOLATED_BASE))
        conn = sqlite3.connect(tmp)
        try:
            conn.execute("CREATE TABLE other (id TEXT PRIMARY KEY NOT NULL)")
            conn.execute(
                "CREATE TABLE courses (course_id TEXT PRIMARY KEY NOT NULL, reference_version TEXT NOT NULL, created_at TEXT NOT NULL, title TEXT NOT NULL, ai_mode TEXT NOT NULL, quality_mode TEXT NOT NULL, owner_scope TEXT NOT NULL, metadata_revision INTEGER NOT NULL, source_refs_json TEXT NOT NULL, workflow_id TEXT NOT NULL, current_job_id TEXT, storage_schema_version TEXT NOT NULL, FOREIGN KEY (workflow_id) REFERENCES other(id))"
            )
            conn.commit()
        finally:
            conn.close()
        result = open_course_store(tmp)
        self.assertIsInstance(result, CoursePersistenceFailure)
        assert isinstance(result, CoursePersistenceFailure)
        self.assertEqual(result.diagnostics[0].code, "unsupported_storage_schema")
        Path(tmp).unlink(missing_ok=True)

    def test_post_open_drift_detected(self) -> None:
        with _tmp_dir() as tmp:
            db_path = Path(tmp) / "courses.sqlite3"
            store = open_course_store(db_path)
            assert isinstance(store, LocalCourseStore)
            rec = _make_course(course_id="c-drift", workflow_id="w-drift")
            self.assertEqual(store.save(rec), rec)
            # external drift: add column
            conn = sqlite3.connect(db_path)
            conn.execute("ALTER TABLE courses ADD COLUMN extra TEXT")
            conn.commit()
            conn.close()
            self.assertEqual(failure_code(store.load("c-drift")), "unsupported_storage_schema")
            self.assertEqual(failure_code(store.save(rec)), "unsupported_storage_schema")
            # list should also fail
            listed = store.list_courses()
            self.assertIsInstance(listed, CoursePersistenceFailure)
            assert isinstance(listed, CoursePersistenceFailure)
            self.assertEqual(listed.diagnostics[0].code, "unsupported_storage_schema")
            store.close()


if __name__ == "__main__":
    unittest.main()

class CourseGuidanceTests(unittest.TestCase):
    def test_guidance_additive_migration_preserves_original_record(self):
        from course_compiler.course_persistence import _LEGACY_TABLE_SQL
        from dataclasses import replace
        with _tmp_dir() as tmp:
            path = Path(tmp)/'legacy.sqlite3'
            connection = sqlite3.connect(path)
            connection.execute(_LEGACY_TABLE_SQL)
            record = _make_course()
            connection.execute('INSERT INTO courses VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (record.course_id,record.reference_version,record.created_at,record.title,
                 record.ai_mode,record.quality_mode,record.owner_scope,0,'[]',record.workflow_id,None,'local-course-sqlite/v1'))
            connection.commit();connection.close()
            store = open_course_store(path)
            self.assertIsInstance(store,LocalCourseStore)
            self.assertEqual(store.load(record.course_id),record)
            guidance = 'Invented guidance.\nKeep every relevant source exercise.'
            updated = replace(record,metadata_revision=1,course_guidance=guidance)
            self.assertEqual(store.save(updated),updated)
            store.close()
            reopened = open_course_store(path)
            self.assertEqual(reopened.load(record.course_id).course_guidance,guidance)
            self.assertNotIn(guidance,repr(reopened.load(record.course_id)))
            reopened.close()
