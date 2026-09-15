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
    SOURCE_PERSISTENCE_SCHEMA_VERSION,
    WORKFLOW_ARTIFACT_PERSISTENCE_SCHEMA_VERSION,
    WORKFLOW_PERSISTENCE_SCHEMA_VERSION,
    LocalSourceEvidenceStore,
    LocalWorkflowArtifactStore,
    LocalWorkflowStateStore,
    SourceEvidenceReference,
    WorkflowArtifactPayload,
    WorkflowArtifactPersistenceDiagnostic,
    WorkflowArtifactPersistenceFailure,
    WorkflowArtifactReference,
    open_source_evidence_store,
    open_workflow_artifact_store,
    open_workflow_state_store,
)
from course_compiler import workflow_artifact_persistence
from course_compiler.workflow_policy import ARTIFACT_PRODUCERS


def reference(artifact_id: str, artifact_kind: str, payload: bytes) -> WorkflowArtifactReference:
    return WorkflowArtifactReference(
        "workflow-artifact-reference/v1",
        artifact_id,
        artifact_kind,
        hashlib.sha256(payload).hexdigest(),
        ARTIFACT_PRODUCERS[artifact_kind],
    )


def failure_code(result: object) -> str:
    if not isinstance(result, WorkflowArtifactPersistenceFailure):
        raise AssertionError(f"workflow artifact persistence failure required, got {type(result).__name__}")
    return result.diagnostics[0].code


class WorkflowArtifactPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="artifact-store-")
        self.database_path = Path(self._temporary.name) / "workflow-artifacts.sqlite"
        self.store = open_workflow_artifact_store(self.database_path)
        assert isinstance(self.store, LocalWorkflowArtifactStore)

    def tearDown(self) -> None:
        self.store.close()
        self._temporary.cleanup()

    def _replace_row(self, **replacement: object) -> None:
        connection = sqlite3.connect(self.database_path)
        try:
            columns = (
                "reference_version",
                "artifact_kind",
                "content_sha256",
                "producer_version",
                "storage_schema_version",
                "payload",
            )
            row = connection.execute(
                "SELECT reference_version, artifact_kind, content_sha256, producer_version, "
                "storage_schema_version, payload FROM workflow_artifact_blobs"
            ).fetchone()
            assert row is not None
            values = [replacement.get(name, value) for name, value in zip(columns, row)]
            connection.execute(
                "UPDATE workflow_artifact_blobs SET reference_version = ?, artifact_kind = ?, "
                "content_sha256 = ?, producer_version = ?, storage_schema_version = ?, payload = ?",
                values,
            )
            connection.commit()
        finally:
            connection.close()

    def test_public_records_are_fixed_and_redact_payloads(self) -> None:
        self.assertEqual(WORKFLOW_ARTIFACT_PERSISTENCE_SCHEMA_VERSION, "local-workflow-artifact-sqlite/v1")
        self.assertEqual(
            [item.name for item in fields(WorkflowArtifactPersistenceDiagnostic)],
            ["code", "classification", "message"],
        )
        self.assertEqual(
            [item.name for item in fields(WorkflowArtifactPersistenceFailure)], ["status", "diagnostics"]
        )
        self.assertEqual([item.name for item in fields(WorkflowArtifactPayload)], ["reference", "payload"])
        diagnostic = WorkflowArtifactPersistenceDiagnostic(
            "artifact_not_found", "storage", "No stored workflow artifact was found."
        )
        self.assertFalse(hasattr(diagnostic, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            diagnostic.code = "other"  # type: ignore[misc]
        payload = b"invented-private-artifact-bytes"
        value = WorkflowArtifactPayload(reference("artifact-one", "validation", payload), payload)
        self.assertNotIn(payload.decode("ascii"), repr(value))

    def test_every_registered_artifact_kind_round_trips_exact_bytes(self) -> None:
        for index, artifact_kind in enumerate(ARTIFACT_PRODUCERS, start=1):
            with self.subTest(artifact_kind=artifact_kind):
                payload = f"invented {artifact_kind} opaque bytes {index}".encode("ascii") + b"\x00\xff"
                expected = reference(f"artifact-{index}", artifact_kind, payload)
                self.assertEqual(self.store.save(expected, payload), WorkflowArtifactPayload(expected, payload))
                loaded = self.store.load(expected)
                self.assertEqual(loaded, WorkflowArtifactPayload(expected, payload))
                assert isinstance(loaded, WorkflowArtifactPayload)
                self.assertEqual(loaded.reference, expected)
                self.assertEqual(loaded.payload, payload)

    def test_fresh_store_reopens_exact_reference_and_bytes(self) -> None:
        payload = b"\x00invented opaque artifact\xff\r\n"
        expected = reference("artifact-one", "lecture_map", payload)
        self.assertEqual(self.store.save(expected, payload), WorkflowArtifactPayload(expected, payload))
        self.store.close()
        reopened = open_workflow_artifact_store(self.database_path)
        assert isinstance(reopened, LocalWorkflowArtifactStore)
        self.store = reopened
        self.assertEqual(reopened.load(expected), WorkflowArtifactPayload(expected, payload))

    def test_invalid_complete_reference_pair_bytes_and_digest_fail_before_mutation(self) -> None:
        payload = b"invented invalid input"
        mismatched = WorkflowArtifactReference(
            "workflow-artifact-reference/v1", "artifact-one", "validation", "0" * 64,
            ARTIFACT_PRODUCERS["validation"],
        )
        self.assertEqual(failure_code(self.store.save(mismatched, payload)), "invalid_persistence_input")
        self.assertEqual(failure_code(self.store.load(mismatched)), "artifact_not_found")
        with self.assertRaises(TypeError):
            self.store.save(reference("artifact-two", "validation", payload), bytearray(payload))  # type: ignore[arg-type]
        forged = object.__new__(WorkflowArtifactReference)
        object.__setattr__(forged, "reference_version", "workflow-artifact-reference/v1")
        object.__setattr__(forged, "artifact_id", "artifact-three")
        object.__setattr__(forged, "artifact_kind", "validation")
        object.__setattr__(forged, "content_sha256", hashlib.sha256(payload).hexdigest())
        object.__setattr__(forged, "producer_version", "wrong-producer/v1")
        self.assertEqual(failure_code(self.store.save(forged, payload)), "invalid_persistence_input")
        with self.assertRaises(TypeError):
            self.store.load("artifact-one")  # type: ignore[arg-type]

    def test_idempotency_immutable_binding_and_expected_reference_mismatch(self) -> None:
        original = b"invented original artifact"
        replacement = b"invented replacement artifact"
        first = reference("artifact-one", "validation", original)
        different_bytes = reference("artifact-one", "validation", replacement)
        different_fields = reference("artifact-one", "lecture_map", original)
        self.assertEqual(self.store.save(first, original), WorkflowArtifactPayload(first, original))
        self.assertEqual(self.store.save(first, original), WorkflowArtifactPayload(first, original))
        self.assertEqual(failure_code(self.store.save(different_bytes, replacement)), "immutable_identity_conflict")
        self.assertEqual(failure_code(self.store.save(different_fields, original)), "immutable_identity_conflict")
        self.assertEqual(self.store.load(first), WorkflowArtifactPayload(first, original))
        self.assertEqual(failure_code(self.store.load(different_bytes)), "artifact_reference_mismatch")
        self.assertEqual(failure_code(self.store.load(different_fields)), "artifact_reference_mismatch")

    def test_schema_and_all_persisted_integrity_failures_are_closed(self) -> None:
        payload = b"invented corruption artifact"
        expected = reference("artifact-one", "validation", payload)
        self.assertEqual(self.store.save(expected, payload), WorkflowArtifactPayload(expected, payload))
        cases = (
            ("schema", {"storage_schema_version": "unsupported-schema/v9"}, "unsupported_storage_schema"),
            ("reference", {"reference_version": "workflow-artifact-reference/v9"}, "stored_artifact_invalid"),
            ("kind", {"artifact_kind": "unknown"}, "stored_artifact_invalid"),
            ("producer", {"producer_version": "wrong-producer/v1"}, "stored_artifact_invalid"),
            ("digest", {"content_sha256": "1" * 64}, "stored_artifact_invalid"),
            ("blob", {"payload": b"tampered invented artifact"}, "stored_artifact_invalid"),
        )
        for name, replacement, code in cases:
            with self.subTest(name=name):
                self._replace_row(
                    reference_version=expected.reference_version,
                    artifact_kind=expected.artifact_kind,
                    content_sha256=expected.content_sha256,
                    producer_version=expected.producer_version,
                    storage_schema_version=WORKFLOW_ARTIFACT_PERSISTENCE_SCHEMA_VERSION,
                    payload=payload,
                )
                self._replace_row(**replacement)
                self.assertEqual(failure_code(self.store.load(expected)), code)

    def test_bad_table_schema_and_colocated_t007_t008_tables_are_untouched(self) -> None:
        colocated_path = Path(self._temporary.name) / "co-located.sqlite"
        workflow_store = open_workflow_state_store(colocated_path)
        source_store = open_source_evidence_store(colocated_path)
        artifact_store = open_workflow_artifact_store(colocated_path)
        assert isinstance(workflow_store, LocalWorkflowStateStore)
        assert isinstance(source_store, LocalSourceEvidenceStore)
        assert isinstance(artifact_store, LocalWorkflowArtifactStore)
        try:
            payload = b"invented co-located artifact"
            expected = reference("artifact-one", "validation", payload)
            self.assertEqual(artifact_store.save(expected, payload), WorkflowArtifactPayload(expected, payload))
        finally:
            workflow_store.close()
            source_store.close()
            artifact_store.close()
        connection = sqlite3.connect(colocated_path)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM workflow_state_snapshots").fetchone(), (0,))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_evidence_blobs").fetchone(), (0,))
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM workflow_artifact_blobs").fetchone(), (1,)
            )
        finally:
            connection.close()
        bad_path = Path(self._temporary.name) / "bad-schema.sqlite"
        connection = sqlite3.connect(bad_path)
        try:
            connection.execute("CREATE TABLE workflow_artifact_blobs (unexpected TEXT)")
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(failure_code(open_workflow_artifact_store(bad_path)), "unsupported_storage_schema")

    def test_failures_redact_payload_path_and_ordinary_exception_text(self) -> None:
        secret = "invented-artifact-payload-secret"
        payload = secret.encode("ascii")
        expected = reference("artifact-one", "validation", payload)
        self.assertEqual(self.store.save(expected, payload), WorkflowArtifactPayload(expected, payload))
        self._replace_row(payload=b"tampered")
        result = self.store.load(expected)
        self.assertEqual(failure_code(result), "stored_artifact_invalid")
        self.assertNotIn(secret, repr(result))
        bad_path = Path(self._temporary.name) / secret / "artifact.sqlite"
        result = open_workflow_artifact_store(bad_path)
        self.assertEqual(failure_code(result), "persistence_unavailable")
        self.assertNotIn(secret, repr(result))
        with mock.patch.object(workflow_artifact_persistence, "_decode_row", side_effect=RuntimeError(secret)):
            result = self.store.save(expected, payload)
        self.assertEqual(failure_code(result), "persistence_exception")
        self.assertNotIn(secret, repr(result))
        with mock.patch.object(workflow_artifact_persistence, "_validated_reference", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.store.save(expected, payload)

    def test_deterministic_identical_concurrent_first_writers_are_idempotent(self) -> None:
        payload = b"invented identical concurrent artifact"
        expected = reference("artifact-one", "validation", payload)
        results: list[object] = []
        barrier = threading.Barrier(3)

        def writer() -> None:
            store = open_workflow_artifact_store(self.database_path)
            assert isinstance(store, LocalWorkflowArtifactStore)
            try:
                barrier.wait()
                results.append(store.save(expected, payload))
            finally:
                store.close()

        threads = [threading.Thread(target=writer), threading.Thread(target=writer)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(results, [WorkflowArtifactPayload(expected, payload)] * 2)
        self.assertEqual(self.store.load(expected), WorkflowArtifactPayload(expected, payload))

    def test_deterministic_conflicting_concurrent_first_writers_preserve_one_value(self) -> None:
        first_payload = b"invented first concurrent artifact"
        second_payload = b"invented second concurrent artifact"
        first = reference("artifact-one", "validation", first_payload)
        second = reference("artifact-one", "validation", second_payload)
        results: list[tuple[WorkflowArtifactReference, object]] = []
        barrier = threading.Barrier(3)

        def writer(item: WorkflowArtifactReference, payload: bytes) -> None:
            store = open_workflow_artifact_store(self.database_path)
            assert isinstance(store, LocalWorkflowArtifactStore)
            try:
                barrier.wait()
                results.append((item, store.save(item, payload)))
            finally:
                store.close()

        threads = [
            threading.Thread(target=writer, args=(first, first_payload)),
            threading.Thread(target=writer, args=(second, second_payload)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        successes = [(item, result) for item, result in results if isinstance(result, WorkflowArtifactPayload)]
        failures = [result for _, result in results if isinstance(result, WorkflowArtifactPersistenceFailure)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failure_code(failures[0]), "immutable_identity_conflict")
        winner, success = successes[0]
        self.assertEqual(self.store.load(winner), success)

    def test_boundary_uses_only_sqlite_and_standard_library_hashing(self) -> None:
        source = Path("course_compiler/workflow_artifact_persistence.py").read_text(encoding="utf-8")
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
        self.assertFalse({"pickle", "marshal", "subprocess", "socket", "requests", "urllib"} & imports)
        self.assertFalse({"eval", "exec", "compile", "open", "__import__"} & calls)


if __name__ == "__main__":
    unittest.main()
