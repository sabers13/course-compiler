from __future__ import annotations

import asyncio
import hashlib
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request

from tests.toolchain_support import (
    MCP_MISSING_REASON,
    UVICORN_MISSING_REASON,
    require_optional_packages,
)

require_optional_packages("mcp", reason=MCP_MISSING_REASON)
require_optional_packages("uvicorn", reason=UVICORN_MISSING_REASON)

import uvicorn
from mcp import Client

from course_compiler.mcp_adapter import (
    DEFAULT_MCP_HOST,
    DEFAULT_MCP_PATH,
    create_streamable_http_app,
)
from tests.test_mcp_adapter import AdapterHarness
from tests.test_mcp_adapter import PRIVATE_URL


class StreamableHttpIntegrationTests(AdapterHarness):
    def setUp(self) -> None:
        super().setUp()
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((DEFAULT_MCP_HOST, 0))
        self.socket.listen(128)
        self.port = self.socket.getsockname()[1]
        application = create_streamable_http_app(self.config)
        self.http_server = uvicorn.Server(
            uvicorn.Config(
                application,
                host=DEFAULT_MCP_HOST,
                port=self.port,
                log_level="critical",
                lifespan="on",
            )
        )
        self.thread = threading.Thread(
            target=self.http_server.run,
            kwargs={"sockets": [self.socket]},
            daemon=True,
        )
        self.thread.start()
        for _ in range(200):
            if self.http_server.started:
                break
            if not self.thread.is_alive():
                break
            time.sleep(0.01)
        if not self.http_server.started:
            self.http_server.should_exit = True
            self.thread.join(5)
            self.socket.close()
            super().tearDown()
            self.fail("local Streamable HTTP server did not start")
        self.url = f"http://{DEFAULT_MCP_HOST}:{self.port}{DEFAULT_MCP_PATH}"

    def tearDown(self) -> None:
        self.http_server.should_exit = True
        self.thread.join(5)
        self.socket.close()
        self.assertFalse(self.thread.is_alive(), "local MCP server did not stop")
        super().tearDown()

    def test_official_client_negotiates_real_boundary_and_lists_exact_schema(self) -> None:
        async def exercise() -> object:
            async with Client(self.url) as client:
                return await client.list_tools()

        listed = asyncio.run(exercise())
        self.assertEqual(
            [item.name for item in listed.tools],
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
        self.assertEqual(
            listed.tools[0].meta,
            {"openai/fileParams": ["file"]},
        )
        file_property = listed.tools[0].input_schema["properties"]["file"]
        file_definition = listed.tools[0].input_schema["$defs"][
            file_property["$ref"].rsplit("/", 1)[-1]
        ]
        self.assertEqual(
            list(file_definition["properties"]),
            ["download_url", "file_id", "mime_type", "file_name"],
        )
        self.assertEqual(file_definition["required"], ["download_url", "file_id"])
        self.assertTrue(
            all(
                property_schema.get("type") == "string"
                and "anyOf" not in property_schema
                for property_schema in file_definition["properties"].values()
            )
        )
        self.assertTrue(all(item.output_schema for item in listed.tools))
        apply_schema = listed.tools[2].output_schema
        self.assertNotIn("workflow_result", str(apply_schema))
        self.assertEqual(
            set(apply_schema["$defs"]["ApplyWorkflowSuccess"]["properties"]),
            {"status", "receipt", "handoff", "diagnostics"},
        )
        get_schema = listed.tools[3].output_schema
        self.assertIn("CourseWorkflowAssociationOutput", get_schema["$defs"])
        self.assertIn("WorkflowHandoffOutput", get_schema["$defs"])
        self.assertTrue(listed.tools[3].annotations.read_only_hint)
        self.assertEqual(DEFAULT_MCP_PATH, "/mcp")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(
                f"http://{DEFAULT_MCP_HOST}:{self.port}/",
                timeout=2,
            )
        try:
            self.assertEqual(caught.exception.code, 404)
        finally:
            caught.exception.close()

    def test_separate_transport_sessions_share_only_persisted_application_state(self) -> None:
        arguments = {
            "artifact_id": "http-artifact-1",
            "artifact_kind": "decision",
            "content_utf8": "invented HTTP-boundary artifact",
        }

        async def one_call() -> object:
            async with Client(self.url) as client:
                return await client.call_tool("ingest_workflow_artifact", arguments)

        first = asyncio.run(one_call())
        second = asyncio.run(one_call())
        self.assertFalse(first.is_error)
        self.assertFalse(second.is_error)
        self.assertEqual(first.structured_content, second.structured_content)
        expected = hashlib.sha256(arguments["content_utf8"].encode("utf-8")).hexdigest()
        self.assertEqual(
            first.structured_content["artifact_reference"]["content_sha256"],
            expected,
        )

    def test_real_http_application_failure_is_fixed_structured_and_redacted(self) -> None:
        async def exercise() -> object:
            async with Client(self.url) as client:
                missing = await client.call_tool(
                    "get_course_workflow",
                    {"course_id": "missing-course", "workflow_id": "missing-workflow"},
                )
                invalid = await client.call_tool(
                    "ingest_course_source",
                    {
                        "source_id": "source-safe",
                        "file": {"download_url": PRIVATE_URL},
                    },
                )
                return missing, invalid

        result, invalid = asyncio.run(exercise())
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["status"], "error")
        self.assertEqual(
            result.structured_content["diagnostics"][0]["code"],
            "association_not_found",
        )
        serialized = result.model_dump_json(by_alias=True)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("sqlite", serialized)
        invalid_serialized = invalid.model_dump_json(by_alias=True)
        self.assertTrue(invalid.is_error)
        self.assertEqual(
            invalid.structured_content["diagnostics"][0]["code"],
            "invalid_mcp_input",
        )
        self.assertNotIn(PRIVATE_URL, invalid_serialized)

    def test_real_http_rejects_every_coercive_scalar_type(self) -> None:
        digest = "1" * 64
        source_reference = {
            "reference_version": "source-evidence-reference/v1",
            "source_id": "source-1",
            "content_sha256": digest,
        }
        page_reference = {
            "reference_version": "pdf-page-reference/v1",
            "source_reference": source_reference,
            "physical_page_ordinal": 1,
        }
        valid_visual = {
            "document_reference": {
                "contract_version": "lecture-document/v1",
                "document_id": "l1",
                "order": 1,
                "content_sha256": digest,
            },
            "source_text_offset": 0,
            "page_reference": page_reference,
            "left_px": 0,
            "top_px": 0,
            "width_px": 1,
            "height_px": 1,
            "asset_reference": {
                "reference_version": "asset-reference/v1",
                "content_sha256": digest,
            },
        }
        cases = [
            (
                "expected_revision_string",
                "apply_course_workflow_request",
                {
                    "course_id": "course-1",
                    "workflow_id": "workflow-1",
                    "operation_id": "op-strict",
                    "expected_revision": "1",
                    "action": "dismiss_new_source",
                    "payload": {"source_content_sha256": digest},
                },
            ),
            (
                "page_ordinal_boolean",
                "preview_source_pdf_page",
                {
                    "course_id": "course-1",
                    "workflow_id": "workflow-1",
                    "page_reference": {
                        **page_reference,
                        "physical_page_ordinal": True,
                    },
                },
            ),
            (
                "crop_coordinate_float",
                "extract_source_pdf_region",
                {
                    "course_id": "course-1",
                    "workflow_id": "workflow-1",
                    "page_reference": page_reference,
                    "left_px": 0.0,
                    "top_px": 0,
                    "width_px": 1,
                    "height_px": 1,
                },
            ),
            (
                "document_order_float",
                "build_course_pdf",
                {
                    "course_id": "course-1",
                    "workflow_id": "workflow-1",
                    "visuals": [
                        {
                            **valid_visual,
                            "document_reference": {
                                **valid_visual["document_reference"],
                                "order": 1.0,
                            },
                        }
                    ],
                },
            ),
            (
                "source_text_offset_string",
                "build_course_pdf",
                {
                    "course_id": "course-1",
                    "workflow_id": "workflow-1",
                    "visuals": [
                        {
                            **valid_visual,
                            "source_text_offset": "0",
                        }
                    ],
                },
            ),
        ]

        async def exercise() -> list[tuple[str, object]]:
            async with Client(self.url) as client:
                return [
                    (label, await client.call_tool(name, arguments))
                    for label, name, arguments in cases
                ]

        for label, result in asyncio.run(exercise()):
            with self.subTest(label=label):
                self.assertTrue(result.is_error)
                self.assertEqual(
                    result.structured_content["diagnostics"][0]["code"],
                    "invalid_mcp_input",
                )


if __name__ == "__main__":
    unittest.main()
