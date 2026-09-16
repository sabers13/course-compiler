from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
import subprocess
import tempfile
import unittest
import urllib.request
import zlib
from pathlib import Path
from unittest import mock

from tests.toolchain_support import (
    HTTPCORE_MISSING_REASON,
    MCP_MISSING_REASON,
    POPPLER_AVAILABLE,
    POPPLER_MISSING_REASON,
    require_optional_packages,
)

require_optional_packages("mcp", reason=MCP_MISSING_REASON)
require_optional_packages("httpcore", reason=HTTPCORE_MISSING_REASON)

from mcp.types import CallToolResult, EmbeddedResource, ImageContent, TextContent

import course_compiler.mcp_adapter as adapter
import course_compiler.mcp_file_ingress as ingress
import course_compiler.course_workflow_operations as workflow_ops
from course_compiler.compilation import COMPILATION_IDENTITY, CompiledPdf
from course_compiler.mcp_adapter import (
    DEFAULT_MCP_HOST,
    DEFAULT_MCP_PATH,
    McpAdapterConfig,
    create_mcp_server,
)
from course_compiler.mcp_file_ingress import (
    FileDownloadPolicy,
    FileIngressFailure,
    OpenAIFileReference,
    download_openai_file,
)
from course_compiler.policy_persistence import (
    LocalPolicyContentStore,
    open_policy_content_store,
)
from course_compiler.workflow import (
    PolicyReference,
    WorkflowPolicySet,
    WorkflowState,
    candidate_subject_sha256,
    map_subject_sha256,
    priority_subject_sha256,
)
from course_compiler.workflow_persistence import (
    LocalWorkflowStateStore,
    open_workflow_state_store,
)
from course_compiler.workflow_policy import POLICY_VERSIONS


POLICY_SLOTS = (
    "source_assessment",
    "priority_basis",
    "lecture_mapping",
    "lecture_production",
    "lecture_validation",
    "workflow_handoff",
)
PRIVATE_URL = "https://files.example.test/private?token=invented-secret-token"
PRIVATE_BYTES = b"invented-private-source-marker"
PRIVATE_EXCEPTION = "invented-private-exception-marker"


def synthetic_vector_pdf() -> bytes:
    content = (
        b"0.90 0.95 1.00 rg 12 12 120 120 re f\n"
        b"0.10 0.30 0.70 RG 4 w 18 18 m 126 126 l S\n"
        b"0.80 0.20 0.10 rg 52 42 48 62 re f\n"
    )
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 180 180] "
            b"/CropBox [18 18 162 162] /Resources << >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n"
        + content
        + b"endstream",
    )
    output = bytearray(b"%PDF-1.4\n% invented MCP adapter fixture\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


def png_bytes(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + b"\x02\x03\x05" * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


def call(server: object, name: str, arguments: dict[str, object]) -> object:
    return asyncio.run(server.call_tool(name, arguments))


def structured(result: object) -> dict[str, object]:
    value = result.structured_content
    if type(value) is not dict:
        raise AssertionError(f"structured result required, got {type(value).__name__}")
    return value


class AdapterHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="mcp-adapter-")
        self.root = Path(self._temporary.name)
        self.policy_path = self.root / "policy.sqlite"
        policy_store = open_policy_content_store(self.policy_path)
        assert type(policy_store) is LocalPolicyContentStore
        references = []
        for kind in POLICY_SLOTS:
            payload = f"invented configured policy for {kind}".encode("utf-8")
            reference = PolicyReference(
                kind,
                POLICY_VERSIONS[kind],
                hashlib.sha256(payload).hexdigest(),
            )
            saved = policy_store.save(reference, payload)
            assert getattr(saved, "reference", saved) == reference
            references.append(reference)
        policy_store.close()
        self.policies = WorkflowPolicySet(*references)
        self.config = McpAdapterConfig(
            association_database_path=self.root / "association.sqlite",
            workflow_database_path=self.root / "workflow.sqlite",
            policy_database_path=self.policy_path,
            source_database_path=self.root / "source.sqlite",
            artifact_database_path=self.root / "artifact.sqlite",
            document_database_path=self.root / "document.sqlite",
            workflow_policies=self.policies,
            file_download_policy=FileDownloadPolicy(("files.example.test",)),
        )
        self.server = create_mcp_server(self.config)
        self._artifact_counter = 0

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def call(self, name: str, **arguments: object) -> object:
        return call(self.server, name, arguments)

    def state(self) -> WorkflowState:
        store = open_workflow_state_store(self.config.workflow_database_path)
        assert type(store) is LocalWorkflowStateStore
        try:
            state = store.load("workflow-1")
            assert type(state) is WorkflowState, state
            return state
        finally:
            store.close()

    def ingest_source(self, payload: bytes) -> dict[str, object]:
        with mock.patch.object(adapter, "download_openai_file", return_value=payload):
            result = self.call(
                "ingest_course_source",
                source_id="source-1",
                file={
                    "download_url": PRIVATE_URL,
                    "file_id": "file-invented-1",
                    "mime_type": "application/pdf",
                    "file_name": "ignored-private-name.pdf",
                },
            )
        self.assertFalse(result.is_error)
        return structured(result)["source_reference"]  # type: ignore[return-value]

    def ingest_artifact(self, kind: str) -> dict[str, object]:
        self._artifact_counter += 1
        result = self.call(
            "ingest_workflow_artifact",
            artifact_id=f"artifact-{kind}-{self._artifact_counter}",
            artifact_kind=kind,
            content_utf8=f"invented GPT artifact for {kind}",
        )
        self.assertFalse(result.is_error)
        return structured(result)["artifact_reference"]  # type: ignore[return-value]

    def apply(
        self,
        action: str,
        payload: dict[str, object],
        operation_id: str,
        *,
        expected_revision: int | None,
    ) -> object:
        arguments: dict[str, object] = {
            "course_id": "course-1",
            "workflow_id": "workflow-1",
            "operation_id": operation_id,
            "action": action,
            "payload": payload,
        }
        if expected_revision is not None:
            arguments["expected_revision"] = expected_revision
        return self.call("apply_course_workflow_request", **arguments)

    def complete_workflow(self, source_payload: bytes) -> tuple[dict[str, object], str]:
        source_reference = self.ingest_source(source_payload)
        initialized = self.apply(
            "initialize_workflow",
            {"source_evidence": [source_reference]},
            "op-init",
            expected_revision=None,
        )
        initialized_value = structured(initialized)
        self.assertEqual(
            set(initialized_value),
            {"status", "receipt", "handoff", "diagnostics"},
        )
        self.assertEqual(initialized_value["status"], "advanced")
        self.assertEqual(
            initialized_value["receipt"]["operation_id"],  # type: ignore[index]
            "op-init",
        )
        self.assertEqual(
            initialized_value["receipt"]["action"],  # type: ignore[index]
            "initialize_workflow",
        )
        self.assertEqual(
            initialized_value["handoff"]["workflow_id"],  # type: ignore[index]
            "workflow-1",
        )
        self.assertNotIn("state", json.dumps(initialized_value))
        self.assertNotIn("policies", json.dumps(initialized_value))
        self.assertEqual(self.state().policies, self.policies)

        assessment = self.ingest_artifact("source_assessment")
        proposal = self.ingest_artifact("priority_proposal")
        hierarchy = self.ingest_artifact("evidence_hierarchy")
        assessment_payload = {
            "assessment_reference": assessment,
            "primary_mode": "exam_driven",
            "priority_proposal_reference": proposal,
            "evidence_hierarchy_reference": hierarchy,
        }
        assessed = self.apply(
            "record_source_assessment",
            assessment_payload,
            "op-assess",
            expected_revision=0,
        )
        self.assertEqual(structured(assessed)["status"], "advanced")
        repeated = self.apply(
            "record_source_assessment",
            assessment_payload,
            "op-assess",
            expected_revision=0,
        )
        repeated_value = structured(repeated)
        self.assertEqual(repeated_value["status"], "idempotent_repeat")
        self.assertEqual(
            repeated_value["receipt"]["operation_id"],  # type: ignore[index]
            "op-assess",
        )
        self.assertEqual(repeated_value["diagnostics"], [])

        state = self.state()
        priority_subject = priority_subject_sha256(
            state.priority_basis,
            state.policies.priority_basis,
        )
        approved_priority = self.apply(
            "approve_priority_basis",
            {"priority_subject_sha256": priority_subject},
            "op-priority",
            expected_revision=state.revision,
        )
        self.assertEqual(structured(approved_priority)["status"], "advanced")

        map_reference = self.ingest_artifact("lecture_map")
        proposed_map = self.apply(
            "record_lecture_map",
            {"map_reference": map_reference, "lecture_ids": ["l1"]},
            "op-map",
            expected_revision=self.state().revision,
        )
        self.assertEqual(structured(proposed_map)["status"], "advanced")
        state = self.state()
        map_subject = map_subject_sha256(
            state.lecture_map,
            state.policies.lecture_mapping,
        )
        approved_map = self.apply(
            "approve_lecture_map",
            {"map_subject_sha256": map_subject},
            "op-map-approve",
            expected_revision=state.revision,
        )
        self.assertEqual(structured(approved_map)["status"], "advanced")

        lecture_source = (
            "# Invented inference lecture\n\n"
            "This synthetic material explains a public toy observation.\n"
        )
        submitted = self.apply(
            "submit_lecture_candidate",
            {"lecture_id": "l1", "order": 1, "source_text": lecture_source},
            "op-candidate",
            expected_revision=self.state().revision,
        )
        self.assertEqual(structured(submitted)["status"], "advanced")
        state = self.state()
        candidate = state.lecture_progress[0].candidate
        assert candidate is not None
        subject = candidate_subject_sha256(candidate)
        validation = self.ingest_artifact("validation")
        validated = self.apply(
            "record_candidate_validation",
            {
                "lecture_id": "l1",
                "candidate_subject_sha256": subject,
                "disposition": "passed",
                "validation_reference": validation,
            },
            "op-validation",
            expected_revision=state.revision,
        )
        self.assertEqual(structured(validated)["status"], "advanced")
        self.assertEqual(self.state().stage, "completed")
        return source_reference, lecture_source


class ToolInventoryAndMappingTests(AdapterHarness):
    def test_registration_stage_keeps_inventory_and_fails_file_ingress_closed(
        self,
    ) -> None:
        disabled_config = McpAdapterConfig(
            association_database_path=self.config.association_database_path,
            workflow_database_path=self.config.workflow_database_path,
            policy_database_path=self.config.policy_database_path,
            source_database_path=self.config.source_database_path,
            artifact_database_path=self.config.artifact_database_path,
            document_database_path=self.config.document_database_path,
            workflow_policies=self.config.workflow_policies,
            file_download_policy=None,
            file_host_observation_path=self.root / "observed-file-host.txt",
        )
        server = create_mcp_server(disabled_config)
        self.assertEqual(
            [tool.name for tool in asyncio.run(server.list_tools())],
            [
                "ingest_course_source",
                "ingest_workflow_artifact",
                "apply_course_workflow_request",
                "get_course_workflow",
                "preview_source_pdf_page",
                "extract_source_pdf_region",
                "build_course_pdf",
            ],
        )
        with mock.patch.object(adapter, "download_openai_file") as download:
            result = call(
                server,
                "ingest_course_source",
                {
                    "source_id": "source-safe",
                    "file": {
                        "download_url": PRIVATE_URL,
                        "file_id": "file-invented-1",
                    },
                },
            )
        self.assertTrue(result.is_error)
        self.assertEqual(
            structured(result)["diagnostics"][0]["code"],  # type: ignore[index]
            "file_download_not_allowed",
        )
        download.assert_not_called()
        self.assertEqual(
            disabled_config.file_host_observation_path.read_text(encoding="ascii"),
            "files.example.test\n",
        )

    def test_exact_tool_inventory_descriptions_schemas_annotations_and_file_metadata(
        self,
    ) -> None:
        tools = asyncio.run(self.server.list_tools())
        expected = [
            "ingest_course_source",
            "ingest_workflow_artifact",
            "apply_course_workflow_request",
            "get_course_workflow",
            "preview_source_pdf_page",
            "extract_source_pdf_region",
            "build_course_pdf",
        ]
        self.assertEqual([item.name for item in tools], expected)
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertGreater(len(tool.description), 45)
                self.assertEqual(tool.input_schema["type"], "object")
                self.assertIn("properties", tool.input_schema)
                self.assertIsNotNone(tool.output_schema)
                self.assertEqual(tool.output_schema["type"], "object")
                self.assertFalse(tool.annotations.destructive_hint)
                self.assertEqual(
                    tool.name == "ingest_course_source",
                    tool.annotations.open_world_hint,
                )
                self.assertEqual(
                    tool.name
                    in {
                        "get_course_workflow",
                        "preview_source_pdf_page",
                        "extract_source_pdf_region",
                        "build_course_pdf",
                    },
                    tool.annotations.read_only_hint,
                )
                if tool.name in {
                    "ingest_course_source",
                    "ingest_workflow_artifact",
                    "apply_course_workflow_request",
                }:
                    self.assertTrue(tool.annotations.idempotent_hint)
        self.assertEqual(tools[0].meta, {"openai/fileParams": ["file"]})
        file_property = tools[0].input_schema["properties"]["file"]
        self.assertEqual(set(file_property), {"$ref"})
        file_definition = tools[0].input_schema["$defs"][
            file_property["$ref"].rsplit("/", 1)[-1]
        ]
        self.assertEqual(
            list(file_definition["properties"]),
            ["download_url", "file_id", "mime_type", "file_name"],
        )
        self.assertEqual(
            file_definition["required"],
            ["download_url", "file_id"],
        )
        self.assertFalse(file_definition["additionalProperties"])
        for name in ("download_url", "file_id", "mime_type", "file_name"):
            with self.subTest(file_property=name):
                self.assertEqual(
                    file_definition["properties"][name]["type"],
                    "string",
                )
                self.assertNotIn("anyOf", file_definition["properties"][name])

        self.assertEqual(
            tools[2].input_schema["properties"]["action"]["enum"],
            [
                "initialize_workflow",
                "record_source_assessment",
                "approve_priority_basis",
                "reject_priority_basis",
                "record_lecture_map",
                "approve_lecture_map",
                "reject_lecture_map",
                "submit_lecture_candidate",
                "record_candidate_validation",
                "record_operation_failure",
                "retry_failed",
                "mark_blocked",
                "clear_blocker",
                "record_new_source",
                "dismiss_new_source",
                "approve_map_reopen",
                "reopen_lectures_for_correction",
            ],
        )
        apply_schema = tools[2].output_schema
        self.assertNotIn("workflow_result", json.dumps(apply_schema))
        self.assertNotIn('"additionalProperties": true', json.dumps(apply_schema))
        self.assertEqual(
            set(apply_schema["$defs"]["ApplyWorkflowSuccess"]["properties"]),
            {"status", "receipt", "handoff", "diagnostics"},
        )
        for definition in (
            "OperationReceiptOutput",
            "WorkflowDiagnosticOutput",
            "WorkflowHandoffOutput",
        ):
            self.assertIn(definition, apply_schema["$defs"])

        get_schema = tools[3].output_schema
        self.assertNotIn('"additionalProperties": true', json.dumps(get_schema))
        self.assertEqual(
            set(tools[3].input_schema["properties"]),
            {"course_id", "workflow_id"},
        )
        self.assertEqual(
            tools[3].input_schema["required"],
            ["course_id", "workflow_id"],
        )
        get_success = get_schema["$defs"]["GetWorkflowSuccess"]
        self.assertEqual(
            get_success["properties"]["association"]["$ref"],
            "#/$defs/CourseWorkflowAssociationOutput",
        )
        self.assertEqual(
            get_success["properties"]["handoff"]["$ref"],
            "#/$defs/WorkflowHandoffOutput",
        )
        self.assertEqual(
            set(get_success["properties"]),
            {
                "status",
                "association",
                "handoff",
                "source_references",
                "accepted_documents",
                "source_assessment",
                "priority_proposal",
                "evidence_hierarchy",
                "lecture_map",
                "priority_subject_sha256",
                "map_subject_sha256",
                "lecture_progress",
                "active_lecture_id",
                "active_candidate",
                "failed_workflow",
                "blocked_workflow",
                "pending_source",
                "map_reopen",
            },
        )
        for definition in (
            "WorkflowArtifactContentOutput",
            "ContinuationLectureProgressOutput",
            "ActiveLectureCandidateOutput",
            "FailedWorkflowContextOutput",
            "BlockedWorkflowContextOutput",
            "MapReopenContinuationContextOutput",
        ):
            self.assertIn(definition, get_schema["$defs"])
            self.assertFalse(
                get_schema["$defs"][definition]["additionalProperties"]
            )

    def test_ingress_delegates_exact_bytes_and_ignores_filename_mime_and_file_id_identity(
        self,
    ) -> None:
        first = self.ingest_source(PRIVATE_BYTES)
        self.assertEqual(first["source_id"], "source-1")
        self.assertEqual(first["content_sha256"], hashlib.sha256(PRIVATE_BYTES).hexdigest())
        self.assertNotIn("file_id", first)
        self.assertNotIn("file_name", first)
        self.assertNotIn("mime_type", first)
        self.assertNotIn("download_url", first)

    def test_artifact_utf8_mapping_and_fixed_application_failure(self) -> None:
        reference = self.ingest_artifact("decision")
        expected = "invented GPT artifact for decision".encode("utf-8")
        self.assertEqual(reference["content_sha256"], hashlib.sha256(expected).hexdigest())
        conflicting = self.call(
            "ingest_workflow_artifact",
            artifact_id=reference["artifact_id"],
            artifact_kind="decision",
            content_utf8="different invented bytes",
        )
        self.assertTrue(conflicting.is_error)
        self.assertEqual(
            structured(conflicting)["diagnostics"][0]["code"],  # type: ignore[index]
            "workflow_artifact_identity_conflict",
        )

    def test_invalid_dto_and_unexpected_exception_are_fixed_and_redacted(self) -> None:
        invalid_descriptor = self.call(
            "ingest_course_source",
            source_id="source-safe",
            file={"download_url": PRIVATE_URL},
        )
        descriptor_serialized = invalid_descriptor.model_dump_json(by_alias=True)
        self.assertEqual(
            structured(invalid_descriptor)["diagnostics"][0]["code"],  # type: ignore[index]
            "invalid_mcp_input",
        )
        self.assertNotIn(PRIVATE_URL, descriptor_serialized)

        invalid = self.call(
            "ingest_course_source",
            source_id="../bad",
            file={
                "download_url": PRIVATE_URL,
                "file_id": "file-1",
                "mime_type": None,
                "file_name": None,
            },
        )
        self.assertEqual(
            structured(invalid)["diagnostics"][0]["code"],  # type: ignore[index]
            "invalid_mcp_input",
        )
        with mock.patch.object(
            adapter,
            "download_openai_file",
            side_effect=RuntimeError(PRIVATE_EXCEPTION),
        ):
            failed = self.call(
                "ingest_course_source",
                source_id="source-safe",
                file={
                    "download_url": PRIVATE_URL,
                    "file_id": "file-1",
                },
            )
        serialized = json.dumps(failed.model_dump(by_alias=True))
        self.assertIn("mcp_adapter_exception", serialized)
        self.assertNotIn(PRIVATE_URL, serialized)
        self.assertNotIn(PRIVATE_EXCEPTION, serialized)
        self.assertNotIn(PRIVATE_BYTES.decode("ascii"), serialized)

    def test_post_validation_value_and_type_errors_are_server_failures(self) -> None:
        for exception_type in (ValueError, TypeError):
            with self.subTest(exception_type=exception_type.__name__), mock.patch.object(
                adapter,
                "download_openai_file",
                side_effect=exception_type(PRIVATE_EXCEPTION),
            ):
                failed = self.call(
                    "ingest_course_source",
                    source_id="source-safe",
                    file={
                        "download_url": PRIVATE_URL,
                        "file_id": "file-1",
                    },
                )
            serialized = failed.model_dump_json(by_alias=True)
            self.assertEqual(
                structured(failed)["diagnostics"][0]["code"],  # type: ignore[index]
                "mcp_adapter_exception",
            )
            self.assertNotIn("invalid_mcp_input", serialized)
            self.assertNotIn(PRIVATE_EXCEPTION, serialized)
            self.assertNotIn(PRIVATE_URL, serialized)

    def test_output_validation_failure_is_a_server_failure(self) -> None:
        invalid_output = CallToolResult(
            content=[TextContent(text="synthetic invalid structured output")],
            structured_content={"status": "ingested"},
        )
        with mock.patch.object(
            adapter,
            "_success_result",
            return_value=invalid_output,
        ):
            failed = self.call(
                "ingest_workflow_artifact",
                artifact_id="artifact-invalid-output",
                artifact_kind="decision",
                content_utf8="invented output validation fixture",
            )
        serialized = failed.model_dump_json(by_alias=True)
        self.assertEqual(
            structured(failed)["diagnostics"][0]["code"],  # type: ignore[index]
            "mcp_adapter_exception",
        )
        self.assertNotIn("invalid_mcp_input", serialized)
        self.assertNotIn("synthetic invalid structured output", serialized)

    def test_post_validation_baseexception_propagates(self) -> None:
        class DeliberateBaseException(BaseException):
            pass

        with mock.patch.object(
            adapter,
            "download_openai_file",
            side_effect=DeliberateBaseException(PRIVATE_EXCEPTION),
        ), self.assertRaises(DeliberateBaseException):
            self.call(
                "ingest_course_source",
                source_id="source-safe",
                file={
                    "download_url": PRIVATE_URL,
                    "file_id": "file-1",
                },
            )


class PersistedWorkflowAndReadSideTests(AdapterHarness):
    def test_completed_workflow_idempotency_revision_document_creation_and_read_side(
        self,
    ) -> None:
        source_reference, lecture_source = self.complete_workflow(
            synthetic_vector_pdf()
        )
        stale = self.apply(
            "dismiss_new_source",
            {"source_content_sha256": source_reference["content_sha256"]},
            "op-stale",
            expected_revision=0,
        )
        self.assertFalse(stale.is_error)
        stale_value = structured(stale)
        self.assertEqual(stale_value["status"], "rejected")
        self.assertIsNone(stale_value["receipt"])
        self.assertEqual(
            stale_value["handoff"]["stage"],  # type: ignore[index]
            "completed",
        )
        self.assertIn(
            "stale_revision",
            [item["code"] for item in stale_value["diagnostics"]],  # type: ignore[union-attr]
        )

        reopened = self.call(
            "get_course_workflow",
            course_id="course-1",
            workflow_id="workflow-1",
        )
        self.assertFalse(reopened.is_error)
        value = structured(reopened)
        self.assertEqual(value["status"], "reopened")
        self.assertEqual(
            value["association"],
            {
                "association_version": "course-workflow-association/v1",
                "course_reference": {
                    "reference_version": "course-reference/v1",
                    "course_id": "course-1",
                },
                "workflow_id": "workflow-1",
            },
        )
        self.assertEqual(value["handoff"]["stage"], "completed")  # type: ignore[index]
        self.assertEqual(value["source_references"], [source_reference])
        self.assertEqual(
            value["accepted_documents"][0]["source_text"],  # type: ignore[index]
            lecture_source,
        )
        serialized = json.dumps(value)
        self.assertNotIn(base64.b64encode(synthetic_vector_pdf()).decode("ascii"), serialized)
        self.assertNotIn("sqlite", serialized)
        self.assertNotIn("policy_contents", serialized)

        self.assertEqual(
            value["lecture_map"]["content"],  # type: ignore[index]
            "invented GPT artifact for lecture_map",
        )
        self.assertEqual(len(value["lecture_progress"]), 1)  # type: ignore[arg-type]
        self.assertEqual(
            value["lecture_progress"][0]["accepted_document_reference"],  # type: ignore[index]
            value["accepted_documents"][0]["document_reference"],  # type: ignore[index]
        )
        self.assertIsNone(value["active_candidate"])

    def test_get_workflow_reopens_exact_priority_candidate_and_pending_context(
        self,
    ) -> None:
        source_reference = self.ingest_source(synthetic_vector_pdf())
        initialized = self.apply(
            "initialize_workflow",
            {"source_evidence": [source_reference]},
            "op-init",
            expected_revision=None,
        )
        self.assertEqual(structured(initialized)["status"], "advanced")
        assessment = self.ingest_artifact("source_assessment")
        proposal = self.ingest_artifact("priority_proposal")
        hierarchy = self.ingest_artifact("evidence_hierarchy")
        self.apply(
            "record_source_assessment",
            {
                "assessment_reference": assessment,
                "primary_mode": "exam_driven",
                "priority_proposal_reference": proposal,
                "evidence_hierarchy_reference": hierarchy,
            },
            "op-assess",
            expected_revision=0,
        )
        priority_state = self.state()
        expected_priority_subject = priority_subject_sha256(
            priority_state.priority_basis,
            priority_state.policies.priority_basis,
        )
        priority = self.call(
            "get_course_workflow",
            course_id="course-1",
            workflow_id="workflow-1",
        )
        self.assertFalse(priority.is_error)
        priority_value = structured(priority)
        self.assertEqual(priority_value["source_assessment"]["reference"], assessment)  # type: ignore[index]
        self.assertEqual(
            priority_value["source_assessment"]["content"],  # type: ignore[index]
            "invented GPT artifact for source_assessment",
        )
        self.assertEqual(priority_value["priority_proposal"]["reference"], proposal)  # type: ignore[index]
        self.assertEqual(priority_value["evidence_hierarchy"]["reference"], hierarchy)  # type: ignore[index]
        self.assertEqual(
            priority_value["priority_subject_sha256"],
            expected_priority_subject,
        )

        self.apply(
            "approve_priority_basis",
            {"priority_subject_sha256": expected_priority_subject},
            "op-priority",
            expected_revision=priority_state.revision,
        )
        lecture_map = self.ingest_artifact("lecture_map")
        self.apply(
            "record_lecture_map",
            {"map_reference": lecture_map, "lecture_ids": ["l1"]},
            "op-map",
            expected_revision=self.state().revision,
        )
        map_state = self.state()
        expected_map_subject = map_subject_sha256(
            map_state.lecture_map,
            map_state.policies.lecture_mapping,
        )
        self.apply(
            "approve_lecture_map",
            {"map_subject_sha256": expected_map_subject},
            "op-map-approve",
            expected_revision=map_state.revision,
        )
        candidate_source = (
            "# Invented active lecture\n\n"
            "Synthetic exact candidate for continuation.\n"
        )
        self.apply(
            "submit_lecture_candidate",
            {"lecture_id": "l1", "order": 1, "source_text": candidate_source},
            "op-candidate",
            expected_revision=self.state().revision,
        )
        candidate_reference = self.state().lecture_progress[0].candidate
        assert candidate_reference is not None
        expected_candidate_subject = candidate_subject_sha256(candidate_reference)
        with mock.patch.object(
            adapter,
            "reopen_course_workflow_continuation",
            wraps=adapter.reopen_course_workflow_continuation,
        ) as continuation_operation:
            candidate = self.call(
                "get_course_workflow",
                course_id="course-1",
                workflow_id="workflow-1",
            )
        continuation_operation.assert_called_once()
        candidate_value = structured(candidate)
        self.assertEqual(candidate_value["lecture_map"]["reference"], lecture_map)  # type: ignore[index]
        self.assertEqual(candidate_value["map_subject_sha256"], expected_map_subject)
        self.assertEqual(
            candidate_value["active_candidate"]["document_reference"],  # type: ignore[index]
            {
                "contract_version": candidate_reference.contract_version,
                "document_id": candidate_reference.document_id,
                "order": candidate_reference.order,
                "content_sha256": candidate_reference.content_sha256,
            },
        )
        self.assertEqual(
            candidate_value["active_candidate"]["source_text"],  # type: ignore[index]
            candidate_source,
        )
        self.assertEqual(
            candidate_value["active_candidate"]["candidate_subject_sha256"],  # type: ignore[index]
            expected_candidate_subject,
        )

        with mock.patch.object(
            adapter,
            "download_openai_file",
            return_value=b"invented second source bytes",
        ):
            pending_ingest = self.call(
                "ingest_course_source",
                source_id="source-2",
                file={"download_url": PRIVATE_URL, "file_id": "file-invented-2"},
            )
        pending_reference = structured(pending_ingest)["source_reference"]
        blocker_reference = self.ingest_artifact("blocker")
        self.apply(
            "record_new_source",
            {
                "source_reference": pending_reference,
                "evidence_reference": blocker_reference,
            },
            "op-new-source",
            expected_revision=self.state().revision,
        )
        pending = self.call(
            "get_course_workflow",
            course_id="course-1",
            workflow_id="workflow-1",
        )
        pending_value = structured(pending)
        self.assertEqual(pending_value["pending_source"], pending_reference)
        self.assertEqual(
            pending_value["blocked_workflow"]["evidence"]["reference"],  # type: ignore[index]
            blocker_reference,
        )
        self.assertEqual(
            pending_value["blocked_workflow"]["evidence"]["content"],  # type: ignore[index]
            "invented GPT artifact for blocker",
        )

    def test_get_workflow_domain_and_unexpected_failures_remain_fixed_and_redacted(
        self,
    ) -> None:
        self.complete_workflow(synthetic_vector_pdf())
        fixed = workflow_ops._continuation_failure("workflow_artifact_not_found")
        with mock.patch.object(
            adapter,
            "reopen_course_workflow_continuation",
            return_value=fixed,
        ):
            domain_failure = self.call(
                "get_course_workflow",
                course_id="course-1",
                workflow_id="workflow-1",
            )
        self.assertEqual(
            structured(domain_failure)["diagnostics"][0]["code"],  # type: ignore[index]
            "workflow_artifact_not_found",
        )

        with mock.patch.object(
            adapter,
            "reopen_course_workflow_continuation",
            side_effect=RuntimeError(PRIVATE_EXCEPTION),
        ):
            unexpected = self.call(
                "get_course_workflow",
                course_id="course-1",
                workflow_id="workflow-1",
            )
        serialized = unexpected.model_dump_json(by_alias=True)
        self.assertEqual(
            structured(unexpected)["diagnostics"][0]["code"],  # type: ignore[index]
            "mcp_adapter_exception",
        )
        self.assertNotIn(PRIVATE_EXCEPTION, serialized)
        self.assertNotIn("sqlite", serialized)

    def test_initialize_uses_only_trusted_configured_policy(self) -> None:
        source = self.ingest_source(synthetic_vector_pdf())
        tool = next(
            item
            for item in asyncio.run(self.server.list_tools())
            if item.name == "apply_course_workflow_request"
        )
        initialize_schema = tool.input_schema["properties"]["payload"]
        self.assertNotIn("policy", json.dumps(initialize_schema).lower())
        result = self.apply(
            "initialize_workflow",
            {"source_evidence": [source]},
            "op-init",
            expected_revision=None,
        )
        self.assertEqual(structured(result)["status"], "advanced")
        self.assertEqual(self.state().policies, self.policies)


class PreviewExtractionAndBuildTests(AdapterHarness):
    def setUp(self) -> None:
        super().setUp()
        self.source_pdf = synthetic_vector_pdf()
        self.source_reference, self.lecture_source = self.complete_workflow(
            self.source_pdf
        )
        self.page_reference = {
            "reference_version": "pdf-page-reference/v1",
            "source_reference": self.source_reference,
            "physical_page_ordinal": 1,
        }

    @unittest.skipUnless(
        POPPLER_AVAILABLE,
        f"real Poppler preview/extraction unavailable ({POPPLER_MISSING_REASON})",
    )
    def test_preview_and_extract_return_standard_mcp_png_content_and_exact_metadata(
        self,
    ) -> None:
        preview = self.call(
            "preview_source_pdf_page",
            course_id="course-1",
            workflow_id="workflow-1",
            page_reference=self.page_reference,
        )
        self.assertFalse(preview.is_error)
        preview_value = structured(preview)
        self.assertEqual(preview_value["page_reference"], self.page_reference)
        self.assertEqual(preview_value["width_px"], 288)
        self.assertEqual(preview_value["height_px"], 288)
        self.assertEqual(
            preview_value["coordinate_frame"],
            "pdftoppm-cropbox-pixels-top-left-144dpi/v1",
        )
        images = [item for item in preview.content if isinstance(item, ImageContent)]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].mime_type, "image/png")
        self.assertTrue(base64.b64decode(images[0].data).startswith(b"\x89PNG"))

        extracted = self.call(
            "extract_source_pdf_region",
            course_id="course-1",
            workflow_id="workflow-1",
            page_reference=self.page_reference,
            left_px=10,
            top_px=20,
            width_px=80,
            height_px=60,
        )
        self.assertFalse(extracted.is_error)
        extracted_value = structured(extracted)
        self.assertEqual(
            [
                extracted_value[key]
                for key in ("left_px", "top_px", "width_px", "height_px")
            ],
            [10, 20, 80, 60],
        )
        images = [item for item in extracted.content if isinstance(item, ImageContent)]
        self.assertEqual(len(images), 1)
        png = base64.b64decode(images[0].data)
        self.assertEqual(
            extracted_value["asset_reference"]["content_sha256"],  # type: ignore[index]
            hashlib.sha256(png).hexdigest(),
        )

    def test_source_reference_must_belong_before_any_private_bytes_reopen(self) -> None:
        foreign_reference = {
            "reference_version": "source-evidence-reference/v1",
            "source_id": "foreign-source",
            "content_sha256": "1" * 64,
        }
        with mock.patch.object(
            adapter,
            "reopen_workflow_source_evidence",
        ) as reopen:
            result = self.call(
                "preview_source_pdf_page",
                course_id="course-1",
                workflow_id="workflow-1",
                page_reference={
                    "reference_version": "pdf-page-reference/v1",
                    "source_reference": foreign_reference,
                    "physical_page_ordinal": 1,
                },
            )
        self.assertEqual(
            structured(result)["diagnostics"][0]["code"],  # type: ignore[index]
            "source_reference_not_in_workflow",
        )
        reopen.assert_not_called()

    def test_zero_visual_and_visual_build_return_embedded_pdf_and_reextract(
        self,
    ) -> None:
        pdf_bytes = b"%PDF-1.4\n% invented exact adapter result\n%%EOF\n"
        compiled = CompiledPdf(
            "compiled",
            COMPILATION_IDENTITY,
            "Course_Study_Lectures.pdf",
            pdf_bytes,
        )
        with mock.patch.object(
            adapter, "build_reopened_course_pdf", return_value=compiled
        ) as build_plain:
            zero = self.call(
                "build_course_pdf",
                course_id="course-1",
                workflow_id="workflow-1",
                visuals=[],
            )
        build_plain.assert_called_once()
        self._assert_pdf_result(zero, pdf_bytes)

        png = png_bytes(40, 30)
        asset_digest = hashlib.sha256(png).hexdigest()
        completed = subprocess.CompletedProcess((), 0, stdout=png, stderr=b"")
        document = self.state().lecture_progress[0].accepted_document
        assert document is not None
        visual = {
            "document_reference": {
                "contract_version": document.contract_version,
                "document_id": document.document_id,
                "order": document.order,
                "content_sha256": document.content_sha256,
            },
            "source_text_offset": 0,
            "page_reference": self.page_reference,
            "left_px": 0,
            "top_px": 0,
            "width_px": 40,
            "height_px": 30,
            "asset_reference": {
                "reference_version": "asset-reference/v1",
                "content_sha256": asset_digest,
            },
        }
        with mock.patch.object(
            adapter,
            "extract_pdf_page_region",
            wraps=adapter.extract_pdf_page_region,
        ) as extraction, mock.patch(
            "course_compiler.pdf_visual_extraction.subprocess.run",
            return_value=completed,
        ), mock.patch.object(
            adapter,
            "build_reopened_course_pdf_with_visuals",
            return_value=compiled,
        ) as build_visual:
            result = self.call(
                "build_course_pdf",
                course_id="course-1",
                workflow_id="workflow-1",
                visuals=[visual],
            )
        extraction.assert_called_once()
        build_visual.assert_called_once()
        placements = build_visual.call_args.kwargs["placements"]
        assets = build_visual.call_args.kwargs["asset_bytes"]
        self.assertEqual(placements[0].asset_reference.content_sha256, asset_digest)
        self.assertEqual(assets[placements[0].asset_reference], png)
        self._assert_pdf_result(result, pdf_bytes)

        mismatched = dict(visual)
        mismatched["asset_reference"] = {
            "reference_version": "asset-reference/v1",
            "content_sha256": "f" * 64,
        }
        with mock.patch(
            "course_compiler.pdf_visual_extraction.subprocess.run",
            return_value=completed,
        ), mock.patch.object(
            adapter, "build_reopened_course_pdf_with_visuals"
        ) as build_visual:
            rejected = self.call(
                "build_course_pdf",
                course_id="course-1",
                workflow_id="workflow-1",
                visuals=[mismatched],
            )
        self.assertEqual(
            structured(rejected)["diagnostics"][0]["code"],  # type: ignore[index]
            "asset_reference_mismatch",
        )
        build_visual.assert_not_called()

    def _assert_pdf_result(self, result: object, expected: bytes) -> None:
        self.assertFalse(result.is_error)
        value = structured(result)
        self.assertEqual(value["mime_type"], "application/pdf")
        self.assertEqual(value["size_bytes"], len(expected))
        self.assertEqual(value["content_sha256"], hashlib.sha256(expected).hexdigest())
        resources = [
            item for item in result.content if isinstance(item, EmbeddedResource)
        ]
        self.assertEqual(len(resources), 1)
        self.assertEqual(resources[0].resource.mime_type, "application/pdf")
        self.assertEqual(base64.b64decode(resources[0].resource.blob), expected)


class FileIngressBoundaryTests(unittest.TestCase):
    class Response:
        def __init__(self, payload: bytes, *, length: str | None = None) -> None:
            self.payload = payload
            self.offset = 0
            self.headers = {} if length is None else {"Content-Length": length}

        def __enter__(self) -> FileIngressBoundaryTests.Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def geturl(self) -> str:
            return PRIVATE_URL

        def read(self, amount: int) -> bytes:
            chunk = self.payload[self.offset : self.offset + amount]
            self.offset += len(chunk)
            return chunk

    class Opener:
        def __init__(self, response: object) -> None:
            self.response = response
            self.requests = []

        def open(self, request: object, *, timeout: float) -> object:
            self.requests.append((request, timeout))
            if isinstance(self.response, BaseException):
                raise self.response
            return self.response

    def setUp(self) -> None:
        self.reference = OpenAIFileReference(
            PRIVATE_URL,
            "file-1",
            "application/pdf",
            "ignored.pdf",
        )

    def test_exact_bounded_download_and_timeout(self) -> None:
        opener = self.Opener(self.Response(PRIVATE_BYTES, length=str(len(PRIVATE_BYTES))))
        policy = FileDownloadPolicy(("files.example.test",), 3.5, 1024)
        with mock.patch.object(ingress.urllib.request, "build_opener", return_value=opener):
            result = download_openai_file(self.reference, policy=policy)
        self.assertEqual(result, PRIVATE_BYTES)
        self.assertEqual(opener.requests[0][1], 3.5)
        self.assertNotIn(PRIVATE_URL, repr(self.reference))

    def test_https_userinfo_host_redirect_destination_and_size_fail_closed(self) -> None:
        policy = FileDownloadPolicy(("files.example.test",), 3.5, 8)
        forbidden = (
            "http://files.example.test/file",
            "https://user:secret@files.example.test/file",
            "https://other.example.test/file",
            "https://files.example.test/file#fragment",
        )
        for url in forbidden:
            with self.subTest(url=url):
                result = download_openai_file(
                    OpenAIFileReference(url, "file-1"),
                    policy=policy,
                )
                self.assertIsInstance(result, FileIngressFailure)
                self.assertEqual(result.code, "file_download_not_allowed")
        redirect_handler = ingress._AllowlistedRedirectHandler(policy)
        original_request = urllib.request.Request(
            "https://files.example.test/original"
        )
        allowed_redirect = redirect_handler.redirect_request(
            original_request,
            None,
            302,
            "Found",
            {},
            "https://files.example.test/next",
        )
        self.assertEqual(
            allowed_redirect.full_url,
            "https://files.example.test/next",
        )
        with self.assertRaises(ingress._DownloadNotAllowed):
            redirect_handler.redirect_request(
                original_request,
                None,
                302,
                "Found",
                {},
                "https://other.example.test/next",
            )
        response = self.Response(b"012345678", length="9")
        with mock.patch.object(
            ingress.urllib.request,
            "build_opener",
            return_value=self.Opener(response),
        ):
            too_large = download_openai_file(self.reference, policy=policy)
        self.assertIsInstance(too_large, FileIngressFailure)
        self.assertEqual(too_large.code, "file_too_large")

    def test_network_exception_is_fixed_and_contains_no_url(self) -> None:
        opener = self.Opener(OSError(PRIVATE_EXCEPTION))
        with mock.patch.object(ingress.urllib.request, "build_opener", return_value=opener):
            result = download_openai_file(
                self.reference,
                policy=FileDownloadPolicy(("files.example.test",)),
            )
        self.assertIsInstance(result, FileIngressFailure)
        serialized = repr(result)
        self.assertEqual(result.code, "file_download_failed")
        self.assertNotIn(PRIVATE_URL, serialized)
        self.assertNotIn(PRIVATE_EXCEPTION, serialized)


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_adapter_is_not_reexported_and_lower_layers_do_not_import_mcp_or_llms(
        self,
    ) -> None:
        root = Path(__file__).resolve().parent.parent
        package_init = (root / "course_compiler" / "__init__.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("mcp_adapter", package_init)
        self.assertNotIn("mcp_file_ingress", package_init)
        for path in (root / "course_compiler").glob("*.py"):
            if path.name in {"mcp_adapter.py", "mcp_file_ingress.py"}:
                continue
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("import mcp", text)
                self.assertNotIn("from mcp", text)
                self.assertNotIn("import openai", text)
                self.assertNotIn("import anthropic", text)
        self.assertEqual(DEFAULT_MCP_HOST, "127.0.0.1")
        self.assertEqual(DEFAULT_MCP_PATH, "/mcp")


if __name__ == "__main__":
    unittest.main()
