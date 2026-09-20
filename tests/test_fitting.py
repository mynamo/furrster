"""The fitting code must recover effects we planted ourselves before we trust it on
real data. The simulator gives every animal a known adoption rate built from known
multipliers, so we can check the estimates against the truth."""

from datetime import datetime, timezone

import numpy as np
import pytest

from furrster import db, fitting as F
from furrster.config import Settings
from furrster.simulate import simulate

# The multipliers simulate.py uses, expressed against the model's reference levels.
TRUE_RATE_RATIOS = {
    "age_baby": 2.4, "age_young": 1.5, "age_senior": 0.45,
    "size_small": 1.2, "size_large": 0.7, "size_xlarge": 0.5,
    "is_cat": 0.85, "special_needs": 0.5, "no_kids": 0.75, "only_pet": 0.7,
    "photos_0": 0.45, "photos_1": 0.75, "thin_writeup": 0.8, "hard_tag": 0.8,
    "log_tenure": 1.0,  # the simulator's hazard does not depend on time listed
}
END = datetime(2026, 9, 1, 14, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def sim_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("fit") / "sim.db"
    simulate(Settings(None, None, None, "m", path, "94110", 50),
             days=90, seed=42, initial_population=140, end=END)
    return path


def test_recovers_planted_effects(sim_db):
    conn = db.connect(sim_db)
    model = F.fit(conn)
    conn.close()
    table = model.table().set_index("key")

    covered = [
        table.loc[k, "ci_low"] <= v <= table.loc[k, "ci_high"]
        for k, v in TRUE_RATE_RATIOS.items()
    ]
    # 95% intervals: expect ~14 of 15 to cover; allow a little bad luck.
    assert sum(covered) >= 13, table.assign(true=TRUE_RATE_RATIOS)

    # And the directions that matter most must be unambiguous.
    for key in ("age_senior", "size_xlarge", "special_needs"):
        assert table.loc[key, "ci_high"] < 1.0
    assert table.loc["age_baby", "ci_low"] > 1.0
    # No duration dependence was planted, and none should be "found".
    assert table.loc["log_tenure", "ci_low"] < 1.0 < table.loc["log_tenure", "ci_high"]
    assert model.simulated is True


def test_panel_chains_runs_per_slice(tmp_path):
    """A dog-only pull followed by a cat-only pull must not mark every dog adopted."""
    from furrster.ingest import ingest_animals
    import httpx
    from furrster.petfinder import PetfinderClient

    def client(animals):
        def handler(req):
            if req.url.path.endswith("/token"):
                return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
            return httpx.Response(200, json={"animals": animals, "pagination": {
                "total_pages": 1, "current_page": 1}})
        return PetfinderClient("k", "s", client=httpx.Client(
            transport=httpx.MockTransport(handler)), sleep_between_pages=0)

    s = Settings(None, None, None, "m", tmp_path / "x.db", "94110", 50)
    dog = {"id": 1, "type": "Dog", "status": "adoptable", "photos": []}
    cat = {"id": 2, "type": "Cat", "status": "adoptable", "photos": []}
    for day in range(3):
        db.set_clock(datetime(2026, 9, 1 + day, 8, tzinfo=timezone.utc))
        ingest_animals(s, animal_type="dog", client=client([dog]))
        db.set_clock(datetime(2026, 9, 1 + day, 9, tzinfo=timezone.utc))
        ingest_animals(s, animal_type="cat", client=client([cat]))
    db.set_clock(None)

    conn = db.connect(s.db_path)
    panel = F.build_panel(conn)
    conn.close()
    assert len(panel) == 4          # 2 intervals x 2 animals
    assert panel["event"].sum() == 0


def test_refuses_to_fit_on_too_little_data(tmp_path):
    conn = db.connect(tmp_path / "empty.db")
    db.init_db(conn)
    with pytest.raises(ValueError, match="need at least"):
        F.fit(conn)
    conn.close()


def test_model_round_trips_and_scores(sim_db, tmp_path):
    conn = db.connect(sim_db)
    model = F.fit(conn)
    rows = db.rows_to_dicts(db.fetch_active(conn))
    conn.close()

    path = F.save(model, tmp_path / "sim.db")
    assert path.name == "sim.model.json"
    again = F.load(tmp_path / "sim.db")
    assert again.coefs == model.coefs

    scored = F.score_rows(again, rows)
    assert all(0 <= s["score"] <= 100 for s in scored)
    senior = {"age": "Senior", "size": "large", "photo_count": 0, "description": ""}
    baby = {"age": "Baby", "size": "small", "photo_count": 4, "description": "x" * 500}
    assert model.still_listed_prob(senior, 10) > model.still_listed_prob(baby, 10)
    assert "senior" in F.explain(model.contributions(senior, 10))


def test_backtest_does_not_peek(sim_db):
    """Fitting with a cut-off must use only intervals that ended before it."""
    conn = db.connect(sim_db)
    cutoff = datetime(2026, 8, 1, tzinfo=timezone.utc)
    panel = F.build_panel(conn, before=cutoff)
    conn.close()
    assert (panel["next_t"] <= cutoff).all()
