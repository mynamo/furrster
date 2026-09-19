"""At-risk / hard-to-place scoring.

This is deliberately a transparent rules-and-weights model, not a black box:
every animal gets a 0-100 score plus the list of factors that produced it, so a
shelter coordinator can argue with the output. Two families of signal:

  TENURE   - how long this listing has been up, measured against its own cohort
             (same species + same rough size class), not against a global average.
  FRICTION - documented attributes that the adoption literature and shelter
             practice associate with longer stays: age, size, special needs,
             restrictions on children / other pets, and thin listings
             (few photos, short or missing description).

Weights live in one dict at the top so they are easy to tune, and the whole thing
is testable without an API key or an LLM.

Caveat worth keeping in the README: "days listed" is measured from `published_at`,
which resets if a shelter relists an animal. Treat it as listing tenure, not time
in the shelter's care.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

WEIGHTS: dict[str, float] = {
    "tenure_percentile": 35.0,   # how long vs. its cohort
    "tenure_absolute": 15.0,     # long is long, even in a slow cohort
    "age_senior": 10.0,
    "age_adult": 4.0,
    "size_large": 6.0,
    "special_needs": 10.0,
    "no_kids": 5.0,
    "no_other_pets": 5.0,
    "thin_listing": 8.0,
    "no_photos": 6.0,
    "bonded_or_pair": 3.0,
}

# Tenure in days at which "absolute" tenure risk saturates.
LONG_STAY_DAYS = 180
THIN_DESCRIPTION_CHARS = 280

# A cohort smaller than this cannot support a percentile: with two large dogs in
# the database, the older one has "outlasted 50% of its peers" and the number is
# noise. Below the floor we back off to the species, then to everything.
MIN_COHORT_N = 10

HARD_TO_PLACE_TAGS = {
    "bonded pair", "bonded", "special needs", "senior", "shy", "timid",
    "needs experienced owner", "only pet", "no small children",
}


@dataclass
class RiskFactor:
    name: str
    points: float
    detail: str


@dataclass
class RiskScore:
    animal_id: int
    name: str
    score: float
    days_listed: int
    tenure_percentile: float
    factors: list[RiskFactor] = field(default_factory=list)

    @property
    def band(self) -> str:
        if self.score >= 65:
            return "critical"
        if self.score >= 45:
            return "elevated"
        if self.score >= 25:
            return "watch"
        return "ok"

    def summary(self) -> str:
        """Tenure in one phrase, then the two biggest *actionable* reasons.

        Tenure is usually the largest contributor, but it isn't something a shelter
        can change — the friction factors (photos, write-up, restrictions) are.
        """
        tenure = {"tenure_percentile", "tenure_absolute"}
        pct = next((f for f in self.factors if f.name == "tenure_percentile"), None)
        rest = sorted((f for f in self.factors if f.name not in tenure),
                      key=lambda f: -f.points)[:2]
        parts = ([f"{self.days_listed}d, {pct.detail.replace('listed ', '')}"]
                 if pct else [f"{self.days_listed} days listed"])
        return "; ".join(parts + [f.detail for f in rest])

    def to_dict(self) -> dict[str, Any]:
        return {
            "animal_id": self.animal_id,
            "name": self.name,
            "score": round(self.score, 1),
            "band": self.band,
            "days_listed": self.days_listed,
            "tenure_percentile": round(self.tenure_percentile, 2),
            "factors": [
                {"name": f.name, "points": round(f.points, 1), "detail": f.detail}
                for f in sorted(self.factors, key=lambda f: -f.points)
            ],
        }


# --------------------------------------------------------------------- helpers


def _days_listed(row: dict[str, Any], now: datetime | None = None) -> int:
    if row.get("days_listed") is not None:
        return int(row["days_listed"])
    now = now or datetime.now(timezone.utc)
    stamp = row.get("published_at") or row.get("first_seen_at")
    if not stamp:
        return 0
    try:
        published = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return 0
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return max(0, (now - published).days)


def _cohort_key(row: dict[str, Any]) -> tuple[str, str]:
    size = (row.get("size") or "unknown").lower()
    bucket = "big" if size in {"large", "xlarge"} else "small"
    return ((row.get("type") or "unknown").lower(), bucket)


def _percentile(value: float, population: Sequence[float]) -> float:
    """Share of the cohort this animal has outlasted, 0.0-1.0."""
    if not population:
        return 0.0
    below = sum(1 for v in population if v < value)
    return below / len(population)


def _tags(row: dict[str, Any]) -> set[str]:
    raw = row.get("tags_json") or "[]"
    try:
        return {str(t).strip().lower() for t in json.loads(raw)}
    except (ValueError, TypeError):
        return set()


# ----------------------------------------------------------------- the scorer


def score_animal(
    row: dict[str, Any],
    cohort_tenures: Sequence[float],
    *,
    now: datetime | None = None,
    cohort_label: str | None = None,
) -> RiskScore:
    days = _days_listed(row, now)
    pct = _percentile(days, cohort_tenures)
    factors: list[RiskFactor] = []

    def add(name: str, share: float, detail: str) -> None:
        pts = WEIGHTS[name] * max(0.0, min(1.0, share))
        if pts > 0:
            factors.append(RiskFactor(name, pts, detail))

    peers = cohort_label or f"comparable {row.get('type') or 'animal'}s"
    add("tenure_percentile", pct, f"listed longer than {pct:.0%} of {peers}")
    add(
        "tenure_absolute", days / LONG_STAY_DAYS,
        f"{days} days on the listing",
    )

    age = (row.get("age") or "").lower()
    if age == "senior":
        add("age_senior", 1.0, "senior")
    elif age == "adult":
        add("age_adult", 1.0, "adult (past the puppy/kitten rush)")

    if (row.get("size") or "").lower() in {"large", "xlarge"}:
        add("size_large", 1.0, "large-breed size limits the housing pool")

    if row.get("special_needs"):
        add("special_needs", 1.0, "flagged special needs")

    if row.get("good_with_children") == 0:
        add("no_kids", 1.0, "not suitable for homes with children")

    if row.get("good_with_dogs") == 0 and row.get("good_with_cats") == 0:
        add("no_other_pets", 1.0, "needs to be the only pet")

    desc_len = len(row.get("description") or "")
    if desc_len < THIN_DESCRIPTION_CHARS:
        share = 1.0 - (desc_len / THIN_DESCRIPTION_CHARS)
        add("thin_listing", share, f"thin write-up ({desc_len} characters)")

    photos = int(row.get("photo_count") or 0)
    if photos == 0:
        add("no_photos", 1.0, "no photos on the listing")
    elif photos == 1:
        add("no_photos", 0.4, "only one photo")

    if _tags(row) & HARD_TO_PLACE_TAGS:
        overlap = sorted(_tags(row) & HARD_TO_PLACE_TAGS)
        add("bonded_or_pair", 1.0, f"tagged {', '.join(overlap)}")

    total = min(100.0, sum(f.points for f in factors))
    return RiskScore(
        animal_id=int(row["animal_id"]),
        name=row.get("name") or f"#{row['animal_id']}",
        score=total,
        days_listed=days,
        tenure_percentile=pct,
        factors=factors,
    )


def score_population(
    rows: Iterable[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> list[RiskScore]:
    """Score every animal against its own cohort. Returns highest risk first."""
    rows = list(rows)
    tenures = {id(r): float(_days_listed(r, now)) for r in rows}

    cohorts: dict[tuple[str, str], list[float]] = {}
    species: dict[str, list[float]] = {}
    everything: list[float] = []
    for row in rows:
        days = tenures[id(row)]
        cohorts.setdefault(_cohort_key(row), []).append(days)
        species.setdefault(_cohort_key(row)[0], []).append(days)
        everything.append(days)

    def reference(row: dict[str, Any]) -> tuple[list[float], str]:
        """Most specific peer group that is big enough to mean something."""
        kind, bucket = _cohort_key(row)
        plural = f"{row.get('type') or 'animal'}s".lower()
        options = [
            (cohorts[(kind, bucket)], f"{bucket}-size {plural} nearby"),
            (species[kind], f"{plural} nearby"),
            (everything, "all animals nearby"),
        ]
        for population, label in options:
            if len(population) >= MIN_COHORT_N:
                return population, label
        return everything, "all animals nearby"

    scored = []
    for r in rows:
        population, label = reference(r)
        scored.append(score_animal(r, population, now=now, cohort_label=label))
    scored.sort(key=lambda s: -s.score)
    return scored


def cohort_stats(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Median / p90 tenure per cohort — the context line for any dashboard."""
    cohorts: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        cohorts.setdefault(_cohort_key(row), []).append(float(_days_listed(row)))
    out: dict[str, dict[str, float]] = {}
    for key, values in cohorts.items():
        values.sort()
        idx = max(0, int(len(values) * 0.9) - 1)
        out[f"{key[0]}/{key[1]}"] = {
            "n": float(len(values)),
            "median_days": float(statistics.median(values)) if values else 0.0,
            "p90_days": values[idx] if values else 0.0,
        }
    return out
