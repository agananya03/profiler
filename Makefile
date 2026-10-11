.PHONY: install test lint run clean

install:
	pip install -e .

test:
	pytest -v

lint:
	python -m ruff check src tests || true

run:
	python -m profiler run --input data/companies.txt --out out/ --max-requests 2000 --budget-usd 10

clean:
	rm -rf build/ dist/ *.egg-info .pytest_cache __pycache__ out/
