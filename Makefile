.PHONY: install run seed test reset demo

install:
	python -m pip install -e ".[dev]"

run:
	uvicorn app.main:app --reload

seed:
	python scripts/seed_demo.py

test:
	pytest

reset:
	rm -f var/audience_ops.db var/mock_marketing_syncs.jsonl
	python scripts/seed_demo.py

demo:
	python scripts/run_demo.py
