# Toolchain

Course Compiler's v0.1.0-alpha.1 Local Developer Preview runs on a
modest local toolchain. The application itself uses only the Python
standard library, but the deterministic PDF build relies on a TeX
Live install.

## Required

- **Python 3.10+** on `PATH` as `python3`.
- The application code uses only the Python standard library. No
  third-party Python packages are required to run `make app`.

## Optional — for `make gate` and PDF compilation

The public gate and the synthetic XeLaTeX toolchain probe require:

- `latexmk`, `xelatex`, and `kpsewhich` from a working **TeX Live**
  install (`texlive-xetex`, `texlive-base`, `texlive-latex-base`,
  `texlive-latex-recommended`, `texlive-latex-extra`,
  `texlive-fonts-recommended`).
- `pdftoppm` (Poppler tools) for any PDF page preview utilities.
- `fc-match` (fontconfig) plus the pinned fonts:
  - `fonts-lmodern` (Latin Modern Roman / Math).
  - `fonts-noto-mono` (Noto Sans Mono).

## Optional — for the MCP seam

`requirements-mcp.txt` lists the official MCP Python SDK v2. It is
**not** required for the v0.1 Local Developer Preview; install it
only when exposing the app to a hosted / remote MCP client.

## Running the public validation gate

From the repository root:

```bash
make gate
```

This runs, in order:

1. The required public files are present.
2. Every Python source parses.
3. The released module manifest validates.
4. No private or governance paths are tracked in Git.
5. The ignored roots (`local-data/`, `local-artifacts/`, `build/`)
   are listed in `.gitignore`.
6. The full public test suite passes.
7. A synthetic XeLaTeX probe compiles (skipped with a non-fatal
   notice if the TeX toolchain is not installed).

## Running the test suite

```bash
make test
```

This runs every tracked unit and integration test under `tests/`.
Tests use invented synthetic PDFs only; no real private course
material is required or included.

## Running the toolchain probe alone

```bash
make toolchain-probe
```

This compiles a synthetic preamble document with `latexmk -xelatex`
and retains the resulting PDF under
`local-artifacts/cp0/toolchain-probe/` for inspection.

## Toolchain probe failures

If the public gate fails the toolchain probe, install the missing
TeX Live / Poppler / fontconfig pieces listed above and rerun
`make gate`. The gate never modifies source files.
