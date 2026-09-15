"""SQLite/Application resource opener seam for the local app.

T048 replaces future seams with real Course/Job store ownership; T047 seam
remains for existing stores and semantic-work future.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from course_compiler.course_workflow_persistence import (
    CourseWorkflowPersistenceFailure,
    open_course_workflow_association_store,
)
from course_compiler.lecture_document_persistence import (
    LectureDocumentPersistenceFailure,
    open_lecture_document_store,
)
from course_compiler.policy_persistence import (
    PolicyPersistenceFailure,
    open_policy_content_store,
)
from course_compiler.source_persistence import (
    SourcePersistenceFailure,
    open_source_evidence_store,
)
from course_compiler.workflow_artifact_persistence import (
    WorkflowArtifactPersistenceFailure,
    open_workflow_artifact_store,
)
from course_compiler.workflow_persistence import (
    WorkflowPersistenceFailure,
    open_workflow_state_store,
)

from .config import AppConfig, AppConfigError


class AppContextError(Exception):
    """A fixed, content-safe context failure."""


class AppContext:
    """Application context owning data-root and SQLite opener lifecycle."""

    def __init__(self, config: AppConfig) -> None:
        if type(config) is not AppConfig:
            raise TypeError("config must be exactly AppConfig")
        self._config = config
        self._prepared = False
        self._open_handles: list[object] = []

    @property
    def config(self) -> AppConfig:
        return self._config

    @property
    def data_root(self) -> Path:
        return self._config.data_root

    def prepare(self) -> Path:
        """Ensure the data root directory exists; return resolved path."""

        try:
            self._config.data_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AppContextError("data_root_unavailable") from exc
        if not self._config.data_root.is_dir():
            raise AppContextError("data_root_unavailable")
        self._prepared = True
        return self._config.data_root

    def database_path(self, name: str) -> Path:
        """Resolve a logical database filename under the data root."""

        if type(name) is not str or not name:
            raise AppContextError("database_name_invalid")
        # Only allow known suffixes; reject traversal.
        if "/" in name or "\\" in name or ".." in name:
            raise AppContextError("database_name_invalid")
        if not name.endswith(".sqlite3") and not name.endswith(".db"):
            raise AppContextError("database_name_invalid")
        return self._config.data_root / name

    # --- Existing store openers (seam for current six stores) ---

    def open_workflow_state_store(self, filename: str = "workflow-state.sqlite3"):  # type: ignore[no-untyped-def]
        self.prepare()
        path = self.database_path(filename)
        result = open_workflow_state_store(path)
        if isinstance(result, WorkflowPersistenceFailure):
            raise AppContextError("store_unavailable")
        self._open_handles.append(result)
        return result

    def open_source_evidence_store(self, filename: str = "source-evidence.sqlite3"):  # type: ignore[no-untyped-def]
        self.prepare()
        path = self.database_path(filename)
        result = open_source_evidence_store(path)
        if isinstance(result, SourcePersistenceFailure):
            raise AppContextError("store_unavailable")
        self._open_handles.append(result)
        return result

    def open_workflow_artifact_store(self, filename: str = "workflow-artifacts.sqlite3"):  # type: ignore[no-untyped-def]
        self.prepare()
        path = self.database_path(filename)
        result = open_workflow_artifact_store(path)
        if isinstance(result, WorkflowArtifactPersistenceFailure):
            raise AppContextError("store_unavailable")
        self._open_handles.append(result)
        return result

    def open_lecture_document_store(self, filename: str = "lecture-documents.sqlite3"):  # type: ignore[no-untyped-def]
        self.prepare()
        path = self.database_path(filename)
        result = open_lecture_document_store(path)
        if isinstance(result, LectureDocumentPersistenceFailure):
            raise AppContextError("store_unavailable")
        self._open_handles.append(result)
        return result

    def open_policy_content_store(self, filename: str = "policy-content.sqlite3"):  # type: ignore[no-untyped-def]
        self.prepare()
        path = self.database_path(filename)
        result = open_policy_content_store(path)
        if isinstance(result, PolicyPersistenceFailure):
            raise AppContextError("store_unavailable")
        self._open_handles.append(result)
        return result

    def open_course_workflow_association_store(self, filename: str = "course-workflow-associations.sqlite3"):  # type: ignore[no-untyped-def]
        self.prepare()
        path = self.database_path(filename)
        result = open_course_workflow_association_store(path)
        if isinstance(result, CourseWorkflowPersistenceFailure):
            raise AppContextError("store_unavailable")
        self._open_handles.append(result)
        return result

    def open_course_store(self, filename: str = "courses.sqlite3"):  # type: ignore[no-untyped-def]
        self.prepare()
        path = self.database_path(filename)
        from course_compiler.course_persistence import (
            CoursePersistenceFailure,
            open_course_store,
        )

        result = open_course_store(path)
        if isinstance(result, CoursePersistenceFailure):
            raise AppContextError("store_unavailable")
        self._open_handles.append(result)
        return result

    def open_course_job_store(self, filename: str = "course-jobs.sqlite3"):  # type: ignore[no-untyped-def]
        self.prepare()
        path = self.database_path(filename)
        from course_compiler.course_job_persistence import (
            CourseJobPersistenceFailure,
            open_course_job_store,
        )

        result = open_course_job_store(path)
        if isinstance(result, CourseJobPersistenceFailure):
            raise AppContextError("store_unavailable")
        self._open_handles.append(result)
        return result

    def open_semantic_work_store(self, filename: str = "semantic-work.sqlite3"):  # type: ignore[no-untyped-def]
        self.prepare()
        path = self.database_path(filename)
        from course_compiler.semantic_work_persistence import (
            SemanticWorkPersistenceFailure,
            open_semantic_work_store,
        )

        result = open_semantic_work_store(path)
        if isinstance(result, SemanticWorkPersistenceFailure):
            raise AppContextError("store_unavailable")
        self._open_handles.append(result)
        return result

    def open_build_record_store(self, filename: str = "build-records.sqlite3"):  # type: ignore[no-untyped-def]
        self.prepare()
        path = self.database_path(filename)
        from course_compiler.build_persistence import (
            BuildPersistenceFailure,
            open_build_record_store,
        )

        result = open_build_record_store(path)
        if isinstance(result, BuildPersistenceFailure):
            raise AppContextError("store_unavailable")
        self._open_handles.append(result)
        return result

    def cache_root_path(self) -> Path:
        """Return the memoized PDF cache directory under the data root."""
        path = self._config.data_root / "cache"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # --- Future seams (T047 legacy) / T049+ future ---

    def future_course_store_path(self) -> Path:
        """Return the path LocalCourseStore uses (now owned)."""

        return self.database_path("courses.sqlite3")

    def future_job_store_path(self) -> Path:
        """Return the path LocalCourseJobStore uses (now owned)."""

        return self.database_path("course-jobs.sqlite3")

    def future_semantic_work_store_path(self) -> Path:
        """Return the path LocalSemanticWorkStore uses (now owned)."""

        return self.database_path("semantic-work.sqlite3")

    def semantic_work_store_path(self) -> Path:
        """Return the path LocalSemanticWorkStore uses (owned)."""

        return self.database_path("semantic-work.sqlite3")

    def close(self) -> None:
        """Close all opened handles without exposing internal errors."""

        for handle in self._open_handles:
            try:
                close = getattr(handle, "close", None)
                if callable(close):
                    close()
            except Exception:
                continue
        self._open_handles.clear()

    def __enter__(self) -> AppContext:
        self.prepare()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore[no-untyped-def]
        self.close()


def create_app_context(config: AppConfig | None = None) -> AppContext:
    """Create an application context for the given or default config."""

    from .config import default_config as _default

    if config is None:
        config = _default()
    return AppContext(config)
