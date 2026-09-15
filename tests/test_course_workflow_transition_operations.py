from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from unittest import mock

import course_compiler
import course_compiler.course_workflow_operations as operations
import course_compiler.course_workflow_transition_operations as transition_operations
from course_compiler import (
    COURSE_REFERENCE_VERSION,
    COURSE_WORKFLOW_ASSOCIATION_VERSION,
    CourseReference,
    CourseWorkflowAssociation,
    LectureDocument,
    LocalCourseWorkflowAssociationStore,
    LocalLectureDocumentStore,
    LocalPolicyContentStore,
    LocalSourceEvidenceStore,
    LocalWorkflowArtifactStore,
    LocalWorkflowStateStore,
    PersistedCourseWorkflowOperationDiagnostic,
    PersistedCourseWorkflowOperationFailure,
    PolicyReference,
    SourceProvenance,
    WorkflowAdvanced,
    WorkflowBlocked,
    WorkflowFailed,
    WorkflowIdempotentRepeat,
    WorkflowPolicySet,
    WorkflowRejected,
    apply_persisted_course_workflow_request,
    ingest_source_evidence,
    ingest_workflow_artifact,
    open_course_workflow_association_store,
    open_lecture_document_store,
    open_policy_content_store,
    open_source_evidence_store,
    open_workflow_artifact_store,
    open_workflow_state_store,
    reopen_accepted_lecture_documents,
    reopen_course_workflow_context,
    reopen_workflow_source_evidence,
)
from course_compiler.workflow import (
    SOURCE_EVIDENCE_REFERENCE_VERSION,
    WORKFLOW_TRANSITION_VERSION,
    ApproveLectureMap,
    ApprovePriorityBasis,
    InitializeWorkflow,
    InitializeWorkflowRequest,
    MarkBlocked,
    RecordCandidateValidation,
    RecordLectureMap,
    RecordNewSource,
    RecordOperationFailure,
    RecordSourceAssessment,
    SourceEvidenceReference,
    SubmitLectureCandidate,
    ValidationRecord,
    WorkflowArtifactReference,
    WorkflowState,
    WorkflowTransitionRequest,
    candidate_subject_sha256,
    map_subject_sha256,
    priority_subject_sha256,
)
from course_compiler.workflow_policy import POLICY_VERSIONS
from course_compiler.lecture_document_persistence import LectureDocumentPersistenceFailure
from course_compiler.workflow_persistence import WorkflowPersistenceFailure


PRIVATE_MARKER = "invented-private-transition-marker"
POLICY_SLOTS = (
    "source_assessment",
    "priority_basis",
    "lecture_mapping",
    "lecture_production",
    "lecture_validation",
    "workflow_handoff",
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def failure_code(result: object) -> str:
    if type(result) is not PersistedCourseWorkflowOperationFailure:
        raise AssertionError(
            f"persisted workflow operation failure required, got {type(result).__name__}"
        )
    return result.diagnostics[0].code


class Harness(unittest.TestCase):
    """Wires up all six T027 stores plus one caller-owned association."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="transition-")
        root = Path(self._temporary.name)
        self.association_store = open_course_workflow_association_store(
            root / "association.sqlite"
        )
        self.workflow_store = open_workflow_state_store(root / "workflow.sqlite")
        self.policy_store = open_policy_content_store(root / "policy.sqlite")
        self.source_store = open_source_evidence_store(root / "source.sqlite")
        self.artifact_store = open_workflow_artifact_store(root / "artifact.sqlite")
        self.document_store = open_lecture_document_store(root / "document.sqlite")
        assert type(self.association_store) is LocalCourseWorkflowAssociationStore
        assert type(self.workflow_store) is LocalWorkflowStateStore
        assert type(self.policy_store) is LocalPolicyContentStore
        assert type(self.source_store) is LocalSourceEvidenceStore
        assert type(self.artifact_store) is LocalWorkflowArtifactStore
        assert type(self.document_store) is LocalLectureDocumentStore
        self.association = CourseWorkflowAssociation(
            COURSE_WORKFLOW_ASSOCIATION_VERSION,
            CourseReference(COURSE_REFERENCE_VERSION, "invented-course"),
            "workflow-1",
        )
        self._artifact_counter = 0

    def tearDown(self) -> None:
        self.association_store.close()
        self.workflow_store.close()
        self.policy_store.close()
        self.source_store.close()
        self.artifact_store.close()
        self.document_store.close()
        self._temporary.cleanup()

    # -- generic apply -----------------------------------------------

    def apply(self, request: object, *, association: object | None = None) -> object:
        return apply_persisted_course_workflow_request(
            association if association is not None else self.association,
            request,
            association_store=self.association_store,
            workflow_store=self.workflow_store,
            policy_store=self.policy_store,
            source_store=self.source_store,
            artifact_store=self.artifact_store,
            document_store=self.document_store,
        )

    # -- synthetic fixture builders ------------------------------------

    def make_policies(self) -> WorkflowPolicySet:
        references = []
        for kind in POLICY_SLOTS:
            payload = f"invented policy bytes for {kind}".encode("utf-8")
            reference = PolicyReference(kind, POLICY_VERSIONS[kind], hashlib.sha256(payload).hexdigest())
            saved = self.policy_store.save(reference, payload)
            assert saved == reference or getattr(saved, "reference", None) == reference
            references.append(reference)
        return WorkflowPolicySet(*references)

    def make_source(self, source_id: str = "source-1") -> SourceEvidenceReference:
        result = ingest_source_evidence(
            source_id, f"invented source bytes for {source_id}".encode("utf-8"),
            source_store=self.source_store,
        )
        assert not isinstance(result, PersistedCourseWorkflowOperationFailure)
        return result.reference

    def make_artifact(self, kind: str) -> WorkflowArtifactReference:
        self._artifact_counter += 1
        artifact_id = f"artifact-{kind}-{self._artifact_counter}"
        result = ingest_workflow_artifact(
            artifact_id, kind, f"invented artifact bytes {artifact_id}".encode("utf-8"),
            artifact_store=self.artifact_store,
        )
        assert type(result).__name__ == "WorkflowArtifactPayload", result
        return result.reference

    def make_document(self, lecture_id: str = "l1", order: int = 1, source: str = "invented candidate") -> LectureDocument:
        return LectureDocument(
            contract_version="lecture-document/v1",
            document_id=lecture_id,
            order=order,
            source_text=source,
            provenance=SourceProvenance(content_sha256=digest(source)),
        )

    # -- driving helpers ------------------------------------------------

    def initialize(self, *, lecture_count: int = 1, source_ids: tuple[str, ...] = ("source-1",)) -> WorkflowAdvanced:
        policies = self.make_policies()
        sources = tuple(self.make_source(source_id) for source_id in source_ids)
        request = InitializeWorkflowRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", "op-init",
            InitializeWorkflow("initialize_workflow", policies, sources),
        )
        result = self.apply(request)
        assert isinstance(result, WorkflowAdvanced), result
        return result

    def state(self) -> WorkflowState:
        loaded = self.workflow_store.load("workflow-1")
        assert type(loaded) is WorkflowState, loaded
        return loaded

    def source_assessed(self, *, operation_id: str = "op-assess") -> object:
        assessment = self.make_artifact("source_assessment")
        proposal = self.make_artifact("priority_proposal")
        hierarchy = self.make_artifact("evidence_hierarchy")
        request = WorkflowTransitionRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", self.state().revision, operation_id,
            RecordSourceAssessment(
                "record_source_assessment", assessment, "exam_driven", proposal, hierarchy
            ),
        )
        return self.apply(request)

    def priority_approved(self) -> object:
        result = self.source_assessed()
        assert isinstance(result, WorkflowAdvanced), result
        subject = priority_subject_sha256(result.state.priority_basis, result.state.policies.priority_basis)
        request = WorkflowTransitionRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", result.state.revision, "op-priority-approve",
            ApprovePriorityBasis("approve_priority_basis", subject),
        )
        return self.apply(request)

    def map_proposed(self, *, lecture_count: int = 1) -> object:
        result = self.priority_approved()
        assert isinstance(result, WorkflowAdvanced), result
        map_artifact = self.make_artifact("lecture_map")
        ids = tuple(f"l{i}" for i in range(1, lecture_count + 1))
        request = WorkflowTransitionRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", result.state.revision, "op-map",
            RecordLectureMap("record_lecture_map", map_artifact, ids),
        )
        return self.apply(request)

    def production_ready(self, *, lecture_count: int = 1) -> object:
        result = self.map_proposed(lecture_count=lecture_count)
        assert isinstance(result, WorkflowAdvanced), result
        subject = map_subject_sha256(result.state.lecture_map, result.state.policies.lecture_mapping)
        request = WorkflowTransitionRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", result.state.revision, "op-map-approve",
            ApproveLectureMap("approve_lecture_map", subject),
        )
        return self.apply(request)

    def submit(self, lecture_id: str, order: int, operation_id: str, *, source: str = "invented candidate") -> object:
        document = self.make_document(lecture_id, order, source)
        request = WorkflowTransitionRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", self.state().revision, operation_id,
            SubmitLectureCandidate("submit_lecture_candidate", lecture_id, document),
        )
        return self.apply(request)

    def validate(self, lecture_id: str, operation_id: str, *, disposition: str = "passed") -> object:
        state = self.state()
        progress = next(item for item in state.lecture_progress if item.lecture_id == lecture_id)
        assert progress.candidate is not None
        subject = candidate_subject_sha256(progress.candidate)
        validation_artifact = self.make_artifact("validation")
        record = ValidationRecord(
            subject, "lecture-validation-policy/v1", state.policies.lecture_validation.content_sha256,
            disposition, validation_artifact,  # type: ignore[arg-type]
        )
        request = WorkflowTransitionRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", state.revision, operation_id,
            RecordCandidateValidation("record_candidate_validation", lecture_id, subject, record),
        )
        return self.apply(request)


class PublicContractTests(Harness):
    def test_public_exports_and_fixed_diagnostics(self) -> None:
        self.assertEqual(
            [item.name for item in fields(PersistedCourseWorkflowOperationDiagnostic)],
            ["code", "classification", "message"],
        )
        self.assertEqual(
            [item.name for item in fields(PersistedCourseWorkflowOperationFailure)],
            ["status", "diagnostics"],
        )
        expected_exports = {
            "PersistedCourseWorkflowOperationDiagnostic",
            "PersistedCourseWorkflowOperationFailure",
            "PersistedCourseWorkflowOperationResult",
            "apply_persisted_course_workflow_request",
        }
        self.assertTrue(expected_exports <= set(course_compiler.__all__))
        self.assertIn(
            "apply_persisted_course_workflow_request", transition_operations.__all__
        )
        with self.assertRaises(ValueError):
            PersistedCourseWorkflowOperationDiagnostic("other", "storage", "other")  # type: ignore[arg-type]
        failure = transition_operations._persisted_workflow_failure("workflow_store_failed")
        self.assertFalse(hasattr(failure, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            failure.status = "other"  # type: ignore[misc]

    def test_runtime_type_misuse_raises(self) -> None:
        request = InitializeWorkflowRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", "op-init",
            InitializeWorkflow("initialize_workflow", self.make_policies(), (self.make_source(),)),
        )
        with self.assertRaises(TypeError):
            apply_persisted_course_workflow_request(
                object(), request,
                association_store=self.association_store, workflow_store=self.workflow_store,
                policy_store=self.policy_store, source_store=self.source_store,
                artifact_store=self.artifact_store, document_store=self.document_store,
            )  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            apply_persisted_course_workflow_request(
                self.association, object(),
                association_store=self.association_store, workflow_store=self.workflow_store,
                policy_store=self.policy_store, source_store=self.source_store,
                artifact_store=self.artifact_store, document_store=self.document_store,
            )  # type: ignore[arg-type]
        for keyword in (
            "association_store", "workflow_store", "policy_store",
            "source_store", "artifact_store", "document_store",
        ):
            kwargs = dict(
                association_store=self.association_store, workflow_store=self.workflow_store,
                policy_store=self.policy_store, source_store=self.source_store,
                artifact_store=self.artifact_store, document_store=self.document_store,
            )
            kwargs[keyword] = object()
            with self.subTest(keyword=keyword):
                with self.assertRaises(TypeError):
                    apply_persisted_course_workflow_request(self.association, request, **kwargs)  # type: ignore[arg-type]


class InitializationTests(Harness):
    def test_success_persists_state_and_association(self) -> None:
        result = self.initialize()
        self.assertIsInstance(result, WorkflowAdvanced)
        stored_state = self.workflow_store.load("workflow-1")
        self.assertEqual(stored_state, result.state)
        stored_association = self.association_store.load(self.association)
        self.assertEqual(stored_association, self.association)

    def test_missing_policy_dependency_fails_closed_without_writes(self) -> None:
        policies = self.make_policies()
        bad_reference = PolicyReference(
            "workflow_handoff", POLICY_VERSIONS["workflow_handoff"], digest("never persisted")
        )
        bad_policies = WorkflowPolicySet(
            *(
                getattr(policies, slot) if slot != "workflow_handoff" else bad_reference
                for slot in POLICY_SLOTS
            )
        )
        source = self.make_source()
        request = InitializeWorkflowRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", "op-init-bad",
            InitializeWorkflow("initialize_workflow", bad_policies, (source,)),
        )
        result = self.apply(request)
        self.assertEqual(failure_code(result), "policy_not_found")
        self.assertIsInstance(self.workflow_store.load("workflow-1"), WorkflowPersistenceFailure)

    def test_missing_source_evidence_dependency_fails_closed_without_writes(self) -> None:
        policies = self.make_policies()
        unsaved_source = SourceEvidenceReference(
            SOURCE_EVIDENCE_REFERENCE_VERSION, "never-ingested", digest("nope")
        )
        request = InitializeWorkflowRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", "op-init-bad-source",
            InitializeWorkflow("initialize_workflow", policies, (unsaved_source,)),
        )
        result = self.apply(request)
        self.assertEqual(failure_code(result), "source_evidence_not_found")
        self.assertIsInstance(self.workflow_store.load("workflow-1"), WorkflowPersistenceFailure)

    def test_t015_reopen_succeeds_after_initialize(self) -> None:
        self.initialize()
        context = reopen_course_workflow_context(
            self.association,
            association_store=self.association_store,
            workflow_store=self.workflow_store,
            policy_store=self.policy_store,
        )
        self.assertNotIsInstance(context, operations.CourseWorkflowReopenFailure)
        self.assertEqual(context.association, self.association)


class SourceAssessmentTests(Harness):
    def setUp(self) -> None:
        super().setUp()
        self.initialize()

    def test_success_advances_and_persists(self) -> None:
        result = self.source_assessed()
        self.assertIsInstance(result, WorkflowAdvanced)
        self.assertEqual(self.workflow_store.load("workflow-1"), result.state)

    def test_each_missing_artifact_dependency_fails_closed(self) -> None:
        good_assessment = self.make_artifact("source_assessment")
        good_proposal = self.make_artifact("priority_proposal")
        good_hierarchy = self.make_artifact("evidence_hierarchy")
        missing = WorkflowArtifactReference(
            "workflow-artifact-reference/v1", "never-ingested", "source_assessment",
            digest("nope"), "source-assessment-policy/v1",
        )
        combos = (
            ("assessment", missing, good_proposal, good_hierarchy),
            ("proposal", good_assessment, WorkflowArtifactReference(
                "workflow-artifact-reference/v1", "never-ingested-2", "priority_proposal",
                digest("nope2"), "priority-basis-policy/v1",
            ), good_hierarchy),
            ("hierarchy", good_assessment, good_proposal, WorkflowArtifactReference(
                "workflow-artifact-reference/v1", "never-ingested-3", "evidence_hierarchy",
                digest("nope3"), "priority-basis-policy/v1",
            )),
        )
        for label, assessment, proposal, hierarchy in combos:
            with self.subTest(missing=label):
                request = WorkflowTransitionRequest(
                    WORKFLOW_TRANSITION_VERSION, "workflow-1", self.state().revision, f"op-assess-missing-{label}",
                    RecordSourceAssessment("record_source_assessment", assessment, "exam_driven", proposal, hierarchy),
                )
                result = self.apply(request)
                self.assertEqual(failure_code(result), "workflow_artifact_not_found")
                self.assertEqual(self.state().revision, 0)


class LectureMapTests(Harness):
    def setUp(self) -> None:
        super().setUp()
        self.initialize()

    def test_success_advances_and_persists(self) -> None:
        result = self.map_proposed()
        self.assertIsInstance(result, WorkflowAdvanced)
        self.assertEqual(self.workflow_store.load("workflow-1"), result.state)

    def test_missing_map_artifact_fails_closed(self) -> None:
        priority_result = self.priority_approved()
        assert isinstance(priority_result, WorkflowAdvanced)
        missing = WorkflowArtifactReference(
            "workflow-artifact-reference/v1", "never-ingested-map", "lecture_map",
            digest("nope"), "lecture-map-policy/v1",
        )
        request = WorkflowTransitionRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", priority_result.state.revision, "op-map-missing",
            RecordLectureMap("record_lecture_map", missing, ("l1",)),
        )
        result = self.apply(request)
        self.assertEqual(failure_code(result), "workflow_artifact_not_found")
        self.assertEqual(self.state().revision, priority_result.state.revision)


class LectureCandidateTests(Harness):
    def setUp(self) -> None:
        super().setUp()
        self.initialize()
        result = self.production_ready()
        assert isinstance(result, WorkflowAdvanced)

    def test_accepted_document_and_state_are_persisted_in_order(self) -> None:
        result = self.submit("l1", 1, "op-submit")
        self.assertIsInstance(result, WorkflowAdvanced)
        candidate_reference = result.state.lecture_progress[0].candidate
        stored_document = self.document_store.load(candidate_reference)
        self.assertNotIsInstance(stored_document, LectureDocumentPersistenceFailure)
        self.assertEqual(self.workflow_store.load("workflow-1"), result.state)

    def test_rejected_candidate_causes_no_document_or_state_write(self) -> None:
        state = self.state()
        bad_document = self.make_document("l1", 1, "bad\r\n")
        request = WorkflowTransitionRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", state.revision, "op-submit-bad",
            SubmitLectureCandidate("submit_lecture_candidate", "l1", bad_document),
        )
        result = self.apply(request)
        self.assertIsInstance(result, WorkflowRejected)
        self.assertEqual(self.state(), state)
        self.assertEqual(len(self.document_store._connection.execute(
            "SELECT 1 FROM lecture_documents"
        ).fetchall()), 0)

    def test_document_store_failure_prevents_workflow_state_write(self) -> None:
        state = self.state()
        with mock.patch.object(
            LocalLectureDocumentStore,
            "save",
            return_value=LectureDocumentPersistenceFailure(
                "persistence_failed",
                (course_compiler.LectureDocumentPersistenceDiagnostic(
                    "persistence_exception", "adapter", "The local lecture-document adapter failed.",
                ),),
            ),
        ):
            result = self.submit("l1", 1, "op-submit-doc-fail")
        self.assertEqual(failure_code(result), "lecture_document_store_failed")
        self.assertEqual(self.state(), state)

    def test_workflow_store_failure_after_document_save_leaves_only_orphan_document(self) -> None:
        state = self.state()
        with mock.patch.object(
            LocalWorkflowStateStore,
            "save",
            return_value=WorkflowPersistenceFailure(
                "persistence_failed",
                (course_compiler.WorkflowPersistenceDiagnostic(
                    "persistence_exception", "adapter", "The local workflow-state adapter failed.",
                ),),
            ),
        ):
            result = self.submit("l1", 1, "op-submit-state-fail")
        self.assertEqual(failure_code(result), "workflow_store_failed")
        # previous workflow revision remains authoritative
        self.assertEqual(self.workflow_store.load("workflow-1"), state)
        # the document was still saved (a harmless orphan)
        rows = self.document_store._connection.execute("SELECT 1 FROM lecture_documents").fetchall()
        self.assertEqual(len(rows), 1)


class ValidationFailureBlockedNewSourceTests(Harness):
    def setUp(self) -> None:
        super().setUp()
        self.initialize()
        result = self.production_ready()
        assert isinstance(result, WorkflowAdvanced)
        submitted = self.submit("l1", 1, "op-submit")
        assert isinstance(submitted, WorkflowAdvanced)

    def test_candidate_validation_success(self) -> None:
        result = self.validate("l1", "op-validate")
        self.assertIsInstance(result, WorkflowAdvanced)
        self.assertEqual(self.workflow_store.load("workflow-1"), result.state)

    def test_candidate_validation_missing_evidence_fails_closed(self) -> None:
        state = self.state()
        progress = state.lecture_progress[0]
        subject = candidate_subject_sha256(progress.candidate)
        missing = WorkflowArtifactReference(
            "workflow-artifact-reference/v1", "never-ingested-validation", "validation",
            digest("nope"), "lecture-validation-policy/v1",
        )
        record = ValidationRecord(
            subject, "lecture-validation-policy/v1", state.policies.lecture_validation.content_sha256,
            "passed", missing,  # type: ignore[arg-type]
        )
        request = WorkflowTransitionRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", state.revision, "op-validate-missing",
            RecordCandidateValidation("record_candidate_validation", "l1", subject, record),
        )
        result = self.apply(request)
        self.assertEqual(failure_code(result), "workflow_artifact_not_found")
        self.assertEqual(self.state().revision, state.revision)

    def test_operation_failure_missing_evidence_fails_closed(self) -> None:
        state = self.state()
        missing = WorkflowArtifactReference(
            "workflow-artifact-reference/v1", "never-ingested-failure", "failure",
            digest("nope"), "course-workflow-state/v1",
        )
        request = WorkflowTransitionRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", state.revision, "op-fail-missing",
            RecordOperationFailure(
                "record_operation_failure", "record_candidate_validation",
                "lecture_validation_operation_failed", "l1", missing,
            ),
        )
        result = self.apply(request)
        self.assertEqual(failure_code(result), "workflow_artifact_not_found")
        self.assertEqual(self.state().revision, state.revision)

    def test_operation_failure_success_advances_and_persists(self) -> None:
        state = self.state()
        evidence = self.make_artifact("failure")
        request = WorkflowTransitionRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", state.revision, "op-fail",
            RecordOperationFailure(
                "record_operation_failure", "record_candidate_validation",
                "lecture_validation_operation_failed", "l1", evidence,
            ),
        )
        result = self.apply(request)
        self.assertIsInstance(result, WorkflowFailed)
        self.assertEqual(self.workflow_store.load("workflow-1"), result.state)

    def test_mark_blocked_missing_evidence_fails_closed(self) -> None:
        state = self.state()
        missing = WorkflowArtifactReference(
            "workflow-artifact-reference/v1", "never-ingested-blocker", "blocker",
            digest("nope"), "course-workflow-state/v1",
        )
        request = WorkflowTransitionRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", state.revision, "op-block-missing",
            MarkBlocked("mark_blocked", "private_artifact_unavailable", "l1", digest("subject"), missing),
        )
        result = self.apply(request)
        self.assertEqual(failure_code(result), "workflow_artifact_not_found")
        self.assertEqual(self.state().revision, state.revision)

    def test_mark_blocked_success_advances_and_persists(self) -> None:
        state = self.state()
        evidence = self.make_artifact("blocker")
        request = WorkflowTransitionRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", state.revision, "op-block",
            MarkBlocked(
                "mark_blocked", "private_artifact_unavailable", "l1",
                digest(state.active_lecture_id or "l1"), evidence,
            ),
        )
        result = self.apply(request)
        self.assertIsInstance(result, WorkflowBlocked)
        self.assertEqual(self.workflow_store.load("workflow-1"), result.state)

    def test_record_new_source_missing_source_evidence_fails_closed(self) -> None:
        state = self.state()
        unsaved_source = SourceEvidenceReference(
            SOURCE_EVIDENCE_REFERENCE_VERSION, "never-ingested-source", digest("nope")
        )
        evidence = self.make_artifact("blocker")
        request = WorkflowTransitionRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", state.revision, "op-new-source-missing-source",
            RecordNewSource("record_new_source", unsaved_source, evidence),
        )
        result = self.apply(request)
        self.assertEqual(failure_code(result), "source_evidence_not_found")
        self.assertEqual(self.state().revision, state.revision)

    def test_record_new_source_missing_artifact_fails_closed(self) -> None:
        state = self.state()
        new_source = self.make_source("source-new")
        missing = WorkflowArtifactReference(
            "workflow-artifact-reference/v1", "never-ingested-blocker-2", "blocker",
            digest("nope"), "course-workflow-state/v1",
        )
        request = WorkflowTransitionRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", state.revision, "op-new-source-missing-artifact",
            RecordNewSource("record_new_source", new_source, missing),
        )
        result = self.apply(request)
        self.assertEqual(failure_code(result), "workflow_artifact_not_found")
        self.assertEqual(self.state().revision, state.revision)

    def test_record_new_source_success_advances_and_persists(self) -> None:
        state = self.state()
        new_source = self.make_source("source-new")
        evidence = self.make_artifact("blocker")
        request = WorkflowTransitionRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", state.revision, "op-new-source",
            RecordNewSource("record_new_source", new_source, evidence),
        )
        result = self.apply(request)
        self.assertIsInstance(result, WorkflowBlocked)
        self.assertEqual(self.workflow_store.load("workflow-1"), result.state)


class T003SemanticsPassthroughTests(Harness):
    def setUp(self) -> None:
        super().setUp()
        self.initialize()

    def test_illegal_transition_is_rejected_without_writes(self) -> None:
        request = WorkflowTransitionRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", 0, "op-illegal",
            ApproveLectureMap("approve_lecture_map", "0" * 64),
        )
        result = self.apply(request)
        self.assertIsInstance(result, WorkflowRejected)
        self.assertEqual(self.state().revision, 0)

    def test_stale_revision_is_rejected_without_writes(self) -> None:
        first = self.source_assessed()
        assert isinstance(first, WorkflowAdvanced)
        stale_request = WorkflowTransitionRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", 0, "op-stale",
            RecordSourceAssessment(
                "record_source_assessment",
                self.make_artifact("source_assessment"), "exam_driven",
                self.make_artifact("priority_proposal"), self.make_artifact("evidence_hierarchy"),
            ),
        )
        result = self.apply(stale_request)
        self.assertIsInstance(result, WorkflowRejected)
        self.assertEqual(result.diagnostics[0].code, "stale_revision")
        self.assertEqual(self.state().revision, first.state.revision)

    def test_repeated_operation_id_with_identical_request_is_idempotent(self) -> None:
        assessment = self.make_artifact("source_assessment")
        proposal = self.make_artifact("priority_proposal")
        hierarchy = self.make_artifact("evidence_hierarchy")
        payload = RecordSourceAssessment(
            "record_source_assessment", assessment, "exam_driven", proposal, hierarchy
        )
        request = WorkflowTransitionRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", 0, "op-repeat", payload
        )
        first = self.apply(request)
        self.assertIsInstance(first, WorkflowAdvanced)
        second = self.apply(request)
        self.assertIsInstance(second, WorkflowIdempotentRepeat)
        self.assertEqual(self.state(), first.state)

    def test_repeated_operation_id_with_different_request_is_rejected(self) -> None:
        assessment = self.make_artifact("source_assessment")
        proposal = self.make_artifact("priority_proposal")
        hierarchy = self.make_artifact("evidence_hierarchy")
        first_request = WorkflowTransitionRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", 0, "op-reused",
            RecordSourceAssessment(
                "record_source_assessment", assessment, "exam_driven", proposal, hierarchy
            ),
        )
        first = self.apply(first_request)
        self.assertIsInstance(first, WorkflowAdvanced)
        other_assessment = self.make_artifact("source_assessment")
        second_request = WorkflowTransitionRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-1", first.state.revision, "op-reused",
            RecordSourceAssessment(
                "record_source_assessment", other_assessment, "exam_driven", proposal, hierarchy
            ),
        )
        second = self.apply(second_request)
        self.assertIsInstance(second, WorkflowRejected)
        self.assertEqual(second.diagnostics[0].code, "operation_id_reuse")
        self.assertEqual(self.state(), first.state)

    def test_unknown_association_fails_closed(self) -> None:
        unknown_association = CourseWorkflowAssociation(
            COURSE_WORKFLOW_ASSOCIATION_VERSION,
            CourseReference(COURSE_REFERENCE_VERSION, "no-such-course"),
            "workflow-999",
        )
        request = WorkflowTransitionRequest(
            WORKFLOW_TRANSITION_VERSION, "workflow-999", 0, "op-unknown",
            ApproveLectureMap("approve_lecture_map", "0" * 64),
        )
        result = self.apply(request, association=unknown_association)
        self.assertEqual(failure_code(result), "association_not_found")


class FullSyntheticWorkflowTests(Harness):
    def test_full_one_lecture_workflow_reaches_completed_and_reopens(self) -> None:
        self.initialize()
        result = self.production_ready()
        self.assertIsInstance(result, WorkflowAdvanced)
        submitted = self.submit("l1", 1, "op-submit")
        self.assertIsInstance(submitted, WorkflowAdvanced)
        validated = self.validate("l1", "op-validate")
        self.assertIsInstance(validated, WorkflowAdvanced)
        self.assertEqual(validated.state.stage, "completed")

        context = reopen_course_workflow_context(
            self.association,
            association_store=self.association_store,
            workflow_store=self.workflow_store,
            policy_store=self.policy_store,
        )
        self.assertNotIsInstance(context, operations.CourseWorkflowReopenFailure)
        documents = reopen_accepted_lecture_documents(context, document_store=self.document_store)
        self.assertIsInstance(documents, tuple)
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0].document_id, "l1")

        sources = reopen_workflow_source_evidence(context, source_store=self.source_store)
        self.assertIsInstance(sources, tuple)
        self.assertEqual(len(sources), 1)
