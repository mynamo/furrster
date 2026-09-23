"""Smoke-test the Streamlit app headlessly against a small simulated database."""

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = str(Path(__file__).resolve().parent.parent / "app" / "streamlit_app.py")


@pytest.fixture(scope="module")
def sim_db(tmp_path_factory):
    from furrster.config import Settings
    from furrster.simulate import simulate

    path = tmp_path_factory.mktemp("app") / "app.db"
    simulate(Settings(None, None, None, "m", path, "94110", 50),
             days=75, seed=11, initial_population=60,
             end=datetime.now(timezone.utc))
    return path


def run_app(monkeypatch, db_path) -> AppTest:
    monkeypatch.setenv("FURRSTER_DB", str(db_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    return at


def test_app_renders_every_tab_without_errors(monkeypatch, sim_db):
    at = run_app(monkeypatch, sim_db)
    assert not at.exception, [e.value for e in at.exception]
    assert [t.label for t in at.tabs] == ["Overview", "At risk", "Lifecycle", "Outreach",
                                         "Match", "Review queue"]
    assert any("Simulated data" in w.value for w in at.warning)
    labels = [m.label for m in at.metric]
    assert "Listed now" in labels and "AUC · rules v1" in labels


def test_match_form_returns_shortlist_without_a_key(monkeypatch, sim_db):
    at = run_app(monkeypatch, sim_db)
    submit = [b for b in at.button if b.label == "Find matches"][0]
    submit.click().run()
    assert not at.exception
    assert any("pass the hard filters" in c.value for c in at.caption)
    # With no API key the rule-based ranker runs and says so.
    assert any("baseline-rules" in c.value for c in at.caption)
    assert any("Suggestions and what came of them" in h.value for h in at.subheader)


def test_review_queue_approve(monkeypatch, sim_db):
    at = run_app(monkeypatch, sim_db)
    approve = [b for b in at.button if b.label == "✓ Approve"]
    assert approve, "simulator should seed demo drafts"
    before = len(approve)
    approve[0].click().run()
    assert not at.exception
    assert len([b for b in at.button if b.label == "✓ Approve"]) == before - 1


def test_empty_database_shows_guidance(monkeypatch, tmp_path):
    at = run_app(monkeypatch, tmp_path / "empty.db")
    assert not at.exception
    assert any("No animals in the database yet" in i.value for i in at.info)


def test_app_switches_to_fitted_model_when_one_exists(monkeypatch, sim_db):
    from furrster import db, fitting

    conn = db.connect(sim_db)
    fitting.save(fitting.fit(conn), sim_db)
    conn.close()
    try:
        at = run_app(monkeypatch, sim_db)
        assert not at.exception, [e.value for e in at.exception]
        assert "Fitted model" in at.radio[0].options
        assert any("What slows adoption down" in h.value for h in at.subheader)
        assert "AUC · fitted v2" in [m.label for m in at.metric]
        at.radio[0].set_value("Rules (v1)").run()
        assert not at.exception
    finally:
        fitting.model_path(sim_db).unlink(missing_ok=True)


def test_outreach_tab_and_publish_flow(monkeypatch, sim_db):
    from furrster import db, fitting

    conn = db.connect(sim_db)
    fitting.save(fitting.fit(conn), sim_db)
    conn.close()
    try:
        at = run_app(monkeypatch, sim_db)
        assert not at.exception, [e.value for e in at.exception]
        subs = [h.value for h in at.subheader]
        assert "This week's picks" in subs and "Listing gaps" in subs
        start = [b for b in at.button if b.label == "Start campaign"]
        assert start
        start[0].click().run()
        assert not at.exception

        # approve a draft, then publish it -> becomes a tracked campaign
        [b for b in at.button if b.label == "✓ Approve"][0].click().run()
        review = [r for r in at.radio if r.label == "Show"][0]
        review.set_value("approved").run()
        pub = [b for b in at.button if b.label == "📣 Mark as published"]
        assert pub
        pub[0].click().run()
        assert not at.exception
        conn = db.connect(sim_db)
        n = conn.execute("SELECT COUNT(*) FROM campaigns WHERE kind = 'copy'").fetchone()[0]
        conn.close()
        assert n == 1
    finally:
        fitting.model_path(sim_db).unlink(missing_ok=True)
