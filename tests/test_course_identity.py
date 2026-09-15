from __future__ import annotations

import ast
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import course_compiler
from course_compiler import COURSE_REFERENCE_VERSION, CourseReference


class HostileStr(str):
    def _raise(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError("hostile caller value must not be used")

    __eq__ = _raise
    __ne__ = _raise
    __str__ = _raise


class CourseIdentityTests(unittest.TestCase):
    def test_public_shape_is_exact_frozen_and_slotted(self) -> None:
        self.assertEqual(COURSE_REFERENCE_VERSION, "course-reference/v1")
        self.assertEqual(
            [item.name for item in fields(CourseReference)],
            ["reference_version", "course_id"],
        )
        reference = CourseReference(COURSE_REFERENCE_VERSION, "linear-models")
        self.assertFalse(hasattr(reference, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            reference.course_id = "changed"  # type: ignore[misc]

    def test_exact_supported_version_is_required(self) -> None:
        self.assertEqual(
            CourseReference("course-reference/v1", "linear-models").reference_version,
            COURSE_REFERENCE_VERSION,
        )
        with self.assertRaisesRegex(ValueError, "^course reference version is unsupported$"):
            CourseReference("course-reference/v2", "linear-models")  # type: ignore[arg-type]

    def test_safe_course_id_grammar_has_exact_bounds(self) -> None:
        accepted = (
            "a",
            "linear-models",
            "linear_models",
            "linear.models-2",
            "a" + "b" * 63,
        )
        rejected = (
            "",
            ".",
            "..",
            "A",
            " linear-models",
            "linear-models ",
            "linear models",
            "café",
            "-linear-models",
            "_linear-models",
            ".linear-models",
            "a" * 65,
        )
        for course_id in accepted:
            with self.subTest(accepted=course_id):
                self.assertEqual(
                    CourseReference(COURSE_REFERENCE_VERSION, course_id).course_id,
                    course_id,
                )
        for course_id in rejected:
            with self.subTest(rejected=repr(course_id)):
                with self.assertRaisesRegex(ValueError, "^course reference ID is invalid$"):
                    CourseReference(COURSE_REFERENCE_VERSION, course_id)

    def test_only_exact_builtin_strings_are_accepted(self) -> None:
        hostile = HostileStr("linear-models")
        for value in (hostile, 1, None, b"linear-models"):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(ValueError, "^course reference ID is invalid$") as caught:
                    CourseReference(COURSE_REFERENCE_VERSION, value)  # type: ignore[arg-type]
                self.assertNotIn("hostile caller value", str(caught.exception))
        with self.assertRaisesRegex(ValueError, "^course reference version is unsupported$"):
            CourseReference(HostileStr(COURSE_REFERENCE_VERSION), "linear-models")  # type: ignore[arg-type]

    def test_identity_is_the_complete_unmodified_pair(self) -> None:
        first = CourseReference(COURSE_REFERENCE_VERSION, "linear-models")
        same = CourseReference(COURSE_REFERENCE_VERSION, "linear-models")
        different = CourseReference(COURSE_REFERENCE_VERSION, "linear_models")
        self.assertEqual(first, same)
        self.assertEqual(hash(first), hash(same))
        self.assertNotEqual(first, different)
        self.assertEqual(first.course_id, "linear-models")
        self.assertEqual(different.course_id, "linear_models")

    def test_public_package_exports_the_course_contract_symbols(self) -> None:
        self.assertIs(course_compiler.CourseReference, CourseReference)
        self.assertEqual(course_compiler.COURSE_REFERENCE_VERSION, COURSE_REFERENCE_VERSION)
        self.assertIn("CourseReference", course_compiler.__all__)
        self.assertIn("COURSE_REFERENCE_VERSION", course_compiler.__all__)

    def test_course_module_is_a_standard_library_leaf(self) -> None:
        module_path = Path(__file__).resolve().parents[1] / "course_compiler" / "course.py"
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(("." * node.level) + (node.module or ""))
        self.assertEqual(sorted(imports), ["__future__", "dataclasses", "re", "typing"])


if __name__ == "__main__":
    unittest.main()
