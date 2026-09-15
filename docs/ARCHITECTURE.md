# Architecture

Course Compiler is a local-first, single-user web app that compiles
private course sources into deterministic study PDFs. The architecture
is deliberately layered so that evidence ingestion, semantic
generation, deterministic rendering, and durable persistence each
have a clear, testable boundary.

## Process shape

A single Python process runs the app on `127.0.0.1` (loopback-only by
default). The browser UI is served as static HTML/CSS/JS alongside a
small HTTP API. All state lives in local SQLite stores under the
git-ignored `local-data/app/` root.

## Layered modules

- **Course layer.** `course_compiler.course`, `course_compiler.course_*`
  hold the durable Course identity, Job identity, source attachments,
  and authoritative workflow association. The Course layer is the
  long-lived record: a Course survives restarts, refreshes, and the
  entire generation lifecycle.
- **Workflow layer.** `course_compiler.course_workflow*` and
  `course_compiler.workflow*` enforce the deterministic state machine
  that advances a job from `created` through `deterministic_building`
  to `completed`. Transitions are governed by versioned contracts.
- **Source persistence.** `course_compiler.source_persistence` stores
  exact, immutable, content-addressed source bytes in SQLite BLOB
  columns keyed by an opaque per-source reference. The original
  browser filename is session-only; it is not part of the durable
  source identity.
- **Semantic layer.** `course_compiler.semantic_work`,
  `course_compiler.semantic_operations`,
  `course_compiler.providers.chatgpt_relay`, and the browser-mediated
  GPT relay UI are the seam between local state and external LLM
  output. The relay never calls a provider automatically; the user
  copies a prompt, runs it in a fresh ChatGPT conversation, and pastes
  the Markdown response back into the app.
- **Render + assemble layer.** `course_compiler.legacy_renderer`,
  `course_compiler.assembly`, and `course_compiler.compilation`
  translate the accepted semantic content into deterministic LaTeX
  via the legacy renderer and assemble the per-lecture plus combined
  course document.
- **Build layer.** `course_compiler.build_*` orchestrates the
  deterministic PDF build via TeX Live (`latexmk` + `xelatex`) and
  records a `BuildRecord` row with the compiled artifact.
- **MCP seam.** `course_compiler.mcp_adapter`,
  `course_compiler.mcp_runtime`, `course_compiler.mcp_file_ingress`
  expose a thin MCP boundary for hosted / remote MCP scenarios. The
  v0.1 app does not require MCP; the SDK is listed as an optional
  dependency in `requirements-mcp.txt`.

## Persistence layout

| Store                              | Purpose                                            |
|------------------------------------|----------------------------------------------------|
| `local-data/app/courses.sqlite3`   | Course identity, titles, guidance                  |
| `local-data/app/jobs.sqlite3`      | Per-course Job state and durable workflow stage    |
| `local-data/app/source-evidence.sqlite3` | Immutable source byte blobs by content SHA-256 |
| `local-data/app/lecture-documents.sqlite3` | Accepted lecture Markdown per lecture id     |
| `local-data/app/build-records.sqlite3`    | Successful/failed build outcomes with artifacts |
| `local-data/app/policy.sqlite3`           | Per-course policy content (review guidance)    |

All stores use a versioned schema and a deterministic initialization
so a fresh clone plus a relaunch recreates the same on-disk shape.

## FAST vs REVIEW

- **FAST** accepts the first semantic result as final and advances
  straight to build.
- **REVIEW** adds an explicit Semantic Content Review step. The
  reviewer verdict either accepts or requests corrections. A new
  review verdict with no further corrections is required before
  build is permitted.

## Privacy boundary

- All content-bearing material lives under ignored roots
  (`local-data/`, `local-artifacts/`, `build/`).
- The tracked Git repository must never contain source bytes, build
  artifacts, or any private material.
- The app never embeds a provider API key; the GPT relay is
  browser-mediated and only the user-attached evidence leaves the
  machine, with the user's explicit consent at each turn.

## Module manifest

The logical module manifest in `architecture/modules.yaml` records
which directories each module owns, which dependencies are allowed,
and which tests cover the module. The public gate validates this
manifest.
