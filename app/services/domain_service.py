"""Domain business logic (workspace-scoped)."""

import re
from urllib.parse import urlparse

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.domain import Domain
from app.models.keyword_analysis import DomainKeyword
from app.models.workspace import Workspace

# A permissive hostname check: labels of letters/digits/hyphens, at least one dot.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$"
)


# Sentinel so update_domain can tell "leave unchanged" (absent) from "clear"
# (explicit null) for the category, which has no empty-string form.
_UNSET = object()


class InvalidDomainError(Exception):
    """Raised when a domain URL cannot be parsed into a valid domain."""


class DomainAlreadyExistsError(Exception):
    """Raised when a domain already exists in the workspace."""


def normalize_domain(raw: str) -> str:
    """Extract a normalized hostname from a bare domain or full URL.

    "https://www.Example.com/path" -> "www.example.com"
    "example.com" -> "example.com"
    """
    value = raw.strip()
    if "//" not in value:
        value = "//" + value  # let urlparse find the netloc
    parsed = urlparse(value, scheme="http")
    host = (parsed.netloc or parsed.path).strip().lower()
    host = host.split("@")[-1]  # drop any userinfo
    host = host.split(":")[0]  # drop any port
    host = host.strip("/")
    if not _HOSTNAME_RE.match(host):
        raise InvalidDomainError(f"Invalid domain: {raw!r}")
    return host


def create_domain(
    db: Session, workspace: Workspace, url: str, name: str | None,
    *, category_id=None,
) -> Domain:
    """Create a domain in the workspace. Raises on invalid/duplicate domain.

    `category_id` optionally assigns the domain's industry category up front, so
    competitor suggestions are niche-scoped from the start."""
    domain = normalize_domain(url)

    exists = db.execute(
        select(Domain.id).where(
            Domain.workspace_id == workspace.id, Domain.domain == domain
        )
    ).first()
    if exists:
        raise DomainAlreadyExistsError(domain)

    domain = Domain(
        workspace_id=workspace.id,
        name=(name or domain),
        domain=domain,
        category_id=category_id,
    )
    db.add(domain)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise DomainAlreadyExistsError(domain) from exc
    db.refresh(domain)
    return domain


def update_domain(
    db: Session, domain: Domain, *, name: str | None = None,
    country: str | None = None, language: str | None = None,
    topic_terms: str | None = None, brand_terms: str | None = None,
    is_local: bool | None = None, category_id=_UNSET,
) -> Domain:
    """Partial update of a domain's market + niche + category. Only provided
    fields change.

    country/language accept "" to CLEAR the override (fall back to the workspace
    default); None means "leave unchanged". topic_terms/brand_terms accept "" to
    clear. category_id uses the _UNSET sentinel for "leave unchanged"; pass None
    to clear the category."""
    if name is not None:
        domain.name = name.strip()[:200] or domain.name
    if country is not None:
        domain.country = (country.strip().lower()[:2] or None)
    if language is not None:
        domain.language = (language.strip().lower()[:10] or None)
    if topic_terms is not None:
        domain.topic_terms = topic_terms.strip() or None
    if brand_terms is not None:
        domain.brand_terms = brand_terms.strip() or None
    if is_local is not None:
        domain.is_local = bool(is_local)
    category_changed = category_id is not _UNSET and category_id != domain.category_id
    if category_id is not _UNSET:
        domain.category_id = category_id
    if category_changed:
        # Propagate to the domain's existing keyword links so competitor
        # suggestions become category-correct on already-researched data,
        # not only after the next research run.
        db.execute(
            update(DomainKeyword)
            .where(DomainKeyword.domain_id == domain.id)
            .values(category_id=category_id)
        )
    db.commit()
    db.refresh(domain)
    return domain


def suggest_topic_terms(host: str) -> list[str]:
    """Best-effort niche terms for a site, by crawling its homepage. Never raises."""
    # Local import: keyword_analysis_service is heavy and imports this module's siblings.
    from app.services import keyword_analysis_service as kas

    try:
        return kas._site_topic_seeds(host)
    except Exception:  # noqa: BLE001 - a crawl failure just yields no suggestions
        return []


def autoderive_topics_task(workspace_id, domain_id) -> None:
    """Background task (own session): fill a domain's topic_terms from its site,
    only when none are set yet. Safe to run after create_domain."""
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        domain = db.get(Domain, domain_id)
        if domain is None or (domain.topic_terms or "").strip():
            return
        terms = suggest_topic_terms(domain.domain)
        if terms:
            domain.topic_terms = ", ".join(terms)
            db.commit()
    finally:
        db.close()


def get_domain_in_workspace(
    db: Session, workspace: Workspace, domain_id: str
) -> Domain | None:
    """Return a domain by id only if it belongs to the workspace."""
    import uuid

    try:
        parsed = uuid.UUID(domain_id)
    except (ValueError, TypeError):
        return None
    return db.execute(
        select(Domain).where(
            Domain.id == parsed, Domain.workspace_id == workspace.id
        )
    ).scalar_one_or_none()


def list_domains(db: Session, workspace: Workspace) -> list[Domain]:
    return list(
        db.execute(
            select(Domain)
            .where(Domain.workspace_id == workspace.id)
            .order_by(Domain.created_at.asc())
        ).scalars()
    )


def delete_domain(db: Session, workspace: Workspace, domain_id: str) -> bool:
    """Delete a workspace's domain. Its keywords, competitors, rankings, SERP and
    opportunity rows go with it (every FK to domains is ON DELETE CASCADE, so a
    SQL DELETE lets Postgres do it). Returns False if it isn't in the workspace."""
    domain = get_domain_in_workspace(db, workspace, domain_id)
    if domain is None:
        return False
    db.execute(delete(Domain).where(Domain.id == domain.id))
    db.commit()
    return True
