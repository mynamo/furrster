"""Shelter simulator: realistic history without an API key.

Why this exists: every lifecycle question in this project needs weeks of daily
snapshots, and real history only accrues in wall-clock time. The simulator
replays N days of a small metro's shelters *through the real ingest path*
(Petfinder-shaped JSON -> MockTransport -> PetfinderClient -> ingest_animals),
with the clock pinned to each simulated day. So the pagination, normalization,
upsert, snapshot and departed-sweep code all run exactly as they will in
production.

It also knows the ground truth. Each animal's daily adoption probability is a
known function of its traits, stored in `sim_ground_truth`, which lets us check
whether the at-risk scorer actually ranks the slow-to-place animals first — a
validation you can never do on real data, where nobody knows the true hazard.

The multipliers below are plausible directions from shelter practice (seniors,
big dogs, special needs and thin listings move slower), not measured effects.
Don't quote the simulated numbers as findings about real shelters.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from . import db
from .config import Settings
from .ingest import ingest_animals
from .petfinder import PetfinderClient

BASE_DAILY_ADOPTION = 0.030

# Planted outreach effect: a featured animal's adoption rate is multiplied by this
# for CAMPAIGN_DAYS. Shelters feature the animals they worry about (slow, long
# listed), which is exactly the selection that fools a naive comparison.
CAMPAIGN_UPLIFT = 1.8
CAMPAIGN_DAYS = 30

AGE_MULT = {"Baby": 2.4, "Young": 1.5, "Adult": 1.0, "Senior": 0.45}
SIZE_MULT = {"Small": 1.2, "Medium": 1.0, "Large": 0.7, "Extra Large": 0.5}
TYPE_MULT = {"Dog": 1.0, "Cat": 0.85}

DOG_BREEDS = [
    ("Labrador Retriever", "Large"), ("Pit Bull Terrier", "Medium"),
    ("Chihuahua", "Small"), ("German Shepherd Dog", "Large"),
    ("Beagle", "Medium"), ("Mastiff", "Extra Large"), ("Terrier", "Small"),
    ("Husky", "Large"), ("Boxer", "Large"), ("Shih Tzu", "Small"),
    ("Australian Cattle Dog / Blue Heeler", "Medium"), ("Great Dane", "Extra Large"),
]
CAT_BREEDS = [
    ("Domestic Short Hair", "Small"), ("Domestic Medium Hair", "Medium"),
    ("Domestic Long Hair", "Medium"), ("Siamese", "Small"), ("Tabby", "Small"),
    ("Maine Coon", "Large"),
]
NAMES = (
    "Biscuit Luna Milo Pepper Olive Juniper Tank Rosie Moose Mabel Otis Hazel Waffles "
    "Ziggy Nala Bruno Cleo Duke Marge Pickles Scout Winnie Rocco Poppy Gus Tofu Maple "
    "Ranger Clementine Bear Pumpkin Sadie Loki Penny Harvey Dot Frankie Mochi Boris "
    "Ginger Hank Lulu Remy Beans Kiwi Arlo Taco Nova Walter Fig Sunny Oreo Blue"
).split()
TAG_POOL = ["Friendly", "Playful", "Affectionate", "Gentle", "Curious", "Couch potato",
            "Shy", "Timid", "Needs experienced owner", "Only pet", "Bonded pair",
            "Loves walks", "Smart", "Quiet"]
SHELTERS = [
    ("CA2201", "Mission Paws Rescue", "San Francisco", "94110"),
    ("CA2202", "East Bay Second Chance", "Oakland", "94601"),
    ("CA2203", "Peninsula Humane Friends", "San Mateo", "94401"),
]


@dataclass
class SimAnimal:
    id: int
    org: tuple[str, str, str, str]
    type: str
    name: str
    breed: str
    mixed: bool
    age: str
    size: str
    gender: str
    special_needs: bool
    kids: bool | None
    dogs: bool | None
    cats: bool | None
    photos: int
    desc_len: int
    tags: list[str]
    published_at: datetime
    arrived_on: datetime
    adopted_on: datetime | None = None
    edits: list[str] = field(default_factory=list)
    campaign_on: datetime | None = None

    def hazard(self, today: datetime | None = None) -> float:
        """Daily adoption probability. Without `today`, the base rate (no campaign)."""
        h = self.base_hazard()
        if (today is not None and self.campaign_on is not None
                and self.campaign_on <= today < self.campaign_on + timedelta(days=CAMPAIGN_DAYS)):
            h *= CAMPAIGN_UPLIFT
        return min(h, 0.5)

    def base_hazard(self) -> float:
        h = BASE_DAILY_ADOPTION
        h *= AGE_MULT[self.age] * SIZE_MULT[self.size] * TYPE_MULT[self.type]
        if self.special_needs:
            h *= 0.5
        if self.kids is False:
            h *= 0.75
        if self.dogs is False and self.cats is False:
            h *= 0.7
        if self.photos == 0:
            h *= 0.45
        elif self.photos == 1:
            h *= 0.75
        if self.desc_len < 280:
            h *= 0.8
        if {"Shy", "Timid", "Needs experienced owner"} & set(self.tags):
            h *= 0.8
        return h

    def to_petfinder(self, now: datetime) -> dict[str, Any]:
        """Serialize exactly the way the real API would, '+0000' offsets included."""
        org_id, _, city, postcode = self.org
        stamp = self.published_at.strftime("%Y-%m-%dT%H:%M:%S+0000")
        sentence = f"{self.name} is a {self.age.lower()} {self.breed.lower()}. "
        description = (sentence * (self.desc_len // len(sentence) + 1))[: self.desc_len]
        photo = {"small": "s.jpg", "medium": f"https://example.org/{self.id}.jpg",
                 "large": "l.jpg", "full": "f.jpg"}
        return {
            "id": self.id,
            "organization_id": org_id,
            "url": f"https://www.petfinder.com/{self.type.lower()}/{self.id}/",
            "type": self.type,
            "species": self.type,
            "breeds": {"primary": self.breed, "secondary": None,
                       "mixed": self.mixed, "unknown": False},
            "colors": {"primary": None, "secondary": None, "tertiary": None},
            "age": self.age,
            "gender": self.gender,
            "size": self.size,
            "coat": None,
            "name": self.name,
            "description": description,
            "photos": [photo] * self.photos,
            "videos": [],
            "status": "adoptable",
            "attributes": {"spayed_neutered": True, "house_trained": None,
                           "declawed": None, "special_needs": self.special_needs,
                           "shots_current": True},
            "environment": {"children": self.kids, "dogs": self.dogs, "cats": self.cats},
            "tags": self.tags,
            "contact": {"email": None, "phone": None,
                        "address": {"city": city, "state": "CA",
                                    "postcode": postcode, "country": "US"}},
            "published_at": stamp,
            "distance": None,
        }


class _Sim:
    def __init__(self, seed: int, start: datetime) -> None:
        self.rng = random.Random(seed)
        self.next_id = 70_000_000
        self.start = start

    def _maybe(self, p_true: float, p_none: float) -> bool | None:
        r = self.rng.random()
        if r < p_none:
            return None
        return self.rng.random() < p_true

    def new_animal(self, arrived: datetime, listed_days_ago: int = 0) -> SimAnimal:
        rng = self.rng
        self.next_id += rng.randint(1, 40)
        kind = "Dog" if rng.random() < 0.6 else "Cat"
        breed, size = rng.choice(DOG_BREEDS if kind == "Dog" else CAT_BREEDS)
        age = rng.choices(["Baby", "Young", "Adult", "Senior"], [0.2, 0.3, 0.37, 0.13])[0]
        tags = rng.sample(TAG_POOL, k=rng.randint(0, 3))
        photos = rng.choices([0, 1, 2, 3, 4, 6], [0.08, 0.17, 0.2, 0.25, 0.2, 0.1])[0]
        desc_len = int(rng.choices([0, 90, 200, 450, 900], [0.07, 0.15, 0.2, 0.35, 0.23])[0]
                       * rng.uniform(0.7, 1.3))
        return SimAnimal(
            id=self.next_id,
            org=rng.choice(SHELTERS),
            type=kind,
            name=rng.choice(NAMES),
            breed=breed,
            mixed=rng.random() < 0.55,
            age=age,
            size=size,
            gender=rng.choice(["Male", "Female"]),
            special_needs=rng.random() < 0.08,
            kids=self._maybe(0.75, 0.35),
            dogs=self._maybe(0.7, 0.4),
            cats=self._maybe(0.55, 0.5),
            photos=photos,
            desc_len=desc_len,
            tags=tags,
            published_at=arrived - timedelta(days=listed_days_ago,
                                             hours=rng.randint(0, 23)),
            arrived_on=arrived - timedelta(days=listed_days_ago),
        )


@dataclass
class SimulationResult:
    days: int
    animals_total: int
    adopted: int
    still_listed: int
    relists: int
    edits: int


def _client_for(animals: list[dict[str, Any]], per_page: int = 100) -> PetfinderClient:
    """A PetfinderClient whose HTTP layer serves today's simulated listings."""
    total_pages = max(1, -(-len(animals) // per_page))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"token_type": "Bearer", "expires_in": 3600,
                                             "access_token": "sim"})
        page = int(request.url.params.get("page", 1))
        chunk = animals[(page - 1) * per_page : page * per_page]
        return httpx.Response(200, json={
            "animals": chunk,
            "pagination": {"count_per_page": per_page, "total_count": len(animals),
                           "current_page": page, "total_pages": total_pages},
        })

    return PetfinderClient("sim", "sim", client=httpx.Client(
        transport=httpx.MockTransport(handler)), sleep_between_pages=0)


def simulate(
    settings: Settings,
    *,
    days: int = 90,
    seed: int = 42,
    initial_population: int = 140,
    arrivals_per_day: float = 3.5,
    end: datetime | None = None,
    campaigns: bool = True,
) -> SimulationResult:
    """Replay `days` of daily ingests ending at `end` (default: now)."""
    end = (end or datetime.now(timezone.utc)).replace(hour=14, minute=0, second=0,
                                                       microsecond=0)
    start = end - timedelta(days=days)
    sim = _Sim(seed, start)
    rng = sim.rng

    # Animals already listed when we start watching: left-truncated on purpose.
    active: list[SimAnimal] = [
        sim.new_animal(start, listed_days_ago=int(rng.expovariate(1 / 45)))
        for _ in range(initial_population)
    ]
    everyone: list[SimAnimal] = list(active)
    relists = edits = 0

    conn = db.connect(settings.db_path)
    db.init_db(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS sim_ground_truth (
        animal_id INTEGER PRIMARY KEY, daily_hazard REAL, arrived_on TEXT,
        adopted_on TEXT, edits_json TEXT)""")
    conn.commit()
    conn.close()

    try:
        for d in range(days + 1):
            today = start + timedelta(days=d)
            db.set_clock(today)

            if d > 0:
                # Adoptions happen before today's pull, so they show up as departures.
                still = []
                for a in active:
                    if rng.random() < a.hazard(today):
                        a.adopted_on = today
                    else:
                        still.append(a)
                active = still

                # Shelters improve some thin listings (a natural experiment).
                for a in active:
                    if (a.photos <= 1 or a.desc_len < 280) and rng.random() < 0.012:
                        a.photos = max(a.photos, rng.randint(3, 5))
                        a.desc_len = max(a.desc_len, rng.randint(400, 800))
                        a.edits.append(today.date().isoformat())
                        edits += 1
                    # Relisting resets published_at but keeps the id.
                    elif rng.random() < 0.004:
                        a.published_at = today - timedelta(hours=rng.randint(1, 10))
                        relists += 1

                # The shelter features animals it's worried about: slow and long-listed.
                if campaigns:
                    for a in active:
                        tenure = (today - a.published_at).days
                        if (a.campaign_on is None and a.base_hazard() < 0.02
                                and tenure >= 21 and rng.random() < 0.03):
                            a.campaign_on = today

                for _ in range(_poisson(rng, arrivals_per_day)):
                    newcomer = sim.new_animal(today - timedelta(hours=rng.randint(1, 20)))
                    active.append(newcomer)
                    everyone.append(newcomer)

            payload = [a.to_petfinder(today) for a in active]
            rng.shuffle(payload)
            ingest_animals(settings, client=_client_for(payload), max_pages=50,
                           location="94110", distance=50)
    finally:
        db.set_clock(None)

    conn = db.connect(settings.db_path)
    conn.executemany(
        """INSERT OR REPLACE INTO sim_ground_truth
           (animal_id, daily_hazard, arrived_on, adopted_on, edits_json)
           VALUES (?, ?, ?, ?, ?)""",
        [(a.id, a.base_hazard(), a.arrived_on.isoformat(),
          a.adopted_on.isoformat() if a.adopted_on else None, json.dumps(a.edits))
         for a in everyone],
    )
    for a in everyone:
        if a.campaign_on is not None:
            conn.execute(
                """INSERT INTO campaigns (animal_id, kind, started_at, note, created_at)
                   VALUES (?, 'feature', ?, 'simulated', ?)""",
                (a.id, a.campaign_on.isoformat(timespec="seconds"), a.campaign_on.isoformat()))
    conn.execute("CREATE TABLE IF NOT EXISTS sim_params (key TEXT PRIMARY KEY, value REAL)")
    conn.execute("INSERT OR REPLACE INTO sim_params VALUES ('campaign_uplift', ?)",
                 (CAMPAIGN_UPLIFT if campaigns else 1.0,))
    for org_id, name, city, postcode in SHELTERS:
        conn.execute(
            "UPDATE organizations SET name=?, city=?, state='CA', postcode=? "
            "WHERE organization_id=?", (name, city, postcode, org_id))
    conn.commit()
    conn.close()

    _seed_demo_drafts(settings, active[:3])

    adopted = sum(1 for a in everyone if a.adopted_on)
    return SimulationResult(days=days, animals_total=len(everyone), adopted=adopted,
                            still_listed=len(active), relists=relists, edits=edits)


def _poisson(rng: random.Random, lam: float) -> int:
    # Knuth; fine for small lambda and keeps us dependency-free.
    import math
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= L:
            return k - 1


def _seed_demo_drafts(settings: Settings, animals: list[SimAnimal]) -> None:
    """Put a few obviously-templated drafts in the review queue so the app's review
    flow can be tried without an Anthropic key. Marked as templates, not LLM output."""
    conn = db.connect(settings.db_path)
    for a in animals:
        rows = [
            ("bio", None, f"{a.name} is a {a.age.lower()} {a.breed.lower()} waiting at "
                          f"{a.org[1]}. [Template placeholder — run `furrster.cli draft` "
                          "with an Anthropic key for a real draft.]"),
            ("social_post", "instagram", f"Meet {a.name}. #adoptdontshop #{a.type.lower()}"),
        ]
        for kind, channel, body in rows:
            conn.execute(
                """INSERT INTO generated_content (animal_id, kind, channel, body, model,
                       prompt_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (a.id, kind, channel, body, "demo-template (not an LLM)", "demo",
                 db.utcnow()))
    conn.commit()
    conn.close()
