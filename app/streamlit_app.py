"""Furrster dashboard.  Run:  python -m furrster.cli app   (or: streamlit run app/streamlit_app.py)

Five tabs, one per job:
  Overview    - where things stand today
  At risk     - who needs help, and why (the scorer, with its reasons)
  Lifecycle   - how long animals actually stay, by segment
  Match       - adopter intake -> SQL shortlist -> optional Claude ranking
  Review      - human approval of every piece of generated copy
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from furrster import db, fitting, lifecycle as L, outreach as O  # noqa: E402
from furrster.config import load_settings  # noqa: E402
from furrster.matcher import AdopterProfile, rank as rank_matches, shortlist  # noqa: E402
from furrster.scoring import score_population  # noqa: E402

# Reference palette (dataviz skill): categorical slots in fixed order, status reserved.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
BAND_COLOR = {"critical": "#d03b3b", "elevated": "#ec835a", "watch": "#fab219",
              "ok": "#0ca30c"}
BAND_ICON = {"critical": "🔴", "elevated": "🟠", "watch": "🟡", "ok": "🟢"}
BAND_ORDER = ["critical", "elevated", "watch", "ok"]
INK_MUTED, GRID = "#898781", "#e1e0d9"

st.set_page_config(page_title="Furrster", page_icon="🐾", layout="wide")
settings = load_settings()


# ---------------------------------------------------------------- data access


def _conn():
    conn = db.connect(settings.db_path)
    db.init_db(conn)
    return conn


@st.cache_data(ttl=60, show_spinner=False)
def load_active(db_path: str, model_version: float = 0.0) -> pd.DataFrame:
    conn = _conn()
    rows = db.rows_to_dicts(db.fetch_active(conn))
    conn.close()
    if not rows:
        return pd.DataFrame()
    scored = {s.animal_id: s for s in score_population(rows)}
    df = pd.DataFrame(rows)
    df["risk_score"] = df["animal_id"].map(lambda i: round(scored[i].score, 1))
    df["band"] = df["animal_id"].map(lambda i: scored[i].band)
    df["why"] = df["animal_id"].map(lambda i: scored[i].summary())
    df["factors"] = df["animal_id"].map(lambda i: scored[i].to_dict()["factors"])

    model = fitting.load(db_path)
    if model is not None:
        v2 = {s["animal_id"]: s for s in fitting.score_rows(model, rows)}
        df["v2_score"] = df["animal_id"].map(lambda i: round(v2[i]["score"], 1))
        df["v2_band"] = df["v2_score"].map(fitting.band)
        df["v2_why"] = df["animal_id"].map(lambda i: fitting.explain(v2[i]["factors"]))
        df["v2_factors"] = df["animal_id"].map(lambda i: v2[i]["factors"])
    return df.sort_values("risk_score", ascending=False)


@st.cache_data(ttl=300, show_spinner=False)
def load_model_table(db_path: str, model_version: float = 0.0) -> tuple[pd.DataFrame, dict] | None:
    model = fitting.load(db_path)
    if model is None:
        return None
    meta = {"events": model.n_events, "animals": model.n_animals, "rows": model.n_rows,
            "fitted_at": model.fitted_at, "simulated": model.simulated, "notes": model.notes}
    return model.table(), meta


@st.cache_data(ttl=60, show_spinner=False)
def load_spells(db_path: str) -> pd.DataFrame:
    conn = _conn()
    df = L.load_spells(conn)
    conn.close()
    return df


@st.cache_data(ttl=60, show_spinner=False)
def load_meta(db_path: str) -> dict:
    conn = _conn()
    q = lambda sql: conn.execute(sql).fetchone()  # noqa: E731
    meta = {
        "last_run": q("SELECT MAX(started_at) FROM ingest_runs WHERE status != 'failed'")[0],
        "runs": q("SELECT COUNT(*) FROM ingest_runs")[0],
        "simulated": bool(q("SELECT COUNT(*) FROM sqlite_master "
                            "WHERE name = 'sim_ground_truth'")[0]),
        "departed_30d": q("SELECT COUNT(*) FROM animals WHERE is_active = 0 "
                          "AND left_listing_at >= datetime('now', '-30 days')")[0],
        "orgs": dict(conn.execute("SELECT organization_id, COALESCE(name, organization_id) "
                                  "FROM organizations").fetchall()),
    }
    conn.close()
    return meta


@st.cache_data(ttl=300, show_spinner=False)
def load_backtest(db_path: str, days_back: int) -> dict:
    conn = _conn()
    out = L.evaluate_scorer(conn, as_of_days_ago=days_back, horizon_days=30)
    conn.close()
    return out


@st.cache_data(ttl=300, show_spinner=False)
def load_edit_effect(db_path: str) -> dict:
    conn = _conn()
    out = L.listing_edit_effect(conn).to_dict()
    conn.close()
    return out


@st.cache_data(ttl=300, show_spinner=False)
def load_outreach(db_path: str, model_version: float) -> dict | None:
    model = fitting.load(db_path)
    if model is None:
        return None
    conn = _conn()
    rows = db.rows_to_dicts(db.fetch_active(conn))
    orgs = dict(conn.execute(
        "SELECT organization_id, COALESCE(name, organization_id) FROM organizations"))
    gaps = O.listing_gaps(model, rows)
    eff = O.measure_campaigns(conn, model)
    camps = O.list_campaigns(conn)
    picks = O.pick_animals(conn, rows, model, n=5)
    conn.close()
    return {"gaps": gaps, "by_shelter": O.gaps_by_shelter(gaps, orgs),
            "effect": eff.to_dict() if eff else None, "campaigns": camps,
            "picks": picks, "orgs": orgs}


def calibration_chart(cal: list[dict]) -> alt.Chart:
    df = pd.DataFrame(cal)
    diag = alt.Chart(pd.DataFrame({"x": [0, 1], "y": [0, 1]})).mark_line(
        color=INK_MUTED, strokeDash=[3, 3], strokeWidth=1).encode(x="x:Q", y="y:Q")
    axis = dict(format="%", gridColor=GRID, domain=False, tickCount=5)
    pts = alt.Chart(df).mark_point(filled=True, size=110, color=SERIES[0],
                                   stroke="#fcfcfb", strokeWidth=2).encode(
        x=alt.X("predicted:Q", title="Predicted share still listed after 30 days",
                scale=alt.Scale(domain=[0, 1]), axis=alt.Axis(**axis)),
        y=alt.Y("observed:Q", title="Observed", scale=alt.Scale(domain=[0, 1]),
                axis=alt.Axis(**axis)),
        tooltip=[alt.Tooltip("group:O", title="Quintile"),
                 alt.Tooltip("animals:Q", title="Animals"),
                 alt.Tooltip("predicted:Q", title="Predicted", format=".0%"),
                 alt.Tooltip("observed:Q", title="Observed", format=".0%")])
    line = alt.Chart(df).mark_line(color=SERIES[0], strokeWidth=2).encode(
        x="predicted:Q", y="observed:Q")
    return (diag + line + pts).properties(height=300)


def refresh():
    st.cache_data.clear()


# --------------------------------------------------------------------- charts


def survival_chart(curves: dict[str, pd.DataFrame], max_day: int = 120) -> alt.Chart:
    frames = []
    for name, km in curves.items():
        k = km[km["day"] <= max_day].copy()
        # Extend each curve flat to the right edge so step lines end together.
        k = pd.concat([k, pd.DataFrame({"day": [float(max_day)],
                                        "survival": [k["survival"].iloc[-1]],
                                        "at_risk": [None], "departures": [0]})])
        k["segment"] = name
        frames.append(k)
    data = pd.concat(frames, ignore_index=True)
    names = list(curves)
    color = alt.Color("segment:N", title=None,
                      scale=alt.Scale(domain=names, range=SERIES[: len(names)]),
                      legend=alt.Legend(orient="top", symbolType="stroke"))
    base = alt.Chart(data).encode(
        x=alt.X("day:Q", title="Days since listing started",
                scale=alt.Scale(domain=[0, max_day], nice=False),
                axis=alt.Axis(grid=False, tickColor=GRID, domainColor=GRID)),
        y=alt.Y("survival:Q", title="Share still listed",
                scale=alt.Scale(domain=[0, 1]),
                axis=alt.Axis(format="%", gridColor=GRID, domain=False, tickCount=5)),
        color=color,
    )
    lines = base.mark_line(interpolate="step-after", strokeWidth=2)
    hover = alt.selection_point(fields=["day"], nearest=True, on="pointerover",
                                empty=False, clear="pointerout")
    points = base.mark_point(size=60, filled=True, opacity=0).add_params(hover).encode(
        opacity=alt.condition(hover, alt.value(1), alt.value(0)),
        tooltip=[alt.Tooltip("segment:N", title="Segment"),
                 alt.Tooltip("day:Q", title="Day", format=".0f"),
                 alt.Tooltip("survival:Q", title="Still listed", format=".0%"),
                 alt.Tooltip("at_risk:Q", title="Animals at risk")])
    rule = alt.Chart(data).mark_rule(color=INK_MUTED, strokeWidth=1).encode(
        x="day:Q").transform_filter(hover)
    half = alt.Chart(pd.DataFrame({"y": [0.5]})).mark_rule(
        color=INK_MUTED, strokeDash=[3, 3], strokeWidth=1).encode(y="y:Q")
    return (lines + half + rule + points).properties(height=340)


def factor_chart(factors: list[dict]) -> alt.Chart:
    df = pd.DataFrame(factors)
    return alt.Chart(df).mark_bar(color=SERIES[0], cornerRadiusEnd=4, height=14).encode(
        x=alt.X("points:Q", title="Points toward risk score",
                axis=alt.Axis(gridColor=GRID, domain=False)),
        y=alt.Y("detail:N", sort="-x", title=None, axis=alt.Axis(labelLimit=320)),
        tooltip=[alt.Tooltip("detail:N", title="Factor"),
                 alt.Tooltip("points:Q", title="Points", format=".1f")],
    ).properties(height=max(90, 30 * len(df)))


def rate_ratio_chart(table: pd.DataFrame) -> alt.Chart:
    """Dot + 95% CI on a log scale; 1.0 = no effect. Single series, so no legend."""
    t = table[table["key"] != "log_tenure"].copy()
    t["direction"] = np.where(t["ci_high"] < 1, "slower",
                              np.where(t["ci_low"] > 1, "faster", "unclear"))
    order = t.sort_values("rate_ratio")["factor"].tolist()
    x = alt.X("rate_ratio:Q", title="Rate of leaving the listing (×, log scale)",
              scale=alt.Scale(type="log", domain=[0.15, 4]),
              axis=alt.Axis(values=[0.25, 0.5, 1, 2, 4], format="~g", gridColor=GRID,
                            domain=False))
    y = alt.Y("factor:N", sort=order, title=None, axis=alt.Axis(labelLimit=320))
    base = alt.Chart(t).encode(y=y)
    ci = base.mark_rule(color=SERIES[0], strokeWidth=2).encode(
        x=alt.X("ci_low:Q", scale=alt.Scale(type="log", domain=[0.15, 4])), x2="ci_high:Q")
    dots = base.mark_point(filled=True, size=90, color=SERIES[0],
                           stroke="#fcfcfb", strokeWidth=2).encode(
        x=x,
        tooltip=[alt.Tooltip("factor:N", title="Factor"),
                 alt.Tooltip("rate_ratio:Q", title="Rate ratio", format=".2f"),
                 alt.Tooltip("ci_low:Q", title="95% CI low", format=".2f"),
                 alt.Tooltip("ci_high:Q", title="95% CI high", format=".2f"),
                 alt.Tooltip("direction:N", title="Reads as")])
    one = alt.Chart(pd.DataFrame({"x": [1.0]})).mark_rule(
        color=INK_MUTED, strokeDash=[3, 3]).encode(x=alt.X("x:Q", scale=alt.Scale(type="log")))
    return (one + ci + dots).properties(height=alt.Step(26))


def band_label(band: str) -> str:
    return f"{BAND_ICON[band]} {band}"


# --------------------------------------------------------------------- header

DB = str(settings.db_path)
# Part of the cache key, so running `fit` shows up without waiting for the TTL.
_mp = fitting.model_path(DB)
MODEL_V = _mp.stat().st_mtime if _mp.exists() else 0.0
meta = load_meta(DB)
active = load_active(DB, MODEL_V)

left, right = st.columns([4, 1])
with left:
    st.title("🐾 Furrster")
    st.caption("Shelter lifecycle analytics · at-risk flagging · adopter matching")
with right:
    st.write("")
    if st.button("↻ Refresh data", width="stretch"):
        refresh()
        st.rerun()

if meta["simulated"]:
    st.warning(
        "**Simulated data.** This database was generated by `furrster.cli simulate`. "
        "The shelters, animals and outcomes are synthetic — useful for building and "
        "validating the pipeline, not for conclusions about real shelters.",
        icon="🧪",
    )

if active.empty:
    st.info(
        "No animals in the database yet. Run `python -m furrster.cli ingest` with a "
        "Petfinder key, or `python -m furrster.cli simulate` to try the app with "
        "synthetic history."
    )
    st.stop()

tab_overview, tab_risk, tab_life, tab_outreach, tab_match, tab_review = st.tabs(
    ["Overview", "At risk", "Lifecycle", "Outreach", "Match", "Review queue"]
)

# ------------------------------------------------------------------- overview

with tab_overview:
    spells = load_spells(DB)
    km_all = L.kaplan_meier(spells) if not spells.empty else None
    med = L.median_days(km_all) if km_all is not None else None
    urgent = int(active["band"].isin(["critical", "elevated"]).sum())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Listed now", f"{len(active):,}")
    c2.metric("Left the listing, last 30 days", f"{meta['departed_30d']:,}")
    c3.metric("Median days on the listing", "—" if med is None else f"{med:.0f}")
    c4.metric("Critical or elevated risk", f"{urgent:,}",
              help="Animals the scorer flags for extra outreach")

    st.caption(
        f"Last pull {meta['last_run'] or 'never'} · {meta['runs']} pulls recorded · "
        "median uses Kaplan–Meier with delayed entry, so animals that were already "
        "listed when collection began don't inflate it."
    )

    st.subheader("Who's listed, by risk band")
    counts = active.groupby(["type", "band"]).size().rename("animals").reset_index()
    counts["band_rank"] = counts["band"].map(BAND_ORDER.index)
    bars = alt.Chart(counts).mark_bar(cornerRadius=2, stroke="#fcfcfb", strokeWidth=2).encode(
        y=alt.Y("type:N", title=None, axis=alt.Axis(domain=False, ticks=False)),
        x=alt.X("sum(animals):Q", title="Animals listed", stack="zero",
                axis=alt.Axis(gridColor=GRID, domain=False, tickCount=6)),
        color=alt.Color("band:N", title=None, sort=BAND_ORDER,
                        scale=alt.Scale(domain=BAND_ORDER,
                                        range=[BAND_COLOR[b] for b in BAND_ORDER]),
                        legend=alt.Legend(orient="top")),
        order=alt.Order("band_rank:Q"),
        tooltip=[alt.Tooltip("type:N", title="Type"), alt.Tooltip("band:N", title="Band"),
                 alt.Tooltip("animals:Q", title="Animals")],
    ).properties(height=alt.Step(36))
    st.altair_chart(bars, width="stretch")
    st.caption("Bands are also written out in the At risk table — colour is never the only cue.")

# -------------------------------------------------------------------- at risk

with tab_risk:
    has_v2 = "v2_score" in active.columns
    scorer = st.radio(
        "Scorer", ["Fitted model", "Rules (v1)"] if has_v2 else ["Rules (v1)"],
        horizontal=True,
        help="Fitted: % chance the animal is still listed in 30 days, learned from "
             "observed departures (`furrster.cli fit`). Rules: hand-set points, 0–100.")
    fitted = scorer == "Fitted model"
    if not has_v2:
        st.caption("Run `python -m furrster.cli fit` once there's enough history to "
                   "switch to the fitted model.")
    if fitted:
        active = active.assign(risk_score=active["v2_score"], band=active["v2_band"],
                               why=active["v2_why"])
    f1, f2, f3 = st.columns([1, 2, 1])
    types = sorted(active["type"].dropna().unique())
    pick_types = f1.multiselect("Type", types, default=types)
    pick_bands = f2.multiselect("Band", BAND_ORDER, default=["critical", "elevated"],
                                format_func=band_label)
    org_names = meta["orgs"]
    orgs = sorted(active["organization_id"].dropna().unique())
    pick_orgs = f3.multiselect("Shelter", orgs, default=orgs,
                               format_func=lambda o: org_names.get(o, o))

    view = active[active["type"].isin(pick_types) & active["band"].isin(pick_bands)
                  & active["organization_id"].isin(pick_orgs)]
    st.caption(f"{len(view)} animals match")

    table = view.assign(
        band=view["band"].map(band_label),
        shelter=view["organization_id"].map(lambda o: org_names.get(o, o)),
        size=view["size"].fillna("—"),
    )[["animal_id", "name", "band", "risk_score", "days_listed", "type", "age",
       "size", "photo_count", "shelter", "why", "url"]]
    event = st.dataframe(
        table, hide_index=True, width="stretch", height=380,
        on_select="rerun", selection_mode="single-row",
        column_config={
            "animal_id": None,
            "risk_score": st.column_config.ProgressColumn(
                "Still listed in 30d" if fitted else "Risk", min_value=0, max_value=100,
                format="%d%%" if fitted else "%d"),
            "days_listed": st.column_config.NumberColumn("Days", format="%d"),
            "photo_count": st.column_config.NumberColumn("Photos"),
            "name": "Name", "band": "Band", "type": "Type", "age": "Age",
            "size": "Size", "shelter": "Shelter",
            "why": st.column_config.TextColumn("Top reasons", width="large"),
            "url": st.column_config.LinkColumn("Listing", display_text="open"),
        },
    )

    picked = event.selection.rows if event and event.selection else []
    if picked:
        row = view.iloc[picked[0]]
        st.divider()
        a, b = st.columns([1, 2])
        with a:
            if isinstance(row.get("primary_photo"), str) and row["primary_photo"].startswith("http") \
                    and not meta["simulated"]:
                st.image(row["primary_photo"], width="stretch")
            st.markdown(f"### {row['name']}")
            st.markdown(
                f"{band_label(row['band'])} · **{row['risk_score']:.0f}"
                f"{'% still listed in 30d' if fitted else '/100'}** · "
                f"{int(row['days_listed'])} days listed"
                + (" · relisted" if row.get("relisted") else "")
            )
            st.markdown(f"{row['age'] or '?'} {row['size'] or ''} {row['type']}"
                        f" — {row.get('breed_primary') or 'breed unknown'}")
            known = {"kids": row.get("good_with_children"), "dogs": row.get("good_with_dogs"),
                     "cats": row.get("good_with_cats")}
            st.markdown("Good with: " + ", ".join(
                f"{k} {'✓' if v == 1 else '✗' if v == 0 else '?'}" for k, v in known.items()))
        with b:
            st.markdown("**Why it scored this way**")
            if fitted:
                contrib = pd.DataFrame(row["v2_factors"])
                if contrib.empty:
                    st.caption("Nothing about this animal slows adoption in the model.")
                else:
                    contrib = contrib.rename(columns={"label": "factor"}).assign(
                        ci_low=lambda d: d["rate_ratio"], ci_high=lambda d: d["rate_ratio"],
                        key=lambda d: d["name"])
                    st.altair_chart(rate_ratio_chart(contrib), width="stretch")
                    st.caption("Each dot: how much this factor changes the rate at which "
                               "similar animals leave the listing. Left of 1 = slower.")
            else:
                st.altair_chart(factor_chart(row["factors"]), width="stretch")
            if settings.anthropic_api_key:
                if st.button("✍️ Draft bio & social copy", key=f"draft-{row['animal_id']}"):
                    from furrster.bios import draft_for_animal
                    from furrster.scoring import score_population as sp
                    risk = next(s for s in sp(active.to_dict("records"))
                                if s.animal_id == row["animal_id"])
                    with st.spinner("Drafting…"):
                        draft_for_animal(settings, row.to_dict(), risk=risk)
                    refresh()
                    st.success("Draft added to the Review queue.")
            else:
                st.caption("Add ANTHROPIC_API_KEY to .env to draft copy from here.")

# ------------------------------------------------------------------ lifecycle

with tab_life:
    spells = load_spells(DB)
    if spells.empty or spells["event"].sum() == 0:
        st.info("Lifecycle curves need animals to have left the listing. Keep the "
                "daily ingest running — a couple of weeks is enough to start.")
    else:
        dims = {"Species × size": "cohort", "Age": "age", "Species": "type",
                "Shelter": "organization_id"}
        c1, c2 = st.columns([2, 1])
        dim_label = c1.radio("Split by", list(dims), horizontal=True)
        horizon = c2.slider("Show first N days", 30, 180, 120, step=10)
        col = dims[dim_label]
        sp = spells.copy()
        if col == "organization_id":
            sp[col] = sp[col].map(lambda o: meta["orgs"].get(o, o))
        curves = L.km_by(sp, col, min_n=15)
        # Hold the colour order stable: sort segments by name, cap at six.
        curves = dict(sorted(curves.items())[:6])
        st.altair_chart(survival_chart(curves, horizon), width="stretch")
        st.caption(
            "Step lines are Kaplan–Meier estimates: the share of animals still listed "
            "N days after their listing started. Dashed line = 50% (the median). "
            "Segments with fewer than 15 animals are hidden."
        )
        st.dataframe(
            L.summarize(sp, col, min_n=15), hide_index=True, width="stretch",
            column_config={
                col: "Segment", "animals": "Animals", "departed": "Left the listing",
                "median_days": st.column_config.NumberColumn("Median days", format="%.0f"),
                "still_listed_at_30d": st.column_config.NumberColumn(
                    "Still listed at 30 days", format="percent"),
                "still_listed_at_60d": st.column_config.NumberColumn(
                    "Still listed at 60 days", format="percent"),
            })

        st.subheader("What slows adoption down")
        fitted_tbl = load_model_table(DB, MODEL_V)
        if fitted_tbl is None:
            st.info("Run `python -m furrster.cli fit` to estimate these from the data.")
        else:
            tbl, fm = fitted_tbl
            st.altair_chart(rate_ratio_chart(tbl), width="stretch")
            tenure = tbl.set_index("key").loc["log_tenure"]
            st.caption(
                f"Poisson rate model on {fm['rows']:,} animal-days, {fm['events']} "
                f"departures. Lines are 95% intervals; a line crossing 1 means the data "
                f"can't tell. Time on the listing itself: ×{tenure['rate_ratio']:.2f} per "
                f"log-day (95% CI {tenure['ci_low']:.2f}–{tenure['ci_high']:.2f})."
                + (" Simulated data: this recovers the simulator's assumptions."
                   if fm["simulated"] else ""))
            for note in fm["notes"]:
                st.caption(f"⚠️ {note}")

        st.subheader("Do listing improvements help?")
        eff = load_edit_effect(DB)
        e1, e2, e3 = st.columns(3)
        e1.metric("Listings that gained photos", eff["edited_animals"])
        e2.metric("Daily departure rate, next 30 days",
                  f"{eff['edited_daily_departure_rate']:.1%}",
                  help="Edited animals, from the day of the edit")
        e3.metric("Untouched animals, same window",
                  f"{eff['control_daily_departure_rate']:.1%}",
                  delta=None if eff["rate_ratio"] is None
                  else f"×{eff['rate_ratio']} for edited", delta_color="off")
        st.caption("Observational: shelters choose which listings to improve. Treat a "
                   "ratio above 1 as a reason to run a proper test, not as proof.")

        st.subheader("Is the risk score any good?")
        back = st.select_slider("Score everyone as of … days ago, check 30 days later",
                                options=[30, 45, 60], value=45)
        bt = load_backtest(DB, back)
        if "error" in bt:
            st.info(bt["error"])
        else:
            b1, b2, b3 = st.columns(3)
            b1.metric("AUC · rules v1", bt["auc"],
                      help="0.5 = coin flip. Probability a randomly chosen animal that "
                           "was still waiting outscored one that had left.")
            b2.metric("AUC · fitted v2", bt["auc_fitted"] or "—",
                      help="Fitted only on data from before the as-of date, so it "
                           "never sees the outcomes it's graded on.")
            if "oracle_auc" in bt:
                b3.metric("Reference · true adoption rates", bt["oracle_auc"],
                          help="Only on simulated data, where each animal's true adoption "
                               "odds (including any campaign boost) are known. It's the best "
                               "score on average; on one sample another score can edge "
                               "past it by chance. Adoption is random even with perfect "
                               "knowledge, so this is well below 1.0.")
            if bt.get("fitted_note"):
                st.caption(f"Fitted v2: {bt['fitted_note']}")
            if bt.get("calibration"):
                st.markdown("**Calibration of the fitted score**")
                st.altair_chart(calibration_chart(bt["calibration"]), width="stretch")
                st.caption(
                    "Each dot is a fifth of the animals, grouped by predicted chance of still "
                    "waiting. On the dashed line, \"70%\" really means 7 in 10. Dots below "
                    "the line at the high end are expected when outreach works: scores assume "
                    "no outreach, and shelters give the hardest cases the most help.")
            st.caption("Share still listed 30 days later, by rules-v1 band:")
            st.dataframe(pd.DataFrame(bt["by_band"]).assign(
                band=lambda d: d["band"].map(band_label)),
                hide_index=True, width="stretch",
                column_config={"share_still_listed": st.column_config.NumberColumn(
                    "Still listed after 30 days", format="percent")})

# ------------------------------------------------------------------- outreach

with tab_outreach:
    data = load_outreach(DB, MODEL_V)
    if data is None:
        st.info("Outreach planning uses the fitted model. Run "
                "`python -m furrster.cli fit` once there's enough history.")
    else:
        orgs = data["orgs"]
        st.subheader("This week's picks")
        st.caption("Highest chance of still waiting in 30 days, not already in a campaign "
                   "or awaiting review. `furrster.cli outreach-cycle` does this weekly and "
                   "drafts copy for them.")
        for p in data["picks"]:
            with st.container(border=True):
                c1, c2 = st.columns([5, 1])
                c1.markdown(
                    f"{band_label(p['band'])} **{p['name']}** · {p['type']} · "
                    f"{orgs.get(p['organization_id'], p['organization_id'])} · "
                    f"{p['days_listed']} days · **{p['score']:.0f}%** still listed in 30d  \n"
                    f"<span style='color:#52514e'>{p['why']}</span>", unsafe_allow_html=True)
                if c2.button("Start campaign", key=f"camp-{p['animal_id']}"):
                    conn = _conn()
                    O.add_campaign(conn, p["animal_id"], "feature", note="started from app")
                    conn.close()
                    refresh()
                    st.rerun()

        st.subheader("Listing gaps")
        bs = data["by_shelter"]
        if bs.empty:
            st.success("No fixable gaps on current listings.")
        else:
            bar = alt.Chart(bs).mark_bar(color=SERIES[0], cornerRadiusEnd=4).encode(
                y=alt.Y("shelter:N", sort="-x", title=None),
                x=alt.X("expected_extra_adoptions:Q",
                        title="Expected extra adoptions in 30 days if gaps were fixed",
                        axis=alt.Axis(gridColor=GRID, domain=False)),
                tooltip=[alt.Tooltip("shelter:N", title="Shelter"),
                         alt.Tooltip("expected_extra_adoptions:Q", title="Expected extra",
                                     format=".1f"),
                         alt.Tooltip("photo_gaps:Q", title="Photo gaps"),
                         alt.Tooltip("writeup_gaps:Q", title="Write-up gaps")],
            ).properties(height=alt.Step(34))
            st.altair_chart(bar, width="stretch")
            st.caption("If the model's associations are causal. It's a way to decide where "
                       "volunteer time goes first; campaign tracking below is how to check. "
                       "Missing compatibility info is counted but not valued: filling it in "
                       "can reveal a restriction as easily as remove a doubt.")
            gaps = data["gaps"].assign(
                shelter=lambda d: d["organization_id"].map(lambda o: orgs.get(o, o)))
            pick = st.multiselect("Shelter", sorted(gaps["shelter"].unique()),
                                  default=sorted(gaps["shelter"].unique()), key="gap-shelter")
            view = gaps[gaps["shelter"].isin(pick)]
            st.dataframe(
                view[["name", "shelter", "days_listed", "fixes", "unknown_info",
                      "p_adopt_now", "p_adopt_fixed"]],
                hide_index=True, width="stretch", height=320,
                column_config={
                    "name": "Name", "shelter": "Shelter", "days_listed": "Days",
                    "fixes": st.column_config.TextColumn("Fix", width="large"),
                    "unknown_info": "Unknown: good with…",
                    "p_adopt_now": st.column_config.NumberColumn(
                        "Adopted in 30d, now", format="percent"),
                    "p_adopt_fixed": st.column_config.NumberColumn(
                        "…if fixed", format="percent"),
                })

        st.subheader("Do campaigns work?")
        eff = data["effect"]
        if eff is None:
            st.info("Needs campaigns with 30 days of follow-up. Start them above, from the "
                    "Review queue (Mark as published), or `furrster.cli campaign add`.")
        else:
            m1, m2, m3 = st.columns(3)
            m1.metric("Campaigns measured", eff["campaigns"])
            m2.metric("vs. equally hard-to-place animals", f"×{eff['matched_rate_ratio']}",
                      help=f"95% CI {eff['matched_ci'][0]}–{eff['matched_ci'][1]}. "
                           "Rate of leaving the listing over 30 days, featured vs. the "
                           f"{eff['controls_per_campaign']} most similar untouched animals "
                           "on the same day.")
            m3.metric("vs. everyone (naive)", f"×{eff['naive_rate_ratio']}",
                      help="Understates the effect: shelters feature the hardest cases.")
            st.caption(f"95% interval for the matched estimate: "
                       f"{eff['matched_ci'][0]}–{eff['matched_ci'][1]}.")
        camps = data["campaigns"]
        if not camps.empty:
            with st.expander(f"All campaigns ({len(camps)})"):
                st.dataframe(camps[["name", "type", "kind", "started_at", "note"]],
                             hide_index=True, width="stretch")

# ---------------------------------------------------------------------- match

with tab_match:
    st.markdown("Describe the adopter in their own words. Hard constraints are applied "
                "in SQL first; Claude then ranks what's left and names a concern for "
                "every pick.")
    with st.form("match"):
        desc = st.text_area(
            "Adopter's situation",
            "Second-floor apartment, no yard. I work from home three days a week and "
            "run most mornings. First dog of my own. No kids, no other pets.",
            height=110)
        m1, m2, m3, m4 = st.columns(4)
        kind = m1.selectbox("Looking for", ["dog", "cat", "any"])
        max_size = m2.selectbox("Largest size", ["xlarge", "large", "medium", "small"],
                                index=1)
        experience = m3.selectbox("Experience", ["first-time", "some", "experienced"])
        home = m4.selectbox("Home", ["apartment", "house-no-yard", "house-yard"])
        h1, h2, h3, h4 = st.columns(4)
        kids = h1.checkbox("Children at home")
        dogs = h2.checkbox("Resident dog(s)")
        cats = h3.checkbox("Resident cat(s)")
        activity = h4.selectbox("Activity", ["moderate", "low", "high"])
        use_llm = st.checkbox("Rank with Claude", value=bool(settings.anthropic_api_key),
                              disabled=not settings.anthropic_api_key,
                              help="Unticked, the rule-based ranker is used — no API key "
                                   "needed, and it's the bar Claude has to clear."
                              if settings.anthropic_api_key
                              else "Add ANTHROPIC_API_KEY to .env to enable. Until then "
                                   "the rule-based ranker is used.")
        go = st.form_submit_button("Find matches", type="primary")

    if go:
        profile = AdopterProfile(
            description=desc, animal_type=None if kind == "any" else kind,
            has_children=kids, has_dogs=dogs, has_cats=cats, max_size=max_size,
            experience=experience, home=home, activity_level=activity)
        pool = shortlist(settings, profile, limit=25)
        st.caption(f"{len(pool)} animals pass the hard filters "
                   "(unknown compatibility is kept, not excluded).")
        if pool:
            with st.spinner("Ranking…"):
                result = rank_matches(settings, profile, pool, use_llm=use_llm)
            st.caption(f"Ranked by **{result.get('model', 'baseline-rules')}**.")
            by_id = {r["animal_id"]: r for r in pool}
            for i, m in enumerate(result.get("matches", []), start=1):
                animal = by_id.get(m.get("animal_id"), {})
                with st.container(border=True):
                    st.markdown(f"**{i}. {animal.get('name', m.get('animal_id'))}** · "
                                f"fit {m.get('fit_score')}/100 · "
                                f"{animal.get('age', '')} {animal.get('size', '')} "
                                f"{animal.get('breed_primary', '')}")
                    st.write(m.get("rationale"))
                    st.markdown(f"⚠️ **Concern:** {m.get('concerns')}")
                    for q in m.get("questions_to_ask", []):
                        st.markdown(f"- Ask the shelter: {q}")
            if result.get("notes"):
                st.info(result["notes"])
        else:
            st.warning("Nothing in the database fits those hard constraints.")

    st.divider()
    st.subheader("Suggestions and what came of them")
    st.caption("A ranker is only as good as what happened next. Record outcomes here; "
               "`furrster.cli feedback summary` compares rankers once there are enough.")
    conn = _conn()
    recent = db.recent_matches(conn, limit=15)
    summary = db.outcome_summary(conn)
    conn.close()
    if summary:
        st.dataframe(pd.DataFrame(summary), hide_index=True, width="stretch",
                     column_config={"model": "Ranker", "suggestions": "Suggestions",
                                    "with_outcome": "With an outcome",
                                    "met_or_adopted": "Met or adopted", "adopted": "Adopted"})
    if not recent:
        st.info("No suggestions saved yet — run a match above.")
    else:
        labels = {"forwarded": "Sent to adopter", "met": "Meet-and-greet",
                  "adopted": "Adopted", "declined_adopter": "Adopter passed",
                  "declined_shelter": "Shelter passed"}
        for m in recent:
            with st.container(border=True):
                c1, c2 = st.columns([3, 2])
                c1.markdown(
                    f"**{m['animal_name'] or m['animal_id']}** · fit {m['fit_score']} · "
                    f"{(m['model'] or '?')} · {m['created_at'][:10]}  \n"
                    f"<span style='color:#52514e'>{(m['adopter'] or '')[:90]}</span>",
                    unsafe_allow_html=True)
                current = m["outcome"]
                if current:
                    c2.markdown(f"Outcome: **{labels.get(current, current)}**")
                else:
                    choice = c2.selectbox("Outcome", ["—"] + list(labels),
                                          format_func=lambda k: labels.get(k, k),
                                          key=f"oc-{m['match_id']}",
                                          label_visibility="collapsed")
                    if choice != "—":
                        conn = _conn()
                        db.record_outcome(conn, m["match_id"], choice)
                        conn.close()
                        st.rerun()

# --------------------------------------------------------------------- review

with tab_review:
    st.markdown("Nothing generated here goes out without a person approving it. "
                "Edit the text in place, then approve or reject.")
    status = st.radio("Show", ["pending", "approved", "rejected"], horizontal=True)
    conn = _conn()
    items = db.list_generated(conn, status)
    conn.close()
    if not items:
        st.info("Nothing here. Drafts appear after `python -m furrster.cli draft` or the "
                "Draft button on the At risk tab.")
    for item in items:
        with st.container(border=True):
            head = (f"**{item.get('animal_name') or item['animal_id']}** · {item['kind']}"
                    + (f" · {item['channel']}" if item.get("channel") else "")
                    + f" · {item.get('model') or '?'} · {item.get('prompt_version') or ''}")
            st.markdown(head)
            key = f"body-{item['content_id']}"
            body = st.text_area("Text", item["body"], key=key, label_visibility="collapsed",
                                height=140 if item["kind"] == "bio" else 90,
                                disabled=status != "pending")
            if status == "pending":
                note = st.text_input("Note (optional)", key=f"note-{item['content_id']}")
                c1, c2, _ = st.columns([1, 1, 4])
                if c1.button("✓ Approve", key=f"ok-{item['content_id']}", type="primary"):
                    conn = _conn()
                    db.set_review(conn, item["content_id"], "approved", note or None,
                                  body=body if body != item["body"] else None)
                    conn.close()
                    st.rerun()
                if c2.button("✗ Reject", key=f"no-{item['content_id']}"):
                    conn = _conn()
                    db.set_review(conn, item["content_id"], "rejected", note or None)
                    conn.close()
                    st.rerun()
            else:
                if item.get("review_note"):
                    st.caption(f"Note: {item['review_note']} · {item.get('reviewed_at')}")
                if status == "approved":
                    conn = _conn()
                    done = conn.execute("SELECT 1 FROM campaigns WHERE content_id = ?",
                                        (item["content_id"],)).fetchone()
                    conn.close()
                    if done:
                        st.caption("📣 Published — tracked as a campaign.")
                    elif st.button("📣 Mark as published", key=f"pub-{item['content_id']}"):
                        conn = _conn()
                        O.add_campaign(conn, item["animal_id"], "copy",
                                       content_id=item["content_id"],
                                       note=f"{item['kind']} {item.get('channel') or ''}".strip())
                        conn.close()
                        refresh()
                        st.rerun()
