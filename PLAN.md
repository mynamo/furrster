# Furrster — build plan

The framing that makes this a portfolio piece rather than an API demo: **shelter
animals are a customer lifecycle problem**. Listings are accounts, days-listed is
tenure, adoption is conversion, and "hard to place" is a churn-risk segment. Every
phase below has an analytics analogue named in italics, because that is the thing a
hiring manager is actually reading for.

Phase 1 is done and in this repo. Phases 2–6 are the roadmap.

---

## Phase 1 — Ingestion and local warehouse ✅ *(shipped)*

*Analogue: event pipeline + slowly-changing dimension.*

- OAuth2 client for Petfinder v2, with token refresh, backoff, and pagination.
- SQLite warehouse: current-state dimensions, an append-only snapshot fact table, and
  a run log.
- Departed-listing sweep as an adoption proxy.
- Transparent at-risk scoring with per-factor attribution and cohort fallback.
- CLI, CSV export, 24 offline tests.

**Why the snapshot table matters:** Petfinder is a *current state* API. It will never
tell you that a dog's description changed or that its photos were removed. Without
`animal_snapshots`, every lifecycle question in phases 3–5 is unanswerable, and you
cannot retrofit history. Start collecting on day one even if you don't use it for a month.

---

## Phase 2 — Make the pull continuous *(1 evening)*

*Analogue: scheduled ELT.*

- `cron` / `launchd` entry: `ingest --type dog --type cat` once daily, off-peak.
  A daily dog+cat pull at `limit=100` across 50 miles is roughly 20–60 requests
  against a 1,000/day budget.
- Log each run to `ingest_runs` (already wired) and alert on `status != 'ok'`.
- Add `--organization` so you can follow two or three specific shelters closely
  rather than an entire metro.
- Backfill orgs weekly (`orgs`), not daily — they barely change.

**Checkpoint:** two weeks of daily snapshots. Until then, every tenure number in the
system comes from `published_at` and inherits the relist problem.

---

## Phase 3 — Replace proxies with measured lifecycle *(1 weekend)*

*Analogue: cohort retention curves.*

Once the snapshot table has real history:

- Compute true observed tenure: `MIN(observed_at)` → `left_listing_at`, instead of
  trusting `published_at`.
- Build survival curves per cohort — Kaplan–Meier is the right tool, animals still
  listed are right-censored, and `lifelines` does it in ten lines.
- Answer the questions that make the project interesting:
  - Median days-to-adoption by age, size, species, breed group, colour.
  - Does adding photos or lengthening a description measurably shorten tenure?
    (The `content_hash` column already detects those edits — this becomes a natural
    experiment.)
  - Which organizations place animals faster than their animal mix predicts?
- Swap the hand-set `WEIGHTS` in `scoring.py` for coefficients fitted against
  observed tenure. Keep the per-factor attribution — an explainable model that a
  shelter volunteer can argue with beats a slightly better AUC.

---

## Phase 4 — Matching, in front of a human *(1 weekend)*

*Analogue: propensity scoring + segmentation.*

- The CLI `match` command is the engine; put a thin Streamlit or FastAPI form on it so
  a volunteer can use it at an adoption event.
- Add a feedback loop: record whether the counselor forwarded the match, and whether
  it led to a meet-and-greet. That table is what turns this from a demo into a system.
- Precompute a coarse compatibility matrix in SQL for the common adopter archetypes
  (apartment/no pets, family with toddlers, experienced large-dog home) so the LLM
  call is only needed for the interesting cases.
- Evaluate honestly: hold out 30 real adopter descriptions, have a human rank the
  shortlist blind, and measure agreement. Report the number even if it's mediocre.

---

## Phase 5 — Outreach that you can measure *(ongoing)*

*Analogue: lifecycle marketing + creative A/B testing.*

- Weekly job: score, take the top N `critical` animals, draft copy, drop it in a
  review queue. **Never auto-post.** A human at the shelter approves every word.
- `generated_content.prompt_version` already exists — write two prompts, alternate
  them, and compare tenure change after publication.
- Track the `unknowns_to_fill` output separately. "Twelve of your long-listed dogs
  have no photos and no cat/dog compatibility data" is often worth more to a shelter
  than any bio you can write.
- Measure the intervention, not the copy: days-listed before vs. after the refresh,
  against a matched control group of animals you didn't touch.

---

## Phase 6 — The portfolio surface *(1 weekend)*

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

- **Phase 2 before everything else.** History accrues in wall-clock time; you cannot
  compress it later. Get the cron job running tonight even if you touch nothing else
  for a month.
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
