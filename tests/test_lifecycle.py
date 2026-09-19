from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from furrster import db, lifecycle as L
from furrster.simulate import simulate


def spells(rows):
    return pd.DataFrame(rows, columns=["entry_day", "exit_day", "event"])


def test_km_matches_hand_calculation():
    # Textbook case, no truncation: departures at 2, 4; censored at 3, 5.
    km = L.kaplan_meier(spells([(0, 2, 1), (0, 3, 0), (0, 4, 1), (0, 5, 0)]))
    s = dict(zip(km["day"], km["survival"]))
    assert s[2.0] == pytest.approx(3 / 4)          # 1 of 4 at risk leaves
    assert s[4.0] == pytest.approx(3 / 4 * 1 / 2)  # 1 of 2 remaining leaves
    assert L.median_days(km) == 4.0


def test_delayed_entry_keeps_late_arrivals_out_of_early_risk_sets():
    # Animal B was already listed 10 days when we first saw it. It must not count
    # as "at risk" at day 2, or it would dilute A's departure.
    km = L.kaplan_meier(spells([(0, 2, 1), (10, 20, 1)]))
    row = km[km["day"] == 2.0].iloc[0]
    assert row["at_risk"] == 1
    assert row["survival"] == 0.0 or km["survival"].iloc[-1] == 0.0


def test_ignoring_truncation_overstates_tenure():
    rng = np.random.default_rng(0)
    # True constant hazard; we only observe animals that survived to a random entry.
    n = 4000
    true = rng.exponential(30, n)
    entry = rng.uniform(0, 60, n)
    seen = true > entry
    df = spells(list(zip(entry[seen], true[seen], [1] * seen.sum())))
    honest = L.median_days(L.kaplan_meier(df))
    naive = L.median_days(L.kaplan_meier(df.assign(entry_day=0.0)))
    true_median = 30 * np.log(2)
    assert abs(honest - true_median) < 3
    assert naive > true_median + 15


@pytest.fixture(scope="module")
def sim_settings(tmp_path_factory):
    from furrster.config import Settings

    s = Settings(None, None, None, "m", tmp_path_factory.mktemp("sim") / "sim.db",
                 "94110", 50)
    simulate(s, days=50, seed=3, initial_population=60,
             end=datetime(2026, 9, 1, tzinfo=timezone.utc))
    return s


def test_simulation_runs_through_real_ingest(sim_settings):
    conn = db.connect(sim_settings.db_path)
    runs = conn.execute("SELECT COUNT(*) FROM ingest_runs").fetchone()[0]
    departed = conn.execute("SELECT COUNT(*) FROM animals WHERE is_active=0").fetchone()[0]
    fp = conn.execute(
        "SELECT COUNT(*) FROM animals WHERE first_published_at IS NULL").fetchone()[0]
    conn.close()
    assert runs == 51
    assert departed > 0
    assert fp == 0  # '+0000' timestamps survived normalization


def test_lifecycle_summaries_and_backtest(sim_settings):
    conn = db.connect(sim_settings.db_path)
    sp = L.load_spells(conn, now=datetime(2026, 9, 1, 14, tzinfo=timezone.utc))
    assert not sp.empty and sp["event"].sum() > 0
    assert (sp["exit_day"] > sp["entry_day"]).all()

    by_age = L.summarize(sp, "age", min_n=5)
    assert {"median_days", "still_listed_at_30d"} <= set(by_age.columns)

    eff = L.listing_edit_effect(conn)
    assert eff.window_days == 30

    bt = L.evaluate_scorer(conn, as_of_days_ago=20, horizon_days=15)
    assert "error" not in bt
    assert 0.0 <= bt["auc"] <= 1.0
    assert bt["spearman_vs_true_hazard"] < 0  # higher risk score <-> lower true hazard
    conn.close()
