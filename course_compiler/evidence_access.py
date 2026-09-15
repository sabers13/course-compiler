"""T051 EvidenceAccess boundary (read-only).

Frozen R001/the frozen v0.1 architecture separates the bounded control plane
(``SemanticWorkRequest.input_refs`` + a small kind-relevant continuation
projection) from the private-evidence data plane. This module resolves the
exact evidence a semantic executor needs for the *current* pending request
into immutable, digest-bound ``EvidenceItem`` references, and separately
serves the exact bytes for one such reference after re-verifying its digest.

No caller-selected filesystem path is ever accepted. No arbitrary previous
lecture, artifact, or source is exposed beyond what one exact kind-specific
policy below names for the current request. Evidence identity is always
recomputed deterministically from ``(kind, continuation)`` — never trusted
from caller input — so an evidence ID can only ever resolve to a byte range
that the current request's own policy names.

Accepted durable sources only: ``LocalSourceEvidenceStore`` for source PDF
bytes; the already-reopened, already-revalidated T030
``CourseWorkflowContinuation`` for artifact/document text (no second store
open is needed for those — the continuation already holds exact validated
UTF-8 content bound to its own reference).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal, TypeAlias

from .course_workflow_operations import (
    CourseWorkflowContinuation,
    ReopenedLectureDocument,
    ReopenedWorkflowArtifact,
)
from .semantic_work import SEMANTIC_KINDS
from .source_persistence import LocalSourceEvidenceStore, SourceEvidencePayload, SourcePersistenceFailure

__all__ = [
    "EvidenceItem",
    "EvidenceAccessDiagnostic",
    "EvidenceAccessFailure",
    "EvidenceAccessResult",
    "resolve_request_evidence",
    "read_evidence_item",
]

EvidenceKind: TypeAlias = Literal["source_pdf", "artifact_text", "document_text"]

_MEDIA_TYPES: dict[str, str] = {
    "source_pdf": "application/pdf",
    "artifact_text": "text/plain; charset=utf-8",
    "document_text": "text/markdown; charset=utf-8",
}

_DIAGNOSTICS = {
    "invalid_evidence_input": "Evidence access input is invalid.",
    "unsupported_kind": "The semantic kind has no evidence policy.",
    "evidence_not_found": "The requested evidence item does not belong to the current request.",
    "source_store_failed": "The source-evidence store failed.",
    "source_not_found": "The referenced source evidence is unavailable.",
    "evidence_integrity_mismatch": "The evidence bytes do not match their expected digest.",
}


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One immutable, digest-bound evidence reference. Carries no bytes."""

    evidence_id: str
    evidence_kind: EvidenceKind  # type: ignore[valid-type]
    media_type: str
    content_sha256: str
    byte_length: int
    label: str

    def __post_init__(self) -> None:
        import re

        if type(self.evidence_id) is not str or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,79}", self.evidence_id):
            raise ValueError("evidence item ID is invalid")
        if type(self.evidence_kind) is not str or self.evidence_kind not in _MEDIA_TYPES:
            raise ValueError("evidence item kind is invalid")
        if self.media_type != _MEDIA_TYPES[self.evidence_kind]:
            raise ValueError("evidence item media type is invalid")
        if type(self.content_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", self.content_sha256):
            raise ValueError("evidence item digest is invalid")
        if type(self.byte_length) is not int or self.byte_length <= 0:
            raise ValueError("evidence item byte length is invalid")
        if type(self.label) is not str or not self.label or len(self.label) > 80:
            raise ValueError("evidence item label is invalid")


@dataclass(frozen=True, slots=True)
class EvidenceAccessDiagnostic:
    code: str
    message: str

    def __post_init__(self) -> None:
        if self.code not in _DIAGNOSTICS:
            raise ValueError("evidence access diagnostic code is not registered")
        if self.message != _DIAGNOSTICS[self.code]:
            raise ValueError("evidence access diagnostic message is not registered")


@dataclass(frozen=True, slots=True)
class EvidenceAccessFailure:
    status: Literal["evidence_access_failed"]
    diagnostics: tuple[EvidenceAccessDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "evidence_access_failed":
            raise ValueError("evidence access failure status is invalid")
        if type(self.diagnostics) is not tuple or len(self.diagnostics) != 1:
            raise ValueError("evidence access failure diagnostics are invalid")


EvidenceAccessResult: TypeAlias = tuple[EvidenceItem, ...] | EvidenceAccessFailure


def _failure(code: str) -> EvidenceAccessFailure:
    return EvidenceAccessFailure(
        status="evidence_access_failed",
        diagnostics=(EvidenceAccessDiagnostic(code, _DIAGNOSTICS[code]),),
    )


def _artifact_item(artifact: ReopenedWorkflowArtifact | None, *, label: str) -> EvidenceItem | None:
    if artifact is None:
        return None
    content_bytes = artifact.content.encode("utf-8")
    return EvidenceItem(
        evidence_id=f"art-{artifact.reference.artifact_id}",
        evidence_kind="artifact_text",
        media_type=_MEDIA_TYPES["artifact_text"],
        content_sha256=artifact.reference.content_sha256,
        byte_length=len(content_bytes),
        label=label,
    )


def _document_item(document: ReopenedLectureDocument | None, *, label: str) -> EvidenceItem | None:
    if document is None:
        return None
    content_bytes = document.source_text.encode("utf-8")
    return EvidenceItem(
        evidence_id=f"doc-{document.document_reference.document_id}",
        evidence_kind="document_text",
        media_type=_MEDIA_TYPES["document_text"],
        content_sha256=document.document_reference.content_sha256,
        byte_length=len(content_bytes),
        label=label,
    )


def _source_items(
    continuation: CourseWorkflowContinuation, *, source_store: LocalSourceEvidenceStore
) -> list[EvidenceItem] | EvidenceAccessFailure:
    items: list[EvidenceItem] = []
    for index, reference in enumerate(continuation.source_references, start=1):
        loaded = source_store.load(reference)
        if isinstance(loaded, SourcePersistenceFailure):
            code = getattr(loaded.diagnostics[0], "code", None) if loaded.diagnostics else None
            if code == "source_not_found":
                return _failure("source_not_found")
            return _failure("source_store_failed")
        if type(loaded) is not SourceEvidencePayload:
            return _failure("source_store_failed")
        items.append(
            EvidenceItem(
                evidence_id=f"src-{reference.source_id}",
                evidence_kind="source_pdf",
                media_type=_MEDIA_TYPES["source_pdf"],
                content_sha256=reference.content_sha256,
                byte_length=len(loaded.payload),
                label=f"source-{index}",
            )
        )
    return items


def resolve_request_evidence(
    kind: str,
    continuation: CourseWorkflowContinuation,
    *,
    source_store: LocalSourceEvidenceStore,
) -> EvidenceAccessResult:
    """Return the exact bounded evidence manifest for one SemanticKind.

    Deterministic in ``(kind, continuation)``: the same pending request
    always resolves to the same evidence identities. Never reads a caller-
    selected filesystem path; every byte range traces back to an accepted
    T008/T009/T010 store reopened through the already-validated T030
    continuation projection.

    """

    if type(kind) is not str or kind not in SEMANTIC_KINDS:
        return _failure("unsupported_kind")
    if type(continuation) is not CourseWorkflowContinuation:
        return _failure("invalid_evidence_input")
    if type(source_store) is not LocalSourceEvidenceStore:
        raise TypeError("source_store must be exactly LocalSourceEvidenceStore")

    items: list[EvidenceItem] = []

    if kind == "source_assessment":
        sources = _source_items(continuation, source_store=source_store)
        if isinstance(sources, EvidenceAccessFailure):
            return sources
        items.extend(sources)

    elif kind == "exam_priority_assessment":
        for artifact, label in (
            (continuation.source_assessment, "source_assessment"),
            (continuation.priority_proposal, "priority_proposal"),
            (continuation.evidence_hierarchy, "evidence_hierarchy"),
        ):
            item = _artifact_item(artifact, label=label)
            if item is not None:
                items.append(item)
        sources = _source_items(continuation, source_store=source_store)
        if isinstance(sources, EvidenceAccessFailure):
            return sources
        items.extend(sources)

    elif kind == "lecture_map_generation":
        for artifact, label in (
            (continuation.source_assessment, "source_assessment"),
            (continuation.priority_proposal, "priority_proposal"),
            (continuation.evidence_hierarchy, "evidence_hierarchy"),
            (continuation.priority_evidence_review, "priority_evidence_review"),
        ):
            item = _artifact_item(artifact, label=label)
            if item is not None:
                items.append(item)
        if continuation.map_reopen is not None:
            item = _artifact_item(continuation.map_reopen.baseline_lecture_map, label="baseline_lecture_map")
            if item is not None:
                items.append(item)
        sources = _source_items(continuation, source_store=source_store)
        if isinstance(sources, EvidenceAccessFailure):
            return sources
        items.extend(sources)

    elif kind == "lecture_generation":
        for artifact, label in (
            (continuation.lecture_map, "lecture_map"),
            (continuation.priority_proposal, "priority_proposal"),
        ):
            item = _artifact_item(artifact, label=label)
            if item is not None:
                items.append(item)
        sources = _source_items(continuation, source_store=source_store)
        if isinstance(sources, EvidenceAccessFailure):
            return sources
        items.extend(sources)

    elif kind in ("visual_selection", "visual_placement"):
        sources = _source_items(continuation, source_store=source_store)
        if isinstance(sources, EvidenceAccessFailure):
            return sources
        items.extend(sources)
        if continuation.active_candidate is not None:
            item = EvidenceItem(
                evidence_id=f"doc-{continuation.active_candidate.document_reference.document_id}",
                evidence_kind="document_text",
                media_type=_MEDIA_TYPES["document_text"],
                content_sha256=continuation.active_candidate.document_reference.content_sha256,
                byte_length=len(continuation.active_candidate.source_text.encode("utf-8")),
                label="active_candidate",
            )
            items.append(item)
        elif continuation.active_lecture_id is not None:
            active = next(
                (
                    doc
                    for doc in continuation.accepted_documents
                    if doc.document_reference.document_id == continuation.active_lecture_id
                ),
                None,
            )
            item = _document_item(active, label="active_lecture")
            if item is not None:
                items.append(item)

    elif kind in ("semantic_review", "semantic_correction"):
        for artifact, label in (
            (continuation.lecture_map, "lecture_map"),
            (continuation.priority_proposal, "priority_proposal"),
        ):
            item = _artifact_item(artifact, label=label)
            if item is not None:
                items.append(item)
        for document in continuation.accepted_documents:
            item = _document_item(document, label=f"accepted-{document.document_reference.document_id}")
            if item is not None:
                items.append(item)

    else:
        return _failure("unsupported_kind")

    # De-duplicate by evidence_id while preserving first-seen order: two
    # policy branches may legitimately name the same artifact.
    seen: set[str] = set()
    deduped: list[EvidenceItem] = []
    for item in items:
        if item.evidence_id in seen:
            continue
        seen.add(item.evidence_id)
        deduped.append(item)
    return tuple(deduped)


def read_evidence_item(
    evidence_id: str,
    kind: str,
    continuation: CourseWorkflowContinuation,
    *,
    source_store: LocalSourceEvidenceStore,
) -> tuple[bytes, str] | EvidenceAccessFailure:
    """Return ``(bytes, media_type)`` for one evidence ID of the current request.

    The manifest is recomputed from the exact same deterministic policy used
    for the control-plane response; an ``evidence_id`` not present in that
    recomputed manifest is rejected. This makes it impossible for a caller
    to enumerate store contents by guessing an ID: only IDs the current
    request's own policy names can ever resolve.

    """

    if type(evidence_id) is not str or not evidence_id:
        return _failure("invalid_evidence_input")
    manifest = resolve_request_evidence(
        kind,
        continuation,
        source_store=source_store,
    )
    if isinstance(manifest, EvidenceAccessFailure):
        return manifest
    match = next((item for item in manifest if item.evidence_id == evidence_id), None)
    if match is None:
        return _failure("evidence_not_found")

    if match.evidence_kind == "source_pdf":
        source_id = match.evidence_id[len("src-") :]
        reference = next(
            (ref for ref in continuation.source_references if ref.source_id == source_id),
            None,
        )
        if reference is None:
            return _failure("evidence_not_found")
        loaded = source_store.load(reference)
        if isinstance(loaded, SourcePersistenceFailure):
            return _failure("source_store_failed")
        if type(loaded) is not SourceEvidencePayload:
            return _failure("source_store_failed")
        content = loaded.payload
    elif match.evidence_kind == "artifact_text":
        artifact_id = match.evidence_id[len("art-") :]
        artifact = next(
            (
                candidate
                for candidate in (
                    continuation.source_assessment,
                    continuation.priority_proposal,
                    continuation.evidence_hierarchy,
                    continuation.priority_evidence_review,
                    continuation.lecture_map,
                    continuation.map_reopen.baseline_lecture_map if continuation.map_reopen is not None else None,
                )
                if candidate is not None and candidate.reference.artifact_id == artifact_id
            ),
            None,
        )
        if artifact is None:
            return _failure("evidence_not_found")
        content = artifact.content.encode("utf-8")
    else:  # document_text
        lecture_id = match.evidence_id[len("doc-") :]
        document = next(
            (
                doc
                for doc in continuation.accepted_documents
                if doc.document_reference.document_id == lecture_id
            ),
            None,
        )
        if document is not None:
            content = document.source_text.encode("utf-8")
        elif (
            continuation.active_candidate is not None
            and continuation.active_candidate.document_reference.document_id == lecture_id
        ):
            content = continuation.active_candidate.source_text.encode("utf-8")
        else:
            return _failure("evidence_not_found")

    if hashlib.sha256(content).hexdigest() != match.content_sha256:
        return _failure("evidence_integrity_mismatch")
    if len(content) != match.byte_length:
        return _failure("evidence_integrity_mismatch")
    return content, match.media_type
