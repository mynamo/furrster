from datetime import datetime, timedelta, timezone

import pytest

from furrster import db
from furrster.ingest import load_fixture
from furrster.scoring import cohort_stats, score_population


def active_rows(settings):
    conn = db.connect(settings.db_path)
    rows = db.rows_to_dicts(db.fetch_active(conn))
    conn.close()
    return rows


def test_long_listed_hard_to_place_animal_ranks_first(settings, pages):
    load_fixture(settings, pages)
    scored = score_population(active_rows(settings))
    assert scored[0].name == "Bruno"
    assert scored[0].band in {"critical", "elevated"}
    names = {f.name for f in scored[0].factors}
    assert {"special_needs", "no_photos", "no_kids", "no_other_pets"} <= names


def test_fresh_well_documented_puppy_scores_low(settings, pages):
    load_fixture(settings, pages)
    scored = {s.name: s for s in score_population(active_rows(settings))}
    assert scored["Pixel"].score < 25
    assert scored["Pixel"].band == "ok"
    assert scored["Pixel"].score < scored["Bruno"].score


def test_missing_published_at_does_not_crash(settings, pages):
    load_fixture(settings, pages)
    scored = {s.name: s for s in score_population(active_rows(settings))}
    assert scored["Nulls"].days_listed >= 0


def test_score_is_bounded_and_explained(settings, pages):
    load_fixture(settings, pages)
    for s in score_population(active_rows(settings)):
        assert 0 <= s.score <= 100
        assert abs(sum(f.points for f in s.factors) - s.score) < 0.01 or s.score == 100
        assert s.summary()


def test_cohorts_are_species_and_size_aware(settings, pages):
    load_fixture(settings, pages)
    stats = cohort_stats(active_rows(settings))
    assert "dog/small" in stats or "dog/big" in stats
    assert "cat/small" in stats
    assert all(v["n"] > 0 for v in stats.values())


def test_percentile_uses_the_cohort_not_the_whole_population():
    now = datetime.now(timezone.utc)
    stamp = lambda d: (now - timedelta(days=d)).isoformat()
    rows = [
        {"animal_id": 1, "name": "OldCat", "type": "Cat", "size": "Small",
         "published_at": stamp(200), "description": "x" * 400, "photo_count": 3},
        {"animal_id": 2, "name": "OlderDog", "type": "Dog", "size": "Small",
         "published_at": stamp(400), "description": "x" * 400, "photo_count": 3},
        {"animal_id": 3, "name": "NewDog", "type": "Dog", "size": "Small",
         "published_at": stamp(2), "description": "x" * 400, "photo_count": 3},
    ]
    scored = {s.name: s for s in score_population(rows, now=now)}
    # Every cohort here is under MIN_COHORT_N, so all three fall back to the
    # whole population rather than to a meaningless two-animal percentile.
    assert scored["OlderDog"].tenure_percentile == pytest.approx(2 / 3)
    assert scored["NewDog"].tenure_percentile == 0.0
    assert scored["OldCat"].tenure_percentile > scored["NewDog"].tenure_percentile


def test_small_cohorts_fall_back_instead_of_inventing_a_percentile():
    now = datetime.now(timezone.utc)
    stamp = lambda d: (now - timedelta(days=d)).isoformat()
    # 12 small dogs (a real cohort) plus 2 big dogs (not a cohort).
    rows = [
        {"animal_id": i, "name": f"Small{i}", "type": "Dog", "size": "Small",
         "published_at": stamp(i * 10), "description": "x" * 400, "photo_count": 3}
        for i in range(1, 13)
    ] + [
        {"animal_id": 100, "name": "BigOld", "type": "Dog", "size": "Large",
         "published_at": stamp(400), "description": "x" * 400, "photo_count": 3},
        {"animal_id": 101, "name": "BigNew", "type": "Dog", "size": "Large",
         "published_at": stamp(1), "description": "x" * 400, "photo_count": 3},
    ]
    scored = {s.name: s for s in score_population(rows, now=now)}
    # dog/big has n=2, so BigOld is ranked against all dogs, not against one peer.
    assert scored["BigOld"].tenure_percentile > 0.9
    # dog/small has n=12, so it keeps its own cohort.
    assert 0.0 < scored["Small6"].tenure_percentile < 1.0
