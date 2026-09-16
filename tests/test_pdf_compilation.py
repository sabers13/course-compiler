from __future__ import annotations

import ast
import os
import subprocess
import tempfile
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from unittest import mock

from course_compiler import (
    COMPILATION_IDENTITY,
    COMPILER_TIMEOUT_SECONDS,
    PDF_COMPILATION_PROFILE,
    CompilationDiagnostic,
    CompilationFailure,
    CompilationProfileIdentity,
    CompiledPdf,
    CompilerCompanionFile,
    CompilerInputBundle,
    LocalPdfCompiler,
    LogicalTexFile,
    compile_pdf,
    compile_pdf_bundle,
)
from course_compiler import compilation


from tests.toolchain_support import TEX_MISSING_REASON, TEX_TOOLCHAIN_AVAILABLE


_SYNTHETIC_TEX = r"""\documentclass{article}
\begin{document}
Invented compiler-boundary material.
\end{document}
"""

_SYNTHETIC_INPUT_ROOT_TEX = r"""\documentclass{article}
\begin{document}
\input{parts/invented.tex}
\end{document}
"""

_SYNTHETIC_COMPANION_TEX = b"Invented companion staging material.\n"


def logical_tex(filename: str = "Course_Study_Lectures.tex", source: str = _SYNTHETIC_TEX) -> LogicalTexFile:
    return LogicalTexFile(logical_filename=filename, tex_source=source)


def companion(filename: str, payload: bytes = b"invented") -> CompilerCompanionFile:
    return CompilerCompanionFile(logical_filename=filename, content_bytes=payload)


def bundle(
    *companions: CompilerCompanionFile,
    root: LogicalTexFile | None = None,
) -> CompilerInputBundle:
    return CompilerInputBundle(
        root_tex=logical_tex() if root is None else root,
        companion_files=companions,
    )


class PublicCompilationContractTests(unittest.TestCase):
    def test_public_records_are_fixed_frozen_and_slotted(self) -> None:
        self.assertEqual(PDF_COMPILATION_PROFILE, "latexmk-xelatex-pdf/v1")
        self.assertEqual(COMPILER_TIMEOUT_SECONDS, 120)
        self.assertEqual(
            COMPILATION_IDENTITY,
            CompilationProfileIdentity(PDF_COMPILATION_PROFILE),
        )
        self.assertEqual(
            [item.name for item in fields(CompiledPdf)],
            ["status", "compilation_profile", "logical_filename", "pdf_content"],
        )
        self.assertEqual(
            [item.name for item in fields(CompilationDiagnostic)],
            ["code", "classification", "message"],
        )
        result = CompiledPdf("compiled", COMPILATION_IDENTITY, "L01.pdf", b"invented")
        self.assertFalse(hasattr(result, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            result.status = "other"  # type: ignore[misc]

    def test_compiler_has_no_caller_configuration_and_delegates(self) -> None:
        compiler = LocalPdfCompiler()
        self.assertIs(compiler.identity, COMPILATION_IDENTITY)
        with self.assertRaises(TypeError):
            LocalPdfCompiler("option")  # type: ignore[call-arg]
        expected = CompilationFailure(
            "compilation_failed",
            (
                CompilationDiagnostic(
                    "compiler_nonzero_exit",
                    "compiler",
                    "The fixed local TeX compiler reported a failure.",
                ),
            ),
        )
        with mock.patch.object(compilation.subprocess, "run", return_value=subprocess.CompletedProcess((), 1)):
            self.assertEqual(compiler.compile(logical_tex()), expected)

    def test_diagnostics_reject_unregistered_or_dynamic_fields(self) -> None:
        with self.assertRaises(ValueError):
            CompilationDiagnostic("invented", "adapter", "invented")
        with self.assertRaises(ValueError):
            CompilationDiagnostic(
                "compiler_timeout",
                "compiler",
                "dynamic caller content",
            )
        forged_identity = CompilationProfileIdentity(PDF_COMPILATION_PROFILE)
        object.__setattr__(forged_identity, "profile", "invented-profile")
        with self.assertRaises(ValueError):
            CompiledPdf("compiled", forged_identity, "L01.pdf", b"invented")

        forged_diagnostic = CompilationDiagnostic(
            "compiler_timeout",
            "compiler",
            "The fixed local TeX compiler timed out.",
        )
        object.__setattr__(forged_diagnostic, "message", "invented-secret")
        with self.assertRaises(ValueError):
            CompilationFailure("compilation_failed", (forged_diagnostic,))


@unittest.skipUnless(
    TEX_TOOLCHAIN_AVAILABLE,
    f"real XeLaTeX compile unavailable ({TEX_MISSING_REASON})",
)
class RealCompilerIntegrationTests(unittest.TestCase):
    def _compile_and_record_workspace(self, value: LogicalTexFile) -> tuple[CompiledPdf, Path]:
        original_factory = compilation._temporary_workspace
        recorded: list[Path] = []

        class RecordingWorkspace:
            def __init__(self) -> None:
                self._inner = original_factory()

            def __enter__(self) -> str:
                name = self._inner.__enter__()
                recorded.append(Path(name))
                return name

            def __exit__(self, *args: object) -> object:
                return self._inner.__exit__(*args)

        with mock.patch.object(compilation, "_temporary_workspace", side_effect=RecordingWorkspace):
            result = compile_pdf(value)
        self.assertIsInstance(result, CompiledPdf)
        self.assertEqual(len(recorded), 1)
        return result, recorded[0]

    def test_invented_combined_logical_tex_compiles_in_memory_and_cleans(self) -> None:
        result, workspace = self._compile_and_record_workspace(logical_tex())
        self.assertEqual(result.status, "compiled")
        self.assertIs(result.compilation_profile, COMPILATION_IDENTITY)
        self.assertEqual(result.logical_filename, "Course_Study_Lectures.pdf")
        self.assertTrue(result.pdf_content)
        self.assertTrue(result.pdf_content.startswith(b"%PDF-"))
        self.assertFalse(workspace.exists())

    def test_invented_individual_logical_tex_compiles(self) -> None:
        result, workspace = self._compile_and_record_workspace(logical_tex("L07.tex"))
        self.assertEqual(result.logical_filename, "L07.pdf")
        self.assertTrue(result.pdf_content.startswith(b"%PDF-"))
        self.assertFalse(workspace.exists())

    def test_synthetic_ambient_latexmk_rc_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ambient-rc-") as root_name:
            root = Path(root_name)
            synthetic_home = root / "home"
            synthetic_home.mkdir()
            marker = root / "ambient-rc-marker"
            marker_literal = str(marker).replace("\\", "\\\\").replace("'", "\\'")
            (synthetic_home / ".latexmkrc").write_text(
                "open(my $fh, '>', '"
                + marker_literal
                + "') or die; print $fh 'invoked'; close($fh);\n",
                encoding="utf-8",
            )
            synthetic_environment = {"HOME": str(synthetic_home)}

            vulnerable_argv = tuple(
                item for item in compilation._COMPILER_ARGV_PREFIX if item != "-norc"
            )
            with mock.patch.dict(os.environ, synthetic_environment):
                with mock.patch.object(
                    compilation, "_COMPILER_ARGV_PREFIX", vulnerable_argv
                ):
                    control = compile_pdf(logical_tex("L08.tex"))
            self.assertIsInstance(control, CompiledPdf)
            self.assertTrue(marker.is_file())
            marker.unlink()

            with mock.patch.dict(os.environ, synthetic_environment):
                result = compile_pdf(logical_tex("L08.tex"))
            self.assertIsInstance(result, CompiledPdf)
            self.assertFalse(marker.exists())
        self.assertFalse(root.exists())


class InputValidationTests(unittest.TestCase):
    def test_wrong_public_type_is_static_misuse_before_side_effects(self) -> None:
        sentinel = "invented-wrong-input-secret"
        with mock.patch.object(compilation, "_temporary_workspace") as workspace:
            with self.assertRaises(TypeError) as caught:
                compile_pdf(sentinel)  # type: ignore[arg-type]
        workspace.assert_not_called()
        self.assertNotIn(sentinel, str(caught.exception))

    def test_forged_exact_values_fail_before_workspace_or_process(self) -> None:
        sentinel = "invented-forged-source-secret"
        values: list[LogicalTexFile] = [object.__new__(LogicalTexFile)]

        missing_filename = logical_tex(source=sentinel)
        object.__delattr__(missing_filename, "logical_filename")
        values.append(missing_filename)

        missing_source = logical_tex(source=sentinel)
        object.__delattr__(missing_source, "tex_source")
        values.append(missing_source)

        bad_filename = logical_tex(source=sentinel)
        object.__setattr__(bad_filename, "logical_filename", "-shell-escape")
        values.append(bad_filename)

        bad_source_type = logical_tex(source=sentinel)
        object.__setattr__(bad_source_type, "tex_source", b"not text")
        values.append(bad_source_type)

        unencodable = logical_tex(source=sentinel)
        object.__setattr__(unencodable, "tex_source", "\ud800")
        values.append(unencodable)

        for value in values:
            with self.subTest(value=type(value).__name__):
                with mock.patch.object(compilation, "_temporary_workspace") as workspace:
                    with mock.patch.object(compilation.subprocess, "run") as run:
                        result = compile_pdf(value)
                self.assert_failure(result, "invalid_compilation_input")
                workspace.assert_not_called()
                run.assert_not_called()
                self.assertNotIn(sentinel, repr(result))

    def test_logical_filename_cannot_inject_arguments_or_commands(self) -> None:
        for filename in ("--shell-escape.tex", "L01.tex;touch-invented", "../L01.tex"):
            with self.subTest(filename=filename):
                with self.assertRaises(ValueError):
                    logical_tex(filename)

    def assert_failure(self, result: object, code: str) -> None:
        self.assertIsInstance(result, CompilationFailure)
        assert isinstance(result, CompilationFailure)
        self.assertEqual(result.status, "compilation_failed")
        self.assertEqual(result.diagnostics[0].code, code)


class FailureAndPrivacyTests(unittest.TestCase):
    def assert_failure(self, result: object, code: str) -> CompilationFailure:
        self.assertIsInstance(result, CompilationFailure)
        assert isinstance(result, CompilationFailure)
        self.assertEqual(result.diagnostics[0].code, code)
        return result

    def test_invalid_workspace_is_registered_and_content_safe(self) -> None:
        sentinel = "invented-workspace-exception-secret"
        with mock.patch.object(compilation, "_temporary_workspace", side_effect=OSError(sentinel)):
            result = compile_pdf(logical_tex(source=sentinel))
        failure = self.assert_failure(result, "invalid_build_workspace")
        self.assertNotIn(sentinel, repr(failure))
        self.assertNotIn(sentinel, str(failure))

    def test_missing_compiler_is_registered_and_exception_text_is_redacted(self) -> None:
        sentinel = "invented-missing-compiler-secret"
        with mock.patch.object(compilation.subprocess, "run", side_effect=FileNotFoundError(sentinel)):
            result = compile_pdf(logical_tex(source=sentinel))
        failure = self.assert_failure(result, "compiler_unavailable")
        self.assertNotIn(sentinel, repr(failure))

    def test_nonzero_exit_redacts_source_output_and_temporary_path_and_cleans(self) -> None:
        sentinel = "invented-compiler-output-secret"
        original_factory = compilation._temporary_workspace
        recorded: list[Path] = []

        class RecordingWorkspace:
            def __init__(self) -> None:
                self._inner = original_factory()

            def __enter__(self) -> str:
                name = self._inner.__enter__()
                recorded.append(Path(name))
                return name

            def __exit__(self, *args: object) -> object:
                return self._inner.__exit__(*args)

        completed = subprocess.CompletedProcess((), 1, stdout=sentinel.encode(), stderr=sentinel.encode())
        with mock.patch.object(compilation, "_temporary_workspace", side_effect=RecordingWorkspace):
            with mock.patch.object(compilation.subprocess, "run", return_value=completed):
                result = compile_pdf(logical_tex(source=sentinel))
        failure = self.assert_failure(result, "compiler_nonzero_exit")
        self.assertEqual(len(recorded), 1)
        self.assertFalse(recorded[0].exists())
        public = repr(failure) + str(failure)
        self.assertNotIn(sentinel, public)
        self.assertNotIn(str(recorded[0]), public)

    def test_timeout_is_registered_and_captured_output_is_redacted(self) -> None:
        sentinel = "invented-timeout-output-secret"
        timeout = subprocess.TimeoutExpired(("latexmk",), 120, output=sentinel.encode(), stderr=sentinel.encode())
        with mock.patch.object(compilation.subprocess, "run", side_effect=timeout):
            result = compile_pdf(logical_tex(source=sentinel))
        failure = self.assert_failure(result, "compiler_timeout")
        self.assertNotIn(sentinel, repr(failure))

    def test_missing_expected_pdf_is_registered(self) -> None:
        with mock.patch.object(
            compilation.subprocess,
            "run",
            return_value=subprocess.CompletedProcess((), 0, stdout=b"invented", stderr=b"invented"),
        ):
            result = compile_pdf(logical_tex())
        self.assert_failure(result, "expected_pdf_missing")

    def test_unreadable_and_empty_expected_pdf_are_registered(self) -> None:
        def produce_empty_pdf(args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            Path(kwargs["cwd"]).joinpath("L01.pdf").write_bytes(b"")  # type: ignore[arg-type]
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

        with mock.patch.object(compilation.subprocess, "run", side_effect=produce_empty_pdf):
            result = compile_pdf(logical_tex("L01.tex"))
        self.assert_failure(result, "expected_pdf_unreadable")

        def produce_pdf(args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            Path(kwargs["cwd"]).joinpath("L01.pdf").write_bytes(b"invented")  # type: ignore[arg-type]
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

        with mock.patch.object(compilation.subprocess, "run", side_effect=produce_pdf):
            with mock.patch.object(Path, "read_bytes", side_effect=PermissionError("invented-private-path")):
                result = compile_pdf(logical_tex("L01.tex"))
        failure = self.assert_failure(result, "expected_pdf_unreadable")
        self.assertNotIn("invented-private-path", repr(failure))

    def test_success_repr_redacts_tex_and_pdf_payloads(self) -> None:
        tex_sentinel = "invented-private-tex-payload"
        pdf_sentinel = b"invented-private-pdf-payload"

        def produce_pdf(args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            Path(kwargs["cwd"]).joinpath("L01.pdf").write_bytes(b"%PDF-1.7\n" + pdf_sentinel)  # type: ignore[arg-type]
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

        with mock.patch.object(compilation.subprocess, "run", side_effect=produce_pdf):
            result = compile_pdf(logical_tex("L01.tex", tex_sentinel))
        self.assertIsInstance(result, CompiledPdf)
        public = repr(result) + str(result)
        self.assertNotIn(tex_sentinel, public)
        self.assertNotIn(pdf_sentinel.decode(), public)
        self.assertNotIn("pdf_content", public)

    def test_ordinary_adapter_exception_is_contained_and_base_exception_propagates(self) -> None:
        sentinel = "invented-adapter-exception-secret"
        with mock.patch.object(compilation, "_compile_in_workspace", side_effect=RuntimeError(sentinel)):
            result = compile_pdf(logical_tex(source=sentinel))
        failure = self.assert_failure(result, "compilation_exception")
        self.assertNotIn(sentinel, repr(failure))

        class StopCompilation(BaseException):
            pass

        with mock.patch.object(compilation, "_compile_in_workspace", side_effect=StopCompilation):
            with self.assertRaises(StopCompilation):
                compile_pdf(logical_tex())


class ProcessSecurityTests(unittest.TestCase):
    def test_compiler_uses_fixed_argv_timeout_and_no_shell(self) -> None:
        completed = subprocess.CompletedProcess((), 1, stdout=b"invented", stderr=b"invented")
        with mock.patch.object(compilation.subprocess, "run", return_value=completed) as run:
            result = compile_pdf(logical_tex("L42.tex"))
        self.assertIsInstance(result, CompilationFailure)
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(
            args[0],
            (
                "latexmk",
                "-norc",
                "-xelatex",
                "-interaction=nonstopmode",
                "-halt-on-error",
                "-file-line-error",
                "-g",
                "L42.tex",
            ),
        )
        self.assertIs(kwargs["shell"], False)
        self.assertIs(kwargs["check"], False)
        self.assertIs(kwargs["capture_output"], True)
        self.assertEqual(kwargs["timeout"], COMPILER_TIMEOUT_SECONDS)
        self.assertIsInstance(kwargs["cwd"], Path)

    def test_compilation_module_has_narrow_import_and_process_policy(self) -> None:
        path = Path("course_compiler/compilation.py")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        absolute_imports: set[str] = set()
        relative_imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                absolute_imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    relative_imports.add(node.module or "")
                elif node.module:
                    absolute_imports.add(node.module)
        self.assertEqual(
            absolute_imports,
            {
                "__future__",
                "subprocess",
                "tempfile",
                "dataclasses",
                "os", "re",
                "pathlib",
                "typing",
            },
        )
        self.assertEqual(relative_imports, {"assembly"})
        source = path.read_text(encoding="utf-8")
        self.assertNotIn("shell=True", source)
        # `os` is present only for os.environ; no process-spawning os API.
        self.assertNotIn("os.system", source)
        self.assertNotIn("os.popen", source)
        self.assertNotIn("os.exec", source)
        self.assertNotIn("os.spawn", source)

    def test_compiler_environment_pins_reproducible_build_variables(self) -> None:
        from course_compiler.compilation import (
            COMPILER_SOURCE_DATE_EPOCH,
            _compiler_environment,
        )

        environment = _compiler_environment()
        self.assertEqual(environment["SOURCE_DATE_EPOCH"], COMPILER_SOURCE_DATE_EPOCH)
        self.assertEqual(environment["FORCE_SOURCE_DATE"], "1")
        # The pins are applied on top of the inherited environment, so the
        # compiler still resolves through the ambient PATH.
        for name in os.environ:
            if name not in {"SOURCE_DATE_EPOCH", "FORCE_SOURCE_DATE"}:
                self.assertEqual(environment[name], os.environ[name])


class CompilerInputShapeTests(unittest.TestCase):
    def test_public_compiler_input_values_are_exact_frozen_and_slotted(self) -> None:
        self.assertEqual(
            [item.name for item in fields(CompilerCompanionFile)],
            ["logical_filename", "content_bytes"],
        )
        self.assertEqual(
            [item.name for item in fields(CompilerInputBundle)],
            ["root_tex", "companion_files"],
        )
        one = companion("plot.png")
        self.assertFalse(hasattr(one, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            one.logical_filename = "other.png"  # type: ignore[misc]
        empty = bundle()
        self.assertFalse(hasattr(empty, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            empty.companion_files = ()  # type: ignore[misc]

    def test_new_public_surface_is_exported_and_reachable(self) -> None:
        import course_compiler

        for name in (
            "CompilerCompanionFile",
            "CompilerInputBundle",
            "compile_pdf_bundle",
        ):
            with self.subTest(name=name):
                self.assertIn(name, course_compiler.__all__)
                self.assertIn(name, compilation.__all__)
                self.assertIs(getattr(course_compiler, name), getattr(compilation, name))
        self.assertTrue(hasattr(LocalPdfCompiler, "compile_bundle"))

    def test_companion_repr_redacts_byte_payload(self) -> None:
        payload = b"invented-companion-payload-secret"
        one = companion("assets/figures/plot.png", payload)
        public = repr(one) + str(one)
        self.assertNotIn(payload.decode(), public)
        self.assertNotIn("content_bytes", public)
        self.assertIn("assets/figures/plot.png", public)

        inside_bundle = repr(bundle(one))
        self.assertNotIn(payload.decode(), inside_bundle)
        self.assertNotIn("content_bytes", inside_bundle)


class CompanionFilenameValidationTests(unittest.TestCase):
    def test_valid_logical_posix_filenames_are_accepted_exactly(self) -> None:
        for filename in (
            "plot.png",
            "parts/invented.tex",
            "assets/figures/plot.png",
            "資料/図.png",
            "no-extension",
            "a/b/c/d/e",
            "image",
        ):
            with self.subTest(filename=filename):
                self.assertEqual(companion(filename).logical_filename, filename)

    def test_invalid_logical_filenames_are_rejected(self) -> None:
        for filename in (
            "",
            "/plot.png",
            "plot.png/",
            "images//plot.png",
            "images/./plot.png",
            "images/../plot.png",
            "../plot.png",
            ".",
            "..",
            "./plot.png",
            "images\\plot.png",
            "C:/plot.png",
            "C:\\plot.png",
            "a:b",
            "path\x00name",
            "\ud800/plot.png",
        ):
            with self.subTest(filename=filename):
                with self.assertRaises(ValueError) as caught:
                    companion(filename)
                self.assertEqual(
                    str(caught.exception), "compiler companion filename is invalid"
                )
                if filename:
                    self.assertNotIn(filename, str(caught.exception))

    def test_non_exact_string_filenames_are_rejected(self) -> None:
        class LoudStr(str):
            pass

        for value in (b"plot.png", LoudStr("plot.png"), None, 7):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ValueError):
                    CompilerCompanionFile(
                        logical_filename=value,  # type: ignore[arg-type]
                        content_bytes=b"invented",
                    )

    def test_filenames_are_never_normalized_case_folded_or_rewritten(self) -> None:
        composed = "caf\u00e9/plot.png"
        decomposed = "cafe\u0301/plot.png"
        upper = "Assets/Plot.PNG"
        lower = "assets/plot.png"
        for filename in (composed, decomposed, upper, lower):
            with self.subTest(filename=filename):
                self.assertEqual(companion(filename).logical_filename, filename)
        self.assertNotEqual(companion(composed), companion(decomposed))
        self.assertNotEqual(companion(upper), companion(lower))
        # Distinct-by-exact-string names never collide with one another.
        self.assertEqual(len(bundle(companion(composed), companion(decomposed)).companion_files), 2)
        self.assertEqual(len(bundle(companion(upper), companion(lower)).companion_files), 2)


class CompanionPayloadValidationTests(unittest.TestCase):
    def test_exact_bytes_including_empty_are_accepted_unchanged(self) -> None:
        for payload in (b"", b"invented", b"\x00\x01\xff", "invented".encode("utf-16")):
            with self.subTest(payload=payload):
                one = companion("plot.png", payload)
                self.assertIs(type(one.content_bytes), bytes)
                self.assertEqual(one.content_bytes, payload)

    def test_bytes_like_alternatives_are_rejected_without_coercion(self) -> None:
        for payload in (
            bytearray(b"invented"),
            memoryview(b"invented"),
            "invented",
            None,
            7,
        ):
            with self.subTest(payload=type(payload).__name__):
                with self.assertRaises(ValueError) as caught:
                    CompilerCompanionFile(
                        logical_filename="plot.png",
                        content_bytes=payload,  # type: ignore[arg-type]
                    )
                self.assertEqual(
                    str(caught.exception),
                    "compiler companion content must be exact bytes",
                )

    def test_payload_participates_in_equality_and_hashing(self) -> None:
        self.assertEqual(companion("plot.png", b"a"), companion("plot.png", b"a"))
        self.assertEqual(hash(companion("plot.png", b"a")), hash(companion("plot.png", b"a")))
        self.assertNotEqual(companion("plot.png", b"a"), companion("plot.png", b"b"))
        self.assertNotEqual(companion("plot.png", b"a"), companion("other.png", b"a"))


class CompilerInputBundleTests(unittest.TestCase):
    def test_root_must_be_an_exact_accepted_logical_tex_file(self) -> None:
        for root in (None, "Course_Study_Lectures.tex", companion("plot.png")):
            with self.subTest(root=type(root).__name__):
                with self.assertRaises(ValueError) as caught:
                    CompilerInputBundle(root_tex=root, companion_files=())  # type: ignore[arg-type]
                self.assertEqual(
                    str(caught.exception), "compiler input root TeX file is invalid"
                )

    def test_forged_root_is_revalidated_at_the_bundle_boundary(self) -> None:
        sentinel = "invented-forged-bundle-root-secret"
        forged = logical_tex(source=sentinel)
        object.__setattr__(forged, "logical_filename", "--shell-escape.tex")
        with self.assertRaises(ValueError) as caught:
            CompilerInputBundle(root_tex=forged, companion_files=())
        self.assertNotIn(sentinel, str(caught.exception))

        missing = logical_tex(source=sentinel)
        object.__delattr__(missing, "tex_source")
        with self.assertRaises(ValueError):
            CompilerInputBundle(root_tex=missing, companion_files=())

    def test_forged_companion_members_are_revalidated(self) -> None:
        payload = b"invented-forged-companion-secret"
        forged = companion("plot.png", payload)
        object.__setattr__(forged, "logical_filename", "../escape.png")
        with self.assertRaises(ValueError) as caught:
            bundle(forged)
        self.assertEqual(
            str(caught.exception), "compiler input companion files are invalid"
        )
        self.assertNotIn(payload.decode(), str(caught.exception))

        forged_payload = companion("plot.png")
        object.__setattr__(forged_payload, "content_bytes", bytearray(b"invented"))
        with self.assertRaises(ValueError):
            bundle(forged_payload)

        with self.assertRaises(ValueError):
            bundle(object.__new__(CompilerCompanionFile))

    def test_companion_collection_must_be_an_exact_immutable_tuple(self) -> None:
        one = companion("plot.png")
        for collection in ([one], {one}, {"plot.png": one}, None):
            with self.subTest(collection=type(collection).__name__):
                with self.assertRaises(ValueError) as caught:
                    CompilerInputBundle(
                        root_tex=logical_tex(),
                        companion_files=collection,  # type: ignore[arg-type]
                    )
                self.assertEqual(
                    str(caught.exception), "compiler input companion files are invalid"
                )
        self.assertIs(type(bundle(one).companion_files), tuple)

    def test_companions_are_stored_in_canonical_ascending_filename_order(self) -> None:
        names = ("parts/invented.tex", "assets/figures/plot.png", "a.png", "資料/図.png")
        stored = bundle(*(companion(name) for name in names)).companion_files
        self.assertEqual(
            [item.logical_filename for item in stored], sorted(names)
        )

    def test_caller_insertion_order_does_not_affect_equality_or_hash(self) -> None:
        first = companion("a/one.tex", b"one")
        second = companion("b/two.tex", b"two")
        third = companion("c/three.tex", b"three")
        forward = bundle(first, second, third)
        reversed_order = bundle(third, second, first)
        self.assertEqual(forward, reversed_order)
        self.assertEqual(hash(forward), hash(reversed_order))
        self.assertEqual(forward.companion_files, reversed_order.companion_files)

    def test_empty_companion_tuple_is_valid_and_equals_a_root_only_bundle(self) -> None:
        empty = bundle()
        self.assertEqual(empty.companion_files, ())
        self.assertEqual(empty, CompilerInputBundle(logical_tex(), ()))
        self.assertEqual(hash(empty), hash(CompilerInputBundle(logical_tex(), ())))

    def test_bundle_equality_covers_root_and_companions(self) -> None:
        self.assertNotEqual(bundle(companion("a.png")), bundle(companion("b.png")))
        self.assertNotEqual(
            bundle(companion("a.png", b"one")), bundle(companion("a.png", b"two"))
        )
        self.assertNotEqual(
            bundle(root=logical_tex("L01.tex")), bundle(root=logical_tex("L02.tex"))
        )


class CompilerInputCollisionTests(unittest.TestCase):
    def assert_collision(self, *companions: CompilerCompanionFile, root: LogicalTexFile | None = None) -> None:
        with self.assertRaises(ValueError) as caught:
            bundle(*companions, root=root)
        self.assertEqual(
            str(caught.exception),
            "compiler input logical file set has a path collision",
        )

    def test_duplicate_companion_filename_is_rejected(self) -> None:
        self.assert_collision(companion("parts/a.tex", b"one"), companion("parts/a.tex", b"two"))
        self.assert_collision(companion("parts/a.tex", b"same"), companion("parts/a.tex", b"same"))

    def test_companion_equal_to_the_root_filename_is_rejected(self) -> None:
        self.assert_collision(companion("Course_Study_Lectures.tex"))
        self.assert_collision(companion("L07.tex"), root=logical_tex("L07.tex"))

    def test_companion_equal_to_the_expected_output_pdf_is_rejected(self) -> None:
        self.assert_collision(companion("Course_Study_Lectures.pdf"))
        self.assert_collision(companion("L07.pdf"), root=logical_tex("L07.tex"))

    def test_root_and_companion_prefix_collision_is_rejected(self) -> None:
        self.assert_collision(companion("Course_Study_Lectures.tex/part.tex"))

    def test_expected_pdf_directory_prefix_collision_is_rejected(self) -> None:
        self.assert_collision(companion("Course_Study_Lectures.pdf/page.png"))

    def test_companion_prefix_collisions_are_rejected_in_both_directions(self) -> None:
        self.assert_collision(companion("images"), companion("images/plot.png"))
        self.assert_collision(companion("images/plot.png"), companion("images"))
        self.assert_collision(companion("a/b"), companion("a/b/c.tex"))
        self.assert_collision(companion("a/b/c.tex"), companion("a/b"))
        self.assert_collision(companion("a/b/c/d.tex"), companion("a/b"))

    def test_near_prefix_names_that_are_not_segment_prefixes_remain_valid(self) -> None:
        stored = bundle(companion("image"), companion("images/plot.png")).companion_files
        self.assertEqual(
            [item.logical_filename for item in stored], ["image", "images/plot.png"]
        )
        for pair in (
            ("a/b", "a/bc.tex"),
            ("plot.png", "plot.png.bak"),
            ("Course_Study_Lectures.texture", "parts/a.tex"),
        ):
            with self.subTest(pair=pair):
                self.assertEqual(
                    len(bundle(*(companion(name) for name in pair)).companion_files), 2
                )

    def test_shared_parent_directories_are_not_a_collision(self) -> None:
        stored = bundle(
            companion("images/one.png"),
            companion("images/two.png"),
            companion("images/nested/three.png"),
        ).companion_files
        self.assertEqual(len(stored), 3)


class BundleCompilationBoundaryTests(unittest.TestCase):
    def assert_failure(self, result: object, code: str) -> CompilationFailure:
        self.assertIsInstance(result, CompilationFailure)
        assert isinstance(result, CompilationFailure)
        self.assertEqual(result.diagnostics[0].code, code)
        return result

    def test_wrong_public_type_is_static_misuse_before_side_effects(self) -> None:
        sentinel = "invented-wrong-bundle-input-secret"
        for value in (sentinel, logical_tex(), None):
            with self.subTest(value=type(value).__name__):
                with mock.patch.object(compilation, "_temporary_workspace") as workspace:
                    with self.assertRaises(TypeError) as caught:
                        compile_pdf_bundle(value)  # type: ignore[arg-type]
                workspace.assert_not_called()
                self.assertNotIn(sentinel, str(caught.exception))

    def test_forged_bundles_fail_closed_before_workspace_or_process(self) -> None:
        sentinel = "invented-forged-bundle-secret"
        payload = b"invented-forged-bundle-payload"
        forged_values: list[CompilerInputBundle] = [object.__new__(CompilerInputBundle)]

        missing_root = bundle(companion("plot.png", payload))
        object.__delattr__(missing_root, "root_tex")
        forged_values.append(missing_root)

        bad_root = bundle(root=logical_tex(source=sentinel))
        object.__setattr__(bad_root, "root_tex", "Course_Study_Lectures.tex")
        forged_values.append(bad_root)

        list_collection = bundle(companion("plot.png", payload))
        object.__setattr__(
            list_collection, "companion_files", [companion("plot.png", payload)]
        )
        forged_values.append(list_collection)

        forged_member = bundle(companion("plot.png", payload))
        escaped = companion("plot.png", payload)
        object.__setattr__(escaped, "logical_filename", "../escape.png")
        object.__setattr__(forged_member, "companion_files", (escaped,))
        forged_values.append(forged_member)

        collided = bundle(companion("plot.png", payload))
        object.__setattr__(
            collided,
            "companion_files",
            (companion("images", payload), companion("images/plot.png", payload)),
        )
        forged_values.append(collided)

        out_of_order = bundle(companion("a.png", payload), companion("b.png", payload))
        object.__setattr__(
            out_of_order,
            "companion_files",
            (companion("b.png", payload), companion("a.png", payload)),
        )
        forged_values.append(out_of_order)

        for value in forged_values:
            with self.subTest(value=id(value)):
                with mock.patch.object(compilation, "_temporary_workspace") as workspace:
                    with mock.patch.object(compilation.subprocess, "run") as run:
                        result = compile_pdf_bundle(value)
                self.assert_failure(result, "invalid_compilation_input")
                workspace.assert_not_called()
                run.assert_not_called()
                public = repr(result) + str(result)
                self.assertNotIn(sentinel, public)
                self.assertNotIn(payload.decode(), public)

    def test_malformed_bundles_never_reach_the_filesystem_at_construction(self) -> None:
        with mock.patch.object(compilation, "_temporary_workspace") as workspace:
            with mock.patch.object(compilation.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    bundle(companion("images"), companion("images/plot.png"))
                with self.assertRaises(ValueError):
                    companion("../escape.png")
        workspace.assert_not_called()
        run.assert_not_called()

    def test_bundle_stages_nested_companions_with_exact_bytes(self) -> None:
        payload = b"\x00\x01\r\n invented exact companion bytes \xff"
        staged: dict[str, bytes] = {}
        listing: list[str] = []

        def produce_pdf(args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            workspace = Path(kwargs["cwd"])  # type: ignore[arg-type]
            for path in sorted(workspace.rglob("*")):
                relative = path.relative_to(workspace).as_posix()
                listing.append(relative)
                if path.is_file():
                    staged[relative] = path.read_bytes()
            workspace.joinpath("Course_Study_Lectures.pdf").write_bytes(b"%PDF-1.7\ninvented")
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

        compiler_input = bundle(
            companion("assets/figures/nested/plot.png", payload),
            companion("parts/invented.tex", _SYNTHETIC_COMPANION_TEX),
            companion("top.dat", b""),
        )
        with mock.patch.object(compilation.subprocess, "run", side_effect=produce_pdf):
            result = compile_pdf_bundle(compiler_input)

        self.assertIsInstance(result, CompiledPdf)
        self.assertEqual(staged["assets/figures/nested/plot.png"], payload)
        self.assertEqual(staged["parts/invented.tex"], _SYNTHETIC_COMPANION_TEX)
        self.assertEqual(staged["top.dat"], b"")
        self.assertEqual(
            staged["Course_Study_Lectures.tex"], _SYNTHETIC_TEX.encode("utf-8")
        )
        self.assertIn("assets/figures/nested", listing)
        self.assertIn("parts", listing)

    def test_bundle_output_and_result_semantics_match_single_root_compilation(self) -> None:
        def produce_pdf(args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            Path(kwargs["cwd"]).joinpath("L01.pdf").write_bytes(b"%PDF-1.7\ninvented")  # type: ignore[arg-type]
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

        root = logical_tex("L01.tex")
        with mock.patch.object(compilation.subprocess, "run", side_effect=produce_pdf):
            plain = compile_pdf(root)
            bundled = compile_pdf_bundle(
                CompilerInputBundle(root, (companion("parts/invented.tex"),))
            )
        self.assertIsInstance(bundled, CompiledPdf)
        self.assertEqual(plain, bundled)
        assert isinstance(bundled, CompiledPdf)
        self.assertEqual(bundled.logical_filename, "L01.pdf")
        self.assertIs(bundled.compilation_profile, COMPILATION_IDENTITY)
        self.assertEqual(
            [item.name for item in fields(CompiledPdf)],
            ["status", "compilation_profile", "logical_filename", "pdf_content"],
        )

    def test_bundle_compilation_preserves_fixed_subprocess_invariants(self) -> None:
        completed = subprocess.CompletedProcess((), 1, stdout=b"invented", stderr=b"invented")
        compiler_input = CompilerInputBundle(
            logical_tex("L42.tex"), (companion("parts/invented.tex"),)
        )
        with mock.patch.object(compilation.subprocess, "run", return_value=completed) as run:
            result = compile_pdf_bundle(compiler_input)
        self.assertIsInstance(result, CompilationFailure)
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(
            args[0],
            (
                "latexmk",
                "-norc",
                "-xelatex",
                "-interaction=nonstopmode",
                "-halt-on-error",
                "-file-line-error",
                "-g",
                "L42.tex",
            ),
        )
        self.assertIs(kwargs["shell"], False)
        self.assertIs(kwargs["check"], False)
        self.assertIs(kwargs["capture_output"], True)
        self.assertEqual(kwargs["timeout"], COMPILER_TIMEOUT_SECONDS)
        self.assertIsInstance(kwargs["cwd"], Path)

    def test_bundle_adapter_method_delegates_to_the_public_operation(self) -> None:
        compiler = LocalPdfCompiler()
        expected = CompilationFailure(
            "compilation_failed",
            (
                CompilationDiagnostic(
                    "compiler_nonzero_exit",
                    "compiler",
                    "The fixed local TeX compiler reported a failure.",
                ),
            ),
        )
        with mock.patch.object(
            compilation.subprocess, "run", return_value=subprocess.CompletedProcess((), 1)
        ):
            self.assertEqual(compiler.compile_bundle(bundle(companion("plot.png"))), expected)

    def test_success_exit_with_bad_log_rejects_pdf_without_leaking_log(self) -> None:
        for log, code in (
            ("Missing character: There is no invented-sensitive-glyph", "compiler_missing_glyph"),
            ("! Bad math environment delimiter. invented-sensitive-source", "compiler_fatal_diagnostic"),
            (r"Overfull \hbox (8.5pt too wide) invented-sensitive-source", "compiler_overflow"),
        ):
            with self.subTest(code=code):
                def compile_with_bad_log(*args, **kwargs):
                    workspace = Path(kwargs["cwd"])
                    (workspace / "Course_Study_Lectures.log").write_text(log)
                    (workspace / "Course_Study_Lectures.pdf").write_bytes(b"%PDF-invented")
                    return subprocess.CompletedProcess((), 0, stdout=b"", stderr=b"")
                with mock.patch.object(compilation.subprocess, "run", side_effect=compile_with_bad_log):
                    result = compile_pdf_bundle(bundle(companion("plot.png")))
                failure = self.assert_failure(result, code)
                self.assertNotIn("invented-sensitive", repr(failure))

    def test_bundle_failure_paths_stay_registered_and_content_safe(self) -> None:
        payload = b"invented-bundle-failure-payload"
        sentinel = "invented-bundle-failure-secret"
        compiler_input = bundle(
            companion("parts/invented.tex", payload),
            root=logical_tex(source=sentinel),
        )

        cases: list[tuple[str, object, str]] = [
            ("_temporary_workspace", OSError(sentinel), "invalid_build_workspace"),
            ("_stage_compiler_input", OSError(sentinel), "invalid_build_workspace"),
            ("_compile_in_workspace", RuntimeError(sentinel), "compilation_exception"),
        ]
        for attribute, error, code in cases:
            with self.subTest(attribute=attribute):
                with mock.patch.object(compilation, attribute, side_effect=error):
                    result = compile_pdf_bundle(compiler_input)
                failure = self.assert_failure(result, code)
                public = repr(failure) + str(failure)
                self.assertNotIn(sentinel, public)
                self.assertNotIn(payload.decode(), public)

        subprocess_cases: list[tuple[object, str]] = [
            (FileNotFoundError(sentinel), "compiler_unavailable"),
            (
                subprocess.TimeoutExpired(
                    ("latexmk",), 120, output=sentinel.encode(), stderr=sentinel.encode()
                ),
                "compiler_timeout",
            ),
        ]
        for error, code in subprocess_cases:
            with self.subTest(code=code):
                with mock.patch.object(compilation.subprocess, "run", side_effect=error):
                    result = compile_pdf_bundle(compiler_input)
                failure = self.assert_failure(result, code)
                self.assertNotIn(sentinel, repr(failure) + str(failure))

        with mock.patch.object(
            compilation.subprocess,
            "run",
            return_value=subprocess.CompletedProcess((), 0, stdout=b"", stderr=b""),
        ):
            self.assert_failure(compile_pdf_bundle(compiler_input), "expected_pdf_missing")

        def produce_empty_pdf(args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            Path(kwargs["cwd"]).joinpath("Course_Study_Lectures.pdf").write_bytes(b"")  # type: ignore[arg-type]
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

        with mock.patch.object(compilation.subprocess, "run", side_effect=produce_empty_pdf):
            self.assert_failure(
                compile_pdf_bundle(compiler_input), "expected_pdf_unreadable"
            )

    def test_bundle_base_exception_propagates_uncontained(self) -> None:
        class StopBundleCompilation(BaseException):
            pass

        with mock.patch.object(
            compilation, "_compile_in_workspace", side_effect=StopBundleCompilation
        ):
            with self.assertRaises(StopBundleCompilation):
                compile_pdf_bundle(bundle(companion("plot.png")))

    def test_bundle_workspace_is_disposable_and_cleaned_up(self) -> None:
        original_factory = compilation._temporary_workspace
        recorded: list[Path] = []

        class RecordingWorkspace:
            def __init__(self) -> None:
                self._inner = original_factory()

            def __enter__(self) -> str:
                name = self._inner.__enter__()
                recorded.append(Path(name))
                return name

            def __exit__(self, *args: object) -> object:
                return self._inner.__exit__(*args)

        compiler_input = bundle(companion("parts/invented.tex", _SYNTHETIC_COMPANION_TEX))
        completed = subprocess.CompletedProcess((), 1, stdout=b"invented", stderr=b"invented")
        with mock.patch.object(compilation, "_temporary_workspace", side_effect=RecordingWorkspace):
            with mock.patch.object(compilation.subprocess, "run", return_value=completed):
                first = compile_pdf_bundle(compiler_input)
            with mock.patch.object(compilation.subprocess, "run", return_value=completed):
                second = compile_pdf_bundle(compiler_input)
        self.assert_failure(first, "compiler_nonzero_exit")
        self.assert_failure(second, "compiler_nonzero_exit")
        self.assertEqual(len(recorded), 2)
        self.assertNotEqual(recorded[0], recorded[1])
        for workspace in recorded:
            self.assertFalse(workspace.exists())

    def test_compilation_layer_has_no_visual_domain_dependency(self) -> None:
        source = Path("course_compiler/compilation.py").read_text(encoding="utf-8")
        for name in (
            "pdf_visual_extraction",
            "visual_placement",
            "AssetReference",
            "ExtractedPdfVisual",
            "VisualPlacement",
            "graphicx",
            "includegraphics",
        ):
            with self.subTest(name=name):
                self.assertNotIn(name, source)
        tree = ast.parse(source, filename="course_compiler/compilation.py")
        relative = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level
        }
        self.assertEqual(relative, {"assembly"})


@unittest.skipUnless(
    TEX_TOOLCHAIN_AVAILABLE,
    f"real XeLaTeX compile unavailable ({TEX_MISSING_REASON})",
)
class BundleRealCompilerIntegrationTests(unittest.TestCase):
    def test_synthetic_nested_input_companion_compiles_with_the_real_toolchain(self) -> None:
        original_factory = compilation._temporary_workspace
        recorded: list[Path] = []

        class RecordingWorkspace:
            def __init__(self) -> None:
                self._inner = original_factory()

            def __enter__(self) -> str:
                name = self._inner.__enter__()
                recorded.append(Path(name))
                return name

            def __exit__(self, *args: object) -> object:
                return self._inner.__exit__(*args)

        compiler_input = CompilerInputBundle(
            root_tex=logical_tex("L09.tex", _SYNTHETIC_INPUT_ROOT_TEX),
            companion_files=(companion("parts/invented.tex", _SYNTHETIC_COMPANION_TEX),),
        )
        with mock.patch.object(compilation, "_temporary_workspace", side_effect=RecordingWorkspace):
            result = compile_pdf_bundle(compiler_input)

        self.assertIsInstance(result, CompiledPdf)
        assert isinstance(result, CompiledPdf)
        self.assertEqual(result.status, "compiled")
        self.assertEqual(result.logical_filename, "L09.pdf")
        self.assertIs(result.compilation_profile, COMPILATION_IDENTITY)
        self.assertTrue(result.pdf_content.startswith(b"%PDF-"))
        self.assertEqual(len(recorded), 1)
        self.assertFalse(recorded[0].exists())


if __name__ == "__main__":
    unittest.main()
