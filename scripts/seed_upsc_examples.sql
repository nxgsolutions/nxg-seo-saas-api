-- UPSC / iaspirants.com example data for the normalized keyword schema (0013).
-- Idempotent: safe to re-run. Anchored to the earliest iaspirants.com domain,
-- India market (country 'in', language 'en').
--
-- Run:
--   psql -U root -h localhost -d seo_saas -f scripts/seed_upsc_examples.sql

BEGIN;

-- Anchor domain (earliest iaspirants.com).
CREATE TEMP TABLE _site ON COMMIT DROP AS
SELECT id FROM domains WHERE domain = 'iaspirants.com' ORDER BY created_at LIMIT 1;

-- 1. keywords -------------------------------------------------------------------
INSERT INTO keywords (keyword, normalized_keyword) VALUES
    ('UPSC PYQs',                            'upsc pyqs'),
    ('UPSC CSE notes',                       'upsc cse notes'),
    ('UPSC prelims previous year questions', 'upsc prelims previous year questions'),
    ('UPSC mains PYQ',                       'upsc mains pyq'),
    ('UPSC CSAT PYQ',                        'upsc csat pyq'),
    ('UPSC syllabus',                        'upsc syllabus'),
    ('NCERT notes for UPSC',                 'ncert notes for upsc'),
    ('UPSC optional subjects notes',         'upsc optional subjects notes'),
    ('UPSC current affairs',                 'upsc current affairs'),
    ('Best notes for UPSC preparation',      'best notes for upsc preparation')
ON CONFLICT (normalized_keyword) DO NOTHING;

-- 2. domain_keywords (target all UPSC keywords for iaspirants, India) ----------
INSERT INTO domain_keywords (domain_id, keyword_id, country_code, language_code)
SELECT (SELECT id FROM _site), k.id, 'in', 'en'
FROM keywords k
WHERE k.normalized_keyword IN (
    'upsc pyqs','upsc cse notes','upsc prelims previous year questions','upsc mains pyq',
    'upsc csat pyq','upsc syllabus','ncert notes for upsc','upsc optional subjects notes',
    'upsc current affairs','best notes for upsc preparation')
ON CONFLICT ON CONSTRAINT uq_domain_keyword DO NOTHING;

-- 3. domain_hosts (a topical subdomain under the iaspirants root host) ---------
INSERT INTO domain_hosts (domain_id, host, host_type, parent_host_id, is_primary)
SELECT r.domain_id, 'currentaffairs.' || r.host, 'subdomain', r.id, FALSE
FROM domain_hosts r
WHERE r.host_type = 'root' AND r.domain_id = (SELECT id FROM _site)
ON CONFLICT (domain_id, host) DO NOTHING;

-- 4. keyword_metrics (India market metrics, today) -----------------------------
INSERT INTO keyword_metrics
    (keyword_id, country_code, language_code, search_engine, device,
     search_volume, cpc, difficulty, competition, trend, trend_change_pct,
     intent, metric_date)
SELECT k.id, 'in', 'en', 'google', 'desktop',
       v.vol, v.cpc, v.kd, v.comp, v.trend, v.chg, v.intent, CURRENT_DATE
FROM keywords k
JOIN (VALUES
    ('upsc pyqs',                            33100::bigint, 2.10, 42.00, 0.55, 88.00,  9.0, 'informational'),
    ('upsc cse notes',                       12100::bigint, 1.80, 38.00, 0.48, 72.00,  4.0, 'informational'),
    ('upsc prelims previous year questions', 14800::bigint, 1.50, 40.00, 0.50, 76.00,  6.0, 'informational'),
    ('upsc mains pyq',                        9900::bigint, 1.40, 39.00, 0.47, 70.00,  3.0, 'informational'),
    ('upsc csat pyq',                         8100::bigint, 1.30, 35.00, 0.44, 66.00,  7.0, 'informational'),
    ('upsc syllabus',                        60500::bigint, 1.10, 48.00, 0.40, 84.00,  2.0, 'informational'),
    ('ncert notes for upsc',                  6600::bigint, 1.60, 33.00, 0.42, 61.00,  5.0, 'informational'),
    ('upsc optional subjects notes',          5400::bigint, 1.70, 36.00, 0.45, 58.00, -2.0, 'informational'),
    ('upsc current affairs',                 49500::bigint, 1.20, 52.00, 0.58, 90.00, 12.0, 'informational'),
    ('best notes for upsc preparation',       2900::bigint, 2.60, 44.00, 0.66, 55.00,  1.0, 'commercial')
   ) AS v(nkw, vol, cpc, kd, comp, trend, chg, intent) ON v.nkw = k.normalized_keyword
ON CONFLICT ON CONSTRAINT uq_keyword_metric DO NOTHING;

-- 5. keyword_rankings (iaspirants position on its root host, today) ------------
INSERT INTO keyword_rankings
    (domain_keyword_id, host_id, position, impressions, clicks, ctr, ranking_date)
SELECT wk.id, h.id, v.position, v.impressions, v.clicks, v.ctr, CURRENT_DATE
FROM domain_keywords wk
JOIN keywords k ON k.id = wk.keyword_id
JOIN domain_hosts h ON h.domain_id = wk.domain_id AND h.host_type = 'root'
JOIN (VALUES
    ('upsc pyqs',                             6.20::numeric, 18400::bigint, 1120::bigint, 0.0609),
    ('upsc cse notes',                        9.40::numeric,  8600::bigint,  430::bigint, 0.0500),
    ('upsc prelims previous year questions', 11.80::numeric,  6200::bigint,  240::bigint, 0.0387),
    ('ncert notes for upsc',                  4.50::numeric,  5100::bigint,  690::bigint, 0.1353)
   ) AS v(nkw, position, impressions, clicks, ctr) ON v.nkw = k.normalized_keyword
WHERE wk.domain_id = (SELECT id FROM _site)
ON CONFLICT ON CONSTRAINT uq_keyword_ranking DO NOTHING;

-- 6. keyword_sources (provenance) ----------------------------------------------
INSERT INTO keyword_sources (keyword_id, source_type, source_reference)
SELECT k.id, v.stype, v.sref
FROM keywords k
JOIN (VALUES
    ('upsc pyqs',                            'seed',         'upsc pyqs'),
    ('upsc cse notes',                       'seed',         'upsc cse notes'),
    ('upsc prelims previous year questions', 'autocomplete', 'google suggest: upsc prelims'),
    ('upsc mains pyq',                       'autocomplete', 'google suggest: upsc mains'),
    ('upsc csat pyq',                        'autocomplete', 'google suggest: upsc csat'),
    ('ncert notes for upsc',                 'autocomplete', 'google suggest: ncert notes'),
    ('upsc current affairs',                 'gsc',          'search console query'),
    ('upsc syllabus',                        'gsc',          'search console query')
   ) AS v(nkw, stype, sref) ON v.nkw = k.normalized_keyword
WHERE NOT EXISTS (
    SELECT 1 FROM keyword_sources s WHERE s.keyword_id = k.id AND s.source_type = v.stype
);

-- 7. keyword_relations (PYQ variants + notes group) ----------------------------
INSERT INTO keyword_relations (keyword_id, related_keyword_id, relation_type, score)
SELECT a.id, b.id, v.rtype, v.score
FROM (VALUES
    ('upsc pyqs',      'upsc prelims previous year questions', 'variant', 0.9100),
    ('upsc pyqs',      'upsc mains pyq',                       'variant', 0.8800),
    ('upsc pyqs',      'upsc csat pyq',                        'variant', 0.8400),
    ('upsc cse notes', 'ncert notes for upsc',                'related', 0.8000),
    ('upsc cse notes', 'upsc optional subjects notes',        'related', 0.7600)
   ) AS v(a_nkw, b_nkw, rtype, score)
JOIN keywords a ON a.normalized_keyword = v.a_nkw
JOIN keywords b ON b.normalized_keyword = v.b_nkw
ON CONFLICT ON CONSTRAINT uq_keyword_relation DO NOTHING;

-- 8. keyword_clusters -----------------------------------------------------------
INSERT INTO keyword_clusters (domain_id, name, description)
SELECT (SELECT id FROM _site), v.name, v.descr
FROM (VALUES
    ('UPSC PYQs',  'Previous-year-questions intent keywords'),
    ('UPSC Notes', 'Study-notes intent keywords')
   ) AS v(name, descr)
ON CONFLICT ON CONSTRAINT uq_cluster_name DO NOTHING;

-- 9. keyword_cluster_members ----------------------------------------------------
INSERT INTO keyword_cluster_members (cluster_id, domain_keyword_id, score)
SELECT c.id, wk.id, 0.9000
FROM keyword_clusters c
JOIN domain_keywords wk ON wk.domain_id = c.domain_id
JOIN keywords k ON k.id = wk.keyword_id
JOIN (VALUES
    ('UPSC PYQs',  'upsc pyqs'),
    ('UPSC PYQs',  'upsc prelims previous year questions'),
    ('UPSC PYQs',  'upsc mains pyq'),
    ('UPSC PYQs',  'upsc csat pyq'),
    ('UPSC Notes', 'upsc cse notes'),
    ('UPSC Notes', 'ncert notes for upsc'),
    ('UPSC Notes', 'upsc optional subjects notes'),
    ('UPSC Notes', 'best notes for upsc preparation')
   ) AS m(cname, nkw) ON m.cname = c.name AND m.nkw = k.normalized_keyword
WHERE c.domain_id = (SELECT id FROM _site)
ON CONFLICT ON CONSTRAINT uq_cluster_member DO NOTHING;

-- 10. keyword_researches (a completed run seeded from 'upsc pyqs') --------------
INSERT INTO keyword_researches
    (domain_id, seed_keyword_id, country_code, language_code, status, completed_at)
SELECT (SELECT id FROM _site), k.id, 'in', 'en', 'completed', now()
FROM keywords k
WHERE k.normalized_keyword = 'upsc pyqs'
  AND NOT EXISTS (
      SELECT 1 FROM keyword_researches r
      WHERE r.seed_keyword_id = k.id AND r.domain_id = (SELECT id FROM _site)
  );

-- 11. keyword_research_results (ranked output of that run) ----------------------
INSERT INTO keyword_research_results (research_id, keyword_id, rank)
SELECT r.id, k.id, v.rank
FROM keyword_researches r
JOIN keywords seed ON seed.id = r.seed_keyword_id AND seed.normalized_keyword = 'upsc pyqs'
JOIN (VALUES
    ('upsc pyqs', 1), ('upsc prelims previous year questions', 2), ('upsc mains pyq', 3),
    ('upsc csat pyq', 4), ('upsc cse notes', 5), ('ncert notes for upsc', 6),
    ('upsc current affairs', 7), ('upsc syllabus', 8), ('upsc optional subjects notes', 9),
    ('best notes for upsc preparation', 10)
   ) AS v(nkw, rank) ON TRUE
JOIN keywords k ON k.normalized_keyword = v.nkw
WHERE r.domain_id = (SELECT id FROM _site)
ON CONFLICT ON CONSTRAINT uq_research_result DO NOTHING;

COMMIT;
