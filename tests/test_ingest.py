import httpx

from furrster import db
from furrster.ingest import ingest_animals, load_fixture
from furrster.petfinder import PetfinderClient
from tests.conftest import fixture


def test_load_fixture_populates_tables(settings, pages):
    result = load_fixture(settings, pages)
    assert result.animals_seen == 33

    conn = db.connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM animals").fetchone()[0] == 33
    assert conn.execute("SELECT COUNT(*) FROM animal_snapshots").fetchone()[0] == 33

    bruno = conn.execute(
        "SELECT * FROM animals WHERE name = 'Bruno'"
    ).fetchone()
    assert bruno["special_needs"] == 1
    assert bruno["good_with_children"] == 0
    assert bruno["photo_count"] == 0

    nulls = conn.execute("SELECT * FROM animals WHERE name = 'Nulls'").fetchone()
    # Unknown must stay NULL, not collapse to 0 — the matcher depends on it.
    assert nulls["good_with_cats"] is None
    assert nulls["published_at"] is None
    conn.close()


def test_reingest_upserts_and_appends_snapshot(settings, pages):
    load_fixture(settings, pages)
    load_fixture(settings, pages)

    conn = db.connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM animals").fetchone()[0] == 33
    assert conn.execute("SELECT COUNT(*) FROM animal_snapshots").fetchone()[0] == 66
    conn.close()


def test_departed_animals_are_retired(settings, pages):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json=fixture("token.json"))
        page = int(request.url.params.get("page", 1))
        return httpx.Response(200, json=fixture(f"animals_page{page}.json"))

    client = PetfinderClient(
        "k", "s", client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep_between_pages=0,
    )
    first = ingest_animals(settings, client=client, max_pages=10)
    assert first.animals_seen == 33

    # Second run returns only page 2 — everything unique to page 1 should retire.
    def handler2(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json=fixture("token.json"))
        payload = fixture("animals_page2.json")
        payload["pagination"]["total_pages"] = 1
        payload["pagination"]["current_page"] = 1
        return httpx.Response(200, json=payload)

    client2 = PetfinderClient(
        "k", "s", client=httpx.Client(transport=httpx.MockTransport(handler2)),
        sleep_between_pages=0,
    )
    second = ingest_animals(settings, client=client2, max_pages=10)
    assert second.animals_seen == 13
    assert second.departed == 20

    conn = db.connect(settings.db_path)
    row = conn.execute("SELECT * FROM animals WHERE name = 'Bruno'").fetchone()
    assert row["is_active"] == 0
    assert row["left_listing_at"] is not None
    conn.close()


def test_content_hash_changes_when_listing_is_edited():
    a = db.normalize_animal({"id": 1, "name": "Rex", "description": "one", "photos": []})
    b = db.normalize_animal({"id": 1, "name": "Rex", "description": "two", "photos": []})
    assert db.content_hash(a) != db.content_hash(b)


def test_petfinder_timestamp_format_is_normalized_for_sqlite(settings):
    """Real Petfinder timestamps end in '+0000', which SQLite silently can't parse."""
    raw = {"id": 42, "name": "Rex", "status": "adoptable", "photos": [],
           "published_at": "2026-06-01T19:13:01+0000"}
    rec = db.normalize_animal(raw)
    assert rec["published_at"] == "2026-06-01T19:13:01+00:00"

    load_fixture(settings, [{"animals": [raw]}])
    conn = db.connect(settings.db_path)
    row = conn.execute("SELECT days_listed FROM v_active_animals WHERE animal_id=42").fetchone()
    conn.close()
    assert row["days_listed"] is not None and row["days_listed"] > 0


def test_to_utc_iso_handles_offsets_and_garbage():
    assert db.to_utc_iso("2026-01-01T00:00:00-0500") == "2026-01-01T05:00:00+00:00"
    assert db.to_utc_iso("2026-01-01T00:00:00Z") == "2026-01-01T00:00:00+00:00"
    assert db.to_utc_iso("not a date") is None
    assert db.to_utc_iso(None) is None


def test_extra_large_is_stored_as_xlarge():
    """The API filters on 'xlarge' but returns 'Extra Large' — scoring and SQL need one form."""
    rec = db.normalize_animal({"id": 1, "size": "Extra Large", "photos": []})
    assert rec["size"] == "xlarge"
    assert db.normalize_animal({"id": 2, "size": "Medium", "photos": []})["size"] == "medium"


def test_relist_does_not_reset_tenure(settings):
    """A shelter relisting an animal bumps published_at; our tenure must not reset."""
    old = {"id": 7, "name": "Otis", "status": "adoptable", "photos": [],
           "published_at": "2026-01-01T00:00:00+0000"}
    relisted = dict(old, published_at="2026-09-01T00:00:00+0000")
    load_fixture(settings, [{"animals": [old]}])
    load_fixture(settings, [{"animals": [relisted]}])

    conn = db.connect(settings.db_path)
    row = conn.execute("SELECT * FROM v_active_animals WHERE animal_id = 7").fetchone()
    conn.close()
    assert row["published_at"].startswith("2026-09-01")
    assert row["first_published_at"].startswith("2026-01-01")
    assert row["listing_started_at"].startswith("2026-01-01")
    assert row["relisted"] == 1


def test_review_workflow_round_trip(settings, pages):
    load_fixture(settings, pages)
    conn = db.connect(settings.db_path)
    conn.execute(
        """INSERT INTO generated_content (animal_id, kind, body, created_at)
           VALUES (1000, 'bio', 'draft text', ?)""", (db.utcnow(),))
    conn.commit()
    pending = db.list_generated(conn, "pending")
    assert len(pending) == 1 and pending[0]["animal_name"] == "Bruno"

    db.set_review(conn, pending[0]["content_id"], "approved", note="lgtm", body="edited")
    assert db.list_generated(conn, "pending") == []
    approved = db.list_generated(conn, "approved")[0]
    assert approved["body"] == "edited" and approved["reviewed_at"]
    conn.close()


def test_migration_adds_columns_to_an_old_database(tmp_path):
    """A v0.1 database (no review columns, no first_published_at) must upgrade in place."""
    path = tmp_path / "old.db"
    conn = db.connect(path)
    db.init_db(conn)
    # Recreate the v0.1 shape by dropping everything added since.
    conn.execute("DROP VIEW v_active_animals")
    for table, column, _ in db._MIGRATIONS:
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    conn.commit()
    conn.close()

    conn = db.connect(path)
    db.init_db(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(generated_content)")}
    assert {"review_status", "reviewed_at", "review_note"} <= cols
    assert "first_published_at" in {r[1] for r in conn.execute("PRAGMA table_info(animals)")}
    conn.execute("SELECT listing_started_at FROM v_active_animals").fetchall()
    conn.close()
