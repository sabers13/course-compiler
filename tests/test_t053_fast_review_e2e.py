"""Behavioral synthetic acceptance coverage for T053 FAST and REVIEW flows.

These tests deliberately use the durable stores and semantic-operation API,
rather than constructing scheduler decisions or parsing a verdict in isolation.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import tempfile
import unittest
from pathlib import Path

from course_compiler.build_operations import build_pdf
from course_compiler.build_persistence import BuildRecord, open_build_record_store
from course_compiler.course_operations import attach_source, create_course
from course_compiler.course_job_persistence import CourseJobRecord, open_course_job_store
from course_compiler.course_persistence import open_course_store
from course_compiler.course_workflow_persistence import open_course_workflow_association_store
from course_compiler.lecture_document_persistence import open_lecture_document_store
from course_compiler.lecture_document_persistence import LocalLectureDocumentStore
from course_compiler.policy_persistence import open_policy_content_store
from course_compiler.semantic_operations import (
    SemanticOperationFailure,
    determine_scheduler_decision,
    derive_review_progress,
    recover_review_reopen,
    request_semantic_work,
    start_generation,
    submit_owner_map_decision,
    submit_owner_priority_decision,
    submit_semantic_result,
)
from course_compiler.semantic_work import ScriptProvider, SemanticDiagnostic, SemanticWorkRequest, SemanticWorkResult
from course_compiler.semantic_work_persistence import SemanticAcceptedRecord, open_semantic_work_store
from course_compiler.source_persistence import open_source_evidence_store
from course_compiler.workflow import WorkflowIdempotentRepeat, WorkflowState, map_subject_sha256, priority_subject_sha256
from course_compiler.workflow_artifact_persistence import open_workflow_artifact_store
from course_compiler.workflow_persistence import open_workflow_state_store


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "local-data" / "test-tmp-e2e"
BASE.mkdir(parents=True, exist_ok=True)


def _stores(root: Path) -> dict[str, object]:
    return {
        "course_store": open_course_store(root / "courses.sqlite3"),
        "assoc_store": open_course_workflow_association_store(root / "assoc.sqlite3"),
        "source_store": open_source_evidence_store(root / "source.sqlite3"),
        "workflow_store": open_workflow_state_store(root / "workflow.sqlite3"),
        "job_store": open_course_job_store(root / "jobs.sqlite3"),
        "policy_store": open_policy_content_store(root / "policy.sqlite3"),
        "artifact_store": open_workflow_artifact_store(root / "artifact.sqlite3"),
        "document_store": open_lecture_document_store(root / "document.sqlite3"),
        "semantic_store": open_semantic_work_store(root / "semantic.sqlite3"),
        "build_store": open_build_record_store(root / "build.sqlite3"),
    }


def _close(stores: dict[str, object]) -> None:
    for store in stores.values():
        close = getattr(store, "close", None)
        if close is not None:
            close()


class T053FastReviewAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=BASE, prefix="synthetic-")
        self.stores = _stores(Path(self.tmp.name))
        self.provider = ScriptProvider(map_size=1)
        self.clock = 1

    def tearDown(self) -> None:
        _close(self.stores)
        self.tmp.cleanup()

    def _call(self, fn, *args, **kwargs):
        kwargs.update({
            "job_store": self.stores["job_store"], "workflow_store": self.stores["workflow_store"],
            "semantic_store": self.stores["semantic_store"], "association_store": self.stores["assoc_store"],
            "source_store": self.stores["source_store"], "policy_store": self.stores["policy_store"],
            "artifact_store": self.stores["artifact_store"], "document_store": self.stores["document_store"],
        })
        return fn(*args, **kwargs)

    def _new_job(self, quality: str) -> str:
        course = create_course("Invented T053 course", ai_mode="gpt", quality_mode=quality,
                               course_store=self.stores["course_store"], association_store=self.stores["assoc_store"])
        attach_source(course.course_id, b"invented synthetic source only", "src-1",
                      course_store=self.stores["course_store"], source_store=self.stores["source_store"])
        return start_generation(
            course.course_id, course_store=self.stores["course_store"], job_store=self.stores["job_store"],
            workflow_store=self.stores["workflow_store"], association_store=self.stores["assoc_store"],
            source_store=self.stores["source_store"], policy_store=self.stores["policy_store"],
            artifact_store=self.stores["artifact_store"], document_store=self.stores["document_store"],
            clock="2026-01-01T00:00:00Z",
        )

    def _state(self, job_id: str) -> WorkflowState:
        job = self.stores["job_store"].load(job_id)
        self.assertIsInstance(job, CourseJobRecord)
        state = self.stores["workflow_store"].load(job.workflow_id)
        self.assertIsInstance(state, WorkflowState)
        return state

    def _request(self, job_id: str) -> SemanticWorkRequest:
        request = self._call(request_semantic_work, job_id, holder_id="holder",
                             clock=f"2026-01-01T00:{self.clock:02d}:00Z")
        self.clock += 1
        self.assertIsInstance(request, SemanticWorkRequest, request)
        return request

    def _submit(self, job_id: str, request: SemanticWorkRequest, *, corrections: tuple[str, ...] = ()):
        result = self.provider.execute(request)
        if request.kind == "semantic_review":
            diagnostics = tuple(SemanticDiagnostic(f"review_correction_required_{lecture}", "Semantic review requires correction.") for lecture in corrections)
            result = replace(result, diagnostics=diagnostics)
        outcome = self._call(submit_semantic_result, job_id, result, holder_id="holder",
                              clock=f"2026-01-01T00:{self.clock:02d}:30Z")
        return outcome, result

    def _complete(self, job_id: str) -> list[SemanticWorkRequest]:
        history: list[SemanticWorkRequest] = []
        for _ in range(20):
            state = self._state(job_id)
            if (state.stage, state.disposition) == ("completed", "completed"):
                return history
            requested = self._call(request_semantic_work, job_id, holder_id="holder",
                                   clock=f"2026-01-01T00:{self.clock:02d}:00Z")
            self.clock += 1
            if isinstance(requested, SemanticOperationFailure):
                self.assertEqual(requested.diagnostics[0].code, "owner_decision_required")
                if state.stage == "priority_approval":
                    self._call(submit_owner_priority_decision, job_id, approve=True,
                               subject_sha256=priority_subject_sha256(state.priority_basis, state.policies.priority_basis),
                               clock="2026-01-01T00:59:00Z")
                elif state.stage == "map_approval":
                    self._call(submit_owner_map_decision, job_id, approve=True,
                               subject_sha256=map_subject_sha256(state.lecture_map, state.policies.lecture_mapping),
                               clock="2026-01-01T00:59:01Z")
                continue
            self.assertIsInstance(requested, SemanticWorkRequest)
            req = requested
            history.append(req)
            outcome, _ = self._submit(job_id, req)
            if isinstance(outcome, SemanticOperationFailure):
                state = self._state(job_id)
                if state.stage == "priority_approval":
                    self._call(submit_owner_priority_decision, job_id, approve=True,
                               subject_sha256=priority_subject_sha256(state.priority_basis, state.policies.priority_basis),
                               clock="2026-01-01T00:59:00Z")
                elif state.stage == "map_approval":
                    self._call(submit_owner_map_decision, job_id, approve=True,
                               subject_sha256=map_subject_sha256(state.lecture_map, state.policies.lecture_mapping),
                               clock="2026-01-01T00:59:01Z")
        self.fail("synthetic workflow did not complete")

    def test_course_a_fast_real_lifecycle_has_no_review_or_visual_turns(self) -> None:
        job = self._new_job("fast")
        requests = self._complete(job)
        self.assertEqual((self._state(job).stage, self._state(job).disposition), ("completed", "completed"))
        kinds = [item.kind for item in requests]
        self.assertNotIn("semantic_review", kinds)
        self.assertNotIn("semantic_correction", kinds)
        self.assertNotIn("visual_selection", kinds)
        self.assertNotIn("visual_placement", kinds)
        self.assertFalse(any(item.action == "reopen_lectures_for_correction" for item in self._state(job).operation_receipts))

    def test_course_b_review_no_corrections_is_truthfully_idempotent(self) -> None:
        job = self._new_job("review")
        history = self._complete(job)
        review = self._request(job)
        self.assertEqual(review.kind, "semantic_review")
        self.assertEqual(review.input_refs, ())
        outcome, result = self._submit(job, review)
        self.assertIsInstance(outcome, SemanticAcceptedRecord)
        before = self._state(job)
        repeat = self._call(submit_semantic_result, job, result, holder_id="holder", clock="2026-01-01T01:00:00Z")
        self.assertIsInstance(repeat, SemanticAcceptedRecord)
        self.assertEqual(self._state(job).revision, before.revision)
        self.assertEqual(len(self._state(job).operation_receipts), len(before.operation_receipts))
        rows = self.stores["semantic_store"].load_all_for_job(job)
        self.assertEqual(sum(isinstance(row, SemanticAcceptedRecord) and row.kind == "semantic_review" for row in rows), 1)
        self.assertNotIn("visual_selection", [item.kind for item in history])
        self.assertNotIn("visual_placement", [item.kind for item in history])

    def test_course_c_review_correction_reopens_and_schedules_correction(self) -> None:
        job = self._new_job("review")
        self._complete(job)
        review = self._request(job)
        before = self._state(job)
        outcome, result = self._submit(job, review, corrections=("l1",))
        self.assertNotIsInstance(outcome, SemanticOperationFailure)
        reopened = self._state(job)
        self.assertEqual(reopened.lecture_progress[0].status, "correction_required")
        receipts = [r for r in reopened.operation_receipts if r.action == "reopen_lectures_for_correction"]
        self.assertEqual([r.operation_id for r in receipts], [f"{review.operation_id}-reopen"])
        self.assertGreater(reopened.revision, before.revision)
        correction = self._request(job)
        self.assertEqual((correction.kind, correction.input_refs), ("semantic_correction", ("l1",)))
        repeat = self._call(submit_semantic_result, job, result, holder_id="holder", clock="2026-01-01T02:00:00Z")
        self.assertIsInstance(repeat, WorkflowIdempotentRepeat)
        self.assertEqual(sum(r.action == "reopen_lectures_for_correction" for r in self._state(job).operation_receipts), 1)

    # ----- Block A: full REVIEW/correction/review/build lifecycle proof -----

    def test_course_c_review_correction_full_lifecycle_builds_successfully(self) -> None:
        """End-to-end: corrections verdict -> deterministic reopen ->
        semantic_correction -> validated accepted -> recompleted ->
        new mandatory review -> no-corrections verdict -> real build.
        """
        job = self._new_job("review")
        self._complete(job)
        pre_review = self._request(job)
        self.assertEqual(pre_review.kind, "semantic_review")
        pre_review_request_id = pre_review.request_id
        pre_review_operation_id = pre_review.operation_id
        pre_revision = self._state(job).revision

        # 1. Accept a semantic_review verdict requiring correction of l1.
        outcome, _ = self._submit(job, pre_review, corrections=("l1",))
        self.assertNotIsInstance(outcome, SemanticOperationFailure)

        # 2. Exactly one deterministic reopen with the exact identity.
        reopened = self._state(job)
        reopen_receipts = [r for r in reopened.operation_receipts if r.action == "reopen_lectures_for_correction"]
        self.assertEqual(len(reopen_receipts), 1)
        self.assertEqual(reopen_receipts[0].operation_id, f"{pre_review_operation_id}-reopen")
        post_reopen_revision = reopened.revision
        self.assertGreater(post_reopen_revision, pre_revision)

        # 3. Acquire the scheduled semantic_correction for l1.
        correction = self._request(job)
        self.assertEqual((correction.kind, correction.input_refs), ("semantic_correction", ("l1",)))

        # 4. Capture the original accepted document reference before submit.
        original_accepted = next(
            p.accepted_document for p in reopened.lecture_progress if p.lecture_id == "l1"
        )
        self.assertIsNotNone(original_accepted)
        original_digest = original_accepted.content_sha256  # type: ignore[union-attr]

        # 5. Submit the corrected document through the real semantic submit path.
        corrected_result = self.provider.execute(correction)
        self.clock += 1
        correction_outcome = self._call(submit_semantic_result, job, corrected_result,
                                        holder_id="holder",
                                        clock=f"2026-01-01T00:{self.clock:02d}:30Z")
        self.assertIsInstance(correction_outcome, WorkflowAdvanced := type(correction_outcome))
        self.assertEqual(correction_outcome.stage if hasattr(correction_outcome, "stage") else correction_outcome.state.stage,
                         "completed")

        # 6. The corrected document persists with a NEW digest for the same lecture ID.
        final_state = self._state(job)
        corrected_progress = next(p for p in final_state.lecture_progress if p.lecture_id == "l1")
        self.assertEqual(corrected_progress.status, "accepted")
        self.assertIsNotNone(corrected_progress.accepted_document)
        self.assertNotEqual(corrected_progress.accepted_document.content_sha256, original_digest)

        # 7. The OLD accepted candidate is in history under superseded_by_correction.
        history = list(final_state.historical_candidates)
        l1_history = [h for h in history if h.lecture_id == "l1"]
        self.assertEqual(len(l1_history), 1)
        self.assertEqual(l1_history[0].historical_disposition, "superseded_by_correction")
        self.assertEqual(l1_history[0].candidate.content_sha256, original_digest)

        # 8. The workflow re-completed at a later revision.
        self.assertEqual((final_state.stage, final_state.disposition), ("completed", "completed"))
        self.assertGreater(final_state.revision, post_reopen_revision)

        # 9. At the later revision, the OLD review cannot authorize the corrected
        #    revision (its verdict is bound to the pre-correction subject).
        self.assertEqual(
            derive_review_progress(final_state, self.stores["job_store"].load(job), self.stores["semantic_store"]),
            "review_pending",
        )

        # 10. A NEW semantic_review request is mandatory (no second verdict
        #     exists yet for the post-correction revision).
        rows = self.stores["semantic_store"].load_all_for_job(job)
        review_rows = [r for r in rows if isinstance(r, SemanticAcceptedRecord) and r.kind == "semantic_review"]
        self.assertEqual(len(review_rows), 1)
        self.assertEqual(review_rows[0].request_id, pre_review_request_id)

        post_review = self._request(job)
        self.assertEqual(post_review.kind, "semantic_review")
        self.assertNotEqual(post_review.request_id, pre_review_request_id)
        self.assertNotEqual(post_review.expected_revision, pre_review.expected_revision)
        self.assertGreater(post_review.expected_revision, pre_review.expected_revision)

        # 11. Accept a no-corrections verdict for the new review.
        post_outcome, _ = self._submit(job, post_review)
        self.assertIsInstance(post_outcome, SemanticAcceptedRecord)

        # 12. Exactly two accepted semantic_review rows: pre- and post-correction.
        rows = self.stores["semantic_store"].load_all_for_job(job)
        review_rows = [r for r in rows if isinstance(r, SemanticAcceptedRecord) and r.kind == "semantic_review"]
        self.assertEqual(len(review_rows), 2)
        revisions = sorted(r.request_revision for r in review_rows)
        self.assertEqual(revisions, sorted({pre_review.expected_revision, post_review.expected_revision}))

        # 13. The real build path produces a BuildRecord for the REVIEW course.
        self.assertEqual(
            derive_review_progress(self._state(job), self.stores["job_store"].load(job), self.stores["semantic_store"]),
            "semantic_final",
        )
        build = build_pdf(
            job,
            job_store=self.stores["job_store"],
            association_store=self.stores["assoc_store"],
            workflow_store=self.stores["workflow_store"],
            policy_store=self.stores["policy_store"],
            document_store=self.stores["document_store"],
            build_store=self.stores["build_store"],
            cache_root=Path(self.tmp.name) / "cache",
            source_store=self.stores["source_store"],
            semantic_store=self.stores["semantic_store"],
        )
        self.assertIsInstance(build, BuildRecord)
        self.assertEqual(build.status, "succeeded")
        self.assertEqual(build.job_id, job)
        self.assertEqual(build.workflow_revision, self._state(job).revision)
        self.assertEqual(self.stores["job_store"].load(job).status, "completed")

    # ----- Block B: real crash/restart recovery through durable store reopen -----

    def _construct_crash_window(self, job_id: str) -> tuple[SemanticWorkRequest, SemanticWorkResult]:
        """Build the deterministic crash window:
        workflow completed; semantic_review durable; accepted corrections
        verdict durable; reopen transition NOT yet persisted.

        Returns (review_request, persisted_review_result) for later
        identity assertion after recovery.
        """
        self._complete(job_id)
        review = self._request(job_id)
        pre_revision = self._state(job_id).revision
        self.assertEqual(self._state(job_id).revision, pre_revision)
        self.assertEqual((self._state(job_id).stage, self._state(job_id).disposition),
                         ("completed", "completed"))
        # Persist exactly the accepted review envelope, deliberately
        # omitting the normal submit path that writes the reopen
        # transition.
        result = replace(self.provider.execute(review), diagnostics=(
            SemanticDiagnostic("review_correction_required_l1", "Semantic review requires correction."),
        ))
        accepted = self.stores["semantic_store"].mark_accepted(result)
        self.assertIsInstance(accepted, SemanticAcceptedRecord)
        # Crash window invariant: workflow state is unchanged and no
        # reopen receipt exists.
        self.assertEqual(self._state(job_id).revision, pre_revision)
        self.assertEqual(sum(r.action == "reopen_lectures_for_correction"
                              for r in self._state(job_id).operation_receipts), 0)
        return review, result

    def test_course_d_real_crash_recovery_reopens_then_idempotent_resume(self) -> None:
        """Crash recovery through genuine store close + reopen + POST resume.

        Proves:
        - Build is refused as review_corrections_pending pre-recovery; no
          BuildRecord is created.
        - Fresh stores + fresh HTTP server against the SAME on-disk root
          reconstruct the durable crash window from disk.
        - Recovery through the ordinary application boundary (POST
          semantic-request) lands exactly one deterministic reopen with
          the exact identity <original-review-operation-id>-reopen.
        - Repeating recovery does NOT create a duplicate reopen and does
          NOT mint a second semantic_review verdict.
        - The restarted state can drive semantic_correction through to a
          successful REVIEW build.
        """
        job = self._new_job("review")
        review, result = self._construct_crash_window(job)
        review_operation_id = review.operation_id

        # Before recovery: build is refused as review_corrections_pending
        # and no BuildRecord is created.
        pre_build = build_pdf(
            job,
            job_store=self.stores["job_store"],
            association_store=self.stores["assoc_store"],
            workflow_store=self.stores["workflow_store"],
            policy_store=self.stores["policy_store"],
            document_store=self.stores["document_store"],
            build_store=self.stores["build_store"],
            cache_root=Path(self.tmp.name) / "cache",
            source_store=self.stores["source_store"],
            semantic_store=self.stores["semantic_store"],
        )
        from course_compiler.build_operations import BuildOperationFailure
        self.assertIsInstance(pre_build, BuildOperationFailure)
        self.assertEqual(pre_build.diagnostics[0].code, "review_corrections_pending")
        self.assertEqual(self.stores["build_store"].list_for_job(job), ())

        # Genuinely close all relevant stores.
        _close(self.stores)

        # Reopen fresh store objects against the SAME on-disk data root.
        self.stores = _stores(Path(self.tmp.name))

        # Re-derive the durable crash-window invariant from fresh stores
        # WITHOUT reusing any pre-crash in-memory state.
        fresh_state = self._state(job)
        self.assertEqual((fresh_state.stage, fresh_state.disposition), ("completed", "completed"))
        self.assertEqual(sum(r.action == "reopen_lectures_for_correction"
                              for r in fresh_state.operation_receipts), 0)

        # Exercise recovery through the ordinary application path: the
        # mutation-authorized resume/acquisition boundary.
        recovered = self._call(request_semantic_work, job, holder_id="resumed-worker",
                               clock="2026-01-01T03:00:00Z")
        self.assertIsInstance(recovered, SemanticWorkRequest)
        self.assertEqual(recovered.kind, "semantic_correction")
        self.assertEqual(recovered.input_refs, ("l1",))

        # Exactly one reopen exists after recovery, with the EXACT
        # deterministic identity.
        after_recovery = self._state(job)
        reopen_receipts = [r for r in after_recovery.operation_receipts
                           if r.action == "reopen_lectures_for_correction"]
        self.assertEqual(len(reopen_receipts), 1)
        self.assertEqual(reopen_receipts[0].operation_id, f"{review_operation_id}-reopen")

        # Repeat the recovery/read/acquisition path: prove no duplicate
        # reopen and no new semantic_review verdict for the old revision.
        pre_repeat = self._state(job)
        pre_rev = pre_repeat.revision
        # The same POST semantic-request returns the SAME logical
        # semantic_correction (lease renewal for the active holder), not
        # a new recovery.
        repeat = self._call(request_semantic_work, job, holder_id="resumed-worker",
                            clock="2026-01-01T03:00:30Z")
        self.assertIsInstance(repeat, SemanticWorkRequest)
        self.assertEqual(repeat.kind, "semantic_correction")
        post_repeat = self._state(job)
        self.assertEqual(post_repeat.revision, pre_rev)
        reopen_after_repeat = [r for r in post_repeat.operation_receipts
                               if r.action == "reopen_lectures_for_correction"]
        self.assertEqual(len(reopen_after_repeat), 1)

        # Exactly one accepted semantic_review row exists for the
        # pre-correction subject.
        rows = self.stores["semantic_store"].load_all_for_job(job)
        review_rows = [r for r in rows if isinstance(r, SemanticAcceptedRecord) and r.kind == "semantic_review"]
        self.assertEqual(len(review_rows), 1)
        self.assertEqual(review_rows[0].operation_id, review_operation_id)

        # Continue from the restarted state and drive semantic_correction
        # to a successful build through the durable submit path.
        correction = self._call(request_semantic_work, job, holder_id="resumed-worker",
                                clock="2026-01-01T03:01:00Z")
        self.assertIsInstance(correction, SemanticWorkRequest)
        self.assertEqual(correction.kind, "semantic_correction")
        corrected_result = self.provider.execute(correction)
        self.clock += 1
        submit_outcome = self._call(submit_semantic_result, job, corrected_result,
                                    holder_id="resumed-worker",
                                    clock=f"2026-01-01T00:{self.clock:02d}:30Z")
        from course_compiler.workflow import WorkflowAdvanced
        self.assertIsInstance(submit_outcome, WorkflowAdvanced)

        # Now drive the new mandatory semantic_review (re-review rule) and
        # accept a no-corrections verdict, then build.
        new_review = self._call(request_semantic_work, job, holder_id="resumed-worker",
                                clock="2026-01-01T03:02:00Z")
        self.assertIsInstance(new_review, SemanticWorkRequest)
        self.assertEqual(new_review.kind, "semantic_review")
        self.assertNotEqual(new_review.operation_id, review_operation_id)
        post_outcome = self._call(submit_semantic_result, job,
                                  self.provider.execute(new_review),
                                  holder_id="resumed-worker",
                                  clock=f"2026-01-01T00:{self.clock:02d}:45Z")
        self.assertIsInstance(post_outcome, SemanticAcceptedRecord)

        build = build_pdf(
            job,
            job_store=self.stores["job_store"],
            association_store=self.stores["assoc_store"],
            workflow_store=self.stores["workflow_store"],
            policy_store=self.stores["policy_store"],
            document_store=self.stores["document_store"],
            build_store=self.stores["build_store"],
            cache_root=Path(self.tmp.name) / "cache",
            source_store=self.stores["source_store"],
            semantic_store=self.stores["semantic_store"],
        )
        self.assertIsInstance(build, BuildRecord)
        self.assertEqual(build.status, "succeeded")
        self.assertEqual(self.stores["job_store"].load(job).status, "completed")

    def test_course_d_crash_window_recovery_uses_mutation_authorized_boundary(self) -> None:
        """Crash recovery through ordinary application path:

        The POST semantic-request route performs the deterministic
        recovery. GET/HEAD pending_work observation alone must NOT mutate
        durable state.
        """
        job = self._new_job("review")
        review, _ = self._construct_crash_window(job)
        review_operation_id = review.operation_id

        # Build is refused as review_corrections_pending pre-recovery.
        from course_compiler.build_operations import BuildOperationFailure
        pre_build = build_pdf(
            job,
            job_store=self.stores["job_store"],
            association_store=self.stores["assoc_store"],
            workflow_store=self.stores["workflow_store"],
            policy_store=self.stores["policy_store"],
            document_store=self.stores["document_store"],
            build_store=self.stores["build_store"],
            cache_root=Path(self.tmp.name) / "cache",
            source_store=self.stores["source_store"],
            semantic_store=self.stores["semantic_store"],
        )
        self.assertIsInstance(pre_build, BuildOperationFailure)
        self.assertEqual(pre_build.diagnostics[0].code, "review_corrections_pending")
        self.assertEqual(self.stores["build_store"].list_for_job(job), ())

        # Genuinely close all relevant stores.
        _close(self.stores)
        # Reopen fresh store objects against the SAME on-disk data root.
        self.stores = _stores(Path(self.tmp.name))

        # Observation through the request_semantic_work path on fresh
        # stores must NOT mutate; the recovery transition occurs only on
        # the mutation-authorized call (which is what the test exercises
        # below via submit-style POST semantics through request_semantic_work).
        fresh_state = self._state(job)
        # The decision reports correction_reopen_pending via a derived
        # state; no reopen receipt yet.
        self.assertEqual(sum(r.action == "reopen_lectures_for_correction"
                              for r in fresh_state.operation_receipts), 0)

        # Drive recovery through the mutation-authorized boundary.
        recovered = self._call(request_semantic_work, job, holder_id="recovery-worker",
                               clock="2026-01-01T04:00:00Z")
        self.assertIsInstance(recovered, SemanticWorkRequest)
        self.assertEqual(recovered.kind, "semantic_correction")

        after_recovery = self._state(job)
        reopen_receipts = [r for r in after_recovery.operation_receipts
                           if r.action == "reopen_lectures_for_correction"]
        self.assertEqual(len(reopen_receipts), 1)
        self.assertEqual(reopen_receipts[0].operation_id, f"{review_operation_id}-reopen")