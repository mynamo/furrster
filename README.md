# Furrster

Shelter pet matcher and at-risk flagger, built on the [Petfinder API v2](https://www.petfinder.com/developers/v2/docs/).

Customer-lifecycle analytics pointed at shelter animals: listings are accounts,
days-listed is tenure, leaving the listing is conversion, and "hard to place" is a
churn-risk segment.

1. **Ingest and keep history.** Pull local listings daily, keep an append-only
   snapshot of every animal on every day. That history is the only way to do
   lifecycle analysis, because Petfinder only shows what's listed right now.
2. **Lifecycle analysis.** Kaplan–Meier survival curves by segment, handled properly
   for animals that were already listed when collection started.
3. **At-risk flagging.** A transparent 0–100 score with the reasons listed, backtested
   against what actually happened afterwards.
4. **Matching and outreach.** Adopter intake → SQL shortlist → Claude ranking with
   honest caveats; drafted bios and social copy that a person approves before use.

Everything except the two Claude features runs with no API keys and no network.

---

## Quick start

```bash
git clone https://github.com/mynamo/furrster.git && cd furrster
make install

# No Petfinder key yet? Build 90 days of synthetic history and open the app on it:
make simulate          # writes data/sim.db (never mixed with real data)
make app-sim           # Streamlit dashboard on the simulated database

# With a key:
cp .env.example .env   # fill in PETFINDER_KEY / PETFINDER_SECRET
make ingest            # first real pull into data/furrster.db
make schedule          # daily 07:15 pull via launchd (macOS)
make app
```

Petfinder keys: <https://www.petfinder.com/developers/>. The form asks for an
application name and URL — this repo's GitHub URL works. An Anthropic key (for
`match` and `draft`) goes in the same `.env`.

## The app

`make app` (or `python -m furrster.cli app`) opens five tabs:

| Tab | What it's for |
|---|---|
| **Overview** | Listed now, departures in the last 30 days, median days listed, risk mix |
| **At risk** | Filterable queue with reasons; click a row for the factor breakdown and a *Draft copy* button |
| **Lifecycle** | Survival curves split by species×size / age / species / shelter; listing-edit effect; scorer backtest |
| **Match** | Adopter intake form → hard-filter shortlist → optional Claude ranking |
| **Review queue** | Approve, edit or reject every generated bio/post before it's used |

A yellow banner marks any simulated database so a demo is never mistaken for findings.

## CLI

| Command | What it does | Needs |
|---|---|---|
| `ingest --type dog --type cat` | Pull, upsert, snapshot, retire departed listings | Petfinder key |
| `orgs` | Pull nearby shelters/rescues | Petfinder key |
| `simulate --days 90` | Replay synthetic history through the real ingest path | — |
| `at-risk --limit 25` | Rank listed animals by risk, with reasons | — |
| `lifecycle` | Survival by segment, edit effect, scorer backtest | — |
| `export --out data/at_risk.csv` | Flat file for notebooks / BI tools | — |
| `stats` | Database contents and recent runs | — |
| `match "…" --children --max-size medium` | Ranked shortlist with rationale and concerns | Anthropic key |
| `draft --count 3` | Bios + Instagram/Facebook/X copy for the top at-risk animals | Anthropic key |
| `app` | Launch the dashboard | — |

## Architecture

```
Petfinder API ─► petfinder.py ─► ingest.py ─► SQLite ─┬─► scoring.py ──► at-risk queue
 (or simulate.py    OAuth2,        normalize,   3 layers ├─► lifecycle.py ─► survival, backtest
  via MockTransport) retries,      upsert,               ├─► matcher.py ──► Claude ranking
                     pagination    snapshot              └─► bios.py ─────► Claude drafts ─► review queue
                                                                                  │
                                                       app/streamlit_app.py ◄────┘
```

**`petfinder.py`** — OAuth2 client-credentials, token cached and refreshed on 401,
backoff on 429/5xx, pagination with a `max_pages` cap. The daily dog+cat pull is
roughly 20–60 requests against a 1,000/day limit.

**`db.py` / `sql/schema.sql`** — current-state tables (`animals`, `organizations`), an
append-only `animal_snapshots` table (one row per animal per pull, with a content
hash that spots edits), and an `ingest_runs` log. Timestamps and sizes are
standardized as they come in (see *Bugs the simulator caught*). Idempotent migrations
upgrade older databases in place.

**Tenure that survives relists.** Petfinder's `published_at` resets when a shelter
relists an animal. `first_published_at` stores the earliest value ever seen and never
moves, and `v_active_animals` measures tenure from the earlier of that and our own
first sighting.

**`simulate.py`** — generates Petfinder-shaped JSON for three fictional shelters over
N days: arrivals, adoptions at a known per-animal daily rate, relists, and listing
improvements. It feeds that data through `MockTransport → PetfinderClient →
ingest_animals` with the clock set to each simulated day, so the production code runs
unchanged. Each animal's true adoption rate is saved in `sim_ground_truth`.

**`lifecycle.py`** — Kaplan–Meier with *delayed entry*: an animal first seen on day 40
of its listing only counts as at risk from day 40 on. Leaving that out is the most
common mistake with this kind of data. On the simulated data it moves the median from
47 days (naive) to 30 (correct), because animals caught partway through a listing are
more likely to be the ones that stay. Also includes a listing-edit comparison (photos
added vs. untouched animals of the same tenure) and a backtest for the scorer.

**`scoring.py`** — weighted factors with the reasons listed: tenure against the
animal's cohort (falling back to species, then to all animals, when a cohort has fewer
than 10), plus age, size, special needs, restrictions, thin write-ups, missing photos,
and hard-to-place tags. The listed reasons put things a shelter can change ahead of
tenure.

**`matcher.py` / `bios.py`** — hard constraints are applied in SQL before Claude sees
anything. Unknown stays unknown throughout (no data on cats ≠ bad with cats). The bio
prompt forbids invented facts and urgency framing, and also returns `unknowns_to_fill`:
what the shelter should add to the listing.

## What the simulation shows (and doesn't)

On 90 simulated days (455 animals, 3 shelters):

| Check | Result | Reading |
|---|---|---|
| Median days listed, truncation-aware vs. naive | 30 vs. 47 | Ignoring delayed entry overstates the headline number by more than half |
| Scorer backtest AUC (as of 45 days ago, still listed 30 days later) | 0.56 | Better than a coin flip, but not by much |
| Same, with the true adoption rate as the score | 0.75 | The best any scorer could do here; adoption stays random even when the odds are known |
| Rank correlation of score with the true rate | −0.51 | The score gets the ordering roughly right… |

…but it leaves a large share of the possible accuracy unused. The weights are
hand-set, and half the points go to tenure, which says little about a single animal
when adoption odds don't change with time. **Fitting the weights to observed outcomes
is the next piece of work** (see `PLAN.md`). Note that the simulation's multipliers
are ones I set, so fitting to simulated data would just recover my own assumptions.
The fitting has to happen on real history.

## Bugs the simulator caught

These passed the hand-written test data and would have broken on real data:

- Petfinder timestamps end in `+0000`. SQLite's date functions can't read that offset
  and quietly return NULL, so **every** tenure would have been NULL. They're now
  converted to UTC ISO format as they come in.
- The API filters with `size=xlarge` but returns `"Extra Large"`, so the scorer's
  large-size check never fired for the biggest dogs. Sizes are now stored in one
  standard form.
- Streamlit's data cache ignored which database was open, so switching `FURRSTER_DB`
  showed the previous database's data.

## Testing

```bash
make test   # 39 tests, no network, no keys
```

Covers the HTTP client (MockTransport), ingest edge cases, relist handling,
migrations, the scorer, the matcher (with a fake LLM), Kaplan–Meier against a
hand-worked example and a large simulated sample where the true median is known,
the review workflow, and a headless run of the Streamlit app (`AppTest`) through every tab.

## Known limitations

- A listing disappearing is not the same as an adoption (it could be a transfer, a
  death, or account cleanup).
- Coverage differs by shelter. A shelter that keeps its listings poorly will look like
  one with hard-to-place animals, so compare within a shelter before comparing across.
- The listing-edit effect is observational. Shelters choose which listings to improve.
- Scorer weights are hand-set and not yet fitted (see above).

## Data use

Petfinder data is subject to the [API terms](https://www.petfinder.com/developers/api-terms/).
Don't republish shelter contact details in bulk. Generated copy is a draft, and a
person approves it before it's used.
