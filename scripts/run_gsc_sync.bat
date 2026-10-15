@echo off
REM Daily Google Search Console sync — real rankings into keyword_metrics.
cd /d D:\seo\backend
.venv\Scripts\python.exe -m app.jobs gsc_sync --triggered-by scheduler --country in --language en
