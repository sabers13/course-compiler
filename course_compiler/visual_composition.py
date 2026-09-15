"""Deterministic placement-aware visual composition of one compiler input.

This is a pure in-memory *application* composition boundary. It accepts
exact `LectureDocument` values, zero or more exact `VisualPlacement` values,
and operation-local exact PNG bytes keyed by `AssetReference`, and returns
either a canonical T024 `CompilerInputBundle` whose root TeX references
deterministic PNG companion files at exact renderer-safe source anchors, or
one fixed content-safe `VisualCompositionFailure`.

The intended dataflow is:

    LectureDocument* -> T004 render -> T005 assemble -> accepted combined root
    VisualPlacement* + AssetReference->bytes -> this module -> parser-safe
    insertion + PNG filename binding + graphicx root transform -> T024
    CompilerInputBundle

This module depends downward on `contracts`, `rendering`, `legacy_renderer`,
`assembly`, `asset`, `visual_placement`, and `compilation`. Nothing below it
depends on this module: `legacy_renderer.py`, `assembly.py`, and
`compilation.py` do not import it. It owns no filesystem staging, no
persistence, no compiler execution, and no workflow behavior; it ends at
`CompilerInputBundle`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypeAlias

from .asset import AssetReference
from .assembly import (
    AssemblyFailure,
    CourseTexAssemblySuccess,
    LogicalTexFile,
    assemble_course_tex,
)
from .compilation import CompilerCompanionFile, CompilerInputBundle
from .contracts import LectureDocument
from .legacy_renderer import (
    LegacyMarkdownTexRenderer,
    _render_with_insertions,
    _renderer_safe_offsets,
)
from .rendering import DocumentReference, RejectedLecture, RenderedLecture, RendererFailure
from .visual_placement import VisualPlacement


__all__ = [
    "VisualCompositionDiagnostic",
    "VisualCompositionFailure",
    "VisualCompositionResult",
    "compose_course_compiler_input",
]


_DIAGNOSTIC_CLASSIFICATIONS: Mapping[str, str] = {
    "invalid_visual_composition_input": "input",
    "empty_document_collection": "input",
    "duplicate_visual_placement": "placement",
    "placement_document_mismatch": "placement",
    "placement_offset_out_of_range": "placement",
    "placement_offset_not_renderer_safe": "placement",
    "invalid_asset_payload": "asset",
    "duplicate_asset_payload": "asset",
    "conflicting_asset_payload": "asset",
    "asset_payload_digest_mismatch": "asset",
    "unsupported_visual_asset_format": "asset",
    "missing_asset_payload": "asset",
    "unused_asset_payload": "asset",
    "lecture_render_rejected": "render",
    "lecture_render_failed": "render",
    "renderer_invariant_failure": "render",
    "course_tex_assembly_failed": "assembly",
    "visual_tex_invariant_failed": "assembly",
    "compiler_input_construction_failed": "assembly",
    "visual_composition_exception": "exception",
}

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_TEX_END_MARKER = "\\end{document}\n"
_TEX_BODY_PREFIX_MARKER = "\\tableofcontents\n\\newpage\n"
_BEGIN_DOCUMENT = "\\begin{document}\n"
_CLEARPAGE = "\\clearpage\n"
_GRAPHICX_PACKAGE = "\\usepackage{graphicx}\n"


@dataclass(frozen=True, slots=True)
class VisualCompositionDiagnostic:
    """One registered, fixed, content-free visual-composition diagnostic."""

    code: str
    classification: Literal["input", "placement", "asset", "render", "assembly", "exception"]
    position: int | None

    def __post_init__(self) -> None:
        if self.code not in _DIAGNOSTIC_CLASSIFICATIONS:
            raise ValueError("visual composition diagnostic code is not registered")
        if self.classification != _DIAGNOSTIC_CLASSIFICATIONS[self.code]:
            raise ValueError("visual composition diagnostic classification is not registered")
        if self.position is not None and (
            isinstance(self.position, bool)
            or not isinstance(self.position, int)
            or self.position < 1
        ):
            raise ValueError("visual composition diagnostic position must be a one-based integer or None")


@dataclass(frozen=True, slots=True)
class VisualCompositionFailure:
    """A content-safe failure with no source text, asset bytes, or TeX."""

    status: Literal["visual_composition_failed"]
    diagnostics: tuple[VisualCompositionDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.status != "visual_composition_failed":
            raise ValueError("visual composition failure status is invalid")
        if (
            type(self.diagnostics) is not tuple
            or not self.diagnostics
            or any(type(item) is not VisualCompositionDiagnostic for item in self.diagnostics)
        ):
            raise ValueError("visual composition failure diagnostics are invalid")


VisualCompositionResult: TypeAlias = CompilerInputBundle | VisualCompositionFailure


def compose_course_compiler_input(
    documents: tuple[LectureDocument, ...],
    *,
    placements: tuple[VisualPlacement, ...],
    asset_bytes: Mapping[AssetReference, bytes],
) -> VisualCompositionResult:
    """Compose one deterministic placement-aware T024 compiler input.

    Renders `documents` through the unchanged accepted T004 renderer and
    assembles them through the unchanged accepted T005 assembly, then, for
    any exact `placements` targeting them, splices already-formed
    non-floating `\\includegraphics` TeX at exact renderer-safe source
    offsets and binds each unique referenced asset to a deterministic
    `assets/<sha256>.png` companion. With zero placements and empty
    `asset_bytes`, the result is byte-identical to the accepted T005 combined
    root with no companions and no `graphicx`.
    """

    if type(documents) is not tuple:
        raise TypeError("documents must be exactly tuple[LectureDocument, ...]")
    if type(placements) is not tuple:
        raise TypeError("placements must be exactly tuple[VisualPlacement, ...]")
    if not isinstance(asset_bytes, Mapping):
        raise TypeError("asset_bytes must be a Mapping[AssetReference, bytes]")

    try:
        return _compose_validated(documents, placements, asset_bytes)
    except Exception:
        return _failure("visual_composition_exception")


def _compose_validated(
    documents: tuple[LectureDocument, ...],
    placements: tuple[VisualPlacement, ...],
    asset_bytes: Mapping[AssetReference, bytes],
) -> VisualCompositionResult:
    if not documents:
        return _failure("empty_document_collection")
    for position, document in enumerate(documents, start=1):
        if type(document) is not LectureDocument:
            return _failure("invalid_visual_composition_input", position)
    for position, item in enumerate(placements, start=1):
        if type(item) is not VisualPlacement:
            return _failure("invalid_visual_composition_input", position)

    # Caller tuple order is not semantic, including for which failure is
    # reported when more than one placement is invalid: every subsequent
    # step (duplicate detection, semantic validation, and the eventual
    # bundle construction) walks this canonical order instead of the
    # caller-supplied order.
    canonical_placements = tuple(sorted(placements, key=_canonical_placement_key))

    duplicate_position = _first_duplicate_position(canonical_placements)
    if duplicate_position is not None:
        return _failure("duplicate_visual_placement", duplicate_position)

    renderer = LegacyMarkdownTexRenderer()
    results: list[RenderedLecture] = []
    for position, document in enumerate(documents, start=1):
        result = renderer.render(document)
        if type(result) is RejectedLecture:
            return _failure("lecture_render_rejected", position)
        if type(result) is RendererFailure:
            return _failure("lecture_render_failed", position)
        if type(result) is not RenderedLecture:
            return _failure("renderer_invariant_failure", position)
        results.append(result)

    references = tuple(result.document for result in results)
    assembly_result = assemble_course_tex(references, tuple(results))
    if type(assembly_result) is AssemblyFailure:
        return _failure("course_tex_assembly_failed")
    if type(assembly_result) is not CourseTexAssemblySuccess:
        return _failure("renderer_invariant_failure")

    documents_by_reference = dict(zip(references, documents))
    ordered_results = tuple(sorted(results, key=lambda item: item.document.order))

    placement_failure = _validate_placements(canonical_placements, documents_by_reference)
    if placement_failure is not None:
        return placement_failure

    asset_snapshot = _snapshot_asset_bytes(asset_bytes)
    if isinstance(asset_snapshot, VisualCompositionFailure):
        return asset_snapshot

    asset_failure = _validate_assets(canonical_placements, asset_snapshot)
    if asset_failure is not None:
        return asset_failure

    return _build_bundle(
        ordered_results,
        documents_by_reference,
        canonical_placements,
        asset_snapshot,
        assembly_result.combined,
    )


def _canonical_placement_key(item: VisualPlacement) -> tuple[int, int, str, str, str]:
    """A total, content-free order over exact `VisualPlacement` values.

    Ordinary valid placements sort by document order, then source-text
    offset, then asset digest, exactly as the T025 canonical placement order
    requires. `document_reference.document_id` and
    `document_reference.content_sha256` are appended only as deterministic
    tie-breakers -- using only existing content-free `DocumentReference`
    fields, never a new identity -- so that even a malformed or
    document-mismatched placement still has one fully deterministic
    position, independent of caller tuple order.
    """

    reference = item.document_reference
    return (
        reference.order,
        item.source_text_offset,
        item.asset_reference.content_sha256,
        reference.document_id,
        reference.content_sha256,
    )


def _first_duplicate_position(placements: tuple[VisualPlacement, ...]) -> int | None:
    seen: set[VisualPlacement] = set()
    for position, item in enumerate(placements, start=1):
        if item in seen:
            return position
        seen.add(item)
    return None


def _validate_placements(
    placements: tuple[VisualPlacement, ...],
    documents_by_reference: Mapping[DocumentReference, LectureDocument],
) -> VisualCompositionFailure | None:
    safe_offsets_cache: dict[DocumentReference, frozenset[int]] = {}
    for position, item in enumerate(placements, start=1):
        document = documents_by_reference.get(item.document_reference)
        if document is None:
            return _failure("placement_document_mismatch", position)
        offset = item.source_text_offset
        if offset > len(document.source_text):
            return _failure("placement_offset_out_of_range", position)
        safe_offsets = safe_offsets_cache.get(item.document_reference)
        if safe_offsets is None:
            safe_offsets = _renderer_safe_offsets(document.source_text)
            safe_offsets_cache[item.document_reference] = safe_offsets
        if offset not in safe_offsets:
            return _failure("placement_offset_not_renderer_safe", position)
    return None


def _is_exact_asset_reference(value: object) -> bool:
    if type(value) is not AssetReference:
        return False
    try:
        AssetReference(value.reference_version, value.content_sha256)
    except Exception:
        return False
    return True


def _snapshot_asset_bytes(
    asset_bytes: Mapping[AssetReference, bytes]
) -> dict[AssetReference, bytes] | VisualCompositionFailure:
    """Snapshot the operation-local mapping, failing closed on adversarial iteration.

    Ordinary Python dictionaries naturally collapse duplicate keys, so this
    path is only reachable for a custom `Mapping` whose `.items()` exposes
    the same logical `AssetReference` more than once.
    """

    snapshot: dict[AssetReference, bytes] = {}
    position = 0
    for key, value in asset_bytes.items():
        position += 1
        if not _is_exact_asset_reference(key):
            return _failure("invalid_asset_payload", position)
        if type(value) is not bytes:
            return _failure("invalid_asset_payload", position)
        if key in snapshot:
            if snapshot[key] == value:
                return _failure("duplicate_asset_payload", position)
            return _failure("conflicting_asset_payload", position)
        snapshot[key] = value
    return snapshot


def _is_valid_png(payload: bytes) -> bool:
    """A narrow PNG-signature-plus-IHDR check; no third-party image library."""

    if not payload.startswith(_PNG_SIGNATURE):
        return False
    header_end = len(_PNG_SIGNATURE) + 8 + 13
    if len(payload) < header_end:
        return False
    chunk_length = int.from_bytes(payload[len(_PNG_SIGNATURE) : len(_PNG_SIGNATURE) + 4], "big")
    chunk_type = payload[len(_PNG_SIGNATURE) + 4 : len(_PNG_SIGNATURE) + 8]
    return chunk_type == b"IHDR" and chunk_length == 13


def _validate_assets(
    placements: tuple[VisualPlacement, ...],
    asset_snapshot: Mapping[AssetReference, bytes],
) -> VisualCompositionFailure | None:
    required = {item.asset_reference for item in placements}

    for position, reference in enumerate(
        sorted(asset_snapshot, key=lambda item: item.content_sha256), start=1
    ):
        payload = asset_snapshot[reference]
        if hashlib.sha256(payload).hexdigest() != reference.content_sha256:
            return _failure("asset_payload_digest_mismatch", position)
        if not _is_valid_png(payload):
            return _failure("unsupported_visual_asset_format", position)

    for position, reference in enumerate(sorted(required, key=lambda item: item.content_sha256), start=1):
        if reference not in asset_snapshot:
            return _failure("missing_asset_payload", position)

    for position, reference in enumerate(
        sorted(asset_snapshot, key=lambda item: item.content_sha256), start=1
    ):
        if reference not in required:
            return _failure("unused_asset_payload", position)

    return None


def _image_block(digest: str) -> str:
    return (
        "\\par\n"
        "{\\centering\\noindent\n"
        f"\\includegraphics[width=0.98\\linewidth,height=0.65\\textheight,keepaspectratio]{{assets/{digest}.png}}\\par\n"
        "}\n"
    )


def _tex_insertions_by_offset(placements: tuple[VisualPlacement, ...]) -> dict[int, str]:
    by_offset: dict[int, list[VisualPlacement]] = {}
    for item in placements:
        by_offset.setdefault(item.source_text_offset, []).append(item)
    return {
        offset: "".join(
            _image_block(item.asset_reference.content_sha256)
            for item in sorted(group, key=lambda entry: entry.asset_reference.content_sha256)
        )
        for offset, group in by_offset.items()
    }


def _build_bundle(
    ordered_results: tuple[RenderedLecture, ...],
    documents_by_reference: Mapping[DocumentReference, LectureDocument],
    placements: tuple[VisualPlacement, ...],
    asset_snapshot: Mapping[AssetReference, bytes],
    combined: LogicalTexFile,
) -> VisualCompositionResult:
    if not placements:
        try:
            return CompilerInputBundle(root_tex=combined, companion_files=())
        except ValueError:
            return _failure("compiler_input_construction_failed")

    placements_by_document: dict[DocumentReference, list[VisualPlacement]] = {}
    for item in placements:
        placements_by_document.setdefault(item.document_reference, []).append(item)

    expected_body_segments: list[str] = []
    visualized_body_segments: list[str] = []
    for result in ordered_results:
        original_segment = _CLEARPAGE + result.tex_fragment
        expected_body_segments.append(original_segment)
        own_placements = placements_by_document.get(result.document)
        if not own_placements:
            visualized_body_segments.append(original_segment)
            continue
        document = documents_by_reference[result.document]
        insertions = _tex_insertions_by_offset(tuple(own_placements))
        visualized_fragment = _render_with_insertions(document.source_text, insertions)
        visualized_body_segments.append(_CLEARPAGE + visualized_fragment)

    expected_body = "".join(expected_body_segments)
    if combined.tex_source.count(expected_body) != 1:
        return _failure("visual_tex_invariant_failed")
    prefix, suffix = combined.tex_source.split(expected_body, 1)
    if suffix != _TEX_END_MARKER or not prefix.endswith(_TEX_BODY_PREFIX_MARKER):
        return _failure("visual_tex_invariant_failed")
    if prefix.count(_BEGIN_DOCUMENT) != 1:
        return _failure("visual_tex_invariant_failed")

    preamble_prefix, preamble_suffix = prefix.split(_BEGIN_DOCUMENT, 1)
    visualized_prefix = preamble_prefix + ("" if _GRAPHICX_PACKAGE in preamble_prefix else _GRAPHICX_PACKAGE) + _BEGIN_DOCUMENT + preamble_suffix
    visualized_body = "".join(visualized_body_segments)
    visualized_tex_source = visualized_prefix + visualized_body + suffix

    required = {item.asset_reference for item in placements}
    companions = tuple(
        CompilerCompanionFile(
            logical_filename=f"assets/{reference.content_sha256}.png",
            content_bytes=asset_snapshot[reference],
        )
        for reference in sorted(required, key=lambda item: item.content_sha256)
    )

    try:
        root_tex = LogicalTexFile(
            logical_filename=combined.logical_filename, tex_source=visualized_tex_source
        )
        return CompilerInputBundle(root_tex=root_tex, companion_files=companions)
    except ValueError:
        return _failure("compiler_input_construction_failed")


def _failure(code: str, position: int | None = None) -> VisualCompositionFailure:
    return VisualCompositionFailure(
        status="visual_composition_failed",
        diagnostics=(
            VisualCompositionDiagnostic(
                code=code,
                classification=_DIAGNOSTIC_CLASSIFICATIONS[code],
                position=position,
            ),
        ),
    )
