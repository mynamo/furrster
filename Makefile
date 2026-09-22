.PHONY: install ingest score lifecycle outreach simulate app app-sim test schedule unschedule schedule-status clean

PY    := ./.venv/bin/python
PLIST := $(HOME)/Library/LaunchAgents/com.furrster.ingest.plist

install:
	python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

ingest:
	$(PY) -m furrster.cli ingest --type dog --type cat

score:
	$(PY) -m furrster.cli at-risk --limit 25

lifecycle:
	$(PY) -m furrster.cli lifecycle

outreach:
	$(PY) -m furrster.cli outreach-cycle

# Synthetic history in its own file, so it can never mix with real pulls.
simulate:
	FURRSTER_DB=data/sim.db $(PY) -m furrster.cli simulate --days 90 --force
	FURRSTER_DB=data/sim.db $(PY) -m furrster.cli fit

app:
	$(PY) -m furrster.cli app

app-sim:
	FURRSTER_DB=data/sim.db $(PY) -m furrster.cli app

test:
	$(PY) -m pytest

# ---- daily pull on macOS (launchd). Run these on your Mac, not in a container.
schedule:
	mkdir -p data/logs "$(HOME)/Library/LaunchAgents"
	sed "s|__REPO__|$(CURDIR)|g" launchd/com.furrster.ingest.plist.template > "$(PLIST)"
	-launchctl bootout gui/$$(id -u) "$(PLIST)" 2>/dev/null
	launchctl bootstrap gui/$$(id -u) "$(PLIST)"
	@echo "Scheduled daily at 07:15. Logs: data/logs/ingest.log"

unschedule:
	-launchctl bootout gui/$$(id -u) "$(PLIST)"
	rm -f "$(PLIST)"

schedule-status:
	@launchctl print gui/$$(id -u)/com.furrster.ingest 2>/dev/null | grep -E "state|last exit|runs" || echo "not scheduled"
	@tail -n 5 data/logs/ingest.log 2>/dev/null || true

clean:
	rm -f data/furrster.db data/sim.db
