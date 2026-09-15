from __future__ import annotations

import ast
import hashlib
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path
from unittest import mock

import course_compiler
import course_compiler.course_workflow_operations as course_workflow_operations
from course_compiler import (
    COURSE_REFERENCE_VERSION,
    COURSE_WORKFLOW_ASSOCIATION_VERSION,
    AcceptedLectureDocumentReopenDiagnostic,
    AcceptedLectureDocumentReopenFailure,
    CourseReference,
    CourseWorkflowAssociation,
    LectureDocument,
    LocalLectureDocumentStore,
    PolicyContentPayload,
    ReopenedCourseWorkflowContext,
    SourceProvenance,
    open_lecture_document_store,
    reopen_accepted_lecture_documents,
)
from course_compiler import lecture_document_persistence
from course_compiler.workflow import ordered_accepted_documents
from tests.test_workflow_contract import make_document
from tests.test_workflow_transitions import Scenario


PRIVATE_MARKER = "invented-private-document-marker"


def context_for(state: object) -> ReopenedCourseWorkflowContext:
    assert hasattr(state, "policies")
    return ReopenedCourseWorkflowContext(
        CourseWorkflowAssociation(
            COURSE_WORKFLOW_ASSOCIATION_VERSION,
            CourseReference(COURSE_REFERENCE_VERSION, "invented-course"),
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


def accepted_scenario(count: int, accepted_count: int | None = None) -> Scenario:
    scenario = Scenario(count).production_ready()
    for index in range(1, (accepted_count if accepted_count is not None else count) + 1):
        scenario.submit(f"l{index}", index, f"op-submit-{index}")
        scenario.validate(f"l{index}", f"op-validate-{index}")
    return scenario


def documents_for(context: ReopenedCourseWorkflowContext) -> tuple[LectureDocument, ...]:
    return tuple(
        make_document(reference.document_id, reference.order)
        for reference in ordered_accepted_documents(context.workflow_state)
    )


def failure_code(result: object) -> str:
    if type(result) is not AcceptedLectureDocumentReopenFailure:
        raise AssertionError("accepted document reopen failure required")
    return result.diagnostics[0].code


class ReopenSentinel(BaseException):
    pass


class CourseWorkflowLectureOperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="document-reopen-")
        self.store = open_lecture_document_store(
            Path(self._temporary.name) / "documents.sqlite"
        )
        assert type(self.store) is LocalLectureDocumentStore

    def tearDown(self) -> None:
        self.store.close()
        self._temporary.cleanup()

    def _persist(self, context: ReopenedCourseWorkflowContext) -> tuple[LectureDocument, ...]:
        documents = documents_for(context)
        for document in documents:
            self.store.save(document)
        return documents

    def test_public_shape_exports_and_fixed_diagnostics(self) -> None:
        self.assertEqual(
            [item.name for item in fields(AcceptedLectureDocumentReopenDiagnostic)],
            ["code", "classification", "message"],
        )
        self.assertEqual(
            [item.name for item in fields(AcceptedLectureDocumentReopenFailure)],
            ["status", "diagnostics"],
        )
        self.assertTrue(
            {
                "AcceptedLectureDocumentReopenDiagnostic",
                "AcceptedLectureDocumentReopenFailure",
                "AcceptedLectureDocumentReopenResult",
                "reopen_accepted_lecture_documents",
            }
            <= set(course_compiler.__all__)
        )
        self.assertIn(
            "reopen_accepted_lecture_documents",
            course_workflow_operations.__all__,
        )
        with self.assertRaises(ValueError):
            AcceptedLectureDocumentReopenDiagnostic("other", "input", "other")

    def test_completed_context_loads_and_returns_canonical_order(self) -> None:
        context = context_for(accepted_scenario(3).state)
        expected = self._persist(context)
        calls: list[object] = []
        original_load = self.store.load
        with mock.patch.object(
            LocalLectureDocumentStore,
            "load",
            side_effect=lambda reference: calls.append(reference) or original_load(reference),
        ):
            result = reopen_accepted_lecture_documents(context, document_store=self.store)
        self.assertEqual(result, expected)
        self.assertEqual(calls, list(ordered_accepted_documents(context.workflow_state)))
        self.assertEqual(tuple(document.order for document in result), (1, 2, 3))
        self.assertEqual(self.store.load(ordered_accepted_documents(context.workflow_state)[0]), expected[0])

    def test_in_progress_zero_accepted_documents_succeeds_without_load(self) -> None:
        context = context_for(Scenario(2).state)
        with mock.patch.object(LocalLectureDocumentStore, "load") as load:
            result = reopen_accepted_lecture_documents(context, document_store=self.store)
        self.assertEqual(result, ())
        load.assert_not_called()

    def test_in_progress_partial_accepted_documents_reopen(self) -> None:
        context = context_for(accepted_scenario(3, 2).state)
        expected = self._persist(context)
        self.assertNotEqual(context.workflow_state.stage, "completed")
        self.assertEqual(
            reopen_accepted_lecture_documents(context, document_store=self.store), expected
        )

    def test_each_missing_document_fails_closed_and_short_circuits(self) -> None:
        context = context_for(accepted_scenario(3).state)
        expected = documents_for(context)
        references = ordered_accepted_documents(context.workflow_state)
        for missing_index in range(3):
            with self.subTest(missing_index=missing_index):
                calls: list[object] = []

                def load(reference: object) -> object:
                    calls.append(reference)
                    if reference == references[missing_index]:
                        return lecture_document_persistence._failure("document_not_found")
                    return expected[references.index(reference)]

                with mock.patch.object(LocalLectureDocumentStore, "load", side_effect=load):
                    result = reopen_accepted_lecture_documents(context, document_store=self.store)
                self.assertEqual(failure_code(result), "lecture_document_not_found")
                self.assertEqual(calls, list(references[: missing_index + 1]))
                self.assertNotIsInstance(result, tuple)

    def test_non_not_found_store_failure_is_redacted(self) -> None:
        context = context_for(accepted_scenario(1).state)
        lower_failure = lecture_document_persistence._failure("unsupported_storage_schema")
        with mock.patch.object(
            LocalLectureDocumentStore, "load", return_value=lower_failure
        ):
            result = reopen_accepted_lecture_documents(context, document_store=self.store)
        self.assertEqual(failure_code(result), "lecture_document_store_failed")
        self.assertNotIn(lower_failure.diagnostics[0].message, repr(result))

    def test_malformed_invalid_and_mismatched_successes_are_inconsistent(self) -> None:
        context = context_for(accepted_scenario(1).state)
        reference = ordered_accepted_documents(context.workflow_state)[0]
        forged = object.__new__(LectureDocument)
        object.__setattr__(forged, "contract_version", "lecture-document/v1")
        object.__setattr__(forged, "document_id", "l1")
        object.__setattr__(forged, "order", 1)
        object.__setattr__(forged, "source_text", "invalid\rsource")
        object.__setattr__(forged, "provenance", SourceProvenance("0" * 64))
        mismatch = LectureDocument(
            "lecture-document/v1",
            "l1",
            1,
            "invented different document",
            SourceProvenance(
                hashlib.sha256(b"invented different document").hexdigest()
            ),
        )
        for returned in (object(), forged, mismatch):
            with self.subTest(returned=type(returned).__name__), mock.patch.object(
                LocalLectureDocumentStore, "load", return_value=returned
            ):
                result = reopen_accepted_lecture_documents(context, document_store=self.store)
            self.assertEqual(failure_code(result), "inconsistent_reopened_documents")
            self.assertNotIn(PRIVATE_MARKER, repr(result))
        self.assertEqual(reference.document_id, "l1")

    def test_invalid_context_is_rejected_before_document_load(self) -> None:
        context = context_for(accepted_scenario(1).state)
        forged = object.__new__(ReopenedCourseWorkflowContext)
        object.__setattr__(forged, "association", context.association)
        invalid_nested = context_for(accepted_scenario(1).state)
        object.__setattr__(invalid_nested.workflow_state, "stage", "not-a-workflow-stage")
        with mock.patch.object(LocalLectureDocumentStore, "load") as load:
            wrong = reopen_accepted_lecture_documents(object(), document_store=self.store)
            invalid = reopen_accepted_lecture_documents(forged, document_store=self.store)
            invalid_state = reopen_accepted_lecture_documents(
                invalid_nested, document_store=self.store
            )
        self.assertEqual(failure_code(wrong), "invalid_accepted_document_reopen_input")
        self.assertEqual(failure_code(invalid), "invalid_accepted_document_reopen_input")
        self.assertEqual(
            failure_code(invalid_state), "invalid_accepted_document_reopen_input"
        )
        load.assert_not_called()

    def test_wrong_store_type_raises_before_any_load(self) -> None:
        context = context_for(accepted_scenario(1).state)
        with self.assertRaises(TypeError):
            reopen_accepted_lecture_documents(context, document_store=object())  # type: ignore[arg-type]

    def test_ordinary_exceptions_are_redacted_and_base_exceptions_propagate(self) -> None:
        context = context_for(accepted_scenario(1).state)
        with mock.patch.object(
            LocalLectureDocumentStore, "load", side_effect=RuntimeError(PRIVATE_MARKER)
        ):
            result = reopen_accepted_lecture_documents(context, document_store=self.store)
        self.assertEqual(failure_code(result), "accepted_document_reopen_exception")
        self.assertNotIn(PRIVATE_MARKER, repr(result))
        for exception in (KeyboardInterrupt(), SystemExit(), ReopenSentinel()):
            with self.subTest(exception=type(exception).__name__), mock.patch.object(
                LocalLectureDocumentStore, "load", side_effect=exception
            ):
                with self.assertRaises(type(exception)):
                    reopen_accepted_lecture_documents(context, document_store=self.store)

    def test_operation_does_not_call_writes_or_lower_layer_operations(self) -> None:
        source = Path(course_compiler.__file__).parent / "course_workflow_operations.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        called_names = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertNotIn("save", called_names)
        self.assertNotIn("close", called_names)
        self.assertNotIn("render", called_names)
        self.assertNotIn("compile", called_names)
        self.assertNotIn("assemble_course_tex", called_names)
        for lower_name in (
            "contracts.py",
            "lecture_document_persistence.py",
            "workflow.py",
        ):
            tree = ast.parse((source.parent / lower_name).read_text(encoding="utf-8"))
            self.assertFalse(
                any(
                    isinstance(node, ast.ImportFrom)
                    and node.level == 1
                    and node.module == "course_workflow_operations"
                    for node in ast.walk(tree)
                )
            )
