"""Create a DataForSEO Sandbox workspace for a user.

    python -m app.scripts.create_sandbox_workspace --owner you@example.com
    python -m app.scripts.create_sandbox_workspace --owner you@example.com \
        --name "Raj's Sandbox" --copy-domain iaspirants.com

The workspace is flagged is_sandbox, so every DataForSEO call made from it goes
to the free Sandbox (dummy data) and its rows are tagged environment='sandbox'.
`--copy-domain` clones one of the owner's domains plus its opportunity keyword
set (volume / KD / intent) so pages have something to test on. SERP-derived
fields (position, ranking URL, competitors, SERP features) are cleared and no
SERP snapshots or analytics are copied — the sandbox starts clean.
Idempotent: re-running reuses the existing workspace/domain.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.domain import Domain
from app.models.opportunity import OpportunityKeyword, OpportunityPage
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.services import workspace_service


def _copy_domain(db, source: Domain, workspace: Workspace) -> tuple[Domain, int]:
    target = db.execute(select(Domain).where(
        Domain.workspace_id == workspace.id, Domain.domain == source.domain)).scalar_one_or_none()
    if target is None:
        target = Domain(workspace_id=workspace.id, name=source.name, domain=source.domain)
        db.add(target)
        db.flush()

    have = set(db.execute(select(OpportunityKeyword.keyword)
                          .where(OpportunityKeyword.domain_id == target.id)).scalars())
    page_map: dict = {}
    for page in db.execute(select(OpportunityPage)
                           .where(OpportunityPage.domain_id == source.id)).scalars():
        existing = db.execute(select(OpportunityPage).where(
            OpportunityPage.domain_id == target.id, OpportunityPage.url == page.url)
        ).scalar_one_or_none()
        if existing is None:
            existing = OpportunityPage(domain_id=target.id, url=page.url, title=page.title)
            db.add(existing)
            db.flush()
        page_map[page.id] = existing.id

    copied = 0
    for kw in db.execute(select(OpportunityKeyword)
                         .where(OpportunityKeyword.domain_id == source.id)).scalars():
        if kw.keyword in have:
            continue
        db.add(OpportunityKeyword(
            domain_id=target.id, page_id=page_map.get(kw.page_id), keyword_id=kw.keyword_id,
            keyword=kw.keyword, search_volume=kw.search_volume, yoy_change=kw.yoy_change,
            keyword_difficulty=kw.keyword_difficulty, intent=kw.intent,
            opportunity_score=kw.opportunity_score, cpc=kw.cpc,
            country_code=kw.country_code,
            # SERP-derived -> start clean; filled by sandbox SERP pulls.
            current_position=None, ranking_url=None, serp_features=[], competitors=[],
        ))
        copied += 1
    return target, copied


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m app.scripts.create_sandbox_workspace")
    ap.add_argument("--owner", required=True, help="owner's login email")
    ap.add_argument("--name", default=None, help="workspace name (default: \"<first>'s Sandbox\")")
    ap.add_argument("--copy-domain", default=None, help="one of the owner's domains to clone")
    args = ap.parse_args()

    if not settings.dataforseo_sandbox_allowed:
        print("Sandbox is disabled in this environment (APP_ENV=production).", file=sys.stderr)
        return 1

    db = SessionLocal()
    try:
        owner = db.execute(select(User).where(User.email == args.owner.lower())).scalar_one_or_none()
        if owner is None:
            print(f"No user with email {args.owner}", file=sys.stderr)
            return 1
        name = args.name or f"{owner.first_name}'s Sandbox".strip()

        ws = db.execute(select(Workspace).where(
            Workspace.owner_id == owner.id, Workspace.name == name,
            Workspace.is_sandbox.is_(True))).scalar_one_or_none()
        if ws is None:
            ws = workspace_service.create_workspace(db, owner, name, is_sandbox=True)
            print(f"Created sandbox workspace {ws.name!r} ({ws.id})")
        else:
            print(f"Reusing sandbox workspace {ws.name!r} ({ws.id})")

        if args.copy_domain:
            source = db.execute(
                select(Domain)
                .join(WorkspaceMember, WorkspaceMember.workspace_id == Domain.workspace_id)
                .join(Workspace, Workspace.id == Domain.workspace_id)
                .where(WorkspaceMember.user_id == owner.id,
                       Domain.domain == args.copy_domain.lower(),
                       Workspace.is_sandbox.is_(False))
                .order_by(Domain.created_at)
                .limit(1)
            ).scalar_one_or_none()
            if source is None:
                print(f"{owner.email} has no live domain {args.copy_domain}", file=sys.stderr)
                return 1
            target, copied = _copy_domain(db, source, ws)
            print(f"Domain {target.domain} ({target.id}): copied {copied} keywords")
        db.commit()
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
