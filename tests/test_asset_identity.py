from __future__ import annotations

import ast
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import course_compiler
from course_compiler import ASSET_REFERENCE_VERSION, AssetReference


class HostileStr(str):
    def _raise(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError("hostile caller value must not be used")

    __eq__ = _raise
    __ne__ = _raise
    __str__ = _raise
    __repr__ = _raise


class AssetIdentityTests(unittest.TestCase):
    def test_public_shape_is_exact_frozen_slotted_and_hashable(self) -> None:
        self.assertEqual(ASSET_REFERENCE_VERSION, "asset-reference/v1")
        self.assertEqual(
            [item.name for item in fields(AssetReference)],
            ["reference_version", "content_sha256"],
        )
        reference = AssetReference(ASSET_REFERENCE_VERSION, "1" * 64)
        self.assertFalse(hasattr(reference, "__dict__"))
        self.assertIsInstance(hash(reference), int)
        with self.assertRaises(FrozenInstanceError):
            reference.content_sha256 = "2" * 64  # type: ignore[misc]
        for name in (
            "asset_id",
            "source_reference",
            "page_reference",
            "mime",
            "format",
            "payload",
        ):
            self.assertFalse(hasattr(reference, name))

    def test_only_exact_supported_version_is_accepted(self) -> None:
        self.assertEqual(
            AssetReference("asset-reference/v1", "1" * 64).reference_version,
            ASSET_REFERENCE_VERSION,
        )
        for value in ("asset-reference/v2", "", b"asset-reference/v1", None, 1, HostileStr(ASSET_REFERENCE_VERSION)):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(
                    ValueError, "^asset reference version is unsupported$"
                ) as caught:
                    AssetReference(value, "1" * 64)  # type: ignore[arg-type]
                self.assertNotIn("hostile caller value", str(caught.exception))

    def test_exact_lowercase_sha256_digest_is_required(self) -> None:
        valid = "0123456789abcdef" * 4
        self.assertEqual(
            AssetReference(ASSET_REFERENCE_VERSION, valid).content_sha256, valid
        )
        invalid = (
            "1" * 63,
            "1" * 65,
            "A" * 64,
            "g" * 64,
            " " + "1" * 64,
            "1" * 64 + " ",
            "",
            b"1" * 64,
            None,
            1,
            object(),
            HostileStr("1" * 64),
        )
        for value in invalid:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(
                    ValueError, "^asset content digest is invalid$"
                ) as caught:
                    AssetReference(ASSET_REFERENCE_VERSION, value)  # type: ignore[arg-type]
                self.assertNotIn("hostile caller value", str(caught.exception))

    def test_complete_identity_is_the_version_and_exact_byte_digest(self) -> None:
        # Different extraction occurrences with identical exact bytes share identity.
        first = AssetReference(ASSET_REFERENCE_VERSION, "1" * 64)
        same = AssetReference(ASSET_REFERENCE_VERSION, "1" * 64)
        different_bytes = AssetReference(ASSET_REFERENCE_VERSION, "2" * 64)
        self.assertEqual(first, same)
        self.assertEqual(hash(first), hash(same))
        self.assertNotEqual(first, different_bytes)

    def test_asset_reference_has_no_ordering_contract(self) -> None:
        first = AssetReference(ASSET_REFERENCE_VERSION, "1" * 64)
        second = AssetReference(ASSET_REFERENCE_VERSION, "2" * 64)
        with self.assertRaises(TypeError):
            first < second  # type: ignore[operator]

    def test_package_exports_public_contract(self) -> None:
        self.assertIs(course_compiler.AssetReference, AssetReference)
        self.assertEqual(course_compiler.ASSET_REFERENCE_VERSION, ASSET_REFERENCE_VERSION)
        self.assertIn("AssetReference", course_compiler.__all__)
        self.assertIn("ASSET_REFERENCE_VERSION", course_compiler.__all__)

    def test_asset_module_is_a_standard_library_leaf_without_io(self) -> None:
        module_path = Path(__file__).resolve().parents[1] / "course_compiler" / "asset.py"
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
        self.assertEqual(sorted(imports), ["__future__", "dataclasses", "re", "typing"])
        self.assertTrue(call_names.isdisjoint({"__import__", "compile", "eval", "exec", "open"}))


if __name__ == "__main__":
    unittest.main()
