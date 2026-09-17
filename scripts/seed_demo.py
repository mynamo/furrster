"""Load the test fixtures into the real database so you can try the CLI with no API key.

    python scripts/seed_demo.py
    python -m furrster.cli at-risk
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from furrster.config import load_settings  # noqa: E402
from furrster.ingest import load_fixture  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"

if __name__ == "__main__":
    settings = load_settings()
    pages = [
        json.loads((FIXTURES / "animals_page1.json").read_text()),
        json.loads((FIXTURES / "animals_page2.json").read_text()),
    ]
    result = load_fixture(settings, pages)
    print(f"Seeded {result.animals_seen} demo animals into {settings.db_path}")
    print("Now try:  python -m furrster.cli at-risk")
