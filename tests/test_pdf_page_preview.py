from __future__ import annotations

import ast
import hashlib
import struct
import subprocess
import unittest
import zlib
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from unittest import mock

from tests.toolchain_support import POPPLER_AVAILABLE, POPPLER_MISSING_REASON

import course_compiler
import course_compiler.pdf_visual_extraction as extraction
from course_compiler import (
    PDF_PAGE_PREVIEW_COORDINATE_FRAME,
    PDF_PAGE_PREVIEW_PROFILE,
    PDF_PAGE_REFERENCE_VERSION,
    ExtractedPdfVisual,
    PdfPagePreviewDiagnostic,
    PdfPagePreviewFailure,
    PdfPageReference,
    RenderedPdfPagePreview,
    extract_pdf_page_region,
    render_pdf_page_preview,
)
from course_compiler.workflow import (
    SOURCE_EVIDENCE_REFERENCE_VERSION,
    SourceEvidenceReference,
)


def synthetic_vector_pdf() -> bytes:
    """Build one invented vector-only page with an explicit CropBox."""

    content = (
        b"0.90 0.95 1.00 rg 12 12 120 120 re f\n"
        b"0.10 0.30 0.70 RG 4 w 18 18 m 126 126 l S\n"
        b"0.80 0.20 0.10 rg 52 42 48 62 re f\n"
    )
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 180 180] "
            b"/CropBox [18 18 162 162] /Resources << >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n"
        + content
        + b"endstream",
    )
    output = bytearray(b"%PDF-1.4\n% invented vector preview fixture\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


def png_bytes(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + b"\x02\x03\x05" * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


def page_reference(source: bytes, ordinal: int = 1) -> PdfPageReference:
    return PdfPageReference(
        PDF_PAGE_REFERENCE_VERSION,
        SourceEvidenceReference(
            SOURCE_EVIDENCE_REFERENCE_VERSION,
            "invented-preview-pdf",
            hashlib.sha256(source).hexdigest(),
        ),
        ordinal,
    )


def preview_with_mocked_png(
    source: bytes,
    output: bytes,
    *,
    reference: PdfPageReference | None = None,
) -> object:
    completed = subprocess.CompletedProcess((), 0, stdout=output, stderr=b"")
    with mock.patch.object(extraction.subprocess, "run", return_value=completed):
        return render_pdf_page_preview(source, reference or page_reference(source))


class PreviewTestCase(unittest.TestCase):
    def assert_failure(self, result: object, code: str) -> PdfPagePreviewFailure:
        self.assertIsInstance(result, PdfPagePreviewFailure)
        assert isinstance(result, PdfPagePreviewFailure)
        self.assertEqual(result.status, "pdf_page_preview_failed")
        self.assertEqual(len(result.diagnostics), 1)
        self.assertEqual(result.diagnostics[0].code, code)
        return result


class PublicPreviewContractTests(PreviewTestCase):
    def test_public_constants_exports_and_exact_record_shapes(self) -> None:
        self.assertEqual(
            PDF_PAGE_PREVIEW_PROFILE,
            "pdftoppm-cropbox-page-png-144dpi/v1",
        )
        self.assertEqual(
            PDF_PAGE_PREVIEW_COORDINATE_FRAME,
            "pdftoppm-cropbox-pixels-top-left-144dpi/v1",
        )
        self.assertEqual(
            [item.name for item in fields(RenderedPdfPagePreview)],
            [
                "status",
                "preview_profile",
                "coordinate_frame",
                "page_reference",
                "width_px",
                "height_px",
                "format",
                "content_bytes",
            ],
        )
        self.assertEqual(
            [item.name for item in fields(PdfPagePreviewDiagnostic)],
            ["code", "classification", "message"],
        )
        self.assertEqual(
            [item.name for item in fields(PdfPagePreviewFailure)],
            ["status", "diagnostics"],
        )
        for name in (
            "PDF_PAGE_PREVIEW_COORDINATE_FRAME",
            "PDF_PAGE_PREVIEW_PROFILE",
            "RenderedPdfPagePreview",
            "PdfPagePreviewDiagnostic",
            "PdfPagePreviewFailure",
            "PdfPagePreviewResult",
            "render_pdf_page_preview",
        ):
            self.assertIn(name, course_compiler.__all__)
            self.assertTrue(hasattr(course_compiler, name))

    def test_records_are_frozen_slotted_and_payload_repr_is_redacted(self) -> None:
        source = synthetic_vector_pdf()
        content = png_bytes(24, 18)
        result = preview_with_mocked_png(source, content)
        self.assertIsInstance(result, RenderedPdfPagePreview)
        assert isinstance(result, RenderedPdfPagePreview)
        self.assertFalse(hasattr(result, "__dict__"))
        self.assertNotIn("content_bytes", repr(result))
        self.assertNotIn(content.hex(), repr(result))
        with self.assertRaises(FrozenInstanceError):
            result.status = "other"  # type: ignore[misc]

        diagnostic = PdfPagePreviewDiagnostic(
            "renderer_timeout",
            "renderer",
            "The fixed local PDF renderer timed out.",
        )
        failure = PdfPagePreviewFailure("pdf_page_preview_failed", (diagnostic,))
        self.assertFalse(hasattr(diagnostic, "__dict__"))
        self.assertFalse(hasattr(failure, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            diagnostic.code = "other"  # type: ignore[misc]

    def test_reconstructed_public_values_are_revalidated(self) -> None:
        with self.assertRaisesRegex(ValueError, "diagnostic code"):
            PdfPagePreviewDiagnostic("invented", "adapter", "invented")
        with self.assertRaisesRegex(ValueError, "diagnostic fields"):
            PdfPagePreviewDiagnostic(
                "renderer_timeout", "renderer", "invented dynamic text"
            )
        diagnostic = PdfPagePreviewDiagnostic(
            "renderer_timeout",
            "renderer",
            "The fixed local PDF renderer timed out.",
        )
        object.__setattr__(diagnostic, "message", "invented dynamic text")
        with self.assertRaisesRegex(ValueError, "failure diagnostics"):
            PdfPagePreviewFailure("pdf_page_preview_failed", (diagnostic,))

        source = synthetic_vector_pdf()
        content = png_bytes(24, 18)
        forged = page_reference(source)
        object.__setattr__(forged, "physical_page_ordinal", 0)
        with self.assertRaisesRegex(ValueError, "page reference"):
            RenderedPdfPagePreview(
                "rendered",
                PDF_PAGE_PREVIEW_PROFILE,
                PDF_PAGE_PREVIEW_COORDINATE_FRAME,
                forged,
                24,
                18,
                "png",
                content,
            )

    def test_success_constructor_checks_profile_frame_dimensions_and_content(self) -> None:
        source = synthetic_vector_pdf()
        reference = page_reference(source)
        content = png_bytes(24, 18)
        base = (
            "rendered",
            PDF_PAGE_PREVIEW_PROFILE,
            PDF_PAGE_PREVIEW_COORDINATE_FRAME,
            reference,
            24,
            18,
            "png",
            content,
        )
        for index, replacement in (
            (0, "other"),
            (1, "other"),
            (2, "other"),
            (4, True),
            (5, 0),
            (6, "jpeg"),
            (7, b"not a png"),
        ):
            values = list(base)
            values[index] = replacement
            with self.subTest(index=index), self.assertRaises(ValueError):
                RenderedPdfPagePreview(*values)  # type: ignore[arg-type]

    def test_failure_vocabulary_and_diagnostics_are_exact_and_content_free(self) -> None:
        expected = {
            "invalid_page_preview_input": (
                "input",
                "The PDF page preview input is invalid.",
            ),
            "source_bytes_mismatch": (
                "input",
                "The source PDF bytes do not match the page reference.",
            ),
            "renderer_unavailable": (
                "renderer",
                "The fixed local PDF renderer is unavailable.",
            ),
            "renderer_timeout": (
                "renderer",
                "The fixed local PDF renderer timed out.",
            ),
            "renderer_failed": (
                "renderer",
                "The fixed local PDF renderer reported a failure.",
            ),
            "invalid_rendered_output": (
                "output",
                "The rendered PNG output is invalid.",
            ),
            "page_preview_dimensions_unsupported": (
                "output",
                "The rendered PDF page dimensions are unsupported.",
            ),
            "page_preview_exception": (
                "adapter",
                "The PDF page preview adapter failed.",
            ),
        }
        for code, (classification, message) in expected.items():
            with self.subTest(code=code):
                diagnostic = PdfPagePreviewDiagnostic(
                    code, classification, message  # type: ignore[arg-type]
                )
                failure = PdfPagePreviewFailure(
                    "pdf_page_preview_failed", (diagnostic,)
                )
                self.assertEqual(failure.diagnostics, (diagnostic,))


class PreviewInputAndProcessTests(PreviewTestCase):
    def test_wrong_runtime_types_raise_before_subprocess(self) -> None:
        source = synthetic_vector_pdf()
        reference = page_reference(source)
        for source_value, reference_value in (
            (bytearray(source), reference),
            (memoryview(source), reference),
            (source, object()),
        ):
            with self.subTest(
                types=(type(source_value).__name__, type(reference_value).__name__)
            ):
                with mock.patch.object(extraction.subprocess, "run") as run:
                    with self.assertRaises(TypeError):
                        render_pdf_page_preview(  # type: ignore[arg-type]
                            source_value, reference_value
                        )
                run.assert_not_called()

    def test_invalid_reconstructed_page_fails_before_subprocess(self) -> None:
        source = synthetic_vector_pdf()
        forged = page_reference(source)
        object.__setattr__(forged, "physical_page_ordinal", 0)
        with mock.patch.object(extraction.subprocess, "run") as run:
            result = render_pdf_page_preview(source, forged)
        self.assert_failure(result, "invalid_page_preview_input")
        run.assert_not_called()

    def test_source_digest_mismatch_fails_before_subprocess(self) -> None:
        source = synthetic_vector_pdf()
        with mock.patch.object(extraction.subprocess, "run") as run:
            result = render_pdf_page_preview(
                source + b"invented mismatch", page_reference(source)
            )
        self.assert_failure(result, "source_bytes_mismatch")
        run.assert_not_called()

    def test_fixed_renderer_invocation_is_exact_single_and_crop_free(self) -> None:
        source = synthetic_vector_pdf()
        reference = page_reference(source, ordinal=7)
        output = png_bytes(321, 123)
        completed = subprocess.CompletedProcess((), 0, stdout=output, stderr=b"")
        with mock.patch.object(
            extraction.subprocess, "run", return_value=completed
        ) as run:
            result = render_pdf_page_preview(source, reference)
        self.assertIsInstance(result, RenderedPdfPagePreview)
        run.assert_called_once_with(
            (
                "pdftoppm",
                "-f",
                "7",
                "-l",
                "7",
                "-singlefile",
                "-r",
                "144",
                "-cropbox",
                "-png",
                "-",
            ),
            input=source,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
            shell=False,
        )
        argv = run.call_args.args[0]
        for crop_flag in ("-x", "-y", "-W", "-H"):
            self.assertNotIn(crop_flag, argv)


class PreviewOutputAndFailureTests(PreviewTestCase):
    def test_exact_stdout_is_retained_and_ihdr_dimensions_are_exposed(self) -> None:
        source = synthetic_vector_pdf()
        output = png_bytes(321, 123)
        result = preview_with_mocked_png(source, output)
        self.assertIsInstance(result, RenderedPdfPagePreview)
        assert isinstance(result, RenderedPdfPagePreview)
        self.assertIs(result.content_bytes, output)
        self.assertEqual((result.width_px, result.height_px), (321, 123))
        self.assertEqual(result.format, "png")

    def test_empty_and_malformed_png_output_fail_closed(self) -> None:
        source = synthetic_vector_pdf()
        valid = png_bytes(1, 1)
        malformed = (
            b"",
            b"not a png",
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 25,
            valid[:12] + b"NOPE" + valid[16:],
        )
        for output in malformed:
            with self.subTest(length=len(output)):
                self.assert_failure(
                    preview_with_mocked_png(source, output),
                    "invalid_rendered_output",
                )

    def test_excessive_page_dimensions_fail_without_downscaling(self) -> None:
        source = synthetic_vector_pdf()
        cases = ((8193, 1), (1, 8193), (4097, 4096))
        for width, height in cases:
            output = bytearray(png_bytes(1, 1))
            output[16:20] = width.to_bytes(4, "big")
            output[20:24] = height.to_bytes(4, "big")
            with self.subTest(dimensions=(width, height)):
                self.assert_failure(
                    preview_with_mocked_png(source, bytes(output)),
                    "page_preview_dimensions_unsupported",
                )

    def test_renderer_failures_are_fixed_and_redacted(self) -> None:
        sentinel = "invented/private/preview-renderer-secret"
        source = synthetic_vector_pdf() + sentinel.encode("ascii")
        reference = page_reference(source)
        cases = (
            (FileNotFoundError(sentinel), "renderer_unavailable"),
            (
                subprocess.TimeoutExpired((sentinel,), 30, output=sentinel),
                "renderer_timeout",
            ),
            (RuntimeError(sentinel), "page_preview_exception"),
        )
        for raised, code in cases:
            with self.subTest(code=code), mock.patch.object(
                extraction.subprocess, "run", side_effect=raised
            ):
                failure = self.assert_failure(
                    render_pdf_page_preview(source, reference), code
                )
            self.assertNotIn(sentinel, repr(failure))
            self.assertNotIn(source.hex(), repr(failure))

        completed = subprocess.CompletedProcess(
            (), 17, stdout=sentinel.encode("ascii"), stderr=sentinel.encode("ascii")
        )
        with mock.patch.object(extraction.subprocess, "run", return_value=completed):
            failure = self.assert_failure(
                render_pdf_page_preview(source, reference), "renderer_failed"
            )
        self.assertNotIn(sentinel, repr(failure))

    def test_non_bytes_stdout_is_invalid_and_output_exception_is_contained(self) -> None:
        source = synthetic_vector_pdf()
        reference = page_reference(source)
        completed = subprocess.CompletedProcess((), 0, stdout="invented", stderr=b"")
        with mock.patch.object(extraction.subprocess, "run", return_value=completed):
            self.assert_failure(
                render_pdf_page_preview(source, reference),
                "invalid_rendered_output",
            )

        completed = subprocess.CompletedProcess(
            (), 0, stdout=png_bytes(2, 2), stderr=b""
        )
        with (
            mock.patch.object(extraction.subprocess, "run", return_value=completed),
            mock.patch.object(
                extraction, "RenderedPdfPagePreview", side_effect=ValueError
            ),
        ):
            self.assert_failure(
                render_pdf_page_preview(source, reference),
                "page_preview_exception",
            )

    def test_base_exception_propagates(self) -> None:
        source = synthetic_vector_pdf()
        with mock.patch.object(
            extraction.subprocess, "run", side_effect=KeyboardInterrupt
        ):
            with self.assertRaises(KeyboardInterrupt):
                render_pdf_page_preview(source, page_reference(source))


@unittest.skipUnless(
    POPPLER_AVAILABLE,
    f"real Poppler rendering unavailable ({POPPLER_MISSING_REASON})",
)
class RealRendererAndBoundaryTests(PreviewTestCase):
    def test_real_rendering_is_deterministic_in_the_pinned_environment(self) -> None:
        source = synthetic_vector_pdf()
        reference = page_reference(source)
        first = render_pdf_page_preview(source, reference)
        second = render_pdf_page_preview(source, reference)
        self.assertIsInstance(first, RenderedPdfPagePreview)
        self.assertIsInstance(second, RenderedPdfPagePreview)
        assert isinstance(first, RenderedPdfPagePreview)
        assert isinstance(second, RenderedPdfPagePreview)
        self.assertEqual(first, second)
        self.assertEqual((first.width_px, first.height_px), (288, 288))

    def test_preview_coordinates_feed_t022_full_page_without_transform(self) -> None:
        source = synthetic_vector_pdf()
        reference = page_reference(source)
        preview = render_pdf_page_preview(source, reference)
        self.assertIsInstance(preview, RenderedPdfPagePreview)
        assert isinstance(preview, RenderedPdfPagePreview)
        extracted = extract_pdf_page_region(
            source,
            reference,
            left_px=0,
            top_px=0,
            width_px=preview.width_px,
            height_px=preview.height_px,
        )
        self.assertIsInstance(extracted, ExtractedPdfVisual)
        assert isinstance(extracted, ExtractedPdfVisual)
        self.assertEqual((extracted.left_px, extracted.top_px), (0, 0))
        self.assertEqual(
            (extracted.width_px, extracted.height_px),
            (preview.width_px, preview.height_px),
        )
        self.assertEqual(extracted.content_bytes, preview.content_bytes)

    def test_ast_dependency_and_ephemeral_boundary(self) -> None:
        path = Path(extraction.__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        forbidden_fragments = (
            "PIL",
            "cv2",
            "fitz",
            "pdfinfo",
            "pdfplumber",
            "pypdf",
            "PyPDF2",
            "requests",
            "socket",
            "tempfile",
            "urllib",
        )
        for fragment in forbidden_fragments:
            self.assertFalse(any(fragment in name for name in imports))

        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
            and node.func.attr == "run"
        ]
        self.assertEqual(len(calls), 1)
        source = path.read_text(encoding="utf-8")
        for forbidden in ("open(", "write_bytes(", "TemporaryDirectory(", ".save("):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
