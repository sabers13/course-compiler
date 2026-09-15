"""Focused tests for T049 semantic-work persistence + leases + results + recovery."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from course_compiler.semantic_work import (
    SEMANTIC_KINDS,
    SEMANTIC_WORK_REQUEST_VERSION,
    SEMANTIC_WORK_RESULT_VERSION,
    ProducedArtifactSpec,
    ProducedDocumentSpec,
    SemanticDiagnostic,
    SemanticWorkRequest,
    SemanticWorkResult,
)
from course_compiler.semantic_work_persistence import (
    SEMANTIC_WORK_PERSISTENCE_SCHEMA_VERSION,
    LocalSemanticWorkStore,
    SemanticAcceptedRecord,
    SemanticArtifactRef,
    SemanticDocumentRef,
    SemanticWorkPersistenceFailure,
    envelope_for_result,
    open_semantic_work_store,
)

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_ISOLATED_BASE = REPOSITORY_ROOT / "local-data" / "test-tmp-sem-persist"
_ISOLATED_BASE.mkdir(parents=True, exist_ok=True)


def _tmp_dir() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(dir=str(_ISOLATED_BASE), prefix="persist-")


def _make_request(
    request_id: str = "creq1",
    job_id: str = "cjob1",
    workflow_id: str = "w1",
    expected_revision: int = 0,
    operation_id: str = "op-1",
    kind: str = "source_assessment",
    input_refs: tuple[str, ...] = (),
    created_at: str = "2026-01-01T00:00:00Z",
    lease_expires_at: str | None = None,
) -> SemanticWorkRequest:
    return SemanticWorkRequest(
        SEMANTIC_WORK_REQUEST_VERSION,  # type: ignore[arg-type]
        request_id,
        job_id,
        workflow_id,
        expected_revision,
        operation_id,
        kind,  # type: ignore[arg-type]
        input_refs,
        created_at,
        lease_expires_at,
    )


def _make_result(
    request_id: str = "creq1",
    operation_id: str = "op-1",
    kind: str = "source_assessment",
    request_revision: int = 0,
    priority_hash: str | None = None,
    map_hash: str | None = None,
    candidate_hash: str | None = None,
    artifacts: tuple[ProducedArtifactSpec, ...] = (),
    documents: tuple[ProducedDocumentSpec, ...] = (),
) -> SemanticWorkResult:
    return SemanticWorkResult(
        SEMANTIC_WORK_RESULT_VERSION,  # type: ignore[arg-type]
        request_id,
        operation_id,
        kind,  # type: ignore[arg-type]
        artifacts,
        documents,
        (),
        priority_hash,
        map_hash,
        candidate_hash,
        request_revision,
    )


def failure_code(result: object) -> str:
    assert isinstance(result, SemanticWorkPersistenceFailure)
    return result.diagnostics[0].code


class TestSemanticContracts(unittest.TestCase):
    def test_frozen_slotted_and_repr_redacted(self) -> None:
        req = _make_request()
        self.assertEqual(req.request_version, SEMANTIC_WORK_REQUEST_VERSION)
        self.assertFalse(hasattr(req, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            req.kind = "lecture_generation"  # type: ignore[misc]
        r = repr(req)
        self.assertIn("creq1", r)
        self.assertIn("source_assessment", repr(SemanticWorkResult("semantic-work-result/v1", "creq1", "op-1", "source_assessment", (), (), (), None, None, None, 0)))
        art = ProducedArtifactSpec("source_assessment", "art-1", b"secret bytes")
        res = _make_result(artifacts=(art,))
        self.assertNotIn("secret bytes", repr(res))
        self.assertNotIn("secret bytes", repr(art))
        doc = ProducedDocumentSpec("l1", "secret document content")
        res2 = _make_result(documents=(doc,), kind="lecture_generation", candidate_hash="a" * 64, request_revision=0)
        self.assertNotIn("secret document", repr(res2))
        self.assertNotIn("secret document", repr(doc))

    def test_valid_kinds(self) -> None:
        for kind in SEMANTIC_KINDS:
            req = _make_request(kind=kind)
            self.assertEqual(req.kind, kind)
        with self.assertRaises(ValueError):
            _make_request(kind="invalid_kind")  # type: ignore[arg-type]

    def test_required_field_validations(self) -> None:
        with self.assertRaises(ValueError):
            _make_request(expected_revision=-1)
        with self.assertRaises(ValueError):
            _make_request(lease_expires_at="not-a-date")
        with self.assertRaises(ValueError):
            _make_request(lease_expires_at="2026-01-01T00:00:00")  # missing Z


class TestEnvelope(unittest.TestCase):
    def test_envelope_records_only_hashes(self) -> None:
        art = ProducedArtifactSpec("source_assessment", "art-1", b"plaintext bytes")
        doc = ProducedDocumentSpec("l1", "sensitive document text")
        result = _make_result(artifacts=(art,), documents=(doc,), kind="lecture_generation", candidate_hash="a" * 64)
        envelope = envelope_for_result(result)
        self.assertEqual(envelope.artifact_refs[0].content_sha256, hashlib.sha256(b"plaintext bytes").hexdigest())
        self.assertEqual(envelope.document_refs[0].content_sha256, hashlib.sha256(b"sensitive document text").hexdigest())
        # No bytes/text in envelope repr or serialization
        text_repr = repr(envelope)
        self.assertNotIn("plaintext", text_repr)
        self.assertNotIn("sensitive", text_repr)
        json_blob = json.dumps({
            "kind": envelope.kind,
            "artifact_refs": [{"kind": r.kind, "artifact_id": r.artifact_id, "content_sha256": r.content_sha256} for r in envelope.artifact_refs],
            "document_refs": [{"lecture_id": r.lecture_id, "content_sha256": r.content_sha256} for r in envelope.document_refs],
        })
        self.assertNotIn("plaintext", json_blob)
        self.assertNotIn("sensitive", json_blob)


class TestSchemaValidation(unittest.TestCase):
    def _bad_open(self, bad_sql: str) -> str:
        with _tmp_dir() as tmp:
            db = Path(tmp) / "x.sqlite3"
            raw = sqlite3.connect(db)
            raw.executescript(bad_sql)
            raw.commit()
            raw.close()
            res = open_semantic_work_store(db)
            if not isinstance(res, SemanticWorkPersistenceFailure):
                res.close()  # type: ignore[attr-defined]
                return "opened_unexpectedly"
            return res.diagnostics[0].code

    def test_open_recognized(self) -> None:
        with _tmp_dir() as tmp:
            db = Path(tmp) / "good.sqlite3"
            res = open_semantic_work_store(db)
            self.assertIsInstance(res, LocalSemanticWorkStore)
            res.close()  # type: ignore[attr-defined]

    def test_missing_lease_holder_column(self) -> None:
        # missing column
        code = self._bad_open(
            "CREATE TABLE semantic_work_requests (request_id TEXT PRIMARY KEY NOT NULL, job_id TEXT NOT NULL, workflow_id TEXT NOT NULL, expected_revision INTEGER NOT NULL, operation_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, status TEXT NOT NULL, request_json TEXT NOT NULL, result_json TEXT, created_at TEXT NOT NULL, lease_expires_at TEXT, storage_schema_version TEXT NOT NULL)"
        )
        self.assertEqual(code, "unsupported_storage_schema")

    def test_extra_column(self) -> None:
        code = self._bad_open(
            "CREATE TABLE semantic_work_requests (request_id TEXT PRIMARY KEY NOT NULL, job_id TEXT NOT NULL, workflow_id TEXT NOT NULL, expected_revision INTEGER NOT NULL, operation_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, status TEXT NOT NULL, request_json TEXT NOT NULL, result_json TEXT, created_at TEXT NOT NULL, lease_expires_at TEXT, lease_holder TEXT, storage_schema_version TEXT NOT NULL, extra TEXT)"
        )
        self.assertEqual(code, "unsupported_storage_schema")

    def test_wrong_pk(self) -> None:
        code = self._bad_open(
            "CREATE TABLE semantic_work_requests (request_id TEXT NOT NULL, job_id TEXT PRIMARY KEY NOT NULL, workflow_id TEXT NOT NULL, expected_revision INTEGER NOT NULL, operation_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, status TEXT NOT NULL, request_json TEXT NOT NULL, result_json TEXT, created_at TEXT NOT NULL, lease_expires_at TEXT, lease_holder TEXT, storage_schema_version TEXT NOT NULL)"
        )
        self.assertEqual(code, "unsupported_storage_schema")

    def test_post_open_drift(self) -> None:
        with _tmp_dir() as tmp:
            db_path = Path(tmp) / "semantic.sqlite3"
            store = open_semantic_work_store(db_path)
            assert isinstance(store, LocalSemanticWorkStore)
            req = _make_request()
            store.create_request(req)
            conn = sqlite3.connect(db_path)
            conn.execute("ALTER TABLE semantic_work_requests ADD COLUMN extra TEXT")
            conn.commit()
            conn.close()
            self.assertEqual(failure_code(store.load("creq1")), "unsupported_storage_schema")
            self.assertEqual(failure_code(store.create_request(_make_request(request_id="creq2", operation_id="op-2"))), "unsupported_storage_schema")
            store.close()

    def test_exact_schema_matches_production_sql_and_xinfo(self) -> None:
        with _tmp_dir() as tmp:
            db_path = Path(tmp) / "semantic.sqlite3"
            store = open_semantic_work_store(db_path)
            self.assertIsInstance(store, LocalSemanticWorkStore)
            store.close()  # type: ignore[attr-defined]

            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            xinfo = cursor.execute("PRAGMA table_xinfo(semantic_work_requests)").fetchall()
            conn.close()

            # Columns in exact order: (cid, name, type, notnull, dflt_value, pk, hidden)
            col_names = [row[1] for row in xinfo]
            expected_names = [
                "request_id",
                "job_id",
                "workflow_id",
                "expected_revision",
                "operation_id",
                "kind",
                "status",
                "request_json",
                "result_json",
                "created_at",
                "lease_expires_at",
                "lease_holder",
                "storage_schema_version",
            ]
            self.assertEqual(col_names, expected_names)
            self.assertNotIn("input_refs_json", col_names)

            # Check nullability: only result_json, lease_expires_at, lease_holder are nullable
            nullable = {row[1] for row in xinfo if row[3] == 0}
            self.assertEqual(nullable, {"result_json", "lease_expires_at", "lease_holder"})

            # Check PK: only request_id
            pk_cols = [row[1] for row in xinfo if row[5] == 1]
            self.assertEqual(pk_cols, ["request_id"])

            # Check indexes: only SQLite autoindexes for PK and UNIQUE(operation_id)
            conn = sqlite3.connect(db_path)
            idx_list = conn.execute("PRAGMA index_list(semantic_work_requests)").fetchall()
            idx_names = {row[1] for row in idx_list}
            self.assertTrue(all(name.startswith("sqlite_autoindex_") for name in idx_names))
            self.assertNotIn("idx_semantic_work_job", idx_names)

            # Check triggers and foreign keys: none
            triggers = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='semantic_work_requests'"
            ).fetchall()
            self.assertEqual(triggers, [])
            fks = conn.execute("PRAGMA foreign_key_list(semantic_work_requests)").fetchall()
            self.assertEqual(fks, [])
            conn.close()


class TestHolderBoundLeasing(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _tmp_dir()
        self.db_path = Path(self.tmp.name) / "semantic.sqlite3"
        self.store = open_semantic_work_store(self.db_path)
        assert isinstance(self.store, LocalSemanticWorkStore)

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_one_active_lease_winner_across_connections(self) -> None:
        self.store.create_request(_make_request())
        store2 = open_semantic_work_store(self.db_path)
        assert isinstance(store2, LocalSemanticWorkStore)
        leased1 = self.store.acquire_lease("creq1", lease_expires_at="2026-01-01T00:05:00Z", now_iso="2026-01-01T00:00:00Z", holder_id="h-a")
        self.assertIsInstance(leased1, SemanticWorkRequest)
        leased2 = store2.acquire_lease("creq1", lease_expires_at="2026-01-01T00:05:00Z", now_iso="2026-01-01T00:00:00Z", holder_id="h-b")
        self.assertIsInstance(leased2, SemanticWorkPersistenceFailure)
        self.assertEqual(leased2.diagnostics[0].code, "lease_conflict")
        store2.close()

    def test_unexpired_lease_cannot_be_stolen_by_different_holder(self) -> None:
        self.store.create_request(_make_request())
        self.store.acquire_lease("creq1", lease_expires_at="2026-01-01T00:05:00Z", now_iso="2026-01-01T00:00:00Z", holder_id="h-a")
        bad = self.store.acquire_lease("creq1", lease_expires_at="2026-01-01T00:10:00Z", now_iso="2026-01-01T00:00:00Z", holder_id="h-b")
        self.assertEqual(failure_code(bad), "lease_conflict")

    def test_same_holder_renews_idempotently(self) -> None:
        self.store.create_request(_make_request())
        first = self.store.acquire_lease("creq1", lease_expires_at="2026-01-01T00:05:00Z", now_iso="2026-01-01T00:00:00Z", holder_id="h-a")
        assert isinstance(first, SemanticWorkRequest)
        second = self.store.acquire_lease("creq1", lease_expires_at="2026-01-01T00:10:00Z", now_iso="2026-01-01T00:00:00Z", holder_id="h-a")
        assert isinstance(second, SemanticWorkRequest)
        self.assertEqual(second.lease_expires_at, "2026-01-01T00:10:00Z")

    def test_expired_lease_reoffers_same_request(self) -> None:
        self.store.create_request(_make_request())
        self.store.acquire_lease("creq1", lease_expires_at="2026-01-01T00:05:00Z", now_iso="2026-01-01T00:00:00Z", holder_id="h-a")
        reoffered = self.store.reoffer_expired("cjob1", now_iso="2026-01-01T01:00:00Z", holder_id="h-b")
        assert isinstance(reoffered, SemanticWorkRequest)
        self.assertEqual(reoffered.request_id, "creq1")
        self.assertEqual(reoffered.expected_revision, 0)
        self.assertEqual(reoffered.operation_id, "op-1")
        self.assertEqual(reoffered.kind, "source_assessment")
        self.assertGreater(reoffered.lease_expires_at, "2026-01-01T01:00:00Z")

    def test_release_lease_clears_columns_and_json(self) -> None:
        self.store.create_request(_make_request())
        self.store.acquire_lease("creq1", lease_expires_at="2026-01-01T00:05:00Z", now_iso="2026-01-01T00:00:00Z", holder_id="h-a")
        cleared = self.store.release_lease("creq1")
        self.assertIsInstance(cleared, SemanticWorkRequest)
        assert isinstance(cleared, SemanticWorkRequest)
        self.assertIsNone(cleared.lease_expires_at)
        # Reopening must still decode the row.
        self.store.close()
        reopened = open_semantic_work_store(self.db_path)
        assert isinstance(reopened, LocalSemanticWorkStore)
        loaded = reopened.load("creq1")
        self.assertIsInstance(loaded, SemanticWorkRequest)
        info = reopened.lease_info("creq1")
        self.assertIsInstance(info, tuple)
        self.assertEqual(info[0], "pending")
        self.assertIsNone(info[1])
        self.assertIsNone(info[2])
        reopened.close()
        self.store = open_semantic_work_store(self.db_path)
        assert isinstance(self.store, LocalSemanticWorkStore)

    def test_lease_timestamp_parsed_not_lex(self) -> None:
        # Fractional expiry must compare correctly against a later now.
        self.store.create_request(_make_request())
        leased = self.store.acquire_lease("creq1", lease_expires_at="2026-01-01T00:00:00.1Z", now_iso="2026-01-01T00:00:00Z", holder_id="h-a")
        self.assertIsInstance(leased, SemanticWorkRequest)
        # now == expiry: NOT active (active means strictly before).
        again = self.store.acquire_lease("creq1", lease_expires_at="2026-01-01T00:00:00.2Z", now_iso="2026-01-01T00:00:00.1Z", holder_id="h-b")
        self.assertIsInstance(again, SemanticWorkRequest)


class TestTimestampParsing(unittest.TestCase):
    def test_fractional_seconds_compare_chronologically(self) -> None:
        from course_compiler.semantic_work_persistence import _is_lease_active, _parse_created_at
        self.assertTrue(_is_lease_active("2026-01-01T00:00:00.001Z", "2026-01-01T00:00:00Z"))
        self.assertFalse(_is_lease_active("2026-01-01T00:00:00.001Z", "2026-01-01T00:00:00.01Z"))
        self.assertTrue(_is_lease_active("2026-01-01T00:00:00.999999Z", "2026-01-01T00:00:00.999998Z"))
        self.assertTrue(_is_lease_active("2026-01-01T00:00:01Z", "2026-01-01T00:00:00.999999Z"))
        self.assertFalse(_is_lease_active("2026-01-01T00:00:00.999999Z", "2026-01-01T00:00:01Z"))
        # Rollover
        self.assertTrue(_is_lease_active("2026-01-01T00:01:00Z", "2026-01-01T00:00:59.999999Z"))
        # Exact boundary: now == expiry -> NOT active
        self.assertFalse(_is_lease_active("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
        # Strict less-than
        with self.assertRaises(ValueError):
            _parse_created_at("not-a-date")


class TestPrivacyPersistence(unittest.TestCase):
    def test_no_produced_bytes_in_sqlite(self) -> None:
        with _tmp_dir() as tmp:
            db_path = Path(tmp) / "semantic.sqlite3"
            store = open_semantic_work_store(db_path)
            assert isinstance(store, LocalSemanticWorkStore)
            store.create_request(_make_request())
            store.acquire_lease("creq1", lease_expires_at="2026-01-01T00:05:00Z", now_iso="2026-01-01T00:00:00Z", holder_id="h-a")
            art = ProducedArtifactSpec("source_assessment", "art-1", b"private-artifact-bytes-XYZ")
            doc = ProducedDocumentSpec("l1", "private-document-text-XYZ")
            res = _make_result(artifacts=(art,), documents=(doc,))
            marked = store.mark_accepted(res)
            self.assertIsInstance(marked, SemanticAcceptedRecord)
            store.close()
            conn = sqlite3.connect(db_path)
            try:
                for table in ("semantic_work_requests",):
                    for row in conn.execute(f"SELECT * FROM {table}").fetchall():
                        text = " ".join(str(x) for x in row if x is not None)
                        self.assertNotIn("private-artifact-bytes", text)
                        self.assertNotIn("private-document-text", text)
                        self.assertNotIn("XYZ", text)
            finally:
                conn.close()


class TestResults(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _tmp_dir()
        self.db_path = Path(self.tmp.name) / "semantic.sqlite3"
        self.store = open_semantic_work_store(self.db_path)
        assert isinstance(self.store, LocalSemanticWorkStore)

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_correct_result_accepted(self) -> None:
        req = _make_request(kind="lecture_generation", input_refs=("l1",))
        self.store.create_request(req)
        self.store.acquire_lease("creq1", lease_expires_at="2026-01-01T00:05:00Z", now_iso="2026-01-01T00:00:00Z", holder_id="h-a")
        doc = ProducedDocumentSpec("l1", "# Lecture l1\n\nContent")
        from course_compiler.rendering import DocumentReference
        from course_compiler.workflow import candidate_subject_sha256

        sha = hashlib.sha256("# Lecture l1\n\nContent".encode()).hexdigest()
        ref = DocumentReference("lecture-document/v1", "l1", 1, sha)
        cand = candidate_subject_sha256(ref)
        res = _make_result(kind="lecture_generation", documents=(doc,), candidate_hash=cand, request_revision=0)
        marked = self.store.mark_accepted(res)
        self.assertIsInstance(marked, SemanticAcceptedRecord)
        loaded = self.store.load("creq1")
        self.assertIsInstance(loaded, SemanticAcceptedRecord)
        # Round-tripped envelope must match the input
        self.assertEqual(loaded, marked)

    def test_exact_envelope_idempotent(self) -> None:
        req = _make_request()
        self.store.create_request(req)
        self.store.acquire_lease("creq1", lease_expires_at="2026-01-01T00:05:00Z", now_iso="2026-01-01T00:00:00Z", holder_id="h-a")
        res = _make_result()
        first = self.store.mark_accepted(res)
        assert isinstance(first, SemanticAcceptedRecord)
        second = self.store.mark_accepted(res)
        assert isinstance(second, SemanticAcceptedRecord)
        self.assertEqual(second, first)

    def test_conflicting_envelope_rejected(self) -> None:
        req = _make_request()
        self.store.create_request(req)
        self.store.acquire_lease("creq1", lease_expires_at="2026-01-01T00:05:00Z", now_iso="2026-01-01T00:00:00Z", holder_id="h-a")
        res = _make_result()
        self.store.mark_accepted(res)
        bad = _make_result(kind="lecture_generation", documents=(ProducedDocumentSpec("l1", "hi"),), candidate_hash="a" * 64)
        self.assertEqual(failure_code(self.store.mark_accepted(bad)), "immutable_identity_conflict")

    def test_operation_id_mismatch(self) -> None:
        req = _make_request()
        self.store.create_request(req)
        self.store.acquire_lease("creq1", lease_expires_at="2026-01-01T00:05:00Z", now_iso="2026-01-01T00:00:00Z", holder_id="h-a")
        bad = _make_result(operation_id="op-bad")
        self.assertEqual(failure_code(self.store.mark_accepted(bad)), "immutable_identity_conflict")

    def test_kind_mismatch(self) -> None:
        req = _make_request(kind="source_assessment")
        self.store.create_request(req)
        self.store.acquire_lease("creq1", lease_expires_at="2026-01-01T00:05:00Z", now_iso="2026-01-01T00:00:00Z", holder_id="h-a")
        bad = _make_result(kind="lecture_generation", documents=(ProducedDocumentSpec("l1", "hi"),), candidate_hash="a" * 64)
        self.assertEqual(failure_code(self.store.mark_accepted(bad)), "immutable_identity_conflict")

    def test_expected_revision_mismatch(self) -> None:
        req = _make_request(expected_revision=0)
        self.store.create_request(req)
        self.store.acquire_lease("creq1", lease_expires_at="2026-01-01T00:05:00Z", now_iso="2026-01-01T00:00:00Z", holder_id="h-a")
        bad = _make_result(request_revision=1)
        self.assertEqual(failure_code(self.store.mark_accepted(bad)), "immutable_identity_conflict")

    def test_invalid_produced_reference(self) -> None:
        with self.assertRaises(ValueError):
            ProducedArtifactSpec("source_assessment", "bad/id", b"data")


class TestLoadCurrent(unittest.TestCase):
    def test_stale_pending_not_returned(self) -> None:
        with _tmp_dir() as tmp:
            db_path = Path(tmp) / "semantic.sqlite3"
            store = open_semantic_work_store(db_path)
            assert isinstance(store, LocalSemanticWorkStore)
            # pending row at revision 0
            store.create_request(_make_request(expected_revision=0))
            store.acquire_lease("creq1", lease_expires_at="2026-01-01T00:05:00Z", now_iso="2026-01-01T00:00:00Z", holder_id="h-a")
            current = store.load_current_for_job("cjob1", workflow_id="w1", expected_revision=1)
            self.assertIsNone(current)
            # matching revision returns the row
            current_match = store.load_current_for_job("cjob1", workflow_id="w1", expected_revision=0)
            self.assertIsInstance(current_match, SemanticWorkRequest)
            store.close()


class TestRecovery(unittest.TestCase):
    def test_crash_boundaries(self) -> None:
        with _tmp_dir() as tmp:
            db_path = Path(tmp) / "semantic.sqlite3"
            store = open_semantic_work_store(db_path)
            assert isinstance(store, LocalSemanticWorkStore)
            store.create_request(_make_request())
            store.close()
            store2 = open_semantic_work_store(db_path)
            assert isinstance(store2, LocalSemanticWorkStore)
            loaded = store2.load("creq1")
            self.assertIsInstance(loaded, SemanticWorkRequest)
            store2.acquire_lease("creq1", lease_expires_at="2026-01-01T00:05:00Z", now_iso="2026-01-01T00:00:00Z", holder_id="h-a")
            store2.close()
            store3 = open_semantic_work_store(db_path)
            assert isinstance(store3, LocalSemanticWorkStore)
            loaded2 = store3.load("creq1")
            self.assertIsInstance(loaded2, SemanticWorkRequest)
            assert isinstance(loaded2, SemanticWorkRequest)
            self.assertEqual(loaded2.lease_expires_at, "2026-01-01T00:05:00Z")
            res = _make_result()
            store3.mark_accepted(res)
            store3.close()
            store4 = open_semantic_work_store(db_path)
            loaded3 = store4.load("creq1")
            self.assertIsInstance(loaded3, SemanticAcceptedRecord)
            store4.close()


if __name__ == "__main__":
    unittest.main()
