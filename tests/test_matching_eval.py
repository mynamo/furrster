"""Matcher: the rule-based ranker, the guardrail's contribution, and feedback."""

from datetime import datetime, timezone

import pytest

from furrster import db, eval_matching as E
from furrster.config import Settings
from furrster.matcher import AdopterProfile, baseline_rank, rank, shortlist
from furrster.simulate import simulate


@pytest.fixture(scope="module")
def sim(tmp_path_factory):
    path = tmp_path_factory.mktemp("match") / "sim.db"
    s = Settings(None, None, None, "m", path, "94110", 50)
    simulate(s, days=45, seed=5, initial_population=90,
             end=datetime(2026, 9, 1, 14, tzinfo=timezone.utc))
    return s


def test_baseline_ranks_without_a_key_and_explains_itself(sim):
    profile = AdopterProfile(description="Small flat, first pet, quiet evenings.",
                             animal_type="dog", home="apartment",
                             experience="first-time", activity_level="low",
                             max_size="medium")
    pool = shortlist(sim, profile, limit=25)
    result = baseline_rank(profile, pool, top_n=5)
    assert len(result["matches"]) == 5
    assert result["model"] == "baseline-rules"
    scores = [m["fit_score"] for m in result["matches"]]
    assert scores == sorted(scores, reverse=True)
    for m in result["matches"]:
        assert m["rationale"] and m["concerns"] and m["questions_to_ask"]
        assert m["animal_id"] in {c["animal_id"] for c in pool}


def test_rank_uses_baseline_when_there_is_no_key(sim):
    profile = AdopterProfile(description="anything", animal_type="cat")
    pool = shortlist(sim, profile, limit=10)
    assert rank(sim, profile, pool)["model"] == "baseline-rules"


def test_first_time_adopters_are_steered_away_from_hard_cases(sim):
    hard = {"animal_id": 1, "name": "Boss", "type": "Dog", "age": "Adult",
            "size": "large", "special_needs": 1, "house_trained": 0,
            "tags_json": '["Needs experienced owner"]', "days_listed": 10}
    easy = dict(hard, animal_id=2, name="Pip", size="small", special_needs=0,
                house_trained=1, tags_json='["Gentle"]')
    newbie = AdopterProfile(description="first dog", experience="first-time",
                            home="apartment")
    pro = AdopterProfile(description="rescue veteran", experience="experienced",
                         home="house-yard")
    assert baseline_rank(newbie, [hard, easy])["matches"][0]["animal_id"] == 2
    assert baseline_rank(pro, [hard, easy])["matches"][0]["animal_id"] == 1


def test_the_sql_guardrail_is_doing_real_work(sim):
    """Unsafe suggestions should be ~0 on the production path and common without it."""
    conn = db.connect(sim.db_path)
    profiles = E.generate_profiles(conn, n=12, seed=3)
    conn.close()
    filtered = E.evaluate(sim, profiles, E.baseline_ranker(), name="baseline",
                          pool="filtered")
    raw = E.evaluate(sim, profiles, E.baseline_ranker(), name="baseline", pool="raw")
    assert filtered.violation_rate == 0.0
    assert raw.violation_rate > 0.2
    assert filtered.mean_utility > raw.mean_utility
    assert filtered.invalid_id_rate == 0.0
    assert raw.examples


def test_eval_catches_a_ranker_that_invents_animals(sim):
    conn = db.connect(sim.db_path)
    profiles = E.generate_profiles(conn, n=4, seed=1)
    conn.close()

    def liar(profile, candidates):
        return {"matches": [{"animal_id": -999, "fit_score": 99}]}

    result = E.evaluate(sim, profiles, liar, name="liar")
    assert result.invalid_id_rate == 1.0


def test_feedback_round_trip(tmp_path):
    conn = db.connect(tmp_path / "f.db")
    db.init_db(conn)
    conn.execute("INSERT INTO animals (animal_id, name, first_seen_at, last_seen_at) "
                 "VALUES (9, 'Otis', '2026-09-01', '2026-09-01')")
    conn.execute("INSERT INTO adopters (label, prefs_json, created_at) "
                 "VALUES ('apartment, first dog', '{}', '2026-09-01')")
    conn.execute("""INSERT INTO matches (adopter_id, animal_id, rank, fit_score, model,
                    created_at) VALUES (1, 9, 1, 88, 'baseline-rules', '2026-09-01')""")
    conn.commit()

    match_id = db.recent_matches(conn)[0]["match_id"]
    assert db.recent_matches(conn)[0]["outcome"] is None
    db.record_outcome(conn, match_id, "met", note="Saturday visit")
    db.record_outcome(conn, match_id, "adopted")
    assert db.recent_matches(conn)[0]["outcome"] == "adopted"

    summary = db.outcome_summary(conn)[0]
    assert summary["model"] == "baseline-rules" and summary["adopted"] == 1
    with pytest.raises(ValueError):
        db.record_outcome(conn, match_id, "vibes")
    conn.close()


def test_power_curve_needs_more_campaigns_for_smaller_effects():
    from furrster.outreach import power_curve

    small = power_curve(uplift=1.3, sims=800, seed=1).set_index("campaigns")["power"]
    big = power_curve(uplift=1.8, sims=800, seed=1).set_index("campaigns")["power"]
    assert (big >= small - 0.02).all()
    assert small.is_monotonic_increasing and big[400] > 0.9
    assert small[25] < 0.6  # a couple of dozen campaigns can't settle a small effect
