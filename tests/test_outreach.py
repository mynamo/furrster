"""Outreach: gap valuation and campaign-effect measurement.

The simulator features slow, long-listed animals (like a real shelter would) and
multiplies their adoption rate by CAMPAIGN_UPLIFT for 30 days. That selection
makes featured animals look no better than average in a naive comparison; the
risk-matched estimate must find the planted effect."""

from datetime import datetime, timezone

import pytest

from furrster import db, fitting as F, lifecycle as L, outreach as O
from furrster.config import Settings
from furrster.simulate import CAMPAIGN_UPLIFT, simulate

END = datetime(2026, 9, 1, 14, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def sim(tmp_path_factory):
    path = tmp_path_factory.mktemp("out") / "sim.db"
    simulate(Settings(None, None, None, "m", path, "94110", 50),
             days=90, seed=42, initial_population=140, end=END, campaigns=True)
    conn = db.connect(path)
    model = F.fit(conn)
    yield conn, model
    conn.close()


def test_matched_estimate_recovers_planted_uplift_and_naive_does_not(sim):
    conn, model = sim
    eff = O.measure_campaigns(conn, model)
    assert eff is not None and eff.campaigns >= 30
    assert eff.ci_low <= CAMPAIGN_UPLIFT <= eff.ci_high
    assert eff.matched_ratio > eff.naive_ratio
    # The naive comparison is fooled by selection: nowhere near the true effect.
    assert eff.naive_ratio < 1.3


def test_model_estimates_campaign_effect_as_a_factor(sim):
    _, model = sim
    row = model.table().set_index("key").loc["in_campaign"]
    assert row["ci_low"] > 1.0
    assert row["ci_low"] <= CAMPAIGN_UPLIFT <= row["ci_high"]
    assert model.feature_counts["in_campaign"] > 0


def test_scores_are_without_outreach_and_never_cite_it(sim):
    conn, model = sim
    rows = db.rows_to_dicts(db.fetch_active(conn))
    for s in F.score_rows(model, rows):
        assert all(f["name"] != "in_campaign" for f in s["factors"])


def test_listing_gaps_value_fixable_things_only(sim):
    conn, model = sim
    rows = db.rows_to_dicts(db.fetch_active(conn))
    gaps = O.listing_gaps(model, rows)
    assert not gaps.empty
    assert (gaps["gain"] >= -1e-9).all()
    # Rows that only lack compatibility info get no claimed gain.
    info_only = gaps[gaps["fixes"] == ""]
    assert (info_only["gain"].abs() < 1e-9).all()
    photo_rows = gaps[gaps["fixes"].str.contains("photos")]
    assert (photo_rows["gain"] > 0).all()

    by_shelter = O.gaps_by_shelter(gaps)
    assert by_shelter["listings_with_gaps"].sum() == len(gaps)
    assert by_shelter["expected_extra_adoptions"].sum() == pytest.approx(gaps["gain"].sum())


def test_backtest_reports_calibration(sim):
    conn, _ = sim
    bt = L.evaluate_scorer(conn, as_of_days_ago=45)
    cal = bt["calibration"]
    assert len(cal) == 5
    assert [g["predicted"] for g in cal] == sorted(g["predicted"] for g in cal)


def test_add_and_list_campaigns(tmp_path):
    conn = db.connect(tmp_path / "c.db")
    db.init_db(conn)
    conn.execute("INSERT INTO animals (animal_id, first_seen_at, last_seen_at) "
                 "VALUES (5, '2026-09-01', '2026-09-01')")
    cid = O.add_campaign(conn, 5, "feature", note="Saturday post")
    df = O.list_campaigns(conn)
    assert df.loc[0, "campaign_id"] == cid and df.loc[0, "kind"] == "feature"
    assert 5 in O.animals_in_recent_campaigns(conn, days=3650)
    conn.close()
