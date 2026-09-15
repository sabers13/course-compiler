"""Local HTTP server for the Course Compiler application (T048).

T047 shell + T048 durable Course/Job ownership: loopback-only, safe 404,
no path traversal, content-safe JSON, no private leakage.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, unquote

from .config import AppConfig, AppConfigError, create_config, default_config
from .context import AppContext, create_app_context

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_INDEX_FILE = _STATIC_DIR / "index.html"

# Browser-owned JSON transport carries up to 256 KiB of semantic text.
# Bound the encoded request before buffering; the model never serializes it.
_RELAY_RESULT_MAX_BYTES = 2 * 1024 * 1024

# Attached source content is capped at 64 MiB decoded (see _handle_attach_source).
# The JSON request-body reader must accept at least base64(64 MiB) plus a small
# envelope margin for "source_id"/"content_base64" JSON structure, or a real
# source at or near the decoded cap is rejected before decoding is ever reached.
_SOURCE_ATTACH_MAX_CONTENT_BYTES = 64 * 1024 * 1024
_SOURCE_ATTACH_MAX_REQUEST_BYTES = ((_SOURCE_ATTACH_MAX_CONTENT_BYTES + 2) // 3) * 4 + (1 * 1024 * 1024)

# T048 imports for real routes (loaded lazily inside handlers to keep module_graph narrow)
try:
    from course_compiler.course_job_persistence import open_course_job_store
    from course_compiler.course_operations import (
        attach_source as _attach_source,
    )
    from course_compiler.course_operations import (
        create_course as _create_course,
    )
    from course_compiler.course_operations import (
        get_course as _get_course,
    )
    from course_compiler.course_operations import (
        get_job_status as _get_job_status,
    )
    from course_compiler.course_operations import (
        list_courses as _list_courses,
    )
    from course_compiler.course_persistence import open_course_store
    from course_compiler.course_workflow_persistence import (
        open_course_workflow_association_store,
    )
    from course_compiler.source_persistence import open_source_evidence_store
    from course_compiler.workflow_persistence import open_workflow_state_store
except Exception:  # pragma: no cover
    open_course_store = None  # type: ignore[assignment]
    open_course_job_store = None  # type: ignore[assignment]
    _create_course = None  # type: ignore[assignment]
    _list_courses = None  # type: ignore[assignment]
    _get_course = None  # type: ignore[assignment]
    _attach_source = None  # type: ignore[assignment]
    _get_job_status = None  # type: ignore[assignment]
    open_course_workflow_association_store = None  # type: ignore[assignment]
    open_source_evidence_store = None  # type: ignore[assignment]
    open_workflow_state_store = None  # type: ignore[assignment]

# T049 semantic-work imports (lazy, narrow)
try:
    from course_compiler.lecture_document_persistence import open_lecture_document_store as _open_lecture_document_store
    from course_compiler.policy_persistence import open_policy_content_store as _open_policy_content_store
    from course_compiler.semantic_operations import (
        observe_owner_decision_gate as _observe_owner_decision_gate,
    )
    from course_compiler.semantic_operations import (
        observe_pending_work as _observe_pending_work,
    )
    from course_compiler.semantic_operations import (
        request_semantic_work as _request_semantic_work,
    )
    from course_compiler.semantic_operations import (
        start_generation as _start_generation,
    )
    from course_compiler.semantic_operations import (
        submit_owner_map_decision as _submit_owner_map_decision,
    )
    from course_compiler.semantic_operations import (
        submit_owner_priority_decision as _submit_owner_priority_decision,
    )
    from course_compiler.semantic_operations import (
        submit_semantic_result as _submit_semantic_result,
    )
    from course_compiler.semantic_work_persistence import open_semantic_work_store as _open_semantic_work_store
    from course_compiler.workflow_artifact_persistence import open_workflow_artifact_store as _open_workflow_artifact_store
except Exception:  # pragma: no cover
    _start_generation = None  # type: ignore[assignment]
    _observe_pending_work = None  # type: ignore[assignment]
    _observe_owner_decision_gate = None  # type: ignore[assignment]
    _request_semantic_work = None  # type: ignore[assignment]
    _submit_owner_map_decision = None  # type: ignore[assignment]
    _submit_owner_priority_decision = None  # type: ignore[assignment]
    _submit_semantic_result = None  # type: ignore[assignment]
    _open_semantic_work_store = None  # type: ignore[assignment]
    _open_lecture_document_store = None  # type: ignore[assignment]
    _open_policy_content_store = None  # type: ignore[assignment]
    _open_workflow_artifact_store = None  # type: ignore[assignment]

# T050 build ownership & artifact retrieval imports (lazy, narrow)
try:
    from course_compiler.build_persistence import open_build_record_store as _open_build_record_store
    from course_compiler.build_operations import (
        build_pdf as _build_pdf,
        get_artifact as _get_artifact,
        get_artifact_history as _get_artifact_history,
        preview_source_pdf_page as _preview_source_pdf_page,
        extract_source_pdf_region as _extract_source_pdf_region,
    )
except Exception:  # pragma: no cover
    _open_build_record_store = None  # type: ignore[assignment]
    _build_pdf = None  # type: ignore[assignment]
    _get_artifact = None  # type: ignore[assignment]
    _get_artifact_history = None  # type: ignore[assignment]
    _preview_source_pdf_page = None  # type: ignore[assignment]
    _extract_source_pdf_region = None  # type: ignore[assignment]

# T051 GPT low-turn relay imports (lazy, narrow)
try:
    from course_compiler.course_workflow import (
        COURSE_WORKFLOW_ASSOCIATION_VERSION as _COURSE_WORKFLOW_ASSOCIATION_VERSION,
    )
    from course_compiler.course_workflow import CourseWorkflowAssociation as _CourseWorkflowAssociation
    from course_compiler.course_workflow_operations import (
        reopen_course_workflow_continuation as _reopen_course_workflow_continuation,
    )
    from course_compiler.course_workflow_operations import (
        reopen_course_workflow_context as _reopen_course_workflow_context,
    )
    from course_compiler.evidence_access import (
        EvidenceAccessFailure as _EvidenceAccessFailure,
    )
    from course_compiler.evidence_access import (
        read_evidence_item as _read_evidence_item,
    )
    from course_compiler.evidence_access import (
        resolve_request_evidence as _resolve_request_evidence,
    )
    from course_compiler.providers.chatgpt_relay import build_semantic_prompt, bind_semantic_text
except Exception:  # pragma: no cover
    _CourseWorkflowAssociation = None  # type: ignore[assignment]
    _COURSE_WORKFLOW_ASSOCIATION_VERSION = None  # type: ignore[assignment]
    _reopen_course_workflow_context = None  # type: ignore[assignment]
    _reopen_course_workflow_continuation = None  # type: ignore[assignment]
    _EvidenceAccessFailure = None  # type: ignore[assignment]
    _resolve_request_evidence = None  # type: ignore[assignment]
    _read_evidence_item = None  # type: ignore[assignment]

# A deliberate fail-closed refusal is a conflict about durable authority, not a
# server fault: reporting it as 500 would hide an identity/state decision.
_BUILD_CONFLICT_CODES = frozenset(
    {
        "workflow_not_completed",
        "semantic_review_pending",
        "review_corrections_pending",
        "accepted_documents_unavailable",
        "build_not_succeeded",
        "build_identity_mismatch",
        "job_build_identity_mismatch",
        "artifact_integrity_mismatch",
        "ephemeral_visual_inputs_rejected",
        "cache_path_traversal_rejected",
    }
)


def _json_bytes(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _safe_join_static(request_path: str) -> Path | None:
    # request_path is like "app.css" or "static/app.css" or "../secret"
    # Called with already-unquoted path without leading "/"
    # Reject absolute, traversal, empty, and separator tricks.
    if not request_path:
        return None
    # Normalize - reject any ".." component or absolute or empty segment
    try:
        # Use PurePosixPath-style split
        parts = request_path.split("/")
    except Exception:
        return None
    if any(part in ("", ".", "..") for part in parts):
        # Allow empty only for trailing slash? we already split; reject empty.
        # But "a/b" -> ["a","b"] no empty; "a//b" would have empty.
        return None
    # Reject if any part contains backslash or is absolute
    if "\\" in request_path:
        return None
    candidate = _STATIC_DIR / Path(*parts)
    try:
        resolved_static = _STATIC_DIR.resolve()
        resolved_candidate = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    try:
        resolved_candidate.relative_to(resolved_static)
    except ValueError:
        return None
    return resolved_candidate


class CourseCompilerHandler(BaseHTTPRequestHandler):
    """Request handler for the application shell."""

    # Class-level configuration injected by factory
    app_config: AppConfig | None = None
    app_context: AppContext | None = None

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        # Content-safe: do not log request bodies or private content.
        # Keep minimal: method and path only, no headers.
        # Suppress by default to keep output clean; could emit to stderr
        # with redacted info if needed.
        return

    def _send_json(self, code: int, payload: object) -> None:
        body = _json_bytes(payload)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

    def _send_html(self, code: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

    def _send_static_file(self, file_path: Path) -> None:
        try:
            if not file_path.is_file():
                self._send_not_found(is_api=False)
                return
            body = file_path.read_bytes()
        except OSError:
            self._send_not_found(is_api=False)
            return
        mime, _ = mimetypes.guess_type(str(file_path))
        if mime is None:
            mime = "application/octet-stream"
            if file_path.suffix == ".js":
                mime = "application/javascript; charset=utf-8"
            elif file_path.suffix == ".css":
                mime = "text/css; charset=utf-8"
            elif file_path.suffix == ".html":
                mime = "text/html; charset=utf-8"
        # Ensure charset for text types
        if mime.startswith("text/") and "charset" not in mime:
            mime += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

    def _send_binary(self, code: int, body: bytes, content_type: str, filename: str | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename is not None:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

    def _send_not_found(self, *, is_api: bool) -> None:
        if is_api:
            self._send_json(404, {"error": "not_found"})
        else:
            html = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Not found</title></head><body><h1>Not found</h1><p>The requested resource was not found.</p></body></html>"""
            self._send_html(404, html)

    def _read_json_body(self, *, max_bytes: int = 65536) -> tuple[dict | None, str | None]:
        length = self.headers.get("Content-Length")
        if length is None:
            return None, "missing_length"
        try:
            n = int(length)
        except ValueError:
            return None, "invalid_length"
        if n < 0 or n > max_bytes:
            return None, "too_large"
        try:
            raw = self.rfile.read(n) if n > 0 else b""
        except Exception:
            return None, "read_failed"
        if len(raw) != n:
            return None, "read_failed"
        if n == 0:
            return None, "empty"
        try:
            text = raw.decode("utf-8")
            data = json.loads(text)
        except Exception:
            return None, "invalid_json"
        if type(data) is not dict:
            return None, "invalid_shape"
        return data, None

    def _course_to_api(self, record: object) -> dict:
        try:
            return {
                "course_id": record.course_id,  # type: ignore[attr-defined]
                "reference_version": record.reference_version,  # type: ignore[attr-defined]
                "created_at": record.created_at,  # type: ignore[attr-defined]
                "title": record.title,  # type: ignore[attr-defined]
                "ai_mode": record.ai_mode,  # type: ignore[attr-defined]
                "quality_mode": record.quality_mode,  # type: ignore[attr-defined]
                "owner_scope": record.owner_scope,  # type: ignore[attr-defined]
                "metadata_revision": record.metadata_revision,  # type: ignore[attr-defined]
                "source_count": len(record.source_refs),  # type: ignore[attr-defined]
                "workflow_id": record.workflow_id,  # type: ignore[attr-defined]
                "current_job_id": record.current_job_id,  # type: ignore[attr-defined]
                "course_guidance": record.course_guidance,
            }
        except Exception:
            return {}

    def _handle_guidance(self, course_id):
        from course_compiler.course_operations import update_course_metadata, CourseOperationFailure
        data, err = self._read_json_body(max_bytes=65536)
        if err or set(data) != {"course_guidance", "expected_revision"}:
            self._send_json(400, {"error": "invalid_input"})
            return
        store = open_course_store(self.server.context.database_path("courses.sqlite3"))
        workflow = open_workflow_state_store(self.server.context.database_path("workflow-state.sqlite3"))
        try:
            result = update_course_metadata(course_id, data["expected_revision"], course_store=store,
                workflow_store=workflow, course_guidance=data["course_guidance"])
            if isinstance(result, CourseOperationFailure):
                self._send_json(409, {"error": result.diagnostics[0].code})
            else:
                self._send_json(200, {"course": self._course_to_api(result)})
        except (TypeError, ValueError):
            self._send_json(400, {"error": "invalid_input"})
        finally:
            store.close()
            workflow.close()

    def _handle_api_courses(self) -> None:
        # Lazy import guard
        if _list_courses is None or open_course_store is None:
            self._send_json(200, {"courses": []})
            return
        ctx = getattr(self.server, "context", None) or getattr(self, "app_context", None)
        if ctx is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        # For HEAD/GET on empty DB, avoid creating file to keep T047 shell invariant for fresh data_root
        if self.command in ("GET", "HEAD"):
            try:
                path = ctx.database_path("courses.sqlite3")
                if not path.is_file():
                    self._send_json(200, {"courses": []})
                    return
            except Exception:
                pass
        # Per-request store (no leak)
        try:
            path = ctx.database_path("courses.sqlite3")
            store = open_course_store(path)
        except Exception:
            self._send_json(500, {"error": "store_unavailable"})
            return
        # Check for persistence failure object
        try:
            from course_compiler.course_persistence import CoursePersistenceFailure

            if isinstance(store, CoursePersistenceFailure):
                self._send_json(500, {"error": "store_unavailable"})
                return
        except Exception:
            pass
        try:
            if self.command not in ("GET", "HEAD"):
                if self.command == "POST":
                    self._handle_create_course(ctx, store)
                    return
                self._send_json(405, {"error": "method_not_allowed"})
                return
            result = _list_courses(course_store=store)  # type: ignore[misc]
            from course_compiler.course_operations import CourseOperationFailure
            from course_compiler.course_persistence import CoursePersistenceFailure as CPF

            if isinstance(result, CourseOperationFailure) or isinstance(result, CPF):
                self._send_json(500, {"error": "store_unavailable"})
                return
            courses = [self._course_to_api(r) for r in result]  # type: ignore[arg-type]
            self._send_json(200, {"courses": courses})
        finally:
            try:
                store.close()  # type: ignore[attr-defined]
            except Exception:
                pass

    def _handle_create_course(self, ctx: AppContext, course_store: object) -> None:
        if _create_course is None or open_course_workflow_association_store is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        data, err = self._read_json_body()
        if err is not None:
            self._send_json(400, {"error": "invalid_input"})
            return
        # Validate shape strictly (extra="forbid" style: only allow known keys)
        allowed = {"title", "ai_mode", "quality_mode", "course_guidance"}
        if set(data.keys()) - allowed:
            self._send_json(400, {"error": "invalid_input"})
            return
        if "title" not in data or "ai_mode" not in data or "quality_mode" not in data:
            self._send_json(400, {"error": "invalid_input"})
            return
        title = data["title"]
        ai_mode = data["ai_mode"]
        quality_mode = data["quality_mode"]
        if type(title) is not str or type(ai_mode) is not str or type(quality_mode) is not str:
            self._send_json(400, {"error": "invalid_input"})
            return
        # Open association store per-request
        assoc_store = None
        try:
            assoc_path = ctx.database_path("course-workflow-associations.sqlite3")
            assoc_store = open_course_workflow_association_store(assoc_path)
            from course_compiler.course_workflow_persistence import CourseWorkflowPersistenceFailure

            if isinstance(assoc_store, CourseWorkflowPersistenceFailure):
                self._send_json(500, {"error": "store_unavailable"})
                return
            result = _create_course(  # type: ignore[misc]
                title,
                course_guidance=data.get("course_guidance", ""),
                ai_mode=ai_mode,
                quality_mode=quality_mode,
                course_store=course_store,
                association_store=assoc_store,
            )
            from course_compiler.course_operations import CourseOperationFailure as COF

            if isinstance(result, COF):
                code = result.diagnostics[0].code if result.diagnostics else ""
                if code == "invalid_course_input":
                    self._send_json(400, {"error": "invalid_input"})
                else:
                    self._send_json(500, {"error": "store_unavailable"})
                return
            # Frozen contract returns CourseReference; fetch full record for UI representation.
            try:
                course_id = result.course_id  # type: ignore[attr-defined]
            except Exception:
                self._send_json(500, {"error": "store_unavailable"})
                return
            fetched = _get_course(course_id, course_store=course_store)  # type: ignore[misc]
            if isinstance(fetched, COF):
                self._send_json(500, {"error": "store_unavailable"})
                return
            # fetched is CourseRecord
            self._send_json(201, {"course": self._course_to_api(fetched)})
            return
        finally:
            if assoc_store is not None:
                try:
                    assoc_store.close()  # type: ignore[attr-defined]
                except Exception:
                    pass

    def _handle_api_course_detail(self, course_id: str) -> None:
        if _get_course is None or open_course_store is None:
            self._send_json(404, {"error": "not_found"})
            return
        if self.command not in ("GET", "HEAD"):
            # Check for sources sub-route is handled separately
            self._send_json(405, {"error": "method_not_allowed"})
            return
        ctx = getattr(self.server, "context", None) or getattr(self, "app_context", None)
        if ctx is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        try:
            path = ctx.database_path("courses.sqlite3")
            store = open_course_store(path)
        except Exception:
            self._send_json(500, {"error": "store_unavailable"})
            return
        try:
            from course_compiler.course_persistence import CoursePersistenceFailure

            if isinstance(store, CoursePersistenceFailure):
                self._send_json(500, {"error": "store_unavailable"})
                return
            result = _get_course(course_id, course_store=store)  # type: ignore[misc]
            from course_compiler.course_operations import CourseOperationFailure as COF
            from course_compiler.course_persistence import CourseRecord as CR

            if isinstance(result, COF):
                code = result.diagnostics[0].code if result.diagnostics else ""
                if code == "course_not_found":
                    self._send_json(404, {"error": "not_found"})
                elif code == "invalid_course_input":
                    self._send_json(400, {"error": "invalid_input"})
                else:
                    self._send_json(500, {"error": "store_unavailable"})
                return
            if type(result) is CR or hasattr(result, "course_id"):
                self._send_json(200, {"course": self._course_to_api(result)})
                return
            self._send_json(500, {"error": "store_unavailable"})
        finally:
            try:
                store.close()  # type: ignore[attr-defined]
            except Exception:
                pass

    def _handle_attach_source(self, course_id: str) -> None:
        if _attach_source is None or open_course_store is None or open_source_evidence_store is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        if self.command != "POST":
            self._send_json(405, {"error": "method_not_allowed"})
            return
        data, err = self._read_json_body(max_bytes=_SOURCE_ATTACH_MAX_REQUEST_BYTES)
        if err is not None:
            self._send_json(400, {"error": "invalid_input"})
            return
        allowed = {"source_id", "content_base64"}
        if set(data.keys()) - allowed:
            self._send_json(400, {"error": "invalid_input"})
            return
        if "source_id" not in data or "content_base64" not in data:
            self._send_json(400, {"error": "invalid_input"})
            return
        source_id = data["source_id"]
        content_b64 = data["content_base64"]
        if type(source_id) is not str or type(content_b64) is not str:
            self._send_json(400, {"error": "invalid_input"})
            return
        try:
            content_bytes = base64.b64decode(content_b64, validate=True)
        except Exception:
            self._send_json(400, {"error": "invalid_input"})
            return
        # Size limit delegated to T018/T008 but add basic cap 64 MiB
        if len(content_bytes) > _SOURCE_ATTACH_MAX_CONTENT_BYTES:
            self._send_json(400, {"error": "invalid_input"})
            return
        ctx = getattr(self.server, "context", None) or getattr(self, "app_context", None)
        if ctx is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        course_store = None
        source_store = None
        try:
            c_path = ctx.database_path("courses.sqlite3")
            s_path = ctx.database_path("source-evidence.sqlite3")
            course_store = open_course_store(c_path)
            source_store = open_source_evidence_store(s_path)
            from course_compiler.course_persistence import CoursePersistenceFailure
            from course_compiler.source_persistence import SourcePersistenceFailure

            if isinstance(course_store, CoursePersistenceFailure) or isinstance(source_store, SourcePersistenceFailure):
                self._send_json(500, {"error": "store_unavailable"})
                return
            result = _attach_source(  # type: ignore[misc]
                course_id, content_bytes, source_id, course_store=course_store, source_store=source_store
            )
            from course_compiler.course_operations import CourseOperationFailure as COF

            if isinstance(result, COF):
                code = result.diagnostics[0].code if result.diagnostics else ""
                if code == "invalid_course_input":
                    self._send_json(400, {"error": "invalid_input"})
                elif code == "course_not_found":
                    self._send_json(404, {"error": "not_found"})
                elif code == "source_identity_conflict":
                    self._send_json(409, {"error": "conflict"})
                elif code == "stale_revision":
                    self._send_json(409, {"error": "conflict"})
                else:
                    self._send_json(500, {"error": "store_unavailable"})
                return
            # Success: result is SourceEvidenceReference
            try:
                payload = {
                    "source_id": result.source_id,  # type: ignore[attr-defined]
                    "content_sha256": result.content_sha256,  # type: ignore[attr-defined]
                    "reference_version": result.reference_version,  # type: ignore[attr-defined]
                }
            except Exception:
                self._send_json(500, {"error": "store_unavailable"})
                return
            self._send_json(201, payload)
        finally:
            for h in (course_store, source_store):
                if h is not None:
                    try:
                        h.close()  # type: ignore[attr-defined]
                    except Exception:
                        pass

    def _handle_api_job_status(self, job_id: str) -> None:
        if _get_job_status is None or open_course_job_store is None or open_workflow_state_store is None:
            self._send_json(404, {"error": "not_found"})
            return
        if self.command not in ("GET", "HEAD"):
            self._send_json(405, {"error": "method_not_allowed"})
            return
        ctx = getattr(self.server, "context", None) or getattr(self, "app_context", None)
        if ctx is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        job_store = None
        ws_store = None
        sem_store = build_store = None
        try:
            j_path = ctx.database_path("course-jobs.sqlite3")
            w_path = ctx.database_path("workflow-state.sqlite3")
            job_store = open_course_job_store(j_path)
            ws_store = open_workflow_state_store(w_path)
            from course_compiler.course_job_persistence import CourseJobPersistenceFailure
            from course_compiler.workflow_persistence import WorkflowPersistenceFailure

            if isinstance(job_store, CourseJobPersistenceFailure) or isinstance(ws_store, WorkflowPersistenceFailure):
                self._send_json(500, {"error": "store_unavailable"})
                return
            result = _get_job_status(job_id, job_store=job_store, workflow_store=ws_store)  # type: ignore[misc]
            from course_compiler.course_operations import CourseOperationFailure as COF
            from course_compiler.course_operations import JobStatusProjection as JSP

            if isinstance(result, COF):
                code = result.diagnostics[0].code if result.diagnostics else ""
                if code == "job_not_found":
                    self._send_json(404, {"error": "not_found"})
                elif code == "invalid_course_input":
                    self._send_json(400, {"error": "invalid_input"})
                else:
                    self._send_json(500, {"error": "store_unavailable"})
                return
            # JSP plus the one owner-decision projection the browser needs.
            # The subject is recomputed from the authoritative persisted
            # WorkflowState; the browser never derives or asks the owner to
            # copy a subject hash.
            try:
                owner_approval = None
                content_review = None
                build_checks = {"status": "pending", "diagnostics": []}
                workflow_state = ws_store.load(result.workflow_id)
                from course_compiler.workflow_persistence import WorkflowPersistenceFailure

                # An approval control may appear only once the semantic
                # evidence for that gate is genuinely accepted. A proposed
                # basis alone is not enough: priority_approval/
                # awaiting_approval reads identically while the
                # exam_priority_assessment turn is still pending, which
                # previously offered Approve/Reject before its evidence
                # existed. The gate observation is read-only and fails
                # closed, so an unopened gate projects nothing.
                gate = None
                from course_compiler.semantic_work_persistence import SemanticWorkPersistenceFailure

                if _open_semantic_work_store is not None:
                    sem_store = _open_semantic_work_store(ctx.database_path("semantic-work.sqlite3"))
                if not isinstance(workflow_state, WorkflowPersistenceFailure) and _observe_owner_decision_gate is not None:
                    if not isinstance(sem_store, SemanticWorkPersistenceFailure):
                        observed = _observe_owner_decision_gate(  # type: ignore[misc]
                            job_id, job_store=job_store, workflow_store=ws_store, semantic_store=sem_store,
                        )
                        if type(observed) is str:
                            gate = observed

                if not isinstance(workflow_state, WorkflowPersistenceFailure):
                    job_record = job_store.load(job_id)
                    if getattr(job_record, "quality_mode", None) == "fast":
                        content_review = {"status": "not_applicable", "outstanding_lectures": []}
                    elif sem_store is not None and not isinstance(sem_store, SemanticWorkPersistenceFailure):
                        from course_compiler.semantic_operations import derive_review_progress

                        review = derive_review_progress(workflow_state, job_record, sem_store)
                        if type(review) is str:
                            content_review = {
                                "status": review,
                                "outstanding_lectures": [
                                    item.lecture_id for item in workflow_state.lecture_progress
                                    if item.status == "correction_required"
                                ],
                            }

                if _open_build_record_store is not None:
                    build_store = _open_build_record_store(ctx.database_path("build-records.sqlite3"))
                    latest = build_store.latest_successful_for_job(job_id)
                    from course_compiler.build_persistence import BuildRecord
                    if isinstance(latest, BuildRecord):
                        build_checks = {
                            "status": latest.status,
                            "diagnostics": [
                                {"code": item.code, "classification": item.classification, "message": item.message}
                                for item in latest.diagnostics
                            ],
                        }

                if not isinstance(workflow_state, WorkflowPersistenceFailure):
                    from course_compiler.workflow import map_subject_sha256, priority_subject_sha256

                    if (
                        gate == "priority"
                        and workflow_state.priority_basis is not None
                        and workflow_state.priority_basis.status == "proposed"
                    ):
                        owner_approval = {
                            "kind": "priority",
                            "subject_sha256": priority_subject_sha256(
                                workflow_state.priority_basis, workflow_state.policies.priority_basis
                            ),
                        }
                    elif (
                        gate == "map"
                        and workflow_state.lecture_map is not None
                        and workflow_state.lecture_map.status == "proposed"
                    ):
                        owner_approval = {
                            "kind": "map",
                            "subject_sha256": map_subject_sha256(
                                workflow_state.lecture_map, workflow_state.policies.lecture_mapping
                            ),
                        }
                if owner_approval is not None:
                    artifact_store = _open_workflow_artifact_store(ctx.database_path("workflow-artifacts.sqlite3"))
                    try:
                        refs = ([workflow_state.lecture_map.map_reference] if gate == "map" else
                            [workflow_state.priority_basis.proposal_reference, workflow_state.priority_basis.evidence_hierarchy_reference])
                        texts = []
                        for ref in refs:
                            item = artifact_store.load(ref)
                            texts.append(item.payload.decode("utf-8"))
                        if gate == "priority":
                            from course_compiler.semantic_work_persistence import SemanticAcceptedRecord
                            from course_compiler.workflow import WorkflowArtifactReference
                            from course_compiler.workflow_policy import ARTIFACT_PRODUCERS
                            reviews = sem_store.load_all_for_job(job_id)
                            for review in reviews:
                                if (isinstance(review, SemanticAcceptedRecord) and review.kind == "exam_priority_assessment"
                                    and review.request_revision == workflow_state.revision):
                                    ref = review.artifact_refs[0]
                                    artifact_ref = WorkflowArtifactReference("workflow-artifact-reference/v1",
                                        ref.artifact_id, ref.kind, ref.content_sha256, ARTIFACT_PRODUCERS[ref.kind])
                                    texts.append("# Priority evidence review\n" + artifact_store.load(artifact_ref).payload.decode("utf-8"))
                        owner_approval["text"] = "\n\n".join(texts)
                    finally:
                        artifact_store.close()
                payload = {
                    "job_id": result.job_id,  # type: ignore[attr-defined]
                    "course_id": result.course_reference.course_id,  # type: ignore[attr-defined]
                    "workflow_id": result.workflow_id,  # type: ignore[attr-defined]
                    "status": result.status,  # type: ignore[attr-defined]
                    "current_revision": result.current_revision,  # type: ignore[attr-defined]
                    "current_stage": result.current_stage,  # type: ignore[attr-defined]
                    "current_disposition": result.current_disposition,  # type: ignore[attr-defined]
                    "failure_code": result.failure_code,  # type: ignore[attr-defined]
                    "completed_build_id": result.completed_build_id,  # type: ignore[attr-defined]
                    "completed_build_sha256": result.completed_build_sha256,  # type: ignore[attr-defined]
                    "owner_approval": owner_approval,
                    "content_review": content_review,
                    "review": content_review,
                    "build_checks": build_checks,
                }
            except Exception:
                self._send_json(500, {"error": "store_unavailable"})
                return
            self._send_json(200, payload)
        finally:
            for h in (job_store, ws_store, sem_store, build_store):
                if h is not None:
                    try:
                        h.close()  # type: ignore[attr-defined]
                    except Exception:
                        pass

    def _handle_start_generation(self, course_id: str) -> None:
        if self.command != "POST":
            self._send_json(405, {"error": "method_not_allowed"})
            return
        if _start_generation is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        # Validate course_id shape
        if not course_id or "/" in course_id or "\\" in course_id or ".." in course_id:
            self._send_json(400, {"error": "invalid_input"})
            return
        ctx = getattr(self.server, "context", None) or getattr(self, "app_context", None)
        if ctx is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        # Open all required stores per request (no leak)
        course_store = job_store = ws_store = assoc_store = source_store = policy_store = artifact_store = doc_store = None
        try:
            try:
                c_path = ctx.database_path("courses.sqlite3")
                j_path = ctx.database_path("course-jobs.sqlite3")
                w_path = ctx.database_path("workflow-state.sqlite3")
                a_path = ctx.database_path("course-workflow-associations.sqlite3")
                s_path = ctx.database_path("source-evidence.sqlite3")
                p_path = ctx.database_path("policy-content.sqlite3")
                art_path = ctx.database_path("workflow-artifacts.sqlite3")
                d_path = ctx.database_path("lecture-documents.sqlite3")
                # Use lazy openers that may be None
                if open_course_store is None or open_course_job_store is None or open_workflow_state_store is None or open_course_workflow_association_store is None or open_source_evidence_store is None:
                    self._send_json(500, {"error": "store_unavailable"})
                    return
                course_store = open_course_store(c_path)
                job_store = open_course_job_store(j_path)
                ws_store = open_workflow_state_store(w_path)
                assoc_store = open_course_workflow_association_store(a_path)
                source_store = open_source_evidence_store(s_path)
                # Policy/artifact/document stores via T049 lazy openers
                if _open_policy_content_store is None or _open_workflow_artifact_store is None or _open_lecture_document_store is None:
                    self._send_json(500, {"error": "store_unavailable"})
                    return
                policy_store = _open_policy_content_store(p_path)
                artifact_store = _open_workflow_artifact_store(art_path)
                doc_store = _open_lecture_document_store(d_path)
            except Exception:
                self._send_json(500, {"error": "store_unavailable"})
                return
            # Check for failure objects
            for h in (course_store, job_store, ws_store, assoc_store, source_store, policy_store, artifact_store, doc_store):
                try:
                    from course_compiler.course_persistence import CoursePersistenceFailure
                    from course_compiler.course_job_persistence import CourseJobPersistenceFailure
                    from course_compiler.workflow_persistence import WorkflowPersistenceFailure
                    from course_compiler.course_workflow_persistence import CourseWorkflowPersistenceFailure
                    from course_compiler.source_persistence import SourcePersistenceFailure
                    from course_compiler.policy_persistence import PolicyPersistenceFailure
                    from course_compiler.workflow_artifact_persistence import WorkflowArtifactPersistenceFailure
                    from course_compiler.lecture_document_persistence import LectureDocumentPersistenceFailure
                    if isinstance(h, (CoursePersistenceFailure, CourseJobPersistenceFailure, WorkflowPersistenceFailure, CourseWorkflowPersistenceFailure, SourcePersistenceFailure, PolicyPersistenceFailure, WorkflowArtifactPersistenceFailure, LectureDocumentPersistenceFailure)):
                        self._send_json(500, {"error": "store_unavailable"})
                        return
                except Exception:
                    pass
            # Read body if any (allow empty)
            length = self.headers.get("Content-Length")
            if length is not None:
                try:
                    n = int(length)
                    if n > 0:
                        # consume but ignore content (strict: must be valid JSON if present, else 400)
                        body = self.rfile.read(n) if n>0 else b""
                        if body:
                            try:
                                data = json.loads(body.decode("utf-8"))
                                if type(data) is not dict:
                                    self._send_json(400, {"error": "invalid_input"})
                                    return
                                # Only allow empty or no extra fields? For now allow empty dict
                                if data and set(data.keys()) - set():
                                    self._send_json(400, {"error": "invalid_input"})
                                    return
                            except Exception:
                                self._send_json(400, {"error": "invalid_input"})
                                return
                except Exception:
                    pass
            result = _start_generation(  # type: ignore[misc]
                course_id,
                course_store=course_store,
                job_store=job_store,
                workflow_store=ws_store,
                association_store=assoc_store,
                source_store=source_store,
                policy_store=policy_store,
                artifact_store=artifact_store,
                document_store=doc_store,
            )
            from course_compiler.semantic_operations import SemanticOperationFailure
            if isinstance(result, SemanticOperationFailure):
                code = result.diagnostics[0].code if result.diagnostics else ""
                if code in ("course_not_found", "job_not_found"):
                    self._send_json(404, {"error": "not_found"})
                elif code in ("invalid_semantic_input", "invalid_course_input"):
                    self._send_json(400, {"error": "invalid_input"})
                elif code in ("course_store_failed", "job_store_failed", "workflow_store_failed", "association_store_failed", "source_store_failed", "policy_store_failed"):
                    self._send_json(500, {"error": "store_unavailable"})
                else:
                    self._send_json(500, {"error": "store_unavailable"})
                return
            # Success: result is job_id string
            try:
                job_id = result  # type: ignore[assignment]
                # Fetch job to get workflow_id for response
                job_obj = job_store.load(job_id)  # type: ignore[attr-defined]
                workflow_id = getattr(job_obj, "workflow_id", "")
            except Exception:
                workflow_id = ""
            self._send_json(201, {"job_id": job_id, "workflow_id": workflow_id})
        finally:
            for h in (course_store, job_store, ws_store, assoc_store, source_store, policy_store, artifact_store, doc_store):
                if h is not None:
                    try:
                        h.close()  # type: ignore[attr-defined]
                    except Exception:
                        pass

    def _semantic_request_to_api(self, req: object) -> dict:
        try:
            return {
                "request_id": req.request_id,  # type: ignore[attr-defined]
                "request_version": req.request_version,  # type: ignore[attr-defined]
                "job_id": req.job_id,  # type: ignore[attr-defined]
                "workflow_id": req.workflow_id,  # type: ignore[attr-defined]
                "expected_revision": req.expected_revision,  # type: ignore[attr-defined]
                "operation_id": req.operation_id,  # type: ignore[attr-defined]
                "kind": req.kind,  # type: ignore[attr-defined]
                "input_refs": list(req.input_refs),  # type: ignore[attr-defined]
                "created_at": req.created_at,  # type: ignore[attr-defined]
                "lease_expires_at": req.lease_expires_at,  # type: ignore[attr-defined]
            }
        except Exception:
            return {}

    def _handle_semantic_request(self, job_id: str) -> None:
        # Lease acquisition is a mutation: only POST acquires/renews a lease.
        # GET and HEAD are observation-only and never touch lease state.
        if self.command not in ("GET", "POST", "HEAD"):
            self._send_json(405, {"error": "method_not_allowed"})
            return
        if _request_semantic_work is None or _observe_pending_work is None or _open_semantic_work_store is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        if not job_id or "/" in job_id or "\\" in job_id or ".." in job_id:
            self._send_json(400, {"error": "invalid_input"})
            return
        ctx = getattr(self.server, "context", None) or getattr(self, "app_context", None)
        if ctx is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        holder_id: str | None = None
        if self.command == "POST":
            # Bounded strict body: exactly {holder_id: SafeId}.
            data, err = self._read_json_body(max_bytes=4096)
            if err is not None:
                self._send_json(400, {"error": "invalid_input"})
                return
            if set(data.keys()) != {"holder_id"}:
                self._send_json(400, {"error": "invalid_input"})
                return
            if type(data["holder_id"]) is not str:
                self._send_json(400, {"error": "invalid_input"})
                return
            holder_id = data["holder_id"]
        elif self.command == "GET":
            # Observation must not carry a mutation body; drain nothing.
            pass
        job_store = ws_store = sem_store = course_store = None
        # POST is the mutation-authorized resume/acquisition boundary and may
        # perform the deterministic correction-reopen recovery if the durable
        # accepted verdict landed without the chained reopen transition.
        # GET/HEAD must stay observation-only and never open the recovery
        # stores, so the recovery cannot leak through read traffic.
        need_recovery_stores = self.command == "POST"
        assoc_store = source_store = pol_store = art_store = doc_store = None
        try:
            try:
                j_path = ctx.database_path("course-jobs.sqlite3")
                w_path = ctx.database_path("workflow-state.sqlite3")
                s_path = ctx.database_path("semantic-work.sqlite3")
                c_path = ctx.database_path("courses.sqlite3")
                if open_course_job_store is None or open_workflow_state_store is None or _open_semantic_work_store is None:
                    self._send_json(500, {"error": "store_unavailable"})
                    return
                job_store = open_course_job_store(j_path)
                ws_store = open_workflow_state_store(w_path)
                sem_store = _open_semantic_work_store(s_path)
                # course_store optional for reconciliation
                try:
                    if open_course_store is not None:
                        course_store = open_course_store(c_path)
                except Exception:
                    course_store = None
                if need_recovery_stores:
                    if (
                        open_course_workflow_association_store is None
                        or open_source_evidence_store is None
                        or _open_policy_content_store is None
                        or _open_workflow_artifact_store is None
                        or _open_lecture_document_store is None
                    ):
                        self._send_json(500, {"error": "store_unavailable"})
                        return
                    assoc_store = open_course_workflow_association_store(ctx.database_path("course-workflow-associations.sqlite3"))
                    source_store = open_source_evidence_store(ctx.database_path("source-evidence.sqlite3"))
                    pol_store = _open_policy_content_store(ctx.database_path("policy-content.sqlite3"))
                    art_store = _open_workflow_artifact_store(ctx.database_path("workflow-artifacts.sqlite3"))
                    doc_store = _open_lecture_document_store(ctx.database_path("lecture-documents.sqlite3"))
            except Exception:
                self._send_json(500, {"error": "store_unavailable"})
                return
            for h in (job_store, ws_store, sem_store, assoc_store, source_store, pol_store, art_store, doc_store):
                if h is None:
                    continue
                try:
                    from course_compiler.course_job_persistence import CourseJobPersistenceFailure
                    from course_compiler.workflow_persistence import WorkflowPersistenceFailure
                    from course_compiler.semantic_work_persistence import SemanticWorkPersistenceFailure
                    if isinstance(h, (CourseJobPersistenceFailure, WorkflowPersistenceFailure, SemanticWorkPersistenceFailure)):
                        self._send_json(500, {"error": "store_unavailable"})
                        return
                except Exception:
                    pass
            if self.command == "POST":
                if holder_id is None:
                    self._send_json(400, {"error": "invalid_input"})
                    return
                result = _request_semantic_work(  # type: ignore[misc]
                    job_id,
                    job_store=job_store,
                    workflow_store=ws_store,
                    semantic_store=sem_store,
                    association_store=assoc_store,
                    source_store=source_store,
                    policy_store=pol_store,
                    artifact_store=art_store,
                    document_store=doc_store,
                    course_store=course_store,
                    holder_id=holder_id,
                )
            else:
                # GET/HEAD observation: no lease acquisition, renewal, or theft.
                result = _observe_pending_work(  # type: ignore[misc]
                    job_id,
                    job_store=job_store,
                    workflow_store=ws_store,
                    semantic_store=sem_store,
                )
                if result is None:
                    self._send_json(200, {"status": "no_work", "reason": "no_semantic_work"})
                    return
            from course_compiler.semantic_operations import SemanticOperationFailure
            if isinstance(result, SemanticOperationFailure):
                code = result.diagnostics[0].code if result.diagnostics else ""
                if code == "job_not_found":
                    self._send_json(404, {"error": "not_found"})
                elif code in ("no_semantic_work", "owner_decision_required", "deterministic_action_pending"):
                    self._send_json(200, {"status": "no_work", "reason": code})
                elif code == "lease_conflict":
                    # The browser relay renews its own lease immediately
                    # before submitting; it must be able to distinguish a
                    # foreign active holder from a generic conflict. The
                    # code is a fixed content-safe T049 vocabulary term.
                    self._send_json(409, {"error": "conflict", "diagnostics": [code]})
                elif code in ("invalid_semantic_input",):
                    self._send_json(400, {"error": "invalid_input"})
                else:
                    self._send_json(500, {"error": "store_unavailable"})
                return
            # Success: SemanticWorkRequest
            self._send_json(200, {"request": self._semantic_request_to_api(result)})
        finally:
            for h in (job_store, ws_store, sem_store, course_store, assoc_store, source_store, pol_store, art_store, doc_store):
                if h is not None:
                    try:
                        h.close()  # type: ignore[attr-defined]
                    except Exception:
                        pass

    def _handle_semantic_result(self, job_id: str) -> None:
        """Internal deterministic-provider endpoint; never a model-facing schema."""
        from course_compiler.semantic_work import ProducedArtifactSpec, ProducedDocumentSpec, SemanticDiagnostic, SemanticWorkResult
        data, err = self._read_json_body()
        if err:
            self._send_json(400, {"error": "invalid_input"})
            return
        try:
            allowed = {"result_version", "request_id", "operation_id", "kind", "produced_artifacts", "produced_documents", "diagnostics", "priority_subject_sha256", "map_subject_sha256", "candidate_subject_sha256", "request_revision", "holder_id"}
            if set(data) - allowed:
                raise ValueError()
            result = SemanticWorkResult(data.get("result_version", "semantic-work-result/v1"), data["request_id"],
                data["operation_id"], data["kind"],
                tuple(ProducedArtifactSpec(a["kind"], a["artifact_id"], base64.b64decode(a["content_b64"], validate=True)) for a in data.get("produced_artifacts", [])),
                tuple(ProducedDocumentSpec(d["lecture_id"], d["source_text"]) for d in data.get("produced_documents", [])),
                tuple(SemanticDiagnostic(d["code"], d["message"]) for d in data.get("diagnostics", [])),
                data.get("priority_subject_sha256"), data.get("map_subject_sha256"), data.get("candidate_subject_sha256"), data["request_revision"])
        except Exception:
            self._send_json(400, {"error": "invalid_input"})
            return
        stores = self._open_t051_stores(self.server.context)
        if stores is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        try:
            self._submit_bound_result(job_id, result, data.get("holder_id"), stores)
        finally:
            for store in stores.values(): store.close()

    def _submit_bound_result(self, job_id, result, holder_id, stores):
        from course_compiler.semantic_operations import SemanticOperationFailure
        from course_compiler.workflow import WorkflowRejected
        outcome = _submit_semantic_result(job_id, result,
            job_store=stores["job"], workflow_store=stores["workflow"], semantic_store=stores["semantic"],
            association_store=stores["association"], source_store=stores["source"], policy_store=stores["policy"],
            artifact_store=stores["artifact"], document_store=stores["document"], holder_id=holder_id)
        if isinstance(outcome, (SemanticOperationFailure, WorkflowRejected)):
            codes = [d.code for d in outcome.diagnostics]
            status = 409 if any(c in ("stale_revision", "lease_conflict", "stale_workflow_state") for c in codes) else 400
            self._send_json(status, {"error": "conflict" if status == 409 else "invalid_input", "diagnostics": codes})
            return
        from course_compiler.semantic_work_persistence import SemanticAcceptedRecord
        if isinstance(outcome, SemanticAcceptedRecord):
            self._send_json(200, {"status": "accepted"})
            return
        self._send_json(200, {"status": outcome.status, "workflow_revision": outcome.state.revision,
            "stage": outcome.state.stage, "disposition": outcome.state.disposition})

    def _open_t051_stores(self, ctx: AppContext) -> dict | None:
        """Open every store the T051 pending-work/evidence handlers need."""

        if (
            open_course_job_store is None
            or open_workflow_state_store is None
            or _open_semantic_work_store is None
            or open_course_workflow_association_store is None
            or open_source_evidence_store is None
            or _open_policy_content_store is None
            or _open_workflow_artifact_store is None
            or _open_lecture_document_store is None
        ):
            return None
        try:
            stores = {
                "job": open_course_job_store(ctx.database_path("course-jobs.sqlite3")),
                "workflow": open_workflow_state_store(ctx.database_path("workflow-state.sqlite3")),
                "semantic": _open_semantic_work_store(ctx.database_path("semantic-work.sqlite3")),
                "association": open_course_workflow_association_store(
                    ctx.database_path("course-workflow-associations.sqlite3")
                ),
                "source": open_source_evidence_store(ctx.database_path("source-evidence.sqlite3")),
                "policy": _open_policy_content_store(ctx.database_path("policy-content.sqlite3")),
                "artifact": _open_workflow_artifact_store(ctx.database_path("workflow-artifacts.sqlite3")),
                "document": _open_lecture_document_store(ctx.database_path("lecture-documents.sqlite3")),
            }
        except Exception:
            return None
        from course_compiler.course_job_persistence import CourseJobPersistenceFailure
        from course_compiler.course_workflow_persistence import CourseWorkflowPersistenceFailure
        from course_compiler.lecture_document_persistence import LectureDocumentPersistenceFailure
        from course_compiler.policy_persistence import PolicyPersistenceFailure
        from course_compiler.semantic_work_persistence import SemanticWorkPersistenceFailure
        from course_compiler.source_persistence import SourcePersistenceFailure
        from course_compiler.workflow_artifact_persistence import WorkflowArtifactPersistenceFailure
        from course_compiler.workflow_persistence import WorkflowPersistenceFailure

        failure_types = (
            CourseJobPersistenceFailure,
            WorkflowPersistenceFailure,
            SemanticWorkPersistenceFailure,
            CourseWorkflowPersistenceFailure,
            SourcePersistenceFailure,
            PolicyPersistenceFailure,
            WorkflowArtifactPersistenceFailure,
            LectureDocumentPersistenceFailure,
        )
        if any(isinstance(handle, failure_types) for handle in stores.values()):
            for handle in stores.values():
                if handle is not None and not isinstance(handle, failure_types):
                    try:
                        handle.close()  # type: ignore[attr-defined]
                    except Exception:
                        pass
            return None
        return stores

    def _reopen_t051_continuation(self, job: object, stores: dict) -> object:
        """Reopen the T030 continuation for one exact known Job; a fixed failure sentinel on any drift."""

        association = _CourseWorkflowAssociation(
            _COURSE_WORKFLOW_ASSOCIATION_VERSION,
            job.course_reference,  # type: ignore[attr-defined]
            job.workflow_id,  # type: ignore[attr-defined]
        )
        context = _reopen_course_workflow_context(
            association,
            association_store=stores["association"],
            workflow_store=stores["workflow"],
            policy_store=stores["policy"],
        )
        from course_compiler.course_workflow_operations import CourseWorkflowReopenFailure

        if isinstance(context, CourseWorkflowReopenFailure):
            return None
        continuation = _reopen_course_workflow_continuation(
            context, artifact_store=stores["artifact"], document_store=stores["document"]
        )
        from course_compiler.course_workflow_operations import CourseWorkflowContinuationFailure

        if isinstance(continuation, CourseWorkflowContinuationFailure):
            return None
        # Priority review results are evidence-only semantic records, rather
        # than WorkflowState fields.  Reopen the exact result bound to the
        # currently approved priority subject so a fresh map author receives
        # the same owner-reviewed evidence as the approval gate.
        if continuation.handoff.stage == "lecture_mapping":
            try:
                from course_compiler.course_workflow_operations import ReopenedWorkflowArtifact
                from course_compiler.semantic_work_persistence import SemanticAcceptedRecord
                from course_compiler.workflow import WorkflowArtifactReference
                from course_compiler.workflow_policy import ARTIFACT_PRODUCERS

                accepted = [
                    row for row in stores["semantic"].load_all_for_job(job.job_id)  # type: ignore[attr-defined]
                    if isinstance(row, SemanticAcceptedRecord)
                    and row.kind == "exam_priority_assessment"
                    and row.priority_subject_sha256 == continuation.priority_subject_sha256
                ]
                if len(accepted) != 1:
                    return None
                review = accepted[0]
                refs = [ref for ref in review.artifact_refs if ref.kind == "priority_proposal"]
                if len(refs) != 1:
                    return None
                ref = refs[0]
                artifact_ref = WorkflowArtifactReference(
                    "workflow-artifact-reference/v1", ref.artifact_id, ref.kind,
                    ref.content_sha256, ARTIFACT_PRODUCERS[ref.kind]
                )
                payload = stores["artifact"].load(artifact_ref)
                content = payload.payload.decode("utf-8")
                continuation = replace(
                    continuation,
                    priority_evidence_review=ReopenedWorkflowArtifact(artifact_ref, content),
                )
            except Exception:
                return None
        return continuation

    def _handle_pending_work(self, course_id: str, job_id: str) -> None:
        """GET/HEAD /api/courses/{course_id}/jobs/{job_id}/pending_work — observation only."""

        if self.command not in ("GET", "HEAD"):
            self._send_json(405, {"error": "method_not_allowed"})
            return
        if (
            _observe_pending_work is None
            or _reopen_course_workflow_context is None
            or _reopen_course_workflow_continuation is None
            or _resolve_request_evidence is None
        ):
            self._send_json(500, {"error": "store_unavailable"})
            return
        if not course_id or ".." in course_id or "/" in course_id or "\\" in course_id:
            self._send_json(400, {"error": "invalid_input"})
            return
        if not job_id or ".." in job_id or "/" in job_id or "\\" in job_id:
            self._send_json(400, {"error": "invalid_input"})
            return
        ctx = getattr(self.server, "context", None) or getattr(self, "app_context", None)
        if ctx is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        stores = self._open_t051_stores(ctx)
        if stores is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        try:
            from course_compiler.course_job_persistence import CourseJobPersistenceFailure

            job = stores["job"].load(job_id)
            if isinstance(job, CourseJobPersistenceFailure):
                self._send_json(404, {"error": "not_found"})
                return
            if job.course_reference.course_id != course_id:
                self._send_json(404, {"error": "not_found"})
                return

            # GET/HEAD observation-only: the deterministic recovery may be
            # *derived* from reads here, but the actual persisted recovery
            # transition must occur through an existing mutation-authorized
            # ordinary application boundary (POST semantic-request). This
            # handler therefore never opens the recovery stores nor writes
            # any durable state; a correction-reopen-pending window simply
            # observes no current semantic request and the browser drives the
            # POST boundary to recover.
            from course_compiler.semantic_operations import SemanticOperationFailure

            result = _observe_pending_work(
                job_id,
                job_store=stores["job"],
                workflow_store=stores["workflow"],
                semantic_store=stores["semantic"],
            )
            if isinstance(result, SemanticOperationFailure):
                code = result.diagnostics[0].code if result.diagnostics else ""
                if code == "job_not_found":
                    self._send_json(404, {"error": "not_found"})
                    return
                if code in ("owner_decision_required", "deterministic_action_pending", "no_semantic_work"):
                    self._send_json(200, {"status": code})
                    return
                self._send_json(500, {"error": "store_unavailable"})
                return
            if result is None:
                self._send_json(200, {"status": "no_semantic_work"})
                return

            continuation = self._reopen_t051_continuation(job, stores)
            if continuation is None:
                self._send_json(500, {"error": "continuation_unavailable"})
                return

            evidence = _resolve_request_evidence(
                result.kind,
                continuation,
                source_store=stores["source"],
            )
            if isinstance(evidence, _EvidenceAccessFailure):
                self._send_json(500, {"error": "evidence_unavailable"})
                return

            course_store = open_course_store(ctx.database_path("courses.sqlite3"))
            try:
                course = course_store.load(course_id)
                try:
                    prompt = build_semantic_prompt(result, continuation, evidence, course.course_guidance)
                except ValueError as exc:
                    self._send_json(409, {"error": str(exc)})
                    return
            finally:
                course_store.close()
            from dataclasses import asdict
            # Browser control data is separate from the text copied to the model.
            handoff = {"request_id": result.request_id, "job_id": result.job_id,
                "workflow_id": result.workflow_id, "operation_id": result.operation_id,
                "expected_revision": result.expected_revision, "kind": result.kind,
                "prompt": prompt, "evidence_manifest": [asdict(item) for item in evidence]}
            self._send_json(200, {"status": "ready_for_chatgpt", "handoff": handoff})
        finally:
            for handle in stores.values():
                try:
                    handle.close()  # type: ignore[attr-defined]
                except Exception:
                    pass

    def _handle_pending_work_evidence(
        self, course_id: str, job_id: str, request_id: str, evidence_id: str
    ) -> None:
        """GET/HEAD .../pending_work/{request_id}/evidence/{evidence_id}."""

        if self.command not in ("GET", "HEAD"):
            self._send_json(405, {"error": "method_not_allowed"})
            return
        if (
            _observe_pending_work is None
            or _reopen_course_workflow_context is None
            or _reopen_course_workflow_continuation is None
            or _read_evidence_item is None
        ):
            self._send_json(500, {"error": "store_unavailable"})
            return
        for value in (course_id, job_id, request_id, evidence_id):
            if not value or ".." in value or "/" in value or "\\" in value:
                self._send_json(400, {"error": "invalid_input"})
                return
        ctx = getattr(self.server, "context", None) or getattr(self, "app_context", None)
        if ctx is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        stores = self._open_t051_stores(ctx)
        if stores is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        try:
            from course_compiler.course_job_persistence import CourseJobPersistenceFailure

            job = stores["job"].load(job_id)
            if isinstance(job, CourseJobPersistenceFailure):
                self._send_json(404, {"error": "not_found"})
                return
            if job.course_reference.course_id != course_id:
                self._send_json(404, {"error": "not_found"})
                return

            result = _observe_pending_work(
                job_id,
                job_store=stores["job"],
                workflow_store=stores["workflow"],
                semantic_store=stores["semantic"],
            )
            from course_compiler.semantic_operations import SemanticOperationFailure

            if isinstance(result, SemanticOperationFailure) or result is None:
                # Stale/absent request: nothing under this Job is currently
                # eligible for evidence delivery.
                self._send_json(404, {"error": "not_found"})
                return
            # Evidence URLs are capabilities for one exact handoff request,
            # never a lookup for whichever request happens to be current.
            if result.request_id != request_id:
                self._send_json(404, {"error": "not_found"})
                return

            continuation = self._reopen_t051_continuation(job, stores)
            if continuation is None:
                self._send_json(500, {"error": "continuation_unavailable"})
                return

            read = _read_evidence_item(
                evidence_id,
                result.kind,
                continuation,
                source_store=stores["source"],
            )
            if isinstance(read, _EvidenceAccessFailure):
                code = read.diagnostics[0].code if read.diagnostics else ""
                if code == "evidence_not_found":
                    self._send_json(404, {"error": "not_found"})
                else:
                    self._send_json(500, {"error": "evidence_unavailable"})
                return
            content, media_type = read
            # The opaque evidence ID is both the manifest identity and the
            # download name; an explicit safe extension lets a fresh ChatGPT
            # conversation recognize the attached file type without exposing
            # an original private filename or path.
            extension = {
                "application/pdf": ".pdf",
                "text/plain; charset=utf-8": ".txt",
                "text/markdown; charset=utf-8": ".md",
            }.get(media_type)
            if extension is None:
                self._send_json(500, {"error": "evidence_unavailable"})
                return
            self._send_binary(200, content, media_type, filename=f"{evidence_id}{extension}")
        finally:
            for handle in stores.values():
                try:
                    handle.close()  # type: ignore[attr-defined]
                except Exception:
                    pass

    def _handle_course_job_result(self, course_id: str, job_id: str) -> None:
        """Browser supplies its request binding; semantic executor supplies only text."""
        from course_compiler.semantic_work import SemanticWorkRequest
        data, err = self._read_json_body(max_bytes=_RELAY_RESULT_MAX_BYTES)
        if err or set(data) != {"request_id", "holder_id", "text"} or any(type(data[k]) is not str for k in data):
            self._send_json(400, {"error": "invalid_input", "diagnostics": ["semantic_text_required"]})
            return
        stores = self._open_t051_stores(self.server.context)
        if stores is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        try:
            job = stores["job"].load(job_id)
            if getattr(getattr(job, "course_reference", None), "course_id", None) != course_id:
                self._send_json(404, {"error": "not_found"})
                return
            from course_compiler.semantic_work_persistence import SemanticAcceptedRecord
            from course_compiler.providers.chatgpt_relay import matches_accepted_text
            previous = stores["semantic"].load(data["request_id"])
            if isinstance(previous, SemanticAcceptedRecord):
                original = stores["semantic"].load_original_request(data["request_id"])
                try:
                    same = (type(original) is SemanticWorkRequest and original.job_id == job_id
                        and original.workflow_id == job.workflow_id
                        and matches_accepted_text(data["text"], original, previous))
                except ValueError:
                    same = False
                self._send_json(200 if same else 409, {"status": "idempotent_repeat"} if same else
                    {"error": "conflict", "diagnostics": ["result_identity_mismatch"]})
                return
            request = _observe_pending_work(job_id, job_store=stores["job"], workflow_store=stores["workflow"], semantic_store=stores["semantic"])
            if type(request) is not SemanticWorkRequest or request.request_id != data["request_id"]:
                self._send_json(409, {"error": "conflict", "diagnostics": ["stale_workflow_state"]})
                return
            state = stores["workflow"].load(job.workflow_id)
            try:
                result = bind_semantic_text(data["text"], request, state)
                if request.kind == "lecture_generation":
                    from course_compiler.source_snippets import validate_snippets
                    validate_snippets(result.produced_documents[0].source_text, state.source_evidence + state.reviewed_source_evidence, stores["source"])
            except ValueError as exc:
                # Parsers emit only registered content-free codes.
                code = str(exc)
                self._send_json(400, {"error": "invalid_input", "diagnostics": [code if re.fullmatch(r"[a-z_]+", code) else "invalid_semantic_content"]})
                return
            self._submit_bound_result(job_id, result, data["holder_id"], stores)
        finally:
            for store in stores.values(): store.close()

    def _handle_owner_priority(self, job_id: str) -> None:
        if self.command != "POST":
            self._send_json(405, {"error": "method_not_allowed"})
            return
        if _submit_owner_priority_decision is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        if not job_id or "/" in job_id or "\\" in job_id or ".." in job_id:
            self._send_json(400, {"error": "invalid_input"})
            return
        data, err = self._read_json_body(max_bytes=4096)
        if err is not None:
            self._send_json(400, {"error": "invalid_input"})
            return
        allowed = {"approve", "subject_sha256"}
        if set(data.keys()) - allowed:
            self._send_json(400, {"error": "invalid_input"})
            return
        if "approve" not in data or type(data["approve"]) is not bool:
            self._send_json(400, {"error": "invalid_input"})
            return
        if "subject_sha256" not in data or type(data["subject_sha256"]) is not str:
            self._send_json(400, {"error": "invalid_input"})
            return
        subject = data["subject_sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}\Z", subject):
            self._send_json(400, {"error": "invalid_input"})
            return
        ctx = getattr(self.server, "context", None) or getattr(self, "app_context", None)
        if ctx is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        job_store = ws_store = sem_store = assoc_store = source_store = policy_store = artifact_store = doc_store = None
        try:
            try:
                j_path = ctx.database_path("course-jobs.sqlite3")
                w_path = ctx.database_path("workflow-state.sqlite3")
                s_path = ctx.database_path("semantic-work.sqlite3")
                a_path = ctx.database_path("course-workflow-associations.sqlite3")
                src_path = ctx.database_path("source-evidence.sqlite3")
                pol_path = ctx.database_path("policy-content.sqlite3")
                art_path = ctx.database_path("workflow-artifacts.sqlite3")
                doc_path = ctx.database_path("lecture-documents.sqlite3")
                if (
                    open_course_job_store is None
                    or open_workflow_state_store is None
                    or _open_semantic_work_store is None
                    or open_course_workflow_association_store is None
                    or open_source_evidence_store is None
                    or _open_policy_content_store is None
                    or _open_workflow_artifact_store is None
                    or _open_lecture_document_store is None
                ):
                    self._send_json(500, {"error": "store_unavailable"})
                    return
                job_store = open_course_job_store(j_path)
                ws_store = open_workflow_state_store(w_path)
                sem_store = _open_semantic_work_store(s_path)
                assoc_store = open_course_workflow_association_store(a_path)
                source_store = open_source_evidence_store(src_path)
                policy_store = _open_policy_content_store(pol_path)
                artifact_store = _open_workflow_artifact_store(art_path)
                doc_store = _open_lecture_document_store(doc_path)
            except Exception:
                self._send_json(500, {"error": "store_unavailable"})
                return
            try:
                decision_result = _submit_owner_priority_decision(  # type: ignore[misc]
                    job_id,
                    approve=data["approve"],
                    subject_sha256=subject,
                    job_store=job_store,
                    workflow_store=ws_store,
                    semantic_store=sem_store,
                    association_store=assoc_store,
                    source_store=source_store,
                    policy_store=policy_store,
                    artifact_store=artifact_store,
                    document_store=doc_store,
                )
            except Exception:
                self._send_json(500, {"error": "store_unavailable"})
                return
            from course_compiler.semantic_operations import SemanticOperationFailure
            from course_compiler.workflow import WorkflowRejected, WorkflowIdempotentRepeat
            if isinstance(decision_result, SemanticOperationFailure):
                code = decision_result.diagnostics[0].code if decision_result.diagnostics else ""
                if code in ("invalid_owner_decision", "invalid_semantic_input", "invalid_subject_hash", "subject_hash_mismatch"):
                    self._send_json(400, {"error": "invalid_input"})
                elif code == "job_not_found":
                    self._send_json(404, {"error": "not_found"})
                else:
                    self._send_json(500, {"error": "store_unavailable"})
                return
            if isinstance(decision_result, WorkflowRejected):
                self._send_json(400, {"error": "invalid_input", "diagnostics": [d.code for d in decision_result.diagnostics]})
                return
            if isinstance(decision_result, WorkflowIdempotentRepeat):
                self._send_json(200, {"status": "idempotent_repeat", "workflow_revision": decision_result.state.revision})
                return
            self._send_json(200, {"status": decision_result.status, "workflow_revision": decision_result.state.revision, "stage": decision_result.state.stage, "disposition": decision_result.state.disposition})
        finally:
            for h in (job_store, ws_store, sem_store, assoc_store, source_store, policy_store, artifact_store, doc_store):
                if h is not None:
                    try:
                        h.close()  # type: ignore[attr-defined]
                    except Exception:
                        pass

    def _handle_owner_map(self, job_id: str) -> None:
        if self.command != "POST":
            self._send_json(405, {"error": "method_not_allowed"})
            return
        if _submit_owner_map_decision is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        if not job_id or "/" in job_id or "\\" in job_id or ".." in job_id:
            self._send_json(400, {"error": "invalid_input"})
            return
        data, err = self._read_json_body(max_bytes=4096)
        if err is not None:
            self._send_json(400, {"error": "invalid_input"})
            return
        allowed = {"approve", "subject_sha256"}
        if set(data.keys()) - allowed:
            self._send_json(400, {"error": "invalid_input"})
            return
        if "approve" not in data or type(data["approve"]) is not bool:
            self._send_json(400, {"error": "invalid_input"})
            return
        if "subject_sha256" not in data or type(data["subject_sha256"]) is not str:
            self._send_json(400, {"error": "invalid_input"})
            return
        subject = data["subject_sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}\Z", subject):
            self._send_json(400, {"error": "invalid_input"})
            return
        ctx = getattr(self.server, "context", None) or getattr(self, "app_context", None)
        if ctx is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        job_store = ws_store = sem_store = assoc_store = source_store = policy_store = artifact_store = doc_store = None
        try:
            try:
                j_path = ctx.database_path("course-jobs.sqlite3")
                w_path = ctx.database_path("workflow-state.sqlite3")
                s_path = ctx.database_path("semantic-work.sqlite3")
                a_path = ctx.database_path("course-workflow-associations.sqlite3")
                src_path = ctx.database_path("source-evidence.sqlite3")
                pol_path = ctx.database_path("policy-content.sqlite3")
                art_path = ctx.database_path("workflow-artifacts.sqlite3")
                doc_path = ctx.database_path("lecture-documents.sqlite3")
                if (
                    open_course_job_store is None
                    or open_workflow_state_store is None
                    or _open_semantic_work_store is None
                    or open_course_workflow_association_store is None
                    or open_source_evidence_store is None
                    or _open_policy_content_store is None
                    or _open_workflow_artifact_store is None
                    or _open_lecture_document_store is None
                ):
                    self._send_json(500, {"error": "store_unavailable"})
                    return
                job_store = open_course_job_store(j_path)
                ws_store = open_workflow_state_store(w_path)
                sem_store = _open_semantic_work_store(s_path)
                assoc_store = open_course_workflow_association_store(a_path)
                source_store = open_source_evidence_store(src_path)
                policy_store = _open_policy_content_store(pol_path)
                artifact_store = _open_workflow_artifact_store(art_path)
                doc_store = _open_lecture_document_store(doc_path)
            except Exception:
                self._send_json(500, {"error": "store_unavailable"})
                return
            try:
                decision_result = _submit_owner_map_decision(  # type: ignore[misc]
                    job_id,
                    approve=data["approve"],
                    subject_sha256=subject,
                    job_store=job_store,
                    workflow_store=ws_store,
                    semantic_store=sem_store,
                    association_store=assoc_store,
                    source_store=source_store,
                    policy_store=policy_store,
                    artifact_store=artifact_store,
                    document_store=doc_store,
                )
            except Exception:
                self._send_json(500, {"error": "store_unavailable"})
                return
            from course_compiler.semantic_operations import SemanticOperationFailure
            from course_compiler.workflow import WorkflowRejected, WorkflowIdempotentRepeat
            if isinstance(decision_result, SemanticOperationFailure):
                code = decision_result.diagnostics[0].code if decision_result.diagnostics else ""
                if code in ("invalid_owner_decision", "invalid_semantic_input", "invalid_subject_hash", "subject_hash_mismatch"):
                    self._send_json(400, {"error": "invalid_input"})
                elif code == "job_not_found":
                    self._send_json(404, {"error": "not_found"})
                else:
                    self._send_json(500, {"error": "store_unavailable"})
                return
            if isinstance(decision_result, WorkflowRejected):
                self._send_json(400, {"error": "invalid_input", "diagnostics": [d.code for d in decision_result.diagnostics]})
                return
            if isinstance(decision_result, WorkflowIdempotentRepeat):
                self._send_json(200, {"status": "idempotent_repeat", "workflow_revision": decision_result.state.revision})
                return
            self._send_json(200, {"status": decision_result.status, "workflow_revision": decision_result.state.revision, "stage": decision_result.state.stage, "disposition": decision_result.state.disposition})
        finally:
            for h in (job_store, ws_store, sem_store, assoc_store, source_store, policy_store, artifact_store, doc_store):
                if h is not None:
                    try:
                        h.close()  # type: ignore[attr-defined]
                    except Exception:
                        pass

    def _handle_build_job(self, job_id: str) -> None:
        if self.command != "POST":
            self._send_json(405, {"error": "method_not_allowed"})
            return
        if _build_pdf is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        if not job_id or "/" in job_id or "\\" in job_id or ".." in job_id:
            self._send_json(400, {"error": "invalid_input"})
            return
        ctx = getattr(self.server, "context", None) or getattr(self, "app_context", None)
        if ctx is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        job_store = assoc_store = ws_store = pol_store = doc_store = build_store = sem_store = source_store = None
        try:
            try:
                j_path = ctx.database_path("course-jobs.sqlite3")
                a_path = ctx.database_path("course-workflow-associations.sqlite3")
                w_path = ctx.database_path("workflow-state.sqlite3")
                pol_path = ctx.database_path("policy-content.sqlite3")
                doc_path = ctx.database_path("lecture-documents.sqlite3")
                b_path = ctx.database_path("build-records.sqlite3")
                if (
                    open_course_job_store is None
                    or open_course_workflow_association_store is None
                    or open_workflow_state_store is None
                    or _open_policy_content_store is None
                    or _open_lecture_document_store is None
                    or _open_build_record_store is None
                    or _open_semantic_work_store is None
                ):
                    self._send_json(500, {"error": "store_unavailable"})
                    return
                job_store = open_course_job_store(j_path)
                assoc_store = open_course_workflow_association_store(a_path)
                ws_store = open_workflow_state_store(w_path)
                pol_store = _open_policy_content_store(pol_path)
                doc_store = _open_lecture_document_store(doc_path)
                source_store = open_source_evidence_store(ctx.database_path("source-evidence.sqlite3"))
                build_store = _open_build_record_store(b_path)
                sem_store = _open_semantic_work_store(ctx.database_path("semantic-work.sqlite3"))
            except Exception:
                self._send_json(500, {"error": "store_unavailable"})
                return

            res = _build_pdf(
                job_id,
                job_store=job_store,
                association_store=assoc_store,
                workflow_store=ws_store,
                policy_store=pol_store,
                document_store=doc_store,
                build_store=build_store,
                cache_root=ctx.cache_root_path(),
                source_store=source_store,
                semantic_store=sem_store,
            )
            from course_compiler.build_operations import BuildOperationFailure
            from course_compiler.build_persistence import BuildRecord
            if isinstance(res, BuildOperationFailure):
                code = res.diagnostics[0].code if res.diagnostics else "build_operation_failed"
                if code in ("job_not_found", "course_not_found"):
                    self._send_json(404, {"error": "not_found"})
                elif code in _BUILD_CONFLICT_CODES:
                    self._send_json(409, {"error": code})
                else:
                    self._send_json(500, {"error": code})
                return
            if isinstance(res, BuildRecord):
                self._send_json(200, {
                    "status": "succeeded",
                    "build_id": res.build_id,
                    "bundle_hash": res.bundle_hash,
                    "created_at": res.created_at,
                })
                return
            self._send_json(500, {"error": "build_operation_failed"})
        finally:
            for h in (job_store, assoc_store, ws_store, pol_store, doc_store, build_store, sem_store, source_store):
                if h is not None:
                    try:
                        h.close()
                    except Exception:
                        pass

    def _handle_get_job_artifact(self, job_id: str) -> None:
        if self.command not in ("GET", "HEAD"):
            self._send_json(405, {"error": "method_not_allowed"})
            return
        if not job_id or "/" in job_id or "\\" in job_id or ".." in job_id:
            self._send_json(400, {"error": "invalid_input"})
            return
        ctx = getattr(self.server, "context", None) or getattr(self, "app_context", None)
        if ctx is None or open_course_job_store is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        job_store = None
        try:
            job_store = open_course_job_store(ctx.database_path("course-jobs.sqlite3"))
            job = job_store.load(job_id)
            from course_compiler.course_job_persistence import CourseJobRecord
            if not isinstance(job, CourseJobRecord):
                self._send_json(404, {"error": "not_found"})
                return
            if job.completed_build_id is None:
                self._send_json(404, {"error": "artifact_not_found"})
                return
            build_id = job.completed_build_id
        finally:
            if job_store is not None:
                try:
                    job_store.close()
                except Exception:
                    pass
        self._handle_get_build_artifact(build_id)

    def _handle_get_build_artifact(self, build_id: str) -> None:
        if self.command not in ("GET", "HEAD"):
            self._send_json(405, {"error": "method_not_allowed"})
            return
        if _get_artifact is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        if not build_id or "/" in build_id or "\\" in build_id or ".." in build_id:
            self._send_json(400, {"error": "invalid_input"})
            return
        ctx = getattr(self.server, "context", None) or getattr(self, "app_context", None)
        if ctx is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        build_store = job_store = assoc_store = ws_store = pol_store = doc_store = source_store = None
        try:
            try:
                b_path = ctx.database_path("build-records.sqlite3")
                j_path = ctx.database_path("course-jobs.sqlite3")
                a_path = ctx.database_path("course-workflow-associations.sqlite3")
                w_path = ctx.database_path("workflow-state.sqlite3")
                pol_path = ctx.database_path("policy-content.sqlite3")
                doc_path = ctx.database_path("lecture-documents.sqlite3")
                if (
                    _open_build_record_store is None
                    or open_course_job_store is None
                    or open_course_workflow_association_store is None
                    or open_workflow_state_store is None
                    or _open_policy_content_store is None
                    or _open_lecture_document_store is None
                ):
                    self._send_json(500, {"error": "store_unavailable"})
                    return
                build_store = _open_build_record_store(b_path)
                job_store = open_course_job_store(j_path)
                assoc_store = open_course_workflow_association_store(a_path)
                ws_store = open_workflow_state_store(w_path)
                pol_store = _open_policy_content_store(pol_path)
                doc_store = _open_lecture_document_store(doc_path)
                source_store = open_source_evidence_store(ctx.database_path("source-evidence.sqlite3"))
            except Exception:
                self._send_json(500, {"error": "store_unavailable"})
                return

            res = _get_artifact(
                build_id,
                build_store=build_store,
                job_store=job_store,
                association_store=assoc_store,
                workflow_store=ws_store,
                policy_store=pol_store,
                document_store=doc_store,
                cache_root=ctx.cache_root_path(),
                source_store=source_store,
            )
            from course_compiler.build_operations import BuildOperationFailure
            if isinstance(res, BuildOperationFailure):
                code = res.diagnostics[0].code if res.diagnostics else "build_operation_failed"
                if code in ("build_not_found", "job_not_found"):
                    self._send_json(404, {"error": "not_found"})
                elif code in _BUILD_CONFLICT_CODES:
                    self._send_json(409, {"error": code})
                else:
                    self._send_json(500, {"error": code})
                return
            if isinstance(res, bytes):
                self._send_binary(200, res, "application/pdf", filename=f"course-{build_id}.pdf")
                return
            self._send_json(500, {"error": "artifact_retrieval_failed"})
        finally:
            for h in (build_store, job_store, assoc_store, ws_store, pol_store, doc_store, source_store):
                if h is not None:
                    try:
                        h.close()
                    except Exception:
                        pass

    def _handle_list_job_builds(self, job_id: str) -> None:
        if self.command not in ("GET", "HEAD"):
            self._send_json(405, {"error": "method_not_allowed"})
            return
        if _get_artifact_history is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        if not job_id or "/" in job_id or "\\" in job_id or ".." in job_id:
            self._send_json(400, {"error": "invalid_input"})
            return
        ctx = getattr(self.server, "context", None) or getattr(self, "app_context", None)
        if ctx is None or _open_build_record_store is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        build_store = None
        try:
            b_path = ctx.database_path("build-records.sqlite3")
            build_store = _open_build_record_store(b_path)
            res = _get_artifact_history(job_id, build_store=build_store)
            from course_compiler.build_operations import BuildOperationFailure
            if isinstance(res, BuildOperationFailure):
                self._send_json(500, {"error": res.diagnostics[0].code})
                return
            builds_json = [
                {
                    "build_id": b.build_id,
                    "job_id": b.job_id,
                    "workflow_revision": b.workflow_revision,
                    "bundle_hash": b.bundle_hash,
                    "created_at": b.created_at,
                    "status": b.status,
                }
                for b in res
            ]
            self._send_json(200, {"builds": builds_json})
        finally:
            if build_store is not None:
                try:
                    build_store.close()
                except Exception:
                    pass

    def _handle_preview_source(self, course_id: str, source_id: str) -> None:
        if self.command not in ("GET", "HEAD"):
            self._send_json(405, {"error": "method_not_allowed"})
            return
        if _preview_source_pdf_page is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        page = 1
        try:
            split = urlsplit(self.path)
            from urllib.parse import parse_qs
            qs = parse_qs(split.query)
            if "page" in qs:
                page = int(qs["page"][0])
        except Exception:
            page = 1
        ctx = getattr(self.server, "context", None) or getattr(self, "app_context", None)
        if ctx is None or open_course_store is None or open_source_evidence_store is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        course_store = source_store = None
        try:
            course_store = open_course_store(ctx.database_path("courses.sqlite3"))
            source_store = open_source_evidence_store(ctx.database_path("source-evidence.sqlite3"))
            res = _preview_source_pdf_page(
                course_id,
                source_id,
                page,
                course_store=course_store,
                source_store=source_store,
            )
            from course_compiler.build_operations import RenderedPdfPagePreview
            if isinstance(res, RenderedPdfPagePreview):
                self._send_binary(200, res.content_bytes, "image/png")
                return
            self._send_json(404, {"error": "not_found"})
        finally:
            for h in (course_store, source_store):
                if h is not None:
                    try:
                        h.close()
                    except Exception:
                        pass

    def _handle_extract_source(self, course_id: str, source_id: str) -> None:
        if self.command != "POST":
            self._send_json(405, {"error": "method_not_allowed"})
            return
        if _extract_source_pdf_region is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        data, err = self._read_json_body(max_bytes=4096)
        if err is not None or not isinstance(data, dict):
            self._send_json(400, {"error": "invalid_input"})
            return
        try:
            page = int(data.get("page", 1))
            left = int(data.get("left", 0))
            top = int(data.get("top", 0))
            width = int(data.get("width", 0))
            height = int(data.get("height", 0))
        except (TypeError, ValueError):
            self._send_json(400, {"error": "invalid_input"})
            return
        ctx = getattr(self.server, "context", None) or getattr(self, "app_context", None)
        if ctx is None or open_course_store is None or open_source_evidence_store is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        course_store = source_store = None
        try:
            course_store = open_course_store(ctx.database_path("courses.sqlite3"))
            source_store = open_source_evidence_store(ctx.database_path("source-evidence.sqlite3"))
            res = _extract_source_pdf_region(
                course_id,
                source_id,
                page,
                left,
                top,
                width,
                height,
                course_store=course_store,
                source_store=source_store,
            )
            from course_compiler.build_operations import ExtractedPdfVisual
            if isinstance(res, ExtractedPdfVisual):
                self._send_binary(200, res.content_bytes, "image/png")
                return
            self._send_json(400, {"error": "extraction_failed"})
        finally:
            for h in (course_store, source_store):
                if h is not None:
                    try:
                        h.close()
                    except Exception:
                        pass

    def _handle_run_controlled_generation(self, job_id: str) -> None:
        if self.command != "POST":
            self._send_json(405, {"error": "method_not_allowed"})
            return
        if not job_id or "/" in job_id or "\\" in job_id or ".." in job_id:
            self._send_json(400, {"error": "invalid_input"})
            return
        data, _ = self._read_json_body(max_bytes=4096)
        map_size = 2
        if isinstance(data, dict) and "map_size" in data:
            try:
                map_size = max(1, min(5, int(data["map_size"])))
            except (TypeError, ValueError):
                map_size = 2
        ctx = getattr(self.server, "context", None) or getattr(self, "app_context", None)
        if ctx is None:
            self._send_json(500, {"error": "store_unavailable"})
            return
        from course_compiler.semantic_work import ScriptProvider
        from course_compiler.workflow import WorkflowState, priority_subject_sha256, map_subject_sha256
        from course_compiler.course_job_persistence import CourseJobRecord
        stores = {}
        try:
            stores["job_store"] = open_course_job_store(ctx.database_path("course-jobs.sqlite3"))
            stores["assoc_store"] = open_course_workflow_association_store(ctx.database_path("course-workflow-associations.sqlite3"))
            stores["source_store"] = open_source_evidence_store(ctx.database_path("source-evidence.sqlite3"))
            stores["workflow_store"] = open_workflow_state_store(ctx.database_path("workflow-state.sqlite3"))
            stores["policy_store"] = _open_policy_content_store(ctx.database_path("policy-content.sqlite3"))
            stores["artifact_store"] = _open_workflow_artifact_store(ctx.database_path("workflow-artifacts.sqlite3"))
            stores["document_store"] = _open_lecture_document_store(ctx.database_path("lecture-documents.sqlite3"))
            stores["semantic_store"] = _open_semantic_work_store(ctx.database_path("semantic-work.sqlite3"))

            provider = ScriptProvider(map_size=map_size)
            for i in range(20):
                job = stores["job_store"].load(job_id)
                if not isinstance(job, CourseJobRecord):
                    self._send_json(404, {"error": "job_not_found"})
                    return
                ws = stores["workflow_store"].load(job.workflow_id)
                if isinstance(ws, WorkflowState) and ws.stage == "completed":
                    break
                req = _request_semantic_work(
                    job_id,
                    job_store=stores["job_store"],
                    workflow_store=stores["workflow_store"],
                    semantic_store=stores["semantic_store"],
                    holder_id="h-app-controlled",
                    clock=f"2026-01-01T00:{i:02d}:00Z",
                )
                from course_compiler.semantic_operations import SemanticOperationFailure
                if isinstance(req, SemanticOperationFailure):
                    if req.diagnostics[0].code == "owner_decision_required":
                        if ws.stage == "priority_approval":
                            assert ws.priority_basis is not None
                            subj = priority_subject_sha256(ws.priority_basis, ws.policies.priority_basis)
                            _submit_owner_priority_decision(
                                job_id, approve=True, subject_sha256=subj,
                                job_store=stores["job_store"], workflow_store=stores["workflow_store"],
                                semantic_store=stores["semantic_store"], association_store=stores["assoc_store"],
                                source_store=stores["source_store"], policy_store=stores["policy_store"],
                                artifact_store=stores["artifact_store"], document_store=stores["document_store"],
                                clock=f"2026-01-01T00:{i:02d}:10Z",
                            )
                        elif ws.stage == "map_approval":
                            assert ws.lecture_map is not None
                            subj = map_subject_sha256(ws.lecture_map, ws.policies.lecture_mapping)
                            _submit_owner_map_decision(
                                job_id, approve=True, subject_sha256=subj,
                                job_store=stores["job_store"], workflow_store=stores["workflow_store"],
                                semantic_store=stores["semantic_store"], association_store=stores["assoc_store"],
                                source_store=stores["source_store"], policy_store=stores["policy_store"],
                                artifact_store=stores["artifact_store"], document_store=stores["document_store"],
                                clock=f"2026-01-01T00:{i:02d}:10Z",
                            )
                    continue

                res = provider.execute(req)
                sub = _submit_semantic_result(
                    job_id, res,
                    job_store=stores["job_store"], workflow_store=stores["workflow_store"],
                    semantic_store=stores["semantic_store"], association_store=stores["assoc_store"],
                    source_store=stores["source_store"], policy_store=stores["policy_store"],
                    artifact_store=stores["artifact_store"], document_store=stores["document_store"],
                    holder_id="h-app-controlled",
                    clock=f"2026-01-01T00:{i:02d}:30Z",
                )
                if isinstance(sub, SemanticOperationFailure) and sub.diagnostics[0].code == "owner_decision_required":
                    ws_cur = stores["workflow_store"].load(job.workflow_id)
                    if req.kind == "exam_priority_assessment":
                        assert ws_cur.priority_basis is not None
                        subj = priority_subject_sha256(ws_cur.priority_basis, ws_cur.policies.priority_basis)
                        _submit_owner_priority_decision(
                            job_id, approve=True, subject_sha256=subj,
                            job_store=stores["job_store"], workflow_store=stores["workflow_store"],
                            semantic_store=stores["semantic_store"], association_store=stores["assoc_store"],
                            source_store=stores["source_store"], policy_store=stores["policy_store"],
                            artifact_store=stores["artifact_store"], document_store=stores["document_store"],
                            clock=f"2026-01-01T00:{i:02d}:40Z",
                        )
                    elif req.kind == "lecture_map_generation":
                        assert ws_cur.lecture_map is not None
                        subj = map_subject_sha256(ws_cur.lecture_map, ws_cur.policies.lecture_mapping)
                        _submit_owner_map_decision(
                            job_id, approve=True, subject_sha256=subj,
                            job_store=stores["job_store"], workflow_store=stores["workflow_store"],
                            semantic_store=stores["semantic_store"], association_store=stores["assoc_store"],
                            source_store=stores["source_store"], policy_store=stores["policy_store"],
                            artifact_store=stores["artifact_store"], document_store=stores["document_store"],
                            clock=f"2026-01-01T00:{i:02d}:40Z",
                        )

            final_job = stores["job_store"].load(job_id)
            final_ws = stores["workflow_store"].load(final_job.workflow_id)
            self._send_json(200, {
                "status": final_job.status,
                "workflow_stage": final_ws.stage if isinstance(final_ws, WorkflowState) else "unknown",
            })
        finally:
            for s in stores.values():
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle()

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._handle()

    def do_TRACE(self) -> None:  # noqa: N802
        self._handle()

    def do_CONNECT(self) -> None:  # noqa: N802
        self._handle()

    def __getattr__(self, name: str):  # noqa: D105
        if name.startswith("do_"):
            def _unhandled() -> None:
                self._handle()

            return _unhandled
        raise AttributeError(name)

    def _handle(self) -> None:
        # Parse path without query; handle leading // which urlsplit treats as netloc.
        try:
            raw_path = self.path
            split = urlsplit(raw_path)
            if split.scheme or split.netloc:
                # Origin-form should not have scheme/netloc; reconstruct path
                # e.g. "//healthz" -> netloc="healthz", path="" should become "/healthz"
                path = unquote("/" + split.netloc + split.path)
            else:
                path = unquote(split.path)
        except Exception:
            self._send_not_found(is_api=False)
            return

        # Normalize: ensure leading slash
        if not path.startswith("/"):
            path = "/" + path

        # Route dispatch
        # Health check
        if path == "/healthz":
            if self.command not in ("GET", "HEAD"):
                self._send_json(405, {"error": "method_not_allowed"})
                return
            self._send_json(200, {"status": "ok"})
            return

        # API courses collection
        if path == "/api/courses":
            self._handle_api_courses()
            return

        # API attach source: POST /api/courses/{course_id}/sources
        if path.startswith("/api/courses/") and path.endswith("/sources"):
            # Expect /api/courses/{id}/sources
            prefix = "/api/courses/"
            suffix = "/sources"
            middle = path[len(prefix) : -len(suffix)]
            # middle should be single segment course_id without slash
            if "/" not in middle and middle:
                # Validate course_id shape loosely (no traversal)
                if ".." not in middle and "/" not in middle and "\\" not in middle:
                    self._handle_attach_source(middle)
                    return
            self._send_json(404, {"error": "not_found"})
            return

        # API start generation: POST /api/courses/{course_id}/start-generation
        if path.startswith("/api/courses/") and path.endswith("/start-generation"):
            prefix = "/api/courses/"
            suffix = "/start-generation"
            middle = path[len(prefix) : -len(suffix)]
            if "/" not in middle and middle:
                if ".." not in middle and "/" not in middle and "\\" not in middle:
                    self._handle_start_generation(middle)
                    return
            self._send_json(404, {"error": "not_found"})
            return

        # API semantic request/result: POST /api/jobs/{job_id}/semantic-request|semantic-result
        if path.startswith("/api/jobs/") and (path.endswith("/semantic-request") or path.endswith("/semantic-result")):
            # Expect /api/jobs/{id}/semantic-request or /semantic-result
            prefix = "/api/jobs/"
            if path.endswith("/semantic-request"):
                suffix = "/semantic-request"
                middle = path[len(prefix) : -len(suffix)]
                if "/" not in middle and middle and ".." not in middle and "\\" not in middle:
                    self._handle_semantic_request(middle)
                    return
            else:
                suffix = "/semantic-result"
                middle = path[len(prefix) : -len(suffix)]
                if "/" not in middle and middle and ".." not in middle and "\\" not in middle:
                    self._handle_semantic_result(middle)
                    return
            self._send_json(404, {"error": "not_found"})
            return

        # API owner decision: POST /api/jobs/{job_id}/owner/priority|map
        if path.startswith("/api/jobs/") and "/owner/" in path:
            prefix = "/api/jobs/"
            middle = path[len(prefix) :]
            if middle.count("/") == 2 and middle.startswith("") and middle.endswith(("/owner/priority", "/owner/map")):
                parts = middle.split("/")
                if len(parts) == 3 and parts[0] and ".." not in parts[0] and "\\" not in parts[0]:
                    if parts[2] == "priority":
                        self._handle_owner_priority(parts[0])
                        return
                    if parts[2] == "map":
                        self._handle_owner_map(parts[0])
                        return
            self._send_json(404, {"error": "not_found"})
            return

        # API build artifact: GET /api/builds/{build_id}/artifact
        if path.startswith("/api/builds/") and path.endswith("/artifact"):
            prefix = "/api/builds/"
            suffix = "/artifact"
            middle = path[len(prefix) : -len(suffix)]
            if "/" not in middle and middle and ".." not in middle and "\\" not in middle:
                self._handle_get_build_artifact(middle)
                return
            self._send_json(404, {"error": "not_found"})
            return

        # API source preview: GET /api/courses/{course_id}/sources/{source_id}/preview
        if path.startswith("/api/courses/") and "/sources/" in path and path.endswith("/preview"):
            parts = path.split("/")
            if len(parts) == 7 and parts[1] == "api" and parts[2] == "courses" and parts[4] == "sources" and parts[6] == "preview":
                c_id = parts[3]
                s_id = parts[5]
                if c_id and s_id and ".." not in c_id and ".." not in s_id and "\\" not in c_id and "\\" not in s_id:
                    self._handle_preview_source(c_id, s_id)
                    return
            self._send_json(404, {"error": "not_found"})
            return

        # API source extract: POST /api/courses/{course_id}/sources/{source_id}/extract
        if path.startswith("/api/courses/") and "/sources/" in path and path.endswith("/extract"):
            parts = path.split("/")
            if len(parts) == 7 and parts[1] == "api" and parts[2] == "courses" and parts[4] == "sources" and parts[6] == "extract":
                c_id = parts[3]
                s_id = parts[5]
                if c_id and s_id and ".." not in c_id and ".." not in s_id and "\\" not in c_id and "\\" not in s_id:
                    self._handle_extract_source(c_id, s_id)
                    return
            self._send_json(404, {"error": "not_found"})
            return

        # API T051 pending-work evidence (data plane), bound to one exact
        # request: GET/HEAD .../pending_work/{request_id}/evidence/{evidence_id}
        if path.startswith("/api/courses/") and "/jobs/" in path and "/pending_work/" in path and "/evidence/" in path:
            parts = path.split("/")
            if (
                len(parts) == 10
                and parts[1] == "api"
                and parts[2] == "courses"
                and parts[4] == "jobs"
                and parts[6] == "pending_work"
                and parts[8] == "evidence"
            ):
                c_id, j_id, r_id, e_id = parts[3], parts[5], parts[7], parts[9]
                if all(v and ".." not in v and "\\" not in v for v in (c_id, j_id, r_id, e_id)):
                    self._handle_pending_work_evidence(c_id, j_id, r_id, e_id)
                    return
            self._send_json(404, {"error": "not_found"})
            return

        # API T051 pending-work (control plane, observation only):
        if path.startswith("/api/courses/") and path.endswith("/guidance"):
            parts = path.split("/")
            if len(parts) == 5 and self.command == "POST":
                self._handle_guidance(parts[3])
            else:
                self._send_json(405, {"error": "method_not_allowed"})
            return

        # GET/HEAD /api/courses/{course_id}/jobs/{job_id}/pending_work
        if path.startswith("/api/courses/") and "/jobs/" in path and path.endswith("/pending_work"):
            parts = path.split("/")
            if (
                len(parts) == 7
                and parts[1] == "api"
                and parts[2] == "courses"
                and parts[4] == "jobs"
                and parts[6] == "pending_work"
            ):
                c_id, j_id = parts[3], parts[5]
                if c_id and j_id and ".." not in c_id and ".." not in j_id and "\\" not in c_id and "\\" not in j_id:
                    self._handle_pending_work(c_id, j_id)
                    return
            self._send_json(404, {"error": "not_found"})
            return

        # API T051 result submission: POST /api/courses/{course_id}/jobs/{job_id}/result
        if path.startswith("/api/courses/") and "/jobs/" in path and path.endswith("/result"):
            parts = path.split("/")
            if (
                len(parts) == 7
                and parts[1] == "api"
                and parts[2] == "courses"
                and parts[4] == "jobs"
                and parts[6] == "result"
            ):
                c_id, j_id = parts[3], parts[5]
                if c_id and j_id and ".." not in c_id and ".." not in j_id and "\\" not in c_id and "\\" not in j_id:
                    self._handle_course_job_result(c_id, j_id)
                    return
            self._send_json(404, {"error": "not_found"})
            return

        # API build job: POST /api/jobs/{job_id}/build
        if path.startswith("/api/jobs/") and path.endswith("/build"):
            prefix = "/api/jobs/"
            suffix = "/build"
            middle = path[len(prefix) : -len(suffix)]
            if "/" not in middle and middle and ".." not in middle and "\\" not in middle:
                self._handle_build_job(middle)
                return
            self._send_json(404, {"error": "not_found"})
            return

        # API run controlled generation: POST /api/jobs/{job_id}/run-controlled-generation
        if path.startswith("/api/jobs/") and path.endswith("/run-controlled-generation"):
            prefix = "/api/jobs/"
            suffix = "/run-controlled-generation"
            middle = path[len(prefix) : -len(suffix)]
            if "/" not in middle and middle and ".." not in middle and "\\" not in middle:
                self._handle_run_controlled_generation(middle)
                return
            self._send_json(404, {"error": "not_found"})
            return

        # API job builds list: GET /api/jobs/{job_id}/builds
        if path.startswith("/api/jobs/") and path.endswith("/builds"):
            prefix = "/api/jobs/"
            suffix = "/builds"
            middle = path[len(prefix) : -len(suffix)]
            if "/" not in middle and middle and ".." not in middle and "\\" not in middle:
                self._handle_list_job_builds(middle)
                return
            self._send_json(404, {"error": "not_found"})
            return

        # API job artifact: GET /api/jobs/{job_id}/artifact
        if path.startswith("/api/jobs/") and path.endswith("/artifact"):
            prefix = "/api/jobs/"
            suffix = "/artifact"
            middle = path[len(prefix) : -len(suffix)]
            if "/" not in middle and middle and ".." not in middle and "\\" not in middle:
                self._handle_get_job_artifact(middle)
                return
            self._send_json(404, {"error": "not_found"})
            return

        # API course detail: GET /api/courses/{course_id}
        if path.startswith("/api/courses/"):
            # Ensure single segment after prefix
            rest = path[len("/api/courses/") :]
            if "/" not in rest and rest:
                if ".." not in rest and "\\" not in rest:
                    self._handle_api_course_detail(rest)
                    return
            self._send_json(404, {"error": "not_found"})
            return

        # API job status: GET /api/jobs/{job_id}
        if path.startswith("/api/jobs/"):
            rest = path[len("/api/jobs/") :]
            if "/" not in rest and rest:
                if ".." not in rest and "\\" not in rest:
                    self._handle_api_job_status(rest)
                    return
            self._send_json(404, {"error": "not_found"})
            return

        # API unknown -> safe JSON 404
        if path.startswith("/api/"):
            self._send_json(404, {"error": "not_found"})
            return

        # Static asset prefix: /static/
        if path.startswith("/static/"):
            if self.command not in ("GET", "HEAD"):
                self._send_not_found(is_api=False)
                return
            rel = path[len("/static/") :]
            # Reject empty or traversal via helper
            safe = _safe_join_static(rel)
            if safe is None or not safe.is_file():
                self._send_not_found(is_api=False)
                return
            self._send_static_file(safe)
            return

        # Direct static file at root like /app.css? For shell we only serve via /static/ and /
        # But also handle known static files directly for robustness: reject traversal attempt
        if path in ("/app.css", "/app.js", "/favicon.ico"):
            self._send_not_found(is_api=False)
            return

        # Shell root
        if path == "/" or path == "/index.html":
            if self.command not in ("GET", "HEAD"):
                self._send_not_found(is_api=False)
                return
            # Serve index.html from static dir
            index = _INDEX_FILE
            if not index.is_file():
                self._send_html(500, "<h1>Shell unavailable</h1>")
                return
            self._send_static_file(index)
            return

        # All other missing routes -> safe 404 (HTML)
        is_api = path.startswith("/api")
        self._send_not_found(is_api=is_api)

    def version_string(self) -> str:  # noqa: D401
        return "CourseCompiler/0.1"


class CourseCompilerServer(ThreadingHTTPServer):
    """Threading HTTP server with loopback binding and context lifecycle."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: AppConfig, handler_class: type[BaseHTTPRequestHandler] | None = None) -> None:
        if type(config) is not AppConfig:
            raise TypeError("config must be exactly AppConfig")
        # Ensure data root exists via context seam
        ctx = create_app_context(config)
        try:
            ctx.prepare()
        except Exception as exc:
            raise AppConfigError("data_root_unavailable") from exc
        handler = handler_class or CourseCompilerHandler
        # Inject config/context for handler
        handler.app_config = config  # type: ignore[attr-defined]
        handler.app_context = ctx  # type: ignore[attr-defined]
        # TCPServer may call server_close() when bind fails.  Establish the
        # context cleanup authority before that constructor path is reachable.
        self.config = config
        self.context = ctx
        self._closed = False
        super().__init__((config.host, config.port), handler)

    def shutdown(self) -> None:  # type: ignore[override]
        super().shutdown()
        self._close_context()

    def server_close(self) -> None:  # type: ignore[override]
        super().server_close()
        self._close_context()

    def _close_context(self) -> None:
        if not self._closed:
            try:
                self.context.close()
            except Exception:
                pass
            self._closed = True


def create_server(
    config: AppConfig | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
    data_root: str | Path | None = None,
    allow_non_loopback: bool = False,
) -> CourseCompilerServer:
    """Create a configured HTTP server without starting it."""

    if config is None:
        config = create_config(host=host, port=port, data_root=data_root, allow_non_loopback=allow_non_loopback)
    else:
        if host is not None or port is not None or data_root is not None:
            raise AppConfigError("config_and_override_conflict")
    return CourseCompilerServer(config)


def run_server(config: AppConfig | None = None, *, host: str | None = None, port: int | None = None) -> None:
    """Run the application shell server until interrupted (blocking)."""

    server = create_server(config, host=host, port=port)
    host_display, port_display = server.server_address
    # Content-safe startup line: no paths, no env, just loopback URL
    print(f"Course Compiler app listening on http://{host_display}:{port_display}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        # Content-safe shutdown line
        print("Course Compiler app stopped.", flush=True)
