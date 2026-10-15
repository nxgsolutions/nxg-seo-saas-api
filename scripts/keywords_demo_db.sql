-- ============================================================================
-- Standalone keyword schema (matches the pasted DBML) + UPSC example data.
-- Target: a SEPARATE database (seo_keywords_demo), independent of seo_saas.
-- Run AFTER creating the database:
--   psql -U root -h localhost -d seo_keywords_demo -f scripts/keywords_demo_db.sql
--
-- Notes:
--  * `domains` is included as the FK anchor (the DBML references domains.id).
--  * UUID PKs via gen_random_uuid() (built into PostgreSQL 13+), TIMESTAMPTZ.
--  * Composite UNIQUE + CHECK constraints and FK indexes are included (from the
--    original detailed spec; the DBML diagram omits them for readability).
-- ============================================================================

BEGIN;

-- ── FK anchor ───────────────────────────────────────────────────────────────
CREATE TABLE domains (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       VARCHAR(200) NOT NULL,
    domain     VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_domain_domain UNIQUE (domain)
);

-- ── 1. keywords ──────────────────────────────────────────────────────────────
CREATE TABLE keywords (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    keyword            TEXT NOT NULL,
    normalized_keyword TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_keywords_normalized UNIQUE (normalized_keyword)
);

-- ── 2. domain_keywords ──────────────────────────────────────────────────────
CREATE TABLE domain_keywords (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain_id    UUID NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    keyword_id    UUID NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    country_code  VARCHAR(2),
    language_code VARCHAR(10),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_domain_keyword UNIQUE (domain_id, keyword_id, country_code, language_code)
);
CREATE INDEX ix_domain_keywords_domain_id ON domain_keywords (domain_id);
CREATE INDEX ix_domain_keywords_keyword_id ON domain_keywords (keyword_id);

-- ── 3. domain_hosts ─────────────────────────────────────────────────────────
CREATE TABLE domain_hosts (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain_id     UUID NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    host           TEXT NOT NULL,
    host_type      VARCHAR(20) NOT NULL DEFAULT 'root',
    parent_host_id UUID REFERENCES domain_hosts(id) ON DELETE CASCADE,
    is_primary     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_domain_host UNIQUE (domain_id, host),
    CONSTRAINT ck_host_type CHECK (host_type IN ('root', 'subdomain'))
);
CREATE INDEX ix_domain_hosts_domain_id ON domain_hosts (domain_id);
CREATE INDEX ix_domain_hosts_parent_host_id ON domain_hosts (parent_host_id);

-- ── 4. keyword_metrics (market metrics — clean schema) ───────────────────────
CREATE TABLE keyword_metrics (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    keyword_id       UUID NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    country_code     VARCHAR(2),
    language_code    VARCHAR(10),
    search_engine    VARCHAR(50),
    device           VARCHAR(20),
    search_volume    BIGINT,
    cpc              NUMERIC(12, 4),
    difficulty       NUMERIC(5, 2),
    competition      NUMERIC(5, 2),
    trend            NUMERIC(12, 4),
    trend_change_pct NUMERIC(12, 4),
    metric_date      DATE NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_keyword_metric
        UNIQUE (keyword_id, country_code, language_code, search_engine, device, metric_date)
);
CREATE INDEX ix_keyword_metrics_keyword_id ON keyword_metrics (keyword_id);

-- ── 5. keyword_rankings ──────────────────────────────────────────────────────
CREATE TABLE keyword_rankings (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain_keyword_id UUID NOT NULL REFERENCES domain_keywords(id) ON DELETE CASCADE,
    host_id            UUID NOT NULL REFERENCES domain_hosts(id) ON DELETE CASCADE,
    position           NUMERIC(8, 2),
    impressions        BIGINT,
    clicks             BIGINT,
    ctr                NUMERIC(8, 4),
    ranking_date       DATE NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_keyword_ranking UNIQUE (domain_keyword_id, host_id, ranking_date)
);
CREATE INDEX ix_keyword_rankings_domain_keyword_id ON keyword_rankings (domain_keyword_id);
CREATE INDEX ix_keyword_rankings_host_id ON keyword_rankings (host_id);

-- ── 6. keyword_sources ───────────────────────────────────────────────────────
CREATE TABLE keyword_sources (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    keyword_id       UUID NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    source_type      VARCHAR(50) NOT NULL,
    source_reference TEXT,
    captured_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_keyword_sources_keyword_id ON keyword_sources (keyword_id);

-- ── 7. keyword_relations ─────────────────────────────────────────────────────
CREATE TABLE keyword_relations (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    keyword_id         UUID NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    related_keyword_id UUID NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    relation_type      VARCHAR(50) NOT NULL,
    score              NUMERIC(8, 4),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_keyword_relation UNIQUE (keyword_id, related_keyword_id, relation_type),
    CONSTRAINT ck_relation_not_self CHECK (keyword_id <> related_keyword_id)
);
CREATE INDEX ix_keyword_relations_keyword_id ON keyword_relations (keyword_id);
CREATE INDEX ix_keyword_relations_related_keyword_id ON keyword_relations (related_keyword_id);

-- ── 8. keyword_clusters ──────────────────────────────────────────────────────
CREATE TABLE keyword_clusters (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain_id        UUID NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    name              VARCHAR(255) NOT NULL,
    description       TEXT,
    parent_cluster_id UUID REFERENCES keyword_clusters(id) ON DELETE SET NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_cluster_name UNIQUE (domain_id, name)
);
CREATE INDEX ix_keyword_clusters_domain_id ON keyword_clusters (domain_id);
CREATE INDEX ix_keyword_clusters_parent_cluster_id ON keyword_clusters (parent_cluster_id);

-- ── 9. keyword_cluster_members ───────────────────────────────────────────────
CREATE TABLE keyword_cluster_members (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cluster_id         UUID NOT NULL REFERENCES keyword_clusters(id) ON DELETE CASCADE,
    domain_keyword_id UUID NOT NULL REFERENCES domain_keywords(id) ON DELETE CASCADE,
    score              NUMERIC(8, 4),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_cluster_member UNIQUE (cluster_id, domain_keyword_id)
);
CREATE INDEX ix_keyword_cluster_members_cluster_id ON keyword_cluster_members (cluster_id);
CREATE INDEX ix_keyword_cluster_members_domain_keyword_id ON keyword_cluster_members (domain_keyword_id);

-- ── 10. keyword_researches ───────────────────────────────────────────────────
CREATE TABLE keyword_researches (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain_id      UUID NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    seed_keyword_id UUID NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    country_code    VARCHAR(2),
    language_code   VARCHAR(10),
    status          VARCHAR(30) NOT NULL DEFAULT 'pending',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);
CREATE INDEX ix_keyword_researches_domain_id ON keyword_researches (domain_id);
CREATE INDEX ix_keyword_researches_seed_keyword_id ON keyword_researches (seed_keyword_id);

-- ── 11. keyword_research_results ─────────────────────────────────────────────
CREATE TABLE keyword_research_results (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    research_id UUID NOT NULL REFERENCES keyword_researches(id) ON DELETE CASCADE,
    keyword_id  UUID NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    rank        INTEGER,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_research_result UNIQUE (research_id, keyword_id)
);
CREATE INDEX ix_keyword_research_results_research_id ON keyword_research_results (research_id);
CREATE INDEX ix_keyword_research_results_keyword_id ON keyword_research_results (keyword_id);

-- ============================================================================
-- EXAMPLE DATA (UPSC / iaspirants theme, India market)
-- ============================================================================

-- domains
INSERT INTO domains (name, domain) VALUES
    ('iAspirants', 'iaspirants.com'),
    ('Drishti IAS', 'drishtiias.com');

-- keywords
INSERT INTO keywords (keyword, normalized_keyword) VALUES
    ('UPSC PYQs',                            'upsc pyqs'),
    ('UPSC CSE notes',                       'upsc cse notes'),
    ('UPSC prelims previous year questions', 'upsc prelims previous year questions'),
    ('UPSC mains PYQ',                       'upsc mains pyq'),
    ('UPSC syllabus',                        'upsc syllabus'),
    ('NCERT notes for UPSC',                 'ncert notes for upsc'),
    ('UPSC current affairs',                 'upsc current affairs'),
    ('Best notes for UPSC preparation',      'best notes for upsc preparation');

-- domain_keywords (iaspirants targets all, India)
INSERT INTO domain_keywords (domain_id, keyword_id, country_code, language_code)
SELECT (SELECT id FROM domains WHERE domain='iaspirants.com'), k.id, 'in', 'en'
FROM keywords k;

-- domain_hosts (root + a subdomain for iaspirants; root for drishti)
INSERT INTO domain_hosts (domain_id, host, host_type, is_primary)
SELECT id, domain, 'root', TRUE FROM domains;
INSERT INTO domain_hosts (domain_id, host, host_type, parent_host_id, is_primary)
SELECT r.domain_id, 'currentaffairs.' || r.host, 'subdomain', r.id, FALSE
FROM domain_hosts r
WHERE r.host_type='root' AND r.domain_id=(SELECT id FROM domains WHERE domain='iaspirants.com');

-- keyword_metrics (India market metrics, today)
INSERT INTO keyword_metrics
    (keyword_id, country_code, language_code, search_engine, device,
     search_volume, cpc, difficulty, competition, trend, trend_change_pct, metric_date)
SELECT k.id, 'in', 'en', 'google', 'desktop',
       v.vol, v.cpc, v.kd, v.comp, v.trend, v.chg, CURRENT_DATE
FROM keywords k
JOIN (VALUES
    ('upsc pyqs',                            33100::bigint, 2.10, 42.00, 0.55, 88.00,  9.0),
    ('upsc cse notes',                       12100::bigint, 1.80, 38.00, 0.48, 72.00,  4.0),
    ('upsc prelims previous year questions', 14800::bigint, 1.50, 40.00, 0.50, 76.00,  6.0),
    ('upsc mains pyq',                        9900::bigint, 1.40, 39.00, 0.47, 70.00,  3.0),
    ('upsc syllabus',                        60500::bigint, 1.10, 48.00, 0.40, 84.00,  2.0),
    ('ncert notes for upsc',                  6600::bigint, 1.60, 33.00, 0.42, 61.00,  5.0),
    ('upsc current affairs',                 49500::bigint, 1.20, 52.00, 0.58, 90.00, 12.0),
    ('best notes for upsc preparation',       2900::bigint, 2.60, 44.00, 0.66, 55.00,  1.0)
   ) AS v(nkw, vol, cpc, kd, comp, trend, chg) ON v.nkw = k.normalized_keyword;

-- keyword_rankings (iaspirants position on its root host, today)
INSERT INTO keyword_rankings
    (domain_keyword_id, host_id, position, impressions, clicks, ctr, ranking_date)
SELECT wk.id, h.id, v.position, v.impressions, v.clicks, v.ctr, CURRENT_DATE
FROM domain_keywords wk
JOIN keywords k ON k.id = wk.keyword_id
JOIN domain_hosts h ON h.domain_id = wk.domain_id AND h.host_type='root'
JOIN (VALUES
    ('upsc pyqs',              6.20::numeric, 18400::bigint, 1120::bigint, 0.0609),
    ('upsc cse notes',         9.40::numeric,  8600::bigint,  430::bigint, 0.0500),
    ('ncert notes for upsc',   4.50::numeric,  5100::bigint,  690::bigint, 0.1353)
   ) AS v(nkw, position, impressions, clicks, ctr) ON v.nkw = k.normalized_keyword;

-- keyword_sources
INSERT INTO keyword_sources (keyword_id, source_type, source_reference)
SELECT k.id, v.stype, v.sref
FROM keywords k
JOIN (VALUES
    ('upsc pyqs',            'seed',         'upsc pyqs'),
    ('upsc cse notes',       'seed',         'upsc cse notes'),
    ('upsc current affairs', 'gsc',          'search console query'),
    ('ncert notes for upsc', 'autocomplete', 'google suggest: ncert notes')
   ) AS v(nkw, stype, sref) ON v.nkw = k.normalized_keyword;

-- keyword_relations
INSERT INTO keyword_relations (keyword_id, related_keyword_id, relation_type, score)
SELECT a.id, b.id, v.rtype, v.score
FROM (VALUES
    ('upsc pyqs',      'upsc prelims previous year questions', 'variant', 0.9100),
    ('upsc pyqs',      'upsc mains pyq',                       'variant', 0.8800),
    ('upsc cse notes', 'ncert notes for upsc',                'related', 0.8000)
   ) AS v(a_nkw, b_nkw, rtype, score)
JOIN keywords a ON a.normalized_keyword = v.a_nkw
JOIN keywords b ON b.normalized_keyword = v.b_nkw;

-- keyword_clusters
INSERT INTO keyword_clusters (domain_id, name, description)
SELECT (SELECT id FROM domains WHERE domain='iaspirants.com'), v.name, v.descr
FROM (VALUES
    ('UPSC PYQs',  'Previous-year-questions intent keywords'),
    ('UPSC Notes', 'Study-notes intent keywords')
   ) AS v(name, descr);

-- keyword_cluster_members
INSERT INTO keyword_cluster_members (cluster_id, domain_keyword_id, score)
SELECT c.id, wk.id, 0.9000
FROM keyword_clusters c
JOIN domain_keywords wk ON wk.domain_id = c.domain_id
JOIN keywords k ON k.id = wk.keyword_id
JOIN (VALUES
    ('UPSC PYQs',  'upsc pyqs'),
    ('UPSC PYQs',  'upsc prelims previous year questions'),
    ('UPSC PYQs',  'upsc mains pyq'),
    ('UPSC Notes', 'upsc cse notes'),
    ('UPSC Notes', 'ncert notes for upsc'),
    ('UPSC Notes', 'best notes for upsc preparation')
   ) AS m(cname, nkw) ON m.cname=c.name AND m.nkw=k.normalized_keyword;

-- keyword_researches (seeded from 'upsc pyqs', completed)
INSERT INTO keyword_researches
    (domain_id, seed_keyword_id, country_code, language_code, status, completed_at)
SELECT (SELECT id FROM domains WHERE domain='iaspirants.com'),
       (SELECT id FROM keywords WHERE normalized_keyword='upsc pyqs'),
       'in', 'en', 'completed', now();

-- keyword_research_results (ranked output)
INSERT INTO keyword_research_results (research_id, keyword_id, rank)
SELECT r.id, k.id, v.rank
FROM keyword_researches r
JOIN (VALUES
    ('upsc pyqs', 1), ('upsc prelims previous year questions', 2), ('upsc mains pyq', 3),
    ('upsc cse notes', 4), ('ncert notes for upsc', 5), ('upsc current affairs', 6),
    ('upsc syllabus', 7), ('best notes for upsc preparation', 8)
   ) AS v(nkw, rank) ON TRUE
JOIN keywords k ON k.normalized_keyword = v.nkw;

COMMIT;
