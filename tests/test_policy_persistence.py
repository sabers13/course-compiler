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
    POLICY_CONTENT_PERSISTENCE_SCHEMA_VERSION,
    LocalLectureDocumentStore,
    LocalPolicyContentStore,
    LocalSourceEvidenceStore,
    LocalWorkflowArtifactStore,
    LocalWorkflowStateStore,
    PolicyContentPayload,
    PolicyPersistenceDiagnostic,
    PolicyPersistenceFailure,
    PolicyReference,
    open_lecture_document_store,
    open_policy_content_store,
    open_source_evidence_store,
    open_workflow_artifact_store,
    open_workflow_state_store,
)
from course_compiler import policy_persistence
from course_compiler.workflow_policy import POLICY_VERSIONS


def policy(payload: bytes, kind: str = "source_assessment") -> PolicyReference:
    return PolicyReference(
        kind,  # type: ignore[arg-type]
        POLICY_VERSIONS[kind],
        hashlib.sha256(payload).hexdigest(),
    )


def failure_code(result: object) -> str:
    if not isinstance(result, PolicyPersistenceFailure):
        raise AssertionError(
            f"policy persistence failure required, got {type(result).__name__}"
        )
    return result.diagnostics[0].code


class PolicyPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="policy-store-")
        self.database_path = Path(self._temporary.name) / "policy-content.sqlite"
        self.store = open_policy_content_store(self.database_path)
        assert isinstance(self.store, LocalPolicyContentStore)

    def tearDown(self) -> None:
        self.store.close()
        self._temporary.cleanup()

    def _replace_row(self, **replacement: object) -> None:
        columns = (
            "policy_kind",
            "policy_version",
            "content_sha256",
            "storage_schema_version",
            "payload",
        )
        connection = sqlite3.connect(self.database_path)
        try:
            row = connection.execute(
                "SELECT policy_kind, policy_version, content_sha256, "
                "storage_schema_version, payload FROM policy_content_blobs"
            ).fetchone()
            assert row is not None
            values = [replacement.get(name, value) for name, value in zip(columns, row)]
            connection.execute("DELETE FROM policy_content_blobs")
            connection.execute(
                "INSERT INTO policy_content_blobs VALUES (?, ?, ?, ?, ?)", values
            )
            connection.commit()
        finally:
            connection.close()

    def test_public_records_profile_and_payload_repr_are_fixed(self) -> None:
        self.assertEqual(
            POLICY_CONTENT_PERSISTENCE_SCHEMA_VERSION,
            "local-policy-content-sqlite/v1",
        )
        self.assertEqual(
            [item.name for item in fields(PolicyContentPayload)],
            ["reference", "payload"],
        )
        self.assertEqual(
            [item.name for item in fields(PolicyPersistenceDiagnostic)],
            ["code", "classification", "message"],
        )
        self.assertEqual(
            [item.name for item in fields(PolicyPersistenceFailure)],
            ["status", "diagnostics"],
        )
        secret = b"invented-private-policy-marker"
        value = PolicyContentPayload(policy(secret), secret)
        self.assertFalse(hasattr(value, "__dict__"))
        self.assertNotIn(secret.decode(), repr(value))
        with self.assertRaises(FrozenInstanceError):
            value.payload = b"changed"  # type: ignore[misc]

    def test_all_registered_policy_pairs_round_trip_exact_binary_bytes(self) -> None:
        expected: list[PolicyContentPayload] = []
        for index, (kind, version) in enumerate(POLICY_VERSIONS.items()):
            payload = bytes((0, index, 255)) + f"invented-{kind}\r\n".encode()
            reference = PolicyReference(kind, version, hashlib.sha256(payload).hexdigest())  # type: ignore[arg-type]
            value = PolicyContentPayload(reference, payload)
            expected.append(value)
            self.assertEqual(self.store.save(reference, payload), value)
        self.store.close()
        reopened = open_policy_content_store(self.database_path)
        assert isinstance(reopened, LocalPolicyContentStore)
        self.store = reopened
        for value in expected:
            loaded = reopened.load(value.reference)
            self.assertEqual(loaded, value)
            assert isinstance(loaded, PolicyContentPayload)
            self.assertEqual(loaded.payload, value.payload)

    def test_zero_short_and_non_text_payloads_are_not_decoded_or_normalized(self) -> None:
        payloads = (b"", b"\x00", b"\xff\xfe\x00\r\n")
        for payload in payloads:
            reference = policy(payload, "lecture_validation")
            self.assertEqual(
                self.store.save(reference, payload),
                PolicyContentPayload(reference, payload),
            )
            self.assertEqual(self.store.load(reference).payload, payload)  # type: ignore[union-attr]

    def test_invalid_references_and_non_bytes_fail_before_mutation(self) -> None:
        payload = b"invented valid policy bytes"
        reference = policy(payload)
        with self.assertRaises(TypeError):
            self.store.save("source_assessment", payload)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            self.store.load((reference.policy_kind, reference.policy_version))  # type: ignore[arg-type]
        for invalid in ("text", bytearray(payload), memoryview(payload), Path("policy")):
            with self.subTest(payload_type=type(invalid).__name__):
                with self.assertRaises(TypeError):
                    self.store.save(reference, invalid)  # type: ignore[arg-type]
        forged = object.__new__(PolicyReference)
        object.__setattr__(forged, "policy_kind", "source_assessment")
        self.assertEqual(
            failure_code(self.store.save(forged, payload)), "invalid_persistence_input"
        )
        self.assertEqual(
            failure_code(self.store.load(forged)), "invalid_persistence_input"
        )
        with self.assertRaises(ValueError):
            PolicyReference("source_assessment", "source-assessment-policy/v2", "0" * 64)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            PolicyReference("Source_Assessment", "source-assessment-policy/v1", "0" * 64)  # type: ignore[arg-type]
        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM policy_content_blobs").fetchone(),
                (0,),
            )
        finally:
            connection.close()

    def test_digest_mismatch_is_rejected_before_mutation_or_replacement(self) -> None:
        original = b"invented exact policy content"
        replacement = b"invented different policy content"
        reference = policy(original)
        self.assertEqual(
            failure_code(self.store.save(reference, replacement)),
            "invalid_persistence_input",
        )
        self.assertEqual(
            self.store.save(reference, original),
            PolicyContentPayload(reference, original),
        )
        self.assertEqual(
            failure_code(self.store.save(reference, replacement)),
            "invalid_persistence_input",
        )
        self.assertEqual(
            self.store.load(reference), PolicyContentPayload(reference, original)
        )

    def test_complete_identity_is_idempotent_and_distinct_digests_coexist(self) -> None:
        first = b"invented first policy content"
        second = b"invented second policy content"
        first_reference = policy(first, "lecture_production")
        second_reference = policy(second, "lecture_production")
        first_value = PolicyContentPayload(first_reference, first)
        second_value = PolicyContentPayload(second_reference, second)
        self.assertEqual(self.store.save(first_reference, first), first_value)
        self.assertEqual(self.store.save(first_reference, first), first_value)
        self.assertEqual(self.store.save(second_reference, second), second_value)
        self.assertEqual(self.store.load(first_reference), first_value)
        self.assertEqual(self.store.load(second_reference), second_value)
        absent = policy(b"invented absent content", "lecture_production")
        self.assertEqual(failure_code(self.store.load(absent)), "policy_not_found")
        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM policy_content_blobs "
                    "WHERE policy_kind = ? AND policy_version = ?",
                    (first_reference.policy_kind, first_reference.policy_version),
                ).fetchone(),
                (2,),
            )
        finally:
            connection.close()

    def test_table_schema_is_exact_and_incompatible_schema_fails_closed(self) -> None:
        connection = sqlite3.connect(self.database_path)
        try:
            actual = connection.execute(
                "PRAGMA table_info(policy_content_blobs)"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(
            [(row[1], row[2], row[3], row[4], row[5]) for row in actual],
            [
                ("policy_kind", "TEXT", 1, None, 1),
                ("policy_version", "TEXT", 1, None, 2),
                ("content_sha256", "TEXT", 1, None, 3),
                ("storage_schema_version", "TEXT", 1, None, 0),
                ("payload", "BLOB", 1, None, 0),
            ],
        )
        bad_path = Path(self._temporary.name) / "bad-schema.sqlite"
        connection = sqlite3.connect(bad_path)
        try:
            connection.execute("CREATE TABLE policy_content_blobs (unexpected TEXT)")
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(
            failure_code(open_policy_content_store(bad_path)),
            "unsupported_storage_schema",
        )

    def test_storage_profile_payload_type_and_byte_corruption_fail_closed(self) -> None:
        payload = b"invented corruption target"
        reference = policy(payload, "priority_basis")
        self.assertIsInstance(self.store.save(reference, payload), PolicyContentPayload)
        self._replace_row(storage_schema_version="unsupported-policy-schema/v9")
        self.assertEqual(
            failure_code(self.store.load(reference)), "unsupported_storage_schema"
        )
        self._replace_row(
            storage_schema_version=POLICY_CONTENT_PERSISTENCE_SCHEMA_VERSION,
            payload="invented text not blob",
        )
        self.assertEqual(failure_code(self.store.load(reference)), "stored_policy_invalid")
        self._replace_row(payload=b"invented corrupt bytes")
        self.assertEqual(failure_code(self.store.load(reference)), "stored_policy_invalid")
        self.assertEqual(failure_code(self.store.save(reference, payload)), "stored_policy_invalid")

    def test_wrong_stored_kind_version_digest_and_expected_reference_fail_closed(self) -> None:
        payload = b"invented row validation target"
        reference = policy(payload, "source_assessment")
        schema = POLICY_CONTENT_PERSISTENCE_SCHEMA_VERSION
        wrong_kind = (
            "priority_basis",
            POLICY_VERSIONS["priority_basis"],
            reference.content_sha256,
            schema,
            payload,
        )
        wrong_version = (
            reference.policy_kind,
            "source-assessment-policy/v2",
            reference.content_sha256,
            schema,
            payload,
        )
        other_payload = b"invented other digest"
        wrong_digest = (
            reference.policy_kind,
            reference.policy_version,
            hashlib.sha256(other_payload).hexdigest(),
            schema,
            other_payload,
        )
        self.assertEqual(
            failure_code(
                policy_persistence._decode_row(
                    wrong_kind, expected_reference=reference
                )
            ),
            "policy_reference_mismatch",
        )
        self.assertEqual(
            failure_code(
                policy_persistence._decode_row(
                    wrong_version, expected_reference=reference
                )
            ),
            "stored_policy_invalid",
        )
        self.assertEqual(
            failure_code(
                policy_persistence._decode_row(
                    wrong_digest, expected_reference=reference
                )
            ),
            "policy_reference_mismatch",
        )

    def test_open_store_load_detects_post_open_schema_drift(self) -> None:
        payload = b"invented load schema drift target"
        reference = policy(payload)
        self.assertIsInstance(self.store.save(reference, payload), PolicyContentPayload)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "ALTER TABLE policy_content_blobs ADD COLUMN unexpected TEXT"
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(
            failure_code(self.store.load(reference)), "unsupported_storage_schema"
        )

    def test_open_store_save_detects_schema_drift_before_new_row(self) -> None:
        first_payload = b"invented first schema drift policy"
        second_payload = b"invented second schema drift policy"
        first_reference = policy(first_payload)
        second_reference = policy(second_payload)
        self.assertIsInstance(
            self.store.save(first_reference, first_payload), PolicyContentPayload
        )
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "ALTER TABLE policy_content_blobs ADD COLUMN unexpected TEXT"
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(
            failure_code(self.store.save(second_reference, second_payload)),
            "unsupported_storage_schema",
        )
        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM policy_content_blobs WHERE content_sha256 = ?",
                    (second_reference.content_sha256,),
                ).fetchone(),
                (0,),
            )
        finally:
            connection.close()

    def test_fixed_failures_redact_payload_path_and_exception_text(self) -> None:
        secret = b"invented-policy-secret-sentinel"
        reference = policy(secret)
        result = self.store.load(reference)
        self.assertEqual(failure_code(result), "policy_not_found")
        self.assertNotIn(secret.decode(), repr(result))
        bad_path = Path(self._temporary.name) / secret.decode() / "policy.sqlite"
        result = open_policy_content_store(bad_path)
        self.assertEqual(failure_code(result), "persistence_unavailable")
        self.assertNotIn(secret.decode(), repr(result))
        self.assertIsInstance(self.store.save(reference, secret), PolicyContentPayload)
        with mock.patch.object(
            policy_persistence, "_decode_row", side_effect=RuntimeError(secret.decode())
        ):
            result = self.store.load(reference)
        self.assertEqual(failure_code(result), "persistence_exception")
        self.assertNotIn(secret.decode(), repr(result))

    def test_ordinary_exceptions_are_contained_and_baseexception_propagates(self) -> None:
        payload = b"invented exception policy"
        reference = policy(payload)
        with mock.patch.object(
            policy_persistence, "_validated_reference", side_effect=RuntimeError("secret")
        ):
            self.assertEqual(
                failure_code(self.store.save(reference, payload)),
                "invalid_persistence_input",
            )
            self.assertEqual(
                failure_code(self.store.load(reference)), "invalid_persistence_input"
            )
        self.assertIsInstance(self.store.save(reference, payload), PolicyContentPayload)
        with mock.patch.object(
            policy_persistence, "_decode_row", side_effect=RuntimeError("secret")
        ):
            self.assertEqual(
                failure_code(self.store.load(reference)), "persistence_exception"
            )
        with mock.patch.object(
            policy_persistence, "_validated_reference", side_effect=KeyboardInterrupt
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.store.save(reference, payload)
        with mock.patch.object(
            policy_persistence, "_decode_row", side_effect=SystemExit
        ):
            with self.assertRaises(SystemExit):
                self.store.load(reference)
        with mock.patch.object(policy_persistence.sqlite3, "connect", side_effect=GeneratorExit):
            with self.assertRaises(GeneratorExit):
                open_policy_content_store(Path("unused.sqlite"))

    def test_separate_connection_identical_writers_converge_idempotently(self) -> None:
        payload = b"invented concurrent identical policy"
        reference = policy(payload, "workflow_handoff")
        expected = PolicyContentPayload(reference, payload)
        results: list[object] = []
        barrier = threading.Barrier(3)

        def writer() -> None:
            store = open_policy_content_store(self.database_path)
            assert isinstance(store, LocalPolicyContentStore)
            try:
                barrier.wait()
                results.append(store.save(reference, payload))
            finally:
                store.close()

        threads = [threading.Thread(target=writer), threading.Thread(target=writer)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(results, [expected, expected])
        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM policy_content_blobs").fetchone(),
                (1,),
            )
        finally:
            connection.close()
        self.assertEqual(self.store.load(reference), expected)

    def test_separate_connection_distinct_references_coexist_without_replacement(self) -> None:
        payloads = (b"invented concurrent policy one", b"invented concurrent policy two")
        references = [policy(payload, "lecture_mapping") for payload in payloads]
        expected = [
            PolicyContentPayload(reference, payload)
            for reference, payload in zip(references, payloads)
        ]
        results: list[object] = []
        barrier = threading.Barrier(3)

        def writer(reference: PolicyReference, payload: bytes) -> None:
            store = open_policy_content_store(self.database_path)
            assert isinstance(store, LocalPolicyContentStore)
            try:
                barrier.wait()
                results.append(store.save(reference, payload))
            finally:
                store.close()

        threads = [
            threading.Thread(target=writer, args=item)
            for item in zip(references, payloads)
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertCountEqual(results, expected)
        self.assertEqual(self.store.load(references[0]), expected[0])
        self.assertEqual(self.store.load(references[1]), expected[1])

    def test_colocation_leaves_t007_through_t010_tables_unchanged(self) -> None:
        colocated_path = Path(self._temporary.name) / "co-located.sqlite"
        stores = (
            open_workflow_state_store(colocated_path),
            open_source_evidence_store(colocated_path),
            open_workflow_artifact_store(colocated_path),
            open_lecture_document_store(colocated_path),
        )
        self.assertIsInstance(stores[0], LocalWorkflowStateStore)
        self.assertIsInstance(stores[1], LocalSourceEvidenceStore)
        self.assertIsInstance(stores[2], LocalWorkflowArtifactStore)
        self.assertIsInstance(stores[3], LocalLectureDocumentStore)
        for store in stores:
            store.close()  # type: ignore[union-attr]
        predecessor_tables = (
            "workflow_state_snapshots",
            "source_evidence_blobs",
            "workflow_artifact_blobs",
            "lecture_documents",
        )
        connection = sqlite3.connect(colocated_path)
        try:
            before = {
                table: (
                    connection.execute(
                        "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?",
                        (table,),
                    ).fetchone(),
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone(),
                )
                for table in predecessor_tables
            }
        finally:
            connection.close()
        policy_store = open_policy_content_store(colocated_path)
        assert isinstance(policy_store, LocalPolicyContentStore)
        payload = b"invented co-located policy content"
        reference = policy(payload)
        try:
            self.assertIsInstance(
                policy_store.save(reference, payload), PolicyContentPayload
            )
            self.assertEqual(
                policy_store.load(reference), PolicyContentPayload(reference, payload)
            )
        finally:
            policy_store.close()
        connection = sqlite3.connect(colocated_path)
        try:
            after = {
                table: (
                    connection.execute(
                        "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?",
                        (table,),
                    ).fetchone(),
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone(),
                )
                for table in predecessor_tables
            }
        finally:
            connection.close()
        self.assertEqual(after, before)

    def test_dependency_direction_and_standard_library_boundary(self) -> None:
        source = Path("course_compiler/policy_persistence.py").read_text(encoding="utf-8")
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
                "course",
                "workflow_policy",
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
        self.assertNotIn("UPDATE policy_content_blobs", source)
        self.assertNotIn("DELETE FROM policy_content_blobs", source)
        workflow_source = Path("course_compiler/workflow.py").read_text(encoding="utf-8")
        self.assertNotIn("policy_persistence", workflow_source)


if __name__ == "__main__":
    unittest.main()
