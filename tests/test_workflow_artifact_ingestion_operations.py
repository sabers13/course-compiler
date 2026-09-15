from __future__ import annotations

import ast
import hashlib
import tempfile
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from unittest import mock

import course_compiler
import course_compiler.workflow_artifact_operations as workflow_artifact_operations
from course_compiler import (
    LocalWorkflowArtifactStore,
    WorkflowArtifactIngestionDiagnostic,
    WorkflowArtifactIngestionFailure,
    WorkflowArtifactPayload,
    ingest_workflow_artifact,
    open_workflow_artifact_store,
)
from course_compiler import workflow_artifact_persistence
from course_compiler.workflow import (
    ARTIFACT_PRODUCERS,
    WORKFLOW_ARTIFACT_REFERENCE_VERSION,
)


PRIVATE_MARKER = "invented-private-workflow-artifact-ingestion-marker"


def failure_code(result: object) -> str:
    if type(result) is not WorkflowArtifactIngestionFailure:
        raise AssertionError("workflow artifact ingestion failure required")
    return result.diagnostics[0].code


class IngestionSentinel(BaseException):
    pass


class StringSubclass(str):
    pass


class BytesSubclass(bytes):
    pass


class StoreSubclass(LocalWorkflowArtifactStore):
    pass


class WorkflowArtifactIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="artifact-ingestion-")
        self.store = open_workflow_artifact_store(
            Path(self._temporary.name) / "workflow-artifact.sqlite"
        )
        assert type(self.store) is LocalWorkflowArtifactStore

    def tearDown(self) -> None:
        self.store.close()
        self._temporary.cleanup()

    def test_public_exports_and_failure_contract_are_fixed_immutable(self) -> None:
        self.assertEqual(
            [item.name for item in fields(WorkflowArtifactIngestionDiagnostic)],
            ["code", "classification", "message"],
        )
        self.assertEqual(
            [item.name for item in fields(WorkflowArtifactIngestionFailure)],
            ["status", "diagnostics"],
        )
        self.assertTrue(
            {
                "WorkflowArtifactIngestionDiagnostic",
                "WorkflowArtifactIngestionFailure",
                "WorkflowArtifactIngestionResult",
                "ingest_workflow_artifact",
            }
            <= set(course_compiler.__all__)
        )
        self.assertIn("ingest_workflow_artifact", workflow_artifact_operations.__all__)
        failure = workflow_artifact_operations._failure("workflow_artifact_store_failed")
        self.assertFalse(hasattr(failure, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            failure.status = "other"  # type: ignore[misc]
        with self.assertRaises(ValueError):
            WorkflowArtifactIngestionDiagnostic("other", "input", "other")  # type: ignore[arg-type]
        forged = WorkflowArtifactIngestionDiagnostic(
            "workflow_artifact_store_failed",
            "storage",
            "The local workflow-artifact store failed.",
        )
        object.__setattr__(forged, "message", PRIVATE_MARKER)
        with self.assertRaises(ValueError):
            WorkflowArtifactIngestionFailure("workflow_artifact_ingestion_failed", (forged,))

    def test_first_ingestion_returns_t009_payload_for_exact_bytes(self) -> None:
        payload = b"\x00invented exact artifact\xff\r\n"
        result = ingest_workflow_artifact(
            "artifact-one", "source_assessment", payload, artifact_store=self.store
        )
        self.assertIsInstance(result, WorkflowArtifactPayload)
        assert type(result) is WorkflowArtifactPayload
        self.assertEqual(result.reference.reference_version, WORKFLOW_ARTIFACT_REFERENCE_VERSION)
        self.assertEqual(result.reference.artifact_id, "artifact-one")
        self.assertEqual(result.reference.artifact_kind, "source_assessment")
        self.assertEqual(result.reference.content_sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(
            result.reference.producer_version, ARTIFACT_PRODUCERS["source_assessment"]
        )
        self.assertEqual(result.payload, payload)
        self.assertEqual(self.store.load(result.reference), result)

    def test_producer_version_is_derived_for_every_registered_kind(self) -> None:
        for kind, producer in ARTIFACT_PRODUCERS.items():
            with self.subTest(kind=kind):
                result = ingest_workflow_artifact(
                    f"artifact-{kind}", kind, f"bytes for {kind}".encode("utf-8"),
                    artifact_store=self.store,
                )
                self.assertIsInstance(result, WorkflowArtifactPayload)
                assert type(result) is WorkflowArtifactPayload
                self.assertEqual(result.reference.producer_version, producer)

    def test_empty_bytes_succeed_without_special_case(self) -> None:
        result = ingest_workflow_artifact(
            "empty-artifact", "lecture_map", b"", artifact_store=self.store
        )
        self.assertIsInstance(result, WorkflowArtifactPayload)
        assert type(result) is WorkflowArtifactPayload
        self.assertEqual(result.payload, b"")
        self.assertEqual(result.reference.content_sha256, hashlib.sha256(b"").hexdigest())

    def test_same_identity_and_exact_bytes_are_idempotent(self) -> None:
        payload = b"invented idempotent artifact"
        first = ingest_workflow_artifact(
            "artifact-one", "validation", payload, artifact_store=self.store
        )
        second = ingest_workflow_artifact(
            "artifact-one", "validation", payload, artifact_store=self.store
        )
        self.assertEqual(first, second)
        self.assertIsInstance(second, WorkflowArtifactPayload)

    def test_same_id_and_different_bytes_maps_to_fixed_conflict(self) -> None:
        self.assertIsInstance(
            ingest_workflow_artifact(
                "artifact-one", "lecture_map", b"invented original", artifact_store=self.store
            ),
            WorkflowArtifactPayload,
        )
        result = ingest_workflow_artifact(
            "artifact-one", "lecture_map", b"invented replacement", artifact_store=self.store
        )
        self.assertEqual(failure_code(result), "workflow_artifact_identity_conflict")
        self.assertNotIn("artifact-one", repr(result))
        self.assertNotIn("replacement", repr(result))

    def test_same_id_and_different_kind_maps_to_fixed_conflict(self) -> None:
        payload = b"invented shared bytes"
        self.assertIsInstance(
            ingest_workflow_artifact(
                "artifact-one", "lecture_map", payload, artifact_store=self.store
            ),
            WorkflowArtifactPayload,
        )
        result = ingest_workflow_artifact(
            "artifact-one", "validation", payload, artifact_store=self.store
        )
        self.assertEqual(failure_code(result), "workflow_artifact_identity_conflict")

    def test_invalid_exact_string_id_maps_to_fixed_input_failure_before_save(self) -> None:
        with mock.patch.object(LocalWorkflowArtifactStore, "save") as save:
            result = ingest_workflow_artifact(
                "invalid/path", "lecture_map", b"invented", artifact_store=self.store
            )
        self.assertEqual(failure_code(result), "invalid_workflow_artifact_ingestion_input")
        save.assert_not_called()

    def test_unregistered_artifact_kind_maps_to_fixed_input_failure_before_save(self) -> None:
        with mock.patch.object(LocalWorkflowArtifactStore, "save") as save:
            result = ingest_workflow_artifact(
                "artifact-one", "not_a_real_kind", b"invented", artifact_store=self.store
            )
        self.assertEqual(failure_code(result), "invalid_workflow_artifact_ingestion_input")
        save.assert_not_called()

    def test_exact_runtime_type_misuse_raises_before_save(self) -> None:
        cases = (
            (123, "lecture_map", b"invented", self.store),
            (StringSubclass("artifact-one"), "lecture_map", b"invented", self.store),
            ("artifact-one", 123, b"invented", self.store),
            ("artifact-one", StringSubclass("lecture_map"), b"invented", self.store),
            ("artifact-one", "lecture_map", "invented", self.store),
            ("artifact-one", "lecture_map", BytesSubclass(b"invented"), self.store),
            ("artifact-one", "lecture_map", bytearray(b"invented"), self.store),
            ("artifact-one", "lecture_map", memoryview(b"invented"), self.store),
            ("artifact-one", "lecture_map", b"invented", object()),
            ("artifact-one", "lecture_map", b"invented", object.__new__(StoreSubclass)),
        )
        for artifact_id, artifact_kind, payload, artifact_store in cases:
            with self.subTest(
                value_type=type(
                    artifact_id if artifact_id != "artifact-one" else
                    (artifact_kind if artifact_kind != "lecture_map" else payload)
                ).__name__
            ), mock.patch.object(LocalWorkflowArtifactStore, "save") as save:
                with self.assertRaises(TypeError):
                    ingest_workflow_artifact(  # type: ignore[arg-type]
                        artifact_id, artifact_kind, payload, artifact_store=artifact_store
                    )
                save.assert_not_called()

    def test_valid_ingestion_invokes_t009_save_exactly_once(self) -> None:
        payload = b"invented one save"
        original_save = self.store.save
        with mock.patch.object(
            LocalWorkflowArtifactStore,
            "save",
            autospec=True,
            side_effect=lambda _store, reference, saved_payload: original_save(
                reference, saved_payload
            ),
        ) as save:
            result = ingest_workflow_artifact(
                "artifact-one", "lecture_map", payload, artifact_store=self.store
            )
        self.assertIsInstance(result, WorkflowArtifactPayload)
        save.assert_called_once()
        reference, saved_payload = save.call_args.args[1:]
        self.assertEqual(reference.artifact_id, "artifact-one")
        self.assertEqual(saved_payload, payload)

    def test_expected_t009_failure_maps_to_store_failure_and_preserves_redaction(self) -> None:
        lower_failure = workflow_artifact_persistence._failure("unsupported_storage_schema")
        with mock.patch.object(LocalWorkflowArtifactStore, "save", return_value=lower_failure):
            result = ingest_workflow_artifact(
                "artifact-one", "lecture_map", b"invented", artifact_store=self.store
            )
        self.assertEqual(failure_code(result), "workflow_artifact_store_failed")
        self.assertNotIn(lower_failure.diagnostics[0].message, repr(result))
        self.assertNotIn("artifact-one", repr(result))

    def test_unexpected_exception_is_contained_and_redacted(self) -> None:
        with mock.patch.object(
            LocalWorkflowArtifactStore,
            "save",
            side_effect=RuntimeError(f"{PRIVATE_MARKER} /private/artifacts.sqlite"),
        ):
            result = ingest_workflow_artifact(
                "artifact-one", "lecture_map", b"invented", artifact_store=self.store
            )
        self.assertEqual(failure_code(result), "workflow_artifact_ingestion_exception")
        self.assertNotIn(PRIVATE_MARKER, repr(result))
        self.assertNotIn("/private/artifacts.sqlite", repr(result))

    def test_baseexception_propagates(self) -> None:
        with mock.patch.object(
            LocalWorkflowArtifactStore, "save", side_effect=IngestionSentinel
        ):
            with self.assertRaises(IngestionSentinel):
                ingest_workflow_artifact(
                    "artifact-one", "lecture_map", b"invented", artifact_store=self.store
                )

    def test_caller_owned_store_remains_usable(self) -> None:
        result = ingest_workflow_artifact(
            "artifact-one", "lecture_map", b"invented", artifact_store=self.store
        )
        assert type(result) is WorkflowArtifactPayload
        self.assertEqual(self.store.load(result.reference), result)

    def test_boundary_has_no_filesystem_subprocess_or_transport_calls(self) -> None:
        tree = ast.parse(
            Path(workflow_artifact_operations.__file__).read_text(encoding="utf-8")
        )
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
