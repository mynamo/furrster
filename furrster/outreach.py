"""Phase 5: turn scores into actions, and measure whether the actions work.

Three parts:

1. Listing gaps. For each listed animal, what could the shelter fix today (photos,
   write-up, missing compatibility info), and how much does the fitted model say
   fixing it would help. That's a counterfactual: rerun the model with the gap
   filled. It's only as causal as the model's coefficients, which are
   observational. So the numbers are a *prioritization* ("do Tank's photos
   before Luna's"), and section 2 is how you find out whether they're real.

2. Campaigns. Explicit records of what was done for which animal and when
   (featured post, published copy, listing refresh).

3. Effect measurement. Shelters feature the animals they're worried about, so
   comparing featured animals with everyone else shows featured animals being
   adopted *slower*, even when the feature helps. The fix is to compare each
   featured animal with untouched animals that looked equally hard to place on
   the same day: nearest neighbors on the fitted model's predicted rate. The
   simulator plants a known campaign effect to prove the matched estimate finds it
   and the naive one doesn't.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from . import db, fitting

GOOD_PHOTOS = 3
GOOD_WRITEUP = 500


# ------------------------------------------------------------------ gaps


def _tenure(row: dict[str, Any]) -> float:
    return float(row.get("days_listed") or 0)


def listing_gaps(model: fitting.FittedModel,
                 rows: list[dict[str, Any]]) -> pd.DataFrame:
    """One row per listed animal with fixable gaps.

    p_adopt_now / p_adopt_fixed are the chances of leaving the listing within 30
    days, before and after filling every *quantifiable* gap. Unknown compatibility
    is listed but not valued: filling it in could just as easily reveal a
    restriction that lowers the odds.
    """
    out = []
    for r in rows:
        tenure = _tenure(r)
        photos = int(r.get("photo_count") or 0)
        desc_len = len(r.get("description") or "")
        fixes, fixed = [], dict(r)
        if photos < 2:
            fixes.append(f"add photos ({photos} now)")
            fixed["photo_count"] = GOOD_PHOTOS
        if desc_len < 280:
            fixes.append(f"expand write-up ({desc_len} chars)")
            fixed["description"] = "x" * GOOD_WRITEUP
            fixed.pop("description_len", None)
        unknown = [label for key, label in (("good_with_children", "kids"),
                                            ("good_with_dogs", "dogs"),
                                            ("good_with_cats", "cats"))
                   if r.get(key) is None]
        if not fixes and not unknown:
            continue
        p_now = 1 - model.still_listed_prob(r, tenure)
        p_fixed = 1 - model.still_listed_prob(fixed, tenure) if fixes else p_now
        out.append({
            "animal_id": r["animal_id"],
            "name": r.get("name"),
            "organization_id": r.get("organization_id"),
            "type": r.get("type"),
            "days_listed": int(tenure),
            "fixes": "; ".join(fixes),
            "unknown_info": ", ".join(unknown),
            "p_adopt_now": p_now,
            "p_adopt_fixed": p_fixed,
            "gain": p_fixed - p_now,
        })
    df = pd.DataFrame(out)
    if df.empty:
        return df
    return df.sort_values(["gain", "days_listed"], ascending=False).reset_index(drop=True)


def gaps_by_shelter(gaps: pd.DataFrame, org_names: dict[str, str] | None = None) -> pd.DataFrame:
    """Per shelter: how many listings have each gap, and the expected extra
    adoptions within 30 days if all of them were fixed (sum of per-animal gains)."""
    if gaps.empty:
        return pd.DataFrame(columns=["shelter", "listings_with_gaps", "photo_gaps",
                                     "writeup_gaps", "unknown_info", "expected_extra_adoptions"])
    g = gaps.assign(
        photo=gaps["fixes"].str.contains("photos"),
        writeup=gaps["fixes"].str.contains("write-up"),
        unknown=gaps["unknown_info"].str.len() > 0,
    )
    agg = g.groupby("organization_id").agg(
        listings_with_gaps=("animal_id", "count"),
        photo_gaps=("photo", "sum"),
        writeup_gaps=("writeup", "sum"),
        unknown_info=("unknown", "sum"),
        expected_extra_adoptions=("gain", "sum"),
    ).reset_index()
    names = org_names or {}
    agg.insert(0, "shelter", agg["organization_id"].map(lambda o: names.get(o, o)))
    return agg.drop(columns="organization_id").sort_values(
        "expected_extra_adoptions", ascending=False).reset_index(drop=True)


# ------------------------------------------------------------- campaigns


def add_campaign(conn: sqlite3.Connection, animal_id: int, kind: str,
                 started_at: str | None = None, content_id: int | None = None,
                 note: str | None = None) -> int:
    cur = conn.execute(
        """INSERT INTO campaigns (animal_id, kind, started_at, content_id, note, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (animal_id, kind, started_at or db.utcnow(), content_id, note, db.utcnow()))
    conn.commit()
    return int(cur.lastrowid)


def list_campaigns(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        """SELECT c.*, a.name, a.type, a.is_active, a.left_listing_at
             FROM campaigns c LEFT JOIN animals a USING (animal_id)
            ORDER BY c.started_at DESC""", conn)


def animals_in_recent_campaigns(conn: sqlite3.Connection, days: int = 30) -> set[int]:
    since = (db.now_dt() - timedelta(days=days)).isoformat(timespec="seconds")
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT animal_id FROM campaigns WHERE started_at >= ?", (since,))}


# ------------------------------------------------------ effect measurement


@dataclass
class CampaignEffect:
    campaigns: int
    window_days: int
    treated_rate: float
    naive_control_rate: float
    matched_control_rate: float
    ci_low: float | None
    ci_high: float | None
    controls_per_campaign: int

    @property
    def naive_ratio(self) -> float | None:
        return self.treated_rate / self.naive_control_rate if self.naive_control_rate else None

    @property
    def matched_ratio(self) -> float | None:
        return self.treated_rate / self.matched_control_rate if self.matched_control_rate else None

    def to_dict(self) -> dict[str, Any]:
        r = lambda x: None if x is None else round(x, 2)  # noqa: E731
        return {
            "campaigns": self.campaigns,
            "window_days": self.window_days,
            "naive_rate_ratio": r(self.naive_ratio),
            "matched_rate_ratio": r(self.matched_ratio),
            "matched_ci": [r(self.ci_low), r(self.ci_high)],
            "controls_per_campaign": self.controls_per_campaign,
        }


def _state_at(conn: sqlite3.Connection, when: pd.Timestamp) -> pd.DataFrame:
    """Every animal listed at `when`, as the pull closest before it saw them."""
    run = conn.execute(
        "SELECT run_id, started_at FROM ingest_runs WHERE started_at <= ? "
        "AND status != 'failed' ORDER BY started_at DESC LIMIT 1",
        (when.isoformat(),)).fetchone()
    if run is None:
        return pd.DataFrame()
    df = pd.read_sql_query(
        """SELECT a.*, s.photo_count AS snap_photos, s.description_len AS snap_desc
             FROM animal_snapshots s JOIN animals a USING (animal_id)
            WHERE s.run_id = ?""", conn, params=(run["run_id"],))
    df["photo_count"] = df["snap_photos"]
    df["description_len"] = df["snap_desc"]
    return df


def measure_campaigns(conn: sqlite3.Connection, model: fitting.FittedModel,
                      window_days: int = 30, k: int = 5, boot: int = 400,
                      seed: int = 0) -> CampaignEffect | None:
    """Treated vs. naive controls vs. risk-matched controls, departures per animal-day.

    Only campaigns with a full `window_days` of follow-up are used. Controls are
    animals listed at the campaign start and not already in a campaign; a control
    that gets its own campaign later is censored at that point. Matched controls
    are the k nearest on log predicted rate.
    CI: bootstrap over campaigns (each keeping its matched set).
    """
    camps = pd.read_sql_query("SELECT animal_id, started_at FROM campaigns", conn)
    if camps.empty:
        return None
    horizon = conn.execute("SELECT MAX(started_at) FROM ingest_runs").fetchone()[0]
    if horizon is None:
        return None
    horizon = pd.Timestamp(horizon)
    window = pd.Timedelta(days=window_days)
    camps["t0"] = pd.to_datetime(camps["started_at"], utc=True, format="ISO8601")
    camps = camps[camps["t0"] + window <= horizon]
    if camps.empty:
        return None

    life = pd.read_sql_query("SELECT animal_id, left_listing_at FROM animals", conn)
    left = dict(zip(life["animal_id"],
                    pd.to_datetime(life["left_listing_at"], utc=True, format="ISO8601",
                                   errors="coerce")))
    touched = camps.groupby("animal_id")["t0"].apply(list).to_dict()

    def follow(aid: int, t0: pd.Timestamp, stop: pd.Timestamp | None = None) -> tuple[int, float]:
        """(departed?, animal-days observed) in [t0, t0+window], cut at `stop`."""
        t1 = t0 + window if stop is None else min(t0 + window, stop)
        end = left.get(aid)
        if end is not None and not pd.isna(end) and t0 < end <= t1:
            return 1, (end - t0) / pd.Timedelta(days=1)
        return 0, max(0.0, (t1 - t0) / pd.Timedelta(days=1))

    def own_campaign_after(aid: int, t0: pd.Timestamp) -> pd.Timestamp | None:
        later = [t for t in touched.get(aid, []) if t0 < t]
        return min(later) if later else None

    per_campaign = []   # (treated (e,d), matched [(e,d)...], naive totals (e,d))
    for c in camps.itertuples():
        state = _state_at(conn, c.t0)
        if state.empty or c.animal_id not in set(state["animal_id"]):
            continue
        state = state.copy()
        start = pd.to_datetime(
            state["first_published_at"].fillna(state["published_at"]).fillna(
                state["first_seen_at"]), utc=True, format="ISO8601", errors="coerce")
        state["tenure"] = ((c.t0 - start) / pd.Timedelta(days=1)).clip(lower=0).fillna(0)
        state["log_rate"] = [math.log(model.daily_rate(r, r["tenure"]))
                             for r in state.to_dict("records")]

        # Exclude animals already inside a campaign window at t0. Do NOT exclude
        # animals whose campaign starts later: shelters pick those *because* they
        # kept waiting, so dropping them would keep only the fast ones as controls
        # (conditioning on the future). Instead, follow them until their own
        # campaign starts and censor there.
        def clean(aid: int) -> bool:
            return not any(c.t0 - window < t <= c.t0 for t in touched.get(aid, []))

        pool = state[[a != c.animal_id and clean(a) for a in state["animal_id"]]]
        if pool.empty:
            continue
        me = state.loc[state["animal_id"] == c.animal_id, "log_rate"].iloc[0]
        nearest = pool.iloc[(pool["log_rate"] - me).abs().argsort()[:k]]

        treated = follow(c.animal_id, c.t0)
        matched = [follow(a, c.t0, own_campaign_after(a, c.t0)) for a in nearest["animal_id"]]
        naive = [follow(a, c.t0, own_campaign_after(a, c.t0)) for a in pool["animal_id"]]
        per_campaign.append((treated, matched,
                             (sum(e for e, _ in naive), sum(d for _, d in naive))))

    if not per_campaign:
        return None

    def rates(items):
        te = sum(t[0][0] for t in items); td = sum(t[0][1] for t in items)
        me = sum(e for t in items for e, _ in t[1]); md = sum(d for t in items for _, d in t[1])
        ne = sum(t[2][0] for t in items); nd = sum(t[2][1] for t in items)
        return te / td if td else 0.0, me / md if md else 0.0, ne / nd if nd else 0.0

    tr, mr, nr = rates(per_campaign)
    rng = np.random.default_rng(seed)
    ratios = []
    for _ in range(boot):
        sample = [per_campaign[i] for i in rng.integers(0, len(per_campaign), len(per_campaign))]
        t, m, _ = rates(sample)
        if m > 0 and t > 0:
            ratios.append(t / m)
    lo, hi = (np.percentile(ratios, [2.5, 97.5]) if len(ratios) > 20 else (None, None))
    return CampaignEffect(len(per_campaign), window_days, tr, nr, mr,
                          None if lo is None else float(lo),
                          None if hi is None else float(hi), k)


# ------------------------------------------------------------ weekly cycle


@dataclass
class RiskNote:
    """Adapter so bios.draft_for_animal can take a fitted-model score."""
    score: float
    band: str
    reasons: str

    def summary(self) -> str:
        return self.reasons


@dataclass
class CycleResult:
    scorer: str
    refit: bool
    picks: list[dict[str, Any]]
    drafted: int
    gaps_csv: str | None
    summary_md: str | None
    effect: dict[str, Any] | None


def pick_animals(conn: sqlite3.Connection, rows: list[dict[str, Any]],
                 model: fitting.FittedModel | None, n: int = 5) -> list[dict[str, Any]]:
    """Highest-risk listed animals not already in a campaign or waiting in review."""
    busy = animals_in_recent_campaigns(conn, days=30)
    busy |= {r[0] for r in conn.execute(
        "SELECT DISTINCT animal_id FROM generated_content WHERE review_status = 'pending'")}
    by_id = {r["animal_id"]: r for r in rows}
    if model is not None:
        scored = [(s["score"], fitting.band(s["score"]), fitting.explain(s["factors"]),
                   s["animal_id"]) for s in fitting.score_rows(model, rows)]
    else:
        from .scoring import score_population
        scored = [(s.score, s.band, s.summary(), s.animal_id) for s in score_population(rows)]
    scored.sort(key=lambda t: -t[0])
    picks = []
    for score, band_, why, aid in scored:
        if aid in busy:
            continue
        r = by_id[aid]
        picks.append({"animal_id": aid, "name": r.get("name"), "type": r.get("type"),
                      "organization_id": r.get("organization_id"),
                      "days_listed": r.get("days_listed"), "score": round(score, 1),
                      "band": band_, "why": why})
        if len(picks) >= n:
            break
    return picks


def run_cycle(settings, *, n: int = 5, draft: bool = True, refit_days: int = 7,
              reports_dir=None) -> CycleResult:
    """Weekly: refresh the model if stale, pick who to help, draft copy (if a key
    is set) into the review queue, and write the listing-gap worklist."""
    from pathlib import Path

    conn = db.connect(settings.db_path)
    db.init_db(conn)
    try:
        model = fitting.load(settings.db_path)
        refit = False
        stale = model is None or (
            db.now_dt() - datetime.fromisoformat(model.fitted_at)).days >= refit_days
        if stale:
            try:
                model = fitting.fit(conn)
                fitting.save(model, settings.db_path)
                refit = True
            except ValueError:
                pass  # not enough history yet; fall back to whatever we have
        rows = db.rows_to_dicts(db.fetch_active(conn))
        picks = pick_animals(conn, rows, model, n=n)

        drafted = 0
        if draft and settings.anthropic_api_key and picks:
            from .bios import draft_for_animal
            by_id = {r["animal_id"]: r for r in rows}
            for p in picks:
                draft_for_animal(settings, by_id[p["animal_id"]],
                                 risk=RiskNote(p["score"], p["band"], p["why"]))
                drafted += 1

        gaps_csv = summary_md = None
        effect = None
        if model is not None:
            reports = Path(reports_dir or settings.db_path.parent / "reports")
            reports.mkdir(parents=True, exist_ok=True)
            stamp = db.now_dt().date().isoformat()
            orgs = dict(conn.execute(
                "SELECT organization_id, COALESCE(name, organization_id) FROM organizations"))
            gaps = listing_gaps(model, rows)
            gaps_path = reports / f"listing_gaps_{stamp}.csv"
            gaps.assign(shelter=gaps["organization_id"].map(lambda o: orgs.get(o, o))
                        if not gaps.empty else None).to_csv(gaps_path, index=False)
            gaps_csv = str(gaps_path)
            eff = measure_campaigns(conn, model)
            effect = eff.to_dict() if eff else None
            summary_md = str(_write_summary(reports / f"outreach_{stamp}.md", stamp, picks,
                                            gaps_by_shelter(gaps, orgs), effect, model, orgs))
        return CycleResult("fitted" if model else "rules", refit, picks, drafted,
                           gaps_csv, summary_md, effect)
    finally:
        conn.close()


def _write_summary(path, stamp, picks, by_shelter, effect, model, orgs):
    lines = [f"# Furrster outreach — week of {stamp}", ""]
    if model.simulated:
        lines += ["> Simulated data. Pipeline demo only.", ""]
    lines += ["## This week's picks", "",
              "| Animal | Shelter | Days listed | Still listed in 30d | Why |",
              "|---|---|---|---|---|"]
    for p in picks:
        lines.append(f"| {p['name']} ({p['type']}) | {orgs.get(p['organization_id'], p['organization_id'])} "
                     f"| {p['days_listed']} | {p['score']:.0f}% | {p['why']} |")
    lines += ["", "## Listing gaps by shelter", "",
              "Expected extra adoptions within 30 days if every photo/write-up gap were "
              "fixed, *assuming the model's associations are causal*. Use it to decide "
              "where to spend volunteer time, and use campaign tracking to check.", "",
              "| Shelter | Listings with gaps | Photo | Write-up | Missing compatibility info | Expected extra adoptions |",
              "|---|---|---|---|---|---|"]
    for r in by_shelter.itertuples():
        lines.append(f"| {r.shelter} | {r.listings_with_gaps} | {r.photo_gaps} | "
                     f"{r.writeup_gaps} | {r.unknown_info} | {r.expected_extra_adoptions:.1f} |")
    lines += ["", "## Do campaigns work?", ""]
    if effect:
        lo, hi = effect["matched_ci"]
        lines.append(
            f"{effect['campaigns']} campaigns with 30 days of follow-up. Compared with "
            f"equally hard-to-place animals on the same day, featured animals left the "
            f"listing at **×{effect['matched_rate_ratio']}** the rate (95% CI {lo}–{hi}). "
            f"A naive comparison against everyone says ×{effect['naive_rate_ratio']}, "
            "which understates the effect, because shelters feature the animals that are hardest to place.")
    else:
        lines.append("Not enough campaigns with 30 days of follow-up yet. Record them with "
                     "`furrster.cli campaign add` or the app.")
    path.write_text("\n".join(lines) + "\n")
    return path
