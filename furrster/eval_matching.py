"""Is the matcher any good?

Two things worth measuring, and they need different setups:

1. **Does it respect the household?** A dog that can't live with cats must never be
   suggested to a cat owner. Normally SQL removes those animals before the ranker
   sees them, so the violation rate is zero by construction. To learn whether the
   *ranker itself* is safe, `pool="raw"` hands it the unfiltered population. That
   answers a real design question: how much of the safety comes from the guardrail
   and how much from the model.

2. **Does it pick well among the safe ones?** Scored with a reference utility that
   is deliberately not the baseline ranker's own formula (which would be circular):
   one point per stated preference satisfied. Also reported: how often a pick relies
   on unknown compatibility data, and how much of the shelter's hard-to-place
   population the suggestions reach.

Profiles are generated from the animals actually in the database, so every profile
has something to match against.
"""

from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from . import db
from .config import Settings
from .matcher import AdopterProfile, SIZE_ORDER_FULL, baseline_rank, shortlist

Ranker = Callable[[AdopterProfile, list[dict[str, Any]]], dict[str, Any]]

DESCRIPTIONS = [
    "Second-floor apartment, no yard. I work from home most days and run in the mornings.",
    "House with a fenced yard, two kids under ten, and a golden retriever already.",
    "Quiet retired couple, small condo. We want company for evenings and short walks.",
    "Share a house with roommates and two cats. Out at work 9-6 on weekdays.",
    "First pet of my own. Small flat, no garden, lots of time in the evenings.",
    "Experienced with rescues, big yard, no other animals. Happy to take a project.",
    "Family with a toddler, house with a yard, looking for something gentle.",
    "Van life, on the road most weeks, hiking every day. Want a trail companion.",
]


def generate_profiles(conn: sqlite3.Connection, n: int = 20, seed: int = 0
                      ) -> list[AdopterProfile]:
    """Adopter profiles drawn from the population that is actually listed."""
    rng = random.Random(seed)
    types = [r[0] for r in conn.execute(
        "SELECT DISTINCT type FROM v_active_animals WHERE type IS NOT NULL")] or ["Dog"]
    out = []
    for i in range(n):
        has_kids = rng.random() < 0.35
        out.append(AdopterProfile(
            description=rng.choice(DESCRIPTIONS),
            animal_type=rng.choice(types).lower() if rng.random() < 0.8 else None,
            has_children=has_kids,
            has_dogs=rng.random() < 0.3,
            has_cats=rng.random() < 0.25,
            max_size=rng.choice(SIZE_ORDER_FULL) if rng.random() < 0.7 else None,
            experience=rng.choice(["first-time", "some", "experienced"]),
            home=rng.choice(["apartment", "house-no-yard", "house-yard"]),
            activity_level=rng.choice(["low", "moderate", "high"]),
        ))
    return out


# ------------------------------------------------------------------ checks


def violations(profile: AdopterProfile, animal: dict[str, Any]) -> list[str]:
    """Hard mismatches: things that make a placement unsafe or a return likely."""
    bad = []
    if profile.has_children and animal.get("good_with_children") == 0:
        bad.append("not safe with children")
    if profile.has_dogs and animal.get("good_with_dogs") == 0:
        bad.append("not safe with resident dogs")
    if profile.has_cats and animal.get("good_with_cats") == 0:
        bad.append("not safe with resident cats")
    size = (animal.get("size") or "").lower()
    if (profile.max_size and size in SIZE_ORDER_FULL
            and SIZE_ORDER_FULL.index(size) > SIZE_ORDER_FULL.index(profile.max_size)):
        bad.append(f"{size} exceeds the {profile.max_size} limit they gave")
    if (profile.animal_type
            and (animal.get("type") or "").lower() != profile.animal_type.lower()):
        bad.append(f"{animal.get('type')} when they asked for a {profile.animal_type}")
    return bad


def reference_utility(profile: AdopterProfile, animal: dict[str, Any]) -> float:
    """Independent 0-1 score: share of the adopter's stated needs that are met.

    Deliberately not the baseline ranker's formula — scoring a ranker with its own
    objective would only prove it can optimise itself.
    """
    points, total = 0.0, 0.0

    def credit(ok: bool, weight: float = 1.0) -> None:
        nonlocal points, total
        total += weight
        points += weight * bool(ok)

    size = (animal.get("size") or "").lower()
    if profile.animal_type:
        credit((animal.get("type") or "").lower() == profile.animal_type.lower(), 2)
    if profile.max_size:
        credit(size in SIZE_ORDER_FULL
               and SIZE_ORDER_FULL.index(size) <= SIZE_ORDER_FULL.index(profile.max_size), 2)
    for present, key in ((profile.has_children, "good_with_children"),
                         (profile.has_dogs, "good_with_dogs"),
                         (profile.has_cats, "good_with_cats")):
        if present:
            credit(animal.get(key) == 1, 2)
    if profile.home == "apartment":
        credit(size in ("small", "medium"))
    if profile.experience == "first-time":
        credit(animal.get("special_needs") != 1)
        credit(animal.get("house_trained") != 0)
    tags = {t.lower() for t in json.loads(animal.get("tags_json") or "[]")}
    if profile.activity_level == "low":
        credit(not (tags & {"playful", "loves walks", "energetic"}))
    if profile.activity_level == "high":
        credit(bool(tags & {"playful", "loves walks", "energetic"})
               or (animal.get("age") or "").lower() in ("young", "adult"))
    return points / total if total else 0.0


def unknown_relevant(profile: AdopterProfile, animal: dict[str, Any]) -> bool:
    return any(animal.get(key) is None for present, key in (
        (profile.has_children, "good_with_children"), (profile.has_dogs, "good_with_dogs"),
        (profile.has_cats, "good_with_cats")) if present)


# ------------------------------------------------------------------- run


@dataclass
class EvalResult:
    ranker: str
    pool: str
    profiles: int
    suggestions: int
    violation_rate: float
    invalid_id_rate: float
    mean_utility: float
    unknown_reliance: float
    at_risk_share: float
    empty_results: int
    examples: list[str]

    def to_dict(self) -> dict[str, Any]:
        d = {k: (round(v, 3) if isinstance(v, float) else v)
             for k, v in self.__dict__.items() if k != "examples"}
        d["examples"] = self.examples[:5]
        return d


def evaluate(settings: Settings, profiles: Sequence[AdopterProfile], ranker: Ranker,
             *, name: str, pool: str = "filtered", top_n: int = 5,
             limit: int = 25) -> EvalResult:
    """Run one ranker over the profiles and score its suggestions.

    pool="filtered": the production path, hard constraints applied in SQL first.
    pool="raw": the ranker sees the population unfiltered — measures whether it
    would keep households safe on its own.
    """
    conn = db.connect(settings.db_path)
    raw_pool = db.rows_to_dicts(conn.execute(
        "SELECT * FROM v_active_animals ORDER BY days_listed DESC LIMIT ?",
        (limit,)).fetchall()) if pool == "raw" else None
    at_risk_ids = set()
    from . import fitting
    model = fitting.load(settings.db_path)
    if model is not None:
        rows = db.rows_to_dicts(db.fetch_active(conn))
        at_risk_ids = {s["animal_id"] for s in fitting.score_rows(model, rows)
                       if fitting.band(s["score"]) in ("critical", "elevated")}
    conn.close()

    n_sug = n_viol = n_unknown = n_atrisk = n_invalid = empty = 0
    utility, examples = [], []
    for profile in profiles:
        candidates = raw_pool if raw_pool is not None else shortlist(
            settings, profile, limit=limit)
        if not candidates:
            empty += 1
            continue
        by_id = {c["animal_id"]: c for c in candidates}
        result = ranker(profile, candidates)
        matches = result.get("matches", [])[:top_n]
        if not matches:
            empty += 1
        for m in matches:
            animal = by_id.get(m.get("animal_id"))
            n_sug += 1
            if animal is None:
                n_invalid += 1
                examples.append(f"suggested unknown animal id {m.get('animal_id')}")
                continue
            bad = violations(profile, animal)
            if bad:
                n_viol += 1
                if len(examples) < 20:
                    examples.append(f"{animal.get('name')}: {bad[0]}")
            utility.append(reference_utility(profile, animal))
            n_unknown += unknown_relevant(profile, animal)
            n_atrisk += animal["animal_id"] in at_risk_ids

    div = max(n_sug, 1)
    return EvalResult(
        ranker=name, pool=pool, profiles=len(profiles), suggestions=n_sug,
        violation_rate=n_viol / div, invalid_id_rate=n_invalid / div,
        mean_utility=sum(utility) / len(utility) if utility else 0.0,
        unknown_reliance=n_unknown / div, at_risk_share=n_atrisk / div,
        empty_results=empty, examples=examples)


def baseline_ranker(top_n: int = 5) -> Ranker:
    return lambda profile, candidates: baseline_rank(profile, candidates, top_n=top_n)


def llm_ranker(settings: Settings, top_n: int = 5) -> Ranker:
    from .matcher import match

    def run(profile: AdopterProfile, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        return match(settings, profile, top_n=top_n, candidates=candidates)

    return run
