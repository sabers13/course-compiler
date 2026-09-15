from __future__ import annotations

import ast
import tempfile
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from unittest import mock

import course_compiler
import course_compiler.course_workflow_operations as operations
from course_compiler import (
    COMPILATION_IDENTITY,
    AssemblyFailure,
    CompiledPdf,
    CoursePdfBuildDiagnostic,
    CoursePdfBuildFailure,
    LocalLectureDocumentStore,
    RejectedLecture,
    RenderDiagnostic,
    RendererFailure,
    build_reopened_course_pdf,
    document_reference,
    open_lecture_document_store,
)
from course_compiler import assembly, compilation
from course_compiler.legacy_renderer import LegacyMarkdownTexRenderer
from course_compiler.workflow import ordered_accepted_documents
from tests.test_course_workflow_lecture_operations import (
    accepted_scenario,
    context_for,
    documents_for,
)


PRIVATE_MARKER = "invented-private-build-marker"


def failure_code(result: object) -> str:
    if type(result) is not CoursePdfBuildFailure:
        raise AssertionError("course PDF build failure required")
    return result.diagnostics[0].code


def compiled_pdf() -> CompiledPdf:
    return CompiledPdf(
        "compiled",
        COMPILATION_IDENTITY,
        "Course_Study_Lectures.pdf",
        b"%PDF-invented-ephemeral",
    )


class BuildSentinel(BaseException):
    pass


class CourseWorkflowBuildOperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="build-")
        self.store = open_lecture_document_store(
            Path(self._temporary.name) / "documents.sqlite"
        )
        assert type(self.store) is LocalLectureDocumentStore

    def tearDown(self) -> None:
        self.store.close()
        self._temporary.cleanup()

    def persisted_context(self, count: int = 1) -> tuple[object, tuple[object, ...]]:
        context = context_for(accepted_scenario(count).state)
        documents = documents_for(context)
        for document in documents:
            self.assertEqual(self.store.save(document), document_reference(document))
        return context, documents

    def build_with_stubbed_compiler(self, context: object) -> object:
        with mock.patch.object(operations, "compile_pdf", return_value=compiled_pdf()):
            return build_reopened_course_pdf(context, document_store=self.store)  # type: ignore[arg-type]

    def test_public_exports_and_failure_contract_are_fixed_immutable(self) -> None:
        self.assertEqual(
            [item.name for item in fields(CoursePdfBuildDiagnostic)],
            ["code", "classification", "message"],
        )
        self.assertEqual(
            [item.name for item in fields(CoursePdfBuildFailure)],
            ["status", "diagnostics"],
        )
        self.assertTrue(
            {
                "CoursePdfBuildDiagnostic",
                "CoursePdfBuildFailure",
                "CoursePdfBuildResult",
                "build_reopened_course_pdf",
            }
            <= set(course_compiler.__all__)
        )
        self.assertIn("build_reopened_course_pdf", operations.__all__)
        failure = operations._course_pdf_build_failure("workflow_not_completed")
        self.assertFalse(hasattr(failure, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            failure.status = "other"  # type: ignore[misc]
        with self.assertRaises(ValueError):
            CoursePdfBuildDiagnostic("other", "input", "other")  # type: ignore[arg-type]
        forged = CoursePdfBuildDiagnostic(
            "workflow_not_completed",
            "workflow",
            "The workflow is not completed and cannot be built.",
        )
        object.__setattr__(forged, "message", PRIVATE_MARKER)
        with self.assertRaises(ValueError):
            CoursePdfBuildFailure("course_pdf_build_failed", (forged,))

    def test_completed_one_document_returns_existing_ephemeral_compiled_pdf(self) -> None:
        context, _ = self.persisted_context()
        expected = compiled_pdf()
        with mock.patch.object(operations, "compile_pdf", return_value=expected):
            result = build_reopened_course_pdf(context, document_store=self.store)
        self.assertIs(result, expected)
        self.assertEqual(
            [item.name for item in fields(result)],  # type: ignore[arg-type]
            ["status", "compilation_profile", "logical_filename", "pdf_content"],
        )
        for excluded in ("build_id", "attempt_id", "history", "publication_state"):
            self.assertFalse(hasattr(result, excluded))

    def test_completed_multi_document_preserves_order_and_compiles_combined_once(self) -> None:
        context, documents = self.persisted_context(3)
        original_render = LegacyMarkdownTexRenderer().render
        render_order: list[str] = []

        def render(document: object) -> object:
            render_order.append(document.document_id)  # type: ignore[union-attr]
            return original_render(document)  # type: ignore[arg-type]

        original_assemble = assembly.assemble_course_tex
        assembly_calls: list[tuple[tuple[object, ...], tuple[object, ...]]] = []
        assembly_results: list[object] = []

        def assemble(references: tuple[object, ...], results: tuple[object, ...]) -> object:
            assembly_calls.append((references, results))
            assembled = original_assemble(references, results)  # type: ignore[arg-type]
            assembly_results.append(assembled)
            return assembled

        expected = compiled_pdf()
        with mock.patch.object(
            operations,
            "LegacyMarkdownTexRenderer",
            return_value=mock.Mock(render=render),
        ), mock.patch.object(
            operations, "assemble_course_tex", side_effect=assemble
        ), mock.patch.object(
            operations, "compile_pdf", return_value=expected
        ) as compile_call:
            result = build_reopened_course_pdf(context, document_store=self.store)

        self.assertIs(result, expected)
        self.assertEqual(render_order, [document.document_id for document in documents])
        self.assertEqual(len(assembly_calls), 1)
        references, rendered = assembly_calls[0]
        self.assertEqual(
            references,
            ordered_accepted_documents(context.workflow_state),
        )
        self.assertEqual(tuple(item.document for item in rendered), references)
        compile_call.assert_called_once()
        assembled = assembly_results[0]
        self.assertNotIsInstance(assembled, AssemblyFailure)
        self.assertIs(compile_call.call_args.args[0], assembled.combined)  # type: ignore[union-attr]
        self.assertNotIn(compile_call.call_args.args[0], assembled.lectures)  # type: ignore[union-attr]

    def test_noncompleted_zero_and_partial_workflows_fail_before_all_build_work(self) -> None:
        contexts = (
            context_for(accepted_scenario(2, 0).state),
            context_for(accepted_scenario(3, 2).state),
        )
        for context in contexts:
            with self.subTest(stage=context.workflow_state.stage), mock.patch.object(
                operations, "reopen_accepted_lecture_documents"
            ) as reopen, mock.patch.object(
                operations, "LegacyMarkdownTexRenderer"
            ) as renderer, mock.patch.object(
                operations, "assemble_course_tex"
            ) as assemble_call, mock.patch.object(
                operations, "compile_pdf"
            ) as compile_call, mock.patch.object(
                LocalLectureDocumentStore, "load"
            ) as load:
                result = build_reopened_course_pdf(context, document_store=self.store)
            self.assertEqual(failure_code(result), "workflow_not_completed")
            reopen.assert_not_called()
            renderer.assert_not_called()
            assemble_call.assert_not_called()
            compile_call.assert_not_called()
            load.assert_not_called()

    def test_store_type_misuse_precedes_context_validation(self) -> None:
        with self.assertRaises(TypeError) as caught:
            build_reopened_course_pdf(object(), document_store=object())  # type: ignore[arg-type]
        self.assertNotIn(PRIVATE_MARKER, str(caught.exception))

    def test_invalid_or_forged_context_fails_before_reopen(self) -> None:
        context, _ = self.persisted_context()
        forged = object.__new__(type(context))
        object.__setattr__(forged, "association", context.association)
        for value in (object(), forged):
            with self.subTest(value=type(value).__name__), mock.patch.object(
                operations, "reopen_accepted_lecture_documents"
            ) as reopen:
                result = build_reopened_course_pdf(value, document_store=self.store)  # type: ignore[arg-type]
            self.assertEqual(failure_code(result), "invalid_course_pdf_build_input")
            reopen.assert_not_called()

    def test_t016_failures_map_without_later_work(self) -> None:
        context, _ = self.persisted_context()
        mappings = {
            "invalid_accepted_document_reopen_input": "invalid_course_pdf_build_input",
            "lecture_document_not_found": "lecture_document_not_found",
            "lecture_document_store_failed": "lecture_document_store_failed",
            "inconsistent_reopened_documents": "inconsistent_reopened_documents",
            "accepted_document_reopen_exception": "accepted_document_reopen_exception",
        }
        for lower_code, expected in mappings.items():
            with self.subTest(code=lower_code), mock.patch.object(
                operations,
                "reopen_accepted_lecture_documents",
                return_value=operations._accepted_document_failure(lower_code),
            ), mock.patch.object(
                operations, "LegacyMarkdownTexRenderer"
            ) as renderer, mock.patch.object(
                operations, "assemble_course_tex"
            ) as assemble_call, mock.patch.object(
                operations, "compile_pdf"
            ) as compile_call:
                result = build_reopened_course_pdf(context, document_store=self.store)
            self.assertEqual(failure_code(result), expected)
            renderer.assert_not_called()
            assemble_call.assert_not_called()
            compile_call.assert_not_called()

    def test_t016_ordinary_store_exception_preserves_reopen_category(self) -> None:
        context, _ = self.persisted_context()
        with mock.patch.object(
            LocalLectureDocumentStore,
            "load",
            side_effect=RuntimeError(PRIVATE_MARKER),
        ):
            result = build_reopened_course_pdf(context, document_store=self.store)
        self.assertEqual(failure_code(result), "accepted_document_reopen_exception")
        self.assertNotIn(PRIVATE_MARKER, repr(result))

    def test_completed_impossible_empty_reopen_is_consistency_failure(self) -> None:
        context, _ = self.persisted_context()
        with mock.patch.object(
            operations, "reopen_accepted_lecture_documents", return_value=()
        ), mock.patch.object(operations, "LegacyMarkdownTexRenderer") as renderer:
            result = build_reopened_course_pdf(context, document_store=self.store)
        self.assertEqual(failure_code(result), "inconsistent_reopened_documents")
        renderer.assert_not_called()

    def test_rejected_and_renderer_failures_are_distinct_and_fail_fast(self) -> None:
        context, documents = self.persisted_context(2)
        reference = document_reference(documents[0])
        assert reference is not None
        rejected = RejectedLecture(
            "validation_failed",
            reference,
            (RenderDiagnostic("invalid_document_id", "error", "validation", None),),
        )
        renderer_failures = (
            RendererFailure(
                "render_failed",
                reference,
                LegacyMarkdownTexRenderer.identity,
                (RenderDiagnostic(code, "error", "render", None),),
            )
            for code in ("renderer_exception", "structural_postcondition_mismatch")
        )
        cases = (
            (rejected, "accepted_document_render_rejected"),
            *((failure, failure.diagnostics[0].code) for failure in renderer_failures),
        )
        for render_result, expected in cases:
            render = mock.Mock(return_value=render_result)
            with self.subTest(code=expected), mock.patch.object(
                operations,
                "LegacyMarkdownTexRenderer",
                return_value=mock.Mock(render=render),
            ), mock.patch.object(
                operations, "assemble_course_tex"
            ) as assemble_call, mock.patch.object(
                operations, "compile_pdf"
            ) as compile_call:
                result = build_reopened_course_pdf(context, document_store=self.store)
            self.assertEqual(failure_code(result), expected)
            render.assert_called_once_with(documents[0])
            assemble_call.assert_not_called()
            compile_call.assert_not_called()
            self.assertFalse(hasattr(result, "rendered_lectures"))

    def test_assembly_failure_has_no_intermediate_output_or_compilation(self) -> None:
        context, _ = self.persisted_context(2)
        lower = assembly._failure("assembly_exception")
        with mock.patch.object(
            operations, "assemble_course_tex", return_value=lower
        ) as assemble_call, mock.patch.object(operations, "compile_pdf") as compile_call:
            result = build_reopened_course_pdf(context, document_store=self.store)
        self.assertEqual(failure_code(result), "course_tex_assembly_failed")
        assemble_call.assert_called_once()
        compile_call.assert_not_called()
        for excluded in ("combined", "lectures", "warnings", "tex_source"):
            self.assertFalse(hasattr(result, excluded))

    def test_all_compiler_failures_preserve_category_without_intermediate_output(self) -> None:
        context, _ = self.persisted_context()
        codes = (
            "invalid_compilation_input",
            "invalid_build_workspace",
            "compiler_unavailable",
            "compiler_timeout",
            "compiler_nonzero_exit",
            "expected_pdf_missing",
            "expected_pdf_unreadable",
            "compilation_exception",
        )
        for code in codes:
            with self.subTest(code=code), mock.patch.object(
                operations, "compile_pdf", return_value=compilation._failure(code)
            ) as compile_call:
                result = build_reopened_course_pdf(context, document_store=self.store)
            self.assertEqual(failure_code(result), code)
            compile_call.assert_called_once()
            for excluded in ("combined", "lectures", "tex_source", "pdf_content"):
                self.assertFalse(hasattr(result, excluded))

    def test_ordinary_exceptions_are_redacted_and_base_exceptions_propagate(self) -> None:
        context, _ = self.persisted_context()
        for operation_name in (
            "reopen_accepted_lecture_documents",
            "assemble_course_tex",
            "compile_pdf",
        ):
            with self.subTest(operation=operation_name), mock.patch.object(
                operations, operation_name, side_effect=RuntimeError(PRIVATE_MARKER)
            ):
                result = build_reopened_course_pdf(context, document_store=self.store)
            self.assertEqual(failure_code(result), "course_pdf_build_exception")
            self.assertNotIn(PRIVATE_MARKER, repr(result))
        with mock.patch.object(
            operations,
            "LegacyMarkdownTexRenderer",
            return_value=mock.Mock(
                render=mock.Mock(side_effect=RuntimeError(PRIVATE_MARKER))
            ),
        ):
            result = build_reopened_course_pdf(context, document_store=self.store)
        self.assertEqual(failure_code(result), "course_pdf_build_exception")
        self.assertNotIn(PRIVATE_MARKER, repr(result))
        with mock.patch.object(
            operations, "reopen_accepted_lecture_documents", side_effect=BuildSentinel
        ):
            with self.assertRaises(BuildSentinel):
                build_reopened_course_pdf(context, document_store=self.store)

    def test_store_and_context_remain_caller_owned_unchanged_and_usable(self) -> None:
        context, documents = self.persisted_context()
        context_snapshot = repr(context)
        reference = ordered_accepted_documents(context.workflow_state)[0]
        with mock.patch.object(
            LocalLectureDocumentStore, "save", wraps=self.store.save
        ) as save, mock.patch.object(
            LocalLectureDocumentStore, "close", wraps=self.store.close
        ) as close, mock.patch.object(
            operations, "compile_pdf", return_value=compiled_pdf()
        ):
            result = build_reopened_course_pdf(context, document_store=self.store)
        self.assertIsInstance(result, CompiledPdf)
        save.assert_not_called()
        close.assert_not_called()
        self.assertEqual(repr(context), context_snapshot)
        self.assertEqual(self.store.load(reference), documents[0])

    def test_policy_payloads_are_not_interpreted_or_forwarded(self) -> None:
        context, documents = self.persisted_context()
        observed: list[object] = []

        def reopen(value: object, *, document_store: object) -> object:
            observed.append(value)
            self.assertIs(document_store, self.store)
            return documents

        with mock.patch.object(
            operations, "reopen_accepted_lecture_documents", side_effect=reopen
        ), mock.patch.object(
            operations, "compile_pdf", return_value=compiled_pdf()
        ):
            result = build_reopened_course_pdf(context, document_store=self.store)
        self.assertIsInstance(result, CompiledPdf)
        self.assertEqual(observed, [context])
        self.assertTrue(all(payload.payload for payload in context.policy_contents))

    def test_failure_diagnostics_do_not_leak_source_tex_logs_or_paths(self) -> None:
        context, _ = self.persisted_context()
        markers = (
            PRIVATE_MARKER,
            "invented-private-source",
            r"\\private{tex}",
            "/tmp/invented-private-path",
            "invented compiler stderr",
        )
        with mock.patch.object(
            operations, "compile_pdf", side_effect=RuntimeError(" ".join(markers))
        ):
            result = build_reopened_course_pdf(context, document_store=self.store)
        public = repr(result) + str(result)
        self.assertEqual(failure_code(result), "course_pdf_build_exception")
        for marker in markers:
            self.assertNotIn(marker, public)

    def test_application_module_owns_no_filesystem_subprocess_or_persistence(self) -> None:
        source = Path(operations.__file__)
        tree = ast.parse(source.read_text(encoding="utf-8"))
        called_attributes = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        for forbidden in (
            "close",
            "connect",
            "execute",
            "open",
            "run",
            "save",
            "write_bytes",
            "write_text",
        ):
            self.assertNotIn(forbidden, called_attributes)
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level == 1
        }
        self.assertTrue({"legacy_renderer", "assembly", "compilation"} <= imports)
        self.assertNotIn("subprocess", source.read_text(encoding="utf-8"))
        self.assertNotIn("tempfile", source.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
