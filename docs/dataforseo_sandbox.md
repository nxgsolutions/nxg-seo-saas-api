# DataForSEO Sandbox

Every DataForSEO call in this app can run against the **free Sandbox**
(`https://sandbox.dataforseo.com`) instead of the paid live API, so a feature
can be tested end-to-end without spending money or polluting live SERP data.

- **Same credentials.** The Sandbox uses the same `DATAFORSEO_LOGIN` /
  `DATAFORSEO_PASSWORD` as live — only the host differs. There is no separate
  sandbox account to configure.
- **Dummy data.** The Sandbox returns canned responses that *ignore your
  input* (e.g. UK "pizza" SERPs for any keyword). It validates plumbing and
  response shape, **not** real results.

## How routing is decided

```
Workspace.is_sandbox ─┐
                      ├─► data_env.env_for_* ──► env ("live" | "sandbox")
DATAFORSEO_FORCE_SANDBOX ┘                          │
                                                    ▼
                               dataforseo_client._base_url(env) picks the host
```

- A workspace flagged `is_sandbox=true` routes **all** its DataForSEO calls to
  the Sandbox, and every provider row it writes is tagged `environment='sandbox'`.
- `DATAFORSEO_FORCE_SANDBOX=true` forces **everything** to the Sandbox (used by
  one-off scripts and local testing).
- Reads filter on `environment`, so sandbox rows never leak into a live
  workspace even though the provider tables (`serp_snapshots`,
  `domain_analytics_snapshots`, `backlink_runs`, `domain_ranked_keyword_runs`,
  `domain_monthly_metrics`) are shared per keyword/domain across workspaces.

## Relevant settings

| Setting | Effect |
| --- | --- |
| `DATAFORSEO_FORCE_SANDBOX` | Route every call to the Sandbox. |
| `DATAFORSEO_SANDBOX_ENABLED` | Allow/deny Sandbox; defaults to allowed except when `APP_ENV=production`. |
| `DATAFORSEO_LIVE_ENABLED` | Kill switch for live calls (stored data still served). |

## Adding a NEW endpoint (checklist)

When you wrap a new DataForSEO endpoint, make it sandbox-safe from day one:

1. **Client** (`app/services/dataforseo_client.py`): add an
   `env: str | None = None` keyword and pass it straight to `_post(...)` /
   `_bl(...)`. Host routing is automatic — never build the URL yourself. For a
   raw httpx call, build the host with `_base_url(env)` (see `account_balance`).
2. **Service**: resolve env **once** with `data_env.env_for_domain(db, domain)`
   / `env_for_workspace(ws)` / `env_for_workspace_id(db, id)` and thread it in.
   Never hard-code `"live"` / `"sandbox"`.
3. **Storage**: if the response is persisted, add an `environment` column to the
   table (Alembic migration). Set it `= env` on write, and filter
   `environment == env` on **every** read.
4. **Jobs**: any nightly/background sweep must skip sandbox workspaces
   (`Workspace.is_sandbox.is_(False)`) so the Sandbox never auto-spends.

`tests/test_dataforseo_sandbox.py::test_every_endpoint_routes_by_env` enforces
step 1 for the whole client — a new endpoint that forgets `env` fails the test.

## LLM calls in a sandbox workspace

Sandbox workspaces also skip the **LLM** so testing never burns OpenAI/Ollama
tokens. `app/services/sandbox_ai.py` returns deterministic, placeholder data —
shaped exactly like the real responses — for every model-backed feature run from
a domain's context:

- competitor discovery (`competitor_service.discover_and_store`)
- business-profile extraction (`competitor_service.refresh_business_profile`)
- manual-add match scoring (`competitor_service._verify_manual_add`)
- category auto-detect (`category_detection_service.detect_category`)

Each gates on `sandbox_ai.is_sandbox(db, domain)`. A new model-backed feature
that runs with a domain in hand should do the same.

## Creating a sandbox workspace to test against

```powershell
python -m app.scripts.create_sandbox_workspace --owner <email> --copy-domain <domain>
```

This creates an `is_sandbox` workspace (optionally copying a domain's keywords)
whose calls hit the Sandbox and whose rows are tagged `environment='sandbox'`.
