@echo off
REM Weekly SERP snapshot — top-priority, stale-or-missing keywords (cost-capped).
cd /d D:\seo\backend
.venv\Scripts\python.exe -m app.jobs serp_sync --triggered-by scheduler --country in --language en
