"""Pull from Petfinder, normalize, and land it in SQLite."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import db
from .config import Settings
from .petfinder import PetfinderClient

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    run_id: int
    animals_seen: int = 0
    organizations_seen: int = 0
    departed: int = 0
    errors: list[str] = field(default_factory=list)


def ingest_animals(
    settings: Settings,
    *,
    animal_type: str | None = None,
    location: str | None = None,
    distance: int | None = None,
    max_pages: int = 20,
    client: PetfinderClient | None = None,
    sweep_departed: bool = True,
    **extra: Any,
) -> IngestResult:
    """One pull of one slice (usually one animal type) into the local warehouse."""
    conn = db.connect(settings.db_path)
    db.init_db(conn)

    params = {
        "type": animal_type,
        "location": location or settings.default_location,
        "distance": distance or settings.default_distance,
        "max_pages": max_pages,
        **extra,
    }
    run_id = db.start_run(conn, params)
    result = IngestResult(run_id=run_id)

    owns_client = client is None
    if client is None:
        key, secret = settings.require_petfinder()
        client = PetfinderClient(key, secret)

    try:
        stream = client.iter_animals(
            animal_type=animal_type,
            location=params["location"],
            distance=params["distance"],
            max_pages=max_pages,
            **extra,
        )
        for raw in stream:
            try:
                record = db.normalize_animal(raw)
                if record["animal_id"] is None:
                    continue
                db.upsert_animal(conn, record, run_id)
                result.animals_seen += 1
                if result.animals_seen % 100 == 0:
                    conn.commit()
                    log.info("…%s animals", result.animals_seen)
            except Exception as exc:  # one bad record should not kill the run
                result.errors.append(f"animal {raw.get('id')}: {exc}")
        conn.commit()

        if sweep_departed and result.animals_seen:
            result.departed = db.mark_departed(conn, run_id, {"type": animal_type})

        db.finish_run(
            conn,
            run_id,
            pages=0,
            animals=result.animals_seen,
            status="ok" if not result.errors else "partial",
            error="; ".join(result.errors[:5]) or None,
        )
    except Exception as exc:
        conn.commit()
        db.finish_run(
            conn, run_id, pages=0, animals=result.animals_seen,
            status="failed", error=str(exc),
        )
        raise
    finally:
        if owns_client:
            client.close()
        conn.close()

    return result


def ingest_organizations(
    settings: Settings,
    *,
    location: str | None = None,
    distance: int | None = None,
    max_pages: int = 10,
    client: PetfinderClient | None = None,
) -> int:
    conn = db.connect(settings.db_path)
    db.init_db(conn)

    owns_client = client is None
    if client is None:
        key, secret = settings.require_petfinder()
        client = PetfinderClient(key, secret)

    count = 0
    try:
        for raw in client.iter_organizations(
            location=location or settings.default_location,
            distance=distance or settings.default_distance,
            max_pages=max_pages,
        ):
            db.upsert_organization(conn, db.normalize_org(raw))
            count += 1
        conn.commit()
    finally:
        if owns_client:
            client.close()
        conn.close()
    return count


def load_fixture(settings: Settings, pages: Iterable[dict[str, Any]]) -> IngestResult:
    """Ingest already-fetched payloads (used by tests and for offline demos)."""
    conn = db.connect(settings.db_path)
    db.init_db(conn)
    run_id = db.start_run(conn, {"source": "fixture"})
    result = IngestResult(run_id=run_id)
    for payload in pages:
        for raw in payload.get("animals", []):
            record = db.normalize_animal(raw)
            db.upsert_animal(conn, record, run_id)
            result.animals_seen += 1
    conn.commit()
    db.finish_run(conn, run_id, pages=0, animals=result.animals_seen)
    conn.close()
    return result
