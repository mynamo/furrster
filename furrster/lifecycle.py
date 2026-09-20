"""Lifecycle analytics (Phase 3): how long animals actually stay listed, and why.

This is the retention-analysis half of the project. Three pieces:

1. Spells. One row per animal: when its listing started, when we started watching
   it, and when (if ever) it left. Built from the snapshot history, so it is
   immune to the relist problem.

2. Kaplan-Meier survival with *delayed entry*. Animals still listed are
   right-censored (we don't know when they'll go). Animals that were already
   listed when collection began are left-truncated: we only learned about them at
   day N of their listing, so they must not count as "at risk" before day N.
   Ignoring that is the classic mistake here: it inflates survival, because the
   animals you catch mid-listing are disproportionately the ones that stay.

3. Checks on the rest of the system:
   - listing_edit_effect: animals whose listings were improved vs. comparable
     animals at the same tenure that weren't touched.
   - evaluate_scorer: rewind to a past date, score everyone listed then, and see
     whether high scores actually predicted who was still waiting N days later.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from . import db
from .scoring import score_population

# ---------------------------------------------------------------------- spells


def load_spells(conn: sqlite3.Connection, now: datetime | None = None) -> pd.DataFrame:
    """One row per animal ever seen, in days since its listing started.

    columns: animal_id, name, type, size, age, organization_id, cohort,
             entry_day   (tenure when we first saw it; >0 means left-truncated),
             exit_day    (tenure at departure, or today if still listed),
             event       (1 = left the listing, 0 = still listed / censored)
    """
    now = now or db.now_dt()
    df = pd.read_sql_query(
        """SELECT animal_id, name, type, size, age, organization_id, special_needs,
                  photo_count, first_published_at, published_at, first_seen_at,
                  left_listing_at, is_active
             FROM animals""",
        conn,
    )
    if df.empty:
        return df

    def ts(col: pd.Series) -> pd.Series:
        return pd.to_datetime(col, utc=True, errors="coerce", format="ISO8601")

    first_pub = ts(df["first_published_at"]).fillna(ts(df["published_at"]))
    seen = ts(df["first_seen_at"])
    start = pd.concat([first_pub.fillna(seen), seen], axis=1).min(axis=1)
    left = ts(df["left_listing_at"])
    end = left.where(df["is_active"] == 0, pd.Timestamp(now))

    day = pd.Timedelta(days=1)
    df["entry_day"] = ((seen - start) / day).clip(lower=0)
    df["exit_day"] = ((end - start) / day).clip(lower=0)
    df["event"] = (df["is_active"] == 0).astype(int)
    df["size"] = df["size"].fillna("unknown").str.lower()
    df["type"] = df["type"].fillna("unknown")
    df["age"] = df["age"].fillna("unknown")
    df["cohort"] = df["type"].str.lower() + "/" + np.where(
        df["size"].isin(["large", "xlarge"]), "big", "small"
    )
    # An animal that appeared and vanished between two pulls has no usable spell.
    return df[df["exit_day"] > df["entry_day"]].reset_index(drop=True)


# -------------------------------------------------------------- Kaplan-Meier


def kaplan_meier(spells: pd.DataFrame) -> pd.DataFrame:
    """Product-limit estimator with delayed entry (left truncation).

    Risk set at time t = animals with entry_day < t <= exit_day.
    Returns columns: day, at_risk, departures, survival (share still listed).
    """
    if spells.empty:
        return pd.DataFrame(columns=["day", "at_risk", "departures", "survival"])

    entry = spells["entry_day"].to_numpy(dtype=float)
    exit_ = spells["exit_day"].to_numpy(dtype=float)
    event = spells["event"].to_numpy().astype(bool)

    times = np.unique(exit_[event])
    # at_risk[j] = #{i : entry_i < t_j <= exit_i}, vectorised over all event times.
    at_risk = ((entry[None, :] < times[:, None]) & (exit_[None, :] >= times[:, None])).sum(1)
    departures = (event[None, :] & (exit_[None, :] == times[:, None])).sum(1)
    keep = at_risk > 0
    times, at_risk, departures = times[keep], at_risk[keep], departures[keep]
    survival = np.cumprod(1.0 - departures / at_risk)

    out = pd.DataFrame({"day": times, "at_risk": at_risk,
                        "departures": departures, "survival": survival})
    start = pd.DataFrame({"day": [0.0], "at_risk": [int((entry <= 0).sum())],
                          "departures": [0], "survival": [1.0]})
    return pd.concat([start, out], ignore_index=True)


def median_days(km: pd.DataFrame) -> float | None:
    """First day survival drops to 50% or below; None if it never does (yet)."""
    hit = km[km["survival"] <= 0.5]
    return None if hit.empty else float(hit["day"].iloc[0])


def survival_at(km: pd.DataFrame, day: float) -> float:
    before = km[km["day"] <= day]
    return float(before["survival"].iloc[-1]) if not before.empty else 1.0


def km_by(spells: pd.DataFrame, column: str, min_n: int = 15) -> dict[str, pd.DataFrame]:
    """Separate curves per group, skipping groups too small to draw honestly."""
    return {
        str(key): kaplan_meier(group)
        for key, group in spells.groupby(column)
        if len(group) >= min_n
    }


def summarize(spells: pd.DataFrame, column: str, min_n: int = 15) -> pd.DataFrame:
    rows = []
    for key, km in km_by(spells, column, min_n).items():
        g = spells[spells[column].astype(str) == key]
        rows.append({
            column: key,
            "animals": len(g),
            "departed": int(g["event"].sum()),
            "median_days": None if (m := median_days(km)) is None else round(m, 1),
            "still_listed_at_30d": round(survival_at(km, 30), 3),
            "still_listed_at_60d": round(survival_at(km, 60), 3),
        })
    return pd.DataFrame(rows).sort_values("still_listed_at_30d", ascending=False)


# -------------------------------------------------------- listing edit effect


@dataclass
class EditEffect:
    edited_animals: int
    edited_departures: int
    edited_animal_days: float
    control_departures: int
    control_animal_days: float
    window_days: int

    @property
    def edited_rate(self) -> float:
        return self.edited_departures / self.edited_animal_days if self.edited_animal_days else 0.0

    @property
    def control_rate(self) -> float:
        return self.control_departures / self.control_animal_days if self.control_animal_days else 0.0

    @property
    def rate_ratio(self) -> float | None:
        return self.edited_rate / self.control_rate if self.control_rate else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "edited_animals": self.edited_animals,
            "window_days": self.window_days,
            "edited_daily_departure_rate": round(self.edited_rate, 4),
            "control_daily_departure_rate": round(self.control_rate, 4),
            "rate_ratio": None if self.rate_ratio is None else round(self.rate_ratio, 2),
        }


def listing_edit_effect(conn: sqlite3.Connection, window_days: int = 30) -> EditEffect:
    """Did adding photos to a listing coincide with faster departure?

    For each animal whose photo count went UP between two snapshots, open a window
    at the edit. Controls: every never-edited animal that was listed at that same
    moment, followed over the same window. Compare departures per animal-day.

    This is observational. Shelters don't pick listings to improve at random,
    so read the ratio as "worth a real test", not as a causal effect.
    """
    snaps = pd.read_sql_query(
        """SELECT animal_id, observed_at, photo_count
             FROM animal_snapshots ORDER BY animal_id, observed_at""", conn)
    if snaps.empty:
        return EditEffect(0, 0, 0.0, 0, 0.0, window_days)
    snaps["observed_at"] = pd.to_datetime(snaps["observed_at"], utc=True, format="ISO8601")
    snaps["prev"] = snaps.groupby("animal_id")["photo_count"].shift()
    edits = snaps[snaps["photo_count"] > snaps["prev"]].groupby("animal_id")["observed_at"].min()

    life = pd.read_sql_query(
        "SELECT animal_id, first_seen_at, left_listing_at, is_active FROM animals", conn)
    life["first_seen_at"] = pd.to_datetime(life["first_seen_at"], utc=True, format="ISO8601")
    life["left"] = pd.to_datetime(life["left_listing_at"], utc=True, format="ISO8601",
                                  errors="coerce")
    horizon = snaps["observed_at"].max()
    life["end"] = life["left"].fillna(horizon)
    life = life.set_index("animal_id")
    controls = life[~life.index.isin(edits.index)]
    window = pd.Timedelta(days=window_days)
    day = pd.Timedelta(days=1)

    e_dep = c_dep = 0
    e_days = c_days = 0.0
    for animal_id, t0 in edits.items():
        if animal_id not in life.index:
            continue
        t1 = min(t0 + window, horizon)
        if t1 <= t0:
            continue
        row = life.loc[animal_id]
        stop = min(row["end"], t1)
        e_days += max(0.0, (stop - t0) / day)
        e_dep += int(pd.notna(row["left"]) and t0 < row["left"] <= t1)

        live = controls[(controls["first_seen_at"] <= t0) & (controls["end"] > t0)]
        stop_c = live["end"].clip(upper=t1)
        c_days += float(((stop_c - t0) / day).clip(lower=0).sum())
        c_dep += int((live["left"].notna() & (live["left"] > t0) & (live["left"] <= t1)).sum())

    return EditEffect(len(edits), e_dep, e_days, c_dep, c_days, window_days)


# ------------------------------------------------------------ scorer backtest


def _concordance(scores: np.ndarray, stayed: np.ndarray) -> float | None:
    """AUC: P(a random animal that stayed outscored a random one that left)."""
    pos, neg = scores[stayed], scores[~stayed]
    if len(pos) == 0 or len(neg) == 0:
        return None
    ranks = pd.Series(np.concatenate([pos, neg])).rank().to_numpy()
    return float((ranks[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2)
                 / (len(pos) * len(neg)))


def evaluate_scorer(
    conn: sqlite3.Connection,
    *,
    as_of_days_ago: int = 30,
    horizon_days: int = 30,
) -> dict[str, Any]:
    """Backtest: rewind, score, and check who was still waiting `horizon_days` later.

    Uses each animal's snapshot from the as-of date (photo count, description
    length) and its tenure *on that date*, so the score only sees what was knowable
    then. Static attributes (age, size, restrictions) come from the current row;
    they rarely change.
    """
    runs = pd.read_sql_query(
        "SELECT run_id, started_at FROM ingest_runs WHERE status != 'failed' ORDER BY run_id",
        conn)
    if runs.empty:
        return {"error": "no ingest history yet"}
    runs["started_at"] = pd.to_datetime(runs["started_at"], utc=True, format="ISO8601")
    latest = runs["started_at"].max()
    target = latest - timedelta(days=as_of_days_ago)
    past = runs[runs["started_at"] <= target]
    if past.empty:
        return {"error": f"need at least {as_of_days_ago} days of history"}
    run = past.iloc[-1]
    as_of = run["started_at"].to_pydatetime()
    horizon = as_of + timedelta(days=horizon_days)

    rows = pd.read_sql_query(
        """SELECT a.*, s.photo_count AS snap_photos, s.description_len AS snap_desc_len
             FROM animal_snapshots s JOIN animals a USING (animal_id)
            WHERE s.run_id = ?""",
        conn, params=(int(run["run_id"]),),
    )
    if rows.empty:
        return {"error": "no animals in the as-of snapshot"}

    records = []
    for r in rows.to_dict("records"):
        start = min(filter(None, [r.get("first_published_at"), r.get("published_at"),
                                  r.get("first_seen_at")]))
        r = dict(r)
        r.pop("days_listed", None)
        r["published_at"] = start
        r["photo_count"] = r["snap_photos"]
        r["description"] = "x" * int(r["snap_desc_len"] or 0)
        r["description_len"] = int(r["snap_desc_len"] or 0)
        records.append(r)

    scored = {s.animal_id: s for s in score_population(records, now=as_of)}
    left = pd.to_datetime(rows["left_listing_at"], utc=True, format="ISO8601", errors="coerce")
    stayed = ~(left.notna() & (left <= pd.Timestamp(horizon))).to_numpy()
    scores = np.array([scored[i].score for i in rows["animal_id"]])
    bands = [scored[i].band for i in rows["animal_id"]]

    by_band = (pd.DataFrame({"band": bands, "stayed": stayed})
               .groupby("band")["stayed"].agg(["count", "mean"])
               .rename(columns={"count": "animals", "mean": "share_still_listed"})
               .reindex(["critical", "elevated", "watch", "ok"]).dropna()
               .round(3).reset_index().to_dict("records"))

    # v2: fit ONLY on intervals that ended by the as-of date, then score the same
    # animals. Anything else would let the model learn the answers it is graded on.
    from . import fitting

    auc_fitted, fitted_note = None, None
    try:
        model = fitting.fit(conn, before=as_of)
        v2 = {s["animal_id"]: s["score"] for s in fitting.score_rows(model, records, now=as_of)}
        v2_scores = np.array([v2[i] for i in rows["animal_id"]])
        auc_fitted = _concordance(v2_scores, stayed)
    except ValueError as exc:
        fitted_note = str(exc)

    result: dict[str, Any] = {
        "as_of": as_of.date().isoformat(),
        "auc_fitted": None if auc_fitted is None else round(auc_fitted, 3),
        "fitted_note": fitted_note,
        "horizon_days": horizon_days,
        "animals_scored": len(rows),
        "still_listed_after_horizon": int(stayed.sum()),
        "auc": None if (auc := _concordance(scores, stayed)) is None else round(auc, 3),
        "by_band": by_band,
    }

    # Simulated databases know the true adoption hazard; real ones never will.
    has_truth = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'sim_ground_truth'").fetchone()
    if has_truth:
        truth = pd.read_sql_query("SELECT animal_id, daily_hazard FROM sim_ground_truth", conn)
        merged = pd.DataFrame({"animal_id": rows["animal_id"], "score": scores}).merge(
            truth, on="animal_id")
        if len(merged) > 5:
            # Higher risk score should mean LOWER true hazard -> negative correlation.
            # Spearman = Pearson on ranks; done by hand to avoid a scipy dependency.
            rho = merged["score"].rank().corr(merged["daily_hazard"].rank())
            result["spearman_vs_true_hazard"] = round(float(rho), 3)
            # The ceiling: a scorer that knew every animal's true hazard. Adoption is
            # random even for a known hazard, so this is well short of 1.0, and it
            # is the number the scorer's AUC should be read against.
            hz = dict(zip(truth["animal_id"], truth["daily_hazard"]))
            oracle = -np.array([hz.get(i, np.nan) for i in rows["animal_id"]])
            ok = ~np.isnan(oracle)
            ceiling = _concordance(oracle[ok], stayed[ok])
            result["oracle_auc"] = None if ceiling is None else round(ceiling, 3)
    return result
