from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "course-compiler" / "SKILL.md"
AUTHORING = ROOT / "skills" / "course-compiler" / "references" / "lecture-authoring.md"
COARSE = ROOT / "skills" / "course-compiler" / "references" / "coarse-workflow.md"
LEGACY = ROOT / "skills" / "course-compiler" / "references" / "fine-grained-legacy.md"
RECOVERY = ROOT / "skills" / "course-compiler" / "references" / "workflow-recovery.md"
OFFSET_HELPER = ROOT / "skills" / "course-compiler" / "scripts" / "find_source_text_offset.py"
MCP_ADAPTER = ROOT / "course_compiler" / "mcp_adapter.py"
SEMANTIC_WORK = ROOT / "course_compiler" / "semantic_work.py"
APP_JS = ROOT / "course_compiler" / "app" / "static" / "app.js"
APP_HTML = ROOT / "course_compiler" / "app" / "static" / "index.html"


def _frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise AssertionError("missing YAML frontmatter start")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise AssertionError("missing YAML frontmatter end") from exc
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise AssertionError(f"invalid frontmatter line: {line}")
        values[key.strip()] = value.strip()
    return values


def _mcp_tool_names() -> tuple[str, ...]:
    tree = ast.parse(MCP_ADAPTER.read_text(encoding="utf-8"), filename=str(MCP_ADAPTER))
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            function = decorator.func
            if not (
                isinstance(function, ast.Attribute)
                and function.attr == "tool"
                and isinstance(function.value, ast.Name)
                and function.value.id == "server"
            ):
                continue
            for keyword in decorator.keywords:
                if (
                    keyword.arg == "name"
                    and isinstance(keyword.value, ast.Constant)
                    and type(keyword.value.value) is str
                ):
                    names.append(keyword.value.value)
    return tuple(names)


def _semantic_kinds() -> frozenset[str]:
    tree = ast.parse(SEMANTIC_WORK.read_text(encoding="utf-8"), filename=str(SEMANTIC_WORK))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "SEMANTIC_KINDS" for target in node.targets):
            continue
        if isinstance(node.value, ast.Call) and isinstance(node.value.args[0], ast.Set):
            return frozenset(
                element.value
                for element in node.value.args[0].elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            )
    raise AssertionError("could not read SEMANTIC_KINDS from semantic_work.py")


def _load_offset_module():
    spec = importlib.util.spec_from_file_location("course_compiler_offset_helper", OFFSET_HELPER)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load offset helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module




class SourceTextOffsetHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_offset_module()

    def test_unique_anchor_returns_python_unicode_code_point_offset(self) -> None:
        text = "α🙂 lecture anchor β"
        offset, count = self.module.find_source_text_offset(text, "lecture")
        self.assertEqual(offset, text.index("lecture"))
        self.assertEqual(count, 1)
        self.assertEqual(text[offset : offset + len("lecture")], "lecture")

    def test_missing_anchor_is_rejected(self) -> None:
        with self.assertRaisesRegex(self.module.OffsetError, "anchor_not_found"):
            self.module.find_source_text_offset("abc", "xyz")

    def test_ambiguous_anchor_is_rejected_without_occurrence(self) -> None:
        with self.assertRaisesRegex(self.module.OffsetError, "anchor_ambiguous"):
            self.module.find_source_text_offset("same -- same", "same")

    def test_explicit_one_based_occurrence_selects_exact_match(self) -> None:
        text = "same -- same -- same"
        offset, count = self.module.find_source_text_offset(text, "same", occurrence=2)
        self.assertEqual(offset, text.index("same", 1))
        self.assertEqual(count, 3)

    def test_helper_does_not_normalize_source_or_anchor(self) -> None:
        decomposed = "Café"
        composed = "Café"
        with self.assertRaisesRegex(self.module.OffsetError, "anchor_not_found"):
            self.module.find_source_text_offset(decomposed, composed)

    def test_cli_returns_content_free_error_for_ambiguous_anchor(self) -> None:
        result = subprocess.run(
            [sys.executable, str(OFFSET_HELPER)],
            input=json.dumps({"source_text": "x x", "anchor": "x"}),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            json.loads(result.stdout),
            {"status": "error", "code": "anchor_ambiguous"},
        )
        self.assertNotIn("x x", result.stdout)

    def test_cli_rejects_explicit_null_occurrence(self) -> None:
        result = subprocess.run(
            [sys.executable, str(OFFSET_HELPER)],
            input=json.dumps({"source_text": "abc", "anchor": "b", "occurrence": None}),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            json.loads(result.stdout),
            {"status": "error", "code": "occurrence_must_be_positive_integer"},
        )

    def test_cli_rejects_unknown_fields(self) -> None:
        result = subprocess.run(
            [sys.executable, str(OFFSET_HELPER)],
            input=json.dumps({"source_text": "abc", "anchor": "b", "extra": True}),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            json.loads(result.stdout),
            {"status": "error", "code": "request_has_unknown_fields"},
        )


if __name__ == "__main__":
    unittest.main()
