"""Command line entry point:  python -m furrster.cli <command>"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from . import db
from .config import ConfigError, load_settings
from .ingest import ingest_animals, ingest_organizations
from .scoring import cohort_stats, score_population


def _log(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not verbose:
        # One line per HTTP request drowns real output (and a simulation makes ~200).
        logging.getLogger("httpx").setLevel(logging.WARNING)


def _table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return "(nothing to show)"
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    head = "  ".join(c.ljust(widths[c]) for c in columns)
    rule = "  ".join("-" * widths[c] for c in columns)
    body = [
        "  ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns) for r in rows
    ]
    return "\n".join([head, rule, *body])


# ------------------------------------------------------------------ commands


def cmd_init(args: argparse.Namespace) -> int:
    settings = load_settings()
    conn = db.connect(settings.db_path)
    db.init_db(conn)
    conn.close()
    print(f"Database ready at {settings.db_path}")
    return 0


def _is_simulated(settings) -> bool:
    if not settings.db_path.exists():
        return False
    conn = db.connect(settings.db_path)
    try:
        return bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'sim_ground_truth'").fetchone())
    finally:
        conn.close()


def cmd_ingest(args: argparse.Namespace) -> int:
    settings = load_settings()
    if _is_simulated(settings):
        print(f"{settings.db_path} holds simulated history. Refusing to add real "
              "Petfinder data to it — point FURRSTER_DB at a different file.")
        return 1
    types = args.type or [None]
    total = 0
    for animal_type in types:
        result = ingest_animals(
            settings,
            animal_type=animal_type,
            location=args.location,
            distance=args.distance,
            max_pages=args.max_pages,
        )
        label = animal_type or "all types"
        print(
            f"{label}: {result.animals_seen} animals, "
            f"{result.departed} no longer listed (run {result.run_id})"
        )
        if result.errors:
            print(f"  {len(result.errors)} record errors, first: {result.errors[0]}")
        total += result.animals_seen
    print(f"Total: {total} animals into {settings.db_path}")
    return 0


def cmd_orgs(args: argparse.Namespace) -> int:
    settings = load_settings()
    count = ingest_organizations(
        settings, location=args.location, distance=args.distance,
        max_pages=args.max_pages,
    )
    print(f"{count} organizations stored")
    return 0


def cmd_at_risk(args: argparse.Namespace) -> int:
    settings = load_settings()
    conn = db.connect(settings.db_path)
    db.init_db(conn)
    rows = db.rows_to_dicts(db.fetch_active(conn, animal_type=args.type))
    conn.close()

    if not rows:
        print("No active animals in the database yet — run `ingest` first.")
        return 1

    from . import fitting

    model = fitting.load(settings.db_path) if args.scorer != "rules" else None
    if args.scorer == "fitted" and model is None:
        print("No fitted model next to this database yet — run `fit` first.")
        return 1

    if model is not None:
        v2 = sorted(fitting.score_rows(model, rows), key=lambda s: -s["score"])
        v2 = [s for s in v2 if s["score"] >= args.min_score][: args.limit]
        by_id = {r["animal_id"]: r for r in rows}
        if args.json:
            print(json.dumps(v2, indent=2, default=float))
            return 0
        print(f"\n{len(rows)} active animals · scorer: fitted model "
              f"({model.n_events} departures, fit {model.fitted_at[:10]})")
        print("Score = % chance the animal is still listed 30 days from now.\n")
        print(_table([{
            "id": s["animal_id"],
            "name": (by_id[s["animal_id"]].get("name") or "")[:20],
            "score": f"{s['score']:.0f}%",
            "band": fitting.band(s["score"]),
            "days": by_id[s["animal_id"]].get("days_listed"),
            "why": fitting.explain(s["factors"])[:80],
        } for s in v2], ["id", "name", "score", "band", "days", "why"]))
        return 0

    scored = score_population(rows)
    keep = [s for s in scored if s.score >= args.min_score][: args.limit]

    if args.json:
        print(json.dumps([s.to_dict() for s in keep], indent=2))
        return 0
    print("(rules scorer v1 — run `fit` once there is enough history for the fitted model)")

    print(f"\n{len(rows)} active animals. Cohort tenure:")
    for cohort, stats in sorted(cohort_stats(rows).items()):
        print(
            f"  {cohort:<16} n={int(stats['n']):<5} "
            f"median {stats['median_days']:.0f}d   p90 {stats['p90_days']:.0f}d"
        )

    print(f"\nTop {len(keep)} at risk:\n")
    print(
        _table(
            [
                {
                    "id": s.animal_id,
                    "name": s.name[:20],
                    "score": f"{s.score:.0f}",
                    "band": s.band,
                    "days": s.days_listed,
                    "why": s.summary()[:70],
                }
                for s in keep
            ],
            ["id", "name", "score", "band", "days", "why"],
        )
    )
    return 0


def cmd_match(args: argparse.Namespace) -> int:
    from .matcher import AdopterProfile, match

    settings = load_settings()
    profile = AdopterProfile(
        description=args.describe,
        animal_type=args.type,
        has_children=args.children,
        has_dogs=args.dogs,
        has_cats=args.cats,
        max_size=args.max_size,
        experience=args.experience,
        home=args.home,
        activity_level=args.activity,
    )
    result = match(settings, profile, top_n=args.top)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    for i, m in enumerate(result.get("matches", []), start=1):
        print(f"\n{i}. animal #{m.get('animal_id')}  fit {m.get('fit_score')}/100")
        print(f"   {m.get('rationale')}")
        print(f"   Concern: {m.get('concerns')}")
        for q in m.get("questions_to_ask", []):
            print(f"   Ask the shelter: {q}")
    if result.get("notes"):
        print(f"\nNotes: {result['notes']}")
    return 0


def cmd_draft(args: argparse.Namespace) -> int:
    from .bios import draft_for_animal

    settings = load_settings()
    conn = db.connect(settings.db_path)
    db.init_db(conn)
    rows = db.rows_to_dicts(db.fetch_active(conn))
    conn.close()

    if not rows:
        print("No active animals — run `ingest` first.")
        return 1

    by_id = {r["animal_id"]: r for r in rows}
    if args.animal_id:
        targets = [(by_id[args.animal_id], None)] if args.animal_id in by_id else []
        if not targets:
            print(f"Animal {args.animal_id} is not in the active set.")
            return 1
    else:
        scored = score_population(rows)[: args.count]
        targets = [(by_id[s.animal_id], s) for s in scored if s.animal_id in by_id]

    for row, risk in targets:
        result = draft_for_animal(settings, row, risk=risk)
        print(f"\n{'=' * 70}\n{row.get('name')}  (#{row['animal_id']})")
        if risk:
            print(f"risk {risk.score:.0f}/100 — {risk.summary()}")
        print(f"\nHOOK: {result.get('hook')}\n\nBIO:\n{result.get('bio')}")
        for post in result.get("social", []):
            print(f"\n[{post.get('channel')}]\n{post.get('body')}")
        if result.get("unknowns_to_fill"):
            print("\nAsk the shelter to add:")
            for gap in result["unknowns_to_fill"]:
                print(f"  - {gap}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    settings = load_settings()
    conn = db.connect(settings.db_path)
    db.init_db(conn)
    q = conn.execute
    print(f"db: {settings.db_path}")
    print(f"  animals total     {q('SELECT COUNT(*) FROM animals').fetchone()[0]}")
    print(
        "  active adoptable  "
        f"{q('SELECT COUNT(*) FROM animals WHERE is_active=1').fetchone()[0]}"
    )
    print(
        "  departed listings "
        f"{q('SELECT COUNT(*) FROM animals WHERE is_active=0').fetchone()[0]}"
    )
    print(f"  snapshots         {q('SELECT COUNT(*) FROM animal_snapshots').fetchone()[0]}")
    print(f"  organizations     {q('SELECT COUNT(*) FROM organizations').fetchone()[0]}")
    print(f"  generated copy    {q('SELECT COUNT(*) FROM generated_content').fetchone()[0]}")
    print("\n  recent runs:")
    for r in q(
        "SELECT run_id, started_at, animals_seen, status FROM ingest_runs "
        "ORDER BY run_id DESC LIMIT 5"
    ):
        print(f"    #{r['run_id']:<4} {r['started_at']}  {r['animals_seen']:>5} animals  {r['status']}")
    conn.close()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    import csv

    settings = load_settings()
    conn = db.connect(settings.db_path)
    db.init_db(conn)
    rows = db.rows_to_dicts(db.fetch_active(conn))
    conn.close()
    scored = {s.animal_id: s for s in score_population(rows)}
    from . import fitting

    model = fitting.load(settings.db_path)
    v2 = {s["animal_id"]: s for s in fitting.score_rows(model, rows)} if model else {}

    fields = [
        "animal_id", "name", "type", "breed_primary", "age", "size", "gender",
        "days_listed", "photo_count", "special_needs", "good_with_children",
        "good_with_dogs", "good_with_cats", "risk_score", "risk_band",
        "risk_reasons", "p_still_listed_30d", "fitted_band", "fitted_reasons", "url",
    ]
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            s = scored.get(row["animal_id"])
            row = dict(row)
            row["risk_score"] = round(s.score, 1) if s else None
            row["risk_band"] = s.band if s else None
            row["risk_reasons"] = s.summary() if s else None
            f = v2.get(row["animal_id"])
            if f:
                row["p_still_listed_30d"] = round(f["score"] / 100, 3)
                row["fitted_band"] = fitting.band(f["score"])
                row["fitted_reasons"] = fitting.explain(f["factors"])
            writer.writerow(row)
    print(f"Wrote {len(rows)} rows to {args.out}")
    return 0


def cmd_fit(args: argparse.Namespace) -> int:
    from . import fitting

    settings = load_settings()
    conn = db.connect(settings.db_path)
    db.init_db(conn)
    try:
        model = fitting.fit(conn)
    except ValueError as exc:
        print(exc)
        return 1
    finally:
        conn.close()

    print(f"\nFitted on {model.n_rows:,} animal-days, {model.n_animals} animals, "
          f"{model.n_events} departures.")
    if model.simulated:
        print("NOTE: simulated database — these estimates recover the simulator's own "
              "assumptions, not facts about shelters.")
    print("\nRate ratio = how fast animals with this factor leave the listing, relative to "
          "otherwise-similar animals without it (1.0 = no difference).\n")
    t = model.table()
    print(_table([{
        "factor": r.factor,
        "rate ratio": f"{r.rate_ratio:.2f}",
        "95% CI": f"{r.ci_low:.2f}–{r.ci_high:.2f}",
        "": "" if r.ci_low <= 1 <= r.ci_high else ("slower" if r.ci_high < 1 else "faster"),
    } for r in t.itertuples()], ["factor", "rate ratio", "95% CI", ""]))
    for note in model.notes:
        print(f"  ! {note}")
    if not args.dry_run:
        path = fitting.save(model, settings.db_path)
        print(f"\nSaved {path.name}. `at-risk` and the app now use it automatically.")
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    from .simulate import simulate

    settings = load_settings()
    if settings.db_path.exists() and not args.force:
        print(
            f"{settings.db_path} already exists. Simulated history must not be mixed "
            "with real pulls. Re-run with --force to add to it, or point FURRSTER_DB "
            "at a separate file (e.g. FURRSTER_DB=data/sim.db)."
        )
        return 1
    print(f"Simulating {args.days} days of daily pulls into {settings.db_path} …")
    logging.getLogger("furrster.ingest").setLevel(logging.WARNING)
    r = simulate(settings, days=args.days, seed=args.seed,
                 initial_population=args.population)
    print(
        f"{r.animals_total} animals, {r.adopted} left the listing, {r.still_listed} still "
        f"listed, {r.relists} relists, {r.edits} listing edits."
    )
    return 0


def cmd_lifecycle(args: argparse.Namespace) -> int:
    from . import lifecycle as L

    settings = load_settings()
    conn = db.connect(settings.db_path)
    db.init_db(conn)
    spells = L.load_spells(conn)
    if spells.empty or spells["event"].sum() == 0:
        print("Not enough history yet: lifecycle analysis needs animals to have left the "
              "listing. Keep the daily ingest running (or try `simulate`).")
        return 1

    km = L.kaplan_meier(spells)
    trunc = int((spells["entry_day"] > 3).sum())
    print(f"\n{len(spells)} listings observed, {int(spells['event'].sum())} departed, "
          f"{trunc} already listed when collection began (handled as delayed entry).")
    med = L.median_days(km)
    print(f"Median days on the listing: {'not reached yet' if med is None else round(med)}")
    print(f"Still listed after 30 days: {L.survival_at(km, 30):.0%}   "
          f"after 60: {L.survival_at(km, 60):.0%}")

    for col in ("cohort", "age"):
        print(f"\nBy {col}:")
        print(L.summarize(spells, col).to_string(index=False))

    eff = L.listing_edit_effect(conn).to_dict()
    print("\nListing improvements (photo count went up), 30-day window vs. untouched animals:")
    print(f"  {eff['edited_animals']} edited listings, departure rate "
          f"{eff['edited_daily_departure_rate']}/day vs {eff['control_daily_departure_rate']}/day"
          f" -> ratio {eff['rate_ratio']}  (observational, not causal)")

    bt = L.evaluate_scorer(conn, as_of_days_ago=args.backtest_days, horizon_days=30)
    print(f"\nScorer backtest (as of {bt.get('as_of', '-')}, 30-day horizon):")
    if "error" in bt:
        print(f"  {bt['error']}")
    else:
        print(f"  AUC rules v1 {bt['auc']}  ·  fitted v2 {bt['auc_fitted'] or '—'}"
              f"  on {bt['animals_scored']} animals")
        if bt.get("fitted_note"):
            print(f"  (v2: {bt['fitted_note']})")
        if "oracle_auc" in bt:
            print(f"  ceiling (true-hazard oracle) {bt['oracle_auc']}, "
                  f"rank correlation with true hazard {bt['spearman_vs_true_hazard']}")
        print("  rules-v1 bands:")
        for b in bt["by_band"]:
            print(f"  {b['band']:<9} {int(b['animals']):>4} animals  "
                  f"{b['share_still_listed']:.0%} still listed")
    conn.close()
    return 0


def cmd_app(args: argparse.Namespace) -> int:
    import subprocess
    from pathlib import Path

    app = Path(__file__).resolve().parent.parent / "app" / "streamlit_app.py"
    return subprocess.call([sys.executable, "-m", "streamlit", "run", str(app)])


# -------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="furrster", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create the SQLite schema").set_defaults(func=cmd_init)

    ing = sub.add_parser("ingest", help="pull animals from Petfinder")
    ing.add_argument("--type", action="append", help="dog, cat, … (repeatable)")
    ing.add_argument("--location", help="postcode, 'city, ST', or 'lat,lng'")
    ing.add_argument("--distance", type=int, help="miles, max 500")
    ing.add_argument("--max-pages", type=int, default=20, help="100 animals per page")
    ing.set_defaults(func=cmd_ingest)

    org = sub.add_parser("orgs", help="pull nearby organizations")
    org.add_argument("--location")
    org.add_argument("--distance", type=int)
    org.add_argument("--max-pages", type=int, default=10)
    org.set_defaults(func=cmd_orgs)

    risk = sub.add_parser("at-risk", help="rank active animals by placement risk")
    risk.add_argument("--type")
    risk.add_argument("--limit", type=int, default=20)
    risk.add_argument("--min-score", type=float, default=0.0)
    risk.add_argument("--json", action="store_true")
    risk.add_argument("--scorer", choices=["auto", "rules", "fitted"], default="auto",
                      help="auto = fitted model if one exists next to the DB, else rules")
    risk.set_defaults(func=cmd_at_risk)

    mat = sub.add_parser("match", help="match an adopter to animals (needs Anthropic key)")
    mat.add_argument("describe", help="the adopter's situation in their own words")
    mat.add_argument("--type")
    mat.add_argument("--children", action="store_true")
    mat.add_argument("--dogs", action="store_true")
    mat.add_argument("--cats", action="store_true")
    mat.add_argument("--max-size", choices=["small", "medium", "large", "xlarge"])
    mat.add_argument("--experience", choices=["first-time", "some", "experienced"])
    mat.add_argument("--home", choices=["apartment", "house-no-yard", "house-yard"])
    mat.add_argument("--activity", choices=["low", "moderate", "high"])
    mat.add_argument("--top", type=int, default=5)
    mat.add_argument("--json", action="store_true")
    mat.set_defaults(func=cmd_match)

    dr = sub.add_parser("draft", help="draft bios/social copy for at-risk animals")
    dr.add_argument("--animal-id", type=int, help="one specific animal")
    dr.add_argument("--count", type=int, default=3, help="top N at-risk animals")
    dr.set_defaults(func=cmd_draft)

    sub.add_parser("stats", help="what is in the database").set_defaults(func=cmd_stats)

    ex = sub.add_parser("export", help="CSV of active animals with risk scores")
    ex.add_argument("--out", default="data/at_risk.csv")
    ex.set_defaults(func=cmd_export)

    sim = sub.add_parser("simulate", help="replay N days of synthetic shelter history")
    sim.add_argument("--days", type=int, default=90)
    sim.add_argument("--seed", type=int, default=42)
    sim.add_argument("--population", type=int, default=140)
    sim.add_argument("--force", action="store_true")
    sim.set_defaults(func=cmd_simulate)

    lc = sub.add_parser("lifecycle", help="survival curves, edit effect, scorer backtest")
    lc.add_argument("--backtest-days", type=int, default=45)
    lc.set_defaults(func=cmd_lifecycle)

    ft = sub.add_parser("fit", help="fit risk weights to observed departures")
    ft.add_argument("--dry-run", action="store_true", help="print, don't save the model")
    ft.set_defaults(func=cmd_fit)

    sub.add_parser("app", help="launch the Streamlit dashboard").set_defaults(func=cmd_app)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _log(args.verbose)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"\nConfiguration problem:\n  {exc}\n", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
