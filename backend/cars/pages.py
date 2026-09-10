"""Server-rendered HTML for the pages crawlers and link previews actually read.

The site is a React app, so until now every URL returned byte-identical HTML: one
generic `<title>Dakka Motors</title>` and an empty `<div id="root">`. Google renders
JavaScript and coped, but nothing else does. Sharing a car on WhatsApp showed no photo,
no price and no model; Bing and the answer engines saw an empty page.

Rather than adding a rendering framework, Django takes the **already-built**
`index.html` out of the frontend bucket and injects a real `<head>` plus a text summary
into it. That keeps one build of the app - the script tags always match whatever the
frontend workflow last deployed, with no manifest to keep in step - and React replaces
the summary the moment it mounts.
"""

import html
import json
import logging
import re
import time

import boto3
from django.conf import settings
from django.http import Http404, HttpResponse

from . import seo
from .models import Car, CarStatus
from .serializers import CarDetailSerializer, CarListSerializer

logger = logging.getLogger(__name__)

FRONTEND_BUCKET = "dakkamotors-frontend"
SHELL_KEY = "index.html"
#: Short, so a frontend deploy is picked up within a minute without redeploying Django.
SHELL_CACHE_SECONDS = 60

_shell_cache = {"fetched_at": 0.0, "html": None}

# Everything Vite emits into <head>: the module script and the stylesheet.
_ASSET_TAG = re.compile(
    r'<(?:script|link)\b[^>]*(?:src|href)="/assets/[^"]+"[^>]*>(?:</script>)?'
)


def _fetch_shell():
    """The deployed index.html, so asset hashes are always current."""
    now = time.monotonic()
    if _shell_cache["html"] and now - _shell_cache["fetched_at"] < SHELL_CACHE_SECONDS:
        return _shell_cache["html"]

    client = boto3.client("s3", region_name=getattr(settings, "AWS_S3_REGION_NAME", None))
    body = client.get_object(Bucket=FRONTEND_BUCKET, Key=SHELL_KEY)["Body"].read()
    _shell_cache.update(fetched_at=now, html=body.decode("utf-8"))
    return _shell_cache["html"]


def asset_tags():
    """Just the <script>/<link> tags, pulled out of the built shell."""
    try:
        return "\n    ".join(_ASSET_TAG.findall(_fetch_shell()))
    except Exception:
        # A missing bundle must not take the site down; the page still carries its
        # metadata, which is what the crawler came for.
        logger.exception("Could not read the built frontend shell from S3")
        return ""


def _esc(value):
    return html.escape(str(value or ""), quote=True)


def _head(*, title, description, canonical, language, image=None, robots=None,
          structured_data=None):
    alternates = "\n    ".join(
        f'<link rel="alternate" hreflang="{code}" href="{_esc(canonical)}?lang={code}" />'
        for code in seo.LANGUAGES
    )
    tags = f"""<title>{_esc(title)}</title>
    <meta name="description" content="{_esc(description)}" />
    <link rel="canonical" href="{_esc(canonical)}" />
    {alternates}
    <link rel="alternate" hreflang="x-default" href="{_esc(canonical)}" />
    <meta property="og:type" content="website" />
    <meta property="og:site_name" content="{_esc(seo.BUSINESS['name'])}" />
    <meta property="og:title" content="{_esc(title)}" />
    <meta property="og:description" content="{_esc(description)}" />
    <meta property="og:url" content="{_esc(canonical)}" />
    <meta property="og:locale" content="{'ja_JP' if language == 'ja' else 'en_US'}" />
    <meta name="twitter:card" content="{'summary_large_image' if image else 'summary'}" />
    <meta name="twitter:title" content="{_esc(title)}" />
    <meta name="twitter:description" content="{_esc(description)}" />"""
    if image:
        tags += f"""
    <meta property="og:image" content="{_esc(image)}" />
    <meta name="twitter:image" content="{_esc(image)}" />"""
    if robots:
        tags += f"""
    <meta name="robots" content="{_esc(robots)}" />"""
    if structured_data:
        tags += (
            '\n    <script type="application/ld+json">'
            + json.dumps(structured_data, ensure_ascii=False, separators=(",", ":"))
            + "</script>"
        )
    return tags


def _render(*, language, head, body, initial_data=None):
    """Assemble a complete document around the built asset tags.

    The body summary lives inside #root and is discarded by React on mount. It exists
    for the readers that never run JavaScript: link previews, Bing, and the answer
    engines.

    `initial_data` is the same payload the API would return, embedded so the app can
    paint real content on the first frame instead of a "Loading..." line that then
    reflows into the full page. That reflow was measuring 0.309 cumulative layout shift,
    well into Lighthouse's "poor" band; it also saves a round-trip.
    """
    payload = ""
    if initial_data is not None:
        # A JSON script block, so the browser never parses it as executable code and a
        # stray "</script>" in the data cannot break out.
        encoded = json.dumps(initial_data, ensure_ascii=False).replace("<", "\\u003c")
        payload = f'<script id="initial-data" type="application/json">{encoded}</script>'

    return f"""<!doctype html>
<html lang="{_esc(language)}">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <meta name="theme-color" content="#1B2430" />
    <link rel="icon" type="image/svg+xml" href="/plate.svg" />
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link href="https://fonts.googleapis.com/css2?family=Zen+Kaku+Gothic+New:wght@400;500;700;900&display=swap" rel="stylesheet" />
    {head}
    {asset_tags()}
  </head>
  <body>
    <div id="root">{body}</div>
    {payload}
  </body>
</html>
"""


def _language_from(request):
    requested = (request.GET.get("lang") or "").lower()
    if requested in seo.LANGUAGES:
        return requested
    accept = request.headers.get("Accept-Language", "").lower()
    return "ja" if accept.startswith("ja") else "en"


def _price_text(car, language):
    if car.price_jpy is None:
        return "価格はお問い合わせください" if language == "ja" else "Call for price"
    return f"¥{car.price_jpy:,}"


# --------------------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------------------


def home(request):
    language = _language_from(request)
    cars = list(
        Car.objects.filter(status=CarStatus.AVAILABLE)
        .prefetch_related("images")[:24]
    )

    if language == "ja":
        title = f"{seo.BUSINESS['region_ja']}{seo.BUSINESS['locality_ja']}の中古車販売｜ダッカモータース"
        description = (
            f"{seo.BUSINESS['region_ja']}{seo.BUSINESS['locality_ja']}の中古車販売店。"
            f"軽自動車を中心に在庫{len(cars)}台。"
            f"お電話（{seo.BUSINESS['telephone_display']}）でお気軽にお問い合わせください。"
        )
    else:
        title = f"Used Cars in {seo.BUSINESS['locality']}, {seo.BUSINESS['region']} | Dakka Motors"
        description = (
            f"Used car dealer in {seo.BUSINESS['locality']}, {seo.BUSINESS['region']}. "
            f"{len(cars)} vehicle(s) in stock, kei cars a speciality. "
            f"Call {seo.BUSINESS['telephone_display']} to arrange a viewing."
        )

    listing = {
        "@type": "ItemList",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": i,
                "url": f"{seo.SITE_URL}{car.get_absolute_url()}",
                "name": car.seo_title_plain,
            }
            for i, car in enumerate(cars, start=1)
        ],
    }

    rows = "".join(
        f'<li><a href="{_esc(car.get_absolute_url())}">'
        f"<h2>{_esc(car.seo_title_plain)}</h2>"
        f"<p>{_esc(_price_text(car, language))}</p></a></li>"
        for car in cars
    )
    body = (
        f"<h1>{_esc(title)}</h1><p>{_esc(description)}</p>"
        f"<ul>{rows}</ul>"
        f'<p><a href="tel:{_esc(seo.BUSINESS["telephone"])}">'
        f'{_esc(seo.BUSINESS["telephone_display"])}</a></p>'
    )

    return HttpResponse(
        _render(
            language=language,
            head=_head(
                title=title,
                description=description,
                canonical=f"{seo.SITE_URL}/",
                language=language,
                structured_data=seo.graph(
                    seo.dealer_schema(), seo.website_schema(), listing
                ),
            ),
            body=body,
            initial_data={
                "kind": "home",
                "data": {
                    "count": len(cars),
                    "next": None,
                    "results": CarListSerializer(cars, many=True).data,
                },
            },
        )
    )


def car_detail(request, slug):
    language = _language_from(request)
    try:
        car = Car.objects.prefetch_related("images").get(slug=slug)
    except Car.DoesNotExist:
        raise Http404("No such car")

    canonical = f"{seo.SITE_URL}{car.get_absolute_url()}"
    price = _price_text(car, language)
    name = car.seo_title_plain

    if language == "ja":
        title = f"{name}｜{seo.BUSINESS['locality_ja']}の中古車 ダッカモータース"
        description = (
            f"{name}（{car.get_fuel_type_display()}・{car.seat_capacity}人乗り"
            + (f"・{car.color}" if car.color else "")
            + f"）{price}。{seo.BUSINESS['locality_ja']}の中古車販売ダッカモータース。"
            f"お問い合わせは{seo.BUSINESS['telephone_display']}。"
        )
    else:
        title = f"{name} for sale in {seo.BUSINESS['locality']} | Dakka Motors"
        description = (
            f"{name} — {price}. {car.get_fuel_type_display()}, "
            f"{car.seat_capacity} seats"
            + (f", {car.color}" if car.color else "")
            + f". Used car for sale in {seo.BUSINESS['locality']}, "
            f"{seo.BUSINESS['region']}. Call {seo.BUSINESS['telephone_display']}."
        )

    images = list(car.images.all())
    image_urls = []
    for image in images:
        urls = image.derivative_urls
        image_urls.append(urls[max(urls)] if urls else image.image.url)

    # A sold car should stay reachable for anyone holding the link, but it should not
    # keep competing in search results against cars that can still be bought.
    robots = "noindex, follow" if car.status == CarStatus.SOLD else None

    spec_rows = "".join(
        f"<li>{_esc(label)}: {_esc(value)}</li>"
        for label, value in [
            ("Make", car.brand), ("Model", car.model_name), ("Grade", car.grade),
            ("Year", car.manufacture_year), ("Fuel", car.get_fuel_type_display()),
            ("Seats", car.seat_capacity), ("Colour", car.color),
            ("Model code", car.model_code), ("Chassis number", car.chassis_number),
        ]
        if value
    )
    description_text = (car.description_ja if language == "ja" else car.description_en) or ""
    body = (
        f"<h1>{_esc(name)}</h1><p>{_esc(price)}</p>"
        f"<ul>{spec_rows}</ul>"
        f"<p>{_esc(description_text)}</p>"
        f'<p><a href="tel:{_esc(seo.BUSINESS["telephone"])}">'
        f'{_esc(seo.BUSINESS["telephone_display"])}</a></p>'
        f'<p><a href="/">{_esc("在庫一覧" if language == "ja" else "All cars")}</a></p>'
    )

    return HttpResponse(
        _render(
            language=language,
            head=_head(
                title=title,
                description=description,
                canonical=canonical,
                language=language,
                image=image_urls[0] if image_urls else None,
                robots=robots,
                structured_data=seo.graph(
                    seo.dealer_schema(),
                    seo.vehicle_schema(car, image_urls, canonical),
                    seo.breadcrumb_schema(
                        [("Home", f"{seo.SITE_URL}/"), (name, canonical)]
                    ),
                ),
            ),
            body=body,
            initial_data={
                "kind": "car",
                "key": car.slug,
                "data": CarDetailSerializer(car).data,
            },
        )
    )


def app_shell(request, title, description):
    """A rendered shell for routes that are pure app: accounts, booking.

    They still need real HTML or the URL 404s, but they carry noindex - an account page
    has no business in search results - and nothing user-specific, because these
    responses are cached at the CDN for everyone alike.
    """
    language = _language_from(request)
    return HttpResponse(
        _render(
            language=language,
            head=_head(
                title=title,
                description=description,
                canonical=f"{seo.SITE_URL}{request.path}",
                language=language,
                robots="noindex, nofollow",
            ),
            body=f"<h1>{_esc(title)}</h1>",
        )
    )


def account_page(request, rest=None):
    """Every /account/... route renders the same shell; the app routes within it."""
    return app_shell(
        request,
        "Your account | Dakka Motors",
        "Sign in to book or manage a test drive at Dakka Motors.",
    )


def book_test_drive_page(request, slug):
    car = Car.objects.filter(slug=slug).first()
    name = car.seo_title_plain if car else "a car"
    return app_shell(
        request,
        f"Book a test drive - {name} | Dakka Motors",
        f"Choose a time to test drive the {name} at Dakka Motors in "
        f"{seo.BUSINESS['locality']}.",
    )
