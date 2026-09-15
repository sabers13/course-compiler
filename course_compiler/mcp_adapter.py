"""Thin Streamable-HTTP MCP adapter for accepted Course Compiler operations.

The adapter owns wire DTO conversion, trusted store lifecycle, ChatGPT file
ingress, and standard MCP binary content.  Workflow semantics, validation,
identity, extraction, composition, and compilation remain owned by the
accepted lower-level modules.
"""

from __future__ import annotations

import base64
import hashlib
import os
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Annotated, Any, Generic, Iterator, Literal, Mapping, TypeAlias, TypeVar
from urllib.parse import urlsplit

from mcp.server import MCPServer
from mcp.types import (
    BlobResourceContents,
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    TextContent,
    ToolAnnotations,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StrictInt,
    StrictStr,
)

from .asset import ASSET_REFERENCE_VERSION, AssetReference
from .compilation import CompiledPdf
from .contracts import DOCUMENT_CONTRACT_VERSION, LectureDocument, SourceProvenance
from .course import COURSE_REFERENCE_VERSION, CourseReference
from .course_workflow import (
    COURSE_WORKFLOW_ASSOCIATION_VERSION,
    CourseWorkflowAssociation,
)
from .course_workflow_operations import (
    ActiveLectureCandidate,
    BlockedWorkflowContext,
    ContinuationLectureProgress,
    CoursePdfBuildFailure,
    CourseWorkflowContinuation,
    CourseWorkflowContinuationFailure,
    CourseWorkflowReopenFailure,
    FailedWorkflowContext,
    MapReopenContinuationContext,
    ReopenedLectureDocument,
    ReopenedWorkflowArtifact,
    ReopenedCourseWorkflowContext,
    WorkflowSourceEvidenceReopenFailure,
    build_reopened_course_pdf,
    build_reopened_course_pdf_with_visuals,
    reopen_course_workflow_continuation,
    reopen_course_workflow_context,
    reopen_workflow_source_evidence,
)
from .course_workflow_persistence import (
    LocalCourseWorkflowAssociationStore,
    open_course_workflow_association_store,
)
from .course_workflow_transition_operations import (
    PersistedCourseWorkflowOperationFailure,
    apply_persisted_course_workflow_request,
)
from .lecture_document_persistence import (
    LocalLectureDocumentStore,
    open_lecture_document_store,
)
from .mcp_file_ingress import (
    FileDownloadPolicy,
    FileIngressFailure,
    OpenAIFileReference,
    download_openai_file,
)
from .pdf_page import PDF_PAGE_REFERENCE_VERSION, PdfPageReference
from .pdf_visual_extraction import (
    ExtractedPdfVisual,
    PdfPagePreviewFailure,
    PdfVisualExtractionFailure,
    RenderedPdfPagePreview,
    extract_pdf_page_region,
    render_pdf_page_preview,
)
from .policy_persistence import LocalPolicyContentStore, open_policy_content_store
from .rendering import DocumentReference
from .source_operations import SourceEvidenceIngestionFailure, ingest_source_evidence
from .source_persistence import (
    LocalSourceEvidenceStore,
    SourceEvidencePayload,
    open_source_evidence_store,
)
from .visual_composition import VisualCompositionFailure
from .visual_placement import VISUAL_PLACEMENT_VERSION, VisualPlacement
from .workflow import (
    InitializeWorkflow,
    InitializeWorkflowRequest,
    OperationReceipt,
    ApproveLectureMap,
    ApproveMapReopen,
    ApprovePriorityBasis,
    ClearBlocker,
    DismissNewSource,
    MarkBlocked,
    RecordCandidateValidation,
    RecordLectureMap,
    RecordNewSource,
    RecordOperationFailure,
    RecordSourceAssessment,
    RejectLectureMap,
    RejectPriorityBasis,
    RetryFailed,
    SourceEvidenceReference,
    SubmitLectureCandidate,
    ValidationRecord,
    WorkflowAdvanced,
    WorkflowArtifactReference,
    WorkflowBlocked,
    WorkflowDiagnostic,
    WorkflowFailed,
    WorkflowHandoff,
    WorkflowIdempotentRepeat,
    WorkflowPolicySet,
    WorkflowRejected,
    WorkflowTransitionRequest,
    derive_workflow_handoff,
)
from .workflow_artifact_operations import (
    WorkflowArtifactIngestionFailure,
    ingest_workflow_artifact,
)
from .workflow_artifact_persistence import (
    LocalWorkflowArtifactStore,
    WorkflowArtifactPayload,
    open_workflow_artifact_store,
)
from .workflow_persistence import LocalWorkflowStateStore, open_workflow_state_store
from .workflow_policy import (
    ArtifactKind,
    BlockerCode,
    FailureCode,
    PriorityMode,
    WorkflowAction,
    WorkflowDisposition,
    WorkflowStage,
    WORKFLOW_TRANSITION_VERSION,
)


MCP_ADAPTER_VERSION = "course-compiler-mcp-adapter/v1"
DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORT = 8000
DEFAULT_MCP_PATH = "/mcp"

_ADAPTER_MESSAGES = {
    "invalid_mcp_input": "The MCP tool input is invalid.",
    "mcp_adapter_exception": "The MCP application adapter failed.",
    "source_ingestion_failed": "The source ingestion operation failed.",
    "source_reference_not_in_workflow": (
        "The source reference does not belong to the requested workflow."
    ),
    "asset_reference_mismatch": (
        "The supplied asset reference does not match deterministic extraction."
    ),
    "workflow_not_buildable": "The workflow is not buildable.",
    "visual_placement_invalid": "The visual placement input is invalid.",
    "course_pdf_build_failed": "The course PDF build operation failed.",
}

_WIRE_INPUT_EXCEPTIONS = (
    AttributeError,
    KeyError,
    RecursionError,
    TypeError,
    UnicodeError,
    ValueError,
)


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SourceEvidenceReferenceInput(_WireModel):
    reference_version: Literal["source-evidence-reference/v1"]
    source_id: str
    content_sha256: str


class WorkflowArtifactReferenceInput(_WireModel):
    reference_version: Literal["workflow-artifact-reference/v1"]
    artifact_id: str
    artifact_kind: Literal[
        "source_assessment",
        "priority_proposal",
        "evidence_hierarchy",
        "lecture_map",
        "lecture_candidate",
        "validation",
        "blocker",
        "failure",
        "decision",
    ]
    content_sha256: str
    producer_version: str


class DocumentReferenceInput(_WireModel):
    contract_version: Literal["lecture-document/v1"]
    document_id: str
    order: int
    content_sha256: str


class AssetReferenceInput(_WireModel):
    reference_version: Literal["asset-reference/v1"]
    content_sha256: str


class PdfPageReferenceInput(_WireModel):
    reference_version: Literal["pdf-page-reference/v1"]
    source_reference: SourceEvidenceReferenceInput
    physical_page_ordinal: int


def _omit_schema_default(schema: dict[str, Any]) -> None:
    """Keep optional file fields absent from `required` without nullable JSON."""

    schema.pop("default", None)


class OpenAIFileReferenceInput(_WireModel):
    download_url: str
    file_id: str
    mime_type: str = Field(  # type: ignore[assignment]
        default=None,
        json_schema_extra=_omit_schema_default,
    )
    file_name: str = Field(  # type: ignore[assignment]
        default=None,
        json_schema_extra=_omit_schema_default,
    )


class InitializeWorkflowPayloadInput(_WireModel):
    source_evidence: list[SourceEvidenceReferenceInput]


class RecordSourceAssessmentPayloadInput(_WireModel):
    assessment_reference: WorkflowArtifactReferenceInput
    primary_mode: Literal["exam_driven", "sheet_driven", "slide_driven", "custom"]
    priority_proposal_reference: WorkflowArtifactReferenceInput
    evidence_hierarchy_reference: WorkflowArtifactReferenceInput


class PrioritySubjectPayloadInput(_WireModel):
    priority_subject_sha256: str


class RecordLectureMapPayloadInput(_WireModel):
    map_reference: WorkflowArtifactReferenceInput
    lecture_ids: list[str]


class MapSubjectPayloadInput(_WireModel):
    map_subject_sha256: str


class SubmitLectureCandidatePayloadInput(_WireModel):
    lecture_id: str
    order: int
    source_text: str


class RecordCandidateValidationPayloadInput(_WireModel):
    lecture_id: str
    candidate_subject_sha256: str
    disposition: Literal["passed", "passed_with_warnings", "rejected"]
    validation_reference: WorkflowArtifactReferenceInput


class RecordOperationFailurePayloadInput(_WireModel):
    failed_action: str
    failure_code: str
    subject_id: str | None
    evidence_reference: WorkflowArtifactReferenceInput


class RetryFailedPayloadInput(_WireModel):
    failure_code: str
    failure_evidence_sha256: str


class MarkBlockedPayloadInput(_WireModel):
    blocker_code: Literal[
        "private_artifact_unavailable", "external_prerequisite_unavailable"
    ]
    subject_id: str | None
    subject_sha256: str
    evidence_reference: WorkflowArtifactReferenceInput


class ClearBlockerPayloadInput(_WireModel):
    blocker_code: Literal[
        "private_artifact_unavailable", "external_prerequisite_unavailable"
    ]
    subject_sha256: str


class RecordNewSourcePayloadInput(_WireModel):
    source_reference: SourceEvidenceReferenceInput
    evidence_reference: WorkflowArtifactReferenceInput


class SourceContentPayloadInput(_WireModel):
    source_content_sha256: str


WorkflowPayloadInput: TypeAlias = (
    InitializeWorkflowPayloadInput
    | RecordSourceAssessmentPayloadInput
    | PrioritySubjectPayloadInput
    | RecordLectureMapPayloadInput
    | MapSubjectPayloadInput
    | SubmitLectureCandidatePayloadInput
    | RecordCandidateValidationPayloadInput
    | RecordOperationFailurePayloadInput
    | RetryFailedPayloadInput
    | MarkBlockedPayloadInput
    | ClearBlockerPayloadInput
    | RecordNewSourcePayloadInput
    | SourceContentPayloadInput
)


class VisualDecisionInput(_WireModel):
    document_reference: DocumentReferenceInput
    source_text_offset: int
    page_reference: PdfPageReferenceInput
    left_px: int
    top_px: int
    width_px: int
    height_px: int
    asset_reference: AssetReferenceInput


class WireDiagnostic(_WireModel):
    code: str
    classification: str
    message: str


class FailureOutput(_WireModel):
    status: Literal["error"]
    diagnostics: list[WireDiagnostic]


class SourceReferenceOutput(_WireModel):
    reference_version: Literal["source-evidence-reference/v1"]
    source_id: str
    content_sha256: str


class ArtifactReferenceOutput(_WireModel):
    reference_version: Literal["workflow-artifact-reference/v1"]
    artifact_id: str
    artifact_kind: ArtifactKind
    content_sha256: str
    producer_version: str


class DocumentReferenceOutput(_WireModel):
    contract_version: Literal["lecture-document/v1"]
    document_id: str
    order: int
    content_sha256: str


class PageReferenceOutput(_WireModel):
    reference_version: Literal["pdf-page-reference/v1"]
    source_reference: SourceReferenceOutput
    physical_page_ordinal: int


class AssetReferenceOutput(_WireModel):
    reference_version: Literal["asset-reference/v1"]
    content_sha256: str


class CourseReferenceOutput(_WireModel):
    reference_version: Literal["course-reference/v1"]
    course_id: str


class CourseWorkflowAssociationOutput(_WireModel):
    association_version: Literal["course-workflow-association/v1"]
    course_reference: CourseReferenceOutput
    workflow_id: str


class OperationReceiptOutput(_WireModel):
    contract_version: Literal["course-workflow-transition/v1", "course-workflow-transition/v2"]
    operation_id: str
    from_revision: int
    to_revision: int
    action: WorkflowAction
    request_sha256: str
    outcome: Literal["advanced", "blocked", "failed"]
    subject_sha256: str


class WorkflowDiagnosticOutput(_WireModel):
    code: str
    severity: Literal["warning", "error"]
    category: Literal["validation", "warning", "blocked", "failure"]
    stage: Literal[
        "initialization",
        "source_assessment",
        "priority_approval",
        "lecture_mapping",
        "map_approval",
        "lecture_production",
        "lecture_validation",
        "completed",
    ]
    subject_id: str | None
    message: str


class WorkflowHandoffOutput(_WireModel):
    contract_version: Literal["course-workflow-handoff/v1", "course-workflow-handoff/v2"]
    workflow_id: str
    revision: int
    stage: WorkflowStage
    disposition: WorkflowDisposition
    source_count: int
    priority_mode: PriorityMode | None
    priority_approved: bool
    map_approved: bool
    lecture_ids: list[str]
    active_lecture_id: str | None
    accepted_documents: list[DocumentReferenceOutput]
    pending_source_sha256: str | None
    active_issue_code: BlockerCode | FailureCode | None
    diagnostic_codes: list[str]
    next_actions: list[WorkflowAction]


class IngestCourseSourceSuccess(_WireModel):
    status: Literal["ingested"]
    source_reference: SourceReferenceOutput


class IngestWorkflowArtifactSuccess(_WireModel):
    status: Literal["ingested"]
    artifact_reference: ArtifactReferenceOutput


class ApplyWorkflowSuccess(_WireModel):
    status: Literal["advanced", "rejected", "blocked", "failed", "idempotent_repeat"]
    receipt: OperationReceiptOutput | None
    handoff: WorkflowHandoffOutput | None
    diagnostics: list[WorkflowDiagnosticOutput]


class AcceptedDocumentOutput(_WireModel):
    document_reference: DocumentReferenceOutput
    source_text: str


class WorkflowArtifactContentOutput(_WireModel):
    reference: ArtifactReferenceOutput
    content: str


class ContinuationLectureProgressOutput(_WireModel):
    lecture_id: str
    order: int
    status: Literal["pending", "candidate", "retry_required", "correction_required", "accepted"]
    attempt: int
    candidate_document_reference: DocumentReferenceOutput | None
    candidate_subject_sha256: str | None
    validation_disposition: Literal["passed", "passed_with_warnings", "rejected"] | None
    validation_reference: ArtifactReferenceOutput | None
    validation_evidence: WorkflowArtifactContentOutput | None
    accepted_document_reference: DocumentReferenceOutput | None


class ActiveLectureCandidateOutput(_WireModel):
    document_reference: DocumentReferenceOutput
    source_text: str
    candidate_subject_sha256: str


class FailedWorkflowContextOutput(_WireModel):
    failed_action: str
    failure_code: str
    subject_id: str | None
    evidence: WorkflowArtifactContentOutput
    failure_evidence_sha256: str


class BlockedWorkflowContextOutput(_WireModel):
    blocker_code: str
    subject_id: str | None
    subject_sha256: str
    evidence: WorkflowArtifactContentOutput


class MapReopenContinuationContextOutput(_WireModel):
    baseline_lecture_map: WorkflowArtifactContentOutput
    baseline_lecture_ids: list[str]
    baseline_reserved_lecture_ids: list[str]
    baseline_map_subject_sha256: str


class GetWorkflowSuccess(_WireModel):
    status: Literal["reopened"]
    association: CourseWorkflowAssociationOutput
    handoff: WorkflowHandoffOutput
    source_references: list[SourceReferenceOutput]
    accepted_documents: list[AcceptedDocumentOutput]
    source_assessment: WorkflowArtifactContentOutput | None
    priority_proposal: WorkflowArtifactContentOutput | None
    evidence_hierarchy: WorkflowArtifactContentOutput | None
    lecture_map: WorkflowArtifactContentOutput | None
    priority_subject_sha256: str | None
    map_subject_sha256: str | None
    lecture_progress: list[ContinuationLectureProgressOutput]
    active_lecture_id: str | None
    active_candidate: ActiveLectureCandidateOutput | None
    failed_workflow: FailedWorkflowContextOutput | None
    blocked_workflow: BlockedWorkflowContextOutput | None
    pending_source: SourceReferenceOutput | None
    map_reopen: MapReopenContinuationContextOutput | None


class PreviewPageSuccess(_WireModel):
    status: Literal["rendered"]
    preview_profile: str
    coordinate_frame: str
    page_reference: PageReferenceOutput
    width_px: int
    height_px: int
    format: Literal["png"]
    mime_type: Literal["image/png"]


class ExtractRegionSuccess(_WireModel):
    status: Literal["extracted"]
    extraction_profile: str
    page_reference: PageReferenceOutput
    left_px: int
    top_px: int
    width_px: int
    height_px: int
    asset_reference: AssetReferenceOutput
    format: Literal["png"]
    mime_type: Literal["image/png"]


class BuildCoursePdfSuccess(_WireModel):
    status: Literal["compiled"]
    compilation_profile: str
    logical_filename: str
    content_sha256: str
    size_bytes: int
    mime_type: Literal["application/pdf"]


_OutputValue = TypeVar("_OutputValue")


class _ObjectOutputRoot(RootModel[_OutputValue], Generic[_OutputValue]):
    """Keep object-only result unions valid in current MCP tool metadata."""

    @classmethod
    def model_json_schema(cls, **kwargs: Any) -> dict[str, Any]:
        schema = super().model_json_schema(**kwargs)
        return {"type": "object", **schema}


class IngestCourseSourceOutput(
    _ObjectOutputRoot[IngestCourseSourceSuccess | FailureOutput]
):
    pass


class IngestWorkflowArtifactOutput(
    _ObjectOutputRoot[IngestWorkflowArtifactSuccess | FailureOutput]
):
    pass


class ApplyWorkflowOutput(_ObjectOutputRoot[ApplyWorkflowSuccess | FailureOutput]):
    pass


class GetWorkflowOutput(_ObjectOutputRoot[GetWorkflowSuccess | FailureOutput]):
    pass


class PreviewPageOutput(_ObjectOutputRoot[PreviewPageSuccess | FailureOutput]):
    pass


class ExtractRegionOutput(_ObjectOutputRoot[ExtractRegionSuccess | FailureOutput]):
    pass


class BuildCoursePdfOutput(_ObjectOutputRoot[BuildCoursePdfSuccess | FailureOutput]):
    pass


@dataclass(frozen=True, slots=True)
class McpAdapterConfig:
    """Trusted server configuration; none of these values are tool inputs."""

    association_database_path: Path
    workflow_database_path: Path
    policy_database_path: Path
    source_database_path: Path
    artifact_database_path: Path
    document_database_path: Path
    workflow_policies: WorkflowPolicySet
    file_download_policy: FileDownloadPolicy | None
    file_host_observation_path: Path | None = None

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name.endswith("_database_path") and not isinstance(value, Path):
                raise ValueError("MCP adapter database paths must be Path values")
        if type(self.workflow_policies) is not WorkflowPolicySet:
            raise ValueError("MCP adapter workflow policies are invalid")
        try:
            self.workflow_policies.__post_init__()
        except Exception:
            raise ValueError("MCP adapter workflow policies are invalid") from None
        if self.file_download_policy is not None and type(
            self.file_download_policy
        ) is not FileDownloadPolicy:
            raise ValueError("MCP adapter file download policy is invalid")
        if self.file_host_observation_path is not None and not isinstance(
            self.file_host_observation_path, Path
        ):
            raise ValueError("MCP adapter file host observation path is invalid")


@dataclass(frozen=True, slots=True)
class _PreparedVisualDecision:
    """Accepted domain values mechanically converted from one wire decision."""

    placement: VisualPlacement
    page_reference: PdfPageReference
    left_px: int
    top_px: int
    width_px: int
    height_px: int


class _StoreOpenFailure(Exception):
    pass


_STORE_SPECS = {
    "association": (
        "association_database_path",
        open_course_workflow_association_store,
        LocalCourseWorkflowAssociationStore,
    ),
    "workflow": (
        "workflow_database_path",
        open_workflow_state_store,
        LocalWorkflowStateStore,
    ),
    "policy": (
        "policy_database_path",
        open_policy_content_store,
        LocalPolicyContentStore,
    ),
    "source": (
        "source_database_path",
        open_source_evidence_store,
        LocalSourceEvidenceStore,
    ),
    "artifact": (
        "artifact_database_path",
        open_workflow_artifact_store,
        LocalWorkflowArtifactStore,
    ),
    "document": (
        "document_database_path",
        open_lecture_document_store,
        LocalLectureDocumentStore,
    ),
}


class _RedactingMCPServer(MCPServer):
    """Distinguish caller validation from later redacted server failures."""

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Any = None,
    ) -> CallToolResult:
        tool = self._tool_manager.get_tool(name)
        if tool is not None:
            try:
                # The SDK validates again during execution.  This first pass
                # establishes that raw caller input is valid before any tool
                # body or structured-output conversion can run.
                tool.fn_metadata.validate_arguments(arguments)
            except _WIRE_INPUT_EXCEPTIONS:
                return _adapter_failure("invalid_mcp_input")
            except Exception:
                return _adapter_failure("mcp_adapter_exception")
        try:
            result = await super().call_tool(name, arguments, context)
        except Exception:
            return _adapter_failure("mcp_adapter_exception")
        if type(result) is not CallToolResult:
            return _adapter_failure("mcp_adapter_exception")
        return result


def create_mcp_server(config: McpAdapterConfig) -> MCPServer:
    """Create the exact seven-tool Course Compiler MCP server."""

    if type(config) is not McpAdapterConfig:
        raise TypeError("config must be exactly McpAdapterConfig")

    server = _RedactingMCPServer(
        name="course-compiler",
        title="Course Compiler",
        description=(
            "Deterministic Course Compiler application operations for a ChatGPT "
            "semantic workflow."
        ),
        instructions=(
            "Use immutable references returned by earlier calls. Inspect a PDF page "
            "before choosing crop coordinates; Course Compiler does not make semantic "
            "lecture or visual decisions."
        ),
        version=MCP_ADAPTER_VERSION,
        debug=False,
        log_level="ERROR",
    )

    @server.tool(
        name="ingest_course_source",
        title="Ingest course source",
        description=(
            "Download one ChatGPT-authorized temporary private course file from an "
            "allowlisted HTTPS host and persist its exact bytes as source evidence."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            openWorldHint=True,
            idempotentHint=True,
        ),
        meta={"openai/fileParams": ["file"]},
    )
    def ingest_course_source(
        source_id: StrictStr,
        file: OpenAIFileReferenceInput,
    ) -> Annotated[CallToolResult, IngestCourseSourceOutput]:
        try:
            try:
                # Reuse the accepted source-ID contract before performing any
                # outbound request; the temporary URL never participates in
                # this identity check.
                SourceEvidenceReference(
                    "source-evidence-reference/v1",
                    source_id,
                    "0" * 64,
                )
                descriptor = OpenAIFileReference(
                    file.download_url,
                    file.file_id,
                    file.mime_type,
                    file.file_name,
                )
            except _WIRE_INPUT_EXCEPTIONS:
                return _adapter_failure("invalid_mcp_input")
            if config.file_download_policy is None:
                _observe_file_host(
                    descriptor,
                    config.file_host_observation_path,
                )
                return _fixed_failure(
                    "file_download_not_allowed",
                    "ingress",
                    "The temporary file download is not allowed.",
                )
            downloaded = download_openai_file(
                descriptor,
                policy=config.file_download_policy,
            )
            if type(downloaded) is FileIngressFailure:
                return _fixed_failure(downloaded.code, "ingress", downloaded.message)
            if type(downloaded) is not bytes:
                return _adapter_failure("source_ingestion_failed")
            with _open_stores(config, "source") as stores:
                result = ingest_source_evidence(
                    source_id,
                    downloaded,
                    source_store=stores["source"],
                )
            if type(result) is SourceEvidenceIngestionFailure:
                return _domain_failure(result)
            if type(result) is not SourceEvidencePayload:
                return _adapter_failure("source_ingestion_failed")
            return _success_result(
                IngestCourseSourceSuccess(
                    status="ingested",
                    source_reference=_source_reference_output(result.reference),
                )
            )
        except Exception:
            return _adapter_failure("mcp_adapter_exception")

    @server.tool(
        name="ingest_workflow_artifact",
        title="Ingest workflow artifact",
        description=(
            "Persist exact UTF-8 GPT-produced workflow evidence under an immutable "
            "accepted workflow-artifact reference."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            openWorldHint=False,
            idempotentHint=True,
        ),
    )
    def ingest_workflow_artifact_tool(
        artifact_id: StrictStr,
        artifact_kind: ArtifactKind,
        content_utf8: StrictStr,
    ) -> Annotated[CallToolResult, IngestWorkflowArtifactOutput]:
        try:
            try:
                content = content_utf8.encode("utf-8")
            except _WIRE_INPUT_EXCEPTIONS:
                return _adapter_failure("invalid_mcp_input")
            with _open_stores(config, "artifact") as stores:
                result = ingest_workflow_artifact(
                    artifact_id,
                    artifact_kind,
                    content,
                    artifact_store=stores["artifact"],
                )
            if type(result) is WorkflowArtifactIngestionFailure:
                return _domain_failure(result)
            if type(result) is not WorkflowArtifactPayload:
                return _adapter_failure("mcp_adapter_exception")
            return _success_result(
                IngestWorkflowArtifactSuccess(
                    status="ingested",
                    artifact_reference=_artifact_reference_output(result.reference),
                )
            )
        except Exception:
            return _adapter_failure("mcp_adapter_exception")

    @server.tool(
        name="apply_course_workflow_request",
        title="Apply course workflow request",
        description=(
            "Apply one exact accepted Course Compiler workflow action with operation-ID "
            "idempotency and trusted server-configured policies."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            openWorldHint=False,
            idempotentHint=True,
        ),
    )
    def apply_course_workflow_request(
        course_id: StrictStr,
        workflow_id: StrictStr,
        operation_id: StrictStr,
        action: WorkflowAction,
        payload: WorkflowPayloadInput,
        expected_revision: StrictInt | None = None,
    ) -> Annotated[CallToolResult, ApplyWorkflowOutput]:
        try:
            try:
                association = _association(course_id, workflow_id)
                request = _workflow_request(
                    workflow_id,
                    operation_id,
                    action,
                    payload,
                    expected_revision,
                    policies=config.workflow_policies,
                )
            except _WIRE_INPUT_EXCEPTIONS:
                return _adapter_failure("invalid_mcp_input")
            with _open_stores(
                config,
                "association",
                "workflow",
                "policy",
                "source",
                "artifact",
                "document",
            ) as stores:
                result = apply_persisted_course_workflow_request(
                    association,
                    request,
                    association_store=stores["association"],
                    workflow_store=stores["workflow"],
                    policy_store=stores["policy"],
                    source_store=stores["source"],
                    artifact_store=stores["artifact"],
                    document_store=stores["document"],
                )
            if type(result) is PersistedCourseWorkflowOperationFailure:
                return _domain_failure(result)
            if not isinstance(
                result,
                (
                    WorkflowAdvanced,
                    WorkflowRejected,
                    WorkflowBlocked,
                    WorkflowFailed,
                    WorkflowIdempotentRepeat,
                ),
            ):
                return _adapter_failure("mcp_adapter_exception")
            return _success_result(
                _apply_workflow_output(result)
            )
        except Exception:
            return _adapter_failure("mcp_adapter_exception")

    @server.tool(
        name="get_course_workflow",
        title="Get course workflow",
        description=(
            "Reopen one exact persisted course workflow and its stage-relevant semantic "
            "evidence for safe continuation in a fresh ChatGPT context."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=False,
        ),
    )
    def get_course_workflow(
        course_id: StrictStr,
        workflow_id: StrictStr,
    ) -> Annotated[CallToolResult, GetWorkflowOutput]:
        try:
            try:
                association = _association(course_id, workflow_id)
            except _WIRE_INPUT_EXCEPTIONS:
                return _adapter_failure("invalid_mcp_input")
            with _open_stores(
                config,
                "association",
                "workflow",
                "policy",
                "artifact",
                "document",
            ) as stores:
                context = reopen_course_workflow_context(
                    association,
                    association_store=stores["association"],
                    workflow_store=stores["workflow"],
                    policy_store=stores["policy"],
                )
                if type(context) is CourseWorkflowReopenFailure:
                    return _domain_failure(context)
                if type(context) is not ReopenedCourseWorkflowContext:
                    return _adapter_failure("mcp_adapter_exception")
                continuation = reopen_course_workflow_continuation(
                    context,
                    artifact_store=stores["artifact"],
                    document_store=stores["document"],
                )
            if type(continuation) is CourseWorkflowContinuationFailure:
                return _domain_failure(continuation)
            if type(continuation) is not CourseWorkflowContinuation:
                return _adapter_failure("mcp_adapter_exception")
            return _success_result(
                GetWorkflowSuccess(
                    status="reopened",
                    association=_association_output(context.association),
                    handoff=_handoff_output(continuation.handoff),
                    source_references=[
                        _source_reference_output(item)
                        for item in continuation.source_references
                    ],
                    accepted_documents=[
                        _accepted_document_output(document)
                        for document in continuation.accepted_documents
                    ],
                    source_assessment=_optional_artifact_content_output(
                        continuation.source_assessment
                    ),
                    priority_proposal=_optional_artifact_content_output(
                        continuation.priority_proposal
                    ),
                    evidence_hierarchy=_optional_artifact_content_output(
                        continuation.evidence_hierarchy
                    ),
                    lecture_map=_optional_artifact_content_output(
                        continuation.lecture_map
                    ),
                    priority_subject_sha256=continuation.priority_subject_sha256,
                    map_subject_sha256=continuation.map_subject_sha256,
                    lecture_progress=[
                        _continuation_progress_output(item)
                        for item in continuation.lecture_progress
                    ],
                    active_lecture_id=continuation.active_lecture_id,
                    active_candidate=(
                        None
                        if continuation.active_candidate is None
                        else _active_candidate_output(continuation.active_candidate)
                    ),
                    failed_workflow=(
                        None
                        if continuation.failed_workflow is None
                        else _failed_workflow_output(continuation.failed_workflow)
                    ),
                    blocked_workflow=(
                        None
                        if continuation.blocked_workflow is None
                        else _blocked_workflow_output(continuation.blocked_workflow)
                    ),
                    pending_source=(
                        None
                        if continuation.pending_source is None
                        else _source_reference_output(continuation.pending_source)
                    ),
                    map_reopen=(
                        None
                        if continuation.map_reopen is None
                        else _map_reopen_output(continuation.map_reopen)
                    ),
                )
            )
        except Exception:
            return _adapter_failure("mcp_adapter_exception")

    @server.tool(
        name="preview_source_pdf_page",
        title="Preview source PDF page",
        description=(
            "Render one exact workflow-owned source PDF page as a PNG in the accepted "
            "top-left 144-DPI extraction coordinate frame."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=False,
        ),
    )
    def preview_source_pdf_page(
        course_id: StrictStr,
        workflow_id: StrictStr,
        page_reference: PdfPageReferenceInput,
    ) -> Annotated[CallToolResult, PreviewPageOutput]:
        try:
            try:
                association = _association(course_id, workflow_id)
                page = _page_reference(page_reference)
            except _WIRE_INPUT_EXCEPTIONS:
                return _adapter_failure("invalid_mcp_input")
            with _open_stores(
                config, "association", "workflow", "policy", "source"
            ) as stores:
                context = _reopen_context(association, stores)
                if type(context) is CallToolResult:
                    return context
                source_bytes = _workflow_source_bytes(context, page, stores["source"])
                if type(source_bytes) is CallToolResult:
                    return source_bytes
                result = render_pdf_page_preview(source_bytes, page)
            if type(result) is PdfPagePreviewFailure:
                return _domain_failure(result)
            if type(result) is not RenderedPdfPagePreview:
                return _adapter_failure("mcp_adapter_exception")
            structured = PreviewPageSuccess(
                status="rendered",
                preview_profile=result.preview_profile,
                coordinate_frame=result.coordinate_frame,
                page_reference=_page_reference_output(result.page_reference),
                width_px=result.width_px,
                height_px=result.height_px,
                format="png",
                mime_type="image/png",
            )
            return _success_result(
                structured,
                content=[
                    TextContent(text="Rendered one workflow-owned PDF page."),
                    ImageContent(
                        data=base64.b64encode(result.content_bytes).decode("ascii"),
                        mimeType="image/png",
                    ),
                ],
            )
        except Exception:
            return _adapter_failure("mcp_adapter_exception")

    @server.tool(
        name="extract_source_pdf_region",
        title="Extract source PDF region",
        description=(
            "Deterministically extract one exact crop from a workflow-owned PDF page "
            "and return its accepted content-addressed asset reference and PNG."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=False,
        ),
    )
    def extract_source_pdf_region(
        course_id: StrictStr,
        workflow_id: StrictStr,
        page_reference: PdfPageReferenceInput,
        left_px: StrictInt,
        top_px: StrictInt,
        width_px: StrictInt,
        height_px: StrictInt,
    ) -> Annotated[CallToolResult, ExtractRegionOutput]:
        try:
            try:
                association = _association(course_id, workflow_id)
                page = _page_reference(page_reference)
            except _WIRE_INPUT_EXCEPTIONS:
                return _adapter_failure("invalid_mcp_input")
            with _open_stores(
                config, "association", "workflow", "policy", "source"
            ) as stores:
                context = _reopen_context(association, stores)
                if type(context) is CallToolResult:
                    return context
                source_bytes = _workflow_source_bytes(context, page, stores["source"])
                if type(source_bytes) is CallToolResult:
                    return source_bytes
                result = extract_pdf_page_region(
                    source_bytes,
                    page,
                    left_px=left_px,
                    top_px=top_px,
                    width_px=width_px,
                    height_px=height_px,
                )
            if type(result) is PdfVisualExtractionFailure:
                return _domain_failure(result)
            if type(result) is not ExtractedPdfVisual:
                return _adapter_failure("mcp_adapter_exception")
            structured = ExtractRegionSuccess(
                status="extracted",
                extraction_profile=result.extraction_profile,
                page_reference=_page_reference_output(result.page_reference),
                left_px=result.left_px,
                top_px=result.top_px,
                width_px=result.width_px,
                height_px=result.height_px,
                asset_reference=_asset_reference_output(result.asset_reference),
                format="png",
                mime_type="image/png",
            )
            return _success_result(
                structured,
                content=[
                    TextContent(text="Extracted one deterministic PDF region."),
                    ImageContent(
                        data=base64.b64encode(result.content_bytes).decode("ascii"),
                        mimeType="image/png",
                    ),
                ],
            )
        except Exception:
            return _adapter_failure("mcp_adapter_exception")

    @server.tool(
        name="build_course_pdf",
        title="Build course PDF",
        description=(
            "Build the final PDF for a completed workflow, deterministically "
            "re-extracting and verifying every supplied visual decision."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=False,
        ),
    )
    def build_course_pdf(
        course_id: StrictStr,
        workflow_id: StrictStr,
        visuals: list[VisualDecisionInput],
    ) -> Annotated[CallToolResult, BuildCoursePdfOutput]:
        try:
            try:
                association = _association(course_id, workflow_id)
                prepared_visuals = _prepare_visual_decisions(visuals)
            except _WIRE_INPUT_EXCEPTIONS:
                return _adapter_failure("invalid_mcp_input")
            names = ["association", "workflow", "policy", "document"]
            if prepared_visuals:
                names.append("source")
            with _open_stores(config, *names) as stores:
                context = _reopen_context(association, stores)
                if type(context) is CallToolResult:
                    return context
                if not prepared_visuals:
                    result = build_reopened_course_pdf(
                        context,
                        document_store=stores["document"],
                    )
                else:
                    extracted = _extract_visual_decisions(
                        context,
                        prepared_visuals,
                        stores["source"],
                    )
                    if type(extracted) is CallToolResult:
                        return extracted
                    placements, asset_bytes = extracted
                    result = build_reopened_course_pdf_with_visuals(
                        context,
                        document_store=stores["document"],
                        placements=placements,
                        asset_bytes=asset_bytes,
                    )
            if isinstance(
                result,
                (CoursePdfBuildFailure, VisualCompositionFailure),
            ):
                return _domain_failure(result)
            if type(result) is not CompiledPdf:
                return _adapter_failure("course_pdf_build_failed")
            digest = hashlib.sha256(result.pdf_content).hexdigest()
            structured = BuildCoursePdfSuccess(
                status="compiled",
                compilation_profile=result.compilation_profile.profile,
                logical_filename=result.logical_filename,
                content_sha256=digest,
                size_bytes=len(result.pdf_content),
                mime_type="application/pdf",
            )
            resource = EmbeddedResource(
                resource=BlobResourceContents(
                    uri=(
                        f"course-compiler://compiled-pdf/{digest}/"
                        f"{result.logical_filename}"
                    ),
                    mimeType="application/pdf",
                    blob=base64.b64encode(result.pdf_content).decode("ascii"),
                )
            )
            return _success_result(
                structured,
                content=[TextContent(text="Built the completed course PDF."), resource],
            )
        except Exception:
            return _adapter_failure("mcp_adapter_exception")

    return server


def create_streamable_http_app(config: McpAdapterConfig) -> Any:
    """Create the stateless local-default Streamable HTTP ASGI application."""

    return create_mcp_server(config).streamable_http_app(
        streamable_http_path=DEFAULT_MCP_PATH,
        stateless_http=True,
        host=DEFAULT_MCP_HOST,
    )


def run_mcp_server(
    config: McpAdapterConfig,
    *,
    host: str = DEFAULT_MCP_HOST,
    port: int = DEFAULT_MCP_PORT,
) -> None:
    """Run the official SDK Streamable HTTP transport at the fixed `/mcp` path."""

    create_mcp_server(config).run(
        transport="streamable-http",
        host=host,
        port=port,
        streamable_http_path=DEFAULT_MCP_PATH,
        stateless_http=True,
    )


@contextmanager
def _open_stores(
    config: McpAdapterConfig,
    *names: str,
) -> Iterator[dict[str, Any]]:
    with ExitStack() as stack:
        stores: dict[str, Any] = {}
        for name in names:
            path_name, opener, expected_type = _STORE_SPECS[name]
            opened = opener(getattr(config, path_name))
            if type(opened) is not expected_type:
                raise _StoreOpenFailure
            stack.callback(opened.close)
            stores[name] = opened
        yield stores


def _association(course_id: str, workflow_id: str) -> CourseWorkflowAssociation:
    return CourseWorkflowAssociation(
        COURSE_WORKFLOW_ASSOCIATION_VERSION,
        CourseReference(COURSE_REFERENCE_VERSION, course_id),
        workflow_id,
    )


def _source_reference(value: SourceEvidenceReferenceInput) -> SourceEvidenceReference:
    return SourceEvidenceReference(
        value.reference_version,
        value.source_id,
        value.content_sha256,
    )


def _artifact_reference(
    value: WorkflowArtifactReferenceInput,
) -> WorkflowArtifactReference:
    return WorkflowArtifactReference(
        value.reference_version,
        value.artifact_id,
        value.artifact_kind,
        value.content_sha256,
        value.producer_version,
    )


def _document_reference(value: DocumentReferenceInput) -> DocumentReference:
    return DocumentReference(
        value.contract_version,
        value.document_id,
        value.order,
        value.content_sha256,
    )


def _asset_reference(value: AssetReferenceInput) -> AssetReference:
    return AssetReference(value.reference_version, value.content_sha256)


def _page_reference(value: PdfPageReferenceInput) -> PdfPageReference:
    return PdfPageReference(
        value.reference_version,
        _source_reference(value.source_reference),
        value.physical_page_ordinal,
    )


def _workflow_request(
    workflow_id: str,
    operation_id: str,
    action: str,
    payload: WorkflowPayloadInput,
    expected_revision: int | None,
    *,
    policies: WorkflowPolicySet,
) -> InitializeWorkflowRequest | WorkflowTransitionRequest:
    if action == "initialize_workflow":
        if type(payload) is not InitializeWorkflowPayloadInput or expected_revision is not None:
            raise ValueError("invalid initialization DTO")
        return InitializeWorkflowRequest(
            WORKFLOW_TRANSITION_VERSION,
            workflow_id,
            operation_id,
            InitializeWorkflow(
                "initialize_workflow",
                policies,
                tuple(_source_reference(item) for item in payload.source_evidence),
            ),
        )

    if type(expected_revision) is not int:
        raise ValueError("expected revision is required")
    domain_payload: object
    if action == "record_source_assessment" and type(payload) is RecordSourceAssessmentPayloadInput:
        domain_payload = RecordSourceAssessment(
            action,
            _artifact_reference(payload.assessment_reference),
            payload.primary_mode,
            _artifact_reference(payload.priority_proposal_reference),
            _artifact_reference(payload.evidence_hierarchy_reference),
        )
    elif (
        action in {"approve_priority_basis", "reject_priority_basis"}
        and type(payload) is PrioritySubjectPayloadInput
    ):
        constructor = (
            ApprovePriorityBasis
            if action == "approve_priority_basis"
            else RejectPriorityBasis
        )
        domain_payload = constructor(action, payload.priority_subject_sha256)
    elif action == "record_lecture_map" and type(payload) is RecordLectureMapPayloadInput:
        domain_payload = RecordLectureMap(
            action,
            _artifact_reference(payload.map_reference),
            tuple(payload.lecture_ids),
        )
    elif (
        action in {"approve_lecture_map", "reject_lecture_map"}
        and type(payload) is MapSubjectPayloadInput
    ):
        constructor = ApproveLectureMap if action == "approve_lecture_map" else RejectLectureMap
        domain_payload = constructor(action, payload.map_subject_sha256)
    elif (
        action == "submit_lecture_candidate"
        and type(payload) is SubmitLectureCandidatePayloadInput
    ):
        source_bytes = payload.source_text.encode("utf-8")
        document = LectureDocument(
            DOCUMENT_CONTRACT_VERSION,
            payload.lecture_id,
            payload.order,
            payload.source_text,
            SourceProvenance(hashlib.sha256(source_bytes).hexdigest()),
        )
        domain_payload = SubmitLectureCandidate(action, payload.lecture_id, document)
    elif (
        action == "record_candidate_validation"
        and type(payload) is RecordCandidateValidationPayloadInput
    ):
        validation_policy = policies.lecture_validation
        validation = ValidationRecord(
            payload.candidate_subject_sha256,
            validation_policy.policy_version,
            validation_policy.content_sha256,
            payload.disposition,
            _artifact_reference(payload.validation_reference),
        )
        domain_payload = RecordCandidateValidation(
            action,
            payload.lecture_id,
            payload.candidate_subject_sha256,
            validation,
        )
    elif (
        action == "record_operation_failure"
        and type(payload) is RecordOperationFailurePayloadInput
    ):
        domain_payload = RecordOperationFailure(
            action,
            payload.failed_action,
            payload.failure_code,
            payload.subject_id,
            _artifact_reference(payload.evidence_reference),
        )
    elif action == "retry_failed" and type(payload) is RetryFailedPayloadInput:
        domain_payload = RetryFailed(
            action,
            payload.failure_code,
            payload.failure_evidence_sha256,
        )
    elif action == "mark_blocked" and type(payload) is MarkBlockedPayloadInput:
        domain_payload = MarkBlocked(
            action,
            payload.blocker_code,
            payload.subject_id,
            payload.subject_sha256,
            _artifact_reference(payload.evidence_reference),
        )
    elif action == "clear_blocker" and type(payload) is ClearBlockerPayloadInput:
        domain_payload = ClearBlocker(
            action,
            payload.blocker_code,
            payload.subject_sha256,
        )
    elif action == "record_new_source" and type(payload) is RecordNewSourcePayloadInput:
        domain_payload = RecordNewSource(
            action,
            _source_reference(payload.source_reference),
            _artifact_reference(payload.evidence_reference),
        )
    elif (
        action in {"dismiss_new_source", "approve_map_reopen"}
        and type(payload) is SourceContentPayloadInput
    ):
        constructor = (
            DismissNewSource if action == "dismiss_new_source" else ApproveMapReopen
        )
        domain_payload = constructor(action, payload.source_content_sha256)
    else:
        raise ValueError("workflow action and payload do not match")

    return WorkflowTransitionRequest(
        WORKFLOW_TRANSITION_VERSION,
        workflow_id,
        expected_revision,
        operation_id,
        domain_payload,
    )


def _reopen_context(
    association: CourseWorkflowAssociation,
    stores: Mapping[str, Any],
) -> ReopenedCourseWorkflowContext | CallToolResult:
    context = reopen_course_workflow_context(
        association,
        association_store=stores["association"],
        workflow_store=stores["workflow"],
        policy_store=stores["policy"],
    )
    if type(context) is CourseWorkflowReopenFailure:
        return _domain_failure(context)
    if type(context) is not ReopenedCourseWorkflowContext:
        return _adapter_failure("mcp_adapter_exception")
    return context


def _workflow_source_bytes(
    context: ReopenedCourseWorkflowContext,
    page_reference: PdfPageReference,
    source_store: LocalSourceEvidenceStore,
) -> bytes | CallToolResult:
    if page_reference.source_reference not in context.workflow_state.source_evidence:
        return _adapter_failure("source_reference_not_in_workflow")
    payloads = reopen_workflow_source_evidence(context, source_store=source_store)
    if type(payloads) is WorkflowSourceEvidenceReopenFailure:
        return _domain_failure(payloads)
    if type(payloads) is not tuple:
        return _adapter_failure("mcp_adapter_exception")
    for payload in payloads:
        if payload.reference == page_reference.source_reference:
            return payload.payload
    return _adapter_failure("source_reference_not_in_workflow")


def _prepare_visual_decisions(
    visuals: list[VisualDecisionInput],
) -> tuple[_PreparedVisualDecision, ...]:
    prepared = []
    for wire in visuals:
        expected_asset = _asset_reference(wire.asset_reference)
        prepared.append(
            _PreparedVisualDecision(
                placement=VisualPlacement(
                    VISUAL_PLACEMENT_VERSION,
                    _document_reference(wire.document_reference),
                    wire.source_text_offset,
                    expected_asset,
                ),
                page_reference=_page_reference(wire.page_reference),
                left_px=wire.left_px,
                top_px=wire.top_px,
                width_px=wire.width_px,
                height_px=wire.height_px,
            )
        )
    return tuple(prepared)


def _extract_visual_decisions(
    context: ReopenedCourseWorkflowContext,
    visuals: tuple[_PreparedVisualDecision, ...],
    source_store: LocalSourceEvidenceStore,
) -> tuple[tuple[VisualPlacement, ...], dict[AssetReference, bytes]] | CallToolResult:
    current_sources = context.workflow_state.source_evidence
    if any(
        item.page_reference.source_reference not in current_sources for item in visuals
    ):
        return _adapter_failure("source_reference_not_in_workflow")

    payloads = reopen_workflow_source_evidence(context, source_store=source_store)
    if type(payloads) is WorkflowSourceEvidenceReopenFailure:
        return _domain_failure(payloads)
    if type(payloads) is not tuple:
        return _adapter_failure("mcp_adapter_exception")
    source_bytes = {item.reference: item.payload for item in payloads}

    placements: list[VisualPlacement] = []
    assets: dict[AssetReference, bytes] = {}
    for visual in visuals:
        extraction = extract_pdf_page_region(
            source_bytes[visual.page_reference.source_reference],
            visual.page_reference,
            left_px=visual.left_px,
            top_px=visual.top_px,
            width_px=visual.width_px,
            height_px=visual.height_px,
        )
        if type(extraction) is PdfVisualExtractionFailure:
            return _domain_failure(extraction)
        if type(extraction) is not ExtractedPdfVisual:
            return _adapter_failure("course_pdf_build_failed")
        expected_asset = visual.placement.asset_reference
        if extraction.asset_reference != expected_asset:
            return _adapter_failure("asset_reference_mismatch")
        placements.append(visual.placement)
        assets[expected_asset] = extraction.content_bytes
    return tuple(placements), assets


def _source_reference_output(
    value: SourceEvidenceReference,
) -> SourceReferenceOutput:
    return SourceReferenceOutput(
        reference_version=value.reference_version,
        source_id=value.source_id,
        content_sha256=value.content_sha256,
    )


def _artifact_content_output(
    value: ReopenedWorkflowArtifact,
) -> WorkflowArtifactContentOutput:
    return WorkflowArtifactContentOutput(
        reference=_artifact_reference_output(value.reference),
        content=value.content,
    )


def _optional_artifact_content_output(
    value: ReopenedWorkflowArtifact | None,
) -> WorkflowArtifactContentOutput | None:
    return None if value is None else _artifact_content_output(value)


def _accepted_document_output(
    value: ReopenedLectureDocument,
) -> AcceptedDocumentOutput:
    return AcceptedDocumentOutput(
        document_reference=_document_reference_output(value.document_reference),
        source_text=value.source_text,
    )


def _continuation_progress_output(
    value: ContinuationLectureProgress,
) -> ContinuationLectureProgressOutput:
    return ContinuationLectureProgressOutput(
        lecture_id=value.lecture_id,
        order=value.order,
        status=value.status,
        attempt=value.attempt,
        candidate_document_reference=(
            None
            if value.candidate_document_reference is None
            else _document_reference_output(value.candidate_document_reference)
        ),
        candidate_subject_sha256=value.candidate_subject_sha256,
        validation_disposition=value.validation_disposition,
        validation_reference=(
            None
            if value.validation_reference is None
            else _artifact_reference_output(value.validation_reference)
        ),
        validation_evidence=_optional_artifact_content_output(
            value.validation_evidence
        ),
        accepted_document_reference=(
            None
            if value.accepted_document_reference is None
            else _document_reference_output(value.accepted_document_reference)
        ),
    )


def _active_candidate_output(
    value: ActiveLectureCandidate,
) -> ActiveLectureCandidateOutput:
    return ActiveLectureCandidateOutput(
        document_reference=_document_reference_output(value.document_reference),
        source_text=value.source_text,
        candidate_subject_sha256=value.candidate_subject_sha256,
    )


def _failed_workflow_output(
    value: FailedWorkflowContext,
) -> FailedWorkflowContextOutput:
    return FailedWorkflowContextOutput(
        failed_action=value.failed_action,
        failure_code=value.failure_code,
        subject_id=value.subject_id,
        evidence=_artifact_content_output(value.evidence),
        failure_evidence_sha256=value.failure_evidence_sha256,
    )


def _blocked_workflow_output(
    value: BlockedWorkflowContext,
) -> BlockedWorkflowContextOutput:
    return BlockedWorkflowContextOutput(
        blocker_code=value.blocker_code,
        subject_id=value.subject_id,
        subject_sha256=value.subject_sha256,
        evidence=_artifact_content_output(value.evidence),
    )


def _map_reopen_output(
    value: MapReopenContinuationContext,
) -> MapReopenContinuationContextOutput:
    return MapReopenContinuationContextOutput(
        baseline_lecture_map=_artifact_content_output(
            value.baseline_lecture_map
        ),
        baseline_lecture_ids=list(value.baseline_lecture_ids),
        baseline_reserved_lecture_ids=list(value.baseline_reserved_lecture_ids),
        baseline_map_subject_sha256=value.baseline_map_subject_sha256,
    )


def _artifact_reference_output(
    value: WorkflowArtifactReference,
) -> ArtifactReferenceOutput:
    return ArtifactReferenceOutput(
        reference_version=value.reference_version,
        artifact_id=value.artifact_id,
        artifact_kind=value.artifact_kind,
        content_sha256=value.content_sha256,
        producer_version=value.producer_version,
    )


def _document_reference_output(value: DocumentReference) -> DocumentReferenceOutput:
    return DocumentReferenceOutput(
        contract_version=value.contract_version,
        document_id=value.document_id,
        order=value.order,
        content_sha256=value.content_sha256,
    )


def _page_reference_output(value: PdfPageReference) -> PageReferenceOutput:
    return PageReferenceOutput(
        reference_version=value.reference_version,
        source_reference=_source_reference_output(value.source_reference),
        physical_page_ordinal=value.physical_page_ordinal,
    )


def _asset_reference_output(value: AssetReference) -> AssetReferenceOutput:
    return AssetReferenceOutput(
        reference_version=value.reference_version,
        content_sha256=value.content_sha256,
    )


def _association_output(
    value: CourseWorkflowAssociation,
) -> CourseWorkflowAssociationOutput:
    return CourseWorkflowAssociationOutput(
        association_version=value.association_version,
        course_reference=CourseReferenceOutput(
            reference_version=value.course_reference.reference_version,
            course_id=value.course_reference.course_id,
        ),
        workflow_id=value.workflow_id,
    )


def _receipt_output(value: OperationReceipt) -> OperationReceiptOutput:
    return OperationReceiptOutput(
        contract_version=value.contract_version,
        operation_id=value.operation_id,
        from_revision=value.from_revision,
        to_revision=value.to_revision,
        action=value.action,
        request_sha256=value.request_sha256,
        outcome=value.outcome,
        subject_sha256=value.subject_sha256,
    )


def _workflow_diagnostic_output(
    value: WorkflowDiagnostic,
) -> WorkflowDiagnosticOutput:
    return WorkflowDiagnosticOutput(
        code=value.code,
        severity=value.severity,
        category=value.category,
        stage=value.stage,
        subject_id=value.subject_id,
        message=value.message,
    )


def _handoff_output(value: WorkflowHandoff) -> WorkflowHandoffOutput:
    return WorkflowHandoffOutput(
        contract_version=value.contract_version,
        workflow_id=value.workflow_id,
        revision=value.revision,
        stage=value.stage,
        disposition=value.disposition,
        source_count=value.source_count,
        priority_mode=value.priority_mode,
        priority_approved=value.priority_approved,
        map_approved=value.map_approved,
        lecture_ids=list(value.lecture_ids),
        active_lecture_id=value.active_lecture_id,
        accepted_documents=[
            _document_reference_output(item) for item in value.accepted_documents
        ],
        pending_source_sha256=value.pending_source_sha256,
        active_issue_code=value.active_issue_code,
        diagnostic_codes=list(value.diagnostic_codes),
        next_actions=list(value.next_actions),
    )


def _apply_workflow_output(
    value: (
        WorkflowAdvanced
        | WorkflowRejected
        | WorkflowBlocked
        | WorkflowFailed
        | WorkflowIdempotentRepeat
    ),
) -> ApplyWorkflowSuccess:
    if type(value) is WorkflowAdvanced:
        receipt = value.receipt
        diagnostics = value.diagnostics
    elif type(value) is WorkflowRejected:
        receipt = None
        diagnostics = value.diagnostics
    elif type(value) in (WorkflowBlocked, WorkflowFailed):
        receipt = value.receipt
        diagnostics = ()
    elif type(value) is WorkflowIdempotentRepeat:
        receipt = value.original_receipt
        diagnostics = ()
    else:  # pragma: no cover - guarded by the caller's accepted-result check
        raise TypeError("workflow result is invalid")
    handoff = (
        None
        if value.state is None
        else _handoff_output(derive_workflow_handoff(value.state))
    )
    return ApplyWorkflowSuccess(
        status=value.status,
        receipt=None if receipt is None else _receipt_output(receipt),
        handoff=handoff,
        diagnostics=[_workflow_diagnostic_output(item) for item in diagnostics],
    )


def _success_result(
    value: BaseModel,
    *,
    content: list[Any] | None = None,
) -> CallToolResult:
    return CallToolResult(
        content=content or [TextContent(text=f"Course Compiler status: {value.status}.")],
        structured_content=value.model_dump(mode="json"),
    )


def _fixed_failure(code: str, classification: str, message: str) -> CallToolResult:
    structured = FailureOutput(
        status="error",
        diagnostics=[
            WireDiagnostic(code=code, classification=classification, message=message)
        ],
    )
    return CallToolResult(
        content=[TextContent(text=f"Course Compiler error: {code}.")],
        structured_content=structured.model_dump(mode="json"),
        is_error=True,
    )


def _observe_file_host(
    reference: OpenAIFileReference,
    observation_path: Path | None,
) -> bool:
    """Persist only the first valid lowercase HTTPS host for a harmless probe."""

    if observation_path is None:
        return False
    try:
        parsed = urlsplit(reference.download_url)
        host = parsed.hostname
        if (
            parsed.scheme.lower() != "https"
            or host is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.fragment
        ):
            return False
        hostname = host.lower()
        FileDownloadPolicy((hostname,))
        payload = (hostname + "\n").encode("ascii")
        observation_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                observation_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            return observation_path.read_bytes() == payload
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        return True
    except (OSError, TypeError, UnicodeError, ValueError):
        return False


def _adapter_failure(code: str) -> CallToolResult:
    return _fixed_failure(code, "adapter", _ADAPTER_MESSAGES[code])


def _domain_failure(value: object) -> CallToolResult:
    try:
        diagnostics = value.diagnostics
        wire = []
        for diagnostic in diagnostics:
            classification = getattr(
                diagnostic,
                "classification",
                getattr(diagnostic, "category", "application"),
            )
            wire.append(
                WireDiagnostic(
                    code=diagnostic.code,
                    classification=classification,
                    message=diagnostic.message,
                )
            )
        if not wire:
            raise ValueError("missing diagnostics")
        structured = FailureOutput(status="error", diagnostics=wire)
        return CallToolResult(
            content=[TextContent(text=f"Course Compiler error: {wire[0].code}.")],
            structured_content=structured.model_dump(mode="json"),
            is_error=True,
        )
    except Exception:
        return _adapter_failure("mcp_adapter_exception")


__all__ = [
    "DEFAULT_MCP_HOST",
    "DEFAULT_MCP_PATH",
    "DEFAULT_MCP_PORT",
    "MCP_ADAPTER_VERSION",
    "McpAdapterConfig",
    "create_mcp_server",
    "create_streamable_http_app",
    "run_mcp_server",
]
