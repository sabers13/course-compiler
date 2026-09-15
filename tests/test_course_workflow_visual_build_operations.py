from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path
from unittest import mock

import course_compiler
import course_compiler.course_workflow_operations as operations
from course_compiler import (
    ASSET_REFERENCE_VERSION,
    COMPILATION_IDENTITY,
    VISUAL_PLACEMENT_VERSION,
    AssetReference,
    CompiledPdf,
    CompilerInputBundle,
    CoursePdfBuildFailure,
    LectureDocument,
    LocalLectureDocumentStore,
    LogicalTexFile,
    ReopenedCourseWorkflowContext,
    VisualCompositionFailure,
    VisualPlacement,
    build_reopened_course_pdf,
    build_reopened_course_pdf_with_visuals,
    document_reference,
    open_lecture_document_store,
)
from course_compiler import compilation, visual_composition
from tests.test_course_workflow_lecture_operations import (
    accepted_scenario,
    context_for,
    documents_for,
)
from tests.test_visual_composition import asset_reference_for, synthetic_png


PRIVATE_MARKER = "invented-private-visual-build-marker"
_ROOT_TEX_SOURCE = "\\documentclass{article}\n\\begin{document}\n\\end{document}\n"


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


def sentinel_bundle() -> CompilerInputBundle:
    return CompilerInputBundle(
        root_tex=LogicalTexFile(
            logical_filename="Course_Study_Lectures.tex",
            tex_source=_ROOT_TEX_SOURCE,
        ),
        companion_files=(),
    )


def placement_for(
    document: LectureDocument, asset: AssetReference, *, offset: int | None = None
) -> VisualPlacement:
    reference = document_reference(document)
    assert reference is not None
    if offset is None:
        offset = len(document.source_text)
    return VisualPlacement(VISUAL_PLACEMENT_VERSION, reference, offset, asset)


class BuildSentinel(BaseException):
    pass


class CourseWorkflowVisualBuildOperationsTests(unittest.TestCase):
    """Shared store fixture plus the bulk of the T026 behavioral coverage."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="visual-build-")
        self.store = open_lecture_document_store(
            Path(self._temporary.name) / "documents.sqlite"
        )
        assert type(self.store) is LocalLectureDocumentStore

    def tearDown(self) -> None:
        self.store.close()
        self._temporary.cleanup()

    def persisted_context(
        self, count: int = 1
    ) -> tuple[ReopenedCourseWorkflowContext, tuple[LectureDocument, ...]]:
        context = context_for(accepted_scenario(count).state)
        documents = documents_for(context)
        for document in documents:
            self.assertEqual(self.store.save(document), document_reference(document))
        return context, documents

    def placements_and_assets(
        self, document: LectureDocument
    ) -> tuple[tuple[VisualPlacement, ...], dict[AssetReference, bytes]]:
        payload = synthetic_png()
        asset = asset_reference_for(payload)
        item = placement_for(document, asset)
        return (item,), {asset: payload}

    def build(
        self,
        context: object,
        *,
        placements: tuple[VisualPlacement, ...] = (),
        asset_bytes: object = None,
    ) -> object:
        if asset_bytes is None:
            asset_bytes = {}
        return build_reopened_course_pdf_with_visuals(  # type: ignore[arg-type]
            context,
            document_store=self.store,
            placements=placements,
            asset_bytes=asset_bytes,
        )

    # -- Public shape -----------------------------------------------------

    def test_public_export_exists_with_expected_signature(self) -> None:
        self.assertIn(
            "build_reopened_course_pdf_with_visuals", course_compiler.__all__
        )
        self.assertIn(
            "build_reopened_course_pdf_with_visuals", operations.__all__
        )
        self.assertIs(
            course_compiler.build_reopened_course_pdf_with_visuals,
            operations.build_reopened_course_pdf_with_visuals,
        )
        signature = inspect.signature(operations.build_reopened_course_pdf_with_visuals)
        self.assertEqual(
            list(signature.parameters),
            ["context", "document_store", "placements", "asset_bytes"],
        )
        self.assertEqual(
            signature.parameters["context"].kind,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        for keyword_only in ("document_store", "placements", "asset_bytes"):
            self.assertEqual(
                signature.parameters[keyword_only].kind,
                inspect.Parameter.KEYWORD_ONLY,
            )

    # -- Type misuse --------------------------------------------------------

    def test_store_type_misuse_raises_before_context_validation(self) -> None:
        with self.assertRaises(TypeError) as caught:
            build_reopened_course_pdf_with_visuals(
                object(),  # type: ignore[arg-type]
                document_store=object(),  # type: ignore[arg-type]
                placements=(),
                asset_bytes={},
            )
        self.assertNotIn(PRIVATE_MARKER, str(caught.exception))

    def test_store_type_misuse_precedes_context_validation_with_valid_context(
        self,
    ) -> None:
        context, _ = self.persisted_context()
        with self.assertRaises(TypeError):
            build_reopened_course_pdf_with_visuals(
                context,
                document_store=object(),  # type: ignore[arg-type]
                placements=(),
                asset_bytes={},
            )

    def test_placements_and_asset_bytes_type_misuse_raise_typeerror_before_reopen(
        self,
    ) -> None:
        context, _ = self.persisted_context()
        with mock.patch.object(
            operations, "reopen_accepted_lecture_documents"
        ) as reopen:
            with self.assertRaises(TypeError):
                self.build(context, placements=[], asset_bytes={})  # type: ignore[arg-type]
            with self.assertRaises(TypeError):
                self.build(context, placements=(), asset_bytes=[])  # type: ignore[arg-type]
        reopen.assert_not_called()

    def test_invalid_or_forged_context_fails_before_reopen(self) -> None:
        context, _ = self.persisted_context()
        forged = object.__new__(type(context))
        object.__setattr__(forged, "association", context.association)
        for value in (object(), forged):
            with self.subTest(value=type(value).__name__), mock.patch.object(
                operations, "reopen_accepted_lecture_documents"
            ) as reopen:
                result = self.build(value)  # type: ignore[arg-type]
            self.assertEqual(failure_code(result), "invalid_course_pdf_build_input")
            reopen.assert_not_called()

    # -- Non-completed workflow ---------------------------------------------

    def test_noncompleted_workflows_short_circuit_before_any_build_work(self) -> None:
        contexts = (
            context_for(accepted_scenario(2, 0).state),
            context_for(accepted_scenario(3, 2).state),
        )
        for context in contexts:
            with self.subTest(stage=context.workflow_state.stage), mock.patch.object(
                operations, "reopen_accepted_lecture_documents"
            ) as reopen, mock.patch.object(
                operations, "compose_course_compiler_input"
            ) as compose_call, mock.patch.object(
                operations, "compile_pdf_bundle"
            ) as compile_call, mock.patch.object(
                LocalLectureDocumentStore, "load"
            ) as load:
                result = self.build(context)
            self.assertEqual(failure_code(result), "workflow_not_completed")
            reopen.assert_not_called()
            compose_call.assert_not_called()
            compile_call.assert_not_called()
            load.assert_not_called()

    # -- T016 reuse -----------------------------------------------------

    def test_t016_documents_forwarded_exactly_and_composition_called_once(
        self,
    ) -> None:
        context, documents = self.persisted_context(2)
        placements, asset_bytes = self.placements_and_assets(documents[0])
        captured: list[tuple[object, object, object]] = []

        def compose(documents_arg: object, *, placements: object, asset_bytes: object) -> object:
            captured.append((documents_arg, placements, asset_bytes))
            return visual_composition._failure("empty_document_collection")

        with mock.patch.object(
            operations, "compose_course_compiler_input", side_effect=compose
        ) as compose_call, mock.patch.object(
            operations, "compile_pdf_bundle"
        ) as compile_call:
            result = self.build(context, placements=placements, asset_bytes=asset_bytes)

        compose_call.assert_called_once()
        self.assertEqual(len(captured), 1)
        documents_arg, placements_arg, asset_bytes_arg = captured[0]
        self.assertEqual(documents_arg, documents)
        self.assertIs(placements_arg, placements)
        self.assertIs(asset_bytes_arg, asset_bytes)
        self.assertIsInstance(result, VisualCompositionFailure)
        compile_call.assert_not_called()

    def test_t016_failures_map_and_composition_is_not_called(self) -> None:
        context, _ = self.persisted_context()
        mappings = {
            code: code for code in operations._BUILD_FAILURE_CODES
        }
        mappings["invalid_accepted_document_reopen_input"] = (
            "invalid_course_pdf_build_input"
        )
        for lower_code, expected in mappings.items():
            with self.subTest(code=lower_code), mock.patch.object(
                operations,
                "reopen_accepted_lecture_documents",
                return_value=operations._accepted_document_failure(lower_code),
            ), mock.patch.object(
                operations, "compose_course_compiler_input"
            ) as compose_call, mock.patch.object(
                operations, "compile_pdf_bundle"
            ) as compile_call:
                result = self.build(context)
            self.assertEqual(failure_code(result), expected)
            compose_call.assert_not_called()
            compile_call.assert_not_called()

    def test_impossible_or_malformed_t016_return_is_consistency_failure(self) -> None:
        context, _ = self.persisted_context()
        for bad_value in ((), object(), "not-a-tuple"):
            with self.subTest(value=type(bad_value).__name__), mock.patch.object(
                operations, "reopen_accepted_lecture_documents", return_value=bad_value
            ), mock.patch.object(
                operations, "compose_course_compiler_input"
            ) as compose_call:
                result = self.build(context)
            self.assertEqual(failure_code(result), "inconsistent_reopened_documents")
            compose_call.assert_not_called()

    # -- T025 composition ------------------------------------------------

    def test_visual_composition_failure_is_returned_unchanged(self) -> None:
        context, _ = self.persisted_context()
        failure = visual_composition._failure("placement_offset_out_of_range", 1)
        with mock.patch.object(
            operations, "compose_course_compiler_input", return_value=failure
        ), mock.patch.object(operations, "compile_pdf_bundle") as compile_call:
            result = self.build(context)
        self.assertIs(result, failure)
        compile_call.assert_not_called()

    def test_unexpected_composition_return_is_generic_build_exception(self) -> None:
        context, _ = self.persisted_context()
        with mock.patch.object(
            operations, "compose_course_compiler_input", return_value=object()
        ), mock.patch.object(operations, "compile_pdf_bundle") as compile_call:
            result = self.build(context)
        self.assertEqual(failure_code(result), "course_pdf_build_exception")
        compile_call.assert_not_called()

    # -- T024 compilation --------------------------------------------------

    def test_success_forwards_exact_bundle_and_returns_pdf_unchanged(self) -> None:
        context, documents = self.persisted_context()
        placements, asset_bytes = self.placements_and_assets(documents[0])
        bundle = sentinel_bundle()
        expected = compiled_pdf()
        with mock.patch.object(
            operations, "compose_course_compiler_input", return_value=bundle
        ) as compose_call, mock.patch.object(
            operations, "compile_pdf_bundle", return_value=expected
        ) as compile_call:
            result = self.build(context, placements=placements, asset_bytes=asset_bytes)
        compose_call.assert_called_once()
        compile_call.assert_called_once_with(bundle)
        self.assertIs(result, expected)
        self.assertEqual(
            [item.name for item in fields(result)],  # type: ignore[arg-type]
            ["status", "compilation_profile", "logical_filename", "pdf_content"],
        )

    def test_unexpected_compiler_return_is_generic_build_exception(self) -> None:
        context, documents = self.persisted_context()
        placements, asset_bytes = self.placements_and_assets(documents[0])
        with mock.patch.object(
            operations, "compose_course_compiler_input", return_value=sentinel_bundle()
        ), mock.patch.object(
            operations, "compile_pdf_bundle", return_value=object()
        ):
            result = self.build(context, placements=placements, asset_bytes=asset_bytes)
        self.assertEqual(failure_code(result), "course_pdf_build_exception")

    def test_all_compiler_failures_preserve_category_without_intermediate_output(
        self,
    ) -> None:
        context, documents = self.persisted_context()
        placements, asset_bytes = self.placements_and_assets(documents[0])
        for code in operations._COMPILATION_BUILD_FAILURE_CODES:
            with self.subTest(code=code), mock.patch.object(
                operations, "compose_course_compiler_input", return_value=sentinel_bundle()
            ), mock.patch.object(
                operations, "compile_pdf_bundle", return_value=compilation._failure(code)
            ) as compile_call:
                result = self.build(context, placements=placements, asset_bytes=asset_bytes)
            self.assertEqual(failure_code(result), code)
            compile_call.assert_called_once()
            for excluded in ("combined", "lectures", "tex_source", "pdf_content"):
                self.assertFalse(hasattr(result, excluded))

    # -- Exception containment ----------------------------------------------

    def test_ordinary_exceptions_are_redacted(self) -> None:
        context, documents = self.persisted_context()
        placements, asset_bytes = self.placements_and_assets(documents[0])
        for operation_name in (
            "reopen_accepted_lecture_documents",
            "compose_course_compiler_input",
            "compile_pdf_bundle",
        ):
            with self.subTest(operation=operation_name), mock.patch.object(
                operations, operation_name, side_effect=RuntimeError(PRIVATE_MARKER)
            ):
                result = self.build(context, placements=placements, asset_bytes=asset_bytes)
            self.assertEqual(failure_code(result), "course_pdf_build_exception")
            self.assertNotIn(PRIVATE_MARKER, repr(result))

    def test_base_exception_propagates(self) -> None:
        context, documents = self.persisted_context()
        placements, asset_bytes = self.placements_and_assets(documents[0])
        with mock.patch.object(
            operations, "reopen_accepted_lecture_documents", side_effect=BuildSentinel
        ):
            with self.assertRaises(BuildSentinel):
                self.build(context, placements=placements, asset_bytes=asset_bytes)

    def test_failure_diagnostics_do_not_leak_private_details(self) -> None:
        context, documents = self.persisted_context()
        placements, asset_bytes = self.placements_and_assets(documents[0])
        markers = (
            PRIVATE_MARKER,
            "invented-private-source",
            r"\\private{tex}",
            "/tmp/invented-private-path",
        )
        with mock.patch.object(
            operations, "compose_course_compiler_input", return_value=sentinel_bundle()
        ), mock.patch.object(
            operations, "compile_pdf_bundle", side_effect=RuntimeError(" ".join(markers))
        ):
            result = self.build(context, placements=placements, asset_bytes=asset_bytes)
        public = repr(result) + str(result)
        self.assertEqual(failure_code(result), "course_pdf_build_exception")
        for marker in markers:
            self.assertNotIn(marker, public)

    # -- Caller-owned lifecycle ----------------------------------------------

    def test_store_and_context_remain_caller_owned_unchanged_and_usable(self) -> None:
        context, documents = self.persisted_context()
        placements, asset_bytes = self.placements_and_assets(documents[0])
        context_snapshot = repr(context)
        reference = document_reference(documents[0])
        with mock.patch.object(
            LocalLectureDocumentStore, "save", wraps=self.store.save
        ) as save, mock.patch.object(
            LocalLectureDocumentStore, "close", wraps=self.store.close
        ) as close, mock.patch.object(
            operations, "compose_course_compiler_input", return_value=sentinel_bundle()
        ), mock.patch.object(
            operations, "compile_pdf_bundle", return_value=compiled_pdf()
        ):
            result = self.build(context, placements=placements, asset_bytes=asset_bytes)
        self.assertIsInstance(result, CompiledPdf)
        save.assert_not_called()
        close.assert_not_called()
        self.assertEqual(repr(context), context_snapshot)
        self.assertEqual(self.store.load(reference), documents[0])

    # -- T017 regression -------------------------------------------------

    def test_t017_operation_remains_unchanged_and_unaffected(self) -> None:
        context, _ = self.persisted_context()
        expected = compiled_pdf()
        with mock.patch.object(operations, "compile_pdf", return_value=expected):
            result = build_reopened_course_pdf(context, document_store=self.store)
        self.assertIs(result, expected)
        self.assertEqual(
            [item.name for item in fields(result)],  # type: ignore[arg-type]
            ["status", "compilation_profile", "logical_filename", "pdf_content"],
        )


class DependencyBoundaryTests(unittest.TestCase):
    def test_new_operation_owns_no_filesystem_subprocess_or_persistence(self) -> None:
        source_path = Path(operations.__file__)
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "build_reopened_course_pdf_with_visuals"
        )
        called_attributes = {
            node.func.attr
            for node in ast.walk(function)
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
        called_names = {
            node.func.id
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertTrue(
            {
                "reopen_accepted_lecture_documents",
                "compose_course_compiler_input",
                "compile_pdf_bundle",
            }
            <= called_names
        )
        self.assertNotIn("reopen_course_workflow_context", called_names)
        full_source = source_path.read_text(encoding="utf-8")
        self.assertNotIn("subprocess", full_source)
        self.assertNotIn("tempfile", full_source)

    def test_module_dependency_boundary_includes_only_approved_visual_modules(
        self,
    ) -> None:
        source_path = Path(operations.__file__)
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level == 1
        }
        self.assertTrue({"visual_composition", "visual_placement", "asset"} <= imports)


class SyntheticRealToolchainTests(unittest.TestCase):
    """One real synthetic compile through the new operation (test-only use)."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="real-toolchain-")
        self.store = open_lecture_document_store(
            Path(self._temporary.name) / "documents.sqlite"
        )
        assert type(self.store) is LocalLectureDocumentStore

    def tearDown(self) -> None:
        self.store.close()
        self._temporary.cleanup()

    def test_completed_workflow_with_one_visual_compiles_through_new_operation(
        self,
    ) -> None:
        context = context_for(accepted_scenario(1).state)
        documents = documents_for(context)
        for document in documents:
            self.store.save(document)
        payload = synthetic_png()
        asset = asset_reference_for(payload)
        item = placement_for(documents[0], asset)

        result = build_reopened_course_pdf_with_visuals(
            context,
            document_store=self.store,
            placements=(item,),
            asset_bytes={asset: payload},
        )

        self.assertIsInstance(result, CompiledPdf)
        assert isinstance(result, CompiledPdf)
        self.assertTrue(result.pdf_content.startswith(b"%PDF-"))


if __name__ == "__main__":
    unittest.main()
