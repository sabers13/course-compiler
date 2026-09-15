from __future__ import annotations

import ast
import hashlib
import tempfile
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from unittest import mock

import course_compiler
from course_compiler import (
    ActiveLectureCandidate,
    CourseWorkflowContinuation,
    CourseWorkflowContinuationDiagnostic,
    CourseWorkflowContinuationFailure,
    COURSE_REFERENCE_VERSION,
    COURSE_WORKFLOW_ASSOCIATION_VERSION,
    CourseReference,
    CourseWorkflowAssociation,
    CourseWorkflowReopenDiagnostic,
    CourseWorkflowReopenFailure,
    LocalCourseWorkflowAssociationStore,
    LectureDocument,
    LocalLectureDocumentStore,
    LocalPolicyContentStore,
    LocalWorkflowArtifactStore,
    LocalWorkflowStateStore,
    PolicyContentPayload,
    PolicyReference,
    ReopenedCourseWorkflowContext,
    ReopenedWorkflowArtifact,
    SourceProvenance,
    WorkflowArtifactReference,
    WorkflowAdvanced,
    WorkflowPolicySet,
    WorkflowState,
    open_course_workflow_association_store,
    open_lecture_document_store,
    open_policy_content_store,
    open_workflow_artifact_store,
    open_workflow_state_store,
    reopen_course_workflow_continuation,
    reopen_course_workflow_context,
)
from course_compiler import (
    course_workflow_persistence,
    policy_persistence,
    workflow_artifact_persistence,
    lecture_document_persistence,
    workflow_persistence,
)
from course_compiler.workflow import (
    ApproveLectureMap,
    ApproveMapReopen,
    ApprovePriorityBasis,
    InitializeWorkflow,
    InitializeWorkflowRequest,
    MarkBlocked,
    RecordCandidateValidation,
    RecordLectureMap,
    RecordNewSource,
    RecordOperationFailure,
    RecordSourceAssessment,
    SubmitLectureCandidate,
    ValidationRecord,
    WorkflowBlocked,
    WorkflowFailed,
    WorkflowTransitionRequest,
    apply_workflow_request,
    candidate_subject_sha256,
    map_subject_sha256,
    priority_subject_sha256,
)
from course_compiler.workflow_policy import ARTIFACT_PRODUCERS, POLICY_VERSIONS
from tests.test_workflow_contract import make_source


POLICY_SLOTS = (
    "source_assessment",
    "priority_basis",
    "lecture_mapping",
    "lecture_production",
    "lecture_validation",
    "workflow_handoff",
)
PRIVATE_MARKER = "private-course-marker"


def initial_state(workflow_id: str = "workflow-1") -> WorkflowState:
    policies = WorkflowPolicySet(
        *(
            PolicyReference(
                kind,
                POLICY_VERSIONS[kind],
                hashlib.sha256(policy_bytes(kind)).hexdigest(),
            )
            for kind in POLICY_SLOTS
        )
    )
    request = InitializeWorkflowRequest(
        "course-workflow-transition/v1",
        workflow_id,
        "op-initialize",
        InitializeWorkflow(
            "initialize_workflow",
            policies,
            (make_source(),),
        ),
    )
    result = apply_workflow_request(None, request)
    assert isinstance(result, WorkflowAdvanced)
    return result.state


def association(workflow_id: str = "workflow-1") -> CourseWorkflowAssociation:
    return CourseWorkflowAssociation(
        COURSE_WORKFLOW_ASSOCIATION_VERSION,
        CourseReference(COURSE_REFERENCE_VERSION, "invented-course"),
        workflow_id,
    )


def policy_payloads(state: WorkflowState) -> tuple[PolicyContentPayload, ...]:
    return tuple(
        PolicyContentPayload(reference, policy_bytes(reference.policy_kind))
        for reference in (
            getattr(state.policies, slot_name) for slot_name in POLICY_SLOTS
        )
    )


def policy_bytes(kind: str) -> bytes:
    return f"invented opaque bytes for {kind}".encode("utf-8")


def failure_code(result: object) -> str:
    if not isinstance(result, CourseWorkflowReopenFailure):
        raise AssertionError(
            f"course-workflow reopen failure required, got {type(result).__name__}"
        )
    return result.diagnostics[0].code


class ReopenSentinel(BaseException):
    pass


class CourseWorkflowOperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="reopen-")
        root = Path(self._temporary.name)
        self.association_store = open_course_workflow_association_store(
            root / "association.sqlite"
        )
        self.workflow_store = open_workflow_state_store(root / "workflow.sqlite")
        self.policy_store = open_policy_content_store(root / "policy.sqlite")
        assert isinstance(
            self.association_store, LocalCourseWorkflowAssociationStore
        )
        assert isinstance(self.workflow_store, LocalWorkflowStateStore)
        assert isinstance(self.policy_store, LocalPolicyContentStore)
        self.association = association()
        self.state = initial_state()
        self.payloads = policy_payloads(self.state)
        self.assertEqual(
            self.association_store.save(self.association), self.association
        )
        self.assertEqual(self.workflow_store.save(self.state), self.state)
        for payload in self.payloads:
            self.assertEqual(
                self.policy_store.save(payload.reference, payload.payload), payload
            )

    def tearDown(self) -> None:
        self.association_store.close()
        self.workflow_store.close()
        self.policy_store.close()
        self._temporary.cleanup()

    def reopen(self) -> object:
        return reopen_course_workflow_context(
            self.association,
            association_store=self.association_store,
            workflow_store=self.workflow_store,
            policy_store=self.policy_store,
        )

    def test_public_records_exports_and_fixed_diagnostics(self) -> None:
        self.assertEqual(
            [item.name for item in fields(ReopenedCourseWorkflowContext)],
            ["association", "workflow_state", "policy_contents"],
        )
        self.assertEqual(
            [item.name for item in fields(CourseWorkflowReopenDiagnostic)],
            ["code", "classification", "message"],
        )
        self.assertEqual(
            [item.name for item in fields(CourseWorkflowReopenFailure)],
            ["status", "diagnostics"],
        )
        expected_exports = {
            "CourseWorkflowReopenDiagnostic",
            "CourseWorkflowReopenFailure",
            "CourseWorkflowReopenResult",
            "ReopenedCourseWorkflowContext",
            "reopen_course_workflow_context",
        }
        self.assertTrue(expected_exports <= set(course_compiler.__all__))
        with self.assertRaises(ValueError):
            CourseWorkflowReopenDiagnostic("other", "storage", "other")  # type: ignore[arg-type]

    def test_success_is_exact_canonical_immutable_and_representation_safe(self) -> None:
        result = self.reopen()
        self.assertIsInstance(result, ReopenedCourseWorkflowContext)
        assert isinstance(result, ReopenedCourseWorkflowContext)
        self.assertEqual(result.association, self.association)
        self.assertEqual(result.workflow_state, self.state)
        self.assertEqual(result.workflow_state.workflow_id, self.association.workflow_id)
        self.assertIs(type(result.policy_contents), tuple)
        self.assertEqual(len(result.policy_contents), 6)
        self.assertEqual(result.policy_contents, self.payloads)
        self.assertEqual(
            tuple(payload.reference.policy_kind for payload in result.policy_contents),
            POLICY_SLOTS,
        )
        for slot_name, payload in zip(POLICY_SLOTS, result.policy_contents):
            self.assertEqual(payload.reference, getattr(self.state.policies, slot_name))
            self.assertNotIn(payload.payload.decode("utf-8"), repr(result))
        self.assertFalse(hasattr(result, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            result.association = association("other-workflow")  # type: ignore[misc]

    def test_separately_backed_stores_are_supported(self) -> None:
        result = self.reopen()
        self.assertIsInstance(result, ReopenedCourseWorkflowContext)
        database_files = list(Path(self._temporary.name).glob("*.sqlite"))
        self.assertEqual(len(database_files), 3)

    def test_success_reads_all_stores_in_the_exact_required_order(self) -> None:
        calls: list[str] = []
        payload_by_reference = {payload.reference: payload for payload in self.payloads}

        with (
            mock.patch.object(
                LocalCourseWorkflowAssociationStore,
                "load",
                side_effect=lambda _: calls.append("association") or self.association,
            ),
            mock.patch.object(
                LocalWorkflowStateStore,
                "load",
                side_effect=lambda _: calls.append("workflow") or self.state,
            ),
            mock.patch.object(
                LocalPolicyContentStore,
                "load",
                side_effect=lambda reference: calls.append(reference.policy_kind)
                or payload_by_reference[reference],
            ),
        ):
            self.assertIsInstance(self.reopen(), ReopenedCourseWorkflowContext)
        self.assertEqual(calls, ["association", "workflow", *POLICY_SLOTS])

    def test_co_located_but_independently_opened_stores_are_supported(self) -> None:
        database_path = Path(self._temporary.name) / "co-located.sqlite"
        association_store = open_course_workflow_association_store(database_path)
        workflow_store = open_workflow_state_store(database_path)
        policy_store = open_policy_content_store(database_path)
        assert isinstance(association_store, LocalCourseWorkflowAssociationStore)
        assert isinstance(workflow_store, LocalWorkflowStateStore)
        assert isinstance(policy_store, LocalPolicyContentStore)
        try:
            self.assertEqual(association_store.save(self.association), self.association)
            self.assertEqual(workflow_store.save(self.state), self.state)
            for payload in self.payloads:
                self.assertEqual(
                    policy_store.save(payload.reference, payload.payload), payload
                )
            result = reopen_course_workflow_context(
                self.association,
                association_store=association_store,
                workflow_store=workflow_store,
                policy_store=policy_store,
            )
            self.assertIsInstance(result, ReopenedCourseWorkflowContext)
            self.assertIsNot(
                association_store._connection, workflow_store._connection
            )
            self.assertIsNot(workflow_store._connection, policy_store._connection)
        finally:
            association_store.close()
            workflow_store.close()
            policy_store.close()

    def test_association_missing_stops_before_workflow_and_policy_reads(self) -> None:
        calls: list[str] = []

        def missing(_: object) -> object:
            calls.append("association")
            return course_workflow_persistence._failure("association_not_found")

        with (
            mock.patch.object(
                LocalCourseWorkflowAssociationStore, "load", side_effect=missing
            ),
            mock.patch.object(
                LocalWorkflowStateStore,
                "load",
                side_effect=lambda _: calls.append("workflow"),
            ),
            mock.patch.object(
                LocalPolicyContentStore,
                "load",
                side_effect=lambda _: calls.append("policy"),
            ),
        ):
            self.assertEqual(failure_code(self.reopen()), "association_not_found")
        self.assertEqual(calls, ["association"])

    def test_workflow_missing_stops_before_policy_reads(self) -> None:
        calls: list[str] = []

        with (
            mock.patch.object(
                LocalCourseWorkflowAssociationStore,
                "load",
                side_effect=lambda _: calls.append("association") or self.association,
            ),
            mock.patch.object(
                LocalWorkflowStateStore,
                "load",
                side_effect=lambda _: calls.append("workflow")
                or workflow_persistence._failure("workflow_not_found"),
            ),
            mock.patch.object(
                LocalPolicyContentStore,
                "load",
                side_effect=lambda _: calls.append("policy"),
            ),
        ):
            self.assertEqual(failure_code(self.reopen()), "workflow_not_found")
        self.assertEqual(calls, ["association", "workflow"])

    def test_each_policy_miss_stops_in_canonical_order_without_partial_success(self) -> None:
        payload_by_reference = {payload.reference: payload for payload in self.payloads}
        for missing_index, missing_slot in enumerate(POLICY_SLOTS):
            calls: list[str] = []

            def load_policy(reference: PolicyReference) -> object:
                calls.append(reference.policy_kind)
                if reference.policy_kind == missing_slot:
                    return policy_persistence._failure("policy_not_found")
                return payload_by_reference[reference]

            with mock.patch.object(
                LocalPolicyContentStore, "load", side_effect=load_policy
            ):
                result = self.reopen()
            with self.subTest(missing_slot=missing_slot):
                self.assertIsInstance(result, CourseWorkflowReopenFailure)
                self.assertEqual(failure_code(result), "policy_not_found")
                self.assertEqual(calls, list(POLICY_SLOTS[: missing_index + 1]))
                self.assertFalse(hasattr(result, "policy_contents"))

    def test_non_not_found_persistence_failures_map_by_store(self) -> None:
        cases = (
            (
                LocalCourseWorkflowAssociationStore,
                course_workflow_persistence._failure("persistence_unavailable"),
                "association_store_failed",
            ),
            (
                LocalWorkflowStateStore,
                workflow_persistence._failure("unsupported_storage_schema"),
                "workflow_store_failed",
            ),
            (
                LocalPolicyContentStore,
                policy_persistence._failure("stored_policy_invalid"),
                "policy_store_failed",
            ),
        )
        for store_type, lower_failure, expected in cases:
            with self.subTest(expected=expected), mock.patch.object(
                store_type, "load", return_value=lower_failure
            ):
                self.assertEqual(failure_code(self.reopen()), expected)

    def test_wrong_top_level_types_raise_before_any_read(self) -> None:
        valid = {
            "association": self.association,
            "association_store": self.association_store,
            "workflow_store": self.workflow_store,
            "policy_store": self.policy_store,
        }
        cases = (
            ("association", object()),
            ("association_store", object()),
            ("workflow_store", object()),
            ("policy_store", object()),
        )
        for name, wrong_value in cases:
            arguments = dict(valid)
            arguments[name] = wrong_value
            with (
                self.subTest(name=name),
                mock.patch.object(
                    LocalCourseWorkflowAssociationStore, "load"
                ) as association_load,
                mock.patch.object(LocalWorkflowStateStore, "load") as workflow_load,
                mock.patch.object(LocalPolicyContentStore, "load") as policy_load,
                self.assertRaises(TypeError),
            ):
                reopen_course_workflow_context(**arguments)  # type: ignore[arg-type]
            association_load.assert_not_called()
            workflow_load.assert_not_called()
            policy_load.assert_not_called()

    def test_forged_exact_type_association_is_invalid_before_reads(self) -> None:
        forged = object.__new__(CourseWorkflowAssociation)
        object.__setattr__(forged, "association_version", PRIVATE_MARKER)
        with (
            mock.patch.object(
                LocalCourseWorkflowAssociationStore, "load"
            ) as association_load,
            mock.patch.object(LocalWorkflowStateStore, "load") as workflow_load,
            mock.patch.object(LocalPolicyContentStore, "load") as policy_load,
        ):
            result = reopen_course_workflow_context(
                forged,
                association_store=self.association_store,
                workflow_store=self.workflow_store,
                policy_store=self.policy_store,
            )
        self.assertEqual(failure_code(result), "invalid_reopen_input")
        association_load.assert_not_called()
        workflow_load.assert_not_called()
        policy_load.assert_not_called()
        self.assertNotIn(PRIVATE_MARKER, repr(result))

    def test_different_association_success_is_inconsistent_and_stops(self) -> None:
        calls: list[str] = []
        different = CourseWorkflowAssociation(
            self.association.association_version,
            CourseReference(COURSE_REFERENCE_VERSION, "different-course"),
            self.association.workflow_id,
        )
        with (
            mock.patch.object(
                LocalCourseWorkflowAssociationStore,
                "load",
                return_value=different,
            ),
            mock.patch.object(
                LocalWorkflowStateStore,
                "load",
                side_effect=lambda _: calls.append("workflow"),
            ),
        ):
            self.assertEqual(
                failure_code(self.reopen()), "inconsistent_reopened_context"
            )
        self.assertEqual(calls, [])

    def test_mismatching_workflow_success_is_inconsistent_and_stops(self) -> None:
        calls: list[str] = []
        with (
            mock.patch.object(
                LocalWorkflowStateStore,
                "load",
                return_value=initial_state("different-workflow"),
            ),
            mock.patch.object(
                LocalPolicyContentStore,
                "load",
                side_effect=lambda _: calls.append("policy"),
            ),
        ):
            self.assertEqual(
                failure_code(self.reopen()), "inconsistent_reopened_context"
            )
        self.assertEqual(calls, [])

    def test_mismatching_policy_reference_is_inconsistent_and_stops(self) -> None:
        calls: list[str] = []
        first = self.payloads[0]
        different_bytes = b"invented different policy"
        different_reference = PolicyReference(
            first.reference.policy_kind,
            first.reference.policy_version,
            hashlib.sha256(different_bytes).hexdigest(),
        )
        different_payload = PolicyContentPayload(different_reference, different_bytes)

        def load_policy(reference: PolicyReference) -> object:
            calls.append(reference.policy_kind)
            return different_payload

        with mock.patch.object(
            LocalPolicyContentStore, "load", side_effect=load_policy
        ):
            result = self.reopen()
        self.assertEqual(failure_code(result), "inconsistent_reopened_context")
        self.assertEqual(calls, [POLICY_SLOTS[0]])
        self.assertNotIn(different_bytes.decode(), repr(result))

    def test_malformed_exact_type_success_values_are_inconsistent(self) -> None:
        malformed_association = object.__new__(CourseWorkflowAssociation)
        object.__setattr__(
            malformed_association,
            "association_version",
            COURSE_WORKFLOW_ASSOCIATION_VERSION,
        )
        malformed_payload = object.__new__(PolicyContentPayload)
        object.__setattr__(malformed_payload, "reference", self.payloads[0].reference)
        object.__setattr__(malformed_payload, "payload", "not-bytes")
        cases = (
            (LocalCourseWorkflowAssociationStore, malformed_association),
            (LocalPolicyContentStore, malformed_payload),
            (LocalWorkflowStateStore, object.__new__(WorkflowState)),
        )
        for store_type, malformed in cases:
            with self.subTest(store=store_type.__name__), mock.patch.object(
                store_type, "load", return_value=malformed
            ):
                self.assertEqual(
                    failure_code(self.reopen()), "inconsistent_reopened_context"
                )

    def test_unknown_success_shape_is_inconsistent(self) -> None:
        with mock.patch.object(
            LocalPolicyContentStore, "load", return_value=object()
        ):
            self.assertEqual(
                failure_code(self.reopen()), "inconsistent_reopened_context"
            )

    def test_stores_remain_open_and_usable_after_operation(self) -> None:
        self.assertIsInstance(self.reopen(), ReopenedCourseWorkflowContext)
        self.assertEqual(self.association_store.load(self.association), self.association)
        self.assertEqual(self.workflow_store.load(self.state.workflow_id), self.state)
        self.assertEqual(
            self.policy_store.load(self.payloads[0].reference), self.payloads[0]
        )

    def test_operation_invokes_no_save_or_write_method(self) -> None:
        with (
            mock.patch.object(
                LocalCourseWorkflowAssociationStore,
                "save",
                side_effect=AssertionError("association save called"),
            ) as association_save,
            mock.patch.object(
                LocalWorkflowStateStore,
                "save",
                side_effect=AssertionError("workflow save called"),
            ) as workflow_save,
            mock.patch.object(
                LocalPolicyContentStore,
                "save",
                side_effect=AssertionError("policy save called"),
            ) as policy_save,
        ):
            self.assertIsInstance(self.reopen(), ReopenedCourseWorkflowContext)
        association_save.assert_not_called()
        workflow_save.assert_not_called()
        policy_save.assert_not_called()

    def test_fixed_failures_redact_hostile_paths_sql_content_and_exceptions(self) -> None:
        hostile = (
            f"{PRIVATE_MARKER} /private/course.sqlite "
            "SELECT payload FROM policy_content_blobs "
            "invented source evidence"
        )
        with mock.patch.object(
            LocalCourseWorkflowAssociationStore,
            "load",
            side_effect=RuntimeError(hostile),
        ):
            result = self.reopen()
        self.assertEqual(failure_code(result), "reopen_exception")
        public = repr(result)
        for marker in (
            PRIVATE_MARKER,
            "/private/course.sqlite",
            "SELECT payload",
            "source evidence",
        ):
            self.assertNotIn(marker, public)

    def test_unexpected_ordinary_exceptions_are_contained_at_each_read_stage(self) -> None:
        for store_type in (
            LocalCourseWorkflowAssociationStore,
            LocalWorkflowStateStore,
            LocalPolicyContentStore,
        ):
            with self.subTest(store=store_type.__name__), mock.patch.object(
                store_type, "load", side_effect=RuntimeError(PRIVATE_MARKER)
            ):
                self.assertEqual(failure_code(self.reopen()), "reopen_exception")

    def test_base_exception_propagates_unchanged(self) -> None:
        sentinel = ReopenSentinel(PRIVATE_MARKER)
        with mock.patch.object(
            LocalPolicyContentStore, "load", side_effect=sentinel
        ):
            with self.assertRaises(ReopenSentinel) as raised:
                self.reopen()
        self.assertIs(raised.exception, sentinel)

    def test_success_constructor_rejects_partial_or_incoherent_policy_results(self) -> None:
        with self.assertRaises(ValueError):
            ReopenedCourseWorkflowContext(
                self.association,
                self.state,
                self.payloads[:-1],
            )
        with self.assertRaises(ValueError):
            ReopenedCourseWorkflowContext(
                self.association,
                self.state,
                list(self.payloads),  # type: ignore[arg-type]
            )
        with self.assertRaises(ValueError):
            ReopenedCourseWorkflowContext(
                self.association,
                self.state,
                tuple(reversed(self.payloads)),
            )

    def test_module_dependency_and_environment_boundary_is_narrow(self) -> None:
        module_path = (
            Path(course_compiler.__file__).parent
            / "course_workflow_operations.py"
        )
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imports: set[str] = set()
        calls: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    calls.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    calls.add(node.func.attr)
        self.assertTrue(imports <= {"__future__", "dataclasses", "typing"})
        self.assertFalse(
            {"open", "connect", "run", "Popen", "urlopen", "socket"} & calls
        )
        for lower_name in (
            "course.py",
            "course_workflow.py",
            "course_workflow_persistence.py",
            "workflow.py",
            "workflow_persistence.py",
            "policy_persistence.py",
        ):
            lower_source = (module_path.parent / lower_name).read_text(encoding="utf-8")
            self.assertNotIn("course_workflow_operations", lower_source)


class WorkflowContinuationReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="continuation-")
        root = Path(self._temporary.name)
        self.artifact_store = open_workflow_artifact_store(root / "artifact.sqlite")
        self.document_store = open_lecture_document_store(root / "document.sqlite")
        assert isinstance(self.artifact_store, LocalWorkflowArtifactStore)
        assert isinstance(self.document_store, LocalLectureDocumentStore)
        self.state = initial_state()
        self.association = association()
        self.payloads = policy_payloads(self.state)
        self._operation = 0
        self._artifact = 0

    def tearDown(self) -> None:
        self.artifact_store.close()
        self.document_store.close()
        self._temporary.cleanup()

    def context(self) -> ReopenedCourseWorkflowContext:
        return ReopenedCourseWorkflowContext(
            self.association,
            self.state,
            self.payloads,
        )

    def artifact(
        self,
        kind: str,
        content: str | bytes | None = None,
        *,
        save: bool = True,
    ) -> tuple[WorkflowArtifactReference, bytes]:
        self._artifact += 1
        payload = (
            f"invented exact {kind} continuation evidence {self._artifact}".encode()
            if content is None
            else content.encode("utf-8")
            if type(content) is str
            else content
        )
        reference = WorkflowArtifactReference(
            "workflow-artifact-reference/v1",
            f"artifact-{kind}-{self._artifact}",
            kind,
            hashlib.sha256(payload).hexdigest(),
            ARTIFACT_PRODUCERS[kind],
        )
        if save:
            saved = self.artifact_store.save(reference, payload)
            self.assertEqual(saved.reference, reference)  # type: ignore[union-attr]
        return reference, payload

    def advance(self, payload: object) -> object:
        self._operation += 1
        request = WorkflowTransitionRequest(
            "course-workflow-transition/v1",
            self.state.workflow_id,
            self.state.revision,
            f"op-{self._operation}",
            payload,
        )
        result = apply_workflow_request(self.state, request)
        self.assertIsInstance(
            result,
            (WorkflowAdvanced, WorkflowBlocked, WorkflowFailed),
        )
        self.state = result.state  # type: ignore[union-attr]
        return result

    def assess(self) -> tuple[tuple[WorkflowArtifactReference, bytes], ...]:
        assessment = self.artifact("source_assessment")
        proposal = self.artifact("priority_proposal")
        hierarchy = self.artifact("evidence_hierarchy")
        self.advance(
            RecordSourceAssessment(
                "record_source_assessment",
                assessment[0],
                "exam_driven",
                proposal[0],
                hierarchy[0],
            )
        )
        return assessment, proposal, hierarchy

    def approve_priority(self) -> None:
        assert self.state.priority_basis is not None
        subject = priority_subject_sha256(
            self.state.priority_basis,
            self.state.policies.priority_basis,
        )
        self.advance(ApprovePriorityBasis("approve_priority_basis", subject))

    def propose_map(
        self,
    ) -> tuple[WorkflowArtifactReference, bytes]:
        lecture_map = self.artifact("lecture_map")
        self.advance(RecordLectureMap("record_lecture_map", lecture_map[0], ("l1",)))
        return lecture_map

    def approve_map(self) -> None:
        assert self.state.lecture_map is not None
        subject = map_subject_sha256(
            self.state.lecture_map,
            self.state.policies.lecture_mapping,
        )
        self.advance(ApproveLectureMap("approve_lecture_map", subject))

    def reach_production(
        self,
    ) -> tuple[WorkflowArtifactReference, bytes]:
        self.assess()
        self.approve_priority()
        lecture_map = self.propose_map()
        self.approve_map()
        return lecture_map

    def document(self, marker: str) -> LectureDocument:
        source_text = f"# Invented lecture\n\nSynthetic continuation {marker}.\n"
        document = LectureDocument(
            "lecture-document/v1",
            "l1",
            1,
            source_text,
            SourceProvenance(hashlib.sha256(source_text.encode()).hexdigest()),
        )
        saved = self.document_store.save(document)
        self.assertEqual(saved.document_id, "l1")  # type: ignore[union-attr]
        return document

    def submit(self, document: LectureDocument) -> None:
        self.advance(SubmitLectureCandidate("submit_lecture_candidate", "l1", document))

    def validate_candidate(
        self,
        disposition: str,
    ) -> tuple[WorkflowArtifactReference, bytes]:
        progress = self.state.lecture_progress[0]
        assert progress.candidate is not None
        subject = candidate_subject_sha256(progress.candidate)
        evidence = self.artifact("validation")
        policy = self.state.policies.lecture_validation
        self.advance(
            RecordCandidateValidation(
                "record_candidate_validation",
                "l1",
                subject,
                ValidationRecord(
                    subject,
                    policy.policy_version,
                    policy.content_sha256,
                    disposition,
                    evidence[0],
                ),
            )
        )
        return evidence

    def reopen(self) -> object:
        return reopen_course_workflow_continuation(
            self.context(),
            artifact_store=self.artifact_store,
            document_store=self.document_store,
        )

    def code(self, result: object) -> str:
        self.assertIsInstance(result, CourseWorkflowContinuationFailure)
        return result.diagnostics[0].code  # type: ignore[union-attr]

    def test_public_contract_is_immutable_and_content_safe_in_repr(self) -> None:
        expected_exports = {
            "ActiveLectureCandidate",
            "BlockedWorkflowContext",
            "ContinuationLectureProgress",
            "CourseWorkflowContinuation",
            "CourseWorkflowContinuationDiagnostic",
            "CourseWorkflowContinuationFailure",
            "CourseWorkflowContinuationResult",
            "FailedWorkflowContext",
            "MapReopenContinuationContext",
            "ReopenedLectureDocument",
            "ReopenedWorkflowArtifact",
            "reopen_course_workflow_continuation",
        }
        self.assertTrue(expected_exports <= set(course_compiler.__all__))
        reference, payload = self.artifact("decision")
        reopened = ReopenedWorkflowArtifact(reference, payload.decode())
        self.assertNotIn(payload.decode(), repr(reopened))
        with self.assertRaises(FrozenInstanceError):
            reopened.content = "changed"  # type: ignore[misc]
        with self.assertRaises(ValueError):
            CourseWorkflowContinuationDiagnostic("unknown", "storage", "unknown")  # type: ignore[arg-type]

    def test_priority_approval_reopens_exact_semantic_artifacts_and_subject(self) -> None:
        expected = self.assess()
        result = self.reopen()
        self.assertIsInstance(result, CourseWorkflowContinuation)
        assert isinstance(result, CourseWorkflowContinuation)
        self.assertEqual(result.handoff.stage, "priority_approval")
        actual = (
            result.source_assessment,
            result.priority_proposal,
            result.evidence_hierarchy,
        )
        for reopened, (reference, payload) in zip(actual, expected):
            assert reopened is not None
            self.assertEqual(reopened.reference, reference)
            self.assertEqual(reopened.content, payload.decode())
        assert self.state.priority_basis is not None
        self.assertEqual(
            result.priority_subject_sha256,
            priority_subject_sha256(
                self.state.priority_basis,
                self.state.policies.priority_basis,
            ),
        )
        self.approve_priority()
        mapping = self.reopen()
        self.assertIsInstance(mapping, CourseWorkflowContinuation)
        assert isinstance(mapping, CourseWorkflowContinuation)
        self.assertEqual(mapping.handoff.stage, "lecture_mapping")
        self.assertEqual(
            (
                mapping.source_assessment.reference,
                mapping.priority_proposal.reference,
                mapping.evidence_hierarchy.reference,
            ),  # type: ignore[union-attr]
            tuple(item[0] for item in expected),
        )

    def test_map_approval_reopens_exact_map_and_authoritative_subject(self) -> None:
        self.assess()
        self.approve_priority()
        reference, payload = self.propose_map()
        result = self.reopen()
        self.assertIsInstance(result, CourseWorkflowContinuation)
        assert isinstance(result, CourseWorkflowContinuation)
        assert result.lecture_map is not None
        self.assertEqual(result.lecture_map.reference, reference)
        self.assertEqual(result.lecture_map.content, payload.decode())
        assert self.state.lecture_map is not None
        self.assertEqual(
            result.map_subject_sha256,
            map_subject_sha256(
                self.state.lecture_map,
                self.state.policies.lecture_mapping,
            ),
        )
        self.assertEqual([item.lecture_id for item in result.lecture_progress], ["l1"])

    def test_lecture_validation_reopens_exact_active_candidate(self) -> None:
        self.reach_production()
        document = self.document("active candidate")
        self.submit(document)
        result = self.reopen()
        self.assertIsInstance(result, CourseWorkflowContinuation)
        assert isinstance(result, CourseWorkflowContinuation)
        assert result.active_candidate is not None
        reference = self.state.lecture_progress[0].candidate
        assert reference is not None
        self.assertEqual(result.active_candidate.document_reference, reference)
        self.assertEqual(result.active_candidate.source_text, document.source_text)
        self.assertEqual(
            result.active_candidate.candidate_subject_sha256,
            candidate_subject_sha256(reference),
        )
        self.assertEqual(result.lecture_progress[0].candidate_document_reference, reference)

    def test_retry_validation_and_completed_documents_are_exact(self) -> None:
        self.reach_production()
        first = self.document("first rejected candidate")
        self.submit(first)
        validation_reference, validation_payload = self.validate_candidate("rejected")
        retry = self.reopen()
        self.assertIsInstance(retry, CourseWorkflowContinuation)
        assert isinstance(retry, CourseWorkflowContinuation)
        progress = retry.lecture_progress[0]
        self.assertEqual(progress.status, "retry_required")
        self.assertEqual(progress.validation_reference, validation_reference)
        assert progress.validation_evidence is not None
        self.assertEqual(progress.validation_evidence.reference, validation_reference)
        self.assertEqual(progress.validation_evidence.content, validation_payload.decode())

        second = self.document("second accepted candidate")
        self.submit(second)
        self.validate_candidate("passed_with_warnings")
        completed = self.reopen()
        self.assertIsInstance(completed, CourseWorkflowContinuation)
        assert isinstance(completed, CourseWorkflowContinuation)
        self.assertEqual(completed.handoff.stage, "completed")
        self.assertEqual(len(completed.accepted_documents), 1)
        self.assertEqual(completed.accepted_documents[0].source_text, second.source_text)
        self.assertEqual(
            completed.accepted_documents[0].document_reference,
            self.state.lecture_progress[0].accepted_document,
        )

    def test_failed_and_blocked_contexts_reopen_exact_recovery_evidence(self) -> None:
        failure_reference, failure_payload = self.artifact("failure")
        failed = self.advance(
            RecordOperationFailure(
                "record_operation_failure",
                "record_source_assessment",
                "source_assessment_operation_failed",
                "source-1",
                failure_reference,
            )
        )
        self.assertIsInstance(failed, WorkflowFailed)
        result = self.reopen()
        self.assertIsInstance(result, CourseWorkflowContinuation)
        assert isinstance(result, CourseWorkflowContinuation)
        assert result.failed_workflow is not None
        self.assertEqual(result.failed_workflow.failed_action, "record_source_assessment")
        self.assertEqual(result.failed_workflow.failure_code, "source_assessment_operation_failed")
        self.assertEqual(result.failed_workflow.subject_id, "source-1")
        self.assertEqual(result.failed_workflow.evidence.reference, failure_reference)
        self.assertEqual(result.failed_workflow.evidence.content, failure_payload.decode())
        self.assertEqual(result.failed_workflow.failure_evidence_sha256, failure_reference.content_sha256)

        self.state = initial_state()
        self.payloads = policy_payloads(self.state)
        blocker_reference, blocker_payload = self.artifact("blocker")
        subject = "a" * 64
        blocked = self.advance(
            MarkBlocked(
                "mark_blocked",
                "private_artifact_unavailable",
                "source-1",
                subject,
                blocker_reference,
            )
        )
        self.assertIsInstance(blocked, WorkflowBlocked)
        result = self.reopen()
        self.assertIsInstance(result, CourseWorkflowContinuation)
        assert isinstance(result, CourseWorkflowContinuation)
        assert result.blocked_workflow is not None
        self.assertEqual(result.blocked_workflow.blocker_code, "private_artifact_unavailable")
        self.assertEqual(result.blocked_workflow.subject_sha256, subject)
        self.assertEqual(result.blocked_workflow.evidence.reference, blocker_reference)
        self.assertEqual(result.blocked_workflow.evidence.content, blocker_payload.decode())

    def test_pending_new_source_and_map_reopen_baseline_are_complete_and_exact(self) -> None:
        map_reference, map_payload = self.reach_production()
        pending = make_source("source-2", "invented-pending-source")
        blocker_reference, _ = self.artifact("blocker")
        self.advance(RecordNewSource("record_new_source", pending, blocker_reference))
        blocked = self.reopen()
        self.assertIsInstance(blocked, CourseWorkflowContinuation)
        assert isinstance(blocked, CourseWorkflowContinuation)
        self.assertEqual(blocked.pending_source, pending)
        self.assertEqual(blocked.blocked_workflow.evidence.reference, blocker_reference)  # type: ignore[union-attr]

        self.advance(ApproveMapReopen("approve_map_reopen", pending.content_sha256))
        reopened = self.reopen()
        self.assertIsInstance(reopened, CourseWorkflowContinuation)
        assert isinstance(reopened, CourseWorkflowContinuation)
        self.assertEqual(reopened.handoff.stage, "source_assessment")
        assert reopened.map_reopen is not None
        self.assertEqual(reopened.map_reopen.baseline_lecture_map.reference, map_reference)
        self.assertEqual(reopened.map_reopen.baseline_lecture_map.content, map_payload.decode())
        self.assertEqual(reopened.map_reopen.baseline_lecture_ids, ("l1",))
        self.assertEqual(reopened.map_reopen.baseline_reserved_lecture_ids, ())
        assert self.state.map_reopen_context is not None
        self.assertEqual(
            reopened.map_reopen.baseline_map_subject_sha256,
            self.state.map_reopen_context.baseline_map_subject_sha256,
        )
        self.assertIsNone(reopened.pending_source)

    def test_artifact_missing_mismatch_invalid_utf8_and_store_failures_fail_closed(self) -> None:
        missing, _ = self.artifact("source_assessment", save=False)
        proposal = self.artifact("priority_proposal")
        hierarchy = self.artifact("evidence_hierarchy")
        self.advance(
            RecordSourceAssessment(
                "record_source_assessment",
                missing,
                "exam_driven",
                proposal[0],
                hierarchy[0],
            )
        )
        self.assertEqual(self.code(self.reopen()), "workflow_artifact_not_found")

        mismatched_reference, mismatched_payload = proposal
        with mock.patch.object(
            LocalWorkflowArtifactStore,
            "load",
            return_value=workflow_artifact_persistence.WorkflowArtifactPayload(
                mismatched_reference,
                mismatched_payload,
            ),
        ):
            self.assertEqual(
                self.code(self.reopen()),
                "inconsistent_reopened_workflow_artifact",
            )

        self.state = initial_state()
        self.payloads = policy_payloads(self.state)
        invalid_reference, _ = self.artifact("source_assessment", b"\xff")
        proposal = self.artifact("priority_proposal")
        hierarchy = self.artifact("evidence_hierarchy")
        self.advance(
            RecordSourceAssessment(
                "record_source_assessment",
                invalid_reference,
                "exam_driven",
                proposal[0],
                hierarchy[0],
            )
        )
        self.assertEqual(self.code(self.reopen()), "workflow_artifact_invalid_utf8")

        with mock.patch.object(
            LocalWorkflowArtifactStore,
            "load",
            return_value=workflow_artifact_persistence._failure(
                "persistence_unavailable"
            ),
        ):
            self.assertEqual(self.code(self.reopen()), "workflow_artifact_store_failed")

    def test_missing_candidate_document_and_document_store_failure_are_fixed(self) -> None:
        self.reach_production()
        document = self.document("candidate removed from persistence fixture")
        self.submit(document)
        with mock.patch.object(
            LocalLectureDocumentStore,
            "load",
            return_value=lecture_document_persistence._failure("document_not_found"),
        ):
            self.assertEqual(self.code(self.reopen()), "lecture_document_not_found")
        with mock.patch.object(
            LocalLectureDocumentStore,
            "load",
            return_value=lecture_document_persistence._failure(
                "persistence_unavailable"
            ),
        ):
            self.assertEqual(self.code(self.reopen()), "lecture_document_store_failed")

    def test_unexpected_exception_is_redacted_and_baseexception_propagates(self) -> None:
        self.assess()
        hostile = f"{PRIVATE_MARKER} /private/db.sqlite SELECT payload"
        with mock.patch.object(
            LocalWorkflowArtifactStore,
            "load",
            side_effect=RuntimeError(hostile),
        ):
            result = self.reopen()
        self.assertEqual(self.code(result), "continuation_reopen_exception")
        self.assertNotIn(PRIVATE_MARKER, repr(result))
        self.assertNotIn("SELECT payload", repr(result))

        sentinel = ReopenSentinel(PRIVATE_MARKER)
        with mock.patch.object(
            LocalWorkflowArtifactStore,
            "load",
            side_effect=sentinel,
        ), self.assertRaises(ReopenSentinel) as raised:
            self.reopen()
        self.assertIs(raised.exception, sentinel)

    def test_continuation_read_never_writes(self) -> None:
        self.assess()
        with (
            mock.patch.object(
                LocalWorkflowArtifactStore,
                "save",
                side_effect=AssertionError("artifact save called"),
            ) as artifact_save,
            mock.patch.object(
                LocalLectureDocumentStore,
                "save",
                side_effect=AssertionError("document save called"),
            ) as document_save,
        ):
            self.assertIsInstance(self.reopen(), CourseWorkflowContinuation)
        artifact_save.assert_not_called()
        document_save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
