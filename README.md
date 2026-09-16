# Course Compiler

Course Compiler is MIT licensed — see [LICENSE](LICENSE).

Course Compiler is a local-first, single-user AI-assisted course-authoring
and study-material compiler. It turns private course sources into
structured, exam-oriented lectures and a verified study PDF. Semantic
reasoning happens in ChatGPT (no app-funded LLM); deterministic evidence,
rendering, and PDF compilation happen locally in Course Compiler.

> **Course Compiler v0.1.0-alpha.1 — Local Developer Preview.**
> Pre-release / Local Developer Preview. Intended for local single-user
> experimentation. Not a hosted or multi-user production service. A small,
> truthful, demonstrable local product the owner can use and show. v0.1 is
> **not** a production SaaS, **not** enterprise-ready, and **not** a
> hosted service.

## What you can do in v0.1

- Create a course in the browser on `http://127.0.0.1:8787/`.
- Attach one or more private PDF source files.
- Choose `fast` (FAST — direct) or `review` (REVIEW — adds semantic
  review and correction before build).
- Generate lecture material through a browser-mediated GPT relay
  (`Continue in ChatGPT` / `Resume in ChatGPT`). The app never messages
  ChatGPT automatically; you copy a prompt into a fresh ChatGPT
  conversation, attach the listed current evidence, paste the Markdown
  response back into the relay.
- Review and correct the generated lectures (REVIEW mode).
- Build a deterministic PDF and download it.
- Refresh or restart the app at any point; courses, jobs, and build
  history survive both.

## What v0.1 deliberately is not

- **No BYOK / user-funded provider.** v0.1 ships GPT-only. BYOK is
  deferred to a future release.
- **No multi-user / no hosted SaaS.** v0.1 is single-user, loopback-only
  (`127.0.0.1`), and runs entirely on your own machine. Multi-user
  isolation, hosted HTTPS deployment, public submission, and the official
  publication step are all deferred to a future release.
- **No broad provider support.** Only ChatGPT semantic mode is
  supported in v0.1.
- **No production visual polish.** v0.1 is a working single-user app,
  not a finished commercial product.

## Prerequisites

The core local web runtime is implemented with the Python standard
library and a vanilla HTML/CSS/JS frontend. To run the app you need:

- **Python 3.10+** on `PATH` as `python3` (no third-party Python
  packages are required to run the app).

To compile the final study PDF you additionally need:

- `latexmk`, `xelatex`, and `kpsewhich` from a working **TeX Live**
  install (`texlive-xetex`, `texlive-base`, `texlive-latex-base`,
  `texlive-latex-recommended`, `texlive-latex-extra`,
  `texlive-fonts-recommended`).
- `pdftoppm` (Poppler tools).
- `fc-match` (fontconfig) plus the pinned fonts (`fonts-lmodern`,
  `fonts-noto-mono`).
- `python3` must be able to spawn `latexmk`.

For the exact pinned versions and reproduction evidence, see
[`docs/TOOLCHAIN.md`](docs/TOOLCHAIN.md).

## Installation

```bash
git clone https://github.com/sabers13/course-compiler.git
cd course-compiler
```

No third-party Python packages are required to run the app. The only
optional Python dependency is the official MCP Python SDK v2, listed in
[`requirements-mcp.txt`](requirements-mcp.txt), which is **not** needed
for the v0.1 Local Developer Preview and is only relevant for hosted /
remote MCP exposure (a future-release concern). To enable the optional
MCP integration tests:

```bash
pip install -r requirements-mcp.txt
```

Without it, those tests skip cleanly with a precise reason instead of
failing. Likewise, PDF/full-toolchain tests require the documented
TeX/font toolchain above and skip cleanly when it is unavailable.

## Start the app

From the repository root:

```bash
make app
```

or equivalently:

```bash
python3 -m course_compiler.app
```

The app binds a loopback port (`127.0.0.1`, default `8787`) and prints
a content-safe startup line, for example:

```
Course Compiler app listening on http://127.0.0.1:8787/
```

Open that URL in your browser. The app creates and uses a SQLite data
root under `local-data/app/` by default — this is inside the
git-ignored `local-data/` privacy root (see *Privacy* below).

Override flags:

- `--port 0` — bind an ephemeral port (the startup line prints the
  chosen port).
- `--port N` — bind a specific port in `1024..65535`.
- `--data-root PATH` — use a different data root. Must live inside
  `local-data/`, `local-artifacts/`, or `build/`; anything outside an
  approved ignored root is refused fail-closed with the
  content-safe code `data_root_invalid`.
- `--allow-non-loopback` — bind a non-loopback interface
  (development-only escape hatch; the app refuses non-loopback binds
  without it).

Stop the app with `Ctrl-C`. The shutdown line is content-safe:

```
Course Compiler app stopped.
```

### If startup fails

Startup diagnostics are content-safe fixed codes:

| Code                | Meaning                                                                                    |
|---------------------|--------------------------------------------------------------------------------------------|
| `host_invalid`      | The `--host` string is malformed.                                                          |
| `host_not_loopback` | The app refuses non-loopback binds unless `--allow-non-loopback` is set.                   |
| `port_invalid`      | The `--port` value is not in the allowed range.                                            |
| `data_root_invalid` | The data root is outside an approved ignored root, missing, or not a `Path`.               |
| `data_root_unavailable` | The data root could not be prepared (e.g. permission denied on the chosen ignored root). |

Resolve the named condition and try again. None of these messages
expose private content, file paths, or stack traces.

## Quick-start workflow

1. Open `http://127.0.0.1:8787/` in your browser.
2. Click **+ New Course**, give the course a title, optionally add
   course guidance, pick `fast` or `review`, and click **Create**.
3. On the course card, click **Attach Sources** and select one or
   more PDF source files. The session-only filenames appear on the
   card for convenience; the app durably stores the exact source
   bytes locally under an opaque content-addressed source reference,
   while the original filename is not a durable source identity.
4. Click **Start Generation**. The app initializes a durable job.
5. **Continue in ChatGPT**: copy the displayed prompt, open a fresh
   ChatGPT conversation, attach only the listed current evidence,
   paste the prompt, paste ChatGPT's Markdown response back into the
   relay, and click **Submit result**.
6. Repeat for each lecture until the job reaches
   `deterministic_building`.
7. **REVIEW mode only:** the Semantic Content Review section asks
   for any corrections. Continue in ChatGPT for the correction turn,
   then re-review; corrections are not submittable until the new
   review verdict returns no further corrections.
8. Click **Build PDF**. The build is accepted only when the backend
   authority permits it; otherwise the button shows a backend-derived
   reason and is disabled.
9. Click **Download PDF**.

A **Refresh** button and a 15-second polling cycle (paused while the
GPT relay is open, while the tab is hidden, and while no courses
exist) keep the page truthful during long human-mediated turns.

## FAST vs REVIEW

- **FAST** — direct path. The app accepts the semantic content as
  final as soon as generation completes; `Build PDF` is enabled once
  the build authority permits it.
- **REVIEW** — adds a Semantic Content Review step between
  generation and build. The first review may request corrections;
  corrections require another fresh GPT turn and a *new* review
  verdict that returns no further corrections before `Build PDF` is
  enabled.

The UI keeps these distinct. `Semantic Content Review` is a separate
section from `Document Checks` (which run in both modes).

## GPT relay explanation

v0.1 deliberately does **not** message ChatGPT automatically. The
relay UI presents:

1. The exact prompt to copy.
2. The exact current evidence files to download and attach.
3. A textarea to paste ChatGPT's Markdown response.
4. A **Submit result** button that pre-renews the lease, verifies the
  durable authority has not changed since the relay opened, and
  submits the response on your behalf.

- `Continue in ChatGPT` is shown while you have a live page session
  with the same `holder_id` (the browser session-storage entry for
  the active job).
- `Resume in ChatGPT` is shown after a page reload (fresh page
  session, no live `holder_id`); the same renewal and identity
  discipline protects durable authority.

If the durable state moved between the moment the relay opened and
the moment you click **Submit result**, the submit is refused with a
plain-language diagnostic and you are asked to reopen the relay.

The relay is purely local and browser-mediated: you copy, attach,
and paste by hand. The app holds no provider API key and makes no
network calls to OpenAI or any other provider; there is no OpenAI
API usage in v0.1.0-alpha.1.

## Where the final PDF appears and how to download it

When the build succeeds, the UI shows a **Download PDF** button on the
course card. Build history (success and failure) is shown beneath the
card; succeeded builds link directly to the PDF.

The build also writes durable `BuildRecord` rows under the data root
(`local-data/app/build-records.sqlite3`) and memoizes the compiled PDF
bytes; the same artifact can be re-fetched deterministically from the
existing `GET /api/jobs/{id}/artifact` route.

## Privacy

Course Compiler's privacy boundary is grounded in where data is
allowed to live, what the app does automatically, and what the owner
must do explicitly.

- **Durable local data lives under ignored roots.** Source bytes,
  semantic outputs, workflow/application state, and durable build
  records are stored under the git-ignored `local-data/` root
  (by default `local-data/app/` SQLite stores). Generated caches and
  build artifacts may additionally use the git-ignored
  `local-artifacts/` and `build/` roots. The tracked Git repository
  must never contain private or content-bearing material.
- **Original filenames are not durable source identities.** The
  original filename you select in the browser is only a session
  convenience for the current page session; the durable source
  identity is an opaque per-source reference tied to the source's
  content digest and stored locally. The relay uses safe evidence
  identifiers/labels instead of relying on your original filenames.
- **GPT mode is not fully offline.** The app does **not** automatically
  upload your evidence to ChatGPT. However, the GPT relay is
  browser-mediated: when you follow the relay, you explicitly
  download the bounded current evidence listed for that turn and
  attach those files to a fresh ChatGPT conversation of your own.
  Evidence content therefore intentionally leaves your machine when
  you choose to follow the relay — the app's role is to keep that
  transfer explicit, bounded, and owner-controlled, never silent.
- **No secrets.** v0.1 ships GPT-only and does not require or store
  any provider API key.
- **The app is loopback-only by default.** It refuses non-loopback
  binds unless you explicitly pass `--allow-non-loopback`.
- **Content-safe diagnostics.** HTTP responses and startup lines
  never include raw source bytes, source paths, or stack traces;
  failure modes are stable codes (`host_invalid`, `data_root_invalid`,
  `build_not_succeeded`, …) that name only the kind of problem.

In short: content-bearing files are kept out of the tracked Git
repository and remain within approved ignored local storage, except
for the exact evidence you choose to transfer to ChatGPT when you
follow the GPT relay. The app does that transfer explicitly, not
silently, and never via an embedded provider key.

## Known limitations

- **Constrained GPT-output profile.** GPT-generated Markdown
  currently has a constrained accepted formatting profile. Unusual
  formula/Markdown formatting can still require repair. Future work
  includes broader GPT-output normalization and improved tolerance.
- **Tests run against invented synthetic PDFs only.** Tracked tests do
  not contain private course material and never exercise private
  genuine-course E2E.
- **v0.1 is a single-user local app, not a hosted service.** No
  multi-user isolation, no hosted HTTPS, no public submission, no
  official publication.
- **GPT mode only.** BYOK is paused and deferred to a future release.
- **Frontend is intentionally minimal.** Vanilla HTML/CSS/JS, no
  framework, no full responsive contract beyond the existing
  `@media(max-width:900px)` narrow-width stacking, no token fidelity,
  no pixel parity.

## v0.1 non-goals

The v0.1 Local Developer Preview does **not** claim and does **not**
include any of the following. They are explicitly deferred to a
future release:

- BYOK provider boundary.
- Multi-user identity / isolation.
- Hosted public HTTPS deployment.
- Public hardening and submission package.
- Official review, submission, and publication.
- Broad provider expansion beyond ChatGPT.
- Hosted SaaS, public service, or any other multi-tenant deployment.
- Production visual polish, full responsive design, pixel-perfect UI.

If you find yourself needing any of these in v0.1, that is the signal
that v0.1 is not the right release cut; it is a deliberate local
preview, not a partial production system.

## Commands

- `make app` — start the local Course Compiler web app on
  `127.0.0.1:8787` (the v0.1 entry point).
- `make test` — run the public test suite. Optional
  integration/full-toolchain cases skip cleanly when their documented
  prerequisites are unavailable.
- `make gate` — run the public release-validation checks (Python
  parsing, manifest integrity, ignored-path check, public tests, and
  the synthetic XeLaTeX toolchain probe, which is non-fatal when the
  TeX toolchain is not installed).

## Where to look next

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — released
  architecture at engineering level.
- [`docs/TOOLCHAIN.md`](docs/TOOLCHAIN.md) — exact pinned toolchain,
  fonts, and reproduction steps.
- [`LICENSE`](LICENSE) — MIT license.

Start with the *Quick-start workflow* above. The v0.1 Local Developer
Preview is intentionally small; everything else is planned for a
future release.
