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
import os
import pathlib
import re
import time

import boto3
from django.conf import settings
from django.http import Http404, HttpResponse

from . import seo
from . import specs
from .choices import CarStatus
from .store import cars as car_store
from .store.errors import NotFound
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


#: The same file on disk, for a laptop. `backend/cars/pages.py` -> repo root.
_LOCAL_SHELL = (pathlib.Path(__file__).resolve().parents[2]
                / "frontend" / "dist" / "index.html")


def _local_shell():
    """The locally built index.html, when there is no bucket to read.

    Only off Lambda, so production behaviour is unchanged -- the same gate
    `config/ssm.py` uses, and for the same reason.

    Without this a Django running on a laptop serves every page unstyled and with no
    app on it: `_fetch_shell` reaches S3 and only S3, so `asset_tags()` swallows the
    failure and returns "". The pages still carried their metadata, so nothing looked
    broken from the outside, and the result was that the one document production
    actually serves -- the server-rendered shell, with the app mounting over it --
    could not be opened or measured locally at all. Its CLS was found by measuring
    something else.
    """
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or not _LOCAL_SHELL.exists():
        return None
    return _LOCAL_SHELL.read_text(encoding="utf-8")


def asset_tags():
    """Just the <script>/<link> tags, pulled out of the built shell."""
    try:
        return "\n    ".join(_ASSET_TAG.findall(_fetch_shell()))
    except Exception:
        local = _local_shell()
        if local is not None:
            return "\n    ".join(_ASSET_TAG.findall(local))
        # A missing bundle must not take the site down; the page still carries its
        # metadata, which is what the crawler came for.
        logger.exception("Could not read the built frontend shell from S3")
        return ""


def _esc(value):
    return html.escape(str(value or ""), quote=True)


#: The picture a link to this site unfurls as, when the page has no better one of its
#: own. A car page does have one -- its first photo, which is what somebody sharing
#: that car means to show -- so this is the fallback, not the default: the home page,
#: the account and booking shells, and any car whose photos have not been added yet.
#:
#: Served from the frontend bucket through the `/assets/*` behaviour, and absolute
#: because a scraper resolves it against nothing.
SHARE_CARD = f"{seo.SITE_URL}/assets/share-card.jpg"


def _head(*, title, description, canonical, language, image=None, robots=None,
          structured_data=None, preload_image=None):
    # Every page unfurls with a picture. Before this, one without its own fell back to
    # `summary`, a card with no image at all -- the least clickable thing a link can
    # turn into in a chat window, which is where most of these are pasted.
    image = image or SHARE_CARD
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
    <meta name="twitter:card" content="summary_large_image" />
    <meta name="twitter:title" content="{_esc(title)}" />
    <meta name="twitter:description" content="{_esc(description)}" />"""
    tags += f"""
    <meta property="og:image" content="{_esc(image)}" />
    <meta name="twitter:image" content="{_esc(image)}" />"""
    if preload_image:
        # The car's first photo is the largest thing above the fold and therefore the
        # LCP element. Without this the browser cannot start fetching it until React
        # has downloaded, parsed and rendered -- three round trips after it could
        # have. `imagesrcset`/`imagesizes` so the preload picks the same file the
        # <picture> will, rather than fetching a second one.
        srcset, sizes = preload_image
        tags += f"""
    <link rel="preload" as="image" imagesrcset="{_esc(srcset)}"
          imagesizes="{_esc(sizes)}" fetchpriority="high" />"""
    if robots:
        tags += f"""
    <meta name="robots" content="{_esc(robots)}" />"""
    if structured_data:
        # Escaped exactly the way the initial-data block is. Until now every value in
        # the graph was typed by staff, so this was theoretical - but a customer's own
        # words go into the FAQ node, and "</script><img src=x onerror=...>" inside a
        # ld+json block is executable, on a public page, cached at the edge for minutes.
        encoded = json.dumps(
            structured_data, ensure_ascii=False, separators=(",", ":")
        ).replace("<", "\\u003c")
        tags += f'\n    <script type="application/ld+json">{encoded}</script>'
    return tags


def _pixel():
    """Meta's base pixel snippet, or nothing at all.

    Nothing is what every environment but production gets: `META_PIXEL_ID` is blank
    locally, under test and in the screenshot passes, so `window.fbq` never exists and
    `frontend/src/lib/pixel.js` -- which only ever calls it when it does -- is inert.
    The id is not a secret; it is in the page source of every site that runs a pixel.

    The snippet is Meta's, copied rather than rewritten, because it is what their
    documentation and their diagnostics both expect to find. The only change is that
    the id is checked to be digits before it goes in: it comes from an environment
    variable into a `<script>` block on a public page cached at the edge, and a
    quotation mark in it would be script injection on every page of the site. A
    misconfigured id is better as no pixel than as a broken page.

    `fbq('track', 'PageView')` here covers the first page load only. The app fires the
    rest, including a PageView per client-side navigation -- see lib/pixel.js.

    **It loads after the page does.** Meta's two files weigh about 773 KB between them
    -- `fbevents.js` plus the pixel config -- which is more than twice this site's
    JavaScript, for something no visitor benefits from. Their snippet is already
    `async`, but async only means "do not block parsing"; it still competes for
    bandwidth with the photograph somebody came to look at, on the 4G connection an
    advertisement click arrives over. Deferring to `load` (and `requestIdleCallback`
    where there is one) takes it off the critical path entirely.

    Nothing is lost by waiting. `fbq` queues calls made before the library arrives --
    that is what `n.queue` in the snippet is for -- so the app's own `ViewContent` and
    `Lead` events fire into the queue and flush when it lands.
    """
    pixel_id = getattr(settings, "META_PIXEL_ID", "")
    if not pixel_id:
        return ""
    if not str(pixel_id).isdigit():
        logger.error("META_PIXEL_ID is not numeric; no pixel rendered")
        return ""
    return f"""<script>
      // Meta's stub, verbatim: it defines `fbq` and queues anything called before the
      // real library arrives. Cheap, synchronous, and it has to exist before the app
      // mounts or early events are dropped on the floor.
      !function(f,b,e,v,n,t,s)
      {{if(f.fbq)return;n=f.fbq=function(){{n.callMethod?
      n.callMethod.apply(n,arguments):n.queue.push(arguments)}};
      if(!f._fbq)f._fbq=n;n.push=n;n.loaded=!0;n.version='2.0';
      n.queue=[];}}(window,document,'script');
      fbq('init', '{pixel_id}');
      fbq('track', 'PageView');
      // The 773 KB half, once the page has finished loading and the browser is idle.
      (function(){{
        var fired = false;
        function load() {{
          if (fired) return; fired = true;
          var t = document.createElement('script');
          t.async = true;
          t.src = 'https://connect.facebook.net/en_US/fbevents.js';
          document.head.appendChild(t);
        }}
        function schedule() {{
          if (window.requestIdleCallback) requestIdleCallback(load, {{timeout: 4000}});
          else setTimeout(load, 1200);
        }}
        if (document.readyState === 'complete') schedule();
        else window.addEventListener('load', schedule, {{once: true}});
      }})();
    </script>
    <noscript><img height="1" width="1" style="display:none" alt=""
      src="https://www.facebook.com/tr?id={pixel_id}&amp;ev=PageView&amp;noscript=1" /></noscript>"""


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
    <meta name="theme-color" content="#0f1519" />
    <link rel="icon" type="image/svg+xml" href="/plate.svg" />
    {head}
    {_pixel()}
    {asset_tags()}
  </head>
  <body>
    <div id="root">{body}</div>
    {payload}
  </body>
</html>
"""


def _language_from(request):
    """Japanese unless the URL asks for something else.

    `Accept-Language` is deliberately ignored. It used to decide here while the app
    decided from `navigator.language`, and the two disagreed often enough that a
    visitor could be served one language and watch it flip to the other the moment
    React mounted. One default, in two places, and they have to agree -- see
    `frontend/src/i18n/index.js`.

    The switcher still works and still remembers; this is only what a visitor gets
    before they have said anything. `?lang=en` is what the English hreflang alternate
    points at, so a crawler still reaches both.
    """
    requested = (request.GET.get("lang") or "").lower()
    return requested if requested in seo.LANGUAGES else "ja"


def _price_text(car, language):
    """The price as a reader meets it, labelled as the total.

    "支払総額" is carried here rather than only in the app because this string goes
    into the `<meta name="description">`, which is the line Google prints under the
    result. A Japanese buyer scanning a page of used-car listings is looking for
    exactly that word: almost every other shop quotes 車両本体価格 and adds the
    諸費用 at the desk, and this one does not. It is the shop's best sentence and
    it belongs where it is read before the click, not after it.
    """
    if car.price_jpy is None:
        return "価格はお問い合わせください" if language == "ja" else "Call for price"
    if language == "ja":
        return f"支払総額¥{car.price_jpy:,}"
    return f"¥{car.price_jpy:,} all in"


# --------------------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------------------


def home(request):
    language = _language_from(request)
    # One Query. The listing card's photo comes from the denormalised reference on the
    # car itself, which is what `prefetch_related("images")` used to buy.
    cars = car_store.stock(limit=24)

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

    # The first card's photograph, preloaded, for the same reason the car page does it:
    # it is the largest thing above the fold and therefore the LCP element, and
    # `CarCard` already marks it `priority`. Without this the fetch cannot start until
    # the bundle has downloaded and parsed, which is the whole wait it is trying to
    # avoid. `primary_image_ref` carries the widths, so this costs no extra read.
    #
    # `sizes` must stay identical to `CARD_SIZES` in `CarCard.jsx`, or the browser
    # preloads one file and the page then asks for a different one -- two downloads
    # and a slower paint than doing nothing at all.
    first_card = cars[0].primary_image if cars else None
    card_urls = first_card.derivative_urls if first_card else {}
    preload = ((", ".join(f"{url} {width}w" for width, url in sorted(card_urls.items())),
                "(max-width: 559px) 100vw, (max-width: 899px) 320px, "
                "(max-width: 1099px) 384px, 530px") if card_urls else None)

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
                preload_image=preload,
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
        # One Query returns the car, its photos and its videos together --
        # the reason this design is single-table, and the page most worth it: it is
        # server-rendered for crawlers and link previews on a possibly-cold Lambda.
        car = car_store.detail_by_slug(slug)
    except NotFound:
        raise Http404("No such car")

    canonical = f"{seo.SITE_URL}{car.get_absolute_url()}"
    price = _price_text(car, language)
    # The reader's name for the car in the title and description; the English one stays
    # in the JSON-LD below, where it is the identifier search engines match on.
    name = car.name_in(language)

    if language == "ja":
        color = car.color_ja or car.color
        title = f"{name}｜{seo.BUSINESS['locality_ja']}の中古車 ダッカモータース"
        description = (
            f"{name}（{car.get_fuel_type_display()}・{car.seat_capacity}人乗り"
            + (f"・{color}" if color else "")
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

    # Already loaded: the detail Query returned the photos with the car.
    images = car.images
    image_urls = []
    for image in images:
        urls = image.derivative_urls
        image_urls.append(urls[max(urls)] if urls else image.url)

    # The srcset the gallery will render, handed to the browser in <head> so the fetch
    # starts with the document rather than after the bundle. Same `sizes` string as
    # `Gallery.jsx`, and it has to stay the same or the browser preloads one file and
    # the page then asks for another.
    first = images[0].derivative_urls if images else {}
    preload = ((", ".join(f"{url} {width}w" for width, url in sorted(first.items())),
                "(max-width: 900px) 100vw, 700px") if first else None)

    # A sold car should stay reachable for anyone holding the link, but it should not
    # keep competing in search results against cars that can still be bought.
    robots = "noindex, follow" if car.status == CarStatus.SOLD else None

    # The nine fixed labels are English even on ?lang=ja. That is a pre-existing
    # inconsistency and fixing it is separate scope -- but the staff-typed rows below
    # are localised, because they carry a Japanese translation staff actually wrote and
    # ignoring it would be worse than the inconsistency.
    fixed = [
        ("Make", car.brand), ("Model", car.model_name), ("Grade", car.grade),
        ("Year", car.manufacture_year), ("Fuel", car.get_fuel_type_display()),
        ("Seats", car.seat_capacity), ("Colour", car.color),
        ("Model code", car.model_code),
        # No chassis number, matching the app's spec table. It stays in the JSON-LD
        # as `vehicleIdentificationNumber`, which is where a search engine wants it
        # and where no reader meets it -- but in the body of a public page it serves
        # no buyer, and it was the note a car with no free-form specs ended on.
    ]
    extra = [specs.localized(row, language) for row in (car.specs or [])]
    spec_rows = "".join(
        f"<li>{_esc(label)}: {_esc(value)}</li>"
        for label, value in fixed + extra
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
                preload_image=preload,
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


def request_car_page(request):
    """The one app route that is indexable: a person searching for a car the shop does
    not have listed is exactly who should find this page."""
    language = _language_from(request)
    if language == "ja":
        title = f"探している車をお伝えください｜{seo.BUSINESS['locality_ja']}の中古車 ダッカモータース"
        description = ("在庫にない車もお探しします。ご希望の車種・年式・ご予算をお知らせください。"
                       f"{seo.BUSINESS['region_ja']}{seo.BUSINESS['locality_ja']}のダッカモータース。")
        heading = "探している車をお伝えください"
    else:
        title = f"Ask us to find your car | Dakka Motors, {seo.BUSINESS['locality']}"
        description = ("Not seeing the car you want? Tell us the make, model, year and "
                       f"budget and Dakka Motors in {seo.BUSINESS['locality']} will look "
                       "for it.")
        heading = "Ask us to find your car"
    return HttpResponse(
        _render(
            language=language,
            head=_head(title=title, description=description,
                       canonical=f"{seo.SITE_URL}/request-a-car", language=language),
            body=f"<h1>{_esc(heading)}</h1><p>{_esc(description)}</p>",
        )
    )


def privacy_page(request):
    """The privacy notice, rendered rather than shelled.

    Indexable: Meta's review of an advertisement looks for this page, and a reviewer
    -- or anybody checking what a shop does with their details before handing them
    over -- should find it in a search rather than only from the footer.

    The body carries the real text, not just a heading, for the same reason the car
    pages do: a reader with no JavaScript, and every crawler that does not run any,
    would otherwise be shown a privacy page with no privacy notice in it. The copy
    lives in the locale files, so this is deliberately a short summary of it with the
    part that actually needs disclosing -- the pixel -- stated in full.
    """
    language = _language_from(request)
    if language == "ja":
        title = "プライバシー｜ダッカモータース"
        description = ("ダッカモータースが当サイトでお客様の情報をどのように取り扱うか。"
                       "FacebookおよびInstagramの広告に使用するMetaピクセルについての説明を含みます。")
        heading = "プライバシー"
        body_text = (
            "お問い合わせフォームでは、お名前とお電話番号（メールアドレスは任意）をお伺いし、"
            "折り返しのご連絡にのみ使用します。アカウントの作成は不要です。"
            "当サイトにはMetaピクセルを設置しており、閲覧されたページとお問い合わせに"
            "進まれたかどうかがMetaに送信されますが、お名前・お電話番号・メールアドレスが"
            "送信されることはありません。"
            f"ご不明な点は{seo.BUSINESS['telephone_display']}までお問い合わせください。"
        )
    else:
        title = "Privacy | Dakka Motors"
        description = ("What Dakka Motors does with your information on this site, "
                       "including the Meta pixel used for advertising on Facebook and "
                       "Instagram.")
        heading = "Privacy"
        body_text = (
            "The contact form asks for your name and phone number, and an email "
            "address if you want to give one. They are used to call you back about a "
            "car and nothing else. There are no accounts and no password. This site "
            "carries a Meta pixel, which tells Meta which pages were visited and "
            "whether somebody used the contact form; your name, phone number and "
            "email address are never sent to Meta. Ad personalisation can be turned "
            "off in your Facebook or Instagram settings. "
            f"Call {seo.BUSINESS['telephone_display']} with any question about this."
        )

    return HttpResponse(
        _render(
            language=language,
            head=_head(title=title, description=description,
                       canonical=f"{seo.SITE_URL}/privacy", language=language),
            body=f"<h1>{_esc(heading)}</h1><p>{_esc(body_text)}</p>",
        )
    )


