from __future__ import annotations

import ast
import hashlib
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from unittest import mock

from course_compiler import (
    LECTURE_DOCUMENT_PERSISTENCE_SCHEMA_VERSION,
    LocalLectureDocumentStore,
    LocalSourceEvidenceStore,
    LocalWorkflowArtifactStore,
    LocalWorkflowStateStore,
    LectureDocument,
    LectureDocumentPersistenceDiagnostic,
    LectureDocumentPersistenceFailure,
    SourceEvidencePayload,
    SourceEvidenceReference,
    SourceProvenance,
    WorkflowArtifactPayload,
    WorkflowArtifactReference,
    candidate_subject_sha256,
    document_reference,
    open_lecture_document_store,
    open_source_evidence_store,
    open_workflow_artifact_store,
    open_workflow_state_store,
    validate_document,
)
from course_compiler import lecture_document_persistence
from course_compiler.rendering import DocumentReference
from course_compiler.workflow_policy import ARTIFACT_PRODUCERS


def lecture(
    source_text: str,
    *,
    document_id: str = "l1",
    order: int = 1,
) -> LectureDocument:
    digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    return LectureDocument(
        "lecture-document/v1",
        document_id,
        order,
        source_text,
        SourceProvenance(digest),
    )


def require_reference(document: LectureDocument) -> DocumentReference:
    reference = document_reference(document)
    if not isinstance(reference, DocumentReference):
        raise AssertionError("valid synthetic document reference required")
    return reference


def failure_code(result: object) -> str:
    if not isinstance(result, LectureDocumentPersistenceFailure):
        raise AssertionError(
            f"lecture document persistence failure required, got {type(result).__name__}"
        )
    return result.diagnostics[0].code


class LectureDocumentPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="document-store-")
        self.database_path = Path(self._temporary.name) / "lecture-documents.sqlite"
        self.store = open_lecture_document_store(self.database_path)
        assert isinstance(self.store, LocalLectureDocumentStore)

    def tearDown(self) -> None:
        self.store.close()
        self._temporary.cleanup()

    def _replace_row(self, **replacement: object) -> None:
        columns = (
            "contract_version",
            "document_id",
            "document_order",
            "content_sha256",
            "storage_schema_version",
            "source_text",
        )
        connection = sqlite3.connect(self.database_path)
        try:
            row = connection.execute(
                "SELECT contract_version, document_id, document_order, content_sha256, "
                "storage_schema_version, source_text FROM lecture_documents"
            ).fetchone()
            assert row is not None
            values = [replacement.get(name, value) for name, value in zip(columns, row)]
            connection.execute("DELETE FROM lecture_documents")
            connection.execute(
                "INSERT INTO lecture_documents VALUES (?, ?, ?, ?, ?, ?)", values
            )
            connection.commit()
        finally:
            connection.close()

    def _insert_raw(self, reference: DocumentReference, payload: bytes) -> None:
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "INSERT INTO lecture_documents VALUES (?, ?, ?, ?, ?, ?)",
                (
                    reference.contract_version,
                    reference.document_id,
                    reference.order,
                    reference.content_sha256,
                    LECTURE_DOCUMENT_PERSISTENCE_SCHEMA_VERSION,
                    payload,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def test_public_records_are_fixed_frozen_and_content_safe(self) -> None:
        self.assertEqual(
            LECTURE_DOCUMENT_PERSISTENCE_SCHEMA_VERSION,
            "local-lecture-document-sqlite/v1",
        )
        self.assertEqual(
            [item.name for item in fields(LectureDocumentPersistenceDiagnostic)],
            ["code", "classification", "message"],
        )
        self.assertEqual(
            [item.name for item in fields(LectureDocumentPersistenceFailure)],
            ["status", "diagnostics"],
        )
        diagnostic = LectureDocumentPersistenceDiagnostic(
            "document_not_found", "storage", "No stored lecture document was found."
        )
        self.assertFalse(hasattr(diagnostic, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            diagnostic.code = "other"  # type: ignore[misc]
        secret = "invented-private-lecture-marker"
        expected = lecture(secret)
        self.assertNotIn(secret, repr(expected))
        result = self.store.load(require_reference(expected))
        self.assertNotIn(secret, repr(result))

    def test_exact_round_trip_reference_and_utf8_blob_through_fresh_store(self) -> None:
        source_text = "# Invented café ∑\n\nLine with tabs\tand emoji 🧪.\n"
        expected = lecture(source_text)
        reference = require_reference(expected)
        self.assertEqual(self.store.save(expected), reference)
        connection = sqlite3.connect(self.database_path)
        try:
            stored = connection.execute(
                "SELECT typeof(source_text), source_text FROM lecture_documents"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(stored, ("blob", source_text.encode("utf-8")))
        self.store.close()
        reopened = open_lecture_document_store(self.database_path)
        assert isinstance(reopened, LocalLectureDocumentStore)
        self.store = reopened
        loaded = reopened.load(reference)
        self.assertEqual(loaded, expected)
        assert isinstance(loaded, LectureDocument)
        self.assertEqual(loaded.source_text.encode("utf-8"), source_text.encode("utf-8"))

    def test_warning_only_documents_are_persistable_without_weakening_validation(self) -> None:
        expected = lecture("# Invented short source\n")
        diagnostics = validate_document(expected)
        self.assertTrue(diagnostics)
        self.assertTrue(all(item.severity == "warning" for item in diagnostics))
        reference = require_reference(expected)
        self.assertEqual(self.store.save(expected), reference)
        self.assertEqual(self.store.load(reference), expected)

    def test_wrong_types_missing_fields_and_validation_errors_fail_before_mutation(self) -> None:
        with self.assertRaises(TypeError):
            self.store.save("invented source")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            self.store.load("l1")  # type: ignore[arg-type]
        forged_reference = object.__new__(DocumentReference)
        object.__setattr__(
            forged_reference, "contract_version", "lecture-document/v1"
        )
        self.assertEqual(
            failure_code(self.store.load(forged_reference)),
            "invalid_persistence_input",
        )
        forged = object.__new__(LectureDocument)
        object.__setattr__(forged, "contract_version", "lecture-document/v1")
        self.assertEqual(
            failure_code(self.store.save(forged)), "invalid_persistence_input"
        )
        invalid_text = "invented invalid\rtext"
        invalid = LectureDocument(
            "lecture-document/v1",
            "l1",
            1,
            invalid_text,
            SourceProvenance(hashlib.sha256(invalid_text.encode("utf-8")).hexdigest()),
        )
        self.assertEqual(
            failure_code(self.store.save(invalid)), "invalid_persistence_input"
        )
        mismatched = LectureDocument(
            "lecture-document/v1", "l1", 1, "invented", SourceProvenance("0" * 64)
        )
        self.assertEqual(
            failure_code(self.store.save(mismatched)), "invalid_persistence_input"
        )
        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM lecture_documents").fetchone(),
                (0,),
            )
        finally:
            connection.close()

    def test_complete_reference_identity_allows_same_id_and_order_with_new_digest(self) -> None:
        first = lecture("# Invented first immutable lecture\n")
        second = lecture("# Invented second immutable lecture\n")
        first_reference = require_reference(first)
        second_reference = require_reference(second)
        self.assertNotEqual(first_reference.content_sha256, second_reference.content_sha256)
        self.assertEqual(self.store.save(first), first_reference)
        self.assertEqual(self.store.save(second), second_reference)
        self.assertEqual(self.store.load(first_reference), first)
        self.assertEqual(self.store.load(second_reference), second)
        missing_reference = DocumentReference(
            "lecture-document/v1", "l1", 1, "0" * 64
        )
        self.assertEqual(
            failure_code(self.store.load(missing_reference)), "document_not_found"
        )
        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM lecture_documents WHERE document_id = 'l1' "
                    "AND document_order = 1"
                ).fetchone(),
                (2,),
            )
        finally:
            connection.close()

    def test_document_identity_is_not_candidate_subject_or_source_evidence_identity(self) -> None:
        source_text = "# Invented shared canonical source\n"
        first = lecture(source_text, document_id="l1", order=1)
        second = lecture(source_text, document_id="l2", order=2)
        first_reference = require_reference(first)
        second_reference = require_reference(second)
        self.assertEqual(first_reference.content_sha256, second_reference.content_sha256)
        self.assertNotEqual(
            candidate_subject_sha256(first_reference),
            candidate_subject_sha256(second_reference),
        )
        self.assertEqual(self.store.save(first), first_reference)
        self.assertEqual(self.store.save(second), second_reference)
        source_reference = SourceEvidenceReference(
            "source-evidence-reference/v1", "source-one", first_reference.content_sha256
        )
        with self.assertRaises(TypeError):
            self.store.load(source_reference)  # type: ignore[arg-type]

    def test_exact_repeat_is_idempotent_and_corruption_is_never_replaced(self) -> None:
        expected = lecture("# Invented immutable document\n")
        reference = require_reference(expected)
        self.assertEqual(self.store.save(expected), reference)
        self.assertEqual(self.store.save(expected), reference)
        self._replace_row(source_text=b"invented tampered bytes")
        self.assertEqual(failure_code(self.store.save(expected)), "stored_document_invalid")
        self.assertEqual(failure_code(self.store.load(reference)), "stored_document_invalid")

    def test_schema_reference_digest_blob_utf8_and_t002_corruption_fail_closed(self) -> None:
        expected = lecture("# Invented corruption target\n")
        reference = require_reference(expected)
        self.assertEqual(self.store.save(expected), reference)
        self._replace_row(storage_schema_version="unsupported-document-schema/v9")
        self.assertEqual(
            failure_code(self.store.load(reference)), "unsupported_storage_schema"
        )

        self._replace_row(
            storage_schema_version=LECTURE_DOCUMENT_PERSISTENCE_SCHEMA_VERSION,
            source_text=expected.source_text.encode("utf-8"),
            content_sha256="1" * 64,
        )
        corrupt_digest_reference = DocumentReference(
            reference.contract_version, reference.document_id, reference.order, "1" * 64
        )
        self.assertEqual(
            failure_code(self.store.load(corrupt_digest_reference)), "stored_document_invalid"
        )

        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("DELETE FROM lecture_documents")
            connection.commit()
        finally:
            connection.close()
        invalid_utf8 = b"\xffinvented"
        invalid_utf8_reference = DocumentReference(
            "lecture-document/v1",
            "l1",
            1,
            hashlib.sha256(invalid_utf8).hexdigest(),
        )
        self._insert_raw(invalid_utf8_reference, invalid_utf8)
        self.assertEqual(
            failure_code(self.store.load(invalid_utf8_reference)), "stored_document_invalid"
        )

        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("DELETE FROM lecture_documents")
            connection.commit()
        finally:
            connection.close()
        invalid = b"invented\rsource"
        invalid_t002_reference = DocumentReference(
            "lecture-document/v1",
            "l1",
            1,
            hashlib.sha256(invalid).hexdigest(),
        )
        self._insert_raw(invalid_t002_reference, invalid)
        self.assertEqual(
            failure_code(self.store.load(invalid_t002_reference)), "stored_document_invalid"
        )

    def test_expected_reference_mismatch_and_bad_table_schema_fail_closed(self) -> None:
        expected = lecture("# Invented reference target\n")
        reference = require_reference(expected)
        row = (
            reference.contract_version,
            "l2",
            reference.order,
            reference.content_sha256,
            LECTURE_DOCUMENT_PERSISTENCE_SCHEMA_VERSION,
            expected.source_text.encode("utf-8"),
        )
        self.assertEqual(
            failure_code(
                lecture_document_persistence._decode_row(
                    row, expected_reference=reference
                )
            ),
            "document_reference_mismatch",
        )
        bad_path = Path(self._temporary.name) / "bad-schema.sqlite"
        connection = sqlite3.connect(bad_path)
        try:
            connection.execute("CREATE TABLE lecture_documents (unexpected TEXT)")
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(
            failure_code(open_lecture_document_store(bad_path)),
            "unsupported_storage_schema",
        )

    def test_already_open_store_load_fails_closed_after_external_schema_drift(self) -> None:
        expected = lecture("# Invented load schema drift target\n")
        reference = require_reference(expected)
        self.assertEqual(self.store.save(expected), reference)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "ALTER TABLE lecture_documents ADD COLUMN unexpected TEXT"
            )
            connection.commit()
        finally:
            connection.close()
        result = self.store.load(reference)
        self.assertEqual(failure_code(result), "unsupported_storage_schema")
        self.assertNotIsInstance(result, LectureDocument)

    def test_already_open_store_save_rejects_schema_drift_without_new_row(self) -> None:
        first = lecture("# Invented first schema drift document\n")
        second = lecture("# Invented second schema drift document\n")
        first_reference = require_reference(first)
        second_reference = require_reference(second)
        self.assertEqual(self.store.save(first), first_reference)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "ALTER TABLE lecture_documents ADD COLUMN unexpected TEXT"
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(
            failure_code(self.store.save(second)), "unsupported_storage_schema"
        )
        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM lecture_documents WHERE contract_version = ? "
                    "AND document_id = ? AND document_order = ? AND content_sha256 = ?",
                    (
                        second_reference.contract_version,
                        second_reference.document_id,
                        second_reference.order,
                        second_reference.content_sha256,
                    ),
                ).fetchone(),
                (0,),
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM lecture_documents").fetchone(),
                (1,),
            )
        finally:
            connection.close()

    def test_failures_redact_source_path_and_ordinary_exception_text(self) -> None:
        secret = "invented-lecture-source-secret"
        expected = lecture(secret)
        reference = require_reference(expected)
        self.assertEqual(self.store.save(expected), reference)
        self._replace_row(source_text=b"tampered")
        result = self.store.load(reference)
        self.assertEqual(failure_code(result), "stored_document_invalid")
        self.assertNotIn(secret, repr(result))
        bad_path = Path(self._temporary.name) / secret / "documents.sqlite"
        result = open_lecture_document_store(bad_path)
        self.assertEqual(failure_code(result), "persistence_unavailable")
        self.assertNotIn(secret, repr(result))
        self._replace_row(source_text=expected.source_text.encode("utf-8"))
        with mock.patch.object(
            lecture_document_persistence, "_decode_row", side_effect=RuntimeError(secret)
        ):
            result = self.store.save(expected)
        self.assertEqual(failure_code(result), "persistence_exception")
        self.assertNotIn(secret, repr(result))

    def test_ordinary_exceptions_are_contained_and_baseexception_propagates(self) -> None:
        expected = lecture("# Invented exception document\n")
        reference = require_reference(expected)
        with mock.patch.object(
            lecture_document_persistence, "_validated_document", side_effect=RuntimeError("secret")
        ):
            self.assertEqual(
                failure_code(self.store.save(expected)), "invalid_persistence_input"
            )
        with mock.patch.object(
            lecture_document_persistence, "_validated_reference", side_effect=RuntimeError("secret")
        ):
            self.assertEqual(
                failure_code(self.store.load(reference)), "invalid_persistence_input"
            )
        self.assertEqual(self.store.save(expected), reference)
        with mock.patch.object(
            lecture_document_persistence, "_decode_row", side_effect=RuntimeError("secret")
        ):
            self.assertEqual(
                failure_code(self.store.load(reference)), "persistence_exception"
            )
        with mock.patch.object(
            lecture_document_persistence, "_validated_document", side_effect=KeyboardInterrupt
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.store.save(expected)
        with mock.patch.object(
            lecture_document_persistence, "_decode_row", side_effect=KeyboardInterrupt
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.store.load(reference)

    def test_separate_connection_identical_writers_are_idempotent(self) -> None:
        expected = lecture("# Invented concurrent identical document\n")
        reference = require_reference(expected)
        results: list[object] = []
        barrier = threading.Barrier(3)

        def writer() -> None:
            store = open_lecture_document_store(self.database_path)
            assert isinstance(store, LocalLectureDocumentStore)
            try:
                barrier.wait()
                results.append(store.save(expected))
            finally:
                store.close()

        threads = [threading.Thread(target=writer), threading.Thread(target=writer)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(results, [reference, reference])
        self.assertEqual(self.store.load(reference), expected)

    def test_separate_connection_different_complete_references_coexist(self) -> None:
        first = lecture("# Invented concurrent first document\n")
        second = lecture("# Invented concurrent second document\n")
        references = [require_reference(first), require_reference(second)]
        results: list[object] = []
        barrier = threading.Barrier(3)

        def writer(document: LectureDocument) -> None:
            store = open_lecture_document_store(self.database_path)
            assert isinstance(store, LocalLectureDocumentStore)
            try:
                barrier.wait()
                results.append(store.save(document))
            finally:
                store.close()

        threads = [
            threading.Thread(target=writer, args=(first,)),
            threading.Thread(target=writer, args=(second,)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertCountEqual(results, references)
        self.assertEqual(self.store.load(references[0]), first)
        self.assertEqual(self.store.load(references[1]), second)

    def test_colocation_does_not_mutate_workflow_source_or_opaque_artifact_state(self) -> None:
        colocated_path = Path(self._temporary.name) / "co-located.sqlite"
        workflow_store = open_workflow_state_store(colocated_path)
        source_store = open_source_evidence_store(colocated_path)
        artifact_store = open_workflow_artifact_store(colocated_path)
        document_store = open_lecture_document_store(colocated_path)
        assert isinstance(workflow_store, LocalWorkflowStateStore)
        assert isinstance(source_store, LocalSourceEvidenceStore)
        assert isinstance(artifact_store, LocalWorkflowArtifactStore)
        assert isinstance(document_store, LocalLectureDocumentStore)
        source_payload = b"invented exact source evidence"
        source_reference = SourceEvidenceReference(
            "source-evidence-reference/v1",
            "source-one",
            hashlib.sha256(source_payload).hexdigest(),
        )
        artifact_payload = b"\xffopaque invented lecture candidate bytes\x00"
        artifact_reference = WorkflowArtifactReference(
            "workflow-artifact-reference/v1",
            "artifact-one",
            "lecture_candidate",
            hashlib.sha256(artifact_payload).hexdigest(),
            ARTIFACT_PRODUCERS["lecture_candidate"],
        )
        expected = lecture("# Invented independent document\n")
        reference = require_reference(expected)
        try:
            self.assertEqual(
                source_store.save(source_reference, source_payload),
                SourceEvidencePayload(source_reference, source_payload),
            )
            self.assertEqual(
                artifact_store.save(artifact_reference, artifact_payload),
                WorkflowArtifactPayload(artifact_reference, artifact_payload),
            )
            self.assertEqual(document_store.save(expected), reference)
            self.assertEqual(document_store.load(reference), expected)
            self.assertEqual(
                artifact_store.load(artifact_reference),
                WorkflowArtifactPayload(artifact_reference, artifact_payload),
            )
        finally:
            workflow_store.close()
            source_store.close()
            artifact_store.close()
            document_store.close()
        connection = sqlite3.connect(colocated_path)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM workflow_state_snapshots").fetchone(),
                (0,),
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM source_evidence_blobs").fetchone(),
                (1,),
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM workflow_artifact_blobs").fetchone(),
                (1,),
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM lecture_documents").fetchone(),
                (1,),
            )
        finally:
            connection.close()

    def test_boundary_imports_no_artifact_interpreter_or_external_dependency(self) -> None:
        source = Path("course_compiler/lecture_document_persistence.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        imports: set[str] = set()
        calls: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                calls.add(node.func.id)
        self.assertFalse(
            {
                "workflow",
                "workflow_artifact_persistence",
                "pickle",
                "marshal",
                "subprocess",
                "socket",
                "requests",
                "urllib",
            }
            & imports
        )
        self.assertFalse({"eval", "exec", "compile", "open", "__import__"} & calls)
        self.assertNotIn("UPDATE lecture_documents", source)
        self.assertNotIn("DELETE FROM lecture_documents", source)


if __name__ == "__main__":
    unittest.main()
