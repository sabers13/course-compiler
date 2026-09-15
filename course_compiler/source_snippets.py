"""Source-derived page snippets: durable directives, deterministic local assets."""
from __future__ import annotations

import hashlib

from .asset import AssetReference, ASSET_REFERENCE_VERSION
from .pdf_page import PdfPageReference, PDF_PAGE_REFERENCE_VERSION
from .pdf_visual_extraction import render_pdf_page_preview, RenderedPdfPagePreview
from .source_persistence import SourceEvidencePayload
from .visual_placement import VisualPlacement, VISUAL_PLACEMENT_VERSION
from .rendering import document_reference

from .snippet_contract import parse_snippets
from .legacy_renderer import _renderer_safe_offsets


def _resolve(snippet, references, store):
    reference = next((r for r in references if r.source_id == snippet.source_id), None)
    if reference is None:
        raise ValueError('snippet_source_not_in_course')
    if store is None:
        raise ValueError('snippet_source_store_unavailable')
    payload = store.load(reference)
    if type(payload) is not SourceEvidencePayload:
        raise ValueError('snippet_source_unavailable')
    page = PdfPageReference(PDF_PAGE_REFERENCE_VERSION, reference, snippet.page)
    preview = render_pdf_page_preview(payload.payload, page)
    if type(preview) is not RenderedPdfPagePreview:
        raise ValueError('snippet_page_unavailable')
    return preview


def validate_snippets(text, references, store):
    snippets = parse_snippets(text)
    safe_offsets = _renderer_safe_offsets(text) if snippets else ()
    for snippet in snippets:
        if snippet.offset not in safe_offsets:
            raise ValueError("snippet_placement_requires_standalone_block")
        _resolve(snippet, references, store)


def derive_snippets(documents, references, store):
    """Re-derived for build, download and recovery; no ephemeral model assets."""
    placements, assets = [], {}
    for document in documents:
        for snippet in parse_snippets(document.source_text):
            preview = _resolve(snippet, references, store)
            asset = AssetReference(ASSET_REFERENCE_VERSION, hashlib.sha256(preview.content_bytes).hexdigest())
            placements.append(VisualPlacement(VISUAL_PLACEMENT_VERSION,
                document_reference(document), snippet.offset, asset))
            assets[asset] = preview.content_bytes
    return tuple(placements), assets
