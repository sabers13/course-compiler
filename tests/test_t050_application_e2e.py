"""Committed automated end-to-end application test for T050.

Validates:
1. Application context & persistent store initialization in isolated directory
2. Course creation and source attachments (2 synthetic sources)
3. Job creation & semantic cycle with ScriptProvider through explicit owner decisions
4. Transition to `deterministic_building` status upon workflow completion
5. Authoritative `BuildRecord` creation and Job advancement to `completed`
6. Memoized PDF cache write and artifact retrieval
7. Cache file deletion and deterministic re-derivation (cache miss)
8. Process crash, store restart, and clean workflow resumption proof
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.toolchain_support import TEX_MISSING_REASON, TEX_TOOLCHAIN_AVAILABLE

from course_compiler.app.config import create_config
from course_compiler.app.context import AppContext
from course_compiler.build_operations import build_pdf, get_artifact, get_artifact_history
from course_compiler.build_persistence import BuildRecord, open_build_record_store
from course_compiler.course_job_persistence import CourseJobRecord, open_course_job_store
from course_compiler.course_operations import attach_source, create_course
from course_compiler.course_persistence import open_course_store
from course_compiler.course_workflow_persistence import open_course_workflow_association_store
from course_compiler.lecture_document_persistence import open_lecture_document_store
from course_compiler.policy_persistence import open_policy_content_store
from course_compiler.semantic_operations import (
    SemanticOperationFailure,
    request_semantic_work,
    start_generation,
    submit_owner_map_decision,
    submit_owner_priority_decision,
    submit_semantic_result,
)
from course_compiler.semantic_work import ScriptProvider
from course_compiler.semantic_work_persistence import open_semantic_work_store
from course_compiler.source_persistence import open_source_evidence_store
from course_compiler.workflow import WorkflowState, map_subject_sha256, priority_subject_sha256
from course_compiler.workflow_artifact_persistence import open_workflow_artifact_store
from course_compiler.workflow_persistence import open_workflow_state_store

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_ISOLATED_BASE = REPOSITORY_ROOT / "local-data" / "test-tmp-e2e"
_ISOLATED_BASE.mkdir(parents=True, exist_ok=True)


def _isolated_tmp_dir() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(dir=str(_ISOLATED_BASE), prefix="e2e-")


def _make_dummy_pdf(seed: str) -> bytes:
    content = (
        f"%PDF-1.4\n%{seed}\n"
        "1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        "2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        "3 0 obj<</Type/Page/MediaBox[0 0 300 144]/Parent 2 0 R/Resources<<>>>>endobj\n"
        "xref\n0 4\n0000000000 65535 f \n0000000013 00000 n \n0000000062 00000 n \n0000000119 00000 n \n"
        "trailer<</Size 4/Root 1 0 R>>\nstartxref\n210\n%%EOF\n"
    )
    return content.encode("latin1")


@unittest.skipUnless(
    TEX_TOOLCHAIN_AVAILABLE,
    f"real XeLaTeX build unavailable ({TEX_MISSING_REASON})",
)
class TestT050ApplicationE2E(unittest.TestCase):
    def test_full_deterministic_application_e2e_with_cache_rederivation(self) -> None:
        with _isolated_tmp_dir() as tmp:
            data_root = Path(tmp)
            cfg = create_config(host="127.0.0.1", port=0, data_root=data_root)
            ctx = AppContext(cfg)
            ctx.prepare()

            # Initialize all durable stores
            course_store = open_course_store(ctx.database_path("courses.sqlite3"))
            job_store = open_course_job_store(ctx.database_path("course-jobs.sqlite3"))
            assoc_store = open_course_workflow_association_store(ctx.database_path("course-workflow-associations.sqlite3"))
            source_store = open_source_evidence_store(ctx.database_path("source-evidence.sqlite3"))
            workflow_store = open_workflow_state_store(ctx.database_path("workflow-state.sqlite3"))
            policy_store = open_policy_content_store(ctx.database_path("policy-content.sqlite3"))
            artifact_store = open_workflow_artifact_store(ctx.database_path("workflow-artifacts.sqlite3"))
            doc_store = open_lecture_document_store(ctx.database_path("lecture-documents.sqlite3"))
            sem_store = open_semantic_work_store(ctx.database_path("semantic-work.sqlite3"))
            build_store = open_build_record_store(ctx.database_path("build-records.sqlite3"))
            cache_root = ctx.cache_root_path()

            try:
                # 1. Create Course
                course_ref = create_course(
                    "Algorithmic Foundations",
                    ai_mode="gpt",
                    quality_mode="fast",
                    course_store=course_store,
                    association_store=assoc_store,
                )
                self.assertFalse(isinstance(course_ref, Exception))
                course_id = course_ref.course_id

                # 2. Attach two distinct synthetic sources
                src1 = attach_source(
                    course_id,
                    _make_dummy_pdf("alpha-lecture-notes"),
                    "notes-part-1",
                    course_store=course_store,
                    source_store=source_store,
                )
                self.assertFalse(isinstance(src1, Exception))

                src2 = attach_source(
                    course_id,
                    _make_dummy_pdf("beta-lecture-notes"),
                    "notes-part-2",
                    course_store=course_store,
                    source_store=source_store,
                )
                self.assertFalse(isinstance(src2, Exception))

                # 3. Start Generation (creates CourseJob & WorkflowState)
                job_id = start_generation(
                    course_id,
                    course_store=course_store,
                    job_store=job_store,
                    workflow_store=workflow_store,
                    association_store=assoc_store,
                    source_store=source_store,
                    policy_store=policy_store,
                    artifact_store=artifact_store,
                    document_store=doc_store,
                )
                self.assertIsInstance(job_id, str)
                initial_job = job_store.load(job_id)
                self.assertIsInstance(initial_job, CourseJobRecord)
                self.assertIn(initial_job.status, ("sourcing", "in_progress"))
                self.assertIsNone(initial_job.completed_build_id)
                self.assertIsNone(initial_job.completed_build_sha256)

                # 4. Controlled semantic cycle with ScriptProvider (map_size=2: 2 lectures)
                provider = ScriptProvider(map_size=2)
                holder_id = "test-e2e-worker"

                for i in range(25):
                    ws = workflow_store.load(initial_job.workflow_id)
                    self.assertIsInstance(ws, WorkflowState)
                    if ws.stage == "completed":
                        break

                    req = request_semantic_work(
                        job_id,
                        job_store=job_store,
                        workflow_store=workflow_store,
                        semantic_store=sem_store,
                        holder_id=holder_id,
                        clock=f"2026-02-01T10:{i:02d}:00Z",
                    )
                    if isinstance(req, SemanticOperationFailure):
                        # Owner decision point
                        if ws.stage == "priority_approval":
                            assert ws.priority_basis is not None
                            subj = priority_subject_sha256(ws.priority_basis, ws.policies.priority_basis)
                            submit_owner_priority_decision(
                                job_id,
                                approve=True,
                                subject_sha256=subj,
                                job_store=job_store,
                                workflow_store=workflow_store,
                                semantic_store=sem_store,
                                association_store=assoc_store,
                                source_store=source_store,
                                policy_store=policy_store,
                                artifact_store=artifact_store,
                                document_store=doc_store,
                                clock=f"2026-02-01T10:{i:02d}:15Z",
                            )
                        elif ws.stage == "map_approval":
                            assert ws.lecture_map is not None
                            subj = map_subject_sha256(ws.lecture_map, ws.policies.lecture_mapping)
                            submit_owner_map_decision(
                                job_id,
                                approve=True,
                                subject_sha256=subj,
                                job_store=job_store,
                                workflow_store=workflow_store,
                                semantic_store=sem_store,
                                association_store=assoc_store,
                                source_store=source_store,
                                policy_store=policy_store,
                                artifact_store=artifact_store,
                                document_store=doc_store,
                                clock=f"2026-02-01T10:{i:02d}:15Z",
                            )
                        continue

                    # Execute task with provider and submit
                    result_payload = provider.execute(req)
                    submit_res = submit_semantic_result(
                        job_id,
                        result_payload,
                        job_store=job_store,
                        workflow_store=workflow_store,
                        semantic_store=sem_store,
                        association_store=assoc_store,
                        source_store=source_store,
                        policy_store=policy_store,
                        artifact_store=artifact_store,
                        document_store=doc_store,
                        holder_id=holder_id,
                        clock=f"2026-02-01T10:{i:02d}:30Z",
                    )
                    if isinstance(submit_res, SemanticOperationFailure) and submit_res.diagnostics[0].code == "owner_decision_required":
                        ws_check = workflow_store.load(initial_job.workflow_id)
                        if req.kind == "exam_priority_assessment":
                            assert ws_check.priority_basis is not None
                            subj = priority_subject_sha256(ws_check.priority_basis, ws_check.policies.priority_basis)
                            submit_owner_priority_decision(
                                job_id,
                                approve=True,
                                subject_sha256=subj,
                                job_store=job_store,
                                workflow_store=workflow_store,
                                semantic_store=sem_store,
                                association_store=assoc_store,
                                source_store=source_store,
                                policy_store=policy_store,
                                artifact_store=artifact_store,
                                document_store=doc_store,
                                clock=f"2026-02-01T10:{i:02d}:45Z",
                            )
                        elif req.kind == "lecture_map_generation":
                            assert ws_check.lecture_map is not None
                            subj = map_subject_sha256(ws_check.lecture_map, ws_check.policies.lecture_mapping)
                            submit_owner_map_decision(
                                job_id,
                                approve=True,
                                subject_sha256=subj,
                                job_store=job_store,
                                workflow_store=workflow_store,
                                semantic_store=sem_store,
                                association_store=assoc_store,
                                source_store=source_store,
                                policy_store=policy_store,
                                artifact_store=artifact_store,
                                document_store=doc_store,
                                clock=f"2026-02-01T10:{i:02d}:45Z",
                            )

                # 5. Invariant: WorkflowState completed -> Job status is deterministic_building
                final_ws = workflow_store.load(initial_job.workflow_id)
                self.assertIsInstance(final_ws, WorkflowState)
                self.assertEqual(final_ws.stage, "completed")
                self.assertEqual(final_ws.disposition, "completed")

                job_pre_build = job_store.load(job_id)
                self.assertIsInstance(job_pre_build, CourseJobRecord)
                self.assertEqual(job_pre_build.status, "deterministic_building")
                self.assertIsNone(job_pre_build.completed_build_id)
                self.assertIsNone(job_pre_build.completed_build_sha256)

                # 6. Execute build_pdf -> produces BuildRecord, updates Job to completed
                build_res = build_pdf(
                    job_id,
                    job_store=job_store,
                    association_store=assoc_store,
                    workflow_store=workflow_store,
                    policy_store=policy_store,
                    document_store=doc_store,
                    build_store=build_store,
                    cache_root=cache_root,
                )
                self.assertIsInstance(build_res, BuildRecord)
                build_id = build_res.build_id
                bundle_hash = build_res.bundle_hash
                self.assertEqual(build_res.status, "succeeded")

                job_post_build = job_store.load(job_id)
                self.assertIsInstance(job_post_build, CourseJobRecord)
                self.assertEqual(job_post_build.status, "completed")
                self.assertEqual(job_post_build.completed_build_id, build_id)
                self.assertEqual(job_post_build.completed_build_sha256, bundle_hash)

                # 7. Memoized cache file exists
                cache_file = cache_root / f"{bundle_hash}.pdf"
                self.assertTrue(cache_file.is_file())
                cached_bytes = cache_file.read_bytes()
                self.assertTrue(cached_bytes.startswith(b"%PDF"))

                # 8. Retrieve artifact via get_artifact (cache hit)
                art_hit = get_artifact(
                    build_id,
                    build_store=build_store,
                    job_store=job_store,
                    association_store=assoc_store,
                    workflow_store=workflow_store,
                    policy_store=policy_store,
                    document_store=doc_store,
                    cache_root=cache_root,
                )
                self.assertEqual(art_hit, cached_bytes)

                # 9. Verify artifact history
                history = get_artifact_history(job_id, build_store=build_store)
                self.assertEqual(len(history), 1)
                self.assertEqual(history[0].build_id, build_id)

                # 10. Cache deletion -> cache miss re-derivation
                cache_file.unlink()
                self.assertFalse(cache_file.exists())

                art_rederived = get_artifact(
                    build_id,
                    build_store=build_store,
                    job_store=job_store,
                    association_store=assoc_store,
                    workflow_store=workflow_store,
                    policy_store=policy_store,
                    document_store=doc_store,
                    cache_root=cache_root,
                )
                self.assertIsInstance(art_rederived, bytes)
                self.assertTrue(art_rederived.startswith(b"%PDF"))
                # Deterministic re-derivation: exact byte equality with the
                # originally compiled artifact, not merely "a valid PDF".
                self.assertEqual(art_rederived, art_hit)
                self.assertEqual(
                    hashlib.sha256(art_rederived).hexdigest(),
                    hashlib.sha256(art_hit).hexdigest(),
                )
                # Cache file must be restored
                self.assertTrue(cache_file.is_file())
                self.assertEqual(cache_file.read_bytes(), art_rederived)

                # 11. Reopen stores from scratch and re-verify persistence
                for s in (course_store, job_store, assoc_store, source_store, workflow_store, policy_store, artifact_store, doc_store, sem_store, build_store):
                    s.close()

                # Reopen fresh stores
                job_store2 = open_course_job_store(ctx.database_path("course-jobs.sqlite3"))
                build_store2 = open_build_record_store(ctx.database_path("build-records.sqlite3"))
                assoc_store2 = open_course_workflow_association_store(ctx.database_path("course-workflow-associations.sqlite3"))
                workflow_store2 = open_workflow_state_store(ctx.database_path("workflow-state.sqlite3"))
                policy_store2 = open_policy_content_store(ctx.database_path("policy-content.sqlite3"))
                doc_store2 = open_lecture_document_store(ctx.database_path("lecture-documents.sqlite3"))

                # Delete the memoized cache again so the fresh stores are
                # forced to re-derive rather than serve a cache hit.
                cache_file.unlink()
                self.assertFalse(cache_file.exists())

                try:
                    reloaded_job = job_store2.load(job_id)
                    self.assertIsInstance(reloaded_job, CourseJobRecord)
                    self.assertEqual(reloaded_job.status, "completed")
                    self.assertEqual(reloaded_job.completed_build_id, build_id)

                    reloaded_build = build_store2.load(build_id)
                    self.assertIsInstance(reloaded_build, BuildRecord)
                    self.assertEqual(reloaded_build.bundle_hash, bundle_hash)

                    art_fresh = get_artifact(
                        build_id,
                        build_store=build_store2,
                        job_store=job_store2,
                        association_store=assoc_store2,
                        workflow_store=workflow_store2,
                        policy_store=policy_store2,
                        document_store=doc_store2,
                        cache_root=cache_root,
                    )
                    # Fresh AppContext stores + deleted cache re-derive the
                    # exact original bytes.
                    self.assertEqual(art_fresh, art_rederived)
                    self.assertEqual(art_fresh, art_hit)
                    self.assertTrue(cache_file.is_file())
                finally:
                    for s in (job_store2, build_store2, assoc_store2, workflow_store2, policy_store2, doc_store2):
                        s.close()

            finally:
                ctx.close()

    def test_process_crash_and_resume_proof(self) -> None:
        with _isolated_tmp_dir() as tmp:
            data_root = Path(tmp)
            cfg = create_config(host="127.0.0.1", port=0, data_root=data_root)
            ctx = AppContext(cfg)
            ctx.prepare()

            # Session 1: Run until mid-workflow
            c_store = open_course_store(ctx.database_path("courses.sqlite3"))
            j_store = open_course_job_store(ctx.database_path("course-jobs.sqlite3"))
            a_store = open_course_workflow_association_store(ctx.database_path("course-workflow-associations.sqlite3"))
            s_store = open_source_evidence_store(ctx.database_path("source-evidence.sqlite3"))
            w_store = open_workflow_state_store(ctx.database_path("workflow-state.sqlite3"))
            p_store = open_policy_content_store(ctx.database_path("policy-content.sqlite3"))
            art_store = open_workflow_artifact_store(ctx.database_path("workflow-artifacts.sqlite3"))
            d_store = open_lecture_document_store(ctx.database_path("lecture-documents.sqlite3"))
            sem_store = open_semantic_work_store(ctx.database_path("semantic-work.sqlite3"))
            b_store = open_build_record_store(ctx.database_path("build-records.sqlite3"))

            c_ref = create_course("Crash Resilience Course", ai_mode="gpt", quality_mode="fast", course_store=c_store, association_store=a_store)
            attach_source(c_ref.course_id, _make_dummy_pdf("resilience-1"), "res-1", course_store=c_store, source_store=s_store)
            attach_source(c_ref.course_id, _make_dummy_pdf("resilience-2"), "res-2", course_store=c_store, source_store=s_store)

            job_id = start_generation(
                c_ref.course_id,
                course_store=c_store,
                job_store=j_store,
                workflow_store=w_store,
                association_store=a_store,
                source_store=s_store,
                policy_store=p_store,
                artifact_store=art_store,
                document_store=d_store,
            )

            # Two lectures, so the kill point can fall after one lecture has
            # been accepted and while the other is still outstanding.
            provider = ScriptProvider(map_size=2)
            # Step 1: priority
            req1 = request_semantic_work(job_id, job_store=j_store, workflow_store=w_store, semantic_store=sem_store, holder_id="crash-worker")
            res1 = provider.execute(req1)
            submit_semantic_result(job_id, res1, job_store=j_store, workflow_store=w_store, semantic_store=sem_store, association_store=a_store, source_store=s_store, policy_store=p_store, artifact_store=art_store, document_store=d_store, holder_id="crash-worker")
            ws1 = w_store.load(j_store.load(job_id).workflow_id)
            subj1 = priority_subject_sha256(ws1.priority_basis, ws1.policies.priority_basis)
            submit_owner_priority_decision(job_id, approve=True, subject_sha256=subj1, job_store=j_store, workflow_store=w_store, semantic_store=sem_store, association_store=a_store, source_store=s_store, policy_store=p_store, artifact_store=art_store, document_store=d_store)

            # Step 2: map
            req2 = request_semantic_work(job_id, job_store=j_store, workflow_store=w_store, semantic_store=sem_store, holder_id="crash-worker")
            res2 = provider.execute(req2)
            submit_semantic_result(job_id, res2, job_store=j_store, workflow_store=w_store, semantic_store=sem_store, association_store=a_store, source_store=s_store, policy_store=p_store, artifact_store=art_store, document_store=d_store, holder_id="crash-worker")
            ws2 = w_store.load(j_store.load(job_id).workflow_id)
            subj2 = map_subject_sha256(ws2.lecture_map, ws2.policies.lecture_mapping)
            submit_owner_map_decision(job_id, approve=True, subject_sha256=subj2, job_store=j_store, workflow_store=w_store, semantic_store=sem_store, association_store=a_store, source_store=s_store, policy_store=p_store, artifact_store=art_store, document_store=d_store)

            # Step 3: drive lecture work until exactly one lecture is accepted
            # and at least one remains, then kill there.
            workflow_id = j_store.load(job_id).workflow_id
            for i in range(20):
                ws_now = w_store.load(workflow_id)
                accepted_now = sum(
                    1 for lp in ws_now.lecture_progress if lp.status == "accepted"
                )
                if accepted_now >= 1:
                    break
                req_n = request_semantic_work(
                    job_id,
                    job_store=j_store,
                    workflow_store=w_store,
                    semantic_store=sem_store,
                    holder_id="crash-worker",
                )
                if isinstance(req_n, SemanticOperationFailure):
                    break
                submit_semantic_result(
                    job_id,
                    provider.execute(req_n),
                    job_store=j_store,
                    workflow_store=w_store,
                    semantic_store=sem_store,
                    association_store=a_store,
                    source_store=s_store,
                    policy_store=p_store,
                    artifact_store=art_store,
                    document_store=d_store,
                    holder_id="crash-worker",
                )

            mid_ws = w_store.load(j_store.load(job_id).workflow_id)
            mid_accepted = tuple(
                lp for lp in mid_ws.lecture_progress if lp.status == "accepted"
            )
            mid_outstanding = tuple(
                lp for lp in mid_ws.lecture_progress if lp.status != "accepted"
            )
            # The kill point: at least one lecture accepted, work still remaining.
            self.assertGreaterEqual(len(mid_accepted), 1)
            self.assertGreaterEqual(len(mid_outstanding), 1)
            self.assertNotEqual(mid_ws.stage, "completed")
            mid_rev = mid_ws.revision
            mid_accepted_ids = tuple(lp.lecture_id for lp in mid_accepted)

            # SIMULATE CRASH: close all stores abruptly
            for s in (c_store, j_store, a_store, s_store, w_store, p_store, art_store, d_store, sem_store, b_store):
                s.close()
            ctx.close()

            # SIMULATE RESTART: fresh context and reopen stores
            ctx2 = AppContext(cfg)
            c_store2 = open_course_store(ctx2.database_path("courses.sqlite3"))
            j_store2 = open_course_job_store(ctx2.database_path("course-jobs.sqlite3"))
            a_store2 = open_course_workflow_association_store(ctx2.database_path("course-workflow-associations.sqlite3"))
            s_store2 = open_source_evidence_store(ctx2.database_path("source-evidence.sqlite3"))
            w_store2 = open_workflow_state_store(ctx2.database_path("workflow-state.sqlite3"))
            p_store2 = open_policy_content_store(ctx2.database_path("policy-content.sqlite3"))
            art_store2 = open_workflow_artifact_store(ctx2.database_path("workflow-artifacts.sqlite3"))
            d_store2 = open_lecture_document_store(ctx2.database_path("lecture-documents.sqlite3"))
            sem_store2 = open_semantic_work_store(ctx2.database_path("semantic-work.sqlite3"))
            b_store2 = open_build_record_store(ctx2.database_path("build-records.sqlite3"))

            try:
                # Invariant: durable state preserved across restart
                reopened_job = j_store2.load(job_id)
                self.assertEqual(reopened_job.status, "awaiting_semantic")
                reopened_ws = w_store2.load(reopened_job.workflow_id)
                self.assertEqual(reopened_ws.revision, mid_rev)
                # The already-accepted lecture survived the kill, proving the
                # accepted work lives in durable storage and not process memory.
                reopened_accepted = tuple(
                    lp.lecture_id
                    for lp in reopened_ws.lecture_progress
                    if lp.status == "accepted"
                )
                self.assertEqual(reopened_accepted, mid_accepted_ids)

                # Reacquire the exact pending work from the fresh stores and
                # finish the remaining lectures.
                for i in range(20):
                    ws_loop = w_store2.load(reopened_job.workflow_id)
                    if ws_loop.stage == "completed":
                        break
                    req_r = request_semantic_work(
                        job_id,
                        job_store=j_store2,
                        workflow_store=w_store2,
                        semantic_store=sem_store2,
                        holder_id="resumed-worker",
                    )
                    if isinstance(req_r, SemanticOperationFailure):
                        break
                    submit_semantic_result(
                        job_id,
                        provider.execute(req_r),
                        job_store=j_store2,
                        workflow_store=w_store2,
                        semantic_store=sem_store2,
                        association_store=a_store2,
                        source_store=s_store2,
                        policy_store=p_store2,
                        artifact_store=art_store2,
                        document_store=d_store2,
                        holder_id="resumed-worker",
                    )

                final_ws = w_store2.load(reopened_job.workflow_id)
                self.assertEqual(final_ws.stage, "completed")
                self.assertGreater(final_ws.revision, mid_rev)

                # Verify deterministic_building status
                self.assertEqual(j_store2.load(job_id).status, "deterministic_building")

                # Build PDF
                build_rec = build_pdf(
                    job_id,
                    job_store=j_store2,
                    association_store=a_store2,
                    workflow_store=w_store2,
                    policy_store=p_store2,
                    document_store=d_store2,
                    build_store=b_store2,
                    cache_root=ctx2.cache_root_path(),
                )
                self.assertIsInstance(build_rec, BuildRecord)
                self.assertEqual(j_store2.load(job_id).status, "completed")

                # Download PDF
                pdf_bytes = get_artifact(
                    build_rec.build_id,
                    build_store=b_store2,
                    job_store=j_store2,
                    association_store=a_store2,
                    workflow_store=w_store2,
                    policy_store=p_store2,
                    document_store=d_store2,
                    cache_root=ctx2.cache_root_path(),
                )
                self.assertIsInstance(pdf_bytes, bytes)
                self.assertTrue(pdf_bytes.startswith(b"%PDF"))

            finally:
                for s in (c_store2, j_store2, a_store2, s_store2, w_store2, p_store2, art_store2, d_store2, sem_store2, b_store2):
                    s.close()
                ctx2.close()
