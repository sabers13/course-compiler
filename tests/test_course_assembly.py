from __future__ import annotations

import ast
import hashlib
import unittest
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path
from unittest import mock

from course_compiler import (
    ASSEMBLY_IDENTITY,
    COURSE_TEX_ASSEMBLY_PROFILE,
    AssemblyFailure,
    AssemblyProfileIdentity,
    CourseTexAssembler,
    CourseTexAssemblySuccess,
    DocumentReference,
    RejectedLecture,
    RenderDiagnostic,
    RenderedLecture,
    RendererFailure,
    RendererIdentity,
    StructuralMetrics,
    assemble_course_tex,
)
from course_compiler import assembly


FIXTURES = Path("tests/fixtures/synthetic/course-assembly")
_ASSEMBLY_ALLOWED_ABSOLUTE_IMPORTS = {"__future__", "dataclasses", "re", "typing"}
_ASSEMBLY_ALLOWED_RELATIVE_IMPORTS = {"contracts", "rendering"}


def assembly_policy_errors(source: str) -> set[str]:
    tree = ast.parse(source)
    errors: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in _ASSEMBLY_ALLOWED_ABSOLUTE_IMPORTS:
                    errors.add("absolute import")
        elif isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" for alias in node.names):
                errors.add("wildcard import")
            if node.level:
                if node.level != 1 or node.module not in _ASSEMBLY_ALLOWED_RELATIVE_IMPORTS:
                    errors.add("relative import")
            elif node.module not in _ASSEMBLY_ALLOWED_ABSOLUTE_IMPORTS:
                errors.add("absolute import")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"__import__", "compile", "eval", "exec", "open"}:
                errors.add("forbidden call")
    return errors


def identity(version: str = "1.0.0") -> RendererIdentity:
    return RendererIdentity(
        renderer_name="synthetic-renderer",
        renderer_version=version,
        contract_version="lecture-document/v1",
        render_profile="legacy-markdown-to-tex/v1",
    )


def reference(order: int, document_id: str | None = None, digest: str | None = None) -> DocumentReference:
    return DocumentReference(
        contract_version="lecture-document/v1",
        document_id=document_id or f"invented-{order}",
        order=order,
        content_sha256=digest or hashlib.sha256(f"invented-{order}".encode()).hexdigest(),
    )


def metrics() -> StructuralMetrics:
    return StructuralMetrics(0, 0, 0, 0, 0, 0, 0, 0, 0, 0)


def rendered(
    ref: DocumentReference,
    *,
    fragment: str | None = None,
    renderer: RendererIdentity | None = None,
    warnings: tuple[RenderDiagnostic, ...] = (),
) -> RenderedLecture:
    return RenderedLecture(
        status="rendered_with_warnings" if warnings else "rendered",
        document=ref,
        renderer=renderer or identity(),
        tex_fragment=fragment if fragment is not None else f"INVENTED-{ref.order}\n",
        metrics=metrics(),
        diagnostics=warnings,
    )


def warning(line: int | None = None) -> RenderDiagnostic:
    return RenderDiagnostic("short_source", "warning", "validation", line)


class PublicShapeTests(unittest.TestCase):
    def test_profile_identity_and_public_results_are_immutable(self) -> None:
        self.assertEqual(COURSE_TEX_ASSEMBLY_PROFILE, "course-tex-assembly/v1")
        self.assertEqual(ASSEMBLY_IDENTITY, AssemblyProfileIdentity(COURSE_TEX_ASSEMBLY_PROFILE))
        self.assertEqual(
            [item.name for item in fields(CourseTexAssemblySuccess)],
            ["status", "assembly_profile", "renderer", "combined", "lectures", "warnings"],
        )
        result = assemble_course_tex((reference(1),), (rendered(reference(1)),))
        self.assertIsInstance(result, CourseTexAssemblySuccess)
        self.assertFalse(hasattr(result, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            result.status = "other"  # type: ignore[misc]

    def test_assembler_has_no_configuration_and_delegates(self) -> None:
        assembler = CourseTexAssembler()
        self.assertIs(assembler.identity, ASSEMBLY_IDENTITY)
        with self.assertRaises(TypeError):
            CourseTexAssembler("option")  # type: ignore[call-arg]
        ref = reference(1)
        self.assertEqual(assembler.assemble((ref,), (rendered(ref),)), assemble_course_tex((ref,), (rendered(ref),)))


class AssemblySuccessTests(unittest.TestCase):
    def test_one_lecture_success_is_complete_and_logical(self) -> None:
        ref = reference(1)
        result = assemble_course_tex((ref,), (rendered(ref),))
        self.assertIsInstance(result, CourseTexAssemblySuccess)
        self.assertEqual(result.combined.logical_filename, "Course_Study_Lectures.tex")
        self.assertEqual(tuple(item.logical_filename for item in result.lectures), ("L01.tex",))
        self.assertIn("\\tableofcontents\n\\newpage\n\\clearpage\n", result.combined.tex_source)
        self.assertTrue(result.combined.tex_source.endswith("\\end{document}\n"))
        self.assertTrue(result.lectures[0].tex_source.endswith("\\end{document}\n"))

    def test_two_lecture_exact_synthetic_goldens(self) -> None:
        first = reference(1)
        second = reference(2)
        result = assemble_course_tex(
            (first, second),
            (rendered(first, fragment="INVENTED-ONE\n"), rendered(second, fragment="INVENTED-TWO\n")),
        )
        self.assertIsInstance(result, CourseTexAssemblySuccess)
        expected = {
            "combined.tex": result.combined.tex_source,
            "L01.tex": result.lectures[0].tex_source,
            "L02.tex": result.lectures[1].tex_source,
        }
        for name, source in expected.items():
            with self.subTest(name=name):
                self.assertEqual(source, (FIXTURES / name).read_text(encoding="utf-8"))

    def test_permuted_inputs_have_identical_canonical_output(self) -> None:
        first, second = reference(1), reference(2)
        first_result, second_result = rendered(first), rendered(second)
        canonical = assemble_course_tex((first, second), (first_result, second_result))
        permuted = assemble_course_tex((second, first), (first_result, second_result))
        independently_permuted = assemble_course_tex((second, first), (second_result, first_result))
        self.assertEqual(canonical, permuted)
        self.assertEqual(canonical, independently_permuted)

    def test_warnings_are_preserved_in_canonical_order(self) -> None:
        first, second = reference(1), reference(2)
        one = RenderDiagnostic("unclosed_code_fence", "warning", "validation", 4)
        two = warning()
        result = assemble_course_tex(
            (second, first),
            (rendered(second, warnings=(two,)), rendered(first, warnings=(one,))),
        )
        self.assertIsInstance(result, CourseTexAssemblySuccess)
        self.assertEqual(result.warnings, (one, two))

    def test_filenames_expand_without_losing_minimum_width(self) -> None:
        references = tuple(reference(order) for order in range(1, 101))
        results = tuple(rendered(item) for item in reversed(references))
        result = assemble_course_tex(tuple(reversed(references)), results)
        self.assertIsInstance(result, CourseTexAssemblySuccess)
        names = tuple(item.logical_filename for item in result.lectures)
        self.assertEqual((names[0], names[8], names[9], names[98], names[99]), ("L01.tex", "L09.tex", "L10.tex", "L99.tex", "L100.tex"))

    def test_success_repr_redacts_every_tex_body(self) -> None:
        sentinel = "private-tex-assembly-sentinel"
        ref = reference(1)
        result = assemble_course_tex((ref,), (rendered(ref, fragment=sentinel),))
        self.assertNotIn(sentinel, repr(result))
        self.assertNotIn("tex_source", repr(result))

    def test_repeated_assembly_is_equal(self) -> None:
        ref = reference(1)
        values = ((ref,), (rendered(ref),))
        self.assertEqual(assemble_course_tex(*values), assemble_course_tex(*values))


class InvalidCollectionTests(unittest.TestCase):
    def assert_failure(self, references: object, results: object, code: str) -> AssemblyFailure:
        result = assemble_course_tex(references, results)  # type: ignore[arg-type]
        self.assertIsInstance(result, AssemblyFailure)
        self.assertEqual(result.status, "assembly_failed")
        self.assertEqual(result.diagnostics[0].code, code)
        return result

    def test_container_and_empty_failures(self) -> None:
        self.assert_failure([], (), "invalid_reference_collection")
        self.assert_failure((), [], "invalid_result_collection")
        self.assert_failure((), (), "empty_reference_collection")

    def test_invalid_nested_values_and_reference_collections_fail_closed(self) -> None:
        good = reference(1)
        cases = (
            ((object(),), (), "invalid_document_reference"),
            ((good, good), (), "duplicate_document_id"),
            ((good, reference(1, "another")), (), "duplicate_document_order"),
            ((reference(2),), (), "non_contiguous_document_orders"),
            ((good,), (object(),), "invalid_render_result"),
        )
        for references, results, code in cases:
            with self.subTest(code=code):
                self.assert_failure(references, results, code)

    def test_result_bindings_and_renderer_states_fail_closed(self) -> None:
        first, second = reference(1), reference(2)
        rejected = RejectedLecture("validation_failed", first, (RenderDiagnostic("invalid_order", "error", "validation", None),))
        failed = RendererFailure("render_failed", first, identity(), (RenderDiagnostic("renderer_exception", "error", "render", None),))
        mismatched = replace(first, content_sha256="0" * 64)
        cases = (
            ((first,), (rejected,), "rejected_lecture_result"),
            ((first,), (failed,), "renderer_failure_result"),
            ((first,), (rendered(first, renderer=identity("1.0.0")), rendered(first, renderer=identity("2.0.0"))), "mixed_renderer_identity"),
            ((first,), (), "missing_render_result"),
            ((first,), (rendered(second),), "extra_render_result"),
            ((first,), (rendered(first), rendered(first)), "duplicate_render_result_binding"),
            ((first,), (rendered(mismatched),), "reference_result_mismatch"),
        )
        for references, results, code in cases:
            with self.subTest(code=code):
                self.assert_failure(references, results, code)

    def test_failure_repr_has_no_partial_tex_or_private_values(self) -> None:
        sentinel = "private-tex-assembly-sentinel"
        reference_with_private_id = reference(1, "private-id")
        result = self.assert_failure((reference_with_private_id,), (), "missing_render_result")
        self.assertNotIn(sentinel, repr(result))
        self.assertNotIn("private-id", repr(result))
        self.assertFalse(hasattr(result, "combined"))


class ContainmentAndPurityTests(unittest.TestCase):
    def assert_invalid_missing_render(
        self, ref: DocumentReference, value: RenderedLecture, sentinel: str
    ) -> None:
        result = assemble_course_tex((ref,), (value,))
        self.assertIsInstance(result, AssemblyFailure)
        self.assertEqual(result.diagnostics[0].code, "invalid_render_result")
        self.assertEqual(result.diagnostics[0].position, 1)
        self.assertNotEqual(result.diagnostics[0].code, "assembly_exception")
        self.assertNotIn(sentinel, repr(result))
        self.assertFalse(hasattr(result, "combined"))
        self.assertFalse(hasattr(result, "lectures"))
        self.assertFalse(hasattr(result, "warnings"))

    def test_ordinary_validation_exception_is_contained_without_partial_output(self) -> None:
        ref = reference(1)
        with mock.patch.object(assembly, "_validate_collection", side_effect=RuntimeError("validation sentinel")):
            result = assemble_course_tex((ref,), (rendered(ref),))
        self.assertIsInstance(result, AssemblyFailure)
        self.assertEqual(result.diagnostics[0].code, "assembly_exception")
        self.assertNotIn("validation sentinel", repr(result))
        self.assertFalse(hasattr(result, "combined"))
        self.assertFalse(hasattr(result, "lectures"))

    def test_validation_base_exceptions_propagate(self) -> None:
        ref = reference(1)
        for exception in (KeyboardInterrupt, SystemExit):
            with self.subTest(exception=exception.__name__):
                with mock.patch.object(assembly, "_validate_collection", side_effect=exception):
                    with self.assertRaises(exception):
                        assemble_course_tex((ref,), (rendered(ref),))

    def test_ordinary_assembly_exception_is_contained_without_partial_output(self) -> None:
        ref = reference(1)
        with mock.patch.object(assembly, "_assemble_validated", side_effect=RuntimeError("private sentinel")):
            result = assemble_course_tex((ref,), (rendered(ref),))
        self.assertIsInstance(result, AssemblyFailure)
        self.assertEqual(result.diagnostics[0].code, "assembly_exception")
        self.assertNotIn("private sentinel", repr(result))
        self.assertFalse(hasattr(result, "combined"))

    def test_base_exception_propagates(self) -> None:
        ref = reference(1)
        with mock.patch.object(assembly, "_assemble_validated", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                assemble_course_tex((ref,), (rendered(ref),))

    def test_malformed_rendered_nested_values_fail_closed_before_behavior(self) -> None:
        ref = reference(1)

        class MetricsSubclass(StructuralMetrics):
            pass

        class HostileRendererIdentity(RendererIdentity):
            def __eq__(self, other: object) -> bool:
                raise RuntimeError("renderer comparison sentinel")

        class DiagnosticSubclass(RenderDiagnostic):
            pass

        metrics_subclass = MetricsSubclass(0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        hostile_renderer = HostileRendererIdentity(
            "synthetic-renderer", "1.0.0", "lecture-document/v1", "legacy-markdown-to-tex/v1"
        )
        diagnostic_subclass = DiagnosticSubclass("short_source", "warning", "validation", None)
        malformed_reference = reference(1)
        object.__setattr__(malformed_reference, "document_id", type("LoudString", (str,), {"__hash__": lambda self: (_ for _ in ()).throw(RuntimeError("hash sentinel"))})("invented-1"))
        cases = (
            replace(rendered(ref), metrics=metrics_subclass),
            replace(rendered(ref), renderer=hostile_renderer),
            replace(rendered(ref, warnings=(warning(),)), diagnostics=(diagnostic_subclass,)),
            replace(rendered(ref), document=malformed_reference),
        )
        for value in cases:
            with self.subTest(value_type=type(value.metrics).__name__):
                result = assemble_course_tex((ref,), (value,))
                self.assertIsInstance(result, AssemblyFailure)
                self.assertEqual(result.diagnostics[0].code, "invalid_render_result")
                self.assertNotIn("sentinel", repr(result))
                self.assertFalse(hasattr(result, "combined"))

    def test_forged_exact_nested_values_revalidate_registered_invariants(self) -> None:
        ref = reference(1)
        sentinel = "invented forged sentinel"

        invalid_renderer = identity()
        object.__setattr__(invalid_renderer, "renderer_name", sentinel)

        invalid_code = warning()
        object.__setattr__(invalid_code, "code", sentinel)

        negative_metrics = metrics()
        object.__setattr__(negative_metrics, "input_characters", -1)

        mismatched_severity_result = rendered(ref, warnings=(warning(),))
        object.__setattr__(mismatched_severity_result.diagnostics[0], "severity", "error")
        mismatched_stage_result = rendered(ref, warnings=(warning(),))
        object.__setattr__(mismatched_stage_result.diagnostics[0], "stage", "render")
        invalid_line = warning()
        object.__setattr__(invalid_line, "line", 0)

        invalid_fragments = rendered(ref)
        object.__setattr__(invalid_fragments, "tex_fragment", "\ud800")

        cases: list[tuple[str, RenderedLecture]] = [
            ("renderer", replace(rendered(ref), renderer=invalid_renderer)),
            ("diagnostic-code", replace(rendered(ref, warnings=(warning(),)), diagnostics=(invalid_code,))),
            ("metrics", replace(rendered(ref), metrics=negative_metrics)),
            ("diagnostic-severity", mismatched_severity_result),
            ("diagnostic-stage", mismatched_stage_result),
            ("diagnostic-line", replace(rendered(ref, warnings=(warning(),)), diagnostics=(invalid_line,))),
            ("fragment", invalid_fragments),
        ]
        for field_name, value in cases:
            with self.subTest(field_name=field_name):
                result = assemble_course_tex((ref,), (value,))
                self.assertIsInstance(result, AssemblyFailure)
                self.assertEqual(result.diagnostics[0].code, "invalid_render_result")
                self.assertNotIn(sentinel, repr(result))
                self.assertFalse(hasattr(result, "combined"))
                self.assertFalse(hasattr(result, "lectures"))
                self.assertFalse(hasattr(result, "warnings"))

    def test_forged_exact_document_references_fail_closed_by_position(self) -> None:
        ref = reference(1)
        changes = (
            ("document_id", "."),
            ("content_sha256", "not-a-digest"),
            ("order", 0),
            ("contract_version", "lecture-document/v2"),
        )
        for field_name, value in changes:
            with self.subTest(field_name=field_name):
                nested = reference(1)
                object.__setattr__(nested, field_name, value)
                result = assemble_course_tex((ref,), (replace(rendered(ref), document=nested),))
                self.assertIsInstance(result, AssemblyFailure)
                self.assertEqual(result.diagnostics[0].code, "invalid_render_result")
                self.assertEqual(result.diagnostics[0].position, 1)
                self.assertNotIn(str(value), repr(result))
                self.assertFalse(hasattr(result, "combined"))

    def test_forged_exact_top_level_reference_is_invalid_reference(self) -> None:
        malformed = reference(1)
        object.__setattr__(malformed, "document_id", ".")
        result = assemble_course_tex((malformed,), (rendered(malformed),))
        self.assertIsInstance(result, AssemblyFailure)
        self.assertEqual(result.diagnostics[0].code, "invalid_document_reference")
        self.assertEqual(result.diagnostics[0].position, 1)
        self.assertNotIn(".", repr(result))
        self.assertFalse(hasattr(result, "combined"))

    def test_missing_top_level_reference_slots_are_invalid_reference(self) -> None:
        uninitialized = object.__new__(DocumentReference)
        values = [("uninitialized", uninitialized)]
        for field_name in ("contract_version", "document_id", "order", "content_sha256"):
            malformed = reference(1, document_id="invented-missing-field-sentinel")
            object.__delattr__(malformed, field_name)
            values.append((field_name, malformed))
        for field_name, malformed in values:
            with self.subTest(field_name=field_name):
                result = assemble_course_tex((malformed,), ())
                self.assertIsInstance(result, AssemblyFailure)
                self.assertEqual(result.diagnostics[0].code, "invalid_document_reference")
                self.assertEqual(result.diagnostics[0].position, 1)
                self.assertNotEqual(result.diagnostics[0].code, "assembly_exception")
                self.assertNotIn("invented-missing-field-sentinel", repr(result))
                self.assertFalse(hasattr(result, "combined"))

    def test_missing_nested_reference_slots_are_invalid_render_result(self) -> None:
        ref = reference(1)
        sentinel = "invented missing nested reference sentinel"
        values = [("uninitialized", object.__new__(DocumentReference))]
        for field_name in ("contract_version", "document_id", "order", "content_sha256"):
            malformed = reference(1)
            object.__delattr__(malformed, field_name)
            values.append((field_name, malformed))
        for field_name, malformed in values:
            with self.subTest(field_name=field_name):
                value = rendered(ref, fragment=sentinel)
                object.__setattr__(value, "document", malformed)
                self.assert_invalid_missing_render(ref, value, sentinel)

    def test_missing_renderer_and_metrics_slots_are_invalid_render_result(self) -> None:
        ref = reference(1)
        sentinel = "invented missing renderer metrics sentinel"
        renderer_fields = (
            "renderer_name",
            "renderer_version",
            "contract_version",
            "render_profile",
        )
        renderer_values = [("uninitialized", object.__new__(RendererIdentity))]
        for field_name in renderer_fields:
            malformed = identity()
            object.__delattr__(malformed, field_name)
            renderer_values.append((field_name, malformed))
        for field_name, malformed in renderer_values:
            with self.subTest(record="renderer", field_name=field_name):
                value = rendered(ref, fragment=sentinel)
                object.__setattr__(value, "renderer", malformed)
                self.assert_invalid_missing_render(ref, value, sentinel)

        metrics_fields = (
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
        )
        metrics_values = [("uninitialized", object.__new__(StructuralMetrics))]
        for field_name in metrics_fields:
            malformed = metrics()
            object.__delattr__(malformed, field_name)
            metrics_values.append((field_name, malformed))
        for field_name, malformed in metrics_values:
            with self.subTest(record="metrics", field_name=field_name):
                value = rendered(ref, fragment=sentinel)
                object.__setattr__(value, "metrics", malformed)
                self.assert_invalid_missing_render(ref, value, sentinel)

    def test_missing_diagnostic_slots_are_invalid_render_result(self) -> None:
        ref = reference(1)
        sentinel = "invented missing diagnostic sentinel"
        values = [("uninitialized", object.__new__(RenderDiagnostic))]
        for field_name in ("code", "severity", "stage", "line"):
            malformed = warning()
            object.__delattr__(malformed, field_name)
            values.append((field_name, malformed))
        for field_name, malformed in values:
            with self.subTest(field_name=field_name):
                value = rendered(ref, fragment=sentinel, warnings=(warning(),))
                object.__setattr__(value, "diagnostics", (malformed,))
                self.assert_invalid_missing_render(ref, value, sentinel)

    def test_missing_rendered_lecture_slots_are_invalid_render_result(self) -> None:
        ref = reference(1)
        sentinel = "invented missing rendered lecture sentinel"
        values = [("uninitialized", object.__new__(RenderedLecture))]
        for field_name in (
            "status",
            "document",
            "renderer",
            "tex_fragment",
            "metrics",
            "diagnostics",
        ):
            malformed = rendered(ref, fragment=sentinel)
            object.__delattr__(malformed, field_name)
            values.append((field_name, malformed))
        for field_name, malformed in values:
            with self.subTest(field_name=field_name):
                self.assert_invalid_missing_render(ref, malformed, sentinel)

    def test_exact_assembly_import_allowlist_and_forbidden_probes(self) -> None:
        source = Path("course_compiler/assembly.py").read_text(encoding="utf-8")
        self.assertEqual(assembly_policy_errors(source), set())
        probes = (
            "import time",
            "import datetime",
            "import locale",
            "import numpy",
            "import openai",
            "from .legacy_renderer import LegacyMarkdownTexRenderer",
            "from . import legacy_renderer",
            "from typing import *",
            "__import__('invented')",
            "open('invented')",
            "eval('1')",
            "exec('pass')",
            "compile('pass', 'invented', 'exec')",
        )
        for probe in probes:
            with self.subTest(probe=probe):
                self.assertTrue(assembly_policy_errors(probe))


if __name__ == "__main__":
    unittest.main()
