"""Phase 3b: learn the risk weights from what actually happened.

The v1 scorer's weights were informed guesses. This module replaces guesses with
estimates, without giving up explainability.

Model
-----
A Poisson regression on an animal-day panel, which is the discrete-time form of a
piecewise-exponential survival model:

    departures_it ~ Poisson( exposure_it * exp(b0 + b . x_it) )

One row per animal per observed interval between two pulls. `x` holds
the same kinds of factors the v1 scorer uses (age, size, restrictions, photos,
write-up, tags) plus log tenure, so the data can say whether time on the
listing itself matters. exp(b_k) is a *rate ratio*: "seniors leave the listing at
0.45x the rate of otherwise-similar adults". Those ratios are what a shelter
coordinator can read and argue with.

Fit by penalized Newton-Raphson (a small ridge keeps rare factors from running
off to infinity). With ~15 parameters and a few tens of thousands of rows this
takes milliseconds; no statsmodels/scipy dependency.

Scoring
-------
Fitted score = probability the animal is still listed 30 days from now,
exp(-30 * predicted daily rate), on a 0-100 scale. Same direction as v1 (higher
= more at risk), but now it is a calibrated quantity rather than a points total.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from . import db

HORIZON_DAYS = 30
MIN_EVENTS = 60          # below this, refuse to fit — the CIs would be meaningless
RIDGE = 1.0              # penalty on non-intercept coefficients

# (name, human label). Reference levels are noted in the labels; the reference
# category for each group is simply the one without a column.
FEATURES: list[tuple[str, str]] = [
    ("age_baby", "baby (vs. adult)"),
    ("age_young", "young (vs. adult)"),
    ("age_senior", "senior (vs. adult)"),
    ("size_small", "small (vs. medium)"),
    ("size_large", "large (vs. medium)"),
    ("size_xlarge", "extra large (vs. medium)"),
    ("is_cat", "cat (vs. dog)"),
    ("special_needs", "special needs"),
    ("no_kids", "not OK with children"),
    ("only_pet", "needs to be the only pet"),
    ("photos_0", "no photos (vs. 2+)"),
    ("photos_1", "one photo (vs. 2+)"),
    ("thin_writeup", "write-up under 280 characters"),
    ("hard_tag", "tagged shy / timid / needs experience"),
    ("in_campaign", "in an outreach campaign (first 30 days)"),
    ("log_tenure", "log(1 + days listed)"),
]
CAMPAIGN_DAYS = 30
FEATURE_NAMES = [f for f, _ in FEATURES]
LABELS = dict(FEATURES)

HARD_TAGS = {"shy", "timid", "needs experienced owner"}


# --------------------------------------------------------------- features


def _tags(value: Any) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        return {str(t).strip().lower() for t in value}
    try:
        return {str(t).strip().lower() for t in json.loads(value or "[]")}
    except (TypeError, ValueError):
        return set()


def feature_vector(row: dict[str, Any], tenure_days: float) -> np.ndarray:
    """Map one animal (as of some moment) to the model's columns."""
    age = (row.get("age") or "").lower()
    size = (row.get("size") or "").lower()
    photos = int(row.get("photo_count") or 0)
    if row.get("description_len") is not None:
        desc_len = int(row.get("description_len") or 0)
    else:
        desc_len = len(row.get("description") or "")
    f = {
        "age_baby": age == "baby",
        "age_young": age == "young",
        "age_senior": age == "senior",
        "size_small": size == "small",
        "size_large": size == "large",
        "size_xlarge": size == "xlarge",
        "is_cat": (row.get("type") or "").lower() == "cat",
        "special_needs": row.get("special_needs") == 1,
        "no_kids": row.get("good_with_children") == 0,
        "only_pet": row.get("good_with_dogs") == 0 and row.get("good_with_cats") == 0,
        "photos_0": photos == 0,
        "photos_1": photos == 1,
        "thin_writeup": desc_len < 280,
        "hard_tag": bool(_tags(row.get("tags_json")) & HARD_TAGS),
        # Scores are "without outreach" by default: the counterfactual that matters
        # for deciding who to help. The panel sets this for campaign periods, so the
        # campaign effect is estimated instead of silently inflating other factors.
        "in_campaign": bool(row.get("in_campaign")),
        "log_tenure": np.log1p(max(0.0, float(tenure_days))),
    }
    return np.array([float(f[name]) for name in FEATURE_NAMES])


# ------------------------------------------------------------------ panel


def _ts(col: pd.Series) -> pd.Series:
    return pd.to_datetime(col, utc=True, errors="coerce", format="ISO8601")


def build_panel(conn: sqlite3.Connection, before: datetime | None = None) -> pd.DataFrame:
    """Animal-interval panel: one row per snapshot that has a following pull.

    event = 1 if the animal was missing from the NEXT pull of the same slice.
    Runs are chained per slice (the `type` they pulled), because a dog-only run
    followed by a cat-only run must not make every dog look adopted.

    `before`: only use intervals that ended on or before this instant. This is
    what keeps the backtest honest — the model can't learn from the future.
    """
    runs = pd.read_sql_query(
        "SELECT run_id, started_at, params_json FROM ingest_runs "
        "WHERE status != 'failed' ORDER BY started_at, run_id", conn)
    if runs.empty:
        return pd.DataFrame()
    runs["t"] = _ts(runs["started_at"])
    runs["slice"] = runs["params_json"].map(
        lambda p: str((json.loads(p or "{}") or {}).get("type") or "*").lower())
    runs["next_run"] = runs.groupby("slice")["run_id"].shift(-1)
    runs["next_t"] = runs.groupby("slice")["t"].shift(-1)
    runs = runs.dropna(subset=["next_run"])
    if before is not None:
        runs = runs[runs["next_t"] <= pd.Timestamp(before)]
    if runs.empty:
        return pd.DataFrame()

    snaps = pd.read_sql_query(
        "SELECT run_id, animal_id, observed_at, photo_count, description_len "
        "FROM animal_snapshots", conn)
    panel = snaps.merge(runs[["run_id", "next_run", "t", "next_t"]], on="run_id")
    if panel.empty:
        return pd.DataFrame()

    present_next = set(zip(snaps["run_id"], snaps["animal_id"]))
    panel["event"] = [
        0 if (int(nr), aid) in present_next else 1
        for nr, aid in zip(panel["next_run"], panel["animal_id"])
    ]
    panel["exposure"] = (panel["next_t"] - panel["t"]) / pd.Timedelta(days=1)
    panel = panel[panel["exposure"] > 0]

    animals = pd.read_sql_query(
        "SELECT animal_id, type, age, size, special_needs, good_with_children, "
        "good_with_dogs, good_with_cats, tags_json, first_published_at, published_at, "
        "first_seen_at FROM animals", conn)
    panel = panel.merge(animals, on="animal_id")
    start = pd.concat([
        _ts(panel["first_published_at"]).fillna(_ts(panel["published_at"]))
        .fillna(_ts(panel["first_seen_at"])),
        _ts(panel["first_seen_at"]),
    ], axis=1).min(axis=1)
    panel["tenure_days"] = ((panel["t"] - start) / pd.Timedelta(days=1)).clip(lower=0)

    panel["in_campaign"] = False
    has_campaigns = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='campaigns'").fetchone()
    if has_campaigns:
        camps = pd.read_sql_query("SELECT animal_id, started_at FROM campaigns", conn)
        if not camps.empty:
            camps["c0"] = _ts(camps["started_at"])
            window = pd.Timedelta(days=CAMPAIGN_DAYS)
            m = panel[["animal_id", "t"]].reset_index().merge(camps, on="animal_id")
            hit = m[(m["c0"] <= m["t"]) & (m["t"] < m["c0"] + window)]["index"].unique()
            panel.loc[hit, "in_campaign"] = True
    return panel.reset_index(drop=True)


def design_matrix(panel: pd.DataFrame) -> np.ndarray:
    rows = panel.to_dict("records")
    return np.vstack([feature_vector(r, r["tenure_days"]) for r in rows])


# -------------------------------------------------------------------- fit


@dataclass
class FittedModel:
    intercept: float
    coefs: dict[str, float]
    std_errors: dict[str, float]
    n_rows: int
    n_events: int
    n_animals: int
    fitted_at: str
    data_through: str | None = None
    simulated: bool = False
    notes: list[str] = field(default_factory=list)
    feature_counts: dict[str, int] = field(default_factory=dict)

    # ---- prediction

    def daily_rate(self, row: dict[str, Any], tenure_days: float) -> float:
        x = feature_vector(row, tenure_days)
        b = np.array([self.coefs[n] for n in FEATURE_NAMES])
        return float(np.exp(self.intercept + x @ b))

    def still_listed_prob(self, row: dict[str, Any], tenure_days: float,
                          horizon: int = HORIZON_DAYS) -> float:
        """P(still listed `horizon` days from now), integrating tenure forward."""
        steps = np.arange(horizon) + tenure_days
        rates = [self.daily_rate(row, t) for t in steps[:: max(1, horizon // 6)]]
        mean_rate = float(np.mean(rates))
        return float(np.exp(-horizon * mean_rate))

    def contributions(self, row: dict[str, Any], tenure_days: float) -> list[dict[str, Any]]:
        """Which factors move THIS animal's rate, as rate ratios, biggest effect first."""
        x = feature_vector(row, tenure_days)
        out = []
        for name, value in zip(FEATURE_NAMES, x):
            if value == 0 or name == "in_campaign":
                continue
            ratio = float(np.exp(self.coefs[name] * value))
            if abs(np.log(ratio)) < 0.05:
                continue
            out.append({"name": name, "label": LABELS[name], "rate_ratio": ratio})
        return sorted(out, key=lambda c: c["rate_ratio"])

    # ---- reporting

    def table(self) -> pd.DataFrame:
        rows = []
        for name in FEATURE_NAMES:
            b, se = self.coefs[name], self.std_errors[name]
            rows.append({
                "factor": LABELS[name],
                "key": name,
                "animal_days": self.feature_counts.get(name),
                "rate_ratio": float(np.exp(b)),
                "ci_low": float(np.exp(b - 1.96 * se)),
                "ci_high": float(np.exp(b + 1.96 * se)),
            })
        return pd.DataFrame(rows)

    # ---- persistence

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "FittedModel":
        return cls(**json.loads(text))


def _newton_poisson(X: np.ndarray, y: np.ndarray, offset: np.ndarray,
                    ridge: float = RIDGE, iters: int = 50) -> tuple[np.ndarray, np.ndarray]:
    """Penalized Poisson GLM. Returns (beta incl. intercept, covariance)."""
    Xi = np.hstack([np.ones((X.shape[0], 1)), X])
    p = Xi.shape[1]
    penalty = np.full(p, ridge)
    penalty[0] = 0.0
    beta = np.zeros(p)
    beta[0] = np.log(max(y.sum(), 1) / np.exp(offset).sum())
    for _ in range(iters):
        eta = np.clip(Xi @ beta + offset, -30, 30)
        mu = np.exp(eta)
        grad = Xi.T @ (y - mu) - penalty * beta
        hess = (Xi * mu[:, None]).T @ Xi + np.diag(penalty)
        step = np.linalg.solve(hess, grad)
        beta += step
        if np.max(np.abs(step)) < 1e-8:
            break
    mu = np.exp(np.clip(Xi @ beta + offset, -30, 30))
    hess = (Xi * mu[:, None]).T @ Xi + np.diag(penalty)
    return beta, np.linalg.inv(hess)


def fit(conn: sqlite3.Connection, before: datetime | None = None) -> FittedModel:
    panel = build_panel(conn, before=before)
    events = int(panel["event"].sum()) if not panel.empty else 0
    if events < MIN_EVENTS:
        raise ValueError(
            f"Only {events} departures observed{' before the cut-off' if before else ''}; "
            f"need at least {MIN_EVENTS} to fit. Keep the daily pull running.")

    X = design_matrix(panel)
    y = panel["event"].to_numpy(dtype=float)
    offset = np.log(panel["exposure"].to_numpy(dtype=float))
    beta, cov = _newton_poisson(X, y, offset)
    se = np.sqrt(np.diag(cov))

    notes = []
    counts = {name: int((col != 0).sum()) for name, col in zip(FEATURE_NAMES, X.T)}
    for name, col in zip(FEATURE_NAMES, X.T):
        if name == "in_campaign" and col.sum() == 0:
            continue  # no campaigns recorded yet: nothing to estimate, nothing to warn about
        if name != "log_tenure" and col.sum() < 20:
            notes.append(f"'{LABELS[name]}' appears in only {int(col.sum())} animal-days; "
                         "its estimate is mostly the ridge prior.")
    simulated = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='sim_ground_truth'").fetchone())

    return FittedModel(
        intercept=float(beta[0]),
        coefs={n: float(b) for n, b in zip(FEATURE_NAMES, beta[1:])},
        std_errors={n: float(s) for n, s in zip(FEATURE_NAMES, se[1:])},
        n_rows=int(len(panel)),
        n_events=events,
        n_animals=int(panel["animal_id"].nunique()),
        fitted_at=db.utcnow(),
        data_through=None if before is None else before.isoformat(),
        simulated=simulated,
        notes=notes,
        feature_counts=counts,
    )


# -------------------------------------------------------------- persistence


def model_path(db_path: Path | str) -> Path:
    """The model lives next to the database it was fit on: data/x.db -> data/x.model.json."""
    p = Path(db_path)
    return p.with_name(p.stem + ".model.json")


def save(model: FittedModel, db_path: Path | str) -> Path:
    path = model_path(db_path)
    path.write_text(model.to_json())
    return path


def load(db_path: Path | str) -> FittedModel | None:
    path = model_path(db_path)
    if not path.exists():
        return None
    try:
        return FittedModel.from_json(path.read_text())
    except (ValueError, TypeError, KeyError):
        return None


# --------------------------------------------------------------- scoring v2


def score_rows(model: FittedModel, rows: Iterable[dict[str, Any]],
               now: datetime | None = None) -> list[dict[str, Any]]:
    """v2 scores: 100 x P(still listed in 30 days), plus the factors behind it."""
    now = now or datetime.now(timezone.utc)
    out = []
    for r in rows:
        if r.get("days_listed") is not None:
            tenure = float(r["days_listed"])
        else:
            stamp = r.get("listing_started_at") or r.get("published_at") or r.get("first_seen_at")
            try:
                start = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                tenure = max(0.0, (now - start).total_seconds() / 86400)
            except (TypeError, ValueError):
                tenure = 0.0
        p = model.still_listed_prob(r, tenure)
        out.append({
            "animal_id": r["animal_id"],
            "score": 100 * p,
            "daily_rate": model.daily_rate(r, tenure),
            "factors": model.contributions(r, tenure),
        })
    return out


def explain(contribs: list[dict[str, Any]], limit: int = 3) -> str:
    slow = [c for c in contribs if c["rate_ratio"] < 1][:limit]
    if not slow:
        return "no slowing factors"
    return "; ".join(f"{c['label'].split(' (')[0]} ×{c['rate_ratio']:.2f}" for c in slow)


def band(score: float) -> str:
    """Bands for the v2 score (chance, in %, of still being listed in 30 days).

    Cut-offs are absolute probabilities, so a band means the same thing week to week:
    critical = at least a 4-in-5 chance the animal is still waiting a month from now.
    On the simulated data this flags about 30% of listed animals as critical or
    elevated, a queue small enough to act on. Revisit once real data is in.
    """
    if score >= 80:
        return "critical"
    if score >= 70:
        return "elevated"
    if score >= 55:
        return "watch"
    return "ok"
