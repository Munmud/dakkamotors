"""robots.txt, sitemap.xml and llms.txt.

All three previously returned 403 - the S3 bucket is private, so a request for a file
that was never uploaded produced an AccessDenied XML document rather than anything
useful. Lighthouse scored the robots.txt check "not applicable" instead of failing it,
which is why the SEO score sat at 100 with no robots file at all.

Serving them from Django keeps the sitemap honest: it is generated from the same rows
the site renders, so it can never list a car that has been sold and removed.
"""

from django.http import HttpResponse

from . import seo
from .choices import CarStatus
from .store import cars as car_store


def robots_txt(request):
    lines = [
        "User-agent: *",
        "Allow: /",
        # Nothing here is useful to a crawler and the staff pages should never be
        # indexed. `/api/` below covers it; this line is kept because the intent is
        # worth stating where somebody reading robots.txt will see it.
        "Disallow: /api/staff/",
        "Disallow: /api/",
        # Sign-in and booking screens: nothing to index, and every URL under them is
        # personal to one customer.
        "Disallow: /account",
        "",
        # Answer engines look for this explicitly.
        f"Sitemap: {seo.SITE_URL}/sitemap.xml",
        "",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain; charset=utf-8")


def sitemap_xml(request):
    # Two Queries over the status partitions, not a Scan: a sitemap must never
    # advertise a car that has been sold, and `for_sitemap` is exactly that filter.
    cars = car_store.for_sitemap()

    out = []
    out.append('<?xml version="1.0" encoding="UTF-8"?>')
    out.append(
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:xhtml="http://www.w3.org/1999/xhtml">'
    )

    def entry(loc, lastmod=None, priority="0.8"):
        out.append("  <url>")
        out.append(f"    <loc>{loc}</loc>")
        if lastmod:
            out.append(f"    <lastmod>{lastmod:%Y-%m-%d}</lastmod>")
        out.append(f"    <priority>{priority}</priority>")
        # Declaring both languages tells search engines the pages are equivalent
        # translations rather than duplicates competing with each other.
        for code in seo.LANGUAGES:
            out.append(
                f'    <xhtml:link rel="alternate" hreflang="{code}" '
                f'href="{loc}?lang={code}"/>'
            )
        out.append("  </url>")

    entry(f"{seo.SITE_URL}/", priority="1.0")
    entry(f"{seo.SITE_URL}/request-a-car", priority="0.6")
    # Low, because nobody is searching for it -- but listed, because a crawler that
    # cannot find the privacy notice from the sitemap reports the site as not having
    # one, and Meta's advertisement review is one of the things that looks.
    entry(f"{seo.SITE_URL}/privacy", priority="0.2")
    for car in cars:
        entry(f"{seo.SITE_URL}{car.get_absolute_url()}", car.updated_at)

    out.append("</urlset>")
    return HttpResponse("\n".join(out), content_type="application/xml; charset=utf-8")


def llms_txt(request):
    """A plain-language brief for answer engines.

    Lighthouse's Agentic Browsing category checks for this, and ChatGPT, Perplexity and
    similar tools use it to understand a site without inferring everything from markup.
    Facts here are stated plainly and match the structured data exactly.
    """
    b = seo.BUSINESS
    cars = car_store.list_by_status(CarStatus.AVAILABLE, limit=50)

    lines = [
        f"# {b['name']} ({b['name_ja']})",
        "",
        f"> Used car dealer in {b['locality']}, {b['region']}, Japan. Kei cars and small "
        f"family vehicles, sold directly to buyers. Prices in JPY.",
        "",
        "## Contact",
        f"- Phone: {b['telephone_display']} (international: {b['telephone']})",
        f"- Address: {b['street_address']}, {b['locality']}, {b['region']} "
        f"{b['postal_code']}, Japan",
        "- Languages: English and Japanese",
        "",
        "## How buying works",
        "- Every listing is one specific vehicle on the lot, not a model or a trim level.",
        "- There is no online checkout and no enquiry form. Buyers phone the number "
        "above to ask about a car or arrange to see it.",
        '- A listing with no price shows "Call for price"; ring to ask.',
        "",
        "## Areas served",
        f"- {', '.join(seo.SERVICE_AREAS)}",
        "",
        "## Pages",
        f"- [All cars in stock]({seo.SITE_URL}/): the full current inventory",
    ]
    for car in cars:
        name = car.seo_title_plain
        price = "Call for price" if car.price_jpy is None else f"¥{car.price_jpy:,}"
        lines.append(f"- [{name}]({seo.SITE_URL}{car.get_absolute_url()}): {price}")

    lines += [
        "",
        "## Notes",
        "- Stock changes often; this file is generated from the live inventory.",
        "- Sold vehicles are removed from this list.",
        "",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain; charset=utf-8")
