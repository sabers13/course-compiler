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
    ASSET_REFERENCE_VERSION,
    PDF_PAGE_REFERENCE_VERSION,
    PDF_VISUAL_EXTRACTION_PROFILE,
    PDF_VISUAL_EXTRACTION_TIMEOUT_SECONDS,
    AssetReference,
    ExtractedPdfVisual,
    PdfPageReference,
    PdfVisualExtractionDiagnostic,
    PdfVisualExtractionFailure,
    extract_pdf_page_region,
)
from course_compiler.workflow import (
    SOURCE_EVIDENCE_REFERENCE_VERSION,
    SourceEvidenceReference,
)


def synthetic_vector_pdf() -> bytes:
    """Build an invented one-page PDF containing only vector drawing commands."""

    content = (
        b"0.90 0.95 1.00 rg 12 12 120 120 re f\n"
        b"0.10 0.30 0.70 RG 4 w 18 18 m 126 126 l S\n"
        b"0.80 0.20 0.10 rg 52 42 48 62 re f\n"
    )
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 144 144] "
            b"/CropBox [0 0 144 144] /Resources << >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n"
        + content
        + b"endstream",
    )
    output = bytearray(b"%PDF-1.4\n% synthetic vector fixture\n")
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


def png_bytes(width: int, height: int, color: tuple[int, int, int] = (2, 3, 5)) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(color) * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


def page_reference(source: bytes, ordinal: int = 1) -> PdfPageReference:
    source_reference = SourceEvidenceReference(
        SOURCE_EVIDENCE_REFERENCE_VERSION,
        "invented-vector-pdf",
        hashlib.sha256(source).hexdigest(),
    )
    return PdfPageReference(PDF_PAGE_REFERENCE_VERSION, source_reference, ordinal)


def extract_with_mocked_png(
    source: bytes,
    output: bytes,
    *,
    reference: PdfPageReference | None = None,
    left_px: int = 4,
    top_px: int = 6,
    width_px: int = 12,
    height_px: int = 10,
) -> object:
    completed = subprocess.CompletedProcess((), 0, stdout=output, stderr=b"")
    with mock.patch.object(extraction.subprocess, "run", return_value=completed):
        return extract_pdf_page_region(
            source,
            reference or page_reference(source),
            left_px=left_px,
            top_px=top_px,
            width_px=width_px,
            height_px=height_px,
        )


class ExtractionTestCase(unittest.TestCase):
    def assert_failure(self, result: object, code: str) -> PdfVisualExtractionFailure:
        self.assertIsInstance(result, PdfVisualExtractionFailure)
        assert isinstance(result, PdfVisualExtractionFailure)
        self.assertEqual(result.status, "pdf_visual_extraction_failed")
        self.assertEqual(len(result.diagnostics), 1)
        self.assertEqual(result.diagnostics[0].code, code)
        return result


class PublicExtractionContractTests(ExtractionTestCase):
    def test_public_constants_exports_and_exact_record_shapes(self) -> None:
        self.assertEqual(
            PDF_VISUAL_EXTRACTION_PROFILE,
            "pdftoppm-cropbox-region-png-144dpi/v1",
        )
        self.assertEqual(PDF_VISUAL_EXTRACTION_TIMEOUT_SECONDS, 30)
        self.assertEqual(
            [item.name for item in fields(ExtractedPdfVisual)],
            [
                "status",
                "extraction_profile",
                "page_reference",
                "left_px",
                "top_px",
                "width_px",
                "height_px",
                "asset_reference",
                "format",
                "content_bytes",
            ],
        )
        self.assertEqual(
            [item.name for item in fields(PdfVisualExtractionDiagnostic)],
            ["code", "classification", "message"],
        )
        self.assertEqual(
            [item.name for item in fields(PdfVisualExtractionFailure)],
            ["status", "diagnostics"],
        )
        for name in (
            "PDF_VISUAL_EXTRACTION_PROFILE",
            "PDF_VISUAL_EXTRACTION_TIMEOUT_SECONDS",
            "ExtractedPdfVisual",
            "PdfVisualExtractionDiagnostic",
            "PdfVisualExtractionFailure",
            "PdfVisualExtractionResult",
            "extract_pdf_page_region",
        ):
            self.assertIn(name, course_compiler.__all__)

    def test_records_are_frozen_slotted_and_payload_repr_is_redacted(self) -> None:
        source = synthetic_vector_pdf()
        content = png_bytes(12, 10)
        result = extract_with_mocked_png(source, content)
        self.assertIsInstance(result, ExtractedPdfVisual)
        assert isinstance(result, ExtractedPdfVisual)
        self.assertFalse(hasattr(result, "__dict__"))
        self.assertNotIn("content_bytes", repr(result))
        self.assertNotIn(content.hex(), repr(result))
        with self.assertRaises(FrozenInstanceError):
            result.status = "other"  # type: ignore[misc]

        diagnostic = PdfVisualExtractionDiagnostic(
            "renderer_timeout",
            "renderer",
            "The fixed local PDF renderer timed out.",
        )
        failure = PdfVisualExtractionFailure(
            "pdf_visual_extraction_failed", (diagnostic,)
        )
        self.assertFalse(hasattr(diagnostic, "__dict__"))
        self.assertFalse(hasattr(failure, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            diagnostic.code = "other"  # type: ignore[misc]

    def test_reconstructed_public_values_are_revalidated(self) -> None:
        with self.assertRaisesRegex(ValueError, "diagnostic code"):
            PdfVisualExtractionDiagnostic("invented", "adapter", "invented")
        with self.assertRaisesRegex(ValueError, "diagnostic fields"):
            PdfVisualExtractionDiagnostic(
                "renderer_timeout", "renderer", "invented dynamic text"
            )
        diagnostic = PdfVisualExtractionDiagnostic(
            "renderer_timeout",
            "renderer",
            "The fixed local PDF renderer timed out.",
        )
        object.__setattr__(diagnostic, "message", "invented dynamic text")
        with self.assertRaisesRegex(ValueError, "failure diagnostics"):
            PdfVisualExtractionFailure(
                "pdf_visual_extraction_failed", (diagnostic,)
            )

        source = synthetic_vector_pdf()
        content = png_bytes(12, 10)
        result = extract_with_mocked_png(source, content)
        assert isinstance(result, ExtractedPdfVisual)
        with self.assertRaisesRegex(ValueError, "asset reference"):
            ExtractedPdfVisual(
                result.status,
                result.extraction_profile,
                result.page_reference,
                result.left_px,
                result.top_px,
                result.width_px,
                result.height_px,
                AssetReference(ASSET_REFERENCE_VERSION, "1" * 64),
                result.format,
                result.content_bytes,
            )


class InputAndProcessTests(ExtractionTestCase):
    def test_wrong_runtime_types_raise_before_subprocess(self) -> None:
        source = synthetic_vector_pdf()
        reference = page_reference(source)
        calls = (
            (bytearray(source), reference, 0, 0, 1, 1),
            (source, object(), 0, 0, 1, 1),
            (source, reference, True, 0, 1, 1),
            (source, reference, 0, 0.0, 1, 1),
            (source, reference, 0, 0, "1", 1),
            (source, reference, 0, 0, 1, None),
        )
        for values in calls:
            with self.subTest(types=tuple(type(value).__name__ for value in values)):
                with mock.patch.object(extraction.subprocess, "run") as run:
                    with self.assertRaises(TypeError):
                        extract_pdf_page_region(
                            values[0],  # type: ignore[arg-type]
                            values[1],  # type: ignore[arg-type]
                            left_px=values[2],  # type: ignore[arg-type]
                            top_px=values[3],  # type: ignore[arg-type]
                            width_px=values[4],  # type: ignore[arg-type]
                            height_px=values[5],  # type: ignore[arg-type]
                        )
                run.assert_not_called()

    def test_invalid_reconstructed_page_and_region_bounds_fail_before_process(self) -> None:
        source = synthetic_vector_pdf()
        forged = page_reference(source)
        object.__setattr__(forged, "physical_page_ordinal", 0)
        with mock.patch.object(extraction.subprocess, "run") as run:
            result = extract_pdf_page_region(
                source, forged, left_px=0, top_px=0, width_px=1, height_px=1
            )
        self.assert_failure(result, "invalid_extraction_input")
        run.assert_not_called()

        invalid_regions = (
            (-1, 0, 1, 1),
            (2_147_483_648, 0, 1, 1),
            (0, -1, 1, 1),
            (0, 2_147_483_648, 1, 1),
            (0, 0, 0, 1),
            (0, 0, 8193, 1),
            (0, 0, 1, 0),
            (0, 0, 1, 8193),
            (0, 0, 4097, 4096),
        )
        for left, top, width, height in invalid_regions:
            with self.subTest(region=(left, top, width, height)):
                with mock.patch.object(extraction.subprocess, "run") as run:
                    result = extract_pdf_page_region(
                        source,
                        page_reference(source),
                        left_px=left,
                        top_px=top,
                        width_px=width,
                        height_px=height,
                    )
                self.assert_failure(result, "invalid_extraction_input")
                run.assert_not_called()

        boundary_output = bytearray(png_bytes(1, 1))
        boundary_output[16:20] = (4096).to_bytes(4, "big")
        boundary_output[20:24] = (4096).to_bytes(4, "big")
        result = extract_with_mocked_png(
            source,
            bytes(boundary_output),
            left_px=2_147_483_647,
            top_px=2_147_483_647,
            width_px=4096,
            height_px=4096,
        )
        self.assertIsInstance(result, ExtractedPdfVisual)

    def test_source_digest_mismatch_fails_before_process(self) -> None:
        source = synthetic_vector_pdf()
        other = source + b"invented mismatch"
        with mock.patch.object(extraction.subprocess, "run") as run:
            result = extract_pdf_page_region(
                other,
                page_reference(source),
                left_px=0,
                top_px=0,
                width_px=1,
                height_px=1,
            )
        self.assert_failure(result, "source_bytes_mismatch")
        run.assert_not_called()

    def test_fixed_renderer_invocation_is_exact_and_single(self) -> None:
        source = synthetic_vector_pdf()
        reference = page_reference(source, ordinal=7)
        output = png_bytes(321, 123)
        completed = subprocess.CompletedProcess((), 0, stdout=output, stderr=b"")
        with mock.patch.object(
            extraction.subprocess, "run", return_value=completed
        ) as run:
            result = extract_pdf_page_region(
                source,
                reference,
                left_px=11,
                top_px=13,
                width_px=321,
                height_px=123,
            )
        self.assertIsInstance(result, ExtractedPdfVisual)
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
                "-x",
                "11",
                "-y",
                "13",
                "-W",
                "321",
                "-H",
                "123",
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


class OutputIdentityAndFailureTests(ExtractionTestCase):
    def test_exact_stdout_is_retained_and_hashed_through(self) -> None:
        source = synthetic_vector_pdf()
        first_png = png_bytes(12, 10, (1, 2, 3))
        second_png = png_bytes(12, 10, (3, 2, 1))
        first = extract_with_mocked_png(source, first_png)
        same = extract_with_mocked_png(source, first_png)
        different = extract_with_mocked_png(source, second_png)
        for value in (first, same, different):
            self.assertIsInstance(value, ExtractedPdfVisual)
        assert isinstance(first, ExtractedPdfVisual)
        assert isinstance(same, ExtractedPdfVisual)
        assert isinstance(different, ExtractedPdfVisual)
        self.assertIs(first.content_bytes, first_png)
        self.assertEqual(
            first.asset_reference,
            AssetReference(
                ASSET_REFERENCE_VERSION, hashlib.sha256(first_png).hexdigest()
            ),
        )
        self.assertEqual(first.asset_reference, same.asset_reference)
        self.assertNotEqual(first.asset_reference, different.asset_reference)

    def test_identical_bytes_may_have_different_page_and_region_provenance(self) -> None:
        source = synthetic_vector_pdf()
        output = png_bytes(12, 10)
        first = extract_with_mocked_png(source, output)
        second = extract_with_mocked_png(
            source,
            output,
            reference=page_reference(source, ordinal=2),
            left_px=40,
            top_px=60,
        )
        assert isinstance(first, ExtractedPdfVisual)
        assert isinstance(second, ExtractedPdfVisual)
        self.assertNotEqual(first.page_reference, second.page_reference)
        self.assertNotEqual((first.left_px, first.top_px), (second.left_px, second.top_px))
        self.assertEqual(first.asset_reference, second.asset_reference)

    def test_renderer_failures_are_fixed_and_redacted(self) -> None:
        sentinel = "invented/private/renderer-secret"
        source = synthetic_vector_pdf() + sentinel.encode("ascii")
        reference = page_reference(source)
        cases = (
            (FileNotFoundError(sentinel), "renderer_unavailable"),
            (
                subprocess.TimeoutExpired(
                    ("pdftoppm",), 30, output=sentinel.encode(), stderr=sentinel.encode()
                ),
                "renderer_timeout",
            ),
            (RuntimeError(sentinel), "extraction_exception"),
        )
        for error, code in cases:
            with self.subTest(code=code):
                with mock.patch.object(extraction.subprocess, "run", side_effect=error):
                    result = extract_pdf_page_region(
                        source,
                        reference,
                        left_px=0,
                        top_px=0,
                        width_px=1,
                        height_px=1,
                    )
                failure = self.assert_failure(result, code)
                self.assertNotIn(sentinel, repr(failure) + str(failure))

        completed = subprocess.CompletedProcess(
            (), 1, stdout=sentinel.encode(), stderr=sentinel.encode()
        )
        with mock.patch.object(extraction.subprocess, "run", return_value=completed):
            result = extract_pdf_page_region(
                source,
                reference,
                left_px=0,
                top_px=0,
                width_px=1,
                height_px=1,
            )
        failure = self.assert_failure(result, "renderer_failed")
        self.assertNotIn(sentinel, repr(failure) + str(failure))

    def test_malformed_png_headers_and_empty_stdout_fail_closed(self) -> None:
        source = synthetic_vector_pdf()
        valid = png_bytes(12, 10)
        bad_length = bytearray(valid)
        bad_length[11] = 12
        bad_type = bytearray(valid)
        bad_type[12:16] = b"IDAT"
        zero_width = bytearray(valid)
        zero_width[16:20] = b"\x00\x00\x00\x00"
        values = (
            b"",
            b"invented non-PNG output",
            valid[:32],
            bytes(bad_length),
            bytes(bad_type),
            bytes(zero_width),
        )
        for output in values:
            with self.subTest(size=len(output)):
                result = extract_with_mocked_png(source, output)
                self.assert_failure(result, "invalid_rendered_output")

    def test_clipped_or_wrong_dimensions_are_region_out_of_bounds(self) -> None:
        source = synthetic_vector_pdf()
        for output in (png_bytes(11, 10), png_bytes(12, 9), png_bytes(1, 1)):
            with self.subTest(size=len(output)):
                result = extract_with_mocked_png(source, output)
                self.assert_failure(result, "region_out_of_bounds")

    def test_base_exception_propagates(self) -> None:
        class StopExtraction(BaseException):
            pass

        source = synthetic_vector_pdf()
        with mock.patch.object(
            extraction.subprocess, "run", side_effect=StopExtraction()
        ):
            with self.assertRaises(StopExtraction):
                extract_pdf_page_region(
                    source,
                    page_reference(source),
                    left_px=0,
                    top_px=0,
                    width_px=1,
                    height_px=1,
                )


@unittest.skipUnless(
    POPPLER_AVAILABLE,
    f"real Poppler extraction unavailable ({POPPLER_MISSING_REASON})",
)
class RealPopplerAndBoundaryTests(ExtractionTestCase):
    def test_real_poppler_vector_region_is_byte_deterministic(self) -> None:
        source = synthetic_vector_pdf()
        reference = page_reference(source)
        arguments = dict(left_px=20, top_px=24, width_px=120, height_px=90)
        first = extract_pdf_page_region(source, reference, **arguments)
        second = extract_pdf_page_region(source, reference, **arguments)
        self.assertIsInstance(first, ExtractedPdfVisual)
        self.assertIsInstance(second, ExtractedPdfVisual)
        assert isinstance(first, ExtractedPdfVisual)
        assert isinstance(second, ExtractedPdfVisual)
        self.assertEqual(first.content_bytes, second.content_bytes)
        self.assertEqual(first.asset_reference, second.asset_reference)
        self.assertEqual(
            (
                first.extraction_profile,
                first.page_reference,
                first.left_px,
                first.top_px,
                first.width_px,
                first.height_px,
                first.format,
            ),
            (
                second.extraction_profile,
                second.page_reference,
                second.left_px,
                second.top_px,
                second.width_px,
                second.height_px,
                second.format,
            ),
        )

    def test_real_poppler_clipped_region_fails_closed(self) -> None:
        source = synthetic_vector_pdf()
        result = extract_pdf_page_region(
            source,
            page_reference(source),
            left_px=280,
            top_px=0,
            width_px=20,
            height_px=10,
        )
        self.assert_failure(result, "region_out_of_bounds")

    def test_production_module_has_no_filesystem_or_unapproved_dependencies(self) -> None:
        module_path = (
            Path(__file__).resolve().parents[1]
            / "course_compiler"
            / "pdf_visual_extraction.py"
        )
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imports: list[str] = []
        forbidden_calls: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(("." * node.level) + (node.module or ""))
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in {
                    "open",
                    "mkstemp",
                    "NamedTemporaryFile",
                    "TemporaryDirectory",
                }:
                    forbidden_calls.add(node.func.id)
                if isinstance(node.func, ast.Attribute) and node.func.attr in {
                    "read_bytes",
                    "write_bytes",
                    "read_text",
                    "write_text",
                }:
                    forbidden_calls.add(node.func.attr)
        self.assertEqual(
            sorted(imports),
            [
                ".asset",
                ".pdf_page",
                "__future__",
                "dataclasses",
                "hashlib",
                "subprocess",
                "typing",
            ],
        )
        self.assertFalse(forbidden_calls)
        source = module_path.read_text(encoding="utf-8").lower()
        for forbidden in (
            "tempfile",
            "pathlib",
            "pdfinfo",
            "pypdf",
            "pymupdf",
            "pdfplumber",
            "pillow",
            "tesseract",
            "sqlite",
            "assetpayload",
            "visualplacement",
        ):
            self.assertNotIn(forbidden, source)

    def test_t020_and_t021_identity_shapes_remain_unchanged(self) -> None:
        self.assertEqual(
            [item.name for item in fields(PdfPageReference)],
            ["reference_version", "source_reference", "physical_page_ordinal"],
        )
        self.assertEqual(
            [item.name for item in fields(AssetReference)],
            ["reference_version", "content_sha256"],
        )


if __name__ == "__main__":
    unittest.main()
