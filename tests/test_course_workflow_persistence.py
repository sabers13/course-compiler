from __future__ import annotations

import ast
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from unittest import mock

import course_compiler
from course_compiler import (
    COURSE_REFERENCE_VERSION,
    COURSE_WORKFLOW_ASSOCIATION_VERSION,
    COURSE_WORKFLOW_PERSISTENCE_SCHEMA_VERSION,
    CourseReference,
    CourseWorkflowAssociation,
    CourseWorkflowPersistenceDiagnostic,
    CourseWorkflowPersistenceFailure,
    LocalCourseWorkflowAssociationStore,
    LocalLectureDocumentStore,
    LocalPolicyContentStore,
    LocalSourceEvidenceStore,
    LocalWorkflowArtifactStore,
    LocalWorkflowStateStore,
    open_course_workflow_association_store,
    open_lecture_document_store,
    open_policy_content_store,
    open_source_evidence_store,
    open_workflow_artifact_store,
    open_workflow_state_store,
)
from course_compiler import course_workflow_persistence


def association(
    course_id: str = "invented-course",
    workflow_id: str = "invented-workflow",
) -> CourseWorkflowAssociation:
    return CourseWorkflowAssociation(
        COURSE_WORKFLOW_ASSOCIATION_VERSION,
        CourseReference(COURSE_REFERENCE_VERSION, course_id),
        workflow_id,
    )


def failure_code(result: object) -> str:
    if not isinstance(result, CourseWorkflowPersistenceFailure):
        raise AssertionError(
            "course-workflow persistence failure required, "
            f"got {type(result).__name__}"
        )
    return result.diagnostics[0].code


class CourseWorkflowPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="association-store-")
        self.database_path = Path(self._temporary.name) / "associations.sqlite"
        self.store = open_course_workflow_association_store(self.database_path)
        assert isinstance(self.store, LocalCourseWorkflowAssociationStore)

    def tearDown(self) -> None:
        self.store.close()
        self._temporary.cleanup()

    def test_public_profile_records_and_exports_are_fixed(self) -> None:
        self.assertEqual(
            COURSE_WORKFLOW_PERSISTENCE_SCHEMA_VERSION,
            "local-course-workflow-association-sqlite/v1",
        )
        self.assertEqual(
            [item.name for item in fields(CourseWorkflowPersistenceDiagnostic)],
            ["code", "classification", "message"],
        )
        self.assertEqual(
            [item.name for item in fields(CourseWorkflowPersistenceFailure)],
            ["status", "diagnostics"],
        )
        result = self.store.load(association())
        assert isinstance(result, CourseWorkflowPersistenceFailure)
        self.assertFalse(hasattr(result, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            result.status = "changed"  # type: ignore[misc]
        expected_exports = {
            "COURSE_WORKFLOW_PERSISTENCE_SCHEMA_VERSION",
            "CourseWorkflowPersistenceDiagnostic",
            "CourseWorkflowPersistenceFailure",
            "CourseWorkflowPersistenceResult",
            "LocalCourseWorkflowAssociationStore",
            "open_course_workflow_association_store",
        }
        self.assertTrue(expected_exports <= set(course_compiler.__all__))
        self.assertIs(
            course_compiler.LocalCourseWorkflowAssociationStore,
            LocalCourseWorkflowAssociationStore,
        )

    def test_table_schema_and_complete_primary_key_are_exact(self) -> None:
        connection = sqlite3.connect(self.database_path)
        try:
            actual = connection.execute(
                "PRAGMA table_info(course_workflow_associations)"
            ).fetchall()
            foreign_keys = connection.execute(
                "PRAGMA foreign_key_list(course_workflow_associations)"
            ).fetchall()
            indexes = connection.execute(
                "PRAGMA index_list(course_workflow_associations)"
            ).fetchall()
            primary_key_columns = connection.execute(
                "PRAGMA index_xinfo(sqlite_autoindex_course_workflow_associations_1)"
            ).fetchall()
            triggers = connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type = 'trigger' "
                "AND tbl_name = 'course_workflow_associations'"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(
            [(row[1], row[2], row[3], row[4], row[5]) for row in actual],
            [
                ("association_version", "TEXT", 1, None, 1),
                ("course_reference_version", "TEXT", 1, None, 2),
                ("course_id", "TEXT", 1, None, 3),
                ("workflow_id", "TEXT", 1, None, 4),
                ("storage_schema_version", "TEXT", 1, None, 0),
            ],
        )
        self.assertEqual(foreign_keys, [])
        self.assertEqual(
            [(row[2], row[3], row[4]) for row in indexes],
            [(1, "pk", 0)],
        )
        self.assertEqual(
            [(row[1], row[2], row[4], row[5]) for row in primary_key_columns],
            [
                (0, "association_version", "BINARY", 1),
                (1, "course_reference_version", "BINARY", 1),
                (2, "course_id", "BINARY", 1),
                (3, "workflow_id", "BINARY", 1),
                (-1, None, "BINARY", 0),
            ],
        )
        self.assertEqual(triggers, [])

    def test_open_rejects_malformed_schema_and_unsupported_profile(self) -> None:
        malformed_path = Path(self._temporary.name) / "malformed.sqlite"
        connection = sqlite3.connect(malformed_path)
        try:
            connection.execute(
                "CREATE TABLE course_workflow_associations (unexpected TEXT)"
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(
            failure_code(open_course_workflow_association_store(malformed_path)),
            "unsupported_storage_schema",
        )

        unsupported_path = Path(self._temporary.name) / "unsupported.sqlite"
        connection = sqlite3.connect(unsupported_path)
        try:
            connection.execute(course_workflow_persistence._SCHEMA_SQL)
            value = association()
            connection.execute(
                "INSERT INTO course_workflow_associations VALUES (?, ?, ?, ?, ?)",
                (
                    value.association_version,
                    value.course_reference.reference_version,
                    value.course_reference.course_id,
                    value.workflow_id,
                    "local-course-workflow-association-sqlite/v9",
                ),
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(
            failure_code(open_course_workflow_association_store(unsupported_path)),
            "unsupported_storage_schema",
        )

    def test_open_rejects_forbidden_partial_key_unique_index(self) -> None:
        bad_path = Path(self._temporary.name) / "partial-unique.sqlite"
        connection = sqlite3.connect(bad_path)
        try:
            connection.execute(course_workflow_persistence._SCHEMA_SQL)
            connection.execute(
                "CREATE UNIQUE INDEX injected_course_unique "
                "ON course_workflow_associations(course_id)"
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(
            failure_code(open_course_workflow_association_store(bad_path)),
            "unsupported_storage_schema",
        )

    def test_post_open_unique_index_drift_precedes_second_insert(self) -> None:
        first = association("invented-course", "invented-workflow-a")
        second = association("invented-course", "invented-workflow-b")
        self.assertEqual(self.store.save(first), first)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "CREATE UNIQUE INDEX injected_course_unique "
                "ON course_workflow_associations(course_id)"
            )
            connection.commit()
        finally:
            connection.close()

        statements: list[str] = []
        self.store._connection.set_trace_callback(statements.append)
        try:
            self.assertEqual(
                failure_code(self.store.load(first)),
                "unsupported_storage_schema",
            )
            statements.clear()
            self.assertEqual(
                failure_code(self.store.save(second)),
                "unsupported_storage_schema",
            )
        finally:
            self.store._connection.set_trace_callback(None)
        self.assertFalse(
            any(
                statement.lstrip().upper().startswith(
                    "INSERT INTO COURSE_WORKFLOW_ASSOCIATIONS"
                )
                for statement in statements
            )
        )
        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT course_id, workflow_id "
                    "FROM course_workflow_associations"
                ).fetchall(),
                [(first.course_reference.course_id, first.workflow_id)],
            )
        finally:
            connection.close()

    def test_open_rejects_otherwise_matching_schema_with_foreign_key(self) -> None:
        bad_path = Path(self._temporary.name) / "foreign-key.sqlite"
        connection = sqlite3.connect(bad_path)
        try:
            connection.execute(
                "CREATE TABLE workflows (workflow_id TEXT PRIMARY KEY NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE course_workflow_associations ("
                "association_version TEXT NOT NULL, "
                "course_reference_version TEXT NOT NULL, "
                "course_id TEXT NOT NULL, "
                "workflow_id TEXT NOT NULL, "
                "storage_schema_version TEXT NOT NULL, "
                "PRIMARY KEY (association_version, course_reference_version, "
                "course_id, workflow_id), "
                "FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id))"
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(
            failure_code(open_course_workflow_association_store(bad_path)),
            "unsupported_storage_schema",
        )

    def test_open_rejects_check_trigger_and_hidden_column_semantics(self) -> None:
        check_path = Path(self._temporary.name) / "check-constraint.sqlite"
        connection = sqlite3.connect(check_path)
        try:
            connection.execute(
                "CREATE TABLE course_workflow_associations ("
                "association_version TEXT NOT NULL, "
                "course_reference_version TEXT NOT NULL, "
                "course_id TEXT NOT NULL, "
                "workflow_id TEXT NOT NULL, "
                "storage_schema_version TEXT NOT NULL, "
                "PRIMARY KEY (association_version, course_reference_version, "
                "course_id, workflow_id), "
                "CHECK (course_id <> workflow_id))"
            )
            connection.commit()
        finally:
            connection.close()

        trigger_path = Path(self._temporary.name) / "trigger.sqlite"
        connection = sqlite3.connect(trigger_path)
        try:
            connection.execute(course_workflow_persistence._SCHEMA_SQL)
            connection.execute(
                "CREATE TRIGGER injected_association_trigger "
                "BEFORE INSERT ON course_workflow_associations "
                "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
            )
            connection.commit()
        finally:
            connection.close()

        generated_path = Path(self._temporary.name) / "generated-column.sqlite"
        connection = sqlite3.connect(generated_path)
        try:
            connection.execute(
                "CREATE TABLE course_workflow_associations ("
                "association_version TEXT NOT NULL, "
                "course_reference_version TEXT NOT NULL, "
                "course_id TEXT NOT NULL, "
                "workflow_id TEXT NOT NULL, "
                "storage_schema_version TEXT NOT NULL, "
                "injected_identity TEXT GENERATED ALWAYS AS (course_id) VIRTUAL, "
                "PRIMARY KEY (association_version, course_reference_version, "
                "course_id, workflow_id))"
            )
            connection.commit()
        finally:
            connection.close()

        for bad_path in (check_path, trigger_path, generated_path):
            with self.subTest(path=bad_path.name):
                self.assertEqual(
                    failure_code(open_course_workflow_association_store(bad_path)),
                    "unsupported_storage_schema",
                )

    def test_invalid_types_and_forged_values_fail_before_mutation(self) -> None:
        with self.assertRaises(TypeError):
            self.store.save(("invented-course", "invented-workflow"))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            self.store.load("invented-course")  # type: ignore[arg-type]
        forged = object.__new__(CourseWorkflowAssociation)
        object.__setattr__(
            forged,
            "association_version",
            COURSE_WORKFLOW_ASSOCIATION_VERSION,
        )
        self.assertEqual(
            failure_code(self.store.save(forged)), "invalid_persistence_input"
        )
        self.assertEqual(
            failure_code(self.store.load(forged)), "invalid_persistence_input"
        )
        invalid = association()
        object.__setattr__(invalid.course_reference, "course_id", "Invalid Course")
        self.assertEqual(
            failure_code(self.store.save(invalid)), "invalid_persistence_input"
        )
        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM course_workflow_associations"
                ).fetchone(),
                (0,),
            )
        finally:
            connection.close()

    def test_exact_identity_round_trips_idempotently_after_reopen(self) -> None:
        value = association("invented-linear-models", "invented-exam-review")
        self.assertEqual(self.store.save(value), value)
        self.assertEqual(self.store.save(value), value)
        self.assertEqual(self.store.load(value), value)
        self.store.close()
        reopened = open_course_workflow_association_store(self.database_path)
        assert isinstance(reopened, LocalCourseWorkflowAssociationStore)
        self.store = reopened
        self.assertEqual(reopened.load(value), value)
        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM course_workflow_associations"
                ).fetchone(),
                (1,),
            )
        finally:
            connection.close()

    def test_cardinality_neutral_complete_identities_coexist(self) -> None:
        same_course_first = association("invented-course", "invented-workflow-a")
        same_course_second = association("invented-course", "invented-workflow-b")
        same_workflow_other_course = association(
            "invented-other-course", "invented-workflow-a"
        )
        identical_text = association("invented-shared-id", "invented-shared-id")
        values = (
            same_course_first,
            same_course_second,
            same_workflow_other_course,
            identical_text,
        )
        for value in values:
            self.assertEqual(self.store.save(value), value)
        for value in values:
            self.assertEqual(self.store.load(value), value)
        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM course_workflow_associations"
                ).fetchone(),
                (4,),
            )
        finally:
            connection.close()

    def test_missing_complete_identity_is_not_found(self) -> None:
        saved = association("invented-course", "invented-workflow-a")
        missing = association("invented-course", "invented-workflow-b")
        self.assertEqual(self.store.save(saved), saved)
        self.assertEqual(
            failure_code(self.store.load(missing)), "association_not_found"
        )
        self.assertEqual(self.store.load(saved), saved)

    def test_stored_row_validation_rejects_malformed_and_mismatched_values(self) -> None:
        expected = association("invented-course", "invented-workflow")
        malformed = (
            "course-workflow-association/v9",
            COURSE_REFERENCE_VERSION,
            "invented-course",
            "invented-workflow",
            COURSE_WORKFLOW_PERSISTENCE_SCHEMA_VERSION,
        )
        mismatch_value = association("invented-other-course", "invented-workflow")
        mismatch = (
            mismatch_value.association_version,
            mismatch_value.course_reference.reference_version,
            mismatch_value.course_reference.course_id,
            mismatch_value.workflow_id,
            COURSE_WORKFLOW_PERSISTENCE_SCHEMA_VERSION,
        )
        unsupported = (*malformed[:4], "unsupported-association-schema/v9")
        self.assertEqual(
            failure_code(
                course_workflow_persistence._decode_row(
                    malformed,
                    expected_association=expected,
                )
            ),
            "stored_association_invalid",
        )
        self.assertEqual(
            failure_code(
                course_workflow_persistence._decode_row(
                    mismatch,
                    expected_association=expected,
                )
            ),
            "association_identity_mismatch",
        )
        self.assertEqual(
            failure_code(
                course_workflow_persistence._decode_row(
                    unsupported,
                    expected_association=expected,
                )
            ),
            "unsupported_storage_schema",
        )

    def test_load_detects_post_open_schema_drift(self) -> None:
        value = association()
        self.assertEqual(self.store.save(value), value)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "ALTER TABLE course_workflow_associations ADD COLUMN unexpected TEXT"
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(
            failure_code(self.store.load(value)), "unsupported_storage_schema"
        )

    def test_save_detects_post_open_schema_drift_before_mutation(self) -> None:
        first = association("invented-course-a", "invented-workflow-a")
        second = association("invented-course-b", "invented-workflow-b")
        self.assertEqual(self.store.save(first), first)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "ALTER TABLE course_workflow_associations ADD COLUMN unexpected TEXT"
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
                    "SELECT COUNT(*) FROM course_workflow_associations"
                ).fetchone(),
                (1,),
            )
        finally:
            connection.close()

    def test_fixed_failures_redact_values_paths_and_exception_text(self) -> None:
        secret = "invented-secret-sentinel"
        value = association(secret, secret)
        result = self.store.load(value)
        self.assertEqual(failure_code(result), "association_not_found")
        self.assertNotIn(secret, repr(result))
        bad_path = Path(self._temporary.name) / secret / "association.sqlite"
        result = open_course_workflow_association_store(bad_path)
        self.assertEqual(failure_code(result), "persistence_unavailable")
        self.assertNotIn(secret, repr(result))
        self.assertEqual(self.store.save(value), value)
        with mock.patch.object(
            course_workflow_persistence,
            "_decode_row",
            side_effect=RuntimeError(secret),
        ):
            result = self.store.load(value)
        self.assertEqual(failure_code(result), "persistence_exception")
        self.assertNotIn(secret, repr(result))

    def test_ordinary_exceptions_are_contained_and_baseexception_propagates(self) -> None:
        value = association()
        with mock.patch.object(
            course_workflow_persistence,
            "_validated_association",
            side_effect=RuntimeError("invented secret"),
        ):
            self.assertEqual(
                failure_code(self.store.save(value)), "invalid_persistence_input"
            )
            self.assertEqual(
                failure_code(self.store.load(value)), "invalid_persistence_input"
            )
        self.assertEqual(self.store.save(value), value)
        with mock.patch.object(
            course_workflow_persistence,
            "_decode_row",
            side_effect=RuntimeError("invented secret"),
        ):
            self.assertEqual(
                failure_code(self.store.load(value)), "persistence_exception"
            )
        with mock.patch.object(
            course_workflow_persistence,
            "_validated_association",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.store.save(value)
        with mock.patch.object(
            course_workflow_persistence,
            "_decode_row",
            side_effect=SystemExit,
        ):
            with self.assertRaises(SystemExit):
                self.store.load(value)
        with mock.patch.object(
            course_workflow_persistence.sqlite3,
            "connect",
            side_effect=GeneratorExit,
        ):
            with self.assertRaises(GeneratorExit):
                open_course_workflow_association_store(Path("unused.sqlite"))

    def test_separate_connection_identical_saves_converge(self) -> None:
        value = association("invented-concurrent-course", "invented-concurrent-workflow")
        results: list[object] = []
        barrier = threading.Barrier(3)

        def writer() -> None:
            store = open_course_workflow_association_store(self.database_path)
            assert isinstance(store, LocalCourseWorkflowAssociationStore)
            try:
                barrier.wait()
                results.append(store.save(value))
            finally:
                store.close()

        threads = [threading.Thread(target=writer), threading.Thread(target=writer)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(results, [value, value])
        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM course_workflow_associations"
                ).fetchone(),
                (1,),
            )
        finally:
            connection.close()

    def test_separate_connection_distinct_saves_remain_independent(self) -> None:
        values = (
            association("invented-concurrent-course", "invented-workflow-a"),
            association("invented-concurrent-course", "invented-workflow-b"),
        )
        results: list[object] = []
        barrier = threading.Barrier(3)

        def writer(value: CourseWorkflowAssociation) -> None:
            store = open_course_workflow_association_store(self.database_path)
            assert isinstance(store, LocalCourseWorkflowAssociationStore)
            try:
                barrier.wait()
                results.append(store.save(value))
            finally:
                store.close()

        threads = [threading.Thread(target=writer, args=(value,)) for value in values]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertCountEqual(results, values)
        self.assertEqual(self.store.load(values[0]), values[0])
        self.assertEqual(self.store.load(values[1]), values[1])

    def test_colocation_leaves_all_predecessor_tables_unchanged(self) -> None:
        colocated_path = Path(self._temporary.name) / "colocated.sqlite"
        stores = (
            open_workflow_state_store(colocated_path),
            open_source_evidence_store(colocated_path),
            open_workflow_artifact_store(colocated_path),
            open_lecture_document_store(colocated_path),
            open_policy_content_store(colocated_path),
        )
        expected_types = (
            LocalWorkflowStateStore,
            LocalSourceEvidenceStore,
            LocalWorkflowArtifactStore,
            LocalLectureDocumentStore,
            LocalPolicyContentStore,
        )
        for store, expected_type in zip(stores, expected_types):
            self.assertIsInstance(store, expected_type)
            store.close()  # type: ignore[union-attr]
        predecessor_tables = (
            "workflow_state_snapshots",
            "source_evidence_blobs",
            "workflow_artifact_blobs",
            "lecture_documents",
            "policy_content_blobs",
        )

        def table_state() -> dict[str, tuple[object, object]]:
            connection = sqlite3.connect(colocated_path)
            try:
                return {
                    table: (
                        connection.execute(
                            "SELECT sql FROM sqlite_schema "
                            "WHERE type = 'table' AND name = ?",
                            (table,),
                        ).fetchone(),
                        connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone(),
                    )
                    for table in predecessor_tables
                }
            finally:
                connection.close()

        before = table_state()
        store = open_course_workflow_association_store(colocated_path)
        assert isinstance(store, LocalCourseWorkflowAssociationStore)
        value = association()
        try:
            self.assertEqual(store.save(value), value)
            self.assertEqual(store.load(value), value)
        finally:
            store.close()
        self.assertEqual(table_state(), before)

    def test_no_partial_lookup_or_lifecycle_api_exists(self) -> None:
        forbidden = {
            "delete",
            "update",
            "replace",
            "detach",
            "reassign",
            "list",
            "find",
            "current",
            "primary",
            "latest",
            "load_by_course",
            "load_by_workflow",
            "list_for_course",
            "list_for_workflow",
        }
        self.assertTrue(forbidden.isdisjoint(dir(self.store)))
        self.assertTrue(forbidden.isdisjoint(course_workflow_persistence.__all__))

    def test_dependency_immutability_and_standard_library_boundaries(self) -> None:
        module_path = Path("course_compiler/course_workflow_persistence.py")
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports: list[str] = []
        calls: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(("." * node.level) + (node.module or ""))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                calls.add(node.func.id)
        self.assertEqual(
            sorted(imports),
            [
                ".course",
                ".course_workflow",
                "__future__",
                "dataclasses",
                "pathlib",
                "sqlite3",
                "typing",
            ],
        )
        self.assertTrue(
            calls.isdisjoint({"__import__", "compile", "eval", "exec", "open"})
        )
        self.assertNotIn("UPDATE course_workflow_associations", source)
        self.assertNotIn("DELETE FROM course_workflow_associations", source)
        for domain_path in (
            Path("course_compiler/course.py"),
            Path("course_compiler/course_workflow.py"),
            Path("course_compiler/workflow.py"),
        ):
            self.assertNotIn(
                "course_workflow_persistence",
                domain_path.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
