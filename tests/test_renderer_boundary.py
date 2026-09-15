from __future__ import annotations

import ast
import hashlib
import inspect
import unittest
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path

from course_compiler import (
    DOCUMENT_CONTRACT_VERSION,
    LEGACY_RENDER_PROFILE,
    DocumentReference,
    LectureDocument,
    LectureRenderer,
    RejectedLecture,
    RenderDiagnostic,
    RenderedLecture,
    RendererFailure,
    RendererIdentity,
    SourceProvenance,
    StructuralMetrics,
    document_reference,
    structural_postconditions_match,
)


def make_identity() -> RendererIdentity:
    return RendererIdentity(
        renderer_name="synthetic-boundary",
        renderer_version="1.0.0",
        contract_version=DOCUMENT_CONTRACT_VERSION,
        render_profile=LEGACY_RENDER_PROFILE,
    )


def make_document(*, order: int = 1) -> LectureDocument:
    source = "invented source"
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return LectureDocument(
        contract_version=DOCUMENT_CONTRACT_VERSION,
        document_id="synthetic-document",
        order=order,
        source_text=source,
        provenance=SourceProvenance(content_sha256=digest),
    )


def make_reference(*, order: int = 1) -> DocumentReference:
    reference = document_reference(make_document(order=order))
    assert reference is not None
    return reference


def make_metrics() -> StructuralMetrics:
    return StructuralMetrics(
        input_characters=20,
        input_lines=2,
        input_headings=1,
        output_headings=1,
        input_code_blocks=1,
        output_code_blocks=1,
        input_display_math_blocks=1,
        output_display_math_blocks=1,
        input_tables=1,
        output_tables=1,
    )


def warning() -> RenderDiagnostic:
    return RenderDiagnostic(
        code="short_source", severity="warning", stage="validation", line=None
    )


def validation_error() -> RenderDiagnostic:
    return RenderDiagnostic(
        code="invalid_order", severity="error", stage="validation", line=None
    )


def render_error() -> RenderDiagnostic:
    return RenderDiagnostic(
        code="structural_postcondition_mismatch",
        severity="error",
        stage="render",
        line=None,
    )


class BoundaryShapeTests(unittest.TestCase):
    def test_renderer_protocol_is_strictly_single_document(self) -> None:
        signature = inspect.signature(LectureRenderer.render)
        self.assertEqual(tuple(signature.parameters), ("self", "document"))
        self.assertFalse(hasattr(LectureRenderer, "render_many"))

    def test_result_and_reference_fields_are_exact_and_fragment_only(self) -> None:
        self.assertEqual(
            [item.name for item in fields(DocumentReference)],
            ["contract_version", "document_id", "order", "content_sha256"],
        )
        self.assertEqual(
            [item.name for item in fields(RenderedLecture)],
            ["status", "document", "renderer", "tex_fragment", "metrics", "diagnostics"],
        )
        self.assertEqual(
            [item.name for item in fields(RejectedLecture)],
            ["status", "document", "diagnostics"],
        )
        self.assertEqual(
            [item.name for item in fields(RendererFailure)],
            ["status", "document", "renderer", "diagnostics"],
        )
        self.assertFalse(hasattr(make_reference(), "source_kind"))
        self.assertFalse(hasattr(self._rendered(), "output_name"))

    def test_identity_and_metrics_fields_are_exact(self) -> None:
        self.assertEqual(
            [item.name for item in fields(RendererIdentity)],
            ["renderer_name", "renderer_version", "contract_version", "render_profile"],
        )
        self.assertEqual(
            [item.name for item in fields(StructuralMetrics)],
            [
                "input_characters",
                "input_lines",
                "input_headings",
                "output_headings",
                "input_code_blocks",
                "output_code_blocks",
                "input_display_math_blocks",
                "output_display_math_blocks",
                "input_tables",
                "output_tables",
            ],
        )

    def test_boundary_types_are_frozen_and_slotted(self) -> None:
        rendered = self._rendered()
        self.assertFalse(hasattr(rendered, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            rendered.status = "rendered_with_warnings"  # type: ignore[misc]

    def test_production_boundary_modules_have_no_io_process_or_legacy_imports(self) -> None:
        forbidden = {"legacy", "os", "pathlib", "shutil", "subprocess", "tempfile"}
        for relative in ("course_compiler/contracts.py", "course_compiler/rendering.py"):
            tree = ast.parse(Path(relative).read_text(encoding="utf-8"), filename=relative)
            imports: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])
            self.assertFalse(forbidden & imports, relative)

    def _rendered(self) -> RenderedLecture:
        return RenderedLecture(
            status="rendered",
            document=make_reference(),
            renderer=make_identity(),
            tex_fragment="invented TeX fragment",
            metrics=make_metrics(),
            diagnostics=(),
        )


class ResultInvariantTests(unittest.TestCase):
    def test_rendered_has_no_diagnostics_and_redacts_fragment(self) -> None:
        secret = "private-tex-fragment-sentinel"
        result = RenderedLecture(
            status="rendered",
            document=make_reference(),
            renderer=make_identity(),
            tex_fragment=secret,
            metrics=make_metrics(),
            diagnostics=(),
        )
        self.assertNotIn(secret, repr(result))
        self.assertNotIn("tex_fragment", repr(result))

    def test_rendered_with_warnings_requires_warnings_only(self) -> None:
        result = RenderedLecture(
            status="rendered_with_warnings",
            document=make_reference(),
            renderer=make_identity(),
            tex_fragment="invented",
            metrics=make_metrics(),
            diagnostics=(warning(),),
        )
        self.assertEqual(result.status, "rendered_with_warnings")
        with self.assertRaisesRegex(ValueError, "warnings only"):
            replace(result, diagnostics=(render_error(),))
        with self.assertRaisesRegex(ValueError, "warnings only"):
            replace(result, diagnostics=())

    def test_wrongly_classified_diagnostic_cannot_create_warning_result(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "^diagnostic classification does not match registered code$",
        ):
            RenderedLecture(
                status="rendered_with_warnings",
                document=make_reference(),
                renderer=make_identity(),
                tex_fragment="invented",
                metrics=make_metrics(),
                diagnostics=(
                    RenderDiagnostic(
                        code="invalid_order",
                        severity="warning",
                        stage="validation",
                        line=None,
                    ),
                ),
            )

    def test_rendered_rejects_any_diagnostics(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot contain diagnostics"):
            RenderedLecture(
                status="rendered",
                document=make_reference(),
                renderer=make_identity(),
                tex_fragment="invented",
                metrics=make_metrics(),
                diagnostics=(warning(),),
            )

    def test_validation_failed_requires_validation_stage_errors(self) -> None:
        rejected = RejectedLecture(
            status="validation_failed",
            document=make_reference(),
            diagnostics=(validation_error(),),
        )
        self.assertEqual(rejected.status, "validation_failed")
        with self.assertRaisesRegex(ValueError, "validation-stage errors"):
            replace(rejected, diagnostics=(warning(),))
        with self.assertRaisesRegex(ValueError, "validation-stage errors"):
            replace(rejected, diagnostics=(render_error(),))

    def test_render_failed_requires_render_stage_errors(self) -> None:
        failure = RendererFailure(
            status="render_failed",
            document=make_reference(),
            renderer=make_identity(),
            diagnostics=(render_error(),),
        )
        self.assertEqual(failure.status, "render_failed")
        with self.assertRaisesRegex(ValueError, "render-stage errors"):
            replace(failure, diagnostics=(validation_error(),))

    def test_structural_mismatch_cannot_be_a_success_result(self) -> None:
        mismatched = replace(make_metrics(), output_tables=0)
        self.assertFalse(structural_postconditions_match(mismatched))
        with self.assertRaisesRegex(ValueError, "matching structural metrics"):
            RenderedLecture(
                status="rendered",
                document=make_reference(),
                renderer=make_identity(),
                tex_fragment="invented",
                metrics=mismatched,
                diagnostics=(),
            )
        failure = RendererFailure(
            status="render_failed",
            document=make_reference(),
            renderer=make_identity(),
            diagnostics=(render_error(),),
        )
        self.assertEqual(failure.status, "render_failed")

    def test_exceptions_do_not_echo_private_fragment(self) -> None:
        secret = "never-echo-this-fragment"
        try:
            RenderedLecture(
                status="rendered",
                document=make_reference(),
                renderer=make_identity(),
                tex_fragment=secret,
                metrics=make_metrics(),
                diagnostics=(warning(),),
            )
        except ValueError as error:
            self.assertNotIn(secret, str(error))
        else:
            self.fail("invalid rendered result was accepted")

    def test_malformed_nested_result_values_are_rejected_content_safely(self) -> None:
        class PrivateValue:
            def __repr__(self) -> str:
                return "private-nested-value-sentinel"

            def __str__(self) -> str:
                return "private-nested-value-sentinel"

        private = PrivateValue()
        rendered = RenderedLecture(
            status="rendered",
            document=make_reference(),
            renderer=make_identity(),
            tex_fragment="invented",
            metrics=make_metrics(),
            diagnostics=(),
        )
        rejected = RejectedLecture(
            status="validation_failed",
            document=make_reference(),
            diagnostics=(validation_error(),),
        )
        failure = RendererFailure(
            status="render_failed",
            document=make_reference(),
            renderer=make_identity(),
            diagnostics=(render_error(),),
        )
        attempts = (
            (
                "rendered result document must be DocumentReference",
                lambda: replace(rendered, document=private),
            ),
            (
                "rendered result renderer must be RendererIdentity",
                lambda: replace(rendered, renderer=private),
            ),
            (
                "rendered result metrics must be StructuralMetrics",
                lambda: replace(rendered, metrics=private),
            ),
            (
                "rejected result document must be DocumentReference or None",
                lambda: replace(rejected, document=private),
            ),
            (
                "renderer failure document must be DocumentReference",
                lambda: replace(failure, document=private),
            ),
            (
                "renderer failure renderer must be RendererIdentity",
                lambda: replace(failure, renderer=private),
            ),
        )
        for expected, attempt in attempts:
            with self.subTest(expected=expected):
                with self.assertRaises(ValueError) as caught:
                    attempt()
                self.assertEqual(str(caught.exception), expected)
                self.assertNotIn("private-nested-value-sentinel", str(caught.exception))

    def test_document_reference_is_unavailable_for_invalid_safe_fields(self) -> None:
        invalid = LectureDocument(
            contract_version=DOCUMENT_CONTRACT_VERSION,
            document_id=".",
            order=1,
            source_text="invented",
            provenance=SourceProvenance(content_sha256="0" * 64),
        )
        self.assertIsNone(document_reference(invalid))

    def test_renderer_identity_requires_fixed_profile_and_semver(self) -> None:
        accepted = ("1.0.0-0", "1.0.0-alpha.1", "1.0.0+01")
        rejected = (
            "version-one",
            "1.0.0-01",
            "1.0.0-alpha.01",
            "1١.0.0",
            "1.0.0-1١",
        )
        for version in accepted:
            with self.subTest(accepted=version):
                identity = replace(make_identity(), renderer_version=version)
                self.assertEqual(identity.renderer_version, version)
        for version in rejected:
            with self.subTest(rejected=version):
                with self.assertRaisesRegex(ValueError, "semantic-version"):
                    replace(make_identity(), renderer_version=version)
        with self.assertRaisesRegex(ValueError, "profile"):
            replace(make_identity(), render_profile="other-profile")

    def test_metrics_require_nonnegative_integer_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-negative integers"):
            replace(make_metrics(), input_lines=-1)
        with self.assertRaisesRegex(ValueError, "non-negative integers"):
            replace(make_metrics(), input_lines=True)


if __name__ == "__main__":
    unittest.main()
