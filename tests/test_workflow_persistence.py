from __future__ import annotations

import ast
import json
import sqlite3
import tempfile
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from unittest import mock

from course_compiler import (
    WORKFLOW_PERSISTENCE_SCHEMA_VERSION,
    LocalWorkflowStateStore,
    WorkflowPersistenceDiagnostic,
    WorkflowPersistenceFailure,
    WorkflowState,
    open_workflow_state_store,
)
from course_compiler import workflow_persistence
from course_compiler.workflow import (
    ApprovePriorityBasis,
    RecordSourceAssessment,
    WorkflowAdvanced,
    WorkflowTransitionRequest,
    apply_workflow_request,
    priority_subject_sha256,
)
from tests.test_workflow_contract import HostileStr, initialize_request, make_artifact


def initial_state() -> WorkflowState:
    result = apply_workflow_request(None, initialize_request())
    assert isinstance(result, WorkflowAdvanced)
    return result.state


def next_state(state: WorkflowState, marker: str = "assess") -> WorkflowState:
    request = WorkflowTransitionRequest(
        "course-workflow-transition/v2" if state.contract_version == "course-workflow-state/v2" else "course-workflow-transition/v1",
        state.workflow_id,
        state.revision,
        f"op-{marker}",
        RecordSourceAssessment(
            "record_source_assessment",
            make_artifact(f"assessment-{marker}", "source_assessment", "source-assessment-policy/v1"),
            "exam_driven",
            make_artifact(f"proposal-{marker}", "priority_proposal", "priority-basis-policy/v1"),
            make_artifact(f"hierarchy-{marker}", "evidence_hierarchy", "priority-basis-policy/v1"),
        ),
    )
    result = apply_workflow_request(state, request)
    assert isinstance(result, WorkflowAdvanced)
    return result.state


def approve_priority(state: WorkflowState) -> WorkflowState:
    assert state.priority_basis is not None
    request = WorkflowTransitionRequest(
        "course-workflow-transition/v1",
        state.workflow_id,
        state.revision,
        "op-priority",
        ApprovePriorityBasis(
            "approve_priority_basis",
            priority_subject_sha256(state.priority_basis, state.policies.priority_basis),
        ),
    )
    result = apply_workflow_request(state, request)
    assert isinstance(result, WorkflowAdvanced)
    return result.state


def failure_code(result: object) -> str:
    if not isinstance(result, WorkflowPersistenceFailure):
        raise AssertionError(f"persistence failure required, got {type(result).__name__}")
    return result.diagnostics[0].code


class WorkflowPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="workflow-store-")
        self.database_path = Path(self._temporary.name) / "workflow-state.sqlite"
        self.store = open_workflow_state_store(self.database_path)
        assert isinstance(self.store, LocalWorkflowStateStore)

    def tearDown(self) -> None:
        self.store.close()
        self._temporary.cleanup()

    def _replace_row(self, *, schema: object | None = None, payload: object | None = None, revision: object | None = None) -> None:
        connection = sqlite3.connect(self.database_path)
        try:
            current = connection.execute(
                "SELECT revision, storage_schema_version, payload FROM workflow_state_snapshots"
            ).fetchone()
            assert current is not None
            connection.execute(
                "UPDATE workflow_state_snapshots SET revision = ?, storage_schema_version = ?, payload = ?",
                (
                    current[0] if revision is None else revision,
                    current[1] if schema is None else schema,
                    current[2] if payload is None else payload,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def test_public_records_are_fixed_and_content_safe(self) -> None:
        self.assertEqual(WORKFLOW_PERSISTENCE_SCHEMA_VERSION, "local-workflow-state-sqlite/v1")
        self.assertEqual(
            [item.name for item in fields(WorkflowPersistenceDiagnostic)],
            ["code", "classification", "message"],
        )
        self.assertEqual(
            [item.name for item in fields(WorkflowPersistenceFailure)],
            ["status", "diagnostics"],
        )
        diagnostic = WorkflowPersistenceDiagnostic(
            "workflow_not_found", "storage", "No stored workflow-state snapshot was found."
        )
        self.assertFalse(hasattr(diagnostic, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            diagnostic.code = "other"  # type: ignore[misc]
        with self.assertRaises(ValueError):
            WorkflowPersistenceDiagnostic("other", "storage", "other")

    def test_revision_zero_round_trip_survives_fresh_adapter(self) -> None:
        state = initial_state()
        self.assertEqual(self.store.save(state), state)
        self.assertEqual(self.store.load(state.workflow_id), state)
        self.store.close()
        reopened = open_workflow_state_store(self.database_path)
        self.assertIsInstance(reopened, LocalWorkflowStateStore)
        assert isinstance(reopened, LocalWorkflowStateStore)
        self.store = reopened
        self.assertEqual(reopened.load(state.workflow_id), state)

    def test_representative_advanced_state_round_trips_exactly(self) -> None:
        state = next_state(initial_state())
        self.assertEqual(self.store.save(initial_state()), initial_state())
        self.assertEqual(self.store.save(state), state)
        self.assertEqual(self.store.load(state.workflow_id), state)

    def test_first_snapshot_requires_revision_zero_and_exact_repeat_is_idempotent(self) -> None:
        state = initial_state()
        self.assertEqual(failure_code(self.store.save(next_state(state))), "initial_revision_required")
        self.assertEqual(self.store.save(state), state)
        self.assertEqual(self.store.save(state), state)
        self.assertEqual(self.store.load(state.workflow_id), state)

    def test_concurrent_advance_prevents_stale_idempotent_success(self) -> None:
        state = initial_state()
        advanced = next_state(state, "concurrent")
        self.assertEqual(self.store.save(state), state)
        competing = open_workflow_state_store(self.database_path)
        assert isinstance(competing, LocalWorkflowStateStore)
        original_decode = workflow_persistence._decode_row
        raced = False

        def advance_after_authoritative_read(
            row: tuple[object, ...], *, expected_workflow_id: str
        ) -> object:
            nonlocal raced
            if not raced:
                raced = True
                self.assertEqual(competing.save(advanced), advanced)
            return original_decode(row, expected_workflow_id=expected_workflow_id)

        try:
            with mock.patch.object(
                workflow_persistence,
                "_decode_row",
                side_effect=advance_after_authoritative_read,
            ):
                result = self.store.save(state)
        finally:
            competing.close()
        self.assertTrue(raced)
        self.assertEqual(failure_code(result), "stale_revision")
        self.assertEqual(self.store.load(state.workflow_id), advanced)

    def test_valid_next_revision_succeeds_and_stale_gap_and_conflict_fail(self) -> None:
        state = initial_state()
        advanced = next_state(state, "one")
        gapped = approve_priority(advanced)
        conflicting = next_state(state, "conflict")
        self.assertEqual(self.store.save(state), state)
        self.assertEqual(failure_code(self.store.save(gapped)), "revision_gap")
        self.assertEqual(self.store.save(advanced), advanced)
        self.assertEqual(failure_code(self.store.save(state)), "stale_revision")
        self.assertEqual(failure_code(self.store.save(conflicting)), "revision_conflict")

    def test_invalid_or_forged_input_has_no_database_mutation(self) -> None:
        state = initial_state()
        self.assertEqual(self.store.save(state), state)
        forged = next_state(state)
        secret = "invented-forged-workflow-secret"
        object.__setattr__(forged, "workflow_id", HostileStr(secret))
        result = self.store.save(forged)
        self.assertEqual(failure_code(result), "invalid_persistence_input")
        self.assertNotIn(secret, repr(result))
        self.assertEqual(self.store.load(state.workflow_id), state)

    def test_workflow_state_subclass_is_rejected_before_database_mutation(self) -> None:
        state = initial_state()
        self.assertEqual(self.store.save(state), state)

        class ForgedWorkflowState(WorkflowState):
            pass

        forged = object.__new__(ForgedWorkflowState)
        for item in fields(WorkflowState):
            object.__setattr__(forged, item.name, getattr(state, item.name))
        with self.assertRaises(TypeError):
            self.store.save(forged)
        self.assertEqual(self.store.load(state.workflow_id), state)

    def test_identity_mismatch_and_unknown_workflow_fail_closed(self) -> None:
        state = initial_state()
        self.assertEqual(self.store.save(state), state)
        payload = self._stored_payload()
        payload["state"]["fields"]["workflow_id"] = "workflow-2"
        self._replace_row(payload=json.dumps(payload, sort_keys=True, separators=(",", ":")))
        self.assertEqual(failure_code(self.store.load(state.workflow_id)), "workflow_identity_mismatch")
        self.assertEqual(failure_code(self.store.load("workflow-unknown")), "workflow_not_found")

    def test_failed_atomic_replacement_preserves_previous_snapshot(self) -> None:
        state = initial_state()
        advanced = next_state(state)
        self.assertEqual(self.store.save(state), state)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "CREATE TRIGGER reject_t007_update BEFORE UPDATE ON workflow_state_snapshots "
                "BEGIN SELECT RAISE(ABORT, 'invented sqlite secret'); END"
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(failure_code(self.store.save(advanced)), "persistence_unavailable")
        self.assertEqual(self.store.load(state.workflow_id), state)

    def test_corrupted_and_incompatible_records_fail_closed(self) -> None:
        state = initial_state()
        self.assertEqual(self.store.save(state), state)
        cases = (
            ("malformed", "{not json", "stored_state_invalid"),
            ("missing-fields", json.dumps({"storage_schema_version": WORKFLOW_PERSISTENCE_SCHEMA_VERSION}), "stored_state_invalid"),
            ("unknown-fields", self._unknown_field_payload(), "stored_state_invalid"),
            ("unsupported-schema", None, "unsupported_storage_schema"),
            ("wrong-primitive", self._wrong_primitive_payload(), "stored_state_invalid"),
            ("forged-domain", self._forged_domain_payload(), "stored_state_invalid"),
            ("revision-metadata", None, "stored_state_invalid"),
        )
        for name, payload, expected in cases:
            with self.subTest(name=name):
                self._restore_valid(state)
                if name == "unsupported-schema":
                    self._replace_row(schema="unsupported-storage/v9")
                elif name == "revision-metadata":
                    self._replace_row(revision=99)
                else:
                    self._replace_row(payload=payload)
                self.assertEqual(failure_code(self.store.load(state.workflow_id)), expected)

    def test_unrecognized_sqlite_schema_fails_at_open(self) -> None:
        bad_path = Path(self._temporary.name) / "incompatible.sqlite"
        connection = sqlite3.connect(bad_path)
        try:
            connection.execute("CREATE TABLE workflow_state_snapshots (unexpected TEXT)")
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(failure_code(open_workflow_state_store(bad_path)), "unsupported_storage_schema")

    def test_database_path_payload_and_exception_text_do_not_escape_failures(self) -> None:
        secret = "invented-diagnostic-secret"
        bad_path = Path(self._temporary.name) / secret / "state.sqlite"
        failure = open_workflow_state_store(bad_path)
        self.assertEqual(failure_code(failure), "persistence_unavailable")
        self.assertNotIn(secret, repr(failure))
        state = initial_state()
        self.assertEqual(self.store.save(state), state)
        self._replace_row(payload=secret)
        failure = self.store.load(state.workflow_id)
        self.assertEqual(failure_code(failure), "stored_state_invalid")
        self.assertNotIn(secret, repr(failure))

    def test_ordinary_storage_exceptions_are_contained_and_baseexception_propagates(self) -> None:
        state = initial_state()
        self.store.close()
        self.assertEqual(failure_code(self.store.load(state.workflow_id)), "persistence_unavailable")
        reopened = open_workflow_state_store(self.database_path)
        assert isinstance(reopened, LocalWorkflowStateStore)
        self.store = reopened
        with mock.patch.object(workflow_persistence, "_serialize_payload", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.store.save(state)

    def test_public_misuse_is_static_before_storage_effects(self) -> None:
        with self.assertRaises(TypeError):
            LocalWorkflowStateStore(None, None)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            self.store.save("invented-secret")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            self.store.load(1)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            open_workflow_state_store(1)  # type: ignore[arg-type]

    def test_boundary_uses_only_sqlite_and_versioned_json(self) -> None:
        source = Path("course_compiler/workflow_persistence.py").read_text(encoding="utf-8")
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

    def _stored_payload(self) -> dict[str, object]:
        connection = sqlite3.connect(self.database_path)
        try:
            row = connection.execute("SELECT payload FROM workflow_state_snapshots").fetchone()
            assert row is not None
            return json.loads(row[0])
        finally:
            connection.close()

    def _restore_valid(self, state: WorkflowState) -> None:
        payload = workflow_persistence._serialize_payload(state)
        self._replace_row(schema=WORKFLOW_PERSISTENCE_SCHEMA_VERSION, payload=payload, revision=state.revision)

    def _wrong_primitive_payload(self) -> str:
        payload = self._stored_payload()
        payload["state"]["fields"]["revision"] = "zero"
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def _unknown_field_payload(self) -> str:
        payload = self._stored_payload()
        payload["unexpected"] = "field"
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def _forged_domain_payload(self) -> str:
        payload = self._stored_payload()
        source = payload["state"]["fields"]["source_evidence"]["items"][0]
        source["fields"]["source_id"] = "bad/path"
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


if __name__ == "__main__":
    unittest.main()
