"""Runtime API projection coverage for the T053 workspace review surfaces."""

from __future__ import annotations

import base64
from dataclasses import replace
import http.client
import json
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from course_compiler.app.config import create_config
from course_compiler.app.server import create_server
from course_compiler.build_persistence import open_build_record_store
from course_compiler.semantic_work import SemanticDiagnostic, ScriptProvider
from course_compiler.semantic_work_persistence import open_semantic_work_store
from course_compiler.workflow_persistence import open_workflow_state_store


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "local-data" / "test-tmp-workspace"
BASE.mkdir(parents=True, exist_ok=True)


def _pdf() -> bytes:
    return b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


class WorkspaceReviewContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=BASE, prefix="synthetic-")
        self.data_root = Path(self.tmp.name)
        self.server = create_server(create_config(host="127.0.0.1", port=0, data_root=self.data_root))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def _call(self, method: str, path: str, body: dict | None = None, expected: int = 200) -> dict:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=60)
        try:
            raw = json.dumps(body).encode() if body is not None else None
            conn.request(method, path, raw, {"content-type": "application/json"} if raw else {})
            response = conn.getresponse()
            payload = response.read()
        finally:
            conn.close()
        self.assertEqual(response.status, expected, payload)
        return json.loads(payload)

    def _head(self, method: str, path: str) -> int:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=60)
        try:
            conn.request(method, path)
            response = conn.getresponse()
            response.read()
        finally:
            conn.close()
        return response.status

    def _asset(self, path: str) -> str:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=60)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            payload = response.read()
        finally:
            conn.close()
        self.assertEqual(response.status, 200)
        return payload.decode("utf-8")

    def _new_job(self, quality: str) -> tuple[str, str]:
        course = self._call("POST", "/api/courses", {"title": "Invented workspace course", "ai_mode": "gpt", "quality_mode": quality}, 201)["course"]
        course_id = course["course_id"]
        self._call("POST", f"/api/courses/{course_id}/sources", {
            "source_id": "src-1", "content_base64": base64.b64encode(_pdf()).decode("ascii"),
        }, 201)
        job = self._call("POST", f"/api/courses/{course_id}/start-generation", expected=201)["job_id"]
        return course_id, job

    def _drive_to_completed(self, job_id: str) -> None:
        self._call("POST", f"/api/jobs/{job_id}/run-controlled-generation", {"map_size": 1})

    def test_runtime_job_projection_keeps_review_and_build_checks_independent(self) -> None:
        _, fast_job = self._new_job("fast")
        fast = self._call("GET", f"/api/jobs/{fast_job}")
        self.assertEqual(fast["review"], {"status": "not_applicable", "outstanding_lectures": []})
        self.assertEqual(fast["build_checks"], {"status": "pending", "diagnostics": []})
        self.assertNotEqual(fast["review"], fast["build_checks"])

        # The controlled synthetic lifecycle writes an actual BuildRecord;
        # the projection must then come from that record, not from job status.
        self._call("POST", f"/api/jobs/{fast_job}/run-controlled-generation", {"map_size": 1})
        built = self._call("POST", f"/api/jobs/{fast_job}/build")
        after_build = self._call("GET", f"/api/jobs/{fast_job}")
        self.assertEqual(after_build["completed_build_id"], built["build_id"])
        self.assertEqual(after_build["build_checks"]["status"], "succeeded")
        self.assertEqual(after_build["build_checks"]["diagnostics"], [])

        _, review_job = self._new_job("review")
        review = self._call("GET", f"/api/jobs/{review_job}")
        self.assertEqual(review["review"]["status"], "workflow_incomplete")
        self.assertEqual(review["build_checks"], {"status": "pending", "diagnostics": []})
        # A non-terminal job is not projected as a successful document check
        # merely because its job status changes independently of BuildRecord.
        self.assertNotEqual(review["build_checks"]["status"], "succeeded")

    def test_frontend_has_distinct_review_and_document_check_surfaces(self) -> None:
        # Runtime API is the authority above. This narrow DOM-template smoke
        # guard ensures the two values remain rendered as distinct labels.
        page = self._asset("/static/app.js")
        self.assertIn("Semantic Content Review", page)
        self.assertIn("Document Checks", page)
        self.assertIn("Not applicable (FAST)", page)
        self.assertIn("re-review is required before build", page)

    def test_frontend_has_truthful_build_failure_and_history_surfaces(self) -> None:
        # T054-MVP: the build path must explain backend authority in plain
        # language and surface the existing build-history route.
        page = self._asset("/static/app.js")
        self.assertIn("semantic_review_pending", page)
        self.assertIn("review_corrections_pending", page)
        self.assertIn("Build history", page)
        self.assertIn("/builds", page)
        self.assertIn("Needs attention", page)
        html = self._asset("/")
        self.assertIn('id="refresh-btn"', html)
        css = self._asset("/static/app.css")
        self.assertIn(".notice", css)

    def test_frontend_build_state_fails_closed_under_review_authority(self) -> None:
        """T054-R01: the shipped buildState decision must consult
        content_review BEFORE treating deterministic_building as
        build-ready, or the UI enables Build PDF while authoritative
        POST /build must reject with 409.

        This executes the readiness decision itself: the served app.js
        source for BUILD_GATE_EXPLANATIONS + buildState is evaluated
        under node against the A-D decision matrix (plus fail-closed
        probes), and ready/reason is asserted per case.
        """
        page = self._asset("/static/app.js")
        # Structural guard: the old unconditional ready return must be gone.
        self.assertNotIn(
            'if (c.job_status === "deterministic_building") return { ready: true, reason: "" };',
            page,
        )
        explanations = re.search(
            r"const BUILD_GATE_EXPLANATIONS = (\{[^}]*\});", page, re.DOTALL
        )
        self.assertIsNotNone(explanations)
        start = page.index("function buildState(c) {")
        depth = 0
        end: int | None = None
        for i in range(start, len(page)):
            if page[i] == "{":
                depth += 1
            elif page[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        self.assertIsNotNone(end)
        assert end is not None
        build_state_src = page[start:end]
        node = shutil.which("node")
        self.assertIsNotNone(node, "node is required to execute the shipped buildState decision")
        assert node is not None
        cases = [
            # A. FAST may build while deterministic_building.
            {"active_job_id": "job-a", "job_status": "deterministic_building",
             "content_review": {"status": "not_applicable", "outstanding_lectures": []}},
            # B. REVIEW awaiting review must NOT build, with the pending reason.
            {"active_job_id": "job-b", "job_status": "deterministic_building",
             "content_review": {"status": "review_pending", "outstanding_lectures": []}},
            # C1. REVIEW correction obligations pending must NOT build.
            {"active_job_id": "job-c1", "job_status": "deterministic_building",
             "content_review": {"status": "correction_reopen_pending", "outstanding_lectures": ["l1"]}},
            # C2. Outstanding lectures alone must NOT build even when the
            # status would otherwise permit it.
            {"active_job_id": "job-c2", "job_status": "deterministic_building",
             "content_review": {"status": "semantic_final", "outstanding_lectures": ["l1"]}},
            # D. REVIEW semantic_final with no obligations may build.
            {"active_job_id": "job-d", "job_status": "deterministic_building",
             "content_review": {"status": "semantic_final", "outstanding_lectures": []}},
            # Fail closed: missing review authority must NOT build.
            {"active_job_id": "job-e", "job_status": "deterministic_building"},
            # Fail closed: unknown review status must NOT build.
            {"active_job_id": "job-f", "job_status": "deterministic_building",
             "content_review": {"status": "future_unknown", "outstanding_lectures": []}},
        ]
        expected = [
            (True, ""),
            (False, "semantic review is pending"),
            (False, "corrections are pending"),
            (False, "corrections are pending"),
            (True, ""),
            (False, "generation and review complete"),
            (False, "generation and review complete"),
        ]
        driver = (
            "const BUILD_GATE_EXPLANATIONS = " + explanations.group(1) + ";\n"
            + build_state_src + "\n"
            + "const fs = require(\"fs\");\n"
            + "const cases = JSON.parse(fs.readFileSync(process.argv[2], \"utf8\"));\n"
            + "const out = cases.map(function (c) {\n"
            + "  const r = buildState(c);\n"
            + "  if (r === null) return { ready: null, reason: null };\n"
            + "  return { ready: !!r.ready, reason: String(r.reason) };\n"
            + "});\n"
            + "process.stdout.write(JSON.stringify(out));\n"
        )
        with tempfile.TemporaryDirectory(dir=str(self.data_root), prefix="buildstate-") as harness:
            cases_path = Path(harness) / "cases.json"
            driver_path = Path(harness) / "driver.js"
            cases_path.write_text(json.dumps(cases), encoding="utf-8")
            driver_path.write_text(driver, encoding="utf-8")
            proc = subprocess.run(
                [node, str(driver_path), str(cases_path)],
                capture_output=True, text=True, timeout=60, cwd=harness,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        results = json.loads(proc.stdout)
        self.assertEqual(len(results), len(expected))
        for idx, ((want_ready, want_reason), got) in enumerate(zip(expected, results)):
            with self.subTest(case=cases[idx]["active_job_id"]):
                self.assertEqual(got["ready"], want_ready)
                if want_ready:
                    self.assertEqual(got["reason"], want_reason)
                else:
                    self.assertIn(want_reason, got["reason"])

    # ----- Block C: HTTP build-gate acceptance proof -----

    def _build_record_count(self, job_id: str) -> int:
        store = open_build_record_store(self.data_root / "build-records.sqlite3")
        try:
            records = store.list_for_job(job_id)
            self.assertNotIsInstance(records, type(store))  # any failure sentinel
            return len(records)
        finally:
            store.close()

    def test_review_build_refused_as_semantic_review_pending_before_any_verdict(self) -> None:
        """Gate 1: At completed REVIEW state before any review verdict, POST
        build must yield HTTP 409 semantic_review_pending and create no
        BuildRecord.
        """
        _, job_id = self._new_job("review")
        # Drive generation to completed without ever issuing semantic_review.
        self._drive_to_completed(job_id)

        refused = self._call("POST", f"/api/jobs/{job_id}/build", expected=409)
        self.assertEqual(refused["error"], "semantic_review_pending")
        self.assertEqual(self._build_record_count(job_id), 0)

    def test_review_build_refused_as_review_corrections_pending_in_crash_window(self) -> None:
        """Gate 2: In the accepted-corrections crash window before reopen,
        POST build must yield HTTP 409 review_corrections_pending and
        create no BuildRecord.
        """
        _, job_id = self._new_job("review")
        self._drive_to_completed(job_id)

        # Mint a semantic_review request and mark its verdict accepted with
        # a corrections diagnostic, deliberately bypassing the normal submit
        # path that would advance the workflow via the reopen transition.
        acquired = self._call("POST", f"/api/jobs/{job_id}/semantic-request",
                              {"holder_id": "reviewholder01"}, 200)
        review_request = acquired["request"]
        from course_compiler.semantic_work import SemanticWorkResult, SEMANTIC_WORK_RESULT_VERSION
        synthesized = SemanticWorkResult(
            SEMANTIC_WORK_RESULT_VERSION,
            review_request["request_id"],
            review_request["operation_id"],
            "semantic_review",
            (), (),
            (SemanticDiagnostic("review_correction_required_l1", "Semantic review requires correction."),),
            None, None, None,
            review_request["expected_revision"],
        )
        sem_store = open_semantic_work_store(self.data_root / "semantic-work.sqlite3")
        try:
            accepted = sem_store.mark_accepted(synthesized)
            self.assertNotIsInstance(accepted, type(sem_store))  # failure sentinel
        finally:
            sem_store.close()

        # The crash window: workflow still completed/completed, no reopen
        # transition yet. POST build must be refused as
        # review_corrections_pending and produce no BuildRecord.
        refused = self._call("POST", f"/api/jobs/{job_id}/build", expected=409)
        self.assertEqual(refused["error"], "review_corrections_pending")
        self.assertEqual(self._build_record_count(job_id), 0)

    def test_review_build_refused_after_correction_recompletes_pre_new_verdict(self) -> None:
        """Gate 3: After correction/revalidation recompletes the workflow
        but before the NEW review verdict, POST build must yield HTTP 409
        semantic_review_pending and create no BuildRecord.
        """
        _, job_id = self._new_job("review")
        self._drive_to_completed(job_id)

        # Submit a corrections verdict via the real HTTP submit path so the
        # reopen transition lands authoritatively.
        acquired = self._call("POST", f"/api/jobs/{job_id}/semantic-request",
                              {"holder_id": "reviewholder02"}, 200)
        review_request = acquired["request"]
        from course_compiler.semantic_work import SemanticWorkResult, SEMANTIC_WORK_RESULT_VERSION
        synthesized = SemanticWorkResult(
            SEMANTIC_WORK_RESULT_VERSION,
            review_request["request_id"],
            review_request["operation_id"],
            "semantic_review",
            (), (),
            (SemanticDiagnostic("review_correction_required_l1", "Semantic review requires correction."),),
            None, None, None,
            review_request["expected_revision"],
        )
        self._call("POST", f"/api/jobs/{job_id}/semantic-result", {
            "result_version": synthesized.result_version,
            "request_id": synthesized.request_id,
            "operation_id": synthesized.operation_id,
            "kind": synthesized.kind,
            "produced_artifacts": [], "produced_documents": [], "diagnostics": [
                {"code": d.code, "message": d.message} for d in synthesized.diagnostics
            ],
            "priority_subject_sha256": None,
            "map_subject_sha256": None,
            "candidate_subject_sha256": None,
            "request_revision": synthesized.request_revision,
            "holder_id": "reviewholder02",
        }, 200)

        # Drive the semantic_correction cycle: POST semantic-request to
        # acquire the correction request, then submit via the HTTP
        # semantic-result route.
        correction_req = self._call("POST", f"/api/jobs/{job_id}/semantic-request",
                                    {"holder_id": "reviewholder02"}, 200)["request"]
        provider = ScriptProvider(map_size=1)
        correction_result = provider.execute(_make_request(correction_req))
        self._call("POST", f"/api/jobs/{job_id}/semantic-result", {
            "result_version": correction_result.result_version,
            "request_id": correction_result.request_id,
            "operation_id": correction_result.operation_id,
            "kind": correction_result.kind,
            "produced_artifacts": [],
            "produced_documents": [
                {"lecture_id": d.lecture_id, "source_text": d.source_text}
                for d in correction_result.produced_documents
            ],
            "diagnostics": [
                {"code": d.code, "message": d.message} for d in correction_result.diagnostics
            ],
            "priority_subject_sha256": None,
            "map_subject_sha256": None,
            "candidate_subject_sha256": correction_result.candidate_subject_sha256,
            "request_revision": correction_result.request_revision,
            "holder_id": "reviewholder02",
        }, 200)

        # Workflow has recompleted but the NEW semantic_review has not been
        # issued/accepted yet.
        refused = self._call("POST", f"/api/jobs/{job_id}/build", expected=409)
        self.assertEqual(refused["error"], "semantic_review_pending")
        self.assertEqual(self._build_record_count(job_id), 0)

    def test_review_build_succeeds_after_new_no_corrections_verdict(self) -> None:
        """Gate 4: After the new no-corrections verdict, the same REVIEW
        course can build successfully.
        """
        _, job_id = self._new_job("review")
        self._drive_to_completed(job_id)

        # Submit corrections verdict and drive one correction cycle as in
        # gate 3, ending with the workflow recompleted.
        acquired = self._call("POST", f"/api/jobs/{job_id}/semantic-request",
                              {"holder_id": "reviewholder03"}, 200)
        review_request = acquired["request"]
        from course_compiler.semantic_work import SemanticWorkResult, SEMANTIC_WORK_RESULT_VERSION
        first_verdict = SemanticWorkResult(
            SEMANTIC_WORK_RESULT_VERSION,
            review_request["request_id"],
            review_request["operation_id"],
            "semantic_review",
            (), (),
            (SemanticDiagnostic("review_correction_required_l1", "Semantic review requires correction."),),
            None, None, None,
            review_request["expected_revision"],
        )
        self._call("POST", f"/api/jobs/{job_id}/semantic-result", {
            "result_version": first_verdict.result_version,
            "request_id": first_verdict.request_id,
            "operation_id": first_verdict.operation_id,
            "kind": first_verdict.kind,
            "produced_artifacts": [], "produced_documents": [], "diagnostics": [
                {"code": d.code, "message": d.message} for d in first_verdict.diagnostics
            ],
            "priority_subject_sha256": None,
            "map_subject_sha256": None,
            "candidate_subject_sha256": None,
            "request_revision": first_verdict.request_revision,
            "holder_id": "reviewholder03",
        }, 200)

        correction_req = self._call("POST", f"/api/jobs/{job_id}/semantic-request",
                                    {"holder_id": "reviewholder03"}, 200)["request"]
        provider = ScriptProvider(map_size=1)
        correction_result = provider.execute(_make_request(correction_req))
        self._call("POST", f"/api/jobs/{job_id}/semantic-result", {
            "result_version": correction_result.result_version,
            "request_id": correction_result.request_id,
            "operation_id": correction_result.operation_id,
            "kind": correction_result.kind,
            "produced_artifacts": [],
            "produced_documents": [
                {"lecture_id": d.lecture_id, "source_text": d.source_text}
                for d in correction_result.produced_documents
            ],
            "diagnostics": [
                {"code": d.code, "message": d.message} for d in correction_result.diagnostics
            ],
            "priority_subject_sha256": None,
            "map_subject_sha256": None,
            "candidate_subject_sha256": correction_result.candidate_subject_sha256,
            "request_revision": correction_result.request_revision,
            "holder_id": "reviewholder03",
        }, 200)

        # Now issue and accept a NEW semantic_review with no corrections.
        second_acquired = self._call("POST", f"/api/jobs/{job_id}/semantic-request",
                                     {"holder_id": "reviewholder03"}, 200)
        second_review = second_acquired["request"]
        self.assertNotEqual(second_review["request_id"], review_request["request_id"])
        second_verdict = SemanticWorkResult(
            SEMANTIC_WORK_RESULT_VERSION,
            second_review["request_id"],
            second_review["operation_id"],
            "semantic_review",
            (), (), (), None, None, None, second_review["expected_revision"],
        )
        self._call("POST", f"/api/jobs/{job_id}/semantic-result", {
            "result_version": second_verdict.result_version,
            "request_id": second_verdict.request_id,
            "operation_id": second_verdict.operation_id,
            "kind": second_verdict.kind,
            "produced_artifacts": [], "produced_documents": [], "diagnostics": [],
            "priority_subject_sha256": None,
            "map_subject_sha256": None,
            "candidate_subject_sha256": None,
            "request_revision": second_verdict.request_revision,
            "holder_id": "reviewholder03",
        }, 200)

        # Build now succeeds.
        built = self._call("POST", f"/api/jobs/{job_id}/build", expected=200)
        self.assertEqual(built["status"], "succeeded")
        self.assertTrue(built["build_id"].startswith("bld-"))
        self.assertEqual(self._build_record_count(job_id), 1)

    # ----- Block D: GET/HEAD pending_work observation-only -----

    def test_get_head_pending_work_observation_does_not_mutate_durable_state(self) -> None:
        """Repeated GET/HEAD pending_work observation alone must not change
        WorkflowState revision, operation receipts, semantic rows, or Job
        durable state. Tested in the crash-window state where a
        correction-reopen recovery is pending: observation must surface
        the deterministic-action-pending window without mutating it.
        """
        _, job_id = self._new_job("review")
        self._drive_to_completed(job_id)

        # Build the deterministic crash window: accepted corrections
        # verdict durable, no reopen transition persisted.
        acquired = self._call("POST", f"/api/jobs/{job_id}/semantic-request",
                              {"holder_id": "observerholder01"}, 200)
        review_request = acquired["request"]
        from course_compiler.semantic_work import SemanticWorkResult, SEMANTIC_WORK_RESULT_VERSION
        synthesized = SemanticWorkResult(
            SEMANTIC_WORK_RESULT_VERSION,
            review_request["request_id"],
            review_request["operation_id"],
            "semantic_review",
            (), (),
            (SemanticDiagnostic("review_correction_required_l1", "Semantic review requires correction."),),
            None, None, None,
            review_request["expected_revision"],
        )
        sem_store = open_semantic_work_store(self.data_root / "semantic-work.sqlite3")
        try:
            accepted = sem_store.mark_accepted(synthesized)
            self.assertNotIsInstance(accepted, type(sem_store))
        finally:
            sem_store.close()

        # Capture durable state before observation traffic.
        ws_store = open_workflow_state_store(self.data_root / "workflow-state.sqlite3")
        sem_store = open_semantic_work_store(self.data_root / "semantic-work.sqlite3")
        build_store = open_build_record_store(self.data_root / "build-records.sqlite3")
        from course_compiler.course_job_persistence import open_course_job_store
        job_store = open_course_job_store(self.data_root / "course-jobs.sqlite3")
        try:
            job_before = job_store.load(job_id)
            from course_compiler.course_job_persistence import CourseJobRecord
            from course_compiler.workflow import WorkflowState
            self.assertIsInstance(job_before, CourseJobRecord)
            ws_before = ws_store.load(job_before.workflow_id)
            self.assertIsInstance(ws_before, WorkflowState)
            revisions_before = ws_before.revision
            receipts_before = tuple(ws_before.operation_receipts)
            sem_before = tuple(sem_store.load_all_for_job(job_id))
            build_before = tuple(build_store.list_for_job(job_id))
            job_metadata_before = job_before.metadata_revision
            course_id = job_before.course_reference.course_id
        finally:
            ws_store.close(); sem_store.close(); build_store.close(); job_store.close()

        # Repeated observation traffic must NOT mutate the durable state.
        for _ in range(5):
            self.assertEqual(
                self._call("GET", f"/api/courses/{course_id}/jobs/{job_id}/pending_work",
                            expected=200)["status"],
                "no_semantic_work",
            )
            self.assertEqual(self._head("HEAD", f"/api/courses/{course_id}/jobs/{job_id}/pending_work"), 200)

        # Reopen stores and confirm nothing changed.
        ws_store = open_workflow_state_store(self.data_root / "workflow-state.sqlite3")
        sem_store = open_semantic_work_store(self.data_root / "semantic-work.sqlite3")
        build_store = open_build_record_store(self.data_root / "build-records.sqlite3")
        job_store = open_course_job_store(self.data_root / "course-jobs.sqlite3")
        try:
            job_after = job_store.load(job_id)
            from course_compiler.course_job_persistence import CourseJobRecord
            from course_compiler.workflow import WorkflowState
            self.assertIsInstance(job_after, CourseJobRecord)
            ws_after = ws_store.load(job_after.workflow_id)
            self.assertIsInstance(ws_after, WorkflowState)
            sem_after = tuple(sem_store.load_all_for_job(job_id))
            build_after = tuple(build_store.list_for_job(job_id))

            self.assertEqual(ws_after.revision, revisions_before)
            self.assertEqual(tuple(ws_after.operation_receipts), receipts_before)
            self.assertEqual(sem_after, sem_before)
            self.assertEqual(build_after, build_before)
            self.assertEqual(job_after.metadata_revision, job_metadata_before)
        finally:
            ws_store.close(); sem_store.close(); build_store.close(); job_store.close()

    def test_post_semantic_request_performs_deterministic_recovery_once(self) -> None:
        """The mutation-authorized POST semantic-request path performs the
        exact deterministic recovery once. Repeated POSTs do not duplicate
        the reopen and do not mint another semantic_review.
        """
        _, job_id = self._new_job("review")
        self._drive_to_completed(job_id)

        # Build the deterministic crash window.
        acquired = self._call("POST", f"/api/jobs/{job_id}/semantic-request",
                              {"holder_id": "recoveryholder01"}, 200)
        review_request = acquired["request"]
        from course_compiler.semantic_work import SemanticWorkResult, SEMANTIC_WORK_RESULT_VERSION
        synthesized = SemanticWorkResult(
            SEMANTIC_WORK_RESULT_VERSION,
            review_request["request_id"],
            review_request["operation_id"],
            "semantic_review",
            (), (),
            (SemanticDiagnostic("review_correction_required_l1", "Semantic review requires correction."),),
            None, None, None,
            review_request["expected_revision"],
        )
        sem_store = open_semantic_work_store(self.data_root / "semantic-work.sqlite3")
        try:
            accepted = sem_store.mark_accepted(synthesized)
            self.assertNotIsInstance(accepted, type(sem_store))
        finally:
            sem_store.close()

        # First POST: recovery lands exactly one reopen and returns a
        # semantic_correction request.
        first_post = self._call("POST", f"/api/jobs/{job_id}/semantic-request",
                                 {"holder_id": "recoveryholder01"}, 200)
        self.assertEqual(first_post["request"]["kind"], "semantic_correction")

        # Second POST: same logical request returned, no duplicate reopen,
        # no duplicate semantic_review verdict.
        second_post = self._call("POST", f"/api/jobs/{job_id}/semantic-request",
                                  {"holder_id": "recoveryholder01"}, 200)
        self.assertEqual(second_post["request"]["kind"], "semantic_correction")
        self.assertEqual(second_post["request"]["request_id"], first_post["request"]["request_id"])

        sem_store = open_semantic_work_store(self.data_root / "semantic-work.sqlite3")
        try:
            rows = sem_store.load_all_for_job(job_id)
            review_rows = [r for r in rows if r.kind == "semantic_review"]
            self.assertEqual(len(review_rows), 1)
        finally:
            sem_store.close()

        ws_store = open_workflow_state_store(self.data_root / "workflow-state.sqlite3")
        try:
            from course_compiler.course_job_persistence import open_course_job_store
            job_store = open_course_job_store(self.data_root / "course-jobs.sqlite3")
            job = job_store.load(job_id)
            ws = ws_store.load(job.workflow_id)
            reopens = [r for r in ws.operation_receipts if r.action == "reopen_lectures_for_correction"]
            self.assertEqual(len(reopens), 1)
            self.assertEqual(reopens[0].operation_id, f"{review_request['operation_id']}-reopen")
            job_store.close()
        finally:
            ws_store.close()


def _make_request(payload: dict) -> "object":
    """Reconstruct a SemanticWorkRequest from the wire shape the HTTP API returns."""

    from course_compiler.semantic_work import SemanticWorkRequest, SEMANTIC_WORK_REQUEST_VERSION
    return SemanticWorkRequest(
        SEMANTIC_WORK_REQUEST_VERSION,
        payload["request_id"],
        payload["job_id"],
        payload["workflow_id"],
        payload["expected_revision"],
        payload["operation_id"],
        payload["kind"],
        tuple(payload["input_refs"]),
        payload["created_at"],
        None,
    )
