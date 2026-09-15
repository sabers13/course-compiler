"""T049 coarse operations + scheduler + semantic-work protocol.

Coarse workflow sits above fine-grained T003 and drives every transition
only through the accepted T027 persisted application boundary
(``apply_persisted_course_workflow_request``). This module never calls the
pure ``apply_workflow_request`` state machine directly and never writes
``WorkflowState`` itself: T003 remains the only state machine and T027
remains the accepted persisted write boundary.

Authoritative write sequences (no broad cross-store transaction):

1. ``submit_semantic_result``:
   ``validate result -> persist immutable artifacts/documents ->
   T027 WorkflowState transition -> mark semantic envelope accepted ->
   reconcile Job projection``.
   A crash after artifact/document persistence but before the WorkflowState
   transition leaves only harmless content-addressed orphans; the previous
   WorkflowState revision remains authoritative and exact retry reuses the
   persisted content idempotently.

2. Owner decisions (priority/map) are explicit transport-neutral operations.
   The scheduler/executor never infers owner approval from diagnostics,
   persistence success, or semantic validity.

3. Deterministic lecture validation is an evidence-bearing application check:
   validation artifact bytes encode the actually performed checks, the
   disposition follows those checks, and everything persists through T027.
"""

from __future__ import annotations

from .lecture_plan import lecture_ids_from_artifact

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal, TypeAlias

from .course import CourseReference, COURSE_REFERENCE_VERSION
from .course_persistence import LocalCourseStore, CoursePersistenceFailure, CourseRecord
from .course_job_persistence import LocalCourseJobStore, CourseJobRecord, CourseJobPersistenceFailure, derive_job_status
from .course_workflow_persistence import LocalCourseWorkflowAssociationStore
from .workflow import (
    WorkflowState,
    WorkflowAdvanced,
    WorkflowBlocked,
    WorkflowFailed,
    WorkflowIdempotentRepeat,
    WorkflowRejected,
    WorkflowTransitionRequest,
    InitializeWorkflowRequest,
    InitializeWorkflow,
    RecordSourceAssessment,
    ApprovePriorityBasis,
    RejectPriorityBasis,
    LectureMapRecord,
    RecordLectureMap,
    ApproveLectureMap,
    RejectLectureMap,
    SubmitLectureCandidate,
    ReopenLecturesForCorrection,
    RecordCandidateValidation,
    ValidationRecord,
    WorkflowPolicySet,
    WorkflowArtifactReference,
    SourceEvidenceReference,
    PolicyReference,
    priority_subject_sha256,
    map_subject_sha256,
    candidate_subject_sha256,
)
from .workflow_persistence import LocalWorkflowStateStore, WorkflowPersistenceFailure
from .workflow_artifact_persistence import LocalWorkflowArtifactStore, WorkflowArtifactPayload
from .lecture_document_persistence import LocalLectureDocumentStore
from .policy_persistence import LocalPolicyContentStore
from .source_persistence import LocalSourceEvidenceStore
from .contracts import LectureDocument, SourceProvenance
from .rendering import DocumentReference, document_reference
from .semantic_work import (
    SEMANTIC_KINDS,
    SEMANTIC_WORK_REQUEST_VERSION,
    SEMANTIC_WORK_RESULT_VERSION,
    SemanticWorkRequest,
    SemanticWorkResult,
    ProducedArtifactSpec,
    ProducedDocumentSpec,
    SemanticDiagnostic,
)
from .semantic_work_persistence import (
    LocalSemanticWorkStore,
    SemanticAcceptedRecord,
    SemanticWorkPersistenceFailure,
    envelope_for_result,
)
from .workflow_policy import ARTIFACT_PRODUCERS, POLICY_VERSIONS, WORKFLOW_TRANSITION_VERSION

__all__ = [
    "SemanticOperationDiagnostic",
    "SemanticOperationFailure",
    "SemanticOperationResult",
    "SchedulerDecision",
    "start_generation",
    "request_semantic_work",
    "observe_pending_work",
    "submit_semantic_result",
    "submit_owner_priority_decision",
    "submit_owner_map_decision",
    "determine_next_semantic_kind",
    "determine_scheduler_decision",
    "derive_review_progress",
    "recover_review_reopen",
]

_DIAGNOSTICS = {
    "invalid_semantic_input": ("input", "The semantic operation input is invalid."),
    "course_not_found": ("storage", "The course was not found."),
    "course_store_failed": ("storage", "The course store failed."),
    "job_not_found": ("storage", "The semantic job was not found."),
    "job_store_failed": ("storage", "The course-job store failed."),
    "workflow_store_failed": ("storage", "The workflow-state store failed."),
    "association_store_failed": ("storage", "The course-workflow association store failed."),
    "semantic_store_failed": ("storage", "The semantic-work store failed."),
    "source_store_failed": ("storage", "The source-evidence store failed."),
    "policy_store_failed": ("storage", "The policy-content store failed."),
    "artifact_store_failed": ("storage", "The workflow-artifact store failed."),
    "document_store_failed": ("storage", "The lecture-document store failed."),
    "no_semantic_work": ("input", "No semantic work is currently available."),
    "owner_decision_required": ("input", "An explicit owner decision is required before further work."),
    "deterministic_action_pending": ("input", "A deterministic follow-up action is pending."),
    "invalid_review_verdict": ("input", "The semantic review verdict is invalid."),
    "invalid_owner_decision": ("input", "The owner decision input is invalid."),
    "invalid_workflow_state": ("input", "The workflow state is invalid for semantic work."),
    "request_identity_mismatch": ("identity", "The semantic request identity is mismatched."),
    "result_identity_mismatch": ("identity", "The semantic result identity is mismatched."),
    "stale_revision": ("revision", "The semantic expected revision is stale."),
    "lease_conflict": ("revision", "The semantic lease is still active."),
    "subject_hash_mismatch": ("input", "The semantic subject hash is mismatched."),
    "invalid_produced_payload": ("input", "The semantic produced payload is invalid."),
    "invalid_subject_hash": ("input", "The semantic subject hash is invalid."),
    "workflow_rejected": ("validation", "The workflow rejected the semantic result."),
    "semantic_operation_exception": ("application", "The semantic operation failed."),
}


@dataclass(frozen=True, slots=True)
class SemanticOperationDiagnostic:
    code: str
    classification: Literal["input", "storage", "identity", "revision", "validation", "application"]
    message: str

    def __post_init__(self) -> None:
        if self.code not in _DIAGNOSTICS:
            raise ValueError("semantic operation diagnostic code is not registered")
        classification, message = _DIAGNOSTICS[self.code]
        if self.classification != classification or self.message != message:
            raise ValueError("semantic operation diagnostic fields are not registered")


@dataclass(frozen=True, slots=True)
class SemanticOperationFailure:
    status: Literal["semantic_operation_failed"]
    diagnostics: tuple[SemanticOperationDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "semantic_operation_failed":
            raise ValueError("semantic operation failure status is invalid")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) != 1
            or not _is_valid_diagnostic(self.diagnostics[0])
        ):
            raise ValueError("semantic operation failure diagnostics are invalid")


SemanticOperationResult: TypeAlias = (
    str | SemanticWorkRequest | SemanticAcceptedRecord | WorkflowAdvanced | WorkflowBlocked | WorkflowFailed | WorkflowIdempotentRepeat | WorkflowRejected | SemanticOperationFailure
)

_MAX_REVISION = 9_223_372_036_854_775_807
_MAX_PRODUCED_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class SchedulerDecision:
    """Interpretation of one accepted T003 WorkflowState for coarse scheduling.

    This interprets accepted T003 authority; it never invents workflow state.
    ``decision`` is one of: ``need_semantic`` (with ``kind``), ``need_owner``
    (with ``gate``), ``need_deterministic`` (with ``action``), ``blocked``,
    ``failed``, ``completed``, ``invalid``.
    """

    decision: str
    kind: str | None
    gate: str | None
    action: str | None

    def __post_init__(self) -> None:
        if type(self.decision) is not str or self.decision not in (
            "need_semantic",
            "need_owner",
            "need_deterministic",
            "blocked",
            "failed",
            "completed",
            "invalid",
        ):
            raise ValueError("scheduler decision is invalid")
        if self.decision == "need_semantic":
            if type(self.kind) is not str or self.kind not in SEMANTIC_KINDS:
                raise ValueError("scheduler semantic kind is invalid")
            if self.gate is not None or self.action is not None:
                raise ValueError("scheduler decision fields are invalid")
        elif self.decision == "need_owner":
            if self.gate not in ("priority", "map"):
                raise ValueError("scheduler owner gate is invalid")
            if self.kind is not None or self.action is not None:
                raise ValueError("scheduler decision fields are invalid")
        elif self.decision == "need_deterministic":
            if self.action not in ("record_candidate_validation", "reopen_lectures_for_correction"):
                raise ValueError("scheduler deterministic action is invalid")
            if self.kind is not None or self.gate is not None:
                raise ValueError("scheduler decision fields are invalid")
        else:
            if self.kind is not None or self.gate is not None or self.action is not None:
                raise ValueError("scheduler decision fields are invalid")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid_safe_id(value: object) -> bool:
    import re

    return type(value) is str and value not in (".", "..") and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}\Z", value) is not None


def _valid_created_at(value: object) -> bool:
    import re

    return type(value) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z", value) is not None


def _parse_created_at(value: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError("created_at timestamp is invalid")
    text = value[:-1]
    parsed: datetime | None = None
    for format in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, format)
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValueError("created_at timestamp is invalid")
    return parsed.replace(tzinfo=timezone.utc)


def _is_lease_active(lease_expires_at: str, now_iso: str) -> bool:
    return _parse_created_at(now_iso) < _parse_created_at(lease_expires_at)


def _generate_safe_id(prefix: str = "c") -> str:
    raw = uuid.uuid4().hex[:15]
    return f"{prefix}{raw}"


def _failure(code: str) -> SemanticOperationFailure:
    classification, message = _DIAGNOSTICS[code]
    return SemanticOperationFailure(status="semantic_operation_failed", diagnostics=(SemanticOperationDiagnostic(code, classification, message),))


def _persistence_failure_code(value: object) -> str | None:
    try:
        diags = value.diagnostics  # type: ignore[attr-defined]
        if type(diags) is tuple and len(diags) == 1:
            c = diags[0].code
            return c if type(c) is str else None
    except Exception:
        return None
    return None


def _require_store(name: str, value: object, expected: type) -> None:
    if type(value) is not expected:
        raise TypeError(f"{name} must be exactly {expected.__name__}")


# ---------------------------------------------------------------------------
# Tracked policy authority (Finding D)
# ---------------------------------------------------------------------------

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_POLICY_SOURCE_PATHS = {
    "source_assessment": Path("SKILL.md"),
    "priority_basis": Path("SKILL.md"),
    "lecture_mapping": Path("SKILL.md"),
    "lecture_production": Path("references/lecture-authoring.md"),
    "lecture_validation": Path("references/lecture-authoring.md"),
    "workflow_handoff": Path("references/coarse-workflow.md"),
}
_SKILL_ROOT = _REPOSITORY_ROOT / "skills" / "course-compiler"


def _read_tracked_policy_bytes(skill_root: Path) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    for kind, relative in _POLICY_SOURCE_PATHS.items():
        try:
            payloads[kind] = (skill_root / relative).read_bytes()
        except OSError:
            raise ValueError("tracked_policy_source_unavailable") from None
    return payloads


def _ensure_tracked_policies(
    policy_store: LocalPolicyContentStore, *, skill_root: Path = _SKILL_ROOT
) -> WorkflowPolicySet | SemanticOperationFailure:
    """Seed production workflow policies from exact tracked Skill bytes.

    No per-course invented prose, no network, no provider dependency. All
    courses share the same content-addressed policy references, so restarts
    and concurrent courses are deterministic. ``ScriptProvider`` synthetic
    semantic output never flows into policy authority.
    """

    try:
        payloads = _read_tracked_policy_bytes(skill_root)
    except ValueError:
        return _failure("policy_store_failed")
    refs: dict[str, PolicyReference] = {}
    for kind in _POLICY_SOURCE_PATHS:
        payload = payloads[kind]
        try:
            ref = PolicyReference(kind, POLICY_VERSIONS[kind], hashlib.sha256(payload).hexdigest())  # type: ignore[arg-type]
        except Exception:
            return _failure("policy_store_failed")
        saved = policy_store.save(ref, payload)
        from course_compiler.policy_persistence import PolicyContentPayload, PolicyPersistenceFailure

        if isinstance(saved, PolicyContentPayload):
            if saved.reference != ref:
                return _failure("policy_store_failed")
        elif isinstance(saved, PolicyPersistenceFailure):
            code = _persistence_failure_code(saved)
            if code == "immutable_identity_conflict":
                # Same reference bound to different bytes is impossible for
                # sha-bound refs; treat stored conflict as store failure.
                return _failure("policy_store_failed")
            return _failure("policy_store_failed")
        else:
            return _failure("policy_store_failed")
        refs[kind] = ref
    try:
        return WorkflowPolicySet(
            source_assessment=refs["source_assessment"],
            priority_basis=refs["priority_basis"],
            lecture_mapping=refs["lecture_mapping"],
            lecture_production=refs["lecture_production"],
            lecture_validation=refs["lecture_validation"],
            workflow_handoff=refs["workflow_handoff"],
        )
    except Exception:
        return _failure("invalid_semantic_input")


# ---------------------------------------------------------------------------
# Scheduler: explicit complete mapping (Finding M)
# ---------------------------------------------------------------------------

def determine_scheduler_decision(state: WorkflowState | None) -> SchedulerDecision:
    """Map one accepted WorkflowState to a coarse scheduling decision.

    Unknown/impossible stage/disposition pairs return ``invalid`` (fail
    closed); they are never collapsed into ordinary ``no_semantic_work``.
    """

    if state is None or type(state) is not WorkflowState:
        return SchedulerDecision("invalid", None, None, None)
    try:
        stage = state.stage
        disp = state.disposition
    except Exception:
        return SchedulerDecision("invalid", None, None, None)
    if disp == "blocked":
        return SchedulerDecision("blocked", None, None, None)
    if disp == "failed":
        return SchedulerDecision("failed", None, None, None)
    if stage == "source_assessment" and disp == "ready":
        return SchedulerDecision("need_semantic", "source_assessment", None, None)
    if stage == "priority_approval" and disp == "awaiting_approval":
        return SchedulerDecision("need_semantic", "exam_priority_assessment", None, None)
    if stage == "lecture_mapping" and disp == "ready":
        return SchedulerDecision("need_semantic", "lecture_map_generation", None, None)
    if stage == "map_approval" and disp == "awaiting_approval":
        # Owner decision only: the scheduler must never auto-approve the map.
        return SchedulerDecision("need_owner", None, "map", None)
    if stage == "lecture_production" and disp == "ready":
        try:
            corrections = [p for p in state.lecture_progress if p.status == "correction_required"]
            pending = [p for p in state.lecture_progress if p.status in ("pending", "retry_required")]
        except Exception:
            return SchedulerDecision("invalid", None, None, None)
        if corrections and state.lecture_map is not None and state.lecture_map.status == "approved":
            return SchedulerDecision("need_semantic", "semantic_correction", None, None)
        if pending and state.lecture_map is not None and state.lecture_map.status == "approved":
            return SchedulerDecision("need_semantic", "lecture_generation", None, None)
        return SchedulerDecision("invalid", None, None, None)
    if stage == "lecture_validation" and disp == "ready":
        return SchedulerDecision("need_deterministic", None, None, "record_candidate_validation")
    if stage == "completed" and disp == "completed":
        return SchedulerDecision("completed", None, None, None)
    return SchedulerDecision("invalid", None, None, None)


def determine_next_semantic_kind(state: WorkflowState | None) -> str | None:
    """Return the next SemanticKind, or None for owner/deterministic/blocked/failed/completed/invalid."""

    try:
        decision = determine_scheduler_decision(state)
    except Exception:
        return None
    if decision.decision == "need_semantic":
        return decision.kind
    return None


def _determine_next_kind_with_metadata(state: WorkflowState | None) -> tuple[str | None, tuple[str, ...]]:
    """Return (kind, input_refs) for next semantic work."""
    kind = determine_next_semantic_kind(state)
    if kind is None:
        return None, ()
    if state is None:
        return None, ()
    if kind == "source_assessment":
        refs = tuple(s.content_sha256 for s in state.source_evidence)
        return kind, refs
    if kind == "exam_priority_assessment":
        try:
            if state.priority_basis is not None:
                exp = priority_subject_sha256(state.priority_basis, state.policies.priority_basis)
                return kind, (exp,)
        except Exception:
            pass
        return kind, ()
    if kind == "lecture_map_generation":
        context = state.map_reopen_context
        reserved = () if context is None else context.baseline_lecture_ids
        policy_sha = state.policies.lecture_mapping.content_sha256
        return kind, (policy_sha, *reserved)
    if kind in ("lecture_generation", "semantic_correction"):
        pending = [p for p in state.lecture_progress if p.status in ("pending", "retry_required")]
        if kind == "semantic_correction":
            pending = [p for p in state.lecture_progress if p.status in ("correction_required", "retry_required")]
        if pending:
            return kind, (pending[0].lecture_id,)
        return None, ()
    return kind, ()


# ---------------------------------------------------------------------------
# start_generation (Finding O)
# ---------------------------------------------------------------------------

def start_generation(
    course_id: str,
    *,
    course_store: LocalCourseStore,
    job_store: LocalCourseJobStore,
    workflow_store: LocalWorkflowStateStore,
    association_store: LocalCourseWorkflowAssociationStore,
    source_store: LocalSourceEvidenceStore,
    policy_store: LocalPolicyContentStore,
    artifact_store: LocalWorkflowArtifactStore,
    document_store: LocalLectureDocumentStore,
    clock: str | None = None,
    job_id_generator=None,
    operation_id_generator=None,
) -> str | SemanticOperationFailure:
    """Sole Job-creation path. Idempotent resume of the active Job.

    At most one Job per Course exists (the job store binds workflow_id
    UNIQUE). Retries/resume never create duplicates: an existing Job for the
    course workflow is reconciled from authoritative WorkflowState and its id
    returned, including terminal history (T050 owns regeneration). A crash
    after Job persistence but before the Course pointer update is a
    recoverable window: the next call finds the Job by workflow and repairs
    the pointer.
    """

    if type(course_id) is not str:
        raise TypeError("course_id must be exactly str")
    _require_store("course_store", course_store, LocalCourseStore)
    _require_store("job_store", job_store, LocalCourseJobStore)
    _require_store("workflow_store", workflow_store, LocalWorkflowStateStore)
    _require_store("association_store", association_store, LocalCourseWorkflowAssociationStore)
    _require_store("source_store", source_store, LocalSourceEvidenceStore)
    _require_store("policy_store", policy_store, LocalPolicyContentStore)

    try:
        course_result = course_store.load(course_id)
        if isinstance(course_result, CoursePersistenceFailure):
            code = _persistence_failure_code(course_result)
            if code == "course_not_found":
                return _failure("course_not_found")
            return _failure("course_store_failed")
        if not isinstance(course_result, CourseRecord):
            return _failure("course_store_failed")
        course: CourseRecord = course_result  # type: ignore[assignment]

        if not course.source_refs:
            return _failure("invalid_semantic_input")

        workflow_id = course.workflow_id

        # Idempotency: a current pointer to a real Job always wins; reconcile
        # the projection from WorkflowState first (WorkflowState wins).
        if course.current_job_id is not None:
            existing = job_store.load(course.current_job_id)
            if isinstance(existing, CourseJobRecord):
                if existing.workflow_id != workflow_id:
                    return _failure("job_store_failed")
                reconciled = _reconcile_job_projection(job_store, existing, workflow_store)
                if isinstance(reconciled, SemanticOperationFailure):
                    return reconciled
                final = reconciled if isinstance(reconciled, CourseJobRecord) else existing
                return final.job_id

        # Recoverable crash window: Job persisted but Course pointer not yet
        # updated. Find by workflow, repair the pointer, reconcile, return.
        by_wf = job_store.load_by_workflow(workflow_id)
        if isinstance(by_wf, CourseJobRecord):
            _repair_course_pointer(course_store, course, by_wf.job_id)
            reconciled = _reconcile_job_projection(job_store, by_wf, workflow_store)
            if isinstance(reconciled, SemanticOperationFailure):
                return reconciled
            final = reconciled if isinstance(reconciled, CourseJobRecord) else by_wf
            return final.job_id

        now_iso = clock if clock is not None else _now_iso()
        if not _valid_created_at(now_iso):
            return _failure("invalid_semantic_input")
        job_id = job_id_generator() if callable(job_id_generator) else _generate_safe_id("j")
        if not _valid_safe_id(job_id):
            return _failure("invalid_semantic_input")
        operation_id = operation_id_generator() if callable(operation_id_generator) else _generate_safe_id("op")
        if not _valid_safe_id(operation_id):
            return _failure("invalid_semantic_input")

        policies = _ensure_tracked_policies(policy_store)
        if isinstance(policies, SemanticOperationFailure):
            return policies  # type: ignore[return-value]

        from course_compiler.course_workflow import CourseWorkflowAssociation, COURSE_WORKFLOW_ASSOCIATION_VERSION

        course_ref = CourseReference(course.reference_version, course.course_id)
        association = CourseWorkflowAssociation(COURSE_WORKFLOW_ASSOCIATION_VERSION, course_ref, workflow_id)
        assoc_load = association_store.load(association)
        from course_compiler.course_workflow_persistence import CourseWorkflowPersistenceFailure

        if isinstance(assoc_load, CourseWorkflowPersistenceFailure):
            code = _persistence_failure_code(assoc_load)
            if code == "association_not_found":
                save_assoc = association_store.save(association)
                if isinstance(save_assoc, CourseWorkflowPersistenceFailure):
                    return _failure("association_store_failed")
            elif code is not None:
                return _failure("association_store_failed")

        existing_ws = workflow_store.load(workflow_id)
        if isinstance(existing_ws, WorkflowState):
            status, stage, disp, fail_code = derive_job_status(existing_ws)
            job_record = CourseJobRecord(
                job_id=job_id,
                course_reference=course_ref,
                workflow_id=workflow_id,
                created_at=now_iso,
                created_revision=existing_ws.revision,
                current_revision=existing_ws.revision,
                metadata_revision=0,
                status=status,  # type: ignore[arg-type]
                current_stage=stage,
                current_disposition=disp,
                ai_mode=course.ai_mode,  # type: ignore[arg-type]
                quality_mode=course.quality_mode,  # type: ignore[arg-type]
                retry_count=0,
                failure_code=fail_code,
            )
            saved_job = job_store.save(job_record)
            if isinstance(saved_job, CourseJobPersistenceFailure):
                existing_by_wf = job_store.load_by_workflow(workflow_id)
                if isinstance(existing_by_wf, CourseJobRecord):
                    _repair_course_pointer(course_store, course, existing_by_wf.job_id)
                    return existing_by_wf.job_id
                return _failure("job_store_failed")
            _repair_course_pointer(course_store, course, job_id)
            return job_id

        for ref in course.source_refs:
            loaded_src = source_store.load(ref)
            from course_compiler.source_persistence import SourcePersistenceFailure

            if isinstance(loaded_src, SourcePersistenceFailure):
                return _failure("source_store_failed")

        init_payload = InitializeWorkflow("initialize_workflow", policies, course.source_refs)  # type: ignore[arg-type]
        init_req = InitializeWorkflowRequest(WORKFLOW_TRANSITION_VERSION, workflow_id, operation_id, init_payload)  # type: ignore[arg-type]
        from course_compiler.course_workflow_transition_operations import apply_persisted_course_workflow_request

        result = apply_persisted_course_workflow_request(
            association,
            init_req,
            association_store=association_store,
            workflow_store=workflow_store,
            policy_store=policy_store,
            source_store=source_store,
            artifact_store=artifact_store,
            document_store=document_store,
        )
        if isinstance(result, WorkflowRejected):
            return _failure("invalid_semantic_input")
        if isinstance(result, WorkflowIdempotentRepeat):
            ws = result.state
        elif isinstance(result, WorkflowAdvanced):
            ws = result.state
        else:
            return _failure("workflow_store_failed")
        status2, stage2, disp2, fail_code2 = derive_job_status(ws)
        job_record2 = CourseJobRecord(
            job_id=job_id,
            course_reference=course_ref,
            workflow_id=workflow_id,
            created_at=now_iso,
            created_revision=ws.revision,
            current_revision=ws.revision,
            metadata_revision=0,
            status=status2,  # type: ignore[arg-type]
            current_stage=stage2,
            current_disposition=disp2,
            ai_mode=course.ai_mode,  # type: ignore[arg-type]
            quality_mode=course.quality_mode,  # type: ignore[arg-type]
            retry_count=0,
            failure_code=fail_code2,
        )
        saved_job2 = job_store.save(job_record2)
        if isinstance(saved_job2, CourseJobPersistenceFailure):
            existing2 = job_store.load_by_workflow(workflow_id)
            if isinstance(existing2, CourseJobRecord):
                _repair_course_pointer(course_store, course, existing2.job_id)
                return existing2.job_id
            return _failure("job_store_failed")
        _repair_course_pointer(course_store, course, job_id)
        return job_id
    except Exception:
        return _failure("semantic_operation_exception")


def _repair_course_pointer(course_store: LocalCourseStore, course: CourseRecord, job_id: str) -> None:
    """Best-effort Course pointer repair for the crash window.

    If the update fails (e.g. concurrent writer advanced metadata), the Job
    row itself remains durable and the next ``start_generation`` repairs the
    pointer via the by-workflow lookup. The window is therefore recoverable,
    never permanently inconsistent.
    """

    if course.current_job_id == job_id:
        return
    try:
        updated = CourseRecord(
            course.reference_version,
            course.course_id,
            course.created_at,
            course.title,
            course.ai_mode,  # type: ignore[arg-type]
            course.quality_mode,  # type: ignore[arg-type]
            course.owner_scope,  # type: ignore[arg-type]
            course.metadata_revision + 1,
            course.source_refs,
            course.workflow_id,
            job_id,
            course.course_guidance,
        )
        course_store.save(updated)
    except Exception:
        pass


def _reconcile_job_projection(
    job_store: LocalCourseJobStore, job: CourseJobRecord, workflow_store: LocalWorkflowStateStore
) -> CourseJobRecord | SemanticOperationFailure:
    """Recompute the Job projection from authoritative WorkflowState (State wins)."""

    try:
        ws_result = workflow_store.load(job.workflow_id)
    except Exception:
        return _failure("workflow_store_failed")
    if isinstance(ws_result, WorkflowPersistenceFailure):
        return _failure("workflow_store_failed")
    if not isinstance(ws_result, WorkflowState):
        return _failure("workflow_store_failed")
    return _save_job_projection(job_store, job, ws_result)


def _save_job_projection(
    job_store: LocalCourseJobStore, job: CourseJobRecord, ws: WorkflowState
) -> CourseJobRecord | SemanticOperationFailure:
    # The authoritative T050 build pointers belong to the build owner, never to
    # semantic projection. Carry them through unchanged and feed them back into
    # the derivation, so projecting a workflow-completed job neither wipes the
    # pointers nor downgrades an already-completed job to deterministic_building.
    completed_build_id = job.completed_build_id
    completed_build_sha256 = job.completed_build_sha256
    status, stage, disp, fail_code = derive_job_status(
        ws,
        completed_build_id=completed_build_id,
        completed_build_sha256=completed_build_sha256,
    )
    if (
        job.status == status
        and job.current_stage == stage
        and job.current_disposition == disp
        and job.current_revision == ws.revision
        and job.failure_code == fail_code
    ):
        return job
    try:
        updated = CourseJobRecord(
            job_id=job.job_id,
            course_reference=job.course_reference,
            workflow_id=job.workflow_id,
            created_at=job.created_at,
            created_revision=job.created_revision,
            current_revision=ws.revision,
            metadata_revision=job.metadata_revision + 1,
            status=status,  # type: ignore[arg-type]
            current_stage=stage,
            current_disposition=disp,
            ai_mode=job.ai_mode,  # type: ignore[arg-type]
            quality_mode=job.quality_mode,  # type: ignore[arg-type]
            retry_count=job.retry_count,
            failure_code=fail_code,
            completed_build_id=completed_build_id,
            completed_build_sha256=completed_build_sha256,
        )
    except Exception:
        return _failure("invalid_semantic_input")
    saved = job_store.save(updated)
    if isinstance(saved, CourseJobRecord):
        return saved
    # One retry on concurrent metadata advancement: reload and project again.
    reloaded = job_store.load(job.job_id)
    if isinstance(reloaded, CourseJobRecord):
        # Re-derive against the reloaded record's own pointers: a concurrent
        # build may have completed the job while this projection was in flight.
        reloaded_build_id = reloaded.completed_build_id
        reloaded_build_sha256 = reloaded.completed_build_sha256
        status, stage, disp, fail_code = derive_job_status(
            ws,
            completed_build_id=reloaded_build_id,
            completed_build_sha256=reloaded_build_sha256,
        )
        if (
            reloaded.status == status
            and reloaded.current_stage == stage
            and reloaded.current_disposition == disp
            and reloaded.current_revision == ws.revision
            and reloaded.failure_code == fail_code
        ):
            return reloaded
        try:
            retry = CourseJobRecord(
                job_id=reloaded.job_id,
                course_reference=reloaded.course_reference,
                workflow_id=reloaded.workflow_id,
                created_at=reloaded.created_at,
                created_revision=reloaded.created_revision,
                current_revision=ws.revision,
                metadata_revision=reloaded.metadata_revision + 1,
                status=status,  # type: ignore[arg-type]
                current_stage=stage,
                current_disposition=disp,
                ai_mode=reloaded.ai_mode,  # type: ignore[arg-type]
                quality_mode=reloaded.quality_mode,  # type: ignore[arg-type]
                retry_count=reloaded.retry_count,
                failure_code=fail_code,
                completed_build_id=reloaded_build_id,
                completed_build_sha256=reloaded_build_sha256,
            )
        except Exception:
            return _failure("invalid_semantic_input")
        saved2 = job_store.save(retry)
        if isinstance(saved2, CourseJobRecord):
            return saved2
    return _failure("job_store_failed")


# ---------------------------------------------------------------------------
# request_semantic_work / observe_pending_work
# ---------------------------------------------------------------------------

def observe_pending_work(
    job_id: str,
    *,
    job_store: LocalCourseJobStore,
    workflow_store: LocalWorkflowStateStore,
    semantic_store: LocalSemanticWorkStore,
    clock: str | None = None,
) -> SemanticWorkRequest | SemanticOperationFailure | None:
    """Read-only observation of the current semantic request (no mutation).

    Never acquires, renews, or steals a lease. Returns the current-bound
    pending/leased request, or None when no current semantic request exists.
    """

    if type(job_id) is not str:
        raise TypeError("job_id must be exactly str")
    if not _valid_safe_id(job_id):
        return _failure("invalid_semantic_input")
    _require_store("job_store", job_store, LocalCourseJobStore)
    _require_store("workflow_store", workflow_store, LocalWorkflowStateStore)
    _require_store("semantic_store", semantic_store, LocalSemanticWorkStore)
    try:
        now_iso = clock if clock is not None else _now_iso()
        if not _valid_created_at(now_iso):
            return _failure("invalid_semantic_input")
        loaded = _load_job_and_workflow(job_store, workflow_store, job_id)
        if isinstance(loaded, SemanticOperationFailure):
            return loaded
        job, workflow_state = loaded
        current = semantic_store.load_current_for_job(
            job_id, workflow_id=job.workflow_id, expected_revision=workflow_state.revision
        )
        if isinstance(current, SemanticWorkPersistenceFailure):
            return _failure("semantic_store_failed")
        return current
    except Exception:
        return _failure("semantic_operation_exception")


# An owner approval gate is open only when the semantic evidence feeding it
# has actually been accepted. WorkflowState alone cannot distinguish "the
# proposal exists but its own semantic turn is still pending" from "the turn
# is accepted and only the owner decision remains": both read
# priority_approval/awaiting_approval with a proposed basis at the same
# revision. The distinguishing authority is the accepted semantic record for
# the current revision and kind, which is exactly the rule
# ``request_semantic_work`` already applies when it reports
# ``owner_decision_required``.
_OWNER_GATE_BY_STAGE = {"priority_approval": "priority", "map_approval": "map"}


def observe_owner_decision_gate(
    job_id: str,
    *,
    job_store: LocalCourseJobStore,
    workflow_store: LocalWorkflowStateStore,
    semantic_store: LocalSemanticWorkStore,
) -> str | SemanticOperationFailure | None:
    """Return the open owner gate (``priority``/``map``), else ``None``.

    Read-only observation: it never acquires, renews, or steals a lease and
    never writes WorkflowState. Fails closed — an unopened gate is reported
    as ``None`` rather than guessed — so a UI built on it cannot offer an
    owner decision before the evidence for that decision exists.
    """

    if type(job_id) is not str:
        raise TypeError("job_id must be exactly str")
    if not _valid_safe_id(job_id):
        return _failure("invalid_semantic_input")
    _require_store("job_store", job_store, LocalCourseJobStore)
    _require_store("workflow_store", workflow_store, LocalWorkflowStateStore)
    _require_store("semantic_store", semantic_store, LocalSemanticWorkStore)
    try:
        loaded = _load_job_and_workflow(job_store, workflow_store, job_id)
        if isinstance(loaded, SemanticOperationFailure):
            return loaded
        _job, workflow_state = loaded

        decision = determine_scheduler_decision(workflow_state)
        if decision.decision == "need_owner":
            # The scheduler itself demands the owner decision (map gate).
            return decision.gate
        if decision.decision != "need_semantic" or decision.kind is None:
            return None

        # The scheduler still names a semantic kind. The gate is open only if
        # that exact kind has already been accepted for this revision.
        gate = _OWNER_GATE_BY_STAGE.get(workflow_state.stage)
        if gate is None or workflow_state.disposition != "awaiting_approval":
            return None
        accepted = _find_accepted_for_revision(
            semantic_store, job_id, workflow_state.revision, decision.kind
        )
        if isinstance(accepted, SemanticOperationFailure):
            return accepted
        return gate if accepted is not None else None
    except Exception:
        return _failure("semantic_operation_exception")


def request_semantic_work(
    job_id: str,
    *,
    job_store: LocalCourseJobStore,
    workflow_store: LocalWorkflowStateStore,
    semantic_store: LocalSemanticWorkStore,
    association_store: LocalCourseWorkflowAssociationStore | None = None,
    source_store: LocalSourceEvidenceStore | None = None,
    policy_store: LocalPolicyContentStore | None = None,
    artifact_store: LocalWorkflowArtifactStore | None = None,
    document_store: LocalLectureDocumentStore | None = None,
    course_store: LocalCourseStore | None = None,
    clock: str | None = None,
    lease_duration_seconds: int = 300,
    holder_id: str | None = None,
) -> SemanticWorkRequest | SemanticOperationFailure:
    """Acquire (or renew for the same holder) the next semantic unit for a job.

    Lease acquisition is a mutation: exactly one active holder owns a lease at
    a time. A different holder while the lease is active fails with
    ``lease_conflict``; the same holder renews idempotently; after expiry the
    next holder obtains the SAME logical request. This function performs lease
    orchestration only and never writes WorkflowState.

    When a REVIEW-mode course is in the ``correction_reopen_pending`` window
    (workflow completed, accepted corrections verdict durably persisted, but
    the deterministic reopen transition never landed), this function is the
    sole mutation-authorized boundary that may perform the reopen. Read-only
    GET/HEAD observation never mutates: the application's observation path
    reports the pending deterministic action without performing it, and a
    POST here is required to recover. Exactly one reopen lands per accepted
    verdict; subsequent calls see the advanced state and proceed to mint the
    next semantic unit.
    """

    if type(job_id) is not str:
        raise TypeError("job_id must be exactly str")
    if not _valid_safe_id(job_id):
        return _failure("invalid_semantic_input")
    _require_store("job_store", job_store, LocalCourseJobStore)
    _require_store("workflow_store", workflow_store, LocalWorkflowStateStore)
    _require_store("semantic_store", semantic_store, LocalSemanticWorkStore)
    if holder_id is not None and type(holder_id) is not str:
        raise TypeError("holder_id must be exactly str or None")
    if type(lease_duration_seconds) is not int or lease_duration_seconds <= 0:
        return _failure("invalid_semantic_input")

    try:
        now_iso = clock if clock is not None else _now_iso()
        if not _valid_created_at(now_iso):
            return _failure("invalid_semantic_input")
        if holder_id is None or not _valid_safe_id(holder_id):
            return _failure("invalid_semantic_input")
        lease_expires = _iso_plus_seconds(now_iso, lease_duration_seconds)
        loaded = _load_job_and_workflow(job_store, workflow_store, job_id)
        if isinstance(loaded, SemanticOperationFailure):
            return loaded
        job, workflow_state = loaded

        # The mutation-authorized recovery boundary for the correction reopen.
        # An accepted corrections verdict on a still-completed workflow means
        # the chained reopen transition never landed; this is the ONLY place
        # that may drive the recovery transition (read paths / GET/HEAD
        # observation never mutate). Subsequent calls see the advanced state
        # and mint the next semantic unit instead, so the recovery is exact
        # and idempotent across repeats.
        if (
            (workflow_state.stage, workflow_state.disposition) == ("completed", "completed")
            and job.quality_mode == "review"
        ):
            pre_review = derive_review_progress(workflow_state, job, semantic_store)
            if isinstance(pre_review, SemanticOperationFailure):
                return pre_review
            if pre_review == "correction_reopen_pending":
                if (
                    association_store is None
                    or source_store is None
                    or policy_store is None
                    or artifact_store is None
                    or document_store is None
                ):
                    return _failure("semantic_store_failed")
                recovered = recover_review_reopen(
                    job_id, job_store=job_store, workflow_store=workflow_store,
                    semantic_store=semantic_store, association_store=association_store,
                    source_store=source_store, policy_store=policy_store,
                    artifact_store=artifact_store, document_store=document_store,
                )
                if isinstance(recovered, SemanticOperationFailure):
                    return recovered
                reloaded = _load_job_and_workflow(job_store, workflow_store, job_id)
                if isinstance(reloaded, SemanticOperationFailure):
                    return reloaded
                job, workflow_state = reloaded

        reconciled = _reconcile_job_with_semantic(job, workflow_state, semantic_store, now_iso)
        if isinstance(reconciled, SemanticOperationFailure):
            return reconciled
        if isinstance(reconciled, CourseJobRecord) and reconciled != job:
            saved = job_store.save(reconciled)
            if isinstance(saved, CourseJobRecord):
                job = saved
            else:
                latest = job_store.load(job_id)
                if isinstance(latest, CourseJobRecord):
                    job = latest
                else:
                    return _failure("job_store_failed")

        decision = determine_scheduler_decision(workflow_state)
        if decision.decision == "invalid":
            return _failure("invalid_workflow_state")
        if decision.decision == "completed" and job.quality_mode == "review":
            review = derive_review_progress(workflow_state, job, semantic_store)
            if isinstance(review, SemanticOperationFailure):
                return review
            if review == "review_pending":
                decision = SchedulerDecision("need_semantic", "semantic_review", None, None)
            elif review == "correction_reopen_pending":
                decision = SchedulerDecision("need_deterministic", None, None, "reopen_lectures_for_correction")
            else:
                return _failure("no_semantic_work")
        if decision.decision in ("blocked", "failed", "completed"):
            return _failure("no_semantic_work")
        if decision.decision == "need_owner":
            return _failure("owner_decision_required")
        if decision.decision == "need_deterministic":
            return _failure("deterministic_action_pending")
        kind = decision.kind
        assert type(kind) is str

        # An already-accepted result for the current revision/kind means the
        # semantic contribution is done and only the owner decision (or a
        # deterministic follow-up driven by submit) remains: never mint a
        # second logical request over it.
        accepted_match = _find_accepted_for_revision(semantic_store, job_id, workflow_state.revision, kind)
        if isinstance(accepted_match, SemanticOperationFailure):
            return accepted_match
        if accepted_match is not None:
            return _failure("owner_decision_required")

        _, input_refs = _determine_next_kind_with_metadata(workflow_state)
        if kind == "semantic_review":
            # Review evidence identity is the accepted evidence manifest; do
            # not overload request input_refs with lifecycle vocabulary.
            input_refs = ()
        # Re-derive deterministically: _determine_next_kind_with_metadata must
        # agree with the scheduler decision kind.
        current_kind, _ = _determine_next_kind_with_metadata(workflow_state)
        if kind != "semantic_review" and current_kind != kind:
            return _failure("invalid_workflow_state")

        pending = semantic_store.load_current_for_job(
            job_id, workflow_id=job.workflow_id, expected_revision=workflow_state.revision
        )
        if isinstance(pending, SemanticWorkPersistenceFailure):
            return _failure("semantic_store_failed")
        if pending is not None:
            if pending.kind != kind:
                # A current pending row for a different kind means the
                # scheduler moved on without this row advancing; leave it as
                # history evidence and mint the correct-kind request below.
                # (create_request guards same-revision duplicates per kind via
                # the at-most-one rule only across identical revisions, so a
                # stale different-kind row would block: mark it superseded.)
                supersede = semantic_store.mark_rejected(pending.request_id)
                if isinstance(supersede, SemanticWorkPersistenceFailure):
                    return _failure("semantic_store_failed")
            else:
                leased = semantic_store.acquire_lease(
                    pending.request_id, lease_expires_at=lease_expires, now_iso=now_iso, holder_id=holder_id
                )
                if isinstance(leased, SemanticWorkPersistenceFailure):
                    code = _persistence_failure_code(leased)
                    if code == "lease_conflict":
                        return _failure("lease_conflict")
                    return _failure("semantic_store_failed")
                return leased  # type: ignore[return-value]

        request_id = _generate_safe_id("req")
        operation_id = _generate_safe_id("op")
        if not _valid_safe_id(request_id) or not _valid_safe_id(operation_id):
            return _failure("semantic_operation_exception")
        req = SemanticWorkRequest(
            SEMANTIC_WORK_REQUEST_VERSION,  # type: ignore[arg-type]
            request_id,
            job_id,
            workflow_id=job.workflow_id,
            expected_revision=workflow_state.revision,
            operation_id=operation_id,
            kind=kind,  # type: ignore[arg-type]
            input_refs=input_refs,
            created_at=now_iso,
            lease_expires_at=None,
        )
        create_res = semantic_store.create_request(req)
        if isinstance(create_res, SemanticWorkPersistenceFailure):
            code = _persistence_failure_code(create_res)
            if code in ("request_identity_conflict", "immutable_identity_conflict", "operation_identity_conflict"):
                current2 = semantic_store.load_current_for_job(
                    job_id, workflow_id=job.workflow_id, expected_revision=workflow_state.revision
                )
                if isinstance(current2, SemanticWorkRequest):
                    leased2 = semantic_store.acquire_lease(
                        current2.request_id, lease_expires_at=lease_expires, now_iso=now_iso, holder_id=holder_id
                    )
                    if isinstance(leased2, SemanticWorkRequest):
                        return leased2
                    if isinstance(leased2, SemanticWorkPersistenceFailure) and _persistence_failure_code(leased2) == "lease_conflict":
                        return _failure("lease_conflict")
                return _failure("semantic_store_failed")
            return _failure("semantic_store_failed")
        leased_new = semantic_store.acquire_lease(request_id, lease_expires_at=lease_expires, now_iso=now_iso, holder_id=holder_id)
        if isinstance(leased_new, SemanticWorkPersistenceFailure):
            return _failure("semantic_store_failed")
        return leased_new  # type: ignore[return-value]
    except Exception:
        return _failure("semantic_operation_exception")


def _load_job_and_workflow(
    job_store: LocalCourseJobStore, workflow_store: LocalWorkflowStateStore, job_id: str
) -> tuple[CourseJobRecord, WorkflowState] | SemanticOperationFailure:
    job_result = job_store.load(job_id)
    if isinstance(job_result, CourseJobPersistenceFailure):
        code = _persistence_failure_code(job_result)
        if code == "job_not_found":
            return _failure("job_not_found")
        return _failure("job_store_failed")
    if not isinstance(job_result, CourseJobRecord):
        return _failure("job_store_failed")
    ws_result = workflow_store.load(job_result.workflow_id)
    if isinstance(ws_result, WorkflowPersistenceFailure):
        return _failure("workflow_store_failed")
    if not isinstance(ws_result, WorkflowState):
        return _failure("workflow_store_failed")
    return (job_result, ws_result)  # type: ignore[tuple-item]


def _find_accepted_for_revision(
    semantic_store: LocalSemanticWorkStore, job_id: str, revision: int, kind: str
) -> SemanticAcceptedRecord | SemanticOperationFailure | None:
    try:
        all_rows = semantic_store.load_all_for_job(job_id)
    except Exception:
        return _failure("semantic_store_failed")
    if isinstance(all_rows, SemanticWorkPersistenceFailure):
        return _failure("semantic_store_failed")
    for row in all_rows:
        if (
            type(row) is SemanticAcceptedRecord
            and row.request_revision == revision
            and row.kind == kind
        ):
            return row
    return None


def _review_lecture_ids(row: SemanticAcceptedRecord, state: WorkflowState) -> tuple[str, ...] | None:
    if row.kind != "semantic_review" or state.lecture_map is None:
        return None
    prefix = "review_correction_required_"
    ids: list[str] = []
    for diagnostic in row.diagnostics:
        if not diagnostic.code.startswith(prefix) or diagnostic.message != "Semantic review requires correction.":
            return None
        lecture_id = diagnostic.code[len(prefix):]
        if lecture_id not in state.lecture_map.lecture_ids or lecture_id in ids:
            return None
        ids.append(lecture_id)
    return tuple(item for item in state.lecture_map.lecture_ids if item in ids)


def derive_review_progress(
    workflow_state: WorkflowState, job: CourseJobRecord, semantic_store: LocalSemanticWorkStore
) -> str | SemanticOperationFailure:
    """Pure durable REVIEW projection keyed by the current workflow revision."""
    if job.quality_mode != "review":
        return "not_applicable"
    if (workflow_state.stage, workflow_state.disposition) != ("completed", "completed"):
        return "workflow_incomplete"
    row = _find_accepted_for_revision(semantic_store, job.job_id, workflow_state.revision, "semantic_review")
    if isinstance(row, SemanticOperationFailure):
        return row
    if row is None:
        return "review_pending"
    ids = _review_lecture_ids(row, workflow_state)
    if ids is None:
        return _failure("invalid_review_verdict")
    return "semantic_final" if not ids else "correction_reopen_pending"


def recover_review_reopen(job_id: str, *, job_store: LocalCourseJobStore, workflow_store: LocalWorkflowStateStore, semantic_store: LocalSemanticWorkStore, association_store: LocalCourseWorkflowAssociationStore, source_store: LocalSourceEvidenceStore, policy_store: LocalPolicyContentStore, artifact_store: LocalWorkflowArtifactStore, document_store: LocalLectureDocumentStore) -> WorkflowIdempotentRepeat | SemanticOperationFailure | None:
    """Recover the sole deterministic reopen for an accepted corrections verdict."""
    loaded = _load_job_and_workflow(job_store, workflow_store, job_id)
    if isinstance(loaded, SemanticOperationFailure):
        return loaded
    job, state = loaded
    if derive_review_progress(state, job, semantic_store) != "correction_reopen_pending":
        return None
    row = _find_accepted_for_revision(semantic_store, job_id, state.revision, "semantic_review")
    if not isinstance(row, SemanticAcceptedRecord):
        return _failure("semantic_store_failed")
    result = SemanticWorkResult(row.result_version, row.request_id, row.operation_id, row.kind, (), (), row.diagnostics, row.priority_subject_sha256, row.map_subject_sha256, row.candidate_subject_sha256, row.request_revision)
    return _handle_resubmission_after_accept(job_id, result, row, job_store, workflow_store, semantic_store, association_store, source_store, policy_store, artifact_store, document_store, _now_iso())


def _reconcile_job_with_semantic(
    job: CourseJobRecord, workflow_state: WorkflowState, semantic_store: LocalSemanticWorkStore, now_iso: str
) -> CourseJobRecord | SemanticOperationFailure:
    """Compute the expected Job projection including the semantic lease overlay.

    A semantic request overlays ``awaiting_semantic``/``semantic_running``
    only when it is bound to the authoritative workflow: same workflow_id AND
    same expected_revision as the current WorkflowState. Stale rows are
    history/recovery evidence, never current authority. Terminal workflow
    states always win regardless of pending rows.
    """

    stage = workflow_state.stage
    disp = workflow_state.disposition
    # The T050 build pointers are the build owner's authority, never semantic
    # authority. They are carried through unchanged AND fed back into every
    # derivation here, so a semantic read of an already-completed Job can
    # neither downgrade it to deterministic_building nor clear its pointers.
    completed_build_id = job.completed_build_id
    completed_build_sha256 = job.completed_build_sha256
    status, stage_out, disp_out, fail_code = derive_job_status(
        workflow_state,
        completed_build_id=completed_build_id,
        completed_build_sha256=completed_build_sha256,
    )
    current = semantic_store.load_current_for_job(
        job.job_id, workflow_id=job.workflow_id, expected_revision=workflow_state.revision
    )
    if isinstance(current, SemanticWorkPersistenceFailure):
        return _failure("semantic_store_failed")
    if current is not None:
        try:
            is_active = current.lease_expires_at is not None and _is_lease_active(current.lease_expires_at, now_iso)
        except ValueError:
            return _failure("semantic_store_failed")
        if status in ("sourcing", "awaiting_semantic"):
            status = "semantic_running" if is_active else "awaiting_semantic"
        if disp in ("blocked", "failed") or stage == "completed":
            status, stage_out, disp_out, fail_code = derive_job_status(
                workflow_state,
                completed_build_id=completed_build_id,
                completed_build_sha256=completed_build_sha256,
            )
    current_revision = workflow_state.revision
    if (
        job.status == status
        and job.current_stage == stage_out
        and job.current_disposition == disp_out
        and job.current_revision == current_revision
        and job.failure_code == fail_code
    ):
        return job
    try:
        return CourseJobRecord(
            job_id=job.job_id,
            course_reference=job.course_reference,
            workflow_id=job.workflow_id,
            created_at=job.created_at,
            created_revision=job.created_revision,
            current_revision=current_revision,
            metadata_revision=job.metadata_revision + 1,
            status=status,  # type: ignore[arg-type]
            current_stage=stage_out,
            current_disposition=disp_out,
            ai_mode=job.ai_mode,  # type: ignore[arg-type]
            quality_mode=job.quality_mode,  # type: ignore[arg-type]
            retry_count=job.retry_count,
            failure_code=fail_code,
            completed_build_id=completed_build_id,
            completed_build_sha256=completed_build_sha256,
        )
    except Exception:
        return _failure("invalid_semantic_input")


# ---------------------------------------------------------------------------
# Strict result validation (Findings K + L)
# ---------------------------------------------------------------------------

def _check_produced_bounds(result: SemanticWorkResult) -> SemanticOperationFailure | None:
    seen_artifact_ids: set[str] = set()
    for art in result.produced_artifacts:
        if type(art.content_bytes) is not bytes or not art.content_bytes:
            return _failure("invalid_produced_payload")
        if len(art.content_bytes) > _MAX_PRODUCED_BYTES:
            return _failure("invalid_produced_payload")
        if not _valid_safe_id(art.artifact_id):
            return _failure("invalid_produced_payload")
        if art.kind not in ARTIFACT_PRODUCERS:
            return _failure("invalid_produced_payload")
        if art.artifact_id in seen_artifact_ids:
            return _failure("invalid_produced_payload")
        seen_artifact_ids.add(art.artifact_id)
    import re

    for doc in result.produced_documents:
        if not doc.source_text or type(doc.source_text) is not str:
            return _failure("invalid_produced_payload")
        if len(doc.source_text.encode("utf-8")) > _MAX_PRODUCED_BYTES:
            return _failure("invalid_produced_payload")
        if not re.fullmatch(r"l[1-9][0-9]{0,3}\Z", doc.lecture_id):
            return _failure("invalid_produced_payload")
        try:
            from course_compiler.contracts import validate_document, has_validation_errors

            lecture_doc = LectureDocument("lecture-document/v1", doc.lecture_id, int(doc.lecture_id[1:]), doc.source_text, SourceProvenance(hashlib.sha256(doc.source_text.encode()).hexdigest()))
            diags = validate_document(lecture_doc)
            if has_validation_errors(diags):
                return _failure("invalid_produced_payload")
        except Exception:
            return _failure("invalid_produced_payload")
    return None


def _check_kind_shape(result: SemanticWorkResult) -> SemanticOperationFailure | None:
    """Enforce the exact allowed output shape per SemanticKind."""

    kind = result.kind
    artifact_kinds = tuple(a.kind for a in result.produced_artifacts)
    if kind == "source_assessment":
        if sorted(artifact_kinds) != ["evidence_hierarchy", "priority_proposal", "source_assessment"]:
            return _failure("invalid_produced_payload")
        if result.produced_documents:
            return _failure("invalid_produced_payload")
    elif kind == "exam_priority_assessment":
        if sorted(artifact_kinds) != ["evidence_hierarchy", "priority_proposal"]:
            return _failure("invalid_produced_payload")
        if result.produced_documents:
            return _failure("invalid_produced_payload")
    elif kind == "lecture_map_generation":
        if artifact_kinds != ("lecture_map",):
            return _failure("invalid_produced_payload")
        if result.produced_documents:
            return _failure("invalid_produced_payload")
        try:
            lecture_ids = lecture_ids_from_artifact(result.produced_artifacts[0].content_bytes)
            from course_compiler.workflow import _valid_lecture_sequence

            if not _valid_lecture_sequence(lecture_ids):
                return _failure("invalid_produced_payload")
        except Exception:
            return _failure("invalid_produced_payload")
    elif kind in ("lecture_generation", "semantic_correction"):
        if result.produced_artifacts:
            return _failure("invalid_produced_payload")
        if len(result.produced_documents) != 1:
            return _failure("invalid_produced_payload")
    elif kind in ("visual_selection", "visual_placement", "semantic_review", "semantic_correction"):
        if result.produced_artifacts or result.produced_documents:
            return _failure("invalid_produced_payload")
        if (
            result.priority_subject_sha256 is not None
            or result.map_subject_sha256 is not None
            or result.candidate_subject_sha256 is not None
        ):
            return _failure("invalid_produced_payload")
    else:
        return _failure("invalid_semantic_input")
    return None


def _check_subject_binding(
    result: SemanticWorkResult, workflow_state: WorkflowState
) -> SemanticOperationFailure | None:
    """Bind result subject hashes to exact authoritative expected subjects."""

    kind = result.kind
    if kind == "exam_priority_assessment":
        if result.priority_subject_sha256 is None or not _valid_digest(result.priority_subject_sha256):
            return _failure("invalid_subject_hash")
        try:
            if workflow_state.priority_basis is None:
                return _failure("subject_hash_mismatch")
            expected = priority_subject_sha256(workflow_state.priority_basis, workflow_state.policies.priority_basis)
        except Exception:
            return _failure("subject_hash_mismatch")
        if result.priority_subject_sha256 != expected:
            return _failure("subject_hash_mismatch")
    elif kind == "lecture_map_generation":
        if result.map_subject_sha256 is None or not _valid_digest(result.map_subject_sha256):
            return _failure("invalid_subject_hash")
        if not result.produced_artifacts:
            return _failure("invalid_produced_payload")
        art = result.produced_artifacts[0]
        try:
            sha = hashlib.sha256(art.content_bytes).hexdigest()
            producer = ARTIFACT_PRODUCERS[art.kind]
            map_ref = WorkflowArtifactReference(
                "workflow-artifact-reference/v1", art.artifact_id, art.kind, sha, producer
            )
            lecture_ids = lecture_ids_from_artifact(art.content_bytes)
            context = workflow_state.map_reopen_context
            reserved = () if context is None else context.baseline_lecture_ids
            lecture_map = LectureMapRecord(
                map_reference=map_ref,
                lecture_ids=lecture_ids,
                status="proposed",
                approval=None,
                reserved_lecture_ids=reserved,
            )
            expected = map_subject_sha256(lecture_map, workflow_state.policies.lecture_mapping)
        except Exception:
            return _failure("invalid_produced_payload")
        if result.map_subject_sha256 != expected:
            return _failure("subject_hash_mismatch")
    elif kind in ("lecture_generation", "semantic_correction"):
        if result.candidate_subject_sha256 is None:
            return _failure("invalid_subject_hash")
        if not result.produced_documents:
            return _failure("invalid_produced_payload")
        first_doc = result.produced_documents[0]
        try:
            sha_doc = hashlib.sha256(first_doc.source_text.encode("utf-8")).hexdigest()
            ref = DocumentReference("lecture-document/v1", first_doc.lecture_id, int(first_doc.lecture_id[1:]), sha_doc)
            expected = candidate_subject_sha256(ref)
        except Exception:
            return _failure("invalid_produced_payload")
        if result.candidate_subject_sha256 != expected:
            return _failure("subject_hash_mismatch")
    return None


# ---------------------------------------------------------------------------
# submit_semantic_result
# ---------------------------------------------------------------------------

def submit_semantic_result(
    job_id: str,
    result: SemanticWorkResult,
    *,
    job_store: LocalCourseJobStore,
    workflow_store: LocalWorkflowStateStore,
    semantic_store: LocalSemanticWorkStore,
    association_store: LocalCourseWorkflowAssociationStore,
    source_store: LocalSourceEvidenceStore,
    policy_store: LocalPolicyContentStore,
    artifact_store: LocalWorkflowArtifactStore,
    document_store: LocalLectureDocumentStore,
    clock: str | None = None,
    holder_id: str | None = None,
) -> WorkflowAdvanced | WorkflowBlocked | WorkflowFailed | WorkflowIdempotentRepeat | WorkflowRejected | SemanticAcceptedRecord | SemanticOperationFailure:
    """Submit a result: validate -> persist immutables -> T027 -> accept -> reconcile.

    ``holder_id`` must be the opaque lease token granted by
    ``request_semantic_work``; results from a different/expired holder fail
    closed. Priority assessment and visual/review kinds persist evidence only
    and never advance the workflow: owner approval stays explicit. Map
    generation records the proposed map only: approval stays explicit.
    """

    if type(job_id) is not str:
        raise TypeError("job_id must be exactly str")
    if type(result) is not SemanticWorkResult:
        raise TypeError("result must be exactly SemanticWorkResult")
    if not _valid_safe_id(job_id):
        return _failure("invalid_semantic_input")
    if holder_id is not None and type(holder_id) is not str:
        raise TypeError("holder_id must be exactly str or None")
    _require_store("job_store", job_store, LocalCourseJobStore)
    _require_store("workflow_store", workflow_store, LocalWorkflowStateStore)
    _require_store("semantic_store", semantic_store, LocalSemanticWorkStore)
    _require_store("association_store", association_store, LocalCourseWorkflowAssociationStore)
    _require_store("source_store", source_store, LocalSourceEvidenceStore)
    _require_store("policy_store", policy_store, LocalPolicyContentStore)
    _require_store("artifact_store", artifact_store, LocalWorkflowArtifactStore)
    _require_store("document_store", document_store, LocalLectureDocumentStore)

    try:
        now_iso = clock if clock is not None else _now_iso()
        if not _valid_created_at(now_iso):
            return _failure("invalid_semantic_input")
        if holder_id is None or not _valid_safe_id(holder_id):
            return _failure("invalid_semantic_input")
        try:
            SemanticWorkResult(
                result.result_version,
                result.request_id,
                result.operation_id,
                result.kind,  # type: ignore[arg-type]
                result.produced_artifacts,
                result.produced_documents,
                result.diagnostics,
                result.priority_subject_sha256,
                result.map_subject_sha256,
                result.candidate_subject_sha256,
                result.request_revision,
            )
        except Exception:
            return _failure("invalid_semantic_input")

        stored = semantic_store.load(result.request_id)
        if isinstance(stored, SemanticWorkPersistenceFailure):
            code = _persistence_failure_code(stored)
            if code == "request_not_found":
                return _failure("request_identity_mismatch")
            return _failure("semantic_store_failed")

        if isinstance(stored, SemanticAcceptedRecord):
            return _handle_resubmission_after_accept(
                job_id, result, stored, job_store, workflow_store, semantic_store, association_store,
                source_store, policy_store, artifact_store, document_store, now_iso
            )

        req: SemanticWorkRequest = stored  # type: ignore[assignment]
        if req.job_id != job_id:
            return _failure("request_identity_mismatch")
        if req.operation_id != result.operation_id:
            return _failure("request_identity_mismatch")
        if req.kind != result.kind:
            return _failure("request_identity_mismatch")

        loaded = _load_job_and_workflow(job_store, workflow_store, job_id)
        if isinstance(loaded, SemanticOperationFailure):
            return loaded
        job_r2, ws = loaded
        if req.workflow_id != job_r2.workflow_id or req.workflow_id != ws.workflow_id:
            return _failure("request_identity_mismatch")

        # Kind-aware crash recovery resume (Finding A):
        # Check whether an authoritative transition already succeeded in WorkflowState
        # before semantic bookkeeping completed.
        if result.kind in ("lecture_generation", "semantic_correction"):
            candidate_receipt = next(
                (rc for rc in ws.operation_receipts if rc.operation_id == result.operation_id), None
            )
            validation_op_id = f"{result.operation_id}-validation"
            validation_receipt = next(
                (rc for rc in ws.operation_receipts if rc.operation_id == validation_op_id), None
            )

            # Case A4: validation receipt without candidate receipt is an impossible/inconsistent state.
            if candidate_receipt is None and validation_receipt is not None:
                return _failure("invalid_workflow_state")

            if candidate_receipt is not None:
                # Case A4: verify candidate receipt corresponds exactly to this semantic result/request.
                if candidate_receipt.action != "submit_lecture_candidate":
                    return _failure("result_identity_mismatch")
                if len(result.produced_documents) != 1:
                    return _failure("invalid_produced_payload")
                first_doc = result.produced_documents[0]
                if not req.input_refs or first_doc.lecture_id != req.input_refs[0]:
                    return _failure("invalid_produced_payload")
                try:
                    sha_doc = hashlib.sha256(first_doc.source_text.encode("utf-8")).hexdigest()
                    ref = DocumentReference(
                        "lecture-document/v1", first_doc.lecture_id, int(first_doc.lecture_id[1:]), sha_doc
                    )
                    expected_cand_subj = candidate_subject_sha256(ref)
                except Exception:
                    return _failure("invalid_produced_payload")

                if candidate_receipt.subject_sha256 != expected_cand_subj:
                    return _failure("result_identity_mismatch")
                if result.candidate_subject_sha256 != expected_cand_subj:
                    return _failure("subject_hash_mismatch")

                if validation_receipt is not None:
                    # Case A3 — candidate + validation receipts both exist.
                    # Semantic unit is already authoritatively completed.
                    if validation_receipt.action != "record_candidate_validation":
                        return _failure("result_identity_mismatch")
                    marked = semantic_store.mark_accepted(result)
                    if isinstance(marked, SemanticWorkPersistenceFailure):
                        code = _persistence_failure_code(marked)
                        if code == "immutable_identity_conflict":
                            return _failure("result_identity_mismatch")
                        if code is not None:
                            return _failure("semantic_store_failed")
                    reconciled = _save_job_projection(job_store, job_r2, ws)
                    if isinstance(reconciled, SemanticOperationFailure):
                        return reconciled
                    return WorkflowIdempotentRepeat(
                        status="idempotent_repeat", state=ws, original_receipt=validation_receipt
                    )

                # Case A2 — candidate receipt exists, validation receipt absent.
                # This is the interrupted intermediate state.
                # Do NOT mark semantic result accepted yet.
                # Require expected lecture_validation state and exact candidate identity.
                if ws.stage != "lecture_validation" or ws.disposition != "ready":
                    return _failure("invalid_workflow_state")
                if ws.active_lecture_id != first_doc.lecture_id:
                    return _failure("invalid_workflow_state")
                prog = next((p for p in ws.lecture_progress if p.lecture_id == ws.active_lecture_id), None)
                if prog is None or prog.candidate is None or prog.status != "candidate":
                    return _failure("invalid_workflow_state")
                if prog.candidate.content_sha256 != sha_doc:
                    return _failure("result_identity_mismatch")
                if candidate_subject_sha256(prog.candidate) != expected_cand_subj:
                    return _failure("result_identity_mismatch")

                # Verify lease authority for this recovery invocation.
                lease_info = semantic_store.lease_info(result.request_id)
                if isinstance(lease_info, SemanticWorkPersistenceFailure):
                    return _failure("semantic_store_failed")
                _status, _expires, _holder = lease_info
                if _status != "leased" or _holder != holder_id:
                    return _failure("lease_conflict") if _status == "leased" else _failure("stale_revision")
                if _expires is None or not _is_lease_active(_expires, now_iso):
                    return _failure("stale_revision")

                # Ensure candidate document exists in document_store.
                from course_compiler.lecture_document_persistence import LectureDocumentPersistenceFailure

                doc_load = document_store.load(prog.candidate)
                if isinstance(doc_load, LectureDocumentPersistenceFailure):
                    lecture_doc = LectureDocument(
                        "lecture-document/v1",
                        first_doc.lecture_id,
                        int(first_doc.lecture_id[1:]),
                        first_doc.source_text,
                        SourceProvenance(sha_doc),
                    )
                    save_doc = document_store.save(lecture_doc)
                    if isinstance(save_doc, LectureDocumentPersistenceFailure):
                        return _failure("document_store_failed")

                # Run/resume deterministic validation via T027.
                from course_compiler.course_workflow import (
                    CourseWorkflowAssociation,
                    COURSE_WORKFLOW_ASSOCIATION_VERSION,
                )

                assoc = CourseWorkflowAssociation(
                    COURSE_WORKFLOW_ASSOCIATION_VERSION, job_r2.course_reference, job_r2.workflow_id
                )
                assoc_load = association_store.load(assoc)
                from course_compiler.course_workflow_persistence import CourseWorkflowPersistenceFailure

                if isinstance(assoc_load, CourseWorkflowPersistenceFailure):
                    return _failure("association_store_failed")

                validation_result = _apply_deterministic_validation(
                    ws,
                    job_r2,
                    assoc,
                    association_store=association_store,
                    workflow_store=workflow_store,
                    policy_store=policy_store,
                    source_store=source_store,
                    artifact_store=artifact_store,
                    document_store=document_store,
                    operation_id=validation_op_id,
                    clock=now_iso,
                )
                if isinstance(validation_result, SemanticOperationFailure):
                    return validation_result
                ws_new = validation_result

                # Only after validation transition succeeds: mark accepted and reconcile.
                marked = semantic_store.mark_accepted(result)
                if isinstance(marked, SemanticWorkPersistenceFailure):
                    code = _persistence_failure_code(marked)
                    if code == "immutable_identity_conflict":
                        return _failure("result_identity_mismatch")
                    if code is not None:
                        return _failure("semantic_store_failed")
                projected = _save_job_projection(job_store, job_r2, ws_new)
                if isinstance(projected, SemanticOperationFailure):
                    return projected
                _supersede_stale_pending(semantic_store, job_r2.job_id, ws_new.revision)
                return WorkflowAdvanced(
                    status="advanced",
                    state=ws_new,
                    receipt=ws_new.operation_receipts[-1],
                    diagnostics=(),
                )

            # Case A1: candidate_receipt is None -> continue to normal path below.
        else:
            # Non-lecture_generation kinds (single transition):
            receipt = next((rc for rc in ws.operation_receipts if rc.operation_id == result.operation_id), None)
            if receipt is not None:
                if result.kind == "source_assessment":
                    if receipt.action != "record_source_assessment":
                        return _failure("result_identity_mismatch")
                elif result.kind == "lecture_map_generation":
                    if receipt.action != "record_lecture_map":
                        return _failure("result_identity_mismatch")
                    if receipt.subject_sha256 != result.map_subject_sha256:
                        return _failure("result_identity_mismatch")
                marked = semantic_store.mark_accepted(result)
                if isinstance(marked, SemanticWorkPersistenceFailure):
                    code = _persistence_failure_code(marked)
                    if code == "immutable_identity_conflict":
                        return _failure("result_identity_mismatch")
                    if code is not None:
                        return _failure("semantic_store_failed")
                reconciled = _save_job_projection(job_store, job_r2, ws)
                if isinstance(reconciled, SemanticOperationFailure):
                    return reconciled
                return WorkflowIdempotentRepeat(status="idempotent_repeat", state=ws, original_receipt=receipt)

        # Lease authority: the row must be actively leased to this holder.
        lease_info = semantic_store.lease_info(result.request_id)
        if isinstance(lease_info, SemanticWorkPersistenceFailure):
            return _failure("semantic_store_failed")
        _status, _expires, _holder = lease_info
        if _status != "leased" or _holder != holder_id:
            return _failure("lease_conflict") if _status == "leased" else _failure("stale_revision")
        if _expires is None:
            return _failure("stale_revision")
        try:
            if not _is_lease_active(_expires, now_iso):
                return _failure("stale_revision")
        except ValueError:
            return _failure("semantic_store_failed")
        if ws.revision != result.request_revision or ws.revision != req.expected_revision:
            return _failure("stale_revision")

        bounds_failure = _check_produced_bounds(result)
        if bounds_failure is not None:
            return bounds_failure
        shape_failure = _check_kind_shape(result)
        if shape_failure is not None:
            return shape_failure
        # The produced lecture must be the lecture this request was issued for
        # and must still be pending/retry in authoritative state.
        if result.kind in ("lecture_generation", "semantic_correction"):
            if not req.input_refs or result.produced_documents[0].lecture_id != req.input_refs[0]:
                return _failure("invalid_produced_payload")
            statuses = ("correction_required", "retry_required") if result.kind == "semantic_correction" else ("pending", "retry_required")
            pending_ids = {p.lecture_id for p in ws.lecture_progress if p.status in statuses}
            if result.produced_documents[0].lecture_id not in pending_ids:
                return _failure("stale_revision")
        binding_failure = _check_subject_binding(result, ws)
        if binding_failure is not None:
            return binding_failure

        # Persist immutable produced objects before any workflow mutation.
        # Exact repeats are idempotent; same-id/different-content conflicts
        # fail closed. A crash here leaves harmless orphans only.
        artifact_refs: list[WorkflowArtifactReference] = []
        for art in result.produced_artifacts:
            sha = hashlib.sha256(art.content_bytes).hexdigest()
            producer = ARTIFACT_PRODUCERS[art.kind]
            try:
                ref = WorkflowArtifactReference("workflow-artifact-reference/v1", art.artifact_id, art.kind, sha, producer)  # type: ignore[arg-type]
            except Exception:
                return _failure("invalid_produced_payload")
            save_res = artifact_store.save(ref, art.content_bytes)
            from course_compiler.workflow_artifact_persistence import WorkflowArtifactPersistenceFailure

            if isinstance(save_res, WorkflowArtifactPersistenceFailure):
                code = _persistence_failure_code(save_res)
                if code == "immutable_identity_conflict":
                    return _failure("result_identity_mismatch")
                return _failure("artifact_store_failed")
            artifact_refs.append(ref)

        document_refs: list[DocumentReference] = []
        for doc in result.produced_documents:
            sha = hashlib.sha256(doc.source_text.encode("utf-8")).hexdigest()
            lecture_doc = LectureDocument("lecture-document/v1", doc.lecture_id, int(doc.lecture_id[1:]), doc.source_text, SourceProvenance(sha))
            save_doc = document_store.save(lecture_doc)
            from course_compiler.lecture_document_persistence import LectureDocumentPersistenceFailure

            if isinstance(save_doc, LectureDocumentPersistenceFailure):
                return _failure("document_store_failed")
            ref = document_reference(lecture_doc)
            if ref is None:
                return _failure("invalid_produced_payload")
            document_refs.append(ref)

        from course_compiler.course_workflow import CourseWorkflowAssociation, COURSE_WORKFLOW_ASSOCIATION_VERSION

        assoc = CourseWorkflowAssociation(COURSE_WORKFLOW_ASSOCIATION_VERSION, job_r2.course_reference, job_r2.workflow_id)
        assoc_load = association_store.load(assoc)
        from course_compiler.course_workflow_persistence import CourseWorkflowPersistenceFailure

        if isinstance(assoc_load, CourseWorkflowPersistenceFailure):
            return _failure("association_store_failed")

        # Evidence-only kinds: persist evidence, accept the envelope, never
        # advance the workflow. Owner approval stays explicit.
        if result.kind in ("exam_priority_assessment", "visual_selection", "visual_placement", "semantic_review"):
            correction_ids: tuple[str, ...] = ()
            if result.kind == "semantic_review":
                if job_r2.quality_mode != "review" or (ws.stage, ws.disposition) != ("completed", "completed"):
                    return _failure("stale_revision")
                synthetic_row = SemanticAcceptedRecord(
                    result.result_version, result.request_id, result.operation_id, result.kind, (), (),
                    result.diagnostics, result.priority_subject_sha256, result.map_subject_sha256,
                    result.candidate_subject_sha256, result.request_revision,
                )
                parsed = _review_lecture_ids(synthetic_row, ws)
                if parsed is None:
                    return _failure("invalid_review_verdict")
                correction_ids = parsed
            marked = semantic_store.mark_accepted(result)
            if isinstance(marked, SemanticWorkPersistenceFailure):
                return _failure("semantic_store_failed")
            if correction_ids:
                from course_compiler.course_workflow_transition_operations import apply_persisted_course_workflow_request

                reopen = ReopenLecturesForCorrection(
                    "reopen_lectures_for_correction", correction_ids, result.request_id,
                    result.operation_id, ws.revision,
                )
                transition = WorkflowTransitionRequest(
                    "course-workflow-transition/v2", ws.workflow_id, ws.revision,
                    f"{result.operation_id}-reopen", reopen,
                )
                applied = apply_persisted_course_workflow_request(
                    assoc, transition, association_store=association_store, workflow_store=workflow_store,
                    policy_store=policy_store, source_store=source_store, artifact_store=artifact_store,
                    document_store=document_store,
                )
                if isinstance(applied, WorkflowRejected):
                    return applied
                if not isinstance(applied, (WorkflowAdvanced, WorkflowIdempotentRepeat)):
                    return _failure("workflow_store_failed")
                ws = applied.state
            projected = _save_job_projection(job_store, job_r2, ws)
            if isinstance(projected, SemanticOperationFailure):
                return projected
            return marked if not correction_ids else WorkflowAdvanced("advanced", ws, ws.operation_receipts[-1], ())

        payload = _build_transition_payload(result, artifact_refs, ws)
        if isinstance(payload, SemanticOperationFailure):
            return payload

        from course_compiler.course_workflow_transition_operations import apply_persisted_course_workflow_request

        transition_version = "course-workflow-transition/v2" if ws.contract_version == "course-workflow-state/v2" else "course-workflow-transition/v1"
        transition_req = WorkflowTransitionRequest(transition_version, ws.workflow_id, ws.revision, result.operation_id, payload)  # type: ignore[arg-type]
        transition_result = apply_persisted_course_workflow_request(
            assoc,
            transition_req,
            association_store=association_store,
            workflow_store=workflow_store,
            policy_store=policy_store,
            source_store=source_store,
            artifact_store=artifact_store,
            document_store=document_store,
        )
        if isinstance(transition_result, WorkflowRejected):
            semantic_store.mark_rejected(result.request_id)
            return transition_result
        if isinstance(transition_result, WorkflowIdempotentRepeat):
            marked = semantic_store.mark_accepted(result)
            if isinstance(marked, SemanticWorkPersistenceFailure):
                code = _persistence_failure_code(marked)
                if code == "immutable_identity_conflict":
                    return _failure("result_identity_mismatch")
                # Already accepted with the same envelope is fine.
            projected = _save_job_projection(job_store, job_r2, transition_result.state)
            if isinstance(projected, SemanticOperationFailure):
                return projected
            return transition_result
        if isinstance(transition_result, (WorkflowAdvanced, WorkflowBlocked, WorkflowFailed)):
            ws_new = transition_result.state
            if result.kind in ("lecture_generation", "semantic_correction") and isinstance(transition_result, WorkflowAdvanced):
                # Chain the deterministic evidence-bearing validation through
                # T027 before accepting: the candidate submit alone must not
                # leave the workflow stranded at lecture_validation.
                validation_result = _apply_deterministic_validation(
                    ws_new,
                    job_r2,
                    assoc,
                    association_store=association_store,
                    workflow_store=workflow_store,
                    policy_store=policy_store,
                    source_store=source_store,
                    artifact_store=artifact_store,
                    document_store=document_store,
                    operation_id=f"{result.operation_id}-validation",
                    clock=now_iso,
                )
                if isinstance(validation_result, SemanticOperationFailure):
                    # Candidate submit already advanced authoritatively; the
                    # validation follow-up failed (e.g. store outage). Leave
                    # the semantic request leased so exact retry resumes via
                    # the receipt path; report the store failure honestly.
                    return validation_result
                ws_new = validation_result
                transition_result = WorkflowAdvanced(
                    status="advanced", state=ws_new, receipt=ws_new.operation_receipts[-1], diagnostics=()
                )
            marked = semantic_store.mark_accepted(result)
            if isinstance(marked, SemanticWorkPersistenceFailure):
                code = _persistence_failure_code(marked)
                if code == "immutable_identity_conflict":
                    return _failure("result_identity_mismatch")
                # Same-envelope idempotent accept is fine; anything else here
                # means the row changed unexpectedly.
                if code is not None:
                    return _failure("semantic_store_failed")
            projected = _save_job_projection(job_store, job_r2, ws_new)
            if isinstance(projected, SemanticOperationFailure):
                return projected
            _supersede_stale_pending(semantic_store, job_r2.job_id, ws_new.revision)
            return transition_result
        from course_compiler.course_workflow_transition_operations import PersistedCourseWorkflowOperationFailure

        if isinstance(transition_result, PersistedCourseWorkflowOperationFailure):
            return _failure("workflow_store_failed")
        return _failure("semantic_operation_exception")
    except Exception:
        return _failure("semantic_operation_exception")


def _handle_resubmission_after_accept(
    job_id: str,
    result: SemanticWorkResult,
    stored: SemanticAcceptedRecord,
    job_store: LocalCourseJobStore,
    workflow_store: LocalWorkflowStateStore,
    semantic_store: LocalSemanticWorkStore,
    association_store: LocalCourseWorkflowAssociationStore,
    source_store: LocalSourceEvidenceStore,
    policy_store: LocalPolicyContentStore,
    artifact_store: LocalWorkflowArtifactStore,
    document_store: LocalLectureDocumentStore,
    now_iso: str,
) -> WorkflowIdempotentRepeat | SemanticAcceptedRecord | SemanticOperationFailure:
    try:
        incoming = envelope_for_result(result)
    except Exception:
        return _failure("invalid_semantic_input")
    if incoming != stored:
        return _failure("result_identity_mismatch")
    loaded = _load_job_and_workflow(job_store, workflow_store, job_id)
    if isinstance(loaded, SemanticOperationFailure):
        return loaded
    job_r, ws_r = loaded
    if result.kind == "semantic_review":
        ids = _review_lecture_ids(stored, ws_r)
        if ids is None:
            # Once reopened, validate the accepted record against the prior
            # terminal map through the immutable reopen receipt instead.
            reopen_receipt = next((item for item in ws_r.operation_receipts if item.operation_id == f"{result.operation_id}-reopen"), None)
            if reopen_receipt is None:
                return _failure("semantic_store_failed")
            projected = _save_job_projection(job_store, job_r, ws_r)
            return projected if isinstance(projected, SemanticOperationFailure) else WorkflowIdempotentRepeat("idempotent_repeat", ws_r, reopen_receipt)
        if not ids:
            projected = _save_job_projection(job_store, job_r, ws_r)
            return projected if isinstance(projected, SemanticOperationFailure) else stored
        reopen_receipt = next((item for item in ws_r.operation_receipts if item.operation_id == f"{result.operation_id}-reopen"), None)
        if reopen_receipt is not None:
            projected = _save_job_projection(job_store, job_r, ws_r)
            return projected if isinstance(projected, SemanticOperationFailure) else WorkflowIdempotentRepeat("idempotent_repeat", ws_r, reopen_receipt)
        from course_compiler.course_workflow import CourseWorkflowAssociation, COURSE_WORKFLOW_ASSOCIATION_VERSION
        from course_compiler.course_workflow_transition_operations import apply_persisted_course_workflow_request

        assoc = CourseWorkflowAssociation(COURSE_WORKFLOW_ASSOCIATION_VERSION, job_r.course_reference, job_r.workflow_id)
        reopen = ReopenLecturesForCorrection("reopen_lectures_for_correction", ids, result.request_id, result.operation_id, ws_r.revision)
        request = WorkflowTransitionRequest("course-workflow-transition/v2", ws_r.workflow_id, ws_r.revision, f"{result.operation_id}-reopen", reopen)
        applied = apply_persisted_course_workflow_request(assoc, request, association_store=association_store, workflow_store=workflow_store, policy_store=policy_store, source_store=source_store, artifact_store=artifact_store, document_store=document_store)
        if not isinstance(applied, (WorkflowAdvanced, WorkflowIdempotentRepeat)):
            return _failure("semantic_store_failed")
        projected = _save_job_projection(job_store, job_r, applied.state)
        return projected if isinstance(projected, SemanticOperationFailure) else WorkflowIdempotentRepeat("idempotent_repeat", applied.state, applied.receipt if isinstance(applied, WorkflowAdvanced) else applied.original_receipt)
    receipt = next((rc for rc in ws_r.operation_receipts if rc.operation_id == result.operation_id), None)
    if receipt is None:
        # Accepted envelope without an authoritative transition is not a
        # state this ordering can produce (accept always follows the
        # transition). Fail closed without fabricating progress.
        return _failure("semantic_store_failed")
    if result.kind in ("lecture_generation", "semantic_correction"):
        validation_receipt = next(
            (rc for rc in ws_r.operation_receipts if rc.operation_id == f"{result.operation_id}-validation"), None
        )
        if validation_receipt is None:
            return _failure("semantic_store_failed")
        receipt = validation_receipt
    projected = _save_job_projection(job_store, job_r, ws_r)
    if isinstance(projected, SemanticOperationFailure):
        return projected
    return WorkflowIdempotentRepeat(status="idempotent_repeat", state=ws_r, original_receipt=receipt)


def _build_transition_payload(
    result: SemanticWorkResult, artifact_refs: list[WorkflowArtifactReference], ws: WorkflowState
) -> RecordSourceAssessment | RecordLectureMap | SubmitLectureCandidate | SemanticOperationFailure:
    try:
        if result.kind == "source_assessment":
            kind_to_ref = {r.artifact_kind: r for r in artifact_refs}
            if sorted(kind_to_ref) != ["evidence_hierarchy", "priority_proposal", "source_assessment"]:
                return _failure("invalid_produced_payload")
            return RecordSourceAssessment("record_source_assessment", kind_to_ref["source_assessment"], "exam_driven", kind_to_ref["priority_proposal"], kind_to_ref["evidence_hierarchy"])
        if result.kind == "lecture_map_generation":
            map_ref = artifact_refs[0]
            lecture_ids = lecture_ids_from_artifact(result.produced_artifacts[0].content_bytes)
            return RecordLectureMap("record_lecture_map", map_ref, lecture_ids)  # type: ignore[arg-type]
        if result.kind in ("lecture_generation", "semantic_correction"):
            doc_spec = result.produced_documents[0]
            lecture_doc = LectureDocument("lecture-document/v1", doc_spec.lecture_id, int(doc_spec.lecture_id[1:]), doc_spec.source_text, SourceProvenance(hashlib.sha256(doc_spec.source_text.encode()).hexdigest()))
            return SubmitLectureCandidate("submit_lecture_candidate", doc_spec.lecture_id, lecture_doc)  # type: ignore[arg-type]
    except Exception:
        return _failure("invalid_produced_payload")
    return _failure("invalid_semantic_input")


def _supersede_stale_pending(semantic_store: LocalSemanticWorkStore, job_id: str, current_revision: int) -> None:
    """Best-effort retirement of superseded pending rows (history preserved).

    Rows whose expected_revision no longer matches authoritative state can
    never become current again; marking them rejected keeps the store to a
    single current request without deleting recovery evidence.
    """

    try:
        all_rows = semantic_store.load_all_for_job(job_id)
    except Exception:
        return
    if not isinstance(all_rows, tuple):
        return
    for row in all_rows:
        if type(row) is SemanticWorkRequest and row.expected_revision != current_revision:
            try:
                semantic_store.mark_rejected(row.request_id)
            except Exception:
                continue


def _apply_deterministic_validation(
    ws: WorkflowState,
    job: CourseJobRecord,
    assoc: object,
    *,
    association_store: LocalCourseWorkflowAssociationStore,
    workflow_store: LocalWorkflowStateStore,
    policy_store: LocalPolicyContentStore,
    source_store: LocalSourceEvidenceStore,
    artifact_store: LocalWorkflowArtifactStore,
    document_store: LocalLectureDocumentStore,
    operation_id: str,
    clock: str,
) -> WorkflowState | SemanticOperationFailure:
    """Record evidence-bearing deterministic candidate validation via T027.

    Loads the authoritative candidate document, runs the real document
    checks, persists validation evidence bytes describing the performed
    checks, then delegates through the accepted T027 persisted boundary.
    There is no direct WorkflowState write path here.
    """

    try:
        if ws.stage != "lecture_validation" or ws.disposition != "ready":
            return _failure("invalid_workflow_state")
        if ws.active_lecture_id is None:
            return _failure("invalid_workflow_state")
        prog = next((p for p in ws.lecture_progress if p.lecture_id == ws.active_lecture_id), None)
        if prog is None or prog.candidate is None or prog.status != "candidate":
            return _failure("invalid_workflow_state")
        if not _valid_safe_id(operation_id):
            return _failure("invalid_semantic_input")
        candidate_ref = prog.candidate
        doc_result = document_store.load(candidate_ref)
        from course_compiler.lecture_document_persistence import LectureDocumentPersistenceFailure

        if isinstance(doc_result, LectureDocumentPersistenceFailure):
            return _failure("document_store_failed")
        from course_compiler.contracts import validate_document, has_validation_errors

        diags = validate_document(doc_result)
        check_codes = sorted({d.code for d in diags if type(getattr(d, "code", None)) is str})
        disposition = "rejected" if has_validation_errors(diags) else "passed"
        candidate_subject = candidate_subject_sha256(candidate_ref)
        policy = ws.policies.lecture_validation
        evidence = {
            "validation": "lecture-validation/v1",
            "lecture_id": prog.lecture_id,
            "candidate_sha256": candidate_ref.content_sha256,
            "candidate_subject_sha256": candidate_subject,
            "policy_version": policy.policy_version,
            "policy_sha256": policy.content_sha256,
            "disposition": disposition,
            "check_codes": check_codes,
        }
        content = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
        artifact_id = f"val-{prog.lecture_id}-{candidate_ref.content_sha256[:16]}"
        if not _valid_safe_id(artifact_id):
            return _failure("invalid_semantic_input")
        ref = WorkflowArtifactReference(
            "workflow-artifact-reference/v1", artifact_id, "validation", hashlib.sha256(content).hexdigest(), policy.policy_version  # type: ignore[arg-type]
        )
        save_res = artifact_store.save(ref, content)
        from course_compiler.workflow_artifact_persistence import WorkflowArtifactPersistenceFailure

        if isinstance(save_res, WorkflowArtifactPersistenceFailure):
            code = _persistence_failure_code(save_res)
            if code == "immutable_identity_conflict":
                return _failure("invalid_semantic_input")
            return _failure("artifact_store_failed")
        validation = ValidationRecord(
            candidate_subject_sha256=candidate_subject,
            validation_policy_version=policy.policy_version,  # type: ignore[arg-type]
            validation_policy_sha256=policy.content_sha256,
            disposition=disposition,  # type: ignore[arg-type]
            validation_reference=ref,
        )
        payload = RecordCandidateValidation("record_candidate_validation", prog.lecture_id, candidate_subject, validation)  # type: ignore[arg-type]
        transition_version = "course-workflow-transition/v2" if ws.contract_version == "course-workflow-state/v2" else "course-workflow-transition/v1"
        transition_req = WorkflowTransitionRequest(transition_version, ws.workflow_id, ws.revision, operation_id, payload)  # type: ignore[arg-type]
        from course_compiler.course_workflow_transition_operations import apply_persisted_course_workflow_request

        transition_result = apply_persisted_course_workflow_request(
            assoc,  # type: ignore[arg-type]
            transition_req,
            association_store=association_store,
            workflow_store=workflow_store,
            policy_store=policy_store,
            source_store=source_store,
            artifact_store=artifact_store,
            document_store=document_store,
        )
        if isinstance(transition_result, (WorkflowAdvanced, WorkflowBlocked, WorkflowFailed)):
            return transition_result.state
        if isinstance(transition_result, WorkflowIdempotentRepeat):
            return transition_result.state
        if isinstance(transition_result, WorkflowRejected):
            return _failure("workflow_rejected")
        return _failure("workflow_store_failed")
    except Exception:
        return _failure("semantic_operation_exception")


# ---------------------------------------------------------------------------
# Explicit owner decisions (Finding C)
# ---------------------------------------------------------------------------

def submit_owner_priority_decision(
    job_id: str,
    *,
    approve: bool,
    subject_sha256: str | None = None,
    job_store: LocalCourseJobStore,
    workflow_store: LocalWorkflowStateStore,
    semantic_store: LocalSemanticWorkStore,
    association_store: LocalCourseWorkflowAssociationStore,
    source_store: LocalSourceEvidenceStore,
    policy_store: LocalPolicyContentStore,
    artifact_store: LocalWorkflowArtifactStore,
    document_store: LocalLectureDocumentStore,
    clock: str | None = None,
    operation_id_generator=None,
) -> WorkflowAdvanced | WorkflowBlocked | WorkflowFailed | WorkflowIdempotentRepeat | WorkflowRejected | SemanticOperationFailure:
    """Record an explicit owner priority-basis decision via T027.

    Never inferred: ``approve`` is required caller intent. The subject is
    bound to the authoritative current proposal/policy subject; a caller
    provided hash that disagrees fails closed.
    """

    if type(job_id) is not str:
        raise TypeError("job_id must be exactly str")
    if type(approve) is not bool:
        raise TypeError("approve must be exactly bool")
    if subject_sha256 is None:
        return _failure("invalid_semantic_input")
    if type(subject_sha256) is not str:
        raise TypeError("subject_sha256 must be exactly str")
    if not _valid_digest(subject_sha256):
        return _failure("invalid_subject_hash")
    if not _valid_safe_id(job_id):
        return _failure("invalid_semantic_input")
    _require_store("job_store", job_store, LocalCourseJobStore)
    _require_store("workflow_store", workflow_store, LocalWorkflowStateStore)
    _require_store("semantic_store", semantic_store, LocalSemanticWorkStore)
    _require_store("association_store", association_store, LocalCourseWorkflowAssociationStore)
    _require_store("source_store", source_store, LocalSourceEvidenceStore)
    _require_store("policy_store", policy_store, LocalPolicyContentStore)
    _require_store("artifact_store", artifact_store, LocalWorkflowArtifactStore)
    _require_store("document_store", document_store, LocalLectureDocumentStore)
    try:
        now_iso = clock if clock is not None else _now_iso()
        if not _valid_created_at(now_iso):
            return _failure("invalid_semantic_input")
        loaded = _load_job_and_workflow(job_store, workflow_store, job_id)
        if isinstance(loaded, SemanticOperationFailure):
            return loaded
        job, ws = loaded
        if ws.stage != "priority_approval" or ws.disposition != "awaiting_approval":
            return _failure("invalid_owner_decision")
        if ws.priority_basis is None:
            return _failure("invalid_owner_decision")
        try:
            expected = priority_subject_sha256(ws.priority_basis, ws.policies.priority_basis)
        except Exception:
            return _failure("invalid_owner_decision")
        if subject_sha256 != expected:
            return _failure("subject_hash_mismatch")
        operation_id = operation_id_generator() if callable(operation_id_generator) else _generate_safe_id("op")
        if not _valid_safe_id(operation_id):
            return _failure("invalid_semantic_input")
        if approve:
            payload: ApprovePriorityBasis | RejectPriorityBasis = ApprovePriorityBasis("approve_priority_basis", expected)
        else:
            payload = RejectPriorityBasis("reject_priority_basis", expected)
        return _apply_owner_transition(
            job, ws, payload, operation_id,
            job_store=job_store, workflow_store=workflow_store, semantic_store=semantic_store,
            association_store=association_store, policy_store=policy_store,
            source_store=source_store, artifact_store=artifact_store, document_store=document_store,
        )
    except Exception:
        return _failure("semantic_operation_exception")


def submit_owner_map_decision(
    job_id: str,
    *,
    approve: bool,
    subject_sha256: str | None = None,
    job_store: LocalCourseJobStore,
    workflow_store: LocalWorkflowStateStore,
    semantic_store: LocalSemanticWorkStore,
    association_store: LocalCourseWorkflowAssociationStore,
    source_store: LocalSourceEvidenceStore,
    policy_store: LocalPolicyContentStore,
    artifact_store: LocalWorkflowArtifactStore,
    document_store: LocalLectureDocumentStore,
    clock: str | None = None,
    operation_id_generator=None,
) -> WorkflowAdvanced | WorkflowBlocked | WorkflowFailed | WorkflowIdempotentRepeat | WorkflowRejected | SemanticOperationFailure:
    """Record an explicit owner lecture-map decision via T027.

    Never inferred from generation completing or semantic validity.
    """

    if type(job_id) is not str:
        raise TypeError("job_id must be exactly str")
    if type(approve) is not bool:
        raise TypeError("approve must be exactly bool")
    if subject_sha256 is None:
        return _failure("invalid_semantic_input")
    if type(subject_sha256) is not str:
        raise TypeError("subject_sha256 must be exactly str")
    if not _valid_digest(subject_sha256):
        return _failure("invalid_subject_hash")
    if not _valid_safe_id(job_id):
        return _failure("invalid_semantic_input")
    _require_store("job_store", job_store, LocalCourseJobStore)
    _require_store("workflow_store", workflow_store, LocalWorkflowStateStore)
    _require_store("semantic_store", semantic_store, LocalSemanticWorkStore)
    _require_store("association_store", association_store, LocalCourseWorkflowAssociationStore)
    _require_store("source_store", source_store, LocalSourceEvidenceStore)
    _require_store("policy_store", policy_store, LocalPolicyContentStore)
    _require_store("artifact_store", artifact_store, LocalWorkflowArtifactStore)
    _require_store("document_store", document_store, LocalLectureDocumentStore)
    try:
        now_iso = clock if clock is not None else _now_iso()
        if not _valid_created_at(now_iso):
            return _failure("invalid_semantic_input")
        loaded = _load_job_and_workflow(job_store, workflow_store, job_id)
        if isinstance(loaded, SemanticOperationFailure):
            return loaded
        job, ws = loaded
        if ws.stage != "map_approval" or ws.disposition != "awaiting_approval":
            return _failure("invalid_owner_decision")
        if ws.lecture_map is None:
            return _failure("invalid_owner_decision")
        try:
            expected = map_subject_sha256(ws.lecture_map, ws.policies.lecture_mapping)
        except Exception:
            return _failure("invalid_owner_decision")
        if subject_sha256 != expected:
            return _failure("subject_hash_mismatch")
        operation_id = operation_id_generator() if callable(operation_id_generator) else _generate_safe_id("op")
        if not _valid_safe_id(operation_id):
            return _failure("invalid_semantic_input")
        if approve:
            payload: ApproveLectureMap | RejectLectureMap = ApproveLectureMap("approve_lecture_map", expected)
        else:
            payload = RejectLectureMap("reject_lecture_map", expected)
        return _apply_owner_transition(
            job, ws, payload, operation_id,
            job_store=job_store, workflow_store=workflow_store, semantic_store=semantic_store,
            association_store=association_store, policy_store=policy_store,
            source_store=source_store, artifact_store=artifact_store, document_store=document_store,
        )
    except Exception:
        return _failure("semantic_operation_exception")


def _apply_owner_transition(
    job: CourseJobRecord,
    ws: WorkflowState,
    payload: ApprovePriorityBasis | RejectPriorityBasis | ApproveLectureMap | RejectLectureMap,
    operation_id: str,
    *,
    job_store: LocalCourseJobStore,
    workflow_store: LocalWorkflowStateStore,
    semantic_store: LocalSemanticWorkStore,
    association_store: LocalCourseWorkflowAssociationStore,
    policy_store: LocalPolicyContentStore,
    source_store: LocalSourceEvidenceStore,
    artifact_store: LocalWorkflowArtifactStore,
    document_store: LocalLectureDocumentStore,
) -> WorkflowAdvanced | WorkflowBlocked | WorkflowFailed | WorkflowIdempotentRepeat | WorkflowRejected | SemanticOperationFailure:
    from course_compiler.course_workflow import CourseWorkflowAssociation, COURSE_WORKFLOW_ASSOCIATION_VERSION

    assoc = CourseWorkflowAssociation(COURSE_WORKFLOW_ASSOCIATION_VERSION, job.course_reference, job.workflow_id)
    assoc_load = association_store.load(assoc)
    from course_compiler.course_workflow_persistence import CourseWorkflowPersistenceFailure

    if isinstance(assoc_load, CourseWorkflowPersistenceFailure):
        return _failure("association_store_failed")
    transition_version = "course-workflow-transition/v2" if ws.contract_version == "course-workflow-state/v2" else "course-workflow-transition/v1"
    transition_req = WorkflowTransitionRequest(transition_version, ws.workflow_id, ws.revision, operation_id, payload)  # type: ignore[arg-type]
    from course_compiler.course_workflow_transition_operations import apply_persisted_course_workflow_request

    transition_result = apply_persisted_course_workflow_request(
        assoc,
        transition_req,
        association_store=association_store,
        workflow_store=workflow_store,
        policy_store=policy_store,
        source_store=source_store,
        artifact_store=artifact_store,
        document_store=document_store,
    )
    from course_compiler.course_workflow_transition_operations import PersistedCourseWorkflowOperationFailure

    if isinstance(transition_result, PersistedCourseWorkflowOperationFailure):
        return _failure("workflow_store_failed")
    if isinstance(transition_result, (WorkflowAdvanced, WorkflowBlocked, WorkflowFailed, WorkflowIdempotentRepeat)):
        projected = _save_job_projection(job_store, job, transition_result.state)
        if isinstance(projected, SemanticOperationFailure):
            return projected
        _supersede_stale_pending(semantic_store, job.job_id, transition_result.state.revision)
        return transition_result
    if isinstance(transition_result, WorkflowRejected):
        return transition_result
    return _failure("semantic_operation_exception")


def _is_valid_diagnostic(value: object) -> bool:
    if type(value) is not SemanticOperationDiagnostic:
        return False
    try:
        SemanticOperationDiagnostic(value.code, value.classification, value.message)
    except (AttributeError, TypeError, ValueError):
        return False
    return True


def _valid_digest(value: object) -> bool:
    import re

    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}\Z", value) is not None


def _iso_plus_seconds(iso: str, seconds: int) -> str:
    try:
        parsed = _parse_created_at(iso)
    except ValueError:
        parsed = datetime.now(timezone.utc)
    return (parsed + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
