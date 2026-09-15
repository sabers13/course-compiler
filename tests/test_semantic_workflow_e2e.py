"""Coarse workflow E2E via ScriptProvider (T049)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from course_compiler.course_persistence import open_course_store
from course_compiler.course_job_persistence import open_course_job_store
from course_compiler.workflow_persistence import open_workflow_state_store
from course_compiler.course_workflow_persistence import open_course_workflow_association_store
from course_compiler.source_persistence import open_source_evidence_store
from course_compiler.policy_persistence import open_policy_content_store
from course_compiler.workflow_artifact_persistence import open_workflow_artifact_store
from course_compiler.lecture_document_persistence import open_lecture_document_store
from course_compiler.semantic_work_persistence import open_semantic_work_store
from course_compiler.course_operations import create_course, attach_source, get_job_status
from course_compiler.course_job_persistence import CourseJobRecord
from course_compiler.semantic_operations import (
    start_generation,
    request_semantic_work,
    submit_semantic_result,
    submit_owner_priority_decision,
    submit_owner_map_decision,
    SemanticOperationFailure,
)
from course_compiler.semantic_work import ScriptProvider, SemanticWorkRequest
from course_compiler.workflow import (
    WorkflowState,
    WorkflowAdvanced,
    WorkflowIdempotentRepeat,
    priority_subject_sha256,
    map_subject_sha256,
)

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_ISOLATED_BASE = REPOSITORY_ROOT / "local-data" / "test-tmp-sem-e2e"
_ISOLATED_BASE.mkdir(parents=True, exist_ok=True)

_HOLDER = "h-e2e"


def _tmp_dir() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(dir=str(_ISOLATED_BASE), prefix="e2e-")


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


def _drive_step(stores: dict, job_id: str, provider: ScriptProvider, *, clock_index: int) -> object:
    """Drive one coarse step: request + submit/owner decision + holder."""
    req = request_semantic_work(
        job_id,
        job_store=stores["job_store"],
        workflow_store=stores["workflow_store"],
        semantic_store=stores["semantic_store"],
        holder_id=_HOLDER,
        clock=f"2026-01-01T00:{clock_index:02d}:00Z",
    )
    if isinstance(req, SemanticOperationFailure):
        # The semantic assessment has already been accepted; only explicit
        # owner intent can advance this gate.
        job = stores["job_store"].load(job_id)
        assert isinstance(job, CourseJobRecord)
        ws = stores["workflow_store"].load(job.workflow_id)
        assert isinstance(ws, WorkflowState)
        if req.diagnostics[0].code == "owner_decision_required":
            if ws.stage == "priority_approval":
                subj = priority_subject_sha256(ws.priority_basis, ws.policies.priority_basis)
                return submit_owner_priority_decision(
                    job_id, approve=True, subject_sha256=subj,
                    job_store=stores["job_store"], workflow_store=stores["workflow_store"],
                    semantic_store=stores["semantic_store"], association_store=stores["assoc_store"],
                    source_store=stores["source_store"], policy_store=stores["policy_store"],
                    artifact_store=stores["artifact_store"], document_store=stores["document_store"],
                    clock=f"2026-01-01T00:{clock_index:02d}:10Z",
                )
            if ws.stage == "map_approval":
                subj = map_subject_sha256(ws.lecture_map, ws.policies.lecture_mapping)
                return submit_owner_map_decision(
                    job_id, approve=True, subject_sha256=subj,
                    job_store=stores["job_store"], workflow_store=stores["workflow_store"],
                    semantic_store=stores["semantic_store"], association_store=stores["assoc_store"],
                    source_store=stores["source_store"], policy_store=stores["policy_store"],
                    artifact_store=stores["artifact_store"], document_store=stores["document_store"],
                    clock=f"2026-01-01T00:{clock_index:02d}:10Z",
                )
        return req
    res = provider.execute(req)
    sub = submit_semantic_result(
        job_id, res,
        job_store=stores["job_store"], workflow_store=stores["workflow_store"],
        semantic_store=stores["semantic_store"], association_store=stores["assoc_store"],
        source_store=stores["source_store"], policy_store=stores["policy_store"],
        artifact_store=stores["artifact_store"], document_store=stores["document_store"],
        holder_id=_HOLDER, clock=f"2026-01-01T00:{clock_index:02d}:30Z",
    )
    if isinstance(sub, SemanticOperationFailure):
        if sub.diagnostics[0].code == "owner_decision_required":
            ws_cur = stores["workflow_store"].load(job.workflow_id)
            if req.kind == "exam_priority_assessment":
                subj = priority_subject_sha256(ws_cur.priority_basis, ws_cur.policies.priority_basis)
                return submit_owner_priority_decision(
                    job_id, approve=True, subject_sha256=subj,
                    job_store=stores["job_store"], workflow_store=stores["workflow_store"],
                    semantic_store=stores["semantic_store"], association_store=stores["assoc_store"],
                    source_store=stores["source_store"], policy_store=stores["policy_store"],
                    artifact_store=stores["artifact_store"], document_store=stores["document_store"],
                    clock=f"2026-01-01T00:{clock_index:02d}:40Z",
                )
            if req.kind == "lecture_map_generation":
                subj = map_subject_sha256(ws_cur.lecture_map, ws_cur.policies.lecture_mapping)
                return submit_owner_map_decision(
                    job_id, approve=True, subject_sha256=subj,
                    job_store=stores["job_store"], workflow_store=stores["workflow_store"],
                    semantic_store=stores["semantic_store"], association_store=stores["assoc_store"],
                    source_store=stores["source_store"], policy_store=stores["policy_store"],
                    artifact_store=stores["artifact_store"], document_store=stores["document_store"],
                    clock=f"2026-01-01T00:{clock_index:02d}:40Z",
                )
    return sub


class TestCoarseWorkflowE2E(unittest.TestCase):
    def test_create_attach_start_durable(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("E2E Course", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"invented source bytes one", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            attach_source(ref.course_id, b"invented source bytes two", "src-2", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(
                ref.course_id,
                course_store=stores["course_store"], job_store=stores["job_store"],
                workflow_store=stores["workflow_store"], association_store=stores["assoc_store"],
                source_store=stores["source_store"], policy_store=stores["policy_store"],
                artifact_store=stores["artifact_store"], document_store=stores["document_store"],
                clock="2026-01-01T00:00:00Z",
            )
            self.assertIsInstance(job_id, str)
            path = stores["path"]
            for k in ("course_store", "assoc_store", "source_store", "workflow_store", "job_store",
                      "policy_store", "artifact_store", "document_store", "semantic_store"):
                try:
                    stores[k].close()
                except Exception:
                    pass
            course_store2 = open_course_store(path / "courses.sqlite3")
            job_store2 = open_course_job_store(path / "jobs.sqlite3")
            workflow_store2 = open_workflow_state_store(path / "workflow.sqlite3")
            job = job_store2.load(job_id)
            self.assertIsInstance(job, CourseJobRecord)
            ws = workflow_store2.load(job.workflow_id)  # type: ignore[attr-defined]
            self.assertIsInstance(ws, WorkflowState)
            self.assertEqual(ws.revision, 0)
            course_store2.close()
            job_store2.close()
            workflow_store2.close()

    def test_e2e_via_script_provider(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("E2E Script", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"invented source A", "src-a", course_store=stores["course_store"], source_store=stores["source_store"])
            attach_source(ref.course_id, b"invented source B", "src-b", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(
                ref.course_id,
                course_store=stores["course_store"], job_store=stores["job_store"],
                workflow_store=stores["workflow_store"], association_store=stores["assoc_store"],
                source_store=stores["source_store"], policy_store=stores["policy_store"],
                artifact_store=stores["artifact_store"], document_store=stores["document_store"],
                clock="2026-01-01T00:00:00Z",
            )
            provider = ScriptProvider(map_size=3)
            for i in range(16):
                outcome = _drive_step(stores, job_id, provider, clock_index=i)
                if isinstance(outcome, SemanticOperationFailure):
                    ws = stores["workflow_store"].load(stores["job_store"].load(job_id).workflow_id)  # type: ignore[attr-defined]
                    self.assertEqual(ws.stage, "completed")
                    break
                ws = stores["workflow_store"].load(stores["job_store"].load(job_id).workflow_id)  # type: ignore[attr-defined]
                if ws.stage == "completed":
                    break
            job = stores["job_store"].load(job_id)
            self.assertIsInstance(job, CourseJobRecord)
            ws = stores["workflow_store"].load(job.workflow_id)  # type: ignore[attr-defined]
            self.assertIsInstance(ws, WorkflowState)
            self.assertEqual(ws.stage, "completed")
            self.assertEqual(ws.disposition, "completed")
            self.assertEqual(len(ws.lecture_progress), 3)
            for prog in ws.lecture_progress:
                self.assertEqual(prog.status, "accepted")
            proj = get_job_status(job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"])
            self.assertEqual(proj.status, "deterministic_building")
            path = stores["path"]
            for k in ("course_store", "assoc_store", "source_store", "workflow_store", "job_store",
                      "policy_store", "artifact_store", "document_store", "semantic_store"):
                try:
                    stores[k].close()
                except Exception:
                    pass
            ws_store2 = open_workflow_state_store(path / "workflow.sqlite3")
            job_store2 = open_course_job_store(path / "jobs.sqlite3")
            ws2 = ws_store2.load(job.workflow_id)  # type: ignore[attr-defined]
            self.assertEqual(ws2.revision, ws.revision)
            self.assertEqual(ws2.stage, "completed")
            ws_store2.close()
            job_store2.close()

    def test_deterministic_script_provider(self) -> None:
        from course_compiler.semantic_work import SEMANTIC_WORK_REQUEST_VERSION
        import socket

        provider = ScriptProvider(map_size=2)
        req = SemanticWorkRequest(SEMANTIC_WORK_REQUEST_VERSION, "creq1", "cjob1", "w1", 0, "op-1", "lecture_generation", ("l1",), "2026-01-01T00:00:00Z", None)  # type: ignore[arg-type]
        orig_getaddrinfo = socket.getaddrinfo

        def fail(*a, **kw):
            raise AssertionError("network not allowed")

        socket.getaddrinfo = fail  # type: ignore[assignment]
        try:
            res1 = provider.execute(req)
            res2 = provider.execute(req)
            self.assertEqual(res1, res2)
            self.assertNotIn("real", res1.produced_documents[0].source_text.lower() if res1.produced_documents else "")
        finally:
            socket.getaddrinfo = orig_getaddrinfo

    def test_no_private_bytes_in_semantic_store(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("Privacy", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"private bytes should not leak", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(
                ref.course_id,
                course_store=stores["course_store"], job_store=stores["job_store"],
                workflow_store=stores["workflow_store"], association_store=stores["assoc_store"],
                source_store=stores["source_store"], policy_store=stores["policy_store"],
                artifact_store=stores["artifact_store"], document_store=stores["document_store"],
                clock="2026-01-01T00:00:00Z",
            )
            provider = ScriptProvider(map_size=1)
            outcome = _drive_step(stores, job_id, provider, clock_index=0)
            # Drive evidence-only priority: submit stays evidence only
            conn = sqlite3.connect(stores["path"] / "sem.sqlite3")
            rows = conn.execute("SELECT * FROM semantic_work_requests").fetchall()
            conn.close()
            for row in rows:
                row_str = " ".join(str(x) for x in row if x is not None)
                self.assertNotIn("private bytes", row_str)
                self.assertNotIn("Invented content", row_str)
            self.assertNotIn("Invented content", repr(outcome))
            for k in ("course_store", "assoc_store", "source_store", "workflow_store", "job_store",
                      "policy_store", "artifact_store", "document_store", "semantic_store"):
                try:
                    stores[k].close()
                except Exception:
                    pass

    def test_recovery_after_lease_expiry(self) -> None:
        """R2: leased before executor returns; active until expiry; same request reoffered."""
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            ref = create_course("Lease", ai_mode="gpt", quality_mode="fast", course_store=stores["course_store"], association_store=stores["assoc_store"])
            attach_source(ref.course_id, b"src", "src-1", course_store=stores["course_store"], source_store=stores["source_store"])
            job_id = start_generation(
                ref.course_id,
                course_store=stores["course_store"], job_store=stores["job_store"],
                workflow_store=stores["workflow_store"], association_store=stores["assoc_store"],
                source_store=stores["source_store"], policy_store=stores["policy_store"],
                artifact_store=stores["artifact_store"], document_store=stores["document_store"],
                clock="2026-01-01T00:00:00Z",
            )
            req = request_semantic_work(
                job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"],
                semantic_store=stores["semantic_store"], holder_id="h-a", clock="2026-01-01T00:00:00Z",
            )
            self.assertIsInstance(req, SemanticWorkRequest)
            # Lease active: different holder cannot acquire
            other = request_semantic_work(
                job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"],
                semantic_store=stores["semantic_store"], holder_id="h-b", clock="2026-01-01T00:01:00Z",
            )
            self.assertIsInstance(other, SemanticOperationFailure)
            self.assertEqual(other.diagnostics[0].code, "lease_conflict")
            # After expiry, same logical request is offered to next holder
            again = request_semantic_work(
                job_id, job_store=stores["job_store"], workflow_store=stores["workflow_store"],
                semantic_store=stores["semantic_store"], holder_id="h-c", clock="2026-01-01T00:20:00Z",
            )
            self.assertIsInstance(again, SemanticWorkRequest)
            self.assertEqual(again.request_id, req.request_id)
            self.assertEqual(again.expected_revision, req.expected_revision)
            self.assertEqual(again.kind, req.kind)
            self.assertEqual(again.input_refs, req.input_refs)
            for k in ("course_store", "assoc_store", "source_store", "workflow_store", "job_store",
                      "policy_store", "artifact_store", "document_store", "semantic_store"):
                try:
                    stores[k].close()
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main()
