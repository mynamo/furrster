"""LLM-drafted bios and social copy for the animals the scorer flags.

The prompt is written around one constraint: shelter marketing copy that invents
facts is worse than no copy at all, because an adopter who shows up expecting a
dog that is "great with toddlers" and meets one that isn't is a returned dog. So
the model gets the record, is told which fields are unknown, and is told to write
around the gaps rather than fill them.

PROMPT_VERSION is stored with every generation so you can A/B two prompts against
real adoption outcomes later.
"""

from __future__ import annotations

import json
from typing import Any

from . import db
from .config import Settings
from .llm import LLM
from .scoring import RiskScore

PROMPT_VERSION = "bio-v1"

SYSTEM_PROMPT = """You write adoption copy for an animal shelter. Your job is to make \
a long-listed animal feel like a specific individual rather than a row in a database.

Hard rules:
- Every concrete claim must come from the record you are given. You may not invent \
behaviours, history, medical facts, or how the animal is with children or other pets.
- Fields marked UNKNOWN are unknown. Write around them. Never guess.
- Do not hide a listed special need or restriction. Frame it honestly and practically \
("needs a home without cats" beats silence, and it filters out the wrong applicants).
- No sob-story framing, no guilt, no urgency theatre ("last chance", "running out of \
time"). Warmth and specificity, not pressure.
- Voice: warm, concrete, a little wry. Short sentences. No purple prose, no clichés \
like "furever home" or "looking for my hooman".

Return JSON only:
{"bio": "120-180 word listing bio", "hook": "one sentence under 15 words", \
"social": [{"channel": "instagram", "body": "caption with 2-4 hashtags"}, \
{"channel": "facebook", "body": "3-4 sentence post"}, {"channel": "x", "body": \
"under 240 characters"}], "unknowns_to_fill": ["what the shelter should add to the \
listing to make this animal easier to place"]}"""


def _record_for_prompt(row: dict[str, Any], risk: RiskScore | None) -> str:
    def tri(value: Any) -> str:
        if value is None:
            return "UNKNOWN"
        return "yes" if value else "no"

    lines = [
        f"Name: {row.get('name')}",
        f"Species/type: {row.get('type')} ({row.get('species')})",
        f"Breed: {row.get('breed_primary')}"
        + (f" / {row['breed_secondary']}" if row.get("breed_secondary") else "")
        + (" (mix)" if row.get("breed_mixed") else ""),
        f"Age: {row.get('age') or 'UNKNOWN'}",
        f"Gender: {row.get('gender') or 'UNKNOWN'}",
        f"Size: {row.get('size') or 'UNKNOWN'}",
        f"Coat: {row.get('coat') or 'UNKNOWN'}",
        f"Colour: {row.get('color_primary') or 'UNKNOWN'}",
        f"House trained: {tri(row.get('house_trained'))}",
        f"Spayed/neutered: {tri(row.get('spayed_neutered'))}",
        f"Special needs: {tri(row.get('special_needs'))}",
        f"Good with children: {tri(row.get('good_with_children'))}",
        f"Good with dogs: {tri(row.get('good_with_dogs'))}",
        f"Good with cats: {tri(row.get('good_with_cats'))}",
        f"Tags: {', '.join(json.loads(row.get('tags_json') or '[]')) or 'none'}",
        f"Photos on listing: {row.get('photo_count')}",
        f"Days listed: {row.get('days_listed')}",
        "",
        "Existing shelter description (may be empty, thin, or boilerplate):",
        (row.get("description") or "").strip() or "(none)",
    ]
    if risk:
        lines += [
            "",
            f"Why this animal was flagged (risk {risk.score:.0f}/100, {risk.band}): "
            f"{risk.summary()}",
        ]
    return "\n".join(lines)


def draft_for_animal(
    settings: Settings,
    row: dict[str, Any],
    *,
    risk: RiskScore | None = None,
    llm: LLM | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    llm = llm or LLM(settings)
    result = llm.complete_json(
        system=SYSTEM_PROMPT,
        user=_record_for_prompt(row, risk),
        max_tokens=1600,
        temperature=0.7,
    )
    result["animal_id"] = row["animal_id"]
    result["model"] = llm.model
    if persist:
        _persist(settings, row["animal_id"], result, risk)
    return result


def _persist(
    settings: Settings,
    animal_id: int,
    result: dict[str, Any],
    risk: RiskScore | None,
) -> None:
    conn = db.connect(settings.db_path)
    try:
        rows = [("bio", None, result.get("bio")), ("hook", None, result.get("hook"))]
        for post in result.get("social", []):
            rows.append(("social_post", post.get("channel"), post.get("body")))
        for kind, channel, body in rows:
            if not body:
                continue
            conn.execute(
                """INSERT INTO generated_content
                       (animal_id, kind, channel, body, model, prompt_version,
                        risk_score, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    animal_id, kind, channel, body, result.get("model"),
                    PROMPT_VERSION, risk.score if risk else None, db.utcnow(),
                ),
            )
        conn.commit()
    finally:
        conn.close()
