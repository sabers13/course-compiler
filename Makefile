.PHONY: app test gate toolchain-probe

PYTHON ?= python3

app:
	@$(PYTHON) -m course_compiler.app

test:
	@$(PYTHON) -m unittest discover -s tests -p 'test_*.py'

gate:
	@$(PYTHON) ci/gate.py

toolchain-probe:
	@mkdir -p local-artifacts/cp0/toolchain-probe
	@latexmk -xelatex -interaction=nonstopmode -halt-on-error \
		-file-line-error -g \
		-outdir=local-artifacts/cp0/toolchain-probe \
		tests/fixtures/synthetic/cp0-toolchain-probe.tex
