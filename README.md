# Furrster

Shelter pet matcher and at-risk flagger, built on the [Petfinder API v2](https://www.petfinder.com/developers/v2/docs/).

Two jobs, both borrowed from customer analytics and pointed at shelter animals:

1. **Lifecycle / at-risk analysis.** Pull local listings on a schedule, track how long
   each animal has been listed, and rank them by how hard they are likely to be to
   place — with the reasons attached, not just a number.
2. **Matching and outreach.** Turn an adopter's description of their life into a ranked
   shortlist with honest caveats, and auto-draft better bios and social copy for the
   animals the scorer flags.

Everything except the two LLM commands runs with no Anthropic key and no network.

---

## Quick start

```bash
git clone https://github.com/mynamo/furrster.git
cd furrster
make install                     # venv + dependencies

cp .env.example .env             # then fill in your Petfinder key/secret
python scripts/seed_demo.py      # optional: 33 fake animals, no API key needed
python -m furrster.cli at-risk
```

Petfinder keys are free: <https://www.petfinder.com/developers/> → *Get an API key*.
An Anthropic key (for `match` and `draft`) goes in the same `.env`.

## Commands

| Command | What it does | Needs a key? |
|---|---|---|
| `init-db` | Create the SQLite schema | no |
| `ingest --type dog --type cat` | Pull listings, upsert, snapshot, retire departed ones | Petfinder |
| `orgs` | Pull nearby shelters/rescues | Petfinder |
| `at-risk --limit 25` | Rank active animals by placement risk, with reasons | no |
| `export --out data/at_risk.csv` | Flat file for a dashboard or a notebook | no |
| `stats` | What's in the database, recent runs | no |
| `match "adopter description" --children --max-size medium` | Ranked shortlist with rationale and concerns | Anthropic |
| `draft --count 3` | Bios + Instagram/Facebook/X copy for the top at-risk animals | Anthropic |

```bash
python -m furrster.cli ingest --type dog --location 94110 --distance 50
python -m furrster.cli at-risk --limit 10
python -m furrster.cli match "Second-floor apartment, no yard. I work from home three \
days a week and run most mornings. First dog of my own, but I grew up with beagles. \
No kids, no other pets." --type dog --max-size medium --experience some
python -m furrster.cli draft --count 3
```

## How it fits together

```
Petfinder API ──► petfinder.py ──► ingest.py ──► SQLite ──► scoring.py ──► CLI / CSV
   OAuth2          pagination       normalize    3 layers    risk + why        │
   + retries       + rate care      + upsert                                   │
                                        │                                      ▼
                                        └──────────────► matcher.py  ·  bios.py
                                                         (Claude)      (Claude)
```

**`petfinder.py`** — OAuth2 client-credentials, token cached and refreshed on 401,
exponential backoff on 429/5xx, pagination generators with a `max_pages` guard rail.
Petfinder allows 1,000 requests/day and 50/second; at `limit=100` the default run is
20 requests.

**`db.py` / `sql/schema.sql`** — three layers:

- `organizations`, `animals` — current state, upserted every run.
- `animal_snapshots` — append-only, one row per animal per run, with a content hash.
  This is the only reason lifecycle analysis is possible: Petfinder tells you what is
  listed *now*, never what changed.
- `ingest_runs` — the run log, so every snapshot traces back to a pull.

When an animal that was active stops appearing, `mark_departed` sets `is_active = 0`
and stamps `left_listing_at`. That is the adoption proxy — Petfinder never tells you
*why* a listing vanished.

**`scoring.py`** — a transparent weighted model, not a black box. Every animal gets a
0–100 score plus the list of factors that produced it, so a shelter coordinator can
disagree with it out loud. Two families of signal:

- *Tenure* — days listed, scored against the animal's own cohort (species × size
  class). Cohorts below `MIN_COHORT_N = 10` fall back to species, then to the whole
  population, because "older than 50% of my two peers" is noise.
- *Friction* — senior/adult age, large size, special needs, restrictions on children
  or other pets, thin write-ups, missing photos, hard-to-place tags.

Weights live in one dict at the top of the file. Tune them, re-run, diff the ranking.

**`matcher.py`** — SQL narrows on hard constraints first (species, size, kid/pet
safety), then Claude ranks the shortlist on temperament and lifestyle fit. The model
never sees an animal that failed a hard filter, is told to use only the supplied
records, and must name a concern for every match. `NULL` is preserved as *unknown*
throughout: an animal with no data on cats is a candidate for a cat owner, but the
model is told the field is unknown rather than shown a `false`.

**`bios.py`** — drafts a listing bio, a one-line hook, and three channel-specific
posts. The prompt's hard rules: no invented facts, unknowns written around rather
than filled in, listed restrictions stated plainly, no urgency theatre. It also
returns `unknowns_to_fill` — what the shelter should add to the listing to make the
animal easier to place, which is often the highest-leverage output.

Every generation is stored in `generated_content` with its `prompt_version` and model,
so you can A/B two prompts against real outcomes later.

## Testing

```bash
make test        # 24 tests, no network, no API keys
```

The Petfinder client is exercised through `httpx.MockTransport` against recorded-shape
fixtures that include the awkward cases: a null-heavy record, a zero-photo senior with
every restriction set, a three-day-old puppy, an animal with no `published_at`.

## Known limitations

- `days_listed` comes from `published_at`, which **resets when a shelter relists an
  animal**. Read it as listing tenure, not time in care. Once you have a few weeks of
  snapshots, `MIN(observed_at)` per animal is the more honest measure.
- A listing disappearing is not the same as an adoption. It could be a transfer, a
  death, or a shelter cleaning up its Petfinder account.
- Petfinder coverage is shelter-dependent. Organizations that manage listings badly
  look like organizations with hard-to-place animals. Segment by organization before
  drawing conclusions.
- The risk weights are informed priors, not fitted coefficients. Fitting them needs
  outcome data — see Phase 5 in `PLAN.md`.

## Layout

```
furrster/
├── furrster/
│   ├── config.py      # env/.env settings, explicit errors when keys are missing
│   ├── petfinder.py   # API v2 client: auth, retries, pagination
│   ├── db.py          # schema bootstrap, normalization, upserts, snapshots
│   ├── ingest.py      # pull → normalize → store → retire departed
│   ├── scoring.py     # at-risk / hard-to-place model
│   ├── matcher.py     # SQL shortlist + Claude ranking
│   ├── bios.py        # Claude-drafted bios and social copy
│   └── cli.py         # python -m furrster.cli
├── sql/schema.sql
├── scripts/seed_demo.py
├── tests/             # fixtures + 24 offline tests
└── PLAN.md            # phased build plan
```

## Licence & data use

Petfinder data is subject to the [Petfinder API terms of use](https://www.petfinder.com/developers/api-terms/).
Don't republish shelter contact details in bulk, and attribute listings back to
Petfinder. LLM-drafted copy is a **draft** — a person at the shelter approves it
before it goes anywhere near an adopter.
