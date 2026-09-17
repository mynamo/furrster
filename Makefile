.PHONY: install ingest score test clean

install:
	python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

ingest:
	./.venv/bin/python -m furrster.cli ingest --type dog --type cat

score:
	./.venv/bin/python -m furrster.cli at-risk --limit 25

test:
	./.venv/bin/python -m pytest

clean:
	rm -f data/furrster.db
