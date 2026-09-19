# Furrster — build plan

The framing that makes this a portfolio piece rather than an API demo: **shelter
animals are a customer lifecycle problem**. Listings are accounts, days-listed is
tenure, adoption is conversion, and "hard to place" is a churn-risk segment. Every
phase below has an analytics analogue named in italics, because that is the thing a
hiring manager is actually reading for.

## Status (Sept 2026)

| Phase | State |
|---|---|
| 1 · Ingestion + warehouse | ✅ shipped |
| 2 · Daily pull | ✅ built (`make schedule`) — **switch on once the Petfinder key arrives** |
| 3 · Lifecycle analytics | ✅ survival curves, edit effect, backtest · ⏳ weight fitting |
| 4 · Matching UI | ✅ Streamlit tab · ⏳ feedback loop, human-agreement eval |
| 5 · Outreach | ✅ review queue · ⏳ prompt A/B against outcomes |
| 6 · Portfolio surface | ✅ dashboard · ⏳ write-up |

**Next up, in order:**

1. **Get the key → `make ingest` → `make schedule`.** History only builds up one day at
   a time. Every day without the schedule running is a day of data that can't be
   collected later.
2. **First real-data shakedown.** Expect more mismatches between how the API
   documents its data and what it actually sends (the simulator caught two already).
   Compare `stats` against what you see on petfinder.com for one shelter.
3. **Fit the scorer (3b below).** It's the biggest known gap: backtest AUC 0.56
   against a ceiling of 0.75 on simulated data.
4. **Write-up (Phase 6)** once there are ~4 weeks of real snapshots.

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

## Phase 3 — Measured lifecycle ✅ / 3b fitting ⏳

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

**3b — fit the weights (next):**

- Model: a Poisson regression of departures per animal-day on the scorer's factors,
  with tenure as an offset or spline. This is equivalent to a piecewise-exponential
  survival model. It can be fit with numpy Newton–Raphson in about 40 lines, or with
  `statsmodels` if a new dependency is acceptable.
- Turn the coefficients into suggested `WEIGHTS` and keep the per-factor reasons.
  A model a shelter coordinator can argue with is worth more than a slightly higher AUC.
- Fit on **real** history only (≥4–6 weeks). Use the simulated data to check that the
  fitting code recovers the multipliers the simulator was given, which makes it a
  good unit test.
- Tenure currently gets 50 of 100 points. If real data shows the adoption rate
  doesn't change much with time listed, most of that weight should move to the
  friction factors.

---

## Phase 4 — Matching in front of a human ✅ UI / ⏳ evaluation

*Analogue: propensity scoring + segmentation.*

- ✅ Match tab: intake form → SQL shortlist (unknown compatibility kept, not
  dropped) → Claude ranking with a named concern for each pick.
- ⏳ Feedback: record whether a counselor forwarded a match and whether it led to a
  meet-and-greet. That table is what turns the demo into a working system.
- ⏳ Evaluation: 30 held-out adopter descriptions, a person ranks the shortlist blind,
  measure agreement. Report the result even if it's mediocre.

---

## Phase 5 — Outreach you can measure ✅ queue / ⏳ experiment

*Analogue: lifecycle marketing + creative A/B testing.*

- ✅ Review queue: every draft is `pending` until a person approves, edits or
  rejects it (`review_status`, `reviewed_at`, `review_note`). Nothing is posted
  automatically.
- ✅ `prompt_version` is stored with every generation.
- ⏳ Weekly job: score → draft the top N critical animals → queue.
- ⏳ Measure the intervention, not the copy: compare days listed before and after a
  refresh against a matched control group, using the same machinery as the
  listing-edit comparison.
- ⏳ `unknowns_to_fill` report per shelter. It's often the most useful thing to
  show a shelter.

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
