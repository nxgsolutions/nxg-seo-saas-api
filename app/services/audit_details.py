"""Actionable, source-HTML audit evidence; no additional network requests."""
import json
import re
from urllib.parse import urljoin, urlparse

FIXES = {
    'title': 'Edit the page SEO title in your CMS. Use one descriptive, page-specific title. Length is a preview guideline, not a Google limit.',
    'description': 'Write one unique meta description summarizing this page in your CMS SEO settings. Length is a preview guideline; Google may choose another snippet.',
    'h1': 'Use a clear main heading describing this page, then organize sections with H2 and H3 headings.',
    'canonical': 'Set one canonical link in the head to the preferred absolute HTTP(S) URL. Keep internal links and sitemap URLs consistent.',
    'img_alt': 'Edit the affected images below. Describe informative images in context; use alt="" for purely decorative images.',
    'indexable': 'If this page should appear in search, remove noindex from robots metadata and response headers, then request indexing in Search Console. Keep intentional exclusions.',
    'speed': 'Review server response, caching and redirects. Use the browser performance panel to diagnose LCP, INP and CLS separately.',
    'size': 'Reduce repeated HTML and inline data; use pagination where appropriate and enable compression on the server.',
    'schema': 'Add relevant, valid JSON-LD that matches visible content. Validate it with Google Rich Results Test; presence alone does not establish eligibility.',
    'og': 'Set og:title, og:description and an absolute og:image URL in your page social-sharing settings.',
    'viewport': 'Add <meta name="viewport" content="width=device-width, initial-scale=1"> in the head and verify the mobile layout.',
    'lang': 'Set the html lang attribute to the actual language of this page, for example lang="en".',
    'charset': 'Declare <meta charset="utf-8"> near the start of the head.',
    'https': 'Enable HTTPS and redirect HTTP URLs to their HTTPS equivalents.',
    'status': 'Check the published URL, redirects and server logs. Public content should return a successful response.',
    'content': 'Review visible main content for completeness and usefulness. Do not add filler to reach a word count.',
}


def audit_details(soup, url, checks):
    findings = []
    extra = []

    def issue(id, label, evidence, fix, status='warn'):
        extra.append(dict(id=id, label=label, detail=evidence, status=status))
        findings.append(dict(id=id, label=label, priority='High' if status == 'fail' else 'Medium', evidence=evidence, fix=fix))

    for check in checks:
        if check.status != 'pass':
            findings.append(dict(id=check.id, label=check.label, priority='High' if check.status == 'fail' else 'Medium', evidence=check.detail, fix=FIXES.get(check.id, 'Review this setting in the page template and rerun the audit.')))

    for selector, label in [('title', 'title tags'), ('meta[name="description" i]', 'meta descriptions'), ('link[rel~="canonical" i]', 'canonical links')]:
        # <title> inside inline SVG icons is an accessible label, not the page title.
        tags = [t for t in soup.select(selector) if not t.find_parent('svg')]
        if len(tags) > 1:
            issue('duplicate_' + selector.split('[')[0], 'Duplicate ' + label, f'Found {len(tags)} {label}.', 'Remove duplicate output from the theme or SEO plugin so this page has one authoritative value.')
    for tag in soup.select('link[rel~="canonical" i]'):
        href = tag.get('href', '').strip()
        if not href or urlparse(urljoin(url, href)).scheme not in ('http', 'https'):
            issue('canonical_invalid', 'Invalid canonical target', href or 'Empty canonical href.', FIXES['canonical'], 'fail')

    image_rows = []
    for index, img in enumerate(soup.find_all('img'), 1):
        fixes = []
        src = (img.get('src') or img.get('data-src') or '').strip()
        if not img.has_attr('alt'):
            fixes.append('Add descriptive alt text for an informative image, or alt="" if decorative.')
        elif not img.get('alt', '').strip():
            fixes.append('Confirm this image is decorative. If it conveys information, replace empty alt with a useful description.')
        if not all(re.fullmatch(r'[1-9]\d*', str(img.get(k, ''))) for k in ('width', 'height')):
            fixes.append('Set intrinsic width and height, or verify CSS reserves the correct aspect ratio to prevent layout shifts.')
        if not img.get('srcset') and not (img.parent.name == 'picture' and img.parent.find('source', srcset=True)):
            fixes.append('For images displayed at multiple sizes, provide srcset and sizes to avoid oversized downloads.')
        if img.get('loading') == 'lazy' and img.get('fetchpriority') == 'high':
            fixes.append('Resolve conflicting lazy loading and high fetch priority. Load the hero/LCP image eagerly; lazy-load offscreen images.')
        if not src:
            fixes.append('Provide a crawlable src fallback; verify the image is visible without custom JavaScript.')
        image_rows.append(dict(position=index, src=urljoin(url, src) if src else '', alt=img.get('alt'), width=img.get('width', ''), height=img.get('height', ''), loading=img.get('loading', 'auto'), fixes=fixes))

    nodes = []
    malformed = 0
    for script in soup.find_all('script', attrs={'type': re.compile(r'^application/ld\+json$', re.I)}):
        try:
            pending = [json.loads(script.get_text())]
            while pending:
                value = pending.pop()
                if isinstance(value, list):
                    pending.extend(value)
                elif isinstance(value, dict):
                    nodes.append(value)
                    pending.extend(v for v in value.values() if isinstance(v, (dict, list)))
        except (ValueError, RecursionError):
            malformed += 1
    if malformed:
        issue('schema_invalid', 'Invalid JSON-LD', f'{malformed} JSON-LD blocks could not be parsed.', 'Correct JSON syntax (quotes, commas and brackets), then validate with Rich Results Test.', 'fail')
        for check in checks:
            if check.id == 'schema':
                check.status = 'fail'
                check.detail = 'JSON-LD is present but includes invalid JSON.'

    articles = [n for n in nodes if any(t in ('Article', 'BlogPosting', 'NewsArticle') for t in (n.get('@type') if isinstance(n.get('@type'), list) else [n.get('@type')]))]
    is_blog = bool(articles or soup.find('meta', attrs={'property': 'og:type', 'content': 'article'}) or re.search(r'/(blog|posts|articles)/[^/]+', urlparse(url).path))
    if is_blog:
        if not articles:
            issue('article_schema', 'Blog article markup', 'Article-like page detected; no Article, BlogPosting or NewsArticle JSON-LD found.', 'For an individual post, add Article or BlogPosting JSON-LD matching its visible headline, author, dates and representative image. Do not mark a blog listing as a single article.')
        for i, article in enumerate(articles, 1):
            missing = [key for key in ('headline', 'author', 'datePublished', 'dateModified', 'image') if not article.get(key)]
            if missing:
                issue(f'article_fields_{i}', 'Article metadata', 'Article ' + str(i) + ' missing recommended fields: ' + ', '.join(missing), 'Add accurate values from the published post. Identify the author, use ISO 8601 dates, and supply a representative image. These are recommendations, not a guarantee of rich results.')

    headings = [dict(level=int(h.name[1]), text=h.get_text(' ', strip=True)) for h in soup.find_all(re.compile(r'^h[1-6]$'))]
    if any(b['level'] > a['level'] + 1 for a, b in zip(headings, headings[1:])):
        issue('heading_order', 'Skipped heading levels', 'The heading outline skips one or more levels.', 'Use a logical heading hierarchy (H1 → H2 → H3); choose visual size using CSS rather than heading level.')
    findings.sort(key=lambda f: f['priority'] != 'High')
    return dict(findings=findings, image_details=image_rows[:200], image_details_truncated=len(image_rows) > 200, heading_outline=headings[:100], page_type='Article / blog post' if is_blog else 'Web page', audit_scope='Source HTML only. Fetch time includes redirects and HTML transfer; it is not browser load time or TTFB. Image bytes, rendered layout, robots.txt and indexing are not verified. Use Core Web Vitals for browser performance.'), extra
