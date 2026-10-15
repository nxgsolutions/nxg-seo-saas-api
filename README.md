# nxg-seo-saas-api

FastAPI + PostgreSQL backend for the Enterprise SEO + AI Search SaaS. The
Next.js dashboard that consumes this API lives in
[`nxg-seo-saas`](https://github.com/nxgsolutions/nxg-seo-saas).

- **Framework:** FastAPI (`app/main.py`), served with Uvicorn
- **Database:** PostgreSQL via SQLAlchemy 2.0 + Alembic migrations (psycopg v3 driver)
- **Python:** 3.11+
- **API docs:** http://localhost:8000/docs · Health check: http://localhost:8000/health

All routes are mounted under `API_V1_PREFIX` (default `/api`).

---

## Features

Layered per feature (route → schema → service → model). Every external provider
is optional — when its key is unset the feature returns clearly-labelled sample
data instead of failing.

- **Auth & multi-tenancy** — register/login, refresh-token rotation, workspaces
  and members; tenants (parent org) layer above workspaces (migration `0023`).
- **Page Audit** — SSRF-guarded on-page analysis with saved per-workspace history.
- **Keyword Research** — one pipeline: Google Ads Keyword Planner (volume/CPC),
  Google Search Console (real rank), DataForSEO (SERP, via background task).
- **SERP Analyzer** (`/serp/analyze`) — live Google SERP via **DataForSEO**,
  stored as rich snapshots in the normalized `serp_snapshots` /
  `serp_snapshot_items` tables (DuckDuckGo scrape kept as a fallback provider).
- **Competitor Analysis** (`/competitors/*`) — a user-editable competitor list
  (`tracked_competitors`), one-click SERP-derived suggestions, an LLM
  **"find competitors" by domain** (`/competitors/discover`, OpenAI — scrapes the
  site then returns the top 10 with reason + confidence), and a rank-change time
  series (`competitor_rank_snapshots`, `/competitors/history` + `/refresh`).
- **Rankings**, **GSC integration**, **Content Optimization** (local AI via
  Ollama), **AI Search / GEO** (Claude), and **Stripe billing** (optional).

Provider keys: `ANTHROPIC_API_KEY` (AI Search), `OPENAI_API_KEY` (competitor
discovery), `DATAFORSEO_LOGIN`/`_PASSWORD` (SERP + keywords), GSC/Ads OAuth,
`STRIPE_*`, Ollama. See `.env.example`.

---

## Prerequisites

- Python 3.11 or newer
- PostgreSQL 14+ running locally (or a connection string to a remote instance)
- (Optional) [Ollama](https://ollama.com) for free local AI in Content Optimization

---

## Setup

Run these once, from the `backend/` directory.

### macOS / Linux

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # use requirements.txt for prod-only
cp .env.example .env                  # then edit .env (set DATABASE_URL + JWT_SECRET_KEY)
alembic upgrade head                  # create/upgrade the database schema
```

### Windows (PowerShell)

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
Copy-Item .env.example .env           # then edit .env (set DATABASE_URL + JWT_SECRET_KEY)
alembic upgrade head
```

> If PowerShell blocks the activate script, run once:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
> (Or use `.\.venv\Scripts\activate.bat` from cmd.exe.)

At minimum, set in `.env`:

```
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@localhost:5432/seo_saas
JWT_SECRET_KEY=<a long random secret>
```

Generate a secret with: `python -c "import secrets; print(secrets.token_urlsafe(48))"`

All other keys (PSI, GSC OAuth, Anthropic, Stripe, DataForSEO, Ollama) are optional —
without them those features return clearly-labelled sample data. See `.env.example`
for the full list and per-feature setup notes.

---

## Start the backend

Activate the virtualenv first, then run Uvicorn with autoreload.

### macOS / Linux

```bash
source .venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Windows (PowerShell)

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Without activating the venv, you can also call the interpreter directly:

- macOS/Linux: `.venv/bin/python -m uvicorn app.main:app --reload --port 8000`
- Windows: `.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000`

Then open http://localhost:8000/docs.

---

## Database migrations (Alembic)

```bash
alembic upgrade head              # apply all migrations
alembic revision -m "message"     # create a new migration (edit the generated file)
alembic downgrade -1              # roll back the last migration
alembic current                   # show the current revision
```

Alembic reads `DATABASE_URL` from `.env`.

---

## Tests

```bash
pytest                              # run the full suite (quiet)
pytest tests/test_competitor_domains.py   # a single file
pytest -k keyword_analysis          # match by name
```

Tests run against the configured `DATABASE_URL`; each test runs inside a
transaction that is rolled back, so the database is left unchanged.

---

## Project layout

```
app/
  main.py         # FastAPI app + CORS + /health
  core/config.py  # Settings (reads .env via pydantic-settings)
  api/            # routers (routes mounted under /api)
  db/             # session + engine
  models/         # SQLAlchemy models (competitor, tenant, keyword_analysis, …)
  schemas/        # Pydantic request/response models
  services/       # business logic (audits, keyword, serp, competitor_*, ai, …)
alembic/          # migration scripts
tests/            # pytest suite
```
