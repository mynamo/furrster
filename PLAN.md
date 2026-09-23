# Furrster — build plan

The framing that makes this a portfolio piece rather than an API demo: **shelter
animals are a customer lifecycle problem**. Listings are accounts, days-listed is
tenure, adoption is conversion, and "hard to place" is a churn-risk segment. Every
phase below has an analytics analogue named in italics, because that is the thing a
hiring manager is actually reading for.

## Status (updated 2026-09-23)

| Phase | State |
|---|---|
| 1 · Ingestion + warehouse | ✅ shipped |
| 2 · Daily pull | ✅ built (`make schedule`) — **switch on once the Petfinder key arrives** |
| 3 · Lifecycle analytics | ✅ survival curves, edit effect, backtest, fitted scorer v2 |
| 4 · Matching | ✅ two rankers, eval harness, feedback loop · ⏳ human-agreement eval, LLM run on real profiles |
| 5 · Outreach | ✅ review queue, weekly cycle, listing-gap worklist, campaign effect (matched) · ⏳ prompt A/B, randomized test |
| 6 · Portfolio surface | ✅ dashboard · ⏳ write-up |

**Next up, in order:**

1. **Get the key → `make ingest` → `make schedule`.** History only builds up one day at
   a time. Every day without the schedule running is a day of data that can't be
   collected later.
2. **First real-data shakedown.** Expect more mismatches between how the API
   documents its data and what it actually sends (the simulator caught two already).
   Compare `stats` against what you see on petfinder.com for one shelter.
3. **Start recording campaigns from day one** (app → *Start campaign* /
   *Mark as published*, or `campaign add`). The effect estimate needs ~100 of them.
4. **Refit on real data.** After about 4 weeks of real pulls, run `fit` and
   `lifecycle`. Compare the real v2 AUC with the simulated one, and check whether
   time on the listing matters in reality (in the simulation it doesn't, by design).
   Re-set the band cut-offs.
5. **Write-up (Phase 6).** The parameter-recovery result and the "why real AUC will be
   lower" argument are the core of it.

---

## Phase 1 — Ingestion and local warehouse ✅

*Analogue: event pipeline + slowly-changing dimension.*

OAuth2 client, three-layer SQLite warehouse (current state + append-only snapshots +
run log), departed-listing sweep as the adoption proxy, transparent scoring, CLI,
offline tests.

---

## Phase 2 — Continuous pull ✅ (waiting on key)

*Analogue: scheduled ELT.*

- `scripts/daily_ingest.sh` + `launchd/` template, installed with `make schedule`
  (daily 07:15; if the Mac is asleep, it runs on wake). Logs go to
  `data/logs/ingest.log`. `make schedule-status` shows the last run.
- The script exits cleanly if `.env` has no key yet, so it's safe to install now.
- `ingest` refuses to write into a simulated database.
- Still to do: `--organization` to follow two or three shelters closely; alert
  when a run fails.

---

## Phase 2½ — Simulator ✅

*Analogue: a staging environment with synthetic traffic.*

Added because the key isn't here yet and every later phase needs history.
`simulate.py` replays N days through the real ingest path with a pinned clock and
records each animal's true adoption rate. It has already paid off: it caught the
`+0000` timestamp bug (every tenure would have been NULL on real data) and the
`Extra Large` / `xlarge` mismatch before either reached real data.

Rule: simulated numbers validate the **pipeline and the method**, never findings
about shelters. The simulation's multipliers are my assumptions.

---

## Phase 3 — Measured lifecycle ✅ (incl. 3b fitted scorer)

*Analogue: cohort retention curves + churn model.*

Done (`lifecycle.py`, Lifecycle tab):

- Relist-proof tenure (`first_published_at`, never overwritten).
- Kaplan–Meier with delayed entry. Animals still listed count as censored, and
  animals already listed when collection began count as at risk only from when we
  first saw them. On simulated data the naive median is 47 days and the correct one 30.
- Listing-edit comparison: animals that gained photos vs. untouched animals of the
  same tenure, followed over the same window. Observational only.
- Scorer backtest: rewind N days, score what was knowable then, check who was still
  listed 30 days later. Reports AUC, and on simulated data the best achievable AUC
  and the correlation with the true rate.

**3b — fitted weights ✅** (`fitting.py`, `fit` command)

- Poisson rate model on the animal-day table (discrete-time piecewise-exponential),
  fit by penalized Newton–Raphson in numpy; rate ratios with 95% intervals.
- Validated by parameter recovery: all 15 planted effects inside their intervals on
  simulated data, run as a test.
- The backtest refits using only pre-as-of data. On simulated data v2 reaches 0.73 AUC
  vs. 0.56 for v1 and a 0.75 ceiling (the gap is partly by construction; see README).
- v2 score = chance of still being listed in 30 days; reasons are rate ratios.
- ⏳ With real data: test interactions (senior × large), compare a tenure spline
  with log tenure, and check calibration (predicted vs. observed share still listed,
  by decile).

---

## Phase 4 — Matching ✅ / ⏳ human agreement

*Analogue: propensity scoring + segmentation.*

- ✅ Match tab: intake → SQL shortlist (unknown compatibility kept, not dropped) →
  ranking, with a named concern for each pick.
- ✅ Rule-based ranker: works with no API key, and is the comparison Claude has to
  beat rather than being compared with nothing.
- ✅ `match-eval`: generated adopter profiles; unsafe-pick rate, made-up-animal rate,
  reference utility, reliance on unknown data, at-risk reach. Run with and without
  the SQL filter — the filter accounts for essentially all of the safety today.
- ✅ Feedback loop: `match_outcomes` + app controls + `feedback summary` by ranker.
- ⏳ Run `match-eval --llm` once a key is in: does Claude beat the rules on fit, and
  does it stay safe when the filter is removed?
- ⏳ Human agreement: 30 real adopter descriptions, a counselor ranks the shortlist
  blind, measure agreement. Report it even if it's poor.
- ⏳ Decide from outcomes, not vibes: once ~50 suggestions have outcomes, compare
  met-or-adopted rates by ranker.

---

## Phase 5 — Outreach you can measure ✅ / ⏳ experiments

*Analogue: lifecycle marketing + incrementality measurement.*

- ✅ Review queue: nothing ships without a person approving, editing or rejecting
  it. `prompt_version` is stored with every generation.
- ✅ `outreach-cycle` (Mondays via the scheduled pull): refit if stale → top 5 animals
  not already in a campaign → Claude drafts into the queue → reports in
  `data/reports/`.
- ✅ Listing-gap worklist: counterfactual value of fixing photos and write-ups per
  animal, summed per shelter as expected extra adoptions.
- ✅ Campaigns table + measurement: risk-matched controls (5 nearest on predicted
  rate, same day), later-campaign controls counted only up to their own campaign,
  bootstrap CI. Validated against a planted ×1.8 effect: the naive comparison says
  ×1.0, the matched interval covers 1.8 on 5/5 seeds.
- ✅ An "in a campaign" factor in the fitted model: a second estimate, and scores are
  defined "without outreach".
- ⏳ **Randomized test.** The only thing that fully beats selection bias. For pairs
  of similar animals, feature one chosen by coin flip. Needs shelter buy-in; the
  campaigns table already supports it (add `kind = 'feature_rct'`).
- ⏳ Prompt A/B: alternate two `prompt_version`s and compare published-copy effects
  with the same matched machinery.
- ✅ Power: `campaign power`. At 0.024 departures/animal-day, a ×1.5 effect needs
  ~100 campaigns for 95% power, ~50 for 74%; a ×1.3 effect needs ~200.

---

## Phase 6 — The portfolio surface ✅ dashboard / ⏳ write-up

*Analogue: the exec dashboard.*

- One page, three panels: cohort tenure curves, the current at-risk queue with
  reasons, and a before/after on animals whose listings were refreshed.
- Write up the methodology honestly, including the limitations in the README. The
  relist problem and the adoption-proxy problem are the most interesting things to
  talk about in an interview — they are exactly the kind of caveat that separates
  someone who has run an analysis from someone who has read about one.
- If you show this to shelters rather than employers, lead with Phase 5's
  `unknowns_to_fill` report. It costs them nothing and is immediately actionable.

---

## Sequencing notes

- **Phase 2 before everything else.** History only builds up one day at a time and
  can't be compressed. `make schedule` the day the key arrives.
- Phases 3 and 4 are independent — do whichever is more fun first.
- Phase 5 needs Phase 3's baseline to be worth anything. Without a tenure baseline you
  cannot tell whether a rewritten bio helped.
- Resist adding a second data source until Phase 3 is done. One API, understood
  deeply, reads better than three APIs joined shallowly.

## Ethical guardrails to keep

- LLM copy is a draft for human approval, never a direct publish.
- Never soften or omit a listed restriction (no kids, no cats, special needs) to make
  an animal sound more adoptable. A mismatched placement ends in a return.
- No urgency or guilt framing in generated copy.
- Don't republish shelter contact details in bulk; respect the
  [Petfinder API terms](https://www.petfinder.com/developers/api-terms/).
