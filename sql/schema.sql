-- Furrster local warehouse.
-- Three-layer design:
--   organizations / animals  -> current-state dimension tables (upserted each run)
--   animal_snapshots         -> append-only fact table, one row per animal per ingest run
--   ingest_runs              -> run log, so every snapshot is traceable to a pull
-- The snapshot table is what makes lifecycle analysis possible: it is the only way
-- to know when a listing appeared, when it changed, and when it stopped appearing.

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    params_json   TEXT NOT NULL,
    pages_fetched INTEGER DEFAULT 0,
    animals_seen  INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'running',
    error         TEXT
);

CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name            TEXT,
    email           TEXT,
    phone           TEXT,
    city            TEXT,
    state           TEXT,
    postcode        TEXT,
    country         TEXT,
    url             TEXT,
    website         TEXT,
    mission         TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    raw_json        TEXT
);

CREATE TABLE IF NOT EXISTS animals (
    animal_id          INTEGER PRIMARY KEY,
    organization_id    TEXT,
    name               TEXT,
    type               TEXT,
    species            TEXT,
    breed_primary      TEXT,
    breed_secondary    TEXT,
    breed_mixed        INTEGER,
    breed_unknown      INTEGER,
    color_primary      TEXT,
    color_secondary    TEXT,
    age                TEXT,      -- baby | young | adult | senior
    gender             TEXT,
    size               TEXT,      -- small | medium | large | xlarge
    coat               TEXT,
    description        TEXT,
    status             TEXT,      -- adoptable | adopted | found
    spayed_neutered    INTEGER,
    house_trained      INTEGER,
    declawed           INTEGER,
    special_needs      INTEGER,
    shots_current      INTEGER,
    good_with_children INTEGER,
    good_with_dogs     INTEGER,
    good_with_cats     INTEGER,
    tags_json          TEXT,
    photo_count        INTEGER,
    video_count        INTEGER,
    primary_photo      TEXT,
    contact_city       TEXT,
    contact_state      TEXT,
    contact_postcode   TEXT,
    distance_miles     REAL,
    url                TEXT,
    published_at       TEXT,      -- from Petfinder; RESETS when a shelter relists
    first_published_at TEXT,      -- earliest published_at we've ever seen; never resets
    status_changed_at  TEXT,
    first_seen_at      TEXT NOT NULL,  -- first time WE saw it
    last_seen_at       TEXT NOT NULL,  -- most recent run it appeared in
    is_active          INTEGER NOT NULL DEFAULT 1,  -- 0 once it stops appearing
    left_listing_at    TEXT,      -- when it first went missing (adoption proxy)
    raw_json           TEXT,
    FOREIGN KEY (organization_id) REFERENCES organizations(organization_id)
);

CREATE INDEX IF NOT EXISTS ix_animals_org     ON animals(organization_id);
CREATE INDEX IF NOT EXISTS ix_animals_type    ON animals(type);
CREATE INDEX IF NOT EXISTS ix_animals_active  ON animals(is_active, status);
CREATE INDEX IF NOT EXISTS ix_animals_pub     ON animals(published_at);

-- One row per animal per run. Never updated, only inserted.
CREATE TABLE IF NOT EXISTS animal_snapshots (
    snapshot_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL,
    animal_id       INTEGER NOT NULL,
    observed_at     TEXT NOT NULL,
    status          TEXT,
    photo_count     INTEGER,
    description_len INTEGER,
    distance_miles  REAL,
    content_hash    TEXT,   -- detects silent edits to the listing
    FOREIGN KEY (run_id)    REFERENCES ingest_runs(run_id),
    FOREIGN KEY (animal_id) REFERENCES animals(animal_id)
);

CREATE INDEX IF NOT EXISTS ix_snap_animal ON animal_snapshots(animal_id, observed_at);
CREATE INDEX IF NOT EXISTS ix_snap_run    ON animal_snapshots(run_id);

-- Generated copy for at-risk animals, kept so you can diff prompt versions.
CREATE TABLE IF NOT EXISTS generated_content (
    content_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    animal_id     INTEGER NOT NULL,
    kind          TEXT NOT NULL,   -- bio | social_post | email_blurb
    channel       TEXT,            -- instagram | facebook | x | newsletter
    body          TEXT NOT NULL,
    model         TEXT,
    prompt_version TEXT,
    risk_score    REAL,
    created_at    TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    reviewed_at   TEXT,
    review_note   TEXT,
    FOREIGN KEY (animal_id) REFERENCES animals(animal_id)
);

CREATE INDEX IF NOT EXISTS ix_content_animal ON generated_content(animal_id, kind);

-- Outreach actions taken for an animal (featured post, approved copy published,
-- listing refresh...). Explicit, so their effect can be measured against matched
-- animals that got nothing.
CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    animal_id    INTEGER NOT NULL,
    kind         TEXT NOT NULL,        -- copy | feature | listing_refresh | event
    started_at   TEXT NOT NULL,
    content_id   INTEGER,              -- generated_content row, if copy-driven
    note         TEXT,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (animal_id) REFERENCES animals(animal_id)
);

CREATE INDEX IF NOT EXISTS ix_campaigns_animal ON campaigns(animal_id, started_at);

-- Adopter intake, so matches are reproducible and reviewable.
CREATE TABLE IF NOT EXISTS adopters (
    adopter_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT,
    prefs_json  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
    match_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    adopter_id  INTEGER NOT NULL,
    animal_id   INTEGER NOT NULL,
    rank        INTEGER,
    fit_score   REAL,
    rationale   TEXT,
    concerns    TEXT,
    model       TEXT,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (adopter_id) REFERENCES adopters(adopter_id),
    FOREIGN KEY (animal_id)  REFERENCES animals(animal_id)
);

-- What happened to a suggestion. The only way to find out whether the matcher is
-- useful rather than merely plausible.
CREATE TABLE IF NOT EXISTS match_outcomes (
    outcome_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    INTEGER NOT NULL,
    outcome     TEXT NOT NULL,   -- forwarded | met | adopted | declined_adopter | declined_shelter
    note        TEXT,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

CREATE INDEX IF NOT EXISTS ix_outcomes_match ON match_outcomes(match_id);

-- Convenience view: everything currently listed, with tenure in days.
--
-- Tenure starts at the earliest of: the first published_at we ever saw, and our own
-- first observation. Petfinder's published_at resets when a shelter relists an
-- animal; first_published_at and first_seen_at never do.
DROP VIEW IF EXISTS v_active_animals;
CREATE VIEW v_active_animals AS
SELECT
    a.*,
    MIN(COALESCE(a.first_published_at, a.published_at, a.first_seen_at), a.first_seen_at)
        AS listing_started_at,
    CAST(
        julianday('now')
        - julianday(MIN(COALESCE(a.first_published_at, a.published_at, a.first_seen_at),
                        a.first_seen_at))
        AS INTEGER
    ) AS days_listed,
    CASE WHEN a.published_at > a.first_published_at THEN 1 ELSE 0 END AS relisted
FROM animals a
WHERE a.is_active = 1 AND a.status = 'adoptable';
