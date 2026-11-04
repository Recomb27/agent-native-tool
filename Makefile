.PHONY: install test verify reproduce figures

install:
	uv sync --extra campaign --extra figures --extra dev --locked

test:
	uv run --extra campaign --extra dev pytest

verify:
	python3 scripts/verify_release.py

reproduce:
	python3 paper/scripts/recompute_results.py

figures:
	uv run --extra figures python paper/scripts/make_v133_figures.py --only all --output-dir outputs/figures

