-- Add distinct real UPSC domains to raju's Workspace (7bc8679f…), the one that
-- holds the clean UPSC example keyword set. Idempotent (UNIQUE(workspace_id,
-- domain) guard). Also seeds a primary `root` domain_host per new site, the way
-- migration 0013 does, so hosts stay populated.
--
-- Run:
--   psql -U root -h localhost -d seo_saas -f scripts/seed_upsc_sites.sql

BEGIN;

-- 1. domains -------------------------------------------------------------------
INSERT INTO domains (workspace_id, name, domain)
SELECT '7bc8679f-e307-4296-8efb-141daab5218f'::uuid, v.name, v.domain
FROM (VALUES
    ('Drishti IAS',     'drishtiias.com'),
    ('Vajiram & Ravi',  'vajiramandravi.com'),
    ('InsightsIAS',     'insightsonindia.com'),
    ('ForumIAS',        'forumias.com'),
    ('ClearIAS',        'clearias.com'),
    ('Testbook IAS',    'testbook.com'),
    ('NEXT IAS',        'nextias.com'),
    ('IASbaba',         'iasbaba.com')
   ) AS v(name, domain)
ON CONFLICT ON CONSTRAINT uq_workspace_domain DO NOTHING;

-- 2. domain_hosts (a primary root host per domain, matching migration 0013) ---
INSERT INTO domain_hosts (domain_id, host, host_type, is_primary)
SELECT w.id,
       regexp_replace(
           regexp_replace(lower(btrim(w.domain)), '^https?://(www\.)?', ''),
           '/.*$', ''),
       'root', TRUE
FROM domains w
WHERE w.workspace_id = '7bc8679f-e307-4296-8efb-141daab5218f'::uuid
ON CONFLICT (domain_id, host) DO NOTHING;

COMMIT;
