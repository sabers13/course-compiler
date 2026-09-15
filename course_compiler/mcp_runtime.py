"""Operator entry point for the private Course Compiler MCP runtime.

This module binds accepted stores and exact tracked Skill bytes to the T029
adapter. It does not contain workflow policy, semantic ranking, or credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Sequence

from .mcp_adapter import (
    DEFAULT_MCP_HOST,
    DEFAULT_MCP_PATH,
    DEFAULT_MCP_PORT,
    McpAdapterConfig,
    run_mcp_server,
)
from .mcp_file_ingress import FileDownloadPolicy
from .policy_persistence import (
    LocalPolicyContentStore,
    PolicyContentPayload,
    open_policy_content_store,
)
from .workflow import PolicyReference, WorkflowPolicySet
from .workflow_policy import POLICY_VERSIONS


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SKILL_ROOT = REPOSITORY_ROOT / "skills" / "course-compiler"
DATABASE_FILENAMES = {
    "association_database_path": "course-workflow-associations.sqlite3",
    "workflow_database_path": "workflow-state.sqlite3",
    "policy_database_path": "policy-content.sqlite3",
    "source_database_path": "source-evidence.sqlite3",
    "artifact_database_path": "workflow-artifacts.sqlite3",
    "document_database_path": "lecture-documents.sqlite3",
}
POLICY_SOURCE_PATHS = {
    "source_assessment": Path("SKILL.md"),
    "priority_basis": Path("SKILL.md"),
    "lecture_mapping": Path("SKILL.md"),
    "lecture_production": Path("references/lecture-authoring.md"),
    "lecture_validation": Path("references/lecture-authoring.md"),
    "workflow_handoff": Path("references/coarse-workflow.md"),
}


class McpRuntimeConfigurationError(Exception):
    """A fixed, content-free runtime setup failure."""


def seed_workflow_policies(
    policy_database_path: Path,
    *,
    skill_root: Path = DEFAULT_SKILL_ROOT,
) -> WorkflowPolicySet:
    """Persist exact tracked Skill bytes and return their immutable references."""

    try:
        payloads = {
            kind: (skill_root / relative_path).read_bytes()
            for kind, relative_path in POLICY_SOURCE_PATHS.items()
        }
    except OSError:
        raise McpRuntimeConfigurationError("tracked_policy_source_unavailable") from None

    opened = open_policy_content_store(policy_database_path)
    if type(opened) is not LocalPolicyContentStore:
        raise McpRuntimeConfigurationError("policy_store_unavailable")

    references: dict[str, PolicyReference] = {}
    try:
        for kind in POLICY_SOURCE_PATHS:
            payload = payloads[kind]
            reference = PolicyReference(
                kind,
                POLICY_VERSIONS[kind],
                hashlib.sha256(payload).hexdigest(),
            )
            saved = opened.save(reference, payload)
            if type(saved) is not PolicyContentPayload or saved.reference != reference:
                raise McpRuntimeConfigurationError("policy_seed_failed")
            references[kind] = reference
    finally:
        opened.close()

    return WorkflowPolicySet(
        references["source_assessment"],
        references["priority_basis"],
        references["lecture_mapping"],
        references["lecture_production"],
        references["lecture_validation"],
        references["workflow_handoff"],
    )


def create_runtime_config(
    data_root: Path,
    allowed_file_hosts: Sequence[str] = (),
    *,
    connection_bound_file_download: bool = False,
    skill_root: Path = DEFAULT_SKILL_ROOT,
) -> McpAdapterConfig:
    """Build trusted adapter configuration without accepting secrets or URLs.

    ``connection_bound_file_download`` selects the explicit "F2" policy
    (``FileDownloadPolicy(mode="connection_bound")``): no pre-enumerated
    attachment hosts are required or used, and it is never inferred from an
    empty/absent ``allowed_file_hosts`` — it must be explicitly requested.
    Selecting it together with a non-empty ``allowed_file_hosts`` is
    rejected as an ambiguous configuration rather than silently preferring
    one meaning. Registration-stage behavior (neither option supplied,
    ``file_download_policy=None``) is unchanged.

    ``allowed_file_hosts``-only ("F1") behavior keeps its exact-host
    membership contract unchanged, but the policy this constructs for the
    real runtime additionally enables ``bind_resolved_destination=True`` —
    the hardened, connection-bound destination-safety path shared with F2 —
    automatically. A directly, separately constructed ``FileDownloadPolicy``
    (e.g. in a test or another caller) is unaffected and keeps the original,
    unhardened default unless it opts in explicitly.
    """

    if not isinstance(data_root, Path):
        raise TypeError("data_root must be a Path")
    if isinstance(allowed_file_hosts, (str, bytes)):
        raise TypeError("allowed_file_hosts must be a sequence of host names")
    try:
        hosts = tuple(allowed_file_hosts)
    except TypeError:
        raise TypeError("allowed_file_hosts must be a sequence of host names") from None
    if type(connection_bound_file_download) is not bool:
        raise TypeError("connection_bound_file_download must be a bool")

    if connection_bound_file_download and hosts:
        raise McpRuntimeConfigurationError("file_download_mode_conflict")
    if connection_bound_file_download:
        try:
            download_policy = FileDownloadPolicy((), mode="connection_bound")
        except (TypeError, ValueError):
            raise McpRuntimeConfigurationError("file_host_allowlist_invalid") from None
    elif hosts:
        try:
            download_policy = FileDownloadPolicy(hosts, bind_resolved_destination=True)
        except (TypeError, ValueError):
            raise McpRuntimeConfigurationError("file_host_allowlist_invalid") from None
    else:
        download_policy = None

    try:
        data_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise McpRuntimeConfigurationError("runtime_data_root_unavailable") from None
    if not data_root.is_dir():
        raise McpRuntimeConfigurationError("runtime_data_root_unavailable")

    database_paths = {
        field_name: data_root / filename
        for field_name, filename in DATABASE_FILENAMES.items()
    }
    policies = seed_workflow_policies(
        database_paths["policy_database_path"],
        skill_root=skill_root,
    )

    return McpAdapterConfig(
        **database_paths,
        workflow_policies=policies,
        file_download_policy=download_policy,
        file_host_observation_path=(
            data_root / "observed-file-host.txt" if download_policy is None else None
        ),
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the private Course Compiler Streamable HTTP MCP server.",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        required=True,
        help="Ignored local-artifacts directory for private runtime databases.",
    )
    parser.add_argument(
        "--allowed-file-host",
        action="append",
        default=[],
        dest="allowed_file_hosts",
        help=(
            "Exact HTTPS host observed from a harmless ChatGPT file upload; "
            "repeatable. Omit only while registration-stage file ingress is disabled."
        ),
    )
    parser.add_argument(
        "--connection-bound-file-download",
        action="store_true",
        dest="connection_bound_file_download",
        help=(
            "Explicitly select the connection-bound (\"F2\") download policy "
            "instead of exact-host enumeration. Requires no --allowed-file-host "
            "and needs no pre-enumerated attachment hosts; mutually exclusive "
            "with --allowed-file-host."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Create and validate local stores, print a content-free summary, and exit.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        config = create_runtime_config(
            args.data_root,
            args.allowed_file_hosts,
            connection_bound_file_download=args.connection_bound_file_download,
        )
    except McpRuntimeConfigurationError as error:
        print(json.dumps({"status": "configuration_failed", "code": str(error)}))
        return 2

    if args.check:
        print(
            json.dumps(
                {
                    "status": "ready",
                    "host": DEFAULT_MCP_HOST,
                    "port": DEFAULT_MCP_PORT,
                    "path": DEFAULT_MCP_PATH,
                    "allowed_file_host_count": (
                        0
                        if config.file_download_policy is None
                        else len(config.file_download_policy.allowed_hosts)
                    ),
                },
                sort_keys=True,
            )
        )
        return 0

    run_mcp_server(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
