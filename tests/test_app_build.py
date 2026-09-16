"""Integration tests for T050 application build ownership, artifact download, preview, and extraction endpoints."""

from __future__ import annotations

import base64
import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.toolchain_support import (
    POPPLER_AVAILABLE,
    POPPLER_MISSING_REASON,
    TEX_MISSING_REASON,
    TEX_TOOLCHAIN_AVAILABLE,
)

from course_compiler.app.config import AppConfig, create_config
from course_compiler.app.context import AppContext
from course_compiler.app.server import create_server

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_ISOLATED_BASE = REPOSITORY_ROOT / "local-data" / "test-tmp-app-build"
_ISOLATED_BASE.mkdir(parents=True, exist_ok=True)


def _isolated_tmp_dir() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(dir=str(_ISOLATED_BASE), prefix="app-")


def _make_dummy_pdf(label: str = "1") -> bytes:
    content = (
        f"%PDF-1.4\n%{label}\n"
        "1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        "2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        "3 0 obj<</Type/Page/MediaBox[0 0 300 144]/Parent 2 0 R/Resources<<>>>>endobj\n"
        "xref\n0 4\n0000000000 65535 f \n0000000013 00000 n \n0000000062 00000 n \n0000000119 00000 n \n"
        "trailer<</Size 4/Root 1 0 R>>\nstartxref\n210\n%%EOF\n"
    )
    return content.encode("latin1")


def _get(port: int, path: str, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        body = resp.read()
        return resp.status, headers, body
    finally:
        conn.close()


def _post(port: int, path: str, payload: dict | None = None) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        headers = {"content-type": "application/json"}
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        body_bytes = resp.read()
        return resp.status, headers, body_bytes
    finally:
        conn.close()


class TestAppBuildEndpoints(unittest.TestCase):
    def _start_server(self, data_root_str: str) -> tuple[object, int, threading.Thread]:
        cfg = create_config(host="127.0.0.1", port=0, data_root=Path(data_root_str))
        srv = create_server(cfg)
        port = srv.server_port
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        time.sleep(0.05)
        return srv, port, t

    @unittest.skipUnless(
        TEX_TOOLCHAIN_AVAILABLE,
        f"real XeLaTeX build unavailable ({TEX_MISSING_REASON})",
    )
    @unittest.skipUnless(
        POPPLER_AVAILABLE,
        f"real Poppler preview/extraction unavailable ({POPPLER_MISSING_REASON})",
    )
    def test_build_lifecycle_endpoints(self) -> None:
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                # 1. Create course
                status, headers, body = _post(
                    port,
                    "/api/courses",
                    {"title": "App Build Test Course", "ai_mode": "gpt", "quality_mode": "fast"},
                )
                self.assertEqual(status, 201)
                course_data = json.loads(body.decode("utf-8"))
                course_id = course_data["course"]["course_id"]

                # 2. Attach two synthetic sources
                pdf_b64_1 = base64.b64encode(_make_dummy_pdf("1")).decode("ascii")
                status, _, body = _post(
                    port,
                    f"/api/courses/{course_id}/sources",
                    {"source_id": "src-1", "content_base64": pdf_b64_1},
                )
                self.assertEqual(status, 201)
                s1_data = json.loads(body.decode("utf-8"))
                s1_id = s1_data["source_id"]

                pdf_b64_2 = base64.b64encode(_make_dummy_pdf("2")).decode("ascii")
                status, _, body = _post(
                    port,
                    f"/api/courses/{course_id}/sources",
                    {"source_id": "src-2", "content_base64": pdf_b64_2},
                )
                self.assertEqual(status, 201)

                # 3. Source preview & extraction endpoints
                p_status, p_headers, p_body = _get(port, f"/api/courses/{course_id}/sources/{s1_id}/preview?page=1")
                self.assertEqual(p_status, 200)
                self.assertEqual(p_headers.get("content-type"), "image/png")
                self.assertTrue(p_body.startswith(b"\x89PNG"))

                e_status, e_headers, e_body = _post(
                    port,
                    f"/api/courses/{course_id}/sources/{s1_id}/extract",
                    {"page": 1, "left": 10, "top": 10, "width": 50, "height": 50},
                )
                self.assertEqual(e_status, 200)
                self.assertEqual(e_headers.get("content-type"), "image/png")
                self.assertTrue(e_body.startswith(b"\x89PNG"))

                # 4. Start generation -> creates job
                status, _, body = _post(port, f"/api/courses/{course_id}/start-generation")
                self.assertEqual(status, 201)
                job_data = json.loads(body.decode("utf-8"))
                job_id = job_data["job_id"]

                # 5. Build before workflow completed -> 409
                b_status, _, b_body = _post(port, f"/api/jobs/{job_id}/build")
                self.assertEqual(b_status, 409)
                self.assertIn(b"workflow_not_completed", b_body)

                # 6. Artifact before build -> 404
                a_status, _, _ = _get(port, f"/api/jobs/{job_id}/artifact")
                self.assertEqual(a_status, 404)

                # 7. Run controlled generation step until completion
                g_status, _, g_body = _post(
                    port,
                    f"/api/jobs/{job_id}/run-controlled-generation",
                    {"map_size": 1},
                )
                self.assertEqual(g_status, 200)
                g_data = json.loads(g_body.decode("utf-8"))
                self.assertEqual(g_data["status"], "deterministic_building")

                # Job status check
                j_status, _, j_body = _get(port, f"/api/jobs/{job_id}")
                self.assertEqual(j_status, 200)
                j_data = json.loads(j_body.decode("utf-8"))
                self.assertEqual(j_data["status"], "deterministic_building")
                self.assertIsNone(j_data["completed_build_id"])

                # 8. POST /api/jobs/{job_id}/build -> succeeds
                b_status, _, b_body = _post(port, f"/api/jobs/{job_id}/build")
                self.assertEqual(b_status, 200)
                b_data = json.loads(b_body.decode("utf-8"))
                self.assertEqual(b_data["status"], "succeeded")
                build_id = b_data["build_id"]
                self.assertTrue(build_id.startswith("bld-"))

                # Verify Job status updated to completed with build pointers
                j_status, _, j_body = _get(port, f"/api/jobs/{job_id}")
                self.assertEqual(j_status, 200)
                j_data = json.loads(j_body.decode("utf-8"))
                self.assertEqual(j_data["status"], "completed")
                self.assertEqual(j_data["completed_build_id"], build_id)

                # 9. GET /api/jobs/{job_id}/artifact -> downloads PDF
                art_status, art_headers, art_bytes = _get(port, f"/api/jobs/{job_id}/artifact")
                self.assertEqual(art_status, 200)
                self.assertEqual(art_headers.get("content-type"), "application/pdf")
                self.assertTrue(art_bytes.startswith(b"%PDF"))
                self.assertIn(f"course-{build_id}.pdf", art_headers.get("content-disposition", ""))

                # 10. GET /api/builds/{build_id}/artifact -> downloads PDF
                art_status2, art_headers2, art_bytes2 = _get(port, f"/api/builds/{build_id}/artifact")
                self.assertEqual(art_status2, 200)
                self.assertEqual(art_bytes2, art_bytes)

                # 11. GET /api/jobs/{job_id}/builds -> lists build history
                hist_status, _, hist_body = _get(port, f"/api/jobs/{job_id}/builds")
                self.assertEqual(hist_status, 200)
                hist_data = json.loads(hist_body.decode("utf-8"))
                self.assertEqual(len(hist_data["builds"]), 1)
                self.assertEqual(hist_data["builds"][0]["build_id"], build_id)

                # 12. Delete cache file, re-request artifact (cache miss re-derivation)
                bundle_hash = b_data["bundle_hash"]
                cache_file = Path(tmp) / "cache" / f"{bundle_hash}.pdf"
                self.assertTrue(cache_file.is_file())
                cache_file.unlink()
                self.assertFalse(cache_file.exists())

                re_status, re_headers, re_bytes = _get(port, f"/api/jobs/{job_id}/artifact")
                self.assertEqual(re_status, 200)
                self.assertTrue(re_bytes.startswith(b"%PDF"))
                # Deterministic re-derivation over HTTP: exact byte equality.
                self.assertEqual(re_bytes, art_bytes)
                # Cache file must be restored
                self.assertTrue(cache_file.is_file())

            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    @unittest.skipUnless(
        POPPLER_AVAILABLE,
        f"real Poppler preview/extraction unavailable ({POPPLER_MISSING_REASON})",
    )
    def test_source_read_boundaries_and_cross_course_rejection(self) -> None:
        """Preview/extraction reject wrong ids, bad pages, bad regions, and cross-course reads."""

        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                # Two distinct courses, each with its own source.
                status, _, body = _post(
                    port,
                    "/api/courses",
                    {"title": "Course One", "ai_mode": "gpt", "quality_mode": "fast"},
                )
                self.assertEqual(status, 201)
                course_a = json.loads(body.decode("utf-8"))["course"]["course_id"]

                status, _, body = _post(
                    port,
                    "/api/courses",
                    {"title": "Course Two", "ai_mode": "gpt", "quality_mode": "fast"},
                )
                self.assertEqual(status, 201)
                course_b = json.loads(body.decode("utf-8"))["course"]["course_id"]

                status, _, body = _post(
                    port,
                    f"/api/courses/{course_a}/sources",
                    {
                        "source_id": "src-a",
                        "content_base64": base64.b64encode(_make_dummy_pdf("a")).decode("ascii"),
                    },
                )
                self.assertEqual(status, 201)
                src_a = json.loads(body.decode("utf-8"))["source_id"]

                status, _, body = _post(
                    port,
                    f"/api/courses/{course_b}/sources",
                    {
                        "source_id": "src-b",
                        "content_base64": base64.b64encode(_make_dummy_pdf("b")).decode("ascii"),
                    },
                )
                self.assertEqual(status, 201)
                src_b = json.loads(body.decode("utf-8"))["source_id"]

                # Baseline: the valid pair works.
                st, hd, bd = _get(port, f"/api/courses/{course_a}/sources/{src_a}/preview?page=1")
                self.assertEqual(st, 200)
                self.assertEqual(hd.get("content-type"), "image/png")

                # Cross-course access: course A must not read course B's source.
                st, _, bd = _get(port, f"/api/courses/{course_a}/sources/{src_b}/preview?page=1")
                self.assertEqual(st, 404)
                self._assert_no_filesystem_path(bd)

                st, _, bd = _post(
                    port,
                    f"/api/courses/{course_a}/sources/{src_b}/extract",
                    {"page": 1, "left": 0, "top": 0, "width": 10, "height": 10},
                )
                self.assertIn(st, (400, 404))
                self._assert_no_filesystem_path(bd)

                # Unknown course and unknown source.
                st, _, bd = _get(port, f"/api/courses/course-nope/sources/{src_a}/preview?page=1")
                self.assertEqual(st, 404)
                self._assert_no_filesystem_path(bd)

                st, _, bd = _get(port, f"/api/courses/{course_a}/sources/src-nope/preview?page=1")
                self.assertEqual(st, 404)
                self._assert_no_filesystem_path(bd)

                # Invalid page numbers.
                for page in (0, -1, 9999):
                    st, _, bd = _get(
                        port, f"/api/courses/{course_a}/sources/{src_a}/preview?page={page}"
                    )
                    self.assertEqual(st, 404, f"page={page}")
                    self._assert_no_filesystem_path(bd)

                # Invalid regions: zero, negative, and out-of-bounds extents.
                for region in (
                    {"page": 1, "left": 0, "top": 0, "width": 0, "height": 0},
                    {"page": 1, "left": 0, "top": 0, "width": -5, "height": 10},
                    {"page": 1, "left": 0, "top": 0, "width": 100000, "height": 100000},
                    {"page": 99, "left": 0, "top": 0, "width": 10, "height": 10},
                ):
                    st, _, bd = _post(
                        port, f"/api/courses/{course_a}/sources/{src_a}/extract", region
                    )
                    self.assertIn(st, (400, 404), f"region={region}")
                    self._assert_no_filesystem_path(bd)

                # Malformed extraction bodies fail closed.
                for bad in ({"page": "one"}, {"left": None}, {"width": [1]}):
                    st, _, bd = _post(
                        port, f"/api/courses/{course_a}/sources/{src_a}/extract", bad
                    )
                    self.assertIn(st, (400, 404), f"body={bad}")
                    self._assert_no_filesystem_path(bd)

            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_get_and_head_do_not_mutate(self) -> None:
        """Safe methods never create builds or advance a job."""

        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                status, _, body = _post(
                    port,
                    "/api/courses",
                    {"title": "Safe Method Course", "ai_mode": "gpt", "quality_mode": "fast"},
                )
                self.assertEqual(status, 201)
                course_id = json.loads(body.decode("utf-8"))["course"]["course_id"]

                status, _, _ = _post(
                    port,
                    f"/api/courses/{course_id}/sources",
                    {
                        "source_id": "src-safe",
                        "content_base64": base64.b64encode(_make_dummy_pdf("s")).decode("ascii"),
                    },
                )
                self.assertEqual(status, 201)

                status, _, body = _post(port, f"/api/courses/{course_id}/start-generation")
                self.assertEqual(status, 201)
                job_id = json.loads(body.decode("utf-8"))["job_id"]

                before_status, _, before_body = _get(port, f"/api/jobs/{job_id}")
                self.assertEqual(before_status, 200)
                before = json.loads(before_body.decode("utf-8"))

                # Mutating routes reject GET and HEAD outright.
                for path in (
                    f"/api/jobs/{job_id}/build",
                    f"/api/jobs/{job_id}/run-controlled-generation",
                    f"/api/courses/{course_id}/sources/src-safe/extract",
                ):
                    for method in ("GET", "HEAD"):
                        st, _, _ = _get(port, path, method=method)
                        self.assertEqual(st, 405, f"{method} {path}")

                # Safe reads leave the job and build history untouched.
                for path in (
                    f"/api/jobs/{job_id}",
                    f"/api/jobs/{job_id}/builds",
                    f"/api/jobs/{job_id}/artifact",
                ):
                    for method in ("GET", "HEAD"):
                        _get(port, path, method=method)

                after_status, _, after_body = _get(port, f"/api/jobs/{job_id}")
                self.assertEqual(after_status, 200)
                after = json.loads(after_body.decode("utf-8"))
                self.assertEqual(after["status"], before["status"])
                self.assertEqual(after["current_revision"], before["current_revision"])
                self.assertEqual(after["current_stage"], before["current_stage"])
                self.assertIsNone(after["completed_build_id"])

                hist_status, _, hist_body = _get(port, f"/api/jobs/{job_id}/builds")
                self.assertEqual(hist_status, 200)
                self.assertEqual(json.loads(hist_body.decode("utf-8"))["builds"], [])

            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def _assert_no_filesystem_path(self, body: bytes) -> None:
        """No error body may leak a private filesystem path."""

        text = body.decode("utf-8", "replace")
        for marker in ("/home/", "/tmp/", "/var/", "local-data", str(REPOSITORY_ROOT)):
            self.assertNotIn(marker, text, f"error body leaked {marker!r}: {text[:200]}")

    def test_invalid_requests_and_safety(self) -> None:
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                # Path traversal attempts
                s, _, _ = _get(port, "/api/jobs/../secret/artifact")
                self.assertEqual(s, 404)

                s, _, _ = _post(port, "/api/jobs/../secret/build")
                self.assertEqual(s, 404)

                # Unknown job / build
                s, _, _ = _get(port, "/api/jobs/job-unknown/artifact")
                self.assertEqual(s, 404)

                s, _, _ = _get(port, "/api/builds/bld-unknown/artifact")
                self.assertEqual(s, 404)

                # Method not allowed
                s, _, _ = _get(port, "/api/jobs/job-1/build")
                self.assertEqual(s, 405)

                s, _, _ = _post(port, "/api/jobs/job-1/artifact")
                self.assertEqual(s, 405)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
