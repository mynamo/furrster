"""SQLite layer: schema bootstrap, upserts, and snapshot writes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()


# ------------------------------------------------------------------ mapping


def _b(value: Any) -> int | None:
    """Petfinder booleans are true / false / null — keep the null distinct."""
    if value is None:
        return None
    return 1 if value else 0


def normalize_animal(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten a Petfinder animal record into our column layout."""
    breeds = raw.get("breeds") or {}
    colors = raw.get("colors") or {}
    attrs = raw.get("attributes") or {}
    env = raw.get("environment") or {}
    contact = raw.get("contact") or {}
    address = contact.get("address") or {}
    photos = raw.get("photos") or []

    return {
        "animal_id": raw.get("id"),
        "organization_id": raw.get("organization_id"),
        "name": (raw.get("name") or "").strip(),
        "type": raw.get("type"),
        "species": raw.get("species"),
        "breed_primary": breeds.get("primary"),
        "breed_secondary": breeds.get("secondary"),
        "breed_mixed": _b(breeds.get("mixed")),
        "breed_unknown": _b(breeds.get("unknown")),
        "color_primary": colors.get("primary"),
        "color_secondary": colors.get("secondary"),
        "age": raw.get("age"),
        "gender": raw.get("gender"),
        "size": raw.get("size"),
        "coat": raw.get("coat"),
        "description": raw.get("description"),
        "status": raw.get("status"),
        "spayed_neutered": _b(attrs.get("spayed_neutered")),
        "house_trained": _b(attrs.get("house_trained")),
        "declawed": _b(attrs.get("declawed")),
        "special_needs": _b(attrs.get("special_needs")),
        "shots_current": _b(attrs.get("shots_current")),
        "good_with_children": _b(env.get("children")),
        "good_with_dogs": _b(env.get("dogs")),
        "good_with_cats": _b(env.get("cats")),
        "tags_json": json.dumps(raw.get("tags") or []),
        "photo_count": len(photos),
        "video_count": len(raw.get("videos") or []),
        "primary_photo": (photos[0].get("medium") if photos else None),
        "contact_city": address.get("city"),
        "contact_state": address.get("state"),
        "contact_postcode": address.get("postcode"),
        "distance_miles": raw.get("distance"),
        "url": raw.get("url"),
        "published_at": raw.get("published_at"),
        "status_changed_at": raw.get("status_changed_at"),
        "raw_json": json.dumps(raw, separators=(",", ":")),
    }


def normalize_org(raw: dict[str, Any]) -> dict[str, Any]:
    address = (raw.get("address") or {})
    return {
        "organization_id": raw.get("id"),
        "name": raw.get("name"),
        "email": raw.get("email"),
        "phone": raw.get("phone"),
        "city": address.get("city"),
        "state": address.get("state"),
        "postcode": address.get("postcode"),
        "country": address.get("country"),
        "url": raw.get("url"),
        "website": raw.get("website"),
        "mission": raw.get("mission_statement"),
        "raw_json": json.dumps(raw, separators=(",", ":")),
    }


def content_hash(record: dict[str, Any]) -> str:
    """Hash the fields a shelter would actually edit, so we can spot silent updates."""
    watched = [
        record.get("name"),
        record.get("description"),
        record.get("status"),
        record.get("photo_count"),
        record.get("age"),
        record.get("size"),
    ]
    blob = "|".join("" if v is None else str(v) for v in watched)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------- writes


def start_run(conn: sqlite3.Connection, params: dict[str, Any]) -> int:
    cur = conn.execute(
        "INSERT INTO ingest_runs (started_at, params_json) VALUES (?, ?)",
        (utcnow(), json.dumps(params)),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    pages: int,
    animals: int,
    status: str = "ok",
    error: str | None = None,
) -> None:
    conn.execute(
        """UPDATE ingest_runs
              SET finished_at = ?, pages_fetched = ?, animals_seen = ?,
                  status = ?, error = ?
            WHERE run_id = ?""",
        (utcnow(), pages, animals, status, error, run_id),
    )
    conn.commit()


def upsert_organization(conn: sqlite3.Connection, org: dict[str, Any]) -> None:
    now = utcnow()
    cols = list(org.keys())
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "organization_id")
    conn.execute(
        f"""INSERT INTO organizations ({", ".join(cols)}, first_seen_at, last_seen_at)
            VALUES ({placeholders}, ?, ?)
            ON CONFLICT(organization_id) DO UPDATE SET
                {updates}, last_seen_at = excluded.last_seen_at""",
        (*[org[c] for c in cols], now, now),
    )


def ensure_organization_stub(conn: sqlite3.Connection, organization_id: str | None) -> None:
    """Animals arrive before organizations do.

    /animals gives us an organization_id but no organization record, so an animal
    insert would trip the foreign key. We write a placeholder row; a later
    `orgs` pull fills in the real name, contact details and mission via upsert.
    """
    if not organization_id:
        return
    now = utcnow()
    conn.execute(
        """INSERT INTO organizations (organization_id, first_seen_at, last_seen_at)
           VALUES (?, ?, ?)
           ON CONFLICT(organization_id) DO UPDATE SET last_seen_at = excluded.last_seen_at""",
        (organization_id, now, now),
    )


def upsert_animal(conn: sqlite3.Connection, record: dict[str, Any], run_id: int) -> None:
    """Insert or update the current-state row, then append an immutable snapshot."""
    now = utcnow()
    ensure_organization_stub(conn, record.get("organization_id"))
    cols = list(record.keys())
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "animal_id")

    conn.execute(
        f"""INSERT INTO animals ({", ".join(cols)}, first_seen_at, last_seen_at, is_active)
            VALUES ({placeholders}, ?, ?, 1)
            ON CONFLICT(animal_id) DO UPDATE SET
                {updates},
                last_seen_at = excluded.last_seen_at,
                is_active = 1,
                left_listing_at = NULL""",
        (*[record[c] for c in cols], now, now),
    )
    conn.execute(
        """INSERT INTO animal_snapshots
               (run_id, animal_id, observed_at, status, photo_count,
                description_len, distance_miles, content_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run_id,
            record["animal_id"],
            now,
            record.get("status"),
            record.get("photo_count"),
            len(record.get("description") or ""),
            record.get("distance_miles"),
            content_hash(record),
        ),
    )


def mark_departed(conn: sqlite3.Connection, run_id: int, scope: dict[str, Any]) -> int:
    """Flag animals that were active before this run but did not appear in it.

    A listing disappearing is our best available adoption proxy — Petfinder does not
    tell you why a pet left. `scope` restricts the sweep to the slice we actually
    re-pulled, so a dog-only run never retires every cat in the database.
    """
    clauses = ["is_active = 1"]
    params: list[Any] = []
    if scope.get("type"):
        clauses.append("type = ?")
        params.append(scope["type"])
    if scope.get("organization_id"):
        clauses.append("organization_id = ?")
        params.append(scope["organization_id"])

    where = " AND ".join(clauses)
    cur = conn.execute(
        f"""UPDATE animals
               SET is_active = 0, left_listing_at = ?
             WHERE {where}
               AND animal_id NOT IN (
                   SELECT animal_id FROM animal_snapshots WHERE run_id = ?
               )""",
        (utcnow(), *params, run_id),
    )
    conn.commit()
    return cur.rowcount


def fetch_active(
    conn: sqlite3.Connection,
    *,
    animal_type: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM v_active_animals"
    params: list[Any] = []
    if animal_type:
        sql += " WHERE type = ?"
        params.append(animal_type)
    sql += " ORDER BY days_listed DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]
