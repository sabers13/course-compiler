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
    LocalSourceEvidenceStore,
    SourceEvidencePayload,
    SourceEvidenceReference,
    SourcePersistenceDiagnostic,
    SourcePersistenceFailure,
    open_source_evidence_store,
)
from course_compiler import source_persistence


def reference(source_id: str, payload: bytes) -> SourceEvidenceReference:
    return SourceEvidenceReference(
        "source-evidence-reference/v1", source_id, hashlib.sha256(payload).hexdigest()
    )


def failure_code(result: object) -> str:
    if not isinstance(result, SourcePersistenceFailure):
        raise AssertionError(f"source persistence failure required, got {type(result).__name__}")
    return result.diagnostics[0].code


class SourcePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="source-store-")
        self.database_path = Path(self._temporary.name) / "source-evidence.sqlite"
        self.store = open_source_evidence_store(self.database_path)
        assert isinstance(self.store, LocalSourceEvidenceStore)

    def tearDown(self) -> None:
        self.store.close()
        self._temporary.cleanup()

    def _replace_row(
        self,
        *,
        reference_version: object | None = None,
        content_sha256: object | None = None,
        schema: object | None = None,
        payload: object | None = None,
    ) -> None:
        connection = sqlite3.connect(self.database_path)
        try:
            row = connection.execute(
                "SELECT reference_version, content_sha256, storage_schema_version, payload "
                "FROM source_evidence_blobs"
            ).fetchone()
            assert row is not None
            connection.execute(
                "UPDATE source_evidence_blobs SET reference_version = ?, content_sha256 = ?, "
                "storage_schema_version = ?, payload = ?",
                (
                    row[0] if reference_version is None else reference_version,
                    row[1] if content_sha256 is None else content_sha256,
                    row[2] if schema is None else schema,
                    row[3] if payload is None else payload,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def test_public_records_are_fixed_and_redact_payloads(self) -> None:
        self.assertEqual(SOURCE_PERSISTENCE_SCHEMA_VERSION, "local-source-evidence-sqlite/v1")
        self.assertEqual(
            [item.name for item in fields(SourcePersistenceDiagnostic)],
            ["code", "classification", "message"],
        )
        self.assertEqual([item.name for item in fields(SourcePersistenceFailure)], ["status", "diagnostics"])
        self.assertEqual([item.name for item in fields(SourceEvidencePayload)], ["reference", "payload"])
        diagnostic = SourcePersistenceDiagnostic(
            "source_not_found", "storage", "No stored source evidence was found."
        )
        self.assertFalse(hasattr(diagnostic, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            diagnostic.code = "other"  # type: ignore[misc]
        payload = b"invented-private-evidence-bytes"
        value = SourceEvidencePayload(reference("source-one", payload), payload)
        self.assertNotIn(payload.decode("ascii"), repr(value))

    def test_exact_bytes_and_reference_round_trip_through_fresh_store(self) -> None:
        payload = b"\x00invented\xff source\r\nbytes"
        expected = reference("source-one", payload)
        saved = self.store.save(expected, payload)
        self.assertEqual(saved, SourceEvidencePayload(expected, payload))
        self.store.close()
        reopened = open_source_evidence_store(self.database_path)
        self.assertIsInstance(reopened, LocalSourceEvidenceStore)
        assert isinstance(reopened, LocalSourceEvidenceStore)
        self.store = reopened
        loaded = reopened.load(expected)
        self.assertEqual(loaded, SourceEvidencePayload(expected, payload))
        assert isinstance(loaded, SourceEvidencePayload)
        self.assertIs(loaded.payload, loaded.payload)
        self.assertEqual(loaded.reference, expected)

    def test_exact_duplicate_is_idempotent_and_identical_bytes_allow_distinct_ids(self) -> None:
        payload = b"invented repeated source evidence"
        first = reference("source-one", payload)
        second = reference("source-two", payload)
        self.assertEqual(self.store.save(first, payload), SourceEvidencePayload(first, payload))
        self.assertEqual(self.store.save(first, payload), SourceEvidencePayload(first, payload))
        self.assertEqual(self.store.save(second, payload), SourceEvidencePayload(second, payload))
        self.assertEqual(self.store.load(first), SourceEvidencePayload(first, payload))
        self.assertEqual(self.store.load(second), SourceEvidencePayload(second, payload))

    def test_digest_mismatch_and_invalid_inputs_leave_no_row(self) -> None:
        payload = b"invented digest mismatch"
        mismatched = SourceEvidenceReference("source-evidence-reference/v1", "source-one", "0" * 64)
        self.assertEqual(failure_code(self.store.save(mismatched, payload)), "invalid_persistence_input")
        self.assertEqual(failure_code(self.store.load(mismatched)), "source_not_found")
        with self.assertRaises(TypeError):
            self.store.save(reference("source-two", payload), bytearray(payload))  # type: ignore[arg-type]
        forged = object.__new__(SourceEvidenceReference)
        object.__setattr__(forged, "reference_version", "source-evidence-reference/v1")
        object.__setattr__(forged, "source_id", "bad/path")
        object.__setattr__(forged, "content_sha256", hashlib.sha256(payload).hexdigest())
        self.assertEqual(failure_code(self.store.save(forged, payload)), "invalid_persistence_input")

    def test_same_source_id_cannot_replace_immutable_content(self) -> None:
        original = b"invented original evidence"
        replacement = b"invented replacement evidence"
        original_reference = reference("source-one", original)
        replacement_reference = reference("source-one", replacement)
        self.assertEqual(self.store.save(original_reference, original), SourceEvidencePayload(original_reference, original))
        self.assertEqual(
            failure_code(self.store.save(replacement_reference, replacement)), "immutable_identity_conflict"
        )
        self.assertEqual(self.store.load(original_reference), SourceEvidencePayload(original_reference, original))
        self.assertEqual(failure_code(self.store.load(replacement_reference)), "source_reference_mismatch")

    def test_schema_metadata_and_stored_content_corruption_fail_closed(self) -> None:
        payload = b"invented corruption evidence"
        expected = reference("source-one", payload)
        self.assertEqual(self.store.save(expected, payload), SourceEvidencePayload(expected, payload))
        cases = (
            ("unsupported", {"schema": "unsupported-source-schema/v9"}, "unsupported_storage_schema"),
            ("digest", {"content_sha256": "1" * 64}, "stored_source_invalid"),
            ("blob", {"payload": b"tampered invented blob"}, "stored_source_invalid"),
            ("reference", {"reference_version": "source-evidence-reference/v9"}, "stored_source_invalid"),
        )
        for name, replacement, code in cases:
            with self.subTest(name=name):
                self._replace_row(
                    reference_version="source-evidence-reference/v1",
                    content_sha256=expected.content_sha256,
                    schema=SOURCE_PERSISTENCE_SCHEMA_VERSION,
                    payload=payload,
                )
                self._replace_row(**replacement)
                self.assertEqual(failure_code(self.store.load(expected)), code)

    def test_unrecognized_table_schema_fails_at_open_and_unrelated_table_is_untouched(self) -> None:
        other_path = Path(self._temporary.name) / "co-located.sqlite"
        connection = sqlite3.connect(other_path)
        try:
            connection.execute("CREATE TABLE unrelated_application_data (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("INSERT INTO unrelated_application_data VALUES ('keep', 'invented')")
            connection.commit()
        finally:
            connection.close()
        colocated = open_source_evidence_store(other_path)
        assert isinstance(colocated, LocalSourceEvidenceStore)
        payload = b"invented co-located source"
        expected = reference("source-one", payload)
        self.assertEqual(colocated.save(expected, payload), SourceEvidencePayload(expected, payload))
        colocated.close()
        connection = sqlite3.connect(other_path)
        try:
            self.assertEqual(
                connection.execute("SELECT key, value FROM unrelated_application_data").fetchall(),
                [("keep", "invented")],
            )
        finally:
            connection.close()
        bad_path = Path(self._temporary.name) / "bad-schema.sqlite"
        connection = sqlite3.connect(bad_path)
        try:
            connection.execute("CREATE TABLE source_evidence_blobs (unexpected TEXT)")
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(failure_code(open_source_evidence_store(bad_path)), "unsupported_storage_schema")

    def test_failures_do_not_reveal_payload_path_or_exception_text(self) -> None:
        secret = "invented-source-payload-secret"
        payload = secret.encode("ascii")
        expected = reference("source-one", payload)
        self.assertEqual(self.store.save(expected, payload), SourceEvidencePayload(expected, payload))
        self._replace_row(payload=b"tampered")
        result = self.store.load(expected)
        self.assertEqual(failure_code(result), "stored_source_invalid")
        self.assertNotIn(secret, repr(result))
        bad_path = Path(self._temporary.name) / secret / "source.sqlite"
        result = open_source_evidence_store(bad_path)
        self.assertEqual(failure_code(result), "persistence_unavailable")
        self.assertNotIn(secret, repr(result))

    def test_ordinary_exceptions_are_contained_and_baseexception_propagates(self) -> None:
        payload = b"invented exception source"
        expected = reference("source-one", payload)
        self.assertEqual(self.store.save(expected, payload), SourceEvidencePayload(expected, payload))
        with mock.patch.object(source_persistence, "_decode_row", side_effect=RuntimeError("invented sqlite secret")):
            result = self.store.save(expected, payload)
        self.assertEqual(failure_code(result), "persistence_exception")
        self.assertNotIn("invented sqlite secret", repr(result))
        with mock.patch.object(source_persistence, "_validated_reference", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.store.save(expected, payload)

    def test_deterministic_conflicting_first_writers_preserve_one_value(self) -> None:
        first_payload = b"invented first concurrent source"
        second_payload = b"invented second concurrent source"
        first = reference("source-one", first_payload)
        second = reference("source-one", second_payload)
        results: list[tuple[SourceEvidenceReference, object]] = []
        barrier = threading.Barrier(3)

        def writer(item: SourceEvidenceReference, payload: bytes) -> None:
            store = open_source_evidence_store(self.database_path)
            assert isinstance(store, LocalSourceEvidenceStore)
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
        successes = [(item, result) for item, result in results if isinstance(result, SourceEvidencePayload)]
        failures = [result for _, result in results if isinstance(result, SourcePersistenceFailure)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failure_code(failures[0]), "immutable_identity_conflict")
        winner, success = successes[0]
        assert isinstance(success, SourceEvidencePayload)
        self.assertEqual(self.store.load(winner), success)

    def test_deterministic_identical_concurrent_writers_are_idempotent(self) -> None:
        payload = b"invented identical concurrent source"
        expected = reference("source-one", payload)
        results: list[object] = []
        barrier = threading.Barrier(3)

        def writer() -> None:
            store = open_source_evidence_store(self.database_path)
            assert isinstance(store, LocalSourceEvidenceStore)
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
        self.assertEqual(results, [SourceEvidencePayload(expected, payload)] * 2)
        self.assertEqual(self.store.load(expected), SourceEvidencePayload(expected, payload))

    def test_boundary_uses_only_sqlite_and_standard_library_hashing(self) -> None:
        source = Path("course_compiler/source_persistence.py").read_text(encoding="utf-8")
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
