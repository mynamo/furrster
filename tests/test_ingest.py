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
