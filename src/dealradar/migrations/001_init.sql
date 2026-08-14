-- DealRadar initial schema (ARCHITEKTURA.md section 5).
-- This file is applied verbatim via sqlite3's executescript — SQLite-specific syntax is allowed here
-- (and only here); db.py's own query code avoids it so a future Postgres move is a connection-string change.

CREATE TABLE source (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL CHECK (kind IN ('api', 'feed', 'rss')),
    base_url    TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    risk_prior  REAL NOT NULL DEFAULT 0.0 CHECK (risk_prior >= 0.0 AND risk_prior <= 1.0)
);

CREATE TABLE raw_offer (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     INTEGER NOT NULL REFERENCES source(id),
    external_id   TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    content_hash  TEXT NOT NULL
);
-- Lets a collector skip re-inserting a record whose payload hasn't changed since the last run.
CREATE INDEX idx_raw_offer_source_hash ON raw_offer (source_id, content_hash);

CREATE TABLE category (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id  INTEGER REFERENCES category(id),
    name       TEXT NOT NULL,
    path       TEXT NOT NULL UNIQUE
);

CREATE TABLE product (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_title  TEXT NOT NULL,
    brand            TEXT,
    mpn              TEXT,
    ean              TEXT,
    category_id      INTEGER REFERENCES category(id),
    created_at       TEXT NOT NULL
);

CREATE TABLE product_attr (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      INTEGER NOT NULL REFERENCES product(id),
    "key"           TEXT NOT NULL,
    value_num       REAL,
    value_text      TEXT,
    unit            TEXT,
    extracted_from  TEXT CHECK (extracted_from IN ('title', 'feed_field', 'description'))
);
CREATE INDEX idx_product_attr_product ON product_attr (product_id);

CREATE TABLE offer (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id            INTEGER NOT NULL REFERENCES source(id),
    external_id          TEXT NOT NULL,
    first_seen           TEXT NOT NULL,
    last_seen            TEXT NOT NULL,
    title                TEXT NOT NULL,
    title_norm           TEXT,
    brand                TEXT,
    brand_norm           TEXT,
    mpn                  TEXT,
    mpn_norm             TEXT,
    ean                  TEXT,
    category_raw         TEXT,
    category_id          INTEGER REFERENCES category(id),
    url                  TEXT NOT NULL,
    seller               TEXT,
    seller_rating        REAL,
    condition            TEXT CHECK (condition IN ('new', 'refurb', 'used', 'damaged')),
    is_bundle            INTEGER NOT NULL DEFAULT 0,
    bundle_size          INTEGER,
    price_gross          INTEGER NOT NULL,
    shipping_cost        INTEGER,
    price_total          INTEGER NOT NULL,
    currency             TEXT NOT NULL DEFAULT 'PLN',
    price_incomplete     INTEGER NOT NULL DEFAULT 0,
    list_price_claimed   INTEGER,
    omnibus_min_30d      INTEGER,
    rating_avg           REAL,
    rating_count         INTEGER,
    warranty_months      INTEGER,
    in_stock             INTEGER,
    product_id           INTEGER REFERENCES product(id),
    match_confidence     REAL,
    match_method         TEXT CHECK (match_method IN ('ean', 'brand_mpn', 'fuzzy', 'manual', 'none')),
    data_issues          TEXT NOT NULL DEFAULT '[]',
    evidence             TEXT NOT NULL DEFAULT '{}',
    UNIQUE (source_id, external_id)
);
-- Required from day one (ARCHITEKTURA.md §5 / A0 spec) — without these the system stalls at ~100k offers.
CREATE INDEX idx_offer_product ON offer (product_id);
CREATE UNIQUE INDEX idx_offer_source_external ON offer (source_id, external_id);
CREATE INDEX idx_offer_ean ON offer (ean) WHERE ean IS NOT NULL;
CREATE INDEX idx_offer_brand_category ON offer (brand_norm, category_id);

CREATE TABLE price_point (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id      INTEGER NOT NULL REFERENCES offer(id),
    observed_at   TEXT NOT NULL,
    price_total   INTEGER NOT NULL,
    in_stock      INTEGER NOT NULL
);
CREATE INDEX idx_price_point_offer_observed ON price_point (offer_id, observed_at);
-- Enforces "at most one price_point per offer per day" at the database level, not just in application code.
CREATE UNIQUE INDEX idx_price_point_offer_day ON price_point (offer_id, date(observed_at));

CREATE TABLE deal (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id              INTEGER NOT NULL REFERENCES offer(id),
    computed_at           TEXT NOT NULL,
    basis                 TEXT NOT NULL CHECK (basis IN ('history', 'cross_shop')),
    deal_score            REAL NOT NULL,
    depth_vs_median_90d   REAL,
    z_robust              REAL,
    price_rank_today      INTEGER,
    is_alltime_low        INTEGER NOT NULL DEFAULT 0,
    n_competing_offers    INTEGER NOT NULL DEFAULT 0,
    n_days_history        INTEGER NOT NULL DEFAULT 0,
    coverage_cap          REAL,
    evidence_json         TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_deal_offer ON deal (offer_id);
CREATE INDEX idx_deal_computed_at ON deal (computed_at);

CREATE TABLE quality_score (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id          INTEGER NOT NULL REFERENCES product(id),
    computed_at         TEXT NOT NULL,
    peer_group_id       TEXT,
    value_score         REAL,
    reliability_score   REAL,
    deal_strength       REAL,
    risk_score          REAL,
    total_1_10          REAL,
    confidence          TEXT CHECK (confidence IN ('high', 'medium', 'low')),
    missing_reason      TEXT,
    evidence_json       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_quality_score_product ON quality_score (product_id);

CREATE TABLE shop_credibility (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id              INTEGER NOT NULL REFERENCES source(id),
    period                 TEXT NOT NULL,
    claimed_discount_avg   REAL,
    real_discount_avg      REAL,
    bullshit_index         REAL,
    n_offers               INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source_id, period)
);

CREATE TABLE match_review (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_a         INTEGER NOT NULL REFERENCES offer(id),
    offer_b         INTEGER NOT NULL REFERENCES offer(id),
    combined_score  REAL,
    method          TEXT CHECK (method IN ('ean', 'brand_mpn', 'fuzzy', 'manual', 'none')),
    label           TEXT CHECK (label IN ('same', 'different', 'conflict', 'unlabeled')),
    labeled_at      TEXT
);
CREATE INDEX idx_match_review_pair ON match_review (offer_a, offer_b);

CREATE TABLE watch (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id       INTEGER REFERENCES product(id),
    query            TEXT,
    max_price        INTEGER,
    min_deal_score   REAL,
    channel          TEXT NOT NULL DEFAULT 'console' CHECK (channel IN ('console', 'file', 'smtp', 'telegram')),
    active           INTEGER NOT NULL DEFAULT 1
);

-- Z7: every CLI command ends by writing a run report here (see db.run_report / models.RunReport).
CREATE TABLE run_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    stage             TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    offers_fetched    INTEGER NOT NULL DEFAULT 0,
    offers_new        INTEGER NOT NULL DEFAULT 0,
    pct_with_ean      REAL,
    pct_matched       REAL,
    products_new      INTEGER NOT NULL DEFAULT 0,
    deals_scored      INTEGER NOT NULL DEFAULT 0,
    quality_scored    INTEGER NOT NULL DEFAULT 0,
    sources_failed    TEXT NOT NULL DEFAULT '[]',
    duration_s        REAL,
    status            TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'partial', 'failed')),
    notes             TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX idx_run_log_stage_started ON run_log (stage, started_at);
