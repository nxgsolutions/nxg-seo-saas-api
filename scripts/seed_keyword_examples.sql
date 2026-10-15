-- Example records for the normalized keyword schema (migration 0013).
-- Idempotent: safe to run repeatedly (ON CONFLICT / NOT EXISTS guards).
-- Anchored to the oldest existing domain so FKs are always valid.
--
-- Run:
--   psql -U root -h localhost -d seo_saas -f scripts/seed_keyword_examples.sql

BEGIN;

-- 1. keywords ---------------------------------------------------------------
INSERT INTO keywords (keyword, normalized_keyword) VALUES
    ('SEO audit tool', 'seo audit tool'),
    ('Best keyword research tool', 'best keyword research tool')
ON CONFLICT (normalized_keyword) DO NOTHING;

-- 2. domain_keywords (link the two keywords to the anchor domain) ---------
INSERT INTO domain_keywords (domain_id, keyword_id, country_code, language_code)
SELECT (SELECT id FROM domains ORDER BY created_at LIMIT 1),
       k.id, 'us', 'en'
FROM keywords k
WHERE k.normalized_keyword IN ('seo audit tool', 'best keyword research tool')
ON CONFLICT ON CONSTRAINT uq_domain_keyword DO NOTHING;

-- 3. domain_hosts (a subdomain under the anchor domain's root host) --------
INSERT INTO domain_hosts (domain_id, host, host_type, parent_host_id, is_primary)
SELECT r.domain_id, 'blog.' || r.host, 'subdomain', r.id, FALSE
FROM domain_hosts r
WHERE r.host_type = 'root'
  AND r.domain_id = (SELECT id FROM domains ORDER BY created_at LIMIT 1)
ON CONFLICT (domain_id, host) DO NOTHING;

-- 4. keyword_metrics (market metrics for the two example keywords) ----------
INSERT INTO keyword_metrics
    (keyword_id, country_code, language_code, search_engine, device,
     search_volume, cpc, difficulty, competition, trend, trend_change_pct,
     intent, metric_date)
SELECT k.id, 'us', 'en', 'google', 'desktop',
       v.search_volume, v.cpc, v.difficulty, v.competition, v.trend, v.change_pct,
       v.intent, CURRENT_DATE
FROM keywords k
JOIN (VALUES
        ('seo audit tool',              22000::bigint, 3.10, 46.00, 0.62, 78.00,  12.0, 'commercial'),
        ('best keyword research tool',   8100::bigint, 4.75, 58.00, 0.71, 64.00,  -5.0, 'commercial')
     ) AS v(nkw, search_volume, cpc, difficulty, competition, trend, change_pct, intent)
  ON v.nkw = k.normalized_keyword
ON CONFLICT ON CONSTRAINT uq_keyword_metric DO NOTHING;

-- 5. keyword_rankings (rank of each example domain_keyword on the root host) -
INSERT INTO keyword_rankings
    (domain_keyword_id, host_id, position, impressions, clicks, ctr, ranking_date)
SELECT wk.id, h.id, v.position, v.impressions, v.clicks, v.ctr, CURRENT_DATE
FROM domain_keywords wk
JOIN keywords k ON k.id = wk.keyword_id
JOIN domain_hosts h ON h.domain_id = wk.domain_id AND h.host_type = 'root'
JOIN (VALUES
        ('seo audit tool',             8.40::numeric, 5400::bigint, 210::bigint, 0.0389),
        ('best keyword research tool', 14.20::numeric, 3100::bigint,  96::bigint, 0.0310)
     ) AS v(nkw, position, impressions, clicks, ctr)
  ON v.nkw = k.normalized_keyword
ON CONFLICT ON CONSTRAINT uq_keyword_ranking DO NOTHING;

-- 6. keyword_sources (provenance for the two example keywords) --------------
INSERT INTO keyword_sources (keyword_id, source_type, source_reference)
SELECT k.id, v.source_type, v.source_reference
FROM keywords k
JOIN (VALUES
        ('seo audit tool',             'seed',         'example seed'),
        ('best keyword research tool', 'autocomplete', 'google suggest')
     ) AS v(nkw, source_type, source_reference)
  ON v.nkw = k.normalized_keyword
WHERE NOT EXISTS (
    SELECT 1 FROM keyword_sources s
    WHERE s.keyword_id = k.id AND s.source_type = v.source_type
);

-- 7. keyword_relations (the two example keywords are related) ---------------
INSERT INTO keyword_relations (keyword_id, related_keyword_id, relation_type, score)
SELECT a.id, b.id, 'related', 0.8200
FROM keywords a, keywords b
WHERE a.normalized_keyword = 'seo audit tool'
  AND b.normalized_keyword = 'best keyword research tool'
ON CONFLICT ON CONSTRAINT uq_keyword_relation DO NOTHING;

-- 8. keyword_clusters (a topic cluster on the anchor domain) ---------------
INSERT INTO keyword_clusters (domain_id, name, description)
VALUES
    ((SELECT id FROM domains ORDER BY created_at LIMIT 1),
     'SEO Tools', 'Example cluster of tool-intent keywords')
ON CONFLICT ON CONSTRAINT uq_cluster_name DO NOTHING;

-- 9. keyword_cluster_members (put the example domain_keywords in the cluster) -
INSERT INTO keyword_cluster_members (cluster_id, domain_keyword_id, score)
SELECT c.id, wk.id, 0.9000
FROM keyword_clusters c
JOIN domain_keywords wk
  ON wk.domain_id = c.domain_id
JOIN keywords k ON k.id = wk.keyword_id
WHERE c.name = 'SEO Tools'
  AND k.normalized_keyword IN ('seo audit tool', 'best keyword research tool')
ON CONFLICT ON CONSTRAINT uq_cluster_member DO NOTHING;

-- 10. keyword_researches (a completed run seeded from an example keyword) ----
INSERT INTO keyword_researches
    (domain_id, seed_keyword_id, country_code, language_code, status, completed_at)
SELECT (SELECT id FROM domains ORDER BY created_at LIMIT 1),
       k.id, 'us', 'en', 'completed', now()
FROM keywords k
WHERE k.normalized_keyword = 'seo audit tool'
  AND NOT EXISTS (
      SELECT 1 FROM keyword_researches r
      WHERE r.seed_keyword_id = k.id
        AND r.domain_id = (SELECT id FROM domains ORDER BY created_at LIMIT 1)
  );

-- 11. keyword_research_results (ranked output of that example run) -----------
INSERT INTO keyword_research_results (research_id, keyword_id, rank)
SELECT r.id, k.id, v.rank
FROM keyword_researches r
JOIN keywords seed ON seed.id = r.seed_keyword_id AND seed.normalized_keyword = 'seo audit tool'
JOIN (VALUES
        ('seo audit tool', 1),
        ('best keyword research tool', 2)
     ) AS v(nkw, rank) ON TRUE
JOIN keywords k ON k.normalized_keyword = v.nkw
ON CONFLICT ON CONSTRAINT uq_research_result DO NOTHING;

COMMIT;
