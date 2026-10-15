"""Application configuration loaded from environment variables."""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings sourced from the environment / .env file."""

    model_config = SettingsConfigDict(
        # Anchored to backend/.env so settings (DB URL, DataForSEO kill switch)
        # load the same no matter which directory the server is started from.
        env_file=str(Path(__file__).resolve().parents[2] / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    PROJECT_NAME: str = "SEO SaaS API"
    API_V1_PREFIX: str = "/api"

    DATABASE_URL: str

    # Comma-separated string in the env; parsed into a list of origins.
    BACKEND_CORS_ORIGINS: str = "http://localhost:3000"

    # JWT / session config.
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Auth cookie config. COOKIE_SECURE must be True in production (HTTPS).
    AUTH_COOKIE_NAME: str = "access_token"
    AUTH_REFRESH_COOKIE_NAME: str = "refresh_token"
    COOKIE_SECURE: bool = False
    COOKIE_SAMESITE: str = "lax"

    # Google PageSpeed Insights API key (optional; PSI works keyless but is
    # heavily rate-limited without one). Get one at https://developers.google.com/speed/docs/insights/v5/get-started
    PSI_API_KEY: str = ""

    # Google Search Console OAuth (optional). When client id/secret are unset,
    # the GSC features stay dormant and the UI shows a "connect" prompt.
    # Create an OAuth 2.0 Client (type: Web application) in Google Cloud Console
    # with the Search Console API enabled, and register the redirect URI below.
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    GOOGLE_OAUTH_REDIRECT_URI: str = "http://localhost:8000/api/gsc/callback"
    # Where the OAuth callback sends the browser back to after connecting.
    FRONTEND_URL: str = "http://localhost:3000"

    @property
    def gsc_configured(self) -> bool:
        return bool(self.GOOGLE_OAUTH_CLIENT_ID and self.GOOGLE_OAUTH_CLIENT_SECRET)

    # Google Ads Keyword Planner (real search volume, competition, CPC). Free —
    # it's a Google API, not a paid third party — but needs an Ads account.
    # Reuses GOOGLE_OAUTH_CLIENT_ID/SECRET (same OAuth app, `adwords` scope).
    # Steps: create a Google Ads account -> get a developer token (API Center) ->
    # generate a refresh token for an account with Ads access -> set the ids below.
    GOOGLE_ADS_DEVELOPER_TOKEN: str = ""
    GOOGLE_ADS_REFRESH_TOKEN: str = ""
    # Account queried (digits only, no dashes). If it sits under a manager (MCC),
    # set GOOGLE_ADS_LOGIN_CUSTOMER_ID to the MCC id.
    GOOGLE_ADS_CUSTOMER_ID: str = ""
    GOOGLE_ADS_LOGIN_CUSTOMER_ID: str = ""
    GOOGLE_ADS_API_VERSION: str = "v22"
    # Optional dedicated OAuth client for Ads. Set these to run Ads on a different
    # Google account/Cloud project without touching the GSC client above; when
    # blank they fall back to GOOGLE_OAUTH_CLIENT_ID/SECRET.
    GOOGLE_ADS_CLIENT_ID: str = ""
    GOOGLE_ADS_CLIENT_SECRET: str = ""

    @property
    def ads_client_id(self) -> str:
        return self.GOOGLE_ADS_CLIENT_ID or self.GOOGLE_OAUTH_CLIENT_ID

    @property
    def ads_client_secret(self) -> str:
        return self.GOOGLE_ADS_CLIENT_SECRET or self.GOOGLE_OAUTH_CLIENT_SECRET

    @property
    def google_ads_configured(self) -> bool:
        # Developer tokens were sunset 2026-09-09 — access is now granted to the
        # Google Cloud project behind the OAuth client, so the token is optional.
        return bool(
            self.ads_client_id
            and self.ads_client_secret
            and self.GOOGLE_ADS_REFRESH_TOKEN
            and self.GOOGLE_ADS_CUSTOMER_ID
        )

    # Claude — optional last fallback for AI Search (after OpenAI / the local
    # model). Unset = not used; no sample output is ever returned.
    ANTHROPIC_API_KEY: str = ""
    AI_MODEL: str = "claude-opus-5"

    @property
    def ai_configured(self) -> bool:
        return bool(self.ANTHROPIC_API_KEY)

    # OpenAI (used by the LLM competitor-discovery feature, mirroring the
    # standalone seo_competitor_finder.py). When unset, discovery returns 503.
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-5.6-luna"
    # OpenAI model for short AI writing: Content Optimization suggestions, AI
    # overview cards and Title & Meta. Scores and numbers are always rule-based;
    # AI only writes text. Falls back to the local model (Ollama) when no key.
    AI_TEXT_MODEL: str = "gpt-4o-mini"

    @property
    def openai_configured(self) -> bool:
        return bool(self.OPENAI_API_KEY)

    # Stripe billing (optional). When keys are unset, plan selection/upgrades
    # work locally (a free plan, or a 1-year trial on paid plans) with no
    # payment; set the keys to route upgrades through Stripe Checkout + Portal.
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_PUBLISHABLE_KEY: str = ""
    # Length of the introductory free trial granted on paid plans (days).
    TRIAL_DAYS: int = 365

    @property
    def stripe_configured(self) -> bool:
        return bool(self.STRIPE_SECRET_KEY)

    # DataForSEO (keyword volume, difficulty, competitor keywords). Without
    # credentials, the keyword-ideas / competitor features return labelled
    # sample data; add login+password to get real data.
    DATAFORSEO_LOGIN: str = ""
    DATAFORSEO_PASSWORD: str = ""
    # How long a cached provider response stays fresh (days). Repeated lookups
    # within this window are served from our DB for free.
    PROVIDER_CACHE_TTL_DAYS: int = 30
    # Safety cap: max NEW (paid) DataForSEO lookups per day. Cached repeats don't
    # count. At ~$0.01-0.02 each, this bounds daily spend. 0 = unlimited.
    DATAFORSEO_DAILY_CALL_CAP: int = 15

    @property
    def dataforseo_configured(self) -> bool:
        return bool(self.DATAFORSEO_LOGIN and self.DATAFORSEO_PASSWORD)

    # Deployment environment: "development" | "production".
    APP_ENV: str = "development"
    # DataForSEO Sandbox (free, dummy data, same credentials, identical response
    # shape). Workspaces flagged is_sandbox call it instead of the paid API and
    # their SERP/analytics rows are tagged environment='sandbox', so they never
    # mix with live data. Allowed by default outside production; set
    # DATAFORSEO_SANDBOX_ENABLED explicitly to override either way.
    DATAFORSEO_SANDBOX_URL: str = "https://sandbox.dataforseo.com"
    DATAFORSEO_SANDBOX_ENABLED: bool | None = None
    # Route EVERY DataForSEO call to the sandbox (tests/CI/offline dev) so
    # nothing is ever charged. Data is then tagged 'sandbox' too.
    DATAFORSEO_FORCE_SANDBOX: bool = False
    # Kill switch for the PAID live API. False = every live call is refused
    # before leaving the server (stored data stays readable; sandbox still works).
    DATAFORSEO_LIVE_ENABLED: bool = True
    # Ranked keywords (Labs ranked_keywords: ~$0.01/call + $0.0001/keyword).
    # Default + hard max keywords per fetch, to bound spend per click.
    DATAFORSEO_RANKED_KEYWORDS_LIMIT: int = 100
    DATAFORSEO_RANKED_KEYWORDS_MAX_LIMIT: int = 200
    # Comma-separated domains allowed to fetch from the PAID live API (budget
    # guard). Empty = any domain. Sandbox workspaces are never restricted.
    DATAFORSEO_LIVE_RANKED_TARGETS: str = ""
    # Multi-page fetches ("relevant" / "all" scopes): hard caps per fetch.
    DATAFORSEO_RANKED_MAX_KEYWORDS_PER_FETCH: int = 5000
    DATAFORSEO_RANKED_MAX_COST_PER_FETCH: float = 0.60
    # "relevant" scope = keyword matches the DOMAIN's own topic rule (per-domain
    # topic_terms via domain_settings — no global/niche regex here any more) AND
    # position <= N AND volume >= M (all filtered server-side, so only matches are
    # billed). These two bounds are niche-agnostic numeric thresholds.
    DATAFORSEO_RELEVANT_MAX_POSITION: int = 20
    DATAFORSEO_RELEVANT_MIN_VOLUME: int = 50
    # Backlinks API ($0.024/request + $0.000036/row live; sandbox free). Rows kept
    # per list (referring domains / backlinks / anchors / link gap) per fetch, and
    # how many tracked competitors a link-gap fetch compares. A profile fetch is
    # 4 requests (~$0.11 at 100 rows); a link gap is 1 (~$0.03). Stored runs are
    # reused until the user explicitly refreshes — never re-fetched automatically.
    # Weekly automatic SERP snapshots (app/jobs/serp_sync — PAID per keyword).
    # Off: the job exits without calling DataForSEO even if a scheduler runs it.
    # User-clicked SERP fetches are not affected.
    SERP_SYNC_ENABLED: bool = False
    DATAFORSEO_BACKLINKS_ROWS: int = 100
    DATAFORSEO_BACKLINKS_MAX_COMPETITORS: int = 5

    @property
    def live_ranked_targets(self) -> set[str]:
        return {t.strip().lower().removeprefix("www.")
                for t in self.DATAFORSEO_LIVE_RANKED_TARGETS.split(",") if t.strip()}

    @property
    def is_production(self) -> bool:
        return self.APP_ENV.strip().lower() == "production"

    @property
    def dataforseo_sandbox_allowed(self) -> bool:
        if self.DATAFORSEO_FORCE_SANDBOX:
            return True
        if self.DATAFORSEO_SANDBOX_ENABLED is not None:
            return self.DATAFORSEO_SANDBOX_ENABLED
        return not self.is_production

    # Manual SERP scraper proxies (optional). Comma-separated Webshare-style
    # host:port:user:pass entries; the scraper rotates through them. Empty =
    # scrape from our own IP. Datacenter proxies are usually blocked/decoyed by
    # search engines — use residential ones for reliable results.
    SERP_PROXIES: str = ""

    # Local AI via Ollama (free, private) — FALLBACK for AI writing when OpenAI
    # isn't configured or fails (openai_ai.write_json). Unreachable = skipped.
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "gemma4:latest"
    OLLAMA_ENABLED: bool = True

    @property
    def cors_origins(self) -> list[str]:
        return [
            origin.strip()
            for origin in self.BACKEND_CORS_ORIGINS.split(",")
            if origin.strip()
        ]

    @field_validator("DATABASE_URL")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        if not value:
            raise ValueError("DATABASE_URL must be set")
        return value

    @field_validator("JWT_SECRET_KEY")
    @classmethod
    def _validate_secret(cls, value: str) -> str:
        if not value or len(value) < 16:
            raise ValueError("JWT_SECRET_KEY must be set (>= 16 chars)")
        return value


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
