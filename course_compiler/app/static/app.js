// Course Compiler app — T048 durable Course list + New Course flow.
(function () {
  function toast(msg) {
    let el = document.getElementById("cc-toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "cc-toast";
      el.setAttribute("role", "status");
      el.style.cssText = "position:fixed;left:50%;bottom:18px;transform:translateX(-50%);background:var(--fg);color:var(--paper);padding:10px 14px;border-radius:8px;box-shadow:var(--shadow);font-size:13px;z-index:20;";
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.style.display = "block";
    setTimeout(function () { el.style.display = "none"; }, 2200);
  }

  document.querySelectorAll("button[disabled][title]").forEach(function (b) {
    b.addEventListener("click", function (e) {
      e.preventDefault();
      toast(b.getAttribute("title") || "Not available in this shell.");
    });
  });

  // Durable course list
  const listEl = document.getElementById("course-list");
  const emptyEl = document.getElementById("empty-state");
  const newBtn = document.getElementById("new-course-btn");
  const navNewBtn = document.getElementById("nav-new-course");
  const dialog = document.getElementById("new-course-dialog");
  const form = document.getElementById("new-course-form");
  const titleInput = document.getElementById("course-title");
  const aiSelect = document.getElementById("course-ai");
  const qualitySelect = document.getElementById("course-quality");
  const errorEl = document.getElementById("dialog-error");
  const cancelBtn = document.getElementById("dialog-cancel");
  // Ephemeral browser projection only. A fresh page load has no live-chat
  // context, so durable pending work is labelled Resume; accepted forward
  // turns in this page session are labelled Continue.
  const liveGptJobs = new Set();
  // Session-only attached source filenames, keyed by course. The server
  // projects a durable source_count; filenames are a browser convenience
  // so the user can see what was attached before generation locks sources.
  const attachedSources = {};
  // Session-cached build history per job, fetched once per loadCourses
  // cycle from the existing GET /api/jobs/{id}/builds route.
  let buildHistory = {};

  // Plain-language explanations for backend states. Every key below names
  // an existing server-side code; nothing here invents backend semantics.
  const BUILD_GATE_EXPLANATIONS = {
    semantic_review_pending: "Not yet — semantic review is pending. Continue in ChatGPT first.",
    review_corrections_pending: "Not yet — corrections are pending. Complete the correction turn, then re-review.",
    workflow_not_completed: "Not yet — generation is still in progress.",
    accepted_documents_unavailable: "Not yet — accepted documents are unavailable. Continue the workflow.",
    build_not_succeeded: "The last build did not succeed. Inspect the build history below.",
    build_identity_mismatch: "The build identity moved. Refresh and retry.",
    job_build_identity_mismatch: "The build identity moved. Refresh and retry.",
    artifact_integrity_mismatch: "The artifact failed its integrity check. Rebuild.",
    ephemeral_visual_inputs_rejected: "Ephemeral visual inputs are rejected. Continue the workflow.",
    cache_path_traversal_rejected: "A build path was rejected. Refresh and retry.",
  };

  const REVIEW_STATUS_EXPLANATIONS = {
    not_applicable: "Not applicable (FAST)",
    workflow_incomplete: "In progress — review runs after generation completes.",
    review_pending: "Awaiting semantic review. Continue in ChatGPT.",
    semantic_final: "Review complete — no corrections required.",
    correction_reopen_pending: "Corrections requested — correct in ChatGPT, then re-review is required before build.",
  };

  function reviewStatusText(review) {
    if (!review) return "";
    if (review.status === "not_applicable") return "Not applicable (FAST)";
    return REVIEW_STATUS_EXPLANATIONS[review.status] || review.status;
  }

  function correctionText(review) {
    const ids = (review && review.outstanding_lectures) || [];
    if (!ids.length) return "";
    const names = ids.map(function (id) { return "Lecture " + id; }).join(", ");
    return names + " — corrections requested. Continue in ChatGPT; re-review is required before build.";
  }

  function needsAttention(c) {
    if (c.job_status === "needs_attention") {
      return { kind: "attention", title: "Needs attention" + (c.failure_code ? " (" + c.failure_code + ")" : "") + ".",
        body: "The workflow is blocked. Continue the pending ChatGPT turn or owner approval below." };
    }
    if (c.job_status === "failed") {
      return { kind: "failure", title: "Failed" + (c.failure_code ? " (" + c.failure_code + ")" : "") + ".",
        body: "This job cannot continue. Create a new course to start over; completed artifacts remain downloadable where present." };
    }
    return null;
  }

  function buildState(c) {
    if (!c.active_job_id) return null;
    if (c.job_status === "completed") return { ready: true, reason: "" };
    // REVIEW authority takes precedence over generic Job status. A REVIEW
    // workflow that is completed/completed but still awaiting (re-)review
    // truthfully projects deterministic_building while authoritative
    // POST /build must reject with semantic_review_pending or
    // review_corrections_pending, so deterministic_building alone must
    // never enable Build PDF. FAST (not_applicable) and REVIEW
    // semantic_final are the only review authorities that permit it;
    // unknown or missing review authority fails closed.
    const review = c.content_review;
    if (review && review.status === "review_pending") return { ready: false, reason: BUILD_GATE_EXPLANATIONS.semantic_review_pending };
    if (review && review.status === "workflow_incomplete") return { ready: false, reason: BUILD_GATE_EXPLANATIONS.workflow_not_completed };
    if (review && (review.status === "correction_reopen_pending" || (review.outstanding_lectures && review.outstanding_lectures.length))) return { ready: false, reason: BUILD_GATE_EXPLANATIONS.review_corrections_pending };
    if (c.job_status === "deterministic_building") {
      if (review && (review.status === "semantic_final" || review.status === "not_applicable")) return { ready: true, reason: "" };
      return { ready: false, reason: "Build is available after generation and review complete." };
    }
    return { ready: false, reason: "Build is available after generation and review complete." };
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function sourceId() {
    const prefix = "src-ui-";
    try {
      const bytes = new Uint8Array(12);
      crypto.getRandomValues(bytes);
      return prefix + Array.prototype.map.call(bytes, function (b) {
        return b.toString(16).padStart(2, "0");
      }).join("");
    } catch (_) {
      return prefix + Date.now().toString(16) + Math.random().toString(16).slice(2, 14);
    }
  }

  function bytesToBase64(bytes) {
    const chunkSize = 0x8000;
    let binary = "";
    for (let offset = 0; offset < bytes.length; offset += chunkSize) {
      binary += String.fromCharCode.apply(null, bytes.subarray(offset, offset + chunkSize));
    }
    return btoa(binary);
  }

  function attachedSourceNames(c) {
    const names = attachedSources[c.course_id] || [];
    if (!names.length) {
      return c.active_job_id ? " (locked for this generation)" : "";
    }
    return " — " + names.map(escapeHtml).join(", ") + (c.active_job_id ? " (locked for this generation)" : "");
  }

  function render(courses) {
    if (!listEl || !emptyEl) return;
    listEl.innerHTML = "";
    if (!courses || courses.length === 0) {
      emptyEl.style.display = "block";
      listEl.style.display = "none";
      return;
    }
    emptyEl.style.display = "none";
    listEl.style.display = "grid";
    courses.forEach(function (c) {
      const card = document.createElement("div");
      card.className = "card-surface";
      card.dataset.courseId = c.course_id;
      if (c.active_job_id) card.dataset.jobId = c.active_job_id;
      const attention = needsAttention(c);
      const build = buildState(c);
      const builds = (c.active_job_id && buildHistory[c.active_job_id]) || [];
      let actionsHtml = "";
      if (c.active_job_id) {
        actionsHtml = '<div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;">';
        if (c.job_status === "completed") {
          actionsHtml += '<a class="action download-btn" href="/api/jobs/' + escapeHtml(c.active_job_id) + '/artifact" download style="text-decoration:none;padding:6px 12px;border-radius:6px;font-size:13px;display:inline-block">Download PDF</a>';
        } else {
          if (c.ai_mode === "gpt") {
            const gptLabel = liveGptJobs.has(c.active_job_id)
              ? "Continue in ChatGPT"
              : "Resume in ChatGPT";
            actionsHtml +=
              '<button class="action gpt-relay-btn" data-course-id="' + escapeHtml(c.course_id) +
              '" data-job-id="' + escapeHtml(c.active_job_id) +
              '" data-label="' + escapeHtml(gptLabel) +
              '" style="padding:6px 12px;font-size:13px">' + escapeHtml(gptLabel) + "</button>";
          }
          if (c.owner_approval) {
            const approvalLabel = c.owner_approval.kind === "priority"
              ? "Priority approval required"
              : "Map approval required";
            actionsHtml += '<label style="width:100%">Review ' + approvalLabel + '<textarea class="owner-plan" rows="12" style="width:100%" ' + (c.owner_approval.kind === "map" ? '' : 'readonly') + '>' + escapeHtml(c.owner_approval.text || '') + '</textarea></label>';
            if (c.owner_approval.kind === "map") {
              actionsHtml += '<button class="revise-plan" data-course-id="' + escapeHtml(c.course_id) + '" data-job-id="' + escapeHtml(c.active_job_id) + '" data-subject="' + escapeHtml(c.owner_approval.subject_sha256) + '">Use edited plan</button>';
            }
            actionsHtml +=
              '<span class="muted approval-status">' + approvalLabel + '</span>' +
              '<button class="action owner-decision-btn" data-job-id="' + escapeHtml(c.active_job_id) +
              '" data-kind="' + escapeHtml(c.owner_approval.kind) +
              '" data-subject-sha256="' + escapeHtml(c.owner_approval.subject_sha256) +
              '" data-approve="true" style="padding:6px 12px;font-size:13px">Approve</button>' +
              '<button class="secondary owner-decision-btn" data-job-id="' + escapeHtml(c.active_job_id) +
              '" data-kind="' + escapeHtml(c.owner_approval.kind) +
              '" data-subject-sha256="' + escapeHtml(c.owner_approval.subject_sha256) +
              '" data-approve="false" style="padding:6px 12px;font-size:13px">Reject</button>';
          }
          actionsHtml += '<button class="secondary run-gen-btn" data-job-id="' + escapeHtml(c.active_job_id) + '" title="Runs the built-in deterministic ScriptProvider fixture. This is demo material, not ChatGPT output." style="padding:6px 12px;font-size:13px">Run Synthetic Demo Generation</button>';
        }
        // Build is always visible once a job exists. It is enabled only
        // when the backend authority permits it; otherwise the reason
        // comes from the existing projection state.
        if (build && c.job_status !== "completed") {
          actionsHtml += '<button class="' + (build.ready ? "action" : "secondary") + ' build-btn" data-job-id="' + escapeHtml(c.active_job_id) + '"' + (build.ready ? "" : " disabled title=\"" + escapeHtml(build.reason) + "\"") + ' style="padding:6px 12px;font-size:13px">Build PDF</button>';
        }
        actionsHtml += "</div>";
      } else {
        actionsHtml = '<div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">' +
          '<button class="secondary attach-sources-btn" data-course-id="' + escapeHtml(c.course_id) + '" style="padding:6px 12px;font-size:13px">Attach Sources</button>' +
          '<input class="source-file-input" data-course-id="' + escapeHtml(c.course_id) + '" type="file" accept="application/pdf,.pdf" multiple hidden>';
        if (c.source_count > 0) {
          actionsHtml += '<button class="action start-generation-btn" data-course-id="' + escapeHtml(c.course_id) + '" style="padding:6px 12px;font-size:13px">Start Generation</button>';
        }
        actionsHtml += "</div>";
      }
      card.innerHTML =
        '<div class="eyebrow">' +
        escapeHtml(c.quality_mode) +
        " \u2022 " +
        escapeHtml(c.ai_mode) +
        "</div>" +
        "<div style=\"font-weight:600;margin-top:4px\">" +
        escapeHtml(c.title) +
        "</div>" +
        '<div class="muted" style="margin-top:6px">' +
        escapeHtml(c.course_id) +
        "</div>" +
        '<div class="muted">created ' +
        escapeHtml(c.created_at) +
        "</div>" +
        (c.source_count != null ? '<div class="muted">' + c.source_count + " source(s)" + attachedSourceNames(c) + "</div>" : "") +
        (c.job_status != null ? '<div class="muted">job status: ' + escapeHtml(c.job_status) + "</div>" : "") +
        (c.current_stage != null ? '<div class="muted">workflow: ' + escapeHtml(c.current_stage) + " / " + escapeHtml(c.current_disposition) + "</div>" : "") +
        (attention ? '<div class="notice' + (attention.kind === "failure" ? " failure" : "") + '" role="alert"><strong>' + escapeHtml(attention.title) + "</strong><p>" + escapeHtml(attention.body) + "</p></div>" : "") +
        (c.content_review ? '<div class="section"><h4>Semantic Content Review</h4><div class="muted">' + escapeHtml(reviewStatusText(c.content_review)) + "</div>" + (correctionText(c.content_review) ? '<div class="muted">' + escapeHtml(correctionText(c.content_review)) + "</div>" : "") + "</div>" : "") +
        (c.active_job_id ? '<div class="section"><h4>Document Checks</h4><div class="muted">' + escapeHtml(c.build_checks && c.build_checks.status !== "pending" ? (c.build_checks.diagnostics.length ? c.build_checks.diagnostics.map(function (d) { return d.code; }).join(", ") : c.build_checks.status) : "Pending — document checks run after final content, in both FAST and REVIEW.") + "</div></div>" : "") +
        (build && c.job_status !== "completed" && !build.ready ? '<div class="section"><h4>Build</h4><p>' + escapeHtml(build.reason) + "</p></div>" : "") +
        (builds.length ? '<div class="section"><h4>Build history</h4><ul class="build-list">' + builds.map(function (b) {
          const label = escapeHtml(b.build_id) + " — " + escapeHtml(b.status);
          if (b.status === "succeeded") {
            return '<li><a href="/api/builds/' + escapeHtml(b.build_id) + '/artifact" download>' + label + "</a></li>";
          }
          return "<li>" + label + "</li>";
        }).join("") + "</ul></div>" : "") +
        '<label>Course guidance<textarea class="course-guidance" rows="3" style="width:100%" ' + (c.active_job_id ? 'readonly' : '') + '>' + escapeHtml(c.course_guidance || '') + '</textarea></label>' +
        (c.active_job_id ? '<p class="muted">Guidance is locked for this generation. Create a new course to use different guidance.</p>' : '<button class="save-guidance" data-course-id="' + escapeHtml(c.course_id) + '" data-revision="' + c.metadata_revision + '">Save guidance</button>') + actionsHtml;
      if (c.owner_approval) {
        const plan = card.querySelector(".owner-plan");
        if (plan) plan.dataset.original = c.owner_approval.text || "";
      }
      listEl.appendChild(card);
    });

    listEl.querySelectorAll(".save-guidance").forEach(function (button) {
      button.addEventListener("click", async function () {
        const value = button.parentElement.querySelector(".course-guidance").value;
        const response = await fetch("/api/courses/" + button.dataset.courseId + "/guidance", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({course_guidance: value, expected_revision: Number(button.dataset.revision)})
        });
        toast(response.ok ? "Guidance saved." : "Guidance could not be saved. Refresh the course and retry.");
        if (response.ok) await loadCourses();
      });
    });

    listEl.querySelectorAll(".attach-sources-btn").forEach(function (b) {
      b.addEventListener("click", function () {
        const courseId = b.getAttribute("data-course-id");
        const input = listEl.querySelector('.source-file-input[data-course-id="' + courseId + '"]');
        if (input) input.click();
      });
    });

    listEl.querySelectorAll(".source-file-input").forEach(function (input) {
      input.addEventListener("change", async function () {
        const courseId = input.getAttribute("data-course-id");
        const files = Array.prototype.slice.call(input.files || []);
        if (!courseId || files.length === 0) return;
        const invalid = files.some(function (file) {
          return file.type && file.type !== "application/pdf" && !/\.pdf$/i.test(file.name);
        });
        if (invalid) {
          toast("Choose PDF source files only.");
          input.value = "";
          return;
        }
        const attachButton = listEl.querySelector('.attach-sources-btn[data-course-id="' + courseId + '"]');
        if (attachButton) {
          attachButton.disabled = true;
          attachButton.textContent = "Attaching...";
        }
        let attached = 0;
        let failed = 0;
        for (const file of files) {
          try {
            const bytes = new Uint8Array(await file.arrayBuffer());
            const res = await fetch("/api/courses/" + encodeURIComponent(courseId) + "/sources", {
              method: "POST",
              headers: { "Content-Type": "application/json", Accept: "application/json" },
              body: JSON.stringify({ source_id: sourceId(), content_base64: bytesToBase64(bytes) }),
            });
            if (!res.ok) throw new Error("attach failed");
            attached += 1;
            if (!attachedSources[courseId]) attachedSources[courseId] = [];
            attachedSources[courseId].push(file.name || "source PDF");
            await loadCourses();
          } catch (_) {
            failed += 1;
          }
        }
        input.value = "";
        if (attachButton) {
          attachButton.disabled = false;
          attachButton.textContent = "Attach Sources";
        }
        if (attached > 0 && failed === 0) toast(attached + " source PDF" + (attached === 1 ? "" : "s") + " attached.");
        else if (attached > 0) toast(attached + " source PDF" + (attached === 1 ? "" : "s") + " attached; " + failed + " failed.");
        else toast("Source PDFs could not be attached.");
      });
    });

    listEl.querySelectorAll(".start-generation-btn").forEach(function (b) {
      b.addEventListener("click", async function () {
        const courseId = b.getAttribute("data-course-id");
        if (!courseId) return;
        b.disabled = true;
        b.textContent = "Starting...";
        try {
          const res = await fetch("/api/courses/" + encodeURIComponent(courseId) + "/start-generation", { method: "POST" });
          if (!res.ok) throw new Error("start failed");
          toast("Generation started.");
          await loadCourses();
        } catch (_) {
          toast("Generation could not be started.");
        } finally {
          b.disabled = false;
          b.textContent = "Start Generation";
        }
      });
    });

    listEl.querySelectorAll(".build-btn").forEach(function (b) {
      if (b.disabled) {
        b.addEventListener("click", function (e) {
          e.preventDefault();
          toast(b.getAttribute("title") || "Build is not available yet.");
        });
        return;
      }
      b.addEventListener("click", async function () {
        const jobId = b.getAttribute("data-job-id");
        if (!jobId) return;
        b.disabled = true;
        b.textContent = "Building...";
        try {
          const res = await fetch("/api/jobs/" + jobId + "/build", { method: "POST" });
          if (res.ok) {
            toast("PDF built successfully.");
            await loadCourses();
          } else {
            let reason = "Build failed.";
            try {
              const payload = await res.json();
              if (payload && payload.error && BUILD_GATE_EXPLANATIONS[payload.error]) {
                reason = "Build not available: " + BUILD_GATE_EXPLANATIONS[payload.error];
              } else if (payload && payload.error) {
                reason = "Build failed (" + payload.error + ").";
              }
            } catch (_) {}
            toast(reason);
            await loadCourses();
          }
        } catch (_) {
          toast("Network error during build.");
        } finally {
          b.disabled = false;
        }
      });
    });

    listEl.querySelectorAll(".gpt-relay-btn").forEach(function (b) {
      b.addEventListener("click", function () {
        const courseId = b.getAttribute("data-course-id");
        const jobId = b.getAttribute("data-job-id");
        const label = b.getAttribute("data-label") || "Continue in ChatGPT";
        if (courseId && jobId) openGptRelay(courseId, jobId, label);
      });
    });

    listEl.querySelectorAll(".revise-plan").forEach(function (button) {
      button.addEventListener("click", async function () {
        const edited = button.parentElement.querySelector(".owner-plan").value;
        const response = await fetch("/api/jobs/" + button.dataset.jobId + "/owner/map", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({approve: false, subject_sha256: button.dataset.subject})
        });
        if (!response.ok) { toast("The plan changed. Refresh and review it again."); return; }
        await openGptRelay(button.dataset.courseId, button.dataset.jobId, "Save edited plan");
        gptResultText.value = edited;
        gptSubmitBtn.click();
      });
    });

    listEl.querySelectorAll(".owner-decision-btn").forEach(function (b) {
      b.addEventListener("click", async function () {
        const jobId = b.getAttribute("data-job-id");
        const kind = b.getAttribute("data-kind");
        const subject = b.getAttribute("data-subject-sha256");
        const approve = b.getAttribute("data-approve") === "true";
        if (!jobId || (kind !== "priority" && kind !== "map") || !subject) return;
        const plan = b.parentElement.querySelector(".owner-plan");
        if (approve && plan && plan.value !== plan.dataset.original) {
          toast("Save your changes with Use edited plan before approving."); return;
        }
        b.disabled = true;
        try {
          const res = await fetch("/api/jobs/" + encodeURIComponent(jobId) + "/owner/" + kind, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ approve: approve, subject_sha256: subject }),
          });
          if (!res.ok) throw new Error("owner decision failed");
          toast((approve ? "Approved" : "Rejected") + ".");
          await loadCourses();
        } catch (_) {
          toast("Owner decision could not be saved.");
        } finally {
          b.disabled = false;
        }
      });
    });

    listEl.querySelectorAll(".run-gen-btn").forEach(function (b) {
      b.addEventListener("click", async function () {
        const jobId = b.getAttribute("data-job-id");
        if (!jobId) return;
        b.disabled = true;
        b.textContent = "Running synthetic demo...";
        try {
          const res = await fetch("/api/jobs/" + jobId + "/run-controlled-generation", { method: "POST" });
          if (res.ok) {
            toast("Synthetic demo generation completed (deterministic fixture content, not ChatGPT). Ready to build.");
            await loadCourses();
          } else {
            toast("Synthetic demo generation failed.");
          }
        } catch (_) {
          toast("Network error during synthetic demo generation.");
        } finally {
          b.disabled = false;
        }
      });
    });
  }

  async function loadCourses() {
    try {
      const res = await fetch("/api/courses", { headers: { Accept: "application/json" } });
      if (!res.ok) throw new Error("list failed");
      const data = await res.json();
      const courses = data.courses || [];
      // Enrich courses with job status using the durable current_job_id
      // that /api/courses already returns per record (see _course_to_api).
      // The course-detail endpoint wraps its payload as {"course": {...}},
      // so it is not fetched here merely to rediscover the same pointer.
      const histories = {};
      for (let i = 0; i < courses.length; i++) {
        try {
          if (courses[i].current_job_id) {
            courses[i].active_job_id = courses[i].current_job_id;
            const jobRes = await fetch("/api/jobs/" + courses[i].current_job_id, { headers: { Accept: "application/json" } });
            if (jobRes.ok) {
              const jobData = await jobRes.json();
              courses[i].job_status = jobData.status;
              courses[i].current_stage = jobData.current_stage;
              courses[i].current_disposition = jobData.current_disposition;
              courses[i].owner_approval = jobData.owner_approval;
              courses[i].failure_code = jobData.failure_code;
              courses[i].content_review = jobData.content_review;
              courses[i].build_checks = jobData.build_checks;
            }
            try {
              const histRes = await fetch("/api/jobs/" + courses[i].current_job_id + "/builds", { headers: { Accept: "application/json" } });
              if (histRes.ok) {
                const histData = await histRes.json();
                histories[courses[i].current_job_id] = histData.builds || [];
              }
            } catch (_) {}
          }
        } catch (_) {}
      }
      buildHistory = histories;
      render(courses);
    } catch (_) {
      toast("Could not load courses.");
    }
  }

  const refreshBtn = document.getElementById("refresh-btn");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", async function () {
      refreshBtn.disabled = true;
      refreshBtn.textContent = "Refreshing...";
      try {
        await loadCourses();
      } finally {
        refreshBtn.disabled = false;
        refreshBtn.textContent = "Refresh";
      }
    });
  }

  // Lightweight polling of open cards so a long human-mediated ChatGPT
  // turn does not require manual reloads to see fresh durable state.
  setInterval(function () {
    if (document.hidden) return;
    if (gptRelayState !== null) return;
    if (!listEl || !listEl.querySelector(".card-surface")) return;
    loadCourses();
  }, 15000);

  function openDialog() {
    if (!dialog) return;
    if (errorEl) {
      errorEl.style.display = "none";
      errorEl.textContent = "";
    }
    if (titleInput) titleInput.value = "";
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    if (titleInput) titleInput.focus();
  }

  function closeDialog() {
    if (!dialog) return;
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  if (newBtn) newBtn.addEventListener("click", openDialog);
  if (navNewBtn) navNewBtn.addEventListener("click", openDialog);
  if (cancelBtn) cancelBtn.addEventListener("click", closeDialog);
  if (dialog)
    dialog.addEventListener("click", function (e) {
      const rect = dialog.getBoundingClientRect();
      if (e.clientX < rect.left || e.clientX > rect.right || e.clientY < rect.top || e.clientY > rect.bottom) closeDialog();
    });

  if (form)
    form.addEventListener("submit", async function (e) {
      e.preventDefault();
      const title = titleInput ? titleInput.value.trim() : "";
      let ai_mode = aiSelect ? aiSelect.value : "gpt";
      if (ai_mode !== "gpt") ai_mode = "gpt";
      const quality_mode = qualitySelect ? qualitySelect.value : "fast";
      if (!title) {
        if (errorEl) {
          errorEl.textContent = "Title is required.";
          errorEl.style.display = "block";
        }
        return;
      }
      try {
        const res = await fetch("/api/courses", {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ title: title, ai_mode: ai_mode, quality_mode: quality_mode, course_guidance: document.getElementById("course-guidance").value }),
        });
        if (!res.ok) {
          let msg = "Could not create course.";
          try {
            const j = await res.json();
            if (j.error) msg = j.error;
          } catch (_) {}
          if (errorEl) {
            errorEl.textContent = msg;
            errorEl.style.display = "block";
          } else toast(msg);
          return;
        }
        closeDialog();
        toast("Course created.");
        await loadCourses();
      } catch (_) {
        if (errorEl) {
          errorEl.textContent = "Network error.";
          errorEl.style.display = "block";
        }
      }
    });

  // T051 GPT low-turn relay: acquire the accepted lease, display the
  // bounded handoff + evidence, and relay back a pasted ChatGPT result.
  // This UI never messages ChatGPT automatically and never injects/repairs
  // the pasted result — see skills/course-compiler/references/coarse-workflow.md.
  const gptDialog = document.getElementById("gpt-relay-dialog");
  const gptTitle = document.getElementById("gpt-relay-title");
  const gptStatus = document.getElementById("gpt-relay-status");
  const gptBody = document.getElementById("gpt-relay-body");
  const gptHandoffText = document.getElementById("gpt-relay-handoff");
  const gptEvidenceList = document.getElementById("gpt-relay-evidence");
  const gptResultText = document.getElementById("gpt-relay-result");
  const gptError = document.getElementById("gpt-relay-error");
  const gptCopyBtn = document.getElementById("gpt-relay-copy");
  const gptOpenChatGptBtn = document.getElementById("gpt-relay-open-chatgpt");
  const gptCloseBtn = document.getElementById("gpt-relay-close");
  const gptSubmitBtn = document.getElementById("gpt-relay-submit");

  let gptRelayState = null; // { courseId, jobId, holderId, handoff, identity }

  // T051 human-latency lease repair.
  //
  // The accepted T049 lease is 300 seconds and is acquired once, when this
  // relay opens. A genuine human-mediated ChatGPT turn (download evidence,
  // open a fresh conversation, attach, paste, read, copy back) routinely
  // takes longer than that, and submit_semantic_result correctly rejects an
  // expired lease as stale_revision. The repair is an ordinary same-holder
  // renewal on the accepted mutation route immediately before submitting,
  // plus an exact identity check so an old model result can never be
  // rebound to different authority. No second lease state machine exists.
  const GPT_REQUEST_IDENTITY_FIELDS = [
    "request_id", "job_id", "workflow_id", "expected_revision",
    "operation_id", "kind", "input_refs",
  ];
  const GPT_STALE_AUTHORITY_MESSAGE =
    "Close this relay. Click Continue/Resume in ChatGPT again. Use the new handoff in a fresh ChatGPT conversation.";
  // Content-safe authority/timing diagnostics: these mean the lease or the
  // durable state moved, never that ChatGPT's semantic content was wrong.
  const GPT_AUTHORITY_DIAGNOSTICS = ["stale_revision", "lease_conflict", "stale_workflow_state"];

  function gptIdentityValuesEqual(a, b) {
    if (Array.isArray(a) || Array.isArray(b)) {
      if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
      for (let i = 0; i < a.length; i += 1) {
        if (a[i] !== b[i]) return false;
      }
      return true;
    }
    return a === b;
  }

  // Pure comparison. Returns the first differing identity field name, or
  // null when the renewed request is exactly the authority the pasted
  // result answered. A non-null return must always suppress the result POST.
  function gptIdentityMismatch(identity, renewed) {
    if (!identity || !renewed) return "request_id";
    for (let i = 0; i < GPT_REQUEST_IDENTITY_FIELDS.length; i += 1) {
      const field = GPT_REQUEST_IDENTITY_FIELDS[i];
      if (!gptIdentityValuesEqual(identity[field], renewed[field])) return field;
    }
    return null;
  }

  function gptHolderId(jobId) {
    const storageKey = "cc-gpt-holder-" + jobId;
    try {
      let holder = sessionStorage.getItem(storageKey);
      if (!holder) {
        holder = ("holder" + Math.random().toString(16).slice(2, 12) + Date.now().toString(16))
          .replace(/[^a-z0-9]/g, "0")
          .slice(0, 32);
        sessionStorage.setItem(storageKey, holder);
      }
      return holder;
    } catch (_) {
      return "holderephemeral00001";
    }
  }

  function closeGptRelay() {
    if (!gptDialog) return;
    if (typeof gptDialog.close === "function") gptDialog.close();
    else gptDialog.removeAttribute("open");
    gptRelayState = null;
  }

  async function openGptRelay(courseId, jobId, label) {
    if (!gptDialog) return;
    gptRelayState = { courseId: courseId, jobId: jobId, holderId: null, handoff: null, identity: null };
    if (gptTitle) gptTitle.textContent = label;
    if (gptStatus) gptStatus.textContent = "Acquiring semantic work...";
    if (gptBody) gptBody.style.display = "none";
    if (gptSubmitBtn) gptSubmitBtn.disabled = true;
    if (gptError) {
      gptError.style.display = "none";
      gptError.textContent = "";
    }
    if (gptResultText) gptResultText.value = "";
    if (typeof gptDialog.showModal === "function") gptDialog.showModal();
    else gptDialog.setAttribute("open", "");

    const holderId = gptHolderId(jobId);
    try {
      // The accepted T049 mutation route is the sole lease acquire/renew
      // path; this UI never invents a second lease state machine.
      const acquireRes = await fetch("/api/jobs/" + jobId + "/semantic-request", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ holder_id: holderId }),
      });
      const acquireBody = await acquireRes.json().catch(function () {
        return {};
      });
      if (!acquireRes.ok) {
        const acquireCode =
          acquireBody.diagnostics && acquireBody.diagnostics.length
            ? acquireBody.diagnostics.join(", ")
            : acquireBody.error || acquireRes.status;
        if (gptStatus) gptStatus.textContent = "No GPT semantic work is currently available (" + acquireCode + ").";
        return;
      }
      if (!acquireBody.request) {
        if (gptStatus)
          gptStatus.textContent =
            "No GPT semantic work is currently available (" + (acquireBody.reason || acquireBody.status || "no_semantic_work") + ").";
        return;
      }
      const pendingRes = await fetch("/api/courses/" + encodeURIComponent(courseId) + "/jobs/" + encodeURIComponent(jobId) + "/pending_work", {
        headers: { Accept: "application/json" },
      });
      if (!pendingRes.ok) {
        if (gptStatus) gptStatus.textContent = "Could not read pending work.";
        return;
      }
      const data = await pendingRes.json();
      if (data.status !== "ready_for_chatgpt" || !data.handoff) {
        if (gptStatus) gptStatus.textContent = "No GPT semantic work is currently pending (" + data.status + ").";
        return;
      }
      // The handoff is the control-plane projection the human actually
      // answers; input_refs live only on the accepted T049 request. Bind
      // both once, here, and compare the pre-submit renewal against this
      // exact record rather than against anything re-read later.
      const identity = {
        request_id: data.handoff.request_id,
        job_id: data.handoff.job_id,
        workflow_id: data.handoff.workflow_id,
        expected_revision: data.handoff.expected_revision,
        operation_id: data.handoff.operation_id,
        kind: data.handoff.kind,
        input_refs: (acquireBody.request.input_refs || []).slice(),
      };
      if (gptIdentityMismatch(identity, acquireBody.request, {
        request_id: acquireBody.request.request_id,
        operation_id: acquireBody.request.operation_id,
        kind: acquireBody.request.kind,
        request_revision: acquireBody.request.expected_revision,
      }) !== null) {
        if (gptStatus) gptStatus.textContent = "The pending semantic work changed while the handoff was being prepared. Reopen this relay.";
        return;
      }
      gptRelayState.holderId = holderId;
      gptRelayState.handoff = data.handoff;
      gptRelayState.identity = identity;
      liveGptJobs.add(jobId);
      if (gptStatus) gptStatus.textContent = "Copy the prompt, attach the listed evidence, then paste the response below.";
      if (gptHandoffText) gptHandoffText.value = data.handoff.prompt;
      if (gptEvidenceList) {
        gptEvidenceList.innerHTML = "";
        (data.handoff.evidence_manifest || []).forEach(function (item) {
          const li = document.createElement("li");
          const href =
            "/api/courses/" +
            encodeURIComponent(courseId) +
            "/jobs/" +
            encodeURIComponent(jobId) +
            "/pending_work/" +
            encodeURIComponent(data.handoff.request_id) +
            "/evidence/" +
            encodeURIComponent(item.evidence_id);
          li.innerHTML =
            escapeHtml(item.label) +
            " (" +
            escapeHtml(item.evidence_kind) +
            ", " +
            item.byte_length +
            ' bytes) — <a href="' +
            href +
            '" target="_blank" rel="noopener" download>Download current evidence</a>';
          gptEvidenceList.appendChild(li);
        });
      }
      if (gptBody) gptBody.style.display = "block";
      if (gptSubmitBtn) gptSubmitBtn.disabled = false;
    } catch (_) {
      if (gptStatus) gptStatus.textContent = "Network error while preparing the handoff.";
    }
  }

  if (gptCloseBtn) gptCloseBtn.addEventListener("click", closeGptRelay);

  if (gptCopyBtn)
    gptCopyBtn.addEventListener("click", function () {
      if (!gptHandoffText) return;
      gptHandoffText.select();
      try {
        navigator.clipboard.writeText(gptHandoffText.value);
        toast("Handoff copied.");
      } catch (_) {
        try {
          document.execCommand("copy");
          toast("Handoff copied.");
        } catch (_) {}
      }
    });

  if (gptOpenChatGptBtn)
    gptOpenChatGptBtn.addEventListener("click", function () {
      // Convenience only: opening ChatGPT does not inject the handoff or
      // attach evidence automatically. The durable contract is the
      // handoff/result relay above, not this shortcut.
      window.open("https://chatgpt.com/", "_blank", "noopener");
    });

  if (gptSubmitBtn)
    gptSubmitBtn.addEventListener("click", async function () {
      if (!gptRelayState || !gptResultText) return;
      if (gptError) {
        gptError.style.display = "none";
        gptError.textContent = "";
      }
      const raw = gptResultText.value;
      if (!raw.trim()) return;
      const failLocally = function (message) {
        if (gptError) {
          gptError.textContent = message;
          gptError.style.display = "block";
        }
      };
      const priorStatus = gptStatus ? gptStatus.textContent : "";
      gptSubmitBtn.disabled = true;
      gptSubmitBtn.textContent = "Verifying...";
      if (gptStatus) gptStatus.textContent = "Verifying current semantic work...";
      try {
        // Same-holder pre-submit renewal on the existing accepted T049
        // mutation route. The 300-second default is unchanged; this is the
        // ordinary renewal T049 already supports for the current holder.
        const renewRes = await fetch("/api/jobs/" + encodeURIComponent(gptRelayState.jobId) + "/semantic-request", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ holder_id: gptRelayState.holderId }),
        });
        const renewBody = await renewRes.json().catch(function () {
          return {};
        });
        if (!renewRes.ok) {
          // Fail closed: another holder owns this work, or it is gone.
          const code =
            renewBody.diagnostics && renewBody.diagnostics.length
              ? renewBody.diagnostics.join(", ")
              : renewBody.error || String(renewRes.status);
          failLocally("This semantic work is no longer held by this browser (" + code + "). " + GPT_STALE_AUTHORITY_MESSAGE);
          return;
        }
        if (!renewBody.request) {
          // A lost response is recoverable by exact read-only repeat matching.
          const repeated = await fetch("/api/courses/" + encodeURIComponent(gptRelayState.courseId) + "/jobs/" + encodeURIComponent(gptRelayState.jobId) + "/result", {
            method: "POST", headers: {"Content-Type":"application/json"},
            body: JSON.stringify({text: raw, holder_id: gptRelayState.holderId, request_id: gptRelayState.identity.request_id})
          });
          if (repeated.ok) {
            toast("Previously submitted content is saved."); closeGptRelay(); await loadCourses(); return;
          }
          failLocally(
            "The App has no current semantic work for this request (" +
              (renewBody.reason || renewBody.status || "no_semantic_work") +
              "). " +
              GPT_STALE_AUTHORITY_MESSAGE
          );
          return;
        }
        const mismatch = gptIdentityMismatch(gptRelayState.identity, renewBody.request);
        if (mismatch !== null) {
          // Never mutate or "repair" the model result to match different
          // authority; the old output is simply not submittable.
          failLocally(
            "The active task changed before submission. " + GPT_STALE_AUTHORITY_MESSAGE
          );
          return;
        }
        const transport = { text: raw, holder_id: gptRelayState.holderId, request_id: gptRelayState.identity.request_id };
        gptSubmitBtn.textContent = "Submitting...";
        if (gptStatus) gptStatus.textContent = "Submitting...";
        const res = await fetch("/api/courses/" + encodeURIComponent(gptRelayState.courseId) + "/jobs/" + encodeURIComponent(gptRelayState.jobId) + "/result", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(transport),
        });
        const body = await res.json().catch(function () {
          return {};
        });
        if (!res.ok) {
          const codes = body.diagnostics && body.diagnostics.length ? body.diagnostics : [];
          const authority = codes.some(function (code) {
            return GPT_AUTHORITY_DIAGNOSTICS.indexOf(code) !== -1;
          });
          if (authority) {
            // Lease/state timing, not ChatGPT's semantic content.
            failLocally(
              "The App could not accept this result because its durable authority moved (" +
                codes.join(", ") +
                "). This is not a problem with the ChatGPT response. " +
                GPT_STALE_AUTHORITY_MESSAGE
            );
          } else {
            failLocally(
              "The App rejected this result: " +
                (body.error || res.status) +
                (codes.length ? " (" + codes.join(", ") + ")" : "") +
                ". Copy this exact diagnostic into your next fresh ChatGPT turn."
            );
          }
          return;
        }
        liveGptJobs.add(gptRelayState.jobId);
        toast("Result accepted (" + (body.status || "advanced") + ").");
        closeGptRelay();
        await loadCourses();
      } catch (_) {
        failLocally("Network error while submitting the result.");
      } finally {
        gptSubmitBtn.disabled = false;
        gptSubmitBtn.textContent = "Submit result";
        if (gptStatus && gptStatus.textContent !== priorStatus) gptStatus.textContent = priorStatus;
      }
    });

  loadCourses();
})();
