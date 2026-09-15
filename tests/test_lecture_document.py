from __future__ import annotations

import hashlib
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

from course_compiler.contracts import (
    DIAGNOSTIC_MESSAGES,
    DOCUMENT_CONTRACT_VERSION,
    LectureDocument,
    RenderDiagnostic,
    SourceProvenance,
    has_validation_errors,
    validate_document,
)


FIXTURES = Path(__file__).parent / "fixtures" / "synthetic" / "lecture-document"


def digest_for(source_text: str) -> str:
    return hashlib.sha256(source_text.encode("utf-8")).hexdigest()


def make_document(
    source_text: object,
    *,
    contract_version: object = DOCUMENT_CONTRACT_VERSION,
    document_id: object = "synthetic-lecture",
    order: object = 1,
    digest: object | None = None,
) -> LectureDocument:
    if digest is None:
        digest = digest_for(source_text)  # type: ignore[arg-type]
    return LectureDocument(
        contract_version=contract_version,  # type: ignore[arg-type]
        document_id=document_id,  # type: ignore[arg-type]
        order=order,  # type: ignore[arg-type]
        source_text=source_text,  # type: ignore[arg-type]
        provenance=SourceProvenance(content_sha256=digest),  # type: ignore[arg-type]
    )


def codes(document: LectureDocument) -> tuple[str, ...]:
    return tuple(item.code for item in validate_document(document))


class LectureDocumentShapeTests(unittest.TestCase):
    def test_contract_has_exact_required_fields(self) -> None:
        self.assertEqual(
            [item.name for item in fields(LectureDocument)],
            ["contract_version", "document_id", "order", "source_text", "provenance"],
        )
        self.assertEqual(
            [item.name for item in fields(SourceProvenance)],
            ["content_sha256"],
        )

    def test_contract_is_frozen_slotted_and_redacts_source(self) -> None:
        secret = "private-source-sentinel"
        document = make_document(secret)
        self.assertNotIn(secret, repr(document))
        self.assertNotIn("source_text", repr(document))
        self.assertFalse(hasattr(document, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            document.order = 2  # type: ignore[misc]

    def test_provenance_is_frozen_slotted_and_content_free(self) -> None:
        provenance = SourceProvenance(content_sha256="0" * 64)
        self.assertFalse(hasattr(provenance, "__dict__"))
        self.assertFalse(hasattr(provenance, "source_kind"))
        with self.assertRaises(FrozenInstanceError):
            provenance.content_sha256 = "1" * 64  # type: ignore[misc]

    def test_diagnostic_has_exact_content_free_fields(self) -> None:
        self.assertEqual(
            [item.name for item in fields(RenderDiagnostic)],
            ["code", "severity", "stage", "line"],
        )
        diagnostic = RenderDiagnostic(
            code="short_source", severity="warning", stage="validation", line=None
        )
        self.assertNotIn("private-source-sentinel", repr(diagnostic))

    def test_all_registered_codes_enforce_canonical_classification(self) -> None:
        classifications = {
            "empty_source": ("warning", "validation"),
            "short_source": ("warning", "validation"),
            "unclosed_code_fence": ("warning", "validation"),
            "unclosed_display_math": ("warning", "validation"),
            "unsupported_contract_version": ("error", "validation"),
            "invalid_document_id": ("error", "validation"),
            "invalid_order": ("error", "validation"),
            "invalid_source_text": ("error", "validation"),
            "invalid_source_digest": ("error", "validation"),
            "source_digest_mismatch": ("error", "validation"),
            "renderer_exception": ("error", "render"),
            "invalid_explicit_math": ("error", "render"),
            "structural_postcondition_mismatch": ("error", "render"),
        }
        self.assertEqual(set(classifications), set(DIAGNOSTIC_MESSAGES))
        possible_classifications = {
            ("warning", "validation"),
            ("warning", "render"),
            ("error", "validation"),
            ("error", "render"),
        }
        for code, canonical in classifications.items():
            with self.subTest(code=code, classification=canonical):
                RenderDiagnostic(
                    code=code,
                    severity=canonical[0],
                    stage=canonical[1],
                    line=None,
                )
            for classification in possible_classifications - {canonical}:
                with self.subTest(code=code, classification=classification):
                    with self.assertRaisesRegex(
                        ValueError,
                        "^diagnostic classification does not match registered code$",
                    ):
                        RenderDiagnostic(
                            code=code,
                            severity=classification[0],
                            stage=classification[1],
                            line=None,
                        )

    def test_invalid_order_cannot_be_classified_as_warning(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "^diagnostic classification does not match registered code$",
        ):
            RenderDiagnostic(
                code="invalid_order",
                severity="warning",
                stage="validation",
                line=None,
            )

    def test_renderer_exception_cannot_be_classified_as_warning(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "^diagnostic classification does not match registered code$",
        ):
            RenderDiagnostic(
                code="renderer_exception",
                severity="warning",
                stage="render",
                line=None,
            )

    def test_diagnostic_classification_exception_is_content_safe(self) -> None:
        secret = "private-classification-sentinel"
        with self.assertRaises(ValueError) as caught:
            RenderDiagnostic(
                code="invalid_order",
                severity=secret,  # type: ignore[arg-type]
                stage="validation",
                line=None,
            )
        self.assertEqual(
            str(caught.exception),
            "diagnostic classification does not match registered code",
        )
        self.assertNotIn(secret, str(caught.exception))


class LectureDocumentValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.minimal = (FIXTURES / "minimal.md").read_text(encoding="utf-8")
        cls.structures = (FIXTURES / "structures.md").read_text(encoding="utf-8")
        cls.unclosed = (FIXTURES / "unclosed-blocks.md").read_text(encoding="utf-8")

    def test_minimal_fixture_is_warning_free_and_over_threshold(self) -> None:
        self.assertGreaterEqual(len(self.minimal), 1000)
        self.assertEqual(validate_document(make_document(self.minimal)), ())

    def test_structures_fixture_is_closed(self) -> None:
        self.assertEqual(codes(make_document(self.structures)), ("short_source",))

    def test_unclosed_display_fixture_reports_content_free_warning(self) -> None:
        diagnostics = validate_document(make_document(self.unclosed))
        self.assertEqual(
            tuple(item.code for item in diagnostics),
            ("short_source", "unclosed_display_math"),
        )
        self.assertTrue(all(item.severity == "warning" for item in diagnostics))
        self.assertNotIn(self.unclosed, repr(diagnostics))

    def test_unclosed_code_fence_is_a_warning(self) -> None:
        source = "# Invented\n\n```text\nvalue = 3\n"
        self.assertEqual(
            codes(make_document(source)),
            ("short_source", "unclosed_code_fence"),
        )

    def test_empty_and_short_diagnostics_are_mutually_exclusive(self) -> None:
        self.assertEqual(codes(make_document("")), ("empty_source",))
        self.assertEqual(codes(make_document("x" * 999)), ("short_source",))
        self.assertEqual(codes(make_document("x" * 1000)), ())

    def test_missing_heading_is_not_a_v1_diagnostic(self) -> None:
        self.assertEqual(codes(make_document("invented plain text")), ("short_source",))
        self.assertNotIn("missing_heading", DIAGNOSTIC_MESSAGES)

    def test_document_id_rule_is_exact(self) -> None:
        accepted = ("a", "a.b_c-9", "a" + "b" * 63)
        rejected = ("", ".", "..", "A", "-a", "a/b", "a" * 65)
        for document_id in accepted:
            with self.subTest(accepted=document_id):
                self.assertNotIn(
                    "invalid_document_id",
                    codes(make_document(self.minimal, document_id=document_id)),
                )
        for document_id in rejected:
            with self.subTest(rejected=document_id):
                self.assertIn(
                    "invalid_document_id",
                    codes(make_document(self.minimal, document_id=document_id)),
                )

    def test_order_rejects_booleans_nonintegers_and_out_of_range_values(self) -> None:
        for order in (True, False, 0, 10000, 1.0, "1"):
            with self.subTest(order=order):
                self.assertIn(
                    "invalid_order",
                    codes(make_document(self.minimal, order=order)),
                )
        for order in (1, 9999):
            with self.subTest(order=order):
                self.assertNotIn(
                    "invalid_order",
                    codes(make_document(self.minimal, order=order)),
                )

    def test_contract_version_is_exact(self) -> None:
        document = make_document(self.minimal, contract_version="lecture-document/v2")
        self.assertEqual(codes(document), ("unsupported_contract_version",))

    def test_source_rejects_bom_cr_nul_and_unencodable_surrogates(self) -> None:
        invalid_sources = ("\ufeffinvented", "invented\r\n", "invented\x00", "\ud800")
        for source in invalid_sources:
            with self.subTest(source_type=repr(source[:1])):
                document = make_document(source, digest="0" * 64)
                self.assertEqual(codes(document), ("invalid_source_text",))

    def test_digest_format_and_exact_canonical_text_are_validated(self) -> None:
        invalid_format = make_document(self.minimal, digest="A" * 64)
        mismatch = make_document(self.minimal, digest="0" * 64)
        self.assertEqual(codes(invalid_format), ("invalid_source_digest",))
        self.assertEqual(codes(mismatch), ("source_digest_mismatch",))

    def test_diagnostics_are_sorted_by_code_and_errors_are_detectable(self) -> None:
        document = make_document(
            "x",
            contract_version="wrong",
            document_id=".",
            order=0,
            digest="bad",
        )
        diagnostics = validate_document(document)
        self.assertEqual(
            tuple(item.code for item in diagnostics),
            tuple(sorted(item.code for item in diagnostics)),
        )
        self.assertTrue(has_validation_errors(diagnostics))
        self.assertEqual(
            tuple(item.severity for item in diagnostics).count("warning"),
            1,
        )

    def test_diagnostics_and_fixed_messages_never_include_source(self) -> None:
        secret = "never-emit-this-source-sentinel"
        document = make_document(secret, document_id=".", digest="0" * 64)
        diagnostics = validate_document(document)
        self.assertNotIn(secret, repr(diagnostics))
        self.assertTrue(all(secret not in message for message in DIAGNOSTIC_MESSAGES.values()))


if __name__ == "__main__":
    unittest.main()
