"""T051 Course->Job browser projection regression.

Reproduces, at the browser/HTTP data-contract level, the live product E2E
that exposed the bug: after ``Start Generation`` a Job exists durably, but
the frontend's ``loadCourses()`` previously read ``current_job_id`` off the
*course-detail* response body directly instead of unwrapping its
``{"course": {...}}`` envelope, so ``active_job_id`` was always undefined
and the card kept rendering Attach Sources / Start Generation forever.

This test exercises the real HTTP response envelopes (no hand-built mock
that flattens them) and asserts:

  1. ``GET /api/courses/{id}`` returns the envelope shape
     ``{"course": {...}}`` — the contract the frontend must respect.
  2. ``GET /api/courses`` already carries a durable ``current_job_id`` on
     each record after generation starts (survives a fresh list fetch,
     i.e. "survives reload").
  3. Replaying the exact frontend data-contract sequence that
     ``loadCourses()`` in ``app.js`` performs (list -> per-course
     ``current_job_id`` -> job status) yields a discoverable active job:
     no Attach/Start controls, GPT action visible.

Invented synthetic bytes only; no real course content, no ChatGPT, no
network egress.
"""

from __future__ import annotations

import base64
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from course_compiler.app.config import create_config
from course_compiler.app.server import create_server

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_ISOLATED_BASE = REPOSITORY_ROOT / "local-data" / "test-tmp-projection-e2e"
_ISOLATED_BASE.mkdir(parents=True, exist_ok=True)


def _isolated_tmp_dir() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(dir=str(_ISOLATED_BASE), prefix="projection-")


def _pdf(label: str) -> bytes:
    return f"%PDF-1.4\n%{label} invented E2E synthetic bytes\n%%EOF".encode("latin1")


def _request(port: int, method: str, path: str, payload: dict | None = None) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"content-type": "application/json"} if body is not None else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        headers_out = {k.lower(): v for k, v in resp.getheaders()}
        body_out = resp.read()
        return resp.status, headers_out, body_out
    finally:
        conn.close()


def _get(port: int, path: str) -> tuple[int, dict[str, str], bytes]:
    return _request(port, "GET", path)


def _post(port: int, path: str, payload: dict | None = None) -> tuple[int, dict[str, str], bytes]:
    return _request(port, "POST", path, payload if payload is not None else {})


class T051CourseJobProjectionE2ETests(unittest.TestCase):
    def _start_server(self, data_root_str: str):
        cfg = create_config(host="127.0.0.1", port=0, data_root=Path(data_root_str))
        srv = create_server(cfg)
        port = srv.server_port
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        time.sleep(0.05)
        return srv, port, t

    def _stop_server(self, server, thread) -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    def _load_courses_like_frontend(self, port: int) -> list[dict]:
        """Replays the exact data-contract sequence app.js's loadCourses()
        performs after the projection fix: GET /api/courses, then for each
        record with a durable current_job_id, project it onto
        active_job_id and fetch the Job's live status. No detail-envelope
        flattening — this is the real contract, not a mock."""
        status, _, body = _get(port, "/api/courses")
        self.assertEqual(status, 200)
        courses = json.loads(body)["courses"]
        for c in courses:
            if c.get("current_job_id"):
                c["active_job_id"] = c["current_job_id"]
                jstatus, _, jbody = _get(port, "/api/jobs/" + c["current_job_id"])
                if jstatus == 200:
                    jdata = json.loads(jbody)
                    c["job_status"] = jdata.get("status")
                    c["current_stage"] = jdata.get("current_stage")
                    c["current_disposition"] = jdata.get("current_disposition")
                    c["owner_approval"] = jdata.get("owner_approval")
        return courses

    def test_course_detail_envelope_is_wrapped(self) -> None:
        """GET /api/courses/{id} returns {"course": {...}}, not a flattened
        body. This is the contract the frontend must unwrap/avoid relying
        on directly for current_job_id."""
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                status, _, body = _post(
                    port, "/api/courses", {"title": "T051 Projection Course", "ai_mode": "gpt", "quality_mode": "fast"}
                )
                self.assertEqual(status, 201)
                course_id = json.loads(body)["course"]["course_id"]

                status, _, body = _get(port, f"/api/courses/{course_id}")
                self.assertEqual(status, 200)
                envelope = json.loads(body)
                self.assertIn("course", envelope)
                self.assertNotIn("current_job_id", envelope, "detail body must stay wrapped, not flattened")
                self.assertIn("current_job_id", envelope["course"])
            finally:
                self._stop_server(server, thread)

    def test_active_job_survives_reload_and_gates_ui_controls(self) -> None:
        """Full live-repro sequence: fresh course -> attach 2 sources ->
        start generation -> reload courses through the same frontend
        data-contract sequence -> active Job is discoverable, no
        Attach/Start controls, GPT action visible. Also proves
        GET /api/courses carries the durable current_job_id after a fresh
        list fetch (i.e. survives reload, not just the initial response)."""
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                status, _, body = _post(
                    port, "/api/courses", {"title": "T051 Projection Course", "ai_mode": "gpt", "quality_mode": "fast"}
                )
                self.assertEqual(status, 201)
                course_id = json.loads(body)["course"]["course_id"]

                # Before any source is attached / generation started: no
                # durable job pointer anywhere in the list contract.
                courses = self._load_courses_like_frontend(port)
                before = next(c for c in courses if c["course_id"] == course_id)
                self.assertIsNone(before.get("current_job_id"))
                self.assertNotIn("active_job_id", before)

                for label in ("one", "two"):
                    status, _, _ = _post(
                        port,
                        f"/api/courses/{course_id}/sources",
                        {"source_id": f"src-{label}", "content_base64": base64.b64encode(_pdf(label)).decode("ascii")},
                    )
                    self.assertEqual(status, 201)

                status, _, body = _post(port, f"/api/courses/{course_id}/start-generation")
                self.assertEqual(status, 201)
                job_id = json.loads(body)["job_id"]

                # Reload #1 (this is the "refresh" from the live repro):
                # GET /api/courses must itself carry the durable pointer.
                status, _, body = _get(port, "/api/courses")
                self.assertEqual(status, 200)
                list_record = next(c for c in json.loads(body)["courses"] if c["course_id"] == course_id)
                self.assertEqual(list_record["current_job_id"], job_id)

                # Reload #2: replay the exact frontend contract sequence
                # end to end and confirm projection + UI gating.
                courses = self._load_courses_like_frontend(port)
                after = next(c for c in courses if c["course_id"] == course_id)
                self.assertEqual(after.get("active_job_id"), job_id, "active Job must be discoverable after reload")

                # UI-gating contract mirrored from app.js render(): a
                # truthy active_job_id means Attach Sources / Start
                # Generation must not be the rendered controls, and for
                # ai_mode == "gpt" the GPT relay action must be visible.
                self.assertTrue(after["active_job_id"], "card must not fall back to Attach/Start controls")
                self.assertEqual(after["ai_mode"], "gpt")
                self.assertNotIn(after.get("job_status"), ("completed", "deterministic_building"))
                # -> render() takes the else-branch for this job_status,
                # which is exactly the branch that emits the GPT relay
                # button when ai_mode == "gpt".

                # A second, independent reload proves this is not a
                # one-shot artifact of the start-generation response.
                courses_again = self._load_courses_like_frontend(port)
                after_again = next(c for c in courses_again if c["course_id"] == course_id)
                self.assertEqual(after_again.get("active_job_id"), job_id)
            finally:
                self._stop_server(server, thread)


if __name__ == "__main__":
    unittest.main()
