"""Focused tests for T048 coarse operations + projection + HTTP + restart + boundaries."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
import shutil

from course_compiler.app.config import create_config
from course_compiler.app.server import create_server
from course_compiler.course import COURSE_REFERENCE_VERSION, CourseReference
from course_compiler.course_job_persistence import (
    CourseJobRecord,
    derive_job_status,
    open_course_job_store,
)
from course_compiler.course_operations import (
    CourseOperationFailure,
    JobStatusProjection,
    attach_source,
    create_course,
    get_course,
    get_job_status,
    list_courses,
)
from course_compiler.course_persistence import CourseRecord, open_course_store
from course_compiler.course_workflow_persistence import open_course_workflow_association_store
from course_compiler.source_persistence import open_source_evidence_store
from course_compiler.workflow_persistence import LocalWorkflowStateStore, open_workflow_state_store
from tests.test_workflow_persistence import initial_state, next_state

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_ISOLATED_BASE = REPOSITORY_ROOT / "local-data" / "test-tmp-app-course-ops"
_ISOLATED_BASE.mkdir(parents=True, exist_ok=True)


def _tmp_dir() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(dir=str(_ISOLATED_BASE), prefix="ops-")


def _get(port: int, path: str, method: str = "GET", body: bytes | None = None, headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        hdrs = headers or {}
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        h = {k.lower(): v for k, v in resp.getheaders()}
        b = resp.read()
        return resp.status, h, b
    finally:
        conn.close()


def _new_stores(tmp: str):
    p = Path(tmp)
    cfg = create_config(host="127.0.0.1", port=0, data_root=p / "data")
    # Use direct open_course_store via paths under data_root
    course_path = p / "data" / "courses.sqlite3"
    course_path.parent.mkdir(parents=True, exist_ok=True)
    course_store = open_course_store(course_path)
    assoc_path = p / "data" / "course-workflow-associations.sqlite3"
    assoc_store = open_course_workflow_association_store(assoc_path)
    source_path = p / "data" / "source-evidence.sqlite3"
    source_store = open_source_evidence_store(source_path)
    workflow_path = p / "data" / "workflow-state.sqlite3"
    workflow_store = open_workflow_state_store(workflow_path)
    job_path = p / "data" / "course-jobs.sqlite3"
    job_store = open_course_job_store(job_path)
    # Unwrap failures if any? store should be Local* else failure
    return {
        "course_store": course_store,
        "assoc_store": assoc_store,
        "source_store": source_store,
        "workflow_store": workflow_store,
        "job_store": job_store,
        "paths": {"course": course_path, "assoc": assoc_path, "source": source_path, "workflow": workflow_path, "job": job_path},
        "cfg": cfg,
        "data_root": p / "data",
    }


class TestCourseOperations(unittest.TestCase):
    def test_create_course_valid_and_persists_association(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            cs = stores["course_store"]
            assoc = stores["assoc_store"]
            assert not isinstance(cs, CourseOperationFailure)
            assert not isinstance(assoc, CourseOperationFailure)
            from course_compiler.course_persistence import LocalCourseStore

            assert isinstance(cs, LocalCourseStore)
            result = create_course("My Course", ai_mode="gpt", quality_mode="fast", course_store=cs, association_store=assoc)
            self.assertIsInstance(result, CourseReference)
            assert isinstance(result, CourseReference)
            # Verify CourseRecord persisted via get_course
            fetched = get_course(result.course_id, course_store=cs)
            assert isinstance(fetched, CourseRecord)
            self.assertEqual(fetched.title, "My Course")
            self.assertEqual(fetched.ai_mode, "gpt")
            self.assertEqual(fetched.metadata_revision, 0)
            # Association should exist
            from course_compiler.course_workflow import CourseWorkflowAssociation

            course_ref = result
            # Need workflow_id from fetched record
            assoc_expected = CourseWorkflowAssociation(
                "course-workflow-association/v1", course_ref, fetched.workflow_id
            )
            loaded_assoc = assoc.load(assoc_expected)
            self.assertEqual(loaded_assoc, assoc_expected)
            # Verify get_course works
            fetched2 = get_course(result.course_id, course_store=cs)
            self.assertEqual(fetched2, fetched)
            # list_courses deterministic
            listed = list_courses(course_store=cs)
            assert isinstance(listed, tuple)
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0].course_id, result.course_id)
            # Cleanup
            for k in ["course_store", "assoc_store", "source_store", "workflow_store", "job_store"]:
                try:
                    stores[k].close()  # type: ignore[attr-defined]
                except Exception:
                    pass

    def test_create_course_validates_title_and_modes(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            cs = stores["course_store"]
            assoc = stores["assoc_store"]
            for bad_title in ["", " ", "a" * 201, "bad\x00"]:
                res = create_course(bad_title, ai_mode="gpt", quality_mode="fast", course_store=cs, association_store=assoc)
                self.assertIsInstance(res, CourseOperationFailure)
                assert isinstance(res, CourseOperationFailure)
                self.assertEqual(res.diagnostics[0].code, "invalid_course_input")
            for bad_ai in ["invalid", "", "GPT"]:
                res = create_course("Valid Title", ai_mode=bad_ai, quality_mode="fast", course_store=cs, association_store=assoc)
                self.assertIsInstance(res, CourseOperationFailure)
            for bad_qm in ["invalid", ""]:
                res = create_course("Valid", ai_mode="gpt", quality_mode=bad_qm, course_store=cs, association_store=assoc)
                self.assertIsInstance(res, CourseOperationFailure)
            for k in stores:
                if hasattr(stores[k], "close"):
                    try:
                        stores[k].close()
                    except Exception:
                        pass

    def test_create_course_does_not_create_job(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            cs = stores["course_store"]
            assoc = stores["assoc_store"]
            job_store = stores["job_store"]
            result = create_course("No Job Course", ai_mode="gpt", quality_mode="fast", course_store=cs, association_store=assoc)
            assert isinstance(result, CourseReference)
            # Fetch full record to check current_job_id
            fetched = get_course(result.course_id, course_store=cs)
            assert isinstance(fetched, CourseRecord)
            self.assertIsNone(fetched.current_job_id)
            # Job store should be empty
            listed_jobs = job_store.list_jobs_for_course(result.course_id)
            assert isinstance(listed_jobs, tuple)
            self.assertEqual(len(listed_jobs), 0)
            for k in stores:
                if hasattr(stores[k], "close"):
                    try:
                        stores[k].close()
                    except Exception:
                        pass

    def test_attach_source_persists_and_is_idempotent(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            cs = stores["course_store"]
            assoc = stores["assoc_store"]
            ss = stores["source_store"]
            course_ref = create_course("Src Course", ai_mode="gpt", quality_mode="fast", course_store=cs, association_store=assoc)
            assert isinstance(course_ref, CourseReference)
            course_id = course_ref.course_id
            payload = b"invented source bytes for testing"
            ref1 = attach_source(course_id, payload, "src-1", course_store=cs, source_store=ss)
            from course_compiler.workflow import SourceEvidenceReference

            self.assertIsInstance(ref1, SourceEvidenceReference)
            assert isinstance(ref1, SourceEvidenceReference)
            self.assertEqual(ref1.source_id, "src-1")
            self.assertEqual(ref1.content_sha256, hashlib.sha256(payload).hexdigest())
            # Course now has reference
            loaded = get_course(course_id, course_store=cs)
            assert isinstance(loaded, CourseRecord)
            self.assertEqual(len(loaded.source_refs), 1)
            self.assertEqual(loaded.source_refs[0], ref1)
            # Exact repeat idempotent
            ref2 = attach_source(course_id, payload, "src-1", course_store=cs, source_store=ss)
            self.assertEqual(ref2, ref1)
            loaded2 = get_course(course_id, course_store=cs)
            assert isinstance(loaded2, CourseRecord)
            self.assertEqual(loaded2.metadata_revision, 1)  # should not increment on idempotent
            # Conflicting same source_id different bytes fails
            ref_conflict = attach_source(course_id, b"different bytes", "src-1", course_store=cs, source_store=ss)
            self.assertIsInstance(ref_conflict, CourseOperationFailure)
            assert isinstance(ref_conflict, CourseOperationFailure)
            self.assertEqual(ref_conflict.diagnostics[0].code, "source_identity_conflict")
            for k in stores:
                if hasattr(stores[k], "close"):
                    try:
                        stores[k].close()
                    except Exception:
                        pass

    def test_course_records_references_only(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            cs = stores["course_store"]
            assoc = stores["assoc_store"]
            ss = stores["source_store"]
            course_ref = create_course("Ref Only", ai_mode="gpt", quality_mode="fast", course_store=cs, association_store=assoc)
            assert isinstance(course_ref, CourseReference)
            payload = b"tiny bytes"
            ref = attach_source(course_ref.course_id, payload, "src-x", course_store=cs, source_store=ss)
            assert hasattr(ref, "source_id")
            loaded = get_course(course_ref.course_id, course_store=cs)
            assert isinstance(loaded, CourseRecord)
            # Ensure source bytes not duplicated in course store: check via direct sqlite read earlier covered, but here ensure source store still has bytes
            from course_compiler.source_persistence import SourceEvidencePayload

            loaded_src = ss.load(ref)  # type: ignore[arg-type]
            self.assertIsInstance(loaded_src, SourceEvidencePayload)
            for k in stores:
                if hasattr(stores[k], "close"):
                    try:
                        stores[k].close()
                    except Exception:
                        pass

    def test_restart_survival_course_and_source(self) -> None:
        with _tmp_dir() as tmp:
            paths = {}
            # Session A
            stores = _new_stores(tmp)
            cs = stores["course_store"]
            assoc = stores["assoc_store"]
            ss = stores["source_store"]
            course_ref = create_course("Restart Course", ai_mode="byok", quality_mode="review", course_store=cs, association_store=assoc)
            assert isinstance(course_ref, CourseReference)
            # Fetch workflow_id via get_course
            fetched_tmp = get_course(course_ref.course_id, course_store=cs)
            assert isinstance(fetched_tmp, CourseRecord)
            payload = b"restart bytes invented"
            ref = attach_source(course_ref.course_id, payload, "src-restart", course_store=cs, source_store=ss)
            assert hasattr(ref, "source_id")
            course_id = course_ref.course_id
            workflow_id = fetched_tmp.workflow_id
            # Close all
            for k in ["course_store", "assoc_store", "source_store", "workflow_store", "job_store"]:
                try:
                    stores[k].close()
                except Exception:
                    pass
            # Session B: reopen fresh stores
            # Use same tmp path
            course_path = stores["paths"]["course"]
            assoc_path = stores["paths"]["assoc"]
            source_path = stores["paths"]["source"]
            cs2 = open_course_store(course_path)
            ss2 = open_source_evidence_store(source_path)
            assert not isinstance(cs2, CourseOperationFailure)
            assert not isinstance(ss2, CourseOperationFailure)
            # list/get must survive
            listed = list_courses(course_store=cs2)  # type: ignore[arg-type]
            assert isinstance(listed, tuple)
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0].course_id, course_id)
            self.assertEqual(listed[0].workflow_id, workflow_id)
            self.assertEqual(listed[0].title, "Restart Course")
            fetched = get_course(course_id, course_store=cs2)  # type: ignore[arg-type]
            assert isinstance(fetched, CourseRecord)
            self.assertEqual(len(fetched.source_refs), 1)
            self.assertEqual(fetched.source_refs[0].source_id, "src-restart")
            # Source bytes still retrievable
            from course_compiler.source_persistence import SourceEvidencePayload

            loaded_src = ss2.load(fetched.source_refs[0])  # type: ignore[arg-type]
            self.assertIsInstance(loaded_src, SourceEvidencePayload)
            cs2.close()
            ss2.close()

    def test_list_courses_deterministic(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            cs = stores["course_store"]
            assoc = stores["assoc_store"]
            ids = []
            for title in ["C-Beta", "A-Alpha", "C-Alpha"]:
                # Generate deterministic course ids to test ordering; we use custom generator
                # Use manual creation via course_persistence directly to control IDs
                from course_compiler.course_persistence import CourseRecord

                cid = f"c{hashlib.sha256(title.encode()).hexdigest()[:6]}"
                ids.append(cid)
            # Create via operation with random ids, then check list is sorted
            for t in ["Zebra", "Apple", "Middle"]:
                create_course(t, ai_mode="gpt", quality_mode="fast", course_store=cs, association_store=assoc)
            listed = list_courses(course_store=cs)
            assert isinstance(listed, tuple)
            course_ids = [c.course_id for c in listed]
            self.assertEqual(course_ids, sorted(course_ids))
            for k in stores:
                if hasattr(stores[k], "close"):
                    try:
                        stores[k].close()
                    except Exception:
                        pass

    def test_get_course_unknown_safe_404(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            cs = stores["course_store"]
            res = get_course("nonexistent123", course_store=cs)
            self.assertIsInstance(res, CourseOperationFailure)
            assert isinstance(res, CourseOperationFailure)
            self.assertEqual(res.diagnostics[0].code, "course_not_found")
            # No private leakage
            self.assertNotIn("nonexistent123", repr(res).lower() if "nonexistent123" in repr(res).lower() else "")
            # Actually our failure repr is generic, should not contain private id in message; but check not leaking path
            self.assertNotIn(str(stores["paths"]["course"]), repr(res))
            for k in stores:
                if hasattr(stores[k], "close"):
                    try:
                        stores[k].close()
                    except Exception:
                        pass

    def test_job_projection_workflow_wins_and_idempotent(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            cs = stores["course_store"]
            assoc = stores["assoc_store"]
            ws_store = stores["workflow_store"]
            job_store = stores["job_store"]
            # Create a course and associated workflow
            course_ref = create_course("Job Course", ai_mode="gpt", quality_mode="fast", course_store=cs, association_store=assoc)
            assert isinstance(course_ref, CourseReference)
            # Fetch workflow_id via get_course
            course_fetched = get_course(course_ref.course_id, course_store=cs)
            assert isinstance(course_fetched, CourseRecord)
            workflow_id = course_fetched.workflow_id
            # Create a WorkflowState for course's workflow_id
            # Use initial_state but need to align workflow_id
            # Create initial state with workflow_id = workflow_id via initialize_request with that id
            from course_compiler.workflow import InitializeWorkflow, InitializeWorkflowRequest, WorkflowPolicySet, PolicyReference
            from tests.test_workflow_contract import make_policy_set, make_source, digest
            from course_compiler.workflow import SOURCE_EVIDENCE_REFERENCE_VERSION, WORKFLOW_TRANSITION_VERSION
            import hashlib as _hash

            def make_init(workflow_id: str):
                ps = make_policy_set()
                src = make_source(source_id="src-1", marker="src-one")
                return InitializeWorkflowRequest(
                    WORKFLOW_TRANSITION_VERSION, workflow_id, "op-init", InitializeWorkflow("initialize_workflow", ps, (src,))
                )

            from course_compiler.workflow import apply_workflow_request
            from course_compiler.workflow import WorkflowAdvanced

            init_req = make_init(workflow_id)
            result = apply_workflow_request(None, init_req)
            assert isinstance(result, WorkflowAdvanced)
            ws = result.state
            # Persist workflow
            saved = ws_store.save(ws)
            assert not isinstance(saved, CourseOperationFailure)
            # Create Job with stale projection (created)
            job = CourseJobRecord(
                job_id="j001",
                course_reference=CourseReference(COURSE_REFERENCE_VERSION, course_ref.course_id),
                workflow_id=workflow_id,
                created_at="2026-01-02T03:04:05Z",
                created_revision=0,
                current_revision=0,
                metadata_revision=0,
                status="created",  # type: ignore[arg-type]
                current_stage=None,
                current_disposition=None,
                ai_mode="gpt",  # type: ignore[arg-type]
                quality_mode="fast",  # type: ignore[arg-type]
                retry_count=0,
                failure_code=None,
            )
            saved_job = job_store.save(job)
            assert isinstance(saved_job, CourseJobRecord)
            # Now get_job_status should reconcile to sourcing
            proj = get_job_status("j001", job_store=job_store, workflow_store=ws_store)
            assert isinstance(proj, JobStatusProjection)
            self.assertEqual(proj.status, "sourcing")
            self.assertEqual(proj.current_revision, ws.revision)
            # Idempotent second call
            proj2 = get_job_status("j001", job_store=job_store, workflow_store=ws_store)
            self.assertEqual(proj2.status, proj.status)
            self.assertEqual(proj2.metadata_revision, proj.metadata_revision)
            # Advance workflow to awaiting_semantic and ensure WorkflowState wins
            ws2 = next_state(ws, "adv2")
            saved2 = ws_store.save(ws2)
            assert not isinstance(saved2, CourseOperationFailure)
            proj3 = get_job_status("j001", job_store=job_store, workflow_store=ws_store)
            assert isinstance(proj3, JobStatusProjection)
            self.assertEqual(proj3.current_revision, ws2.revision)
            self.assertEqual(proj3.status, "awaiting_semantic")
            # Verify job store now has updated revision
            reloaded = job_store.load("j001")
            assert isinstance(reloaded, CourseJobRecord)
            self.assertEqual(reloaded.current_revision, ws2.revision)
            for k in stores:
                if hasattr(stores[k], "close"):
                    try:
                        stores[k].close()
                    except Exception:
                        pass

    def test_no_semantic_work_leak(self) -> None:
        # Ensure course_operations does not import SemanticWork
        import ast

        src = Path("course_compiler/course_operations.py").read_text(encoding="utf-8")
        self.assertNotIn("SemanticWorkRequest", src)
        self.assertNotIn("SemanticWorkResult", src)
        self.assertNotIn("LocalSemanticWorkStore", src)
        # Ensure course_job_persistence does not contain semantic executor logic
        src2 = Path("course_compiler/course_job_persistence.py").read_text(encoding="utf-8")
        self.assertNotIn("SemanticWorkRequest", src2)
        self.assertNotIn("SemanticWorkResult", src2)
        self.assertNotIn("LocalSemanticWorkStore", src2)


class TestHttpWiring(unittest.TestCase):
    def _start_server(self, tmp: str):
        cfg = create_config(host="127.0.0.1", port=0, data_root=Path(tmp) / "data")
        server = create_server(cfg)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                status, _, _ = _get(port, "/healthz")
                if status == 200:
                    break
            except Exception:
                pass
            time.sleep(0.05)
        return server, port, t

    def test_get_api_courses_persisted_and_create(self) -> None:
        with _tmp_dir() as tmp:
            data_root = Path(tmp) / "data"
            server, port, thr = self._start_server(tmp)
            try:
                # Initially empty or not hardcoded []
                status, _, body = _get(port, "/api/courses")
                self.assertEqual(status, 200)
                payload = json.loads(body.decode())
                self.assertIn("courses", payload)
                initial_count = len(payload["courses"])
                # Create course via POST
                post_body = json.dumps({"title": "HTTP Course", "ai_mode": "gpt", "quality_mode": "fast"}).encode()
                status2, _, body2 = _get(port, "/api/courses", method="POST", body=post_body, headers={"Content-Type": "application/json", "Content-Length": str(len(post_body))})
                self.assertEqual(status2, 201)
                created = json.loads(body2.decode())
                self.assertIn("course", created)
                course_id = created["course"]["course_id"]
                self.assertIsNotNone(course_id)
                # GET list now contains it
                status3, _, body3 = _get(port, "/api/courses")
                payload3 = json.loads(body3.decode())
                self.assertEqual(len(payload3["courses"]), initial_count + 1)
                ids = [c["course_id"] for c in payload3["courses"]]
                self.assertIn(course_id, ids)
                # GET detail
                status4, _, body4 = _get(port, f"/api/courses/{course_id}")
                self.assertEqual(status4, 200)
                detail = json.loads(body4.decode())
                self.assertEqual(detail["course"]["course_id"], course_id)
                self.assertEqual(detail["course"]["title"], "HTTP Course")
                # Unknown course 404
                status5, _, body5 = _get(port, "/api/courses/nonexistent999")
                self.assertEqual(status5, 404)
                self.assertEqual(json.loads(body5.decode()), {"error": "not_found"})
                # Invalid create 400
                bad_body = json.dumps({"title": "", "ai_mode": "gpt", "quality_mode": "fast"}).encode()
                status6, _, body6 = _get(port, "/api/courses", method="POST", body=bad_body, headers={"Content-Type": "application/json", "Content-Length": str(len(bad_body))})
                self.assertEqual(status6, 400)
                # Ensure no private leakage in errors
                text = body6.decode()
                self.assertNotIn("local-data", text)
                self.assertNotIn("Traceback", text)
            finally:
                server.shutdown()
                server.server_close()
                thr.join(timeout=2)

    def test_attach_source_via_http_and_restart(self) -> None:
        with _tmp_dir() as tmp:
            data_root = Path(tmp) / "data"
            server, port, thr = self._start_server(tmp)
            try:
                # Create course
                post_body = json.dumps({"title": "Src HTTP", "ai_mode": "gpt", "quality_mode": "fast"}).encode()
                status, _, body = _get(port, "/api/courses", method="POST", body=post_body, headers={"Content-Type": "application/json", "Content-Length": str(len(post_body))})
                course_id = json.loads(body.decode())["course"]["course_id"]
                # Attach source
                content = b"invented http source bytes"
                b64 = base64.b64encode(content).decode()
                attach_body = json.dumps({"source_id": "src-http-1", "content_base64": b64}).encode()
                status2, _, body2 = _get(port, f"/api/courses/{course_id}/sources", method="POST", body=attach_body, headers={"Content-Type": "application/json", "Content-Length": str(len(attach_body))})
                self.assertEqual(status2, 201)
                ref = json.loads(body2.decode())
                self.assertEqual(ref["source_id"], "src-http-1")
                self.assertEqual(ref["content_sha256"], hashlib.sha256(content).hexdigest())
                # Verify course detail shows source_count
                status3, _, body3 = _get(port, f"/api/courses/{course_id}")
                detail = json.loads(body3.decode())
                self.assertEqual(detail["course"]["source_count"], 1)
            finally:
                server.shutdown()
                server.server_close()
                thr.join(timeout=2)
            # Restart server and verify persistence
            server2, port2, thr2 = self._start_server(tmp)
            try:
                status4, _, body4 = _get(port2, "/api/courses")
                payload = json.loads(body4.decode())
                ids = [c["course_id"] for c in payload["courses"]]
                self.assertIn(course_id, ids)
                status5, _, body5 = _get(port2, f"/api/courses/{course_id}")
                detail2 = json.loads(body5.decode())
                self.assertEqual(detail2["course"]["source_count"], 1)
            finally:
                server2.shutdown()
                server2.server_close()
                thr2.join(timeout=2)

    def test_attach_source_accepts_real_world_pdf_sized_payload(self) -> None:
        # Regression (T051 private golden Product E2E, Phase 1): the JSON
        # request-body reader for POST /api/courses/{course_id}/sources was
        # capped at 8 MiB, well below base64(64 MiB) -- the endpoint's own
        # declared decoded-content cap. A genuine real-world scanned-PDF
        # source in the tens of megabytes (e.g. a lecture-slide deck) was
        # rejected as "invalid_input" before its bytes were ever decoded,
        # even though it was well under the intended content cap. The fix
        # sizes the request-body cap from the same declared content cap.
        with _tmp_dir() as tmp:
            server, port, thr = self._start_server(tmp)
            try:
                post_body = json.dumps({"title": "Large Src", "ai_mode": "gpt", "quality_mode": "fast"}).encode()
                status, _, body = _get(port, "/api/courses", method="POST", body=post_body, headers={"Content-Type": "application/json", "Content-Length": str(len(post_body))})
                course_id = json.loads(body.decode())["course"]["course_id"]
                # 16 MiB of invented content produces an approximately 20 MiB
                # base64/JSON envelope: it exceeds the old 8 MiB request-body
                # cap while remaining well under the 64 MiB decoded-content cap.
                content = b"\x00invented large pdf-sized bytes\x00" * (16 * 1024 * 1024 // 32)
                b64 = base64.b64encode(content).decode()
                self.assertGreater(len(b64), 8 * 1024 * 1024)
                attach_body = json.dumps({"source_id": "src-large-1", "content_base64": b64}).encode()
                status2, _, body2 = _get(
                    port,
                    f"/api/courses/{course_id}/sources",
                    method="POST",
                    body=attach_body,
                    headers={"Content-Type": "application/json", "Content-Length": str(len(attach_body))},
                )
                self.assertEqual(status2, 201)
                ref = json.loads(body2.decode())
                self.assertEqual(ref["source_id"], "src-large-1")
                self.assertEqual(ref["content_sha256"], hashlib.sha256(content).hexdigest())
            finally:
                server.shutdown()
                server.server_close()
                thr.join(timeout=2)

    def test_job_status_http(self) -> None:
        with _tmp_dir() as tmp:
            # Need to seed a job and workflow directly via stores, then query via HTTP
            cfg = create_config(host="127.0.0.1", port=0, data_root=Path(tmp) / "data")
            # Create course via HTTP first to get workflow_id
            server, port, thr = self._start_server(tmp)
            try:
                post_body = json.dumps({"title": "Job HTTP", "ai_mode": "gpt", "quality_mode": "fast"}).encode()
                status, _, body = _get(port, "/api/courses", method="POST", body=post_body, headers={"Content-Type": "application/json", "Content-Length": str(len(post_body))})
                course = json.loads(body.decode())["course"]
                course_id = course["course_id"]
                workflow_id = course["workflow_id"]
                # Directly create workflow and job via stores
                from course_compiler.course_job_persistence import CourseJobRecord
                from course_compiler.course import CourseReference
                from course_compiler.workflow_persistence import open_workflow_state_store
                from course_compiler.course_job_persistence import open_course_job_store
                # Create workflow state
                w_path = Path(tmp) / "data" / "workflow-state.sqlite3"
                j_path = Path(tmp) / "data" / "course-jobs.sqlite3"
                # Ensure stores exist via previous HTTP call, now reopen
                ws_store = open_workflow_state_store(w_path)
                job_store = open_course_job_store(j_path)
                # Create workflow
                from tests.test_workflow_contract import make_policy_set, make_source
                from course_compiler.workflow import InitializeWorkflow, InitializeWorkflowRequest, WORKFLOW_TRANSITION_VERSION, apply_workflow_request, WorkflowAdvanced

                src = make_source()
                init = InitializeWorkflowRequest(WORKFLOW_TRANSITION_VERSION, workflow_id, "op-init-jobhttp", InitializeWorkflow("initialize_workflow", make_policy_set(), (src,)))
                res = apply_workflow_request(None, init)
                assert isinstance(res, WorkflowAdvanced)
                ws = res.state
                ws_store.save(ws)
                # Create job stale
                job = CourseJobRecord(
                    job_id="job-http-1",
                    course_reference=CourseReference(COURSE_REFERENCE_VERSION, course_id),
                    workflow_id=workflow_id,
                    created_at="2026-01-02T03:04:05Z",
                    created_revision=0,
                    current_revision=0,
                    metadata_revision=0,
                    status="created",  # type: ignore[arg-type]
                    current_stage=None,
                    current_disposition=None,
                    ai_mode="gpt",  # type: ignore[arg-type]
                    quality_mode="fast",  # type: ignore[arg-type]
                    retry_count=0,
                    failure_code=None,
                )
                job_store.save(job)
                ws_store.close()
                job_store.close()
                # Now query via HTTP
                status2, _, body2 = _get(port, "/api/jobs/job-http-1")
                self.assertEqual(status2, 200)
                proj = json.loads(body2.decode())
                self.assertEqual(proj["job_id"], "job-http-1")
                self.assertEqual(proj["workflow_id"], workflow_id)
                # Should be reconciled to sourcing
                self.assertEqual(proj["status"], "sourcing")
                self.assertEqual(proj["current_revision"], ws.revision)
            finally:
                server.shutdown()
                server.server_close()
                thr.join(timeout=2)

    def test_no_private_leak_on_api(self) -> None:
        with _tmp_dir() as tmp:
            server, port, thr = self._start_server(tmp)
            try:
                # Try traversal
                status, _, body = _get(port, "/api/courses/../secret")
                self.assertEqual(status, 404)
                self.assertNotIn(b"local-data", body)
                self.assertNotIn(b"Traceback", body)
                # Unknown api
                status2, _, body2 = _get(port, "/api/unknown")
                self.assertEqual(status2, 404)
                self.assertNotIn(b"Traceback", body2)
            finally:
                server.shutdown()
                server.server_close()
                thr.join(timeout=2)


class TestAudits(unittest.TestCase):
    def test_create_course_returns_course_reference(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            cs = stores["course_store"]
            assoc = stores["assoc_store"]
            job_store = stores["job_store"]
            ref = create_course("Audit Ref", ai_mode="gpt", quality_mode="fast", course_store=cs, association_store=assoc)
            self.assertIsInstance(ref, CourseReference)
            # CourseRecord durably persisted
            rec = get_course(ref.course_id, course_store=cs)
            assert isinstance(rec, CourseRecord)
            self.assertEqual(rec.course_id, ref.course_id)
            self.assertEqual(rec.reference_version, ref.reference_version)
            # Association durably persisted
            from course_compiler.course_workflow import CourseWorkflowAssociation
            assoc_expected = CourseWorkflowAssociation("course-workflow-association/v1", ref, rec.workflow_id)
            loaded = assoc.load(assoc_expected)
            self.assertEqual(loaded, assoc_expected)
            # No Job created
            jobs = job_store.list_jobs_for_course(ref.course_id)
            assert isinstance(jobs, tuple)
            self.assertEqual(len(jobs), 0)
            for k in stores:
                if hasattr(stores[k], "close"):
                    try:
                        stores[k].close()
                    except Exception:
                        pass

    def test_duplicate_digest_rejected(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            cs = stores["course_store"]
            assoc = stores["assoc_store"]
            ss = stores["source_store"]
            ref = create_course("Dup Digest", ai_mode="gpt", quality_mode="fast", course_store=cs, association_store=assoc)
            assert isinstance(ref, CourseReference)
            payload = b"same bytes"
            r1 = attach_source(ref.course_id, payload, "src-a", course_store=cs, source_store=ss)
            assert hasattr(r1, "source_id")
            # Same bytes different source_id should be rejected as source_identity_conflict (workflow invariant)
            r2 = attach_source(ref.course_id, payload, "src-b", course_store=cs, source_store=ss)
            self.assertIsInstance(r2, CourseOperationFailure)
            assert isinstance(r2, CourseOperationFailure)
            self.assertEqual(r2.diagnostics[0].code, "source_identity_conflict")
            # Verify source blob orphan remains but course not advanced
            course = get_course(ref.course_id, course_store=cs)
            assert isinstance(course, CourseRecord)
            self.assertEqual(len(course.source_refs), 1)
            self.assertEqual(course.source_refs[0].source_id, "src-a")
            for k in stores:
                if hasattr(stores[k], "close"):
                    try:
                        stores[k].close()
                    except Exception:
                        pass

    def test_missing_workflow_state_created_vs_corrupted(self) -> None:
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            cs = stores["course_store"]
            assoc = stores["assoc_store"]
            ws_store = stores["workflow_store"]
            job_store = stores["job_store"]
            ref = create_course("Missing WS", ai_mode="gpt", quality_mode="fast", course_store=cs, association_store=assoc)
            assert isinstance(ref, CourseReference)
            fetched = get_course(ref.course_id, course_store=cs)
            assert isinstance(fetched, CourseRecord)
            workflow_id = fetched.workflow_id
            # Create job in created state (legitimate missing workflow)
            job_created = CourseJobRecord(
                job_id="j-missing-1",
                course_reference=ref,
                workflow_id=workflow_id,
                created_at="2026-01-02T03:04:05Z",
                created_revision=0,
                current_revision=0,
                metadata_revision=0,
                status="created",
                current_stage=None,
                current_disposition=None,
                ai_mode="gpt",
                quality_mode="fast",
                retry_count=0,
                failure_code=None,
            )
            job_store.save(job_created)
            # Missing workflow should be treated as created (legitimate crash window)
            proj = get_job_status("j-missing-1", job_store=job_store, workflow_store=ws_store)
            assert isinstance(proj, JobStatusProjection)
            self.assertEqual(proj.status, "created")
            # Now advance job to sourcing via workflow, then delete workflow to simulate corruption
            from tests.test_workflow_contract import make_policy_set, make_source
            from course_compiler.workflow import InitializeWorkflow, InitializeWorkflowRequest, WORKFLOW_TRANSITION_VERSION, apply_workflow_request, WorkflowAdvanced
            src = make_source(source_id="src-1", marker="x")
            init = InitializeWorkflowRequest(WORKFLOW_TRANSITION_VERSION, workflow_id, "op-missing-test", InitializeWorkflow("initialize_workflow", make_policy_set(), (src,)))
            res = apply_workflow_request(None, init)
            assert isinstance(res, WorkflowAdvanced)
            ws = res.state
            ws_store.save(ws)
            # Reconcile job to sourcing
            reconciled = job_store.reconcile_from_workflow("j-missing-1", ws)
            assert isinstance(reconciled, CourseJobRecord)
            self.assertEqual(reconciled.status, "sourcing")
            # Now delete workflow row to simulate missing after advanced (corrupted)
            import sqlite3
            conn = sqlite3.connect(stores["paths"]["workflow"])
            conn.execute("DELETE FROM workflow_state_snapshots WHERE workflow_id=?", (workflow_id,))
            conn.commit()
            conn.close()
            # get_job_status should now fail closed as workflow_store_failed, not silently revert to created
            proj2 = get_job_status("j-missing-1", job_store=job_store, workflow_store=ws_store)
            self.assertIsInstance(proj2, CourseOperationFailure)
            assert isinstance(proj2, CourseOperationFailure)
            self.assertEqual(proj2.diagnostics[0].code, "workflow_store_failed")
            for k in stores:
                if hasattr(stores[k], "close"):
                    try:
                        stores[k].close()
                    except Exception:
                        pass

    def test_active_job_invariant_preserved(self) -> None:
        # Ensure Course 1 — * Job history with at most one active semantics can be established without schema redesign
        with _tmp_dir() as tmp:
            stores = _new_stores(tmp)
            cs = stores["course_store"]
            assoc = stores["assoc_store"]
            job_store = stores["job_store"]
            ref = create_course("Active Inv", ai_mode="gpt", quality_mode="fast", course_store=cs, association_store=assoc)
            assert isinstance(ref, CourseReference)
            # Create multiple jobs for same course (history)
            j1 = CourseJobRecord(
                job_id="j-active-1",
                course_reference=ref,
                workflow_id="w-active-1",
                created_at="2026-01-02T03:04:05Z",
                created_revision=0,
                current_revision=0,
                metadata_revision=0,
                status="created",
                current_stage=None,
                current_disposition=None,
                ai_mode="gpt",
                quality_mode="fast",
                retry_count=0,
                failure_code=None,
            )
            j2 = CourseJobRecord(
                job_id="j-active-2",
                course_reference=ref,
                workflow_id="w-active-2",
                created_at="2026-01-02T03:04:06Z",
                created_revision=0,
                current_revision=0,
                metadata_revision=0,
                status="created",
                current_stage=None,
                current_disposition=None,
                ai_mode="gpt",
                quality_mode="fast",
                retry_count=0,
                failure_code=None,
            )
            self.assertEqual(job_store.save(j1), j1)
            self.assertEqual(job_store.save(j2), j2)
            listed = job_store.list_jobs_for_course(ref.course_id)
            assert isinstance(listed, tuple)
            self.assertEqual(len(listed), 2)
            # current_job_id remains None (T048 does not auto-create Job); future T049 can set it via conditional CourseRecord update
            rec = get_course(ref.course_id, course_store=cs)
            assert isinstance(rec, CourseRecord)
            self.assertIsNone(rec.current_job_id)
            # Simulate T049 setting active job pointer via metadata_revision conditional update
            updated = CourseRecord(
                rec.reference_version,
                rec.course_id,
                rec.created_at,
                rec.title,
                rec.ai_mode,
                rec.quality_mode,
                rec.owner_scope,
                rec.metadata_revision + 1,
                rec.source_refs,
                rec.workflow_id,
                "j-active-1",
            )
            self.assertEqual(cs.save(updated), updated)
            # No schema redesign needed: workflow_id UNIQUE and course_id grouping supports history
            for k in stores:
                if hasattr(stores[k], "close"):
                    try:
                        stores[k].close()
                    except Exception:
                        pass


if __name__ == "__main__":
    unittest.main()
