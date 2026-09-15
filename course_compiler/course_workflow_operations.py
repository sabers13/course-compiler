"""Exact reopening and ephemeral PDF build application operations."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Literal, Mapping, TypeAlias

from .asset import AssetReference
from .assembly import AssemblyFailure, CourseTexAssemblySuccess, assemble_course_tex
from .compilation import (
    CompilationFailure,
    CompiledPdf,
    CompilerInputBundle,
    compile_pdf,
    compile_pdf_bundle,
)
from .course_workflow import CourseWorkflowAssociation
from .course_workflow_persistence import (
    CourseWorkflowPersistenceFailure,
    LocalCourseWorkflowAssociationStore,
)
from .contracts import LectureDocument, SourceProvenance, has_validation_errors, validate_document
from .lecture_document_persistence import (
    LectureDocumentPersistenceFailure,
    LocalLectureDocumentStore,
)
from .legacy_renderer import LegacyMarkdownTexRenderer
from .policy_persistence import (
    LocalPolicyContentStore,
    PolicyContentPayload,
    PolicyPersistenceFailure,
)
from .source_persistence import (
    LocalSourceEvidenceStore,
    SourceEvidencePayload,
    SourcePersistenceFailure,
)
from .visual_composition import VisualCompositionFailure, compose_course_compiler_input
from .visual_placement import VisualPlacement
from .workflow import (
    PolicyReference,
    SourceEvidenceReference,
    WorkflowArtifactReference,
    WorkflowBlocker,
    WorkflowFailure,
    WorkflowHandoff,
    WorkflowState,
    candidate_subject_sha256,
    derive_workflow_handoff,
    map_subject_sha256,
    ordered_accepted_documents,
    priority_subject_sha256,
    validate_workflow_state,
)
from .workflow_artifact_persistence import (
    LocalWorkflowArtifactStore,
    WorkflowArtifactPayload,
    WorkflowArtifactPersistenceFailure,
)
from .rendering import (
    DocumentReference,
    RejectedLecture,
    RenderedLecture,
    RendererFailure,
    document_reference,
)
from .workflow_persistence import (
    LocalWorkflowStateStore,
    WorkflowPersistenceFailure,
)


__all__ = [
    "AcceptedLectureDocumentReopenDiagnostic",
    "AcceptedLectureDocumentReopenFailure",
    "AcceptedLectureDocumentReopenResult",
    "CoursePdfBuildDiagnostic",
    "CoursePdfBuildFailure",
    "CoursePdfBuildResult",
    "CourseWorkflowReopenDiagnostic",
    "CourseWorkflowReopenFailure",
    "CourseWorkflowReopenResult",
    "CourseWorkflowContinuation",
    "CourseWorkflowContinuationDiagnostic",
    "CourseWorkflowContinuationFailure",
    "CourseWorkflowContinuationResult",
    "ContinuationLectureProgress",
    "ActiveLectureCandidate",
    "BlockedWorkflowContext",
    "FailedWorkflowContext",
    "MapReopenContinuationContext",
    "ReopenedLectureDocument",
    "ReopenedWorkflowArtifact",
    "ReopenedCourseWorkflowContext",
    "WorkflowSourceEvidenceReopenDiagnostic",
    "WorkflowSourceEvidenceReopenFailure",
    "WorkflowSourceEvidenceReopenResult",
    "build_reopened_course_pdf",
    "build_reopened_course_pdf_with_visuals",
    "reopen_accepted_lecture_documents",
    "reopen_course_workflow_context",
    "reopen_course_workflow_continuation",
    "reopen_workflow_source_evidence",
]


_POLICY_SLOT_NAMES = (
    "source_assessment",
    "priority_basis",
    "lecture_mapping",
    "lecture_production",
    "lecture_validation",
    "workflow_handoff",
)

_DIAGNOSTICS = {
    "invalid_reopen_input": (
        "input",
        "The course-workflow reopening input is invalid.",
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
    "inconsistent_reopened_context": (
        "consistency",
        "The reopened course-workflow context is inconsistent.",
    ),
    "reopen_exception": (
        "application",
        "The course-workflow reopening operation failed.",
    ),
}

_CONTINUATION_DIAGNOSTICS = {
    "invalid_continuation_reopen_input": (
        "input",
        "The workflow-continuation reopening input is invalid.",
    ),
    "workflow_artifact_not_found": (
        "storage",
        "A required workflow artifact was not found.",
    ),
    "workflow_artifact_store_failed": (
        "storage",
        "The workflow-artifact store failed.",
    ),
    "workflow_artifact_invalid_utf8": (
        "consistency",
        "A required workflow artifact is not valid UTF-8.",
    ),
    "inconsistent_reopened_workflow_artifact": (
        "consistency",
        "A reopened workflow artifact is inconsistent.",
    ),
    "lecture_document_not_found": (
        "storage",
        "A required lecture document was not found.",
    ),
    "lecture_document_store_failed": (
        "storage",
        "The lecture-document store failed.",
    ),
    "inconsistent_continuation_context": (
        "consistency",
        "The reopened workflow-continuation context is inconsistent.",
    ),
    "continuation_reopen_exception": (
        "application",
        "The workflow-continuation reopening operation failed.",
    ),
}

_ACCEPTED_DOCUMENT_DIAGNOSTICS = {
    "invalid_accepted_document_reopen_input": (
        "input",
        "The accepted lecture-document reopening input is invalid.",
    ),
    "lecture_document_not_found": (
        "storage",
        "A required accepted lecture document was not found.",
    ),
    "lecture_document_store_failed": (
        "storage",
        "The lecture-document store failed.",
    ),
    "inconsistent_reopened_documents": (
        "consistency",
        "The reopened accepted lecture documents are inconsistent.",
    ),
    "accepted_document_reopen_exception": (
        "application",
        "The accepted lecture-document reopening operation failed.",
    ),
}

_WORKFLOW_SOURCE_EVIDENCE_REOPEN_DIAGNOSTICS = {
    "invalid_workflow_source_evidence_reopen_input": (
        "input",
        "The workflow source-evidence reopening input is invalid.",
    ),
    "source_evidence_not_found": (
        "storage",
        "The required source evidence was not found.",
    ),
    "source_evidence_store_failed": (
        "storage",
        "The source-evidence store failed.",
    ),
    "inconsistent_reopened_source_evidence": (
        "consistency",
        "The reopened source evidence is inconsistent.",
    ),
    "workflow_source_evidence_reopen_exception": (
        "application",
        "The workflow source-evidence reopening operation failed.",
    ),
}

_COURSE_PDF_BUILD_DIAGNOSTICS = {
    "invalid_course_pdf_build_input": (
        "input",
        "The course PDF build input is invalid.",
    ),
    "workflow_not_completed": (
        "workflow",
        "The workflow is not completed and cannot be built.",
    ),
    "lecture_document_not_found": (
        "storage",
        "A required accepted lecture document was not found.",
    ),
    "lecture_document_store_failed": (
        "storage",
        "The lecture-document store failed.",
    ),
    "inconsistent_reopened_documents": (
        "consistency",
        "The reopened accepted lecture documents are inconsistent.",
    ),
    "accepted_document_reopen_exception": (
        "application",
        "The accepted lecture-document reopening operation failed.",
    ),
    "accepted_document_render_rejected": (
        "render",
        "An accepted lecture document was rejected by the renderer.",
    ),
    "renderer_exception": (
        "render",
        "The renderer failed while converting an accepted lecture document.",
    ),
    "structural_postcondition_mismatch": (
        "render",
        "Rendered structure does not match the accepted lecture document.",
    ),
    "course_tex_assembly_failed": (
        "assembly",
        "The combined course TeX assembly failed.",
    ),
    "invalid_compilation_input": (
        "input",
        "The logical TeX input is invalid.",
    ),
    "invalid_build_workspace": (
        "workspace",
        "The disposable compilation workspace is unavailable.",
    ),
    "compiler_unavailable": (
        "compiler",
        "The fixed local TeX compiler is unavailable.",
    ),
    "compiler_timeout": (
        "compiler",
        "The fixed local TeX compiler timed out.",
    ),
    "compiler_missing_glyph": ("compiler", "A required glyph is missing from the selected font."),
    "compiler_fatal_diagnostic": ("compiler", "TeX reported a fatal formatting or font error."),
    "compiler_overflow": ("compiler", "Content exceeds layout bounds; split long formulas or blocks."),
    "compiler_nonzero_exit": (
        "compiler",
        "The fixed local TeX compiler reported a failure.",
    ),
    "expected_pdf_missing": (
        "output",
        "The expected compiled PDF was not produced.",
    ),
    "expected_pdf_unreadable": (
        "output",
        "The expected compiled PDF could not be read.",
    ),
    "compilation_exception": (
        "adapter",
        "The local compilation adapter failed.",
    ),
    "course_pdf_build_exception": (
        "application",
        "The course PDF build operation failed.",
    ),
}

_BUILD_FAILURE_CODES = frozenset(
    {
        "lecture_document_not_found",
        "lecture_document_store_failed",
        "inconsistent_reopened_documents",
        "accepted_document_reopen_exception",
    }
)

_COMPILATION_BUILD_FAILURE_CODES = frozenset(
    {
        "invalid_compilation_input",
        "invalid_build_workspace",
        "compiler_unavailable",
        "compiler_timeout",
        "compiler_nonzero_exit",
        "compiler_missing_glyph",
        "compiler_fatal_diagnostic",
        "compiler_overflow",
        "expected_pdf_missing",
        "expected_pdf_unreadable",
        "compilation_exception",
    }
)


@dataclass(frozen=True, slots=True)
class AcceptedLectureDocumentReopenDiagnostic:
    """One fixed, content-safe accepted-document reopening diagnostic."""

    code: str
    classification: Literal["input", "storage", "consistency", "application"]
    message: str

    def __post_init__(self) -> None:
        if self.code not in _ACCEPTED_DOCUMENT_DIAGNOSTICS:
            raise ValueError("accepted lecture-document reopen diagnostic is not registered")
        classification, message = _ACCEPTED_DOCUMENT_DIAGNOSTICS[self.code]
        if self.classification != classification or self.message != message:
            raise ValueError("accepted lecture-document reopen diagnostic fields are not registered")


@dataclass(frozen=True, slots=True)
class AcceptedLectureDocumentReopenFailure:
    """A fail-closed accepted-document reopening failure."""

    status: Literal["accepted_document_reopen_failed"]
    diagnostics: tuple[AcceptedLectureDocumentReopenDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "accepted_document_reopen_failed":
            raise ValueError("accepted lecture-document reopen failure status is invalid")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) != 1
            or not _is_valid_accepted_document_diagnostic(self.diagnostics[0])
        ):
            raise ValueError("accepted lecture-document reopen diagnostics are invalid")


AcceptedLectureDocumentReopenResult: TypeAlias = (
    tuple[LectureDocument, ...] | AcceptedLectureDocumentReopenFailure
)


@dataclass(frozen=True, slots=True)
class WorkflowSourceEvidenceReopenDiagnostic:
    """One fixed, content-safe workflow source-evidence reopening diagnostic."""

    code: str
    classification: Literal["input", "storage", "consistency", "application"]
    message: str

    def __post_init__(self) -> None:
        if self.code not in _WORKFLOW_SOURCE_EVIDENCE_REOPEN_DIAGNOSTICS:
            raise ValueError("workflow source-evidence reopen diagnostic is not registered")
        classification, message = _WORKFLOW_SOURCE_EVIDENCE_REOPEN_DIAGNOSTICS[
            self.code
        ]
        if self.classification != classification or self.message != message:
            raise ValueError("workflow source-evidence reopen diagnostic fields are not registered")


@dataclass(frozen=True, slots=True)
class WorkflowSourceEvidenceReopenFailure:
    """A fail-closed workflow source-evidence reopening failure."""

    status: Literal["workflow_source_evidence_reopen_failed"]
    diagnostics: tuple[WorkflowSourceEvidenceReopenDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "workflow_source_evidence_reopen_failed":
            raise ValueError("workflow source-evidence reopen failure status is invalid")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) != 1
            or not _is_valid_workflow_source_evidence_reopen_diagnostic(
                self.diagnostics[0]
            )
        ):
            raise ValueError("workflow source-evidence reopen diagnostics are invalid")


WorkflowSourceEvidenceReopenResult: TypeAlias = (
    tuple[SourceEvidencePayload, ...] | WorkflowSourceEvidenceReopenFailure
)


@dataclass(frozen=True, slots=True)
class CoursePdfBuildDiagnostic:
    """One fixed, content-safe ephemeral course-PDF build diagnostic."""

    code: str
    classification: Literal[
        "input",
        "workflow",
        "storage",
        "consistency",
        "render",
        "assembly",
        "workspace",
        "compiler",
        "output",
        "adapter",
        "application",
    ]
    message: str

    def __post_init__(self) -> None:
        if self.code not in _COURSE_PDF_BUILD_DIAGNOSTICS:
            raise ValueError("course PDF build diagnostic is not registered")
        classification, message = _COURSE_PDF_BUILD_DIAGNOSTICS[self.code]
        if self.classification != classification or self.message != message:
            raise ValueError("course PDF build diagnostic fields are not registered")


@dataclass(frozen=True, slots=True)
class CoursePdfBuildFailure:
    """A fail-closed ephemeral course-PDF build failure."""

    status: Literal["course_pdf_build_failed"]
    diagnostics: tuple[CoursePdfBuildDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "course_pdf_build_failed":
            raise ValueError("course PDF build failure status is invalid")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) != 1
            or not _is_valid_course_pdf_build_diagnostic(self.diagnostics[0])
        ):
            raise ValueError("course PDF build diagnostics are invalid")


CoursePdfBuildResult: TypeAlias = CompiledPdf | CoursePdfBuildFailure


@dataclass(frozen=True, slots=True)
class CourseWorkflowReopenDiagnostic:
    """One fixed, content-safe application-operation diagnostic."""

    code: str
    classification: Literal["input", "storage", "consistency", "application"]
    message: str

    def __post_init__(self) -> None:
        if self.code not in _DIAGNOSTICS:
            raise ValueError("course-workflow reopen diagnostic code is not registered")
        classification, message = _DIAGNOSTICS[self.code]
        if self.classification != classification or self.message != message:
            raise ValueError("course-workflow reopen diagnostic fields are not registered")


@dataclass(frozen=True, slots=True)
class CourseWorkflowReopenFailure:
    """A fail-closed reopening failure with exactly one fixed diagnostic."""

    status: Literal["reopen_failed"]
    diagnostics: tuple[CourseWorkflowReopenDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "reopen_failed":
            raise ValueError("course-workflow reopen failure status is invalid")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) != 1
            or not _is_valid_diagnostic(self.diagnostics[0])
        ):
            raise ValueError("course-workflow reopen failure diagnostics are invalid")


@dataclass(frozen=True, slots=True)
class ReopenedCourseWorkflowContext:
    """One coherent association, workflow snapshot, and six policy payloads."""

    association: CourseWorkflowAssociation
    workflow_state: WorkflowState
    policy_contents: tuple[PolicyContentPayload, ...]

    def __post_init__(self) -> None:
        try:
            association = _reconstruct_association(self.association)
            workflow_state = _reconstruct_workflow_state(self.workflow_state)
            if type(self.policy_contents) is not tuple or len(self.policy_contents) != 6:
                raise ValueError("reopened course-workflow policy contents are invalid")
            policy_contents = tuple(
                _reconstruct_policy_payload(value) for value in self.policy_contents
            )
            if association != self.association or workflow_state != self.workflow_state:
                raise ValueError("reopened course-workflow context is invalid")
            if workflow_state.workflow_id != association.workflow_id:
                raise ValueError("reopened course-workflow context is invalid")
            for index, (slot_name, payload) in enumerate(
                zip(_POLICY_SLOT_NAMES, policy_contents)
            ):
                if payload != self.policy_contents[index]:
                    raise ValueError("reopened course-workflow policy contents are invalid")
                if payload.reference != getattr(workflow_state.policies, slot_name):
                    raise ValueError("reopened course-workflow policy contents are invalid")
        except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
            raise ValueError("reopened course-workflow context is invalid") from None


CourseWorkflowReopenResult: TypeAlias = (
    ReopenedCourseWorkflowContext | CourseWorkflowReopenFailure
)


@dataclass(frozen=True, slots=True)
class ReopenedWorkflowArtifact:
    """One exact workflow artifact decoded strictly as UTF-8."""

    reference: WorkflowArtifactReference
    content: str = field(repr=False)

    def __post_init__(self) -> None:
        try:
            reference = _reconstruct_workflow_artifact_reference(self.reference)
            if type(self.content) is not str:
                raise ValueError("workflow artifact content is invalid")
            payload = WorkflowArtifactPayload(reference, self.content.encode("utf-8"))
            if payload.reference != self.reference:
                raise ValueError("workflow artifact reference is invalid")
        except (AttributeError, KeyError, RecursionError, TypeError, UnicodeError, ValueError):
            raise ValueError("reopened workflow artifact is invalid") from None


@dataclass(frozen=True, slots=True)
class ReopenedLectureDocument:
    """One exact lecture document projected without its internal domain record."""

    document_reference: DocumentReference
    source_text: str = field(repr=False)

    def __post_init__(self) -> None:
        try:
            reference = _reconstruct_document_reference(self.document_reference)
            if type(self.source_text) is not str:
                raise ValueError("lecture document source text is invalid")
            document = LectureDocument(
                reference.contract_version,
                reference.document_id,
                reference.order,
                self.source_text,
                SourceProvenance(reference.content_sha256),
            )
            if has_validation_errors(validate_document(document)):
                raise ValueError("lecture document source text is invalid")
            if document_reference(document) != reference:
                raise ValueError("lecture document reference is inconsistent")
        except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
            raise ValueError("reopened lecture document is invalid") from None


@dataclass(frozen=True, slots=True)
class ContinuationLectureProgress:
    """Compact public progress for one current mapped lecture."""

    lecture_id: str
    order: int
    status: Literal["pending", "candidate", "retry_required", "correction_required", "accepted"]
    attempt: int
    candidate_document_reference: DocumentReference | None
    candidate_subject_sha256: str | None
    validation_disposition: Literal["passed", "passed_with_warnings", "rejected"] | None
    validation_reference: WorkflowArtifactReference | None
    validation_evidence: ReopenedWorkflowArtifact | None
    accepted_document_reference: DocumentReference | None

    def __post_init__(self) -> None:
        try:
            if type(self.lecture_id) is not str or type(self.order) is not int:
                raise ValueError("lecture progress identity is invalid")
            if type(self.attempt) is not int or self.attempt < 0:
                raise ValueError("lecture progress attempt is invalid")
            if self.status not in ("pending", "candidate", "retry_required", "correction_required", "accepted"):
                raise ValueError("lecture progress status is invalid")
            candidate = (
                None
                if self.candidate_document_reference is None
                else _reconstruct_document_reference(self.candidate_document_reference)
            )
            accepted = (
                None
                if self.accepted_document_reference is None
                else _reconstruct_document_reference(self.accepted_document_reference)
            )
            validation = (
                None
                if self.validation_reference is None
                else _reconstruct_workflow_artifact_reference(self.validation_reference)
            )
            if candidate is None:
                if self.candidate_subject_sha256 is not None:
                    raise ValueError("lecture progress candidate subject is invalid")
            elif self.candidate_subject_sha256 != candidate_subject_sha256(candidate):
                raise ValueError("lecture progress candidate subject is invalid")
            if self.validation_disposition not in (
                None,
                "passed",
                "passed_with_warnings",
                "rejected",
            ):
                raise ValueError("lecture progress validation is invalid")
            if (validation is None) != (self.validation_disposition is None):
                raise ValueError("lecture progress validation is invalid")
            if self.validation_evidence is not None:
                _reconstruct_reopened_workflow_artifact(self.validation_evidence)
                if self.validation_evidence.reference != validation:
                    raise ValueError("lecture progress validation evidence is invalid")
            if self.status == "retry_required":
                if (
                    candidate is None
                    or self.validation_disposition != "rejected"
                    or self.validation_evidence is None
                    or accepted is not None
                ):
                    raise ValueError("retry-required lecture progress is invalid")
            elif self.validation_evidence is not None:
                raise ValueError("non-retry lecture has validation evidence")
            if self.status == "pending" and any(
                value is not None
                for value in (candidate, validation, accepted, self.candidate_subject_sha256)
            ):
                raise ValueError("pending lecture progress is invalid")
            if self.status == "candidate" and (
                candidate is None or validation is not None or accepted is not None
            ):
                raise ValueError("candidate lecture progress is invalid")
            if self.status == "accepted" and (
                candidate is None
                or accepted != candidate
                or self.validation_disposition not in ("passed", "passed_with_warnings")
            ):
                raise ValueError("accepted lecture progress is invalid")
            if self.status == "correction_required" and (
                candidate is None
                or accepted != candidate
                or self.validation_disposition not in ("passed", "passed_with_warnings")
            ):
                raise ValueError("correction-required lecture progress is invalid")
        except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
            raise ValueError("continuation lecture progress is invalid") from None


@dataclass(frozen=True, slots=True)
class ActiveLectureCandidate:
    """Exact current candidate required for semantic lecture validation."""

    document_reference: DocumentReference
    source_text: str = field(repr=False)
    candidate_subject_sha256: str

    def __post_init__(self) -> None:
        try:
            document = ReopenedLectureDocument(self.document_reference, self.source_text)
            if self.candidate_subject_sha256 != candidate_subject_sha256(
                document.document_reference
            ):
                raise ValueError("active candidate subject is invalid")
        except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
            raise ValueError("active lecture candidate is invalid") from None


@dataclass(frozen=True, slots=True)
class FailedWorkflowContext:
    """Exact current recovery evidence for one failed workflow."""

    failed_action: str
    failure_code: str
    subject_id: str | None
    evidence: ReopenedWorkflowArtifact
    failure_evidence_sha256: str

    def __post_init__(self) -> None:
        try:
            artifact = _reconstruct_reopened_workflow_artifact(self.evidence)
            if (
                type(self.failed_action) is not str
                or type(self.failure_code) is not str
                or (self.subject_id is not None and type(self.subject_id) is not str)
                or self.failure_evidence_sha256 != artifact.reference.content_sha256
            ):
                raise ValueError("failed workflow context is invalid")
        except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
            raise ValueError("failed workflow context is invalid") from None


@dataclass(frozen=True, slots=True)
class BlockedWorkflowContext:
    """Exact current evidence and subject required to clear one blocker."""

    blocker_code: str
    subject_id: str | None
    subject_sha256: str
    evidence: ReopenedWorkflowArtifact

    def __post_init__(self) -> None:
        try:
            _reconstruct_reopened_workflow_artifact(self.evidence)
            if (
                type(self.blocker_code) is not str
                or (self.subject_id is not None and type(self.subject_id) is not str)
                or not _valid_sha256(self.subject_sha256)
            ):
                raise ValueError("blocked workflow context is invalid")
        except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
            raise ValueError("blocked workflow context is invalid") from None


@dataclass(frozen=True, slots=True)
class MapReopenContinuationContext:
    """Public exact baseline required while revising an approved lecture map."""

    baseline_lecture_map: ReopenedWorkflowArtifact
    baseline_lecture_ids: tuple[str, ...]
    baseline_reserved_lecture_ids: tuple[str, ...]
    baseline_map_subject_sha256: str

    def __post_init__(self) -> None:
        try:
            artifact = _reconstruct_reopened_workflow_artifact(
                self.baseline_lecture_map
            )
            if artifact.reference.artifact_kind != "lecture_map":
                raise ValueError("map-reopen baseline artifact is invalid")
            if (
                type(self.baseline_lecture_ids) is not tuple
                or not self.baseline_lecture_ids
                or any(type(value) is not str for value in self.baseline_lecture_ids)
                or type(self.baseline_reserved_lecture_ids) is not tuple
                or any(
                    type(value) is not str
                    for value in self.baseline_reserved_lecture_ids
                )
                or not _valid_sha256(self.baseline_map_subject_sha256)
            ):
                raise ValueError("map-reopen baseline context is invalid")
        except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
            raise ValueError("map-reopen continuation context is invalid") from None


@dataclass(frozen=True, slots=True)
class CourseWorkflowContinuation:
    """Stage-relevant exact evidence needed to continue one persisted workflow."""

    handoff: WorkflowHandoff
    source_references: tuple[SourceEvidenceReference, ...]
    source_assessment: ReopenedWorkflowArtifact | None
    priority_proposal: ReopenedWorkflowArtifact | None
    evidence_hierarchy: ReopenedWorkflowArtifact | None
    lecture_map: ReopenedWorkflowArtifact | None
    priority_subject_sha256: str | None
    map_subject_sha256: str | None
    lecture_progress: tuple[ContinuationLectureProgress, ...]
    active_lecture_id: str | None
    active_candidate: ActiveLectureCandidate | None
    accepted_documents: tuple[ReopenedLectureDocument, ...]
    failed_workflow: FailedWorkflowContext | None
    blocked_workflow: BlockedWorkflowContext | None
    pending_source: SourceEvidenceReference | None
    map_reopen: MapReopenContinuationContext | None
    # The GPT relay may attach the accepted priority review from the durable
    # semantic-result record.  It is optional here because the T030
    # continuation is also used by predecessor read-only clients that do not
    # open the semantic-work store.
    priority_evidence_review: ReopenedWorkflowArtifact | None = None

    def __post_init__(self) -> None:
        try:
            _reconstruct_workflow_handoff(self.handoff)
            if type(self.source_references) is not tuple:
                raise ValueError("continuation source references are invalid")
            for reference in self.source_references:
                _reconstruct_source_evidence_reference(reference)
            for value in (
                self.source_assessment,
                self.priority_proposal,
                self.evidence_hierarchy,
                self.lecture_map,
                self.priority_evidence_review,
            ):
                if value is not None:
                    _reconstruct_reopened_workflow_artifact(value)
            expected_artifact_kinds = (
                (self.source_assessment, "source_assessment"),
                (self.priority_proposal, "priority_proposal"),
                (self.evidence_hierarchy, "evidence_hierarchy"),
                (self.lecture_map, "lecture_map"),
                # `exam_priority_assessment` persists its reviewed Markdown
                # under the existing priority-proposal artifact kind.  Keep
                # the optional relay projection just as strongly typed as
                # every workflow-owned artifact above.
                (self.priority_evidence_review, "priority_proposal"),
            )
            if any(
                value is not None and value.reference.artifact_kind != expected_kind
                for value, expected_kind in expected_artifact_kinds
            ):
                raise ValueError("continuation artifact kind is invalid")
            if type(self.lecture_progress) is not tuple:
                raise ValueError("continuation lecture progress is invalid")
            for value in self.lecture_progress:
                _reconstruct_continuation_lecture_progress(value)
            if type(self.accepted_documents) is not tuple:
                raise ValueError("continuation accepted documents are invalid")
            for value in self.accepted_documents:
                _reconstruct_reopened_lecture_document(value)
            if self.active_candidate is not None:
                _reconstruct_active_lecture_candidate(self.active_candidate)
            if self.failed_workflow is not None:
                _reconstruct_failed_workflow_context(self.failed_workflow)
            if self.blocked_workflow is not None:
                _reconstruct_blocked_workflow_context(self.blocked_workflow)
            if self.pending_source is not None:
                _reconstruct_source_evidence_reference(self.pending_source)
            if self.map_reopen is not None:
                _reconstruct_map_reopen_context(self.map_reopen)
            if (
                self.priority_subject_sha256 is not None
                and not _valid_sha256(self.priority_subject_sha256)
            ) or (
                self.map_subject_sha256 is not None
                and not _valid_sha256(self.map_subject_sha256)
            ):
                raise ValueError("continuation subject is invalid")
            if self.active_lecture_id is not None and type(self.active_lecture_id) is not str:
                raise ValueError("continuation active lecture is invalid")
            if self.handoff.active_lecture_id != self.active_lecture_id:
                raise ValueError("continuation active lecture is inconsistent")
            if (self.handoff.disposition == "failed") != (self.failed_workflow is not None):
                raise ValueError("continuation failure disposition is inconsistent")
            if (self.handoff.disposition == "blocked") != (self.blocked_workflow is not None):
                raise ValueError("continuation blocker disposition is inconsistent")
        except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
            raise ValueError("course workflow continuation is invalid") from None


@dataclass(frozen=True, slots=True)
class CourseWorkflowContinuationDiagnostic:
    """One fixed, content-safe continuation-read diagnostic."""

    code: str
    classification: Literal["input", "storage", "consistency", "application"]
    message: str

    def __post_init__(self) -> None:
        if self.code not in _CONTINUATION_DIAGNOSTICS:
            raise ValueError("workflow continuation diagnostic is not registered")
        classification, message = _CONTINUATION_DIAGNOSTICS[self.code]
        if self.classification != classification or self.message != message:
            raise ValueError("workflow continuation diagnostic fields are not registered")


@dataclass(frozen=True, slots=True)
class CourseWorkflowContinuationFailure:
    """A fail-closed continuation read with exactly one fixed diagnostic."""

    status: Literal["continuation_reopen_failed"]
    diagnostics: tuple[CourseWorkflowContinuationDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "continuation_reopen_failed":
            raise ValueError("workflow continuation failure status is invalid")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) != 1
            or not _is_valid_continuation_diagnostic(self.diagnostics[0])
        ):
            raise ValueError("workflow continuation diagnostics are invalid")


CourseWorkflowContinuationResult: TypeAlias = (
    CourseWorkflowContinuation | CourseWorkflowContinuationFailure
)


class _ContinuationReadFailure(Exception):
    def __init__(self, result: CourseWorkflowContinuationFailure) -> None:
        self.result = result


def reopen_course_workflow_context(
    association: CourseWorkflowAssociation,
    *,
    association_store: LocalCourseWorkflowAssociationStore,
    workflow_store: LocalWorkflowStateStore,
    policy_store: LocalPolicyContentStore,
) -> CourseWorkflowReopenResult:
    """Reopen one exact known association and its immutable referenced context."""

    if type(association) is not CourseWorkflowAssociation:
        raise TypeError("association must be exactly CourseWorkflowAssociation")
    if type(association_store) is not LocalCourseWorkflowAssociationStore:
        raise TypeError(
            "association_store must be exactly LocalCourseWorkflowAssociationStore"
        )
    if type(workflow_store) is not LocalWorkflowStateStore:
        raise TypeError("workflow_store must be exactly LocalWorkflowStateStore")
    if type(policy_store) is not LocalPolicyContentStore:
        raise TypeError("policy_store must be exactly LocalPolicyContentStore")

    try:
        try:
            requested_association = _reconstruct_association(association)
        except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
            return _failure("invalid_reopen_input")

        association_result = association_store.load(requested_association)
        if type(association_result) is CourseWorkflowPersistenceFailure:
            if _failure_code(association_result) == "association_not_found":
                return _failure("association_not_found")
            return _failure("association_store_failed")
        try:
            reopened_association = _reconstruct_association(association_result)
        except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
            return _failure("inconsistent_reopened_context")
        if reopened_association != requested_association:
            return _failure("inconsistent_reopened_context")

        workflow_result = workflow_store.load(reopened_association.workflow_id)
        if type(workflow_result) is WorkflowPersistenceFailure:
            if _failure_code(workflow_result) == "workflow_not_found":
                return _failure("workflow_not_found")
            return _failure("workflow_store_failed")
        try:
            workflow_state = _reconstruct_workflow_state(workflow_result)
        except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
            return _failure("inconsistent_reopened_context")
        if workflow_state.workflow_id != reopened_association.workflow_id:
            return _failure("inconsistent_reopened_context")

        policy_contents: list[PolicyContentPayload] = []
        for slot_name in _POLICY_SLOT_NAMES:
            reference = getattr(workflow_state.policies, slot_name)
            policy_result = policy_store.load(reference)
            if type(policy_result) is PolicyPersistenceFailure:
                if _failure_code(policy_result) == "policy_not_found":
                    return _failure("policy_not_found")
                return _failure("policy_store_failed")
            try:
                policy_payload = _reconstruct_policy_payload(policy_result)
            except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
                return _failure("inconsistent_reopened_context")
            if policy_payload.reference != reference:
                return _failure("inconsistent_reopened_context")
            policy_contents.append(policy_payload)

        try:
            return ReopenedCourseWorkflowContext(
                association=reopened_association,
                workflow_state=workflow_state,
                policy_contents=tuple(policy_contents),
            )
        except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
            return _failure("inconsistent_reopened_context")
    except Exception:
        return _failure("reopen_exception")


def reopen_course_workflow_continuation(
    context: ReopenedCourseWorkflowContext,
    *,
    artifact_store: LocalWorkflowArtifactStore,
    document_store: LocalLectureDocumentStore,
) -> CourseWorkflowContinuationResult:
    """Reopen only exact current evidence required to continue the workflow."""

    if type(artifact_store) is not LocalWorkflowArtifactStore:
        raise TypeError("artifact_store must be exactly LocalWorkflowArtifactStore")
    if type(document_store) is not LocalLectureDocumentStore:
        raise TypeError("document_store must be exactly LocalLectureDocumentStore")

    try:
        try:
            reopened_context = _reconstruct_reopened_context(context)
        except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
            return _continuation_failure("invalid_continuation_reopen_input")

        state = reopened_context.workflow_state
        artifacts: dict[WorkflowArtifactReference, ReopenedWorkflowArtifact] = {}

        def reopen_artifact(
            reference: WorkflowArtifactReference,
        ) -> ReopenedWorkflowArtifact | CourseWorkflowContinuationFailure:
            existing = artifacts.get(reference)
            if existing is not None:
                return existing
            result = artifact_store.load(reference)
            if type(result) is WorkflowArtifactPersistenceFailure:
                code = _failure_code(result)
                if code == "artifact_not_found":
                    return _continuation_failure("workflow_artifact_not_found")
                if code in {
                    "invalid_persistence_input",
                    "artifact_reference_mismatch",
                    "stored_artifact_invalid",
                    "immutable_identity_conflict",
                }:
                    return _continuation_failure(
                        "inconsistent_reopened_workflow_artifact"
                    )
                return _continuation_failure("workflow_artifact_store_failed")
            try:
                payload = _reconstruct_workflow_artifact_payload(result)
                if payload.reference != reference:
                    raise ValueError("workflow artifact reference is inconsistent")
                try:
                    content = payload.payload.decode("utf-8", errors="strict")
                except UnicodeError:
                    return _continuation_failure("workflow_artifact_invalid_utf8")
                reopened = ReopenedWorkflowArtifact(reference, content)
            except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
                return _continuation_failure(
                    "inconsistent_reopened_workflow_artifact"
                )
            artifacts[reference] = reopened
            return reopened

        def required_artifact(
            reference: WorkflowArtifactReference,
        ) -> ReopenedWorkflowArtifact:
            result = reopen_artifact(reference)
            if type(result) is CourseWorkflowContinuationFailure:
                raise _ContinuationReadFailure(result)
            return result

        source_assessment = None
        priority_proposal = None
        evidence_hierarchy = None
        lecture_map = None
        if state.stage in ("priority_approval", "lecture_mapping"):
            if state.source_assessment is None or state.priority_basis is None:
                return _continuation_failure("inconsistent_continuation_context")
            source_assessment = required_artifact(
                state.source_assessment.assessment_reference
            )
            priority_proposal = required_artifact(
                state.priority_basis.proposal_reference
            )
            evidence_hierarchy = required_artifact(
                state.priority_basis.evidence_hierarchy_reference
            )
        if state.stage in (
            "map_approval",
            "lecture_production",
            "lecture_validation",
            "completed",
        ):
            if state.lecture_map is None:
                return _continuation_failure("inconsistent_continuation_context")
            lecture_map = required_artifact(state.lecture_map.map_reference)

        map_reopen = None
        if state.map_reopen_context is not None:
            baseline = state.map_reopen_context
            map_reopen = MapReopenContinuationContext(
                baseline_lecture_map=required_artifact(
                    baseline.baseline_map_reference
                ),
                baseline_lecture_ids=baseline.baseline_lecture_ids,
                baseline_reserved_lecture_ids=baseline.baseline_reserved_lecture_ids,
                baseline_map_subject_sha256=baseline.baseline_map_subject_sha256,
            )

        progress_projection: list[ContinuationLectureProgress] = []
        for progress in state.lecture_progress:
            validation = progress.validation
            validation_evidence = None
            if progress.status == "retry_required":
                if validation is None:
                    return _continuation_failure(
                        "inconsistent_continuation_context"
                    )
                validation_evidence = required_artifact(
                    validation.validation_reference
                )
            progress_projection.append(
                ContinuationLectureProgress(
                    lecture_id=progress.lecture_id,
                    order=progress.map_position,
                    status=progress.status,
                    attempt=progress.attempt,
                    candidate_document_reference=progress.candidate,
                    candidate_subject_sha256=(
                        None
                        if progress.candidate is None
                        else candidate_subject_sha256(progress.candidate)
                    ),
                    validation_disposition=(
                        None if validation is None else validation.disposition
                    ),
                    validation_reference=(
                        None if validation is None else validation.validation_reference
                    ),
                    validation_evidence=validation_evidence,
                    accepted_document_reference=progress.accepted_document,
                )
            )

        active_candidate = None
        if state.stage == "lecture_validation":
            active = next(
                (
                    progress
                    for progress in state.lecture_progress
                    if progress.lecture_id == state.active_lecture_id
                ),
                None,
            )
            if active is None or active.candidate is None:
                return _continuation_failure("inconsistent_continuation_context")
            document_result = _load_continuation_document(
                active.candidate,
                document_store=document_store,
            )
            if type(document_result) is CourseWorkflowContinuationFailure:
                return document_result
            active_candidate = ActiveLectureCandidate(
                document_reference=document_result.document_reference,
                source_text=document_result.source_text,
                candidate_subject_sha256=candidate_subject_sha256(active.candidate),
            )

        accepted_result = reopen_accepted_lecture_documents(
            reopened_context,
            document_store=document_store,
        )
        if type(accepted_result) is AcceptedLectureDocumentReopenFailure:
            code = _failure_code(accepted_result)
            if code == "lecture_document_not_found":
                return _continuation_failure("lecture_document_not_found")
            if code == "lecture_document_store_failed":
                return _continuation_failure("lecture_document_store_failed")
            if code == "accepted_document_reopen_exception":
                return _continuation_failure("continuation_reopen_exception")
            return _continuation_failure("inconsistent_continuation_context")
        if type(accepted_result) is not tuple:
            return _continuation_failure("inconsistent_continuation_context")
        accepted_documents = tuple(
            ReopenedLectureDocument(
                document_reference(document),
                document.source_text,
            )
            for document in accepted_result
        )

        failed_workflow = None
        blocked_workflow = None
        if type(state.active_issue) is WorkflowFailure:
            failure = state.active_issue
            evidence = required_artifact(failure.evidence_reference)
            failed_workflow = FailedWorkflowContext(
                failed_action=failure.failed_action,
                failure_code=failure.code,
                subject_id=failure.subject_id,
                evidence=evidence,
                failure_evidence_sha256=failure.evidence_reference.content_sha256,
            )
        elif type(state.active_issue) is WorkflowBlocker:
            blocker = state.active_issue
            blocked_workflow = BlockedWorkflowContext(
                blocker_code=blocker.code,
                subject_id=blocker.subject_id,
                subject_sha256=blocker.subject_sha256,
                evidence=required_artifact(blocker.evidence_reference),
            )

        handoff = derive_workflow_handoff(state)
        return CourseWorkflowContinuation(
            handoff=handoff,
            source_references=state.source_evidence,
            source_assessment=source_assessment,
            priority_proposal=priority_proposal,
            evidence_hierarchy=evidence_hierarchy,
            lecture_map=lecture_map,
            priority_subject_sha256=(
                None
                if state.priority_basis is None
                else priority_subject_sha256(
                    state.priority_basis,
                    state.policies.priority_basis,
                )
            ),
            map_subject_sha256=(
                None
                if state.lecture_map is None
                else map_subject_sha256(
                    state.lecture_map,
                    state.policies.lecture_mapping,
                )
            ),
            lecture_progress=tuple(progress_projection),
            active_lecture_id=state.active_lecture_id,
            active_candidate=active_candidate,
            accepted_documents=accepted_documents,
            failed_workflow=failed_workflow,
            blocked_workflow=blocked_workflow,
            pending_source=(
                None
                if state.pending_source is None
                else state.pending_source.source_reference
            ),
            map_reopen=map_reopen,
        )
    except _ContinuationReadFailure as failure:
        return failure.result
    except Exception:
        return _continuation_failure("continuation_reopen_exception")


def reopen_accepted_lecture_documents(
    context: ReopenedCourseWorkflowContext,
    *,
    document_store: LocalLectureDocumentStore,
) -> AcceptedLectureDocumentReopenResult:
    """Reopen all currently accepted documents in canonical workflow order."""

    if type(document_store) is not LocalLectureDocumentStore:
        raise TypeError("document_store must be exactly LocalLectureDocumentStore")
    try:
        try:
            reopened_context = _reconstruct_reopened_context(context)
        except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
            return _accepted_document_failure("invalid_accepted_document_reopen_input")

        references = ordered_accepted_documents(reopened_context.workflow_state)
        documents: list[LectureDocument] = []
        for reference in references:
            document_result = document_store.load(reference)
            if type(document_result) is LectureDocumentPersistenceFailure:
                if _failure_code(document_result) == "document_not_found":
                    return _accepted_document_failure("lecture_document_not_found")
                return _accepted_document_failure("lecture_document_store_failed")
            try:
                document = _reconstruct_document(document_result)
                derived_reference = document_reference(document)
                if type(derived_reference) is not DocumentReference:
                    raise ValueError("reopened lecture document is invalid")
                if derived_reference != reference:
                    raise ValueError("reopened lecture document is inconsistent")
            except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
                return _accepted_document_failure("inconsistent_reopened_documents")
            documents.append(document)
        return tuple(documents)
    except Exception:
        return _accepted_document_failure("accepted_document_reopen_exception")


def reopen_workflow_source_evidence(
    context: ReopenedCourseWorkflowContext,
    *,
    source_store: LocalSourceEvidenceStore,
) -> WorkflowSourceEvidenceReopenResult:
    """Reopen all current workflow source evidence in canonical state order."""

    if type(source_store) is not LocalSourceEvidenceStore:
        raise TypeError("source_store must be exactly LocalSourceEvidenceStore")
    try:
        try:
            reopened_context = _reconstruct_reopened_context(context)
        except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
            return _workflow_source_evidence_reopen_failure(
                "invalid_workflow_source_evidence_reopen_input"
            )

        payloads: list[SourceEvidencePayload] = []
        for reference in reopened_context.workflow_state.source_evidence:
            source_result = source_store.load(reference)
            if type(source_result) is SourcePersistenceFailure:
                if _failure_code(source_result) == "source_not_found":
                    return _workflow_source_evidence_reopen_failure(
                        "source_evidence_not_found"
                    )
                return _workflow_source_evidence_reopen_failure(
                    "source_evidence_store_failed"
                )
            try:
                payload = _reconstruct_source_evidence_payload(source_result)
                if payload.reference != reference:
                    raise ValueError("reopened source evidence is inconsistent")
            except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
                return _workflow_source_evidence_reopen_failure(
                    "inconsistent_reopened_source_evidence"
                )
            payloads.append(payload)
        return tuple(payloads)
    except Exception:
        return _workflow_source_evidence_reopen_failure(
            "workflow_source_evidence_reopen_exception"
        )


def build_reopened_course_pdf(
    context: ReopenedCourseWorkflowContext,
    *,
    document_store: LocalLectureDocumentStore,
) -> CoursePdfBuildResult:
    """Build one completed reopened course as an ephemeral in-memory PDF."""

    if type(document_store) is not LocalLectureDocumentStore:
        raise TypeError("document_store must be exactly LocalLectureDocumentStore")

    try:
        try:
            reopened_context = _reconstruct_reopened_context(context)
        except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
            return _course_pdf_build_failure("invalid_course_pdf_build_input")

        if reopened_context.workflow_state.stage != "completed":
            return _course_pdf_build_failure("workflow_not_completed")

        reopened_documents = reopen_accepted_lecture_documents(
            reopened_context,
            document_store=document_store,
        )
        if type(reopened_documents) is AcceptedLectureDocumentReopenFailure:
            code = _failure_code(reopened_documents)
            if code == "invalid_accepted_document_reopen_input":
                return _course_pdf_build_failure("invalid_course_pdf_build_input")
            if code in _BUILD_FAILURE_CODES:
                return _course_pdf_build_failure(code)
            return _course_pdf_build_failure("accepted_document_reopen_exception")
        if type(reopened_documents) is not tuple or not reopened_documents:
            return _course_pdf_build_failure("inconsistent_reopened_documents")

        renderer = LegacyMarkdownTexRenderer()
        render_document = renderer.render
        rendered_lectures: list[RenderedLecture] = []
        for document in reopened_documents:
            render_result = render_document(document)
            if type(render_result) is RejectedLecture:
                return _course_pdf_build_failure(
                    "accepted_document_render_rejected"
                )
            if type(render_result) is RendererFailure:
                code = _failure_code(render_result)
                if code in {"renderer_exception", "structural_postcondition_mismatch"}:
                    return _course_pdf_build_failure(code)
                return _course_pdf_build_failure("course_pdf_build_exception")
            if type(render_result) is not RenderedLecture:
                return _course_pdf_build_failure("course_pdf_build_exception")
            rendered_lectures.append(render_result)

        assembly_result = assemble_course_tex(
            tuple(result.document for result in rendered_lectures),
            tuple(rendered_lectures),
        )
        if type(assembly_result) is AssemblyFailure:
            return _course_pdf_build_failure("course_tex_assembly_failed")
        if type(assembly_result) is not CourseTexAssemblySuccess:
            return _course_pdf_build_failure("course_pdf_build_exception")

        compilation_result = compile_pdf(assembly_result.combined)
        if type(compilation_result) is CompilationFailure:
            code = _failure_code(compilation_result)
            if code in _COMPILATION_BUILD_FAILURE_CODES:
                return _course_pdf_build_failure(code)
            return _course_pdf_build_failure("course_pdf_build_exception")
        if type(compilation_result) is not CompiledPdf:
            return _course_pdf_build_failure("course_pdf_build_exception")
        return compilation_result
    except Exception:
        return _course_pdf_build_failure("course_pdf_build_exception")


def build_reopened_course_pdf_with_visuals(
    context: ReopenedCourseWorkflowContext,
    *,
    document_store: LocalLectureDocumentStore,
    placements: tuple[VisualPlacement, ...],
    asset_bytes: Mapping[AssetReference, bytes],
) -> CoursePdfBuildResult | VisualCompositionFailure:
    """Build one completed reopened course as an ephemeral placement-aware PDF.

    This is the sibling of `build_reopened_course_pdf` for the visual path:
    it reopens the same exact accepted T016 `LectureDocument` values, then
    delegates all rendering/assembly/placement composition to the unchanged
    T025 `compose_course_compiler_input` and compiles its exact
    `CompilerInputBundle` through the unchanged T024 `compile_pdf_bundle`.
    `build_reopened_course_pdf` itself is untouched and keeps its own
    non-visual T004/T005/T006 sequence.
    """

    if type(document_store) is not LocalLectureDocumentStore:
        raise TypeError("document_store must be exactly LocalLectureDocumentStore")
    if type(placements) is not tuple:
        raise TypeError("placements must be exactly tuple[VisualPlacement, ...]")
    if not isinstance(asset_bytes, Mapping):
        raise TypeError("asset_bytes must be a Mapping[AssetReference, bytes]")

    try:
        try:
            reopened_context = _reconstruct_reopened_context(context)
        except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
            return _course_pdf_build_failure("invalid_course_pdf_build_input")

        if reopened_context.workflow_state.stage != "completed":
            return _course_pdf_build_failure("workflow_not_completed")

        reopened_documents = reopen_accepted_lecture_documents(
            reopened_context,
            document_store=document_store,
        )
        if type(reopened_documents) is AcceptedLectureDocumentReopenFailure:
            code = _failure_code(reopened_documents)
            if code == "invalid_accepted_document_reopen_input":
                return _course_pdf_build_failure("invalid_course_pdf_build_input")
            if code in _BUILD_FAILURE_CODES:
                return _course_pdf_build_failure(code)
            return _course_pdf_build_failure("accepted_document_reopen_exception")
        if type(reopened_documents) is not tuple or not reopened_documents:
            return _course_pdf_build_failure("inconsistent_reopened_documents")

        composition_result = compose_course_compiler_input(
            reopened_documents,
            placements=placements,
            asset_bytes=asset_bytes,
        )
        if type(composition_result) is VisualCompositionFailure:
            return composition_result
        if type(composition_result) is not CompilerInputBundle:
            return _course_pdf_build_failure("course_pdf_build_exception")

        compilation_result = compile_pdf_bundle(composition_result)
        if type(compilation_result) is CompilationFailure:
            code = _failure_code(compilation_result)
            if code in _COMPILATION_BUILD_FAILURE_CODES:
                return _course_pdf_build_failure(code)
            return _course_pdf_build_failure("course_pdf_build_exception")
        if type(compilation_result) is not CompiledPdf:
            return _course_pdf_build_failure("course_pdf_build_exception")
        return compilation_result
    except Exception:
        return _course_pdf_build_failure("course_pdf_build_exception")


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


def _reconstruct_workflow_state(value: object) -> WorkflowState:
    if type(value) is not WorkflowState:
        raise ValueError("workflow state is invalid")
    try:
        reconstructed = WorkflowState(
            **{item.name: getattr(value, item.name) for item in fields(WorkflowState)}
        )
        if validate_workflow_state(reconstructed) != ():
            raise ValueError("workflow state is invalid")
        return reconstructed
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        raise ValueError("workflow state is invalid") from None


def _reconstruct_reopened_context(value: object) -> ReopenedCourseWorkflowContext:
    if type(value) is not ReopenedCourseWorkflowContext:
        raise ValueError("reopened course-workflow context is invalid")
    try:
        return ReopenedCourseWorkflowContext(
            value.association,
            value.workflow_state,
            value.policy_contents,
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        raise ValueError("reopened course-workflow context is invalid") from None


def _reconstruct_document(value: object) -> LectureDocument:
    if type(value) is not LectureDocument:
        raise ValueError("reopened lecture document is invalid")
    try:
        if tuple(item.name for item in fields(LectureDocument)) != (
            "contract_version",
            "document_id",
            "order",
            "source_text",
            "provenance",
        ):
            raise ValueError("reopened lecture document is invalid")
        if (
            type(value.contract_version) is not str
            or type(value.document_id) is not str
            or type(value.order) is not int
            or type(value.source_text) is not str
            or type(value.provenance) is not SourceProvenance
            or type(value.provenance.content_sha256) is not str
        ):
            raise ValueError("reopened lecture document is invalid")
        document = LectureDocument(
            value.contract_version,
            value.document_id,
            value.order,
            value.source_text,
            SourceProvenance(value.provenance.content_sha256),
        )
        if has_validation_errors(validate_document(document)):
            raise ValueError("reopened lecture document is invalid")
        return document
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        raise ValueError("reopened lecture document is invalid") from None


def _reconstruct_source_evidence_payload(value: object) -> SourceEvidencePayload:
    if type(value) is not SourceEvidencePayload:
        raise ValueError("reopened source evidence is invalid")
    try:
        reference = value.reference
        if type(reference) is not SourceEvidenceReference:
            raise ValueError("reopened source evidence is invalid")
        return SourceEvidencePayload(
            SourceEvidenceReference(
                reference.reference_version,
                reference.source_id,
                reference.content_sha256,
            ),
            value.payload,
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        raise ValueError("reopened source evidence is invalid") from None


def _reconstruct_source_evidence_reference(
    value: object,
) -> SourceEvidenceReference:
    if type(value) is not SourceEvidenceReference:
        raise ValueError("source evidence reference is invalid")
    try:
        return SourceEvidenceReference(
            value.reference_version,
            value.source_id,
            value.content_sha256,
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        raise ValueError("source evidence reference is invalid") from None


def _reconstruct_document_reference(value: object) -> DocumentReference:
    if type(value) is not DocumentReference:
        raise ValueError("document reference is invalid")
    try:
        return DocumentReference(
            value.contract_version,
            value.document_id,
            value.order,
            value.content_sha256,
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        raise ValueError("document reference is invalid") from None


def _reconstruct_workflow_artifact_reference(
    value: object,
) -> WorkflowArtifactReference:
    if type(value) is not WorkflowArtifactReference:
        raise ValueError("workflow artifact reference is invalid")
    try:
        return WorkflowArtifactReference(
            value.reference_version,
            value.artifact_id,
            value.artifact_kind,
            value.content_sha256,
            value.producer_version,
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        raise ValueError("workflow artifact reference is invalid") from None


def _reconstruct_workflow_artifact_payload(
    value: object,
) -> WorkflowArtifactPayload:
    if type(value) is not WorkflowArtifactPayload:
        raise ValueError("workflow artifact payload is invalid")
    try:
        return WorkflowArtifactPayload(
            _reconstruct_workflow_artifact_reference(value.reference),
            value.payload,
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        raise ValueError("workflow artifact payload is invalid") from None


def _reconstruct_reopened_workflow_artifact(
    value: object,
) -> ReopenedWorkflowArtifact:
    if type(value) is not ReopenedWorkflowArtifact:
        raise ValueError("reopened workflow artifact is invalid")
    try:
        return ReopenedWorkflowArtifact(value.reference, value.content)
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        raise ValueError("reopened workflow artifact is invalid") from None


def _reconstruct_reopened_lecture_document(
    value: object,
) -> ReopenedLectureDocument:
    if type(value) is not ReopenedLectureDocument:
        raise ValueError("reopened lecture document projection is invalid")
    try:
        return ReopenedLectureDocument(
            value.document_reference,
            value.source_text,
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        raise ValueError("reopened lecture document projection is invalid") from None


def _reconstruct_continuation_lecture_progress(
    value: object,
) -> ContinuationLectureProgress:
    if type(value) is not ContinuationLectureProgress:
        raise ValueError("continuation lecture progress is invalid")
    try:
        return ContinuationLectureProgress(
            **{
                item.name: getattr(value, item.name)
                for item in fields(ContinuationLectureProgress)
            }
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        raise ValueError("continuation lecture progress is invalid") from None


def _reconstruct_active_lecture_candidate(value: object) -> ActiveLectureCandidate:
    if type(value) is not ActiveLectureCandidate:
        raise ValueError("active lecture candidate is invalid")
    try:
        return ActiveLectureCandidate(
            value.document_reference,
            value.source_text,
            value.candidate_subject_sha256,
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        raise ValueError("active lecture candidate is invalid") from None


def _reconstruct_failed_workflow_context(value: object) -> FailedWorkflowContext:
    if type(value) is not FailedWorkflowContext:
        raise ValueError("failed workflow context is invalid")
    try:
        return FailedWorkflowContext(
            **{item.name: getattr(value, item.name) for item in fields(FailedWorkflowContext)}
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        raise ValueError("failed workflow context is invalid") from None


def _reconstruct_blocked_workflow_context(value: object) -> BlockedWorkflowContext:
    if type(value) is not BlockedWorkflowContext:
        raise ValueError("blocked workflow context is invalid")
    try:
        return BlockedWorkflowContext(
            **{item.name: getattr(value, item.name) for item in fields(BlockedWorkflowContext)}
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        raise ValueError("blocked workflow context is invalid") from None


def _reconstruct_map_reopen_context(
    value: object,
) -> MapReopenContinuationContext:
    if type(value) is not MapReopenContinuationContext:
        raise ValueError("map-reopen continuation context is invalid")
    try:
        return MapReopenContinuationContext(
            **{
                item.name: getattr(value, item.name)
                for item in fields(MapReopenContinuationContext)
            }
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        raise ValueError("map-reopen continuation context is invalid") from None


def _valid_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _reconstruct_workflow_handoff(value: object) -> WorkflowHandoff:
    if type(value) is not WorkflowHandoff:
        raise ValueError("workflow handoff is invalid")
    try:
        return WorkflowHandoff(
            **{item.name: getattr(value, item.name) for item in fields(WorkflowHandoff)}
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        raise ValueError("workflow handoff is invalid") from None


def _load_continuation_document(
    reference: DocumentReference,
    *,
    document_store: LocalLectureDocumentStore,
) -> ReopenedLectureDocument | CourseWorkflowContinuationFailure:
    result = document_store.load(reference)
    if type(result) is LectureDocumentPersistenceFailure:
        code = _failure_code(result)
        if code == "document_not_found":
            return _continuation_failure("lecture_document_not_found")
        if code in {
            "invalid_persistence_input",
            "document_reference_mismatch",
            "stored_document_invalid",
            "immutable_identity_conflict",
        }:
            return _continuation_failure("inconsistent_continuation_context")
        return _continuation_failure("lecture_document_store_failed")
    try:
        document = _reconstruct_document(result)
        if document_reference(document) != reference:
            raise ValueError("lecture document reference is inconsistent")
        return ReopenedLectureDocument(reference, document.source_text)
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        return _continuation_failure("inconsistent_continuation_context")


def _reconstruct_policy_reference(value: object) -> PolicyReference:
    if type(value) is not PolicyReference:
        raise ValueError("policy reference is invalid")
    try:
        return PolicyReference(
            value.policy_kind,
            value.policy_version,
            value.content_sha256,
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        raise ValueError("policy reference is invalid") from None


def _reconstruct_policy_payload(value: object) -> PolicyContentPayload:
    if type(value) is not PolicyContentPayload:
        raise ValueError("policy content payload is invalid")
    try:
        reference = _reconstruct_policy_reference(value.reference)
        return PolicyContentPayload(reference, value.payload)
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        raise ValueError("policy content payload is invalid") from None


def _failure_code(value: object) -> str | None:
    try:
        diagnostics = value.diagnostics
        if type(diagnostics) is tuple and len(diagnostics) == 1:
            code = diagnostics[0].code
            return code if type(code) is str else None
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        pass
    return None


def _is_valid_diagnostic(value: object) -> bool:
    if type(value) is not CourseWorkflowReopenDiagnostic:
        return False
    try:
        CourseWorkflowReopenDiagnostic(
            value.code,
            value.classification,
            value.message,
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        return False
    return True


def _is_valid_accepted_document_diagnostic(value: object) -> bool:
    if type(value) is not AcceptedLectureDocumentReopenDiagnostic:
        return False
    try:
        AcceptedLectureDocumentReopenDiagnostic(
            value.code,
            value.classification,
            value.message,
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        return False
    return True


def _is_valid_workflow_source_evidence_reopen_diagnostic(value: object) -> bool:
    if type(value) is not WorkflowSourceEvidenceReopenDiagnostic:
        return False
    try:
        WorkflowSourceEvidenceReopenDiagnostic(
            value.code,
            value.classification,
            value.message,
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        return False
    return True


def _is_valid_course_pdf_build_diagnostic(value: object) -> bool:
    if type(value) is not CoursePdfBuildDiagnostic:
        return False
    try:
        CoursePdfBuildDiagnostic(
            value.code,
            value.classification,
            value.message,
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        return False
    return True


def _is_valid_continuation_diagnostic(value: object) -> bool:
    if type(value) is not CourseWorkflowContinuationDiagnostic:
        return False
    try:
        CourseWorkflowContinuationDiagnostic(
            value.code,
            value.classification,
            value.message,
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        return False
    return True


def _failure(code: str) -> CourseWorkflowReopenFailure:
    classification, message = _DIAGNOSTICS[code]
    return CourseWorkflowReopenFailure(
        status="reopen_failed",
        diagnostics=(CourseWorkflowReopenDiagnostic(code, classification, message),),
    )


def _continuation_failure(code: str) -> CourseWorkflowContinuationFailure:
    classification, message = _CONTINUATION_DIAGNOSTICS[code]
    return CourseWorkflowContinuationFailure(
        status="continuation_reopen_failed",
        diagnostics=(
            CourseWorkflowContinuationDiagnostic(code, classification, message),
        ),
    )


def _accepted_document_failure(code: str) -> AcceptedLectureDocumentReopenFailure:
    classification, message = _ACCEPTED_DOCUMENT_DIAGNOSTICS[code]
    return AcceptedLectureDocumentReopenFailure(
        status="accepted_document_reopen_failed",
        diagnostics=(
            AcceptedLectureDocumentReopenDiagnostic(code, classification, message),
        ),
    )


def _workflow_source_evidence_reopen_failure(
    code: str,
) -> WorkflowSourceEvidenceReopenFailure:
    classification, message = _WORKFLOW_SOURCE_EVIDENCE_REOPEN_DIAGNOSTICS[code]
    return WorkflowSourceEvidenceReopenFailure(
        status="workflow_source_evidence_reopen_failed",
        diagnostics=(
            WorkflowSourceEvidenceReopenDiagnostic(code, classification, message),
        ),
    )


def _course_pdf_build_failure(code: str) -> CoursePdfBuildFailure:
    classification, message = _COURSE_PDF_BUILD_DIAGNOSTICS[code]
    return CoursePdfBuildFailure(
        status="course_pdf_build_failed",
        diagnostics=(CoursePdfBuildDiagnostic(code, classification, message),),
    )
