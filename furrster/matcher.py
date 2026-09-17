"""Adopter -> animal matching.

Two stages on purpose:

  1. SQL narrows the pool on hard constraints (species, size, kid/pet safety).
     Cheap, deterministic, and it keeps the prompt small.
  2. The LLM ranks the shortlist on the soft stuff — energy level, experience
     required, what the description actually implies about the animal's temperament
     — and has to justify every pick against the adopter's own words.

The model never sees an animal it could not legally/safely be matched with, and it
is instructed to work only from the supplied records, so it cannot invent a dog.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from . import db
from .config import Settings
from .llm import LLM

SYSTEM_PROMPT = """You are an adoption counselor at an animal shelter. You match \
adopters to specific animals from a shortlist, and you are honest when the fit is \
imperfect.

Rules you must follow:
- Only recommend animals present in the CANDIDATES list. Never invent one.
- Use only facts contained in the candidate records. If a record does not say an \
animal is good with children, say the information is missing — do not assume either way.
- Weigh the adopter's living situation, experience, and time budget at least as \
heavily as their stated breed or looks preferences.
- Name a real concern for every match. A match with no downside is a match you have \
not thought about.
- If none of the candidates are a responsible fit, say so and return fewer matches.

Return JSON only, no prose outside it, in this shape:
{"matches": [{"animal_id": 123, "fit_score": 0-100, "rationale": "2-3 sentences \
addressed to the adopter", "concerns": "the honest caveat", "questions_to_ask": \
["question for the shelter"]}], "notes": "anything the counselor should know"}"""


@dataclass
class AdopterProfile:
    """Free text plus the few structured fields worth filtering on in SQL."""

    description: str
    animal_type: str | None = None          # dog | cat | ...
    has_children: bool = False
    has_dogs: bool = False
    has_cats: bool = False
    max_size: str | None = None             # small | medium | large | xlarge
    experience: str | None = None           # first-time | some | experienced
    home: str | None = None                 # apartment | house-no-yard | house-yard
    activity_level: str | None = None       # low | moderate | high

    def to_prompt_block(self) -> str:
        bits = [f"In their own words: {self.description.strip()}"]
        facts = {
            "Animal type wanted": self.animal_type,
            "Children at home": self.has_children,
            "Resident dogs": self.has_dogs,
            "Resident cats": self.has_cats,
            "Largest size they can take": self.max_size,
            "Experience level": self.experience,
            "Home": self.home,
            "Activity level": self.activity_level,
        }
        for label, value in facts.items():
            if value not in (None, ""):
                bits.append(f"{label}: {value}")
        return "\n".join(bits)


SIZE_ORDER = ["small", "medium", "large", "xlarge"]


def shortlist(
    settings: Settings,
    profile: AdopterProfile,
    *,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Stage 1 — hard filters in SQL. Nulls are kept: unknown is not the same as no."""
    clauses: list[str] = ["is_active = 1", "status = 'adoptable'"]
    params: list[Any] = []

    if profile.animal_type:
        clauses.append("LOWER(type) = ?")
        params.append(profile.animal_type.lower())
    if profile.has_children:
        clauses.append("(good_with_children IS NULL OR good_with_children = 1)")
    if profile.has_dogs:
        clauses.append("(good_with_dogs IS NULL OR good_with_dogs = 1)")
    if profile.has_cats:
        clauses.append("(good_with_cats IS NULL OR good_with_cats = 1)")
    if profile.max_size and profile.max_size.lower() in SIZE_ORDER:
        allowed = SIZE_ORDER[: SIZE_ORDER.index(profile.max_size.lower()) + 1]
        clauses.append(
            "(size IS NULL OR LOWER(size) IN (%s))" % ", ".join("?" for _ in allowed)
        )
        params.extend(allowed)

    sql = f"""
        SELECT * FROM v_active_animals
        WHERE {' AND '.join(clauses)}
        ORDER BY days_listed DESC
        LIMIT ?
    """
    params.append(limit)

    conn = db.connect(settings.db_path)
    try:
        return db.rows_to_dicts(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def _candidate_card(row: dict[str, Any]) -> dict[str, Any]:
    """Trim a DB row to what the model needs. Keeps prompts small and cheap."""
    desc = (row.get("description") or "").strip()
    return {
        "animal_id": row["animal_id"],
        "name": row.get("name"),
        "type": row.get("type"),
        "breed": row.get("breed_primary"),
        "mixed_breed": bool(row.get("breed_mixed")),
        "age": row.get("age"),
        "size": row.get("size"),
        "gender": row.get("gender"),
        "days_listed": row.get("days_listed"),
        "house_trained": row.get("house_trained"),
        "special_needs": row.get("special_needs"),
        "good_with_children": row.get("good_with_children"),
        "good_with_dogs": row.get("good_with_dogs"),
        "good_with_cats": row.get("good_with_cats"),
        "tags": json.loads(row.get("tags_json") or "[]"),
        "shelter_description": desc[:900],
        "url": row.get("url"),
    }


def match(
    settings: Settings,
    profile: AdopterProfile,
    *,
    top_n: int = 5,
    candidates: Sequence[dict[str, Any]] | None = None,
    llm: LLM | None = None,
) -> dict[str, Any]:
    """Stage 2 — LLM ranking of the shortlist. Persists the result for review."""
    pool = list(candidates) if candidates is not None else shortlist(settings, profile)
    if not pool:
        return {"matches": [], "notes": "No animals in the database fit the hard filters."}

    llm = llm or LLM(settings)
    cards = [_candidate_card(r) for r in pool]
    user = (
        f"ADOPTER\n{profile.to_prompt_block()}\n\n"
        f"CANDIDATES ({len(cards)} animals)\n"
        f"{json.dumps(cards, indent=1)}\n\n"
        f"Return the {top_n} best fits, best first."
    )
    result = llm.complete_json(system=SYSTEM_PROMPT, user=user, max_tokens=3000)
    result["model"] = llm.model
    _persist(settings, profile, result)
    return result


def _persist(settings: Settings, profile: AdopterProfile, result: dict[str, Any]) -> None:
    conn = db.connect(settings.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO adopters (label, prefs_json, created_at) VALUES (?, ?, ?)",
            (
                profile.description[:60],
                json.dumps(profile.__dict__),
                db.utcnow(),
            ),
        )
        adopter_id = cur.lastrowid
        for rank, m in enumerate(result.get("matches", []), start=1):
            conn.execute(
                """INSERT INTO matches
                       (adopter_id, animal_id, rank, fit_score, rationale,
                        concerns, model, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    adopter_id,
                    m.get("animal_id"),
                    rank,
                    m.get("fit_score"),
                    m.get("rationale"),
                    m.get("concerns"),
                    result.get("model"),
                    db.utcnow(),
                ),
            )
        conn.commit()
        result["adopter_id"] = adopter_id
    finally:
        conn.close()
