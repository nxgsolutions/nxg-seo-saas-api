"""page history: site_pages, page_snapshots, page_search_daily (+ backfill)

Every fetch of a website page (Page Audit, Content Optimization, Title & Meta)
is stored as a timestamped snapshot with what changed since the previous one;
daily Search Console numbers per page are stored next to it so changes can be
compared with clicks / position before and after. Private per workspace.
Existing page_audits are backfilled as the first snapshots.

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-05
"""
import json
import uuid as uuidlib
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE site_pages (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            domain_id uuid REFERENCES domains(id) ON DELETE SET NULL,
            url_key text NOT NULL,
            url text NOT NULL,
            title varchar(512) NOT NULL DEFAULT '',
            latest_audit_score integer,
            snapshot_count integer NOT NULL DEFAULT 0,
            first_seen_at timestamptz NOT NULL DEFAULT now(),
            last_checked_at timestamptz NOT NULL DEFAULT now(),
            last_changed_at timestamptz,
            CONSTRAINT uq_site_page_url UNIQUE (workspace_id, url_key)
        );
        CREATE INDEX ix_site_pages_workspace_id ON site_pages (workspace_id);
        CREATE INDEX ix_site_pages_domain_id ON site_pages (domain_id);

        CREATE TABLE page_snapshots (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            page_id uuid NOT NULL REFERENCES site_pages(id) ON DELETE CASCADE,
            workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            captured_at timestamptz NOT NULL DEFAULT now(),
            source varchar(20) NOT NULL,
            source_id uuid,
            device varchar(10),
            audit_score integer,
            content_score integer,
            keyword varchar(255),
            facts jsonb NOT NULL DEFAULT '{}'::jsonb,
            tech jsonb,
            checks jsonb,
            content_hash varchar(64),
            body_text text,
            changes jsonb NOT NULL DEFAULT '[]'::jsonb
        );
        CREATE INDEX ix_page_snapshots_page_time ON page_snapshots (page_id, captured_at DESC);
        CREATE INDEX ix_page_snapshots_workspace_id ON page_snapshots (workspace_id);

        CREATE TABLE page_search_daily (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            page_id uuid NOT NULL REFERENCES site_pages(id) ON DELETE CASCADE,
            day date NOT NULL,
            clicks integer NOT NULL DEFAULT 0,
            impressions integer NOT NULL DEFAULT 0,
            ctr double precision NOT NULL DEFAULT 0,
            position double precision,
            fetched_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_page_search_day UNIQUE (page_id, day)
        );
        """
    )
    # Existing audits become the first snapshots of each page.
    _backfill_from_audits(op.get_bind())


def _backfill_from_audits(bind) -> None:
    """Turn saved page audits into the first page-history snapshots.

    Self-contained (raw SQL + the service's PURE helpers only) so it never
    loads the live ``Workspace``/``SitePage`` ORM models — those gain columns
    in later migrations (e.g. ``workspaces.default_country`` in 0039), and
    loading them here would select columns that do not exist yet at 0035.
    """
    # Pure, side-effect-free helpers (safe to import — no row loads on import).
    from app.services.page_history_service import audit_tech, diff, url_key

    audits = bind.execute(
        text(
            "SELECT id, workspace_id, url, score, status_code, load_time_ms, "
            "page_size_bytes, metrics, checks, created_at "
            "FROM page_audits ORDER BY created_at"
        )
    ).mappings().all()

    dom_cache: dict = {}

    def _domain_for(ws_id, url):
        host = url_key(url).split("/", 1)[0]
        if ws_id not in dom_cache:
            dom_cache[ws_id] = bind.execute(
                text("SELECT id, domain FROM domains WHERE workspace_id = :w"),
                {"w": ws_id},
            ).all()
        for did, dname in dom_cache[ws_id]:
            dh = (dname or "").lower().removeprefix("www.")
            if host == dh or host.endswith("." + dh):
                return did
        return None

    def _as_obj(v, default):
        if v is None:
            return default
        return json.loads(v) if isinstance(v, str) else v

    for a in audits:
        ws_id = a["workspace_id"]
        m = _as_obj(a["metrics"], {})
        checks_src = _as_obj(a["checks"], [])
        key = url_key(a["url"])
        created = a["created_at"]

        page = bind.execute(
            text(
                "SELECT id, snapshot_count FROM site_pages "
                "WHERE workspace_id = :w AND url_key = :k"
            ),
            {"w": ws_id, "k": key},
        ).first()
        if page is None:
            page_id = uuidlib.uuid4()
            bind.execute(
                text(
                    "INSERT INTO site_pages (id, workspace_id, domain_id, url_key, url, "
                    "title, snapshot_count, first_seen_at, last_checked_at) "
                    "VALUES (:id, :w, :d, :k, :u, :t, 0, :c, :c)"
                ),
                {"id": page_id, "w": ws_id, "d": _domain_for(ws_id, a["url"]),
                 "k": key, "u": a["url"], "t": (m.get("title") or "")[:512], "c": created},
            )
            snapshot_count = 0
        else:
            page_id = page.id
            snapshot_count = page.snapshot_count or 0

        facts = {"title": m.get("title") or "", "meta": m.get("description") or "",
                 "h1": [m["h1_text"]] if m.get("h1_text") else []}
        checks = [{"id": c.get("id"), "label": c.get("label"), "status": c.get("status")}
                  for c in checks_src]
        tech = audit_tech(a["status_code"], a["load_time_ms"], a["page_size_bytes"], m, checks)

        prev = bind.execute(
            text(
                "SELECT facts, tech, checks FROM page_snapshots "
                "WHERE page_id = :p ORDER BY captured_at DESC LIMIT 1"
            ),
            {"p": page_id},
        ).first()
        prev_dict = None
        if prev is not None:
            prev_dict = {"facts": _as_obj(prev.facts, {}), "tech": _as_obj(prev.tech, None),
                         "checks": _as_obj(prev.checks, None), "content_hash": None, "body_text": None}
        changes = diff(prev_dict, {"facts": facts, "tech": tech, "checks": checks,
                                   "content_hash": None, "body_text": None})

        bind.execute(
            text(
                "INSERT INTO page_snapshots (id, page_id, workspace_id, captured_at, source, "
                "source_id, device, audit_score, facts, tech, checks, changes) "
                "VALUES (:id, :p, :w, :cap, 'audit', :sid, :dev, :score, "
                "CAST(:facts AS jsonb), CAST(:tech AS jsonb), CAST(:checks AS jsonb), CAST(:changes AS jsonb))"
            ),
            {"id": uuidlib.uuid4(), "p": page_id, "w": ws_id, "cap": created, "sid": a["id"],
             "dev": m.get("device") or "desktop", "score": a["score"],
             "facts": json.dumps(facts), "tech": json.dumps(tech),
             "checks": json.dumps(checks), "changes": json.dumps(changes)},
        )

        bind.execute(
            text(
                "UPDATE site_pages SET url = :u, "
                "title = COALESCE(NULLIF(:t, ''), title), "
                "first_seen_at = LEAST(COALESCE(first_seen_at, :c), :c), "
                "last_checked_at = :c, latest_audit_score = :score, snapshot_count = :sc, "
                "last_changed_at = CASE WHEN :has_changes THEN :c ELSE last_changed_at END "
                "WHERE id = :id"
            ),
            {"u": a["url"], "t": facts["title"][:512], "c": created, "score": a["score"],
             "sc": snapshot_count + 1, "has_changes": bool(changes), "id": page_id},
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS page_search_daily; DROP TABLE IF EXISTS page_snapshots; DROP TABLE IF EXISTS site_pages;")
