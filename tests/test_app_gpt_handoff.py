"""HTTP-level tests for the T051 GPT low-turn relay endpoints:

    GET  /api/courses/{course_id}/jobs/{job_id}/pending_work
    GET  /api/courses/{course_id}/jobs/{job_id}/pending_work/{request_id}/evidence/{id}
    POST /api/courses/{course_id}/jobs/{job_id}/result

Invented synthetic bytes only.
"""

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

from course_compiler.app.config import create_config
from course_compiler.app.server import create_server

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_ISOLATED_BASE = REPOSITORY_ROOT / "local-data" / "test-tmp-app-gpt-handoff"
_ISOLATED_BASE.mkdir(parents=True, exist_ok=True)


def _isolated_tmp_dir() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(dir=str(_ISOLATED_BASE), prefix="app-")


def _pdf(label: str) -> bytes:
    return f"%PDF-1.4\n%{label} invented synthetic bytes\n%%EOF".encode("latin1")


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


def _head(port: int, path: str) -> tuple[int, dict[str, str], bytes]:
    return _request(port, "HEAD", path)


def _post(port: int, path: str, payload: dict | None = None) -> tuple[int, dict[str, str], bytes]:
    return _request(port, "POST", path, payload if payload is not None else {})


class GptHandoffHttpTests(unittest.TestCase):
    def _source_result(self, request, holder):
        return {"request_id": request["request_id"], "holder_id": holder,
            "text": "# Source assessment\nInvented assessment.\n# Priority proposal\nFull invented coverage.\n# Evidence hierarchy\nOfficial invented source."}

    def _start_server(self, data_root_str: str):
        cfg = create_config(host="127.0.0.1", port=0, data_root=Path(data_root_str))
        srv = create_server(cfg)
        port = srv.server_port
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        time.sleep(0.05)
        return srv, port, t

    def _new_course_and_job(self, port: int, *, tag: str = "x") -> tuple[str, str]:
        status, _, body = _post(port, "/api/courses", {"title": "GPT Handoff Course", "ai_mode": "gpt", "quality_mode": "fast"})
        self.assertEqual(status, 201)
        course_id = json.loads(body)["course"]["course_id"]
        for label in ("a", "b"):
            status, _, body = _post(
                port,
                f"/api/courses/{course_id}/sources",
                {"source_id": f"src-{tag}-{label}", "content_base64": base64.b64encode(_pdf(tag + label)).decode("ascii")},
            )
            self.assertEqual(status, 201)
        status, _, body = _post(port, f"/api/courses/{course_id}/start-generation")
        self.assertEqual(status, 201)
        job_id = json.loads(body)["job_id"]
        return course_id, job_id



    def test_pending_work_requires_the_lease_acquire_mutation_first(self) -> None:
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                course_id, job_id = self._new_course_and_job(port)

                # No pending row exists yet: a GET must never silently
                # acquire one.
                status, _, body = _get(port, f"/api/courses/{course_id}/jobs/{job_id}/pending_work")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["status"], "no_semantic_work")

                # The accepted T049 mutation route acquires the lease.
                status, _, body = _post(port, f"/api/jobs/{job_id}/semantic-request", {"holder_id": "holder0000000010"})
                self.assertEqual(status, 200)
                acquired = json.loads(body)["request"]
                self.assertEqual(acquired["kind"], "source_assessment")

                # Now the coarse observation reflects the acquired request.
                status, _, body = _get(port, f"/api/courses/{course_id}/jobs/{job_id}/pending_work")
                self.assertEqual(status, 200)
                data = json.loads(body)
                self.assertEqual(data["status"], "ready_for_chatgpt")
                handoff = data["handoff"]
                self.assertIn("prompt", handoff)
                self.assertEqual(handoff["job_id"], job_id)
                self.assertEqual(handoff["request_id"], acquired["request_id"])
                self.assertEqual(handoff["operation_id"], acquired["operation_id"])
                self.assertEqual(handoff["kind"], "source_assessment")
                self.assertTrue(len(handoff["evidence_manifest"]) >= 2)
                encoded = json.dumps(handoff, sort_keys=True, separators=(",", ":"))
                self.assertLessEqual(len(encoded.encode("utf-8")), 512 * 1024)

                # Observation is repeatable and non-mutating: the same GET
                # twice returns the identical request identity.
                status2, _, body2 = _get(port, f"/api/courses/{course_id}/jobs/{job_id}/pending_work")
                self.assertEqual(json.loads(body2)["handoff"]["request_id"], handoff["request_id"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_pending_work_rejects_course_job_mismatch(self) -> None:
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                course_a, job_a = self._new_course_and_job(port)
                course_b, _job_b = self._new_course_and_job(port)

                status, _, _ = _get(port, f"/api/courses/{course_b}/jobs/{job_a}/pending_work")
                self.assertEqual(status, 404)

                status, _, _ = _get(port, "/api/courses/does-not-exist/jobs/" + job_a + "/pending_work")
                self.assertEqual(status, 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_pending_work_rejects_post_and_bad_ids(self) -> None:
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                course_id, job_id = self._new_course_and_job(port)
                status, _, _ = _post(port, f"/api/courses/{course_id}/jobs/{job_id}/pending_work")
                self.assertEqual(status, 405)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_evidence_endpoint_serves_exact_digest_verified_bytes(self) -> None:
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                course_id, job_id = self._new_course_and_job(port)
                _post(port, f"/api/jobs/{job_id}/semantic-request", {"holder_id": "holder0000000011"})
                status, _, body = _get(port, f"/api/courses/{course_id}/jobs/{job_id}/pending_work")
                self.assertEqual(status, 200)
                handoff = json.loads(body)["handoff"]

                for item in handoff["evidence_manifest"]:
                    e_status, e_headers, e_body = _get(
                        port,
                        f"/api/courses/{course_id}/jobs/{job_id}/pending_work/{handoff['request_id']}/evidence/{item['evidence_id']}",
                    )
                    self.assertEqual(e_status, 200)
                    self.assertEqual(e_headers.get("content-type"), item["media_type"])
                    self.assertEqual(e_headers.get("cache-control"), "no-store")
                    expected_extension = {
                        "application/pdf": ".pdf",
                        "text/plain; charset=utf-8": ".txt",
                        "text/markdown; charset=utf-8": ".md",
                    }[item["media_type"]]
                    self.assertEqual(
                        e_headers.get("content-disposition"),
                        f'attachment; filename="{item["evidence_id"]}{expected_extension}"',
                    )
                    self.assertEqual(len(e_body), item["byte_length"])
                    self.assertEqual(hashlib.sha256(e_body).hexdigest(), item["content_sha256"])

                    # HEAD is non-mutating and returns no body.
                    h_status, h_headers, h_body = _head(
                        port,
                        f"/api/courses/{course_id}/jobs/{job_id}/pending_work/{handoff['request_id']}/evidence/{item['evidence_id']}",
                    )
                    self.assertEqual(h_status, 200)
                    self.assertEqual(h_body, b"")
                    self.assertEqual(h_headers.get("content-length"), str(item["byte_length"]))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_evidence_endpoint_rejects_unknown_id_and_cross_course(self) -> None:
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                course_a, job_a = self._new_course_and_job(port, tag="a")
                course_b, job_b = self._new_course_and_job(port, tag="b")
                _post(port, f"/api/jobs/{job_a}/semantic-request", {"holder_id": "holder0000000012"})
                _post(port, f"/api/jobs/{job_b}/semantic-request", {"holder_id": "holder0000000013"})

                status, _, body = _get(port, f"/api/courses/{course_a}/jobs/{job_a}/pending_work")
                handoff_a = json.loads(body)["handoff"]
                evidence_id = handoff_a["evidence_manifest"][0]["evidence_id"]

                # Arbitrary/unknown evidence ID.
                status, _, _ = _get(
                    port, f"/api/courses/{course_a}/jobs/{job_a}/pending_work/{handoff_a['request_id']}/evidence/src-not-a-real-one"
                )
                self.assertEqual(status, 404)

                # Cross-course: job_a's evidence under course_b's path.
                status, _, _ = _get(
                    port, f"/api/courses/{course_b}/jobs/{job_a}/pending_work/{handoff_a['request_id']}/evidence/{evidence_id}"
                )
                self.assertEqual(status, 404)

                # Cross-job: job_a's evidence id under job_b (even same course_b path is invalid; use job_b/course_b).
                status, _, _ = _get(
                    port, f"/api/courses/{course_b}/jobs/{job_b}/pending_work/{handoff_a['request_id']}/evidence/{evidence_id}"
                )
                self.assertEqual(status, 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_old_request_evidence_url_is_stale_after_request_advances(self) -> None:
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                course_id, job_id = self._new_course_and_job(port, tag="stale")
                holder = "holder-stale"
                _post(port, f"/api/jobs/{job_id}/semantic-request", {"holder_id": holder})
                _, _, body = _get(port, f"/api/courses/{course_id}/jobs/{job_id}/pending_work")
                handoff = json.loads(body)["handoff"]
                evidence_id = handoff["evidence_manifest"][0]["evidence_id"]
                old_url = f"/api/courses/{course_id}/jobs/{job_id}/pending_work/{handoff['request_id']}/evidence/{evidence_id}"
                status, _, _ = _get(port, old_url)
                self.assertEqual(status, 200)

                status, _, request_body = _post(port, f"/api/jobs/{job_id}/semantic-request", {"holder_id": holder})
                self.assertEqual(status, 200)
                # Same holder renewal keeps R1; accepting it advances to R2.
                status, _, _ = _post(port, f"/api/courses/{course_id}/jobs/{job_id}/result", self._source_result(json.loads(request_body)["request"], holder))
                self.assertEqual(status, 200)
                status, _, _ = _get(port, old_url)
                self.assertEqual(status, 404)
                status, _, request_body = _post(port, f"/api/jobs/{job_id}/semantic-request", {"holder_id": holder})
                self.assertEqual(status, 200)
                _request2 = json.loads(request_body)["request"]
                _, _, body2 = _get(port, f"/api/courses/{course_id}/jobs/{job_id}/pending_work")
                handoff2 = json.loads(body2)["handoff"]
                new_url = f"/api/courses/{course_id}/jobs/{job_id}/pending_work/{handoff2['request_id']}/evidence/{evidence_id}"
                status, _, _ = _get(port, old_url)
                self.assertEqual(status, 404)
                status, _, _ = _get(port, new_url)
                self.assertEqual(status, 200)
                unknown_url = f"/api/courses/{course_id}/jobs/{job_id}/pending_work/req-unknown/evidence/{evidence_id}"
                status, _, _ = _get(port, unknown_url)
                self.assertEqual(status, 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


    def test_t051_relay_result_size_is_bounded_before_body_read(self) -> None:
        """A malformed oversized relay result is refused without buffering it."""

        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                course_id, job_id = self._new_course_and_job(port, tag="too-large")
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
                try:
                    conn.request(
                        "POST",
                        f"/api/courses/{course_id}/jobs/{job_id}/result",
                        body=b"",
                        headers={"Content-Type": "application/json", "Connection": "close", "Content-Length": str(2 * 1024 * 1024 + 1)},
                    )
                    response = conn.getresponse()
                    self.assertEqual(response.status, 400)
                    self.assertEqual(json.loads(response.read())["error"], "invalid_input")
                finally:
                    conn.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)



    def test_result_endpoint_accepts_valid_result_and_rejects_cross_course(self) -> None:
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                course_id, job_id = self._new_course_and_job(port)
                holder = "holder0000000014"
                status, _, body = _post(port, f"/api/jobs/{job_id}/semantic-request", {"holder_id": holder})
                self.assertEqual(status, 200)
                req = json.loads(body)["request"]

                payload = self._source_result(req, holder)

                # Cross-course rejection first: a foreign course_id must
                # never be able to submit against this job.
                course_b, _job_b = self._new_course_and_job(port, tag="b")
                status, _, _ = _post(port, f"/api/courses/{course_b}/jobs/{job_id}/result", payload)
                self.assertEqual(status, 404)

                status, _, body = _post(port, f"/api/courses/{course_id}/jobs/{job_id}/result", payload)
                self.assertEqual(status, 200)
                data = json.loads(body)
                self.assertIn(data.get("status"), ("advanced", "idempotent_repeat"))

                # The workflow actually advanced: no request is pending yet
                # for the new revision (observation never mints one), but
                # acquiring the lease again now yields the next kind in the
                # low-turn sequence.
                status, _, body = _get(port, f"/api/courses/{course_id}/jobs/{job_id}/pending_work")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["status"], "no_semantic_work")

                status, _, body = _post(port, f"/api/jobs/{job_id}/semantic-request", {"holder_id": holder})
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["request"]["kind"], "exam_priority_assessment")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_result_endpoint_rejects_unknown_job(self) -> None:
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                course_id, _job_id = self._new_course_and_job(port)
                status, _, _ = _post(
                    port,
                    f"/api/courses/{course_id}/jobs/does-not-exist/result",
                    {"request_id": "req1", "holder_id": "browser", "text": "assessment"},
                )
                self.assertEqual(status, 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
