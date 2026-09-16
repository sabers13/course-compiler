from __future__ import annotations

import ast
import hashlib
import struct
import unittest
import zlib
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest import mock

from tests.toolchain_support import TEX_MISSING_REASON, TEX_TOOLCHAIN_AVAILABLE

from course_compiler import (
    ASSET_REFERENCE_VERSION,
    DOCUMENT_CONTRACT_VERSION,
    VISUAL_PLACEMENT_VERSION,
    AssetReference,
    CompilerInputBundle,
    CourseTexAssemblySuccess,
    DocumentReference,
    LectureDocument,
    LegacyMarkdownTexRenderer,
    RenderedLecture,
    SourceProvenance,
    VisualCompositionDiagnostic,
    VisualCompositionFailure,
    VisualPlacement,
    compile_pdf_bundle,
    compose_course_compiler_input,
    document_reference,
)
from course_compiler import legacy_renderer, visual_composition


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_document(
    source_text: str, *, document_id: str = "l1", order: int = 1
) -> LectureDocument:
    digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    return LectureDocument(
        contract_version=DOCUMENT_CONTRACT_VERSION,
        document_id=document_id,
        order=order,
        source_text=source_text,
        provenance=SourceProvenance(content_sha256=digest),
    )


def reference_for(document: LectureDocument) -> DocumentReference:
    result = document_reference(document)
    assert result is not None
    return result


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    )


def synthetic_png(pixel: bytes = b"\x10\x20\x30") -> bytes:
    """A real, minimal, deterministic 1x1 truecolor PNG built from stdlib only."""

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw_scanline = b"\x00" + pixel
    idat = zlib.compress(raw_scanline)
    return (
        signature
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def asset_reference_for(payload: bytes) -> AssetReference:
    return AssetReference(ASSET_REFERENCE_VERSION, hashlib.sha256(payload).hexdigest())


def placement(
    *, document: LectureDocument, offset: int, asset: AssetReference
) -> VisualPlacement:
    return VisualPlacement(VISUAL_PLACEMENT_VERSION, reference_for(document), offset, asset)


class AdversarialAssetMapping(Mapping):
    """A `Mapping` whose `.items()` can expose duplicate logical keys."""

    def __init__(self, pairs: tuple[tuple[AssetReference, bytes], ...]) -> None:
        self._pairs = pairs

    def items(self):  # type: ignore[override]
        return iter(self._pairs)

    def __iter__(self):
        return iter(key for key, _ in self._pairs)

    def __len__(self) -> int:
        return len(self._pairs)

    def __getitem__(self, key: object) -> bytes:
        for stored_key, value in self._pairs:
            if stored_key == key:
                return value
        raise KeyError(key)


def failure_code(result: object) -> str:
    assert isinstance(result, VisualCompositionFailure)
    return result.diagnostics[0].code


# ---------------------------------------------------------------------------
# Renderer-focused tests: the private generic insertion hook
# ---------------------------------------------------------------------------


class PrivateInsertionHookTests(unittest.TestCase):
    def test_ordinary_render_with_no_insertion_hook_is_unchanged(self) -> None:
        source = "# Title\n\nHello world.\n\nSecond paragraph.\n"
        plain = legacy_renderer._convert_source(source)
        via_hook = legacy_renderer._convert_source(source, None)
        self.assertEqual(plain.tex_fragment, via_hook.tex_fragment)

    def test_empty_insertion_set_reproduces_exact_accepted_output(self) -> None:
        source = "# Title\n\nHello world.\n\nSecond paragraph.\n"
        plain = legacy_renderer._convert_source(source).tex_fragment
        via_empty_mapping = legacy_renderer._render_with_insertions(source, {})
        self.assertEqual(plain, via_empty_mapping)

    def test_hook_is_private_and_not_exported(self) -> None:
        self.assertEqual(legacy_renderer.__all__, ["LegacyMarkdownTexRenderer"])
        for name in ("_convert_source", "_renderer_safe_offsets", "_render_with_insertions"):
            self.assertTrue(hasattr(legacy_renderer, name))
            self.assertNotIn(name, legacy_renderer.__all__)

    def test_offsets_are_unicode_code_points_not_utf8_bytes(self) -> None:
        source = "caf\u00e9 line one.\n\nSecond line.\n"
        safe = legacy_renderer._renderer_safe_offsets(source)
        second_line_offset = source.index("Second line.")
        # If offsets were UTF-8 byte offsets, this code-point offset would
        # not line up with the actual "Second line." boundary at all.
        self.assertIn(second_line_offset, safe)
        self.assertEqual(source.encode("utf-8").__len__() > len(source), True)

    def test_offset_zero_is_always_valid(self) -> None:
        source = "Hello.\n\nWorld.\n"
        self.assertIn(0, legacy_renderer._renderer_safe_offsets(source))

    def test_offset_len_source_text_is_always_valid(self) -> None:
        source = "Hello.\n\nWorld.\n"
        self.assertIn(len(source), legacy_renderer._renderer_safe_offsets(source))

    def test_empty_source_offset_zero_is_valid(self) -> None:
        self.assertEqual(legacy_renderer._renderer_safe_offsets(""), frozenset({0}))

    def test_offset_beyond_source_length_is_not_produced_as_safe(self) -> None:
        source = "Hello.\n"
        safe = legacy_renderer._renderer_safe_offsets(source)
        self.assertNotIn(len(source) + 1, safe)

    def test_mismatched_document_reference_is_a_composition_concern(self) -> None:
        # T023 VisualPlacement itself carries no source text, so mismatch
        # detection is exercised at the compose_course_compiler_input layer;
        # see PlacementValidationTests below.
        pass

    def test_safe_top_level_line_boundary_between_paragraphs(self) -> None:
        source = "Alpha line.\n\nBravo line.\n"
        offset = source.index("Bravo line.")
        self.assertIn(offset, legacy_renderer._renderer_safe_offsets(source))
        rendered = legacy_renderer._render_with_insertions(source, {offset: "XMARK\n"})
        self.assertIn("XMARK\n", rendered)
        # Exact deterministic insertion: splicing at this offset does not
        # otherwise change the surrounding ordinary conversion.
        without = legacy_renderer._convert_source(source).tex_fragment
        self.assertEqual(rendered.replace("XMARK\n", "", 1), without)

    def test_unsafe_inline_character_offset_is_rejected(self) -> None:
        source = "Alpha line.\n\nBravo line.\n"
        mid_word_offset = source.index("Bravo") + 2
        self.assertNotIn(mid_word_offset, legacy_renderer._renderer_safe_offsets(source))

    def test_inside_fenced_code_is_unsafe(self) -> None:
        source = "```\ncode line one\ncode line two\n```\n\nAfter.\n"
        safe = legacy_renderer._renderer_safe_offsets(source)
        self.assertIn(0, safe)
        self.assertNotIn(source.index("code line two"), safe)
        self.assertIn(source.index("After."), safe)

    def test_inside_display_math_is_unsafe(self) -> None:
        source = "\\[\nx + y\n\\]\n\nAfter.\n"
        safe = legacy_renderer._renderer_safe_offsets(source)
        self.assertIn(0, safe)
        self.assertNotIn(source.index("x + y"), safe)
        self.assertIn(source.index("After."), safe)

    def test_inside_table_is_unsafe(self) -> None:
        source = "| A | B |\n|---|---|\n| 1 | 2 |\n\nAfter.\n"
        safe = legacy_renderer._renderer_safe_offsets(source)
        self.assertIn(0, safe)
        self.assertNotIn(source.index("| 1 | 2 |"), safe)
        self.assertIn(source.index("After."), safe)

    def test_inside_blockquote_is_unsafe(self) -> None:
        source = "> Line one\n> Line two\n\nAfter.\n"
        safe = legacy_renderer._renderer_safe_offsets(source)
        self.assertIn(0, safe)
        self.assertNotIn(source.index("> Line two"), safe)
        self.assertIn(source.index("After."), safe)

    def test_inside_list_continuation_is_unsafe(self) -> None:
        # The list stays "open" (list_stack non-empty) across any blank
        # lines that follow it, and is only closed while processing the
        # first ordinary content line after it -- one line boundary too
        # late for that same boundary to be safe. The next boundary after
        # that (once the list is genuinely closed) is safe again.
        source = "- item one\n- item two\n\nAfter one.\n\nAfter two.\n"
        safe = legacy_renderer._renderer_safe_offsets(source)
        self.assertIn(0, safe)
        self.assertNotIn(source.index("- item two"), safe)
        self.assertNotIn(source.index("After one."), safe)
        self.assertIn(source.index("After two."), safe)

    def test_active_keeptogether_span_before_display_math_is_unsafe(self) -> None:
        source = "Intro line.\n\n\\[\nx\n\\]\n\nAfter.\n"
        safe = legacy_renderer._renderer_safe_offsets(source)
        self.assertNotIn(source.index("\\["), safe)

    def test_active_samepage_span_before_fenced_code_is_unsafe(self) -> None:
        source = "Intro line.\n\n```\ncode\n```\n\nAfter.\n"
        safe = legacy_renderer._renderer_safe_offsets(source)
        self.assertNotIn(source.index("```"), safe)

    def test_active_keeptogether_span_for_revision_paragraph_heading_is_unsafe(self) -> None:
        source = "# Title\n\n## The Revision Paragraph\n\nSome text.\n\nAfter.\n"
        safe = legacy_renderer._renderer_safe_offsets(source)
        self.assertNotIn(source.index("Some text."), safe)
        self.assertIn(source.index("After."), safe)

    def test_prose_lead_in_bound_to_dependent_heading_is_unsafe(self) -> None:
        # T004's own pagination policy (_is_heading_lead_in + Needspace)
        # intentionally associates this lead-in with the heading immediately
        # below it; a placement must not separate the two.
        source = (
            "# Title\n\nPrior paragraph.\n\nThese are the steps:\n## Steps\n\nBody.\n"
        )
        safe = legacy_renderer._renderer_safe_offsets(source)
        self.assertNotIn(source.index("## Steps"), safe)
        # Ordinary output is unaffected by this rejection-only mechanism.
        self.assertEqual(
            legacy_renderer._convert_source(source).tex_fragment,
            legacy_renderer._render_with_insertions(source, {}),
        )

    def test_prose_lead_in_bound_to_dependent_list_is_unsafe(self) -> None:
        source = "# Title\n\nThe steps are:\n- one\n- two\n\nAfter.\n"
        safe = legacy_renderer._renderer_safe_offsets(source)
        self.assertNotIn(source.index("- one"), safe)
        # (The list's own lazy-closing behavior separately keeps the
        # boundary right after it unsafe too; see
        # test_inside_list_continuation_is_unsafe. End-of-source remains
        # unconditionally safe regardless.)
        self.assertIn(len(source), safe)
        self.assertEqual(
            legacy_renderer._convert_source(source).tex_fragment,
            legacy_renderer._render_with_insertions(source, {}),
        )

    def test_lead_in_immediately_after_another_heading_is_unprotected(self) -> None:
        # T004's own lookup only links a lead-in to a dependent heading when
        # the lead-in line is *not* itself immediately preceded by another
        # heading; this boundary is a control case proving the protection is
        # exactly as narrow as T004's own policy.
        source = "# Title\n\n## Section\n\nThese are the steps:\n## Steps\n\nBody.\n"
        safe = legacy_renderer._renderer_safe_offsets(source)
        self.assertIn(source.index("## Steps"), safe)

    def test_lead_in_across_blank_line_to_dependent_heading_is_unsafe(self) -> None:
        # T004 finds the dependent construct with its next-nonempty-line
        # lookup, so the blank line it skipped is inside the bound interval
        # and is not an independent insertion point either.
        source = (
            "# Title\n\nPrior paragraph.\n\nThese are the steps:\n\n## Steps\n\nBody.\n"
        )
        blank_boundary = source.index("These are the steps:") + len(
            "These are the steps:\n"
        )
        safe = legacy_renderer._renderer_safe_offsets(source)
        self.assertNotIn(blank_boundary, safe)
        self.assertNotIn(source.index("## Steps"), safe)
        # Protection expires immediately after the dependent construct's own
        # start boundary; later independent boundaries are unaffected.
        self.assertIn(source.index("Body."), safe)
        self.assertEqual(
            legacy_renderer._convert_source(source).tex_fragment,
            legacy_renderer._render_with_insertions(source, {}),
        )

    def test_lead_in_across_blank_line_to_dependent_list_is_unsafe(self) -> None:
        source = "# Title\n\nPrior paragraph.\n\nThe steps are:\n\n- one\n- two\n\nAfter.\n"
        blank_boundary = source.index("The steps are:") + len("The steps are:\n")
        safe = legacy_renderer._renderer_safe_offsets(source)
        self.assertNotIn(blank_boundary, safe)
        self.assertNotIn(source.index("- one"), safe)
        self.assertEqual(
            legacy_renderer._convert_source(source).tex_fragment,
            legacy_renderer._render_with_insertions(source, {}),
        )

    def test_every_boundary_across_multiple_blank_lines_is_unsafe(self) -> None:
        heading_source = (
            "# Title\n\nPrior paragraph.\n\nThese are the steps:\n\n\n\n## Steps\n\nBody.\n"
        )
        heading_gap_start = heading_source.index("These are the steps:") + len(
            "These are the steps:\n"
        )
        heading_safe = legacy_renderer._renderer_safe_offsets(heading_source)
        # Three intervening blank-line boundaries, then the heading itself.
        for step in range(3):
            with self.subTest(construct="heading", blank_line=step):
                self.assertNotIn(heading_gap_start + step, heading_safe)
        self.assertNotIn(heading_source.index("## Steps"), heading_safe)
        self.assertIn(heading_source.index("Body."), heading_safe)
        self.assertEqual(
            legacy_renderer._convert_source(heading_source).tex_fragment,
            legacy_renderer._render_with_insertions(heading_source, {}),
        )

        list_source = (
            "# Title\n\nPrior paragraph.\n\nThe steps are:\n\n\n- one\n- two\n\nAfter.\n"
        )
        list_gap_start = list_source.index("The steps are:") + len("The steps are:\n")
        list_safe = legacy_renderer._renderer_safe_offsets(list_source)
        for step in range(2):
            with self.subTest(construct="list", blank_line=step):
                self.assertNotIn(list_gap_start + step, list_safe)
        self.assertNotIn(list_source.index("- one"), list_safe)
        self.assertEqual(
            legacy_renderer._convert_source(list_source).tex_fragment,
            legacy_renderer._render_with_insertions(list_source, {}),
        )

    def test_blank_boundary_unrelated_to_a_lead_in_pair_stays_safe(self) -> None:
        # A blank line between two ordinary paragraphs establishes no
        # Needspace relationship, so it must remain safe; the interval
        # protection must not broaden into unrelated boundaries.
        source = "# Title\n\nAlpha paragraph.\n\nBravo paragraph.\n"
        blank_boundary = source.index("Alpha paragraph.") + len("Alpha paragraph.\n")
        safe = legacy_renderer._renderer_safe_offsets(source)
        self.assertIn(blank_boundary, safe)
        self.assertIn(source.index("Bravo paragraph."), safe)


# ---------------------------------------------------------------------------
# Placement collection / ordering tests
# ---------------------------------------------------------------------------


class PlacementValidationTests(unittest.TestCase):
    def test_zero_placements_is_valid(self) -> None:
        document = make_document("Hello.\n\nWorld.\n")
        result = compose_course_compiler_input((document,), placements=(), asset_bytes={})
        self.assertIsInstance(result, CompilerInputBundle)

    def test_mismatched_document_reference_fails(self) -> None:
        document = make_document("Hello.\n\nWorld.\n")
        other = make_document("Other.\n\nDoc.\n", document_id="l2")
        payload = synthetic_png()
        asset = asset_reference_for(payload)
        item = placement(document=other, offset=0, asset=asset)
        result = compose_course_compiler_input((document,), placements=(item,), asset_bytes={asset: payload})
        self.assertEqual(failure_code(result), "placement_document_mismatch")

    def test_offset_out_of_range_fails(self) -> None:
        document = make_document("Hello.\n")
        payload = synthetic_png()
        asset = asset_reference_for(payload)
        item = placement(document=document, offset=len(document.source_text) + 1, asset=asset)
        result = compose_course_compiler_input((document,), placements=(item,), asset_bytes={asset: payload})
        self.assertEqual(failure_code(result), "placement_offset_out_of_range")

    def test_offset_not_renderer_safe_fails(self) -> None:
        source = "```\ncode\n```\n\nAfter.\n"
        document = make_document(source)
        payload = synthetic_png()
        asset = asset_reference_for(payload)
        item = placement(document=document, offset=source.index("code"), asset=asset)
        result = compose_course_compiler_input((document,), placements=(item,), asset_bytes={asset: payload})
        self.assertEqual(failure_code(result), "placement_offset_not_renderer_safe")

    def test_blank_gap_between_lead_in_and_dependent_heading_fails(self) -> None:
        # Application-level proof of the renderer-safe interval: a placement
        # at the blank-line boundary T004 skipped to find the dependent
        # heading is rejected, not composed into a bundle.
        source = (
            "# Title\n\nPrior paragraph.\n\nThese are the steps:\n\n## Steps\n\nBody.\n"
        )
        document = make_document(source)
        payload = synthetic_png()
        asset = asset_reference_for(payload)
        blank_boundary = source.index("These are the steps:") + len(
            "These are the steps:\n"
        )
        item = placement(document=document, offset=blank_boundary, asset=asset)
        result = compose_course_compiler_input(
            (document,), placements=(item,), asset_bytes={asset: payload}
        )
        self.assertNotIsInstance(result, CompilerInputBundle)
        self.assertEqual(failure_code(result), "placement_offset_not_renderer_safe")

    def test_blank_gap_between_lead_in_and_dependent_list_fails(self) -> None:
        source = "# Title\n\nPrior paragraph.\n\nThe steps are:\n\n- one\n- two\n\nAfter.\n"
        document = make_document(source)
        payload = synthetic_png()
        asset = asset_reference_for(payload)
        blank_boundary = source.index("The steps are:") + len("The steps are:\n")
        item = placement(document=document, offset=blank_boundary, asset=asset)
        result = compose_course_compiler_input(
            (document,), placements=(item,), asset_bytes={asset: payload}
        )
        self.assertNotIsInstance(result, CompilerInputBundle)
        self.assertEqual(failure_code(result), "placement_offset_not_renderer_safe")

    def test_exact_duplicate_placement_fails(self) -> None:
        document = make_document("Hello.\n\nWorld.\n")
        payload = synthetic_png()
        asset = asset_reference_for(payload)
        item = placement(document=document, offset=0, asset=asset)
        result = compose_course_compiler_input(
            (document,), placements=(item, item), asset_bytes={asset: payload}
        )
        self.assertEqual(failure_code(result), "duplicate_visual_placement")

    def test_same_asset_at_multiple_offsets_is_valid(self) -> None:
        source = "Alpha.\n\nBravo.\n\nCharlie.\n"
        document = make_document(source)
        payload = synthetic_png()
        asset = asset_reference_for(payload)
        first = placement(document=document, offset=0, asset=asset)
        second = placement(document=document, offset=source.index("Bravo."), asset=asset)
        result = compose_course_compiler_input(
            (document,), placements=(first, second), asset_bytes={asset: payload}
        )
        self.assertIsInstance(result, CompilerInputBundle)
        self.assertEqual(len(result.companion_files), 1)
        self.assertEqual(result.root_tex.tex_source.count("includegraphics"), 2)

    def test_different_assets_at_same_offset_is_valid_and_digest_ordered(self) -> None:
        source = "Alpha.\n\nBravo.\n"
        document = make_document(source)
        payload_one = synthetic_png(b"\x01\x02\x03")
        payload_two = synthetic_png(b"\x04\x05\x06")
        asset_one = asset_reference_for(payload_one)
        asset_two = asset_reference_for(payload_two)
        offset = source.index("Bravo.")
        first = placement(document=document, offset=offset, asset=asset_one)
        second = placement(document=document, offset=offset, asset=asset_two)
        result = compose_course_compiler_input(
            (document,),
            placements=(first, second),
            asset_bytes={asset_one: payload_one, asset_two: payload_two},
        )
        self.assertIsInstance(result, CompilerInputBundle)
        expected_order = sorted(
            (asset_one.content_sha256, asset_two.content_sha256)
        )
        first_pos = result.root_tex.tex_source.index(expected_order[0])
        second_pos = result.root_tex.tex_source.index(expected_order[1])
        self.assertLess(first_pos, second_pos)

    def test_caller_placement_order_does_not_affect_output(self) -> None:
        source = "Alpha.\n\nBravo.\n\nCharlie.\n"
        document = make_document(source)
        payload_one = synthetic_png(b"\x11\x11\x11")
        payload_two = synthetic_png(b"\x22\x22\x22")
        asset_one = asset_reference_for(payload_one)
        asset_two = asset_reference_for(payload_two)
        first = placement(document=document, offset=0, asset=asset_one)
        second = placement(document=document, offset=source.index("Bravo."), asset=asset_two)
        assets = {asset_one: payload_one, asset_two: payload_two}
        forward = compose_course_compiler_input(
            (document,), placements=(first, second), asset_bytes=assets
        )
        reversed_order = compose_course_compiler_input(
            (document,), placements=(second, first), asset_bytes=assets
        )
        self.assertEqual(forward, reversed_order)

    def test_canonical_document_order_dominates_caller_order(self) -> None:
        first_document = make_document("First doc body.\n", document_id="l1", order=1)
        second_document = make_document("Second doc body.\n", document_id="l2", order=2)
        payload_one = synthetic_png(b"\x33\x33\x33")
        payload_two = synthetic_png(b"\x44\x44\x44")
        asset_one = asset_reference_for(payload_one)
        asset_two = asset_reference_for(payload_two)
        placement_one = placement(document=first_document, offset=0, asset=asset_one)
        placement_two = placement(document=second_document, offset=0, asset=asset_two)
        assets = {asset_one: payload_one, asset_two: payload_two}

        caller_order_a = compose_course_compiler_input(
            (first_document, second_document),
            placements=(placement_two, placement_one),
            asset_bytes=assets,
        )
        caller_order_b = compose_course_compiler_input(
            (second_document, first_document),
            placements=(placement_one, placement_two),
            asset_bytes=assets,
        )
        self.assertIsInstance(caller_order_a, CompilerInputBundle)
        self.assertEqual(caller_order_a, caller_order_b)
        position_one = caller_order_a.root_tex.tex_source.index(asset_one.content_sha256)
        position_two = caller_order_a.root_tex.tex_source.index(asset_two.content_sha256)
        self.assertLess(position_one, position_two)

    def test_mixed_out_of_range_and_unsafe_failure_is_permutation_independent(self) -> None:
        # Two different placements are each invalid in a different way; the
        # reported failure must depend only on canonical placement order
        # (document order, then offset, then asset digest), never on which
        # order the caller happened to supply them in.
        source = "```\ncode\n```\n\nAfter.\n"
        document = make_document(source)
        payload = synthetic_png()
        asset = asset_reference_for(payload)
        out_of_range = placement(
            document=document, offset=len(source) + 1, asset=asset
        )
        unsafe = placement(document=document, offset=source.index("code"), asset=asset)
        assets = {asset: payload}

        forward = compose_course_compiler_input(
            (document,), placements=(out_of_range, unsafe), asset_bytes=assets
        )
        reversed_order = compose_course_compiler_input(
            (document,), placements=(unsafe, out_of_range), asset_bytes=assets
        )
        self.assertEqual(forward, reversed_order)
        self.assertIsInstance(forward, VisualCompositionFailure)

    def test_duplicate_failure_is_permutation_independent(self) -> None:
        source = "Alpha.\n\nBravo.\n"
        document = make_document(source)
        payload_one = synthetic_png(b"\xcc\xcc\xcc")
        payload_two = synthetic_png(b"\xdd\xdd\xdd")
        asset_one = asset_reference_for(payload_one)
        asset_two = asset_reference_for(payload_two)
        duplicated = placement(document=document, offset=0, asset=asset_one)
        other = placement(document=document, offset=source.index("Bravo."), asset=asset_two)
        assets = {asset_one: payload_one, asset_two: payload_two}

        # The exact duplicate appears at different original caller
        # positions in each permutation below.
        caller_order_a = compose_course_compiler_input(
            (document,), placements=(duplicated, other, duplicated), asset_bytes=assets
        )
        caller_order_b = compose_course_compiler_input(
            (document,), placements=(duplicated, duplicated, other), asset_bytes=assets
        )
        self.assertEqual(caller_order_a, caller_order_b)
        self.assertEqual(failure_code(caller_order_a), "duplicate_visual_placement")
        assert isinstance(caller_order_a, VisualCompositionFailure)
        assert isinstance(caller_order_b, VisualCompositionFailure)
        self.assertEqual(
            caller_order_a.diagnostics[0].position, caller_order_b.diagnostics[0].position
        )


# ---------------------------------------------------------------------------
# Asset validation tests
# ---------------------------------------------------------------------------


class AssetValidationTests(unittest.TestCase):
    def test_missing_payload_fails(self) -> None:
        document = make_document("Hello.\n")
        payload = synthetic_png()
        asset = asset_reference_for(payload)
        item = placement(document=document, offset=0, asset=asset)
        result = compose_course_compiler_input((document,), placements=(item,), asset_bytes={})
        self.assertEqual(failure_code(result), "missing_asset_payload")

    def test_extra_unreferenced_payload_fails(self) -> None:
        document = make_document("Hello.\n")
        payload = synthetic_png()
        asset = asset_reference_for(payload)
        result = compose_course_compiler_input(
            (document,), placements=(), asset_bytes={asset: payload}
        )
        self.assertEqual(failure_code(result), "unused_asset_payload")

    def test_wrong_digest_fails(self) -> None:
        document = make_document("Hello.\n")
        payload = synthetic_png()
        wrong_asset = AssetReference(ASSET_REFERENCE_VERSION, "0" * 64)
        item = placement(document=document, offset=0, asset=wrong_asset)
        result = compose_course_compiler_input(
            (document,), placements=(item,), asset_bytes={wrong_asset: payload}
        )
        self.assertEqual(failure_code(result), "asset_payload_digest_mismatch")

    def test_non_png_payload_fails(self) -> None:
        document = make_document("Hello.\n")
        payload = b"not a png at all"
        asset = asset_reference_for(payload)
        item = placement(document=document, offset=0, asset=asset)
        result = compose_course_compiler_input(
            (document,), placements=(item,), asset_bytes={asset: payload}
        )
        self.assertEqual(failure_code(result), "unsupported_visual_asset_format")

    def test_deterministic_companion_path(self) -> None:
        document = make_document("Hello.\n")
        payload = synthetic_png()
        asset = asset_reference_for(payload)
        item = placement(document=document, offset=0, asset=asset)
        result = compose_course_compiler_input(
            (document,), placements=(item,), asset_bytes={asset: payload}
        )
        self.assertIsInstance(result, CompilerInputBundle)
        self.assertEqual(
            result.companion_files[0].logical_filename,
            f"assets/{asset.content_sha256}.png",
        )

    def test_same_asset_repeated_produces_one_companion(self) -> None:
        source = "Alpha.\n\nBravo.\n"
        document = make_document(source)
        payload = synthetic_png()
        asset = asset_reference_for(payload)
        first = placement(document=document, offset=0, asset=asset)
        second = placement(document=document, offset=source.index("Bravo."), asset=asset)
        result = compose_course_compiler_input(
            (document,), placements=(first, second), asset_bytes={asset: payload}
        )
        self.assertIsInstance(result, CompilerInputBundle)
        self.assertEqual(len(result.companion_files), 1)

    def test_different_assets_produce_distinct_companions(self) -> None:
        source = "Alpha.\n\nBravo.\n"
        document = make_document(source)
        payload_one = synthetic_png(b"\x55\x55\x55")
        payload_two = synthetic_png(b"\x66\x66\x66")
        asset_one = asset_reference_for(payload_one)
        asset_two = asset_reference_for(payload_two)
        first = placement(document=document, offset=0, asset=asset_one)
        second = placement(document=document, offset=source.index("Bravo."), asset=asset_two)
        result = compose_course_compiler_input(
            (document,),
            placements=(first, second),
            asset_bytes={asset_one: payload_one, asset_two: payload_two},
        )
        self.assertIsInstance(result, CompilerInputBundle)
        self.assertEqual(len(result.companion_files), 2)

    def test_companion_payload_bytes_are_exact_and_unmodified(self) -> None:
        document = make_document("Hello.\n")
        payload = synthetic_png(b"\x77\x88\x99")
        asset = asset_reference_for(payload)
        item = placement(document=document, offset=0, asset=asset)
        result = compose_course_compiler_input(
            (document,), placements=(item,), asset_bytes={asset: payload}
        )
        self.assertIsInstance(result, CompilerInputBundle)
        self.assertEqual(result.companion_files[0].content_bytes, payload)

    def test_wrong_payload_type_fails(self) -> None:
        document = make_document("Hello.\n")
        asset = AssetReference(ASSET_REFERENCE_VERSION, "1" * 64)
        item = placement(document=document, offset=0, asset=asset)
        result = compose_course_compiler_input(
            (document,), placements=(item,), asset_bytes={asset: "not-bytes"}  # type: ignore[dict-item]
        )
        self.assertEqual(failure_code(result), "invalid_asset_payload")

    def test_wrong_key_type_fails(self) -> None:
        document = make_document("Hello.\n")
        payload = synthetic_png()
        result = compose_course_compiler_input(
            (document,), placements=(), asset_bytes={"not-a-reference": payload}  # type: ignore[dict-item]
        )
        self.assertEqual(failure_code(result), "invalid_asset_payload")

    def test_adversarial_mapping_duplicate_key_fails_closed(self) -> None:
        document = make_document("Hello.\n")
        payload = synthetic_png()
        asset = asset_reference_for(payload)
        item = placement(document=document, offset=0, asset=asset)
        mapping = AdversarialAssetMapping(((asset, payload), (asset, payload)))
        result = compose_course_compiler_input((document,), placements=(item,), asset_bytes=mapping)
        self.assertEqual(failure_code(result), "duplicate_asset_payload")

    def test_adversarial_mapping_conflicting_key_fails_closed(self) -> None:
        document = make_document("Hello.\n")
        payload_a = synthetic_png(b"\xaa\xaa\xaa")
        payload_b = synthetic_png(b"\xbb\xbb\xbb")
        asset = asset_reference_for(payload_a)
        item = placement(document=document, offset=0, asset=asset)
        mapping = AdversarialAssetMapping(((asset, payload_a), (asset, payload_b)))
        result = compose_course_compiler_input((document,), placements=(item,), asset_bytes=mapping)
        self.assertEqual(failure_code(result), "conflicting_asset_payload")

    def test_ordinary_dict_naturally_collapses_duplicate_keys(self) -> None:
        document = make_document("Hello.\n")
        payload = synthetic_png()
        asset = asset_reference_for(payload)
        item = placement(document=document, offset=0, asset=asset)
        ordinary = {asset: payload}
        result = compose_course_compiler_input((document,), placements=(item,), asset_bytes=ordinary)
        self.assertIsInstance(result, CompilerInputBundle)


# ---------------------------------------------------------------------------
# TeX syntax tests
# ---------------------------------------------------------------------------


class TexSyntaxTests(unittest.TestCase):
    def test_no_image_with_zero_placements(self) -> None:
        document = make_document("Hello.\n\nWorld.\n")
        result = compose_course_compiler_input((document,), placements=(), asset_bytes={})
        self.assertIsInstance(result, CompilerInputBundle)
        self.assertNotIn("\\includegraphics", result.root_tex.tex_source)

    def test_graphicx_present_exactly_once_with_placements(self) -> None:
        source = "Alpha.\n\nBravo.\n"
        document = make_document(source)
        payload = synthetic_png()
        asset = asset_reference_for(payload)
        first = placement(document=document, offset=0, asset=asset)
        second = placement(document=document, offset=source.index("Bravo."), asset=asset)
        result = compose_course_compiler_input(
            (document,), placements=(first, second), asset_bytes={asset: payload}
        )
        self.assertIsInstance(result, CompilerInputBundle)
        self.assertEqual(result.root_tex.tex_source.count("\\usepackage{graphicx}"), 1)

    def test_exact_non_floating_image_syntax(self) -> None:
        document = make_document("Hello.\n")
        payload = synthetic_png()
        asset = asset_reference_for(payload)
        item = placement(document=document, offset=0, asset=asset)
        result = compose_course_compiler_input(
            (document,), placements=(item,), asset_bytes={asset: payload}
        )
        self.assertIsInstance(result, CompilerInputBundle)
        expected = (
            "\\par\n"
            "{\\centering\\noindent\n"
            f"\\includegraphics[width=0.98\\linewidth,height=0.65\\textheight,keepaspectratio]"
            f"{{assets/{asset.content_sha256}.png}}\\par\n"
            "}\n"
        )
        self.assertIn(expected, result.root_tex.tex_source)

    def test_no_figure_caption_or_label_in_injected_block(self) -> None:
        document = make_document("Hello.\n")
        payload = synthetic_png()
        asset = asset_reference_for(payload)
        item = placement(document=document, offset=0, asset=asset)
        result = compose_course_compiler_input(
            (document,), placements=(item,), asset_bytes={asset: payload}
        )
        self.assertIsInstance(result, CompilerInputBundle)
        marker = f"assets/{asset.content_sha256}.png"
        start = result.root_tex.tex_source.index(marker)
        block_start = result.root_tex.tex_source.rindex("\\par\n{\\centering\\noindent\n", 0, start)
        block_end = result.root_tex.tex_source.index("\n}\n", start) + len("\n}\n")
        block = result.root_tex.tex_source[block_start:block_end]
        for forbidden in ("\\begin{figure}", "\\caption", "\\label"):
            self.assertNotIn(forbidden, block)


# ---------------------------------------------------------------------------
# Compatibility tests
# ---------------------------------------------------------------------------


class CompatibilityTests(unittest.TestCase):
    def test_zero_placements_matches_exact_t005_root_content(self) -> None:
        document = make_document("# Title\n\nHello world.\n\nSecond paragraph.\n")
        reference = reference_for(document)
        rendered = LegacyMarkdownTexRenderer().render(document)
        self.assertIsInstance(rendered, RenderedLecture)
        from course_compiler import assemble_course_tex

        assembled = assemble_course_tex((reference,), (rendered,))
        self.assertIsInstance(assembled, CourseTexAssemblySuccess)
        assert isinstance(assembled, CourseTexAssemblySuccess)
        result = compose_course_compiler_input((document,), placements=(), asset_bytes={})
        self.assertIsInstance(result, CompilerInputBundle)
        self.assertEqual(result.root_tex, assembled.combined)
        self.assertEqual(result.root_tex.tex_source, assembled.combined.tex_source)
        self.assertEqual(result.root_tex.logical_filename, assembled.combined.logical_filename)

    def test_zero_placements_has_empty_companion_files(self) -> None:
        document = make_document("Hello.\n")
        result = compose_course_compiler_input((document,), placements=(), asset_bytes={})
        self.assertIsInstance(result, CompilerInputBundle)
        self.assertEqual(result.companion_files, ())

    def test_zero_placements_has_no_image(self) -> None:
        document = make_document("Hello.\n")
        result = compose_course_compiler_input((document,), placements=(), asset_bytes={})
        self.assertIsInstance(result, CompilerInputBundle)
        self.assertNotIn("\\includegraphics", result.root_tex.tex_source)

    def test_multi_document_zero_placements_matches_exact_t005_root(self) -> None:
        first_document = make_document("First body.\n", document_id="l1", order=1)
        second_document = make_document("Second body.\n", document_id="l2", order=2)
        first_reference = reference_for(first_document)
        second_reference = reference_for(second_document)
        first_rendered = LegacyMarkdownTexRenderer().render(first_document)
        second_rendered = LegacyMarkdownTexRenderer().render(second_document)
        from course_compiler import assemble_course_tex

        assembled = assemble_course_tex(
            (first_reference, second_reference), (first_rendered, second_rendered)
        )
        self.assertIsInstance(assembled, CourseTexAssemblySuccess)
        assert isinstance(assembled, CourseTexAssemblySuccess)
        result = compose_course_compiler_input(
            (first_document, second_document), placements=(), asset_bytes={}
        )
        self.assertIsInstance(result, CompilerInputBundle)
        self.assertEqual(result.root_tex.tex_source, assembled.combined.tex_source)


# ---------------------------------------------------------------------------
# Public misuse / basic input validation
# ---------------------------------------------------------------------------


class PublicMisuseTests(unittest.TestCase):
    def test_wrong_documents_type_raises_type_error(self) -> None:
        with self.assertRaises(TypeError):
            compose_course_compiler_input("not-a-tuple", placements=(), asset_bytes={})  # type: ignore[arg-type]

    def test_wrong_placements_type_raises_type_error(self) -> None:
        document = make_document("Hello.\n")
        with self.assertRaises(TypeError):
            compose_course_compiler_input(
                (document,), placements="not-a-tuple", asset_bytes={}  # type: ignore[arg-type]
            )

    def test_wrong_asset_bytes_type_raises_type_error(self) -> None:
        document = make_document("Hello.\n")
        with self.assertRaises(TypeError):
            compose_course_compiler_input(
                (document,), placements=(), asset_bytes="not-a-mapping"  # type: ignore[arg-type]
            )

    def test_empty_document_collection_fails(self) -> None:
        result = compose_course_compiler_input((), placements=(), asset_bytes={})
        self.assertEqual(failure_code(result), "empty_document_collection")

    def test_non_document_element_fails(self) -> None:
        result = compose_course_compiler_input(("not-a-document",), placements=(), asset_bytes={})  # type: ignore[arg-type]
        self.assertEqual(failure_code(result), "invalid_visual_composition_input")

    def test_non_placement_element_fails(self) -> None:
        document = make_document("Hello.\n")
        result = compose_course_compiler_input(
            (document,), placements=("not-a-placement",), asset_bytes={}  # type: ignore[arg-type]
        )
        self.assertEqual(failure_code(result), "invalid_visual_composition_input")

    def test_rejected_lecture_document_fails_closed(self) -> None:
        document = LectureDocument(
            contract_version=DOCUMENT_CONTRACT_VERSION,
            document_id="bad id",
            order=1,
            source_text="Hello.\n",
            provenance=SourceProvenance(content_sha256="0" * 64),
        )
        result = compose_course_compiler_input((document,), placements=(), asset_bytes={})
        self.assertEqual(failure_code(result), "lecture_render_rejected")

    def test_duplicate_document_order_fails_via_assembly(self) -> None:
        first_document = make_document("First body.\n", document_id="l1", order=1)
        second_document = make_document("Second body.\n", document_id="l2", order=1)
        result = compose_course_compiler_input(
            (first_document, second_document), placements=(), asset_bytes={}
        )
        self.assertEqual(failure_code(result), "course_tex_assembly_failed")

    def test_unexpected_render_result_type_is_a_renderer_invariant_failure(self) -> None:
        document = make_document("Hello.\n")
        with mock.patch.object(
            visual_composition.LegacyMarkdownTexRenderer, "render", return_value=object()
        ):
            result = compose_course_compiler_input((document,), placements=(), asset_bytes={})
        self.assertEqual(failure_code(result), "renderer_invariant_failure")

    def test_unexpected_assembly_result_type_is_a_renderer_invariant_failure(self) -> None:
        document = make_document("Hello.\n")
        with mock.patch.object(
            visual_composition, "assemble_course_tex", return_value=object()
        ):
            result = compose_course_compiler_input((document,), placements=(), asset_bytes={})
        self.assertEqual(failure_code(result), "renderer_invariant_failure")

    def test_tampered_combined_root_is_a_visual_tex_invariant_failure(self) -> None:
        document = make_document("Hello.\n")
        payload = synthetic_png()
        asset = asset_reference_for(payload)
        item = placement(document=document, offset=0, asset=asset)
        real_assemble = visual_composition.assemble_course_tex

        def tampered(references: object, results: object) -> object:
            real = real_assemble(references, results)
            tampered_combined = replace(
                real.combined,
                tex_source="garbage that does not contain the expected body\n",
            )
            return replace(real, combined=tampered_combined)

        with mock.patch.object(visual_composition, "assemble_course_tex", side_effect=tampered):
            result = compose_course_compiler_input(
                (document,), placements=(item,), asset_bytes={asset: payload}
            )
        self.assertEqual(failure_code(result), "visual_tex_invariant_failed")

    def test_bundle_construction_failure_is_reported(self) -> None:
        document = make_document("Hello.\n")
        payload = synthetic_png()
        asset = asset_reference_for(payload)
        item = placement(document=document, offset=0, asset=asset)
        with mock.patch.object(visual_composition, "CompilerInputBundle", side_effect=ValueError):
            result = compose_course_compiler_input(
                (document,), placements=(item,), asset_bytes={asset: payload}
            )
        self.assertEqual(failure_code(result), "compiler_input_construction_failed")

    def test_zero_placement_bundle_construction_failure_is_reported(self) -> None:
        document = make_document("Hello.\n")
        with mock.patch.object(visual_composition, "CompilerInputBundle", side_effect=ValueError):
            result = compose_course_compiler_input((document,), placements=(), asset_bytes={})
        self.assertEqual(failure_code(result), "compiler_input_construction_failed")

    def test_unhandled_exception_is_contained(self) -> None:
        document = make_document("Hello.\n")
        with mock.patch.object(
            visual_composition, "assemble_course_tex", side_effect=RuntimeError("invented")
        ):
            result = compose_course_compiler_input((document,), placements=(), asset_bytes={})
        self.assertEqual(failure_code(result), "visual_composition_exception")

    def test_base_exception_propagates_uncontained(self) -> None:
        document = make_document("Hello.\n")

        class StopComposition(BaseException):
            pass

        with mock.patch.object(
            visual_composition, "assemble_course_tex", side_effect=StopComposition
        ):
            with self.assertRaises(StopComposition):
                compose_course_compiler_input((document,), placements=(), asset_bytes={})


# ---------------------------------------------------------------------------
# Public shape / failure value tests
# ---------------------------------------------------------------------------


class PublicShapeTests(unittest.TestCase):
    def test_diagnostic_and_failure_are_frozen_slotted_and_registered(self) -> None:
        diagnostic = VisualCompositionDiagnostic("empty_document_collection", "input", None)
        self.assertFalse(hasattr(diagnostic, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            diagnostic.code = "other"  # type: ignore[misc]
        with self.assertRaises(ValueError):
            VisualCompositionDiagnostic("not-registered", "input", None)
        with self.assertRaises(ValueError):
            VisualCompositionDiagnostic("empty_document_collection", "asset", None)
        with self.assertRaises(ValueError):
            VisualCompositionDiagnostic("empty_document_collection", "input", 0)
        with self.assertRaises(ValueError):
            VisualCompositionDiagnostic("empty_document_collection", "input", True)  # type: ignore[arg-type]

        failure = VisualCompositionFailure("visual_composition_failed", (diagnostic,))
        self.assertFalse(hasattr(failure, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            failure.status = "other"  # type: ignore[misc]
        with self.assertRaises(ValueError):
            VisualCompositionFailure("other", (diagnostic,))
        with self.assertRaises(ValueError):
            VisualCompositionFailure("visual_composition_failed", ())

    def test_public_surface_is_exported_and_reachable(self) -> None:
        import course_compiler

        for name in (
            "VisualCompositionDiagnostic",
            "VisualCompositionFailure",
            "VisualCompositionResult",
            "compose_course_compiler_input",
        ):
            with self.subTest(name=name):
                self.assertIn(name, course_compiler.__all__)
                self.assertIn(name, visual_composition.__all__)
                self.assertIs(getattr(course_compiler, name), getattr(visual_composition, name))

    def test_no_error_text_reveals_source_content(self) -> None:
        sentinel = "invented-private-source-secret"
        document = make_document(f"{sentinel}\n")
        result = compose_course_compiler_input((document,), placements=(), asset_bytes={})
        self.assertIsInstance(result, CompilerInputBundle)
        payload = synthetic_png()
        asset = asset_reference_for(payload)
        item = placement(document=document, offset=0, asset=asset)
        failure_result = compose_course_compiler_input(
            (document,), placements=(item, item), asset_bytes={asset: payload}
        )
        self.assertIsInstance(failure_result, VisualCompositionFailure)
        self.assertNotIn(sentinel, repr(failure_result))
        self.assertNotIn(sentinel, str(failure_result))


# ---------------------------------------------------------------------------
# Dependency direction, privacy, and production-boundary tests
# ---------------------------------------------------------------------------


class DependencyBoundaryTests(unittest.TestCase):
    def test_legacy_renderer_has_no_asset_placement_or_compiler_imports(self) -> None:
        module_path = Path("course_compiler/legacy_renderer.py")
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(module_path))
        relative_imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level
        }
        self.assertEqual(relative_imports, {"contracts", "rendering", "math_format", "snippet_contract"})
        for forbidden in ("graphicx", "includegraphics"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_lower_modules_do_not_import_visual_composition(self) -> None:
        root = Path("course_compiler")
        for lower_name in ("legacy_renderer.py", "assembly.py", "compilation.py"):
            with self.subTest(module=lower_name):
                source = (root / lower_name).read_text(encoding="utf-8")
                self.assertNotIn("visual_composition", source)

    def test_visual_composition_has_only_approved_downward_imports(self) -> None:
        module_path = Path("course_compiler/visual_composition.py")
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        relative_imports: set[str] = set()
        absolute_imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    relative_imports.add(node.module or "")
                elif node.module:
                    absolute_imports.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                absolute_imports.update(alias.name.split(".")[0] for alias in node.names)
        self.assertEqual(
            relative_imports,
            {"asset", "assembly", "compilation", "contracts", "legacy_renderer", "rendering", "visual_placement"},
        )
        self.assertEqual(absolute_imports, {"__future__", "hashlib", "collections", "dataclasses", "typing"})

    def test_no_compiler_execution_or_filesystem_staging_in_production_module(self) -> None:
        source = Path("course_compiler/visual_composition.py").read_text(encoding="utf-8")
        for forbidden in (
            "compile_pdf",
            "LocalPdfCompiler",
            "subprocess",
            "latexmk",
            "xelatex",
            "tempfile",
            "pathlib",
            "sqlite3",
            "course_workflow",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_no_privacy_sensitive_strings_in_production_source(self) -> None:
        # Checked against the production modules only: this assertion's own
        # forbidden-string literals would otherwise trip on this test file.
        forbidden_fragments = ("local-data", "local-artifacts", "/tmp/")
        forbidden_home_marker = "/home/" + "saber"
        for path in (
            Path("course_compiler/visual_composition.py"),
            Path("course_compiler/legacy_renderer.py"),
        ):
            source = path.read_text(encoding="utf-8")
            for forbidden in (*forbidden_fragments, forbidden_home_marker):
                with self.subTest(path=str(path), forbidden=forbidden):
                    self.assertNotIn(forbidden, source)


# ---------------------------------------------------------------------------
# Synthetic real-toolchain integration test (test-only compiler use)
# ---------------------------------------------------------------------------


@unittest.skipUnless(
    TEX_TOOLCHAIN_AVAILABLE,
    f"real XeLaTeX compile unavailable ({TEX_MISSING_REASON})",
)
class SyntheticRealCompileTests(unittest.TestCase):
    def test_composed_bundle_compiles_through_unchanged_compile_pdf_bundle(self) -> None:
        source = "# Title\n\nSome ordinary lecture prose.\n"
        document = make_document(source)
        payload = synthetic_png()
        asset = asset_reference_for(payload)
        item = placement(document=document, offset=len(source), asset=asset)
        bundle = compose_course_compiler_input(
            (document,), placements=(item,), asset_bytes={asset: payload}
        )
        self.assertIsInstance(bundle, CompilerInputBundle)
        assert isinstance(bundle, CompilerInputBundle)
        result = compile_pdf_bundle(bundle)
        from course_compiler import CompiledPdf

        self.assertIsInstance(result, CompiledPdf)
        assert isinstance(result, CompiledPdf)
        self.assertTrue(result.pdf_content.startswith(b"%PDF-"))


if __name__ == "__main__":
    unittest.main()
