import re

from furrster.ingest import load_fixture
from furrster.matcher import AdopterProfile, match, shortlist


class FakeLLM:
    """Stands in for Anthropic so the matching logic is testable offline."""

    model = "fake-model"

    def __init__(self):
        self.last_user = None

    def complete_json(self, *, system, user, **kwargs):
        self.last_user = user
        ids = [int(m) for m in re.findall(r'"animal_id":\s*(\d+)', user)]
        return {
            "matches": [
                {"animal_id": ids[0], "fit_score": 82, "rationale": "r",
                 "concerns": "c", "questions_to_ask": ["q"]}
            ],
            "notes": "n",
        }


def test_shortlist_respects_hard_constraints(settings, pages):
    load_fixture(settings, pages)
    profile = AdopterProfile(
        description="Family with a toddler and a resident dog, small apartment.",
        animal_type="dog", has_children=True, has_dogs=True, max_size="medium",
    )
    rows = shortlist(settings, profile)
    assert rows
    for r in rows:
        assert r["type"].lower() == "dog"
        assert r["good_with_children"] in (None, 1)
        assert r["good_with_dogs"] in (None, 1)
        assert (r["size"] or "small").lower() in {"small", "medium"}
    # Bruno is dog-hostile and kid-hostile: he must not reach the model.
    assert "Bruno" not in {r["name"] for r in rows}


def test_unknown_is_not_treated_as_no(settings, pages):
    load_fixture(settings, pages)
    profile = AdopterProfile(description="I have cats.", has_cats=True)
    names = {r["name"] for r in shortlist(settings, profile, limit=100)}
    assert "Nulls" in names  # good_with_cats is NULL, so it stays a candidate


def test_match_persists_results(settings, pages):
    load_fixture(settings, pages)
    fake = FakeLLM()
    profile = AdopterProfile(description="Quiet first-time adopter.", animal_type="dog")
    result = match(settings, profile, candidates=shortlist(settings, profile), llm=fake)

    assert result["matches"][0]["fit_score"] == 82
    assert "adopter_id" in result

    from furrster import db
    conn = db.connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM adopters").fetchone()[0] == 1
    conn.close()


def test_empty_pool_short_circuits_without_calling_the_model(settings):
    profile = AdopterProfile(description="anything", animal_type="hamster")
    result = match(settings, profile, candidates=[], llm=None)
    assert result["matches"] == []
