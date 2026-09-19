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
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from furrster import db, lifecycle as L  # noqa: E402
from furrster.config import load_settings  # noqa: E402
from furrster.matcher import AdopterProfile, shortlist  # noqa: E402
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
def load_active(db_path: str) -> pd.DataFrame:
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
    return df.sort_values("risk_score", ascending=False)


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


def band_label(band: str) -> str:
    return f"{BAND_ICON[band]} {band}"


# --------------------------------------------------------------------- header

DB = str(settings.db_path)
meta = load_meta(DB)
active = load_active(DB)

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

tab_overview, tab_risk, tab_life, tab_match, tab_review = st.tabs(
    ["Overview", "At risk", "Lifecycle", "Match", "Review queue"]
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
                "Risk", min_value=0, max_value=100, format="%d"),
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
                f"{band_label(row['band'])} · **{row['risk_score']:.0f}/100** · "
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
            b1.metric("AUC (still listed after 30 days)", bt["auc"],
                      help="0.5 = coin flip. Probability a randomly chosen animal that "
                           "was still waiting outscored one that had left.")
            if "oracle_auc" in bt:
                b2.metric("Ceiling: true-hazard oracle", bt["oracle_auc"],
                          help="Only available on simulated data, where the true "
                               "adoption odds are known. Adoption is random even with "
                               "perfect knowledge, so this is well below 1.0.")
                b3.metric("Rank correlation with true hazard",
                          bt["spearman_vs_true_hazard"],
                          help="Negative is good: higher risk score ↔ lower true "
                               "chance of adoption.")
            st.dataframe(pd.DataFrame(bt["by_band"]).assign(
                band=lambda d: d["band"].map(band_label)),
                hide_index=True, width="stretch",
                column_config={"share_still_listed": st.column_config.NumberColumn(
                    "Still listed after 30 days", format="percent")})

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
                              help=None if settings.anthropic_api_key
                              else "Add ANTHROPIC_API_KEY to .env to enable")
        go = st.form_submit_button("Find matches", type="primary")

    if go:
        profile = AdopterProfile(
            description=desc, animal_type=None if kind == "any" else kind,
            has_children=kids, has_dogs=dogs, has_cats=cats, max_size=max_size,
            experience=experience, home=home, activity_level=activity)
        pool = shortlist(settings, profile, limit=25)
        st.caption(f"{len(pool)} animals pass the hard filters "
                   "(unknown compatibility is kept, not excluded).")
        if use_llm and pool:
            from furrster.matcher import match
            with st.spinner("Ranking with Claude…"):
                result = match(settings, profile, candidates=pool)
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
        elif pool:
            st.dataframe(pd.DataFrame(pool)[
                ["name", "type", "age", "size", "breed_primary", "days_listed",
                 "good_with_children", "good_with_dogs", "good_with_cats"]],
                hide_index=True, width="stretch")

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
            elif item.get("review_note"):
                st.caption(f"Note: {item['review_note']} · {item.get('reviewed_at')}")
