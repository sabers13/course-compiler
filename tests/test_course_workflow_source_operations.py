from __future__ import annotations

import ast
import hashlib
import tempfile
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from unittest import mock

import course_compiler
import course_compiler.course_workflow_operations as course_workflow_operations
from course_compiler import (
    COURSE_REFERENCE_VERSION,
    COURSE_WORKFLOW_ASSOCIATION_VERSION,
    CourseReference,
    CourseWorkflowAssociation,
    LocalSourceEvidenceStore,
    PolicyContentPayload,
    ReopenedCourseWorkflowContext,
    SourceEvidencePayload,
    WorkflowSourceEvidenceReopenDiagnostic,
    WorkflowSourceEvidenceReopenFailure,
    open_source_evidence_store,
    reopen_workflow_source_evidence,
)
from course_compiler import source_persistence
from course_compiler.workflow import (
    SOURCE_EVIDENCE_REFERENCE_VERSION,
    DismissNewSource,
    InitializeWorkflow,
    InitializeWorkflowRequest,
    RecordNewSource,
    SourceEvidenceReference,
    WorkflowAdvanced,
    apply_workflow_request,
)
from tests.test_workflow_contract import make_artifact, make_policy_set, make_source
from tests.test_workflow_transitions import Scenario


PRIVATE_MARKER = "invented-private-workflow-source-marker"


def context_for_sources(
    source_bytes: tuple[tuple[str, bytes], ...],
) -> tuple[ReopenedCourseWorkflowContext, tuple[SourceEvidencePayload, ...]]:
    payloads = tuple(
        SourceEvidencePayload(
            SourceEvidenceReference(
                SOURCE_EVIDENCE_REFERENCE_VERSION,
                source_id,
                hashlib.sha256(payload).hexdigest(),
            ),
            payload,
        )
        for source_id, payload in source_bytes
    )
    initialized = apply_workflow_request(
        None,
        InitializeWorkflowRequest(
            "course-workflow-transition/v1",
            "workflow-source-reopen",
            "op-source-reopen",
            InitializeWorkflow(
                "initialize_workflow",
                make_policy_set(),
                tuple(payload.reference for payload in payloads),
            ),
        ),
    )
    assert type(initialized) is WorkflowAdvanced
    state = initialized.state
    return (
        ReopenedCourseWorkflowContext(
            CourseWorkflowAssociation(
                COURSE_WORKFLOW_ASSOCIATION_VERSION,
                CourseReference(COURSE_REFERENCE_VERSION, "invented-source-course"),
                state.workflow_id,
            ),
            state,
            tuple(
                PolicyContentPayload(getattr(state.policies, slot), slot.encode("utf-8"))
                for slot in (
                    "source_assessment",
                    "priority_basis",
                    "lecture_mapping",
                    "lecture_production",
                    "lecture_validation",
                    "workflow_handoff",
                )
            ),
        ),
        payloads,
    )


def context_for_state(state: object) -> ReopenedCourseWorkflowContext:
    assert hasattr(state, "policies")
    return ReopenedCourseWorkflowContext(
        CourseWorkflowAssociation(
            COURSE_WORKFLOW_ASSOCIATION_VERSION,
            CourseReference(COURSE_REFERENCE_VERSION, "invented-source-course"),
            state.workflow_id,  # type: ignore[union-attr]
        ),
        state,  # type: ignore[arg-type]
        tuple(
            PolicyContentPayload(getattr(state.policies, slot), slot.encode("utf-8"))
            for slot in (
                "source_assessment",
                "priority_basis",
                "lecture_mapping",
                "lecture_production",
                "lecture_validation",
                "workflow_handoff",
            )
        ),
    )


def failure_code(result: object) -> str:
    if type(result) is not WorkflowSourceEvidenceReopenFailure:
        raise AssertionError("workflow source-evidence reopen failure required")
    return result.diagnostics[0].code


class ReopenSentinel(BaseException):
    pass


class CourseWorkflowSourceOperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="source-reopen-")
        self.store = open_source_evidence_store(
            Path(self._temporary.name) / "source-evidence.sqlite"
        )
        assert type(self.store) is LocalSourceEvidenceStore

    def tearDown(self) -> None:
        self.store.close()
        self._temporary.cleanup()

    def test_public_shape_exports_and_fixed_diagnostics(self) -> None:
        self.assertEqual(
            [item.name for item in fields(WorkflowSourceEvidenceReopenDiagnostic)],
            ["code", "classification", "message"],
        )
        self.assertEqual(
            [item.name for item in fields(WorkflowSourceEvidenceReopenFailure)],
            ["status", "diagnostics"],
        )
        self.assertTrue(
            {
                "WorkflowSourceEvidenceReopenDiagnostic",
                "WorkflowSourceEvidenceReopenFailure",
                "WorkflowSourceEvidenceReopenResult",
                "reopen_workflow_source_evidence",
            }
            <= set(course_compiler.__all__)
        )
        self.assertIn(
            "reopen_workflow_source_evidence", course_workflow_operations.__all__
        )
        failure = course_workflow_operations._workflow_source_evidence_reopen_failure(
            "source_evidence_store_failed"
        )
        self.assertEqual(failure.status, "workflow_source_evidence_reopen_failed")
        self.assertFalse(hasattr(failure, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            failure.status = "other"  # type: ignore[misc]
        with self.assertRaises(ValueError):
            WorkflowSourceEvidenceReopenDiagnostic("other", "input", "other")

    def test_reopens_all_current_sources_once_in_existing_order(self) -> None:
        context, expected = context_for_sources(
            (
                ("source-alpha", b"invented alpha"),
                ("source-mu", b"invented mu"),
                ("source-zeta", b"invented zeta"),
            )
        )
        for payload in expected:
            self.assertEqual(self.store.save(payload.reference, payload.payload), payload)
        calls: list[object] = []
        original_load = self.store.load
        with mock.patch.object(
            LocalSourceEvidenceStore,
            "load",
            side_effect=lambda reference: calls.append(reference) or original_load(reference),
        ):
            result = reopen_workflow_source_evidence(context, source_store=self.store)
        self.assertEqual(result, expected)
        self.assertEqual(calls, list(context.workflow_state.source_evidence))
        self.assertEqual(tuple(value.reference.source_id for value in result), (
            "source-alpha",
            "source-mu",
            "source-zeta",
        ))
        self.assertEqual(self.store.load(expected[0].reference), expected[0])

    def test_reviewed_and_pending_sources_are_not_loaded(self) -> None:
        scenario = Scenario().production_ready()
        scenario.submit("l1", 1, "op-source-candidate")
        current = scenario.state.source_evidence[0]
        self.store.save(current, b"source-one")
        pending = make_source("source-2", "invented pending source")
        scenario.apply(
            "op-pending-source",
            RecordNewSource(
                "record_new_source",
                pending,
                make_artifact("pending-evidence", "blocker", "course-workflow-state/v1"),
            ),
        )
        context = context_for_state(scenario.state)
        calls: list[object] = []
        original_load = self.store.load
        with mock.patch.object(
            LocalSourceEvidenceStore,
            "load",
            side_effect=lambda reference: calls.append(reference) or original_load(reference),
        ):
            self.assertEqual(
                reopen_workflow_source_evidence(context, source_store=self.store),
                (SourceEvidencePayload(current, b"source-one"),),
            )
        self.assertEqual(calls, [current])
        self.assertEqual(context.workflow_state.pending_source.source_reference, pending)
        dismissed = scenario.apply(
            "op-dismiss-source",
            DismissNewSource("dismiss_new_source", pending.content_sha256),
        )
        self.assertIsInstance(dismissed, WorkflowAdvanced)
        reviewed_context = context_for_state(scenario.state)
        self.assertEqual(
            reopen_workflow_source_evidence(reviewed_context, source_store=self.store),
            (SourceEvidencePayload(current, b"source-one"),),
        )
        self.assertEqual(reviewed_context.workflow_state.reviewed_source_evidence, (pending,))

    def test_missing_sources_stop_at_first_failure_without_partial_result(self) -> None:
        context, expected = context_for_sources(
            (
                ("source-alpha", b"invented alpha"),
                ("source-mu", b"invented mu"),
                ("source-zeta", b"invented zeta"),
            )
        )
        references = context.workflow_state.source_evidence
        for missing_index in range(3):
            with self.subTest(missing_index=missing_index):
                calls: list[object] = []

                def load(reference: object) -> object:
                    calls.append(reference)
                    if reference == references[missing_index]:
                        return source_persistence._failure("source_not_found")
                    return expected[references.index(reference)]

                with mock.patch.object(LocalSourceEvidenceStore, "load", side_effect=load):
                    result = reopen_workflow_source_evidence(context, source_store=self.store)
                self.assertEqual(failure_code(result), "source_evidence_not_found")
                self.assertEqual(calls, list(references[: missing_index + 1]))
                self.assertNotIsInstance(result, tuple)

    def test_other_store_failures_are_redacted(self) -> None:
        context, _ = context_for_sources((("source-one", b"invented source"),))
        lower_failure = source_persistence._failure("unsupported_storage_schema")
        with mock.patch.object(LocalSourceEvidenceStore, "load", return_value=lower_failure):
            result = reopen_workflow_source_evidence(context, source_store=self.store)
        self.assertEqual(failure_code(result), "source_evidence_store_failed")
        self.assertNotIn(lower_failure.diagnostics[0].message, repr(result))

    def test_hostile_successes_are_inconsistent(self) -> None:
        context, expected = context_for_sources((("source-one", b"invented source"),))
        reference = expected[0].reference
        mismatch = SourceEvidencePayload(
            SourceEvidenceReference(
                SOURCE_EVIDENCE_REFERENCE_VERSION,
                "source-other",
                hashlib.sha256(b"invented other").hexdigest(),
            ),
            b"invented other",
        )
        digest_invalid = object.__new__(SourceEvidencePayload)
        object.__setattr__(digest_invalid, "reference", reference)
        object.__setattr__(digest_invalid, "payload", b"digest-invalid")
        for returned in (object(), mismatch, digest_invalid):
            with self.subTest(returned=type(returned).__name__), mock.patch.object(
                LocalSourceEvidenceStore, "load", return_value=returned
            ):
                result = reopen_workflow_source_evidence(context, source_store=self.store)
            self.assertEqual(failure_code(result), "inconsistent_reopened_source_evidence")
            self.assertNotIn(PRIVATE_MARKER, repr(result))

    def test_invalid_context_is_rejected_before_load(self) -> None:
        context, _ = context_for_sources((("source-one", b"invented source"),))
        forged = object.__new__(ReopenedCourseWorkflowContext)
        object.__setattr__(forged, "association", context.association)
        malformed = context_for_state(context.workflow_state)
        object.__setattr__(malformed.workflow_state, "stage", "not-a-workflow-stage")
        with mock.patch.object(LocalSourceEvidenceStore, "load") as load:
            wrong = reopen_workflow_source_evidence(object(), source_store=self.store)
            incomplete = reopen_workflow_source_evidence(forged, source_store=self.store)
            invalid = reopen_workflow_source_evidence(malformed, source_store=self.store)
        self.assertEqual(
            failure_code(wrong), "invalid_workflow_source_evidence_reopen_input"
        )
        self.assertEqual(
            failure_code(incomplete), "invalid_workflow_source_evidence_reopen_input"
        )
        self.assertEqual(
            failure_code(invalid), "invalid_workflow_source_evidence_reopen_input"
        )
        load.assert_not_called()

    def test_wrong_store_type_raises_before_read(self) -> None:
        context, _ = context_for_sources((("source-one", b"invented source"),))
        with self.assertRaises(TypeError):
            reopen_workflow_source_evidence(context, source_store=object())  # type: ignore[arg-type]

    def test_ordinary_exceptions_are_redacted_and_base_exceptions_propagate(self) -> None:
        context, _ = context_for_sources((("source-one", b"invented source"),))
        with mock.patch.object(
            LocalSourceEvidenceStore,
            "load",
            side_effect=RuntimeError(f"{PRIVATE_MARKER} /private/source.sqlite"),
        ):
            result = reopen_workflow_source_evidence(context, source_store=self.store)
        self.assertEqual(
            failure_code(result), "workflow_source_evidence_reopen_exception"
        )
        self.assertNotIn(PRIVATE_MARKER, repr(result))
        with mock.patch.object(
            LocalSourceEvidenceStore, "load", side_effect=ReopenSentinel
        ):
            with self.assertRaises(ReopenSentinel):
                reopen_workflow_source_evidence(context, source_store=self.store)

    def test_boundary_is_read_only_and_caller_store_remains_usable(self) -> None:
        context, expected = context_for_sources((("source-one", b"invented source"),))
        self.store.save(expected[0].reference, expected[0].payload)
        with mock.patch.object(LocalSourceEvidenceStore, "save") as save, mock.patch.object(
            LocalSourceEvidenceStore, "close"
        ) as close:
            self.assertEqual(
                reopen_workflow_source_evidence(context, source_store=self.store), expected
            )
        save.assert_not_called()
        close.assert_not_called()
        self.assertEqual(self.store.load(expected[0].reference), expected[0])
        tree = ast.parse(Path(course_workflow_operations.__file__).read_text(encoding="utf-8"))
        operation = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "reopen_workflow_source_evidence"
        )
        calls = {
            node.func.attr
            for node in ast.walk(operation)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertEqual(calls, {"append", "load"})


if __name__ == "__main__":
    unittest.main()
