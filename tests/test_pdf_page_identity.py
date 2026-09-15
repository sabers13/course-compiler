from __future__ import annotations

import ast
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import course_compiler
from course_compiler import PDF_PAGE_REFERENCE_VERSION, PdfPageReference
from course_compiler.workflow import (
    SOURCE_EVIDENCE_REFERENCE_VERSION,
    SourceEvidenceReference,
)


class HostileStr(str):
    def _raise(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError("hostile caller value must not be used")

    __eq__ = _raise
    __ne__ = _raise
    __str__ = _raise


def source_reference(
    source_id: str = "invented-source",
    content_sha256: str = "1" * 64,
) -> SourceEvidenceReference:
    return SourceEvidenceReference(
        SOURCE_EVIDENCE_REFERENCE_VERSION,
        source_id,
        content_sha256,
    )


class PdfPageIdentityTests(unittest.TestCase):
    def test_public_shape_is_exact_frozen_slotted_and_hashable(self) -> None:
        self.assertEqual(PDF_PAGE_REFERENCE_VERSION, "pdf-page-reference/v1")
        self.assertEqual(
            [item.name for item in fields(PdfPageReference)],
            ["reference_version", "source_reference", "physical_page_ordinal"],
        )
        reference = PdfPageReference(
            PDF_PAGE_REFERENCE_VERSION, source_reference(), 1
        )
        self.assertFalse(hasattr(reference, "__dict__"))
        self.assertIsInstance(hash(reference), int)
        with self.assertRaises(FrozenInstanceError):
            reference.physical_page_ordinal = 2  # type: ignore[misc]

    def test_only_exact_supported_version_is_accepted(self) -> None:
        valid_source = source_reference()
        self.assertEqual(
            PdfPageReference("pdf-page-reference/v1", valid_source, 1).reference_version,
            PDF_PAGE_REFERENCE_VERSION,
        )
        for value in ("pdf-page-reference/v2", "", b"pdf-page-reference/v1", None, HostileStr(PDF_PAGE_REFERENCE_VERSION), 1):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(
                    ValueError, "^PDF page reference version is unsupported$"
                ) as caught:
                    PdfPageReference(value, valid_source, 1)  # type: ignore[arg-type]
                self.assertNotIn("hostile caller value", str(caught.exception))

    def test_source_reference_is_exact_and_revalidated(self) -> None:
        valid = source_reference()
        reference = PdfPageReference(PDF_PAGE_REFERENCE_VERSION, valid, 1)
        self.assertIs(reference.source_reference, valid)
        forged_bad_id = object.__new__(SourceEvidenceReference)
        object.__setattr__(
            forged_bad_id, "reference_version", SOURCE_EVIDENCE_REFERENCE_VERSION
        )
        object.__setattr__(forged_bad_id, "source_id", "bad/path")
        object.__setattr__(forged_bad_id, "content_sha256", "1" * 64)
        forged_bad_version = object.__new__(SourceEvidenceReference)
        object.__setattr__(forged_bad_version, "reference_version", "source/v9")
        object.__setattr__(forged_bad_version, "source_id", "invented-source")
        object.__setattr__(forged_bad_version, "content_sha256", "1" * 64)
        invalid_values = (None, "invented-source", forged_bad_id, forged_bad_version)
        for value in invalid_values:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(
                    ValueError, "^PDF page source reference is invalid$"
                ) as caught:
                    PdfPageReference(PDF_PAGE_REFERENCE_VERSION, value, 1)  # type: ignore[arg-type]
                self.assertNotIn("bad/path", str(caught.exception))

    def test_positive_builtin_integer_ordinals_are_one_based(self) -> None:
        valid_source = source_reference()
        for ordinal in (1, 2, 500, 10**100):
            with self.subTest(ordinal=ordinal):
                self.assertEqual(
                    PdfPageReference(
                        PDF_PAGE_REFERENCE_VERSION, valid_source, ordinal
                    ).physical_page_ordinal,
                    ordinal,
                )
        for ordinal in (0, -1, -500, True, False, 1.0, "1", None):
            with self.subTest(ordinal=repr(ordinal)):
                with self.assertRaisesRegex(
                    ValueError, "^PDF physical page ordinal is invalid$"
                ):
                    PdfPageReference(
                        PDF_PAGE_REFERENCE_VERSION, valid_source, ordinal  # type: ignore[arg-type]
                    )

    def test_complete_identity_uses_source_and_ordinal(self) -> None:
        first = PdfPageReference(PDF_PAGE_REFERENCE_VERSION, source_reference(), 2)
        same = PdfPageReference(PDF_PAGE_REFERENCE_VERSION, source_reference(), 2)
        different_source_id = PdfPageReference(
            PDF_PAGE_REFERENCE_VERSION, source_reference("other-source"), 2
        )
        different_digest = PdfPageReference(
            PDF_PAGE_REFERENCE_VERSION, source_reference(content_sha256="2" * 64), 2
        )
        different_ordinal = PdfPageReference(
            PDF_PAGE_REFERENCE_VERSION, source_reference(), 3
        )
        self.assertEqual(first, same)
        self.assertEqual(hash(first), hash(same))
        self.assertNotEqual(first, different_source_id)
        self.assertNotEqual(first, different_digest)
        self.assertNotEqual(first, different_ordinal)

    def test_page_reference_has_no_ordering_contract(self) -> None:
        first = PdfPageReference(PDF_PAGE_REFERENCE_VERSION, source_reference(), 1)
        second = PdfPageReference(PDF_PAGE_REFERENCE_VERSION, source_reference(), 2)
        with self.assertRaises(TypeError):
            first < second  # type: ignore[operator]

    def test_package_exports_public_contract(self) -> None:
        self.assertIs(course_compiler.PdfPageReference, PdfPageReference)
        self.assertEqual(
            course_compiler.PDF_PAGE_REFERENCE_VERSION,
            PDF_PAGE_REFERENCE_VERSION,
        )
        self.assertIn("PdfPageReference", course_compiler.__all__)
        self.assertIn("PDF_PAGE_REFERENCE_VERSION", course_compiler.__all__)

    def test_domain_module_has_only_approved_dependencies_and_no_io(self) -> None:
        module_path = Path(__file__).resolve().parents[1] / "course_compiler" / "pdf_page.py"
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imports: list[str] = []
        call_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(("." * node.level) + (node.module or ""))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                call_names.add(node.func.id)
        self.assertEqual(
            sorted(imports),
            [".workflow", "__future__", "dataclasses", "typing"],
        )
        self.assertTrue(
            call_names.isdisjoint({"__import__", "compile", "eval", "exec", "open"})
        )
        source = module_path.read_text(encoding="utf-8").lower()
        for forbidden in ("sqlite", "subprocess", "pathlib", "mime", "asset", "pypdf", "fitz"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
