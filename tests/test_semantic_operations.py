"""Tests for T049 coarse operations: start_generation, request, submit, scheduler, reconciliation."""

from __future__ import annotations

import dataclasses
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from course_compiler.course_persistence import open_course_store
from course_compiler.course_job_persistence import open_course_job_store
from course_compiler.workflow_persistence import open_workflow_state_store, LocalWorkflowStateStore
from course_compiler.course_workflow_persistence import open_course_workflow_association_store
from course_compiler.source_persistence import open_source_evidence_store
from course_compiler.policy_persistence import open_policy_content_store
from course_compiler.workflow_artifact_persistence import open_workflow_artifact_store, LocalWorkflowArtifactStore
from course_compiler.lecture_document_persistence import open_lecture_document_store
from course_compiler.semantic_work_persistence import open_semantic_work_store, SemanticAcceptedRecord, LocalSemanticWorkStore
from course_compiler.build_operations import (
    compute_bundle_hash,
    reconcile_job_build_projection,
)
from course_compiler.course_workflow import (
    COURSE_WORKFLOW_ASSOCIATION_VERSION,
    CourseWorkflowAssociation,
)
from course_compiler.course_workflow_operations import (
    reopen_accepted_lecture_documents,
    reopen_course_workflow_context,
)
from course_compiler.visual_composition import compose_course_compiler_input
from course_compiler.build_persistence import (
    BuildRecord,
    LocalBuildRecordStore,
    open_build_record_store,
)
from course_compiler.course_operations import create_course, attach_source
from course_compiler.semantic_operations import (
    determine_next_semantic_kind,
    determine_scheduler_decision,
    observe_pending_work,
    request_semantic_work,
    start_generation,
    submit_owner_map_decision,
    submit_owner_priority_decision,
    submit_semantic_result,
    SemanticOperationFailure,
    SchedulerDecision,
)
from course_compiler.semantic_work import (
    ProducedArtifactSpec,
    ProducedDocumentSpec,
    ScriptProvider,
    SEMANTIC_WORK_REQUEST_VERSION,
    SEMANTIC_WORK_RESULT_VERSION,
    SemanticWorkRequest,
    SemanticWorkResult,
)
from course_compiler.workflow import (
    DocumentReference,
    LectureMapRecord,
    PolicyReference,
    WorkflowAdvanced,
    WorkflowArtifactReference,
    WorkflowIdempotentRepeat,
    WorkflowRejected,
    WorkflowState,
    candidate_subject_sha256,
    priority_subject_sha256,
    map_subject_sha256,
)
from course_compiler.workflow_policy import ARTIFACT_PRODUCERS, POLICY_VERSIONS
from course_compiler.course_job_persistence import CourseJobRecord
from course_compiler.course_persistence import CourseRecord
from tests.test_semantic_workflow_e2e import _drive_step as _drive_coarse_step

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_ISOLATED_BASE = REPOSITORY_ROOT / "local-data" / "test-tmp-sem-ops"
_ISOLATED_BASE.mkdir(parents=True, exist_ok=True)

_HOLDER = "h-owner"


def _tmp_dir() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(dir=str(_ISOLATED_BASE), prefix="ops-")


def _new_stores(tmp: str) -> dict:
    p = Path(tmp)
    return {
        "course_store": open_course_store(p / "courses.sqlite3"),
        "assoc_store": open_course_workflow_association_store(p / "assoc.sqlite3"),
        "source_store": open_source_evidence_store(p / "source.sqlite3"),
        "workflow_store": open_workflow_state_store(p / "workflow.sqlite3"),
        "job_store": open_course_job_store(p / "jobs.sqlite3"),
        "policy_store": open_policy_content_store(p / "pol.sqlite3"),
        "artifact_store": open_workflow_artifact_store(p / "art.sqlite3"),
        "document_store": open_lecture_document_store(p / "doc.sqlite3"),
        "semantic_store": open_semantic_work_store(p / "sem.sqlite3"),
        "path": p,
    }


def _close(stores: dict) -> None:
    for k in stores.values():
        if hasattr(k, "close"):
            try:
                k.close()
            except Exception:
                pass


def _advance_to(stores: dict, job_id: str, provider: ScriptProvider, *, target_kind: str) -> None:
    for i in range(12):
        job = stores["job_store"].load(job_id)
        ws = stores["workflow_store"].load(job.workflow_id)
        if target_kind == "lecture_map_generation" and ws.stage == "lecture_mapping" and ws.disposition == "ready":
            return
        if target_kind == "lecture_generation" and ws.stage == "lecture_production" and ws.disposition == "ready":
            return
        if ws.stage == "priority_approval" and ws.disposition == "awaiting_approval":
            all_rows = stores["semantic_store"].load_all_for_job(job_id)
            has_accepted = any(
                type(r) is SemanticAcceptedRecord and r.request_revision == ws.revision and r.kind == "exam_priority_assessment"
                for r in all_rows
            )
            if has_accepted:
                p_subj = priority_subject_sha256(ws.priority_basis, ws.policies.priority_basis)
                submit_owner_priority_decision(
                    job_id,
                    approve=True,
                    subject_sha256=p_subj,
                    job_store=stores["job_store"],
                    workflow_store=stores["workflow_store"],
                    semantic_store=stores["semantic_store"],
                    association_store=stores["assoc_store"],
                    source_store=stores["source_store"],
                    policy_store=stores["policy_store"],
                    artifact_store=stores["artifact_store"],
                    document_store=stores["document_store"],
                    clock=f"2026-01-01T00:{i:02d}:40Z",
                )
                continue
        if ws.stage == "map_approval" and ws.disposition == "awaiting_approval":
            m_subj = map_subject_sha256(ws.lecture_map, ws.policies.lecture_mapping)
            submit_owner_map_decision(
                job_id,
                approve=True,
                subject_sha256=m_subj,
                job_store=stores["job_store"],
                workflow_store=stores["workflow_store"],
                semantic_store=stores["semantic_store"],
                association_store=stores["assoc_store"],
                source_store=stores["source_store"],
                policy_store=stores["policy_store"],
                artifact_store=stores["artifact_store"],
                document_store=stores["document_store"],
                clock=f"2026-01-01T00:{i:02d}:40Z",
            )
            continue

        req = request_semantic_work(
            job_id,
            job_store=stores["job_store"],
            workflow_store=stores["workflow_store"],
            semantic_store=stores["semantic_store"],
            holder_id=_HOLDER,
            clock=f"2026-01-01T00:{i:02d}:00Z",
        )
        if not isinstance(req, SemanticWorkRequest):
            return
        if req.kind == target_kind:
            return
        res = provider.execute(req)
        submit_semantic_result(
            job_id,
            res,
            job_store=stores["job_store"],
            workflow_store=stores["workflow_store"],
            semantic_store=stores["semantic_store"],
            association_store=stores["assoc_store"],
            source_store=stores["source_store"],
            policy_store=stores["policy_store"],
            artifact_store=stores["artifact_store"],
            document_store=stores["document_store"],
            holder_id=_HOLDER,
            clock=f"2026-01-01T00:{i:02d}:30Z",
        )


class TestSchedulerDecision(unittest.TestCase):
    def test_stage_disposition_mapping(self) -> None:
        self.assertEqual(determine_scheduler_decision(None).decision, "invalid")
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("Sched", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            ws = stores["workflow_store"].load(stores["course_store"].load(ref.course_id).workflow_id)  # type: ignore[attr-defined]
            self.assertIsInstance(ws, WorkflowState)
            decision = determine_scheduler_decision(ws)
            self.assertIsInstance(decision, SchedulerDecision)
            self.assertEqual(decision.decision, "need_semantic")
            self.assertEqual(decision.kind, "source_assessment")
            _close(stores)


class TestStartGeneration(unittest.TestCase):
    def test_start_creates_job_and_workflow(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("T049 Course", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src one", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            self.assertIsInstance(job_id, str)
            job = stores["job_store"].load(job_id)
            self.assertIsInstance(job, CourseJobRecord)
            ws = stores["workflow_store"].load(job.workflow_id)  # type: ignore[attr-defined]
            self.assertIsInstance(ws, WorkflowState)
            self.assertEqual(ws.revision, 0)
            course = stores["course_store"].load(ref.course_id)
            self.assertIsInstance(course, CourseRecord)
            self.assertEqual(course.current_job_id, job_id)
            _close(stores)

    def test_start_idempotent_resume(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("Idempotent", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            j1 = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            j2 = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            self.assertEqual(j1, j2)
            listed = stores["job_store"].list_jobs_for_course(ref.course_id)
            self.assertIsInstance(listed, tuple)
            self.assertEqual(len(listed), 1)
            _close(stores)

    def test_start_requires_course_and_sources(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            res = start_generation("nonexistent", course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"])
            self.assertIsInstance(res, SemanticOperationFailure)
            ref = create_course("NoSrc", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            res2 = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"])
            self.assertIsInstance(res2, SemanticOperationFailure)
            _close(stores)

    def test_at_most_one_active_job(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("Active", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            j1 = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            j2 = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:01:00Z")
            self.assertEqual(j1, j2)
            listed = stores["job_store"].list_jobs_for_course(ref.course_id)
            self.assertIsInstance(listed, tuple)
            self.assertEqual(len(listed), 1)
            _close(stores)


class TestRequestSemanticWork(unittest.TestCase):
    def test_request_creates_and_leases(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("Req", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            req = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:00:00Z")
            self.assertIsInstance(req, SemanticWorkRequest)
            self.assertEqual(req.job_id, job_id)
            self.assertEqual(req.expected_revision, 0)
            req2 = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:01:00Z")
            self.assertIsInstance(req2, SemanticWorkRequest)
            self.assertEqual(req.request_id, req2.request_id)
            self.assertEqual(req.operation_id, req2.operation_id)
            _close(stores)

    def test_request_no_work_when_completed(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("NoWork", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            provider = ScriptProvider(map_size=1)
            completed = self._drive_to_completion(job_id, stores, provider)
            if completed:
                req2 = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T01:00:00Z")
                self.assertIsInstance(req2, SemanticOperationFailure)
                self.assertEqual(req2.diagnostics[0].code, "no_semantic_work")
            _close(stores)

    def _drive_to_completion(self, job_id: str, stores: dict, provider: ScriptProvider) -> bool:
        for i in range(8):
            req = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock=f"2026-01-01T00:0{i}:00Z")
            if not isinstance(req, SemanticWorkRequest):
                break
            res = provider.execute(req)
            decision = submit_semantic_result(job_id, res, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], holder_id=_HOLDER, clock=f"2026-01-01T00:0{i}:30Z")
            if isinstance(decision, SemanticOperationFailure):
                if decision.diagnostics[0].code == "owner_decision_required":
                    kind = req.kind
                    if kind == "exam_priority_assessment":
                        ws_p = stores["workflow_store"].load(stores["job_store"].load(job_id).workflow_id)
                        p_subj = priority_subject_sha256(ws_p.priority_basis, ws_p.policies.priority_basis)
                        decision = submit_owner_priority_decision(job_id, approve=True, subject_sha256=p_subj, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock=f"2026-01-01T00:0{i}:40Z")
                    elif kind == "lecture_map_generation":
                        ws_m = stores["workflow_store"].load(stores["job_store"].load(job_id).workflow_id)
                        m_subj = map_subject_sha256(ws_m.lecture_map, ws_m.policies.lecture_mapping)
                        decision = submit_owner_map_decision(job_id, approve=True, subject_sha256=m_subj, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock=f"2026-01-01T00:0{i}:40Z")
                    else:
                        return False
                else:
                    return False
            if isinstance(decision, SemanticOperationFailure):
                return False
            job = stores["job_store"].load(job_id)
            assert isinstance(job, CourseJobRecord)
            ws = stores["workflow_store"].load(job.workflow_id)
            assert isinstance(ws, WorkflowState)
            if ws.stage == "completed":
                return True
        return False

    def test_completed_job_build_pointers_survive_semantic_paths(self) -> None:
        """A completed Job keeps its build authority across every semantic read.

        WorkflowState stays authoritative for workflow progress and BuildRecord
        stays authoritative for successful build evidence. Semantic projection
        owns neither, so reading, re-reading, or retrying a semantic path on a
        Job that is already `completed WorkflowState + successful BuildRecord +
        completed Job` must leave the Job exactly as it was -- same status, same
        completed_build_id, same completed_build_sha256, and no unnecessary
        metadata revision advance.
        """

        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("PointerSurvival", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            provider = ScriptProvider(map_size=1)
            for index in range(14):
                _drive_coarse_step(stores, job_id, provider, clock_index=index)
                job = stores["job_store"].load(job_id)
                assert isinstance(job, CourseJobRecord)
                ws = stores["workflow_store"].load(job.workflow_id)
                assert isinstance(ws, WorkflowState)
                if ws.stage == "completed":
                    break
            self.assertEqual(ws.stage, "completed")
            self.assertEqual(ws.disposition, "completed")
            # A completed workflow alone does not complete the Job.
            self.assertEqual(job.status, "deterministic_building")

            build_store = open_build_record_store(stores["path"] / "builds.sqlite3")
            assert isinstance(build_store, LocalBuildRecordStore)
            # The BuildRecord must be honest durable evidence: its canonical
            # document references and bundle hash are recomputed from the same
            # accepted deterministic inputs that build_pdf, get_artifact, and
            # reconcile_job_build_projection reopen, so reconciliation verifies
            # it against re-derived inputs rather than trusting its own fields.
            context = reopen_course_workflow_context(
                CourseWorkflowAssociation(
                    COURSE_WORKFLOW_ASSOCIATION_VERSION, ref, job.workflow_id
                ),
                association_store=stores["assoc_store"],
                workflow_store=stores["workflow_store"],
                policy_store=stores["policy_store"],
            )
            accepted_docs = reopen_accepted_lecture_documents(
                context, document_store=stores["document_store"]
            )
            self.assertIsInstance(accepted_docs, tuple)
            assert isinstance(accepted_docs, tuple)
            self.assertTrue(accepted_docs)
            bundle = compose_course_compiler_input(
                accepted_docs, placements=(), asset_bytes={}
            )
            canonical_document_refs = tuple(
                DocumentReference(
                    d.contract_version, d.document_id, d.order, d.provenance.content_sha256
                )
                for d in accepted_docs
            )
            record = BuildRecord(
                build_id="bld-semantic00001",
                job_id=job_id,
                workflow_revision=ws.revision,
                document_refs=canonical_document_refs,
                placement_refs=(),
                asset_refs=(),
                bundle_hash=compute_bundle_hash(bundle),
                # This proof never retrieves artifact bytes, so the derived-byte
                # digest is a fixed placeholder; every field reconciliation
                # actually verifies is re-derived above.
                pdf_sha256="c" * 64,
                created_at="2026-01-01T02:00:00Z",
                status="succeeded",
                diagnostics=(),
            )
            self.assertIsInstance(build_store.save(record), BuildRecord)

            completed = reconcile_job_build_projection(
                job_id,
                job_store=stores["job_store"],
                build_store=build_store,
                association_store=stores["assoc_store"],
                workflow_store=stores["workflow_store"],
                policy_store=stores["policy_store"],
                document_store=stores["document_store"],
            )
            self.assertIsInstance(completed, CourseJobRecord)
            assert isinstance(completed, CourseJobRecord)
            self.assertEqual(completed.status, "completed")
            self.assertEqual(completed.completed_build_id, record.build_id)
            self.assertEqual(completed.completed_build_sha256, record.bundle_hash)

            before = stores["job_store"].load(job_id)
            assert isinstance(before, CourseJobRecord)

            def _assert_unchanged(label: str) -> None:
                after = stores["job_store"].load(job_id)
                self.assertIsInstance(after, CourseJobRecord)
                assert isinstance(after, CourseJobRecord)
                self.assertEqual(after, before, label)
                self.assertEqual(after.status, "completed", label)
                self.assertEqual(after.completed_build_id, record.build_id, label)
                self.assertEqual(after.completed_build_sha256, record.bundle_hash, label)
                self.assertEqual(after.metadata_revision, before.metadata_revision, label)

            # request_semantic_work drives the semantic lease reconciliation.
            for index in range(2):
                req = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock=f"2026-01-01T03:0{index}:00Z")
                self.assertIsInstance(req, SemanticOperationFailure)
                assert isinstance(req, SemanticOperationFailure)
                self.assertEqual(req.diagnostics[0].code, "no_semantic_work")
                _assert_unchanged(f"request_semantic_work retry {index}")

            # observe_pending_work is the non-acquiring read of the same path.
            observed = observe_pending_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], clock="2026-01-01T03:10:00Z")
            self.assertNotIsInstance(observed, SemanticWorkRequest)
            _assert_unchanged("observe_pending_work")

            # start_generation is the idempotent Job projection path.
            for index in range(2):
                again = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T03:20:00Z")
                self.assertEqual(again, job_id)
                _assert_unchanged(f"start_generation retry {index}")

            # The store's own projection helper agrees.
            reconciled = stores["job_store"].reconcile_from_workflow(job_id, ws)
            self.assertIsInstance(reconciled, CourseJobRecord)
            _assert_unchanged("reconcile_from_workflow")

            # Build reconciliation stays idempotent on the repaired Job.
            repeated = reconcile_job_build_projection(
                job_id,
                job_store=stores["job_store"],
                build_store=build_store,
                association_store=stores["assoc_store"],
                workflow_store=stores["workflow_store"],
                policy_store=stores["policy_store"],
                document_store=stores["document_store"],
            )
            self.assertEqual(repeated, before)
            _assert_unchanged("reconcile_job_build_projection")

            build_store.close()
            _close(stores)

    def test_lease_exclusivity_different_holder(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("LeaseX", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            req = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id="h-a", clock="2026-01-01T00:00:00Z")
            self.assertIsInstance(req, SemanticWorkRequest)
            # Different holder cannot acquire while active
            bad = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id="h-b", clock="2026-01-01T00:01:00Z")
            self.assertIsInstance(bad, SemanticOperationFailure)
            self.assertEqual(bad.diagnostics[0].code, "lease_conflict")
            # Same holder can renew
            again = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id="h-a", clock="2026-01-01T00:02:00Z")
            self.assertIsInstance(again, SemanticWorkRequest)
            # After expiry, same logical request is offered to next holder
            next_holder = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id="h-c", clock="2026-01-01T00:20:00Z")
            self.assertIsInstance(next_holder, SemanticWorkRequest)
            self.assertEqual(next_holder.request_id, req.request_id)
            self.assertEqual(next_holder.operation_id, req.operation_id)
            self.assertEqual(next_holder.expected_revision, req.expected_revision)
            self.assertEqual(next_holder.kind, req.kind)
            self.assertEqual(next_holder.input_refs, req.input_refs)
            _close(stores)

    def test_observe_does_not_acquire(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("Obs", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            observed = observe_pending_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], clock="2026-01-01T00:00:00Z")
            self.assertIsNone(observed)
            req = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id="h-a", clock="2026-01-01T00:00:00Z")
            self.assertIsInstance(req, SemanticWorkRequest)
            # Observe returns same request without mutating lease holder/expiry
            observed2 = observe_pending_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], clock="2026-01-01T00:01:00Z")
            self.assertIsInstance(observed2, SemanticWorkRequest)
            self.assertEqual(observed2.request_id, req.request_id)
            # Different holder can still acquire (the observation didn't steal)
            other = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id="h-b", clock="2026-01-01T00:02:00Z")
            self.assertIsInstance(other, SemanticOperationFailure)
            self.assertEqual(other.diagnostics[0].code, "lease_conflict")
            _close(stores)


class TestOwnerDecisions(unittest.TestCase):
    def test_priority_approval_blocked_until_owner(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("Owner", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            provider = ScriptProvider(map_size=1)
            req = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:00:00Z")
            assert isinstance(req, SemanticWorkRequest)
            res = provider.execute(req)
            # Source assessment advances into the owner priority gate.
            submit = submit_semantic_result(job_id, res, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], holder_id=_HOLDER, clock="2026-01-01T00:01:00Z")
            self.assertNotIsInstance(submit, SemanticOperationFailure)
            ws = stores["workflow_store"].load(stores["job_store"].load(job_id).workflow_id)  # type: ignore[attr-defined]
            self.assertEqual(ws.stage, "priority_approval")
            self.assertEqual(ws.disposition, "awaiting_approval")
            # The priority assessor may persist review evidence, but that
            # result itself cannot decide the owner gate.
            assessment = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:02:00Z")
            self.assertIsInstance(assessment, SemanticWorkRequest)
            assert isinstance(assessment, SemanticWorkRequest)
            self.assertEqual(assessment.kind, "exam_priority_assessment")
            assessment_result = provider.execute(assessment)
            submitted_assessment = submit_semantic_result(job_id, assessment_result, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], holder_id=_HOLDER, clock="2026-01-01T00:02:30Z")
            self.assertNotIsInstance(submitted_assessment, SemanticOperationFailure)
            # Subsequent semantic lecture work is unavailable until explicit owner intent.
            again = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:02:00Z")
            self.assertIsInstance(again, SemanticOperationFailure)
            self.assertEqual(again.diagnostics[0].code, "owner_decision_required")
            # Owner approves explicitly
            ws_cur = stores["workflow_store"].load(stores["job_store"].load(job_id).workflow_id)
            p_subj = priority_subject_sha256(ws_cur.priority_basis, ws_cur.policies.priority_basis)
            approve = submit_owner_priority_decision(job_id, approve=True, subject_sha256=p_subj, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:03:00Z")
            self.assertIsInstance(approve, WorkflowAdvanced)
            ws = stores["workflow_store"].load(stores["job_store"].load(job_id).workflow_id)  # type: ignore[attr-defined]
            self.assertEqual(ws.stage, "lecture_mapping")
            self.assertEqual(ws.disposition, "ready")
            _close(stores)

    def test_priority_reject_recomputes_baseline(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("Reject", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            provider = ScriptProvider(map_size=1)
            req = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:00:00Z")
            assert isinstance(req, SemanticWorkRequest)
            res = provider.execute(req)
            submit_semantic_result(job_id, res, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], holder_id=_HOLDER, clock="2026-01-01T00:01:00Z")
            ws_cur = stores["workflow_store"].load(stores["job_store"].load(job_id).workflow_id)
            p_subj = priority_subject_sha256(ws_cur.priority_basis, ws_cur.policies.priority_basis)
            reject = submit_owner_priority_decision(job_id, approve=False, subject_sha256=p_subj, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:02:00Z")
            self.assertIsInstance(reject, WorkflowAdvanced)
            ws = stores["workflow_store"].load(stores["job_store"].load(job_id).workflow_id)  # type: ignore[attr-defined]
            self.assertEqual(ws.stage, "source_assessment")
            self.assertEqual(ws.disposition, "ready")
            _close(stores)

    def test_map_owner_decision(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("Map", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            provider = ScriptProvider(map_size=1)
            # Source assessment -> priority assessment -> explicit priority approval -> map generation.
            source = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:00:00Z")
            assert isinstance(source, SemanticWorkRequest)
            submit_semantic_result(job_id, provider.execute(source), job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], holder_id=_HOLDER, clock="2026-01-01T00:00:30Z")
            priority = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:01:00Z")
            assert isinstance(priority, SemanticWorkRequest)
            submit_semantic_result(job_id, provider.execute(priority), job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], holder_id=_HOLDER, clock="2026-01-01T00:01:30Z")
            ws_cur = stores["workflow_store"].load(stores["job_store"].load(job_id).workflow_id)
            p_subj = priority_subject_sha256(ws_cur.priority_basis, ws_cur.policies.priority_basis)
            submit_owner_priority_decision(job_id, approve=True, subject_sha256=p_subj, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:01:40Z")
            map_request = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:02:00Z")
            assert isinstance(map_request, SemanticWorkRequest)
            self.assertEqual(map_request.kind, "lecture_map_generation")
            submit = submit_semantic_result(job_id, provider.execute(map_request), job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], holder_id=_HOLDER, clock="2026-01-01T00:02:30Z")
            self.assertNotIsInstance(submit, SemanticOperationFailure)
            ws = stores["workflow_store"].load(stores["job_store"].load(job_id).workflow_id)  # type: ignore[attr-defined]
            self.assertEqual(ws.stage, "map_approval")
            self.assertEqual(ws.disposition, "awaiting_approval")
            m_subj = map_subject_sha256(ws.lecture_map, ws.policies.lecture_mapping)
            decision = submit_owner_map_decision(job_id, approve=True, subject_sha256=m_subj, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:10:00Z")
            self.assertIsInstance(decision, WorkflowAdvanced)
            ws = stores["workflow_store"].load(stores["job_store"].load(job_id).workflow_id)  # type: ignore[attr-defined]
            self.assertEqual(ws.stage, "lecture_production")
            self.assertEqual(ws.disposition, "ready")
            _close(stores)

    def test_priority_decision_subject_binding_variations(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("PrioVar", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            provider = ScriptProvider(map_size=1)
            source = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:00:00Z")
            submit_semantic_result(job_id, provider.execute(source), job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], holder_id=_HOLDER, clock="2026-01-01T00:00:30Z")
            priority = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:01:00Z")
            submit_semantic_result(job_id, provider.execute(priority), job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], holder_id=_HOLDER, clock="2026-01-01T00:01:30Z")

            ws_p = stores["workflow_store"].load(stores["job_store"].load(job_id).workflow_id)
            expected_p_subj = priority_subject_sha256(ws_p.priority_basis, ws_p.policies.priority_basis)
            rev_p = ws_p.revision

            # Missing subject -> invalid_semantic_input
            res_none = submit_owner_priority_decision(job_id, approve=True, subject_sha256=None, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"])
            self.assertIsInstance(res_none, SemanticOperationFailure)
            self.assertEqual(res_none.diagnostics[0].code, "invalid_semantic_input")

            # Non-64hex -> invalid_subject_hash
            res_bad = submit_owner_priority_decision(job_id, approve=True, subject_sha256="not-hex", job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"])
            self.assertIsInstance(res_bad, SemanticOperationFailure)
            self.assertEqual(res_bad.diagnostics[0].code, "invalid_subject_hash")

            # Mismatched valid 64-hex -> subject_hash_mismatch
            res_mismatch = submit_owner_priority_decision(job_id, approve=True, subject_sha256="9" * 64, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"])
            self.assertIsInstance(res_mismatch, SemanticOperationFailure)
            self.assertEqual(res_mismatch.diagnostics[0].code, "subject_hash_mismatch")

            # Verify no mutation
            ws_check = stores["workflow_store"].load(stores["job_store"].load(job_id).workflow_id)
            self.assertEqual(ws_check.revision, rev_p)
            self.assertEqual(ws_check.stage, "priority_approval")

            # Exact subject rejection advances along rejection path
            res_reject = submit_owner_priority_decision(job_id, approve=False, subject_sha256=expected_p_subj, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"])
            self.assertIsInstance(res_reject, WorkflowAdvanced)
            ws_after = stores["workflow_store"].load(stores["job_store"].load(job_id).workflow_id)
            self.assertEqual(ws_after.stage, "source_assessment")
            _close(stores)

    def test_map_decision_subject_binding_variations(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("MapVar", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            provider = ScriptProvider(map_size=1)
            source = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:00:00Z")
            submit_semantic_result(job_id, provider.execute(source), job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], holder_id=_HOLDER, clock="2026-01-01T00:00:30Z")
            priority = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:01:00Z")
            submit_semantic_result(job_id, provider.execute(priority), job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], holder_id=_HOLDER, clock="2026-01-01T00:01:30Z")
            ws_p = stores["workflow_store"].load(stores["job_store"].load(job_id).workflow_id)
            p_subj = priority_subject_sha256(ws_p.priority_basis, ws_p.policies.priority_basis)
            submit_owner_priority_decision(job_id, approve=True, subject_sha256=p_subj, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:01:40Z")
            map_req = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:02:00Z")
            submit_semantic_result(job_id, provider.execute(map_req), job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], holder_id=_HOLDER, clock="2026-01-01T00:02:30Z")

            ws_m = stores["workflow_store"].load(stores["job_store"].load(job_id).workflow_id)
            expected_m_subj = map_subject_sha256(ws_m.lecture_map, ws_m.policies.lecture_mapping)
            rev_m = ws_m.revision

            # Missing subject -> invalid_semantic_input
            res_none = submit_owner_map_decision(job_id, approve=True, subject_sha256=None, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"])
            self.assertIsInstance(res_none, SemanticOperationFailure)
            self.assertEqual(res_none.diagnostics[0].code, "invalid_semantic_input")

            # Non-64hex -> invalid_subject_hash
            res_bad = submit_owner_map_decision(job_id, approve=True, subject_sha256="not-hex", job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"])
            self.assertIsInstance(res_bad, SemanticOperationFailure)
            self.assertEqual(res_bad.diagnostics[0].code, "invalid_subject_hash")

            # Mismatched valid 64-hex -> subject_hash_mismatch
            res_mismatch = submit_owner_map_decision(job_id, approve=True, subject_sha256="8" * 64, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"])
            self.assertIsInstance(res_mismatch, SemanticOperationFailure)
            self.assertEqual(res_mismatch.diagnostics[0].code, "subject_hash_mismatch")

            # Verify no mutation
            ws_check = stores["workflow_store"].load(stores["job_store"].load(job_id).workflow_id)
            self.assertEqual(ws_check.revision, rev_m)
            self.assertEqual(ws_check.stage, "map_approval")

            # Exact subject rejection advances along rejection path
            res_reject = submit_owner_map_decision(job_id, approve=False, subject_sha256=expected_m_subj, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"])
            self.assertIsInstance(res_reject, WorkflowAdvanced)
            ws_after = stores["workflow_store"].load(stores["job_store"].load(job_id).workflow_id)
            self.assertEqual(ws_after.stage, "lecture_mapping")
            _close(stores)


class TestSubmitSemanticResult(unittest.TestCase):
    def test_submit_valid_and_idempotent(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("Submit", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            provider = ScriptProvider(map_size=1)
            req = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:00:00Z")
            assert isinstance(req, SemanticWorkRequest)
            res = provider.execute(req)
            sub1 = submit_semantic_result(job_id, res, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], holder_id=_HOLDER, clock="2026-01-01T00:01:00Z")
            self.assertIsInstance(sub1, WorkflowAdvanced)
            sub2 = submit_semantic_result(job_id, res, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], holder_id=_HOLDER, clock="2026-01-01T00:01:00Z")
            self.assertIsInstance(sub2, WorkflowIdempotentRepeat)
            _close(stores)

    def test_submit_wrong_holder_rejected(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("Holder", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            provider = ScriptProvider(map_size=1)
            req = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id="h-a", clock="2026-01-01T00:00:00Z")
            assert isinstance(req, SemanticWorkRequest)
            res = provider.execute(req)
            bad = submit_semantic_result(job_id, res, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], holder_id="h-b", clock="2026-01-01T00:01:00Z")
            self.assertIsInstance(bad, SemanticOperationFailure)
            self.assertEqual(bad.diagnostics[0].code, "lease_conflict")
            _close(stores)

    def test_subject_hash_mismatch(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("Subject", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            provider = ScriptProvider(map_size=1)
            # Advance to lecture_generation
            for i in range(8):
                req = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock=f"2026-01-01T00:0{i}:00Z")
                if not isinstance(req, SemanticWorkRequest):
                    break
                res = provider.execute(req)
                if req.kind == "lecture_generation":
                    bad = SemanticWorkResult(res.result_version, res.request_id, res.operation_id, res.kind, res.produced_artifacts, res.produced_documents, res.diagnostics, res.priority_subject_sha256, res.map_subject_sha256, "b" * 64, res.request_revision)
                    sub = submit_semantic_result(job_id, bad, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], holder_id=_HOLDER, clock=f"2026-01-01T00:0{i}:30Z")
                    self.assertIsInstance(sub, SemanticOperationFailure)
                    self.assertEqual(sub.diagnostics[0].code, "subject_hash_mismatch")
                    break
                sub = submit_semantic_result(job_id, res, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], holder_id=_HOLDER, clock=f"2026-01-01T00:0{i}:30Z")
                if isinstance(sub, SemanticOperationFailure):
                    if sub.diagnostics[0].code == "owner_decision_required":
                        if req.kind == "exam_priority_assessment":
                            ws_p = stores["workflow_store"].load(stores["job_store"].load(job_id).workflow_id)
                            p_subj = priority_subject_sha256(ws_p.priority_basis, ws_p.policies.priority_basis)
                            submit_owner_priority_decision(job_id, approve=True, subject_sha256=p_subj, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock=f"2026-01-01T00:0{i}:40Z")
                        elif req.kind == "lecture_map_generation":
                            ws_m = stores["workflow_store"].load(stores["job_store"].load(job_id).workflow_id)
                            m_subj = map_subject_sha256(ws_m.lecture_map, ws_m.policies.lecture_mapping)
                            submit_owner_map_decision(job_id, approve=True, subject_sha256=m_subj, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock=f"2026-01-01T00:0{i}:40Z")
            _close(stores)


class TestJobReconciliation(unittest.TestCase):
    def test_pending_to_awaiting_and_running(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("Recon", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            req = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:00:00Z")
            assert isinstance(req, SemanticWorkRequest)
            job = stores["job_store"].load(job_id)
            assert isinstance(job, CourseJobRecord)
            ws = stores["workflow_store"].load(job.workflow_id)
            assert isinstance(ws, WorkflowState)
            from course_compiler.semantic_operations import _reconcile_job_with_semantic
            reconciled = _reconcile_job_with_semantic(job, ws, stores["semantic_store"], "2026-01-01T00:01:00Z")  # type: ignore[arg-type]
            self.assertEqual(reconciled.status, "semantic_running")
            # Same logical request, expired, recoverable to awaiting_semantic
            reconciled_expired = _reconcile_job_with_semantic(job, ws, stores["semantic_store"], "2026-01-01T00:20:00Z")  # type: ignore[arg-type]
            self.assertEqual(reconciled_expired.status, "awaiting_semantic")
            _close(stores)


class TestLectureMapSubjectBinding(unittest.TestCase):
    def test_map_subject_binding_exact_success(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("MapExact", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            provider = ScriptProvider(map_size=1)
            _advance_to(stores, job_id, provider, target_kind="lecture_map_generation")
            req = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:02:00Z")
            assert isinstance(req, SemanticWorkRequest)
            self.assertEqual(req.kind, "lecture_map_generation")
            res = provider.execute(req)
            outcome = submit_semantic_result(job_id, res, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], holder_id=_HOLDER, clock="2026-01-01T00:02:30Z")
            self.assertIsInstance(outcome, WorkflowAdvanced)
            ws = stores["workflow_store"].load(stores["job_store"].load(job_id).workflow_id)
            self.assertEqual(ws.stage, "map_approval")
            _close(stores)

    def test_map_subject_binding_wrong_valid_64hex_fails_closed(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("MapBadHex", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            provider = ScriptProvider(map_size=1)
            _advance_to(stores, job_id, provider, target_kind="lecture_map_generation")
            req = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:02:00Z")
            assert isinstance(req, SemanticWorkRequest)
            res = provider.execute(req)
            bad_res = dataclasses.replace(res, map_subject_sha256="0" * 64)
            outcome = submit_semantic_result(job_id, bad_res, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], holder_id=_HOLDER, clock="2026-01-01T00:02:30Z")
            self.assertIsInstance(outcome, SemanticOperationFailure)
            self.assertEqual(outcome.diagnostics[0].code, "subject_hash_mismatch")
            ws = stores["workflow_store"].load(stores["job_store"].load(job_id).workflow_id)
            self.assertEqual(ws.stage, "lecture_mapping")
            self.assertEqual(ws.disposition, "ready")
            _close(stores)

    def test_map_subject_binding_different_map_bytes_fails_closed(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("MapBadBytes", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            provider = ScriptProvider(map_size=1)
            _advance_to(stores, job_id, provider, target_kind="lecture_map_generation")
            req = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:02:00Z")
            assert isinstance(req, SemanticWorkRequest)
            res = provider.execute(req)
            bad_art = ProducedArtifactSpec("lecture_map", res.produced_artifacts[0].artifact_id, b"l1|l2")
            bad_res = dataclasses.replace(res, produced_artifacts=(bad_art,))
            outcome = submit_semantic_result(job_id, bad_res, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], holder_id=_HOLDER, clock="2026-01-01T00:02:30Z")
            self.assertIsInstance(outcome, SemanticOperationFailure)
            self.assertEqual(outcome.diagnostics[0].code, "subject_hash_mismatch")
            _close(stores)

    def test_map_subject_binding_different_lecture_ids_fails_closed(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("MapBadIds", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            provider = ScriptProvider(map_size=1)
            _advance_to(stores, job_id, provider, target_kind="lecture_map_generation")
            req = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:02:00Z")
            assert isinstance(req, SemanticWorkRequest)
            res = provider.execute(req)
            bad_art = ProducedArtifactSpec("lecture_map", res.produced_artifacts[0].artifact_id, b"l99")
            bad_res = dataclasses.replace(res, produced_artifacts=(bad_art,))
            outcome = submit_semantic_result(job_id, bad_res, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], holder_id=_HOLDER, clock="2026-01-01T00:02:30Z")
            self.assertIsInstance(outcome, SemanticOperationFailure)
            self.assertIn(outcome.diagnostics[0].code, ("subject_hash_mismatch", "invalid_produced_payload"))
            _close(stores)

    def test_map_subject_binding_different_mapping_policy_fails_closed(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("MapBadPol", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            provider = ScriptProvider(map_size=1)
            _advance_to(stores, job_id, provider, target_kind="lecture_map_generation")
            req = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:02:00Z")
            assert isinstance(req, SemanticWorkRequest)
            art_id = f"art-{req.request_id[:8]}-lm"
            content = b"l1"
            sha = hashlib.sha256(content).hexdigest()
            producer = ARTIFACT_PRODUCERS["lecture_map"]
            map_ref = WorkflowArtifactReference("workflow-artifact-reference/v1", art_id, "lecture_map", sha, producer)
            pol_ref = PolicyReference("lecture_mapping", POLICY_VERSIONS["lecture_mapping"], "f" * 64)
            rec = LectureMapRecord(map_reference=map_ref, lecture_ids=("l1",), status="proposed", approval=None, reserved_lecture_ids=())
            different_pol_hash = map_subject_sha256(rec, pol_ref)
            res = SemanticWorkResult(
                SEMANTIC_WORK_RESULT_VERSION,
                req.request_id,
                req.operation_id,
                req.kind,
                produced_artifacts=(ProducedArtifactSpec("lecture_map", art_id, content),),
                produced_documents=(),
                diagnostics=(),
                priority_subject_sha256=None,
                map_subject_sha256=different_pol_hash,
                candidate_subject_sha256=None,
                request_revision=req.expected_revision,
            )
            outcome = submit_semantic_result(job_id, res, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], holder_id=_HOLDER, clock="2026-01-01T00:02:30Z")
            self.assertIsInstance(outcome, SemanticOperationFailure)
            self.assertEqual(outcome.diagnostics[0].code, "subject_hash_mismatch")
            _close(stores)

    def test_script_provider_map_subject_equals_t003_derived_subject(self) -> None:
        pol_sha = "a" * 64
        req = SemanticWorkRequest(
            SEMANTIC_WORK_REQUEST_VERSION,
            "req-map-1",
            "job-1",
            "wf-1",
            1,
            "op-map-1",
            "lecture_map_generation",
            (pol_sha,),
            "2026-01-01T00:00:00Z",
            None,
        )
        provider = ScriptProvider(map_size=2)
        res = provider.execute(req)
        self.assertEqual(res.kind, "lecture_map_generation")
        self.assertIsNotNone(res.map_subject_sha256)
        art = res.produced_artifacts[0]
        sha = hashlib.sha256(art.content_bytes).hexdigest()
        producer = ARTIFACT_PRODUCERS["lecture_map"]
        map_ref = WorkflowArtifactReference("workflow-artifact-reference/v1", art.artifact_id, "lecture_map", sha, producer)
        pol_ref = PolicyReference("lecture_mapping", POLICY_VERSIONS["lecture_mapping"], pol_sha)
        rec = LectureMapRecord(map_reference=map_ref, lecture_ids=("l1", "l2"), status="proposed", approval=None, reserved_lecture_ids=())
        expected = map_subject_sha256(rec, pol_ref)
        self.assertEqual(res.map_subject_sha256, expected)

    def test_map_reopen_reserved_id_subject_derivation(self) -> None:
        pol_sha = "b" * 64
        req = SemanticWorkRequest(
            SEMANTIC_WORK_REQUEST_VERSION,
            "req-map-2",
            "job-2",
            "wf-2",
            5,
            "op-map-2",
            "lecture_map_generation",
            (pol_sha, "l1", "l2"),
            "2026-01-01T00:00:00Z",
            None,
        )
        provider = ScriptProvider(map_size=3)
        res = provider.execute(req)
        art = res.produced_artifacts[0]
        sha = hashlib.sha256(art.content_bytes).hexdigest()
        producer = ARTIFACT_PRODUCERS["lecture_map"]
        map_ref = WorkflowArtifactReference("workflow-artifact-reference/v1", art.artifact_id, "lecture_map", sha, producer)
        pol_ref = PolicyReference("lecture_mapping", POLICY_VERSIONS["lecture_mapping"], pol_sha)
        rec = LectureMapRecord(map_reference=map_ref, lecture_ids=("l1", "l2", "l3"), status="proposed", approval=None, reserved_lecture_ids=("l1", "l2"))
        expected = map_subject_sha256(rec, pol_ref)
        self.assertEqual(res.map_subject_sha256, expected)


class TestLectureGenerationCrashRecovery(unittest.TestCase):
    def test_r5_l1_crash_after_candidate_transition_resumes_validation(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("R5L1", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            provider = ScriptProvider(map_size=1)
            _advance_to(stores, job_id, provider, target_kind="lecture_generation")
            req = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:05:00Z")
            assert isinstance(req, SemanticWorkRequest)
            self.assertEqual(req.kind, "lecture_generation")
            res = provider.execute(req)

            # Crash immediately after submit_lecture_candidate transition, before validation artifact persistence
            real_save = LocalWorkflowArtifactStore.save

            def crash_save(s_self, ref_art, content):
                if getattr(ref_art, "artifact_kind", None) == "validation":
                    raise RuntimeError("Simulated crash on saving validation")
                return real_save(s_self, ref_art, content)

            with patch.object(LocalWorkflowArtifactStore, "save", crash_save):
                out_crash = submit_semantic_result(
                    job_id, res,
                    job_store=stores["job_store"],
                    workflow_store=stores["workflow_store"],
                    semantic_store=stores["semantic_store"],
                    association_store=stores["assoc_store"],
                    source_store=stores["source_store"],
                    policy_store=stores["policy_store"],
                    artifact_store=stores["artifact_store"],
                    document_store=stores["document_store"],
                    holder_id=_HOLDER,
                    clock="2026-01-01T00:05:30Z",
                )
                self.assertIsInstance(out_crash, SemanticOperationFailure)
            _close(stores)

            # Verify crashed intermediate state: candidate receipt exists, validation receipt absent
            fresh = _new_stores(tmp)
            job = fresh["job_store"].load(job_id)
            assert isinstance(job, CourseJobRecord)
            ws = fresh["workflow_store"].load(job.workflow_id)
            assert isinstance(ws, WorkflowState)
            self.assertEqual(ws.stage, "lecture_validation")
            self.assertTrue(any(rc.operation_id == res.operation_id for rc in ws.operation_receipts))
            self.assertFalse(any(rc.operation_id == f"{res.operation_id}-validation" for rc in ws.operation_receipts))
            sem_info = fresh["semantic_store"].lease_info(res.request_id)
            self.assertEqual(sem_info[0], "leased")

            # Reopen and exact retry: validation runs, validation artifact exists, validation receipt exists, semantic accepted
            outcome = submit_semantic_result(
                job_id, res,
                job_store=fresh["job_store"],
                workflow_store=fresh["workflow_store"],
                semantic_store=fresh["semantic_store"],
                association_store=fresh["assoc_store"],
                source_store=fresh["source_store"],
                policy_store=fresh["policy_store"],
                artifact_store=fresh["artifact_store"],
                document_store=fresh["document_store"],
                holder_id=_HOLDER,
                clock="2026-01-01T00:06:00Z",
            )
            self.assertIsInstance(outcome, WorkflowAdvanced)
            ws_final = fresh["workflow_store"].load(job.workflow_id)
            assert isinstance(ws_final, WorkflowState)
            val_rc = next((rc for rc in ws_final.operation_receipts if rc.operation_id == f"{res.operation_id}-validation"), None)
            self.assertIsNotNone(val_rc)
            self.assertEqual(val_rc.action, "record_candidate_validation")
            sem_after = fresh["semantic_store"].lease_info(res.request_id)
            self.assertEqual(sem_after[0], "accepted")

            # Idempotent repeat on subsequent submission
            rep = submit_semantic_result(
                job_id, res,
                job_store=fresh["job_store"],
                workflow_store=fresh["workflow_store"],
                semantic_store=fresh["semantic_store"],
                association_store=fresh["assoc_store"],
                source_store=fresh["source_store"],
                policy_store=fresh["policy_store"],
                artifact_store=fresh["artifact_store"],
                document_store=fresh["document_store"],
                holder_id=_HOLDER,
                clock="2026-01-01T00:06:30Z",
            )
            self.assertIsInstance(rep, WorkflowIdempotentRepeat)
            _close(fresh)

    def test_r5_l2_crash_after_validation_artifact_before_transition(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("R5L2", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            provider = ScriptProvider(map_size=1)
            _advance_to(stores, job_id, provider, target_kind="lecture_generation")
            req = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:05:00Z")
            assert isinstance(req, SemanticWorkRequest)
            res = provider.execute(req)

            # Crash after validation artifact saved, before validation workflow transition
            real_wf_save = LocalWorkflowStateStore.save

            def crash_wf_save(s_self, ws):
                if any(rc.operation_id.endswith("-validation") for rc in ws.operation_receipts):
                    raise RuntimeError("Simulated crash on saving workflow with op suffix -validation")
                return real_wf_save(s_self, ws)

            with patch.object(LocalWorkflowStateStore, "save", crash_wf_save):
                out_crash = submit_semantic_result(
                    job_id, res,
                    job_store=stores["job_store"],
                    workflow_store=stores["workflow_store"],
                    semantic_store=stores["semantic_store"],
                    association_store=stores["assoc_store"],
                    source_store=stores["source_store"],
                    policy_store=stores["policy_store"],
                    artifact_store=stores["artifact_store"],
                    document_store=stores["document_store"],
                    holder_id=_HOLDER,
                    clock="2026-01-01T00:05:30Z",
                )
                self.assertIsInstance(out_crash, SemanticOperationFailure)
            _close(stores)

            # Reopen fresh stores: artifact exists, retry reuses/re-saves idempotently and completes transition
            fresh = _new_stores(tmp)
            outcome = submit_semantic_result(
                job_id, res,
                job_store=fresh["job_store"],
                workflow_store=fresh["workflow_store"],
                semantic_store=fresh["semantic_store"],
                association_store=fresh["assoc_store"],
                source_store=fresh["source_store"],
                policy_store=fresh["policy_store"],
                artifact_store=fresh["artifact_store"],
                document_store=fresh["document_store"],
                holder_id=_HOLDER,
                clock="2026-01-01T00:06:00Z",
            )
            self.assertIsInstance(outcome, WorkflowAdvanced)
            ws_final = fresh["workflow_store"].load(fresh["job_store"].load(job_id).workflow_id)
            val_rc = next((rc for rc in ws_final.operation_receipts if rc.operation_id == f"{res.operation_id}-validation"), None)
            self.assertIsNotNone(val_rc)
            _close(fresh)

    def test_r5_l3_crash_after_validation_transition_before_mark_accepted(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("R5L3", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            provider = ScriptProvider(map_size=1)
            _advance_to(stores, job_id, provider, target_kind="lecture_generation")
            req = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:05:00Z")
            assert isinstance(req, SemanticWorkRequest)
            res = provider.execute(req)

            # Crash after validation transition, before semantic_store.mark_accepted
            def crash_sem_mark(s_self, result):
                raise RuntimeError("Simulated crash on mark_accepted")

            with patch.object(LocalSemanticWorkStore, "mark_accepted", crash_sem_mark):
                out_crash = submit_semantic_result(
                    job_id, res,
                    job_store=stores["job_store"],
                    workflow_store=stores["workflow_store"],
                    semantic_store=stores["semantic_store"],
                    association_store=stores["assoc_store"],
                    source_store=stores["source_store"],
                    policy_store=stores["policy_store"],
                    artifact_store=stores["artifact_store"],
                    document_store=stores["document_store"],
                    holder_id=_HOLDER,
                    clock="2026-01-01T00:05:30Z",
                )
                self.assertIsInstance(out_crash, SemanticOperationFailure)
            _close(stores)

            # Reopen fresh stores: Case A3 enters, workflow state preserved, semantic row reconciled/accepted
            fresh = _new_stores(tmp)
            ws_before = fresh["workflow_store"].load(fresh["job_store"].load(job_id).workflow_id)
            rev_before = ws_before.revision
            outcome = submit_semantic_result(
                job_id, res,
                job_store=fresh["job_store"],
                workflow_store=fresh["workflow_store"],
                semantic_store=fresh["semantic_store"],
                association_store=fresh["assoc_store"],
                source_store=fresh["source_store"],
                policy_store=fresh["policy_store"],
                artifact_store=fresh["artifact_store"],
                document_store=fresh["document_store"],
                holder_id=_HOLDER,
                clock="2026-01-01T00:06:00Z",
            )
            self.assertIsInstance(outcome, WorkflowIdempotentRepeat)
            ws_after = fresh["workflow_store"].load(fresh["job_store"].load(job_id).workflow_id)
            self.assertEqual(ws_after.revision, rev_before)
            sem_after = fresh["semantic_store"].lease_info(res.request_id)
            self.assertEqual(sem_after[0], "accepted")
            _close(fresh)

    def test_r5_l4_wrong_conflicting_resubmission_after_candidate_receipt_fails_closed(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("R5L4", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(ref.course_id, course_store=stores["course_store"], job_store=stores["job_store"], workflow_store=stores["workflow_store"], association_store=stores["assoc_store"], source_store=stores["source_store"], policy_store=stores["policy_store"], artifact_store=stores["artifact_store"], document_store=stores["document_store"], clock="2026-01-01T00:00:00Z")
            provider = ScriptProvider(map_size=1)
            _advance_to(stores, job_id, provider, target_kind="lecture_generation")
            req = request_semantic_work(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"], semantic_store=stores["semantic_store"], holder_id=_HOLDER, clock="2026-01-01T00:05:00Z")
            assert isinstance(req, SemanticWorkRequest)
            res = provider.execute(req)

            # Crash after submit_lecture_candidate transition
            real_save = LocalWorkflowArtifactStore.save

            def crash_save(s_self, ref_art, content):
                if getattr(ref_art, "artifact_kind", None) == "validation":
                    raise RuntimeError("Simulated crash on saving validation")
                return real_save(s_self, ref_art, content)

            with patch.object(LocalWorkflowArtifactStore, "save", crash_save):
                out_crash = submit_semantic_result(
                    job_id, res,
                    job_store=stores["job_store"],
                    workflow_store=stores["workflow_store"],
                    semantic_store=stores["semantic_store"],
                    association_store=stores["assoc_store"],
                    source_store=stores["source_store"],
                    policy_store=stores["policy_store"],
                    artifact_store=stores["artifact_store"],
                    document_store=stores["document_store"],
                    holder_id=_HOLDER,
                    clock="2026-01-01T00:05:30Z",
                )
                self.assertIsInstance(out_crash, SemanticOperationFailure)
            _close(stores)

            # Reopen fresh stores: submit a conflicting semantic result with different document text
            fresh = _new_stores(tmp)
            bad_doc = ProducedDocumentSpec("l1", "Conflicting lecture text that does not match candidate")
            bad_sha = hashlib.sha256(bad_doc.source_text.encode("utf-8")).hexdigest()
            bad_ref = DocumentReference("lecture-document/v1", "l1", 1, bad_sha)
            bad_subj = candidate_subject_sha256(bad_ref)
            conflicting_res = dataclasses.replace(
                res,
                produced_documents=(bad_doc,),
                candidate_subject_sha256=bad_subj,
            )
            outcome = submit_semantic_result(
                job_id, conflicting_res,
                job_store=fresh["job_store"],
                workflow_store=fresh["workflow_store"],
                semantic_store=fresh["semantic_store"],
                association_store=fresh["assoc_store"],
                source_store=fresh["source_store"],
                policy_store=fresh["policy_store"],
                artifact_store=fresh["artifact_store"],
                document_store=fresh["document_store"],
                holder_id=_HOLDER,
                clock="2026-01-01T00:06:00Z",
            )
            self.assertIsInstance(outcome, SemanticOperationFailure)
            self.assertIn(outcome.diagnostics[0].code, ("result_identity_mismatch", "subject_hash_mismatch"))

            # Verify it did not accept the conflicting result or advance the workflow
            sem_status = fresh["semantic_store"].lease_info(res.request_id)
            self.assertEqual(sem_status[0], "leased")
            ws_cur = fresh["workflow_store"].load(fresh["job_store"].load(job_id).workflow_id)
            self.assertEqual(ws_cur.stage, "lecture_validation")
            self.assertFalse(any(rc.operation_id == f"{res.operation_id}-validation" for rc in ws_cur.operation_receipts))
            _close(fresh)


if __name__ == "__main__":
    unittest.main()
