"""T051 human-latency GPT lease repair regressions (L1-L8).

A genuine owner-driven GPT Product E2E reached its first real semantic
turn and was rejected at submission: the browser acquired the accepted
T049 300-second lease once, when the relay opened, and did not renew it
before posting the result. A real human-mediated ChatGPT turn routinely
exceeds five minutes, so ``submit_semantic_result`` correctly refused the
expired lease as ``stale_revision``.

These tests pin the repair (same-holder pre-submit renewal on the
existing accepted ``POST /api/jobs/{job_id}/semantic-request`` route plus
exact identity verification), prove the underlying T049 lease was not
weakened, and prove the exact content-safe diagnostics now survive the
T051 browser-facing transport.

Invented synthetic bytes only; no real course content, no ChatGPT, no
network egress.
"""

from __future__ import annotations

import base64
import http.client
import json
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from course_compiler.app.config import create_config
from course_compiler.app.server import create_server

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_ISOLATED_BASE = REPOSITORY_ROOT / "local-data" / "test-tmp-lease-repair"
_ISOLATED_BASE.mkdir(parents=True, exist_ok=True)

_ISO = "%Y-%m-%dT%H:%M:%SZ"
# The accepted T049 default. These tests must never change it; they only
# prove the browser survives a human turn longer than it.
_LEASE_SECONDS = 300


def _isolated_tmp_dir() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(dir=str(_ISOLATED_BASE), prefix="lease-")


def _pdf(label: str) -> bytes:
    return f"%PDF-1.4\n%{label} invented lease-repair synthetic bytes\n%%EOF".encode("latin1")


def _request(port: int, method: str, path: str, payload: dict | None = None) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"content-type": "application/json"} if body is not None else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _get(port: int, path: str) -> tuple[int, bytes]:
    return _request(port, "GET", path)


def _post(port: int, path: str, payload: dict | None = None) -> tuple[int, bytes]:
    return _request(port, "POST", path, payload if payload is not None else {})


def _age_lease(data_root: str, request_id: str, *, seconds: int) -> str:
    """Simulate genuine elapsed human latency on a persisted lease.

    Rewrites exactly the consistent pair the store itself maintains: the
    ``lease_expires_at`` column and the same field inside the immutable
    ``request_json``. The resulting row is byte-for-byte what the store
    would hold if ``seconds`` had really elapsed since acquisition, so
    every downstream expiry check is the real one.
    """

    connection = sqlite3.connect(str(Path(data_root) / "semantic-work.sqlite3"))
    try:
        row = connection.execute(
            "SELECT lease_expires_at, request_json FROM semantic_work_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None or row[0] is None:
            raise AssertionError("expected a leased semantic-work row")
        expires = datetime.strptime(row[0], _ISO).replace(tzinfo=timezone.utc)
        aged = (expires - timedelta(seconds=seconds)).strftime(_ISO)
        payload = json.loads(row[1])
        payload["lease_expires_at"] = aged
        connection.execute(
            "UPDATE semantic_work_requests SET lease_expires_at = ?, request_json = ? WHERE request_id = ?",
            (aged, json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True), request_id),
        )
        connection.commit()
        return aged
    finally:
        connection.close()


def _lease_columns(data_root: str, request_id: str) -> tuple[str, str | None, str | None]:
    connection = sqlite3.connect(str(Path(data_root) / "semantic-work.sqlite3"))
    try:
        row = connection.execute(
            "SELECT status, lease_expires_at, lease_holder FROM semantic_work_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise AssertionError("expected a semantic-work row")
        return row[0], row[1], row[2]
    finally:
        connection.close()


class _RelayHarness(unittest.TestCase):
    """Reaches the first genuine semantic turn (source_assessment) the way
    the browser does: create course -> attach two invented PDFs -> start
    generation -> acquire the accepted T049 lease."""

    def _source_result(self, request, holder):
        return {"request_id": request["request_id"], "holder_id": holder,
            "text": "# Source assessment\nInvented assessment.\n# Priority proposal\nFull invented coverage.\n# Evidence hierarchy\nOfficial invented source."}

    def _start_server(self, data_root_str: str):
        cfg = create_config(host="127.0.0.1", port=0, data_root=Path(data_root_str))
        srv = create_server(cfg)
        port = srv.server_port
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.05)
        return srv, port, thread

    def _stop_server(self, server, thread) -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    def _reach_source_assessment(self, port: int, holder: str, tag: str) -> tuple[str, str, dict]:
        status, body = _post(
            port, "/api/courses", {"title": "T051 Lease Course", "ai_mode": "gpt", "quality_mode": "fast"}
        )
        self.assertEqual(status, 201)
        course_id = json.loads(body)["course"]["course_id"]
        for label in ("one", "two"):
            status, _ = _post(
                port,
                f"/api/courses/{course_id}/sources",
                {
                    "source_id": f"src-{tag}-{label}",
                    "content_base64": base64.b64encode(_pdf(label)).decode("ascii"),
                },
            )
            self.assertEqual(status, 201)
        status, body = _post(port, f"/api/courses/{course_id}/start-generation")
        self.assertEqual(status, 201)
        job_id = json.loads(body)["job_id"]
        status, body = _post(port, f"/api/jobs/{job_id}/semantic-request", {"holder_id": holder})
        self.assertEqual(status, 200)
        request = json.loads(body)["request"]
        self.assertEqual(request["kind"], "source_assessment")
        return course_id, job_id, request


    def _workflow_revision(self, port: int, job_id: str) -> int:
        status, body = _get(port, f"/api/jobs/{job_id}")
        self.assertEqual(status, 200)
        return json.loads(body)["current_revision"]


class T051HumanLatencyLeaseTests(_RelayHarness):
    def test_l1_human_turn_longer_than_the_lease_still_submits_after_renewal(self) -> None:
        """L1: >300s of genuine human latency, workflow unchanged. The
        same-holder pre-submit renewal must re-offer the SAME logical
        request with a fresh expiry, and the original result must then be
        accepted. This is the central regression."""

        holder = "holderlatency001"
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                course_id, job_id, original = self._reach_source_assessment(port, holder, "l1")
                result = self._source_result(original, holder)
                revision_before = self._workflow_revision(port, job_id)

                # The human takes longer than the 300-second T049 lease.
                _age_lease(tmp, original["request_id"], seconds=_LEASE_SECONDS + 60)
                status, expires_at, held_by = _lease_columns(tmp, original["request_id"])
                self.assertEqual(status, "leased")
                self.assertEqual(held_by, holder)
                self.assertLess(
                    datetime.strptime(expires_at, _ISO).replace(tzinfo=timezone.utc),
                    datetime.now(timezone.utc),
                    "the aged lease must genuinely be expired",
                )
                # Authority itself did not move.
                self.assertEqual(self._workflow_revision(port, job_id), revision_before)

                # Pre-submit renewal on the accepted T049 mutation route.
                status, body = _post(port, f"/api/jobs/{job_id}/semantic-request", {"holder_id": holder})
                self.assertEqual(status, 200)
                renewed = json.loads(body)["request"]

                for field in ("request_id", "operation_id", "kind", "expected_revision", "job_id", "workflow_id"):
                    self.assertEqual(renewed[field], original[field], field)
                self.assertEqual(renewed["input_refs"], original["input_refs"])
                self.assertGreater(
                    datetime.strptime(renewed["lease_expires_at"], _ISO).replace(tzinfo=timezone.utc),
                    datetime.now(timezone.utc),
                    "renewal must install a new active lease expiry",
                )

                # The unchanged original ChatGPT result now submits.
                status, body = _post(port, f"/api/courses/{course_id}/jobs/{job_id}/result", result)
                self.assertEqual(status, 200, body)
                self.assertIn(json.loads(body)["status"], ("advanced", "blocked"))
            finally:
                self._stop_server(server, thread)

    def test_l2_t049_still_rejects_an_expired_lease_without_renewal(self) -> None:
        """L2: without the renewal the underlying T049 authority still
        refuses an expired lease as stale_revision. This proves the repair
        lives in the browser relay and did not weaken T049."""

        holder = "holderlatency002"
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                course_id, job_id, original = self._reach_source_assessment(port, holder, "l2")
                result = self._source_result(original, holder)
                _age_lease(tmp, original["request_id"], seconds=_LEASE_SECONDS + 60)

                status, body = _post(port, f"/api/courses/{course_id}/jobs/{job_id}/result", result)
                self.assertEqual(status, 409)
                # L6: the exact diagnostic survives the T051 transport.
                self.assertEqual(json.loads(body), {"error": "conflict", "diagnostics": ["stale_revision"]})

                # Nothing was accepted.
                row_status, _, _ = _lease_columns(tmp, original["request_id"])
                self.assertEqual(row_status, "leased")

                # And the documented repair recovers the very same result.
                status, _ = _post(port, f"/api/jobs/{job_id}/semantic-request", {"holder_id": holder})
                self.assertEqual(status, 200)
                status, body = _post(port, f"/api/courses/{course_id}/jobs/{job_id}/result", result)
                self.assertEqual(status, 200, body)
            finally:
                self._stop_server(server, thread)

    def test_l3_a_different_holder_cannot_renew_and_mutates_nothing(self) -> None:
        """L3: while H1 holds an active lease, a browser using H2 gets 409
        lease_conflict from the pre-submit renewal and performs zero result
        mutation. L7: the exact lease_conflict diagnostic is preserved."""

        holder_one = "holderlatency031"
        holder_two = "holderlatency032"
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                course_id, job_id, original = self._reach_source_assessment(port, holder_one, "l3")
                revision_before = self._workflow_revision(port, job_id)

                status, body = _post(port, f"/api/jobs/{job_id}/semantic-request", {"holder_id": holder_two})
                self.assertEqual(status, 409)
                self.assertEqual(json.loads(body), {"error": "conflict", "diagnostics": ["lease_conflict"]})

                # The lease is untouched and still belongs to H1.
                row_status, _, held_by = _lease_columns(tmp, original["request_id"])
                self.assertEqual(row_status, "leased")
                self.assertEqual(held_by, holder_one)
                self.assertEqual(self._workflow_revision(port, job_id), revision_before)

                # A foreign-holder result is refused too: the browser's
                # local refusal is a fail-fast, not the only guard.
                status, body = _post(
                    port,
                    f"/api/courses/{course_id}/jobs/{job_id}/result",
                    self._source_result(original, holder_two),
                )
                self.assertEqual(status, 409)
                self.assertEqual(json.loads(body), {"error": "conflict", "diagnostics": ["lease_conflict"]})
                self.assertEqual(self._workflow_revision(port, job_id), revision_before)
                row_status, _, _ = _lease_columns(tmp, original["request_id"])
                self.assertEqual(row_status, "leased")
            finally:
                self._stop_server(server, thread)


    def test_l5_renewal_returning_a_different_request_is_a_refusable_identity_change(self) -> None:
        """L5: authority moved to the next semantic kind, so the renewal
        succeeds but returns a genuinely different request. Every identity
        field the browser compares differs, and the old result must never be
        rebound to the new authority."""

        holder = "holderlatency005"
        with _isolated_tmp_dir() as tmp:
            server, port, thread = self._start_server(tmp)
            try:
                course_id, job_id, original = self._reach_source_assessment(port, holder, "l5")
                old_result = self._source_result(original, holder)
                status, _ = _post(port, f"/api/courses/{course_id}/jobs/{job_id}/result", old_result)
                self.assertEqual(status, 200)

                status, body = _post(port, f"/api/jobs/{job_id}/semantic-request", {"holder_id": holder})
                self.assertEqual(status, 200)
                renewed = json.loads(body)["request"]

                # Exactly the identity fields the browser compares changed.
                self.assertNotEqual(renewed["request_id"], original["request_id"])
                self.assertNotEqual(renewed["operation_id"], original["operation_id"])
                self.assertNotEqual(renewed["kind"], original["kind"])
                self.assertNotEqual(renewed["expected_revision"], original["expected_revision"])
                self.assertNotEqual(renewed["input_refs"], original["input_refs"])
                # Job/workflow identity is stable; only the work moved on.
                self.assertEqual(renewed["job_id"], original["job_id"])
                self.assertEqual(renewed["workflow_id"], original["workflow_id"])

                revision_before = self._workflow_revision(port, job_id)
                status, _ = _post(port, f"/api/courses/{course_id}/jobs/{job_id}/result", old_result)
                self.assertNotEqual(status, 201)
                self.assertEqual(
                    self._workflow_revision(port, job_id),
                    revision_before,
                    "an old model result must never be rebound to new authority",
                )
            finally:
                self._stop_server(server, thread)






if __name__ == "__main__":
    unittest.main()
