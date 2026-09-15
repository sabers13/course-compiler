from __future__ import annotations

import contextlib
import hashlib
import io
import tempfile
import unittest
from pathlib import Path

from course_compiler.mcp_file_ingress import FileDownloadPolicy
from course_compiler.mcp_runtime import (
    DATABASE_FILENAMES,
    DEFAULT_SKILL_ROOT,
    POLICY_SOURCE_PATHS,
    McpRuntimeConfigurationError,
    create_runtime_config,
    main,
    seed_workflow_policies,
)
from course_compiler.policy_persistence import (
    LocalPolicyContentStore,
    PolicyContentPayload,
    open_policy_content_store,
)
from course_compiler.workflow import WorkflowPolicySet
from course_compiler.workflow_policy import POLICY_VERSIONS


class McpRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="mcp-runtime-")
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_runtime_constructs_exact_six_store_paths_and_download_policy(self) -> None:
        data_root = self.root / "runtime"
        config = create_runtime_config(data_root, ("files.example.test",))

        for field_name, filename in DATABASE_FILENAMES.items():
            self.assertEqual(getattr(config, field_name), data_root / filename)
        self.assertEqual(
            config.file_download_policy,
            FileDownloadPolicy(
                ("files.example.test",), bind_resolved_destination=True
            ),
        )
        self.assertIsInstance(config.workflow_policies, WorkflowPolicySet)

    def test_registration_stage_disables_file_ingress_without_inventing_host(self) -> None:
        data_root = self.root / "runtime"
        config = create_runtime_config(data_root)

        for field_name, filename in DATABASE_FILENAMES.items():
            self.assertEqual(getattr(config, field_name), data_root / filename)
        self.assertIsNone(config.file_download_policy)
        self.assertEqual(
            config.file_host_observation_path,
            data_root / "observed-file-host.txt",
        )
        self.assertIsInstance(config.workflow_policies, WorkflowPolicySet)

    def test_policy_seeding_uses_exact_tracked_skill_bytes_for_all_six_slots(self) -> None:
        database_path = self.root / "policy.sqlite3"
        policy_set = seed_workflow_policies(database_path)
        opened = open_policy_content_store(database_path)
        self.assertIs(type(opened), LocalPolicyContentStore)
        assert type(opened) is LocalPolicyContentStore
        try:
            for kind, relative_path in POLICY_SOURCE_PATHS.items():
                payload = (DEFAULT_SKILL_ROOT / relative_path).read_bytes()
                reference = getattr(policy_set, kind)
                self.assertEqual(reference.policy_kind, kind)
                self.assertEqual(reference.policy_version, POLICY_VERSIONS[kind])
                self.assertEqual(
                    reference.content_sha256,
                    hashlib.sha256(payload).hexdigest(),
                )
                loaded = opened.load(reference)
                self.assertIs(type(loaded), PolicyContentPayload)
                assert type(loaded) is PolicyContentPayload
                self.assertEqual(loaded.payload, payload)
        finally:
            opened.close()

    def test_policy_seeding_is_idempotent(self) -> None:
        database_path = self.root / "policy.sqlite3"
        first = seed_workflow_policies(database_path)
        second = seed_workflow_policies(database_path)
        self.assertEqual(first, second)

    def test_invalid_host_is_rejected_with_fixed_content_free_code(self) -> None:
        data_root = self.root / "runtime"
        with self.assertRaisesRegex(
            McpRuntimeConfigurationError,
            "^file_host_allowlist_invalid$",
        ):
            create_runtime_config(data_root, ("HTTPS://secret.invalid/path",))
        self.assertFalse(data_root.exists())

    def test_missing_tracked_policy_source_is_redacted(self) -> None:
        with self.assertRaisesRegex(
            McpRuntimeConfigurationError,
            "^tracked_policy_source_unavailable$",
        ):
            seed_workflow_policies(
                self.root / "policy.sqlite3",
                skill_root=self.root / "absent",
            )

    def test_check_mode_creates_stores_without_starting_server(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(
                (
                    "--data-root",
                    str(self.root / "runtime"),
                    "--allowed-file-host",
                    "files.example.test",
                    "--check",
                )
            )
        self.assertEqual(status, 0)
        self.assertEqual(
            output.getvalue(),
            '{"allowed_file_host_count": 1, "host": "127.0.0.1", '
            '"path": "/mcp", "port": 8000, "status": "ready"}\n',
        )

    def test_check_mode_allows_registration_stage_without_file_host(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(
                (
                    "--data-root",
                    str(self.root / "runtime"),
                    "--check",
                )
            )
        self.assertEqual(status, 0)
        self.assertEqual(
            output.getvalue(),
            '{"allowed_file_host_count": 0, "host": "127.0.0.1", '
            '"path": "/mcp", "port": 8000, "status": "ready"}\n',
        )


class McpRuntimeFileDownloadModeTests(unittest.TestCase):
    """Explicit F1/F2 runtime-selection contract.

    F1 ("exact_host") remains the unchanged default; F2 ("connection_bound")
    must be explicitly requested and is never inferred from an
    empty/absent ``--allowed-file-host``.
    """

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="mcp-runtime-")
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_existing_f1_only_configuration_remains_backward_compatible(self) -> None:
        data_root = self.root / "runtime"
        config = create_runtime_config(data_root, ("files.example.test",))
        assert config.file_download_policy is not None
        self.assertEqual(config.file_download_policy.mode, "exact_host")
        self.assertEqual(config.file_download_policy.allowed_hosts, ("files.example.test",))
        self.assertIsNone(config.file_host_observation_path)

    def test_runtime_f1_policy_enables_hardened_destination_bound_safety(self) -> None:
        """The real runtime's F1 configuration is not the same as a directly,
        separately constructed ``FileDownloadPolicy``: it additionally opts
        into the shared connection-bound destination-safety engine F2 uses,
        per the T033 corrective review's runtime-distinction requirement."""

        data_root = self.root / "runtime"
        config = create_runtime_config(data_root, ("files.example.test",))
        assert config.file_download_policy is not None
        self.assertTrue(config.file_download_policy.bind_resolved_destination)
        # A directly constructed policy without that opt-in stays unhardened.
        self.assertFalse(
            FileDownloadPolicy(("files.example.test",)).bind_resolved_destination
        )

    def test_registration_stage_fail_closed_behavior_remains_unchanged(self) -> None:
        data_root = self.root / "runtime"
        config = create_runtime_config(data_root)
        self.assertIsNone(config.file_download_policy)
        self.assertEqual(
            config.file_host_observation_path,
            data_root / "observed-file-host.txt",
        )

    def test_explicit_connection_bound_selection_works_without_any_host(self) -> None:
        data_root = self.root / "runtime"
        config = create_runtime_config(
            data_root, connection_bound_file_download=True
        )
        assert config.file_download_policy is not None
        self.assertEqual(config.file_download_policy.mode, "connection_bound")
        self.assertEqual(config.file_download_policy.allowed_hosts, ())
        # F2 selection is not the registration-stage "no policy" case, and it
        # never promotes/observes a candidate host either.
        self.assertIsNone(config.file_host_observation_path)

    def test_connection_bound_selection_is_never_inferred_from_empty_hosts(self) -> None:
        data_root = self.root / "runtime"
        # Omitting --allowed-file-host alone (the existing, unchanged
        # registration-stage call shape) must NOT silently become F2.
        config = create_runtime_config(data_root, ())
        self.assertIsNone(config.file_download_policy)

    def test_connection_bound_plus_allowed_hosts_is_rejected_as_ambiguous(self) -> None:
        data_root = self.root / "runtime"
        with self.assertRaisesRegex(
            McpRuntimeConfigurationError,
            "^file_download_mode_conflict$",
        ):
            create_runtime_config(
                data_root,
                ("files.example.test",),
                connection_bound_file_download=True,
            )
        self.assertFalse(data_root.exists())

    def test_connection_bound_flag_requires_a_bool(self) -> None:
        data_root = self.root / "runtime"
        with self.assertRaises(TypeError):
            create_runtime_config(
                data_root, connection_bound_file_download="yes"  # type: ignore[arg-type]
            )

    def test_cli_explicit_connection_bound_flag_selects_f2(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(
                (
                    "--data-root",
                    str(self.root / "runtime"),
                    "--connection-bound-file-download",
                    "--check",
                )
            )
        self.assertEqual(status, 0)
        self.assertEqual(
            output.getvalue(),
            '{"allowed_file_host_count": 0, "host": "127.0.0.1", '
            '"path": "/mcp", "port": 8000, "status": "ready"}\n',
        )

    def test_cli_connection_bound_plus_allowed_host_fails_closed(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(
                (
                    "--data-root",
                    str(self.root / "runtime"),
                    "--allowed-file-host",
                    "files.example.test",
                    "--connection-bound-file-download",
                    "--check",
                )
            )
        self.assertEqual(status, 2)
        self.assertEqual(
            output.getvalue(),
            '{"status": "configuration_failed", "code": "file_download_mode_conflict"}\n',
        )

    def test_f2_selection_requires_no_runtime_restart_or_host_promotion(self) -> None:
        """One static, unchanging F2 startup call is a complete configuration —
        unlike F1's registration-stage-then-restart-with-observed-host flow,
        nothing about F2 selection depends on any host ever being observed,
        promoted, or added between runtime starts."""

        data_root = self.root / "runtime"
        first = create_runtime_config(data_root, connection_bound_file_download=True)
        second = create_runtime_config(data_root, connection_bound_file_download=True)
        self.assertEqual(first.file_download_policy, second.file_download_policy)
        assert first.file_download_policy is not None
        self.assertEqual(first.file_download_policy.allowed_hosts, ())


if __name__ == "__main__":
    unittest.main()
