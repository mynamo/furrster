"""Configuration, loaded from environment / .env. No secrets live in code."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is optional at runtime
    def load_dotenv(*_a, **_k):  # type: ignore
        return False

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    petfinder_key: str | None
    petfinder_secret: str | None
    anthropic_api_key: str | None
    anthropic_model: str
    db_path: Path
    default_location: str | None
    default_distance: int

    def require_petfinder(self) -> tuple[str, str]:
        if not self.petfinder_key or not self.petfinder_secret:
            raise ConfigError(
                "PETFINDER_KEY / PETFINDER_SECRET are not set. "
                "Copy .env.example to .env and fill them in "
                "(get them at https://www.petfinder.com/developers/)."
            )
        return self.petfinder_key, self.petfinder_secret

    def require_anthropic(self) -> str:
        if not self.anthropic_api_key:
            raise ConfigError(
                "ANTHROPIC_API_KEY is not set. The ingest and scoring commands work "
                "without it; matching and bio drafting need it."
            )
        return self.anthropic_api_key


def load_settings() -> Settings:
    db = os.getenv("FURRSTER_DB", "data/furrster.db")
    db_path = Path(db)
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return Settings(
        petfinder_key=os.getenv("PETFINDER_KEY"),
        petfinder_secret=os.getenv("PETFINDER_SECRET"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
        db_path=db_path,
        default_location=os.getenv("FURRSTER_LOCATION"),
        default_distance=int(os.getenv("FURRSTER_DISTANCE", "50")),
    )
