"""Pure versioned workflow and state contract for Course Compiler."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, fields, replace
from typing import Literal, TypeAlias

from .contracts import LectureDocument, RenderDiagnostic, SourceProvenance, has_validation_errors, validate_document
from .rendering import DocumentReference, document_reference
from .workflow_policy import (
    ARTIFACT_PRODUCERS,
    DIAGNOSTIC_MESSAGES,
    DIAGNOSTIC_REGISTRY,
    DIAGNOSTIC_STAGES,
    EXTERNAL_PREREQUISITE_BLOCKER_STAGES,
    FAILURE_REGISTRY,
    NEW_SOURCE_STAGES,
    NORMAL_DISPOSITIONS,
    POLICY_VERSIONS,
    PRIVATE_ARTIFACT_BLOCKER_STARTS,
    RECEIPT_OUTCOMES,
    SOURCE_EVIDENCE_REFERENCE_VERSION,
    WORKFLOW_ACTIONS,
    WORKFLOW_ARTIFACT_REFERENCE_VERSION,
    WORKFLOW_ARTIFACT_PRODUCER_VERSION,
    WORKFLOW_HANDOFF_VERSION,
    WORKFLOW_STAGES,
    WORKFLOW_STATE_VERSION,
    WORKFLOW_TRANSITION_VERSION,
    paired_handoff_version,
    paired_state_version,
    ArtifactKind,
    BlockerCode,
    FailureCode,
    PolicyKind,
    PriorityMode,
    ProducerVersion,
    ProgressStatus,
    WorkflowAction,
    WorkflowDisposition,
    WorkflowResultStatus,
    WorkflowStage,
)


Sha256Hex: TypeAlias = str
SafeId: TypeAlias = str
WorkflowId: TypeAlias = str
OperationId: TypeAlias = str
SourceId: TypeAlias = str
ArtifactId: TypeAlias = str
DiagnosticSubjectId: TypeAlias = str
LectureId: TypeAlias = str
MapPosition: TypeAlias = int
Revision: TypeAlias = int
Attempt: TypeAlias = int
WorkflowDiagnosticCode: TypeAlias = str

_SAFE_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_LECTURE_ID_RE = re.compile(r"l[1-9][0-9]{0,3}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_REVISION = 9_223_372_036_854_775_807
_MAX_ATTEMPT = 2_147_483_647
_ZERO_DIGEST = "0" * 64
_CATEGORY_RANK = {"warning": 0, "blocked": 1, "failure": 2, "validation": 3}
_STAGE_RANK = {name: index for index, name in enumerate(DIAGNOSTIC_STAGES)}
_CANDIDATE_WARNING_MAP = {
    "empty_source": "candidate_empty_source",
    "short_source": "candidate_short_source",
    "unclosed_code_fence": "candidate_unclosed_code_fence",
    "unclosed_display_math": "candidate_unclosed_display_math",
}


def _valid_safe_id(value: object) -> bool:
    return (
        type(value) is str
        and value not in (".", "..")
        and _SAFE_ID_RE.fullmatch(value) is not None
    )


def _valid_lecture_id(value: object) -> bool:
    return type(value) is str and _LECTURE_ID_RE.fullmatch(value) is not None


def _valid_digest(value: object) -> bool:
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _valid_int(value: object, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _valid_literal(value: object, permitted: object) -> bool:
    return type(value) is str and value in permitted


def _require_exact_shape(value: object, expected_type: type, message: str) -> None:
    if type(value) is not expected_type:
        raise ValueError(message)
    try:
        for item in fields(expected_type):
            getattr(value, item.name)
    except (AttributeError, TypeError):
        raise ValueError(message) from None


def _revalidate_exact(value: object, expected_type: type, message: str) -> None:
    _require_exact_shape(value, expected_type, message)
    try:
        if expected_type is DocumentReference:
            if (
                type(value.contract_version) is not str
                or type(value.document_id) is not str
                or type(value.order) is not int
                or type(value.content_sha256) is not str
            ):
                raise ValueError(message)
        elif expected_type is LectureDocument:
            if (
                type(value.contract_version) is not str
                or type(value.document_id) is not str
                or type(value.order) is not int
                or type(value.source_text) is not str
            ):
                raise ValueError(message)
            _require_exact_shape(value.provenance, SourceProvenance, message)
            if type(value.provenance.content_sha256) is not str:
                raise ValueError(message)
        post_init = getattr(value, "__post_init__", None)
        if post_init is not None:
            post_init()
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        raise ValueError(message) from None


def _safe_rejected_state(value: object) -> WorkflowState | None:
    try:
        _require_exact_shape(value, WorkflowState, "")
    except ValueError:
        return None
    return value


def _exact_tuple(value: object, item_type: type, message: str) -> tuple:
    if type(value) is not tuple:
        raise ValueError(message)
    for item in value:
        _revalidate_exact(item, item_type, message)
    return value


def _require_action(value: object) -> None:
    if not _valid_literal(value, WORKFLOW_ACTIONS):
        raise ValueError("workflow action is invalid")


def _require_artifact_kind(reference: object, kind: str, message: str) -> None:
    _revalidate_exact(reference, WorkflowArtifactReference, message)
    if reference.artifact_kind != kind:
        raise ValueError(message)


@dataclass(frozen=True, slots=True)
class SourceEvidenceReference:
    reference_version: Literal["source-evidence-reference/v1"]
    source_id: SourceId
    content_sha256: Sha256Hex

    def __post_init__(self) -> None:
        if not _valid_literal(self.reference_version, (SOURCE_EVIDENCE_REFERENCE_VERSION,)):
            raise ValueError("source evidence reference version is unsupported")
        if not _valid_safe_id(self.source_id):
            raise ValueError("source evidence ID is invalid")
        if not _valid_digest(self.content_sha256):
            raise ValueError("source evidence digest is invalid")


@dataclass(frozen=True, slots=True)
class WorkflowArtifactReference:
    reference_version: Literal["workflow-artifact-reference/v1"]
    artifact_id: ArtifactId
    artifact_kind: ArtifactKind
    content_sha256: Sha256Hex
    producer_version: ProducerVersion

    def __post_init__(self) -> None:
        if not _valid_literal(self.reference_version, (WORKFLOW_ARTIFACT_REFERENCE_VERSION,)):
            raise ValueError("workflow artifact reference version is unsupported")
        if not _valid_safe_id(self.artifact_id):
            raise ValueError("workflow artifact ID is invalid")
        if not _valid_literal(self.artifact_kind, ARTIFACT_PRODUCERS):
            raise ValueError("workflow artifact kind is invalid")
        if not _valid_digest(self.content_sha256):
            raise ValueError("workflow artifact digest is invalid")
        if not _valid_literal(self.producer_version, (ARTIFACT_PRODUCERS[self.artifact_kind],)):
            raise ValueError("workflow artifact producer is invalid")


@dataclass(frozen=True, slots=True)
class PolicyReference:
    policy_kind: PolicyKind
    policy_version: ProducerVersion
    content_sha256: Sha256Hex

    def __post_init__(self) -> None:
        if not _valid_literal(self.policy_kind, POLICY_VERSIONS):
            raise ValueError("policy kind is invalid")
        if not _valid_literal(self.policy_version, (POLICY_VERSIONS[self.policy_kind],)):
            raise ValueError("policy kind and version do not match")
        if not _valid_digest(self.content_sha256):
            raise ValueError("policy digest is invalid")


@dataclass(frozen=True, slots=True)
class WorkflowPolicySet:
    source_assessment: PolicyReference
    priority_basis: PolicyReference
    lecture_mapping: PolicyReference
    lecture_production: PolicyReference
    lecture_validation: PolicyReference
    workflow_handoff: PolicyReference

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            _revalidate_exact(value, PolicyReference, "workflow policy set member is invalid")
            if value.policy_kind != item.name:
                raise ValueError("workflow policy set member is invalid")


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    gate: Literal["priority_basis", "lecture_map", "new_source_irrelevant", "map_reopen"]
    decision: Literal["approved", "rejected"]
    subject_sha256: Sha256Hex
    state_revision: Revision
    operation_id: OperationId

    def __post_init__(self) -> None:
        if not _valid_literal(self.gate, ("priority_basis", "lecture_map", "new_source_irrelevant", "map_reopen")):
            raise ValueError("approval gate is invalid")
        if not _valid_literal(self.decision, ("approved", "rejected")):
            raise ValueError("approval decision is invalid")
        if not _valid_digest(self.subject_sha256):
            raise ValueError("approval subject digest is invalid")
        if not _valid_int(self.state_revision, 1, _MAX_REVISION):
            raise ValueError("approval revision is invalid")
        if not _valid_safe_id(self.operation_id):
            raise ValueError("approval operation ID is invalid")


@dataclass(frozen=True, slots=True)
class SourceAssessmentRecord:
    source_set_sha256: Sha256Hex
    assessment_reference: WorkflowArtifactReference
    recorded_revision: Revision

    def __post_init__(self) -> None:
        if not _valid_digest(self.source_set_sha256):
            raise ValueError("source assessment subject digest is invalid")
        _require_artifact_kind(self.assessment_reference, "source_assessment", "source assessment reference is invalid")
        if not _valid_int(self.recorded_revision, 1, _MAX_REVISION):
            raise ValueError("source assessment revision is invalid")


@dataclass(frozen=True, slots=True)
class PriorityBasisRecord:
    primary_mode: PriorityMode
    proposal_reference: WorkflowArtifactReference
    evidence_hierarchy_reference: WorkflowArtifactReference
    status: Literal["proposed", "approved"]
    approval: ApprovalRecord | None

    def __post_init__(self) -> None:
        if not _valid_literal(self.primary_mode, ("exam_driven", "sheet_driven", "slide_driven", "custom")):
            raise ValueError("priority mode is invalid")
        _require_artifact_kind(self.proposal_reference, "priority_proposal", "priority proposal reference is invalid")
        _require_artifact_kind(self.evidence_hierarchy_reference, "evidence_hierarchy", "evidence hierarchy reference is invalid")
        if not _valid_literal(self.status, ("proposed", "approved")):
            raise ValueError("priority basis status is invalid")
        if self.status == "proposed":
            if self.approval is not None:
                raise ValueError("proposed priority basis cannot have approval")
        elif self.status == "approved":
            _revalidate_exact(self.approval, ApprovalRecord, "approved priority basis requires matching approval")
            if self.approval.gate != "priority_basis" or self.approval.decision != "approved":
                raise ValueError("approved priority basis requires matching approval")


def _valid_lecture_sequence(value: object) -> bool:
    return (
        type(value) is tuple
        and 1 <= len(value) <= 9999
        and all(type(item) is str and item == f"l{index}" for index, item in enumerate(value, 1))
    )


@dataclass(frozen=True, slots=True)
class LectureMapRecord:
    map_reference: WorkflowArtifactReference
    lecture_ids: tuple[LectureId, ...]
    status: Literal["proposed", "approved"]
    approval: ApprovalRecord | None
    reserved_lecture_ids: tuple[LectureId, ...]

    def __post_init__(self) -> None:
        _require_artifact_kind(self.map_reference, "lecture_map", "lecture map reference is invalid")
        if not _valid_lecture_sequence(self.lecture_ids):
            raise ValueError("lecture map IDs are invalid")
        if type(self.reserved_lecture_ids) is not tuple or any(not _valid_lecture_id(item) for item in self.reserved_lecture_ids):
            raise ValueError("reserved lecture IDs are invalid")
        if len(set(self.reserved_lecture_ids)) != len(self.reserved_lecture_ids):
            raise ValueError("reserved lecture IDs are invalid")
        if self.lecture_ids[: len(self.reserved_lecture_ids)] != self.reserved_lecture_ids:
            raise ValueError("reserved lecture IDs are invalid")
        if not _valid_literal(self.status, ("proposed", "approved")):
            raise ValueError("lecture map status is invalid")
        if self.status == "proposed":
            if self.approval is not None:
                raise ValueError("proposed lecture map cannot have approval")
        elif self.status == "approved":
            _revalidate_exact(self.approval, ApprovalRecord, "approved lecture map requires matching approval")
            if self.approval.gate != "lecture_map" or self.approval.decision != "approved":
                raise ValueError("approved lecture map requires matching approval")


@dataclass(frozen=True, slots=True)
class MapReopenContext:
    baseline_map_reference: WorkflowArtifactReference
    baseline_lecture_ids: tuple[LectureId, ...]
    baseline_reserved_lecture_ids: tuple[LectureId, ...]
    baseline_map_subject_sha256: Sha256Hex
    baseline_approval: ApprovalRecord
    created_revision: Revision

    def __post_init__(self) -> None:
        _require_artifact_kind(self.baseline_map_reference, "lecture_map", "map reopen baseline reference is invalid")
        if not _valid_lecture_sequence(self.baseline_lecture_ids):
            raise ValueError("map reopen baseline IDs are invalid")
        if type(self.baseline_reserved_lecture_ids) is not tuple or any(not _valid_lecture_id(item) for item in self.baseline_reserved_lecture_ids):
            raise ValueError("map reopen reserved IDs are invalid")
        if self.baseline_lecture_ids[: len(self.baseline_reserved_lecture_ids)] != self.baseline_reserved_lecture_ids:
            raise ValueError("map reopen reserved IDs are invalid")
        if not _valid_digest(self.baseline_map_subject_sha256):
            raise ValueError("map reopen subject digest is invalid")
        _revalidate_exact(self.baseline_approval, ApprovalRecord, "map reopen baseline approval is invalid")
        if self.baseline_approval.gate != "lecture_map" or self.baseline_approval.decision != "approved" or self.baseline_approval.subject_sha256 != self.baseline_map_subject_sha256:
            raise ValueError("map reopen baseline approval is invalid")
        if not _valid_int(self.created_revision, 1, _MAX_REVISION):
            raise ValueError("map reopen revision is invalid")


@dataclass(frozen=True, slots=True)
class PendingSourceRecord:
    source_reference: SourceEvidenceReference
    detected_stage: Literal["lecture_production", "lecture_validation"]
    detected_revision: Revision
    blocker_subject_sha256: Sha256Hex

    def __post_init__(self) -> None:
        _revalidate_exact(self.source_reference, SourceEvidenceReference, "pending source reference is invalid")
        if not _valid_literal(self.detected_stage, NEW_SOURCE_STAGES):
            raise ValueError("pending source stage is invalid")
        if not _valid_int(self.detected_revision, 1, _MAX_REVISION):
            raise ValueError("pending source revision is invalid")
        if not _valid_digest(self.blocker_subject_sha256) or self.blocker_subject_sha256 != self.source_reference.content_sha256:
            raise ValueError("pending source subject is invalid")


@dataclass(frozen=True, slots=True)
class ValidationRecord:
    candidate_subject_sha256: Sha256Hex
    validation_policy_version: Literal["lecture-validation-policy/v1"]
    validation_policy_sha256: Sha256Hex
    disposition: Literal["passed", "passed_with_warnings", "rejected"]
    validation_reference: WorkflowArtifactReference

    def __post_init__(self) -> None:
        if not _valid_digest(self.candidate_subject_sha256):
            raise ValueError("candidate subject digest is invalid")
        if not _valid_literal(self.validation_policy_version, ("lecture-validation-policy/v1",)):
            raise ValueError("validation policy version is invalid")
        if not _valid_digest(self.validation_policy_sha256):
            raise ValueError("validation policy digest is invalid")
        if not _valid_literal(self.disposition, ("passed", "passed_with_warnings", "rejected")):
            raise ValueError("validation disposition is invalid")
        _require_artifact_kind(self.validation_reference, "validation", "validation reference is invalid")


@dataclass(frozen=True, slots=True)
class LectureProgress:
    lecture_id: LectureId
    map_position: MapPosition
    status: ProgressStatus
    attempt: Attempt
    candidate: DocumentReference | None
    validation: ValidationRecord | None
    accepted_document: DocumentReference | None

    def __post_init__(self) -> None:
        if not _valid_lecture_id(self.lecture_id):
            raise ValueError("lecture progress ID is invalid")
        if not _valid_int(self.map_position, 1, 9999):
            raise ValueError("lecture progress position is invalid")
        if not _valid_int(self.attempt, 0, _MAX_ATTEMPT):
            raise ValueError("lecture progress attempt is invalid")
        if not _valid_literal(self.status, ("pending", "candidate", "retry_required", "correction_required", "accepted")):
            raise ValueError("lecture progress status is invalid")
        if self.candidate is not None:
            _revalidate_exact(self.candidate, DocumentReference, "lecture progress candidate is invalid")
        if self.validation is not None:
            _revalidate_exact(self.validation, ValidationRecord, "lecture progress validation is invalid")
        if self.accepted_document is not None:
            _revalidate_exact(self.accepted_document, DocumentReference, "lecture progress accepted document is invalid")
        if self.status == "pending":
            valid = self.attempt == 0 and self.candidate is None and self.validation is None and self.accepted_document is None
        elif self.status == "candidate":
            valid = self.attempt >= 1 and self.candidate is not None and self.validation is None and self.accepted_document is None
        elif self.status == "retry_required":
            valid = self.attempt >= 1 and self.candidate is not None and self.validation is not None and self.validation.disposition == "rejected" and self.accepted_document is None
        elif self.status == "correction_required":
            valid = self.attempt >= 1 and self.candidate is not None and self.validation is not None and self.validation.disposition in ("passed", "passed_with_warnings") and self.accepted_document == self.candidate
        elif self.status == "accepted":
            valid = self.attempt >= 1 and self.candidate is not None and self.validation is not None and self.validation.disposition in ("passed", "passed_with_warnings") and self.accepted_document == self.candidate
        if not valid:
            raise ValueError("lecture progress fields do not match status")
        if self.candidate is not None:
            if self.candidate.document_id != self.lecture_id or self.candidate.order != self.map_position:
                raise ValueError("lecture progress candidate identity is invalid")
            if self.validation is not None and self.validation.candidate_subject_sha256 != candidate_subject_sha256(self.candidate):
                raise ValueError("lecture progress candidate subject is invalid")


@dataclass(frozen=True, slots=True)
class HistoricalCandidateRecord:
    lecture_id: LectureId
    map_position: MapPosition
    approved_map_subject_sha256: Sha256Hex
    attempt: Attempt
    candidate: DocumentReference
    validation: ValidationRecord | None
    historical_disposition: Literal["rejected", "invalidated_by_map_reopen", "superseded_by_correction"]

    def __post_init__(self) -> None:
        if not _valid_int(self.map_position, 1, 9999):
            raise ValueError("historical candidate position is invalid")
        if not _valid_lecture_id(self.lecture_id) or self.lecture_id != f"l{self.map_position}":
            raise ValueError("historical candidate identity is invalid")
        if not _valid_digest(self.approved_map_subject_sha256):
            raise ValueError("historical map subject is invalid")
        if not _valid_int(self.attempt, 1, _MAX_ATTEMPT):
            raise ValueError("historical candidate attempt is invalid")
        _revalidate_exact(self.candidate, DocumentReference, "historical candidate reference is invalid")
        if self.candidate.document_id != self.lecture_id or self.candidate.order != self.map_position:
            raise ValueError("historical candidate reference is invalid")
        if self.validation is not None:
            _revalidate_exact(self.validation, ValidationRecord, "historical candidate validation is invalid")
            if self.validation.candidate_subject_sha256 != candidate_subject_sha256(self.candidate):
                raise ValueError("historical candidate validation is invalid")
        if not _valid_literal(self.historical_disposition, ("rejected", "invalidated_by_map_reopen", "superseded_by_correction")):
            raise ValueError("historical candidate disposition is invalid")


@dataclass(frozen=True, slots=True)
class WorkflowBlocker:
    code: BlockerCode
    stage: WorkflowStage
    resume_disposition: WorkflowDisposition
    subject_id: DiagnosticSubjectId | None
    subject_sha256: Sha256Hex
    evidence_reference: WorkflowArtifactReference
    recorded_revision: Revision

    def __post_init__(self) -> None:
        if not _valid_literal(self.code, ("private_artifact_unavailable", "external_prerequisite_unavailable", "new_source_review_required")):
            raise ValueError("workflow blocker code is invalid")
        if not _valid_literal(self.stage, WORKFLOW_STAGES[:-1]):
            raise ValueError("workflow blocker stage is invalid")
        if not _valid_literal(self.resume_disposition, ("ready", "awaiting_approval")):
            raise ValueError("workflow blocker resume disposition is invalid")
        if self.code == "private_artifact_unavailable":
            valid_classification = (self.stage, self.resume_disposition) in PRIVATE_ARTIFACT_BLOCKER_STARTS
        elif self.code == "external_prerequisite_unavailable":
            valid_classification = self.stage in EXTERNAL_PREREQUISITE_BLOCKER_STAGES and self.resume_disposition == "ready"
        else:
            valid_classification = self.stage in NEW_SOURCE_STAGES and self.resume_disposition == "ready"
        if not valid_classification:
            raise ValueError("workflow blocker classification is invalid")
        if self.subject_id is not None and not _valid_safe_id(self.subject_id):
            raise ValueError("workflow blocker subject ID is invalid")
        if not _valid_digest(self.subject_sha256):
            raise ValueError("workflow blocker subject digest is invalid")
        _revalidate_exact(self.evidence_reference, WorkflowArtifactReference, "workflow blocker evidence is invalid")
        if self.evidence_reference.artifact_kind not in ("blocker", "decision"):
            raise ValueError("workflow blocker evidence is invalid")
        if not _valid_int(self.recorded_revision, 1, _MAX_REVISION):
            raise ValueError("workflow blocker revision is invalid")


@dataclass(frozen=True, slots=True)
class WorkflowFailure:
    code: FailureCode
    stage: Literal["source_assessment", "lecture_mapping", "lecture_production", "lecture_validation"]
    failed_action: WorkflowAction
    subject_id: DiagnosticSubjectId | None
    evidence_reference: WorkflowArtifactReference
    recorded_revision: Revision

    def __post_init__(self) -> None:
        if not _valid_literal(self.stage, FAILURE_REGISTRY) or not _valid_literal(self.failed_action, WORKFLOW_ACTIONS) or not _valid_literal(self.code, {item[1] for item in FAILURE_REGISTRY.values()}) or FAILURE_REGISTRY[self.stage] != (self.failed_action, self.code):
            raise ValueError("workflow failure classification is invalid")
        if self.subject_id is not None and not _valid_safe_id(self.subject_id):
            raise ValueError("workflow failure subject ID is invalid")
        _require_artifact_kind(self.evidence_reference, "failure", "workflow failure evidence is invalid")
        if not _valid_int(self.recorded_revision, 1, _MAX_REVISION):
            raise ValueError("workflow failure revision is invalid")


@dataclass(frozen=True, slots=True)
class OperationReceipt:
    contract_version: Literal["course-workflow-transition/v1", "course-workflow-transition/v2"]
    operation_id: OperationId
    from_revision: int
    to_revision: Revision
    action: WorkflowAction
    request_sha256: Sha256Hex
    outcome: Literal["advanced", "blocked", "failed"]
    subject_sha256: Sha256Hex

    def __post_init__(self) -> None:
        if not _valid_literal(self.contract_version, ("course-workflow-transition/v1", "course-workflow-transition/v2")):
            raise ValueError("receipt contract version is unsupported")
        if not _valid_safe_id(self.operation_id):
            raise ValueError("receipt operation ID is invalid")
        if not _valid_int(self.from_revision, -1, _MAX_REVISION) or not _valid_int(self.to_revision, 0, _MAX_REVISION) or self.to_revision != self.from_revision + 1:
            raise ValueError("receipt revisions are invalid")
        _require_action(self.action)
        if self.from_revision == -1 and self.action != "initialize_workflow":
            raise ValueError("initialization receipt action is invalid")
        if self.from_revision != -1 and self.action == "initialize_workflow":
            raise ValueError("initialization receipt revisions are invalid")
        if not _valid_digest(self.request_sha256) or not _valid_digest(self.subject_sha256):
            raise ValueError("receipt digest is invalid")
        if not _valid_literal(self.outcome, (RECEIPT_OUTCOMES[self.action],)):
            raise ValueError("receipt outcome is invalid")


@dataclass(frozen=True, slots=True)
class WorkflowDiagnostic:
    code: WorkflowDiagnosticCode
    severity: Literal["warning", "error"]
    category: Literal["validation", "warning", "blocked", "failure"]
    stage: Literal["initialization", "source_assessment", "priority_approval", "lecture_mapping", "map_approval", "lecture_production", "lecture_validation", "completed"]
    subject_id: DiagnosticSubjectId | None

    def __post_init__(self) -> None:
        if type(self.code) is not str:
            raise ValueError("workflow diagnostic classification is invalid")
        registered = DIAGNOSTIC_REGISTRY.get(self.code)
        if registered is None or not _valid_literal(self.severity, (registered[0],)) or not _valid_literal(self.category, (registered[1],)) or not _valid_literal(self.stage, registered[2]):
            raise ValueError("workflow diagnostic classification is invalid")
        if self.subject_id is not None and not _valid_safe_id(self.subject_id):
            raise ValueError("workflow diagnostic subject ID is invalid")

    @property
    def message(self) -> str:
        return DIAGNOSTIC_MESSAGES[self.code]


@dataclass(frozen=True, slots=True)
class InitializeWorkflow:
    action: Literal["initialize_workflow"]
    policies: WorkflowPolicySet
    source_evidence: tuple[SourceEvidenceReference, ...]

    def __post_init__(self) -> None:
        if not _valid_literal(self.action, ("initialize_workflow",)):
            raise ValueError("initialize action is invalid")
        _revalidate_exact(self.policies, WorkflowPolicySet, "initialize policies are invalid")
        _validate_source_tuple(self.source_evidence, require_non_empty=True)


@dataclass(frozen=True, slots=True)
class RecordSourceAssessment:
    action: Literal["record_source_assessment"]
    assessment_reference: WorkflowArtifactReference
    primary_mode: PriorityMode
    priority_proposal_reference: WorkflowArtifactReference
    evidence_hierarchy_reference: WorkflowArtifactReference

    def __post_init__(self) -> None:
        if not _valid_literal(self.action, ("record_source_assessment",)):
            raise ValueError("source assessment action is invalid")
        _require_artifact_kind(self.assessment_reference, "source_assessment", "source assessment reference is invalid")
        if not _valid_literal(self.primary_mode, ("exam_driven", "sheet_driven", "slide_driven", "custom")):
            raise ValueError("priority mode is invalid")
        _require_artifact_kind(self.priority_proposal_reference, "priority_proposal", "priority proposal reference is invalid")
        _require_artifact_kind(self.evidence_hierarchy_reference, "evidence_hierarchy", "evidence hierarchy reference is invalid")


@dataclass(frozen=True, slots=True)
class ApprovePriorityBasis:
    action: Literal["approve_priority_basis"]
    priority_subject_sha256: Sha256Hex

    def __post_init__(self) -> None:
        _validate_digest_action(self.action, "approve_priority_basis", self.priority_subject_sha256)


@dataclass(frozen=True, slots=True)
class RejectPriorityBasis:
    action: Literal["reject_priority_basis"]
    priority_subject_sha256: Sha256Hex

    def __post_init__(self) -> None:
        _validate_digest_action(self.action, "reject_priority_basis", self.priority_subject_sha256)


@dataclass(frozen=True, slots=True)
class RecordLectureMap:
    action: Literal["record_lecture_map"]
    map_reference: WorkflowArtifactReference
    lecture_ids: tuple[LectureId, ...]

    def __post_init__(self) -> None:
        if not _valid_literal(self.action, ("record_lecture_map",)):
            raise ValueError("lecture map action is invalid")
        _require_artifact_kind(self.map_reference, "lecture_map", "lecture map reference is invalid")
        if not _valid_lecture_sequence(self.lecture_ids):
            raise ValueError("lecture map IDs are invalid")


@dataclass(frozen=True, slots=True)
class ApproveLectureMap:
    action: Literal["approve_lecture_map"]
    map_subject_sha256: Sha256Hex

    def __post_init__(self) -> None:
        _validate_digest_action(self.action, "approve_lecture_map", self.map_subject_sha256)


@dataclass(frozen=True, slots=True)
class RejectLectureMap:
    action: Literal["reject_lecture_map"]
    map_subject_sha256: Sha256Hex

    def __post_init__(self) -> None:
        _validate_digest_action(self.action, "reject_lecture_map", self.map_subject_sha256)


@dataclass(frozen=True, slots=True)
class SubmitLectureCandidate:
    action: Literal["submit_lecture_candidate"]
    lecture_id: LectureId
    document: LectureDocument = field(repr=False)

    def __post_init__(self) -> None:
        if not _valid_literal(self.action, ("submit_lecture_candidate",)):
            raise ValueError("candidate submission action is invalid")
        if not _valid_lecture_id(self.lecture_id):
            raise ValueError("candidate lecture ID is invalid")
        _revalidate_exact(self.document, LectureDocument, "candidate document is invalid")


@dataclass(frozen=True, slots=True)
class ReopenLecturesForCorrection:
    action: Literal["reopen_lectures_for_correction"]
    lecture_ids: tuple[LectureId, ...]
    review_request_id: SafeId
    review_operation_id: SafeId
    review_revision: Revision

    def __post_init__(self) -> None:
        if not _valid_literal(self.action, ("reopen_lectures_for_correction",)):
            raise ValueError("correction reopen action is invalid")
        if not self.lecture_ids or len(set(self.lecture_ids)) != len(self.lecture_ids) or any(not _valid_lecture_id(value) for value in self.lecture_ids):
            raise ValueError("correction reopen lectures are invalid")
        if not _valid_safe_id(self.review_request_id) or not _valid_safe_id(self.review_operation_id):
            raise ValueError("correction review identity is invalid")
        if not _valid_int(self.review_revision, 0, _MAX_REVISION):
            raise ValueError("correction review revision is invalid")


@dataclass(frozen=True, slots=True)
class RecordCandidateValidation:
    action: Literal["record_candidate_validation"]
    lecture_id: LectureId
    candidate_subject_sha256: Sha256Hex
    validation: ValidationRecord

    def __post_init__(self) -> None:
        if not _valid_literal(self.action, ("record_candidate_validation",)):
            raise ValueError("candidate validation action is invalid")
        if not _valid_lecture_id(self.lecture_id):
            raise ValueError("candidate validation lecture ID is invalid")
        if not _valid_digest(self.candidate_subject_sha256):
            raise ValueError("candidate validation subject is invalid")
        _revalidate_exact(self.validation, ValidationRecord, "candidate validation record is invalid")


@dataclass(frozen=True, slots=True)
class RecordOperationFailure:
    action: Literal["record_operation_failure"]
    failed_action: WorkflowAction
    failure_code: FailureCode
    subject_id: DiagnosticSubjectId | None
    evidence_reference: WorkflowArtifactReference

    def __post_init__(self) -> None:
        if not _valid_literal(self.action, ("record_operation_failure",)):
            raise ValueError("operation failure action is invalid")
        _require_action(self.failed_action)
        if not _valid_literal(self.failure_code, {item[1] for item in FAILURE_REGISTRY.values()}):
            raise ValueError("operation failure code is invalid")
        if self.subject_id is not None and not _valid_safe_id(self.subject_id):
            raise ValueError("operation failure subject ID is invalid")
        _require_artifact_kind(self.evidence_reference, "failure", "operation failure evidence is invalid")


@dataclass(frozen=True, slots=True)
class RetryFailed:
    action: Literal["retry_failed"]
    failure_code: FailureCode
    failure_evidence_sha256: Sha256Hex

    def __post_init__(self) -> None:
        if not _valid_literal(self.action, ("retry_failed",)):
            raise ValueError("retry action is invalid")
        if not _valid_literal(self.failure_code, {item[1] for item in FAILURE_REGISTRY.values()}):
            raise ValueError("retry failure code is invalid")
        if not _valid_digest(self.failure_evidence_sha256):
            raise ValueError("retry evidence digest is invalid")


@dataclass(frozen=True, slots=True)
class MarkBlocked:
    action: Literal["mark_blocked"]
    blocker_code: Literal["private_artifact_unavailable", "external_prerequisite_unavailable"]
    subject_id: DiagnosticSubjectId | None
    subject_sha256: Sha256Hex
    evidence_reference: WorkflowArtifactReference

    def __post_init__(self) -> None:
        if not _valid_literal(self.action, ("mark_blocked",)):
            raise ValueError("blocker action is invalid")
        if not _valid_literal(self.blocker_code, ("private_artifact_unavailable", "external_prerequisite_unavailable")):
            raise ValueError("generic blocker code is invalid")
        if self.subject_id is not None and not _valid_safe_id(self.subject_id):
            raise ValueError("blocker subject ID is invalid")
        if not _valid_digest(self.subject_sha256):
            raise ValueError("blocker subject digest is invalid")
        _revalidate_exact(self.evidence_reference, WorkflowArtifactReference, "blocker evidence reference is invalid")


@dataclass(frozen=True, slots=True)
class ClearBlocker:
    action: Literal["clear_blocker"]
    blocker_code: Literal["private_artifact_unavailable", "external_prerequisite_unavailable"]
    subject_sha256: Sha256Hex

    def __post_init__(self) -> None:
        if not _valid_literal(self.action, ("clear_blocker",)):
            raise ValueError("clear blocker action is invalid")
        if not _valid_literal(self.blocker_code, ("private_artifact_unavailable", "external_prerequisite_unavailable")):
            raise ValueError("clear blocker code is invalid")
        if not _valid_digest(self.subject_sha256):
            raise ValueError("clear blocker subject is invalid")


@dataclass(frozen=True, slots=True)
class RecordNewSource:
    action: Literal["record_new_source"]
    source_reference: SourceEvidenceReference
    evidence_reference: WorkflowArtifactReference

    def __post_init__(self) -> None:
        if not _valid_literal(self.action, ("record_new_source",)):
            raise ValueError("new source action is invalid")
        _revalidate_exact(self.source_reference, SourceEvidenceReference, "new source reference is invalid")
        _revalidate_exact(self.evidence_reference, WorkflowArtifactReference, "new source evidence is invalid")


@dataclass(frozen=True, slots=True)
class DismissNewSource:
    action: Literal["dismiss_new_source"]
    source_content_sha256: Sha256Hex

    def __post_init__(self) -> None:
        _validate_digest_action(self.action, "dismiss_new_source", self.source_content_sha256)


@dataclass(frozen=True, slots=True)
class ApproveMapReopen:
    action: Literal["approve_map_reopen"]
    source_content_sha256: Sha256Hex

    def __post_init__(self) -> None:
        _validate_digest_action(self.action, "approve_map_reopen", self.source_content_sha256)


WorkflowActionPayload: TypeAlias = (
    RecordSourceAssessment
    | ApprovePriorityBasis
    | RejectPriorityBasis
    | RecordLectureMap
    | ApproveLectureMap
    | RejectLectureMap
    | SubmitLectureCandidate
    | ReopenLecturesForCorrection
    | RecordCandidateValidation
    | RecordOperationFailure
    | RetryFailed
    | MarkBlocked
    | ClearBlocker
    | RecordNewSource
    | DismissNewSource
    | ApproveMapReopen
)
_PAYLOAD_TYPES = (
    RecordSourceAssessment,
    ApprovePriorityBasis,
    RejectPriorityBasis,
    RecordLectureMap,
    ApproveLectureMap,
    RejectLectureMap,
    SubmitLectureCandidate,
    ReopenLecturesForCorrection,
    RecordCandidateValidation,
    RecordOperationFailure,
    RetryFailed,
    MarkBlocked,
    ClearBlocker,
    RecordNewSource,
    DismissNewSource,
    ApproveMapReopen,
)
_PAYLOAD_ACTION_BY_TYPE = {
    InitializeWorkflow: "initialize_workflow",
    RecordSourceAssessment: "record_source_assessment",
    ApprovePriorityBasis: "approve_priority_basis",
    RejectPriorityBasis: "reject_priority_basis",
    RecordLectureMap: "record_lecture_map",
    ApproveLectureMap: "approve_lecture_map",
    RejectLectureMap: "reject_lecture_map",
    SubmitLectureCandidate: "submit_lecture_candidate",
    ReopenLecturesForCorrection: "reopen_lectures_for_correction",
    RecordCandidateValidation: "record_candidate_validation",
    RecordOperationFailure: "record_operation_failure",
    RetryFailed: "retry_failed",
    MarkBlocked: "mark_blocked",
    ClearBlocker: "clear_blocker",
    RecordNewSource: "record_new_source",
    DismissNewSource: "dismiss_new_source",
    ApproveMapReopen: "approve_map_reopen",
}


@dataclass(frozen=True, slots=True)
class InitializeWorkflowRequest:
    contract_version: Literal["course-workflow-transition/v1", "course-workflow-transition/v2"]
    workflow_id: WorkflowId
    operation_id: OperationId
    payload: InitializeWorkflow

    def __post_init__(self) -> None:
        if not _valid_literal(self.contract_version, ("course-workflow-transition/v1", "course-workflow-transition/v2")):
            raise ValueError("transition contract version is unsupported")
        if not _valid_safe_id(self.workflow_id):
            raise ValueError("workflow ID is invalid")
        if not _valid_safe_id(self.operation_id):
            raise ValueError("operation ID is invalid")
        _revalidate_exact(self.payload, InitializeWorkflow, "initialization payload is invalid")


@dataclass(frozen=True, slots=True)
class WorkflowTransitionRequest:
    contract_version: Literal["course-workflow-transition/v1", "course-workflow-transition/v2"]
    workflow_id: WorkflowId
    expected_revision: Revision
    operation_id: OperationId
    payload: WorkflowActionPayload

    def __post_init__(self) -> None:
        if not _valid_literal(self.contract_version, ("course-workflow-transition/v1", "course-workflow-transition/v2")):
            raise ValueError("transition contract version is unsupported")
        if not _valid_safe_id(self.workflow_id):
            raise ValueError("workflow ID is invalid")
        if not _valid_int(self.expected_revision, 0, _MAX_REVISION):
            raise ValueError("expected revision is invalid")
        if not _valid_safe_id(self.operation_id):
            raise ValueError("operation ID is invalid")
        if type(self.payload) not in _PAYLOAD_TYPES:
            raise ValueError("transition payload is invalid")
        _revalidate_exact(self.payload, type(self.payload), "transition payload is invalid")


@dataclass(frozen=True, slots=True)
class WorkflowState:
    contract_version: Literal["course-workflow-state/v1", "course-workflow-state/v2"]
    workflow_id: WorkflowId
    revision: Revision
    stage: WorkflowStage
    disposition: WorkflowDisposition
    policies: WorkflowPolicySet
    source_evidence: tuple[SourceEvidenceReference, ...]
    reviewed_source_evidence: tuple[SourceEvidenceReference, ...]
    source_assessment: SourceAssessmentRecord | None
    priority_basis: PriorityBasisRecord | None
    lecture_map: LectureMapRecord | None
    map_reopen_context: MapReopenContext | None
    active_lecture_id: LectureId | None
    pending_source: PendingSourceRecord | None
    lecture_progress: tuple[LectureProgress, ...]
    historical_candidates: tuple[HistoricalCandidateRecord, ...]
    active_issue: WorkflowBlocker | WorkflowFailure | None
    approval_history: tuple[ApprovalRecord, ...]
    diagnostics: tuple[WorkflowDiagnostic, ...]
    operation_receipts: tuple[OperationReceipt, ...]

    def __post_init__(self) -> None:
        if _state_violation(self) is not None:
            raise ValueError("workflow state invariants are invalid")


@dataclass(frozen=True, slots=True)
class WorkflowAdvanced:
    status: Literal["advanced"]
    state: WorkflowState
    receipt: OperationReceipt
    diagnostics: tuple[WorkflowDiagnostic, ...]

    def __post_init__(self) -> None:
        if not _valid_literal(self.status, ("advanced",)):
            raise ValueError("advanced result status is invalid")
        _revalidate_exact(self.state, WorkflowState, "advanced result nested value is invalid")
        _revalidate_exact(self.receipt, OperationReceipt, "advanced result nested value is invalid")
        _require_workflow_diagnostics(self.diagnostics)
        if any(item.severity != "warning" for item in self.diagnostics):
            raise ValueError("advanced result diagnostics must be warnings")
        if self.state.operation_receipts[-1] != self.receipt or self.state.revision != self.receipt.to_revision:
            raise ValueError("advanced result receipt is inconsistent")


@dataclass(frozen=True, slots=True)
class WorkflowRejected:
    status: Literal["rejected"]
    state: WorkflowState | None
    diagnostics: tuple[WorkflowDiagnostic, ...]

    def __post_init__(self) -> None:
        if not _valid_literal(self.status, ("rejected",)):
            raise ValueError("rejected result status is invalid")
        if self.state is not None:
            _require_exact_shape(self.state, WorkflowState, "rejected result state is invalid")
        _require_workflow_diagnostics(self.diagnostics, non_empty=True)
        if any(item.severity != "error" or item.category != "validation" for item in self.diagnostics):
            raise ValueError("rejected result diagnostics must be validation errors")


@dataclass(frozen=True, slots=True)
class WorkflowBlocked:
    status: Literal["blocked"]
    state: WorkflowState
    receipt: OperationReceipt
    blocker: WorkflowBlocker

    def __post_init__(self) -> None:
        if not _valid_literal(self.status, ("blocked",)):
            raise ValueError("blocked result status is invalid")
        _revalidate_exact(self.state, WorkflowState, "blocked result nested value is invalid")
        _revalidate_exact(self.receipt, OperationReceipt, "blocked result nested value is invalid")
        _revalidate_exact(self.blocker, WorkflowBlocker, "blocked result nested value is invalid")
        if self.state.active_issue != self.blocker or self.state.disposition != "blocked":
            raise ValueError("blocked result state is inconsistent")
        if self.state.operation_receipts[-1] != self.receipt:
            raise ValueError("blocked result receipt is inconsistent")


@dataclass(frozen=True, slots=True)
class WorkflowFailed:
    status: Literal["failed"]
    state: WorkflowState
    receipt: OperationReceipt
    failure: WorkflowFailure

    def __post_init__(self) -> None:
        if not _valid_literal(self.status, ("failed",)):
            raise ValueError("failed result status is invalid")
        _revalidate_exact(self.state, WorkflowState, "failed result nested value is invalid")
        _revalidate_exact(self.receipt, OperationReceipt, "failed result nested value is invalid")
        _revalidate_exact(self.failure, WorkflowFailure, "failed result nested value is invalid")
        if self.state.active_issue != self.failure or self.state.disposition != "failed":
            raise ValueError("failed result state is inconsistent")
        if self.state.operation_receipts[-1] != self.receipt:
            raise ValueError("failed result receipt is inconsistent")


@dataclass(frozen=True, slots=True)
class WorkflowIdempotentRepeat:
    status: Literal["idempotent_repeat"]
    state: WorkflowState
    original_receipt: OperationReceipt

    def __post_init__(self) -> None:
        if not _valid_literal(self.status, ("idempotent_repeat",)):
            raise ValueError("idempotent result status is invalid")
        _revalidate_exact(self.state, WorkflowState, "idempotent result nested value is invalid")
        _revalidate_exact(self.original_receipt, OperationReceipt, "idempotent result nested value is invalid")
        if self.original_receipt not in self.state.operation_receipts:
            raise ValueError("idempotent result receipt is inconsistent")


WorkflowTransitionResult: TypeAlias = WorkflowAdvanced | WorkflowRejected | WorkflowBlocked | WorkflowFailed | WorkflowIdempotentRepeat


@dataclass(frozen=True, slots=True)
class WorkflowHandoff:
    contract_version: Literal["course-workflow-handoff/v1", "course-workflow-handoff/v2"]
    workflow_id: WorkflowId
    revision: Revision
    stage: WorkflowStage
    disposition: WorkflowDisposition
    source_count: int
    priority_mode: PriorityMode | None
    priority_approved: bool
    map_approved: bool
    lecture_ids: tuple[LectureId, ...]
    active_lecture_id: LectureId | None
    accepted_documents: tuple[DocumentReference, ...]
    pending_source_sha256: Sha256Hex | None
    active_issue_code: BlockerCode | FailureCode | None
    diagnostic_codes: tuple[WorkflowDiagnosticCode, ...]
    next_actions: tuple[WorkflowAction, ...]

    def __post_init__(self) -> None:
        if not _valid_literal(self.contract_version, ("course-workflow-handoff/v1", "course-workflow-handoff/v2")):
            raise ValueError("handoff contract version is unsupported")
        if not _valid_safe_id(self.workflow_id) or not _valid_int(self.revision, 0, _MAX_REVISION):
            raise ValueError("handoff identity is invalid")
        if not _valid_literal(self.stage, WORKFLOW_STAGES) or not _valid_literal(self.disposition, set(NORMAL_DISPOSITIONS.values()) | {"blocked", "failed"}):
            raise ValueError("handoff stage or disposition is invalid")
        if not _valid_int(self.source_count, 1, _MAX_REVISION):
            raise ValueError("handoff source count is invalid")
        if self.priority_mode is not None and not _valid_literal(self.priority_mode, ("exam_driven", "sheet_driven", "slide_driven", "custom")):
            raise ValueError("handoff priority mode is invalid")
        if type(self.priority_approved) is not bool or type(self.map_approved) is not bool:
            raise ValueError("handoff approval flag is invalid")
        if type(self.lecture_ids) is not tuple or (self.lecture_ids and not _valid_lecture_sequence(self.lecture_ids)):
            raise ValueError("handoff lecture IDs are invalid")
        if self.active_lecture_id is not None and not _valid_lecture_id(self.active_lecture_id):
            raise ValueError("handoff active lecture is invalid")
        _exact_tuple(self.accepted_documents, DocumentReference, "handoff accepted documents are invalid")
        if self.pending_source_sha256 is not None and not _valid_digest(self.pending_source_sha256):
            raise ValueError("handoff pending source is invalid")
        if self.active_issue_code is not None and not _valid_literal(self.active_issue_code, DIAGNOSTIC_REGISTRY):
            raise ValueError("handoff active issue is invalid")
        if type(self.diagnostic_codes) is not tuple or any(not _valid_literal(item, DIAGNOSTIC_REGISTRY) for item in self.diagnostic_codes) or len(set(self.diagnostic_codes)) != len(self.diagnostic_codes):
            raise ValueError("handoff diagnostic codes are invalid")
        if type(self.next_actions) is not tuple or any(not _valid_literal(item, WORKFLOW_ACTIONS) for item in self.next_actions):
            raise ValueError("handoff next actions are invalid")
        if self.next_actions != tuple(action for action in WORKFLOW_ACTIONS if action in self.next_actions) or len(set(self.next_actions)) != len(self.next_actions):
            raise ValueError("handoff next actions are invalid")
        blocker_failure_codes = {"private_artifact_unavailable", "external_prerequisite_unavailable", "new_source_review_required"} | {item[1] for item in FAILURE_REGISTRY.values()}
        if self.active_issue_code is not None and self.active_issue_code not in blocker_failure_codes:
            raise ValueError("handoff active issue is invalid")
        if (self.disposition in ("blocked", "failed")) != (self.active_issue_code is not None):
            raise ValueError("handoff active issue is invalid")
        if self.disposition not in ("blocked", "failed") and self.disposition != NORMAL_DISPOSITIONS[self.stage]:
            raise ValueError("handoff stage or disposition is invalid")
        if self.disposition == "failed":
            if self.stage not in FAILURE_REGISTRY or FAILURE_REGISTRY[self.stage][1] != self.active_issue_code:
                raise ValueError("handoff active issue is invalid")
        if self.disposition == "blocked":
            if self.active_issue_code == "external_prerequisite_unavailable" and self.stage not in EXTERNAL_PREREQUISITE_BLOCKER_STAGES:
                raise ValueError("handoff active issue is invalid")
            if self.active_issue_code == "new_source_review_required" and self.stage not in NEW_SOURCE_STAGES:
                raise ValueError("handoff active issue is invalid")
            if self.active_issue_code == "private_artifact_unavailable" and self.stage == "completed":
                raise ValueError("handoff active issue is invalid")
        if (self.active_issue_code == "new_source_review_required") != (self.pending_source_sha256 is not None):
            raise ValueError("handoff pending source is invalid")
        if (self.stage == "completed") != (self.disposition == "completed"):
            raise ValueError("handoff completed state is invalid")
        if self.active_lecture_id is not None and self.stage != "lecture_validation":
            raise ValueError("handoff active lecture is invalid")
        if self.map_approved and not self.lecture_ids:
            raise ValueError("handoff map approval is invalid")
        if self.priority_approved and self.priority_mode is None:
            raise ValueError("handoff priority approval is invalid")
        if any(item.document_id not in self.lecture_ids or item.order != self.lecture_ids.index(item.document_id) + 1 for item in self.accepted_documents):
            raise ValueError("handoff accepted documents are invalid")
        expected_actions = _actions_for(self.stage, self.disposition, self.active_issue_code)
        if self.contract_version == "course-workflow-handoff/v2" and self.stage == "completed" and self.disposition == "completed":
            expected_actions = ("reopen_lectures_for_correction",)
        if self.next_actions != expected_actions:
            raise ValueError("handoff next actions are invalid")


def _validate_digest_action(action: object, expected: str, digest: object) -> None:
    if not _valid_literal(action, (expected,)):
        raise ValueError("workflow action literal is invalid")
    if not _valid_digest(digest):
        raise ValueError("workflow action subject digest is invalid")


def _validate_source_tuple(value: object, *, require_non_empty: bool) -> None:
    _exact_tuple(value, SourceEvidenceReference, "source evidence tuple is invalid")
    if require_non_empty and not value:
        raise ValueError("source evidence tuple must not be empty")
    ids = tuple(item.source_id for item in value)
    digests = tuple(item.content_sha256 for item in value)
    if ids != tuple(sorted(ids)) or len(set(ids)) != len(ids) or len(set(digests)) != len(digests):
        raise ValueError("source evidence tuple is not canonical")


def _require_workflow_diagnostics(value: object, *, non_empty: bool = False) -> None:
    _exact_tuple(value, WorkflowDiagnostic, "workflow diagnostics tuple is invalid")
    if non_empty and not value:
        raise ValueError("workflow diagnostics tuple must not be empty")


def _frame(value: bytes) -> bytes:
    return str(len(value)).encode("ascii") + b":" + value


def _ascii(value: str) -> bytes:
    if type(value) is not str:
        raise ValueError("canonical value must be ASCII")
    try:
        encoded = value.encode("ascii")
    except (AttributeError, UnicodeEncodeError):
        raise ValueError("canonical value must be ASCII") from None
    if not encoded:
        raise ValueError("canonical string must not be empty")
    return encoded


def _encode_string(value: str) -> bytes:
    return _frame(b"string/v1") + _frame(_ascii(value))


def _encode_integer(value: int) -> bytes:
    if not _valid_int(value, 0, _MAX_REVISION):
        raise ValueError("canonical integer is invalid")
    return _frame(b"integer/v1") + _frame(str(value).encode("ascii"))


def _encode_tuple(value: tuple) -> bytes:
    result = _frame(b"tuple/v1") + _frame(str(len(value)).encode("ascii"))
    for item in value:
        result += _frame(_encode_value(item))
    return result


def _record(tag: str, field_values: tuple[tuple[str, object], ...]) -> bytes:
    result = _frame(b"record/v1") + _frame(_ascii(tag))
    result += _frame(str(len(field_values)).encode("ascii"))
    for name, value in field_values:
        result += _frame(_ascii(name)) + _frame(_encode_value(value))
    return result


def _encode_value(value: object) -> bytes:
    if type(value) is str:
        return _encode_string(value)
    if type(value) is int:
        return _encode_integer(value)
    if value is None:
        return _frame(b"none/v1")
    if type(value) is tuple:
        return _encode_tuple(value)
    if type(value) is SourceEvidenceReference:
        _revalidate_exact(value, SourceEvidenceReference, "canonical source reference is invalid")
        return _record(
            "source-evidence-reference/v1",
            (
                ("reference_version", value.reference_version),
                ("source_id", value.source_id),
                ("content_sha256", value.content_sha256),
            ),
        )
    if type(value) is WorkflowArtifactReference:
        _revalidate_exact(value, WorkflowArtifactReference, "canonical artifact reference is invalid")
        return _record(
            "workflow-artifact-reference/v1",
            (
                ("reference_version", value.reference_version),
                ("artifact_id", value.artifact_id),
                ("artifact_kind", value.artifact_kind),
                ("content_sha256", value.content_sha256),
                ("producer_version", value.producer_version),
            ),
        )
    if type(value) is PolicyReference:
        _revalidate_exact(value, PolicyReference, "canonical policy reference is invalid")
        return _record(
            "policy-reference/v1",
            (
                ("policy_kind", value.policy_kind),
                ("policy_version", value.policy_version),
                ("content_sha256", value.content_sha256),
            ),
        )
    if type(value) is WorkflowPolicySet:
        _revalidate_exact(value, WorkflowPolicySet, "canonical policy set is invalid")
        return _record(
            "workflow-policy-set/v1",
            tuple((item.name, getattr(value, item.name)) for item in fields(value)),
        )
    if type(value) is DocumentReference:
        _revalidate_exact(value, DocumentReference, "canonical document reference is invalid")
        return _record(
            "document-reference/v1",
            (
                ("contract_version", value.contract_version),
                ("document_id", value.document_id),
                ("order", value.order),
                ("content_sha256", value.content_sha256),
            ),
        )
    if type(value) is ValidationRecord:
        _revalidate_exact(value, ValidationRecord, "canonical validation record is invalid")
        return _record(
            "workflow-validation-record/v1",
            tuple((item.name, getattr(value, item.name)) for item in fields(value)),
        )
    if type(value) is InitializeWorkflow:
        _revalidate_exact(value, InitializeWorkflow, "canonical payload is invalid")
        return _encode_payload(value)
    if type(value) in _PAYLOAD_TYPES:
        _revalidate_exact(value, type(value), "canonical payload is invalid")
        return _encode_payload(value)
    raise ValueError("canonical value type is unsupported")


def canonical_encode(value: object) -> bytes:
    """Return the exact framed v1 bytes for a supported content-free value."""

    return _encode_value(value)


def _sha256(value: bytes) -> Sha256Hex:
    return hashlib.sha256(value).hexdigest()


def source_set_subject_sha256(
    source_evidence: tuple[SourceEvidenceReference, ...],
) -> Sha256Hex:
    _validate_source_tuple(source_evidence, require_non_empty=True)
    return _sha256(
        _record("source-set-subject/v1", (("source_evidence", source_evidence),))
    )


def _priority_subject_from_parts(
    primary_mode: PriorityMode,
    proposal_reference: WorkflowArtifactReference,
    evidence_hierarchy_reference: WorkflowArtifactReference,
    priority_policy: PolicyReference,
) -> Sha256Hex:
    return _sha256(
        _record(
            "priority-subject/v1",
            (
                ("primary_mode", primary_mode),
                ("proposal_reference", proposal_reference),
                ("evidence_hierarchy_reference", evidence_hierarchy_reference),
                ("priority_policy", priority_policy),
            ),
        )
    )


def priority_subject_sha256(
    basis: PriorityBasisRecord, priority_policy: PolicyReference
) -> Sha256Hex:
    _revalidate_exact(basis, PriorityBasisRecord, "priority subject input is invalid")
    _revalidate_exact(priority_policy, PolicyReference, "priority subject input is invalid")
    if priority_policy.policy_kind != "priority_basis":
        raise ValueError("priority subject input is invalid")
    return _priority_subject_from_parts(
        basis.primary_mode,
        basis.proposal_reference,
        basis.evidence_hierarchy_reference,
        priority_policy,
    )


def _map_subject_from_parts(
    map_reference: WorkflowArtifactReference,
    lecture_ids: tuple[LectureId, ...],
    reserved_lecture_ids: tuple[LectureId, ...],
    lecture_mapping_policy: PolicyReference,
) -> Sha256Hex:
    return _sha256(
        _record(
            "map-subject/v1",
            (
                ("map_reference", map_reference),
                ("lecture_ids", lecture_ids),
                ("reserved_lecture_ids", reserved_lecture_ids),
                ("lecture_mapping_policy", lecture_mapping_policy),
            ),
        )
    )


def map_subject_sha256(
    lecture_map: LectureMapRecord, lecture_mapping_policy: PolicyReference
) -> Sha256Hex:
    _revalidate_exact(lecture_map, LectureMapRecord, "map subject input is invalid")
    _revalidate_exact(lecture_mapping_policy, PolicyReference, "map subject input is invalid")
    if lecture_mapping_policy.policy_kind != "lecture_mapping":
        raise ValueError("map subject input is invalid")
    return _map_subject_from_parts(
        lecture_map.map_reference,
        lecture_map.lecture_ids,
        lecture_map.reserved_lecture_ids,
        lecture_mapping_policy,
    )


def candidate_subject_sha256(candidate_reference: DocumentReference) -> Sha256Hex:
    _revalidate_exact(candidate_reference, DocumentReference, "candidate subject reference is invalid")
    return _sha256(
        _record(
            "candidate-subject/v1",
            (("candidate_reference", candidate_reference),),
        )
    )


_PAYLOAD_TAGS = {
    "initialize_workflow": "initialize-workflow/v1",
    "record_source_assessment": "record-source-assessment/v1",
    "approve_priority_basis": "approve-priority-basis/v1",
    "reject_priority_basis": "reject-priority-basis/v1",
    "record_lecture_map": "record-lecture-map/v1",
    "approve_lecture_map": "approve-lecture-map/v1",
    "reject_lecture_map": "reject-lecture-map/v1",
    "submit_lecture_candidate": "submit-lecture-candidate/v1",
    "record_candidate_validation": "record-candidate-validation/v1",
    "record_operation_failure": "record-operation-failure/v1",
    "retry_failed": "retry-failed/v1",
    "mark_blocked": "mark-blocked/v1",
    "clear_blocker": "clear-blocker/v1",
    "record_new_source": "record-new-source/v1",
    "dismiss_new_source": "dismiss-new-source/v1",
    "approve_map_reopen": "approve-map-reopen/v1",
    "reopen_lectures_for_correction": "reopen-lectures-for-correction/v1",
}


def _encode_payload(payload: object) -> bytes:
    action = payload.action
    values: list[tuple[str, object]] = []
    for item in fields(payload):
        value = getattr(payload, item.name)
        if type(payload) is SubmitLectureCandidate and item.name == "document":
            diagnostics = validate_document(value)
            reference = document_reference(value) if not has_validation_errors(diagnostics) else None
            if reference is None:
                raise ValueError("candidate document cannot be fingerprinted")
            value = reference
        values.append((item.name, value))
    return _record(_PAYLOAD_TAGS[action], tuple(values))


def _nested_contract_code(value: object, seen: set[int] | None = None) -> str | None:
    if seen is None:
        seen = set()
    identity = id(value)
    if identity in seen:
        return None
    seen.add(identity)
    try:
        if type(value) is SourceEvidenceReference:
            if type(value.reference_version) is str and value.reference_version != SOURCE_EVIDENCE_REFERENCE_VERSION:
                return "unsupported_reference_version"
        elif type(value) is WorkflowArtifactReference:
            if type(value.reference_version) is str and value.reference_version != WORKFLOW_ARTIFACT_REFERENCE_VERSION:
                return "unsupported_reference_version"
        elif type(value) is DocumentReference:
            if type(value.contract_version) is str and value.contract_version != "lecture-document/v1":
                return "unsupported_reference_version"
        elif type(value) is PolicyReference:
            if type(value.policy_kind) is str and type(value.policy_version) is str and (value.policy_kind not in POLICY_VERSIONS or POLICY_VERSIONS[value.policy_kind] != value.policy_version):
                return "unsupported_policy_kind_version"
        elif type(value) is ValidationRecord:
            if type(value.validation_policy_version) is str and value.validation_policy_version != "lecture-validation-policy/v1":
                return "unsupported_policy_kind_version"
        if type(value) is tuple:
            nested_values = value
        elif hasattr(type(value), "__dataclass_fields__"):
            nested_values = tuple(getattr(value, item.name) for item in fields(type(value)))
        else:
            nested_values = ()
        for nested in nested_values:
            code = _nested_contract_code(nested, seen)
            if code is not None:
                return code
    except (AttributeError, KeyError, RecursionError, TypeError):
        return None
    return None


def _candidate_document_is_valid(document: object) -> bool:
    try:
        _revalidate_exact(document, LectureDocument, "")
        return not has_validation_errors(validate_document(document))
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        return False


def _request_validation_code(request: object) -> str | None:
    if type(request) not in (InitializeWorkflowRequest, WorkflowTransitionRequest):
        return "unsupported_transition_contract_version"
    try:
        _require_exact_shape(request, type(request), "")
        if not _valid_literal(request.contract_version, ("course-workflow-transition/v1", "course-workflow-transition/v2")):
            return "unsupported_transition_contract_version"
        if not _valid_safe_id(request.workflow_id):
            return "invalid_workflow_id"
        if not _valid_safe_id(request.operation_id):
            return "invalid_operation_id"
        if type(request) is WorkflowTransitionRequest and not _valid_int(request.expected_revision, 0, _MAX_REVISION):
            return "illegal_transition"
        if type(request) is InitializeWorkflowRequest:
            if type(request.payload) is not InitializeWorkflow:
                return "invalid_source_evidence"
        elif type(request.payload) not in _PAYLOAD_TYPES:
            return "illegal_transition"
        expected_action = _PAYLOAD_ACTION_BY_TYPE.get(type(request.payload))
        if expected_action is None or not _valid_literal(request.payload.action, (expected_action,)):
            return "illegal_transition"
        nested_code = _nested_contract_code(request.payload)
        if nested_code is not None:
            return nested_code
        _revalidate_exact(request, type(request), "request is invalid")
        if type(request.payload) is SubmitLectureCandidate and not _candidate_document_is_valid(request.payload.document):
            return "invalid_candidate_document"
        return None
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        if type(request) is InitializeWorkflowRequest:
            return "invalid_source_evidence"
        payload = getattr(request, "payload", None)
        if type(payload) is SubmitLectureCandidate:
            return "invalid_candidate_document"
        if type(payload) is RecordCandidateValidation:
            return "invalid_validation_evidence"
        return "illegal_transition"


def request_fingerprint(
    request: InitializeWorkflowRequest | WorkflowTransitionRequest,
) -> Sha256Hex:
    if _request_validation_code(request) is not None:
        raise ValueError("request is invalid")
    if type(request) is InitializeWorkflowRequest:
        encoded = _record(
            "initialize-workflow-request/v1",
            (
                ("contract_version", request.contract_version),
                ("workflow_id", request.workflow_id),
                ("operation_id", request.operation_id),
                ("payload", request.payload),
            ),
        )
    elif type(request) is WorkflowTransitionRequest:
        encoded = _record(
            "workflow-transition-request/v1",
            (
                ("contract_version", request.contract_version),
                ("workflow_id", request.workflow_id),
                ("expected_revision", request.expected_revision),
                ("operation_id", request.operation_id),
                ("payload", request.payload),
            ),
        )
    else:
        raise ValueError("request type is invalid")
    return _sha256(encoded)


def _diagnostic_sort_key(item: WorkflowDiagnostic) -> tuple[int, int, str, str]:
    return (
        _STAGE_RANK[item.stage],
        _CATEGORY_RANK[item.category],
        item.code,
        item.subject_id or "",
    )


def _sorted_diagnostics(
    diagnostics: tuple[WorkflowDiagnostic, ...] | list[WorkflowDiagnostic],
) -> tuple[WorkflowDiagnostic, ...]:
    return tuple(sorted(diagnostics, key=_diagnostic_sort_key))


def _state_violation(state: object) -> str | None:
    try:
        if type(state) is not WorkflowState:
            return "type"
        if not _valid_literal(state.contract_version, ("course-workflow-state/v1", "course-workflow-state/v2")):
            return "version"
        if state.contract_version == "course-workflow-state/v1" and (
            any(item.status == "correction_required" for item in state.lecture_progress)
            or any(item.historical_disposition == "superseded_by_correction" for item in state.historical_candidates)
            or any(item.contract_version != "course-workflow-transition/v1" or item.action == "reopen_lectures_for_correction" for item in state.operation_receipts)
        ):
            return "version"
        if not _valid_safe_id(state.workflow_id) or not _valid_int(state.revision, 0, _MAX_REVISION):
            return "identity"
        if not _valid_literal(state.stage, WORKFLOW_STAGES) or not _valid_literal(state.disposition, ("ready", "awaiting_approval", "blocked", "failed", "completed")):
            return "stage"
        _revalidate_exact(state.policies, WorkflowPolicySet, "")
        _validate_source_tuple(state.source_evidence, require_non_empty=True)
        _validate_source_tuple(state.reviewed_source_evidence, require_non_empty=False)
        current_ids = {item.source_id for item in state.source_evidence}
        current_digests = {item.content_sha256 for item in state.source_evidence}
        reviewed_ids = {item.source_id for item in state.reviewed_source_evidence}
        reviewed_digests = {item.content_sha256 for item in state.reviewed_source_evidence}
        if current_ids & reviewed_ids or current_digests & reviewed_digests:
            return "source_disjointness"
        optional_types = (
            (state.source_assessment, SourceAssessmentRecord),
            (state.priority_basis, PriorityBasisRecord),
            (state.lecture_map, LectureMapRecord),
            (state.map_reopen_context, MapReopenContext),
            (state.pending_source, PendingSourceRecord),
        )
        for value, expected in optional_types:
            if value is not None:
                _revalidate_exact(value, expected, "")
        _exact_tuple(state.lecture_progress, LectureProgress, "")
        _exact_tuple(state.historical_candidates, HistoricalCandidateRecord, "")
        _exact_tuple(state.approval_history, ApprovalRecord, "")
        _exact_tuple(state.diagnostics, WorkflowDiagnostic, "")
        _exact_tuple(state.operation_receipts, OperationReceipt, "")
        if state.active_issue is not None:
            if type(state.active_issue) not in (WorkflowBlocker, WorkflowFailure):
                return "issue_type"
            _revalidate_exact(state.active_issue, type(state.active_issue), "")

        if state.pending_source is not None:
            pending = state.pending_source.source_reference
            if pending.source_id in current_ids | reviewed_ids or pending.content_sha256 in current_digests | reviewed_digests:
                return "pending_source"

        if tuple(item.map_position for item in state.lecture_progress) != tuple(range(1, len(state.lecture_progress) + 1)):
            return "progress_order"
        if tuple(item.lecture_id for item in state.lecture_progress) != tuple(f"l{index}" for index in range(1, len(state.lecture_progress) + 1)):
            return "progress_ids"

        history_keys = tuple(
            (
                item.approved_map_subject_sha256,
                item.map_position,
                item.attempt,
                candidate_subject_sha256(item.candidate),
            )
            for item in state.historical_candidates
        )
        if history_keys != tuple(sorted(history_keys)) or len(set(history_keys)) != len(history_keys):
            return "historical_order"

        approval_revisions = tuple(item.state_revision for item in state.approval_history)
        if approval_revisions != tuple(sorted(approval_revisions)) or len(set(approval_revisions)) != len(approval_revisions):
            return "approval_order"
        approval_operations = tuple(item.operation_id for item in state.approval_history)
        if len(set(approval_operations)) != len(approval_operations):
            return "approval_operations"
        receipts_by_operation = {item.operation_id: item for item in state.operation_receipts}
        for approval in state.approval_history:
            approval_receipt = receipts_by_operation.get(approval.operation_id)
            if approval_receipt is None or approval_receipt.to_revision != approval.state_revision or approval_receipt.subject_sha256 != approval.subject_sha256:
                return "approval_receipt"
            if approval.gate == "priority_basis":
                expected_action = "approve_priority_basis" if approval.decision == "approved" else "reject_priority_basis"
            elif approval.gate == "lecture_map":
                expected_action = "approve_lecture_map" if approval.decision == "approved" else "reject_lecture_map"
            elif approval.gate == "new_source_irrelevant":
                expected_action = "dismiss_new_source"
            else:
                expected_action = "approve_map_reopen"
            if approval_receipt.action != expected_action:
                return "approval_action"

        if state.diagnostics != _sorted_diagnostics(state.diagnostics) or len(set(state.diagnostics)) != len(state.diagnostics):
            return "diagnostic_order"
        if any(item.category == "validation" for item in state.diagnostics):
            return "diagnostic_persistence"

        if not state.operation_receipts:
            return "receipts"
        for index, receipt in enumerate(state.operation_receipts):
            if receipt.from_revision != index - 1 or receipt.to_revision != index:
                return "receipt_chain"
        if state.operation_receipts[-1].to_revision != state.revision:
            return "receipt_revision"
        operation_ids = tuple(item.operation_id for item in state.operation_receipts)
        if len(set(operation_ids)) != len(operation_ids):
            return "receipt_operations"

        candidate_warning_codes = frozenset(_CANDIDATE_WARNING_MAP.values())
        progress_by_id = {item.lecture_id: item for item in state.lecture_progress}
        for diagnostic in state.diagnostics:
            if diagnostic.code in candidate_warning_codes:
                progress_item = progress_by_id.get(diagnostic.subject_id)
                if progress_item is None or progress_item.candidate is None:
                    return "candidate_warning"

        if state.source_assessment is not None:
            if state.source_assessment.source_set_sha256 != source_set_subject_sha256(state.source_evidence):
                return "assessment_subject"
            if state.source_assessment.recorded_revision > state.revision:
                return "assessment_revision"
            assessment_receipt = next((item for item in state.operation_receipts if item.to_revision == state.source_assessment.recorded_revision), None)
            if assessment_receipt is None or assessment_receipt.action != "record_source_assessment":
                return "assessment_receipt"

        if state.priority_basis is not None and state.priority_basis.status == "approved":
            approval = state.priority_basis.approval
            expected = priority_subject_sha256(state.priority_basis, state.policies.priority_basis)
            if approval is None or approval.subject_sha256 != expected or state.approval_history.count(approval) != 1:
                return "priority_approval"

        if state.lecture_map is not None and state.lecture_map.status == "approved":
            approval = state.lecture_map.approval
            expected = map_subject_sha256(state.lecture_map, state.policies.lecture_mapping)
            if approval is None or approval.subject_sha256 != expected or state.approval_history.count(approval) != 1:
                return "map_approval"

        if state.map_reopen_context is not None:
            context = state.map_reopen_context
            expected_baseline = _map_subject_from_parts(
                context.baseline_map_reference,
                context.baseline_lecture_ids,
                context.baseline_reserved_lecture_ids,
                state.policies.lecture_mapping,
            )
            if expected_baseline != context.baseline_map_subject_sha256 or state.approval_history.count(context.baseline_approval) != 1:
                return "reopen_context"
            reopen_decisions = tuple(item for item in state.approval_history if item.gate == "map_reopen" and item.decision == "approved" and item.state_revision == context.created_revision)
            if len(reopen_decisions) != 1:
                return "reopen_decision"
            if state.lecture_map is not None:
                if state.lecture_map.status != "proposed" or state.lecture_map.reserved_lecture_ids != context.baseline_lecture_ids or state.lecture_map.lecture_ids[: len(context.baseline_lecture_ids)] != context.baseline_lecture_ids:
                    return "reopen_map"

        if state.lecture_map is not None:
            target_ids = state.lecture_map.lecture_ids
        elif state.map_reopen_context is not None:
            target_ids = state.map_reopen_context.baseline_lecture_ids
        else:
            target_ids = ()
        if tuple(item.lecture_id for item in state.lecture_progress) != target_ids:
            return "progress_map"

        approved_map_subjects = {
            item.subject_sha256
            for item in state.approval_history
            if item.gate == "lecture_map" and item.decision == "approved"
        }
        lineage_ids = (
            state.map_reopen_context.baseline_lecture_ids
            if state.map_reopen_context is not None
            else state.lecture_map.lecture_ids
            if state.lecture_map is not None and state.lecture_map.status == "approved"
            else ()
        )
        for historical in state.historical_candidates:
            if historical.approved_map_subject_sha256 not in approved_map_subjects:
                return "historical_approval"
            if historical.map_position > len(lineage_ids) or lineage_ids[historical.map_position - 1] != historical.lecture_id:
                return "historical_lineage"

        candidates = tuple(item for item in state.lecture_progress if item.status == "candidate")
        if len(candidates) > 1:
            return "active_candidate"
        if state.stage == "lecture_validation":
            if len(candidates) != 1 or state.active_lecture_id != candidates[0].lecture_id:
                return "active_lecture"
        elif state.active_lecture_id is not None or candidates:
            return "active_lecture"

        issue_diagnostics = tuple(item for item in state.diagnostics if item.category in ("blocked", "failure"))
        if state.disposition == "blocked":
            if type(state.active_issue) is not WorkflowBlocker or state.active_issue.stage != state.stage:
                return "blocked_issue"
            if state.active_issue.recorded_revision != state.revision:
                return "blocked_revision"
            expected_issue = WorkflowDiagnostic(
                code=state.active_issue.code,
                severity="error",
                category="blocked",
                stage=state.stage,
                subject_id=state.active_issue.subject_id,
            )
            if issue_diagnostics != (expected_issue,):
                return "blocked_diagnostic"
        elif state.disposition == "failed":
            if type(state.active_issue) is not WorkflowFailure or state.active_issue.stage != state.stage:
                return "failed_issue"
            if state.active_issue.recorded_revision != state.revision:
                return "failed_revision"
            expected_issue = WorkflowDiagnostic(
                code=state.active_issue.code,
                severity="error",
                category="failure",
                stage=state.stage,
                subject_id=state.active_issue.subject_id,
            )
            if issue_diagnostics != (expected_issue,):
                return "failed_diagnostic"
        elif state.active_issue is not None or issue_diagnostics:
            return "unexpected_issue"

        if state.pending_source is not None:
            if type(state.active_issue) is not WorkflowBlocker or state.active_issue.code != "new_source_review_required" or state.active_issue.subject_sha256 != state.pending_source.blocker_subject_sha256:
                return "pending_issue"
            if state.pending_source.detected_revision != state.revision:
                return "pending_revision"
        elif type(state.active_issue) is WorkflowBlocker and state.active_issue.code == "new_source_review_required":
            return "pending_missing"

        if state.active_issue is None and state.disposition != NORMAL_DISPOSITIONS[state.stage]:
            return "normal_disposition"

        if state.revision == 0:
            if state.stage != "source_assessment" or state.disposition != "ready" or state.reviewed_source_evidence or any(
                value is not None
                for value in (
                    state.source_assessment,
                    state.priority_basis,
                    state.lecture_map,
                    state.map_reopen_context,
                    state.active_lecture_id,
                    state.pending_source,
                    state.active_issue,
                )
            ) or state.lecture_progress or state.historical_candidates or state.approval_history or state.diagnostics:
                return "initial_state"

        if state.stage == "priority_approval":
            if state.source_assessment is None or state.priority_basis is None or state.priority_basis.status != "proposed":
                return "priority_stage"
        if state.stage == "source_assessment":
            if state.source_assessment is not None or state.priority_basis is not None or state.lecture_map is not None:
                return "assessment_stage"
        if state.stage == "lecture_mapping":
            if state.source_assessment is None or state.priority_basis is None or state.priority_basis.status != "approved" or state.lecture_map is not None:
                return "mapping_stage"
        if state.stage == "map_approval":
            if state.source_assessment is None or state.priority_basis is None or state.priority_basis.status != "approved" or state.lecture_map is None or state.lecture_map.status != "proposed":
                return "map_stage"
        if state.stage in ("lecture_production", "lecture_validation", "completed"):
            if state.source_assessment is None or state.priority_basis is None or state.priority_basis.status != "approved" or state.lecture_map is None or state.lecture_map.status != "approved" or state.map_reopen_context is not None:
                return "production_stage"
        if state.stage == "completed":
            if not state.lecture_progress or any(item.status != "accepted" for item in state.lecture_progress) or state.pending_source is not None or state.active_issue is not None or any(item.severity == "error" for item in state.diagnostics):
                return "completed"
        return None
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        return "malformed"


def _state_validation_code(state: object) -> str | None:
    if type(state) is not WorkflowState:
        return "invalid_workflow_state"
    try:
        _require_exact_shape(state, WorkflowState, "")
        if type(state.contract_version) is str and state.contract_version not in ("course-workflow-state/v1", "course-workflow-state/v2"):
            return "unsupported_workflow_contract_version"
        nested_code = _nested_contract_code(state)
        if nested_code is not None:
            return nested_code
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        return "invalid_workflow_state"
    return None if _state_violation(state) is None else "invalid_workflow_state"


def validate_workflow_state(state: WorkflowState) -> tuple[WorkflowDiagnostic, ...]:
    """Validate one content-free snapshot without resolving any artifacts."""

    code = _state_validation_code(state)
    if code is None:
        return ()
    stage_value = getattr(state, "stage", None) if type(state) is WorkflowState else None
    stage = stage_value if _valid_literal(stage_value, WORKFLOW_STAGES) else "initialization"
    return (_diagnostic(code, stage),)


def resume_workflow(state: WorkflowState) -> WorkflowState | WorkflowRejected:
    """Return the same valid snapshot or a content-safe rejection."""

    diagnostics = validate_workflow_state(state)
    if diagnostics:
        return WorkflowRejected(status="rejected", state=_safe_rejected_state(state), diagnostics=diagnostics)
    return state


def _diagnostic(
    code: str, stage: str, subject_id: str | None = None
) -> WorkflowDiagnostic:
    severity, category, permitted = DIAGNOSTIC_REGISTRY[code]
    if stage not in permitted:
        stage = next(item for item in DIAGNOSTIC_STAGES if item in permitted)
    return WorkflowDiagnostic(
        code=code,
        severity=severity,
        category=category,
        stage=stage,
        subject_id=subject_id,
    )


def _reject(
    state: WorkflowState | None,
    code: str,
    *,
    stage: str | None = None,
    subject_id: str | None = None,
) -> WorkflowRejected:
    diagnostic_stage = stage or (state.stage if state is not None else "initialization")
    return WorkflowRejected(
        status="rejected",
        state=state,
        diagnostics=(_diagnostic(code, diagnostic_stage, subject_id),),
    )


def _receipt(
    state: WorkflowState | None,
    request: InitializeWorkflowRequest | WorkflowTransitionRequest,
    subject_sha256: str,
) -> OperationReceipt:
    from_revision = -1 if state is None else state.revision
    return OperationReceipt(
        contract_version=request.contract_version,
        operation_id=request.operation_id,
        from_revision=from_revision,
        to_revision=from_revision + 1,
        action=request.payload.action,
        request_sha256=request_fingerprint(request),
        outcome=RECEIPT_OUTCOMES[request.payload.action],
        subject_sha256=subject_sha256,
    )


def _mutated_state(
    state: WorkflowState,
    request: WorkflowTransitionRequest,
    subject_sha256: str,
    **changes: object,
) -> tuple[WorkflowState, OperationReceipt]:
    receipt = _receipt(state, request, subject_sha256)
    changes["revision"] = receipt.to_revision
    changes["operation_receipts"] = state.operation_receipts + (receipt,)
    return replace(state, **changes), receipt


def _without_issue_diagnostics(
    diagnostics: tuple[WorkflowDiagnostic, ...],
) -> tuple[WorkflowDiagnostic, ...]:
    return tuple(item for item in diagnostics if item.category not in ("blocked", "failure"))


def _without_candidate_warnings(
    diagnostics: tuple[WorkflowDiagnostic, ...], lecture_id: str | None = None
) -> tuple[WorkflowDiagnostic, ...]:
    codes = frozenset(_CANDIDATE_WARNING_MAP.values())
    return tuple(
        item
        for item in diagnostics
        if not (item.code in codes and (lecture_id is None or item.subject_id == lecture_id))
    )


def _warning_diagnostics(
    diagnostics: tuple[RenderDiagnostic, ...], lecture_id: str
) -> tuple[WorkflowDiagnostic, ...]:
    return tuple(
        _diagnostic(_CANDIDATE_WARNING_MAP[item.code], "lecture_validation", lecture_id)
        for item in diagnostics
        if item.severity == "warning" and item.code in _CANDIDATE_WARNING_MAP
    )


def _find_progress(state: WorkflowState, lecture_id: str) -> tuple[int, LectureProgress] | None:
    for index, item in enumerate(state.lecture_progress):
        if item.lecture_id == lecture_id:
            return index, item
    return None


def _replace_progress(
    progress: tuple[LectureProgress, ...], index: int, value: LectureProgress
) -> tuple[LectureProgress, ...]:
    return progress[:index] + (value,) + progress[index + 1 :]


def _history_key(item: HistoricalCandidateRecord) -> tuple[str, int, int, str]:
    return (
        item.approved_map_subject_sha256,
        item.map_position,
        item.attempt,
        candidate_subject_sha256(item.candidate),
    )


def _initialize(request: InitializeWorkflowRequest) -> WorkflowAdvanced:
    subject = source_set_subject_sha256(request.payload.source_evidence)
    receipt = _receipt(None, request, subject)
    state = WorkflowState(
        contract_version=paired_state_version(request.contract_version),  # type: ignore[arg-type]
        workflow_id=request.workflow_id,
        revision=0,
        stage="source_assessment",
        disposition="ready",
        policies=request.payload.policies,
        source_evidence=request.payload.source_evidence,
        reviewed_source_evidence=(),
        source_assessment=None,
        priority_basis=None,
        lecture_map=None,
        map_reopen_context=None,
        active_lecture_id=None,
        pending_source=None,
        lecture_progress=(),
        historical_candidates=(),
        active_issue=None,
        approval_history=(),
        diagnostics=(),
        operation_receipts=(receipt,),
    )
    return WorkflowAdvanced(status="advanced", state=state, receipt=receipt, diagnostics=())


def apply_workflow_request(
    state: WorkflowState | None,
    request: InitializeWorkflowRequest | WorkflowTransitionRequest,
) -> WorkflowTransitionResult:
    """Apply one pure deterministic request to an optional current snapshot."""

    if state is not None:
        state_diagnostics = validate_workflow_state(state)
        if state_diagnostics:
            return WorkflowRejected(
                status="rejected",
                state=_safe_rejected_state(state),
                diagnostics=state_diagnostics,
            )

    request_code = _request_validation_code(request)
    if request_code is not None:
        diagnostic_stage = state.stage if state is not None else "initialization"
        return _reject(state, request_code, stage=diagnostic_stage)

    if state is None:
        if type(request) is not InitializeWorkflowRequest:
            return _reject(None, "illegal_transition")
        return _initialize(request)

    if type(request) is InitializeWorkflowRequest:
        if request.workflow_id != state.workflow_id:
            return _reject(state, "invalid_workflow_id")
        existing = next((item for item in state.operation_receipts if item.operation_id == request.operation_id), None)
        if existing is None:
            return _reject(state, "illegal_transition")
        fingerprint = request_fingerprint(request)
        if existing.action == "initialize_workflow" and existing.request_sha256 == fingerprint:
            return WorkflowIdempotentRepeat(status="idempotent_repeat", state=state, original_receipt=existing)
        return _reject(state, "operation_id_reuse")

    if type(request) is not WorkflowTransitionRequest:
        return _reject(state, "unsupported_transition_contract_version")
    if request.workflow_id != state.workflow_id:
        return _reject(state, "invalid_workflow_id")
    if paired_state_version(request.contract_version) != state.contract_version:
        return _reject(state, "unsupported_transition_contract_version")

    fingerprint = request_fingerprint(request)
    existing = next((item for item in state.operation_receipts if item.operation_id == request.operation_id), None)
    if existing is not None:
        if existing.request_sha256 == fingerprint:
            return WorkflowIdempotentRepeat(status="idempotent_repeat", state=state, original_receipt=existing)
        return _reject(state, "operation_id_reuse")
    if request.expected_revision != state.revision:
        return _reject(state, "stale_revision")

    payload = request.payload
    if type(payload) is RecordSourceAssessment:
        return _record_source_assessment(state, request, payload)
    if type(payload) is ApprovePriorityBasis:
        return _decide_priority(state, request, payload, approved=True)
    if type(payload) is RejectPriorityBasis:
        return _decide_priority(state, request, payload, approved=False)
    if type(payload) is RecordLectureMap:
        return _record_lecture_map(state, request, payload)
    if type(payload) is ApproveLectureMap:
        return _decide_map(state, request, payload, approved=True)
    if type(payload) is RejectLectureMap:
        return _decide_map(state, request, payload, approved=False)
    if type(payload) is SubmitLectureCandidate:
        return _submit_candidate(state, request, payload)
    if type(payload) is ReopenLecturesForCorrection:
        return _reopen_for_correction(state, request, payload)
    if type(payload) is RecordCandidateValidation:
        return _record_validation(state, request, payload)
    if type(payload) is RecordOperationFailure:
        return _record_failure(state, request, payload)
    if type(payload) is RetryFailed:
        return _retry_failure(state, request, payload)
    if type(payload) is MarkBlocked:
        return _mark_blocked(state, request, payload)
    if type(payload) is ClearBlocker:
        return _clear_blocker(state, request, payload)
    if type(payload) is RecordNewSource:
        return _record_new_source(state, request, payload)
    if type(payload) is DismissNewSource:
        return _dismiss_new_source(state, request, payload)
    if type(payload) is ApproveMapReopen:
        return _approve_map_reopen(state, request, payload)
    return _reject(state, "illegal_transition")


def _record_source_assessment(
    state: WorkflowState,
    request: WorkflowTransitionRequest,
    payload: RecordSourceAssessment,
) -> WorkflowTransitionResult:
    if state.stage != "source_assessment" or state.disposition != "ready" or state.active_issue is not None:
        return _reject(state, "illegal_transition")
    revision = state.revision + 1
    assessment = SourceAssessmentRecord(
        source_set_sha256=source_set_subject_sha256(state.source_evidence),
        assessment_reference=payload.assessment_reference,
        recorded_revision=revision,
    )
    basis = PriorityBasisRecord(
        primary_mode=payload.primary_mode,
        proposal_reference=payload.priority_proposal_reference,
        evidence_hierarchy_reference=payload.evidence_hierarchy_reference,
        status="proposed",
        approval=None,
    )
    subject = priority_subject_sha256(basis, state.policies.priority_basis)
    new_state, receipt = _mutated_state(
        state,
        request,
        subject,
        stage="priority_approval",
        disposition="awaiting_approval",
        source_assessment=assessment,
        priority_basis=basis,
    )
    return WorkflowAdvanced(status="advanced", state=new_state, receipt=receipt, diagnostics=())


def _decide_priority(
    state: WorkflowState,
    request: WorkflowTransitionRequest,
    payload: ApprovePriorityBasis | RejectPriorityBasis,
    *,
    approved: bool,
) -> WorkflowTransitionResult:
    if state.stage != "priority_approval" or state.disposition != "awaiting_approval" or state.priority_basis is None or state.priority_basis.status != "proposed":
        return _reject(state, "illegal_transition")
    subject = priority_subject_sha256(state.priority_basis, state.policies.priority_basis)
    if payload.priority_subject_sha256 != subject:
        return _reject(state, "approval_subject_mismatch")
    revision = state.revision + 1
    approval = ApprovalRecord(
        gate="priority_basis",
        decision="approved" if approved else "rejected",
        subject_sha256=subject,
        state_revision=revision,
        operation_id=request.operation_id,
    )
    history = state.approval_history + (approval,)
    if approved:
        basis = replace(state.priority_basis, status="approved", approval=approval)
        changes = dict(
            stage="lecture_mapping",
            disposition="ready",
            priority_basis=basis,
            approval_history=history,
        )
    else:
        changes = dict(
            stage="source_assessment",
            disposition="ready",
            source_assessment=None,
            priority_basis=None,
            approval_history=history,
        )
    new_state, receipt = _mutated_state(state, request, subject, **changes)
    return WorkflowAdvanced(status="advanced", state=new_state, receipt=receipt, diagnostics=())


def _record_lecture_map(
    state: WorkflowState,
    request: WorkflowTransitionRequest,
    payload: RecordLectureMap,
) -> WorkflowTransitionResult:
    if state.stage != "lecture_mapping" or state.disposition != "ready" or state.priority_basis is None or state.priority_basis.status != "approved":
        return _reject(state, "illegal_transition")
    context = state.map_reopen_context
    reserved = () if context is None else context.baseline_lecture_ids
    if context is not None and payload.lecture_ids[: len(reserved)] != reserved:
        return _reject(state, "map_reopen_violation")
    lecture_map = LectureMapRecord(
        map_reference=payload.map_reference,
        lecture_ids=payload.lecture_ids,
        status="proposed",
        approval=None,
        reserved_lecture_ids=reserved,
    )
    if context is None:
        progress = tuple(
            LectureProgress(
                lecture_id=lecture_id,
                map_position=index,
                status="pending",
                attempt=0,
                candidate=None,
                validation=None,
                accepted_document=None,
            )
            for index, lecture_id in enumerate(payload.lecture_ids, 1)
        )
    else:
        baseline = state.lecture_progress[: len(reserved)]
        appended = tuple(
            LectureProgress(
                lecture_id=lecture_id,
                map_position=index,
                status="pending",
                attempt=0,
                candidate=None,
                validation=None,
                accepted_document=None,
            )
            for index, lecture_id in enumerate(payload.lecture_ids[len(reserved) :], len(reserved) + 1)
        )
        progress = baseline + appended
    subject = map_subject_sha256(lecture_map, state.policies.lecture_mapping)
    new_state, receipt = _mutated_state(
        state,
        request,
        subject,
        stage="map_approval",
        disposition="awaiting_approval",
        lecture_map=lecture_map,
        lecture_progress=progress,
    )
    return WorkflowAdvanced(status="advanced", state=new_state, receipt=receipt, diagnostics=())


def _decide_map(
    state: WorkflowState,
    request: WorkflowTransitionRequest,
    payload: ApproveLectureMap | RejectLectureMap,
    *,
    approved: bool,
) -> WorkflowTransitionResult:
    if state.stage != "map_approval" or state.disposition != "awaiting_approval" or state.lecture_map is None or state.lecture_map.status != "proposed":
        return _reject(state, "illegal_transition")
    subject = map_subject_sha256(state.lecture_map, state.policies.lecture_mapping)
    if payload.map_subject_sha256 != subject:
        return _reject(state, "approval_subject_mismatch")
    revision = state.revision + 1
    approval = ApprovalRecord(
        gate="lecture_map",
        decision="approved" if approved else "rejected",
        subject_sha256=subject,
        state_revision=revision,
        operation_id=request.operation_id,
    )
    history = state.approval_history + (approval,)
    if approved:
        lecture_map = replace(state.lecture_map, status="approved", approval=approval)
        changes = dict(
            stage="lecture_production",
            disposition="ready",
            lecture_map=lecture_map,
            map_reopen_context=None,
            approval_history=history,
        )
    elif state.map_reopen_context is None:
        changes = dict(
            stage="lecture_mapping",
            disposition="ready",
            lecture_map=None,
            lecture_progress=(),
            approval_history=history,
        )
    else:
        count = len(state.map_reopen_context.baseline_lecture_ids)
        changes = dict(
            stage="lecture_mapping",
            disposition="ready",
            lecture_map=None,
            lecture_progress=state.lecture_progress[:count],
            approval_history=history,
        )
    new_state, receipt = _mutated_state(state, request, subject, **changes)
    return WorkflowAdvanced(status="advanced", state=new_state, receipt=receipt, diagnostics=())


def _submit_candidate(
    state: WorkflowState,
    request: WorkflowTransitionRequest,
    payload: SubmitLectureCandidate,
) -> WorkflowTransitionResult:
    if state.stage != "lecture_production" or state.disposition != "ready" or state.lecture_map is None or state.lecture_map.status != "approved" or state.active_issue is not None:
        return _reject(state, "illegal_transition")
    found = _find_progress(state, payload.lecture_id)
    if found is None or found[1].status not in ("pending", "retry_required", "correction_required"):
        return _reject(state, "illegal_transition")
    index, old_progress = found
    diagnostics = validate_document(payload.document)
    if has_validation_errors(diagnostics):
        return _reject(state, "invalid_candidate_document", subject_id=payload.lecture_id)
    if payload.document.document_id != payload.lecture_id:
        return _reject(state, "candidate_id_mismatch", subject_id=payload.lecture_id)
    if payload.document.order != old_progress.map_position:
        return _reject(state, "candidate_order_mismatch", subject_id=payload.lecture_id)
    reference = document_reference(payload.document)
    if reference is None:
        return _reject(state, "invalid_candidate_document", subject_id=payload.lecture_id)
    subject = candidate_subject_sha256(reference)
    history = list(state.historical_candidates)
    if old_progress.status in ("retry_required", "correction_required"):
        if old_progress.candidate is None:
            return _reject(state, "invalid_workflow_state")
        map_subject = map_subject_sha256(state.lecture_map, state.policies.lecture_mapping)
        history.append(
            HistoricalCandidateRecord(
                lecture_id=old_progress.lecture_id,
                map_position=old_progress.map_position,
                approved_map_subject_sha256=map_subject,
                attempt=old_progress.attempt,
                candidate=old_progress.candidate,
                validation=old_progress.validation,
                historical_disposition="superseded_by_correction" if old_progress.status == "correction_required" else "rejected",
            )
        )
    new_progress = LectureProgress(
        lecture_id=old_progress.lecture_id,
        map_position=old_progress.map_position,
        status="candidate",
        attempt=old_progress.attempt + 1,
        candidate=reference,
        validation=None,
        accepted_document=None,
    )
    warnings = _warning_diagnostics(diagnostics, payload.lecture_id)
    persisted = _without_candidate_warnings(state.diagnostics, payload.lecture_id) + warnings
    new_state, receipt = _mutated_state(
        state,
        request,
        subject,
        stage="lecture_validation",
        disposition="ready",
        active_lecture_id=payload.lecture_id,
        lecture_progress=_replace_progress(state.lecture_progress, index, new_progress),
        historical_candidates=tuple(sorted(history, key=_history_key)),
        diagnostics=_sorted_diagnostics(persisted),
    )
    return WorkflowAdvanced(status="advanced", state=new_state, receipt=receipt, diagnostics=warnings)


def correction_reopen_subject_sha256(payload: ReopenLecturesForCorrection) -> Sha256Hex:
    _revalidate_exact(payload, ReopenLecturesForCorrection, "correction reopen payload is invalid")
    return _sha256(_record("correction-reopen-subject/v1", (
        ("lecture_ids", payload.lecture_ids),
        ("review_request_id", payload.review_request_id),
        ("review_operation_id", payload.review_operation_id),
        ("review_revision", payload.review_revision),
    )))


def _reopen_for_correction(
    state: WorkflowState,
    request: WorkflowTransitionRequest,
    payload: ReopenLecturesForCorrection,
) -> WorkflowTransitionResult:
    if state.contract_version != "course-workflow-state/v2" or state.stage != "completed" or state.disposition != "completed" or state.lecture_map is None or state.lecture_map.status != "approved" or state.active_issue is not None or state.pending_source is not None or state.map_reopen_context is not None or state.active_lecture_id is not None or payload.review_revision != state.revision:
        return _reject(state, "illegal_transition")
    ids = state.lecture_map.lecture_ids
    if tuple(item for item in ids if item in payload.lecture_ids) != payload.lecture_ids:
        return _reject(state, "illegal_transition")
    selected = set(payload.lecture_ids)
    if any(item.lecture_id in selected and item.status != "accepted" for item in state.lecture_progress):
        return _reject(state, "illegal_transition")
    progress = tuple(replace(item, status="correction_required") if item.lecture_id in selected else item for item in state.lecture_progress)
    state_new, receipt = _mutated_state(
        state, request, correction_reopen_subject_sha256(payload), stage="lecture_production",
        disposition="ready", active_lecture_id=None, lecture_progress=progress,
    )
    return WorkflowAdvanced(status="advanced", state=state_new, receipt=receipt, diagnostics=())


def _record_validation(
    state: WorkflowState,
    request: WorkflowTransitionRequest,
    payload: RecordCandidateValidation,
) -> WorkflowTransitionResult:
    if state.stage != "lecture_validation" or state.disposition != "ready" or state.active_lecture_id != payload.lecture_id:
        return _reject(state, "illegal_transition")
    found = _find_progress(state, payload.lecture_id)
    if found is None or found[1].status != "candidate" or found[1].candidate is None:
        return _reject(state, "invalid_workflow_state")
    index, progress = found
    subject = candidate_subject_sha256(progress.candidate)
    if payload.candidate_subject_sha256 != subject or payload.validation.candidate_subject_sha256 != subject:
        return _reject(state, "candidate_subject_mismatch", subject_id=payload.lecture_id)
    policy = state.policies.lecture_validation
    if payload.validation.validation_policy_version != policy.policy_version or payload.validation.validation_policy_sha256 != policy.content_sha256:
        return _reject(state, "validation_policy_mismatch", subject_id=payload.lecture_id)
    if payload.validation.validation_reference.producer_version != policy.policy_version:
        return _reject(state, "invalid_validation_evidence", subject_id=payload.lecture_id)
    if payload.validation.disposition == "rejected":
        updated = LectureProgress(
            lecture_id=progress.lecture_id,
            map_position=progress.map_position,
            status="retry_required",
            attempt=progress.attempt,
            candidate=progress.candidate,
            validation=payload.validation,
            accepted_document=None,
        )
        next_stage: WorkflowStage = "lecture_production"
        next_disposition: WorkflowDisposition = "ready"
    else:
        updated = LectureProgress(
            lecture_id=progress.lecture_id,
            map_position=progress.map_position,
            status="accepted",
            attempt=progress.attempt,
            candidate=progress.candidate,
            validation=payload.validation,
            accepted_document=progress.candidate,
        )
        tentative = _replace_progress(state.lecture_progress, index, updated)
        complete = all(item.status == "accepted" for item in tentative)
        next_stage = "completed" if complete else "lecture_production"
        next_disposition = "completed" if complete else "ready"
    new_progress = _replace_progress(state.lecture_progress, index, updated)
    new_state, receipt = _mutated_state(
        state,
        request,
        subject,
        stage=next_stage,
        disposition=next_disposition,
        active_lecture_id=None,
        lecture_progress=new_progress,
    )
    return WorkflowAdvanced(status="advanced", state=new_state, receipt=receipt, diagnostics=())


def _record_failure(
    state: WorkflowState,
    request: WorkflowTransitionRequest,
    payload: RecordOperationFailure,
) -> WorkflowTransitionResult:
    if state.disposition != "ready" or state.stage not in FAILURE_REGISTRY or FAILURE_REGISTRY[state.stage] != (payload.failed_action, payload.failure_code) or state.active_issue is not None:
        return _reject(state, "illegal_transition")
    revision = state.revision + 1
    failure = WorkflowFailure(
        code=payload.failure_code,
        stage=state.stage,
        failed_action=payload.failed_action,
        subject_id=payload.subject_id,
        evidence_reference=payload.evidence_reference,
        recorded_revision=revision,
    )
    diagnostic = _diagnostic(payload.failure_code, state.stage, payload.subject_id)
    diagnostics = _sorted_diagnostics(state.diagnostics + (diagnostic,))
    subject = payload.evidence_reference.content_sha256
    new_state, receipt = _mutated_state(
        state,
        request,
        subject,
        disposition="failed",
        active_issue=failure,
        diagnostics=diagnostics,
    )
    return WorkflowFailed(status="failed", state=new_state, receipt=receipt, failure=failure)


def _retry_failure(
    state: WorkflowState,
    request: WorkflowTransitionRequest,
    payload: RetryFailed,
) -> WorkflowTransitionResult:
    failure = state.active_issue
    if state.disposition != "failed" or type(failure) is not WorkflowFailure or failure.code != payload.failure_code or failure.evidence_reference.content_sha256 != payload.failure_evidence_sha256:
        return _reject(state, "illegal_transition")
    new_state, receipt = _mutated_state(
        state,
        request,
        payload.failure_evidence_sha256,
        disposition=NORMAL_DISPOSITIONS[state.stage],
        active_issue=None,
        diagnostics=_without_issue_diagnostics(state.diagnostics),
    )
    return WorkflowAdvanced(status="advanced", state=new_state, receipt=receipt, diagnostics=())


def _mark_blocked(
    state: WorkflowState,
    request: WorkflowTransitionRequest,
    payload: MarkBlocked,
) -> WorkflowTransitionResult:
    if state.active_issue is not None or state.disposition != NORMAL_DISPOSITIONS[state.stage] or payload.evidence_reference.artifact_kind != "blocker":
        return _reject(state, "illegal_transition")
    if payload.blocker_code == "private_artifact_unavailable":
        legal = (state.stage, state.disposition) in PRIVATE_ARTIFACT_BLOCKER_STARTS
    else:
        legal = state.stage in EXTERNAL_PREREQUISITE_BLOCKER_STAGES and state.disposition == "ready"
    if not legal:
        return _reject(state, "illegal_transition")
    revision = state.revision + 1
    blocker = WorkflowBlocker(
        code=payload.blocker_code,
        stage=state.stage,
        resume_disposition=state.disposition,
        subject_id=payload.subject_id,
        subject_sha256=payload.subject_sha256,
        evidence_reference=payload.evidence_reference,
        recorded_revision=revision,
    )
    diagnostic = _diagnostic(payload.blocker_code, state.stage, payload.subject_id)
    new_state, receipt = _mutated_state(
        state,
        request,
        payload.subject_sha256,
        disposition="blocked",
        active_issue=blocker,
        diagnostics=_sorted_diagnostics(state.diagnostics + (diagnostic,)),
    )
    return WorkflowBlocked(status="blocked", state=new_state, receipt=receipt, blocker=blocker)


def _clear_blocker(
    state: WorkflowState,
    request: WorkflowTransitionRequest,
    payload: ClearBlocker,
) -> WorkflowTransitionResult:
    blocker = state.active_issue
    if state.disposition != "blocked" or type(blocker) is not WorkflowBlocker or blocker.code == "new_source_review_required" or blocker.code != payload.blocker_code or blocker.subject_sha256 != payload.subject_sha256:
        return _reject(state, "illegal_transition")
    new_state, receipt = _mutated_state(
        state,
        request,
        payload.subject_sha256,
        disposition=blocker.resume_disposition,
        active_issue=None,
        diagnostics=_without_issue_diagnostics(state.diagnostics),
    )
    return WorkflowAdvanced(status="advanced", state=new_state, receipt=receipt, diagnostics=())


def _record_new_source(
    state: WorkflowState,
    request: WorkflowTransitionRequest,
    payload: RecordNewSource,
) -> WorkflowTransitionResult:
    if state.stage not in NEW_SOURCE_STAGES or state.disposition != "ready" or state.active_issue is not None or state.pending_source is not None:
        return _reject(state, "illegal_transition")
    if payload.evidence_reference.artifact_kind != "blocker" or payload.evidence_reference.producer_version != WORKFLOW_ARTIFACT_PRODUCER_VERSION:
        return _reject(state, "illegal_transition")
    all_sources = state.source_evidence + state.reviewed_source_evidence
    if any(item.source_id == payload.source_reference.source_id for item in all_sources):
        return _reject(state, "duplicate_source_id", subject_id=payload.source_reference.source_id)
    if any(item.content_sha256 == payload.source_reference.content_sha256 for item in all_sources):
        return _reject(state, "duplicate_source_digest", subject_id=payload.source_reference.source_id)
    revision = state.revision + 1
    pending = PendingSourceRecord(
        source_reference=payload.source_reference,
        detected_stage=state.stage,
        detected_revision=revision,
        blocker_subject_sha256=payload.source_reference.content_sha256,
    )
    blocker = WorkflowBlocker(
        code="new_source_review_required",
        stage=state.stage,
        resume_disposition="ready",
        subject_id=payload.source_reference.source_id,
        subject_sha256=payload.source_reference.content_sha256,
        evidence_reference=payload.evidence_reference,
        recorded_revision=revision,
    )
    diagnostic = _diagnostic("new_source_review_required", state.stage, payload.source_reference.source_id)
    new_state, receipt = _mutated_state(
        state,
        request,
        payload.source_reference.content_sha256,
        disposition="blocked",
        pending_source=pending,
        active_issue=blocker,
        diagnostics=_sorted_diagnostics(state.diagnostics + (diagnostic,)),
    )
    return WorkflowBlocked(status="blocked", state=new_state, receipt=receipt, blocker=blocker)


def _new_source_state(state: WorkflowState) -> tuple[PendingSourceRecord, WorkflowBlocker] | None:
    if state.disposition != "blocked" or state.pending_source is None or type(state.active_issue) is not WorkflowBlocker or state.active_issue.code != "new_source_review_required":
        return None
    return state.pending_source, state.active_issue


def _dismiss_new_source(
    state: WorkflowState,
    request: WorkflowTransitionRequest,
    payload: DismissNewSource,
) -> WorkflowTransitionResult:
    pair = _new_source_state(state)
    if pair is None:
        return _reject(state, "illegal_transition")
    pending, blocker = pair
    if payload.source_content_sha256 != pending.source_reference.content_sha256:
        return _reject(state, "new_source_subject_mismatch")
    revision = state.revision + 1
    approval = ApprovalRecord(
        gate="new_source_irrelevant",
        decision="approved",
        subject_sha256=payload.source_content_sha256,
        state_revision=revision,
        operation_id=request.operation_id,
    )
    reviewed = tuple(sorted(state.reviewed_source_evidence + (pending.source_reference,), key=lambda item: item.source_id))
    new_state, receipt = _mutated_state(
        state,
        request,
        payload.source_content_sha256,
        stage=pending.detected_stage,
        disposition=blocker.resume_disposition,
        reviewed_source_evidence=reviewed,
        pending_source=None,
        active_issue=None,
        approval_history=state.approval_history + (approval,),
        diagnostics=_without_issue_diagnostics(state.diagnostics),
    )
    return WorkflowAdvanced(status="advanced", state=new_state, receipt=receipt, diagnostics=())


def _approve_map_reopen(
    state: WorkflowState,
    request: WorkflowTransitionRequest,
    payload: ApproveMapReopen,
) -> WorkflowTransitionResult:
    pair = _new_source_state(state)
    if pair is None or state.lecture_map is None or state.lecture_map.status != "approved" or state.lecture_map.approval is None:
        return _reject(state, "illegal_transition")
    pending, _blocker = pair
    if payload.source_content_sha256 != pending.source_reference.content_sha256:
        return _reject(state, "new_source_subject_mismatch")
    revision = state.revision + 1
    current_map_subject = map_subject_sha256(state.lecture_map, state.policies.lecture_mapping)
    context = MapReopenContext(
        baseline_map_reference=state.lecture_map.map_reference,
        baseline_lecture_ids=state.lecture_map.lecture_ids,
        baseline_reserved_lecture_ids=state.lecture_map.reserved_lecture_ids,
        baseline_map_subject_sha256=current_map_subject,
        baseline_approval=state.lecture_map.approval,
        created_revision=revision,
    )
    history = list(state.historical_candidates)
    for progress in state.lecture_progress:
        if progress.candidate is not None:
            history.append(
                HistoricalCandidateRecord(
                    lecture_id=progress.lecture_id,
                    map_position=progress.map_position,
                    approved_map_subject_sha256=current_map_subject,
                    attempt=progress.attempt,
                    candidate=progress.candidate,
                    validation=progress.validation,
                    historical_disposition="invalidated_by_map_reopen",
                )
            )
    reset_progress = tuple(
        LectureProgress(
            lecture_id=lecture_id,
            map_position=index,
            status="pending",
            attempt=0,
            candidate=None,
            validation=None,
            accepted_document=None,
        )
        for index, lecture_id in enumerate(state.lecture_map.lecture_ids, 1)
    )
    approval = ApprovalRecord(
        gate="map_reopen",
        decision="approved",
        subject_sha256=payload.source_content_sha256,
        state_revision=revision,
        operation_id=request.operation_id,
    )
    sources = tuple(sorted(state.source_evidence + (pending.source_reference,), key=lambda item: item.source_id))
    diagnostics = _without_candidate_warnings(_without_issue_diagnostics(state.diagnostics))
    new_state, receipt = _mutated_state(
        state,
        request,
        payload.source_content_sha256,
        stage="source_assessment",
        disposition="ready",
        source_evidence=sources,
        source_assessment=None,
        priority_basis=None,
        lecture_map=None,
        map_reopen_context=context,
        active_lecture_id=None,
        pending_source=None,
        lecture_progress=reset_progress,
        historical_candidates=tuple(sorted(history, key=_history_key)),
        active_issue=None,
        approval_history=state.approval_history + (approval,),
        diagnostics=diagnostics,
    )
    return WorkflowAdvanced(status="advanced", state=new_state, receipt=receipt, diagnostics=())


def ordered_accepted_documents(
    state: WorkflowState,
) -> tuple[DocumentReference, ...]:
    """Return accepted safe references in current map-position order."""

    if validate_workflow_state(state):
        raise ValueError("workflow state is invalid")
    return tuple(
        item.accepted_document
        for item in state.lecture_progress
        if item.accepted_document is not None
    )


def _actions_for(
    stage: str, disposition: str, active_issue_code: str | None
) -> tuple[WorkflowAction, ...]:
    if disposition == "failed":
        legal = {"retry_failed"} if stage in FAILURE_REGISTRY and FAILURE_REGISTRY[stage][1] == active_issue_code else set()
    elif disposition == "blocked":
        if active_issue_code == "new_source_review_required":
            legal = {"dismiss_new_source", "approve_map_reopen"} if stage in NEW_SOURCE_STAGES else set()
        elif active_issue_code == "private_artifact_unavailable" and (stage, NORMAL_DISPOSITIONS.get(stage)) in PRIVATE_ARTIFACT_BLOCKER_STARTS:
            legal = {"clear_blocker"}
        elif active_issue_code == "external_prerequisite_unavailable" and stage in EXTERNAL_PREREQUISITE_BLOCKER_STAGES:
            legal = {"clear_blocker"}
        else:
            legal = set()
    elif disposition != NORMAL_DISPOSITIONS.get(stage):
        legal = set()
    elif stage == "source_assessment":
        legal = {"record_source_assessment", "record_operation_failure", "mark_blocked"}
    elif stage == "priority_approval":
        legal = {"approve_priority_basis", "reject_priority_basis", "mark_blocked"}
    elif stage == "lecture_mapping":
        legal = {"record_lecture_map", "record_operation_failure", "mark_blocked"}
    elif stage == "map_approval":
        legal = {"approve_lecture_map", "reject_lecture_map", "mark_blocked"}
    elif stage == "lecture_production":
        legal = {"submit_lecture_candidate", "record_operation_failure", "mark_blocked", "record_new_source"}
    elif stage == "lecture_validation":
        legal = {"record_candidate_validation", "record_operation_failure", "mark_blocked", "record_new_source"}
    else:
        legal = set()
    return tuple(action for action in WORKFLOW_ACTIONS if action in legal)


def _legal_actions(state: WorkflowState) -> tuple[WorkflowAction, ...]:
    actions = _actions_for(
        state.stage,
        state.disposition,
        state.active_issue.code if state.active_issue is not None else None,
    )
    if state.contract_version == "course-workflow-state/v2" and state.stage == "completed" and state.disposition == "completed":
        return ("reopen_lectures_for_correction",)
    return actions


def derive_workflow_handoff(state: WorkflowState) -> WorkflowHandoff:
    """Derive the deterministic content-free handoff projection."""

    if validate_workflow_state(state):
        raise ValueError("workflow state is invalid")
    priority_mode = state.priority_basis.primary_mode if state.priority_basis is not None else None
    priority_approved = state.priority_basis is not None and state.priority_basis.status == "approved"
    map_approved = state.lecture_map is not None and state.lecture_map.status == "approved"
    if state.lecture_map is not None:
        lecture_ids = state.lecture_map.lecture_ids
    elif state.map_reopen_context is not None:
        lecture_ids = state.map_reopen_context.baseline_lecture_ids
    else:
        lecture_ids = ()
    codes: list[str] = []
    seen: set[str] = set()
    for diagnostic in state.diagnostics:
        if diagnostic.code not in seen:
            seen.add(diagnostic.code)
            codes.append(diagnostic.code)
    return WorkflowHandoff(
        contract_version=paired_handoff_version(state.contract_version),  # type: ignore[arg-type]
        workflow_id=state.workflow_id,
        revision=state.revision,
        stage=state.stage,
        disposition=state.disposition,
        source_count=len(state.source_evidence),
        priority_mode=priority_mode,
        priority_approved=priority_approved,
        map_approved=map_approved,
        lecture_ids=lecture_ids,
        active_lecture_id=state.active_lecture_id,
        accepted_documents=ordered_accepted_documents(state),
        pending_source_sha256=state.pending_source.source_reference.content_sha256 if state.pending_source is not None else None,
        active_issue_code=state.active_issue.code if state.active_issue is not None else None,
        diagnostic_codes=tuple(codes),
        next_actions=_legal_actions(state),
    )


def replay_workflow(
    initialization: InitializeWorkflowRequest,
    requests: tuple[WorkflowTransitionRequest, ...],
    expected_receipts: tuple[OperationReceipt, ...],
    expected_states: tuple[WorkflowState, ...],
) -> WorkflowState | WorkflowRejected:
    """Deterministically reapply an initialization-inclusive request sequence."""

    if _request_validation_code(initialization) is not None:
        return _reject(None, "invalid_workflow_state")
    if type(requests) is not tuple or any(
        type(item) is not WorkflowTransitionRequest or _request_validation_code(item) is not None
        for item in requests
    ):
        return _reject(None, "invalid_workflow_state")
    try:
        _exact_tuple(expected_receipts, OperationReceipt, "replay receipts are invalid")
        _exact_tuple(expected_states, WorkflowState, "replay states are invalid")
    except ValueError:
        return _reject(None, "invalid_workflow_state")
    if len(expected_receipts) != len(requests) + 1 or len(expected_states) != len(requests) + 1:
        return _reject(None, "invalid_workflow_state")
    if any(_state_validation_code(item) is not None for item in expected_states):
        return _reject(None, "invalid_workflow_state")
    initial_result = apply_workflow_request(None, initialization)
    if type(initial_result) is not WorkflowAdvanced:
        return _reject(None, "invalid_workflow_state")
    if initial_result.receipt != expected_receipts[0] or initial_result.state != expected_states[0]:
        return _reject(initial_result.state, "invalid_workflow_state")
    current = initial_result.state
    for index, request in enumerate(requests):
        if request.expected_revision != index:
            return _reject(current, "invalid_workflow_state")
        result = apply_workflow_request(current, request)
        if type(result) not in (WorkflowAdvanced, WorkflowBlocked, WorkflowFailed):
            return _reject(current, "invalid_workflow_state")
        if result.receipt != expected_receipts[index + 1] or result.state != expected_states[index + 1]:
            return _reject(result.state, "invalid_workflow_state")
        current = result.state
    return expected_states[-1]


__all__ = [
    "ApprovalRecord",
    "ApproveLectureMap",
    "ApproveMapReopen",
    "ApprovePriorityBasis",
    "ArtifactId",
    "ArtifactKind",
    "Attempt",
    "BlockerCode",
    "ClearBlocker",
    "DIAGNOSTIC_MESSAGES",
    "DIAGNOSTIC_REGISTRY",
    "DiagnosticSubjectId",
    "DismissNewSource",
    "FailureCode",
    "HistoricalCandidateRecord",
    "InitializeWorkflow",
    "InitializeWorkflowRequest",
    "LectureId",
    "LectureMapRecord",
    "LectureProgress",
    "MapPosition",
    "MapReopenContext",
    "MarkBlocked",
    "OperationId",
    "OperationReceipt",
    "PendingSourceRecord",
    "PolicyKind",
    "PolicyReference",
    "PriorityBasisRecord",
    "PriorityMode",
    "ProducerVersion",
    "ProgressStatus",
    "RecordCandidateValidation",
    "RecordLectureMap",
    "RecordNewSource",
    "RecordOperationFailure",
    "RecordSourceAssessment",
    "RejectLectureMap",
    "RejectPriorityBasis",
    "RetryFailed",
    "Revision",
    "SOURCE_EVIDENCE_REFERENCE_VERSION",
    "SafeId",
    "Sha256Hex",
    "SourceAssessmentRecord",
    "SourceEvidenceReference",
    "SourceId",
    "SubmitLectureCandidate",
    "ValidationRecord",
    "WORKFLOW_ACTIONS",
    "WORKFLOW_ARTIFACT_REFERENCE_VERSION",
    "WORKFLOW_HANDOFF_VERSION",
    "WORKFLOW_STATE_VERSION",
    "WORKFLOW_TRANSITION_VERSION",
    "WorkflowAction",
    "WorkflowActionPayload",
    "WorkflowAdvanced",
    "WorkflowArtifactReference",
    "WorkflowBlocked",
    "WorkflowBlocker",
    "WorkflowDiagnostic",
    "WorkflowDiagnosticCode",
    "WorkflowDisposition",
    "WorkflowFailed",
    "WorkflowFailure",
    "WorkflowHandoff",
    "WorkflowId",
    "WorkflowIdempotentRepeat",
    "WorkflowPolicySet",
    "WorkflowRejected",
    "WorkflowResultStatus",
    "WorkflowStage",
    "WorkflowState",
    "WorkflowTransitionRequest",
    "WorkflowTransitionResult",
    "apply_workflow_request",
    "candidate_subject_sha256",
    "canonical_encode",
    "derive_workflow_handoff",
    "map_subject_sha256",
    "ordered_accepted_documents",
    "priority_subject_sha256",
    "replay_workflow",
    "request_fingerprint",
    "resume_workflow",
    "source_set_subject_sha256",
    "validate_workflow_state",
]
