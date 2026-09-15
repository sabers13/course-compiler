from __future__ import annotations

import ast
import hashlib
import tempfile
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from unittest import mock

import course_compiler
import course_compiler.source_operations as source_operations
from course_compiler import (
    LocalSourceEvidenceStore,
    SourceEvidenceIngestionDiagnostic,
    SourceEvidenceIngestionFailure,
    SourceEvidencePayload,
    ingest_source_evidence,
    open_source_evidence_store,
)
from course_compiler import source_persistence
from course_compiler.workflow import SOURCE_EVIDENCE_REFERENCE_VERSION


PRIVATE_MARKER = "invented-private-source-ingestion-marker"


def failure_code(result: object) -> str:
    if type(result) is not SourceEvidenceIngestionFailure:
        raise AssertionError("source ingestion failure required")
    return result.diagnostics[0].code


class IngestionSentinel(BaseException):
    pass


class StringSubclass(str):
    pass


class BytesSubclass(bytes):
    pass


class StoreSubclass(LocalSourceEvidenceStore):
    pass


class SourceEvidenceIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="source-ingestion-")
        self.store = open_source_evidence_store(
            Path(self._temporary.name) / "source-evidence.sqlite"
        )
        assert type(self.store) is LocalSourceEvidenceStore

    def tearDown(self) -> None:
        self.store.close()
        self._temporary.cleanup()

    def test_public_exports_and_failure_contract_are_fixed_immutable(self) -> None:
        self.assertEqual(
            [item.name for item in fields(SourceEvidenceIngestionDiagnostic)],
            ["code", "classification", "message"],
        )
        self.assertEqual(
            [item.name for item in fields(SourceEvidenceIngestionFailure)],
            ["status", "diagnostics"],
        )
        self.assertTrue(
            {
                "SourceEvidenceIngestionDiagnostic",
                "SourceEvidenceIngestionFailure",
                "SourceEvidenceIngestionResult",
                "ingest_source_evidence",
            }
            <= set(course_compiler.__all__)
        )
        self.assertIn("ingest_source_evidence", source_operations.__all__)
        failure = source_operations._failure("source_store_failed")
        self.assertFalse(hasattr(failure, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            failure.status = "other"  # type: ignore[misc]
        with self.assertRaises(ValueError):
            SourceEvidenceIngestionDiagnostic("other", "input", "other")  # type: ignore[arg-type]
        forged = SourceEvidenceIngestionDiagnostic(
            "source_store_failed",
            "storage",
            "The local source-evidence store failed.",
        )
        object.__setattr__(forged, "message", PRIVATE_MARKER)
        with self.assertRaises(ValueError):
            SourceEvidenceIngestionFailure("source_ingestion_failed", (forged,))

    def test_first_ingestion_returns_t008_payload_for_exact_bytes(self) -> None:
        payload = b"\x00invented exact evidence\xff\r\n"
        result = ingest_source_evidence("source-one", payload, source_store=self.store)
        self.assertIsInstance(result, SourceEvidencePayload)
        assert type(result) is SourceEvidencePayload
        self.assertEqual(result.reference.reference_version, SOURCE_EVIDENCE_REFERENCE_VERSION)
        self.assertEqual(result.reference.source_id, "source-one")
        self.assertEqual(result.reference.content_sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(result.payload, payload)
        self.assertEqual(self.store.load(result.reference), result)

    def test_empty_bytes_succeed_without_special_case(self) -> None:
        result = ingest_source_evidence("empty-source", b"", source_store=self.store)
        self.assertIsInstance(result, SourceEvidencePayload)
        assert type(result) is SourceEvidencePayload
        self.assertEqual(result.payload, b"")
        self.assertEqual(result.reference.content_sha256, hashlib.sha256(b"").hexdigest())

    def test_same_id_and_exact_bytes_are_idempotent(self) -> None:
        payload = b"invented idempotent source"
        first = ingest_source_evidence("source-one", payload, source_store=self.store)
        second = ingest_source_evidence("source-one", payload, source_store=self.store)
        self.assertEqual(first, second)
        self.assertIsInstance(second, SourceEvidencePayload)

    def test_same_id_and_different_bytes_maps_to_fixed_conflict(self) -> None:
        self.assertIsInstance(
            ingest_source_evidence("source-one", b"invented original", source_store=self.store),
            SourceEvidencePayload,
        )
        result = ingest_source_evidence(
            "source-one", b"invented replacement", source_store=self.store
        )
        self.assertEqual(failure_code(result), "source_identity_conflict")
        self.assertNotIn("source-one", repr(result))
        self.assertNotIn("replacement", repr(result))

    def test_invalid_exact_string_id_maps_to_fixed_input_failure_before_save(self) -> None:
        with mock.patch.object(LocalSourceEvidenceStore, "save") as save:
            result = ingest_source_evidence("invalid/path", b"invented", source_store=self.store)
        self.assertEqual(failure_code(result), "invalid_source_ingestion_input")
        save.assert_not_called()

    def test_exact_runtime_type_misuse_raises_before_save(self) -> None:
        cases = (
            (123, b"invented", self.store),
            (StringSubclass("source-one"), b"invented", self.store),
            ("source-one", "invented", self.store),
            ("source-one", BytesSubclass(b"invented"), self.store),
            ("source-one", bytearray(b"invented"), self.store),
            ("source-one", memoryview(b"invented"), self.store),
            ("source-one", b"invented", object()),
            ("source-one", b"invented", object.__new__(StoreSubclass)),
        )
        for source_id, payload, source_store in cases:
            with self.subTest(value_type=type(source_id if source_id != "source-one" else payload).__name__), mock.patch.object(
                LocalSourceEvidenceStore, "save"
            ) as save:
                with self.assertRaises(TypeError):
                    ingest_source_evidence(source_id, payload, source_store=source_store)  # type: ignore[arg-type]
                save.assert_not_called()

    def test_valid_ingestion_invokes_t008_save_exactly_once(self) -> None:
        payload = b"invented one save"
        original_save = self.store.save
        with mock.patch.object(
            LocalSourceEvidenceStore,
            "save",
            autospec=True,
            side_effect=lambda _store, reference, saved_payload: original_save(
                reference, saved_payload
            ),
        ) as save:
            result = ingest_source_evidence("source-one", payload, source_store=self.store)
        self.assertIsInstance(result, SourceEvidencePayload)
        save.assert_called_once()
        reference, saved_payload = save.call_args.args[1:]
        self.assertEqual(reference.source_id, "source-one")
        self.assertEqual(saved_payload, payload)

    def test_expected_t008_failure_maps_to_store_failure_and_preserves_redaction(self) -> None:
        lower_failure = source_persistence._failure("unsupported_storage_schema")
        with mock.patch.object(LocalSourceEvidenceStore, "save", return_value=lower_failure):
            result = ingest_source_evidence("source-one", b"invented", source_store=self.store)
        self.assertEqual(failure_code(result), "source_store_failed")
        self.assertNotIn(lower_failure.diagnostics[0].message, repr(result))
        self.assertNotIn("source-one", repr(result))

    def test_unexpected_exception_is_contained_and_redacted(self) -> None:
        with mock.patch.object(
            LocalSourceEvidenceStore,
            "save",
            side_effect=RuntimeError(f"{PRIVATE_MARKER} /private/evidence.sqlite"),
        ):
            result = ingest_source_evidence("source-one", b"invented", source_store=self.store)
        self.assertEqual(failure_code(result), "source_ingestion_exception")
        self.assertNotIn(PRIVATE_MARKER, repr(result))
        self.assertNotIn("/private/evidence.sqlite", repr(result))

    def test_baseexception_propagates(self) -> None:
        with mock.patch.object(LocalSourceEvidenceStore, "save", side_effect=IngestionSentinel):
            with self.assertRaises(IngestionSentinel):
                ingest_source_evidence("source-one", b"invented", source_store=self.store)

    def test_caller_owned_store_remains_usable(self) -> None:
        result = ingest_source_evidence("source-one", b"invented", source_store=self.store)
        assert type(result) is SourceEvidencePayload
        self.assertEqual(self.store.load(result.reference), result)

    def test_boundary_has_no_filesystem_subprocess_or_transport_calls(self) -> None:
        tree = ast.parse(Path(source_operations.__file__).read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertFalse(imports & {"os", "pathlib", "socket", "subprocess"})
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertFalse(calls & {"open", "connect", "run", "Popen"})
