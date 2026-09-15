from __future__ import annotations

import ast
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import course_compiler
from course_compiler import (
    COURSE_REFERENCE_VERSION,
    COURSE_WORKFLOW_ASSOCIATION_VERSION,
    CourseReference,
    CourseWorkflowAssociation,
)


class HostileStr(str):
    def _raise(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError("hostile caller value must not leak")

    __eq__ = _raise
    __ne__ = _raise
    __str__ = _raise


class CourseReferenceSubclass(CourseReference):
    pass


def association(
    course_id: str = "linear-models",
    workflow_id: str = "exam-review.v1",
) -> CourseWorkflowAssociation:
    return CourseWorkflowAssociation(
        COURSE_WORKFLOW_ASSOCIATION_VERSION,
        CourseReference(COURSE_REFERENCE_VERSION, course_id),
        workflow_id,
    )


class CourseWorkflowAssociationTests(unittest.TestCase):
    def test_public_shape_is_exact_frozen_and_slotted(self) -> None:
        self.assertEqual(
            COURSE_WORKFLOW_ASSOCIATION_VERSION,
            "course-workflow-association/v1",
        )
        self.assertEqual(
            [item.name for item in fields(CourseWorkflowAssociation)],
            ["association_version", "course_reference", "workflow_id"],
        )
        value = association()
        self.assertFalse(hasattr(value, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            value.workflow_id = "changed"  # type: ignore[misc]

    def test_exact_supported_association_version_is_required(self) -> None:
        self.assertEqual(
            association().association_version,
            COURSE_WORKFLOW_ASSOCIATION_VERSION,
        )
        reference = CourseReference(COURSE_REFERENCE_VERSION, "linear-models")
        for version in (
            "course-workflow-association/v2",
            HostileStr(COURSE_WORKFLOW_ASSOCIATION_VERSION),
            None,
        ):
            with self.subTest(version_type=type(version).__name__):
                with self.assertRaisesRegex(
                    ValueError,
                    "^course-workflow association version is unsupported$",
                ) as caught:
                    CourseWorkflowAssociation(version, reference, "exam-review")  # type: ignore[arg-type]
                self.assertNotIn("hostile caller value", str(caught.exception))

    def test_nested_course_reference_must_be_exact_and_valid(self) -> None:
        exact = CourseReference(COURSE_REFERENCE_VERSION, "linear-models")
        self.assertIs(
            CourseWorkflowAssociation(
                COURSE_WORKFLOW_ASSOCIATION_VERSION,
                exact,
                "exam-review",
            ).course_reference,
            exact,
        )
        subclass = CourseReferenceSubclass(
            COURSE_REFERENCE_VERSION,
            "linear-models",
        )
        malformed = object.__new__(CourseReference)
        invalid = CourseReference(COURSE_REFERENCE_VERSION, "linear-models")
        object.__setattr__(invalid, "course_id", HostileStr("linear-models"))
        for value in (subclass, malformed, invalid, object(), None):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(
                    ValueError,
                    "^course-workflow association course reference is invalid$",
                ) as caught:
                    CourseWorkflowAssociation(
                        COURSE_WORKFLOW_ASSOCIATION_VERSION,
                        value,  # type: ignore[arg-type]
                        "exam-review",
                    )
                self.assertNotIn("hostile caller value", str(caught.exception))

    def test_nested_course_reference_is_revalidated(self) -> None:
        invalid_version = CourseReference(COURSE_REFERENCE_VERSION, "linear-models")
        object.__setattr__(invalid_version, "reference_version", "course-reference/v2")
        invalid_id = CourseReference(COURSE_REFERENCE_VERSION, "linear-models")
        object.__setattr__(invalid_id, "course_id", "Linear Models")
        for value in (invalid_version, invalid_id):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "^course-workflow association course reference is invalid$",
                ):
                    CourseWorkflowAssociation(
                        COURSE_WORKFLOW_ASSOCIATION_VERSION,
                        value,
                        "exam-review",
                    )

    def test_safe_workflow_id_grammar_has_exact_bounds(self) -> None:
        accepted = (
            "a",
            "exam-review",
            "exam_review",
            "exam.review-2",
            "a" + "b" * 63,
        )
        rejected = (
            "",
            ".",
            "..",
            "A",
            " exam-review",
            "exam-review ",
            "exam review",
            "café",
            "-exam-review",
            "_exam-review",
            ".exam-review",
            "a" * 65,
        )
        for workflow_id in accepted:
            with self.subTest(accepted=workflow_id):
                self.assertEqual(association(workflow_id=workflow_id).workflow_id, workflow_id)
        for workflow_id in rejected:
            with self.subTest(rejected=repr(workflow_id)):
                with self.assertRaisesRegex(
                    ValueError,
                    "^course-workflow association workflow ID is invalid$",
                ):
                    association(workflow_id=workflow_id)

    def test_only_exact_builtin_workflow_id_strings_are_accepted(self) -> None:
        for value in (HostileStr("exam-review"), 1, None, b"exam-review"):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(
                    ValueError,
                    "^course-workflow association workflow ID is invalid$",
                ) as caught:
                    CourseWorkflowAssociation(
                        COURSE_WORKFLOW_ASSOCIATION_VERSION,
                        CourseReference(COURSE_REFERENCE_VERSION, "linear-models"),
                        value,  # type: ignore[arg-type]
                    )
                self.assertNotIn("hostile caller value", str(caught.exception))

    def test_identity_is_complete_and_preserves_exact_values(self) -> None:
        first = association("linear-models", "exam-review")
        same = association("linear-models", "exam-review")
        different_course = association("linear_models", "exam-review")
        different_workflow = association("linear-models", "exam_review")
        self.assertEqual(first, same)
        self.assertEqual(hash(first), hash(same))
        self.assertNotEqual(first, different_course)
        self.assertNotEqual(first, different_workflow)
        self.assertEqual(first.course_reference.course_id, "linear-models")
        self.assertEqual(first.workflow_id, "exam-review")

    def test_course_and_workflow_namespaces_remain_distinct(self) -> None:
        value = association("shared-id", "shared-id")
        self.assertEqual(value.course_reference.course_id, value.workflow_id)
        self.assertIsInstance(value.course_reference, CourseReference)
        self.assertNotIsInstance(value.workflow_id, CourseReference)

        same_course_other_workflow = association("shared-id", "other-workflow")
        same_workflow_other_course = association("other-course", "shared-id")
        self.assertNotEqual(value, same_course_other_workflow)
        self.assertNotEqual(value, same_workflow_other_course)

    def test_public_package_exports_association_symbols(self) -> None:
        self.assertIs(course_compiler.CourseWorkflowAssociation, CourseWorkflowAssociation)
        self.assertEqual(
            course_compiler.COURSE_WORKFLOW_ASSOCIATION_VERSION,
            COURSE_WORKFLOW_ASSOCIATION_VERSION,
        )
        self.assertIn("CourseWorkflowAssociation", course_compiler.__all__)
        self.assertIn("COURSE_WORKFLOW_ASSOCIATION_VERSION", course_compiler.__all__)

    def test_domain_module_has_only_approved_dependencies_and_no_io(self) -> None:
        module_path = (
            Path(__file__).resolve().parents[1]
            / "course_compiler"
            / "course_workflow.py"
        )
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
            [".course", "__future__", "dataclasses", "re", "typing"],
        )
        self.assertTrue(
            call_names.isdisjoint(
                {"__import__", "compile", "eval", "exec", "open"},
            )
        )


if __name__ == "__main__":
    unittest.main()
