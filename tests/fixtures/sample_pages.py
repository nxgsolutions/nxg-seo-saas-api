"""Canned HTML pages shared across the scoring tests.

These exercise the genuine-score behaviour: a deep, well-built page must beat a
thin or blocked one, and blockers (noindex / no title / non-200 / severely thin)
must cap the overall score. Kept deliberately small and self-contained — the
scorers are pure functions over HTML, so no network is involved.
"""

# A long, genuinely useful blog post: unique title/meta, one H1, several H2/H3,
# real paragraphs (700+ words), a list, internal + external links, an image with
# alt text, and Article structured data. This is the "should score high" page.
_PARA = (
    "Brass diyas have lit Indian homes for centuries, and choosing the right one "
    "for Diwali comes down to metal purity, finish, and how the lamp sits in your "
    "space. A hand-finished brass lamp holds heat evenly, so the flame stays "
    "steady through a long evening of prayers and gatherings. "
)
RICH_BLOG = f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>How to Choose Brass Diyas for Diwali: A Complete 2026 Guide</title>
  <meta name="description" content="A practical guide to choosing brass diyas for Diwali: metal purity, finishes, sizes and care, with first-hand testing notes from our workshop in India.">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="canonical" href="https://www.peetalkraft.com/blog/choose-brass-diyas">
  <meta property="og:title" content="How to Choose Brass Diyas for Diwali">
  <meta property="og:image" content="https://www.peetalkraft.com/og.png">
  <script type="application/ld+json">{{"@context":"https://schema.org","@type":"BlogPosting","headline":"How to Choose Brass Diyas for Diwali","author":{{"@type":"Person","name":"Asha Rao"}},"datePublished":"2026-09-01","dateModified":"2026-10-01","image":"https://www.peetalkraft.com/og.png"}}</script>
</head>
<body>
  <main>
    <h1>How to Choose Brass Diyas for Diwali</h1>
    <p>{_PARA}</p>
    <h2>Why brass diyas still matter</h2>
    <p>{_PARA}</p>
    <p>{_PARA}</p>
    <h2>Metal purity and finish</h2>
    <p>{_PARA}</p>
    <h3>Antique vs mirror finish</h3>
    <p>{_PARA}</p>
    <ul>
      <li>Pick a diameter that matches your puja thali and shelf space.</li>
      <li>Look for a lacquer-free finish if you want a natural patina over time.</li>
      <li>Check the base weight so the lamp does not tip when filled with oil.</li>
    </ul>
    <h2>Caring for your diya</h2>
    <p>{_PARA}</p>
    <p>{_PARA}</p>
    <p>Read our <a href="/blog/clean-brass">guide to cleaning brass</a> and our
       <a href="/products/brass-diyas">brass diya collection</a> for more.
       The <a href="https://en.wikipedia.org/wiki/Diya_(lamp)">history of the diya</a>
       is worth a read too.</p>
    <img src="/hero.jpg" alt="A row of lit brass diyas on a festival table">
  </main>
</body>
</html>
"""

# A reasonable product page: shorter prose than a blog (that's fine for a
# product), with title/meta/H1, Product schema, one image with alt.
GOOD_PRODUCT = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Handcrafted Brass Diya Set of 5 | PeetalKraft</title>
  <meta name="description" content="A set of five hand-finished brass diyas for Diwali, cast and polished by artisans in Madhya Pradesh. Free delivery across India.">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="canonical" href="https://www.peetalkraft.com/products/brass-diya-set">
  <meta property="og:title" content="Brass Diya Set of 5">
  <meta property="og:image" content="https://www.peetalkraft.com/diya.png">
  <script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Brass Diya Set of 5"}</script>
</head>
<body>
  <main>
    <h1>Handcrafted Brass Diya Set of 5</h1>
    <p>This set of five brass diyas is cast and hand-polished by artisans in
       Madhya Pradesh. Each lamp holds enough oil for a full evening and carries a
       warm antique finish that deepens with use over the years. The set ships in
       protective packaging with a care card, and qualifies for free delivery
       across India. The brass is lead-free and safe for daily puja, and the wide
       base keeps each lamp steady when it is filled with oil and lit.</p>
    <p>Pair it with our brass pooja thali for a complete Diwali table. Every piece
       is checked by hand before it leaves the workshop, so no two lamps are
       exactly alike. The diyas measure about seven centimetres across, which fits
       most thalis and shelves comfortably, and they stack for easy storage once
       the festival is over. If you want a mirror finish instead of the antique
       patina, choose the polished variant at checkout. We also include simple
       instructions for seasoning and cleaning the brass so it keeps its glow.</p>
    <img src="/diya.jpg" alt="Set of five brass diyas">
  </main>
</body>
</html>
"""

# ~16 visible words of body — the "why is this 93?" case. Has a title and H1, so
# the ONLY blocker is severely thin content.
THIN_16_WORDS = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Contact Us | PeetalKraft</title>
  <meta name="description" content="Get in touch with the PeetalKraft team for orders, wholesale and support.">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="canonical" href="https://www.peetalkraft.com/contact">
</head>
<body>
  <main>
    <h1>Contact Us</h1>
    <p>Email us at hello@example.com and we will reply within one business day, thanks.</p>
  </main>
</body>
</html>
"""

# Same decent body as the product, but told not to index — a hard blocker.
NOINDEX_PAGE = GOOD_PRODUCT.replace(
    '<meta name="viewport" content="width=device-width, initial-scale=1">',
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<meta name="robots" content="noindex,follow">',
)

# A content-rich page that is missing its <title> tag entirely — a blocker.
EMPTY_TITLE = RICH_BLOG.replace(
    "<title>How to Choose Brass Diyas for Diwali: A Complete 2026 Guide</title>", ""
)

# Decent HTML, but served with a non-200 status (passed to analyze_html as 503).
NON_200 = GOOD_PRODUCT
