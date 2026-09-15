"""Persisted write-side T027 course-workflow transition application operation.

This module is the write-side sibling of `course_workflow_operations.py`.
That module remains exact reopening/read/build application behavior with no
persistence writes, preserving its accepted T016/T017-era invariant. This
module owns the opposite half: applying accepted T003 requests against
durable Course Compiler state by delegating all semantic validation and
transition behavior to the unchanged T003 `apply_workflow_request` and then
persisting exactly what T003 decided to produce.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from .course_workflow import CourseWorkflowAssociation
from .course_workflow_persistence import (
    CourseWorkflowPersistenceFailure,
    LocalCourseWorkflowAssociationStore,
)
from .lecture_document_persistence import LocalLectureDocumentStore
from .policy_persistence import LocalPolicyContentStore, PolicyPersistenceFailure
from .rendering import DocumentReference
from .source_persistence import LocalSourceEvidenceStore, SourcePersistenceFailure
from .workflow import (
    InitializeWorkflowRequest,
    MarkBlocked,
    RecordCandidateValidation,
    RecordLectureMap,
    RecordNewSource,
    RecordOperationFailure,
    RecordSourceAssessment,
    SubmitLectureCandidate,
    WorkflowAdvanced,
    WorkflowBlocked,
    WorkflowFailed,
    WorkflowState,
    WorkflowTransitionRequest,
    WorkflowTransitionResult,
    apply_workflow_request,
)
from .workflow_artifact_persistence import (
    LocalWorkflowArtifactStore,
    WorkflowArtifactPersistenceFailure,
)
from .workflow_persistence import (
    LocalWorkflowStateStore,
    WorkflowPersistenceFailure,
)


__all__ = [
    "PersistedCourseWorkflowOperationDiagnostic",
    "PersistedCourseWorkflowOperationFailure",
    "PersistedCourseWorkflowOperationResult",
    "apply_persisted_course_workflow_request",
]


_POLICY_SLOT_NAMES = (
    "source_assessment",
    "priority_basis",
    "lecture_mapping",
    "lecture_production",
    "lecture_validation",
    "workflow_handoff",
)

_PERSISTED_WORKFLOW_DIAGNOSTICS = {
    "invalid_persisted_workflow_input": (
        "input",
        "The persisted course-workflow request input is invalid.",
    ),
    "association_not_found": (
        "storage",
        "The exact course-workflow association was not found.",
    ),
    "association_store_failed": (
        "storage",
        "The course-workflow association store failed.",
    ),
    "workflow_not_found": (
        "storage",
        "The associated workflow state was not found.",
    ),
    "workflow_store_failed": (
        "storage",
        "The workflow-state store failed.",
    ),
    "policy_not_found": (
        "storage",
        "A required workflow policy was not found.",
    ),
    "policy_store_failed": (
        "storage",
        "The policy-content store failed.",
    ),
    "source_evidence_not_found": (
        "storage",
        "A required source-evidence dependency was not found.",
    ),
    "source_evidence_store_failed": (
        "storage",
        "The source-evidence store failed.",
    ),
    "workflow_artifact_not_found": (
        "storage",
        "A required workflow-artifact dependency was not found.",
    ),
    "workflow_artifact_store_failed": (
        "storage",
        "The workflow-artifact store failed.",
    ),
    "lecture_document_store_failed": (
        "storage",
        "The lecture-document store failed.",
    ),
    "persisted_workflow_operation_exception": (
        "application",
        "The persisted course-workflow operation failed.",
    ),
}


@dataclass(frozen=True, slots=True)
class PersistedCourseWorkflowOperationDiagnostic:
    """One fixed, content-safe T027 persisted-operation diagnostic."""

    code: str
    classification: Literal["input", "storage", "application"]
    message: str

    def __post_init__(self) -> None:
        if self.code not in _PERSISTED_WORKFLOW_DIAGNOSTICS:
            raise ValueError("persisted workflow operation diagnostic code is not registered")
        classification, message = _PERSISTED_WORKFLOW_DIAGNOSTICS[self.code]
        if self.classification != classification or self.message != message:
            raise ValueError("persisted workflow operation diagnostic fields are not registered")


@dataclass(frozen=True, slots=True)
class PersistedCourseWorkflowOperationFailure:
    """A fail-closed T027 persistence-layer failure, distinct from a T003 rejection."""

    status: Literal["persisted_workflow_operation_failed"]
    diagnostics: tuple[PersistedCourseWorkflowOperationDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "persisted_workflow_operation_failed":
            raise ValueError("persisted workflow operation failure status is invalid")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) != 1
            or not _is_valid_persisted_workflow_diagnostic(self.diagnostics[0])
        ):
            raise ValueError("persisted workflow operation failure diagnostics are invalid")


PersistedCourseWorkflowOperationResult: TypeAlias = (
    WorkflowTransitionResult | PersistedCourseWorkflowOperationFailure
)


def apply_persisted_course_workflow_request(
    association: CourseWorkflowAssociation,
    request: InitializeWorkflowRequest | WorkflowTransitionRequest,
    *,
    association_store: LocalCourseWorkflowAssociationStore,
    workflow_store: LocalWorkflowStateStore,
    policy_store: LocalPolicyContentStore,
    source_store: LocalSourceEvidenceStore,
    artifact_store: LocalWorkflowArtifactStore,
    document_store: LocalLectureDocumentStore,
) -> PersistedCourseWorkflowOperationResult:
    """Apply one exact accepted T003 request against durable Course Compiler state.

    This resolves exact durable dependencies, delegates all semantic
    validation and transition behavior to the unchanged T003
    `apply_workflow_request`, and then persists exactly the immutable
    content and workflow-state revision that T003 decided to produce.
    `WorkflowRejected` and `WorkflowIdempotentRepeat` are pure T003 outcomes
    and are returned unchanged with no new authoritative write. For
    `submit_lecture_candidate`, the newly submitted `LectureDocument` is
    always persisted before the next `WorkflowState` revision; if workflow
    persistence subsequently fails, the document is an acceptable orphan and
    the previous workflow revision remains authoritative.
    """

    if type(association) is not CourseWorkflowAssociation:
        raise TypeError("association must be exactly CourseWorkflowAssociation")
    if type(request) is not InitializeWorkflowRequest and type(request) is not WorkflowTransitionRequest:
        raise TypeError(
            "request must be exactly InitializeWorkflowRequest or WorkflowTransitionRequest"
        )
    if type(association_store) is not LocalCourseWorkflowAssociationStore:
        raise TypeError(
            "association_store must be exactly LocalCourseWorkflowAssociationStore"
        )
    if type(workflow_store) is not LocalWorkflowStateStore:
        raise TypeError("workflow_store must be exactly LocalWorkflowStateStore")
    if type(policy_store) is not LocalPolicyContentStore:
        raise TypeError("policy_store must be exactly LocalPolicyContentStore")
    if type(source_store) is not LocalSourceEvidenceStore:
        raise TypeError("source_store must be exactly LocalSourceEvidenceStore")
    if type(artifact_store) is not LocalWorkflowArtifactStore:
        raise TypeError("artifact_store must be exactly LocalWorkflowArtifactStore")
    if type(document_store) is not LocalLectureDocumentStore:
        raise TypeError("document_store must be exactly LocalLectureDocumentStore")

    try:
        try:
            requested_association = _reconstruct_association(association)
        except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
            return _persisted_workflow_failure("invalid_persisted_workflow_input")
        if requested_association.workflow_id != request.workflow_id:
            return _persisted_workflow_failure("invalid_persisted_workflow_input")

        if type(request) is InitializeWorkflowRequest:
            return _apply_persisted_initialize(
                requested_association,
                request,
                association_store=association_store,
                workflow_store=workflow_store,
                policy_store=policy_store,
                source_store=source_store,
            )

        return _apply_persisted_transition(
            requested_association,
            request,  # type: ignore[arg-type]
            association_store=association_store,
            workflow_store=workflow_store,
            source_store=source_store,
            artifact_store=artifact_store,
            document_store=document_store,
        )
    except Exception:
        return _persisted_workflow_failure("persisted_workflow_operation_exception")


def _apply_persisted_initialize(
    association: CourseWorkflowAssociation,
    request: InitializeWorkflowRequest,
    *,
    association_store: LocalCourseWorkflowAssociationStore,
    workflow_store: LocalWorkflowStateStore,
    policy_store: LocalPolicyContentStore,
    source_store: LocalSourceEvidenceStore,
) -> PersistedCourseWorkflowOperationResult:
    for reference in request.payload.source_evidence:
        source_result = source_store.load(reference)
        if type(source_result) is SourcePersistenceFailure:
            if _failure_code(source_result) == "source_not_found":
                return _persisted_workflow_failure("source_evidence_not_found")
            return _persisted_workflow_failure("source_evidence_store_failed")

    for slot_name in _POLICY_SLOT_NAMES:
        reference = getattr(request.payload.policies, slot_name)
        policy_result = policy_store.load(reference)
        if type(policy_result) is PolicyPersistenceFailure:
            if _failure_code(policy_result) == "policy_not_found":
                return _persisted_workflow_failure("policy_not_found")
            return _persisted_workflow_failure("policy_store_failed")

    transition_result = apply_workflow_request(None, request)
    if type(transition_result) is not WorkflowAdvanced:
        return transition_result

    saved_state = workflow_store.save(transition_result.state)
    if type(saved_state) is WorkflowPersistenceFailure:
        return _persisted_workflow_failure("workflow_store_failed")

    saved_association = association_store.save(association)
    if type(saved_association) is CourseWorkflowPersistenceFailure:
        return _persisted_workflow_failure("association_store_failed")

    return transition_result


_ARTIFACT_DEPENDENCY_ACCESSORS = {
    RecordSourceAssessment: lambda payload: (
        payload.assessment_reference,
        payload.priority_proposal_reference,
        payload.evidence_hierarchy_reference,
    ),
    RecordLectureMap: lambda payload: (payload.map_reference,),
    RecordCandidateValidation: lambda payload: (payload.validation.validation_reference,),
    RecordOperationFailure: lambda payload: (payload.evidence_reference,),
    MarkBlocked: lambda payload: (payload.evidence_reference,),
    RecordNewSource: lambda payload: (payload.evidence_reference,),
}


def _apply_persisted_transition(
    association: CourseWorkflowAssociation,
    request: WorkflowTransitionRequest,
    *,
    association_store: LocalCourseWorkflowAssociationStore,
    workflow_store: LocalWorkflowStateStore,
    source_store: LocalSourceEvidenceStore,
    artifact_store: LocalWorkflowArtifactStore,
    document_store: LocalLectureDocumentStore,
) -> PersistedCourseWorkflowOperationResult:
    association_result = association_store.load(association)
    if type(association_result) is CourseWorkflowPersistenceFailure:
        if _failure_code(association_result) == "association_not_found":
            return _persisted_workflow_failure("association_not_found")
        return _persisted_workflow_failure("association_store_failed")
    try:
        reopened_association = _reconstruct_association(association_result)
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        return _persisted_workflow_failure("invalid_persisted_workflow_input")
    if reopened_association != association:
        return _persisted_workflow_failure("invalid_persisted_workflow_input")

    workflow_result = workflow_store.load(reopened_association.workflow_id)
    if type(workflow_result) is WorkflowPersistenceFailure:
        if _failure_code(workflow_result) == "workflow_not_found":
            return _persisted_workflow_failure("workflow_not_found")
        return _persisted_workflow_failure("workflow_store_failed")
    current_state: WorkflowState = workflow_result  # type: ignore[assignment]

    payload = request.payload
    payload_type = type(payload)

    if type(payload) is RecordNewSource:
        source_result = source_store.load(payload.source_reference)
        if type(source_result) is SourcePersistenceFailure:
            if _failure_code(source_result) == "source_not_found":
                return _persisted_workflow_failure("source_evidence_not_found")
            return _persisted_workflow_failure("source_evidence_store_failed")

    dependency_accessor = _ARTIFACT_DEPENDENCY_ACCESSORS.get(payload_type)
    if dependency_accessor is not None:
        for reference in dependency_accessor(payload):
            artifact_result = artifact_store.load(reference)
            if type(artifact_result) is WorkflowArtifactPersistenceFailure:
                if _failure_code(artifact_result) == "artifact_not_found":
                    return _persisted_workflow_failure("workflow_artifact_not_found")
                return _persisted_workflow_failure("workflow_artifact_store_failed")

    transition_result = apply_workflow_request(current_state, request)
    if not isinstance(transition_result, (WorkflowAdvanced, WorkflowBlocked, WorkflowFailed)):
        return transition_result

    if type(payload) is SubmitLectureCandidate:
        document_result = document_store.save(payload.document)
        if type(document_result) is not DocumentReference:
            return _persisted_workflow_failure("lecture_document_store_failed")

    saved_state = workflow_store.save(transition_result.state)
    if type(saved_state) is WorkflowPersistenceFailure:
        return _persisted_workflow_failure("workflow_store_failed")

    return transition_result


def _reconstruct_association(value: object) -> CourseWorkflowAssociation:
    if type(value) is not CourseWorkflowAssociation:
        raise ValueError("course-workflow association is invalid")
    try:
        return CourseWorkflowAssociation(
            value.association_version,
            value.course_reference,
            value.workflow_id,
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        raise ValueError("course-workflow association is invalid") from None


def _failure_code(value: object) -> str | None:
    try:
        diagnostics = value.diagnostics
        if type(diagnostics) is tuple and len(diagnostics) == 1:
            code = diagnostics[0].code
            return code if type(code) is str else None
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        pass
    return None


def _is_valid_persisted_workflow_diagnostic(value: object) -> bool:
    if type(value) is not PersistedCourseWorkflowOperationDiagnostic:
        return False
    try:
        PersistedCourseWorkflowOperationDiagnostic(
            value.code,
            value.classification,
            value.message,
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        return False
    return True


def _persisted_workflow_failure(code: str) -> PersistedCourseWorkflowOperationFailure:
    classification, message = _PERSISTED_WORKFLOW_DIAGNOSTICS[code]
    return PersistedCourseWorkflowOperationFailure(
        status="persisted_workflow_operation_failed",
        diagnostics=(
            PersistedCourseWorkflowOperationDiagnostic(code, classification, message),
        ),
    )
