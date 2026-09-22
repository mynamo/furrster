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
5. **Outreach you can measure.** A weekly worklist of who to help and which listing
   fixes matter most, plus campaign tracking that compares featured animals with
   equally hard-to-place ones, so you can tell whether the outreach worked.

Everything except the two Claude features runs with no API keys and no network.

---

## Quick start

```bash
git clone https://github.com/mynamo/furrster.git && cd furrster
make install

# No Petfinder key yet? Build 90 days of synthetic history and open the app on it:
make simulate          # writes data/sim.db (never mixed with real data) and fits on it
make app-sim           # Streamlit dashboard on the simulated database

# With a key:
cp .env.example .env   # fill in PETFINDER_KEY / PETFINDER_SECRET
make ingest            # first real pull into data/furrster.db
make schedule          # daily 07:15 pull via launchd (macOS); Mondays also run outreach-cycle
make app
```

Petfinder keys: <https://www.petfinder.com/developers/>. The form asks for an
application name and URL — this repo's GitHub URL works. An Anthropic key (for
`match` and `draft`) goes in the same `.env`.

## The app

`make app` (or `python -m furrster.cli app`) opens six tabs:

| Tab | What it's for |
|---|---|
| **Overview** | Listed now, departures in the last 30 days, median days listed, risk mix |
| **At risk** | Filterable queue with reasons; click a row for the factor breakdown and a *Draft copy* button |
| **Lifecycle** | Survival curves by segment; what slows adoption (rate ratios); listing-edit effect; scorer backtest and calibration |
| **Outreach** | This week's picks with *Start campaign*; listing gaps per shelter with expected extra adoptions; measured campaign effect |
| **Match** | Adopter intake form → hard-filter shortlist → optional Claude ranking |
| **Review queue** | Approve, edit or reject every generated bio/post; *Mark as published* turns approved copy into a tracked campaign |

A yellow banner marks any simulated database so a demo is never mistaken for findings.

## CLI

| Command | What it does | Needs |
|---|---|---|
| `ingest --type dog --type cat` | Pull, upsert, snapshot, retire departed listings | Petfinder key |
| `orgs` | Pull nearby shelters/rescues | Petfinder key |
| `simulate --days 90` | Replay synthetic history through the real ingest path | — |
| `at-risk --limit 25` | Rank listed animals by risk, with reasons (fitted model if present) | — |
| `lifecycle` | Survival by segment, edit effect, scorer backtest | — |
| `fit` | Fit risk weights to observed departures; `at-risk` and the app switch to it | — |
| `export --out data/at_risk.csv` | Flat file for notebooks / BI tools | — |
| `stats` | Database contents and recent runs | — |
| `match "…" --children --max-size medium` | Ranked shortlist with rationale and concerns | Anthropic key |
| `draft --count 3` | Bios + Instagram/Facebook/X copy for the top at-risk animals | Anthropic key |
| `outreach-cycle` | Weekly: refit if stale, pick top animals, draft copy, write gap report to `data/reports/` | (Anthropic key for drafts) |
| `campaign add/list/effect` | Record outreach for an animal; measure the effect against matched animals | — |
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

**`fitting.py` — the fitted scorer (v2).** A Poisson regression on the animal-day
table: one row per animal per interval between two pulls, with an outcome of
1 if it was gone at the next pull. This is the discrete-time version of a
piecewise-exponential survival model. It's fit by penalized Newton–Raphson in numpy
(no scipy/statsmodels). The coefficients are *rate ratios*, e.g. "seniors leave the
listing at 0.41× the rate of otherwise-similar adults", each with a 95% interval. The
v2 score is the **chance the animal is still listed 30 days from now**, so it's an
actual probability rather than a points total. The reasons are the factors slowing
this animal down, each with its multiplier. Some details:

- Pulls are chained per slice (e.g. dog pulls with dog pulls), so a dog-only pull
  followed by a cat-only pull doesn't make every dog look adopted.
- `log(1 + days listed)` is a feature, so the data decides whether time on the
  listing matters in itself. In the simulation it correctly comes out at ×1.00.
- It refuses to fit on fewer than 60 departures, and flags factors seen on too few
  animal-days to estimate.
- The model is saved next to the database it was fit on (`data/x.db` →
  `data/x.model.json`). `at-risk`, `export` and the app pick it up automatically.
- v2 can flag an animal on day 6: if everything about it predicts a long wait, it's
  worth helping before the wait happens, not only after.

**`outreach.py` — from scores to actions, and whether the actions work.**

- *Listing gaps.* For every listed animal, what the shelter could fix today and what
  the fitted model says each fix is worth. It reruns the model with the gap filled
  and reports "adopted within 30 days: 36% now → 69% with photos and a proper
  write-up". Per shelter, the gains add up to expected extra adoptions. Missing
  compatibility info is listed but not valued, since filling it in can reveal a
  restriction as easily as remove a doubt. The values are only as causal as the
  model's coefficients, so treat them as a way to prioritize and check them with
  campaign tracking.
- *Campaigns.* Explicit records of outreach: featured posts, published copy (the
  review queue's *Mark as published*), listing refreshes.
- *Effect measurement.* Shelters feature the animals they worry about, so a naive
  comparison with all other animals makes featured animals look no better off.
  Instead, each featured animal is compared with its 5 nearest untouched animals on
  the fitted model's predicted rate on the same day, counting departures per
  animal-day over 30 days, with a bootstrap 95% interval. A control animal that gets
  its own campaign later is counted only up to that point, not dropped. Dropping it
  would keep only the fast adopters as controls, because shelters pick animals that
  kept waiting. The first version made exactly that mistake and understated the
  effect.
- The fitted model also has an "in a campaign" factor. That gives a second,
  regression-based estimate of the effect and keeps outreach from inflating other
  factors. Scores are always computed *without* outreach, which is the question that
  matters when deciding who to help.
- `outreach-cycle` runs weekly (the scheduled pull runs it on Mondays). It writes
  `data/reports/outreach_<date>.md` and `listing_gaps_<date>.csv`, and drafts copy
  into the review queue when an Anthropic key is set.

**`matcher.py` / `bios.py`** — hard constraints are applied in SQL before Claude sees
anything. Unknown stays unknown throughout (no data on cats ≠ bad with cats). The bio
prompt forbids invented facts and urgency framing, and also returns `unknowns_to_fill`:
what the shelter should add to the listing.

## What the simulation shows (and doesn't)

On 90 simulated days (455 animals, 3 shelters):

**Survival.** The median time listed is 30 days when delayed entry is handled and
47 days when it isn't. Ignoring it overstates the headline number by more than half.

**Parameter recovery.** The simulator assigns each animal a known adoption rate built
from known multipliers. Fitting on its history, **all 15 true rate ratios fall inside
their 95% intervals** (e.g. senior: true 0.45, fitted 0.41 [0.27–0.61]; extra large:
true 0.50, fitted 0.39 [0.24–0.61]; time listed: true 1.00, fitted 1.00 [0.89–1.11]).
This is a test in `tests/test_fitting.py`. It shows the fitting code is correct
before it's trusted on real data.

**Backtest.** Score everyone as of N days ago, then check who was still listed 30 days
later. The fitted model only sees data from before the as-of date:

| As of | Rules v1 AUC | Fitted v2 AUC | Best possible (true rates) |
|---|---|---|---|
| 30 days ago | 0.56 | 0.64 | 0.66 |
| 45 days ago | 0.56 | 0.73 | 0.75 |
| 60 days ago | 0.60 | 0.76 | 0.79 |

(Backtest numbers are from a simulation without outreach campaigns,
`simulate --no-campaigns`. With campaigns switched on, the outreach itself adds
noise and the numbers move around more (0.60–0.70 for the fitted model), but it still
beats the rules at every horizon.)

**Outreach effect.** The simulator features slow, long-listed animals, as a real
shelter would, and plants a ×1.8 boost to their adoption rate for 30 days:

| Estimate (seed 42) | Rate ratio | 95% CI |
|---|---|---|
| Naive: featured vs. everyone else | ×1.0 | — |
| Matched: featured vs. 5 equally hard-to-place animals | ×1.5 | 1.0–2.4 |
| Model factor "in a campaign" | ×1.6 | 1.1–2.4 |

Across five seeds, the matched interval contained the true ×1.8 every time, while
the naive estimate ranged from ×0.8 to ×1.15 (it says outreach does nothing). With
about 45 campaigns the interval is wide. On real data, plan on roughly 100 or more
campaigns before trusting a precise number.

**Calibration.** The fitted score is well calibrated where outreach doesn't
interfere. At the top end, animals with a predicted "75% still waiting" are observed
at about 55%. That's expected: scores assume no outreach, and those animals are the
ones the shelter features.

**How to read that honestly:** v2 comes close to the best possible score here partly
by construction. The simulator's adoption rates have exactly the multiplicative form
the model assumes, and use the same factors. Real adoptions depend on things no
listing records (how photogenic the animal is, the shelter's foot traffic, the
season), so expect a real-data AUC well below these numbers. What the simulation does
show: (1) the fitting and backtest code is correct and doesn't peek at the future,
(2) hand-set weights left a lot of accuracy unused, and (3) with a few weeks of real
data, the same pipeline will say how much of the gap is real.

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
make test   # 52 tests, no network, no keys
```

Covers the HTTP client (MockTransport), ingest edge cases, relist handling,
migrations, both scorers, recovery of the simulator's planted effects, a check that
the backtest can't see the future, recovery of a planted campaign effect (and the naive
estimate missing it), listing-gap valuation, the publish-to-campaign flow, the matcher (with a fake LLM), Kaplan–Meier against a
hand-worked example and a large simulated sample where the true median is known,
the review workflow, and a headless run of the Streamlit app (`AppTest`) through every tab.

## Known limitations

- A listing disappearing is not the same as an adoption (it could be a transfer, a
  death, or account cleanup).
- Coverage differs by shelter. A shelter that keeps its listings poorly will look like
  one with hard-to-place animals, so compare within a shelter before comparing across.
- The listing-edit effect and the listing-gap values are observational. Shelters
  choose which listings to improve. Campaign measurement uses matching, which only
  adjusts for what the model can see; a shelter that features the animals it knows
  are about to be adopted would fool it. A randomized test (feature one of two
  similar animals, chosen by coin flip) would settle it.
- The fitted model assumes each factor multiplies the adoption rate independently,
  with no interactions (e.g. "large *and* senior" is just the two ratios multiplied).
  Enough real data would let us test that.
- Band cut-offs (critical ≥ 80% chance of still being listed in 30 days, elevated
  ≥ 70%) were set on simulated data. Revisit them once real data is in.

## Data use

Petfinder data is subject to the [API terms](https://www.petfinder.com/developers/api-terms/).
Don't republish shelter contact details in bulk. Generated copy is a draft, and a
person approves it before it's used.
