from __future__ import annotations

import ast
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import course_compiler
from course_compiler import (
    ASSET_REFERENCE_VERSION,
    VISUAL_PLACEMENT_VERSION,
    AssetReference,
    DocumentReference,
    VisualPlacement,
)


class HostileStr(str):
    def _raise(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError("hostile caller value must not leak")

    __eq__ = _raise
    __ne__ = _raise
    __str__ = _raise
    __repr__ = _raise


class HostileInt(int):
    """An int subclass, distinct from the exact built-in `int` type."""


class DocumentReferenceSubclass(DocumentReference):
    pass


class AssetReferenceSubclass(AssetReference):
    pass


def document_reference(
    document_id: str = "l1",
    order: int = 1,
    content_sha256: str = "1" * 64,
) -> DocumentReference:
    return DocumentReference("lecture-document/v1", document_id, order, content_sha256)


def asset_reference(content_sha256: str = "2" * 64) -> AssetReference:
    return AssetReference(ASSET_REFERENCE_VERSION, content_sha256)


def placement(
    *,
    document: DocumentReference | None = None,
    offset: int = 0,
    asset: AssetReference | None = None,
) -> VisualPlacement:
    return VisualPlacement(
        VISUAL_PLACEMENT_VERSION,
        document if document is not None else document_reference(),
        offset,
        asset if asset is not None else asset_reference(),
    )


class VisualPlacementTests(unittest.TestCase):
    # 1, 2, 3, 4, 5: version constant, field order, frozen, slotted, hashable.
    def test_public_shape_is_exact_frozen_slotted_and_hashable(self) -> None:
        self.assertEqual(VISUAL_PLACEMENT_VERSION, "visual-placement/v1")
        self.assertEqual(
            [item.name for item in fields(VisualPlacement)],
            [
                "placement_version",
                "document_reference",
                "source_text_offset",
                "asset_reference",
            ],
        )
        value = placement()
        self.assertFalse(hasattr(value, "__dict__"))
        self.assertIsInstance(hash(value), int)
        with self.assertRaises(FrozenInstanceError):
            value.source_text_offset = 5  # type: ignore[misc]

    # 6, 7: supported version accepted / unsupported version rejected.
    def test_only_exact_supported_version_is_accepted(self) -> None:
        self.assertEqual(placement().placement_version, VISUAL_PLACEMENT_VERSION)
        for value in (
            "visual-placement/v2",
            "",
            b"visual-placement/v1",
            None,
            1,
            HostileStr(VISUAL_PLACEMENT_VERSION),
        ):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(
                    ValueError, "^visual placement version is unsupported$"
                ) as caught:
                    VisualPlacement(
                        value,  # type: ignore[arg-type]
                        document_reference(),
                        0,
                        asset_reference(),
                    )
                self.assertNotIn("hostile caller value", str(caught.exception))

    # 8, 9, 10, 11: valid document reference / invalid type / forged revalidation / fixed message.
    def test_document_reference_is_exact_and_revalidated(self) -> None:
        exact = document_reference()
        self.assertIs(placement(document=exact).document_reference, exact)

        subclass = DocumentReferenceSubclass("lecture-document/v1", "l1", 1, "1" * 64)
        forged_bad_id = object.__new__(DocumentReference)
        object.__setattr__(forged_bad_id, "contract_version", "lecture-document/v1")
        object.__setattr__(forged_bad_id, "document_id", "bad/path")
        object.__setattr__(forged_bad_id, "order", 1)
        object.__setattr__(forged_bad_id, "content_sha256", "1" * 64)
        forged_bad_version = object.__new__(DocumentReference)
        object.__setattr__(forged_bad_version, "contract_version", "lecture-document/v2")
        object.__setattr__(forged_bad_version, "document_id", "l1")
        object.__setattr__(forged_bad_version, "order", 1)
        object.__setattr__(forged_bad_version, "content_sha256", "1" * 64)
        invalid_values = (
            None,
            "l1",
            object(),
            subclass,
            forged_bad_id,
            forged_bad_version,
        )
        for value in invalid_values:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(
                    ValueError, "^visual placement document reference is invalid$"
                ) as caught:
                    VisualPlacement(
                        VISUAL_PLACEMENT_VERSION,
                        value,  # type: ignore[arg-type]
                        0,
                        asset_reference(),
                    )
                self.assertNotIn("bad/path", str(caught.exception))
                self.assertNotIn("hostile caller value", str(caught.exception))

    # 12, 13, 14: zero offset / positive offset / negative offset rejected.
    # 15, 16, 17, 18, 19: True / False / float / string / int-subclass rejected.
    def test_source_text_offset_requires_exact_non_negative_builtin_int(self) -> None:
        for offset in (0, 1, 2, 500, 10**9):
            with self.subTest(offset=offset):
                self.assertEqual(placement(offset=offset).source_text_offset, offset)
        for offset in (-1, -500, True, False, 1.0, 0.0, "0", "3", None, HostileInt(3)):
            with self.subTest(offset=repr(offset), offset_type=type(offset).__name__):
                with self.assertRaisesRegex(
                    ValueError, "^visual placement source-text offset is invalid$"
                ):
                    VisualPlacement(
                        VISUAL_PLACEMENT_VERSION,
                        document_reference(),
                        offset,  # type: ignore[arg-type]
                        asset_reference(),
                    )

    # 20, 21, 22, 23: valid asset reference / invalid type / forged revalidation / fixed message.
    def test_asset_reference_is_exact_and_revalidated(self) -> None:
        exact = asset_reference()
        self.assertIs(placement(asset=exact).asset_reference, exact)

        subclass = AssetReferenceSubclass(ASSET_REFERENCE_VERSION, "2" * 64)
        forged_bad_digest = object.__new__(AssetReference)
        object.__setattr__(forged_bad_digest, "reference_version", ASSET_REFERENCE_VERSION)
        object.__setattr__(forged_bad_digest, "content_sha256", "not-a-digest")
        forged_bad_version = object.__new__(AssetReference)
        object.__setattr__(forged_bad_version, "reference_version", "asset-reference/v2")
        object.__setattr__(forged_bad_version, "content_sha256", "2" * 64)
        invalid_values = (
            None,
            "2" * 64,
            object(),
            subclass,
            forged_bad_digest,
            forged_bad_version,
        )
        for value in invalid_values:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(
                    ValueError, "^visual placement asset reference is invalid$"
                ) as caught:
                    VisualPlacement(
                        VISUAL_PLACEMENT_VERSION,
                        document_reference(),
                        0,
                        value,  # type: ignore[arg-type]
                    )
                self.assertNotIn("not-a-digest", str(caught.exception))
                self.assertNotIn("hostile caller value", str(caught.exception))

    # 24, 25, 26, 27, 28: equality/hash across identical and differing fields.
    def test_equality_and_hash_track_the_complete_four_field_value(self) -> None:
        first = placement(document=document_reference("l1"), offset=3, asset=asset_reference("2" * 64))
        same = placement(document=document_reference("l1"), offset=3, asset=asset_reference("2" * 64))
        different_document = placement(
            document=document_reference("l2"), offset=3, asset=asset_reference("2" * 64)
        )
        different_offset = placement(
            document=document_reference("l1"), offset=4, asset=asset_reference("2" * 64)
        )
        different_asset = placement(
            document=document_reference("l1"), offset=3, asset=asset_reference("3" * 64)
        )
        self.assertEqual(first, same)
        self.assertEqual(hash(first), hash(same))
        self.assertNotEqual(first, different_document)
        self.assertNotEqual(first, different_offset)
        self.assertNotEqual(first, different_asset)

    # 29, 30: same asset at different offsets; different assets share one offset.
    def test_placements_have_no_uniqueness_or_ordering_constraint(self) -> None:
        asset = asset_reference("4" * 64)
        first = placement(offset=0, asset=asset)
        second = placement(offset=10, asset=asset)
        self.assertEqual(first.asset_reference, second.asset_reference)
        self.assertNotEqual(first, second)

        shared_offset = 7
        left = placement(offset=shared_offset, asset=asset_reference("5" * 64))
        right = placement(offset=shared_offset, asset=asset_reference("6" * 64))
        self.assertEqual(left.source_text_offset, right.source_text_offset)
        self.assertNotEqual(left, right)

        with self.assertRaises(TypeError):
            first < second  # type: ignore[operator]

    # 31, 32, 33: no payload / provenance / layout fields.
    def test_no_payload_provenance_or_layout_fields_exist(self) -> None:
        value = placement()
        forbidden_names = (
            # payload exclusion
            "content_bytes",
            "bytes",
            "bytearray",
            "payload",
            "png_bytes",
            "asset_payload",
            "path",
            "filename",
            "mime",
            "format",
            "extension",
            "width_px",
            "height_px",
            # extraction-provenance exclusion
            "page_reference",
            "physical_page_ordinal",
            "extraction_profile",
            "left_px",
            "top_px",
            "source_evidence",
            # layout/presentation exclusion
            "width",
            "height",
            "scale",
            "caption",
            "alt_text",
            "alignment",
            "figure_number",
            # identity/serialization exclusion
            "placement_id",
            "id",
            "uuid",
            "digest",
        )
        for name in forbidden_names:
            with self.subTest(name=name):
                self.assertFalse(hasattr(value, name))

    # 34: public package exports.
    def test_public_package_exports_placement_symbols(self) -> None:
        self.assertIs(course_compiler.VisualPlacement, VisualPlacement)
        self.assertEqual(
            course_compiler.VISUAL_PLACEMENT_VERSION, VISUAL_PLACEMENT_VERSION
        )
        self.assertIn("VisualPlacement", course_compiler.__all__)
        self.assertIn("VISUAL_PLACEMENT_VERSION", course_compiler.__all__)

    # 35, 36, 37, 38, 39, 40, 41: dependency direction and absent capabilities.
    def test_domain_module_has_only_approved_dependencies_and_no_io(self) -> None:
        module_path = (
            Path(__file__).resolve().parents[1]
            / "course_compiler"
            / "visual_placement.py"
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
            [".asset", ".rendering", "__future__", "dataclasses", "typing"],
        )
        self.assertTrue(
            call_names.isdisjoint({"__import__", "compile", "eval", "exec", "open"})
        )
        source = module_path.read_text(encoding="utf-8").lower()
        for forbidden in (
            "sqlite",
            "subprocess",
            "pathlib",
            "os.path",
            "tempfile",
            "legacy_renderer",
            "compilation",
            "assembly",
            "course_workflow",
            "workflow_persistence",
            "pdf_visual_extraction",
        ):
            self.assertNotIn(forbidden, source)

    def test_lower_accepted_modules_do_not_import_visual_placement(self) -> None:
        root = Path(__file__).resolve().parents[1] / "course_compiler"
        for lower_name in ("asset.py", "rendering.py", "pdf_visual_extraction.py"):
            with self.subTest(module=lower_name):
                source = (root / lower_name).read_text(encoding="utf-8")
                self.assertNotIn("visual_placement", source)

    # 42: fixed failure messages do not expose adversarial custom object content.
    def test_fixed_failure_messages_never_interpolate_caller_values(self) -> None:
        class Loud:
            def __repr__(self) -> str:
                return "LOUD-SECRET-PAYLOAD"

            def __str__(self) -> str:
                return "LOUD-SECRET-PAYLOAD"

        for args in (
            (Loud(), document_reference(), 0, asset_reference()),
            (VISUAL_PLACEMENT_VERSION, Loud(), 0, asset_reference()),
            (VISUAL_PLACEMENT_VERSION, document_reference(), Loud(), asset_reference()),
            (VISUAL_PLACEMENT_VERSION, document_reference(), 0, Loud()),
        ):
            with self.subTest(args=args):
                with self.assertRaises(ValueError) as caught:
                    VisualPlacement(*args)  # type: ignore[arg-type]
                self.assertNotIn("LOUD-SECRET-PAYLOAD", str(caught.exception))

    # 43: BaseException behavior follows accepted defensive-validation conventions.
    def test_base_exception_from_forged_nested_values_propagates(self) -> None:
        class StopValidation(BaseException):
            pass

        class HostileVersion(str):
            def _raise(self, *args: object, **kwargs: object) -> object:
                raise StopValidation("must not be swallowed")

            __eq__ = _raise
            __ne__ = _raise

        # An exact (non-subclass) forged DocumentReference whose version
        # comparison raises a direct BaseException subclass during
        # constructor revalidation. `_valid_document_reference` contains
        # ordinary `Exception` only; it does not catch `BaseException`
        # itself, so this must propagate rather than being reported as an
        # ordinary invalid-reference failure.
        forged_document = object.__new__(DocumentReference)
        object.__setattr__(
            forged_document, "contract_version", HostileVersion("lecture-document/v1")
        )
        object.__setattr__(forged_document, "document_id", "l1")
        object.__setattr__(forged_document, "order", 1)
        object.__setattr__(forged_document, "content_sha256", "1" * 64)
        self.assertIs(type(forged_document), DocumentReference)
        with self.assertRaises(StopValidation):
            VisualPlacement(
                VISUAL_PLACEMENT_VERSION,
                forged_document,
                0,
                asset_reference(),
            )

    # Regression: an ordinary Exception raised while revalidating a forged
    # nested DocumentReference must be contained as the fixed redacted
    # ValueError, not leaked to the caller. The accepted T002
    # `DocumentReference` validates `document_id` with an `isinstance`-based
    # membership check (`value not in (".", "..")`), so a hostile `str`
    # subclass whose `__eq__`/`__ne__` raises an ordinary exception can
    # otherwise escape constructor revalidation with caller-controlled
    # content.
    def test_ordinary_exception_from_forged_document_reference_is_redacted(self) -> None:
        class HostileDocumentId(str):
            def _raise(self, *args: object, **kwargs: object) -> object:
                raise RuntimeError("LOUD-NESTED-DOCUMENT-SECRET")

            __eq__ = _raise
            __ne__ = _raise

        forged_document = object.__new__(DocumentReference)
        object.__setattr__(forged_document, "contract_version", "lecture-document/v1")
        object.__setattr__(
            forged_document, "document_id", HostileDocumentId("l1")
        )
        object.__setattr__(forged_document, "order", 1)
        object.__setattr__(forged_document, "content_sha256", "1" * 64)
        self.assertIs(type(forged_document), DocumentReference)

        with self.assertRaises(ValueError) as caught:
            VisualPlacement(
                VISUAL_PLACEMENT_VERSION,
                forged_document,
                0,
                asset_reference(),
            )
        self.assertEqual(
            str(caught.exception), "visual placement document reference is invalid"
        )
        self.assertNotIn("LOUD-NESTED-DOCUMENT-SECRET", str(caught.exception))
        self.assertNotIsInstance(caught.exception, RuntimeError)


if __name__ == "__main__":
    unittest.main()
