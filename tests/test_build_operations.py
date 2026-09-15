"""Tests for coarse build operations, PDF retrieval, memoized cache, and crash recovery (T050)."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from course_compiler.assembly import LogicalTexFile
from course_compiler.asset import AssetReference
from course_compiler.build_operations import (
    BuildOperationDiagnostic,
    BuildOperationFailure,
    _safe_cache_path,
    _sha256_hex,
    build_pdf,
    compute_bundle_hash,
    extract_source_pdf_region,
    get_artifact,
    get_artifact_history,
    preview_source_pdf_page,
    reconcile_job_build_projection,
)
from course_compiler.build_persistence import (
    BuildDiagnostic,
    BuildRecord,
    LocalBuildRecordStore,
    open_build_record_store,
)
from course_compiler.compilation import (
    CompilerCompanionFile,
    CompilerInputBundle,
)
from course_compiler.course_job_persistence import (
    CourseJobRecord,
    LocalCourseJobStore,
    open_course_job_store,
)
from course_compiler.course_operations import attach_source, create_course
from course_compiler.course_persistence import (
    CourseRecord,
    LocalCourseStore,
    open_course_store,
)
from course_compiler.course_workflow_persistence import (
    LocalCourseWorkflowAssociationStore,
    open_course_workflow_association_store,
)
from course_compiler.lecture_document_persistence import (
    LocalLectureDocumentStore,
    open_lecture_document_store,
)
from course_compiler.pdf_visual_extraction import (
    ExtractedPdfVisual,
    RenderedPdfPagePreview,
)
from course_compiler.policy_persistence import (
    LocalPolicyContentStore,
    open_policy_content_store,
)
from course_compiler.rendering import DocumentReference
from course_compiler.semantic_operations import (
    SemanticOperationFailure,
    request_semantic_work,
    start_generation,
    submit_owner_map_decision,
    submit_owner_priority_decision,
    submit_semantic_result,
)
from course_compiler.semantic_work import ScriptProvider
from course_compiler.semantic_work_persistence import (
    LocalSemanticWorkStore,
    open_semantic_work_store,
)
from course_compiler.source_persistence import (
    LocalSourceEvidenceStore,
    open_source_evidence_store,
)
from course_compiler.visual_placement import VisualPlacement
from course_compiler.workflow import (
    WorkflowState,
    map_subject_sha256,
    priority_subject_sha256,
)
from course_compiler.workflow_artifact_persistence import (
    LocalWorkflowArtifactStore,
    open_workflow_artifact_store,
)
from course_compiler.workflow_persistence import (
    LocalWorkflowStateStore,
    open_workflow_state_store,
)

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_ISOLATED_BASE = REPOSITORY_ROOT / "local-data" / "test-tmp-build-ops"
_ISOLATED_BASE.mkdir(parents=True, exist_ok=True)
_HOLDER = "h-build-ops"


def _tmp_dir() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(dir=str(_ISOLATED_BASE), prefix="test-build-ops-")


def _make_sample_pdf_bytes() -> bytes:
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n"
        b"3 0 obj<</Type/Page/MediaBox[0 0 300 144]/Parent 2 0 R/Resources<<>>>>endobj\n"
        b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n0000000052 00000 n \n0000000102 00000 n \n"
        b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n178\n%%EOF\n"
    )


class TestBundleHash(unittest.TestCase):
    def test_bundle_hash_deterministic(self) -> None:
        root_tex = LogicalTexFile("Course_Study_Lectures.tex", "\\begin{document}Hello\\end{document}")
        comp1 = CompilerCompanionFile("assets/1.png", b"image1bytes")
        comp2 = CompilerCompanionFile("assets/2.png", b"image2bytes")

        bundle_a = CompilerInputBundle(root_tex, (comp1, comp2))
        bundle_b = CompilerInputBundle(root_tex, (comp2, comp1))

        hash_a = compute_bundle_hash(bundle_a)
        hash_b = compute_bundle_hash(bundle_b)

        self.assertEqual(hash_a, hash_b)
        self.assertEqual(len(hash_a), 64)

    def test_bundle_hash_changes_with_content(self) -> None:
        root_a = LogicalTexFile("Course_Study_Lectures.tex", "Content A")
        root_b = LogicalTexFile("Course_Study_Lectures.tex", "Content B")

        bundle_a = CompilerInputBundle(root_a, ())
        bundle_b = CompilerInputBundle(root_b, ())

        self.assertNotEqual(compute_bundle_hash(bundle_a), compute_bundle_hash(bundle_b))


class TestBuildOperations(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _tmp_dir()
        self.p = Path(self.tmp.name)
        self.course_store = open_course_store(self.p / "courses.sqlite3")
        self.assoc_store = open_course_workflow_association_store(self.p / "assoc.sqlite3")
        self.source_store = open_source_evidence_store(self.p / "sources.sqlite3")
        self.workflow_store = open_workflow_state_store(self.p / "workflows.sqlite3")
        self.job_store = open_course_job_store(self.p / "jobs.sqlite3")
        self.policy_store = open_policy_content_store(self.p / "policies.sqlite3")
        self.artifact_store = open_workflow_artifact_store(self.p / "artifacts.sqlite3")
        self.doc_store = open_lecture_document_store(self.p / "docs.sqlite3")
        self.semantic_store = open_semantic_work_store(self.p / "semantics.sqlite3")
        self.build_store = open_build_record_store(self.p / "builds.sqlite3")
        self.cache_root = self.p / "cache"

        assert isinstance(self.course_store, LocalCourseStore)
        assert isinstance(self.assoc_store, LocalCourseWorkflowAssociationStore)
        assert isinstance(self.source_store, LocalSourceEvidenceStore)
        assert isinstance(self.workflow_store, LocalWorkflowStateStore)
        assert isinstance(self.job_store, LocalCourseJobStore)
        assert isinstance(self.policy_store, LocalPolicyContentStore)
        assert isinstance(self.artifact_store, LocalWorkflowArtifactStore)
        assert isinstance(self.doc_store, LocalLectureDocumentStore)
        assert isinstance(self.semantic_store, LocalSemanticWorkStore)
        assert isinstance(self.build_store, LocalBuildRecordStore)

    def tearDown(self) -> None:
        self.course_store.close()
        self.assoc_store.close()
        self.source_store.close()
        self.workflow_store.close()
        self.job_store.close()
        self.policy_store.close()
        self.artifact_store.close()
        self.doc_store.close()
        self.semantic_store.close()
        self.build_store.close()
        self.tmp.cleanup()

    def _drive_step(self, job_id: str, provider: ScriptProvider, *, clock_index: int) -> object:
        req = request_semantic_work(
            job_id,
            job_store=self.job_store,
            workflow_store=self.workflow_store,
            semantic_store=self.semantic_store,
            holder_id=_HOLDER,
            clock=f"2026-01-01T00:{clock_index:02d}:00Z",
        )
        if isinstance(req, SemanticOperationFailure):
            job = self.job_store.load(job_id)
            assert isinstance(job, CourseJobRecord)
            ws = self.workflow_store.load(job.workflow_id)
            assert isinstance(ws, WorkflowState)
            if req.diagnostics[0].code == "owner_decision_required":
                if ws.stage == "priority_approval":
                    assert ws.priority_basis is not None
                    subj = priority_subject_sha256(ws.priority_basis, ws.policies.priority_basis)
                    return submit_owner_priority_decision(
                        job_id,
                        approve=True,
                        subject_sha256=subj,
                        job_store=self.job_store,
                        workflow_store=self.workflow_store,
                        semantic_store=self.semantic_store,
                        association_store=self.assoc_store,
                        source_store=self.source_store,
                        policy_store=self.policy_store,
                        artifact_store=self.artifact_store,
                        document_store=self.doc_store,
                        clock=f"2026-01-01T00:{clock_index:02d}:10Z",
                    )
                if ws.stage == "map_approval":
                    assert ws.lecture_map is not None
                    subj = map_subject_sha256(ws.lecture_map, ws.policies.lecture_mapping)
                    return submit_owner_map_decision(
                        job_id,
                        approve=True,
                        subject_sha256=subj,
                        job_store=self.job_store,
                        workflow_store=self.workflow_store,
                        semantic_store=self.semantic_store,
                        association_store=self.assoc_store,
                        source_store=self.source_store,
                        policy_store=self.policy_store,
                        artifact_store=self.artifact_store,
                        document_store=self.doc_store,
                        clock=f"2026-01-01T00:{clock_index:02d}:10Z",
                    )
            return req

        res = provider.execute(req)
        sub = submit_semantic_result(
            job_id,
            res,
            job_store=self.job_store,
            workflow_store=self.workflow_store,
            semantic_store=self.semantic_store,
            association_store=self.assoc_store,
            source_store=self.source_store,
            policy_store=self.policy_store,
            artifact_store=self.artifact_store,
            document_store=self.doc_store,
            holder_id=_HOLDER,
            clock=f"2026-01-01T00:{clock_index:02d}:30Z",
        )
        if isinstance(sub, SemanticOperationFailure):
            job = self.job_store.load(job_id)
            assert isinstance(job, CourseJobRecord)
            ws_cur = self.workflow_store.load(job.workflow_id)
            assert isinstance(ws_cur, WorkflowState)
            if sub.diagnostics[0].code == "owner_decision_required":
                if req.kind == "exam_priority_assessment":
                    assert ws_cur.priority_basis is not None
                    subj = priority_subject_sha256(ws_cur.priority_basis, ws_cur.policies.priority_basis)
                    return submit_owner_priority_decision(
                        job_id,
                        approve=True,
                        subject_sha256=subj,
                        job_store=self.job_store,
                        workflow_store=self.workflow_store,
                        semantic_store=self.semantic_store,
                        association_store=self.assoc_store,
                        source_store=self.source_store,
                        policy_store=self.policy_store,
                        artifact_store=self.artifact_store,
                        document_store=self.doc_store,
                        clock=f"2026-01-01T00:{clock_index:02d}:40Z",
                    )
                if req.kind == "lecture_map_generation":
                    assert ws_cur.lecture_map is not None
                    subj = map_subject_sha256(ws_cur.lecture_map, ws_cur.policies.lecture_mapping)
                    return submit_owner_map_decision(
                        job_id,
                        approve=True,
                        subject_sha256=subj,
                        job_store=self.job_store,
                        workflow_store=self.workflow_store,
                        semantic_store=self.semantic_store,
                        association_store=self.assoc_store,
                        source_store=self.source_store,
                        policy_store=self.policy_store,
                        artifact_store=self.artifact_store,
                        document_store=self.doc_store,
                        clock=f"2026-01-01T00:{clock_index:02d}:40Z",
                    )
        return sub

    def _setup_completed_course(self, course_title: str = "Test Course") -> tuple[str, str, str]:
        ref = create_course(
            course_title,
            ai_mode="gpt",
            quality_mode="fast",
            course_store=self.course_store,
            association_store=self.assoc_store,
        )
        course_id = ref.course_id
        pdf_bytes = _make_sample_pdf_bytes()
        attach_source(
            course_id,
            pdf_bytes,
            "src-1",
            course_store=self.course_store,
            source_store=self.source_store,
        )
        job_id = start_generation(
            course_id,
            course_store=self.course_store,
            job_store=self.job_store,
            workflow_store=self.workflow_store,
            association_store=self.assoc_store,
            source_store=self.source_store,
            policy_store=self.policy_store,
            artifact_store=self.artifact_store,
            document_store=self.doc_store,
            clock="2026-01-01T00:00:00Z",
        )
        assert isinstance(job_id, str)
        provider = ScriptProvider(map_size=1)
        for i in range(12):
            self._drive_step(job_id, provider, clock_index=i)
            job = self.job_store.load(job_id)
            assert isinstance(job, CourseJobRecord)
            ws = self.workflow_store.load(job.workflow_id)
            assert isinstance(ws, WorkflowState)
            if ws.stage == "completed":
                break

        return course_id, job_id, "src-1"

    def _build(self, job_id: str):
        return build_pdf(
            job_id,
            job_store=self.job_store,
            association_store=self.assoc_store,
            workflow_store=self.workflow_store,
            policy_store=self.policy_store,
            document_store=self.doc_store,
            build_store=self.build_store,
            cache_root=self.cache_root,
        )

    def _artifact(self, build_id: str):
        return get_artifact(
            build_id,
            build_store=self.build_store,
            job_store=self.job_store,
            association_store=self.assoc_store,
            workflow_store=self.workflow_store,
            policy_store=self.policy_store,
            document_store=self.doc_store,
            cache_root=self.cache_root,
        )

    def _reconcile(self, job_id: str):
        return reconcile_job_build_projection(
            job_id,
            job_store=self.job_store,
            build_store=self.build_store,
            association_store=self.assoc_store,
            workflow_store=self.workflow_store,
            policy_store=self.policy_store,
            document_store=self.doc_store,
        )

    def _workflow_state(self, job_id: str) -> WorkflowState:
        job = self.job_store.load(job_id)
        assert isinstance(job, CourseJobRecord)
        ws = self.workflow_store.load(job.workflow_id)
        assert isinstance(ws, WorkflowState)
        return ws

    def test_build_pdf_not_completed_fails(self) -> None:
        ref = create_course(
            "Unfinished Course",
            ai_mode="gpt",
            quality_mode="fast",
            course_store=self.course_store,
            association_store=self.assoc_store,
        )
        attach_source(
            ref.course_id,
            _make_sample_pdf_bytes(),
            "src-u",
            course_store=self.course_store,
            source_store=self.source_store,
        )
        job_id = start_generation(
            ref.course_id,
            course_store=self.course_store,
            job_store=self.job_store,
            workflow_store=self.workflow_store,
            association_store=self.assoc_store,
            source_store=self.source_store,
            policy_store=self.policy_store,
            artifact_store=self.artifact_store,
            document_store=self.doc_store,
            clock="2026-01-01T00:00:00Z",
        )
        assert isinstance(job_id, str)

        res = build_pdf(
            job_id,
            job_store=self.job_store,
            association_store=self.assoc_store,
            workflow_store=self.workflow_store,
            policy_store=self.policy_store,
            document_store=self.doc_store,
            build_store=self.build_store,
            cache_root=self.cache_root,
        )
        self.assertIsInstance(res, BuildOperationFailure)
        assert isinstance(res, BuildOperationFailure)
        self.assertEqual(res.diagnostics[0].code, "workflow_not_completed")

    def test_build_pdf_success_and_cache_miss_rederives(self) -> None:
        course_id, job_id, src_id = self._setup_completed_course("Build Test Course")

        # Execute build_pdf
        res = build_pdf(
            job_id,
            job_store=self.job_store,
            association_store=self.assoc_store,
            workflow_store=self.workflow_store,
            policy_store=self.policy_store,
            document_store=self.doc_store,
            build_store=self.build_store,
            cache_root=self.cache_root,
        )
        self.assertIsInstance(res, BuildRecord)
        assert isinstance(res, BuildRecord)
        self.assertEqual(res.status, "succeeded")
        self.assertEqual(res.job_id, job_id)

        # Check job updated to completed with build pointers
        job_updated = self.job_store.load(job_id)
        assert isinstance(job_updated, CourseJobRecord)
        self.assertEqual(job_updated.status, "completed")
        self.assertEqual(job_updated.completed_build_id, res.build_id)
        self.assertEqual(job_updated.completed_build_sha256, res.bundle_hash)

        # Check memoized cache file exists
        cache_file = self.cache_root / f"{res.bundle_hash}.pdf"
        self.assertTrue(cache_file.exists())
        cached_bytes = cache_file.read_bytes()
        self.assertTrue(cached_bytes.startswith(b"%PDF"))

        # Retrieve artifact (cache hit)
        art1 = get_artifact(
            res.build_id,
            build_store=self.build_store,
            job_store=self.job_store,
            association_store=self.assoc_store,
            workflow_store=self.workflow_store,
            policy_store=self.policy_store,
            document_store=self.doc_store,
            cache_root=self.cache_root,
        )
        self.assertEqual(art1, cached_bytes)

        # B5: Delete cache file and retrieve artifact (cache miss re-derives deterministically)
        cache_file.unlink()
        self.assertFalse(cache_file.exists())

        art2 = get_artifact(
            res.build_id,
            build_store=self.build_store,
            job_store=self.job_store,
            association_store=self.assoc_store,
            workflow_store=self.workflow_store,
            policy_store=self.policy_store,
            document_store=self.doc_store,
            cache_root=self.cache_root,
        )
        self.assertIsInstance(art2, bytes)
        assert isinstance(art2, bytes)
        self.assertTrue(art2.startswith(b"%PDF"))
        # The re-derived artifact is byte-for-byte identical to the first one.
        self.assertEqual(art2, art1)
        # Verify cache file was restored
        self.assertTrue(cache_file.exists())
        self.assertEqual(cache_file.read_bytes(), art2)

    def test_b3_crash_recovery_reconcile_job_build_projection(self) -> None:
        course_id, job_id, src_id = self._setup_completed_course("Crash Recovery Course")

        res = build_pdf(
            job_id,
            job_store=self.job_store,
            association_store=self.assoc_store,
            workflow_store=self.workflow_store,
            policy_store=self.policy_store,
            document_store=self.doc_store,
            build_store=self.build_store,
            cache_root=self.cache_root,
        )
        assert isinstance(res, BuildRecord)

        # Reset job to simulate B3 crash (BuildRecord committed, Job status uncommitted)
        job_loaded = self.job_store.load(job_id)
        assert isinstance(job_loaded, CourseJobRecord)
        reset_job = CourseJobRecord(
            job_id=job_id,
            course_reference=job_loaded.course_reference,
            workflow_id=job_loaded.workflow_id,
            created_at=job_loaded.created_at,
            created_revision=job_loaded.created_revision,
            current_revision=job_loaded.current_revision,
            metadata_revision=job_loaded.metadata_revision + 1,
            status="deterministic_building",
            current_stage="completed",
            current_disposition="completed",
            ai_mode=job_loaded.ai_mode,
            quality_mode=job_loaded.quality_mode,
            retry_count=0,
            failure_code=None,
            completed_build_id=None,
            completed_build_sha256=None,
        )
        self.job_store.save(reset_job)

        # Call reconcile_job_build_projection
        repaired = self._reconcile(job_id)
        self.assertIsInstance(repaired, CourseJobRecord)
        assert isinstance(repaired, CourseJobRecord)
        self.assertEqual(repaired.status, "completed")
        self.assertEqual(repaired.completed_build_id, res.build_id)
        self.assertEqual(repaired.completed_build_sha256, res.bundle_hash)

        # Reconciliation is idempotent: repeating it changes nothing at all.
        again = self._reconcile(job_id)
        self.assertIsInstance(again, CourseJobRecord)
        assert isinstance(again, CourseJobRecord)
        self.assertEqual(again, repaired)

    # ------------------------------------------------------------------
    # Crash / recovery boundaries B1-B5
    # ------------------------------------------------------------------

    def test_b1_crash_before_build_record_persistence(self) -> None:
        """B1: compile succeeds, crash before the BuildRecord is persisted."""

        course_id, job_id, src_id = self._setup_completed_course("B1 Course")
        ws_before = self._workflow_state(job_id)

        class _CrashingSave:
            def __init__(self, store: object) -> None:
                self._store = store

            def __getattr__(self, name: str) -> object:
                return getattr(self._store, name)

            def save(self, record: object) -> object:
                raise RuntimeError("simulated crash before BuildRecord persistence")

        crashed = build_pdf(
            job_id,
            job_store=self.job_store,
            association_store=self.assoc_store,
            workflow_store=self.workflow_store,
            policy_store=self.policy_store,
            document_store=self.doc_store,
            build_store=_CrashingSave(self.build_store),  # type: ignore[arg-type]
            cache_root=self.cache_root,
        )
        self.assertIsInstance(crashed, BuildOperationFailure)

        # No successful BuildRecord exists.
        self.assertEqual(get_artifact_history(job_id, build_store=self.build_store), ())
        # The Job is not completed.
        job_after = self.job_store.load(job_id)
        assert isinstance(job_after, CourseJobRecord)
        self.assertEqual(job_after.status, "deterministic_building")
        self.assertIsNone(job_after.completed_build_id)
        self.assertIsNone(job_after.completed_build_sha256)

        # Retry rebuilds safely on the same unchanged workflow revision.
        retried = self._build(job_id)
        self.assertIsInstance(retried, BuildRecord)
        assert isinstance(retried, BuildRecord)
        self.assertEqual(retried.status, "succeeded")
        job_final = self.job_store.load(job_id)
        assert isinstance(job_final, CourseJobRecord)
        self.assertEqual(job_final.status, "completed")
        self.assertEqual(job_final.completed_build_id, retried.build_id)
        self.assertEqual(self._workflow_state(job_id).revision, ws_before.revision)

    def test_b2_crash_before_cache_write(self) -> None:
        """B2: BuildRecord persisted, crash before the cache write lands."""

        course_id, job_id, src_id = self._setup_completed_course("B2 Course")
        res = self._build(job_id)
        assert isinstance(res, BuildRecord)
        expected = self._artifact(res.build_id)
        assert isinstance(expected, bytes)

        ws_before = self._workflow_state(job_id)
        semantic_before = self.semantic_store.load_all_for_job(job_id)

        # Simulate the crash: the durable record survives, the cache write did not.
        cache_file = self.cache_root / f"{res.bundle_hash}.pdf"
        cache_file.unlink()
        # A partial temporary file from the interrupted write is inert.
        (self.cache_root / ".tmp-crashed.pdf").write_bytes(b"%PDF-1.4 partial")

        # The BuildRecord remains authoritative.
        reloaded = self.build_store.load(res.build_id)
        self.assertIsInstance(reloaded, BuildRecord)
        assert isinstance(reloaded, BuildRecord)
        self.assertEqual(reloaded, res)
        job_after = self.job_store.load(job_id)
        assert isinstance(job_after, CourseJobRecord)
        self.assertEqual(job_after.status, "completed")

        # The cache is reconstructed from persisted inputs, byte for byte.
        rebuilt = self._artifact(res.build_id)
        self.assertIsInstance(rebuilt, bytes)
        assert isinstance(rebuilt, bytes)
        self.assertEqual(rebuilt, expected)
        self.assertEqual(cache_file.read_bytes(), expected)

        # No semantic work was rerun and no new workflow revision was created.
        ws_after = self._workflow_state(job_id)
        self.assertEqual(ws_after.revision, ws_before.revision)
        self.assertEqual(self.semantic_store.load_all_for_job(job_id), semantic_before)
        # No additional BuildRecord was created.
        self.assertEqual(
            get_artifact_history(job_id, build_store=self.build_store), (res,)
        )

    def test_b4_job_pointer_intact_with_cache_deleted(self) -> None:
        """B4: Job pointer exists but the cache is deleted."""

        course_id, job_id, src_id = self._setup_completed_course("B4 Course")
        res = self._build(job_id)
        assert isinstance(res, BuildRecord)
        first = self._artifact(res.build_id)
        assert isinstance(first, bytes)

        ws_before = self._workflow_state(job_id)
        semantic_before = self.semantic_store.load_all_for_job(job_id)

        cache_file = self.cache_root / f"{res.bundle_hash}.pdf"
        cache_file.unlink()
        self.assertFalse(cache_file.exists())

        # The Job stays completed with its pointer untouched.
        job_after = self.job_store.load(job_id)
        assert isinstance(job_after, CourseJobRecord)
        self.assertEqual(job_after.status, "completed")
        self.assertEqual(job_after.completed_build_id, res.build_id)
        self.assertEqual(job_after.completed_build_sha256, res.bundle_hash)

        # get_artifact re-derives the exact same bytes.
        rederived = self._artifact(res.build_id)
        self.assertIsInstance(rederived, bytes)
        assert isinstance(rederived, bytes)
        self.assertEqual(rederived, first)

        # No new semantic work, no new workflow revision, no new build.
        ws_after = self._workflow_state(job_id)
        self.assertEqual(ws_after.revision, ws_before.revision)
        self.assertEqual(self.semantic_store.load_all_for_job(job_id), semantic_before)
        self.assertEqual(
            get_artifact_history(job_id, build_store=self.build_store), (res,)
        )

    def test_b5_cross_job_build_record_fails_closed(self) -> None:
        """B5: a BuildRecord from another Job never repairs this Job."""

        course_a, job_a, _ = self._setup_completed_course("B5 Course A")
        build_a = self._build(job_a)
        assert isinstance(build_a, BuildRecord)

        course_b, job_b, _ = self._setup_completed_course("B5 Course B")
        job_b_before = self.job_store.load(job_b)
        assert isinstance(job_b_before, CourseJobRecord)
        self.assertEqual(job_b_before.status, "deterministic_building")

        # Forge a successful BuildRecord for Job B that carries Job A's identity
        # by writing it under Job B's id but at a foreign workflow revision.
        foreign = BuildRecord(
            build_id="bld-b5-foreign-0001",
            job_id=job_b,
            workflow_revision=build_a.workflow_revision + 97,
            document_refs=build_a.document_refs,
            placement_refs=build_a.placement_refs,
            asset_refs=build_a.asset_refs,
            bundle_hash=build_a.bundle_hash,
            pdf_sha256=build_a.pdf_sha256,
            created_at="2026-01-01T00:00:00Z",
            status="succeeded",
            diagnostics=(),
        )
        self.assertIsInstance(self.build_store.save(foreign), BuildRecord)

        reconciled = self._reconcile(job_b)
        self.assertIsInstance(reconciled, BuildOperationFailure)
        assert isinstance(reconciled, BuildOperationFailure)
        self.assertEqual(
            reconciled.diagnostics[0].code, "job_build_identity_mismatch"
        )

        # Job B was not silently repaired.
        job_b_after = self.job_store.load(job_b)
        assert isinstance(job_b_after, CourseJobRecord)
        self.assertEqual(job_b_after.status, "deterministic_building")
        self.assertIsNone(job_b_after.completed_build_id)
        self.assertIsNone(job_b_after.completed_build_sha256)

    def test_b5_pointer_disagreeing_with_build_fails_closed(self) -> None:
        """B5: a completed Job whose pointer names a different build fails closed."""

        course_id, job_id, src_id = self._setup_completed_course("B5 Pointer Course")
        res = self._build(job_id)
        assert isinstance(res, BuildRecord)

        job_loaded = self.job_store.load(job_id)
        assert isinstance(job_loaded, CourseJobRecord)
        divergent = CourseJobRecord(
            job_id=job_loaded.job_id,
            course_reference=job_loaded.course_reference,
            workflow_id=job_loaded.workflow_id,
            created_at=job_loaded.created_at,
            created_revision=job_loaded.created_revision,
            current_revision=job_loaded.current_revision,
            metadata_revision=job_loaded.metadata_revision + 1,
            status="completed",
            current_stage="completed",
            current_disposition="completed",
            ai_mode=job_loaded.ai_mode,
            quality_mode=job_loaded.quality_mode,
            retry_count=job_loaded.retry_count,
            failure_code=None,
            completed_build_id="bld-someone-elses",
            completed_build_sha256="c" * 64,
        )
        self.assertIsInstance(self.job_store.save(divergent), CourseJobRecord)

        reconciled = self._reconcile(job_id)
        self.assertIsInstance(reconciled, BuildOperationFailure)
        assert isinstance(reconciled, BuildOperationFailure)
        self.assertEqual(
            reconciled.diagnostics[0].code, "job_build_identity_mismatch"
        )

    def test_cache_path_traversal_rejected(self) -> None:
        # A well-formed hash resolves to a real file inside the cache root.
        inside = _safe_cache_path(self.cache_root, "a" * 64)
        self.assertIsInstance(inside, Path)
        assert isinstance(inside, Path)
        self.assertTrue(inside.is_relative_to(self.cache_root.resolve()))

        # Traversal components can never escape the cache root.
        for hostile in ("../../etc/passwd", "..%2f..%2fetc", "../" * 8 + "secret"):
            escaped = _safe_cache_path(self.cache_root, hostile)
            if isinstance(escaped, Path):
                self.assertTrue(
                    escaped.is_relative_to(self.cache_root.resolve()),
                    f"{hostile!r} escaped the cache root",
                )
            else:
                self.assertEqual(
                    escaped.diagnostics[0].code, "cache_path_traversal_rejected"
                )

    def test_cache_symlink_escape_rejected(self) -> None:
        course_id, job_id, src_id = self._setup_completed_course("Symlink Cache Course")
        res = self._build(job_id)
        assert isinstance(res, BuildRecord)

        cache_file = self.cache_root / f"{res.bundle_hash}.pdf"
        outside = self.p / "outside-secret.pdf"
        outside.write_bytes(b"%PDF-1.4 outside the cache root\n%%EOF\n")

        cache_file.unlink()
        cache_file.symlink_to(outside)
        self.assertTrue(cache_file.is_symlink())

        art = self._artifact(res.build_id)
        self.assertIsInstance(art, BuildOperationFailure)
        assert isinstance(art, BuildOperationFailure)
        self.assertEqual(
            art.diagnostics[0].code, "cache_path_traversal_rejected"
        )
        # The out-of-root file is never read and never modified.
        self.assertEqual(
            outside.read_bytes(), b"%PDF-1.4 outside the cache root\n%%EOF\n"
        )

    def test_corrupt_cache_entry_is_not_trusted(self) -> None:
        course_id, job_id, src_id = self._setup_completed_course("Corrupt Cache Course")
        res = self._build(job_id)
        assert isinstance(res, BuildRecord)

        good = self._artifact(res.build_id)
        assert isinstance(good, bytes)

        cache_file = self.cache_root / f"{res.bundle_hash}.pdf"
        for corruption in (
            b"not a pdf at all",
            b"%PDF-1.4 truncated with no trailer",
            b"",
        ):
            cache_file.write_bytes(corruption)
            art = self._artifact(res.build_id)
            self.assertIsInstance(art, bytes)
            assert isinstance(art, bytes)
            # The corrupt bytes are never served; the artifact is re-derived.
            self.assertNotEqual(art, corruption)
            self.assertEqual(art, good)
            self.assertEqual(cache_file.read_bytes(), good)

    def test_source_page_preview_and_region_extraction(self) -> None:
        course_id, job_id, src_id = self._setup_completed_course("Visual Source Course")

        preview_res = preview_source_pdf_page(
            course_id,
            src_id,
            page_number=1,
            course_store=self.course_store,
            source_store=self.source_store,
        )
        self.assertIsInstance(preview_res, RenderedPdfPagePreview)
        assert isinstance(preview_res, RenderedPdfPagePreview)
        self.assertEqual(preview_res.format, "png")
        self.assertTrue(preview_res.content_bytes.startswith(b"\x89PNG\r\n\x1a\n"))

        extract_res = extract_source_pdf_region(
            course_id,
            src_id,
            page_number=1,
            left_px=10,
            top_px=10,
            width_px=50,
            height_px=50,
            course_store=self.course_store,
            source_store=self.source_store,
        )
        self.assertIsInstance(extract_res, ExtractedPdfVisual)
        assert isinstance(extract_res, ExtractedPdfVisual)
        self.assertEqual(extract_res.format, "png")
        self.assertEqual(extract_res.width_px, 50)
        self.assertEqual(extract_res.height_px, 50)

    def test_get_artifact_history(self) -> None:
        course_id, job_id, src_id = self._setup_completed_course("History Test Course")
        history = get_artifact_history(job_id, build_store=self.build_store)
        self.assertEqual(history, ())

        res = build_pdf(
            job_id,
            job_store=self.job_store,
            association_store=self.assoc_store,
            workflow_store=self.workflow_store,
            policy_store=self.policy_store,
            document_store=self.doc_store,
            build_store=self.build_store,
            cache_root=self.cache_root,
        )
        assert isinstance(res, BuildRecord)

        history_after = get_artifact_history(job_id, build_store=self.build_store)
        self.assertEqual(history_after, (res,))

    # ------------------------------------------------------------------
    # B5 exact build identity: build_pdf, get_artifact, cache substitution
    # ------------------------------------------------------------------

    def _mutate_job(self, job_id: str, **overrides: object) -> CourseJobRecord:
        """Persist one deliberately altered projection of an existing Job."""

        job = self.job_store.load(job_id)
        assert isinstance(job, CourseJobRecord)
        fields: dict = {
            "job_id": job.job_id,
            "course_reference": job.course_reference,
            "workflow_id": job.workflow_id,
            "created_at": job.created_at,
            "created_revision": job.created_revision,
            "current_revision": job.current_revision,
            "metadata_revision": job.metadata_revision + 1,
            "status": job.status,
            "current_stage": job.current_stage,
            "current_disposition": job.current_disposition,
            "ai_mode": job.ai_mode,
            "quality_mode": job.quality_mode,
            "retry_count": job.retry_count,
            "failure_code": job.failure_code,
            "completed_build_id": job.completed_build_id,
            "completed_build_sha256": job.completed_build_sha256,
        }
        fields.update(overrides)
        saved = self.job_store.save(CourseJobRecord(**fields))  # type: ignore[arg-type]
        assert isinstance(saved, CourseJobRecord)
        return saved

    def _forge_build_record(self, build_a: BuildRecord, **overrides: object) -> BuildRecord:
        """Persist one BuildRecord derived from a real one with altered identity."""

        fields: dict = {
            "build_id": build_a.build_id,
            "job_id": build_a.job_id,
            "workflow_revision": build_a.workflow_revision,
            "document_refs": build_a.document_refs,
            "placement_refs": build_a.placement_refs,
            "asset_refs": build_a.asset_refs,
            "bundle_hash": build_a.bundle_hash,
            "pdf_sha256": build_a.pdf_sha256,
            "created_at": build_a.created_at,
            "status": build_a.status,
            "diagnostics": build_a.diagnostics,
        }
        fields.update(overrides)
        record = BuildRecord(**fields)  # type: ignore[arg-type]
        saved = self.build_store.save(record)
        assert isinstance(saved, BuildRecord)
        return record

    def _assert_build_pdf_fails_closed(self, job_id: str) -> None:
        """build_pdf must reject, mint no BuildRecord, and mutate no Job."""

        history_before = get_artifact_history(job_id, build_store=self.build_store)
        assert isinstance(history_before, tuple)
        job_before = self.job_store.load(job_id)
        assert isinstance(job_before, CourseJobRecord)

        res = self._build(job_id)

        self.assertIsInstance(res, BuildOperationFailure)
        assert isinstance(res, BuildOperationFailure)
        self.assertEqual(res.diagnostics[0].code, "job_build_identity_mismatch")

        history_after = get_artifact_history(job_id, build_store=self.build_store)
        self.assertEqual(history_after, history_before)
        job_after = self.job_store.load(job_id)
        self.assertEqual(job_after, job_before)

    def test_b5_build_pdf_pointer_to_missing_record_fails_closed(self) -> None:
        """B5: a Job pointer naming no BuildRecord is corruption, not a rebuild request."""

        _, job_id, _ = self._setup_completed_course("B5 Missing Pointer")
        built = self._build(job_id)
        assert isinstance(built, BuildRecord)

        self._mutate_job(job_id, completed_build_id="bld-absent000001")
        self._assert_build_pdf_fails_closed(job_id)

    def test_b5_build_pdf_pointer_to_failed_record_fails_closed(self) -> None:
        """B5: a Job pointer naming a failed BuildRecord fails closed."""

        _, job_id, _ = self._setup_completed_course("B5 Failed Pointer")
        built = self._build(job_id)
        assert isinstance(built, BuildRecord)

        failed = self._forge_build_record(
            built,
            build_id="bld-failed000001",
            document_refs=(),
            pdf_sha256=None,
            status="failed",
            diagnostics=(
                BuildDiagnostic("build_not_found", "storage", "No stored build record was found."),
            ),
        )
        self._mutate_job(job_id, completed_build_id=failed.build_id)
        self._assert_build_pdf_fails_closed(job_id)

    def test_b5_build_pdf_pointer_to_other_job_record_fails_closed(self) -> None:
        """B5: a Job pointer naming another Job's BuildRecord fails closed."""

        _, job_id, _ = self._setup_completed_course("B5 Foreign Pointer")
        built = self._build(job_id)
        assert isinstance(built, BuildRecord)

        foreign = self._forge_build_record(
            built,
            build_id="bld-foreign00001",
            job_id="job-someone-else",
        )
        self._mutate_job(job_id, completed_build_id=foreign.build_id)
        self._assert_build_pdf_fails_closed(job_id)

    def test_b5_build_pdf_pointer_to_wrong_revision_fails_closed(self) -> None:
        """B5: a Job pointer naming a superseded workflow revision fails closed."""

        _, job_id, _ = self._setup_completed_course("B5 Revision Pointer")
        built = self._build(job_id)
        assert isinstance(built, BuildRecord)

        stale = self._forge_build_record(
            built,
            build_id="bld-oldrev00001",
            workflow_revision=built.workflow_revision + 7,
        )
        self._mutate_job(job_id, completed_build_id=stale.build_id)
        self._assert_build_pdf_fails_closed(job_id)

    def test_b5_build_pdf_pointer_to_wrong_bundle_hash_fails_closed(self) -> None:
        """B5: a Job pointer whose bundle hash contradicts the record fails closed."""

        _, job_id, _ = self._setup_completed_course("B5 Hash Pointer")
        built = self._build(job_id)
        assert isinstance(built, BuildRecord)

        self._mutate_job(job_id, completed_build_sha256="0" * 64)
        self._assert_build_pdf_fails_closed(job_id)

    def test_b5_build_pdf_pointer_to_mismatched_document_refs_fails_closed(self) -> None:
        """B5: matching bundle hash never excuses mismatching canonical input refs."""

        _, job_id, _ = self._setup_completed_course("B5 Refs Pointer")
        built = self._build(job_id)
        assert isinstance(built, BuildRecord)

        divergent = self._forge_build_record(
            built,
            build_id="bld-refsdrift001",
            document_refs=built.document_refs
            + (DocumentReference("lecture-document/v1", "l99", 99, "e" * 64),),
        )
        self.assertEqual(divergent.bundle_hash, built.bundle_hash)
        self._mutate_job(job_id, completed_build_id=divergent.build_id)
        self._assert_build_pdf_fails_closed(job_id)

    def _assert_get_artifact_fails_closed(self, build_id: str, *, bundle_hash: str) -> None:
        cache_file = self.cache_root / f"{bundle_hash}.pdf"
        self.assertTrue(cache_file.exists(), "the cache entry must exist for this proof")
        cached_before = cache_file.read_bytes()

        res = get_artifact(
            build_id,
            build_store=self.build_store,
            job_store=self.job_store,
            association_store=self.assoc_store,
            workflow_store=self.workflow_store,
            policy_store=self.policy_store,
            document_store=self.doc_store,
            cache_root=self.cache_root,
        )
        self.assertIsInstance(res, BuildOperationFailure)
        assert isinstance(res, BuildOperationFailure)
        self.assertEqual(res.diagnostics[0].code, "job_build_identity_mismatch")
        # The cache bytes were never served and never discarded.
        self.assertEqual(cache_file.read_bytes(), cached_before)

    def test_b5_get_artifact_cache_hit_job_pointer_disagreement_fails_closed(self) -> None:
        """B5: a cache hit never bypasses Job pointer agreement."""

        _, job_id, _ = self._setup_completed_course("B5 Cache Pointer")
        built = self._build(job_id)
        assert isinstance(built, BuildRecord)

        self._mutate_job(job_id, completed_build_id="bld-elsewhere0001")
        self._assert_get_artifact_fails_closed(built.build_id, bundle_hash=built.bundle_hash)

    def test_b5_get_artifact_cache_hit_wrong_workflow_revision_fails_closed(self) -> None:
        """B5: a cache hit never bypasses the workflow revision binding."""

        _, job_id, _ = self._setup_completed_course("B5 Cache Revision")
        built = self._build(job_id)
        assert isinstance(built, BuildRecord)

        wrong_revision = self._forge_build_record(
            built,
            build_id="bld-cacherev0001",
            workflow_revision=built.workflow_revision + 7,
        )
        self._mutate_job(job_id, completed_build_id=wrong_revision.build_id)
        self._assert_get_artifact_fails_closed(
            wrong_revision.build_id, bundle_hash=built.bundle_hash
        )

    def test_b5_get_artifact_cache_hit_foreign_job_record_fails_closed(self) -> None:
        """B5: a foreign Job's BuildRecord is not downloadable because a cache file exists."""

        _, job_id, _ = self._setup_completed_course("B5 Cache Foreign A")
        built = self._build(job_id)
        assert isinstance(built, BuildRecord)

        _, other_job_id, _ = self._setup_completed_course("B5 Cache Foreign B")
        other_job = self.job_store.load(other_job_id)
        assert isinstance(other_job, CourseJobRecord)
        self.assertIsNone(other_job.completed_build_id)

        foreign = self._forge_build_record(
            built,
            build_id="bld-cachefrgn001",
            job_id=other_job_id,
        )
        self._assert_get_artifact_fails_closed(
            foreign.build_id, bundle_hash=built.bundle_hash
        )

    def test_b5_get_artifact_cache_hit_stale_successful_record_fails_closed(self) -> None:
        """B5: a superseded successful BuildRecord is not downloadable from cache."""

        _, job_id, _ = self._setup_completed_course("B5 Cache Stale")
        built = self._build(job_id)
        assert isinstance(built, BuildRecord)

        successor = self._forge_build_record(
            built,
            build_id="bld-successor001",
            created_at="2026-12-31T23:59:59Z",
        )
        self._mutate_job(job_id, completed_build_id=successor.build_id)

        # The stale record is refused ...
        self._assert_get_artifact_fails_closed(built.build_id, bundle_hash=built.bundle_hash)
        # ... while the record the Job actually points at still serves.
        current = self._artifact(successor.build_id)
        self.assertIsInstance(current, bytes)
        assert isinstance(current, bytes)
        self.assertTrue(current.startswith(b"%PDF"))

    def test_valid_but_different_pdf_is_never_returned_from_cache(self) -> None:
        """A structurally valid substituted PDF is discarded and re-derived exactly."""

        _, job_id, _ = self._setup_completed_course("Cache Substitution")
        built = self._build(job_id)
        assert isinstance(built, BuildRecord)

        authentic = self._artifact(built.build_id)
        assert isinstance(authentic, bytes)
        self.assertEqual(_sha256_hex(authentic), built.pdf_sha256)

        # Substitute a different, structurally well-formed PDF at the cache path.
        substitute = _make_sample_pdf_bytes()
        self.assertTrue(substitute.startswith(b"%PDF-"))
        self.assertIn(b"%%EOF", substitute[-2048:])
        self.assertNotEqual(substitute, authentic)

        cache_file = self.cache_root / f"{built.bundle_hash}.pdf"
        cache_file.write_bytes(substitute)

        served = self._artifact(built.build_id)
        self.assertIsInstance(served, bytes)
        assert isinstance(served, bytes)
        self.assertNotEqual(served, substitute)
        self.assertEqual(served, authentic)
        self.assertEqual(_sha256_hex(served), built.pdf_sha256)
        # The cache was rederived back to exactly the authentic artifact.
        self.assertEqual(cache_file.read_bytes(), authentic)

    def test_build_pdf_rejects_ephemeral_visual_inputs(self) -> None:
        """T050 durable builds cover the non-visual persisted path only."""

        _, job_id, _ = self._setup_completed_course("Ephemeral Visual Build")
        asset_ref = AssetReference("asset-reference/v1", "a" * 64)
        placement = VisualPlacement(
            "visual-placement/v1",
            DocumentReference("lecture-document/v1", "l1", 1, "b" * 64),
            0,
            asset_ref,
        )

        for placements, assets in (
            ((placement,), None),
            ((), {asset_ref: b"png-bytes"}),
        ):
            res = build_pdf(
                job_id,
                job_store=self.job_store,
                association_store=self.assoc_store,
                workflow_store=self.workflow_store,
                policy_store=self.policy_store,
                document_store=self.doc_store,
                build_store=self.build_store,
                cache_root=self.cache_root,
                visual_placements=placements,
                visual_assets=assets,
            )
            self.assertIsInstance(res, BuildOperationFailure)
            assert isinstance(res, BuildOperationFailure)
            self.assertEqual(res.diagnostics[0].code, "ephemeral_visual_inputs_rejected")

        # No durable evidence was created for a build that could not be reopened.
        self.assertEqual(get_artifact_history(job_id, build_store=self.build_store), ())

    def test_get_artifact_rejects_ephemeral_visual_inputs(self) -> None:
        """get_artifact never re-derives from caller-supplied ephemeral visuals."""

        _, job_id, _ = self._setup_completed_course("Ephemeral Visual Artifact")
        built = self._build(job_id)
        assert isinstance(built, BuildRecord)

        asset_ref = AssetReference("asset-reference/v1", "a" * 64)
        placement = VisualPlacement(
            "visual-placement/v1",
            DocumentReference("lecture-document/v1", "l1", 1, "b" * 64),
            0,
            asset_ref,
        )
        for placements, assets in (
            ((placement,), None),
            ((), {asset_ref: b"png-bytes"}),
        ):
            res = get_artifact(
                built.build_id,
                build_store=self.build_store,
                job_store=self.job_store,
                association_store=self.assoc_store,
                workflow_store=self.workflow_store,
                policy_store=self.policy_store,
                document_store=self.doc_store,
                cache_root=self.cache_root,
                visual_placements=placements,
                visual_assets=assets,
            )
            self.assertIsInstance(res, BuildOperationFailure)
            assert isinstance(res, BuildOperationFailure)
            self.assertEqual(res.diagnostics[0].code, "ephemeral_visual_inputs_rejected")

    # ------------------------------------------------------------------
    # B3 reconciliation: canonical deterministic inputs are reopened before
    # a BuildRecord is ever adopted as Job build authority (R-B3-1..R-B3-5).
    # ------------------------------------------------------------------

    def _reset_job_to_b3(self, job_id: str) -> CourseJobRecord:
        """Put a Job in the legitimate B3 state: workflow completed, pointer absent."""

        job = self._mutate_job(
            job_id,
            status="deterministic_building",
            current_stage="completed",
            current_disposition="completed",
            completed_build_id=None,
            completed_build_sha256=None,
        )
        self.assertEqual(job.status, "deterministic_building")
        self.assertIsNone(job.completed_build_id)
        self.assertIsNone(job.completed_build_sha256)
        ws = self._workflow_state(job_id)
        self.assertEqual(ws.stage, "completed")
        self.assertEqual(ws.disposition, "completed")
        return job

    def _assert_reconcile_fails_closed(self, job_id: str) -> None:
        """Reconciliation must refuse and leave the Job byte-identical."""

        job_before = self.job_store.load(job_id)
        assert isinstance(job_before, CourseJobRecord)

        res = self._reconcile(job_id)

        self.assertIsInstance(res, BuildOperationFailure)
        assert isinstance(res, BuildOperationFailure)
        self.assertEqual(res.diagnostics[0].code, "job_build_identity_mismatch")

        job_after = self.job_store.load(job_id)
        self.assertEqual(job_after, job_before)
        assert isinstance(job_after, CourseJobRecord)
        self.assertEqual(job_after.status, "deterministic_building")
        self.assertIsNone(job_after.completed_build_id)
        self.assertIsNone(job_after.completed_build_sha256)

    def test_rb3_1_reconcile_rejects_wrong_document_refs(self) -> None:
        """R-B3-1: a record whose canonical document refs differ is never adopted."""

        _, job_id, _ = self._setup_completed_course("R-B3-1 Document Refs")
        built = self._build(job_id)
        assert isinstance(built, BuildRecord)

        divergent = self._forge_build_record(
            built,
            build_id="bld-rb3docrefs01",
            created_at="2030-01-01T00:00:00Z",
            document_refs=built.document_refs
            + (DocumentReference("lecture-document/v1", "l98", 98, "c" * 64),),
        )
        # The forged record is the one reconciliation would otherwise adopt, and
        # its bundle hash is deliberately unchanged: only the canonical refs lie.
        self.assertEqual(divergent.bundle_hash, built.bundle_hash)
        latest = self.build_store.latest_successful_for_job(job_id)
        self.assertEqual(latest, divergent)

        self._reset_job_to_b3(job_id)
        self._assert_reconcile_fails_closed(job_id)

    def test_rb3_2_reconcile_rejects_wrong_bundle_hash(self) -> None:
        """R-B3-2: a record whose bundle hash is not the re-derived input identity fails."""

        _, job_id, _ = self._setup_completed_course("R-B3-2 Bundle Hash")
        built = self._build(job_id)
        assert isinstance(built, BuildRecord)

        divergent = self._forge_build_record(
            built,
            build_id="bld-rb3bundle01",
            created_at="2030-01-01T00:00:00Z",
            bundle_hash="1" * 64,
        )
        self.assertNotEqual(divergent.bundle_hash, built.bundle_hash)
        latest = self.build_store.latest_successful_for_job(job_id)
        self.assertEqual(latest, divergent)

        self._reset_job_to_b3(job_id)
        self._assert_reconcile_fails_closed(job_id)

    def test_rb3_3_reconcile_rejects_impossible_visual_refs(self) -> None:
        """R-B3-3: under the T050 non-visual durable boundary canonical visual refs are empty."""

        _, job_id, _ = self._setup_completed_course("R-B3-3 Visual Refs")
        built = self._build(job_id)
        assert isinstance(built, BuildRecord)
        self.assertEqual(built.placement_refs, ())
        self.assertEqual(built.asset_refs, ())

        asset_ref = AssetReference("asset-reference/v1", "d" * 64)
        divergent = self._forge_build_record(
            built,
            build_id="bld-rb3visual01",
            created_at="2030-01-01T00:00:00Z",
            placement_refs=("l1@0:" + "d" * 64,),
            asset_refs=(asset_ref,),
        )
        self.assertEqual(divergent.bundle_hash, built.bundle_hash)
        latest = self.build_store.latest_successful_for_job(job_id)
        self.assertEqual(latest, divergent)

        self._reset_job_to_b3(job_id)
        self._assert_reconcile_fails_closed(job_id)

    def test_rb3_4_reconcile_recovers_exact_record(self) -> None:
        """R-B3-4: the exact authentic record recovers the Job pointer pair."""

        _, job_id, _ = self._setup_completed_course("R-B3-4 Exact Recovery")
        built = self._build(job_id)
        assert isinstance(built, BuildRecord)

        self._reset_job_to_b3(job_id)
        recovered = self._reconcile(job_id)
        self.assertIsInstance(recovered, CourseJobRecord)
        assert isinstance(recovered, CourseJobRecord)
        self.assertEqual(recovered.status, "completed")
        self.assertEqual(recovered.completed_build_id, built.build_id)
        self.assertEqual(recovered.completed_build_sha256, built.bundle_hash)

    def test_rb3_5_reconcile_repeat_is_equality_idempotent(self) -> None:
        """R-B3-5: repeating exact recovery is a no-op, not a second advance."""

        _, job_id, _ = self._setup_completed_course("R-B3-5 Idempotent")
        built = self._build(job_id)
        assert isinstance(built, BuildRecord)

        self._reset_job_to_b3(job_id)
        first = self._reconcile(job_id)
        assert isinstance(first, CourseJobRecord)
        second = self._reconcile(job_id)
        assert isinstance(second, CourseJobRecord)
        third = self._reconcile(job_id)
        assert isinstance(third, CourseJobRecord)

        self.assertEqual(second, first)
        self.assertEqual(third, first)
        self.assertEqual(second.metadata_revision, first.metadata_revision)
        stored = self.job_store.load(job_id)
        self.assertEqual(stored, first)

    def test_rb3_reconcile_requires_reopenable_documents(self) -> None:
        """Reconciliation refuses when the durable build inputs cannot be reopened."""

        _, job_id, _ = self._setup_completed_course("R-B3 Unreopenable")
        built = self._build(job_id)
        assert isinstance(built, BuildRecord)
        self._reset_job_to_b3(job_id)

        class _EmptyDocumentStore:
            def __init__(self, store: object) -> None:
                self._store = store

            def __getattr__(self, name: str) -> object:
                return getattr(self._store, name)

            def load(self, *args: object, **kwargs: object) -> object:
                raise RuntimeError("simulated unreadable lecture document store")

        res = reconcile_job_build_projection(
            job_id,
            job_store=self.job_store,
            build_store=self.build_store,
            association_store=self.assoc_store,
            workflow_store=self.workflow_store,
            policy_store=self.policy_store,
            document_store=_EmptyDocumentStore(self.doc_store),  # type: ignore[arg-type]
        )
        self.assertIsInstance(res, BuildOperationFailure)
        assert isinstance(res, BuildOperationFailure)
        self.assertIn(
            res.diagnostics[0].code,
            ("accepted_documents_unavailable", "build_operation_exception"),
        )
        job_after = self.job_store.load(job_id)
        assert isinstance(job_after, CourseJobRecord)
        self.assertEqual(job_after.status, "deterministic_building")
        self.assertIsNone(job_after.completed_build_id)


if __name__ == "__main__":
    unittest.main()
