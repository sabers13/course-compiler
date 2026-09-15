"""Coarse build operations, PDF retrieval, memoized cache, and source previews (T050)."""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Literal, TypeAlias

from .asset import AssetReference
from .build_persistence import (
    BuildDiagnostic,
    BuildPersistenceFailure,
    BuildRecord,
    LocalBuildRecordStore,
)
from .compilation import (
    CompiledPdf,
    CompilerInputBundle,
)
from .course_job_persistence import (
    CourseJobPersistenceFailure,
    CourseJobRecord,
    LocalCourseJobStore,
)
from .course_persistence import CourseRecord, LocalCourseStore
from .course_workflow import COURSE_WORKFLOW_ASSOCIATION_VERSION, CourseWorkflowAssociation
from .course_workflow_persistence import LocalCourseWorkflowAssociationStore
from .course_workflow_operations import (
    AcceptedLectureDocumentReopenFailure,
    ReopenedCourseWorkflowContext,
    build_reopened_course_pdf_with_visuals,
    reopen_accepted_lecture_documents,
    reopen_course_workflow_context,
)
from .lecture_document_persistence import LocalLectureDocumentStore
from .pdf_page import PDF_PAGE_REFERENCE_VERSION, PdfPageReference
from .pdf_visual_extraction import (
    ExtractedPdfVisual,
    PdfPagePreviewResult,
    PdfVisualExtractionResult,
    RenderedPdfPagePreview,
    extract_pdf_page_region,
    render_pdf_page_preview,
)
from .policy_persistence import LocalPolicyContentStore
from .rendering import DocumentReference
from .source_persistence import LocalSourceEvidenceStore, SourceEvidencePayload
from .visual_composition import (
    VisualCompositionFailure,
    VisualCompositionResult,
    compose_course_compiler_input,
)
from .source_snippets import derive_snippets
from .visual_placement import VisualPlacement
from .workflow import SourceEvidenceReference, WorkflowState
from .workflow_persistence import LocalWorkflowStateStore
from .semantic_work_persistence import LocalSemanticWorkStore

__all__ = [
    "BUILD_OPERATION_DIAGNOSTICS",
    "BuildDiagnostic",
    "BuildOperationFailure",
    "BuildOperationResult",
    "ExtractedPdfVisual",
    "RenderedPdfPagePreview",
    "build_pdf",
    "compute_bundle_hash",
    "extract_source_pdf_region",
    "get_artifact",
    "get_artifact_history",
    "preview_source_pdf_page",
    "reconcile_job_build_projection",
]

_DIAGNOSTICS = {
    "invalid_build_input": ("input", "The build operation input is invalid."),
    "job_not_found": ("storage", "The course job was not found."),
    "job_store_failed": ("storage", "The course job store failed."),
    "course_not_found": ("storage", "The course was not found."),
    "workflow_not_completed": ("state", "The course workflow has not completed."),
    "semantic_review_pending": ("state", "The semantic content review has not completed."),
    "review_corrections_pending": ("state", "Semantic review corrections are outstanding."),
    "accepted_documents_unavailable": ("storage", "The accepted lecture documents are unavailable."),
    "composition_failed": ("build", "Composing compiler input bundle failed."),
    "compiler_missing_glyph": ("build", "A required glyph is missing from the selected font."),
    "compiler_fatal_diagnostic": ("build", "TeX reported a fatal formatting or font error."),
    "compiler_overflow": ("build", "Content exceeds layout bounds; split long formulas or blocks."),
    "compilation_failed": ("build", "Compiling course PDF bundle failed."),
    "build_store_failed": ("storage", "The build record store failed."),
    "build_not_found": ("storage", "The build record was not found."),
    "build_not_succeeded": ("state", "The requested build did not succeed."),
    "build_identity_mismatch": ("identity", "The re-derived build identity does not match."),
    "cache_path_traversal_rejected": ("security", "The cache path is outside the allowed cache directory."),
    "cache_entry_corrupt": ("security", "The memoized cache entry is not a usable artifact."),
    "job_build_identity_mismatch": ("identity", "The build, job, and workflow identities do not match."),
    "artifact_integrity_mismatch": ("identity", "The artifact bytes do not match the recorded build digest."),
    "source_snippet_invalid": ("input", "A source snippet cannot be resolved from this course or placed at this location."),
    "ephemeral_visual_inputs_rejected": ("input", "Durable visual builds are not supported by this build operation."),
    "source_not_found": ("storage", "The source evidence was not found."),
    "build_operation_exception": ("adapter", "The build operation failed."),
}


_MINIMUM_PDF_BYTES = 32
_PDF_TRAILER_WINDOW_BYTES = 2048


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _generate_build_id() -> str:
    return f"bld-{uuid.uuid4().hex[:16]}"


@dataclass(frozen=True, slots=True)
class BuildOperationDiagnostic:
    """One fixed, content-safe build operation diagnostic."""

    code: str
    classification: Literal["input", "storage", "state", "build", "identity", "security", "adapter"]
    message: str

    def __post_init__(self) -> None:
        if self.code not in _DIAGNOSTICS:
            raise ValueError("build operation diagnostic code is not registered")
        classification, message = _DIAGNOSTICS[self.code]
        if self.classification != classification or self.message != message:
            raise ValueError("build operation diagnostic fields are not registered")


@dataclass(frozen=True, slots=True)
class BuildOperationFailure:
    """A fail-closed build operation failure."""

    status: Literal["build_operation_failed"]
    diagnostics: tuple[BuildOperationDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "build_operation_failed":
            raise ValueError("build operation failure status is invalid")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) != 1
            or not _is_valid_diagnostic(self.diagnostics[0])
        ):
            raise ValueError("build operation failure diagnostics are invalid")


BuildOperationResult: TypeAlias = BuildRecord | bytes | tuple[BuildRecord, ...] | BuildOperationFailure


def _failure(code: str) -> BuildOperationFailure:
    classification, message = _DIAGNOSTICS[code]
    return BuildOperationFailure(
        status="build_operation_failed",
        diagnostics=(BuildOperationDiagnostic(code, classification, message),),
    )


def _is_valid_diagnostic(value: object) -> bool:
    if type(value) is not BuildOperationDiagnostic:
        return False
    try:
        BuildOperationDiagnostic(value.code, value.classification, value.message)
    except Exception:
        return False
    return True


def compute_bundle_hash(bundle: CompilerInputBundle) -> str:
    """Compute exact SHA-256 over canonical length-prefixed framing of CompilerInputBundle."""

    if type(bundle) is not CompilerInputBundle:
        raise TypeError("bundle must be exactly CompilerInputBundle")

    h = hashlib.sha256()
    root_name_bytes = bundle.root_tex.logical_filename.encode("utf-8")
    root_content_bytes = bundle.root_tex.tex_source.encode("utf-8")
    h.update(f"ROOT:{len(root_name_bytes)}:".encode("ascii"))
    h.update(root_name_bytes)
    h.update(f":{len(root_content_bytes)}:".encode("ascii"))
    h.update(root_content_bytes)

    companions = sorted(bundle.companion_files, key=lambda item: item.logical_filename)
    h.update(f":COMPANIONS:{len(companions)}:".encode("ascii"))
    for comp in companions:
        c_name_bytes = comp.logical_filename.encode("utf-8")
        h.update(f"COMP:{len(c_name_bytes)}:".encode("ascii"))
        h.update(c_name_bytes)
        h.update(f":{len(comp.content_bytes)}:".encode("ascii"))
        h.update(comp.content_bytes)

    return h.hexdigest().lower()


def _safe_cache_path(cache_root: Path, bundle_hash: str) -> Path | BuildOperationFailure:
    if not isinstance(cache_root, Path):
        return _failure("invalid_build_input")
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        root_resolved = cache_root.resolve()
        target = (cache_root / f"{bundle_hash}.pdf").resolve()
        if not target.is_relative_to(root_resolved):
            return _failure("cache_path_traversal_rejected")
        return target
    except Exception:
        return _failure("cache_path_traversal_rejected")


def _write_cache_atomically(cache_root: Path, cache_path: Path, pdf_bytes: bytes) -> None:
    """Publish cache bytes by atomic rename, discarding any partial temporary file.

    A crash mid-write leaves only an unreferenced `.tmp-*` file, never a
    half-written entry at the real cache path, so the next read sees either the
    complete artifact or a clean miss.
    """

    temp_path = None
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        temp_path = cache_root / f".tmp-{os.getpid()}-{uuid.uuid4().hex}.pdf"
        temp_path.write_bytes(pdf_bytes)
        temp_path.replace(cache_path)
    except Exception:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except Exception:
                pass


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().lower()


def _cached_artifact_is_usable(pdf_bytes: bytes, expected_pdf_sha256: str | None = None) -> bool:
    """Reject a memoized cache entry that is not the exact recorded artifact.

    The cache is a disposable derivative, so a damaged, truncated, or
    substituted entry is never trusted and never returned: the caller treats an
    unusable entry as a miss and re-derives from the persisted authoritative
    inputs. A structural check alone only rejects truncation, so the entry must
    additionally hash to the immutable ``BuildRecord.pdf_sha256`` -- otherwise
    any other well-formed PDF dropped at the cache path would be served.
    """

    if type(pdf_bytes) is not bytes or len(pdf_bytes) < _MINIMUM_PDF_BYTES:
        return False
    if not pdf_bytes.startswith(b"%PDF-"):
        return False
    # A complete PDF ends with the EOF marker; a truncated write does not.
    if b"%%EOF" not in pdf_bytes[-_PDF_TRAILER_WINDOW_BYTES:]:
        return False
    if expected_pdf_sha256 is None:
        return False
    return _sha256_hex(pdf_bytes) == expected_pdf_sha256


@dataclass(frozen=True, slots=True)
class _CanonicalBuildInputs:
    """The exact canonical deterministic inputs recomputed for one build."""

    document_refs: tuple[DocumentReference, ...]
    placement_refs: tuple[str, ...]
    asset_refs: tuple[AssetReference, ...]
    bundle_hash: str


def _canonical_build_inputs(
    accepted_docs: tuple,
    *,
    visual_placements: tuple[VisualPlacement, ...],
    bundle_hash: str,
) -> _CanonicalBuildInputs:
    """Derive the canonical document/placement/asset refs for a composed bundle."""

    return _CanonicalBuildInputs(
        document_refs=tuple(
            DocumentReference(
                d.contract_version,
                d.document_id,
                d.order,
                d.provenance.content_sha256,
            )
            for d in accepted_docs
        ),
        placement_refs=tuple(
            f"{p.document_reference.document_id}@{p.source_text_offset}:{p.asset_reference.content_sha256}"
            for p in visual_placements
        ),
        asset_refs=tuple(p.asset_reference for p in visual_placements),
        bundle_hash=bundle_hash,
    )


def _reject_ephemeral_visual_inputs(
    visual_placements: object,
    visual_assets: object,
) -> BuildOperationFailure | None:
    """Fail closed on caller-supplied visual inputs (T050 durability boundary).

    A BuildRecord is only honest evidence when everything it names can be
    reopened from durable authority after process loss. T001-T050 persists no
    exact ``VisualPlacement`` values and no asset bytes, so a build driven by
    caller-supplied visuals could not be re-derived in a fresh process and must
    not be recorded as if it could. Durable visual-build ownership is deferred
    to the later visual lifecycle; until then only the empty visual tuple path
    produces durable final builds.
    """

    if type(visual_placements) is not tuple:
        return _failure("invalid_build_input")
    if visual_placements:
        return _failure("ephemeral_visual_inputs_rejected")
    if visual_assets is not None:
        if not isinstance(visual_assets, Mapping):
            return _failure("invalid_build_input")
        if len(visual_assets) > 0:
            return _failure("ephemeral_visual_inputs_rejected")
    return None


def _verify_exact_build_identity(
    build_record: BuildRecord,
    job: CourseJobRecord,
    workflow_state: WorkflowState,
    *,
    require_job_pointer: bool,
    canonical: _CanonicalBuildInputs | None = None,
) -> BuildOperationFailure | None:
    """Verify one exact BuildRecord <-> Job <-> WorkflowState <-> inputs binding.

    This is the single B5 identity authority shared by the ``build_pdf``
    existing-record shortcut, ``get_artifact`` (cache hit and cache miss alike),
    and ``reconcile_job_build_projection``. It adds no state machine of its own:
    WorkflowState stays authoritative for workflow progress and BuildRecord
    stays authoritative for successful build evidence; this only proves they
    agree. ``require_job_pointer`` is False exactly once -- while reconciling a
    Job whose pointer has not landed yet -- and ``canonical`` is supplied
    whenever the deterministic inputs have been recomputed.
    """

    if (
        type(build_record) is not BuildRecord
        or type(job) is not CourseJobRecord
        or type(workflow_state) is not WorkflowState
    ):
        return _failure("job_build_identity_mismatch")

    if build_record.status != "succeeded":
        return _failure("build_not_succeeded")
    if build_record.pdf_sha256 is None:
        return _failure("job_build_identity_mismatch")
    if build_record.job_id != job.job_id:
        return _failure("job_build_identity_mismatch")

    if workflow_state.stage != "completed" or workflow_state.disposition != "completed":
        return _failure("workflow_not_completed")
    if build_record.workflow_revision != workflow_state.revision:
        return _failure("job_build_identity_mismatch")

    if require_job_pointer:
        if (
            job.completed_build_id != build_record.build_id
            or job.completed_build_sha256 != build_record.bundle_hash
        ):
            return _failure("job_build_identity_mismatch")

    if canonical is not None:
        if (
            build_record.bundle_hash != canonical.bundle_hash
            or build_record.document_refs != canonical.document_refs
            or build_record.placement_refs != canonical.placement_refs
            or build_record.asset_refs != canonical.asset_refs
        ):
            return _failure("job_build_identity_mismatch")

    return None


def _compile_bundle_through_accepted_build(
    context: ReopenedCourseWorkflowContext,
    *,
    document_store: LocalLectureDocumentStore,
    visual_placements: tuple[VisualPlacement, ...],
    assets_map: Mapping[AssetReference, bytes],
) -> CompiledPdf | BuildOperationFailure:
    """Produce course PDF bytes through the accepted T026 build operation.

    T050 owns durable build evidence, not a second renderer or compiler: PDF
    production stays entirely inside `build_reopened_course_pdf_with_visuals`,
    which reopens the same accepted documents and drives the unchanged T025
    composition and T024 compilation.
    """

    built = build_reopened_course_pdf_with_visuals(
        context,
        document_store=document_store,
        placements=visual_placements,
        asset_bytes=assets_map,
    )
    if type(built) is VisualCompositionFailure:
        return _failure("composition_failed")
    if type(built) is not CompiledPdf:
        code = built.diagnostics[0].code if getattr(built, "diagnostics", ()) else ""
        return _failure(code if code in ("compiler_missing_glyph", "compiler_fatal_diagnostic", "compiler_overflow") else "compilation_failed")
    return built


def build_pdf(
    job_id: str,
    *,
    job_store: LocalCourseJobStore,
    association_store: LocalCourseWorkflowAssociationStore,
    workflow_store: LocalWorkflowStateStore,
    policy_store: LocalPolicyContentStore,
    document_store: LocalLectureDocumentStore,
    build_store: LocalBuildRecordStore,
    cache_root: Path,
    course_store: LocalCourseStore | None = None,
    source_store: LocalSourceEvidenceStore | None = None,
    semantic_store: LocalSemanticWorkStore | None = None,
    visual_placements: tuple[VisualPlacement, ...] = (),
    visual_assets: Mapping[AssetReference, bytes] | None = None,
) -> BuildRecord | BuildOperationFailure:
    """Build course PDF, persist authoritative BuildRecord, write cache, and advance Job."""

    if type(job_id) is not str:
        raise TypeError("job_id must be exactly str")

    visual_rejection = _reject_ephemeral_visual_inputs(visual_placements, visual_assets)
    if visual_rejection is not None:
        return visual_rejection

    try:
        job_res = job_store.load(job_id)
        if type(job_res) is CourseJobPersistenceFailure:
            return _failure("job_not_found")
        if type(job_res) is not CourseJobRecord:
            return _failure("job_store_failed")
        job: CourseJobRecord = job_res

        association = CourseWorkflowAssociation(
            COURSE_WORKFLOW_ASSOCIATION_VERSION,
            job.course_reference,
            job.workflow_id,
        )
        context = reopen_course_workflow_context(
            association,
            association_store=association_store,
            workflow_store=workflow_store,
            policy_store=policy_store,
        )
        if type(context) is not ReopenedCourseWorkflowContext:
            return _failure("course_not_found")

        wf_state = context.workflow_state
        if wf_state.stage != "completed" or wf_state.disposition != "completed":
            return _failure("workflow_not_completed")
        if job.quality_mode == "review":
            if semantic_store is None:
                return _failure("semantic_review_pending")
            from .semantic_operations import SemanticOperationFailure, derive_review_progress

            review = derive_review_progress(wf_state, job, semantic_store)
            if isinstance(review, SemanticOperationFailure) or review == "review_pending":
                return _failure("semantic_review_pending")
            if review != "semantic_final":
                return _failure("review_corrections_pending")

        # Reopen accepted documents
        accepted_docs = reopen_accepted_lecture_documents(
            context,
            document_store=document_store,
        )
        if type(accepted_docs) is AcceptedLectureDocumentReopenFailure:
            return _failure("accepted_documents_unavailable")
        if type(accepted_docs) is not tuple or not accepted_docs:
            return _failure("accepted_documents_unavailable")

        # Compose compiler input bundle
        try:
            visual_placements, assets_map = derive_snippets(accepted_docs,
                context.workflow_state.source_evidence + context.workflow_state.reviewed_source_evidence, source_store)
        except ValueError:
            return _failure("source_snippet_invalid")
        composition = compose_course_compiler_input(
            accepted_docs,
            placements=visual_placements,
            asset_bytes=assets_map,
        )
        if type(composition) is VisualCompositionFailure:
            return _failure("composition_failed")
        if type(composition) is not CompilerInputBundle:
            return _failure("composition_failed")
        bundle: CompilerInputBundle = composition

        bundle_hash = compute_bundle_hash(bundle)
        canonical = _canonical_build_inputs(
            accepted_docs,
            visual_placements=visual_placements,
            bundle_hash=bundle_hash,
        )

        # An existing build pointer is build authority, not a hint. Any pointer
        # at all forces exact B5 validation before it is honoured, and a pointer
        # that contradicts the current build inputs is corruption, never an
        # invitation to silently mint a replacement BuildRecord: a missing,
        # failed, foreign, wrong-revision, or wrong-input record fails closed
        # with no new record and no Job mutation.
        if job.completed_build_id is not None or job.completed_build_sha256 is not None:
            if job.completed_build_id is None:
                return _failure("job_build_identity_mismatch")
            existing_build = build_store.load(job.completed_build_id)
            if type(existing_build) is not BuildRecord:
                return _failure("job_build_identity_mismatch")
            verdict = _verify_exact_build_identity(
                existing_build,
                job,
                wf_state,
                require_job_pointer=True,
                canonical=canonical,
            )
            if verdict is not None:
                # Every contradiction on this path is one thing: the Job claims
                # build authority that the durable evidence does not support.
                return _failure("job_build_identity_mismatch")

            # Repopulate the memoized cache only with bytes that hash to the
            # immutable recorded digest.
            cache_path = _safe_cache_path(cache_root, bundle_hash)
            if isinstance(cache_path, Path) and not cache_path.exists():
                rebuilt = _compile_bundle_through_accepted_build(
                    context,
                    document_store=document_store,
                    visual_placements=visual_placements,
                    assets_map=assets_map,
                )
                if type(rebuilt) is BuildOperationFailure:
                    return rebuilt
                if _sha256_hex(rebuilt.pdf_content) != existing_build.pdf_sha256:
                    return _failure("artifact_integrity_mismatch")
                _write_cache_atomically(cache_root, cache_path, rebuilt.pdf_content)
            return existing_build

        # Produce the PDF through the accepted build operation (no second pipeline)
        compile_res = _compile_bundle_through_accepted_build(
            context,
            document_store=document_store,
            visual_placements=visual_placements,
            assets_map=assets_map,
        )
        if type(compile_res) is BuildOperationFailure:
            return compile_res
        compiled_pdf: CompiledPdf = compile_res

        # Persist BuildRecord. bundle_hash is the deterministic INPUT identity;
        # pdf_sha256 is the immutable DERIVED byte integrity of this exact
        # artifact, and the two are never interchanged.
        build_id = _generate_build_id()
        build_record = BuildRecord(
            build_id=build_id,
            job_id=job.job_id,
            workflow_revision=wf_state.revision,
            document_refs=canonical.document_refs,
            placement_refs=canonical.placement_refs,
            asset_refs=canonical.asset_refs,
            bundle_hash=bundle_hash,
            pdf_sha256=_sha256_hex(compiled_pdf.pdf_content),
            created_at=_now_iso(),
            status="succeeded",
            diagnostics=(),
        )
        saved_build = build_store.save(build_record)
        if type(saved_build) is BuildPersistenceFailure:
            return _failure("build_store_failed")

        # Write memoized cache (non-authoritative; failure never fails the build)
        cache_path = _safe_cache_path(cache_root, bundle_hash)
        if isinstance(cache_path, Path):
            _write_cache_atomically(cache_root, cache_path, compiled_pdf.pdf_content)

        # Advance Job to completed with build pointers
        updated_job = CourseJobRecord(
            job_id=job.job_id,
            course_reference=job.course_reference,
            workflow_id=job.workflow_id,
            created_at=job.created_at,
            created_revision=job.created_revision,
            current_revision=wf_state.revision,
            metadata_revision=job.metadata_revision + 1,
            status="completed",
            current_stage="completed",
            current_disposition="completed",
            ai_mode=job.ai_mode,
            quality_mode=job.quality_mode,
            retry_count=job.retry_count,
            failure_code=None,
            completed_build_id=build_id,
            completed_build_sha256=bundle_hash,
        )
        saved_job = job_store.save(updated_job)
        if type(saved_job) is CourseJobPersistenceFailure:
            return _failure("job_store_failed")

        return build_record

    except Exception:
        return _failure("build_operation_exception")


def get_artifact(
    build_id: str,
    *,
    build_store: LocalBuildRecordStore,
    job_store: LocalCourseJobStore,
    association_store: LocalCourseWorkflowAssociationStore,
    workflow_store: LocalWorkflowStateStore,
    policy_store: LocalPolicyContentStore,
    document_store: LocalLectureDocumentStore,
    cache_root: Path,
    course_store: LocalCourseStore | None = None,
    source_store: LocalSourceEvidenceStore | None = None,
    visual_placements: tuple[VisualPlacement, ...] = (),
    visual_assets: Mapping[AssetReference, bytes] | None = None,
) -> bytes | BuildOperationFailure:
    """Retrieve compiled PDF bytes by build_id, returning cached bytes or re-deriving deterministically.

    The frozen artifact contract is validated before any bytes leave this
    function, on the cache-hit path exactly as on the cache-miss path: the
    BuildRecord, the Job, the current WorkflowState, and the exact canonical
    deterministic inputs must all agree, and the returned bytes must hash to the
    immutable ``BuildRecord.pdf_sha256``. A stale or cross-Job BuildRecord is
    never downloadable merely because a cache file happens to exist.
    """

    if type(build_id) is not str:
        raise TypeError("build_id must be exactly str")

    visual_rejection = _reject_ephemeral_visual_inputs(visual_placements, visual_assets)
    if visual_rejection is not None:
        return visual_rejection

    try:
        build_res = build_store.load(build_id)
        if type(build_res) is BuildPersistenceFailure:
            return _failure("build_not_found")
        if type(build_res) is not BuildRecord:
            return _failure("build_store_failed")
        build_rec: BuildRecord = build_res

        if build_rec.status != "succeeded":
            return _failure("build_not_succeeded")

        # Identity first: load the owning Job and its authoritative workflow,
        # and recompute the exact deterministic inputs, before the cache is even
        # consulted.
        job_res = job_store.load(build_rec.job_id)
        if type(job_res) is not CourseJobRecord:
            return _failure("job_store_failed")
        job: CourseJobRecord = job_res

        association = CourseWorkflowAssociation(
            COURSE_WORKFLOW_ASSOCIATION_VERSION,
            job.course_reference,
            job.workflow_id,
        )
        context = reopen_course_workflow_context(
            association,
            association_store=association_store,
            workflow_store=workflow_store,
            policy_store=policy_store,
        )
        if type(context) is not ReopenedCourseWorkflowContext:
            return _failure("course_not_found")
        wf_state = context.workflow_state

        accepted_docs = reopen_accepted_lecture_documents(
            context,
            document_store=document_store,
        )
        if type(accepted_docs) is AcceptedLectureDocumentReopenFailure:
            return _failure("accepted_documents_unavailable")
        if type(accepted_docs) is not tuple or not accepted_docs:
            return _failure("accepted_documents_unavailable")

        try:
            visual_placements, assets_map = derive_snippets(accepted_docs,
                context.workflow_state.source_evidence + context.workflow_state.reviewed_source_evidence, source_store)
        except ValueError:
            return _failure("source_snippet_invalid")
        composition = compose_course_compiler_input(
            accepted_docs,
            placements=visual_placements,
            asset_bytes=assets_map,
        )
        if type(composition) is not CompilerInputBundle:
            return _failure("composition_failed")
        bundle: CompilerInputBundle = composition

        rederived_bundle_hash = compute_bundle_hash(bundle)
        canonical = _canonical_build_inputs(
            accepted_docs,
            visual_placements=visual_placements,
            bundle_hash=rederived_bundle_hash,
        )
        verdict = _verify_exact_build_identity(
            build_rec,
            job,
            wf_state,
            require_job_pointer=True,
            canonical=canonical,
        )
        if verdict is not None:
            return verdict

        cache_path = _safe_cache_path(cache_root, build_rec.bundle_hash)
        if type(cache_path) is BuildOperationFailure:
            return cache_path
        assert isinstance(cache_path, Path)

        # Reject a symlinked cache entry outright: the memoized path must be a
        # real file inside the cache root, never an indirection to elsewhere.
        raw_cache_path = cache_root / f"{build_rec.bundle_hash}.pdf"
        if os.path.islink(raw_cache_path) or os.path.islink(cache_path):
            return _failure("cache_path_traversal_rejected")

        if cache_path.exists() and cache_path.is_file():
            try:
                pdf_bytes = cache_path.read_bytes()
            except Exception:
                pdf_bytes = b""  # Unreadable cache falls through to re-derivation
            if _cached_artifact_is_usable(pdf_bytes, build_rec.pdf_sha256):
                return pdf_bytes
            # Corrupt, truncated, or substituted entry: discard it rather than
            # serving it. A structurally valid but different PDF is exactly as
            # unusable here as a truncated one.
            try:
                cache_path.unlink()
            except Exception:
                pass

        # Cache miss: re-derive deterministically from the persisted inputs that
        # were already validated above.
        compile_res = _compile_bundle_through_accepted_build(
            context,
            document_store=document_store,
            visual_placements=visual_placements,
            assets_map=assets_map,
        )
        if type(compile_res) is BuildOperationFailure:
            return compile_res

        pdf_bytes = compile_res.pdf_content
        if _sha256_hex(pdf_bytes) != build_rec.pdf_sha256:
            return _failure("artifact_integrity_mismatch")

        # Repopulate the memoized cache; a cache write failure never withholds
        # the artifact, because the cache is derivative and not authoritative.
        _write_cache_atomically(cache_root, cache_path, pdf_bytes)

        return pdf_bytes

    except Exception:
        return _failure("build_operation_exception")


def get_artifact_history(
    job_id: str,
    *,
    build_store: LocalBuildRecordStore,
) -> tuple[BuildRecord, ...] | BuildOperationFailure:
    """Return all BuildRecord values for one job in chronological order."""

    if type(job_id) is not str:
        raise TypeError("job_id must be exactly str")
    try:
        res = build_store.list_for_job(job_id)
        if type(res) is BuildPersistenceFailure:
            return _failure("build_store_failed")
        if type(res) is not tuple:
            return _failure("build_store_failed")
        return res
    except Exception:
        return _failure("build_operation_exception")


def preview_source_pdf_page(
    course_id: str,
    source_id: str,
    page_number: int,
    *,
    course_store: LocalCourseStore,
    source_store: LocalSourceEvidenceStore,
) -> PdfPagePreviewResult:
    """Render one complete source PDF page preview in 144dpi PNG pixel frame."""
    from .pdf_visual_extraction import PdfPagePreviewFailure, PdfPagePreviewDiagnostic

    if type(course_id) is not str or type(source_id) is not str or type(page_number) is not int or page_number < 1:
        return PdfPagePreviewFailure(
            status="pdf_page_preview_failed",
            diagnostics=(PdfPagePreviewDiagnostic("source_bytes_mismatch", "input", "The source PDF bytes do not match the page reference."),),
        )

    course = course_store.load(course_id)
    if type(course) is not CourseRecord:
        return PdfPagePreviewFailure(
            status="pdf_page_preview_failed",
            diagnostics=(PdfPagePreviewDiagnostic("source_bytes_mismatch", "input", "The source PDF bytes do not match the page reference."),),
        )

    source_ref = next((ref for ref in course.source_refs if ref.source_id == source_id), None)
    if source_ref is None:
        return PdfPagePreviewFailure(
            status="pdf_page_preview_failed",
            diagnostics=(PdfPagePreviewDiagnostic("source_bytes_mismatch", "input", "The source PDF bytes do not match the page reference."),),
        )

    payload = source_store.load(source_ref)
    if type(payload) is not SourceEvidencePayload:
        return PdfPagePreviewFailure(
            status="pdf_page_preview_failed",
            diagnostics=(PdfPagePreviewDiagnostic("source_bytes_mismatch", "input", "The source PDF bytes do not match the page reference."),),
        )

    page_ref = PdfPageReference(
        PDF_PAGE_REFERENCE_VERSION,
        source_ref,
        page_number,
    )
    return render_pdf_page_preview(payload.payload, page_ref)


def extract_source_pdf_region(
    course_id: str,
    source_id: str,
    page_number: int,
    left_px: int,
    top_px: int,
    width_px: int,
    height_px: int,
    *,
    course_store: LocalCourseStore,
    source_store: LocalSourceEvidenceStore,
) -> PdfVisualExtractionResult:
    """Extract one CropBox pixel region from source evidence as exact PNG bytes."""
    from .pdf_visual_extraction import PdfVisualExtractionFailure, PdfVisualExtractionDiagnostic

    if (
        type(course_id) is not str
        or type(source_id) is not str
        or type(page_number) is not int
        or page_number < 1
        or type(left_px) is not int
        or type(top_px) is not int
        or type(width_px) is not int
        or type(height_px) is not int
    ):
        return PdfVisualExtractionFailure(
            status="pdf_visual_extraction_failed",
            diagnostics=(PdfVisualExtractionDiagnostic("source_bytes_mismatch", "input", "The source PDF bytes do not match the page reference."),),
        )

    course = course_store.load(course_id)
    if type(course) is not CourseRecord:
        return PdfVisualExtractionFailure(
            status="pdf_visual_extraction_failed",
            diagnostics=(PdfVisualExtractionDiagnostic("source_bytes_mismatch", "input", "The source PDF bytes do not match the page reference."),),
        )

    source_ref = next((ref for ref in course.source_refs if ref.source_id == source_id), None)
    if source_ref is None:
        return PdfVisualExtractionFailure(
            status="pdf_visual_extraction_failed",
            diagnostics=(PdfVisualExtractionDiagnostic("source_bytes_mismatch", "input", "The source PDF bytes do not match the page reference."),),
        )

    payload = source_store.load(source_ref)
    if type(payload) is not SourceEvidencePayload:
        return PdfVisualExtractionFailure(
            status="pdf_visual_extraction_failed",
            diagnostics=(PdfVisualExtractionDiagnostic("source_bytes_mismatch", "input", "The source PDF bytes do not match the page reference."),),
        )

    page_ref = PdfPageReference(
        PDF_PAGE_REFERENCE_VERSION,
        source_ref,
        page_number,
    )
    return extract_pdf_page_region(
        payload.payload,
        page_ref,
        left_px=left_px,
        top_px=top_px,
        width_px=width_px,
        height_px=height_px,
    )


def reconcile_job_build_projection(
    job_id: str,
    *,
    job_store: LocalCourseJobStore,
    build_store: LocalBuildRecordStore,
    association_store: LocalCourseWorkflowAssociationStore,
    workflow_store: LocalWorkflowStateStore,
    policy_store: LocalPolicyContentStore,
    document_store: LocalLectureDocumentStore,
    source_store: LocalSourceEvidenceStore | None = None,
) -> CourseJobRecord | BuildOperationFailure:
    """Idempotently repair a Job whose BuildRecord landed but whose pointer did not.

    This is the B3 recovery path: a successful BuildRecord is durable evidence,
    so a Job left at `deterministic_building` by a crash before the pointer
    update is advanced to `completed` here. Reconciliation is strictly
    fail-closed on identity (B5): before a BuildRecord is adopted as Job build
    authority the exact durable deterministic inputs are reopened here -- the
    accepted lecture documents, the composed compiler bundle, and the canonical
    document/placement/asset references and bundle hash derived from them -- and
    the one shared identity authority is then called with that exact canonical
    input set. Adopting a record on Job/revision agreement alone would let a
    record naming inputs that were never built (or, under the T050 non-visual
    durable boundary, visual inputs that cannot be reopened at all) become Job
    build authority. A build from another Job, from a superseded revision, or
    from different inputs is never used to repair this Job.
    """

    if type(job_id) is not str:
        raise TypeError("job_id must be exactly str")

    try:
        job_res = job_store.load(job_id)
        if type(job_res) is not CourseJobRecord:
            return _failure("job_not_found")
        job: CourseJobRecord = job_res

        association = CourseWorkflowAssociation(
            COURSE_WORKFLOW_ASSOCIATION_VERSION,
            job.course_reference,
            job.workflow_id,
        )
        context = reopen_course_workflow_context(
            association,
            association_store=association_store,
            workflow_store=workflow_store,
            policy_store=policy_store,
        )
        if type(context) is not ReopenedCourseWorkflowContext:
            return _failure("course_not_found")
        wf_state = context.workflow_state

        latest_build = build_store.latest_successful_for_job(job_id)
        if type(latest_build) is BuildPersistenceFailure:
            return _failure("build_store_failed")
        if type(latest_build) is not BuildRecord:
            # No durable build evidence: nothing to reconcile, Job stays as is.
            return job

        # Reopen the exact durable deterministic build inputs before this
        # record is allowed to become Job build authority. This is the same
        # accepted path build_pdf and get_artifact use -- reopen the accepted
        # lecture documents, compose the compiler bundle, derive the canonical
        # references and bundle hash -- so reconciliation proves the record
        # against recomputed inputs rather than trusting its own fields.
        accepted_docs = reopen_accepted_lecture_documents(
            context,
            document_store=document_store,
        )
        if type(accepted_docs) is AcceptedLectureDocumentReopenFailure:
            return _failure("accepted_documents_unavailable")
        if type(accepted_docs) is not tuple or not accepted_docs:
            return _failure("accepted_documents_unavailable")

        try:
            visual_placements, assets_map = derive_snippets(accepted_docs,
                context.workflow_state.source_evidence + context.workflow_state.reviewed_source_evidence, source_store)
        except ValueError:
            return _failure("source_snippet_invalid")
        composition = compose_course_compiler_input(accepted_docs,
            placements=visual_placements, asset_bytes=assets_map)
        if type(composition) is not CompilerInputBundle:
            return _failure("composition_failed")
        canonical = _canonical_build_inputs(
            accepted_docs,
            visual_placements=visual_placements,
            bundle_hash=compute_bundle_hash(composition),
        )

        # B5 via the one shared exact-build-identity authority: never repair
        # across Job identity, workflow revision, or canonical build inputs. The
        # Job pointer is required to already name this exact build only once the
        # Job claims completion; this is the single place where a not-yet-landed
        # pointer is legitimate.
        already_completed = job.status == "completed"
        verdict = _verify_exact_build_identity(
            latest_build,
            job,
            wf_state,
            require_job_pointer=already_completed,
            canonical=canonical,
        )
        if verdict is not None:
            return _failure("job_build_identity_mismatch")

        if already_completed:
            return job

        updated_job = CourseJobRecord(
            job_id=job.job_id,
            course_reference=job.course_reference,
            workflow_id=job.workflow_id,
            created_at=job.created_at,
            created_revision=job.created_revision,
            current_revision=wf_state.revision,
            metadata_revision=job.metadata_revision + 1,
            status="completed",
            current_stage="completed",
            current_disposition="completed",
            ai_mode=job.ai_mode,
            quality_mode=job.quality_mode,
            retry_count=job.retry_count,
            failure_code=None,
            completed_build_id=latest_build.build_id,
            completed_build_sha256=latest_build.bundle_hash,
        )
        saved = job_store.save(updated_job)
        if type(saved) is not CourseJobRecord:
            return _failure("job_store_failed")
        return saved
    except Exception:
        return _failure("build_operation_exception")
