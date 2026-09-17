import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURES = Path(__file__).resolve().parent / "fixtures"

from furrster.config import Settings  # noqa: E402


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        petfinder_key="test-key",
        petfinder_secret="test-secret",
        anthropic_api_key=None,
        anthropic_model="claude-sonnet-4-5",
        db_path=tmp_path / "test.db",
        default_location="94110",
        default_distance=50,
    )


@pytest.fixture
def pages() -> list[dict]:
    return [fixture("animals_page1.json"), fixture("animals_page2.json")]
