"""Frozen semantic-work protocol domain contracts (T049).

Transport-neutral control plane for semantic executor coordination.
No network, no inference, no provider SDK.
"""

from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass, fields
from typing import Literal, Protocol, TypeAlias


SEMANTIC_WORK_REQUEST_VERSION = "semantic-work-request/v1"
SEMANTIC_WORK_RESULT_VERSION = "semantic-work-result/v1"

SEMANTIC_KINDS = frozenset(
    {
        "source_assessment",
        "exam_priority_assessment",
        "lecture_map_generation",
        "lecture_generation",
        "visual_selection",
        "visual_placement",
        "semantic_review",
        "semantic_correction",
    }
)
SemanticKind: TypeAlias = Literal[
    "source_assessment",
    "exam_priority_assessment",
    "lecture_map_generation",
    "lecture_generation",
    "visual_selection",
    "visual_placement",
    "semantic_review",
    "semantic_correction",
]

_SAFE_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_CREATED_AT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_REVISION = 9_223_372_036_854_775_807

_DIAG_KINDS = {
    "invalid_semantic_request": ("input", "The semantic request is invalid."),
    "invalid_semantic_kind": ("input", "The semantic kind is invalid."),
    "invalid_semantic_result": ("input", "The semantic result is invalid."),
}


def _valid_safe_id(value: object) -> bool:
    return type(value) is str and value not in (".", "..") and _SAFE_ID_RE.fullmatch(value) is not None


def _valid_created_at(value: object) -> bool:
    return type(value) is str and _CREATED_AT_RE.fullmatch(value) is not None


def _valid_kind(value: object) -> bool:
    return type(value) is str and value in SEMANTIC_KINDS


def _valid_revision(value: object) -> bool:
    return type(value) is int and 0 <= value <= _MAX_REVISION


def _valid_digest(value: object) -> bool:
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class ProducedArtifactSpec:
    """One immutable artifact payload to be persisted via ingest_workflow_artifact."""

    kind: str
    artifact_id: str
    content_bytes: bytes  # repr=False via custom __repr__

    def __post_init__(self) -> None:
        if type(self.kind) is not str or not self.kind:
            raise ValueError("produced artifact kind is invalid")
        # restrict to known artifact kinds for safety, but allow generic for extensibility?
        # We enforce safe id for artifact_id
        if not _valid_safe_id(self.artifact_id):
            raise ValueError("produced artifact ID is invalid")
        if type(self.content_bytes) is not bytes or not self.content_bytes:
            raise ValueError("produced artifact bytes are invalid")

    def __repr__(self) -> str:
        return f"ProducedArtifactSpec(kind={self.kind!r}, artifact_id={self.artifact_id!r}, content_bytes=<redacted>)"


@dataclass(frozen=True, slots=True)
class ProducedDocumentSpec:
    """One document payload to be persisted as LectureDocument."""

    lecture_id: str
    source_text: str  # repr=False

    def __post_init__(self) -> None:
        # lecture_id grammar l[1-9][0-9]{0,3}
        if type(self.lecture_id) is not str or not re.fullmatch(r"l[1-9][0-9]{0,3}\Z", self.lecture_id):
            raise ValueError("produced document lecture ID is invalid")
        if type(self.source_text) is not str or not self.source_text:
            raise ValueError("produced document source text is invalid")
        # basic content safety: source_text must be non-empty and valid utf-8 (always)
        try:
            self.source_text.encode("utf-8")
        except Exception:
            raise ValueError("produced document source text is invalid") from None

    def __repr__(self) -> str:
        return f"ProducedDocumentSpec(lecture_id={self.lecture_id!r}, source_text=<redacted>)"


@dataclass(frozen=True, slots=True)
class SemanticDiagnostic:
    """One fixed semantic result diagnostic."""

    code: str
    message: str

    def __post_init__(self) -> None:
        if type(self.code) is not str or not self.code:
            raise ValueError("semantic diagnostic code is invalid")
        if type(self.message) is not str or not self.message:
            raise ValueError("semantic diagnostic message is invalid")


@dataclass(frozen=True, slots=True)
class SemanticWorkRequest:
    """Frozen durable request for one semantic unit (control plane only)."""

    request_version: Literal["semantic-work-request/v1"]
    request_id: str
    job_id: str
    workflow_id: str
    expected_revision: int
    operation_id: str
    kind: SemanticKind  # type: ignore[valid-type]
    input_refs: tuple[str, ...]
    created_at: str
    lease_expires_at: str | None

    def __post_init__(self) -> None:
        if type(self.request_version) is not str or self.request_version != SEMANTIC_WORK_REQUEST_VERSION:
            raise ValueError("semantic request version is unsupported")
        if not _valid_safe_id(self.request_id):
            raise ValueError("semantic request ID is invalid")
        if not _valid_safe_id(self.job_id):
            raise ValueError("semantic job ID is invalid")
        if not _valid_safe_id(self.workflow_id):
            raise ValueError("semantic workflow ID is invalid")
        if not _valid_revision(self.expected_revision):
            raise ValueError("semantic expected revision is invalid")
        if not _valid_safe_id(self.operation_id):
            raise ValueError("semantic operation ID is invalid")
        if not _valid_kind(self.kind):
            raise ValueError("semantic kind is invalid")
        if type(self.input_refs) is not tuple or any(type(x) is not str for x in self.input_refs):
            raise ValueError("semantic input_refs is invalid")
        if not _valid_created_at(self.created_at):
            raise ValueError("semantic created_at is invalid")
        if self.lease_expires_at is not None and not _valid_created_at(self.lease_expires_at):
            raise ValueError("semantic lease_expires_at is invalid")

    def __repr__(self) -> str:
        return (
            f"SemanticWorkRequest(request_id={self.request_id!r}, kind={self.kind!r}, "
            f"job_id={self.job_id!r}, workflow_id={self.workflow_id!r}, expected_revision={self.expected_revision!r}, "
            f"operation_id={self.operation_id!r})"
        )


@dataclass(frozen=True, slots=True)
class SemanticWorkResult:
    """Frozen result for one semantic request (control plane)."""

    result_version: Literal["semantic-work-result/v1"]
    request_id: str
    operation_id: str
    kind: SemanticKind  # type: ignore[valid-type]
    produced_artifacts: tuple[ProducedArtifactSpec, ...]
    produced_documents: tuple[ProducedDocumentSpec, ...]
    diagnostics: tuple[SemanticDiagnostic, ...]
    priority_subject_sha256: str | None
    map_subject_sha256: str | None
    candidate_subject_sha256: str | None
    request_revision: int

    def __post_init__(self) -> None:
        if type(self.result_version) is not str or self.result_version != SEMANTIC_WORK_RESULT_VERSION:
            raise ValueError("semantic result version is unsupported")
        if not _valid_safe_id(self.request_id):
            raise ValueError("semantic result request ID is invalid")
        if not _valid_safe_id(self.operation_id):
            raise ValueError("semantic result operation ID is invalid")
        if not _valid_kind(self.kind):
            raise ValueError("semantic result kind is invalid")
        if type(self.produced_artifacts) is not tuple or any(type(x) is not ProducedArtifactSpec for x in self.produced_artifacts):
            raise ValueError("semantic produced_artifacts is invalid")
        # Revalidate each spec via its own constructor (already validated)
        for spec in self.produced_artifacts:
            try:
                ProducedArtifactSpec(spec.kind, spec.artifact_id, spec.content_bytes)
            except Exception:
                raise ValueError("semantic produced_artifacts is invalid") from None
        if type(self.produced_documents) is not tuple or any(type(x) is not ProducedDocumentSpec for x in self.produced_documents):
            raise ValueError("semantic produced_documents is invalid")
        for spec in self.produced_documents:
            try:
                ProducedDocumentSpec(spec.lecture_id, spec.source_text)
            except Exception:
                raise ValueError("semantic produced_documents is invalid") from None
        if type(self.diagnostics) is not tuple or any(type(x) is not SemanticDiagnostic for x in self.diagnostics):
            raise ValueError("semantic diagnostics is invalid")
        for d in self.diagnostics:
            try:
                SemanticDiagnostic(d.code, d.message)
            except Exception:
                raise ValueError("semantic diagnostics is invalid") from None
        if self.priority_subject_sha256 is not None and not _valid_digest(self.priority_subject_sha256):
            raise ValueError("priority subject hash is invalid")
        if self.map_subject_sha256 is not None and not _valid_digest(self.map_subject_sha256):
            raise ValueError("map subject hash is invalid")
        if self.candidate_subject_sha256 is not None and not _valid_digest(self.candidate_subject_sha256):
            raise ValueError("candidate subject hash is invalid")
        if not _valid_revision(self.request_revision):
            raise ValueError("semantic request_revision is invalid")
        # Kind-specific payload expectations: at least basic shape
        # For lecture_generation, require one produced document; for others allow but tests will enforce
        # We keep minimal here; submit_semantic_result will enforce stricter.

    def __repr__(self) -> str:
        return (
            f"SemanticWorkResult(request_id={self.request_id!r}, kind={self.kind!r}, "
            f"operation_id={self.operation_id!r}, request_revision={self.request_revision!r}, "
            f"artifacts={len(self.produced_artifacts)}, documents={len(self.produced_documents)})"
        )


# Transport-neutral executor boundary (R001 §E)
class SemanticExecutor(Protocol):
    """Transport-neutral semantic executor trait."""

    def execute(self, request: SemanticWorkRequest) -> SemanticWorkResult:
        ...

    def can_handle_kind(self, kind: str) -> bool:  # optional helper
        ...


# Deterministic in-process ScriptProvider for fixtures/testing (T049)

class ScriptProvider:
    """Deterministic in-process provider returning synthetic results (no network)."""

    def __init__(self, *, map_size: int = 3, lecture_prefix: str = "Synthetic") -> None:
        if type(map_size) is not int or not (1 <= map_size <= 20):
            raise ValueError("map_size is invalid")
        if type(lecture_prefix) is not str or not lecture_prefix:
            raise ValueError("lecture_prefix is invalid")
        self._map_size = map_size
        self._lecture_prefix = lecture_prefix

    def can_handle_kind(self, kind: str) -> bool:
        return kind in SEMANTIC_KINDS

    def execute(self, request: SemanticWorkRequest) -> SemanticWorkResult:
        if type(request) is not SemanticWorkRequest:
            raise TypeError("request must be exactly SemanticWorkRequest")
        # Revalidate copy
        try:
            SemanticWorkRequest(
                request.request_version,
                request.request_id,
                request.job_id,
                request.workflow_id,
                request.expected_revision,
                request.operation_id,
                request.kind,  # type: ignore[arg-type]
                request.input_refs,
                request.created_at,
                request.lease_expires_at,
            )
        except Exception:
            raise ValueError("request is invalid") from None

        kind = request.kind
        # Deterministic synthetic payloads
        artifacts: list[ProducedArtifactSpec] = []
        documents: list[ProducedDocumentSpec] = []
        priority_hash = None
        map_hash = None
        candidate_hash = None

        if kind == "source_assessment":
            artifacts.append(ProducedArtifactSpec("source_assessment", f"art-{request.request_id[:8]}-sa", f"synthetic source_assessment for {request.workflow_id}".encode()))
            artifacts.append(ProducedArtifactSpec("priority_proposal", f"art-{request.request_id[:8]}-pp", f"synthetic priority_proposal {request.job_id}".encode()))
            artifacts.append(ProducedArtifactSpec("evidence_hierarchy", f"art-{request.request_id[:8]}-eh", f"synthetic evidence_hierarchy {request.workflow_id}".encode()))
            # For source_assessment, no subject hash needed yet (priority subject will be derived after)
        elif kind == "exam_priority_assessment":
            artifacts.append(ProducedArtifactSpec("priority_proposal", f"art-{request.request_id[:8]}-pp2", f"synthetic priority2 {request.job_id}".encode()))
            artifacts.append(ProducedArtifactSpec("evidence_hierarchy", f"art-{request.request_id[:8]}-eh2", f"synthetic eh2 {request.workflow_id}".encode()))
            # Use expected priority subject from input_refs if provided (echo), else dummy valid digest
            if request.input_refs and _valid_digest(request.input_refs[0]):
                priority_hash = request.input_refs[0]
            else:
                priority_hash = hashlib.sha256(f"priority-{request.request_id}".encode()).hexdigest()
        elif kind == "lecture_map_generation":
            reserved_ids: tuple[str, ...] = ()
            policy_sha: str | None = None
            if request.input_refs:
                if _SHA256_RE.fullmatch(request.input_refs[0]):
                    policy_sha = request.input_refs[0]
                reserved_ids = tuple(
                    r for r in request.input_refs[1:] if re.fullmatch(r"l[1-9][0-9]{0,3}\Z", r)
                )
            if policy_sha is None:
                try:
                    from pathlib import Path

                    repo_root = Path(__file__).resolve().parent.parent
                    skill_file = repo_root / "skills" / "course-compiler" / "SKILL.md"
                    policy_sha = hashlib.sha256(skill_file.read_bytes()).hexdigest()
                except Exception:
                    policy_sha = "0" * 64

            total_size = max(self._map_size, len(reserved_ids))
            lecture_ids = tuple(f"l{i}" for i in range(1, total_size + 1))
            content = ("|".join(lecture_ids)).encode("utf-8")
            art_id = f"art-{request.request_id[:8]}-lm"
            artifacts.append(ProducedArtifactSpec("lecture_map", art_id, content))

            try:
                from course_compiler.workflow import (
                    LectureMapRecord,
                    PolicyReference,
                    WorkflowArtifactReference,
                    map_subject_sha256,
                )
                from course_compiler.workflow_policy import ARTIFACT_PRODUCERS, POLICY_VERSIONS

                sha = hashlib.sha256(content).hexdigest()
                producer = ARTIFACT_PRODUCERS["lecture_map"]
                map_ref = WorkflowArtifactReference(
                    "workflow-artifact-reference/v1", art_id, "lecture_map", sha, producer
                )
                pol_ref = PolicyReference("lecture_mapping", POLICY_VERSIONS["lecture_mapping"], policy_sha)
                rec = LectureMapRecord(
                    map_reference=map_ref,
                    lecture_ids=lecture_ids,
                    status="proposed",
                    approval=None,
                    reserved_lecture_ids=reserved_ids,
                )
                map_hash = map_subject_sha256(rec, pol_ref)
            except Exception:
                map_hash = hashlib.sha256(f"map-{request.request_id}".encode()).hexdigest()
        elif kind in ("lecture_generation", "semantic_correction"):
            # Determine lecture_id from input_refs if present else deterministic fallback
            lecture_id = "l1"
            for ref in request.input_refs:
                if re.fullmatch(r"l[1-9][0-9]{0,3}\Z", ref):
                    lecture_id = ref
                    break
            label = "Corrected" if kind == "semantic_correction" else self._lecture_prefix
            text = f"# {label} Lecture {lecture_id}\n\nInvented content for {lecture_id} in job {request.job_id}. Reference {request.request_id}."
            documents.append(ProducedDocumentSpec(lecture_id, text))
            # Compute correct candidate subject hash via workflow helper
            try:
                from course_compiler.rendering import DocumentReference as _DocRef
                from course_compiler.workflow import candidate_subject_sha256 as _cand_hash

                _sha = hashlib.sha256(text.encode()).hexdigest()
                _ref = _DocRef("lecture-document/v1", lecture_id, int(lecture_id[1:]), _sha)
                candidate_hash = _cand_hash(_ref)
            except Exception:
                candidate_hash = hashlib.sha256(f"candidate-{lecture_id}-{text[:10]}".encode()).hexdigest()
        elif kind in ("visual_selection", "visual_placement"):
            # For now no visual payload; produce empty diagnostic
            pass
        elif kind == "semantic_review":
            # Produce empty correction (no new artifacts)
            pass
        else:
            # Unknown kind should have been rejected earlier; but for safety produce empty
            pass

        return SemanticWorkResult(
            result_version=SEMANTIC_WORK_RESULT_VERSION,  # type: ignore[arg-type]
            request_id=request.request_id,
            operation_id=request.operation_id,
            kind=kind,  # type: ignore[arg-type]
            produced_artifacts=tuple(artifacts),
            produced_documents=tuple(documents),
            diagnostics=(),
            priority_subject_sha256=priority_hash,
            map_subject_sha256=map_hash,
            candidate_subject_sha256=candidate_hash,
            request_revision=request.expected_revision,
        )
